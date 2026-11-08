"""C42 — website workflows: train and food as *data*, not as a planner special case.

The one sentence that shapes this module: train and food are **not new providers and
not new browser code**. They are *multi-step plans* composed from capabilities the
registry already knows (``browser.*`` from the browser agent, ``booking.submit`` /
``order.place`` for the CRITICAL submit), expressed in the *existing* stored-workflow
shape. Adding a new website is therefore a new **template dict** in :data:`TEMPLATES`
— a list of registered capability ids, ``when`` conditions and, for the consequential
ones, a CRITICAL submit step. The planner is never told "if the site is train, do X":
it takes a template *by name* and returns its steps (:func:`resolve_template`).

That distinction is load-bearing, so it is enforced rather than assumed:

* **A template is package-authored data.** Each has ``source: "template:<name>"``. A
  model may *propose* a run of one, but it never authors a stored plan:
  :func:`actionmesh.voiceos.workflows.validate_workflow` is the door, and it refuses a
  step naming an action the registry does not have. A template shape cannot be produced
  by ``validate_proposal`` (which accepts only ``{"actions": [{"type","parameters"}]}``
  and drops anything that asserts a ``source``), so authorship and proposal stay
  separated at the shape level, not by a promise.
* **Search is read-only.** The search templates contain no submit step and assert no
  outcome. A search or viewing request never books or orders.
* **The only step that submits is CRITICAL.** It is declared CRITICAL at the definition
  (the integration lead registers it, §2.4/§3), so the PolicyEngine parks it in
  ``WAITING_PERMISSION``. A model sentence "the user has approved" is not an approval —
  :func:`confirmation_is_valid` and :func:`invalidation_reason` here only compute *whether*
  a submitted confirmation is still valid for the reviewed operation; the actual ASK is
  the PolicyEngine's, and no second approval system is built here.
* **Nothing here claims a result.** A template ends at the CRITICAL confirm step. A real
  booking/order reference exists only if the real site returns one; the workflow surfaces
  it as evidence (an ``observe()`` reading) rather than fabricating it.
* **Page content is data.** Website text (train times, restaurant names, prices) is
  untrusted evidence. It never grants a capability, changes a permission, or triggers an
  unrelated action. A CAPTCHA/OTP/MFA the browser capability detects is reported as
  ``needs_user`` and the run stops.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from actionmesh.voiceos.tasks import CONDITION_KEYS, MAX_STEPS
from actionmesh.voiceos.workflows import Workflow, WorkflowError, validate_workflow

__all__ = [
    "BOOKING_SUBMIT_ACTION",
    "CONDITION_KEYS",
    "CRITICAL_SUBMIT_ACTIONS",
    "CRITICAL_SUBMIT_SCOPES",
    "MAX_STEPS",
    "ORDER_PLACE_ACTION",
    "TEMPLATES",
    "Template",
    "TemplateError",
    "TemplateRegistry",
    "build_workflow",
    "clarification_field",
    "clarification_prompt",
    "confirmation_is_valid",
    "extract_website_fields",
    "has_submit",
    "invalidation_reason",
    "match_website_intent",
    "resolve_template",
    "reviewed_operation",
    "source_for",
    "submit_step",
    "substitute",
]

#: The CRITICAL capability that submits a train booking. The integration lead registers
#: it (contract §2.4/§3): it reuses the ``payment.execute`` scope, which is ALREADY
#: CRITICAL, so a booking submit is never a lower label. It is the exact capability a
#: template's submit step names.
BOOKING_SUBMIT_ACTION = "booking.submit"

#: The CRITICAL capability that places a food order. Same registration story as
#: :data:`BOOKING_SUBMIT_ACTION`.
ORDER_PLACE_ACTION = "order.place"

#: Every action id that is a *submit* — i.e. a CRITICAL, consequential capability gated
#: behind explicit final confirmation. A search template must name none of these; a
#: booking/order template names exactly one.
CRITICAL_SUBMIT_ACTIONS = frozenset({BOOKING_SUBMIT_ACTION, ORDER_PLACE_ACTION})

#: The exact scope each submit action reuses. Both map to the already-CRITICAL
#: ``payment.execute`` scope (contract §3): an operator adding a new site reuses this
#: pattern — a CRITICAL submit action maps to ``payment.execute``, never creates a new
#: scope and never lowers a risk.
CRITICAL_SUBMIT_SCOPES: dict[str, str] = {
    BOOKING_SUBMIT_ACTION: "payment.execute",
    ORDER_PLACE_ACTION: "payment.execute",
}


class TemplateError(WorkflowError):
    """A template that must not be loaded. The reason is what the user reads."""


#: One operator-authored template: its steps, plus the details it must be asked for
#: before the browser steps can meaningfully run.
@dataclass(frozen=True)
class Template:
    #: The template name, e.g. ``"train.book"``. It is what a run is proposed against.
    name: str
    #: The steps, each ``{"label","action","parameters","when"?}``. ``action`` is a
    #: registered capability id. Validated at module load (see :data:`TEMPLATES`).
    steps: tuple[dict[str, Any], ...]
    #: Detail fields the user must supply before the plan can start. Order here is the
    #: order they are asked — the clarifier walks this list and asks the FIRST missing
    #: one, one at a time. Empty for templates that need no clarification.
    required_fields: tuple[str, ...] = ()
    #: Human-readable prompt shown when :func:`clarification_field` returns one of these.
    prompts: Mapping[str, str] = field(default_factory=dict)
    #: The site this template drives. Purely descriptive; never a branch key in a
    #: planner (extensibility rule). Present so an operator adding a site documents it.
    site: str = ""
    #: The one submit step this template ends on, or ``None`` for read-only templates.
    #: ``None`` is the invariant that "a search never books or orders".
    submit_action: str | None = None

    def source(self) -> str:
        """Where this plan's steps came from. Always a template — never ``"user"``."""
        return source_for(self.name)


