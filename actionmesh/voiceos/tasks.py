"""Operator-authored task templates — the only place a multi-step plan gets its details.

"Run my tests" needs a test command. There are exactly two honest sources for one: the
words the user just said, and something the operator configured. A language model is not
either of them, and neither is a guess — which is why the reference planner deliberately
maps ``run the tests`` to *nothing*. This module is where an operator can say "for this
laptop, that request means these steps", and where those steps are checked before they
are ever offered.

A template is still only a proposal. Each step it contains goes through registry
resolution, parameter validation, the policy engine, consent, risk and the approval gate
exactly like a typed command would; nothing here bypasses any of that. What the template
*does* carry is authorship: an action type or argument that arrived from a model or a
transcript could not have come from here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: A plan longer than this is a sign of a badly written template, not a clever user.
MAX_STEPS = 8

#: Keys a step's ``when`` condition may inspect on an earlier step's observed result.
CONDITION_KEYS = frozenset({"exit_code", "state", "verified", "error"})


class TemplateError(ValueError):
    """A template that must not be loaded. The reason is the point of the exception."""


@dataclass(frozen=True)
class TaskTemplate:
    name: str
    match: tuple[str, ...]
    steps: tuple[dict[str, Any], ...]

    def phrases(self) -> tuple[str, ...]:
        return self.match


@dataclass(frozen=True)
class TemplateSource:
    """What loaded, what was refused, and why. A refused entry is never silent."""

    templates: tuple[TaskTemplate, ...] = ()
    refusals: tuple[str, ...] = ()
    source: str = ""


def _scalar_or_list(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return True
    return isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value)


def _validate_step(raw: Any, position: int, known_action: Callable[[str], bool]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TemplateError(f"step {position} is not an object")
    label = str(raw.get("label") or "").strip()
    action_type = str(raw.get("action") or "").strip()
    if not label:
        raise TemplateError(f"step {position} has no label")
    if not action_type:
        raise TemplateError(f"step {position} has no action")
    if not known_action(action_type):
        # A template that names an action nobody registered is a template written
        # against a machine that does not exist. Refuse it at load, not at run.
        raise TemplateError(f"step {position} names unregistered action {action_type!r}")
    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise TemplateError(f"step {position} parameters are not an object")
    for key, value in parameters.items():
        if not isinstance(key, str):
            raise TemplateError(f"step {position} has a non-string parameter name")
        if not _scalar_or_list(value):
            raise TemplateError(f"step {position} parameter {key!r} is not a scalar or flat list")
    when = raw.get("when") or {}
    if not isinstance(when, dict):
        raise TemplateError(f"step {position} when is not an object")
    if when:
        target = when.get("step")
        if not isinstance(target, int) or target < 0 or target >= position:
            raise TemplateError(f"step {position} conditions on a step that is not earlier than it")
        unknown = set(when) - {"step"} - CONDITION_KEYS
        if unknown:
            raise TemplateError(
                f"step {position} conditions on {sorted(unknown)}, which is not observed evidence"
            )
    return {"label": label, "action": action_type, "parameters": dict(parameters), "when": dict(when)}


def _validate_task(raw: Any, position: int, known_action: Callable[[str], bool]) -> TaskTemplate:
    if not isinstance(raw, dict):
        raise TemplateError(f"task {position} is not an object")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise TemplateError(f"task {position} has no name")
    match = raw.get("match")
    if not isinstance(match, list) or not match:
        raise TemplateError(f"task {name!r} has no match phrases")
    phrases = tuple(str(item).strip().lower() for item in match if str(item).strip())
    if not phrases:
        raise TemplateError(f"task {name!r} has only blank match phrases")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise TemplateError(f"task {name!r} has no steps")
    if len(steps) > MAX_STEPS:
        raise TemplateError(f"task {name!r} has {len(steps)} steps; the limit is {MAX_STEPS}")
    validated = tuple(_validate_step(step, index, known_action) for index, step in enumerate(steps))
    return TaskTemplate(name=name, match=phrases, steps=validated)


def load_templates(
    path: str | Path | None, known_action: Callable[[str], bool]
) -> TemplateSource:
    """Read operator-authored task templates. A missing file is not an error.

    A *malformed* file is reported rather than half-applied: a template that silently
    failed to load would make the next "run my tests" come back as "I could not safely
    map that", and the operator would have no way to know their own file was the cause.
    """
    if not path:
        return TemplateSource()
    target = Path(path)
    if not target.exists():
        return TemplateSource(source=str(target))
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return TemplateSource(refusals=(f"{target}: unreadable ({exc})",), source=str(target))
    tasks = raw.get("tasks") if isinstance(raw, dict) else raw
    if not isinstance(tasks, list):
        return TemplateSource(refusals=(f"{target}: expected a list under 'tasks'",), source=str(target))
    templates: list[TaskTemplate] = []
    refusals: list[str] = []
    for position, entry in enumerate(tasks):
        try:
            templates.append(_validate_task(entry, position, known_action))
        except TemplateError as exc:
            refusals.append(f"{target}: task {position} refused — {exc}")
    return TemplateSource(templates=tuple(templates), refusals=tuple(refusals), source=str(target))


def match_template(templates: tuple[TaskTemplate, ...], text: str) -> TaskTemplate | None:
    """Longest phrase wins, so 'run the tests' beats 'tests'."""
    haystack = " ".join(text.lower().split())
    best: TaskTemplate | None = None
    best_length = 0
    for template in templates:
        for phrase in template.phrases():
            if phrase and phrase in haystack and len(phrase) > best_length:
                best = template
                best_length = len(phrase)
    return best


__all__ = [
    "CONDITION_KEYS",
    "MAX_STEPS",
    "TaskTemplate",
    "TemplateError",
    "TemplateSource",
    "load_templates",
    "match_template",
]
