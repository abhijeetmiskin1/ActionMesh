"""C2 — Action Registry tests: validation, versions, discovery, resolution."""

from __future__ import annotations

import unittest

from actionmesh.core.registry import (
    ActionRegistry,
    DefinitionError,
    DuplicateError,
    UnknownActionError,
    max_risk,
    seed_definitions,
    validate_definition,
    version_key,
)
from actionmesh.protocol.models import (
    ActionDefinition,
    RetryPolicy,
    RiskLevel,
    VerificationSpec,
)
from actionmesh.protocol.validate import ValidationError


def make_def(
    name: str = "test.action",
    *,
    version: str = "1.0",
    risk: RiskLevel = RiskLevel.MEDIUM,
    permissions: list[str] | None = None,
    properties: dict | None = None,
    required: list[str] | None = None,
) -> ActionDefinition:
    return ActionDefinition(
        name=name,
        version=version,
        description="test capability",
        risk_level=risk,
        permissions=["message.send"] if permissions is None else permissions,
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}} if properties is None else properties,
            "required": ["message"] if required is None else required,
        },
        verification=VerificationSpec(required=True, method="provider_verify"),
        provider="mock",
        retry_policy=RetryPolicy(max_attempts=2),
    )


class TestVersionKey(unittest.TestCase):
    def test_numeric_ordering(self) -> None:
        self.assertLess(version_key("1.9"), version_key("1.10"))
        self.assertEqual(version_key("1.0.0"), (1, 0, 0))

    def test_non_numeric_is_zero(self) -> None:
        self.assertEqual(version_key("x"), (0,))


class TestMaxRisk(unittest.TestCase):
    def test_never_lowers(self) -> None:
        self.assertIs(max_risk(RiskLevel.LOW, RiskLevel.HIGH), RiskLevel.HIGH)
        self.assertIs(max_risk(RiskLevel.CRITICAL, RiskLevel.LOW), RiskLevel.CRITICAL)
        self.assertIs(max_risk(RiskLevel.MEDIUM, RiskLevel.MEDIUM), RiskLevel.MEDIUM)


class TestDefinitionValidation(unittest.TestCase):
    def test_valid_definition_accepted(self) -> None:
        self.assertIsNotNone(validate_definition(make_def()))

    def test_empty_name_rejected(self) -> None:
        with self.assertRaises(DefinitionError):
            validate_definition(make_def(name=""))

    def test_bad_version_rejected(self) -> None:
        for bad in ("", "   ", "vNext"):
            with self.subTest(version=bad), self.assertRaises(DefinitionError):
                validate_definition(make_def(version=bad))

    def test_bad_timeout_rejected(self) -> None:
        d = make_def()
        d.timeout_s = 0
        with self.assertRaises(DefinitionError):
            validate_definition(d)

    def test_unknown_permission_rejected(self) -> None:
        with self.assertRaises(DefinitionError):
            validate_definition(make_def(permissions=["not.a.thing"]))

    def test_wildcard_permission_rejected(self) -> None:
        with self.assertRaises(DefinitionError):
            validate_definition(make_def(permissions=["*"]))

    def test_namespace_wildcard_permission_rejected(self) -> None:
        with self.assertRaises(DefinitionError):
            validate_definition(make_def(permissions=["message.*"]))

    def test_required_must_name_known_property(self) -> None:
        with self.assertRaises(DefinitionError):
            validate_definition(make_def(required=["nope"]))

    def test_non_object_schema_rejected(self) -> None:
        d = make_def()
        d.input_schema = "not a dict"  # type: ignore[assignment]
        with self.assertRaises(DefinitionError):
            validate_definition(d)

    def test_properties_must_be_object(self) -> None:
        d = make_def()
        d.input_schema = {"type": "object", "properties": []}  # type: ignore[dict-item]
        with self.assertRaises(DefinitionError):
            validate_definition(d)

    def test_bad_backoff_rejected(self) -> None:
        # RetryPolicy validates in __post_init__, so an invalid backoff is
        # rejected at construction — earlier than registry validation.
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=1, backoff="magic")

    def test_zero_max_attempts_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=0)


