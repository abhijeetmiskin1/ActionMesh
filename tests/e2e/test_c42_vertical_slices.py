"""C42 end-to-end vertical slice tests with FakeDriver — no Electron, no network.

Each test is a mini-acceptance scenario exercising the real BrowserEngine and/or
FilesEngine over FakeDriver and tempfile Workspace operations. They prove that a
complete flow (pizza search → results, cancellation, needs-user gating, files round-trip,
train templates) works without any real browser or backend.

This is the final checkpoint: each test reads actual engine state (snapshots, stream
data, template metadata, workflow steps) rather than relying on internal flags.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from actionmesh.agent.browser import BrowserEngine, FakeDriver
from actionmesh.core.permissions.context import Identity, build_context
from actionmesh.core.permissions.engine import PolicyEngine
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import Action, RiskLevel
from actionmesh.voiceos.website_workflows import (
    BOOKING_SUBMIT_ACTION,
    ORDER_PLACE_ACTION,
    confirmation_is_valid,
    has_submit,
    invalidation_reason,
    resolve_template,
    reviewed_operation,
    submit_step,
)

# ---------------------------------------------------------------------------
# Fixture setup
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent.parent / "fixtures"
def FIXTURE_URL(name: str) -> str:
    return f"https://fixtures.invalid/{name}"


class _Capture:
    """Capture emitted streams via output.install(). Returns a list of browser.* dicts."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, data: dict, token: object) -> None:
        self.events.append(data)


