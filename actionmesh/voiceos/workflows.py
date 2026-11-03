"""Automations: a stored plan, a trigger, and no authority of its own.

An automation is the durable form of a task session — the same steps, kept so they can
be proposed again later without the user retyping the sentence. What it is **not** is a
grant. The model here deliberately has no field for a permission, a risk level, an
approval, a credential or a policy: creating an automation, editing one, or scheduling
one confers nothing, and a run is only ever a fresh proposal that goes through the same
registry, consent, risk and approval gate as a sentence typed a second ago.

Three rules from the work order are structural rather than procedural:

* **A condition is evidence, never a claim.** ``when`` may only name a key in
  :data:`actionmesh.voiceos.tasks.CONDITION_KEYS` on a step that is strictly earlier in
  the same run. There is no expression language and no model in the path, so "if the
  tests passed" can only mean a recorded observation that says so.
* **A run is named before it happens, and its name is its claim.** Each occurrence has a
  slot — the due time, or an event id — and the store refuses a second row for the same
  slot. That is what makes a restart unable to run the same occurrence twice.
* **A device is aimed at, never redirected.** A step may name a computer; if it is
  offline the step is *unavailable*, and nothing is sent anywhere else.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from actionmesh.voiceos.models import _now
from actionmesh.voiceos.tasks import CONDITION_KEYS, MAX_STEPS

#: What a trigger can be. A workflow has at most one of each.
TRIGGER_KINDS = ("manual", "schedule", "event")

#: The schedule shapes this program understands, and all it understands. A general
#: cron dialect would be a second language to validate and to get wrong; three shapes
#: cover "every N minutes", "once a day" and "weekly", in the user's own local time.
SCHEDULE_KINDS = ("interval", "daily", "weekly")

#: The event types a stored workflow may name as a trigger. A closed list, and closed
#: for a reason: an event trigger is a promise that something will publish that event.
#: Only the types the executor actually publishes are here. There is deliberately no
#: ``execution.failed`` — the executor reports a failed node inside
#: ``execution.completed`` (whose payload carries ``state``), so a workflow triggered on
#: a type that is never published would be a feature that silently never fires.
DEFAULT_EVENT_TYPES = (
    "execution.completed",
    "execution.denied",
    "execution.device_refused",
    "execution.cancelled",
    "execution.interrupted",
)

#: The outcome states an ``execution.completed`` event can carry, copied from
#: :class:`actionmesh.protocol.models.ActionState`. Spelled out rather than derived so
#: a new enum member cannot silently become a trigger condition: adding one here is a
#: deliberate act, in the same spirit as every other closed list in this product.
EVENT_STATES = (
    "success",
    "failed",
    "denied",
    "cancelled",
    "timeout",
    "partially_completed",
    "requires_user",
    "interrupted",
)

#: The states a stored run can be in. ``missed`` exists because a process that claimed
#: an occurrence and then stopped must say it never started — the alternative is a row
#: that looks like work and no record of what it did.
RUN_STATES = (
    "claimed",
    "running",
    "awaiting_approval",
    "succeeded",
    "partial",
    "failed",
    "denied",
    "cancelled",
    "interrupted",
    "missed",
)

#: A run in one of these is over. Nothing may move it again.
SETTLED_RUN_STATES = frozenset(
    {"succeeded", "partial", "failed", "denied", "cancelled", "interrupted", "missed"}
)

#: Names are shown in the library list and said back in a sentence. Bounded so a
#: hostile label cannot become a payload.
MAX_NAME = 80
MAX_DESCRIPTION = 400
MAX_DEVICE = 120
#: An interval below this would be a busy loop, not a schedule.
MIN_INTERVAL_S = 60
MAX_INTERVAL_S = 30 * 24 * 60 * 60


class WorkflowError(ValueError):
    """An automation that must not be stored. The reason is what the user reads."""


def new_workflow_id() -> str:
    return f"wf_{uuid.uuid4().hex[:16]}"


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:16]}"


def _scalar_or_list(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return True
    return isinstance(value, list) and all(
        isinstance(item, (str, int, float, bool)) for item in value
    )


@dataclass
class WorkflowStep:
    """One proposed action, with the same evidence rule as a task step."""

    index: int
    label: str
    action_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    #: ``{"step": int, **CONDITION_KEYS}`` — evaluated against a recorded observation.
    when: dict[str, Any] = field(default_factory=dict)
    #: The computer this step is addressed to. Empty means the workflow's own
    #: selection at run time. A name here aims the step; the executor still resolves
    #: and may refuse it, and an offline device is never substituted.
    device_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "action_type": self.action_type,
            "parameters": dict(self.parameters),
            "when": dict(self.when),
            "device_id": self.device_id,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> WorkflowStep:
        return WorkflowStep(
            index=int(raw.get("index", 0)),
            label=str(raw.get("label") or ""),
            action_type=str(raw.get("action_type") or ""),
            parameters=dict(raw.get("parameters") or {}),
            when=dict(raw.get("when") or {}),
            device_id=str(raw.get("device_id") or ""),
        )


@dataclass
class Workflow:
    """A stored automation. Everything here is *intent*; none of it is authority."""

    workflow_id: str
    actor_id: str
    name: str
    steps: list[WorkflowStep] = field(default_factory=list)
    description: str = ""
    #: Where the steps came from: ``"user"`` for a hand-written plan, or
    #: ``"template:<name>"`` when an operator-authored template supplied them. A model
    #: never authors a stored plan — it may only propose one for review.
    source: str = "user"
    #: ``{"kind": "interval", "seconds": 900}`` / ``{"kind": "daily", "at": "09:00"}`` /
    #: ``{"kind": "weekly", "days": [0, 2, 4], "at": "08:30"}``. Empty means no schedule.
    schedule: dict[str, Any] = field(default_factory=dict)
    #: ``{"event_type": "execution.failed"}``. Empty means no event trigger. Matching is
    #: exact on the event type, which the hub names — never on payload content.
    event: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    #: Where a scheduled occurrence is addressed when its steps name no computer.
    device_id: str = ""
    next_run_at: str = ""
    last_run_at: str = ""
    last_run_id: str = ""
    last_state: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def touch(self) -> None:
        self.updated_at = _now()

    def trigger_kind(self) -> str:
        if self.schedule:
            return "schedule"
        if self.event:
            return "event"
        return "manual"

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "actor_id": self.actor_id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "trigger": self.trigger_kind(),
            "schedule": dict(self.schedule),
            "event": dict(self.event),
            "enabled": self.enabled,
            "device_id": self.device_id,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "last_run_id": self.last_run_id,
            "last_state": self.last_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": [step.to_dict() for step in self.steps],
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Workflow:
        return Workflow(
            workflow_id=str(raw.get("workflow_id") or ""),
            actor_id=str(raw.get("actor_id") or ""),
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            source=str(raw.get("source") or "user"),
            schedule=dict(raw.get("schedule") or {}),
            event=dict(raw.get("event") or {}),
            enabled=bool(raw.get("enabled", True)),
            device_id=str(raw.get("device_id") or ""),
            next_run_at=str(raw.get("next_run_at") or ""),
            last_run_at=str(raw.get("last_run_at") or ""),
            last_run_id=str(raw.get("last_run_id") or ""),
            last_state=str(raw.get("last_state") or ""),
            created_at=str(raw.get("created_at") or _now()),
            updated_at=str(raw.get("updated_at") or _now()),
            steps=[WorkflowStep.from_dict(item) for item in raw.get("steps") or []],
        )


@dataclass
class AutomationRun:
    """One occurrence of one workflow, and what actually happened to it.

    ``slot`` is the occurrence's identity: a due timestamp for a schedule, an event id
    for a trigger, a fresh id for a manual press. The store's uniqueness on
    ``(workflow_id, slot)`` is the whole duplicate-suppression story.
    """

    run_id: str
    workflow_id: str
    workflow_name: str
    actor_id: str
    slot: str
    trigger: str
    state: str = "claimed"
    session_id: str = ""
    #: The steps as the run actually left them: label, state, verified, device, error.
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    #: The trigger's own evidence — the event type and id for an event run, the due
    #: time for a schedule. So "why did this run?" is answerable from the record.
    trigger_detail: dict[str, Any] = field(default_factory=dict)
    scheduled_for: str = ""
    created_at: str = field(default_factory=_now)
    started_at: str = ""
    finished_at: str = ""

    @property
    def settled(self) -> bool:
        return self.state in SETTLED_RUN_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "actor_id": self.actor_id,
            "trigger": self.trigger,
            "state": self.state,
            "session_id": self.session_id,
            "steps": [dict(step) for step in self.steps],
            "error": self.error,
            "trigger_detail": dict(self.trigger_detail),
            "scheduled_for": self.scheduled_for,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> AutomationRun:
        return AutomationRun(
            run_id=str(raw.get("run_id") or ""),
            workflow_id=str(raw.get("workflow_id") or ""),
            workflow_name=str(raw.get("workflow_name") or ""),
            actor_id=str(raw.get("actor_id") or ""),
            slot=str(raw.get("slot") or ""),
            trigger=str(raw.get("trigger") or "manual"),
            state=str(raw.get("state") or "claimed"),
            session_id=str(raw.get("session_id") or ""),
            steps=[dict(step) for step in raw.get("steps") or []],
            error=str(raw.get("error") or ""),
            trigger_detail=dict(raw.get("trigger_detail") or {}),
            scheduled_for=str(raw.get("scheduled_for") or ""),
            created_at=str(raw.get("created_at") or _now()),
            started_at=str(raw.get("started_at") or ""),
            finished_at=str(raw.get("finished_at") or ""),
        )


# -- validation ------------------------------------------------------------
#
# Everything below refuses rather than repairs. A half-understood automation is a plan
# that will do something the user did not ask for, and the honest moment to say so is
# when it is created — not at 07:00 the next morning with nobody watching.


def _validate_step(raw: Any, position: int, known_action: Any) -> WorkflowStep:
    if not isinstance(raw, dict):
        raise WorkflowError(f"step {position + 1} is not an object")
    label = str(raw.get("label") or "").strip()
    action_type = str(raw.get("action") or raw.get("action_type") or "").strip()
    if not label:
        raise WorkflowError(f"step {position + 1} has no label")
    if not action_type:
        raise WorkflowError(f"step {position + 1} has no action")
    if not known_action(action_type):
        raise WorkflowError(
            f"step {position + 1} names unregistered action {action_type!r}"
        )
    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise WorkflowError(f"step {position + 1} parameters are not an object")
    for key, value in parameters.items():
        if not isinstance(key, str):
            raise WorkflowError(f"step {position + 1} has a non-string parameter name")
        if not _scalar_or_list(value):
            raise WorkflowError(
                f"step {position + 1} parameter {key!r} is not a scalar or flat list"
            )
    when = raw.get("when") or {}
    if not isinstance(when, dict):
        raise WorkflowError(f"step {position + 1} when is not an object")
    if when:
        target = when.get("step")
        if not isinstance(target, int) or target < 0 or target >= position:
            raise WorkflowError(
                f"step {position + 1} conditions on a step that is not earlier than it"
            )
        unknown = set(when) - {"step"} - CONDITION_KEYS
        if unknown:
            raise WorkflowError(
                f"step {position + 1} conditions on {sorted(unknown)}, which is not "
                "observed evidence"
            )
    device_id = str(raw.get("device_id") or raw.get("device") or "").strip()
    if len(device_id) > MAX_DEVICE:
        raise WorkflowError(f"step {position + 1} names an implausibly long device id")
    return WorkflowStep(
        index=position,
        label=label,
        action_type=action_type,
        parameters=dict(parameters),
        when=dict(when),
        device_id=device_id,
    )


def _clock(value: Any) -> tuple[int, int]:
    """``"HH:MM"`` as (hour, minute). Anything else is refused, not defaulted."""
    if not isinstance(value, str):
        raise WorkflowError("a schedule time must be a string like '09:30'")
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise WorkflowError(f"schedule time {value!r} is not HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise WorkflowError(f"schedule time {value!r} is not a real time of day")
    return hour, minute


def validate_schedule(raw: Any) -> dict[str, Any]:
    """Normalise a schedule, or refuse it. Empty means "no schedule"."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise WorkflowError("a schedule must be an object")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in SCHEDULE_KINDS:
        raise WorkflowError(
            f"schedule kind {kind!r} is not one of {', '.join(SCHEDULE_KINDS)}"
        )
    if kind == "interval":
        try:
            seconds = int(raw.get("seconds"))
        except (TypeError, ValueError):
            raise WorkflowError("an interval schedule needs whole seconds") from None
        if seconds < MIN_INTERVAL_S:
            raise WorkflowError(
                f"an interval shorter than {MIN_INTERVAL_S}s would be a busy loop"
            )
        if seconds > MAX_INTERVAL_S:
            raise WorkflowError("an interval longer than 30 days is not a schedule")
        return {"kind": "interval", "seconds": seconds}
    hour, minute = _clock(raw.get("at"))
    at = f"{hour:02d}:{minute:02d}"
    if kind == "daily":
        return {"kind": "daily", "at": at}
    days = raw.get("days")
    if not isinstance(days, list) or not days:
        raise WorkflowError("a weekly schedule needs at least one day")
    normalised: list[int] = []
    for day in days:
        if not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 6:
            raise WorkflowError("weekly days are integers 0 (Monday) to 6 (Sunday)")
        if day not in normalised:
            normalised.append(day)
    return {"kind": "weekly", "days": sorted(normalised), "at": at}


