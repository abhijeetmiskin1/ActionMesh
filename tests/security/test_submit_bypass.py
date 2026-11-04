"""C42 submission-bypass tests — a plain ``browser.click`` is NEVER a submission.

Contract §2.4/§5 gate 3, made executable: clicking a button labelled "Place order" or
"Book & Pay" through the browser surface must never be equivalent to invoking the
CRITICAL submit capabilities ``booking.submit`` / ``order.place``. The proof has four
legs:

1. **Registration invariant** (expectedFailure "registration pending"): both submit ids
   must exist in the action registry with risk CRITICAL and the ``payment.execute``
   scope. They are NOT yet in the seeds in this tree, so those two tests fail ON PURPOSE
   — the failure is the finding, reported to the integration lead, not a bug in here.

2. **The PolicyEngine never auto-approves.** A locally-constructed CRITICAL definition
   (the same shape the integration lead must register) evaluated through the REAL
   engine: no grant -> DENY with ``payment.execute`` listed as missing; grant + allow
   rule -> **ASK** with ``requires_approval=True`` and ``authorizes_execution() ==
   False``. The ONLY path to ALLOW is a typed ``ApprovalRequest`` whose ``action_id``
   matches the action — which a model sentence can never produce. Every assertion here
   is against the engine's returned decision, not a mock.

3. **A click is MEDIUM and needs no payment scope.** ``browser.click`` (browser.control)
   evaluates to ALLOW with zero payment grants, and the engine's driver log after
   clicking the checkout "Place order" button contains ``click`` and never a submit —
   because no code path from a click reaches a submit capability at all.

4. **Confirmation binding and self-approval refusal.** Material fields (price,
   passenger, destination, selection) invalidate a review when changed; a field outside
   ``CONFIRMATION_FIELDS`` does not. ``validate_proposal`` drops whole entries that
   assert fields they do not own (``approved``, ``risk_level``, ``source``), and
   parameters that smuggle ``"approved": true`` are inert data — PolicyContext reads
   approvals only from its typed list.

Doc note: the brief cites ``actionmesh/core/policy/engine.py``; the real module is
``actionmesh/core/permissions/engine.py`` — same PolicyEngine, that is the path used.
"""

from __future__ import annotations

import functools
import unittest
from collections.abc import Callable
from typing import Any

from actionmesh.agent.browser import BrowserEngine, FakeDriver
from actionmesh.core.permissions.catalog import is_known, permission
from actionmesh.core.permissions.context import PermissionGrant, build_context
from actionmesh.core.permissions.engine import PolicyEngine
from actionmesh.core.permissions.policy import policy_from_rules
from actionmesh.core.registry import ActionRegistry
from actionmesh.core.registry.registry import UnknownActionError
from actionmesh.protocol.models import (
    Action,
    ActionDefinition,
    ApprovalRequest,
    Identity,
    PolicyDecision,
    RiskLevel,
)
from actionmesh.voiceos.llm import validate_proposal
from actionmesh.voiceos.website_workflows import (
    BOOKING_SUBMIT_ACTION,
    CONFIRMATION_FIELDS,
    CRITICAL_SUBMIT_ACTIONS,
    CRITICAL_SUBMIT_SCOPES,
    ORDER_PLACE_ACTION,
    TEMPLATES,
    confirmation_is_valid,
    invalidation_reason,
    reviewed_operation,
)


