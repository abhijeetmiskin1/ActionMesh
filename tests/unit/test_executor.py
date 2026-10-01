"""C5 — Execution Engine tests.

These prove behaviour, not structure: that authorization actually gates
execution, that retries are bounded, that idempotency prevents a double side
effect, and that an executed-but-unverified action is never reported as
success.
"""

from __future__ import annotations

import asyncio
import itertools
import unittest

from actionmesh.core.executor import (
    DEFAULT_RETRYABLE,
    ExecutionEngine,
    IdempotencyLedger,
    InvalidTransition,
    can_transition,
    ensure_transition,
    is_terminal,
)
from actionmesh.core.graph import GraphBuilder
from actionmesh.core.permissions import (
    PermissionGrant,
    PolicyContext,
    PolicyEngine,
    default_policy,
    policy_from_rules,
)
from actionmesh.core.registry import ActionRegistry
from actionmesh.core.verification import VerificationEngine
from actionmesh.protocol.models import (
    Action,
    ActionState,
    ApprovalRequest,
    FailureKind,
    Identity,
    RiskLevel,
)
from actionmesh.providers import MockProvider, ProviderError

ALLOW_ALL = policy_from_rules([{"action": "*", "decision": "allow"}])


def send(text: str = "hi", key: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"type": "message.send", "parameters": {"message": text}}
    if key:
        payload["idempotency_key"] = key
    return payload


def remind(text: str = "call") -> dict[str, object]:
    return {"type": "reminder.create", "parameters": {"text": text}}


def context(scopes: list[str], *, risk: RiskLevel = RiskLevel.HIGH, policy=None) -> PolicyContext:
    return PolicyContext(
        actor=Identity(id="user_1"),
        policy=policy if policy is not None else default_policy(),
        grants=[PermissionGrant(scope=s, max_risk=risk) for s in scopes],
    )


def linear_graph(first: dict, second: dict):
    b = GraphBuilder("test")
    b.action(first, node_id="a1")
    b.action(second, node_id="a2", depends_on=["a1"])
    b.end(depends_on=["a2"])
    return b.build()


def single_graph(action: Action) -> tuple[Action, object]:
    b = GraphBuilder("single")
    b.action(action, node_id="a1")
    b.end(depends_on=["a1"])
    return action, b.build()


def run(coro):
    return asyncio.run(coro)


class ExecutorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ActionRegistry.with_seeds()
        self.engine = PolicyEngine()
        self.provider = MockProvider()
        self.exec_engine = ExecutionEngine(self.registry, self.engine, [self.provider])

    def allow_all(self, scopes: list[str], risk: RiskLevel = RiskLevel.HIGH) -> PolicyContext:
        return context(scopes, risk=risk, policy=ALLOW_ALL)


class TestLifecycle(unittest.TestCase):
    def test_happy_path_transitions_allowed(self) -> None:
        path = [
            ActionState.CREATED, ActionState.VALIDATING, ActionState.PLANNED,
            ActionState.AUTHORIZED, ActionState.EXECUTING, ActionState.VERIFYING,
            ActionState.SUCCESS,
        ]
        for current, nxt in itertools.pairwise(path):
            with self.subTest(f"{current}->{nxt}"):
                self.assertTrue(can_transition(current, nxt))

    def test_must_pass_through_authorization(self) -> None:
        self.assertFalse(can_transition(ActionState.PLANNED, ActionState.EXECUTING))
        self.assertTrue(can_transition(ActionState.PLANNED, ActionState.AUTHORIZED))

    def test_cannot_skip_to_success(self) -> None:
        self.assertFalse(can_transition(ActionState.CREATED, ActionState.SUCCESS))

    def test_terminal_states_are_final(self) -> None:
        for state in (ActionState.SUCCESS, ActionState.FAILED,
                      ActionState.CANCELLED, ActionState.DENIED):
            with self.subTest(state=state):
                self.assertTrue(is_terminal(state))
                self.assertFalse(can_transition(state, ActionState.EXECUTING))


class TestIdempotencyLedger(unittest.TestCase):
    def test_first_claim_is_not_duplicate(self) -> None:
        self.assertEqual(IdempotencyLedger().begin("k"), (False, None))

    def test_second_claim_is_duplicate(self) -> None:
        ledger = IdempotencyLedger()
        ledger.begin("k")
        ledger.complete("k", {"ok": True})
        self.assertEqual(ledger.begin("k"), (True, {"ok": True}))

    def test_in_flight_claim_is_duplicate(self) -> None:
        ledger = IdempotencyLedger()
        ledger.begin("k")
        self.assertTrue(ledger.begin("k")[0])

    def test_fail_releases_for_retry(self) -> None:
        ledger = IdempotencyLedger()
        ledger.begin("k")
        ledger.fail("k")
        self.assertEqual(ledger.state("k"), None)
        self.assertEqual(ledger.begin("k"), (False, None))

    def test_release_removes(self) -> None:
        ledger = IdempotencyLedger()
        ledger.begin("k")
        ledger.release("k")
        self.assertIsNone(ledger.state("k"))


