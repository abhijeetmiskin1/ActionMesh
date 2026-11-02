"""Understanding: what the user meant, in the context of what just happened.

Deterministic by design. Every capability here — intent detection, ordinal and
demonstrative reference resolution, decomposition into steps, and the sentences that get
said back — runs without a model, so the product works on a laptop with no credentials
and no network, and so a model can be added as a *proposal source* rather than as the
thing holding the conversation together.

The rule that shapes all of it: a reference resolves to something the user was **shown**.
"Stop the second one" works because the previous answer listed two things; it does not
work by guessing at a process, and it never becomes an approval.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from actionmesh.core.planner import Planner
from actionmesh.voiceos.models import Conversation, TaskSession, TaskStep
from actionmesh.voiceos.tasks import TaskTemplate, match_template

#: How a remembered item is addressed, so "the second server" can find it.
_REFERENCE_ORDINAL = re.compile(
    r"(?:the\s+|that\s+|this\s+)?(?:first|second|third|fourth|fifth|last|\d+(?:st|nd|rd|th)?)\s+"
    r"(server|process|project|editor|file|one)\b",
    re.I,
)
_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1,
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
}
_STATUS = re.compile(r"^\s*(?:what(?:'s| is) )?(?:the |my |our )?status\b|^\s*what are you doing", re.I)
_STOP = re.compile(r"^\s*(?:stop|cancel|abort)\b", re.I)
_CLEAR = re.compile(r"^\s*(?:clear|forget|reset)\b[^.]{0,40}\b(?:history|conversation|context|memory)\b", re.I)

#: Stop-style references map to the registered capability that acts on them.
_STOP_ACTION = {
    "server": ("device.server.stop", "name"),
    "process": ("device.process.stop", "process_id"),
    "project": ("device.project.status", "project"),
    "file": ("device.file.read", "path"),
}


@dataclass
class Understanding:
    """What VoiceOS decided the request *is*. Still a proposal, never a decision."""

    kind: str
    reply: str
    session: TaskSession | None = None
    notes: tuple[str, ...] = ()
    #: For ``stop``: the execution the user means, if one is active.
    target_execution_id: str = ""
    unresolved: tuple[str, ...] = field(default_factory=tuple)


class Reasoner:
    """Turns a sentence and a conversation into an intent and, sometimes, a task."""

    def __init__(
        self,
        *,
        planner: Planner,
        known_action: Callable[[str], bool],
        templates: Callable[[], tuple[TaskTemplate, ...]],
        device_id: str = "",
    ) -> None:
        self.planner = planner
        self.known_action = known_action
        self.templates = templates
        self.device_id = device_id

    # -- entry point -------------------------------------------------------

    def understand(self, text: str, conversation: Conversation) -> Understanding:
        request = " ".join(text.split())
        if not request:
            return Understanding(kind="answer", reply="Say something first — or type it.")
        if _CLEAR.match(request):
            return Understanding(
                kind="clear",
                reply="Conversation cleared. I have forgotten the references; nothing else changed.",
            )
        if _STATUS.match(request):
            return self._status(conversation)
        if _STOP.match(request):
            return self._stop(request, conversation)
        return self._task(request, conversation)

    # -- conversational intents --------------------------------------------

    def _status(self, conversation: Conversation) -> Understanding:
        if not conversation.active_session_id:
            return Understanding(
                kind="answer",
                reply="No task is in progress. Ask for something and I will show a plan first.",
            )
        session = conversation.active_session
        if session is None:  # pragma: no cover - the caller always loads it
            return Understanding(kind="answer", reply="The active task could not be read.")
        return Understanding(kind="answer", reply=describe_session(session), session=session)

    def _stop(self, request: str, conversation: Conversation) -> Understanding:
        # "Stop the second one" names a *target* and becomes a proposed stop request that
        # goes through policy and approval like any other action. "Stop it" names the live
        # step of the active task and becomes a cancellation through ActionMesh. Both are
        # answers about a target; neither is an approval.
        step = self._reference_step(request, conversation)
        if step is not None:
            session = self._session(request, [step], conversation)
            target = next(iter(step.parameters.values()), "")
            return Understanding(
                kind="task",
                reply=(
                    f"I identified {target!r} on the selected laptop. Here is the stop "
                    "request — nothing has run yet."
                ),
                session=session,
            )
        if _REFERENCE_ORDINAL.search(request):
            return Understanding(
                kind="answer",
                reply=(
                    "I have no list to take that from. Ask me what is running first, then "
                    "say which one to stop."
                ),
            )
        active: TaskSession | None = conversation.active_session
        if active is None:
            return Understanding(
                kind="answer",
                reply="There is no running task for me to stop.",
            )
        running = next((item for item in active.steps if item.state == "running"), None)
        parked = next((item for item in active.steps if item.state == "awaiting_approval"), None)
        live = running or parked
        if live is None:
            return Understanding(
                kind="answer",
                reply=f"The task is {active.status}, so there is nothing running to stop.",
                session=active,
            )
        return Understanding(
            kind="stop",
            reply=f"Cancellation requested for {live.execution_id}. I will report what actually stopped.",
            target_execution_id=live.execution_id,
            session=active,
        )

    def _reference_step(self, request: str, conversation: Conversation) -> TaskStep | None:
        """Turn ``the second server`` into a step against the id the user was shown."""
        resolved = self._resolve_reference(request, conversation)
        if resolved is None:
            return None
        kind, value = resolved
        action_type, parameter = _STOP_ACTION.get(kind, ("", ""))
        if not action_type or not value:
            return None
        if not self.known_action(action_type):
            return None
        return TaskStep(
            index=0,
            label=f"{request.rstrip('.?!')} — {value}",
            action_type=action_type,
            parameters={parameter: value},
        )

    # -- task decomposition ------------------------------------------------

    def _task(self, request: str, conversation: Conversation) -> Understanding:
        step = self._reference_step(request, conversation)
        if step is not None:
            session = self._session(request, [step], conversation)
            target = next(iter(step.parameters.values()), "")
            return Understanding(
                kind="task",
                reply=(
                    f"I resolved that to {target!r} from what I showed you a moment ago. "
                    f"Plan: 1) {step.label}. Nothing has run yet."
                ),
                session=session,
            )
        template = match_template(self.templates(), request)
        if template is not None:
            steps = [
                TaskStep(
                    index=index,
                    label=str(spec.get("label")),
                    action_type=str(spec.get("action")),
                    parameters=dict(spec.get("parameters") or {}),
                    when=dict(spec.get("when") or {}),
                )
                for index, spec in enumerate(template.steps)
            ]
            session = self._session(request, steps, conversation)
            listing = "; ".join(f"{step.index + 1}) {step.label}" for step in steps)
            return Understanding(
                kind="task",
                reply=f"Here is the plan from your configured task {template.name!r}: {listing}. Nothing has run yet.",
                session=session,
            )
        plan = self.planner.plan(request)
        if plan.actions:
            steps = [
                TaskStep(
                    index=index,
                    label=f"Run {action.type}",
                    action_type=action.type,
                    parameters=dict(action.parameters),
                )
                for index, action in enumerate(plan.actions)
            ]
            session = self._session(request, steps, conversation)
            listing = "; ".join(f"{step.index + 1}) {step.label}" for step in steps)
            notes = tuple(plan.notes)
            return Understanding(
                kind="task",
                reply=f"Plan: {listing}. Nothing has run yet.",
                session=session,
                notes=notes,
            )
        missing = tuple(plan.notes) or ("no rule matched",)
        return Understanding(
            kind="answer",
            reply=(
                "I could not safely map that to an action this laptop registers. "
                "Ask me what is running, name the target, or add a task template for it."
            ),
            unresolved=missing,
        )

    def _session(self, request: str, steps: list[TaskStep], conversation: Conversation) -> TaskSession:
        session = TaskSession(
            session_id=f"task_{uuid.uuid4().hex[:12]}",
            actor_id=conversation.actor_id,
            request=request,
            device_id=self.device_id or conversation.device_id,
            steps=steps,
        )
        conversation.active_session_id = session.session_id
        return session

    def _resolve_reference(
        self, request: str, conversation: Conversation
    ) -> tuple[str, str] | None:
        """``the second server`` -> the id the user was actually shown."""
        match = _REFERENCE_ORDINAL.search(request)
        if not match:
            return None
        kind = match.group(1).lower()
        ordinal_token = match.group(0).lower()
        number = None
        for word, value in _ORDINAL_WORDS.items():
            if word in ordinal_token:
                number = value
                break
        if number is None:
            digits = re.search(r"(\d+)", ordinal_token)
            number = int(digits.group(1)) if digits else None
        if number is None:
            return None
        pool = [
            item
            for item in conversation.references
            if kind in {"one", item.get("kind", "")}
        ]
        if not pool:
            return None
        index = len(pool) - 1 if number == -1 else number - 1
        if not 0 <= index < len(pool):
            return None
        chosen = pool[index]
        return (kind if kind != "one" else chosen.get("kind", "one"), chosen.get("id", ""))


def describe_session(session: TaskSession) -> str:
    """A sentence about where a task actually stands. No optimism, no padding."""
    done = [step for step in session.steps if step.settled]
    current: TaskStep | None = session.pending_step()
    parts = [
        f"Task {session.session_id}: {len(done)} of {len(session.steps)} steps settled, status {session.status}."
    ]
    for step in session.steps:
        evidence = ""
        if step.verified is True:
            evidence = " · verified"
        elif step.verified is False:
            evidence = " · NOT verified"
        parts.append(f"  {step.index + 1}) {step.label} — {step.state}{evidence}")
    if current is not None:
        parts.append(f"Next: {current.label} ({current.state}).")
    if session.summary:
        parts.append(session.summary)
    return "\n".join(parts)


def observed_references(action_type: str, result: dict[str, Any]) -> list[dict[str, str]]:
    """Remember what an observation listed, so a later turn can point at it.

    Reads the provider's own result shape (``processes``, ``servers``, ``projects``,
    ``entries``) — the unprojected data the server holds. The client only ever sees
    counts and short rows, so a reference here is to something the user was *shown*,
    which is the only kind of reference that is fair to resolve.
    """
    out: list[dict[str, str]] = []
    kind_by_key = {
        "processes": "process",
        "servers": "server",
        "projects": "project",
        "editors": "editor",
        "entries": "file",
    }
    for key, kind in kind_by_key.items():
        rows = result.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier = str(
                row.get("process_id") or row.get("name") or row.get("project") or row.get("path") or ""
            ).strip()
            if not identifier:
                continue
            label = str(row.get("label") or row.get("command") or identifier).strip()
            out.append({"kind": kind, "id": identifier, "label": label, "action_type": action_type})
    return out


__all__ = ["Reasoner", "Understanding", "describe_session", "observed_references"]