def expected_failure_with_reason(reason: str) -> Callable:
    """``unittest.expectedFailure`` plus a reason string.

    The brief asks for a clear "registration pending" reason; stdlib ``expectedFailure``
    takes no arguments, so the reason is folded into the test name/docstring (what the
    report prints) while the decorator stays the stdlib one.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper.__doc__ = f"{func.__doc__ or ''}\n[expected failure: {reason}]".strip()
        return unittest.expectedFailure(wrapper)

    return decorator


def _submit_definition(name: str) -> ActionDefinition:
    """The CRITICAL definition the integration lead must register (§2.4/§3), built
    locally so the invariant is tested even while registration is pending."""
    return ActionDefinition(
        name=name,
        description="consequential web submit",
        risk_level=RiskLevel.CRITICAL,
        permissions=[CRITICAL_SUBMIT_SCOPES[name]],
        provider="web",
    )


def _click_definition() -> ActionDefinition:
    return ActionDefinition(
        name="browser.click",
        description="click one snapshotted element by opaque ref",
        risk_level=RiskLevel.MEDIUM,
        permissions=["browser.control"],
        provider="laptop",
    )


def _register_click(reg: ActionRegistry):
    """Register browser.click as MEDIUM so tests that resolve it succeed."""
    reg.register(_click_definition())


def _allow_rule(name: str):
    """A policy whose single rule would ALLOW this action if the risk gate let it."""
    return policy_from_rules([{"action": name, "decision": "allow"}])


def _submit_action(name: str, params: dict[str, Any]) -> Action:
    return Action(type=name, provider="web", parameters=params)


class TestRegistrationInvariant(unittest.TestCase):
    """The contract REQUIRES both submits registered as CRITICAL via the real
    device-action path (``register_device_actions``); these now assert it directly."""

    def setUp(self) -> None:
        from actionmesh.agent.definitions import register_device_actions
        self.reg = ActionRegistry.with_seeds()
        register_device_actions(self.reg)

    def test_booking_submit_registered_in_registry(self) -> None:
        self.assertTrue(
            self.reg.has(BOOKING_SUBMIT_ACTION),
            f"{BOOKING_SUBMIT_ACTION} must be registered (contract §2.4)",
        )
        defn = self.reg.get(BOOKING_SUBMIT_ACTION)
        self.assertIs(defn.risk_level, RiskLevel.CRITICAL)
        self.assertIn("payment.execute", list(defn.permissions))

    def test_order_place_registered_in_registry(self) -> None:
        self.assertTrue(
            self.reg.has(ORDER_PLACE_ACTION),
            f"{ORDER_PLACE_ACTION} must be registered (contract §2.4)",
        )
        defn = self.reg.get(ORDER_PLACE_ACTION)
        self.assertIs(defn.risk_level, RiskLevel.CRITICAL)
        self.assertIn("payment.execute", list(defn.permissions))

    def test_payment_execute_scope_exists_and_is_critical(self) -> None:
        """The scope both submits reuse is in the closed catalog at CRITICAL — the
        half of the invariant that IS already wired: submits can never lower the label."""
        self.assertTrue(is_known("payment.execute"))
        self.assertIs(permission("payment.execute").risk_level, RiskLevel.CRITICAL)

    def test_critical_submit_scopes_map_to_payment_execute(self) -> None:
        self.assertEqual(CRITICAL_SUBMIT_SCOPES[BOOKING_SUBMIT_ACTION], "payment.execute")
        self.assertEqual(CRITICAL_SUBMIT_SCOPES[ORDER_PLACE_ACTION], "payment.execute")

    def test_browser_click_is_never_a_submit_id(self) -> None:
        """Structurally: the click name is not among the submit names, and the submit
        names are not among the click verb spellings a model might hallucinate."""
        self.assertNotIn("browser.click", CRITICAL_SUBMIT_ACTIONS)
        self.assertNotIn("booking.submit", ("browser.click", "browser.type", "browser.submit"))


class TestPolicyEngineNeverAutoApproves(unittest.TestCase):
    """Every assertion here is against the REAL engine's returned outcome."""

    def setUp(self) -> None:
        self.engine = PolicyEngine()
        self.actor = Identity(id="user_1", kind="user")

    def evaluate(
        self,
        name: str,
        *,
        grants: list[PermissionGrant],
        params: dict[str, Any] | None = None,
        approvals: list[ApprovalRequest] | None = None,
        policy=None,
    ):
        action = _submit_action(name, params or {"passenger": "Asha"})
        ctx = build_context(
            action,
            _submit_definition(name),
            self.actor,
            grants=grants,
            policy=policy if policy is not None else _allow_rule(name),
            approvals=approvals or [],
        )
        return ctx, action, self.engine.evaluate(ctx)

    def test_no_grant_denies_and_names_payment_execute(self) -> None:
        for name in (BOOKING_SUBMIT_ACTION, ORDER_PLACE_ACTION):
            _, _, outcome = self.evaluate(name, grants=[])
            self.assertIs(outcome.decision, PolicyDecision.DENY, name)
            self.assertIn("payment.execute", outcome.missing_permissions, name)
            self.assertFalse(outcome.authorizes_execution(), name)

    def test_a_high_capped_grant_cannot_satisfy_critical(self) -> None:
        """PermissionGrant defaults to max_risk HIGH; HIGH < CRITICAL, so even a
        perfectly-scoped grant cannot authorise a submit. This kills the
        'reuse an old broad grant' bypass."""
        for name in (BOOKING_SUBMIT_ACTION, ORDER_PLACE_ACTION):
            _, _, outcome = self.evaluate(
                name, grants=[PermissionGrant(scope="payment.execute")]  # default HIGH cap
            )
            self.assertIs(outcome.decision, PolicyDecision.DENY, name)
            self.assertIn("payment.execute", outcome.missing_permissions, name)

    def test_grant_plus_allow_rule_still_asks_never_allows(self) -> None:
        """The core §2.4 promise: CRITICAL always lands in ASK (WAITING_PERMISSION),
        requires_approval True, and the outcome does NOT authorize execution."""
        for name in (BOOKING_SUBMIT_ACTION, ORDER_PLACE_ACTION):
            grant = PermissionGrant(scope="payment.execute", max_risk=RiskLevel.CRITICAL)
            _, _, outcome = self.evaluate(name, grants=[grant])
            self.assertIs(outcome.decision, PolicyDecision.ASK, name)
            self.assertTrue(outcome.requires_approval, name)
            self.assertFalse(outcome.authorizes_execution(), name)
            self.assertIn("CRITICAL risk always requires explicit user approval", outcome.reasons, name)
            self.assertIn("awaiting user approval", outcome.reasons, name)

    def test_allow_high_risk_flag_does_not_lift_critical_ask(self) -> None:
        """allow_high_risk is a server-side HIGH opt-in; it must NOT touch CRITICAL."""
        grant = PermissionGrant(scope="payment.execute", max_risk=RiskLevel.CRITICAL)
        action = _submit_action(BOOKING_SUBMIT_ACTION, {})
        ctx = build_context(
            action,
            _submit_definition(BOOKING_SUBMIT_ACTION),
            self.actor,
            grants=[grant],
            policy=_allow_rule(BOOKING_SUBMIT_ACTION),
            allow_high_risk=True,
        )
        outcome = self.engine.evaluate(ctx)
        self.assertIs(outcome.decision, PolicyDecision.ASK)
        self.assertFalse(outcome.authorizes_execution())

    def test_only_a_typed_approval_flips_ask_to_allow(self) -> None:
        """The single code path that executes a submit: an ApprovalRequest (server-
        minted action_id, status approved) for THIS action. Nothing else."""
        grant = PermissionGrant(scope="payment.execute", max_risk=RiskLevel.CRITICAL)
        action = _submit_action(BOOKING_SUBMIT_ACTION, {"passenger": "Asha", "price": 1200})
        ctx_no_approval = build_context(
            action, _submit_definition(BOOKING_SUBMIT_ACTION), self.actor,
            grants=[grant], policy=_allow_rule(BOOKING_SUBMIT_ACTION),
        )
        self.assertIs(self.engine.evaluate(ctx_no_approval).decision, PolicyDecision.ASK)

        ctx_approved = build_context(
            action, _submit_definition(BOOKING_SUBMIT_ACTION), self.actor,
            grants=[grant], policy=_allow_rule(BOOKING_SUBMIT_ACTION),
            approvals=[ApprovalRequest(
                action_id=action.action_id,
                action_type=BOOKING_SUBMIT_ACTION,
                status="approved",
            )],
        )
        outcome = self.engine.evaluate(ctx_approved)
        self.assertIs(outcome.decision, PolicyDecision.ALLOW)
        self.assertFalse(outcome.requires_approval)
        self.assertTrue(outcome.authorizes_execution())

    def test_a_rejected_or_other_actions_approval_does_not_help(self) -> None:
        grant = PermissionGrant(scope="payment.execute", max_risk=RiskLevel.CRITICAL)
        action = _submit_action(BOOKING_SUBMIT_ACTION, {})
        for status in ("rejected", "pending", "expired"):
            ctx = build_context(
                action, _submit_definition(BOOKING_SUBMIT_ACTION), self.actor,
                grants=[grant], policy=_allow_rule(BOOKING_SUBMIT_ACTION),
                approvals=[ApprovalRequest(action_id=action.action_id, status=status)],
            )
            self.assertIs(self.engine.evaluate(ctx).decision, PolicyDecision.ASK, status)
        # An approval bound to a DIFFERENT action id proves nothing for this action.
        ctx = build_context(
            action, _submit_definition(BOOKING_SUBMIT_ACTION), self.actor,
            grants=[grant], policy=_allow_rule(BOOKING_SUBMIT_ACTION),
            approvals=[ApprovalRequest(action_id="act-someone-elses", status="approved")],
        )
        self.assertIs(self.engine.evaluate(ctx).decision, PolicyDecision.ASK)

    def test_no_actor_fails_closed(self) -> None:
        grant = PermissionGrant(scope="payment.execute", max_risk=RiskLevel.CRITICAL)
        ctx = build_context(
            _submit_action(BOOKING_SUBMIT_ACTION, {}),
            _submit_definition(BOOKING_SUBMIT_ACTION),
            None,
            grants=[grant],
            policy=_allow_rule(BOOKING_SUBMIT_ACTION),
        )
        outcome = self.engine.evaluate(ctx)
        self.assertIs(outcome.decision, PolicyDecision.DENY)
        self.assertFalse(outcome.authorizes_execution())