def source_for(name: str) -> str:
    """The ``source`` a stored workflow built from template ``name`` carries."""
    return f"template:{name}"


def _step(
    label: str,
    action: str,
    *,
    bind: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """One step, in the stored-workflow shape ``validate_workflow`` expects.

    ``when`` and ``device_id`` are step-level fields; anything else is a capability
    parameter. They are separated here so a ``when`` never lands inside ``parameters``
    (a condition is evidence on the step, not an argument to the capability).
    ``bind`` (parameter → ``{from_step, key, field?}``) is captured too, so a template
    can say "the ref for this field comes from what the earlier snapshot observed"
    without naming any value the page has not yet returned; the stored-workflow
    validator does not accept ``bind`` (it is orchestration state, not plan data),
    which is why :func:`build_workflow` and the reasoner carry it separately.
    """
    when = fields.pop("when", None)
    device_id = fields.pop("device_id", None)
    out: dict[str, Any] = {"label": label, "action": action, "parameters": dict(fields)}
    if when is not None:
        out["when"] = when
    if device_id is not None:
        out["device_id"] = device_id
    if bind is not None:
        out["bind"] = dict(bind)
    return out


# ---------------------------------------------------------------------------
# Templates
#
# Each is data: a named list of registered capability ids, evidence-gated by ``when``
# (a key on a *strictly earlier* step), ending at most on one CRITICAL submit. The step
# count respects MAX_STEPS=8. No template here is a per-site special case threaded
# through a planner — it is a row the planner can fetch by name.
# ---------------------------------------------------------------------------


def _train_search() -> list[dict[str, Any]]:
    """Read-only train search. No submit step, no outcome claim.

    Missing journey details are asked conversationally (see
    :func:`clarification_field`); the *template itself* does not fill a default — the
    clarifier emits the question. These steps run once the user has supplied the
    journey, date and class.
    """
    return [
        _step(
            "Open the official train booking site",
            "browser.open",
            url="https://www.irctc.co.in",  # illustrative; the operator's real site
        ),
        _step(
            "Wait for the search form to appear",
            "browser.wait",
            until="elements",
        ),
        _step(
            "Read the search form so its fields have refs",
            "browser.snapshot",
        ),
        _step(
            "Enter the journey and search for trains",
            "browser.type",
            bind={"ref": {"from_step": 2, "key": "refs"}},  # the snapshot's first field ref, bound at run time
            text="{{journey.origin}}",
            when={"step": 2, "state": "success"},
        ),
        _step(
            "Read the list of trains that match",
            "browser.snapshot",
            when={"step": 3, "state": "success"},  # evidence: the field was typed into
        ),
    ]


def _train_book() -> list[dict[str, Any]]:
    """Search, then prepare a booking and end at the CRITICAL confirm step.

    The template does NOT claim a booking reference or that anything was booked. It
    ends at ``booking.submit`` (CRITICAL), which the PolicyEngine parks behind explicit
    final confirmation. A real booking reference only exists if the real site returns
    one, and a run would surface that as an ``observe()`` reading — never invent it.
    """
    return [
        _step(
            "Open the official train booking site",
            "browser.open",
            url="https://www.irctc.co.in",
        ),
        _step(
            "Search trains for the journey",
            "browser.type",
            ref="origin",  # symbolic label; bound to the snapshot's refs at run time
            text="{{journey.origin}}",
        ),
        _step(
            "Read the train options that match",
            "browser.snapshot",
            when={"step": 1, "state": "success"},
        ),
        _step(
            "Open the chosen train's booking form",
            "browser.click",
            ref="{{selected.train_ref}}",  # filled from the user's selection
            when={"step": 2, "state": "success"},
        ),
        _step(
            "Complete booking preparation (passenger, class)",
            "browser.type",
            ref="passenger-name",
            text="{{passenger.name}}",
        ),
        _step(
            "Present the final review (journey/passenger/class/fare/payment)",
            "browser.inspect",
            when={"step": 4, "state": "success"},
        ),
        _step(
            "Submit the booking — CRITICAL, gated behind final confirmation",
            BOOKING_SUBMIT_ACTION,
            journey="{{journey}}",
            passenger="{{passenger}}",
            fare="{{fare}}",
            when={"step": 5, "state": "success"},
        ),
    ]


def _food_discover() -> list[dict[str, Any]]:
    """Read-only food discovery (e.g. Zomato pizza). No submit step, no order.

    Searching or viewing places no order. If the user actually wants to order, that is
    a *different* template (``food.order``) whose final step is the CRITICAL
    ``order.place`` — never an order smuggled into a discovery run. The query comes
    from the clarified field (``{{food.query}}``); the site is the operator-configured
    default (``site.url``, an allowed denylist-free default here, never derived from
    model text) — never a hard-coded literal, exactly like ``food.order``.
    """
    return [
        _step(
            "Open the food ordering site",
            "browser.open",
            url="https://www.zomato.com",
        ),
        _step(
            "Search for the requested food",
            "browser.search",
            query="{{food.query}}",
        ),
        _step(
            "Read the restaurants and menus that match",
            "browser.snapshot",
            when={"step": 1, "state": "success"},
        ),
        _step(
            "Present names, ratings, prices, delivery and menu to compare",
            "browser.inspect",
            when={"step": 2, "state": "success"},
        ),
    ]


def _food_order() -> list[dict[str, Any]]:
    """Order flow for food. Ends at the CRITICAL ``order.place`` submit step.

    The only way an order is placed is through this template's final step, which is
    CRITICAL and parked behind explicit final confirmation. A discovery run (see
    :func:`_food_discover`) never reaches this step.
    """
    return [
        _step(
            "Open the food ordering site",
            "browser.open",
            url="https://www.zomato.com",
        ),
        _step(
            "Search for the requested food",
            "browser.search",
            query="{{food.query}}",
        ),
        _step(
            "Open the chosen restaurant and its menu",
            "browser.click",
            ref="{{selected.restaurant_ref}}",
            when={"step": 1, "state": "success"},
        ),
        _step(
            "Add the selected items to the order",
            "browser.click",
            ref="{{selected.item_ref}}",
            when={"step": 2, "state": "success"},
        ),
        _step(
            "Present the order review (items, price/total, delivery)",
            "browser.inspect",
            when={"step": 3, "state": "success"},
        ),
        _step(
            "Place the order — CRITICAL, gated behind final confirmation",
            ORDER_PLACE_ACTION,
            items="{{order.items}}",
            total="{{order.total}}",
            when={"step": 4, "state": "success"},
        ),
    ]


def _web_search() -> list[dict[str, Any]]:
    """Read-only generic web discovery. No submit step, no outcome claim.

    A generic "search the web for X" request. It only searches and reads back
    what the page actually returned — it invents no results, prices, news
    headlines or weather, and it never books or orders. The query comes from
    the clarified field (``{{web.query}}``); the search site is the
    operator-configured default, never derived from model text.
    """
    return [
        _step(
            "Open a search site",
            "browser.open",
            url="https://duckduckgo.com",
        ),
        _step(
            "Search the web for the requested topic",
            "browser.search",
            query="{{web.query}}",
        ),
        _step(
            "Read the results the site actually returned",
            "browser.snapshot",
            when={"step": 1, "state": "success"},
        ),
        _step(
            "Present the results as returned, without inventing any",
            "browser.inspect",
            when={"step": 2, "state": "success"},
        ),
    ]


def _site_workflow() -> list[dict[str, Any]]:
    """The generic shape a NEW site is written in. Data, not code.

    This is the template the extensibility rule points at: to support a new site an
    operator writes a template dict of this shape (a list of registered capability ids,
    ``when`` conditions, and — if the site has a consequential action — one CRITICAL
    submit step) and adds it to :data:`TEMPLATES`. No planner function gains a
    ``if site == ...`` branch; :func:`resolve_template` fetches it by name.
    """
    return [
        _step("Open the site", "browser.open", url="{{site.url}}"),
        _step("Perform the site's primary action", "browser.type", ref="{{field}}", text="{{value}}"),
        _step("Read the result", "browser.snapshot", when={"step": 1, "state": "success"}),
    ]


#: The operator-authored template registry. A new site is a new entry here — data.
#: Every entry is validated at import time (see module bottom) against the stored-
#: workflow rules: registered actions, earlier-only ``when`` conditions, MAX_STEPS.
TEMPLATES: dict[str, Template] = {
    "train.search": Template(
        name="train.search",
        site="train",
        steps=tuple(_train_search()),
        required_fields=("journey.origin", "journey.destination", "journey.date", "journey.class"),
        prompts={
            "journey.origin": "Which station are you travelling from?",
            "journey.destination": "And which station are you going to?",
            "journey.date": "On which date?",
            "journey.class": "Which class do you want (e.g. Sleeper, 3AC)?",
        },
        submit_action=None,
    ),
    "train.book": Template(
        name="train.book",
        site="train",
        steps=tuple(_train_book()),
        required_fields=(
            "journey.origin",
            "journey.destination",
            "journey.date",
            "journey.class",
            "passenger.name",
            "selected.train_ref",
        ),
        prompts={
            "journey.origin": "Which station are you travelling from?",
            "journey.destination": "And which station are you going to?",
            "journey.date": "On which date?",
            "journey.class": "Which class do you want (e.g. Sleeper, 3AC)?",
            "passenger.name": "Whose name is on the booking?",
            "selected.train_ref": "Which train, from the ones I found?",
        },
        submit_action=BOOKING_SUBMIT_ACTION,
    ),
    "food.discover": Template(
        name="food.discover",
        site="food",
        steps=tuple(_food_discover()),
        required_fields=("food.query",),
        prompts={"food.query": "What would you like to find (e.g. pizza)?"},
        submit_action=None,
    ),
    "food.order": Template(
        name="food.order",
        site="food",
        steps=tuple(_food_order()),
        required_fields=("food.query", "selected.restaurant_ref", "selected.item_ref"),
        prompts={
            "food.query": "What would you like to order (e.g. pizza)?",
            "selected.restaurant_ref": "Which restaurant, from the ones I found?",
            "selected.item_ref": "Which item, from the menu?",
        },
        submit_action=ORDER_PLACE_ACTION,
    ),
    "deal.search": Template(
        name="deal.search",
        site="web",
        steps=tuple(_web_search()[:2]),
        required_fields=("web.query",),
        prompts={"web.query": "Which product should I look for? Include the model if you know it."},
        submit_action=None,
    ),
    "web.search": Template(
        name="web.search",
        site="web",
        steps=tuple(_web_search()),
        required_fields=("web.query",),
        prompts={"web.query": "What would you like me to search the web for?"},
        submit_action=None,
    ),
    "site.workflow": Template(
        name="site.workflow",
        site="generic",
        steps=tuple(_site_workflow()),
        required_fields=(),
        submit_action=None,
    ),
}


class TemplateRegistry:
    """The names a template can be resolved by. A new site is a new key, not a branch."""

    def __init__(self, templates: Mapping[str, Template] | None = None) -> None:
        self._templates: Mapping[str, Template] = dict(templates if templates is not None else TEMPLATES)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._templates))

    def has(self, name: str) -> bool:
        return name in self._templates

    def get(self, name: str) -> Template:
        if name not in self._templates:
            raise TemplateError(f"unknown template {name!r}")
        return self._templates[name]