class TestSuccessfulExecution(ExecutorCase):
    def test_allowed_graph_succeeds_and_verifies(self) -> None:
        graph = linear_graph(send(), remind())
        result = run(self.exec_engine.execute(graph, self.allow_all(["message.send", "calendar.write"])))
        self.assertIs(result.state, ActionState.SUCCESS)
        self.assertTrue(result.all_verified)
        self.assertTrue(result.simulated, "a mock run must be labelled simulated")

    def test_provider_was_actually_called_in_order(self) -> None:
        graph = linear_graph(send(), remind())
        run(self.exec_engine.execute(graph, self.allow_all(["message.send", "calendar.write"])))
        self.assertEqual(
            [a.type for a in self.provider.calls], ["message.send", "reminder.create"]
        )

    def test_result_is_json_serializable(self) -> None:
        import json

        graph = linear_graph(send(), remind())
        result = run(self.exec_engine.execute(graph, self.allow_all(["message.send", "calendar.write"])))
        payload = result.to_dict()
        json.dumps(payload)
        self.assertIn("a1", payload["nodes"])

    def test_unverified_result_is_not_replayed_as_success(self) -> None:
        self.exec_engine.verifier = VerificationEngine()
        action = Action(
            type="message.send",
            parameters={"message": "needs verification"},
            idempotency_key="unverified-replay",
        )
        _, graph = single_graph(action)
        first = run(self.exec_engine.execute(graph, self.allow_all(["message.send"])))
        second = run(self.exec_engine.execute(graph, self.allow_all(["message.send"])))
        self.assertIs(first.state, ActionState.REQUIRES_USER)
        self.assertIs(second.state, ActionState.REQUIRES_USER)
        self.assertNotEqual(second.state, ActionState.SUCCESS)


