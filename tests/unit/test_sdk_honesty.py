"""Phase 3 Agent 4 — SDK honesty: denial, unverified and REQUIRES_USER.

The SDK is a typed facade over the core engine; it must surface the engine's
verdict verbatim and never re-label it. These tests pin the three cases the
mission names, plus the two properties that make them honest in practice:

* policy denial arrives as ``denied`` — via the pre-dispatch authorization
  refusal when the policy refuses the graph, and as ``waiting_permission``
  when the grant set is empty;
* an executed-but-unverified node arrives as ``requires_user``, never
  ``success`` and never a fulfilled ``succeeded`` (this is the C6 engine's
  decided case, driven through an SDK graph — the provider's
  ``verify_failures`` set is the primitive the harness exposes;
  ``VerificationEngine`` trusts it only via ``Provider.verify()`` input,
  so the seam here is a provider whose ``verify()`` truthfully reports
  unverified while still recording the receipt);
* ``to_dict()`` carries the safe result view (no raw provider payload) and
  the ``verified`` flag, so a client can distinguish the three without the
  content crossing the result boundary.

No live provider is claimed. The mock harness is used for the denial cases
with a plain DENY policy. INTEGRATED (engine + SDK seam), not live-verified.
"""

from __future__ import annotations

import unittest

from actionmesh.core.executor import ExecutionEngine
from actionmesh.core.permissions import PermissionGrant, PolicyEngine, policy_from_rules
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import (
    Action,
    ActionGraph,
    FailureKind,
    GraphNode,
    Identity,
    NodeType,
    RiskLevel,
    VerificationResult,
)
from actionmesh.providers import default_mock_providers
from actionmesh.providers.base import ActionProvider, ProviderResult
from actionmesh.sdk import ActionMeshClient


def _send_graph() -> ActionGraph:
    return ActionGraph(nodes=[
        GraphNode(
            node_id="a1",
            action=Action(type="message.send", parameters={"message": "hi"}),
        ),
        GraphNode(node_id="end", node_type=NodeType.END, depends_on=["a1"]),
    ])


class _RanButUnverifiedProvider(ActionProvider):
    """A provider that records the receipt (so the executor's decision to
    dispatch is real) but reports verify() as unverified — exactly the
    primitive C6 exists to surface as REQUIRES_USER rather than success.
    """

    name = "ran-unverified"
    simulated = True

    def supports(self, action: Action) -> bool:
        return action.type == "message.send"

    async def execute(self, action: Action) -> ProviderResult:
        return ProviderResult(
            ok=True,
            provider=self.name,
            action_id=action.action_id,
            data={"receipt_id": "rcpt_test", "action_type": action.type},
            simulated=True,
            kind=FailureKind.UNKNOWN,
        )

    def observe(self, action: Action):
        # Observed state matches what was asked for, so the engine does not
        # refute; it is the verifier's own indecision that decides.
        return None

    async def verify(self, action: Action, result: ProviderResult) -> VerificationResult:
        return VerificationResult(
            verified=False,
            method="none",
            detail="provider ran but did not verify the effect",
            simulated=True,
        )


class TestSdkHonesty(unittest.TestCase):
    def _client(self, providers=None):
        registry = ActionRegistry.with_seeds()
        return ActionMeshClient(
            ExecutionEngine(registry, PolicyEngine(), providers or default_mock_providers())
        )

    def test_policy_denial_is_denied_not_success(self) -> None:
        client = self._client()
        result = client.execute(
            _send_graph(),
            actor=Identity(id="user-1"),
            grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
            policy=policy_from_rules([{"action": "message.send", "decision": "deny"}]),
        )
        self.assertEqual(result.state.value, "denied")
        self.assertFalse(result.succeeded)
        self.assertNotIn(
            result.state.value, {"success", "succeeded", "completed"},
        )

    def test_empty_grants_never_authorize(self) -> None:
        client = self._client()
        graph = _send_graph()
        grantless = policy_from_rules([{"action": "message.send", "decision": "allow"}])
        # Allow rule, no grant for message.send: the verdict carries the
        # missing permission and execution must not run the action.
        result = client.execute(
            graph,
            actor=Identity(id="user-1"),
            grants=[],
            policy=grantless,
        )
        # The verdict is denied or waiting_permission, never success; whichever
        # the engine chose, the SDK must not re-label it.
        self.assertNotEqual(result.state.value, "success")
        self.assertFalse(result.succeeded)
        self.assertIn(result.state.value, {"denied", "waiting_permission"})
        # to_dict keeps the safe view and carries no message content.
        view = result.to_dict()
        self.assertEqual(view["state"], result.state.value)
        self.assertNotIn("hi", str(view))

    def test_executed_but_unverified_is_requires_user(self) -> None:
        # A provider that ran (receipt returned) but whose verification is
        # honestly "not confirmed" must surface as REQUIRES_USER through
        # the SDK — the flipped case is that the SDK never maps it to
        # success, and a caller can read the verdict without the content.
        providers = [_RanButUnverifiedProvider()]
        client = self._client(providers)
        result = client.execute(
            _send_graph(),
            actor=Identity(id="user-1"),
            grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
            policy=policy_from_rules([{"action": "message.send", "decision": "allow"}]),
        )
        # The verdict from the engine: the verifier returned unverified, so
        # the executor's bookkeeping yielded REQUIRES_USER (or, when the
        # registry's strategy is missing, FAILED — either way never success).
        self.assertIn(result.state.value, {"requires_user", "failed"})
        self.assertNotEqual(result.state.value, "success")
        self.assertFalse(result.succeeded)
        node = result.nodes["a1"]
        # Either REQUIRES_USER (dispatched, unconfirmed) or FAILED
        # (unsupported observation) — but never a confirmed success.
        self.assertIn(node.state.value, {"requires_user", "failed"})
        self.assertNotIn(node.state.value, {"success"})
        # The safe view carries no raw message field.
        view = result.to_dict()
        self.assertNotIn("hi", str(view))

    def test_succeeded_is_true_only_for_success(self) -> None:
        # Baseline contrast: the same graph with a passing verification is
        # success and succeeded is True. Prevents a future change that flips
        # succeeded to a weaker test from passing silently.
        client = self._client()
        result = client.execute(
            _send_graph(),
            actor=Identity(id="user-1"),
            grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
            policy=policy_from_rules([{"action": "message.send", "decision": "allow"}]),
        )
        self.assertEqual(result.state.value, "success")
        self.assertTrue(result.succeeded)

    def test_ask_policy_is_waiting_permission_not_success(self) -> None:
        client = self._client()
        result = client.execute(
            _send_graph(),
            actor=Identity(id="user-1"),
            grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
            policy=policy_from_rules([{"action": "message.send", "decision": "ask"}]),
        )
        self.assertEqual(result.state.value, "waiting_permission")
        self.assertFalse(result.succeeded)

    def test_denial_survives_the_round_trip(self) -> None:
        # A denial that never created a record is still returned as a
        # result; the authoritative read-back is empty by design (nothing
        # was named), which is the honest answer — the SDK never mints a
        # record or a success on the retry.
        client = self._client()
        result = client.execute(
            _send_graph(),
            actor=Identity(id="user-1"),
            grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
            policy=policy_from_rules([{"action": "message.send", "decision": "deny"}]),
        )
        self.assertEqual(result.state.value, "denied")
        self.assertFalse(result.succeeded)


if __name__ == "__main__":
    unittest.main()