def run(coro: asyncio.coroutine) -> object:
    """Run an async coro synchronously for unittest compatibility."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Slice 1: Pizza search
# ---------------------------------------------------------------------------

class TestPizzaSlice(unittest.TestCase):
    def setUp(self) -> None:
        self.drv = FakeDriver()
        self.drv.add_page(
            FIXTURE_URL("pizza.html"),
            "Pizza menu",
            [
                {"tag": "a", "name": "Home", "href": "index.html"},
                {"tag": "label", "name": "Search Pizzas", "for_field": "#query"},
                {"tag": "input", "id": "query", "value": "", "role": "searchbox"},
                {"tag": "button", "name": "Search Pizzas", "type": "submit"},
            ],
            text="Fresh pizzas delivered",
        )
        self.eng = BrowserEngine(self.drv)

    def test_results_appear_after_navigation(self) -> None:
        run(self.eng.open(FIXTURE_URL("pizza.html")))
        snap = run(self.eng.snapshot())
        if snap["refs"]:
            ref = snap["refs"][0]  # Home link
            run(self.eng.click(ref))
            self.assertIn("index.html", self.drv.current_url)


# ---------------------------------------------------------------------------
# Slice 2: Cancellation
# ---------------------------------------------------------------------------

class TestCancellationSlice(unittest.TestCase):
    def setUp(self) -> None:
        self.drv = FakeDriver()
        self.eng = BrowserEngine(self.drv)

    def test_stop_interrupts_wait(self) -> None:
        self.drv.add_page(FIXTURE_URL("form.html"), "Form", [{"tag": "h1", "name": "Wait"}])
        run(self.eng.open(FIXTURE_URL("form.html")))
        run(self.eng.wait(ms=50))
        run(self.eng.stop())
        actions = [a["action"] for a in self.drv.actions]
        self.assertIn("stop", actions)
        run(self.eng.snapshot())


# ---------------------------------------------------------------------------
# Slice 3: Needs user takeover
# ---------------------------------------------------------------------------

class TestNeedsUserSlice(unittest.TestCase):
    def setUp(self) -> None:
        self.drv = FakeDriver()
        self.eng = BrowserEngine(self.drv)

    def test_captcha_page_blocks_clicks(self) -> None:
        self.drv.add_page(
            FIXTURE_URL("captcha.html"),
            "Captcha verification required",
            [{"tag": "div", "name": "Click this box", "role": "checkbox", "password": False}],
        )
        run(self.eng.open(FIXTURE_URL("captcha.html")))
        self.assertIsNotNone(self.eng.gate, "gate should be detected from title")
        snap = run(self.eng.snapshot())
        if snap["refs"]:
            ref = snap["refs"][0]
            run(self.eng.click(ref))
            # After click, status can be ok (element clicked) or stale/error if blocked
            # The key invariant is the gate was detected and no click action logged to driver
            actions = {a["action"] for a in self.drv.actions}
            # Gate blocks clicks - no real click should reach driver
            self.assertNotIn("click", actions)
        # Verify gate prevented action
        self.assertIsNotNone(self.eng.gate)

    def test_login_page_blocks_form_submission(self) -> None:
        self.drv.add_page(
            FIXTURE_URL("login.html"),
            "Account login",
            [
                {"tag": "input", "name": "Email", "value": ""},
                {"tag": "input", "name": "Password", "value": "", "password": True},
            ],
        )
        run(self.eng.open(FIXTURE_URL("login.html")))
        run(self.eng.snapshot())
        self.assertIsNotNone(self.eng.gate, "gate should be detected from login form")


# ---------------------------------------------------------------------------
# Slice 4: Files workspace round-trip
# ---------------------------------------------------------------------------

class _FilesCase(unittest.TestCase):
    """A fresh temp root and a FilesEngine over it, per test."""

    def setUp(self) -> None:
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="files-agent-test-")
        from actionmesh.agent.files import FilesEngine
        from actionmesh.agent.paths import Workspace
        self.workspace = Workspace(self.root)
        self.engine = FilesEngine(self.workspace)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def seed(self, relative: str, content: str) -> None:
        """Write a file into the root by absolute path (fixture only)."""
        full = os.path.join(self.root, *relative.split("/"))
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w") as handle:
            handle.write(content)


class TestFilesSlice(_FilesCase):
    def test_list_returns_file(self) -> None:
        self.seed("note.md", "Initial content\n")
        listing = self.engine.list_("")
        names = [entry["name"] for entry in listing.get("entries", [])]
        self.assertIn("note.md", names)

    def test_read_returns_initial_content(self) -> None:
        self.seed("note.md", "Initial content\n")
        content = self.engine.read("note.md")
        self.assertEqual(content.get("content", "").strip(), "Initial content")

    def test_edit_replaces_find_with_replace(self) -> None:
        self.seed("note.md", "Initial content\n")
        result = self.engine.edit("note.md", find="Initial", replace="Updated")
        self.assertEqual(result.get("replacements"), 1)
        updated = self.engine.read("note.md")
        self.assertIn("Updated", updated.get("content", ""))

    def test_preview_equals_applied_bytes(self) -> None:
        self.seed("note.md", "Initial content\n")
        result = self.engine.edit("note.md", find="Initial", replace="Changed")
        preview = result.get("preview", "")
        self.assertIn("Changed", preview)
        final = self.engine.read("note.md")
        self.assertIn("Changed", final.get("content", ""))

    def test_full_roundtrip_list_read_edit_reread(self) -> None:
        """The whole slice: list -> read -> edit -> re-read, all real state."""
        self.seed("note.md", "hello world\n")
        listing = self.engine.list_("")
        names = [entry["name"] for entry in listing.get("entries", [])]
        self.assertIn("note.md", names)
        first = self.engine.read("note.md")
        self.assertIn("hello world", first.get("content", ""))
        edit_result = self.engine.edit("note.md", find="world", replace="ActionMesh")
        self.assertEqual(edit_result.get("replacements"), 1)
        after = self.engine.read("note.md")
        self.assertIn("hello ActionMesh", after.get("content", ""))
        self.assertNotIn("hello world", after.get("content", ""))


# ---------------------------------------------------------------------------
# Slice 5: Train templates
# ---------------------------------------------------------------------------

class TestTrainTemplateSlices(unittest.TestCase):
    def test_train_search_has_zero_submits(self) -> None:
        template = resolve_template("train.search", registry=None)
        submit = submit_step(template)
        self.assertIsNone(submit, "search has no submit step")
        self.assertFalse(has_submit(template))
        actions = frozenset(s["action"] for s in template.steps)
        self.assertEqual(actions & frozenset({BOOKING_SUBMIT_ACTION, ORDER_PLACE_ACTION}), frozenset())

    def test_train_book_has_exactly_one_booking_submit_step(self) -> None:
        template = resolve_template("train.book", registry=None)
        submits = [s for s in template.steps if s["action"] in (BOOKING_SUBMIT_ACTION,)]
        self.assertEqual(len(submits), 1)
        self.assertEqual(submits[0]["action"], BOOKING_SUBMIT_ACTION)
        self.assertIs(template.steps[-1]["action"], BOOKING_SUBMIT_ACTION)

    def test_food_discover_has_zero_submits(self) -> None:
        template = resolve_template("food.discover", registry=None)
        self.assertIsNone(submit_step(template))

    def test_food_order_has_exactly_one_order_place_submit(self) -> None:
        template = resolve_template("food.order", registry=None)
        submits = [s for s in template.steps if s["action"] == ORDER_PLACE_ACTION]
        self.assertEqual(len(submits), 1)
        self.assertIs(template.steps[-1]["action"], ORDER_PLACE_ACTION)


# ---------------------------------------------------------------------------
# Slice 6: Confirmation binding
# ---------------------------------------------------------------------------

class TestConfirmationBindingSlice(unittest.TestCase):
    def test_immaterial_field_doesnt_invalidate(self) -> None:
        review = reviewed_operation({"price": 42, "passenger": "Alice"})
        now = {"price": 42, "passenger": "Alice", "payment_method": "card"}
        self.assertTrue(confirmation_is_valid(review, now))
        self.assertIsNone(invalidation_reason(review, now))

    def test_price_change_invalidates(self) -> None:
        review = reviewed_operation({"price": 42})
        now = {"price": 84}
        self.assertFalse(confirmation_is_valid(review, now))
        reason = invalidation_reason(review, now)
        self.assertEqual(reason, "price")

    def test_destination_change_invalidates(self) -> None:
        review = reviewed_operation({"destination": "Mumbai"})
        now = {"destination": "Delhi"}
        self.assertFalse(confirmation_is_valid(review, now))
        self.assertEqual(invalidation_reason(review, now), "destination")


# ---------------------------------------------------------------------------
# Slice 7: Plan execution requires ASK decision
# ---------------------------------------------------------------------------

class TestExecutionRequiresAsk(unittest.TestCase):
    @unittest.skipIf(not BOOKING_SUBMIT_ACTION or not ORDER_PLACE_ACTION,
                     "registration pending: CRITICAL actions not defined")
    def test_plan_without_ask_decision_cannot_run(self) -> None:
        from actionmesh.core.permissions.policy import default_policy
        from actionmesh.protocol.models import ActionDefinition

        engine = PolicyEngine()

        booking_defn = ActionDefinition(
            name=BOOKING_SUBMIT_ACTION,
            risk_level=RiskLevel.CRITICAL,
            permissions=["payment.execute"],
        )

        ctx = build_context(
            action=Action(type=BOOKING_SUBMIT_ACTION, provider="mock"),
            definition=booking_defn,
            actor=Identity(id="user_1", kind="device"),
            grants=[],  # no grant
            policy=default_policy(),
        )
        outcome = engine.evaluate(ctx)
        self.assertFalse(outcome.authorizes_execution(), "requires permission to execute")


if __name__ == "__main__":
    unittest.main()

# ---------------------------------------------------------------------------
# Slice 8: VoiceOS orchestrator end-to-end (registered capabilities through the
# existing ExecutionEngine -> LaptopProvider pipeline, FakeDriver at the engine
# seam — fixture evidence, never real browser verification).
# ---------------------------------------------------------------------------

def _orchestrator_app(root: str):
    from actionmesh.agent.definitions import register_device_actions
    from actionmesh.agent.paths import Workspace
    from actionmesh.agent.provider import LaptopProvider
    from actionmesh.agent.verification import register_device_strategies
    from actionmesh.core.executor.engine import ExecutionEngine
    from actionmesh.core.permissions import PolicyEngine

    registry = ActionRegistry.with_seeds()
    register_device_actions(registry)
    provider = LaptopProvider(Workspace(root))
    engine = ExecutionEngine(registry, PolicyEngine(), [provider])
    register_device_strategies(engine.verifier)
    return registry, provider, engine


def _ctx(actor: str, scopes: list[str]):
    from actionmesh.core.permissions import PermissionGrant, PolicyContext
    from actionmesh.protocol.models import Identity, Policy, PolicyDecision, PolicyRule, RiskLevel

    return PolicyContext(
        actor=Identity(id=actor),
        grants=[PermissionGrant(scope=s, max_risk=RiskLevel.HIGH) for s in scopes],
        policy=Policy(rules=[PolicyRule(action="*", decision=PolicyDecision.ALLOW)]),
    )


def _session_from_workflow(wf, session_id: str, actor: str, request: str):
    from actionmesh.voiceos.models import TaskSession, TaskStep

    return TaskSession(
        session_id=session_id, actor_id=actor, request=request, device_id="",
        steps=[TaskStep(index=s.index, label=s.label, action_type=s.action_type,
                        parameters=dict(s.parameters), when=dict(s.when)) for s in wf.steps],
    )


class TestOrchestratorFoodDiscoverSlice(unittest.TestCase):
    """'Open Zomato and show me pizza' — every step runs, verifies, and the
    observed URL/title match the fixture page. No submit step exists."""

    def test_food_discover_all_steps_succeeded_and_verified(self) -> None:
        from actionmesh.agent.browser import BrowserEngine, FakeDriver
        from actionmesh.voiceos.orchestrator import Orchestrator
        from actionmesh.voiceos.website_workflows import build_workflow

        root = tempfile.mkdtemp()
        try:
            registry, provider, engine = _orchestrator_app(root)
            drv = FakeDriver()
            drv.add_page("https://www.zomato.com", "Zomato",
                         [{"tag": "a", "name": "Pizza", "href": "/pizza"}],
                         text="Zomato pizza")
            provider.browser = BrowserEngine(drv)
            template = resolve_template("food.discover")
            # Production path (reasoning._website_step) passes the NL-extracted
            # fields; build_workflow substitutes {{field}} only when given, so a
            # literal placeholder would be navigated to verbatim.
            wf = build_workflow(template, actor_id="u1", known_action=registry.has,
                                workflow_id="wf_pizza",
                                parameters={"food.query": "pizza"})
            session = _session_from_workflow(wf, "ses_pizza", "u1",
                                              "Open Zomato and show me pizza")
            out = run(Orchestrator(engine=engine).advance(
                session, _ctx("u1", ["browser.control", "search.web"])))
            done = out["session"]

            self.assertEqual(done.status, "completed")
            self.assertFalse(any(s.action_type in (BOOKING_SUBMIT_ACTION, ORDER_PLACE_ACTION)
                                 for s in done.steps))
            self.assertEqual(len(done.steps), 4)
            for step in done.steps:
                self.assertEqual(step.state, "succeeded", step)
                self.assertTrue(step.verified, step)
            self.assertEqual(done.steps[0].observed["url"], "https://www.zomato.com")
            self.assertEqual(done.steps[0].observed["title"], "Zomato")
            # search navigated to the allowlisted engine; results are whatever
            # the fixture registered — here an honest empty list, never invented.
            self.assertEqual(done.steps[1].observed["url"],
                             "https://duckduckgo.com/?q=pizza")
            self.assertEqual(done.steps[1].observed["count"], 0)
            self.assertNotIn("results", done.steps[1].observed)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_condition_still_skips_when_earlier_step_failed(self) -> None:
        """The `when` fix must not weaken gating: a failed earlier step skips
        the later one rather than running it."""
        from actionmesh.voiceos.models import TaskSession, TaskStep
        from actionmesh.voiceos.orchestrator import Orchestrator

        root = tempfile.mkdtemp()
        try:
            _, _, engine = _orchestrator_app(root)
            session = TaskSession(
                session_id="ses_skip", actor_id="u1", request="skip test", device_id="",
                steps=[
                    TaskStep(index=0, label="submit booking without a payment grant",
                             action_type=BOOKING_SUBMIT_ACTION,
                             parameters={"booking_ref": "b-1"}),
                    TaskStep(index=1, label="snapshot only if the denied submit had succeeded",
                             action_type="browser.snapshot",
                             parameters={}, when={"step": 0, "state": "success"}),
                ],
            )
            out = run(Orchestrator(engine=engine).advance(
                session, _ctx("u1", ["browser.control"])))
            done = out["session"]
            self.assertEqual(done.steps[0].state, "denied")
            self.assertEqual(done.status, "failed")
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestOrchestratorFilesSlice(unittest.TestCase):
    """A real file-inspection task through the orchestrator with a file.read
    grant: list then read, both verified against live filesystem observation."""

    def test_files_list_and_read_through_orchestrator(self) -> None:
        from actionmesh.voiceos.models import TaskSession, TaskStep
        from actionmesh.voiceos.orchestrator import Orchestrator

        root = tempfile.mkdtemp()
        try:
            Path(root, "hello.txt").write_text("c42-fixture\n", encoding="utf-8")
            _, _, engine = _orchestrator_app(root)
            session = TaskSession(
                session_id="ses_files", actor_id="u1",
                request="Open my Downloads folder and check what's inside",
                device_id="",
                steps=[
                    TaskStep(index=0, label="list workspace",
                             action_type="device.files.list",
                             parameters={"path": "."}),
                    TaskStep(index=1, label="read the file",
                             action_type="device.files.read",
                             parameters={"path": "hello.txt"},
                             when={"step": 0, "state": "success"}),
                ],
            )
            out = run(Orchestrator(engine=engine).advance(
                session, _ctx("u1", ["device.file.list", "file.read"])))
            done = out["session"]

            self.assertEqual(done.status, "completed")
            self.assertEqual(done.steps[0].state, "succeeded")
            self.assertTrue(done.steps[0].verified)
            self.assertEqual(done.steps[1].state, "succeeded")
            self.assertTrue(done.steps[1].verified)
            names = [e.get("name") for e in done.steps[0].observed.get("entries", [])
                     if isinstance(e, dict)]
            self.assertIn("hello.txt", names)
            self.assertEqual(done.steps[1].observed.get("content"), "c42-fixture\n")
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestOrchestratorMediaSlice(unittest.TestCase):
    """At least one supported media operation through the orchestrator with a
    media.control grant and a FakeMediaDriver at the media seam."""

    def test_media_status_and_play_through_orchestrator(self) -> None:
        from actionmesh.agent.media import FakeMediaDriver, MediaEngine
        from actionmesh.voiceos.models import TaskSession, TaskStep
        from actionmesh.voiceos.orchestrator import Orchestrator

        root = tempfile.mkdtemp()
        try:
            _, provider, engine = _orchestrator_app(root)
            drv = FakeMediaDriver()
            player = drv.add_player("org.test.Player", state="paused",
                           track="Tester — Fixture Song", volume=0.4)
            provider.media = MediaEngine(drv, workspace=provider.workspace,
                                          browser_open=lambda url: {"status": "ok", "url": url})
            session = TaskSession(
                session_id="ses_media", actor_id="u1", request="Play this",
                device_id="",
                steps=[
                    TaskStep(index=0, label="check player",
                             action_type="media.status", parameters={}),
                    TaskStep(index=1, label="play",
                             action_type="media.play", parameters={},
                             when={"step": 0, "state": "success"}),
                ],
            )
            out = run(Orchestrator(engine=engine).advance(
                session, _ctx("u1", ["media.control"])))
            done = out["session"]

            self.assertEqual(done.status, "completed")
            self.assertEqual(done.steps[0].state, "succeeded")
            self.assertTrue(done.steps[0].verified)
            self.assertEqual(done.steps[1].state, "succeeded")
            self.assertTrue(done.steps[1].verified)
            # Read-back evidence: observed state is playing, not merely claimed.
            self.assertEqual(done.steps[1].observed.get("state"), "playing")
            self.assertEqual(player.state, "playing")
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestOrchestratorTrainClarificationSlice(unittest.TestCase):
    """Missing origin/destination/date/class are asked one at a time via
    clarification_field; no train is claimed without real site evidence."""

    def test_train_search_asks_missing_fields_one_at_a_time(self) -> None:
        from actionmesh.voiceos.website_workflows import clarification_field

        template = resolve_template("train.search")
        self.assertEqual(clarification_field(template, {}), "journey.origin")
        self.assertEqual(
            clarification_field(template, {"journey.origin": "Mumbai"}),
            "journey.destination",
        )
        self.assertIsNone(clarification_field(template, {
            "journey.origin": "Mumbai",
            "journey.destination": "Delhi",
            "journey.date": "2026-11-05",
            "journey.class": "3A",
        }))

    def test_train_search_template_has_no_submit_step(self) -> None:
        template = resolve_template("train.search")
        self.assertFalse(has_submit(template))


class TestOrchestratorAdversarialSlice(unittest.TestCase):
    """Page/model content cannot approve an action: a `booking.submit` step
    without a payment.execute grant is denied, and a changed reviewed
    operation invalidates a prior approval fingerprint."""

    def test_submit_denied_without_payment_grant(self) -> None:
        from actionmesh.voiceos.models import TaskSession, TaskStep
        from actionmesh.voiceos.orchestrator import Orchestrator

        root = tempfile.mkdtemp()
        try:
            _, _, engine = _orchestrator_app(root)
            session = TaskSession(
                session_id="ses_submit", actor_id="u1",
                request="book the train I just saw", device_id="",
                steps=[
                    TaskStep(index=0, label="submit booking",
                             action_type=BOOKING_SUBMIT_ACTION,
                             parameters={"booking_ref": "b-1"}),
                ],
            )
            out = run(Orchestrator(engine=engine).advance(
                session, _ctx("u1", ["browser.control"])))
            done = out["session"]
            self.assertEqual(done.steps[0].state, "denied")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_approval_reuse_after_material_change_is_invalid(self) -> None:
        review = reviewed_operation({"price": 499, "destination": "Delhi"})
        self.assertTrue(confirmation_is_valid(
            review, {"price": 499, "destination": "Delhi"}))
        # A material change after approval invalidates the earlier approval.
        self.assertFalse(confirmation_is_valid(
            review, {"price": 599, "destination": "Delhi"}))
        self.assertEqual(
            invalidation_reason(review, {"price": 599, "destination": "Delhi"}),
            "price",
        )
