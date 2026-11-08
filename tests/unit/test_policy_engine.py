"""C3 — PolicyEngine: the trusted authorization boundary."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from actionmesh.core.permissions import (
    PermissionGrant,
    PolicyContext,
    PolicyEngine,
    build_context,
    default_policy,
    normalize_decision,
    policy_from_rules,
)
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import (
    Action,
    ActionDefinition,
    ApprovalRequest,
    Identity,
    Policy,
    PolicyDecision,
    PolicyRule,
    RiskLevel,
)

ALLOW_ALL = policy_from_rules([{"action": "*", "decision": "allow"}])


def grant(scope: str, cap: RiskLevel = RiskLevel.HIGH) -> PermissionGrant:
    return PermissionGrant(scope=scope, max_risk=cap)


class PolicyEngineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ActionRegistry.with_seeds()
        self.engine = PolicyEngine()

    def context(
        self,
        action_type: str,
        params: dict,
        *,
        grants: list[PermissionGrant],
        policy: Policy | None = None,
        actor: Identity | None = Identity(id="user_1"),
        allow_high_risk: bool = False,
        approvals: list[ApprovalRequest] | None = None,
    ) -> PolicyContext:
        resolved = self.registry.resolve({"type": action_type, "parameters": params})
        return build_context(
            resolved.action,
            resolved.definition,
            actor,
            grants=grants,
            policy=policy if policy is not None else default_policy(),
            approvals=approvals or [],
            allow_high_risk=allow_high_risk,
        )

    def decide(self, ctx: PolicyContext):
        return self.engine.evaluate(ctx)


class TestPolicyConstruction(PolicyEngineCase):
    def test_normalize_decision_known_values(self) -> None:
        self.assertIs(normalize_decision("allow"), PolicyDecision.ALLOW)
        self.assertIs(normalize_decision("deny"), PolicyDecision.DENY)
        self.assertIs(normalize_decision("ask"), PolicyDecision.ASK)
        self.assertIs(
            normalize_decision("allow_with_limit"), PolicyDecision.ALLOW_WITH_LIMIT
        )

    def test_normalize_decision_aliases(self) -> None:
        self.assertIs(normalize_decision("deny_without_confirmation"), PolicyDecision.ASK)
        self.assertIs(normalize_decision("confirm"), PolicyDecision.ASK)

    def test_normalize_unknown_is_deny(self) -> None:
        self.assertIs(normalize_decision("maybe"), PolicyDecision.DENY)
        self.assertIs(normalize_decision(None), PolicyDecision.DENY)

    def test_malformed_rule_becomes_deny(self) -> None:
        policy = policy_from_rules(["not-a-dict", {"decision": "allow"}])
        self.assertEqual(len(policy.rules), 2)
        for rule in policy.rules:
            self.assertIs(rule.decision, PolicyDecision.DENY)

    def test_default_policy_denies_by_default(self) -> None:
        self.assertIs(default_policy().default_decision, PolicyDecision.DENY)

    def test_default_policy_marks_messages_ask(self) -> None:
        rule = self.engine.select_rule(default_policy(), "message.send")
        self.assertIsNotNone(rule)
        assert rule is not None
        self.assertIs(rule.decision, PolicyDecision.ASK)

    def test_exact_rule_beats_namespace_rule(self) -> None:
        policy = policy_from_rules(
            [
                {"action": "message.*", "decision": "deny"},
                {"action": "message.send", "decision": "allow"},
            ]
        )
        rule = self.engine.select_rule(policy, "message.send")
        assert rule is not None
        self.assertEqual(rule.action, "message.send")

    def test_equal_specificity_resolves_to_most_restrictive(self) -> None:
        policy = policy_from_rules(
            [
                {"action": "*", "decision": "allow"},
                {"action": "*", "decision": "deny"},
            ]
        )
        rule = self.engine.select_rule(policy, "whatever")
        assert rule is not None
        self.assertIs(rule.decision, PolicyDecision.DENY)

    def test_policy_rule_objects_are_accepted(self) -> None:
        policy = policy_from_rules([PolicyRule(action="*", decision="allow")])
        assert policy.rules[0].decision is PolicyDecision.ALLOW


class TestPolicyDecisions(PolicyEngineCase):
    def test_message_send_asks_and_requires_approval(self) -> None:
        ctx = self.context("message.send", {"message": "hi"}, grants=[grant("message.send")])
        outcome = self.decide(ctx)
        self.assertIs(outcome.decision, PolicyDecision.ASK)
        self.assertTrue(outcome.requires_approval)
        self.assertFalse(outcome.authorizes_execution())

    def test_allow_decision_executes_without_approval(self) -> None:
        ctx = self.context(
            "reminder.create",
            {"text": "call"},
            grants=[grant("calendar.write")],
            policy=ALLOW_ALL,
        )
        outcome = self.decide(ctx)
        self.assertIs(outcome.decision, PolicyDecision.ALLOW)
        self.assertFalse(outcome.requires_approval)
        self.assertTrue(outcome.authorizes_execution())

    def test_deny_decision_blocks(self) -> None:
        policy = policy_from_rules([{"action": "*", "decision": "deny"}])
        ctx = self.context(
            "reminder.create",
            {"text": "call"},
            grants=[grant("calendar.write")],
            policy=policy,
        )
        self.assertFalse(self.decide(ctx).authorizes_execution())

    def test_unlisted_action_falls_through_to_deny(self) -> None:
        ctx = self.context(
            "device.notify",
            {"device_id": "d1", "title": "hi"},
            grants=[grant("device.notify")],
            policy=Policy(),
        )
        self.assertIs(self.decide(ctx).decision, PolicyDecision.DENY)


class TestFailClosed(PolicyEngineCase):
    def test_missing_actor_denies(self) -> None:
        ctx = self.context(
            "reminder.create", {"text": "call"}, grants=[grant("calendar.write")],
            actor=None,
        )
        outcome = self.decide(ctx)
        self.assertIs(outcome.decision, PolicyDecision.DENY)
        self.assertIn("actor", " ".join(outcome.reasons))

    def test_missing_action_denies(self) -> None:
        ctx = PolicyContext(actor=Identity(id="u1"), policy=ALLOW_ALL)
        self.assertIs(self.decide(ctx).decision, PolicyDecision.DENY)

    def test_missing_definition_denies(self) -> None:
        ctx = PolicyContext(
            action=Action(type="message.send"), actor=Identity(id="u1"), policy=ALLOW_ALL
        )
        self.assertIs(self.decide(ctx).decision, PolicyDecision.DENY)

    def test_definition_mismatch_denies(self) -> None:
        ctx = build_context(
            Action(type="message.send"),
            ActionDefinition(name="other.action", permissions=["message.send"]),
            Identity(id="u1"),
            policy=ALLOW_ALL,
        )
        self.assertIs(self.decide(ctx).decision, PolicyDecision.DENY)

    def test_definition_without_permissions_denies(self) -> None:
        ctx = build_context(
            Action(type="message.send"),
            ActionDefinition(name="message.send", permissions=[]),
            Identity(id="u1"),
            policy=ALLOW_ALL,
        )
        self.assertIs(self.decide(ctx).decision, PolicyDecision.DENY)

    def test_missing_permission_denies(self) -> None:
        ctx = self.context(
            "message.send", {"message": "hi"}, grants=[grant("file.read")], policy=ALLOW_ALL
        )
        outcome = self.decide(ctx)
        self.assertIs(outcome.decision, PolicyDecision.DENY)
        self.assertIn("message.send", outcome.missing_permissions)

    def test_wildcard_grant_does_not_satisfy_requirement(self) -> None:
        ctx = self.context(
            "message.send", {"message": "hi"}, grants=[grant("*")], policy=ALLOW_ALL
        )
        self.assertIs(self.decide(ctx).decision, PolicyDecision.DENY)

    def test_wildcard_requirement_in_definition_denies(self) -> None:
        ctx = build_context(
            Action(type="message.send"),
            ActionDefinition(name="message.send", permissions=["message.*"]),
            Identity(id="u1"),
            policy=ALLOW_ALL,
        )
        self.assertIs(self.decide(ctx).decision, PolicyDecision.DENY)

    def test_expired_grant_denies(self) -> None:
        ctx = self.context(
            "message.send",
            {"message": "hi"},
            grants=[
                PermissionGrant(
                    scope="message.send",
                    expires_at=datetime.now(UTC) - timedelta(hours=1),
                )
            ],
            policy=ALLOW_ALL,
        )
        self.assertIs(self.decide(ctx).decision, PolicyDecision.DENY)

    def test_non_grant_objects_are_ignored(self) -> None:
        ctx = self.context("message.send", {"message": "hi"}, grants=[], policy=ALLOW_ALL)
        ctx.grants = ["message.send"]  # type: ignore[list-item]
        self.assertIs(self.decide(ctx).decision, PolicyDecision.DENY)


class TestRiskGate(PolicyEngineCase):
    def test_critical_always_asks_even_with_allow_policy(self) -> None:
        ctx = self.context(
            "payment.confirm",
            {"payment_id": "p1"},
            grants=[grant("payment.execute", RiskLevel.CRITICAL)],
            policy=ALLOW_ALL,
        )
        outcome = self.decide(ctx)
        self.assertIs(outcome.decision, PolicyDecision.ASK)
        self.assertTrue(outcome.requires_approval)
        self.assertIs(outcome.effective_risk, RiskLevel.CRITICAL)

    def test_high_risk_asks_without_server_opt_in(self) -> None:
        ctx = self.context(
            "file.share",
            {"path": "/a.pdf"},
            grants=[grant("file.share")],
            policy=ALLOW_ALL,
        )
        self.assertTrue(self.decide(ctx).requires_approval)

    def test_high_risk_allows_with_server_opt_in(self) -> None:
        ctx = self.context(
            "file.share",
            {"path": "/a.pdf"},
            grants=[grant("file.share")],
            policy=ALLOW_ALL,
            allow_high_risk=True,
        )
        outcome = self.decide(ctx)
        self.assertIs(outcome.decision, PolicyDecision.ALLOW)
        self.assertFalse(outcome.requires_approval)

    def test_caller_cannot_lower_risk(self) -> None:
        resolved = self.registry.resolve(
            {
                "type": "payment.confirm",
                "risk_level": "low",
                "parameters": {"payment_id": "p1"},
            }
        )
        ctx = build_context(
            resolved.action,
            resolved.definition,
            Identity(id="u1"),
            grants=[grant("payment.execute", RiskLevel.CRITICAL)],
            policy=ALLOW_ALL,
        )
        self.assertIs(self.decide(ctx).effective_risk, RiskLevel.CRITICAL)

    def test_grant_capped_below_action_risk_is_insufficient(self) -> None:
        ctx = self.context(
            "payment.confirm",
            {"payment_id": "p1"},
            grants=[grant("payment.execute", RiskLevel.HIGH)],
            policy=ALLOW_ALL,
        )
        self.assertIs(self.decide(ctx).decision, PolicyDecision.DENY)


class TestApproval(PolicyEngineCase):
    def test_approval_upgrades_ask_to_allow(self) -> None:
        base = self.context(
            "message.send", {"message": "hi"}, grants=[grant("message.send")]
        )
        base.approvals = [
            ApprovalRequest(action_id=base.action.action_id, status="approved")
        ]
        outcome = self.decide(base)
        self.assertIs(outcome.decision, PolicyDecision.ALLOW)
        self.assertFalse(outcome.requires_approval)

    def test_rejected_approval_does_not_authorize(self) -> None:
        base = self.context(
            "message.send", {"message": "hi"}, grants=[grant("message.send")]
        )
        base.approvals = [ApprovalRequest(action_id=base.action.action_id, status="rejected")]
        self.assertIs(self.decide(base).decision, PolicyDecision.ASK)

    def test_approval_cannot_upgrade_a_deny(self) -> None:
        policy = policy_from_rules([{"action": "*", "decision": "deny"}])
        base = self.context(
            "reminder.create",
            {"text": "call"},
            grants=[grant("calendar.write")],
            policy=policy,
        )
        base.approvals = [ApprovalRequest(action_id=base.action.action_id, status="approved")]
        self.assertIs(self.decide(base).decision, PolicyDecision.DENY)

    def test_approval_for_other_action_is_ignored(self) -> None:
        base = self.context(
            "message.send", {"message": "hi"}, grants=[grant("message.send")]
        )
        base.approvals = [ApprovalRequest(action_id="act_other", status="approved")]
        self.assertIs(self.decide(base).decision, PolicyDecision.ASK)


class TestExplain(PolicyEngineCase):
    def test_explain_is_json_serializable_and_complete(self) -> None:
        import json

        ctx = self.context("message.send", {"message": "hi"}, grants=[grant("message.send")])
        report = self.engine.explain(ctx)
        json.dumps(report)
        self.assertEqual(report["decision"], "ask")
        self.assertTrue(report["requires_approval"])
        self.assertFalse(report["authorizes_execution"])
        self.assertIn("message.send", report["required_permissions"])
        self.assertTrue(report["reasons"])


if __name__ == "__main__":
    unittest.main()
