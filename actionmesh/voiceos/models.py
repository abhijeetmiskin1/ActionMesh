"""Task sessions: what was asked, what was proposed, what actually happened.

A session is **interaction state, never authority**. It deliberately has no field for
permissions, grants, risk levels or an approval decision — those live in the consent
store, the registry and the policy engine, and a conversation that could describe its
own authority would be the second policy engine this product refuses to have.

Steps are recorded one at a time because that is how they run: each step is its own
ActionMesh execution, with its own authorization, its own approval if the risk asks for
one, and its own verification. A session that "just worked" without any of that is a
bug, not a feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: What a step can be doing. `awaiting_approval` is a step whose execution parked;
#: it is not finished, and nothing may report it as one.
STEP_STATES = (
    "pending",
    "running",
    "awaiting_approval",
    # C35: the process that owned the run is gone and its outcome is unknown.
    "interrupted",
    "succeeded",
    "succeeded_unverified",
    "failed",
    "denied",
    "cancelled",
    "skipped",
)

#: What a session can be doing.
SESSION_STATES = (
    "proposed",
    "running",
    "awaiting_approval",
    "interrupted",
    "completed",
    "stopped",
    "failed",
    "cancelled",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class TaskStep:
    """One proposed action, plus the evidence of whatever actually happened to it."""

    index: int
    label: str
    action_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    #: A condition on an earlier step's observed result, e.g. ``{"step": 0, "exit_code": 0}``.
    #: Operator-authored, never model-authored.
    when: dict[str, Any] = field(default_factory=dict)
    state: str = "pending"
    execution_id: str = ""
    verified: bool | None = None
    observed: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def settled(self) -> bool:
        return self.state in {
            "succeeded",
            "succeeded_unverified",
            "failed",
            "denied",
            "cancelled",
            "skipped",
            "interrupted",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "action_type": self.action_type,
            "parameters": dict(self.parameters),
            "when": dict(self.when),
            "state": self.state,
            "execution_id": self.execution_id,
            "verified": self.verified,
            "observed": dict(self.observed),
            "error": self.error,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> TaskStep:
        return TaskStep(
            index=int(raw.get("index", 0)),
            label=str(raw.get("label", "")),
            action_type=str(raw.get("action_type", "")),
            parameters=dict(raw.get("parameters") or {}),
            when=dict(raw.get("when") or {}),
            state=str(raw.get("state") or "pending"),
            execution_id=str(raw.get("execution_id") or ""),
            verified=raw.get("verified"),
            observed=dict(raw.get("observed") or {}),
            error=str(raw.get("error") or ""),
        )


@dataclass
class TaskSession:
    """A request, the steps proposed for it, and who asked."""

    session_id: str
    actor_id: str
    request: str
    steps: list[TaskStep] = field(default_factory=list)
    device_id: str = ""
    status: str = "proposed"
    summary: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def touch(self) -> None:
        self.updated_at = _now()

    def pending_step(self) -> TaskStep | None:
        """The next step that may be attempted, or ``None`` when the work is done."""
        for step in self.steps:
            if not step.settled:
                return step
        return None

    def step(self, index: int) -> TaskStep | None:
        for item in self.steps:
            if item.index == index:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "actor_id": self.actor_id,
            "request": self.request,
            "device_id": self.device_id,
            "status": self.status,
            "summary": self.summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": [step.to_dict() for step in self.steps],
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> TaskSession:
        return TaskSession(
            session_id=str(raw.get("session_id", "")),
            actor_id=str(raw.get("actor_id", "")),
            request=str(raw.get("request", "")),
            device_id=str(raw.get("device_id") or ""),
            status=str(raw.get("status") or "proposed"),
            summary=str(raw.get("summary") or ""),
            created_at=str(raw.get("created_at") or _now()),
            updated_at=str(raw.get("updated_at") or _now()),
            steps=[TaskStep.from_dict(item) for item in raw.get("steps") or []],
        )


@dataclass
class Conversation:
    """Per-actor conversational context. Separate from credentials on purpose.

    ``references`` holds what the last observation actually listed — process ids,
    server names — so "stop the second one" can resolve to a target the user was shown.
    It is a memory of *labels*, never a grant: the resolved action still goes through
    policy, consent and approval like any other.
    """

    actor_id: str
    history: list[dict[str, str]] = field(default_factory=list)
    active_session_id: str = ""
    device_id: str = ""
    references: list[dict[str, str]] = field(default_factory=list)
    updated_at: str = field(default_factory=_now)
    #: The session ``active_session_id`` names, resolved by whoever loaded it. Derived,
    #: never stored: a conversation that carried its own copy of a task could drift from
    #: the record the executor actually wrote.
    active_session: TaskSession | None = field(default=None, repr=False, compare=False)

    def remember(self, role: str, text: str, limit: int = 40) -> None:
        self.history.append({"role": role, "text": text, "at": _now()})
        del self.history[:-limit]

    def reference(self, ordinal: int) -> dict[str, str] | None:
        """One-based ordinal into the last observed inventory."""
        if 1 <= ordinal <= len(self.references):
            return self.references[ordinal - 1]
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "history": list(self.history),
            "active_session_id": self.active_session_id,
            "device_id": self.device_id,
            "references": list(self.references),
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Conversation:
        return Conversation(
            actor_id=str(raw.get("actor_id", "")),
            history=[dict(item) for item in raw.get("history") or []],
            active_session_id=str(raw.get("active_session_id") or ""),
            device_id=str(raw.get("device_id") or ""),
            references=[dict(item) for item in raw.get("references") or []],
            updated_at=str(raw.get("updated_at") or _now()),
        )


__all__ = ["SESSION_STATES", "STEP_STATES", "Conversation", "TaskSession", "TaskStep"]