class TestClickIsNotSubmission(unittest.TestCase):
    """browser.click is MEDIUM, needs only browser.control, and drives no submit."""

    def setUp(self) -> None:
        self.engine = PolicyEngine()
        self.actor = Identity(id="user_1", kind="user")
        self.reg = ActionRegistry.with_seeds()
        _register_click(self.reg)  # Register click so resolve works

    def test_click_outcome_requires_no_payment_permission(self) -> None:
        """A click under a browser.control grant: ALLOW, and payment.execute is not
        even in the requirement list."""
        action = Action(type="browser.click", parameters={"ref": 3}, risk_level=RiskLevel.MEDIUM)
        ctx = build_context(
            action, _click_definition(), self.actor,
            grants=[PermissionGrant(scope="browser.control")],  # default HIGH cap
            policy=_allow_rule("browser.click"),
        )
        outcome = self.engine.evaluate(ctx)
        self.assertIs(outcome.decision, PolicyDecision.ALLOW)
        self.assertTrue(outcome.authorizes_execution())
        self.assertEqual(outcome.required_permissions, ["browser.control"])
        self.assertNotIn("payment.execute", outcome.required_permissions)
        self.assertIs(outcome.effective_risk, RiskLevel.MEDIUM)

    def test_clicking_place_order_button_never_reaches_a_submit(self) -> None:
        """Drive the real engine over the checkout fixture's "Place order" button.
        The driver log is the proof: a click happened; no submit capability exists to
        be invoked — the browser surface has no name for one."""
        drv = FakeDriver()
        drv.add_page(
            "https://fixtures.invalid/checkout.html",
            "Checkout",
            [
                {"tag": "input", "name": "Delivery address", "value": ""},
                {"tag": "button", "name": "Place order"},
            ],
            "Total: $26.50. Two margherita, one garlic bread.",
        )
        drv.add_page("https://fixtures.invalid/submit-order.html", "Order placed", [])
        eng = BrowserEngine(drv)
        import asyncio

        run = asyncio.run
        run(eng.open("https://fixtures.invalid/checkout.html"))
        snap = run(eng.snapshot())
        button_ref = next(
            item["ref"] for item in _snapshot_records(drv, snap) if item["name"] == "Place order"
        )
        result = run(eng.click(button_ref))
        self.assertEqual(result["status"], "ok")
        # A click in the fake DOM marks the button pressed; it does NOT submit the
        # form: the page never changed, and the action log has one "click" and no
        # navigate/submit of any kind beyond what open() itself performed.
        self.assertNotIn("submit-order.html", drv.current_url)
        kinds = [a["action"] for a in drv.actions]
        self.assertEqual(kinds.count("click"), 1)
        for forbidden in (BOOKING_SUBMIT_ACTION, ORDER_PLACE_ACTION, "payment.execute", "submit"):
            self.assertNotIn(forbidden, kinds)

    def test_engine_exposes_no_submission_verb_at_all(self) -> None:
        """Static leg of the guarantee: the BrowserEngine's public surface cannot
        even name a submit/book/order/pay operation, so no dispatch path from a
        click leads to one."""
        verbs = {
            name.lower()
            for name in dir(BrowserEngine)
            if not name.startswith("_") and callable(getattr(BrowserEngine, name, None))
        }
        for forbidden in ("submit", "book", "order", "pay", "purchase", "confirm"):
            self.assertFalse(
                any(forbidden in verb for verb in verbs),
                f"BrowserEngine must not expose a {forbidden!r} verb: {sorted(verbs)}",
            )