def validate_event(raw: Any) -> dict[str, Any]:
    """An event trigger names one event type exactly. Empty means "no event trigger".

    ``state`` is optional and, when present, narrows the trigger to completions whose
    recorded state is that value. It is a closed list of the executor's own
    :class:`~actionmesh.protocol.models.ActionState` names — the verdict the engine
    wrote into the record — and never a field a provider supplied. Without it, a
    workflow triggered on ``execution.completed`` fires on every completion, which is
    honest but rarely what "when the build fails" means.
    """
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise WorkflowError("an event trigger must be an object")
    event_type = str(raw.get("event_type") or "").strip()
    if not event_type:
        raise WorkflowError("an event trigger needs an event_type")
    if len(event_type) > 120 or any(ch.isspace() for ch in event_type):
        raise WorkflowError(f"event type {event_type!r} is not an event name")
    if event_type not in DEFAULT_EVENT_TYPES:
        raise WorkflowError(
            f"event type {event_type!r} is not one this program publishes "
            f"({', '.join(DEFAULT_EVENT_TYPES)})"
        )
    state = str(raw.get("state") or "").strip()
    if state:
        if event_type != "execution.completed":
            raise WorkflowError(
                "only execution.completed carries a state to narrow a trigger by"
            )
        if state not in EVENT_STATES:
            raise WorkflowError(
                f"state {state!r} is not one the executor records "
                f"({', '.join(EVENT_STATES)})"
            )
        return {"event_type": event_type, "state": state}
    return {"event_type": event_type}


