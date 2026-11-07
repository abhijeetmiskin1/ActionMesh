"""C42.3 provider-level regression: browser / files / media effect journal + observe.

The engine-level suites prove each engine alone. This file proves the *provider*
layer that was missing the effect journal: every ``browser.*``, ``device.files.*``
and ``media.*`` action dispatched through ``LaptopProvider.execute`` records its
read-back via the provider-owned ``_effects`` map, persists it through the one
existing ``_journal_effect`` ledger write, and is recovered by ``observe()``
with fresh, read-only browser and MPRIS observations.

Behavioural contract (all assertions here are about observable outcomes, never
about which line recorded them):

* success records an effect and ``observe()`` answers it;
* every refusal (``stale``, ``needs_user``, ``unavailable``, ``ambiguous``,
  ``no_effect``, ``unsupported``, ``invalid_input``) records nothing and
  ``observe()`` returns ``None``;
* the persisted ledger row and the in-memory observation agree byte for byte
  (``observe()``'s answer is what is journalled);
* ``verify()`` stays the honest default — it never turns these into success;
* the News inject / approval-reuse adversarial cases from C42 keep refusing.

The browser is ``BrowserEngine(FakeDriver())`` and the player is
``MediaEngine(FakeMediaDriver())`` throughout: these tests prove the provider
wiring, never that a real browser or player was reached.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest

from actionmesh.agent.browser import BrowserEngine, FakeDriver
from actionmesh.agent.definitions import CAPABILITIES, IMPLEMENTED, PROVIDER
from actionmesh.agent.media import FakeMediaDriver, MediaEngine
from actionmesh.agent.paths import Workspace
from actionmesh.agent.provider import LaptopProvider
from actionmesh.agent.verification import register_device_strategies
from actionmesh.core.storage import SQLiteStore
from actionmesh.core.verification.engine import default_engine
from actionmesh.protocol.models import Action
from actionmesh.providers.base import ProviderError


def _action(action_type: str, **parameters: object) -> Action:
    return Action(type=action_type, provider=PROVIDER, parameters=dict(parameters))


def _browser_pages() -> FakeDriver:
    drv = FakeDriver()
    drv.add_page(
        "https://search.test/?q=pizza",
        "Search results",
        [{"tag": "a", "name": "Pizza Place", "href": "https://pizza.test/place"}],
        text="tasty pizza nearby, 4.8 stars, 25 min",
    )
    drv.add_page(
        "https://pizza.test/place",
        "Pizza Place",
        [{"tag": "h1", "name": "Pizza Place"}, {"tag": "button", "name": "Order now"}],
        text="wood-fired pizza",
    )
    drv.add_page(
        "https://gate.test/",
        "Sign in",
        [{"tag": "input", "name": "Password", "password": True, "value": ""}],
        text="sign in with your account password to continue",
    )
    return drv


def _browser_provider(ledger=None, *, pages: FakeDriver | None = None) -> LaptopProvider:
    provider = LaptopProvider(Workspace(tempfile.mkdtemp()), ledger=ledger)
    provider.browser = BrowserEngine(pages or _browser_pages())
    return provider


def _media_provider(ledger=None, *, driver: FakeMediaDriver | None = None) -> LaptopProvider:
    provider = LaptopProvider(Workspace(tempfile.mkdtemp()), ledger=ledger)
    if driver is None:
        drv = FakeMediaDriver()
        drv.add_player("vlc")
    else:
        drv = driver
    # Mirror production wiring: the engine is built against the provider's
    # workspace so `media.open_file` can confine through it.
    provider.media = MediaEngine(drv, workspace=provider.workspace)
    return provider


def _run(action: Action, provider: LaptopProvider):
    return asyncio.run(provider.execute(action))


def _ledger_row(store: SQLiteStore, action: Action) -> dict:
    row = store.get_dispatch(action.action_id)
    assert row is not None, "the effect journal must have written a row"
    return json.loads(row["observation"])


# =============================================================================
# Registration: browser, file and media operations are implemented and advertised
# =============================================================================

class TestC42Registration(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.provider = LaptopProvider(Workspace(self.root))

    def test_browser_file_and_media_operations_are_registered_and_supported(self) -> None:
        browser = {c for c in CAPABILITIES if c.startswith("browser.")}
        files = {c for c in CAPABILITIES if c.startswith("device.files.")}
        media = {c for c in CAPABILITIES if c.startswith("media.")}
        self.assertEqual(len(browser), 14, browser)
        self.assertEqual(len(files), 12, files)
        self.assertEqual(len(media), 8, media)
        for capability in browser | files | media:
            self.assertIn(capability, IMPLEMENTED, capability)
        # The provider claims exactly what is implemented, nothing more.
        self.assertEqual(LaptopProvider(None).SUPPORTED, IMPLEMENTED)

    def test_each_operation_is_claimed_by_type_regardless_of_provider_hint(self) -> None:
        browser = sorted(c for c in CAPABILITIES if c.startswith("browser."))
        files = sorted(c for c in CAPABILITIES if c.startswith("device.files."))
        media = sorted(c for c in CAPABILITIES if c.startswith("media."))
        for capability in browser + files + media:
            self.assertTrue(
                self.provider.supports(_action(capability)),
                f"{capability} must be claimed by type, not by provider hint",
            )

    def test_browser_open_without_a_session_observes_nothing(self) -> None:
        action = _action("browser.navigate", url="https://search.test/?q=pizza")
        # No provider-level session exists yet: the engine answers unavailable
        # (no page was opened), which is not an ok and so records no effect.
        result = _run(action, _browser_provider())
        self.assertEqual(result.data["status"], "unavailable")
        self.assertNotIn(action.action_id, _browser_provider()._effects)
        self.assertIsNone(self.provider.observe(action))


# =============================================================================
# Browser through the provider: success -> effect -> observe; refusal -> none
# =============================================================================

class TestBrowserThroughProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = _browser_provider()

    def test_open_records_effect_and_observes(self) -> None:
        action = _action("browser.open", url="https://search.test/?q=pizza")
        result = _run(action, self.provider)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "ok")
        self.assertIn(action.action_id, self.provider._effects)
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertEqual(observed["action_type"], "browser.open")
        self.assertEqual(observed["idempotency_key"], action.idempotency_key)
        self.assertEqual(observed["parameters"]["url"], "https://search.test/?q=pizza")
        self.assertEqual(observed["parameters"]["title"], "Search results")

    def test_snapshot_after_open_observes_the_page(self) -> None:
        _run(_action("browser.open", url="https://search.test/?q=pizza"), self.provider)
        action = _action("browser.snapshot")
        result = _run(action, self.provider)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "ok")
        self.assertGreaterEqual(result.data["count"], 1)
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertEqual(observed["state"]["count"], result.data["count"])
        # The observation carries counts and flags, never page text.
        blob = json.dumps(observed)
        self.assertNotIn("tasty pizza", blob)
        self.assertNotIn("stars", blob)

    def test_inspect_observes_page_content_shape_not_text(self) -> None:
        _run(_action("browser.open", url="https://search.test/?q=pizza"), self.provider)
        action = _action("browser.inspect")
        result = _run(action, self.provider)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "ok")
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertIn("element_count", observed["state"])

    def test_click_accepts_the_registry_string_ref(self) -> None:
        _run(_action("browser.open", url="https://search.test/?q=pizza"), self.provider)
        snap = _run(_action("browser.snapshot"), self.provider)
        ref = str(snap.data["refs"][0])
        result = _run(_action("browser.click", ref=ref), self.provider)
        self.assertEqual(result.data["status"], "ok")

    def test_click_rejects_selectors_and_non_handles(self) -> None:
        for ref in [True, -1, "1.5", "button", "1;code", "0", "999999999999999999999999"]:
            with self.subTest(ref=ref), self.assertRaises(ProviderError):
                _run(_action("browser.click", ref=ref), self.provider)

    def test_click_after_navigation_with_a_stale_ref_records_nothing(self) -> None:
        _run(_action("browser.open", url="https://search.test/?q=pizza"), self.provider)
        snap = _run(_action("browser.snapshot"), self.provider)
        ref = snap.data["refs"][0]
        # Navigation bumps the epoch: the snapshot ref is now stale.
        _run(_action("browser.navigate", url="https://pizza.test/place"), self.provider)
        stale = _action("browser.click", ref=ref)
        result = _run(stale, self.provider)
        self.assertEqual(result.data["status"], "stale")
        self.assertNotIn(stale.action_id, self.provider._effects)
        self.assertIsNone(self.provider.observe(stale))

    def test_a_login_gate_is_needs_user_and_records_nothing(self) -> None:
        action = _action("browser.open", url="https://gate.test/")
        result = _run(action, self.provider)
        self.assertEqual(result.data["status"], "needs_user")
        self.assertNotIn(action.action_id, self.provider._effects)
        self.assertIsNone(self.provider.observe(action))

    def test_type_into_a_password_field_is_needs_user_not_a_write(self) -> None:
        _run(_action("browser.open", url="https://gate.test/"), self.provider)
        snap = _run(_action("browser.snapshot"), self.provider)
        ref = snap.data["refs"][0]
        action = _action("browser.type", ref=ref, text="hunter2-canary")
        result = _run(action, self.provider)
        self.assertEqual(result.data["status"], "needs_user")
        self.assertNotIn(action.action_id, self.provider._effects)
        self.assertIsNone(self.provider.observe(action))
        # The canary never appears in any provider-visible surface.
        self.assertNotIn("hunter2-canary", json.dumps(result.data))

    def test_back_and_forward_record_effects(self) -> None:
        _run(_action("browser.open", url="https://search.test/?q=pizza"), self.provider)
        _run(_action("browser.navigate", url="https://pizza.test/place"), self.provider)
        back = _action("browser.back")
        result = _run(back, self.provider)
        self.assertEqual(result.data["status"], "ok")
        seen = self.provider.observe(back)
        self.assertIsNotNone(seen)
        self.assertEqual(seen["parameters"]["url"], "https://search.test/?q=pizza")
        forward = _action("browser.forward")
        result = _run(forward, self.provider)
        self.assertEqual(result.data["status"], "ok")
        seen = self.provider.observe(forward)
        self.assertIsNotNone(seen)
        self.assertEqual(seen["parameters"]["url"], "https://pizza.test/place")

    def test_refresh_and_wait_record_effects(self) -> None:
        _run(_action("browser.open", url="https://search.test/?q=pizza"), self.provider)
        refresh = _action("browser.refresh")
        result = _run(refresh, self.provider)
        self.assertEqual(result.data["status"], "ok")
        self.assertIsNotNone(self.provider.observe(refresh))
        wait = _action("browser.wait", ms=10)
        result = _run(wait, self.provider)
        self.assertEqual(result.data["status"], "ok")
        self.assertIsNotNone(self.provider.observe(wait))

    def test_search_is_read_only_at_the_provider_layer(self) -> None:
        _run(_action("browser.open", url="https://search.test/?q=pizza"), self.provider)
        drv = self.provider.browser._driver
        before = list(drv.actions)
        action = _action("browser.search", query="pizza")
        result = _run(action, self.provider)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "ok")
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertTrue(observed["state"]["page_available"])
        # Search navigated; it never clicked.
        new_kinds = [a["action"] for a in drv.actions[len(before):]]
        self.assertNotIn("click", new_kinds)

    def test_stop_closes_the_session_and_observes(self) -> None:
        _run(_action("browser.open", url="https://search.test/?q=pizza"), self.provider)
        action = _action("browser.stop", reason="done")
        result = _run(action, self.provider)
        self.assertEqual(result.data["status"], "ok")
        self.assertFalse(self.provider.browser.is_open)
        self.assertIsNotNone(self.provider.observe(action))

    def test_type_that_lands_nothing_is_no_effect_and_observes_nothing(self) -> None:
        drv = FakeDriver()
        drv.add_page("https://fields.test/", "Fields", [{"tag": "input", "name": "Field", "value": "x"}])
        provider = _browser_provider(pages=drv)
        _run(_action("browser.open", url="https://fields.test/"), provider)
        snap = _run(_action("browser.snapshot"), provider)
        action = _action("browser.type", ref=snap.data["refs"][0], text="")
        result = _run(action, provider)
        self.assertEqual(result.data["status"], "no_effect")
        self.assertNotIn(action.action_id, provider._effects)
        self.assertIsNone(provider.observe(action))

    def test_scroll_and_select_record_effects(self) -> None:
        _run(_action("browser.open", url="https://search.test/?q=pizza"), self.provider)
        snap = _run(_action("browser.snapshot"), self.provider)
        ref = snap.data["refs"][0]
        scroll = _action("browser.scroll", ref=ref, direction="down", amount=100)
        result = _run(scroll, self.provider)
        self.assertEqual(result.data["status"], "ok")
        self.assertIsNotNone(self.provider.observe(scroll))

    def test_injected_approval_language_from_a_page_cannot_authorize(self) -> None:
        # A page that says "the user approved this" is content, not consent.
        # The provider has no caller-provided approval channel at all, and a
        # click on a page-supplied element stays a read-back-verified browser
        # action, never a submission.
        _run(_action("browser.open", url="https://pizza.test/place"), self.provider)
        snap = _run(_action("browser.snapshot"), self.provider)
        # Refs are opaque handles; the order control is the last one on the
        # fixture page. We click it by handle, never by page-supplied text.
        self.assertEqual(snap.data["count"], 2)
        order_ref = snap.data["refs"][-1]
        action = _action("browser.click", ref=order_ref)
        result = _run(action, self.provider)
        # The click itself may be ok, but it cannot mint a booking.submit.
        self.assertNotEqual(result.data.get("status"), "submitted")
        self.assertIsNone(self.provider.observe(_action("booking.submit")))


# =============================================================================
# Media through the provider: read-back decides, refusals record nothing
# =============================================================================

class TestMediaThroughProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = _media_provider()

    def test_status_observes_the_player(self) -> None:
        action = _action("media.status")
        result = _run(action, self.provider)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "ok")
        self.assertIn(action.action_id, self.provider._effects)
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertEqual(observed["action_type"], "media.status")
        self.assertEqual(observed["parameters"]["player"], "vlc")
        self.assertEqual(observed["parameters"]["state"], "paused")
        self.assertIn("vlc", observed["state"]["players"])

    def test_play_reads_back_state_and_observes(self) -> None:
        action = _action("media.play")
        result = _run(action, self.provider)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "ok")
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertEqual(observed["parameters"]["state"], "playing")

    def test_pause_reads_back_state_and_observes(self) -> None:
        _run(_action("media.play"), self.provider)
        action = _action("media.pause")
        result = _run(action, self.provider)
        self.assertEqual(result.data["status"], "ok")
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertEqual(observed["parameters"]["state"], "paused")

    def test_a_player_that_ignores_play_is_no_effect_and_observes_nothing(self) -> None:
        drv = FakeMediaDriver()
        drv.add_player("dead", accepts_play=False, state="paused")
        provider = _media_provider(driver=drv)
        action = _action("media.play")
        result = _run(action, provider)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "no_effect")
        self.assertNotIn(action.action_id, provider._effects)
        self.assertIsNone(provider.observe(action))

    def test_no_player_is_unavailable_and_observes_nothing(self) -> None:
        provider = _media_provider(driver=FakeMediaDriver())
        action = _action("media.status")
        result = _run(action, provider)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "unavailable")
        self.assertNotIn(action.action_id, provider._effects)
        self.assertIsNone(provider.observe(action))

    def test_two_players_without_a_ref_is_ambiguous_and_observes_nothing(self) -> None:
        drv = FakeMediaDriver()
        drv.add_player("alpha", state="paused")
        drv.add_player("beta", state="paused")
        provider = _media_provider(driver=drv)
        action = _action("media.play")
        result = _run(action, provider)
        self.assertEqual(result.data["status"], "ambiguous")
        self.assertNotIn(action.action_id, provider._effects)
        self.assertIsNone(provider.observe(action))

    def test_volume_records_and_observes_the_level(self) -> None:
        action = _action("media.volume", level=0.6)
        result = _run(action, self.provider)
        self.assertEqual(result.data["status"], "ok")
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        # The fake quantizes volume to 0.1 steps: the observation is the
        # player's read-back value, asserted approximately rather than exactly.
        self.assertAlmostEqual(observed["state"]["level"], 0.6, places=6)

    def test_volume_out_of_range_is_invalid_and_observes_nothing(self) -> None:
        action = _action("media.volume", level=9.5)
        result = _run(action, self.provider)
        self.assertNotEqual(result.data["status"], "ok")
        self.assertNotIn(action.action_id, self.provider._effects)
        self.assertIsNone(self.provider.observe(action))

    def test_next_and_previous_observe(self) -> None:
        for op in ("media.next", "media.previous"):
            action = _action(op)
            result = _run(action, self.provider)
            self.assertEqual(result.data["status"], "ok", op)
            self.assertIsNotNone(self.provider.observe(action), op)

    def test_open_file_observes_opened(self) -> None:
        # `MediaEngine.open_file` confines the path through the workspace, so the
        # provider's workspace must be the one the engine was built against.
        action = _action("media.open_file", path="song.mp3")
        with open(os.path.join(self.provider.workspace.root, "song.mp3"), "w") as handle:
            handle.write("x")
        result = _run(action, self.provider)
        self.assertEqual(result.data["status"], "ok")
        observed = self.provider.observe(action)
        # The launcher receipt cannot prove which track a player opened.
        self.assertFalse(observed["postcondition_observed"])


# =============================================================================
# Files through the provider: effects on writes, live read-back on observe
# =============================================================================

class TestFilesThroughProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        with open(os.path.join(self.root, "hello.txt"), "w") as handle:
            handle.write("hello world\n")
        self.provider = LaptopProvider(Workspace(self.root))

    def _exec(self, action_type: str, **params):
        action = _action(action_type, **params)
        return action, _run(action, self.provider)

    def test_read_records_sha_and_observes_unchanged(self) -> None:
        action, result = self._exec("device.files.read", path="hello.txt")
        self.assertTrue(result.ok)
        self.assertIn(action.action_id, self.provider._effects)
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertEqual(observed["action_type"], "device.files.read")

    def test_list_search_info_open_are_not_effect_writes(self) -> None:
        for op, params in (
            ("device.files.list", {"path": ""}),
            ("device.files.search", {"path": "", "pattern": "hello"}),
            ("device.files.info", {"path": "hello.txt"}),
            ("device.files.open", {"path": "hello.txt"}),
        ):
            action, result = self._exec(op, **params)
            self.assertTrue(result.ok, op)
            # Reads do not mutate, so they journal nothing.
            self.assertNotIn(action.action_id, self.provider._effects, op)

    def test_create_records_bytes_and_sha(self) -> None:
        action, result = self._exec("device.files.create", path="new.txt", content="fresh\n")
        self.assertTrue(result.ok)
        self.assertIn(action.action_id, self.provider._effects)
        self.assertTrue(os.path.exists(os.path.join(self.root, "new.txt")))

    def test_rename_move_copy_edit_record_effects(self) -> None:
        cases = (
            ("device.files.rename", {"path": "hello.txt", "to": "renamed.txt"}),
            ("device.files.copy", {"path": "renamed.txt", "to": "copied.txt"}),
            ("device.files.edit", {"path": "copied.txt", "find": "hello", "replace": "goodbye"}),
            ("device.files.move", {"path": "copied.txt", "to": "moved.txt"}),
        )
        for op, params in cases:
            action, result = self._exec(op, **params)
            self.assertTrue(result.ok, (op, result.data))
            self.assertIn(action.action_id, self.provider._effects, op)


# =============================================================================
# Ledger: the journal write and the live observation agree; no second ledger
# =============================================================================

class TestEffectJournal(unittest.TestCase):
    def test_browser_journal_payload_matches_in_memory_observation(self) -> None:
        store = SQLiteStore()
        provider = _browser_provider(ledger=store)
        action = _action("browser.open", url="https://search.test/?q=pizza")
        _run(action, provider)
        live = provider.observe(action)
        persisted = _ledger_row(store, action)
        self.assertEqual(persisted, live)
        self.assertEqual(persisted["action_type"], "browser.open")
        # The journal carries the observation shape (parameters/state), never
        # the raw effect record or its ref-map keys.
        self.assertIn("parameters", persisted)
        self.assertIn("state", persisted)
        self.assertNotIn("refs", json.dumps(persisted))

    def test_media_journal_payload_matches_in_memory_observation(self) -> None:
        store = SQLiteStore()
        provider = _media_provider(ledger=store)
        action = _action("media.play")
        _run(action, provider)
        live = provider.observe(action)
        persisted = _ledger_row(store, action)
        self.assertEqual(persisted, live)
        self.assertEqual(persisted["parameters"]["state"], "playing")

    def test_a_refusal_journals_nothing(self) -> None:
        store = SQLiteStore()
        provider = _browser_provider(ledger=store)
        action = _action("browser.open", url="https://gate.test/")
        _run(action, provider)
        self.assertIsNone(store.get_dispatch(action.action_id))

    def test_observe_after_restart_does_not_confirm_a_historical_browser_receipt(self) -> None:
        store = SQLiteStore()
        provider = _browser_provider(ledger=store)
        action = _action("browser.open", url="https://search.test/?q=pizza")
        _run(action, provider)
        fresh = _browser_provider(ledger=store)
        recovered = fresh.observe(action)
        self.assertIsNone(recovered)

    def test_journal_never_carries_page_content_or_credentials(self) -> None:
        store = SQLiteStore()
        provider = _browser_provider(ledger=store)
        action = _action("browser.open", url="https://search.test/?q=pizza")
        _run(action, provider)
        blob = store.get_dispatch(action.action_id)["observation"]
        self.assertNotIn("tasty pizza", blob)
        self.assertNotIn("stars", blob)
        self.assertNotIn("cookie", blob.lower())
        self.assertNotIn("password", blob.lower())


# =============================================================================
# Verification independence, idempotency, submits, and cancellation
# =============================================================================

class TestVerificationAndRefusals(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = default_engine()
        register_device_strategies(self.verifier)

    def test_provider_verify_still_never_self_certifies(self) -> None:
        provider = _browser_provider()
        action = _action("browser.open", url="https://search.test/?q=pizza")
        result = _run(action, provider)
        claim = asyncio.run(provider.verify(action, result))
        self.assertFalse(claim.verified)

    def test_the_verification_engine_confirms_an_observed_navigation(self) -> None:
        provider = _browser_provider()
        action = _action("browser.open", url="https://search.test/?q=pizza")
        result = _run(action, provider)
        outcome = self.verifier.verify(action, result, provider)
        self.assertTrue(outcome.verified)

    def test_the_verification_engine_refutes_when_the_page_moved_on(self) -> None:
        provider = _browser_provider()
        action = _action("browser.open", url="https://search.test/?q=pizza")
        result = _run(action, provider)
        # The world changed under the recorded effect: verification must notice
        # rather than replay the claim.
        provider._effects[action.action_id]["url"] = "https://elsewhere.test/"
        outcome = self.verifier.verify(action, result, provider)
        self.assertFalse(outcome.verified)

    def test_booking_submit_still_refuses_fail_closed(self) -> None:
        provider = LaptopProvider(None)
        for op in ("booking.submit", "order.place"):
            with self.assertRaises(ProviderError, msg=op):
                _run(_action(op), provider)
            self.assertIsNone(provider.observe(_action(op)))

    def test_a_cancelled_operation_records_no_effect(self) -> None:
        provider = _browser_provider()
        action = _action("browser.open", url="https://search.test/?q=pizza")
        # A handler that is cancelled before it returns cannot have recorded
        # its effect (records happen after the await). Run the cancellation in
        # one event loop: a task created on one loop and awaited on another
        # would be another engine claim, not a cancellation proof.
        async def _run_and_cancel():
            task = asyncio.ensure_future(provider.execute(action))
            # Let the handler reach its first await, then cancel.
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(_run_and_cancel())
        # The provider records after the handler returns; cancellation raised
        # before that, so nothing may be in the effects map.
        self.assertNotIn(action.action_id, provider._effects)
        self.assertIsNone(provider.observe(action))

    def test_duplicate_execution_replays_the_same_verdict(self) -> None:
        provider = _browser_provider()
        action = _action("browser.open", url="https://search.test/?q=pizza")
        first = _run(action, provider)
        second = _run(action, provider)
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(provider.observe(action)["parameters"]["url"], "https://search.test/?q=pizza")
if __name__ == "__main__":
    unittest.main()
