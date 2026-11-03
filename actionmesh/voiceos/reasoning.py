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
from actionmesh.voiceos.targeting import TargetChoice, describe_choice
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
#: "switch to my Mac", "use the Linux workstation". The trailing span is a device
#: name and is resolved by :mod:`actionmesh.voiceos.targeting`; this pattern only
#: decides that the sentence *is* a switch, not that it names a real computer.
_SWITCH = re.compile(
    r"^\s*(?:switch|change|select|use|target|move)\b[^.]{0,24}?\b(?:to|onto)\b\s+(?P<name>.+)$"
    r"|^\s*(?:switch|change|move)\b\s+(?:the\s+)?(?:computer|device|machine|target)\b\s*[:\-]?\s*(?P<bare>.+)$",
    re.I,
)

#: "check my backend", "find the latest error", "why did my app crash". Narrow on
#: purpose: these decide that the sentence is a *diagnosis request*, not that anything
#: should run. The plan they produce is two ordinary registered reads, and the analysis
#: is a separate, explicit step the user can see.
_DIAGNOSE = re.compile(
    r"\bcheck\b[^.]{0,24}?\b(?:my|the|our)\s+(?:backend|server|service|api|app|application)\b"
    r"|\bfind\b[^.]{0,12}?\b(?:the\s+)?(?:latest|last|newest)\s+(?:error|crash|exception|failure)\b"
    r"|\bwhat(?:'s| is|\s+was)\s+the\s+(?:latest|last)\s+(?:error|crash|exception|failure)\b"
    r"|\bwhy\s+(?:did|does|is|was|has)\b[^.]{0,40}?\b(?:crash|crashed|fail|failed|failing|broken|down)\b"
    r"|\b(?:explain|analyse|analyze|diagnose)\b[^.]{0,24}?\b(?:error|crash|failure|logs?)\b"
    r"|\b(?:show|read|get)\b[^.]{0,16}?\b(?:the\s+)?(?:latest\s+)?(?:error|crash)\b[^.]{0,16}?"
    r"\b(?:logs?|from\s+the\s+logs?)\b",
    re.I,
)

#: Names a service when the sentence names one ("check my backend api"). Only used to
#: narrow *which* running server to read the logs of; it never widens a read.
_DIAGNOSE_NAMED = re.compile(
    r"\b(?:of|for|from|on)\s+(?:the\s+)?(?P<name>[A-Za-z][\w.-]{1,40})\b"
    r"(?:\s+(?:server|service|process|backend|app))?",
    re.I,
)

#: The capability that carries a program's log body. Named once here because the
#: diagnosis decomposition is built on exactly this read and nothing else.
_DIAGNOSE_LOG_ACTION = "device.process.logs"
_DIAGNOSE_STATUS_ACTION = "device.server.status"

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
    #: For ``device``: the choice the user asked for. Already validated by the
    #: resolver the caller supplied; a refusal is an ``answer`` with no session.
    target: TargetChoice | None = None
    #: The computer the request is addressed to, after resolution. Written onto
    #: every step of a proposed session so "where did this run?" is answerable.
    device_id: str = ""
    device_name: str = ""
    unresolved: tuple[str, ...] = field(default_factory=tuple)


def _diagnose_name(request: str) -> str:
    """The service a diagnosis request names, if it names one.

    Only ever used as a *filter* for the status read: the status step lists the servers
    this agent launched and this narrows the list to one. A name that matches nothing
    therefore produces an empty observation, which the bind step treats as "nothing to
    read" and denies — it does not become a fabricated target elsewhere on the machine.

    Two sources, in that order of confidence: an explicit "the api-server", and what
    follows "of/for/from/on". A sentence like *"check my backend on Linux"* names no
    service at all — "backend" is the role being checked, and the operating system is
    where — so the result is empty and the status read lists every server instead of
    filtering on a role word that would match nothing.
    """
    for pattern in (_DIAGNOSE_EXPLICIT, _DIAGNOSE_NAMED):
        match = pattern.search(request)
        if match is None:
            continue
        name = match.group("name").strip(" .,:;!?'\"")
        if not name or name.lower() in _NOT_A_SERVICE_NAME:
            continue
        return name
    return ""


#: "the api-server", "the frontend service" — a name with a shape, which is the least
#: ambiguous thing a sentence can offer.
_DIAGNOSE_EXPLICIT = re.compile(
    r"\b(?:the|my|our)\s+(?P<name>[A-Za-z][\w.-]*(?:[-_][\w.-]+)+)\b", re.I
)

#: Words that follow "on/in/for" or name a role without naming a service. Filtering
#: these is what keeps "check my backend on Linux" from filtering a status read by a
#: string that can never match.
_NOT_A_SERVICE_NAME = frozenset(
    {
        "linux", "mac", "macbook", "macos", "windows", "ubuntu", "debian", "fedora",
        "this", "that", "it", "my", "the", "a", "an", "me", "now", "here", "there",
        "backend", "server", "service", "app", "application", "api", "frontend",
        "computer", "machine", "device", "system", "process", "project",
        "error", "crash", "exception", "failure", "log", "logs",
    }
)