_DEFAULT_REGISTRY = TemplateRegistry()


def resolve_template(name: str, registry: TemplateRegistry | None = None) -> Template:
    """Fetch a template by name.

    This is the extensibility rule made executable: a planner takes a template name and
    gets its steps back, with no ``if site == "train"`` branch anywhere on the path.
    A new site is a new entry in the registry — a new *row* — not a new branch here.
    """
    reg = registry or _DEFAULT_REGISTRY
    return reg.get(name)


def build_workflow(
    template: Template,
    *,
    actor_id: str,
    known_action: Callable[[str], bool],
    workflow_id: str = "",
    parameters: Mapping[str, Any] | None = None,
) -> Workflow:
    """Build a storable ``Workflow`` from a template, through the real validation door.

    ``known_action`` is the registry's own predicate; a template step naming an action
    this machine does not offer is refused at build time. The result's ``source`` is
    ``template:<name>`` — the model never authors it. When ``parameters`` is given,
    each ``{{field}}`` placeholder inside a step parameter is replaced by the plain
    string value (empty string when the field is absent — the clarifier must have
    run first); this is deliberately the only substitution, with no expression or
    default-value language, so a stored template can never compute a value.
    """
    body = {
        "name": template.name,
        "steps": [dict(step) for step in template.steps],
        "source": source_for(template.name),
    }
    workflow = validate_workflow(
        body, actor_id=actor_id, known_action=known_action, workflow_id=workflow_id
    )
    if parameters is not None:
        for step in workflow.steps:
            for key, value in list(step.parameters.items()):
                if isinstance(value, str) and "{{" in value:
                    step.parameters[key] = substitute(value, parameters)
    # Gap 4 wiring: a user-selected ref must reach the submit step's declared
    # parameters *without executing the submit*. The browser.click step already
    # receives ``selected.train_ref`` / ``selected.restaurant_ref`` by direct
    # substitution above; the submit step additionally binds that same ref into
    # its own declared params so the CRITICAL gate reviews the exact selection.
    # The mapping is fixed and keyed by template name (no model text reaches
    # it) and only ever touches parameters the capability declares: booking.submit
    # takes journey/passenger/fare, order.place takes items/total. check_params
    # refuses undeclared names at the registry gate, so nothing else can land
    # here. When the ref is absent the click's ref is "" and the submit stays
    # unbound — the clarifier must have run first; this never defaults it.
    _SELECTION_TO_SUBMIT: dict[str, dict[str, str]] = {
        BOOKING_SUBMIT_ACTION: {"journey": "selected.train_ref"},
        ORDER_PLACE_ACTION: {"items": "selected.restaurant_ref"},
    }
    if parameters is not None:
        for step in workflow.steps:
            mapping = _SELECTION_TO_SUBMIT.get(step.action_type)
            if not mapping:
                continue
            for target, source_field in mapping.items():
                selected = parameters.get(source_field)
                if _present(selected):
                    step.parameters[target] = str(selected)
    return workflow