class TestRegistryBasics(unittest.TestCase):
    def test_seed_count_is_sixteen(self) -> None:
        self.assertEqual(len(seed_definitions()), 16)
        self.assertEqual(len(ActionRegistry.with_seeds()), 16)

    def test_register_and_get(self) -> None:
        reg = ActionRegistry()
        reg.register(make_def("custom.one"))
        self.assertEqual(reg.get("custom.one").name, "custom.one")

    def test_duplicate_name_version_rejected(self) -> None:
        reg = ActionRegistry()
        reg.register(make_def("custom.one"))
        with self.assertRaises(DuplicateError):
            reg.register(make_def("custom.one"))

    def test_different_version_allowed(self) -> None:
        reg = ActionRegistry()
        reg.register(make_def("custom.one", version="1.0"))
        reg.register(make_def("custom.one", version="2.0"))
        self.assertEqual(reg.versions("custom.one"), ["1.0", "2.0"])

    def test_unknown_action_raises(self) -> None:
        with self.assertRaises(UnknownActionError):
            ActionRegistry.with_seeds().get("nope.nope")

    def test_unknown_version_raises(self) -> None:
        with self.assertRaises(UnknownActionError):
            ActionRegistry.with_seeds().get("message.send", "9.9")

    def test_get_returns_highest_version_by_default(self) -> None:
        reg = ActionRegistry()
        reg.register(make_def("custom.one", version="1.0"))
        reg.register(make_def("custom.one", version="2.0"))
        self.assertEqual(reg.get("custom.one").version, "2.0")

    def test_explicit_version_returned(self) -> None:
        reg = ActionRegistry()
        reg.register(make_def("custom.one", version="1.0"))
        reg.register(make_def("custom.one", version="2.0"))
        self.assertEqual(reg.get("custom.one", "1.0").version, "1.0")

    def test_invalid_definition_cannot_register(self) -> None:
        reg = ActionRegistry()
        with self.assertRaises(DefinitionError):
            reg.register(make_def(permissions=["bogus.perm"]))
        self.assertEqual(len(reg), 0)


class TestDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = ActionRegistry.with_seeds()

    def test_all_capabilities_discovered(self) -> None:
        self.assertEqual(len(self.reg.discover()), 16)

    def test_filter_by_name(self) -> None:
        names = [c.action for c in self.reg.discover(name="message")]
        self.assertEqual(sorted(names), ["message.read", "message.send"])

    def test_filter_by_provider(self) -> None:
        self.assertEqual(len(self.reg.discover(provider="mock")), 16)
        self.assertEqual(len(self.reg.discover(provider="telegram")), 0)


class TestResolution(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = ActionRegistry.with_seeds()

    def test_resolve_known_action(self) -> None:
        r = self.reg.resolve({"type": "message.send", "parameters": {"message": "hi"}})
        self.assertEqual(r.definition.name, "message.send")
        self.assertEqual(r.warnings, [])

    def test_resolve_unknown_action_fails(self) -> None:
        with self.assertRaises(UnknownActionError):
            self.reg.resolve({"type": "totally.unknown", "parameters": {}})

    def test_resolve_missing_param_fails(self) -> None:
        with self.assertRaises(ValidationError):
            self.reg.resolve({"type": "message.send", "parameters": {}})

    def test_resolve_wrong_param_type_fails(self) -> None:
        with self.assertRaises(ValidationError):
            self.reg.resolve({"type": "message.send", "parameters": {"message": 1}})

    def test_resolve_rejects_action_without_type(self) -> None:
        with self.assertRaises(ValidationError):
            self.reg.resolve({"parameters": {"message": "hi"}})

    def test_resolve_rejects_non_dict_parameters(self) -> None:
        with self.assertRaises(ValidationError):
            self.reg.resolve({"type": "message.send", "parameters": "nope"})

    def test_risk_is_lifted_to_definition(self) -> None:
        r = self.reg.resolve(
            {"type": "message.send", "risk_level": "low", "parameters": {"message": "hi"}}
        )
        self.assertIs(r.effective_risk, RiskLevel.MEDIUM)
        self.assertTrue(any("risk lifted" in w for w in r.warnings))

    def test_caller_cannot_lower_critical_risk(self) -> None:
        r = self.reg.resolve(
            {
                "type": "payment.confirm",
                "risk_level": "low",
                "parameters": {"payment_id": "p1"},
            }
        )
        self.assertIs(r.effective_risk, RiskLevel.CRITICAL)

    def test_untrusted_state_is_reset_to_created(self) -> None:
        r = self.reg.resolve(
            {
                "type": "message.send",
                "state": "success",
                "parameters": {"message": "hi"},
            }
        )
        self.assertEqual(r.action.state.value, "created")

    def test_resolve_preserves_idempotency_key(self) -> None:
        r = self.reg.resolve(
            {
                "type": "message.send",
                "idempotency_key": "idem_fixed",
                "parameters": {"message": "hi"},
            }
        )
        self.assertEqual(r.action.idempotency_key, "idem_fixed")


if __name__ == "__main__":
    unittest.main()


    def test_filter_by_minimum_risk(self) -> None:
        caps = self.reg.discover(risk=RiskLevel.CRITICAL)
        self.assertEqual(
            sorted(c.action for c in caps), ["payment.confirm", "payment.prepare"]
        )

    def test_filter_by_permission(self) -> None:
        caps = self.reg.discover(permission="message.send")
        self.assertEqual([c.action for c in caps], ["message.send"])

    def test_discovery_returns_latest_version_only(self) -> None:
        reg = ActionRegistry()
        reg.register(make_def("custom.one", version="1.0"))
        reg.register(make_def("custom.one", version="2.0"))
        caps = reg.discover()
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0].version, "2.0")