def validate_workflow(
    raw: Any,
    *,
    actor_id: str,
    known_action: Any,
    workflow_id: str = "",
) -> Workflow:
    """Build a storable workflow from an untrusted body, refusing anything unclear.

    ``known_action`` is the registry's own predicate. A plan naming an action this
    machine does not offer is refused at save time rather than discovered at 07:00.
    """
    if not isinstance(raw, dict):
        raise WorkflowError("a workflow must be an object")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise WorkflowError("a workflow needs a name")
    if len(name) > MAX_NAME:
        raise WorkflowError(f"the name is longer than {MAX_NAME} characters")
    description = str(raw.get("description") or "").strip()
    if len(description) > MAX_DESCRIPTION:
        raise WorkflowError(f"the description is longer than {MAX_DESCRIPTION} characters")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise WorkflowError("a workflow needs at least one step")
    if len(steps_raw) > MAX_STEPS:
        raise WorkflowError(f"a workflow may have at most {MAX_STEPS} steps")
    steps = [_validate_step(step, index, known_action) for index, step in enumerate(steps_raw)]
    schedule = validate_schedule(raw.get("schedule"))
    event = validate_event(raw.get("event"))
    if schedule and event:
        # Two triggers on one plan is ambiguous: does a failure *and* 09:00 mean two
        # runs or one? Refusing is the only answer that cannot surprise the user.
        raise WorkflowError("a workflow may have a schedule or an event trigger, not both")
    device_id = str(raw.get("device_id") or "").strip()
    if len(device_id) > MAX_DEVICE:
        raise WorkflowError("the device id is implausibly long")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise WorkflowError("enabled must be true or false")
    return Workflow(
        workflow_id=workflow_id or str(raw.get("workflow_id") or "") or new_workflow_id(),
        actor_id=actor_id,
        name=name,
        description=description,
        source=str(raw.get("source") or "user"),
        schedule=schedule,
        event=event,
        enabled=enabled,
        device_id=device_id,
        steps=steps,
    )


