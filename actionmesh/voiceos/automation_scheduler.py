"""The local scheduler: a tick, a claim, and no cloud anywhere.

There is no daemon, no queue service and no external scheduler. One ``asyncio`` task
lives inside this process and, on each tick, asks the durable store which workflows are
due. That is the whole design, and it has exactly one hard property to protect:

**An occurrence runs at most once, across restarts.** Two mechanisms, both durable:

1. ``next_run_at`` is stored, and a run is scheduled from it. Advancing it is part of
   the same step that claims the occurrence, so a workflow whose run is claimed is no
   longer due.
2. The run row's ``(workflow_id, slot)`` uniqueness is the real guard. ``store.claim``
   is an ``INSERT OR IGNORE``; only the caller whose insert took effect proceeds. A
   second ticker — a second process, or this one after a crash and restart — computes
   the same slot and is told the slot is taken.

The startup reconciliation closes the remaining window: an occurrence claimed by a
process that died before it could start is marked ``missed``, once, and never re-run.
Re-running it would be a run at a time nobody chose, so "missed" is the honest record
— and it is what the user is shown in the history.

Event triggers use a different path but the same claim: the slot is the event's own id,
so one event can start a workflow once no matter how many subscribers see it.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from actionmesh.core.permissions.context import PolicyContext
from actionmesh.voiceos.automation_run import Runner, run_state_for, schedule_next, slot_for_schedule
from actionmesh.voiceos.automation_store import AutomationStore
from actionmesh.voiceos.models import _now
from actionmesh.voiceos.workflows import (
    AutomationRun,
    Workflow,
    new_run_id,
    parse_when,
)

#: How often the ticker looks for due work. A minute is fine because no schedule is
#: allowed below that (``MIN_INTERVAL_S``); a faster tick would only spin.
TICK_SECONDS = 30.0

#: How late an occurrence may be claimed before it is recorded ``missed`` instead of
#: run. A laptop that slept through 09:00 and woke at 15:00 must not silently run the
#: morning's job six hours late — the user is told it was missed. A short grace window
#: absorbs ordinary scheduling jitter and a slow first tick.
MISSED_AFTER_S = 15 * 60

#: Most workflows one tick will start. A large backlog is not a reason to peg a core.
MAX_RUNS_PER_TICK = 5

#: Event types whose publication is allowed to start a workflow. A closed list, on the
#: same principle as every other closed list in this product: an event that is not
#: named here cannot trigger automation, so a future event type cannot silently become
#: one. These are all *outcomes* the control plane already publishes; none of them is a
#: request a caller can forge by publishing. The list is the executor's own — there is
#: deliberately no ``execution.failed``, because the engine reports a failed node inside
#: ``execution.completed`` with ``state: failed``, which :meth:`Scheduler.on_event`
#: matches through the trigger's optional ``state``.
TRIGGERABLE_EVENTS = frozenset(
    {
        "execution.completed",
        "execution.denied",
        "execution.device_refused",
        "execution.cancelled",
        "execution.interrupted",
    }
)


class Scheduler:
    """A local ticker over the durable store. Not a service, not an engine."""

    def __init__(
        self,
        *,
        store: AutomationStore,
        runner: Runner,
        context_for: Callable[[str], PolicyContext] | None = None,
        tick_seconds: float = TICK_SECONDS,
        missed_after_s: float = MISSED_AFTER_S,
    ) -> None:
        self.store = store
        self.runner = runner
        #: Builds the context for a manual press by a named actor. The scheduler never
        #: calls it; the API route does, so the two cannot drift.
        self.context_for = context_for
        self.tick_seconds = float(tick_seconds)
        self.missed_after_s = float(missed_after_s)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        #: Serialises the ticker's own runs. A scheduled run and an event run may both
        #: be in flight; the engine is fine with that, but this keeps the ordering of
        #: one workflow's occurrences predictable.
        self._lock = asyncio.Lock()
        #: Recently started run ids, so a caller asking "what just fired?" has an
        #: answer that outlives the row it reads.
        self._recent: deque[str] = deque(maxlen=50)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the ticker. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="automations-scheduler")

    async def stop(self) -> None:
        """Stop the ticker and wait for it. Safe to call when it never started."""
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    def reconcile(self) -> list[AutomationRun]:
        """Startup: mark abandoned claims ``missed``, and re-arm schedules.

        The second half matters as much as the first. ``next_run_at`` is what the ticker
        scans; a workflow whose next run passed while this program was not running must
        be moved forward here, or the first tick after every restart would see it as
        due — and, worse, a workflow with a stale timestamp would stay stale forever if
        the ticker chose to skip rather than claim it.
        """
        missed = self.store.reconcile_claimed()
        now = datetime.now(UTC)
        for workflow in self.store.list_workflows(limit=1000):
            if not workflow.schedule or not workflow.enabled:
                continue
            due = parse_when(workflow.next_run_at)
            if due is not None and due > now:
                continue
            # Future-dated or missing: recompute from now. A due-in-the-past workflow
            # is left alone for the ticker, which records it missed and moves it.
            if due is None:
                schedule_next(workflow, now=now)
                self.store.save_workflow(workflow)
        return missed

    # -- the loop ----------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not end the loop
                # Deliberately swallowed: a scheduler that dies on the first error is
                # indistinguishable from one that works, because nothing announces its
                # silence. The next tick retries; the durable state is unchanged.
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                continue

    async def tick(self, *, now: datetime | None = None) -> list[AutomationRun]:
        """Run every due occurrence, once. Returns the runs this tick started."""
        moment = now or datetime.now(UTC)
        started: list[AutomationRun] = []
        async with self._lock:
            for workflow in self.store.due_workflows(moment.isoformat()):
                if len(started) >= MAX_RUNS_PER_TICK:
                    break
                run = await self._run_due(workflow, moment)
                if run is not None:
                    started.append(run)
        return started

    async def _run_due(self, workflow: Workflow, now: datetime) -> AutomationRun | None:
        due = parse_when(workflow.next_run_at)
        if due is None:
            # A row the ticker cannot interpret is not silently deleted: recompute and
            # save, so the next tick has a real timestamp. Nothing runs this tick.
            schedule_next(workflow, now=now)
            self.store.save_workflow(workflow)
            return None
        slot = slot_for_schedule(workflow, due, now)
        late = (now - due).total_seconds()
        run = AutomationRun(
            run_id=new_run_id(),
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.name,
            actor_id=workflow.actor_id,
            slot=slot,
            trigger="schedule",
            trigger_detail={"due_at": workflow.next_run_at},
            scheduled_for=workflow.next_run_at,
            created_at=_now(),
        )
        if late > self.missed_after_s:
            # Overdue by more than the grace window: the occurrence was due while this
            # program was not running, and is recorded as missed rather than run now.
            run.state = "missed"
            run.error = (
                f"the occurrence due at {workflow.next_run_at} was missed by "
                f"{int(late)}s; it was not run late"
            )
            run.finished_at = _now()
            if not self.store.claim(run):
                return None
            self._advance(workflow, now)
            return run
        if not self.store.claim(run):
            # Another ticker — or this one before a restart — owns the slot. Moving the
            # workflow on would let a second occurrence be claimed for the same stored
            # due time by this caller, so the schedule is advanced by whoever *did*
            # claim it, and this caller does nothing at all.
            return None
        # Claimed. Advance the schedule *before* running, so a crash mid-run cannot
        # leave the same due time in place for a restart to claim again.
        self._advance(workflow, now)
        self._recent.append(run.run_id)
        await self.runner.execute(workflow, run, save_run=self.store.save_run)
        self._finish(workflow, run)
        return run

    def _advance(self, workflow: Workflow, now: datetime) -> None:
        workflow.last_run_at = now.isoformat()
        schedule_next(workflow, now=now)
        workflow.touch()
        self.store.save_workflow(workflow)

    def _finish(self, workflow: Workflow, run: AutomationRun) -> None:
        workflow.last_run_id = run.run_id
        workflow.last_state = run.state
        workflow.last_run_at = run.started_at or run.created_at
        workflow.touch()
        self.store.save_workflow(workflow)

    # -- event triggers ----------------------------------------------------

    async def on_event(
        self,
        event_type: str,
        *,
        event_id: str,
        actor: str = "",
        state: str = "",
        occurred_at: str = "",
    ) -> list[AutomationRun]:
        """Start every workflow triggered by ``event_type``, at most once per event.

        ``event_id`` is the slot, so a redelivered event — or a second subscriber —
        cannot start a second run. Only the event's *type* and its recorded outcome
        ``state`` are matched, never its payload: an event payload is data an execution
        produced, and deriving a trigger from it would let a provider's output choose
        what runs next. ``state`` is the engine's own verdict on the record, which is
        why "when a build fails" is ``execution.completed`` narrowed to
        ``state: failed`` rather than a fabricated ``execution.failed`` event.
        """
        if event_type not in TRIGGERABLE_EVENTS:
            return []
        started: list[AutomationRun] = []
        async with self._lock:
            for workflow in self.store.list_workflows(limit=1000):
                if not workflow.enabled or not workflow.event:
                    continue
                if workflow.event.get("event_type") != event_type:
                    continue
                wanted = str(workflow.event.get("state") or "")
                if wanted and wanted != state:
                    # The trigger named an outcome; this event carried a different one.
                    continue
                if workflow.actor_id != actor:
                    # A trigger fires for its own owner's workflows only. An event from
                    # another actor must not be able to start someone else's workflow,
                    # which would be one actor setting another's machine going.
                    continue
                run = AutomationRun(
                    run_id=new_run_id(),
                    workflow_id=workflow.workflow_id,
                    workflow_name=workflow.name,
                    actor_id=workflow.actor_id,
                    slot=f"event:{event_id}",
                    trigger="event",
                    trigger_detail={
                        "event_type": event_type,
                        "event_id": event_id,
                        "state": state,
                        "occurred_at": occurred_at,
                    },
                    created_at=_now(),
                )
                if not self.store.claim(run):
                    continue
                self._recent.append(run.run_id)
                await self.runner.execute(workflow, run, save_run=self.store.save_run)
                self._finish(workflow, run)
                started.append(run)
        return started

    # -- manual ------------------------------------------------------------

    async def run_now(
        self,
        workflow: Workflow,
        *,
        context: PolicyContext | None = None,
        default_device_id: str = "",
    ) -> AutomationRun:
        """A manual press. Not suppressed by a slot, because a person asked twice.

        ``context`` is the caller's real one when the request carried a credential;
        ``None`` means the local operator, whose identity is the workflow's owner.

        ``default_device_id`` is the target the *caller* resolved for a plan that names
        none — the route decides that from where the press came, so this layer is not
        guessing; it is carrying a decision. Empty leaves the step unaimed, which the
        orchestrator then refuses rather than redirecting.
        """
        run = AutomationRun(
            run_id=new_run_id(),
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.name,
            actor_id=workflow.actor_id,
            # A fresh id each press. The slot constraint exists to stop one *occurrence*
            # running twice, not to stop a human pressing run twice.
            slot=f"manual:{new_run_id()}",
            trigger="manual",
            trigger_detail={"requested_at": _now()},
            created_at=_now(),
        )
        if not self.store.claim(run):
            # Only reachable with an astronomical collision on a random id.
            raise RuntimeError("this run id was already claimed")
        self._recent.append(run.run_id)
        await self.runner.execute(
            workflow,
            run,
            context=context,
            save_run=self.store.save_run,
            default_device_id=default_device_id,
        )
        self._finish(workflow, run)
        return run

    # -- read-side ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """What the AUTOMATIONS page needs to say whether the ticker is alive."""
        running = self._task is not None and not self._task.done()
        return {
            "running": running,
            "tick_seconds": self.tick_seconds,
            "missed_after_s": self.missed_after_s,
            "recent_runs": list(self._recent),
            "triggerable_events": sorted(TRIGGERABLE_EVENTS),
        }

    @staticmethod
    def next_due(workflow: Workflow) -> str:
        """The stored due time, or a recomputation when it is missing."""
        if not workflow.schedule or not workflow.enabled:
            return ""
        due = parse_when(workflow.next_run_at)
        if due is not None and due > datetime.now(UTC):
            return workflow.next_run_at
        upcoming = schedule_next_dry(workflow)
        return upcoming

    @staticmethod
    def missed_cutoff(now: datetime | None = None) -> str:
        moment = now or datetime.now(UTC)
        return (moment - timedelta(seconds=MISSED_AFTER_S)).isoformat()


def schedule_next_dry(workflow: Workflow) -> str:
    """The next occurrence a schedule would have, without writing anything."""
    from actionmesh.voiceos.workflows import next_occurrence

    upcoming = next_occurrence(workflow.schedule, datetime.now(UTC))
    return upcoming.isoformat() if upcoming is not None else ""


def summarize_run(run: AutomationRun) -> str:
    """One sentence for the history list."""
    if run.state == "missed":
        return f"{run.workflow_name!r} was due but did not run: {run.error}"
    if run.state == "awaiting_approval":
        return f"{run.workflow_name!r} is waiting for your approval before it continues."
    if run.state == "succeeded":
        return f"{run.workflow_name!r} ran and every step succeeded."
    if run.state == "partial":
        return f"{run.workflow_name!r} ran, but not every step succeeded or verified."
    if run.state in {"failed", "denied"}:
        return f"{run.workflow_name!r} did not complete: {run.error or run.state}"
    if run.state == "cancelled":
        return f"{run.workflow_name!r} was cancelled."
    if run.state == "interrupted":
        return f"{run.workflow_name!r} was interrupted and its outcome is unknown."
    return f"{run.workflow_name!r} is {run.state}."


__all__ = [
    "MAX_RUNS_PER_TICK",
    "MISSED_AFTER_S",
    "TICK_SECONDS",
    "TRIGGERABLE_EVENTS",
    "Scheduler",
    "run_state_for",
    "schedule_next_dry",
    "summarize_run",
]