"""C16 — Compensation execution.

C6 could *classify* recovery but nothing ever undid a side effect. These tests
pin the two properties that matter:

* A COMPENSATION node must never run as a normal forward action. ``builder.
  compensate()`` sets no ``depends_on``, so Kahn layering used to treat the undo
  node as a root: create+delete both fired and the engine reported ``success``
  for a net no-op.
* When forward execution fails, the compensations of nodes that *actually
  produced a verified side effect* run in reverse order — and a rollback is
  never reported as success.
"""

from __future__ import annotations

import asyncio
import unittest

from actionmesh.core.executor import ExecutionEngine
from actionmesh.core.graph import (
    GraphBuilder,
    NodeConfigError,
    execution_order,
    topological_layers,
)
from actionmesh.core.permissions import (
    PermissionGrant,
    PolicyContext,
    PolicyEngine,
    policy_from_rules,
)
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import (
    ActionState,
    FailureKind,
    Identity,
    RiskLevel,
)
from actionmesh.providers import MockProvider, ProviderError

ALLOW_ALL = policy_from_rules([{"action": "*", "decision": "allow"}])

CAL_SCOPE = "calendar.write"


def cal(title: str = "x") -> dict[str, object]:
    return {"type": "calendar.create", "parameters": {"title": title}}


def undo() -> dict[str, object]:
    return {"type": "calendar.delete", "parameters": {"event_id": "x"}}


def share() -> dict[str, object]:
    return {"type": "file.share", "parameters": {"path": "/a"}}


def context(*scopes: str) -> PolicyContext:
    return PolicyContext(
        actor=Identity(id="u1"),
        policy=ALLOW_ALL,
        grants=[
            PermissionGrant(scope=s, max_risk=RiskLevel.HIGH) for s in scopes
        ],
        allow_high_risk=True,
    )


def build(with_compensation: bool = True):
    b = GraphBuilder("c16")
    b.action(cal(), node_id="a1")
    if with_compensation:
        b.compensate("a1", undo(), node_id="c1")
    b.end(depends_on=["a1"])
    return b.build()


def run(coro):
    return asyncio.run(coro)


def engine_for(fail_types: set[str]) -> tuple[ExecutionEngine, MockProvider]:
    provider = MockProvider()
    for action_type in fail_types:
        provider.failures[action_type] = ProviderError(
            f"injected failure for {action_type}", FailureKind.PERMANENT
        )
    return (
        ExecutionEngine(ActionRegistry.with_seeds(), PolicyEngine(), [provider]),
        provider,
    )


def called(provider: MockProvider) -> list[str]:
    return [c.type for c in provider.calls]


class TestCompensationIsNotForwardWork(unittest.TestCase):
    """The bug: an undo node used to execute as an ordinary action."""

    def setUp(self) -> None:
        self.provider = MockProvider()
        self.engine = ExecutionEngine(
            ActionRegistry.with_seeds(), PolicyEngine(), [self.provider]
        )

    def test_compensation_node_is_not_in_the_forward_schedule(self) -> None:
        layers = topological_layers(build())
        self.assertNotIn("c1", [nid for layer in layers for nid in layer])
        self.assertNotIn("c1", execution_order(build()))

    def test_undo_action_never_fires_during_a_successful_run(self) -> None:
        result = run(self.engine.execute(build(), context(CAL_SCOPE)))
        self.assertIs(result.state, ActionState.SUCCESS)
        # The whole point: only the forward action ran.
        self.assertEqual(called(self.provider), ["calendar.create"])

    def test_a_graph_without_compensation_is_unaffected(self) -> None:
        result = run(
            self.engine.execute(build(with_compensation=False), context(CAL_SCOPE))
        )
        self.assertIs(result.state, ActionState.SUCCESS)
        self.assertEqual(called(self.provider), ["calendar.create"])

    def test_nested_compensation_is_rejected_not_silently_dropped(self) -> None:
        # An undo-of-an-undo has no sensible run order. Accepting it would mean
        # the author believes they declared a rollback and have none.
        b = GraphBuilder("nested")
        b.action(cal(), node_id="a1")
        b.compensate("a1", undo(), node_id="c1")
        b.compensate("c1", undo(), node_id="c2")
        b.end(depends_on=["a1"])
        with self.assertRaises(NodeConfigError) as ctx:
            b.build()
        self.assertIn("nested compensation", str(ctx.exception))