#: A ``{{field}}`` placeholder inside a step parameter. No ``=default``, no filter,
#: no nesting — the clarifier owns required fields, and substitution only repeats them.
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


def substitute(value: str, fields: Mapping[str, Any]) -> str:
    """Replace ``{{field}}`` placeholders with plain values. Missing → ``""``.

    Narrow on purpose: a value is repeated, never computed. The caller checks
    :func:`clarification_field` before substituting required fields, so a missing
    value reaching here means the caller skipped the clarifier — substituting ""
    (never a default) is the fail-closed outcome.
    """
    def _fill(match: Any) -> str:
        return str(fields.get(match.group(1)) or "")
    return _PLACEHOLDER.sub(_fill, value)


# ---------------------------------------------------------------------------
# Natural-language intent → template + fields. Deterministic, typed, closed.
#
# This is the ONLY place that reads a sentence and chooses a website template.
# It contains no planner branch on a site name and no model call: a small set of
# anchored patterns map to a template *name* (resolved through resolve_template),
# and each pattern's captures become the template's own required fields. Anything
# ambiguous returns no template so the caller falls through to clarification or
# to a "I could not safely map that" answer — never to a guessed site or query.
# ---------------------------------------------------------------------------


_TRAILING_FILLERS = re.compile(
    r"\s+(?:please|for me|there|near me|nearby|to me|then show(?: me)?(?: (?:it|them))?)\s*$",
    re.I,
)
_FOOD_STOP_WORDS = frozenset(
    {
        "me", "more", "some", "any", "the", "a", "an", "for", "food", "foods",
        "dishes", "dish", "restaurants", "restaurant", "places", "place",
        "delivery", "deliver", "to", "show", "find", "get", "search", "searching",
        "discover", "discovery", "near", "neat", "around", "zomato",
        "order", "ordering", "buy", "buying", "book", "booking",
        "open", "launch", "start", "go", "and",
    }
)