class Reasoner:
    """Turns a sentence and a conversation into an intent and, sometimes, a task."""

    def __init__(
        self,
        *,
        planner: Planner,
        known_action: Callable[[str], bool],
        templates: Callable[[], tuple[TaskTemplate, ...]],
        device_id: str = "",
        resolve: Callable[[str], TargetChoice] | None = None,
        local_device_id: Callable[[], str] | None = None,
    ) -> None:
        self.planner = planner
        self.known_action = known_action
        self.templates = templates
        self.device_id = device_id
        #: A device name -> validated target. Supplied by the API because only it
        #: holds the durable registry; a reasoner without one can still understand
        #: everything that does not name a computer.
        self.resolve = resolve
        #: The machine this control service serves, used to answer "which computer
        #: am I talking to?" when no device was selected.
        self.local_device_id = local_device_id

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
        diagnosed = self._diagnose(request, conversation)
        if diagnosed is not None:
            return diagnosed
        switched = self._switch(request, conversation)
        if switched is not None:
            return switched
        return self._task(request, conversation)

    # -- device selection --------------------------------------------------

    def _switch(self, request: str, conversation: Conversation) -> Understanding | None:
        """Handle "switch to the Mac". ``None`` when the sentence is not a switch.

        The request does not change authority: it changes which computer later
        steps are *addressed to*, and the executor still resolves that address,
        checks its role and risk ceiling, and consults policy for the same actor.
        """
        match = _SWITCH.match(request)
        if match is None:
            return None
        named = (match.group("name") or match.group("bare") or "").strip(" .?!")
        if not named:
            return None
        if self.resolve is None:
            return Understanding(
                kind="answer",
                reply="I cannot look at the computer registry from here, so I will not guess which one you mean.",
            )
        choice = self.resolve(named)
        if not choice.resolved:
            return Understanding(
                kind="answer", reply=describe_choice(choice), unresolved=(choice.reason,)
            )
        state = "" if choice.online else " It is offline right now, so running anything there will be refused."
        return Understanding(
            kind="device",
            reply=f"Target computer is now {choice.name} ({choice.device_id}).{state}",
            target=choice,
            device_id=choice.device_id,
            device_name=choice.name,
        )

    # -- diagnosis ---------------------------------------------------------

    def _diagnose(self, request: str, conversation: Conversation) -> Understanding | None:
        """Decompose "check my backend and explain the latest error" into two reads.

        ``None`` when the sentence is not a diagnosis request. When it is, this builds
        a plan — never a diagnosis. Nothing here reads a log, guesses a process id or
        claims a cause: the first step is an ordinary registered status read, the
        second is an ordinary registered log read whose ``process_id`` is *bound* to
        what the first step observed. If nothing was observed, the second step is
        denied rather than run against an invented id.

        The explanation happens after that, in a separate explicit step the user can
        see (:mod:`actionmesh.voiceos.diagnosis_flow`), because an explanation is a
        claim and a claim needs evidence the system actually holds.
        """
        if _DIAGNOSE.search(request) is None:
            return None
        if not (
            self.known_action(_DIAGNOSE_STATUS_ACTION)
            and self.known_action(_DIAGNOSE_LOG_ACTION)
        ):
            # Saying "I could not map that" is better than proposing a plan one of
            # whose steps this build cannot serve: a plan that cannot run is a
            # promise, and this layer does not make promises about capabilities it
            # has not been told about.
            return Understanding(
                kind="answer",
                reply=(
                    "This control service does not register the reads that would let me "
                    "check a server and read its log, so I will not propose a plan I "
                    "cannot run. Nothing has happened."
                ),
            )
        name = _diagnose_name(request)
        status_step = TaskStep(
            index=0,
            label=(
                f"Check the status of {name}"
                if name
                else "Check which of this computer's servers are running"
            ),
            action_type=_DIAGNOSE_STATUS_ACTION,
            parameters={"name": name} if name else {},
        )
        log_step = TaskStep(
            index=1,
            label="Read the most recent output of that server",
            action_type=_DIAGNOSE_LOG_ACTION,
            bind={
                "process_id": {
                    "from_step": 0,
                    "key": "servers",
                    "field": "process_id",
                }
            },
        )
        # The explanation is a third step *described*, never dispatched: it has no
        # action type, so nothing in the pipeline could run it even by mistake. It is
        # placed in the plan so the user sees that an explanation is coming and that
        # it is separate from the reads — which is the whole product promise.
        analysis_step = TaskStep(
            index=2,
            label="Explain the latest error and suggest a fix",
            action_type="",
            # Read by the diagnose route and never persisted: the question this session
            # is answering, so the explanation is about what was asked.
            analysis={
                "question": request,
                "computer": self.device_id or conversation.device_id,
            },
        )
        session = self._session(request, [status_step, log_step, analysis_step], conversation)
        listing = "; ".join(f"{step.index + 1}) {step.label}" for step in session.steps)
        return Understanding(
            kind="task",
            reply=(
                f"Here is how I would find out: {listing}. "
                f"Target computer: {self._current_device(conversation)}. Nothing has run "
                "yet — press run and I will read the real status and the real log, then "
                "explain the latest error and suggest a fix. I will not change anything."
            ),
            session=session,
            device_id=session.device_id,
        )

    # -- conversational intents --------------------------------------------

    def _status(self, conversation: Conversation) -> Understanding:
        if not conversation.active_session_id:
            where = self._current_device(conversation)
            return Understanding(
                kind="answer",
                reply=f"No task is in progress on {where}. Ask for something and I will show a plan first.",
                device_id=conversation.device_id,
            )
        session = conversation.active_session
        if session is None:  # pragma: no cover - the caller always loads it
            return Understanding(kind="answer", reply="The active task could not be read.")
        return Understanding(kind="answer", reply=describe_session(session), session=session)

    def _current_device(self, conversation: Conversation) -> str:
        """How to name the target in a sentence: by device id, never by role."""
        if conversation.device_id:
            return f"computer {conversation.device_id!r}"
        if self.device_id:
            return f"computer {self.device_id!r}"
        if self.local_device_id is not None:
            local = self.local_device_id()
            if local:
                return f"the local computer {local!r}"
        return "whichever computer you have selected"

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
                    f"I identified {target!r} on computer "
                    f"{self._current_device(conversation)}. Here is the stop "
                    "request — nothing has run yet."
                ),
                session=session,
                device_id=session.device_id,
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
                    f"Plan: 1) {step.label}, on {self._current_device(conversation)}. "
                    "Nothing has run yet."
                ),
                session=session,
                device_id=session.device_id,
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
                reply=(
                    f"Here is the plan from your configured task {template.name!r}: {listing}. "
                    f"Target computer: {self._current_device(conversation)}. Nothing has run yet."
                ),
                session=session,
                device_id=session.device_id,
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
                reply=(
                    f"Plan: {listing}. Target computer: {self._current_device(conversation)}. "
                    "Nothing has run yet."
                ),
                session=session,
                notes=notes,
                device_id=session.device_id,
            )
        missing = tuple(plan.notes) or ("no rule matched",)
        # A *drop* is different from no rule matching: the words were understood and
        # the action simply is not offered here. Saying "I could not map that" would
        # describe a parsing failure and hide the real one -- an operator who has not
        # enabled the laptop agent. Name the capability and the missing piece.
        dropped = sorted({
            note.split("'")[1]
            for note in plan.notes
            if note.startswith("dropped unregistered action type") and "'" in note
        })
        if dropped:
            return Understanding(
                kind="answer",
                reply=(
                    f"I understood that as asking for {', '.join(dropped)}, but this "
                    "control service does not offer it, so there is nothing to propose. "
                    "Enable the laptop agent (or the capability it needs) and ask again. "
                    "Nothing has run."
                ),
                unresolved=missing,
            )
        return Understanding(
            kind="answer",
            reply=(
                "I could not safely map that to an action this control service registers. "
                "Ask me what is running, name the target, or add a task template for it."
            ),
            unresolved=missing,
        )

    def _session(self, request: str, steps: list[TaskStep], conversation: Conversation) -> TaskSession:
        target = self.device_id or conversation.device_id
        for step in steps:
            # A step built from a reference inherits the target at *run* time (it
            # keeps ``""``), so a later "switch to the Mac" applies to it. A step
            # that already names its computer keeps it: a multi-device plan must
            # not be rewritten by a switch that came after it was proposed.
            if not step.device_id:
                step.device_id = target
        session = TaskSession(
            session_id=f"task_{uuid.uuid4().hex[:12]}",
            actor_id=conversation.actor_id,
            request=request,
            device_id=target,
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
    # The analysis step is not a step the orchestrator runs; it is filled by the diagnose
    # route. Counting it as outstanding would report a finished read as unfinished.
    work = [step for step in session.steps if not step.analysis]
    current: TaskStep | None = session.pending_step()
    parts = [
        f"Task {session.session_id}: {len(done)} of {len(work)} steps settled, status {session.status}."
    ]
    if session.device_id:
        # Always before the step list: a reader has to be able to see *which
        # computer* a task is about before reading what it did there.
        parts.append(f"Target computer: {session.device_id}.")
    for step in session.steps:
        evidence = ""
        if step.verified is True:
            evidence = " · verified"
        elif step.verified is False:
            evidence = " · NOT verified"
        elsewhere = (
            f" · on {step.device_id}"
            if step.device_id and step.device_id != session.device_id
            else ""
        )
        if step.analysis:
            answered = bool(str(step.analysis.get("result") or ""))
            state = "explained" if answered else "awaiting your request"
            parts.append(f"  {step.index + 1}) {step.label} — {state} (this is not run)")
            continue
        parts.append(
            f"  {step.index + 1}) {step.label} — {step.state}{evidence}{elsewhere}"
        )
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