class TestCompensationRunsOnFailure(unittest.TestCase):
    def test_verified_side_effect_is_undone_when_a_later_node_fails(self) -> None:
        # a1 (calendar.create) succeeds; a2 (file.share) fails permanently.
        b = GraphBuilder("c16")
        b.action(cal(), node_id="a1")
        b.compensate("a1", undo(), node_id="c1")
        b.action(share(), node_id="a2", depends_on=["a1"])
        b.end(depends_on=["a2"])
        engine, provider = engine_for({"file.share"})

        result = run(engine.execute(b.build(), context(CAL_SCOPE, "file.share")))
        # The undo ran, and it ran *after* the failure that triggered it. The
        # exact call list also contains the failed `file.share` attempt, which
        # the mock records before injecting the failure.
        calls = called(provider)
        self.assertEqual(calls[0], "calendar.create")
        self.assertEqual(calls[-1], "calendar.delete")
        # The forward run did fail, and a rollback never upgrades that to success.
        self.assertIsNot(result.state, ActionState.SUCCESS)
        self.assertIs(result.state, ActionState.PARTIALLY_COMPLETED)
        # Every undo succeeded, so nothing is left standing.
        self.assertTrue(result.compensations)
        self.assertTrue(all(c.compensated for c in result.compensations))
        self.assertFalse(result.compensation_required)

    def test_nothing_to_undo_when_the_failure_came_first(self) -> None:
        # a1 fails, so it never produced a side effect; there is nothing to undo.
        b = GraphBuilder("c16")
        b.action(cal(), node_id="a1")
        b.compensate("a1", undo(), node_id="c1")
        b.action(share(), node_id="a2", depends_on=["a1"])
        b.end(depends_on=["a2"])
        engine, provider = engine_for({"calendar.create"})

        run(engine.execute(b.build(), context(CAL_SCOPE, "file.share")))
        # a1 never produced an effect, so its undo must not run: undoing work
        # that never happened would be a second, needless side effect.
        self.assertNotIn("calendar.delete", called(provider))

    def test_compensation_order_is_reverse_of_execution(self) -> None:
        b = GraphBuilder("c16")
        b.action(cal("one"), node_id="a1")
        b.action(cal("two"), node_id="a2", depends_on=["a1"])
        b.compensate("a1", undo(), node_id="c1")
        b.compensate("a2", undo(), node_id="c2")
        b.action(share(), node_id="a3", depends_on=["a2"])
        b.end(depends_on=["a3"])
        engine, provider = engine_for({"file.share"})

        run(engine.execute(b.build(), context(CAL_SCOPE, "file.share")))
        # The later effect is undone first.
        self.assertEqual(
            called(provider),
            ["calendar.create", "calendar.create", "file.share",
             "calendar.delete", "calendar.delete"],
        )

    def test_irreversible_action_is_never_compensated(self) -> None:
        # message.send is in IRREVERSIBLE. A declared compensation must still
        # not be run, because the honest answer is "this cannot be undone".
        b = GraphBuilder("c16")
        b.action(
            {"type": "message.send", "parameters": {"message": "hi"}}, node_id="a1"
        )
        b.compensate(
            "a1",
            {"type": "message.send", "parameters": {"message": "unsend"}},
            node_id="c1",
        )
        b.action(share(), node_id="a2", depends_on=["a1"])
        b.end(depends_on=["a2"])
        engine, provider = engine_for({"file.share"})

        run(engine.execute(b.build(), context("message.send", "file.share")))
        self.assertEqual(called(provider), ["message.send", "file.share"])

    def test_a_failed_compensation_is_reported_not_hidden(self) -> None:
        b = GraphBuilder("c16")
        b.action(cal(), node_id="a1")
        b.compensate("a1", undo(), node_id="c1")
        b.action(share(), node_id="a2", depends_on=["a1"])
        b.end(depends_on=["a2"])
        engine, provider = engine_for({"file.share", "calendar.delete"})

        result = run(engine.execute(b.build(), context(CAL_SCOPE, "file.share")))
        # The undo was attempted and failed, so the result must not imply the
        # system is back to a clean state.
        self.assertIn("calendar.delete", called(provider))
        self.assertIsNot(result.state, ActionState.SUCCESS)
        self.assertTrue(result.compensation_required)


class TestUnconfirmedEffectsAreReported(unittest.TestCase):
    """A dispatched-but-unconfirmed action may have really happened."""

    class WeakProvider(MockProvider):
        """Succeeds, but leaves nothing observable → INDETERMINATE."""

        def observe(self, action):
            return {"action_type": action.type}

    def _graph(self):
        b = GraphBuilder("unconfirmed")
        b.action(cal(), node_id="a1")
        b.compensate("a1", undo(), node_id="c1")
        b.action(share(), node_id="a2", depends_on=["a1"])
        b.end(depends_on=["a2"])
        return b.build()

    def test_dispatched_but_unverified_node_is_flagged_not_hidden(self) -> None:
        provider = self.WeakProvider()
        provider.failures["file.share"] = ProviderError(
            "boom", FailureKind.PERMANENT
        )
        engine = ExecutionEngine(
            ActionRegistry.with_seeds(), PolicyEngine(), [provider]
        )
        result = run(
            engine.execute(self._graph(), context(CAL_SCOPE, "file.share"))
        )

        # a1 really was dispatched, so its effect may exist.
        self.assertIn("calendar.create", called(provider))
        self.assertIs(result.nodes["a1"].state, ActionState.REQUIRES_USER)

        # It must NOT be auto-undone: we do not know the effect exists.
        self.assertNotIn("calendar.delete", called(provider))
        # But it must be reported, or the caller reads a clean result.
        self.assertTrue(result.compensation_required)
        unconfirmed = [
            c for c in result.compensations if c.target_node == "a1"
        ]
        self.assertEqual(len(unconfirmed), 1, result.compensations)
        self.assertFalse(unconfirmed[0].compensated)
        self.assertIn("may still exist", unconfirmed[0].reason)

    def test_a_node_that_never_dispatched_is_not_reported(self) -> None:
        # a1 is denied before dispatch, so it cannot have left an effect and
        # must not be reported as a possible live side effect.
        b = GraphBuilder("denied")
        b.action(cal(), node_id="a1")
        b.action(share(), node_id="a2", depends_on=["a1"])
        b.end(depends_on=["a2"])
        provider = self.WeakProvider()
        engine = ExecutionEngine(
            ActionRegistry.with_seeds(), PolicyEngine(), [provider]
        )
        result = run(engine.execute(b.build(), context()))  # no scopes granted
        self.assertIs(result.state, ActionState.DENIED)
        self.assertEqual(called(provider), [])


if __name__ == "__main__":
    unittest.main()