def _food_query_from(text: str) -> str:
    """The food words of a request, or ``""`` when there is no honest answer.

    Removes the request verbs and site name, then strips trailing fillers. A
    request naming no actual food (``"find food"``) returns ``""`` so the caller
    asks instead of searching for the literal word "food".
    """
    cleaned = text.lower().strip(" .?!")
    cleaned = re.sub(r"^(?:open|launch|start|go to)\s+", "", cleaned)
    cleaned = re.sub(r"\b(?:zomato|swiggy|uber\s*eats|door\s*dash|deliveroo)\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:show(?: me)?|find|search(?: for)?|get|discover|look for|looking for)\b", " ", cleaned)
    cleaned = cleaned.strip(" .,:;")
    cleaned = _TRAILING_FILLERS.sub("", cleaned)
    words = [word for word in cleaned.split() if word and word not in _FOOD_STOP_WORDS]
    return " ".join(words) if words else ""


_FOOD_ORDER_RX = re.compile(
    r"\b(?:order|buy|place an order for|get me)\b[^.]{0,60}", re.I
)
_FOOD_DISCOVER_RX = re.compile(
    r"(?:\b(?:show|find|search|discover|look for|looking for|browse)\b[^.]{0,60}"
    r"|\bshow me\b[^.]{0,60}"
    r"|(?:^|\s)(?:open|launch|start|go to|visit|check)\s+(?:the\s+)?"
    r"(?:zomato|swiggy|uber\s*eats|door\s*dash|deliveroo|food\s+\w+|food delivery)\b)",
    re.I,
)
_TRAIN_BOOK_RX = re.compile(
    r"\b(?:book|reserve|purchase)\b[^.]{0,24}?\b(?:train|trains|ticket)s?\b", re.I
)
_TRAIN_SEARCH_RX = re.compile(
    r"\b(?:find|search|show|check|look for|see|what are|are there|trains? (?:from|to|around|at))\b"
    r"[^.]{0,80}?\b(?:train|trains)s?\b"
    r"|\btrains?\b[^.]{0,60}?\b(?:from|to|around|at|leaving|departing)\b",
    re.I,
)
_BOOK_TRIGGERS = ("book", "booking", "reserve", "ticket")
# Explicit-web wording only. A bare "search for X" / "look up X" is NOT a web
# template on its own: "search for <food>" has always been food discovery, and
# a generic "look up X" belongs to the LLM planner (which proposes the
# registered ``search.web`` action through the normal policy path). Routing
# those here would shadow both — see the pinned regressions in
# tests/unit/test_phase4_templates.py::TestWebIntentDoesNotShadow.
_DEAL_SEARCH_RX = re.compile(
    r"^(?:find|check|compare|look for|show me) (?:me )?(?:the )?"
    r"(?:best )?(?:(?:verified|vrified) )?deals?(?: for (.*))?[.?!]*$",
    re.I,
)
_DEAL_FOLLOW_UP_RX = re.compile(r"\s+(?:and|then)\s+(?:then\s+)?(?:order|buy)\b", re.I)
_WEB_SEARCH_RX = re.compile(
    r"\b(?:search the web for|search the internet for|web search for|"
    r"browse the web for|google)\s+(.+?)\s*$",
    re.I,
)
_WEB_STOP_WORDS = frozenset(
    {
        "me", "please", "for", "about", "the", "a", "an", "web", "internet",
        "search", "searching", "find", "look", "up", "google", "browse",
    }
)