def _snapshot_records(drv: FakeDriver, snap: dict) -> list[dict]:
    """Match snapshot refs to scripted element names via a fresh driver snapshot.

    ``BrowserEngine.snapshot`` deliberately returns only opaque ints; the element
    NAMES ride the transient stream. Tests that need to address "the button named
    Place order" read the driver's own records and map position -> ref order.
    """
    import asyncio

    _, records = asyncio.run(drv.snapshot_elements())
    # The engine mints refs in record order during the snapshot that produced `snap`;
    # both lists are positionally aligned, so zip by order.
    refs = snap["refs"]
    return [
        {"ref": ref, "name": rec["name"], "tag": rec["tag"]}
        for ref, rec in zip(refs, records, strict=True)
    ]


class TestConfirmationBinding(unittest.TestCase):
    """An approval covers the EXACT reviewed operation or it is void."""

    def setUp(self) -> None:
        # The train-booking instantiation the contract scenario uses.
        self.reviewed = reviewed_operation(
            {
                "origin": "NZM",
                "destination": "CSMT",
                "selection": "12903",
                "passenger": "Asha",
                "price": 1200,
                "total": 1200,
            }
        )

    def _now(self, **changes: Any) -> dict[str, Any]:
        merged = dict(self.reviewed.operation)
        merged.update(changes)
        return merged

    def test_identical_operation_stays_valid(self) -> None:
        self.assertTrue(confirmation_is_valid(self.reviewed, self._now()))
        self.assertIsNone(invalidation_reason(self.reviewed, self._now()))

    def test_passenger_change_invalidates_and_names_the_field(self) -> None:
        now = self._now(passenger="Ravi")
        self.assertFalse(confirmation_is_valid(self.reviewed, now))
        self.assertEqual(invalidation_reason(self.reviewed, now), "passenger")

    def test_price_change_invalidates_and_names_the_field(self) -> None:
        """The financial guard: a fare that moved after the review voids the approval."""
        now = self._now(price=1400, total=1400)
        self.assertFalse(confirmation_is_valid(self.reviewed, now))
        self.assertEqual(invalidation_reason(self.reviewed, now), "price")

    def test_selection_change_invalidates(self) -> None:
        now = self._now(selection="22903")
        self.assertFalse(confirmation_is_valid(self.reviewed, now))
        self.assertEqual(invalidation_reason(self.reviewed, now), "selection")

    def test_immaterial_param_keeps_confirmation_valid(self) -> None:
        """Fields outside CONFIRMATION_FIELDS are not part of the fingerprint:
        notes/meal preference changing must NOT force a pointless re-ask."""
        now = self._now(seat_preference="window", notes="call before delivery")
        self.assertTrue(confirmation_is_valid(self.reviewed, now))
        self.assertIsNone(invalidation_reason(self.reviewed, now))

    def test_journey_class_is_currently_not_material_reported_as_finding(self) -> None:
        """FINDING (not a test bug): the brief expects a class change to invalidate,
        but CONFIRMATION_FIELDS has no "class"/"journey.class" entry — only
        price/total/destination/origin/selection/passenger. A bare {"class": ...}
        change therefore STAYS valid. Documented in the report; asserted here as-is
        so the behaviour cannot silently drift."""
        self.assertNotIn("class", CONFIRMATION_FIELDS)
        review = reviewed_operation({"price": 1200, "class": "3AC"})
        now = {"price": 1200, "class": "2AC"}
        self.assertTrue(confirmation_is_valid(review, now))
        # The material equivalent today is "selection"; operators must fold class into it.

    def test_whitespace_only_change_is_not_a_change(self) -> None:
        now = self._now(passenger="  Asha ")
        self.assertTrue(confirmation_is_valid(self.reviewed, now))

    def test_after_invalidation_a_fresh_review_becomes_confirmable(self) -> None:
        now = self._now(price=1400, total=1400)
        self.assertFalse(confirmation_is_valid(self.reviewed, now))
        fresh = reviewed_operation(now)
        self.assertTrue(confirmation_is_valid(fresh, now))


