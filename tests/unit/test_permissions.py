"""C3 — Permission catalog, matching, risk and consent store."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from actionmesh.core.permissions import (
    NAMESPACES,
    Consent,
    ConsentError,
    ConsentStore,
    PermissionGrant,
    decision_rank,
    is_concrete,
    is_known,
    is_namespace,
    is_wildcard,
    known_permissions,
    permission,
    permission_scope,
    risk_at_least,
    risk_rank,
    rule_matches,
    rule_specificity,
    scope_satisfied,
)
from actionmesh.protocol.models import PolicyDecision, RiskLevel


class TestCatalog(unittest.TestCase):
    def test_catalog_is_non_empty(self) -> None:
        self.assertGreaterEqual(len(known_permissions()), 1)

    def test_expected_scopes_present(self) -> None:
        for scope in (
            "calendar.read",
            "calendar.write",
            "message.read",
            "message.send",
            "file.read",
            "file.write",
            "file.share",
            "file.delete",
            "device.notify",
            "location.read",
            "payment.prepare",
            "payment.execute",
            "payment.verify",
            "system.app.launch",
        ):
            with self.subTest(scope=scope):
                self.assertTrue(is_known(scope))

    def test_every_scope_is_concrete(self) -> None:
        for scope in known_permissions():
            with self.subTest(scope=scope):
                self.assertTrue(is_concrete(scope))

    def test_unknown_scope_is_not_known(self) -> None:
        self.assertFalse(is_known("does.not.exist"))
        self.assertFalse(is_concrete("does.not.exist"))
        self.assertFalse(is_known(""))

    def test_wildcards_are_not_concrete(self) -> None:
        self.assertFalse(is_concrete("*"))
        self.assertFalse(is_concrete("message.*"))

    def test_namespace_predicate(self) -> None:
        self.assertTrue(is_namespace("message.*"))
        self.assertFalse(is_namespace("nosuch.*"))
        self.assertFalse(is_namespace("message.send"))

    def test_global_wildcard_predicate(self) -> None:
        self.assertTrue(is_wildcard("*"))
        self.assertFalse(is_wildcard("message.*"))

    def test_permission_lookup_returns_metadata(self) -> None:
        self.assertEqual(permission("payment.execute").name, "payment.execute")
        self.assertIs(permission("payment.execute").risk_level, RiskLevel.CRITICAL)

    def test_permission_lookup_raises_for_unknown(self) -> None:
        with self.assertRaises(KeyError):
            permission("nope.nope")



class TestMatching(unittest.TestCase):
    def test_grant_matching_is_exact_only(self) -> None:
        self.assertTrue(scope_satisfied("message.send", "message.send"))
        self.assertFalse(scope_satisfied("message.*", "message.send"))
        self.assertFalse(scope_satisfied("*", "message.send"))
        self.assertFalse(scope_satisfied("message", "message.send"))

    def test_rule_specificity_ordering(self) -> None:
        self.assertGreater(rule_specificity("message.send"), rule_specificity("message.*"))
        self.assertGreater(rule_specificity("message.*"), rule_specificity("*"))

    def test_rule_matching(self) -> None:
        self.assertTrue(rule_matches("message.send", "message.send"))
        self.assertTrue(rule_matches("message.*", "message.send"))
        self.assertTrue(rule_matches("*", "anything.at.all"))
        self.assertFalse(rule_matches("message.*", "file.read"))
        self.assertFalse(rule_matches("", "message.send"))

    def test_decision_rank_is_restrictiveness(self) -> None:
        self.assertLess(
            decision_rank(PolicyDecision.ALLOW), decision_rank(PolicyDecision.DENY)
        )
        self.assertLess(
            decision_rank(PolicyDecision.ASK), decision_rank(PolicyDecision.DENY)
        )


class TestPermissionGrant(unittest.TestCase):
    def test_grant_without_expiry_is_active(self) -> None:
        g = PermissionGrant(scope="message.send")
        self.assertTrue(g.is_active(datetime.now(UTC), RiskLevel.MEDIUM))

    def test_expired_grant_is_inactive(self) -> None:
        past = datetime.now(UTC) - timedelta(hours=1)
        g = PermissionGrant(scope="message.send", expires_at=past)
        self.assertTrue(g.is_expired())
        self.assertFalse(g.is_active())

    def test_future_expiry_is_active(self) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)
        g = PermissionGrant(scope="message.send", expires_at=future)
        self.assertTrue(g.is_active())

    def test_grant_risk_ceiling_is_enforced(self) -> None:
        g = PermissionGrant(scope="payment.execute", max_risk=RiskLevel.HIGH)
        self.assertFalse(g.is_active(risk=RiskLevel.CRITICAL))
        self.assertTrue(g.is_active(risk=RiskLevel.HIGH))


class TestConsentStore(unittest.TestCase):
    def test_concrete_consent_is_stored(self) -> None:
        store = ConsentStore()
        store.grant(Consent(scope="message.send"))
        self.assertEqual(len(store.consents("message.send")), 1)

    def test_wildcard_consent_is_refused(self) -> None:
        store = ConsentStore()
        for bad in ("*", "message.*", ""):
            with self.subTest(scope=bad), self.assertRaises(ConsentError):
                store.grant(Consent(scope=bad))
        self.assertEqual(len(store), 0)

    def test_unknown_scope_is_refused(self) -> None:
        with self.assertRaises(ConsentError):
            ConsentStore().grant(Consent(scope="not.a.scope"))

    def test_revoke_removes_consent(self) -> None:
        store = ConsentStore()
        store.grant(Consent(scope="message.send"))
        self.assertEqual(store.revoke("message.send"), 1)
        self.assertEqual(len(store), 0)

    def test_grants_flattens_consents(self) -> None:
        store = ConsentStore()
        store.grant(Consent(scope="message.send", max_risk=RiskLevel.HIGH))
        self.assertEqual([g.scope for g in store.grants()], ["message.send"])

    def test_grants_for_risk_respects_consent_ceiling(self) -> None:
        store = ConsentStore()
        store.grant(Consent(scope="payment.execute", max_risk=RiskLevel.HIGH))
        # A HIGH ceiling covers HIGH but never CRITICAL.
        self.assertEqual(len(store.grants(for_risk=RiskLevel.HIGH)), 1)
        self.assertEqual(store.grants(for_risk=RiskLevel.CRITICAL), [])
        store.grant(Consent(scope="payment.execute", max_risk=RiskLevel.CRITICAL))
        self.assertEqual(len(store.grants(for_risk=RiskLevel.CRITICAL)), 1)

    def test_expired_consent_yields_no_active_grant(self) -> None:
        store = ConsentStore()
        store.grant(
            Consent(
                scope="message.send",
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        self.assertEqual(store.grants(), [])

    def test_consent_converts_to_grant(self) -> None:
        grant = Consent(scope="file.share", max_risk=RiskLevel.MEDIUM).to_grant()
        self.assertEqual(grant.scope, "file.share")
        self.assertIs(grant.max_risk, RiskLevel.MEDIUM)


if __name__ == "__main__":
    unittest.main()

    def test_namespaces_cover_all_scopes(self) -> None:
        for scope in known_permissions():
            with self.subTest(scope=scope):
                self.assertIn(scope.split(".")[0], NAMESPACES)

    def test_permission_scope_returns_members(self) -> None:
        self.assertIn("message.send", permission_scope("message"))
        with self.assertRaises(KeyError):
            permission_scope("nosuch")


class TestRisk(unittest.TestCase):
    def test_rank_ordering(self) -> None:
        self.assertLess(risk_rank(RiskLevel.LOW), risk_rank(RiskLevel.MEDIUM))
        self.assertLess(risk_rank(RiskLevel.MEDIUM), risk_rank(RiskLevel.HIGH))
        self.assertLess(risk_rank(RiskLevel.HIGH), risk_rank(RiskLevel.CRITICAL))

    def test_unknown_risk_is_treated_as_critical(self) -> None:
        self.assertEqual(risk_rank("apocalyptic"), risk_rank(RiskLevel.CRITICAL))

    def test_risk_at_least(self) -> None:
        self.assertTrue(risk_at_least(RiskLevel.HIGH, RiskLevel.MEDIUM))
        self.assertFalse(risk_at_least(RiskLevel.LOW, RiskLevel.MEDIUM))
