"""One local Continuum contract, executed through the VoiceOS orchestrator."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from time import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from actionmesh.agent.apps import MAC_APPS
from actionmesh.agent.processes import CONTINUUM_ARGUMENTS, CONTINUUM_NAME, CONTINUUM_PORT
from actionmesh.core.permissions.context import PolicyContext
from actionmesh.voiceos.models import TaskSession, TaskStep
from actionmesh.voiceos.orchestrator import TERMINAL_STATES, Orchestrator


class GoalContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=120)
    invariant: Literal["server_health", "app_running", "file_exists"] = "server_health"
    resource: str = Field(default="", max_length=240)
    expected: bool = True

    @model_validator(mode="after")
    def validate_resource(self) -> GoalContract:
        if self.invariant == "app_running" and self.resource not in MAC_APPS:
            raise ValueError("Choose an allowlisted Mac app")
        if self.invariant == "file_exists" and not self.resource.strip():
            raise ValueError("Provide a workspace-relative file path")
        if self.invariant == "server_health" and (self.resource or not self.expected):
            raise ValueError("Server goals target the fixed demonstration service")
        return self

    interval: int = Field(default=5, ge=3, le=60)
    max_attempts: int = Field(default=2, ge=1, le=3)
    enabled: bool = True


class Continuum:
    def __init__(
        self, database: str, orchestrator: Orchestrator,
        context: Callable[[], PolicyContext], device: Callable[[], str],
    ) -> None:
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS continuum (id INTEGER PRIMARY KEY, body TEXT)")
        self.orchestrator = orchestrator
        self.context = context
        self.device = device
        self.lock = asyncio.Lock()

    def read(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT body FROM continuum WHERE id = 1").fetchone()
        return GoalState.model_validate_json(row[0]).model_dump() if row else None

    def save(self, goal: GoalState) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO continuum VALUES (1, ?)", (goal.model_dump_json(),))

    def event(self, goal: GoalState, state: str, detail: str, execution_id: str = "") -> None:
        goal.state = state
        entry = {"at": datetime.now(UTC).isoformat(), "state": state,
                 "detail": detail, "execution_id": execution_id}
        if not goal.events or any(goal.events[-1][key] != entry[key]
                                  for key in ("state", "detail")):
            goal.events = [*goal.events[-39:], entry]
        self.save(goal)

    async def run_action(self, goal: GoalState, action: str, params: dict[str, Any]) -> TaskStep:
        step = TaskStep(index=0, label=action, action_type=action, parameters=params)
        session = TaskSession(
            session_id=uuid4().hex, actor_id=self.context().actor.id,
            request=goal.contract.description, device_id=goal.device_id, steps=[step],
        )

        def checkpoint(current: TaskSession) -> None:
            if action == "device.server.start":
                goal.pending = current.steps[0].execution_id
                self.save(goal)
            elif action == "device.server.stop":
                goal.stop_pending = current.steps[0].execution_id
                self.save(goal)

        await self.orchestrator.advance(
            session, self.context(), stop_on_unverified=True, checkpoint=checkpoint,
        )
        return step

    async def tick(self) -> None:
        async with self.lock:
            saved = self.read()
            if saved is None:
                return
            goal = GoalState.model_validate(saved)
            if not goal.contract.enabled or goal.device_id != self.device():
                return
            if goal.stop_pending:
                record = self.orchestrator.engine.record(goal.stop_pending)
                if record is None or record.state not in TERMINAL_STATES:
                    return
                self.event(goal, goal.state, f"Stop execution: {record.state.value}", goal.stop_pending)
                goal.stop_pending = ""
                self.save(goal)
            if goal.contract.invariant != "server_health":
                action, parameter, field = (
                    ("device.app.status_native", "app", "running")
                    if goal.contract.invariant == "app_running"
                    else ("device.files.info", "path", "exists")
                )
                step = await self.run_action(goal, action, {parameter: goal.contract.resource})
                goal.observed_at = datetime.now(UTC).isoformat()
                actual = step.observed.get(field)
                if step.verified is not True or type(actual) is not bool:
                    goal.observation = "UNKNOWN"
                    goal.strategy = "Wait for verified evidence."
                    self.event(goal, "UNKNOWN", step.error or "Observation unavailable", step.execution_id)
                    return
                healthy = actual is goal.contract.expected
                goal.observation = "VERIFIED" if healthy else "VIOLATED"
                if healthy:
                    goal.last_verified = goal.observed_at
                goal.strategy = f"Observed {field}={actual}. This goal is observation only."
                self.event(goal, "HEALTHY" if healthy else "VIOLATED", goal.strategy, step.execution_id)
                return
            recovering = False
            if goal.pending:
                record = self.orchestrator.engine.record(goal.pending)
                if record is None:
                    self.event(goal, "UNKNOWN", "Prior repair record unavailable. Operator review required.")
                    return
                if record.state not in TERMINAL_STATES:
                    return
                recovering = record.state.value == "success"
                self.event(goal, "VERIFYING" if recovering else "REPAIR_FAILED",
                           f"Repair execution: {record.state.value}", goal.pending)
                goal.pending = ""
                self.save(goal)
            step = await self.run_action(goal, "device.server.status", {"name": CONTINUUM_NAME})
            rows = step.observed.get("servers", [])
            live = [row for row in rows if row.get("state") == "running"]
            goal.observed_at = datetime.now(UTC).isoformat()
            if step.verified is not True:
                goal.observation = "UNKNOWN"
                goal.strategy = "Wait for verified evidence."
                self.event(goal, "UNKNOWN", step.error or "No verified server observation", step.execution_id)
                return
            if len(live) == 1 and live[0].get("ready") is True:
                goal.observation = "VERIFIED"
                goal.last_verified = goal.observed_at
                goal.attempts = 0
                goal.retry_after = 0
                goal.strategy = "No action. The managed service answered HTTP 200."
                state = "RECOVERED" if recovering or goal.state == "RECOVERED" else "HEALTHY"
                self.event(goal, state, goal.strategy, step.execution_id if recovering else "")
                return
            goal.observation = "VIOLATED"
            if live or step.observed.get("listening") is not False:
                goal.strategy = "Observe again. A managed process is active or the port is occupied."
                self.event(goal, "DEGRADED", goal.strategy)
                return
            if goal.attempts >= goal.contract.max_attempts:
                self.event(goal, "REPAIR_FAILED", "Attempt budget exhausted.")
                return
            if recovering:
                self.event(goal, "REPAIR_FAILED", "The service did not pass its HTTP check after repair.")
                return
            if time() < goal.retry_after:
                return
            self.event(goal, "VIOLATED", "The dedicated service is stopped and its port is free.",
                       step.execution_id)
            goal.strategy = "Start the dedicated managed server, then observe its HTTP response again."
            goal.attempts += 1
            goal.retry_after = time() + goal.contract.interval
            self.event(goal, "REPAIR_PROPOSED", goal.strategy, step.execution_id)
            start = await self.run_action(goal, "device.server.start", {
                "name": CONTINUUM_NAME, "command": "python3", "port": CONTINUUM_PORT,
                "arguments": list(CONTINUUM_ARGUMENTS),
            })
            goal.pending = start.execution_id if start.state in {"running", "awaiting_approval", "succeeded"} else ""
            self.event(goal, {
                "awaiting_approval": "AWAITING_APPROVAL", "running": "REPAIRING",
                "succeeded": "VERIFYING",
            }.get(start.state, "REPAIR_FAILED"), start.error or start.state, start.execution_id)

    async def monitor(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            saved = self.read()
            try:
                await asyncio.wait_for(stop.wait(), timeout=saved["contract"]["interval"] if saved else 3)
            except TimeoutError:
                try:
                    await self.tick()
                except Exception:
                    logging.getLogger(__name__).exception("Continuum monitoring check failed")
                    current = self.read()
                    if current and current["contract"]["enabled"]:
                        goal = GoalState.model_validate(current)
                        goal.observation = "ERROR"
                        goal.strategy = "Review the monitoring error before retrying."
                        self.event(goal, "UNKNOWN", "Observation cycle failed.")


class GoalState(BaseModel):
    contract: GoalContract
    goal_id: str = Field(default_factory=lambda: uuid4().hex)
    revision: int = 1
    actor_id: Literal["voiceos-local"] = "voiceos-local"
    device_id: str
    state: str = "INITIALIZING"
    observation: str = "UNKNOWN"
    observed_at: str = ""
    last_verified: str = ""
    strategy: str = ""
    attempts: int = 0
    pending: str = ""
    retry_after: float = 0
    stop_pending: str = ""
    events: list[dict[str, str]] = Field(default_factory=list)