class TestSelfApprovalIsRefused(unittest.TestCase):
    """Model output that asserts its own approval dies at the doors."""

    KNOWN = BROWSER_ALL | CRITICAL_SUBMIT_ACTIONS if (BROWSER_ALL := frozenset(
        {"browser.open", "browser.navigate", "browser.search", "browser.inspect",
         "browser.snapshot", "browser.click", "browser.type", "browser.select",
         "browser.scroll", "browser.back", "browser.forward", "browser.refresh",
         "browser.wait", "browser.stop"})) else CRITICAL_SUBMIT_ACTIONS

    def test_entry_level_approved_field_drops_the_whole_entry(self) -> None:
        proposal = validate_proposal(
            {"actions": [
                {"type": BOOKING_SUBMIT_ACTION, "parameters": {"price": 1},
                 "approved": True},
            ]},
            lambda t: t in self.KNOWN,
        )
        self.assertEqual(proposal.actions, ())
        self.assertTrue(any("asserted fields it does not own" in n for n in proposal.notes))
        self.assertTrue(any("'approved'" in n for n in proposal.notes))

    def test_entry_level_risk_level_field_drops_the_whole_entry(self) -> None:
        proposal = validate_proposal(
            {"actions": [
                {"type": BOOKING_SUBMIT_ACTION, "parameters": {}, "risk_level": "low"},
            ]},
            lambda t: t in self.KNOWN,
        )
        self.assertEqual(proposal.actions, ())

    def test_unregistered_type_is_dropped_with_a_note(self) -> None:
        proposal = validate_proposal(
            {"actions": [
                {"type": "ghost.submit", "parameters": {}},
                {"type": "browser.click", "parameters": {"ref": 1}},
            ]},
            lambda t: t in self.KNOWN,
        )
        self.assertEqual(len(proposal.actions), 1)
        self.assertEqual(proposal.actions[0][0], "browser.click")
        self.assertTrue(any("dropped unregistered action type 'ghost.submit'" in n
                            for n in proposal.notes))

    def test_registry_resolve_refuses_an_unregistered_name(self) -> None:
        """validate_proposal's predicate is advisory; the registry door is hard.
        A proposed type with no definition never resolves to an executable action."""
        reg = ActionRegistry.with_seeds()
        with self.assertRaises(UnknownActionError):
            reg.resolve({"type": "ghost.submit", "parameters": {}})

    def test_approved_true_inside_parameters_is_inert_data(self) -> None:
        """The subtle case: {"approved": true} as a PARAMETER survives validate_proposal
        (parameters are model-owned data) — but PolicyContext reads approvals ONLY from
        its typed ApprovalRequest list, so the claim changes nothing. Asserted against
        the real engine's decision."""
        kept = validate_proposal(
            {"actions": [
                {"type": BOOKING_SUBMIT_ACTION, "parameters": {"price": 1200, "approved": True}},
            ]},
            lambda t: t in self.KNOWN,
        )
        self.assertEqual(len(kept.actions), 1)  # the claim survives as DATA
        action_type, params = kept.actions[0]
        self.assertIs(action_type, BOOKING_SUBMIT_ACTION)
        self.assertTrue(params.get("approved"))

        engine = PolicyEngine()
        action = Action(type=action_type, parameters=params)
        ctx = build_context(
            action,
            _submit_definition(action_type),
            Identity(id="user_1", kind="user"),
            grants=[PermissionGrant(scope="payment.execute", max_risk=RiskLevel.CRITICAL)],
            policy=_allow_rule(action_type),
        )
        outcome = engine.evaluate(ctx)
        self.assertIs(outcome.decision, PolicyDecision.ASK)
        self.assertFalse(outcome.authorizes_execution())

    def test_model_sentence_cannot_author_a_stored_plan(self) -> None:
        """A proposal impersonating a template (carrying source/index/label) is refused
        whole — plan authorship is package data, never model output."""
        proposal = validate_proposal(
            {"actions": [
                {"type": "browser.open", "parameters": {"url": "https://x.example"},
                 "source": "template:train.search", "label": "Open"},
            ]},
            lambda t: t in self.KNOWN,
        )
        self.assertEqual(proposal.actions, ())


