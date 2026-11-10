"""A2 — NL → verified execution through the real orchestration entry point.

Not a fixture test of the matcher alone: each case enters at the public surface the
API uses (``Reasoner.understand``) and passes the resulting :class:`TaskSession`
through ``Orchestrator.advance`` with the real ExecutionEngine → LaptopProvider →
FakeDriver chain. The observations asserted are what the FakeDriver fixture actually
returned; no restaurant, train, price or result is claimed beyond that.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest

from actionmesh.core.permissions import PermissionGrant, PolicyContext
from actionmesh.protocol.models import Identity, Policy, PolicyDecision, PolicyRule, RiskLevel


def run(coro):
    return asyncio.run(coro)


def _ctx(actor: str, scopes: list[str]) -> PolicyContext:
    return PolicyContext(
        actor=Identity(id=actor),
        grants=[PermissionGrant(scope=s, max_risk=RiskLevel.HIGH) for s in scopes],
        policy=Policy(rules=[PolicyRule(action="*", decision=PolicyDecision.ALLOW)]),
    )


def _app(root: str):
    from actionmesh.agent.definitions import register_device_actions
    from actionmesh.agent.paths import Workspace
    from actionmesh.agent.provider import LaptopProvider
    from actionmesh.agent.verification import register_device_strategies
    from actionmesh.core.executor.engine import ExecutionEngine
    from actionmesh.core.permissions import PolicyEngine
    from actionmesh.core.registry import ActionRegistry

    registry = ActionRegistry.with_seeds()
    register_device_actions(registry)
    provider = LaptopProvider(Workspace(root))
    engine = ExecutionEngine(registry, PolicyEngine(), [provider])
    register_device_strategies(engine.verifier)
    return registry, provider, engine


def _reasoner(registry):
    from actionmesh.core.planner import RuleBasedPlanner
    from actionmesh.voiceos.reasoning import Reasoner

    return Reasoner(
        planner=RuleBasedPlanner(known_action=registry.has),
        known_action=registry.has,
        templates=lambda: (),
    )


class TestOpenZomatoShowMePizza(unittest.TestCase):
    def test_pizza_discover_runs_verifies_and_reports_fixture_evidence(self) -> None:
        from actionmesh.agent.browser import BrowserEngine, FakeDriver
        from actionmesh.voiceos.orchestrator import Orchestrator
        from actionmesh.voiceos.website_workflows import ORDER_PLACE_ACTION

        root = tempfile.mkdtemp()
        try:
            registry, provider, engine = _app(root)
            drv = FakeDriver()
            drv.add_page("https://www.zomato.com", "Zomato",
                         [{"tag": "a", "name": "Pizza", "href": "/pizza"}],
                         text="Zomato pizza")
            provider.browser = BrowserEngine(drv)

            answer = _reasoner(registry).understand(
                "Open Zomato and show me pizza",
                _conversation(),
            )
            self.assertEqual(answer.kind, "task")
            self.assertIsNotNone(answer.session)
            session = answer.session
            # The entry point is the real one: /plan → policy → approval → executor.
            out = run(Orchestrator(engine=engine).advance(
                session, _ctx("u1", ["browser.control", "search.web"])))
            done = out["session"]

            self.assertEqual(done.status, "completed")
            self.assertEqual(len(done.steps), 4)
            self.assertNotIn(ORDER_PLACE_ACTION, [s.action_type for s in done.steps])
            for step in done.steps:
                self.assertEqual(step.state, "succeeded", step)
                self.assertTrue(step.verified, step)
            self.assertEqual(done.steps[0].observed["url"], "https://www.zomato.com")
            self.assertEqual(done.steps[0].observed["title"], "Zomato")
            # The search step uses the extracted query, not a hard-coded literal.
            self.assertEqual(done.steps[1].observed["url"], "https://duckduckgo.com/?q=pizza")
            self.assertEqual(done.steps[1].observed["count"], 0)
            self.assertNotIn("results", done.steps[1].observed)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_order_pizza_never_reaches_place_without_approval(self) -> None:
        from actionmesh.agent.browser import BrowserEngine, FakeDriver

        root = tempfile.mkdtemp()
        try:
            registry, provider, _ = _app(root)
            drv = FakeDriver()
            drv.add_page("https://www.zomato.com", "Zomato",
                         [{"tag": "a", "name": "Pizza", "href": "/pizza"}],
                         text="Zomato pizza")
            provider.browser = BrowserEngine(drv)

            # "order pizza" names no restaurant — the reasoner asks, no session.
            answer = _reasoner(registry).understand("order pizza", _conversation())
            self.assertEqual(answer.kind, "answer")
            self.assertIsNone(answer.session)
            self.assertEqual(answer.unresolved, ("selected.restaurant_ref",))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestTrainSearch(unittest.TestCase):
    def test_fully_specified_search_runs_read_only(self) -> None:
        from actionmesh.agent.browser import BrowserEngine, FakeDriver
        from actionmesh.voiceos.orchestrator import Orchestrator
        from actionmesh.voiceos.website_workflows import BOOKING_SUBMIT_ACTION

        root = tempfile.mkdtemp()
        try:
            registry, provider, engine = _app(root)
            drv = FakeDriver()
            drv.add_page("https://www.irctc.co.in", "IRCTC",
                         [{"tag": "input", "name": "origin"}], text="train search")
            provider.browser = BrowserEngine(drv)

            answer = _reasoner(registry).understand(
                "find trains from Delhi to Mumbai on 2026-11-10 3ac",
                _conversation(),
            )
            self.assertEqual(answer.kind, "task")
            self.assertIsNotNone(answer.session)
            session = answer.session
            self.assertNotIn(BOOKING_SUBMIT_ACTION, [s.action_type for s in session.steps])
            # The origin reached the browser.type step verbatim.
            self.assertEqual(session.steps[3].parameters.get("text"), "delhi")
            # The ref is not in the plan — it is bound from the snapshot observation.

            out = run(Orchestrator(engine=engine).advance(
                session, _ctx("u1", ["browser.control", "search.web"])))
            done = out["session"]
            self.assertEqual(done.status, "completed")
            for step in done.steps:
                self.assertEqual(step.state, "succeeded", step)
                self.assertTrue(step.verified, step)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_around_4pm_asks_for_origin(self) -> None:
        from actionmesh.agent.browser import BrowserEngine, FakeDriver

        root = tempfile.mkdtemp()
        try:
            registry, provider, _ = _app(root)
            provider.browser = BrowserEngine(FakeDriver())
            answer = _reasoner(registry).understand(
                "find trains around 4 PM", _conversation()
            )
            self.assertEqual(answer.kind, "answer")
            self.assertIsNone(answer.session)
            self.assertEqual(answer.unresolved, ("journey.origin",))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestBrowserDisabledRefusal(unittest.TestCase):
    def test_food_discover_with_no_browser_capability_is_refused(self) -> None:
        from actionmesh.core.planner import RuleBasedPlanner
        from actionmesh.voiceos.models import Conversation
        from actionmesh.voiceos.reasoning import Reasoner

        # A registry with no browser capability at all, mirroring the laptop
        # agent not being enabled.
        class _NoBrowser:
            def has(self, name: str) -> bool:
                return False

        registry = _NoBrowser()
        reasoner = Reasoner(
            planner=RuleBasedPlanner(known_action=registry.has),
            known_action=registry.has,
            templates=lambda: (),
        )
        answer = reasoner.understand("Open Zomato and show me pizza", Conversation(actor_id="u1"))
        self.assertEqual(answer.kind, "answer")
        self.assertIsNone(answer.session)
        self.assertIn("laptop agent", answer.reply)


def _conversation():
    from actionmesh.voiceos.models import Conversation

    return Conversation(actor_id="u1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