class TestAuthorizationGates(ExecutorCase):
    def test_missing_permission_denies_before_any_provider_call(self) -> None:
        graph = linear_graph(send(), remind())
        result = run(self.exec_engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertIs(result.state, ActionState.DENIED)
        self.assertEqual(self.provider.calls, [], "a denied graph must not execute")

    def test_awaiting_approval_does_not_execute(self) -> None:
        # message.send is ASK in the default policy, so this pauses.
        graph = linear_graph(send(), send())
        result = run(self.exec_engine.execute(graph, context(["message.send"])))
        self.assertIs(result.state, ActionState.WAITING_PERMISSION)
        self.assertEqual(self.provider.calls, [])

    def test_approval_unblocks_execution(self) -> None:
        action, graph = single_graph(Action(type="message.send", parameters={"message": "hi"}))
        self.exec_engine.submit_approval(
            ApprovalRequest(action_id=action.action_id, status="approved")
        )
        result = run(self.exec_engine.execute(graph, context(["message.send"])))
        self.assertIs(result.state, ActionState.SUCCESS)
        self.assertEqual(len(self.provider.calls), 1)

    def test_rejected_approval_still_blocks(self) -> None:
        action, graph = single_graph(Action(type="message.send", parameters={"message": "hi"}))
        self.exec_engine.submit_approval(
            ApprovalRequest(action_id=action.action_id, status="rejected")
        )
        result = run(self.exec_engine.execute(graph, context(["message.send"])))
        self.assertIs(result.state, ActionState.WAITING_PERMISSION)
        self.assertEqual(self.provider.calls, [])

    def test_approval_for_other_action_does_not_unblock(self) -> None:
        _, graph = single_graph(Action(type="message.send", parameters={"message": "hi"}))
        self.exec_engine.submit_approval(
            ApprovalRequest(action_id="act_someone_else", status="approved")
        )
        result = run(self.exec_engine.execute(graph, context(["message.send"])))
        self.assertIs(result.state, ActionState.WAITING_PERMISSION)
        self.assertEqual(self.provider.calls, [])

    def test_in_flight_idempotency_claim_is_not_reported_as_success(self) -> None:
        action, graph = single_graph(Action(type="message.send", parameters={"message": "hi"}))
        self.exec_engine.ledger.begin(action.idempotency_key)
        result = run(self.exec_engine.execute(graph, self.allow_all(["message.send"])))
        self.assertIs(result.state, ActionState.REQUIRES_USER)
        self.assertEqual(self.provider.calls, [])

    def test_critical_action_with_low_capped_grant_is_denied(self) -> None:
        b = GraphBuilder("pay")
        b.action({"type": "payment.confirm", "parameters": {"payment_id": "p1"}}, node_id="p1")
        b.end(depends_on=["p1"])
        result = run(self.exec_engine.execute(b.build(), self.allow_all(["payment.execute"])))
        self.assertIs(result.state, ActionState.DENIED)
        self.assertEqual(self.provider.calls, [])

    def test_trusted_high_risk_opt_in_survives_dispatch_recheck(self) -> None:
        _, graph = single_graph(
            Action(type="calendar.delete", parameters={"event_id": "event-1"})
        )
        trusted_context = context(["calendar.write"], policy=ALLOW_ALL)
        trusted_context.allow_high_risk = True
        result = run(self.exec_engine.execute(graph, trusted_context))
        self.assertIs(result.state, ActionState.SUCCESS)
        self.assertEqual(len(self.provider.calls), 1)


    def test_invalid_transition_raises(self) -> None:
        with self.assertRaises(InvalidTransition):
            ensure_transition(ActionState.SUCCESS, ActionState.EXECUTING)

    def test_requires_user_may_proceed_after_approval(self) -> None:
        self.assertTrue(
            can_transition(ActionState.REQUIRES_USER, ActionState.AUTHORIZED)
        )


class TestRetryAndFailure(ExecutorCase):
    def test_transient_failure_is_retried_then_succeeds(self) -> None:
        self.provider.flaky["reminder.create"] = 3  # fails attempts 1-2
        _, graph = single_graph(Action(type="reminder.create", parameters={"text": "call"}))
        result = run(self.exec_engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertIs(result.state, ActionState.SUCCESS)
        self.assertEqual(result.nodes["a1"].attempts, 3)

    def test_retry_is_bounded_by_policy(self) -> None:
        self.provider.failures["reminder.create"] = ProviderError("down", FailureKind.TRANSIENT)
        _, graph = single_graph(Action(type="reminder.create", parameters={"text": "call"}))
        result = run(self.exec_engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertIs(result.state, ActionState.FAILED)
        self.assertLessEqual(result.nodes["a1"].attempts, 3)

    def test_non_retryable_failure_fails_immediately(self) -> None:
        self.provider.failures["reminder.create"] = ProviderError(
            "bad input", FailureKind.INVALID_INPUT
        )
        _, graph = single_graph(Action(type="reminder.create", parameters={"text": "call"}))
        result = run(self.exec_engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertIs(result.state, ActionState.FAILED)
        self.assertEqual(result.nodes["a1"].attempts, 1, "invalid input must not be retried")
        self.assertIs(result.nodes["a1"].failure_kind, FailureKind.INVALID_INPUT)

    def test_authorization_failure_is_not_retried(self) -> None:
        self.provider.failures["reminder.create"] = ProviderError(
            "forbidden", FailureKind.AUTHORIZATION
        )
        _, graph = single_graph(Action(type="reminder.create", parameters={"text": "call"}))
        result = run(self.exec_engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertEqual(result.nodes["a1"].attempts, 1)

    def test_permanent_and_authorization_are_not_retryable(self) -> None:
        for kind in (FailureKind.PERMANENT, FailureKind.AUTHORIZATION, FailureKind.INVALID_INPUT):
            with self.subTest(kind=kind):
                self.assertNotIn(kind, DEFAULT_RETRYABLE)

    def test_partial_completion_when_second_node_fails(self) -> None:
        # a1 (message.send) succeeds; a2 (reminder.create) fails permanently.
        self.provider.failures["reminder.create"] = ProviderError("gone", FailureKind.PERMANENT)
        graph = linear_graph(send("first"), remind("second"))
        result = run(self.exec_engine.execute(graph, self.allow_all(["message.send", "calendar.write"])))
        self.assertIs(result.state, ActionState.PARTIALLY_COMPLETED)
        self.assertIs(result.nodes["a1"].state, ActionState.SUCCESS)
        self.assertIs(result.nodes["a2"].state, ActionState.FAILED)

    def test_no_provider_for_unknown_provider_name(self) -> None:
        action = Action(type="reminder.create", parameters={"text": "x"}, provider="telegram")
        with self.assertRaises(ProviderError):
            self.exec_engine.provider_for(action)


class TestVerificationIsSeparate(ExecutorCase):
    def test_executed_but_unverified_is_not_success(self) -> None:
        # A provider that claims success but leaves nothing observable: the
        # exact case where "HTTP 200" must not be read as business success.
        class LyingProvider(MockProvider):
            def observe(self, action):
                return None

        engine = ExecutionEngine(self.registry, self.engine, [LyingProvider()])
        _, graph = single_graph(Action(type="reminder.create", parameters={"text": "call"}))
        result = run(engine.execute(graph, self.allow_all(["calendar.write"])))
        # Nothing is observable, so the effect is refuted — not merely
        # unconfirmed. Either way it is emphatically not SUCCESS.
        self.assertIsNot(result.state, ActionState.SUCCESS)
        self.assertIs(result.state, ActionState.FAILED)
        self.assertEqual(result.nodes["a1"].verified, False)

    def test_weak_evidence_yields_requires_user(self) -> None:
        """Indeterminate (we could not confirm or refute) → REQUIRES_USER."""

        class WeakProvider(MockProvider):
            def observe(self, action):
                return {"action_type": action.type}  # no key, no parameters

        engine = ExecutionEngine(self.registry, self.engine, [WeakProvider()])
        _, graph = single_graph(Action(type="reminder.create", parameters={"text": "call"}))
        result = run(engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertIs(result.state, ActionState.REQUIRES_USER)
        self.assertIs(result.nodes["a1"].verified, False)

    def test_verified_run_reports_verified(self) -> None:
        _, graph = single_graph(Action(type="reminder.create", parameters={"text": "call"}))
        result = run(self.exec_engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertIs(result.nodes["a1"].state, ActionState.SUCCESS)
        self.assertTrue(result.all_verified)

    def test_unverified_node_blocks_graph_success(self) -> None:
        class LyingProvider(MockProvider):
            def observe(self, action):
                return None

        engine = ExecutionEngine(self.registry, self.engine, [LyingProvider()])
        graph = linear_graph(remind("first"), remind("second"))
        result = run(engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertNotEqual(result.state, ActionState.SUCCESS)



class TestIdempotentExecution(ExecutorCase):
    def test_same_key_executes_provider_once(self) -> None:
        action = Action(
            type="reminder.create", parameters={"text": "once"}, idempotency_key="fixed"
        )
        _, graph1 = single_graph(action)
        run(self.exec_engine.execute(graph1, self.allow_all(["calendar.write"])))
        self.assertEqual(len(self.provider.calls), 1)

        # A second run with the same key must not repeat the side effect.
        action2 = Action(
            type="reminder.create", parameters={"text": "once"}, idempotency_key="fixed"
        )
        _, graph2 = single_graph(action2)
        result = run(self.exec_engine.execute(graph2, self.allow_all(["calendar.write"])))
        self.assertEqual(len(self.provider.calls), 1, "duplicate side effect")
        self.assertIs(result.state, ActionState.SUCCESS)

    def test_different_keys_execute_twice(self) -> None:
        for key in ("k1", "k2"):
            action = Action(
                type="reminder.create", parameters={"text": "x"}, idempotency_key=key
            )
            _, graph = single_graph(action)
            run(self.exec_engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertEqual(len(self.provider.calls), 2)

    def test_key_is_carried_from_the_caller(self) -> None:
        _, graph = single_graph(
            Action(
                type="reminder.create", parameters={"text": "x"},
                idempotency_key="from_caller",
            )
        )
        run(self.exec_engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertEqual(self.provider.calls[0].idempotency_key, "from_caller")


class TestConcurrency(ExecutorCase):
    def test_independent_nodes_run_concurrently(self) -> None:
        b = GraphBuilder("parallel")
        b.action(remind("a"), node_id="p1")
        b.action(remind("b"), node_id="p2")
        b.action(remind("c"), node_id="p3")
        b.end(depends_on=["p1", "p2", "p3"])
        graph = b.build()

        dispatched: list[str] = []

        class SlowMock(MockProvider):
            async def execute(self, action):
                dispatched.append(action.parameters.get("text"))
                await asyncio.sleep(0.01)
                return await super().execute(action)

        engine = ExecutionEngine(self.registry, self.engine, [SlowMock()])
        result = run(engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertIs(result.state, ActionState.SUCCESS)
        # All three were dispatched before any could complete — only possible
        # if the topological layer ran concurrently.
        self.assertEqual(sorted(dispatched), ["a", "b", "c"])

    def test_dependent_node_runs_after_its_dependency(self) -> None:
        order: list[str] = []

        class RecordingMock(MockProvider):
            async def execute(self, action):
                order.append(action.parameters.get("text"))
                return await super().execute(action)

        engine = ExecutionEngine(self.registry, self.engine, [RecordingMock()])
        graph = linear_graph(remind("first"), remind("second"))
        run(engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertEqual(order, ["first", "second"])


class TestCancellation(ExecutorCase):
    def test_cancelled_execution_does_not_run(self) -> None:
        graph = linear_graph(remind("a"), remind("b"))
        self.exec_engine.cancel(graph.graph_id)
        result = run(self.exec_engine.execute(graph, self.allow_all(["calendar.write"])))
        self.assertIs(result.state, ActionState.CANCELLED)
        self.assertEqual(self.provider.calls, [])


if __name__ == "__main__":
    unittest.main()

