"""C6 — Verification + Recovery tests.

The central property: an action is confirmed only by independent evidence of its
effect. A provider that says "it worked" is not enough, and an action with no
verification strategy is not silently treated as successful.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from actionmesh.core.graph import GraphBuilder
from actionmesh.core.permissions import (
    PermissionGrant,
    PolicyContext,
    policy_from_rules,
)
from actionmesh.core.registry import ActionRegistry
from actionmesh.core.verification import (
    Evidence,
    Recovery,
    RecoveryPlanner,
    VerificationEngine,
    VerificationStatus,
    decide,
    default_engine,
    unsupported,
)
from actionmesh.protocol.models import (
    Action,
    FailureKind,
    Identity,
    RiskLevel,
)
from actionmesh.providers import MockProvider, ProviderResult

ALLOW_ALL = policy_from_rules([{"action": "*", "decision": "allow"}])


def ctx(scopes: list[str]) -> PolicyContext:
    return PolicyContext(
        actor=Identity(id="u1"),
        policy=ALLOW_ALL,
        grants=[PermissionGrant(scope=s, max_risk=RiskLevel.HIGH) for s in scopes],
    )


def action(kind: str = "reminder.create", **params: Any) -> Action:
    base: dict[str, Any] = {"text": "call"}
    base.update(params)
    return Action(type=kind, parameters=base)


def ok_result() -> ProviderResult:
    return ProviderResult(ok=True, provider="mock", data={"receipt_id": "r1"})


class TestEvidenceWeighting(unittest.TestCase):
    def test_observed_state_outweighs_provider_claim(self) -> None:
        outcome = decide(
            "s",
            [Evidence("provider_claim", "provider said ok"), Evidence("observed_state", "present")],
        )
        self.assertTrue(outcome.verified)
        self.assertIn("observed_state", outcome.detail)

    def test_provider_claim_alone_is_insufficient(self) -> None:
        """A claim with no independent check must not confirm the effect."""
        outcome = decide("s", [Evidence("provider_claim", "trust me")])
        self.assertFalse(outcome.verified)
        self.assertIs(outcome.status, VerificationStatus.INDETERMINATE)

    def test_no_evidence_is_never_confirmation(self) -> None:
        outcome = decide("s", [])
        self.assertFalse(outcome.verified)
        self.assertIs(outcome.status, VerificationStatus.INDETERMINATE)

    def test_caller_assertion_has_zero_weight(self) -> None:
        outcome = decide("s", [Evidence("caller_assertion", "I promise")])
        self.assertFalse(outcome.verified)

    def test_contradiction_refutes(self) -> None:
        outcome = decide(
            "s",
            [Evidence("observed_state", "ok"), Evidence("contradiction", "absent")],
        )
        self.assertFalse(outcome.verified)
        self.assertIs(outcome.status, VerificationStatus.REFUTED)
        self.assertIs(outcome.failure_kind, FailureKind.PERMANENT)

    def test_to_result_projects_onto_protocol(self) -> None:
        result = decide("s", [Evidence("observed_state", "ok")]).to_result()
        self.assertTrue(result.verified)
        self.assertEqual(result.method, "s")


class TestUnsupported(unittest.TestCase):
    def test_unregistered_action_type_is_not_confirmed(self) -> None:
        outcome = unsupported("some.unknown.action")
        self.assertFalse(outcome.verified)
        self.assertIs(outcome.status, VerificationStatus.UNSUPPORTED)


class TestVerificationEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = MockProvider()
        self.engine = default_engine()

    def test_every_seeded_action_has_a_strategy(self) -> None:
        for cap in ActionRegistry.with_seeds().discover():
            with self.subTest(action=cap.action):
                self.assertIsNotNone(self.engine.strategy_for(cap.action))

    def test_unknown_action_type_is_unsupported(self) -> None:
        outcome = self.engine.verify(action("not.registered"), ok_result(), self.provider)
        self.assertIs(outcome.status, VerificationStatus.UNSUPPORTED)

    def test_observed_state_confirms(self) -> None:
        act = action()
        asyncio.run(self.provider.execute(act))
        self.assertTrue(self.engine.verify(act, ok_result(), self.provider).verified)

    def test_missing_state_refutes_even_when_provider_claims_success(self) -> None:
        act = action()  # never executed, so nothing was recorded
        outcome = self.engine.verify(act, ok_result(), self.provider)
        self.assertFalse(outcome.verified)
        self.assertIs(outcome.status, VerificationStatus.REFUTED)

    def test_provider_reported_failure_is_contradiction(self) -> None:
        act = action()
        asyncio.run(self.provider.execute(act))
        outcome = self.engine.verify(act, ProviderResult(ok=False), self.provider)
        self.assertIs(outcome.status, VerificationStatus.REFUTED)

    def test_state_for_a_different_key_does_not_confirm(self) -> None:
        """A record from an earlier run must not satisfy a fresh request."""
        asyncio.run(self.provider.execute(action(idempotency_key="old_key")))
        outcome = self.engine.verify(action(idempotency_key="new_key"), ok_result(), self.provider)
        self.assertFalse(outcome.verified)

    def test_mismatched_parameters_do_not_confirm(self) -> None:
        act = action(text="original")
        asyncio.run(self.provider.execute(act))
        altered = action(text="tampered", idempotency_key=act.idempotency_key)
        self.assertFalse(self.engine.verify(altered, ok_result(), self.provider).verified)

    def test_provider_without_observe_is_not_confirmed(self) -> None:
        class Opaque(MockProvider):
            observe = None  # type: ignore[assignment]

        opaque = Opaque()
        act = action()
        asyncio.run(opaque.execute(act))
        self.assertFalse(self.engine.verify(act, ok_result(), opaque).verified)

    def test_custom_engine_can_register_strategies(self) -> None:
        engine = VerificationEngine()
        engine.register("my.action", "observed_state")
        self.assertEqual(engine.strategy_for("my.action"), "observed_state")
        self.assertIsNone(engine.strategy_for("other.action"))


def _graph(*, compensate: bool = False):
    b = GraphBuilder("recovery")
    b.action(action(), node_id="a1")
    if compensate:
        b.compensate("a1", action(kind="calendar.delete", event_id="e1"), node_id="c1")
        b.end(depends_on=["a1", "c1"])
    else:
        b.end(depends_on=["a1"])
    return b.build()


class TestRecoveryPlanner(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = RecoveryPlanner()

    def test_transient_with_attempts_left_retries(self) -> None:
        plan = self.planner.plan(
            _graph(), "a1", FailureKind.TRANSIENT, attempts_used=1, max_attempts=3
        )
        self.assertIs(plan.recovery, Recovery.RETRY)
        self.assertTrue(plan.reversible)

    def test_permanent_failure_does_not_retry(self) -> None:
        plan = self.planner.plan(
            _graph(), "a1", FailureKind.PERMANENT, attempts_used=1, max_attempts=3
        )
        self.assertIsNot(plan.recovery, Recovery.RETRY)

    def test_reversible_with_compensation_compensates(self) -> None:
        plan = self.planner.plan(
            _graph(compensate=True), "a1", FailureKind.PERMANENT,
            attempts_used=3, max_attempts=3,
        )
        self.assertIs(plan.recovery, Recovery.COMPENSATE)
        self.assertEqual(plan.compensation_node, "c1")

    def test_reversible_without_compensation_escalates(self) -> None:
        plan = self.planner.plan(
            _graph(), "a1", FailureKind.PERMANENT, attempts_used=3, max_attempts=3
        )
        self.assertIs(plan.recovery, Recovery.ESCALATE)
        self.assertIn("no compensation", plan.reason)

    def test_irreversible_action_is_not_claimed_compensatable(self) -> None:
        """Never promise a rollback for something that cannot be undone."""
        b = GraphBuilder("send")
        b.action(Action(type="message.send", parameters={"message": "hi"}), node_id="m1")
        b.compensate("m1", Action(type="message.send", parameters={"message": "undo"}), node_id="c1")
        b.end(depends_on=["m1", "c1"])
        plan = self.planner.plan(
            b.build(), "m1", FailureKind.PERMANENT, attempts_used=1, max_attempts=1
        )
        self.assertIs(plan.recovery, Recovery.NOT_COMPENSATABLE)
        self.assertFalse(plan.reversible)
        self.assertIn("irreversible", plan.reason)

    def test_rollback_scope_excludes_the_failed_node(self) -> None:
        planner = RecoveryPlanner(completed={"a1", "a2"})
        self.assertNotIn("a2", planner.rollback_scope("a2"))
        self.assertIn("a1", planner.rollback_scope("a2"))

    def test_plan_is_json_serializable(self) -> None:
        import json

        plan = self.planner.plan(
            _graph(), "a1", FailureKind.TRANSIENT, attempts_used=0, max_attempts=3
        )
        json.dumps(plan.to_dict())


if __name__ == "__main__":
    unittest.main()

