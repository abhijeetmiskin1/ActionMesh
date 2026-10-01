"""C18 — Intent to plan: the one step a model performs, and it may only propose.

The pipeline begins with a human intent ("tell mom I'll be late") and ends with an
executed, verified action. Exactly one step in that pipeline is performed by a
language model: turning an intent into a *candidate* plan. That output is
**untrusted** — the same class of data as an HTTP body — and this module draws the
boundary.

* :class:`Planner` is the structural interface: ``plan(intent) -> Plan``.
* :class:`RuleBasedPlanner` is a deterministic, offline reference planner, the
  planner counterpart of the mock providers. It makes the whole pipeline runnable
  without a network or a model, and it is reproducible.
* A :class:`Plan` carries *only* proposed action types and parameters. It has no
  field for risk, permissions, dependencies, authorization or lifecycle state,
  because no proposal may assert those. :meth:`Plan.as_payload` returns the
  untrusted ``{intent, actions[]}`` shape consumed by
  :func:`actionmesh.protocol.validate_plan`.

Nothing here authorizes or executes. AI proposes; policy decides; the user
controls.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from actionmesh.core.graph import graph_from_actions
from actionmesh.core.graph.errors import GraphValidationError
from actionmesh.protocol.models import ActionGraph


@dataclass(frozen=True)
class ProposedAction:
    """A proposed ``{type, parameters}`` pair: an intent to act, never consent.

    There is deliberately no ``risk_level``, ``permissions``, ``depends_on`` or
    ``state`` field. A proposal that could name its own risk or grant itself a
    permission would be the exact vulnerability this boundary exists to stop.
    """

    type: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "parameters": dict(self.parameters)}


@dataclass(frozen=True)
class Plan:
    """An untrusted candidate plan, plus notes on anything the planner dropped.

    ``notes`` exists so a dropped clause is *visible* rather than silently
    omitted: a plan that quietly ignored half an intent would look complete.
    """

    intent: str
    actions: tuple[ProposedAction, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.actions

    def as_payload(self) -> dict[str, Any]:
        """The untrusted ``{intent, actions[]}`` payload ``validate_plan`` reads."""
        return {"intent": self.intent, "actions": [a.as_dict() for a in self.actions]}


class Planner(Protocol):
    """Structural interface: intent text -> an untrusted candidate plan."""

    def plan(self, intent: str) -> Plan:  # pragma: no cover - protocol
        ...


def _action(action_type: str, **parameters: Any) -> ProposedAction:
    return ProposedAction(action_type, {k: v for k, v in parameters.items() if v not in (None, "")})


# -- rule table --------------------------------------------------------------
#
# Each rule is (action_type, compiled pattern, parameter builder). They are tried
# in order, most specific first, and the first match wins for a clause. The table
# is deliberately small and readable: this is a reference planner, not a general
# natural-language parser, so the patterns double as the documentation.


def _message(m: re.Match[str]) -> dict[str, Any]:
    return {"to": m.group("to").strip(), "message": m.group("body").strip()}


def _reminder(m: re.Match[str]) -> dict[str, Any]:
    return {"text": m.group("text").strip(), "time": (m.group("time") or "").strip()}


def _calendar(m: re.Match[str]) -> dict[str, Any]:
    return {"title": m.group("title").strip(), "time": (m.group("time") or "").strip()}


def _search(m: re.Match[str]) -> dict[str, Any]:
    return {"query": m.group("query").strip()}


def _file(m: re.Match[str]) -> dict[str, Any]:
    return {"path": m.group("path").strip()}


def _launch(m: re.Match[str]) -> dict[str, Any]:
    return {"app": m.group("app").strip()}


def _payment(m: re.Match[str]) -> dict[str, Any]:
    return {"recipient": m.group("recipient").strip(), "amount": float(m.group("amount"))}


def _notify(m: re.Match[str]) -> dict[str, Any]:
    return {"device_id": m.group("device_id").strip(), "title": m.group("title").strip()}


_RULES: tuple[
    tuple[str, re.Pattern[str], Callable[[re.Match[str]], dict[str, Any]]], ...
] = (
    # messaging: "tell alice: hi" / "message bob, running late" (colon or comma),
    # then "tell alice that ...", then the bare "tell mom I'll be late" form.
    ("message.send", re.compile(
        r"^(?:send\s+(?:a\s+)?(?:message|text)\s+to|message|text|tell)\s+"
        r"(?P<to>[^:,]+?)\s*[:,]\s*(?P<body>.+)$", re.I), _message),
    ("message.send", re.compile(
        r"^(?:tell|message|text)\s+(?P<to>\S+)\s+that\s+(?P<body>.+)$", re.I), _message),
    ("message.send", re.compile(
        r"^(?:tell|message|text)\s+(?P<to>\S+)\s+(?P<body>.+)$", re.I), _message),
    # reminders
    ("reminder.create", re.compile(
        r"^(?:remind me to|remind me|set a reminder to|reminder)\s*:?\s+"
        r"(?P<text>.+?)(?:\s+at\s+(?P<time>.+))?$", re.I), _reminder),
    # calendar
    ("calendar.create", re.compile(
        r"^(?:schedule|create (?:an? )?(?:event|meeting|appointment)|book)\s+"
        r"(?P<title>.+?)(?:\s+(?:at|on)\s+(?P<time>.+))?$", re.I), _calendar),
    # web search
    ("search.web", re.compile(
        r"^(?:search(?: the web)? for|search|look up|google)\s+(?P<query>.+)$", re.I), _search),
    # files (before launch, so "open file notes.txt" is a read, not an app)
    ("file.read", re.compile(
        r"^(?:read|show|open)\s+(?:the\s+)?file\s+(?P<path>\S+)$", re.I), _file),
    # payments — CRITICAL downstream; the planner only proposes, never confirms
    ("payment.prepare", re.compile(
        r"^pay\s+(?P<recipient>\S+)\s+(?P<amount>\d+(?:\.\d+)?)$", re.I), _payment),
    ("payment.prepare", re.compile(
        r"^(?:send|transfer)\s+(?P<amount>\d+(?:\.\d+)?)\s+to\s+(?P<recipient>.+)$",
        re.I), _payment),
    # device notification
    ("device.notify", re.compile(
        r"^notify\s+(?P<device_id>\S+)\s*[:,]?\s*(?P<title>.+)$", re.I), _notify),
    # application launch (last: it is the broadest pattern)
    ("system.launch_app", re.compile(
        r"^(?:open|launch|start)\s+(?:the\s+)?(?:app\s+)?(?P<app>[\w .+-]+)$", re.I), _launch),
)


_CLAUSE_SPLIT = re.compile(r"\s*;\s*|\s+then\s+", re.I)


def _clauses(text: str) -> list[str]:
    """Split a compound intent into clauses on ``;`` or the word "then"."""
    return [part.strip() for part in _CLAUSE_SPLIT.split(text) if part.strip()]


def _match(clause: str) -> ProposedAction | None:
    """First matching rule wins; a clause with no rule yields ``None``."""
    for action_type, pattern, build in _RULES:
        match = pattern.match(clause)
        if match:
            return _action(action_type, **build(match))
    return None


class RuleBasedPlanner:
    """Deterministic, offline reference planner (the mock-provider analogue).

    It recognises a small, fixed set of patterns — the rule table above — and
    proposes at most :attr:`max_actions` actions. It is *not* a general
    natural-language parser, and it makes no authorization judgement. A compound
    intent (clauses separated by ``;`` or ``then``) yields one proposal per clause,
    which downstream becomes a linear dependency chain.

    ``known_action`` may be supplied (for example a predicate over the registry) so
    the planner declines to propose a type it should not; the decline is recorded
    in :attr:`Plan.notes` rather than happening silently.
    """

    def __init__(
        self,
        *,
        known_action: Callable[[str], bool] | None = None,
        max_actions: int = 8,
    ) -> None:
        if max_actions < 1:
            raise ValueError("max_actions must be at least 1")
        self.known_action = known_action
        self.max_actions = max_actions

    def plan(self, intent: str) -> Plan:
        if not isinstance(intent, str) or not intent.strip():
            return Plan(intent="", notes=("intent was empty",))
        text = " ".join(intent.split())
        clauses = _clauses(text)
        proposals: list[ProposedAction] = []
        notes: list[str] = []
        for index, clause in enumerate(clauses):
            if len(proposals) >= self.max_actions:
                notes.append(
                    f"stopped at max_actions={self.max_actions}; "
                    f"{len(clauses) - index} clause(s) left unplanned"
                )
                break
            proposed = _match(clause)
            if proposed is None:
                notes.append(f"no rule matched: {clause!r}")
                continue
            if self.known_action is not None and not self.known_action(proposed.type):
                notes.append(f"dropped unregistered action type {proposed.type!r}")
                continue
            proposals.append(proposed)
        return Plan(intent=text, actions=tuple(proposals), notes=tuple(notes))


def plan_to_graph(plan: Plan) -> ActionGraph:
    """Cross the trust boundary: validate an untrusted plan into a trusted graph.

    Everything before this call is a *proposal*; everything after it is protocol
    data the registry, policy engine and executor may act on. The actions are
    re-validated as untrusted input and chained in order with a terminal node, so
    the result is a graph the executor accepts. An empty plan is rejected rather
    than executed as a no-op, and node ids are deterministic (the same plan always
    yields the same graph).
    """
    if plan.empty:
        raise GraphValidationError("plan proposes no actions; refusing to build a graph")
    return graph_from_actions([action.as_dict() for action in plan.actions], intent=plan.intent)


__all__ = ["Plan", "Planner", "ProposedAction", "RuleBasedPlanner", "plan_to_graph"]