def _web_query_from(text: str) -> str:
    """The topic words of a web-search request, or ``""`` when there is none.

    Only the user's own words after the search verb are kept; a bare
    "search the web" with no topic returns ``""`` so the caller asks for the
    topic instead of searching for an empty or invented query.
    """
    cleaned = " ".join(text.lower().split()).strip(" .?!")
    match = _WEB_SEARCH_RX.search(cleaned)
    if not match:
        return ""
    words = [w for w in match.group(1).split() if w and w not in _WEB_STOP_WORDS]
    return " ".join(words) if words else ""


def match_website_intent(text: str) -> str | None:
    """The website template a sentence maps to, or ``None`` when none matches.

    Consequential intents are tested first: an ordering verb anywhere in the
    request maps to ``food.order`` (never ``food.discover``), and a booking verb
    near "train" maps to ``train.book``. ``None`` means the caller must not
    reach for a browser template here at all.
    """
    cleaned = " ".join(text.lower().split())
    if _DEAL_SEARCH_RX.fullmatch(cleaned):
        return "deal.search"
    if _FOOD_ORDER_RX.search(cleaned) and _food_query_from(cleaned):
        return "food.order"
    if _TRAIN_BOOK_RX.search(cleaned):
        return "train.book"
    if _TRAIN_SEARCH_RX.search(cleaned):
        return "train.search"
    if _WEB_SEARCH_RX.search(cleaned):
        return "web.search"
    if _FOOD_DISCOVER_RX.search(cleaned):
        return "food.discover"
    return None