# -- scheduling arithmetic -------------------------------------------------


def _local(moment: datetime) -> datetime:
    return moment.astimezone() if moment.tzinfo is not None else moment


def next_occurrence(schedule: dict[str, Any], after: datetime) -> datetime | None:
    """The first time this schedule fires strictly after ``after``, in local time.

    Pure arithmetic on the stored shape — no calendar service, no cloud scheduler, and
    no guessing at a timezone the user did not name. ``None`` for "no schedule".
    """
    if not schedule:
        return None
    local = _local(after)
    kind = schedule.get("kind")
    if kind == "interval":
        return local + timedelta(seconds=int(schedule["seconds"]))
    hour, minute = _clock(schedule.get("at"))
    if kind == "daily":
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return candidate if candidate > local else candidate + timedelta(days=1)
    if kind == "weekly":
        days = [int(day) for day in schedule.get("days") or []]
        if not days:
            return None
        for offset in range(0, 8):
            candidate = (local + timedelta(days=offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate <= local:
                continue
            if candidate.weekday() in days:
                return candidate
        return None
    return None


def parse_when(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def describe_workflow(workflow: Workflow, *, devices: dict[str, str] | None = None) -> str:
    """One sentence a person can read: what it does, when, and on which computer."""
    names = devices or {}
    parts = [f"{workflow.name!r} has {len(workflow.steps)} step"
             f"{'' if len(workflow.steps) == 1 else 's'}."]
    target = workflow.device_id
    if target:
        parts.append(f"It is addressed to {names.get(target, target)!r}.")
    if workflow.schedule:
        kind = workflow.schedule.get("kind")
        if kind == "interval":
            parts.append(f"It runs every {workflow.schedule['seconds']} seconds.")
        elif kind == "daily":
            parts.append(f"It runs every day at {workflow.schedule['at']}.")
        else:
            days = ",".join(str(day) for day in workflow.schedule.get("days") or [])
            parts.append(f"It runs weekly (days {days}) at {workflow.schedule.get('at')}.")
        if workflow.next_run_at:
            parts.append(f"Next run: {workflow.next_run_at}.")
    elif workflow.event:
        parts.append(f"It runs when {workflow.event.get('event_type')!r} is published.")
    else:
        parts.append("It runs only when you press run.")
    if not workflow.enabled:
        parts.append("It is disabled, so no schedule or event will start it.")
    parts.append("Automations confer no permission: every step is authorised when it runs.")
    return " ".join(parts)


__all__ = [
    "DEFAULT_EVENT_TYPES",
    "EVENT_STATES",
    "MAX_DESCRIPTION",
    "MAX_DEVICE",
    "MAX_NAME",
    "MIN_INTERVAL_S",
    "RUN_STATES",
    "SCHEDULE_KINDS",
    "SETTLED_RUN_STATES",
    "TRIGGER_KINDS",
    "AutomationRun",
    "Workflow",
    "WorkflowError",
    "WorkflowStep",
    "describe_workflow",
    "new_run_id",
    "new_workflow_id",
    "next_occurrence",
    "parse_when",
    "validate_event",
    "validate_schedule",
    "validate_workflow",
]