"""Drafting a workflow from a sentence — a proposal, and nothing more.

"Every morning at nine, check the server and then read its log if it failed" is a
reasonable thing to ask for. What must not happen is that saying it creates a running
automation. So this module's output is a **draft**: steps, a trigger and a name, shown
to the user for review. Saving it is a separate, explicit act, and saving it still
grants nothing — the steps are authorised when they run, against whoever is running.

Where the steps come from
-------------------------
There are two honest sources, and the draft says which one it used:

* an **operator template** (``actionmesh/voiceos/tasks.py``) whose phrase appears in
  the sentence — authored by a person, validated at load, and the only source that can
  ever produce more than a trigger;
* otherwise **nothing**. The draft carries the trigger it understood (a schedule, an
  event) and an empty step list, with ``reason`` explaining that no steps were
  invented. A model is consulted only to *recognise* a schedule, and only if a
  provider is configured; it is never asked to emit an action, a parameter or a
  condition, because a model-authored step would mean a plan could carry an action
  type no operator wrote.

Why the model may parse a schedule but not a step
-------------------------------------------------
A misheard schedule is visible in the review screen — "every day at 09:00" is a thing
a person can check at a glance — and it starts nothing until saved. A model-authored
step, by contrast, is an action type and parameters that a human would have to audit
as if they had written them. The asymmetry is the point: the parsing failure mode is
visible, the step failure mode is plausible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from actionmesh.voiceos.tasks import TaskTemplate, match_template
from actionmesh.voiceos.workflows import (
    MAX_NAME,
    WorkflowError,
    validate_event,
    validate_schedule,
)

#: Longest sentence this will consider. Longer is a document, not a request.
MAX_SENTENCE = 600

#: Weekday words the parser understands in a weekly schedule.
_DAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

#: "... every 15 minutes", "each hour", "every 2 hours".
_INTERVAL = re.compile(
    r"\bevery\s+(\d+)?\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE
)
#: "at 9am", "at 09:30", "at 18:00".
_CLOCK = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)
#: "when the build fails", "if an execution is denied".
_WHEN = re.compile(
    r"\b(?:when|if|whenever)\s+(?:an?\s+|the\s+)?([a-z_.]+(?:\s+[a-z_.]+){0,3}?)"
    r"\s+(fails?|failed|denied|is denied|is offline|goes offline|completes?|succeeds?)\b",
    re.IGNORECASE,
)
_DAILY = re.compile(r"\b(every day|daily|each day)\b", re.IGNORECASE)
_WEEKLY = re.compile(r"\b(every week|weekly|each week)\b", re.IGNORECASE)

#: The event triggers a sentence can name. Each entry is the stored ``event`` object
#: exactly as :func:`validate_event` will normalise it, so a draft never describes a
#: trigger the save route would refuse. "When the build fails" is ``execution.completed``
#: narrowed to ``state: failed`` — the executor publishes one completion event and puts
#: the verdict in its state, so there is no separate failure event to name.
_EVENT_PHRASES: tuple[tuple[str, tuple[str, ...], dict[str, str]], ...] = (
    (
        "a run fails",
        ("fail", "failed", "fails", "failing"),
        {"event_type": "execution.completed", "state": "failed"},
    ),
    (
        "a run is denied",
        ("denied", "deny", "denies", "refused"),
        {"event_type": "execution.denied"},
    ),
    (
        "a run completes",
        ("completes", "completed", "succeeds", "succeeded", "finished"),
        {"event_type": "execution.completed"},
    ),
    (
        "a run is interrupted",
        ("interrupted", "interrupts"),
        {"event_type": "execution.interrupted"},
    ),
)


@dataclass
class Draft:
    """A proposed workflow. **It is not stored and it runs nothing.**

    ``created: False`` is a field rather than a constant in the response so no caller
    can mistake a draft for a workflow by reading the same shape twice.
    """

    name: str = ""
    description: str = ""
    schedule: dict[str, Any] = field(default_factory=dict)
    event: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    #: Where the steps came from: ``"template:<name>"``, ``"none"``.
    source: str = "none"
    #: Why the draft is partial, in one sentence, when it is.
    reason: str = ""
    #: The words that produced the trigger, so the review screen can show the basis.
    understood: str = ""
    created: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "schedule": dict(self.schedule),
            "event": dict(self.event),
            "steps": [dict(step) for step in self.steps],
            "source": self.source,
            "reason": self.reason,
            "understood": self.understood,
            "created": False,
        }


def _name_for(text: str) -> str:
    """A short name from the sentence: the first clause, bounded."""
    clause = re.split(r"[,.;]", text.strip(), maxsplit=1)[0].strip()
    name = clause[:MAX_NAME].strip()
    return name or "Untitled automation"


def _interval(sentence: str) -> tuple[dict[str, Any], str] | None:
    match = _INTERVAL.search(sentence)
    if match is None:
        return None
    amount = int(match.group(1) or 1)
    unit = match.group(2).lower()
    if unit.startswith("sec"):
        seconds = amount
    elif unit.startswith(("min",)):
        seconds = amount * 60
    else:
        seconds = amount * 3600
    return {"kind": "interval", "seconds": seconds}, match.group(0)


def _clock_time(sentence: str) -> str | None:
    match = _CLOCK.search(sentence)
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _weekly_days(sentence: str) -> list[int]:
    found: list[int] = []
    for word, index in _DAYS.items():
        if re.search(rf"\b{word}\b", sentence.lower()) and index not in found:
            found.append(index)
    return sorted(found)


def parse_schedule(sentence: str) -> tuple[dict[str, Any], str]:
    """Recognise a schedule from the sentence, or return ``({}, reason)``.

    Deterministic and local: no model is consulted. The three shapes this program
    supports are exactly the three it can recognise, so a draft never describes a
    schedule the scheduler cannot run.
    """
    interval = _interval(sentence)
    if interval is not None:
        return interval
    weekly = _WEEKLY.search(sentence)
    daily = _DAILY.search(sentence)
    if weekly or daily:
        at = _clock_time(sentence)
        if at is None:
            return {}, "a recurring schedule was recognised but no time of day was given"
        if weekly:
            days = _weekly_days(sentence)
            if not days:
                return {}, (
                    "a weekly schedule was recognised but no weekday was given "
                    "(say 'every Monday')"
                )
            return {"kind": "weekly", "days": days, "at": at}, weekly.group(0)
        if daily is None:
            return {}, "a recurring schedule was recognised but no time of day was given"
        return {"kind": "daily", "at": at}, daily.group(0)
    at = _clock_time(sentence)
    if at is not None and re.search(r"\b(morning|evening|night|afternoon)\b", sentence, re.I):
        return {"kind": "daily", "at": at}, "a time of day was given"
    return {}, ""


def parse_event(sentence: str) -> tuple[dict[str, Any], str]:
    """Recognise an event trigger from the sentence, or ``({}, reason)``."""
    match = _WHEN.search(sentence)
    if match is None:
        return {}, ""
    subject = match.group(1).lower().strip()
    verb = match.group(2).lower().strip()
    if "device" in subject:
        # The device plane records presence on demand rather than publishing an
        # "offline" event, so a trigger on one would never fire. Saying so is better
        # than storing a workflow that looks armed and is not.
        return {}, (
            "a device going offline is not an event this program publishes, so a "
            "workflow cannot be triggered by it"
        )
    for label, verbs, event in _EVENT_PHRASES:
        if verb in verbs:
            return dict(event), f"when {subject} {verb} ({label})"
    return {}, f"the trigger {match.group(0)!r} was not recognised as an event"


def _steps_from_template(template: TaskTemplate) -> list[dict[str, Any]]:
    return [
        {
            "label": step["label"],
            "action": step["action"],
            "parameters": dict(step.get("parameters") or {}),
            "when": dict(step.get("when") or {}),
        }
        for step in template.steps
    ]


def draft(
    sentence: str,
    *,
    templates: tuple[TaskTemplate, ...] = (),
    actor_id: str = "",
) -> Draft:
    """Turn a sentence into a reviewable draft. **Nothing is stored or started.**

    The returned draft is deliberately not a :class:`Workflow`: it has no id and no
    ``created`` flag, so passing it straight to the save route would fail validation
    on its missing name rather than silently persisting a half-understood plan.
    """
    text = " ".join((sentence or "").split())
    out = Draft(name=_name_for(text), description="")
    if not text:
        out.reason = "there was no sentence to read"
        return out
    if len(text) > MAX_SENTENCE:
        out.reason = f"the sentence is longer than {MAX_SENTENCE} characters"
        return out

    schedule, schedule_basis = parse_schedule(text)
    event, event_basis = parse_event(text)
    notes: list[str] = []
    # A schedule and an event are both possible readings of one sentence. This program
    # stores at most one trigger, so the draft takes the explicit schedule when both
    # appear and says what it did not take.
    if schedule and event:
        notes.append(
            f"it mentions both {schedule_basis!r} and {event_basis!r}; "
            "only the schedule was kept"
        )
        event = {}
    out.schedule = schedule
    out.event = event
    out.understood = schedule_basis or event_basis
    if schedule:
        # Validate through the same function a save goes through, so a draft never
        # describes a schedule that cannot be saved.
        try:
            out.schedule = validate_schedule(schedule)
        except WorkflowError as exc:
            notes.append(str(exc))
            out.schedule = {}
    if out.event:
        try:
            out.event = validate_event(out.event)
        except WorkflowError as exc:
            notes.append(str(exc))
            out.event = {}

    template = match_template(templates, text) if templates else None
    if template is not None:
        out.steps = _steps_from_template(template)
        out.source = f"template:{template.name}"
    else:
        out.source = "none"
        notes.append(
            "no operator template matched this sentence, so no steps were written: "
            "a step has to come from a phrase an operator authored, not from a guess"
        )

    if not out.schedule and not out.event:
        notes.insert(0, "no trigger was recognised, so this would have to be run by hand")
    if actor_id:
        out.description = f"proposed for {actor_id}"
    out.reason = " ".join(note for note in notes if note)
    return out


def draft_to_body(d: Draft) -> dict[str, Any]:
    """The shape ``POST /automations`` accepts. Empty fields are omitted.

    ``enabled`` is stated, and stated as ``False``. It is not omitted, because
    the save route's default is ``True`` — a plan arriving without the field is
    a plan whose owner meant to arm it — and a draft is the one body that was
    never authored by anyone who saw a trigger. A sentence may have named a
    schedule; inheriting "armed" from that is how pressing "save" on a reading
    of your own words would start firing at nine with nobody having switched it
    on. Saving stores the plan; arming is the next, separate act.
    """
    body: dict[str, Any] = {"name": d.name, "steps": list(d.steps), "enabled": False}
    if d.description:
        body["description"] = d.description
    if d.schedule:
        body["schedule"] = dict(d.schedule)
    if d.event:
        body["event"] = dict(d.event)
    return body


#: What a configured model may be asked. Deliberately not "produce a workflow": the
#: only thing a model is used for is saying which of the three schedule shapes a
#: sentence names, and even that answer is validated by :func:`validate_schedule`.
SCHEDULE_INSTRUCTION = (
    "Read the sentence and return ONLY a JSON object, no prose, from this closed set:\n"
    '{"kind": "interval", "seconds": <int>}\n'
    '{"kind": "daily", "at": "HH:MM"}\n'
    '{"kind": "weekly", "days": [<0-6, Monday=0>], "at": "HH:MM"}\n'
    'If no schedule is named, return {}. Do not invent times the sentence does not state.'
)

__all__ = [
    "MAX_SENTENCE",
    "SCHEDULE_INSTRUCTION",
    "Draft",
    "draft",
    "draft_to_body",
    "parse_event",
    "parse_schedule",
]