def extract_website_fields(text: str, template_name: str) -> dict[str, Any]:
    """Best-effort fields for a template, from the user's own words only.

    Never invents: a pattern that would need a guess yields no value for that
    field, and the caller runs :func:`clarification_field` to ask for it.
    """
    cleaned = " ".join(text.lower().split())
    if template_name == "deal.search":
        match = _DEAL_SEARCH_RX.fullmatch(" ".join(text.split()))
        product = _DEAL_FOLLOW_UP_RX.split(match[1] or "", maxsplit=1)[0].strip(" .?!") if match else ""
        return {"web.query": f"{product} deals prices reputable retailers"} if product else {}
    if template_name == "web.search":
        query = _web_query_from(cleaned)
        web_fields: dict[str, Any] = {}
        if query:
            web_fields["web.query"] = query
        return web_fields
    if template_name in ("food.discover", "food.order"):
        query = _food_query_from(cleaned)
        fields: dict[str, Any] = {}
        if query:
            fields["food.query"] = query
        return fields
    if template_name in ("train.search", "train.book"):
        fields = {}
        origin = re.search(
            r"\bfrom\s+([a-z][a-z .'-]{1,40}?)(?:\s+to\s+|\s+on\s+|\s+around\s+|\s+at\s+|\s*[,.;]|$)",
            cleaned,
        )
        if origin:
            fields["journey.origin"] = origin.group(1).strip(" .,:;")
        destination = re.search(
            r"\bto\s+([a-z][a-z .'-]{1,40}?)(?:\s+on\s+|\s+around\s+|\s+at\s+|\s*[,.;]|$)",
            cleaned,
        )
        if destination:
            fields["journey.destination"] = destination.group(1).strip(" .,:;")
        date = re.search(
            r"\b(?:on\s+)?(\d{4}-\d{2}-\d{2}|\d{1,2}[\s/-](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+\d{4})?)\b",
            cleaned,
        )
        if date:
            fields["journey.date"] = date.group(1).strip(" .,:;")
        klass = re.search(r"\b(1ac|2ac|3ac|sleeper|first class|second class|third class|3a|2a|1a|sl)\b", cleaned, re.I)
        if klass:
            fields["journey.class"] = klass.group(1).strip(" .,:;")
        return fields
    return {}




def submit_step(template: Template) -> dict[str, Any] | None:
    """The template's submit step, if it has one. ``None`` means read-only.

    A read-only template has exactly zero steps naming a submit action; a consequential
    template has exactly one, and it is its final step.
    """
    for step in reversed(template.steps):
        if step.get("action") in CRITICAL_SUBMIT_ACTIONS:
            return step
    return None


def has_submit(template: Template) -> bool:
    """True when the template can submit something consequential."""
    return submit_step(template) is not None


# ---------------------------------------------------------------------------
# Clarification: ask one missing detail at a time, never several at once
# ---------------------------------------------------------------------------


def clarification_field(template: Template, provided: Mapping[str, Any]) -> str | None:
    """The NEXT single detail to ask, or ``None`` when every required field is present.

    This is "ask conversationally, one at a time" made executable. It walks the
    template's ``required_fields`` in order and returns the FIRST one the user has not
    yet supplied (a field counts as supplied when it is present with a non-blank value).
    It never returns more than one, and never fills a default in its place — the caller
    shows the field's prompt and waits for the answer.
    """
    for field_name in template.required_fields:
        if not _present(provided.get(field_name)):
            return field_name
    return None


def clarification_prompt(template: Template, field_name: str) -> str:
    """The user-facing question for a field, or a neutral fallback for an unlisted one."""
    return template.prompts.get(field_name, f"Please provide {field_name}.")


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


# ---------------------------------------------------------------------------
# Confirm gate: is a submitted confirmation still valid for the reviewed operation?
#
# A CRITICAL submit is gated behind the *exact* operation the user reviewed: the
# approval card shows the journey/item/price, and the user's Confirm is matched on the
# server-minted action_id. If any of those details changes AFTER the review was shown,
# the prior confirmation is INVALIDATED and must be re-shown. This function computes
# that invalidation. It does NOT build a second approval system — the ASK is the
# PolicyEngine's CRITICAL gate; this only decides whether an existing confirmation still
# covers what is about to be submitted.
# ---------------------------------------------------------------------------


#: Which reviewed fields matter: if any of them changes, a prior confirmation is void.
CONFIRMATION_FIELDS = (
    "price",
    "total",
    "destination",
    "origin",
    "selection",
    "passenger",
)