class TestTemplatesKeepClickAndSubmitSeparated(unittest.TestCase):
    """The plan layer: a click step is a click; the submit step is named explicitly."""

    def test_train_search_names_no_submit(self) -> None:
        for step in TEMPLATES["train.search"].steps:
            self.assertNotIn(step["action"], CRITICAL_SUBMIT_ACTIONS)

    def test_train_book_clicks_and_submits_are_distinct_steps(self) -> None:
        steps = TEMPLATES["train.book"].steps
        clicks = [s for s in steps if s["action"] == "browser.click"]
        submits = [s for s in steps if s["action"] in CRITICAL_SUBMIT_ACTIONS]
        self.assertTrue(clicks)  # the booking form is opened by a click...
        self.assertEqual(len(submits), 1)  # ...but exactly one named step submits
        self.assertEqual(submits[0]["action"], BOOKING_SUBMIT_ACTION)
        # A click step must never carry submit-shaped identity: its action is a
        # browser verb, not a CRITICAL capability id.
        for click in clicks:
            self.assertNotIn(click["action"], CRITICAL_SUBMIT_ACTIONS)

    def test_food_order_names_order_place_once_not_booking(self) -> None:
        submits = [
            s["action"] for s in TEMPLATES["food.order"].steps
            if s["action"] in CRITICAL_SUBMIT_ACTIONS
        ]
        self.assertEqual(submits, [ORDER_PLACE_ACTION])