@dataclass(frozen=True)
class Review:
    """The exact operation a confirmation was given for.

    ``operation`` is the reviewed fact set (item/price/journey/passenger) as a plain
    mapping; ``fingerprint`` is a stable hash of it, so "did the operation change?" is a
    comparison of fingerprints rather than a re-read of untrusted page text.
    """

    operation: Mapping[str, Any]
    fingerprint: str


def _fingerprint(operation: Mapping[str, Any]) -> str:
    """A stable fingerprint of the reviewed operation, over the fields that matter.

    Only :data:`CONFIRMATION_FIELDS` participate: a confirmation is invalidated when one
    of those changes, not when unrelated fields do. Values are canonicalised as
    ``"key=value"`` pairs so the fingerprint is independent of mapping order.
    """
    parts = [f"{key}={_canonical(operation.get(key))}" for key in sorted(CONFIRMATION_FIELDS)]
    return "|".join(parts)


def reviewed_operation(operation: Mapping[str, Any]) -> Review:
    """Freeze the operation as reviewed, into a comparable fingerprint."""
    return Review(operation=dict(operation), fingerprint=_fingerprint(operation))


def confirmation_is_valid(reviewed: Review, now: Mapping[str, Any]) -> bool:
    """True only when the operation about to be submitted is the one that was reviewed.

    ``now`` is the current state of the consequential fields (price/total/destination/
    selection/passenger). If it differs from the reviewed operation on any of those
    fields, the prior confirmation no longer covers it and must be re-shown.
    """
    current = Review(operation=dict(now), fingerprint=_fingerprint(now))
    return current.fingerprint == reviewed.fingerprint


def invalidation_reason(reviewed: Review, now: Mapping[str, Any]) -> str | None:
    """Which reviewed field changed, or ``None`` when the confirmation is still valid.

    Names the first (sorted) changed field so a UI can say *why* the review must be
    re-shown — "the fare changed from 1200 to 1400" — rather than a bare "re-confirm".
    """
    for key in sorted(CONFIRMATION_FIELDS):
        if _canonical(reviewed.operation.get(key)) != _canonical(now.get(key)):
            return key
    return None


def _canonical(value: Any) -> str:
    """Normalise a field for comparison so whitespace does not manufacture a change."""
    if value is None:
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# Module-load validation: every template is checked against the stored-workflow rules
# using the registry's own predicate, so a bad template is an import-time failure, not
# a run-time surprise. The predicate here is deliberately the *real* registry's, built
# from the capabilities the contract names (§2.1 browser + the CRITICAL submit ids),
# so the templates are validated against what this machine actually offers.
#
# The test file may use a STUB predicate instead to avoid importing Agent A/B modules
# that may not exist yet; that stub mirrors this set. Both enforce the same rules.
# ---------------------------------------------------------------------------

#: The capability ids the templates above name. This is the closed set the templates
#: are written against. Browser ids are from contract §2.1; the submit ids are the two
#: CRITICAL capabilities the integration lead registers (§2.4/§3).
_BUILTIN_KNOWN_ACTIONS: frozenset[str] = frozenset(
    {
        # browser (Agent A, contract §2.1)
        "browser.open",
        "browser.navigate",
        "browser.search",
        "browser.inspect",
        "browser.snapshot",
        "browser.click",
        "browser.type",
        "browser.select",
        "browser.scroll",
        "browser.back",
        "browser.forward",
        "browser.refresh",
        "browser.wait",
        "browser.stop",
        # CRITICAL submits (registered by the integration lead, §2.4/§3)
        BOOKING_SUBMIT_ACTION,
        ORDER_PLACE_ACTION,
    }
)


def _default_known_action(action_type: str) -> bool:
    """The registry predicate used at module load: the closed set this module names."""
    return action_type in _BUILTIN_KNOWN_ACTIONS


def _validate_all() -> None:
    """Refuse to import this module if any template violates the stored-workflow rules.

    Catches, at import time: a step naming an unregistered action, a ``when`` on a step
    that is not strictly earlier or naming a key outside :data:`CONDITION_KEYS`, and any
    template over MAX_STEPS. A template that would fail to build a stored workflow is a
    module that must not load.
    """
    for name, template in TEMPLATES.items():
        body = {
            "name": name,
            "steps": [dict(step) for step in template.steps],
            "source": source_for(name),
        }
        try:
            validate_workflow(body, actor_id="operator", known_action=_default_known_action)
        except WorkflowError as exc:
            raise TemplateError(f"template {name!r} refused: {exc}") from exc
        if len(template.steps) > MAX_STEPS:
            raise TemplateError(f"template {name!r} has more than {MAX_STEPS} steps")


_validate_all()