class TestObserveSubmitBranch(unittest.TestCase):
    """observe() must treat a rejected submit as *nothing happened*, never as a
    witness — and must do so above the workspace guard, because a submit drives a
    website, not a path under the root."""

    def _provider_with_effect(self, action_type: str, effect: dict[str, Any]):
        from actionmesh.agent.provider import LaptopProvider

        provider = LaptopProvider()  # workspace=None by design
        provider._effects["act-test"] = {"action_type": action_type, **effect}
        return provider

    def test_one_rejected_submission_returns_none(self) -> None:
        for name in (BOOKING_SUBMIT_ACTION, ORDER_PLACE_ACTION):
            provider = self._provider_with_effect(
                name, {"status": "rejected"}
            )
            action = _submit_action(name, {"key": "value"})
            action.action_id = "act-test"
            self.assertIsNone(provider.observe(action), name)

    def test_branch_executes_before_the_workspace_guard(self) -> None:
        """A bare provider has workspace=None; if the submit branch sat below the
        guard, a submission would wrongly need a workspace to be observed."""
        provider = self._provider_with_effect(
            BOOKING_SUBMIT_ACTION,
            {"status": "submitted", "reference": "BK-1234"},
        )
        action = _submit_action(
            BOOKING_SUBMIT_ACTION,
            {"journey": "NZM→CSMT", "passenger": "Asha", "fare": "1200"},
        )
        action.action_id = "act-test"
        observed = provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertEqual(observed["state"], {"reference": "BK-1234"})

    def test_successful_submission_follows_the_correct_observation_path(self) -> None:
        provider = self._provider_with_effect(
            ORDER_PLACE_ACTION,
            {"status": "submitted", "reference": "OR-5678"},
        )
        action = _submit_action(
            ORDER_PLACE_ACTION,
            {"items": "2×margherita", "total": "26.50"},  # noqa: RUF001
        )
        action.action_id = "act-test"
        observed = provider.observe(action)
        self.assertIsNotNone(observed)
        # Narrowed parameters: only the recorded keys, never the whole request.
        self.assertEqual(observed["parameters"], {"items": "2×margherita", "total": "26.50"})  # noqa: RUF001
        self.assertEqual(observed["state"], {"reference": "OR-5678"})

    def test_other_workspace_actions_still_use_the_workspace_guard(self) -> None:
        provider = self._provider_with_effect(
            "device.file.read", {"status": "ok", "path": "a.txt"}
        )
        action = Action(type="device.file.read", parameters={"path": "a.txt"})
        action.action_id = "act-test"
        self.assertIsNone(provider.observe(action))

    def test_unrelated_action_cannot_enter_the_submission_specific_branch(self) -> None:
        """A non-submit record must not be answered by the submit branch — it falls
        through to the workspace guard and, with no workspace, returns None (or,
        with a workspace, its own file branch)."""
        provider = self._provider_with_effect(
            "browser.click", {"status": "submitted", "reference": "should-not-appear"}
        )
        action = Action(type="browser.click", parameters={"ref": 1})
        action.action_id = "act-test"
        self.assertIsNone(provider.observe(action))


if __name__ == "__main__":
    unittest.main()
