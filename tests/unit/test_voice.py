"""C34 VoiceOS conversation + task surface — what a sentence may and may not do.

The page grew from "preview a plan" to "run a multi-step task", so the invariants moved
with it. The old one was *the page cannot execute*. The new, harder one is **the page can
execute, but only when a human presses the button that says so, and only one authorised
step at a time** — and that is what is checked here, against the HTML the server serves.

Concretely:

* the request targets are the existing API surface plus `/voiceos/*`, and never the
  consent or pairing surface;
* a message proposes (`ran: false` on the server side, and no execution call in the send
  path here); running is `runTask`, whose only caller is the button;
* editing the transcript invalidates the task that was built from the old words, so a
  hand-fix can never run against a stale plan;
* the stream is opened before the run is asked for, because output is transient;
* approvals are per-action presses, and nothing approves on a timer, on load, or as a
  side effect of running;
* spoken success comes from the authoritative record's verified nodes, and a parked run is
  never reported as an outcome;
* the microphone is still one tap for one utterance, and there is still no audio path.
"""

from __future__ import annotations

import re
import unittest

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.dashboard import service_worker as service_worker_source
from actionmesh.voice import page

#: Everything the voice surface may ask of the server.
ALLOWED_TARGETS = {
    "/health",
    "/capabilities",
    "/devices",
    "/approvals",
    "/events/stream-ticket",
    "/executions/",
    "/voiceos/messages",
    "/voiceos/conversation",
    "/voiceos/sessions",
    "/voiceos/sessions/",
}


def _segment(html: str, start: str, end: str) -> str:
    first = html.index(start)
    return html[first : html.index(end, first)]


class TestVoiceRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(create_app(), client=("127.0.0.1", 45123))

    @classmethod
    def tearDownClass(cls) -> None:
        # A `TestClient` that was never entered as a context manager runs no lifespan,
        # so the stores this app opened during the requests have no shutdown to hang
        # their close on. Same convention as `app.state.audit_backend` in
        # test_audit_api: whoever exercised it releases it, or the suite stops being
        # warning-clean.
        store = getattr(cls.client.app.state, "voiceos_store_instance", None)
        if store is not None:
            store.close()

    def test_it_serves_html_with_the_dashboard_headers(self) -> None:
        response = self.client.get("/voice")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_it_does_not_replace_the_control_room(self) -> None:
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertIn("ActionMesh Control Room", self.client.get("/dashboard").text)

    def test_the_page_links_back_to_the_control_room(self) -> None:
        self.assertIn('href="/dashboard"', page())

    def test_a_message_proposes_and_creates_no_execution(self) -> None:
        before = len(self.client.get("/executions").json())
        answer = self.client.post(
            "/voiceos/messages", json={"text": "what is my laptop running"}
        ).json()
        self.assertTrue(answer["session_id"])
        self.assertFalse(answer["ran"])
        self.assertEqual(len(self.client.get("/executions").json()), before)


class TestTheRequestSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = page()

    def test_every_request_target_is_existing_api(self) -> None:
        literals = set(re.findall(r"(?:get|post)\('([^']*)'", self.html))
        self.assertTrue(literals)
        self.assertLessEqual(
            literals,
            ALLOWED_TARGETS,
            f"unexpected request targets: {sorted(literals - ALLOWED_TARGETS)}",
        )
        self.assertIn("/voiceos/messages", literals)
        self.assertIn("/voiceos/sessions/", literals)

    def test_the_consent_and_pairing_surfaces_are_not_touched(self) -> None:
        # Granting a permission and admitting a device are the control room's acts. A
        # voice layer that could do either would be authorising itself.
        for forbidden in ("/consents", "/pairing", "/pair", "/revoke", "/audit", "/ready"):
            self.assertNotIn(forbidden, self.html)

    def test_no_authority_field_leaves_the_page(self) -> None:
        for forbidden in (
            "permissions:",
            "grants:",
            "risk_level:",
            "authorized",
            "role:",
            "allow_high_risk",
            "max_risk:",
        ):
            self.assertNotIn(forbidden, self.html)

    def test_a_paired_phone_never_names_its_own_actor(self) -> None:
        self.assertIn(
            "const identityBody = () => (getToken() ? {} : {actor: {id: 'voice-user'}});",
            self.html,
        )

    def test_a_stream_is_watched_through_the_same_ticket_the_dashboard_uses(self) -> None:
        self.assertIn("get('/events/stream-ticket')", self.html)
        self.assertIn("new EventSource('/events?ticket='", self.html)


class TestRunningNeedsItsOwnPress(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = page()

    def test_the_run_button_is_the_only_caller(self) -> None:
        self.assertIn("el('runTask').addEventListener('click', runTask)", self.html)
        self.assertEqual(re.findall(r"(?<![A-Za-z])runTask\(", self.html), [])

    def test_the_run_button_starts_disabled(self) -> None:
        segment = _segment(self.html, 'id="runTask"', "</section>")
        self.assertIn("disabled", segment)

    def test_running_is_one_authorised_step_on_the_session(self) -> None:
        segment = _segment(self.html, "const runTask", "el('runTask').addEventListener")
        self.assertIn("post('/voiceos/sessions/' + encodeURIComponent(state.sessionId) + '/run'", segment)
        self.assertIn("if (!state.sessionId) return;", segment)

    def test_the_stream_is_opened_before_the_run_is_asked_for(self) -> None:
        segment = _segment(self.html, "const runTask", "el('runTask').addEventListener")
        self.assertLess(segment.index("await openStream();"), segment.index("post('/voiceos/sessions/'"))

    def test_the_run_is_named_by_the_response_not_by_the_stream(self) -> None:
        # This test used to require the opposite. C35 made the event timeline durable,
        # so a reconnect now replays this actor's retained history: the first execution id
        # on the wire is the *oldest* retained run, not the one just pressed. Driving the
        # page in a browser showed it announcing an hours-old success as the result of the
        # step it had started. The id comes from the response, which is where it is known.
        segment = _segment(self.html, "const bindStream", "const openStream")
        self.assertIn("if (!state.executionId || data.execution_id !== state.executionId) return;", segment)
        runner = _segment(self.html, "const runTask", "el('runTask').addEventListener")
        self.assertIn("state.executionId = latest.execution_id;", runner)

    def test_recognition_never_reaches_execution(self) -> None:
        segment = _segment(self.html, "// -- push to talk --", "</script>")
        self.assertIn("sendMessage(", segment)
        self.assertNotIn("runTask", segment)
        self.assertNotIn("/run", segment)

    def test_an_edited_request_invalidates_the_task_it_came_from(self) -> None:
        segment = _segment(self.html, "const syncTranscript", "// -- conversation --")
        self.assertIn("state.task = null;", segment)
        self.assertIn("el('runTask').disabled = true;", segment)
        sender = _segment(self.html, "const sendMessage", "// -- task plan --")
        self.assertNotIn("runTask()", sender)

    def test_stopping_the_task_is_its_own_press(self) -> None:
        self.assertIn("el('cancelTask').addEventListener('click', cancelTask)", self.html)
        segment = _segment(self.html, "const cancelTask", "el('cancelTask').addEventListener")
        self.assertIn("'/cancel'", segment)


class TestApprovalStaysExplicit(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = page()

    def test_approving_is_a_separate_press_on_the_action_it_names(self) -> None:
        segment = _segment(self.html, "const refreshApprovals", "// -- live stream and timeline --")
        self.assertIn("approve.addEventListener('click'", segment)
        self.assertIn("reject.addEventListener('click'", segment)
        self.assertIn("action_id: action.action_id", segment)
        self.assertIn("action_type: action.action_type", segment)

    def test_nothing_approves_itself(self) -> None:
        lines = [line.strip() for line in self.html.splitlines() if "decideApproval(" in line]
        self.assertEqual(len(lines), 2, f"unexpected approval call sites: {lines}")
        for line in lines:
            self.assertIn("addEventListener('click'", line)

    def test_the_approval_card_is_hidden_until_something_parks(self) -> None:
        self.assertIn('<section class="card" id="approvalCard" hidden>', self.html)


class TestSpeechReportsOnlyWhatTheRecordSays(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = page()

    def test_success_is_spoken_from_the_verified_node_count(self) -> None:
        segment = _segment(self.html, "const finishRun", "// -- cancellation of the live step --")
        self.assertIn("outcome === 'success'", segment)
        self.assertIn(".verified === true", segment)
        self.assertIn("not independently verified", segment)

    def test_the_spoken_result_waits_for_the_record_not_the_event_payload(self) -> None:
        segment = _segment(self.html, "const finishRun", "// -- cancellation of the live step --")
        self.assertIn("await reconcile();", segment)
        self.assertLess(segment.index("await reconcile();"), segment.index("speak(clean"))
        self.assertNotIn("showResult(payload)", segment)

    def test_an_outcome_is_announced_once(self) -> None:
        segment = _segment(self.html, "const finishRun", "// -- cancellation of the live step --")
        self.assertIn("if (state.reported === state.executionId) return;", segment)

    def test_an_unconfirmed_effect_is_spoken_as_unconfirmed(self) -> None:
        self.assertIn("effect could not be confirmed", self.html)
        self.assertIn("An effect may still be standing", self.html)

    def test_command_output_is_never_spoken(self) -> None:
        segment = _segment(self.html, "const appendOutput", "let eventStream")
        self.assertNotIn("speak(", segment)
        self.assertIn("box.textContent = state.output", segment)

    def test_synthesis_sits_behind_one_adapter_seam(self) -> None:
        self.assertIn("const SPEAK_ADAPTERS", self.html)
        self.assertIn("const createSpeaker", self.html)
        self.assertIn("'speechSynthesis' in window", self.html)

    def test_spoken_output_is_interruptible_and_stoppable(self) -> None:
        self.assertIn("window.speechSynthesis.cancel();", self.html)
        self.assertIn("el('stopSpeaking').addEventListener('click'", self.html)
        self.assertIn("el('speakToggle').addEventListener('click'", self.html)

    def test_the_timeline_is_driven_by_events_not_timers(self) -> None:
        segment = _segment(self.html, "const bindStream", "const openStream")
        self.assertIn("on('node.started'", segment)
        self.assertIn("on('execution.completed'", segment)
        self.assertNotIn("setTimeout", segment)
        self.assertNotIn("setInterval", segment)

    def test_a_step_is_only_marked_when_its_evidence_arrives(self) -> None:
        segment = _segment(self.html, "const markStep", "const renderNodes")
        self.assertIn("A step is only ever marked from something that actually happened", segment)

    def test_a_parked_run_is_not_reported_as_an_outcome(self) -> None:
        declaration = _segment(self.html, "const TERMINAL", "const isTerminal")
        self.assertNotIn("waiting_permission", declaration)
        handler = _segment(self.html, "on('execution.completed'", "source.addEventListener('open'")
        self.assertIn("if (!isTerminal(payload.state)) return;", handler)

    def test_the_terminal_set_is_declared_once(self) -> None:
        self.assertEqual(1, self.html.count("const TERMINAL = ["))
        self.assertNotIn("'timeout', 'partially_completed'].includes", self.html)
        self.assertGreaterEqual(self.html.count("isTerminal("), 3)


class TestNoAudioIsCollected(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = page()

    def test_no_audio_capture_or_upload_primitive_appears(self) -> None:
        for forbidden in (
            "MediaRecorder",
            "getUserMedia",
            "mediaDevices",
            "FormData",
            "application/octet-stream",
            "base64",
        ):
            self.assertNotIn(forbidden, self.html)

    def test_only_text_reaches_the_server(self) -> None:
        self.assertNotIn("blob:", self.html)
        self.assertNotIn("ArrayBuffer", self.html)

    def test_nothing_is_persisted_beyond_the_tab_credential(self) -> None:
        self.assertNotIn("localStorage", self.html)
        self.assertIn("sessionStorage.getItem(tokenKey)", self.html)

    def test_it_discloses_where_the_audio_actually_goes(self) -> None:
        self.assertIn("your browser's own speech service", self.html)
        self.assertIn("ActionMesh receives text", self.html)


class TestListeningIsUserInitiatedAndBounded(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = page()

    def _talk(self) -> str:
        return self.html[self.html.index("// -- push to talk --") :]

    def test_the_microphone_is_bound_to_a_user_gesture(self) -> None:
        self.assertIn("micButton.addEventListener('click', toggleListening)", self.html)

    def test_listening_starts_only_inside_the_gesture_handler(self) -> None:
        self.assertIn("recognizer.start()", self._talk())
        self.assertNotIn(".start()", self.html[: self.html.index("// -- push to talk --")])

    def test_it_is_never_continuous(self) -> None:
        for forbidden in ("continuous = true", "continuous: true"):
            self.assertNotIn(forbidden, self.html)

    def test_it_never_restarts_itself(self) -> None:
        segment = self._talk()
        self.assertIn("recognizer.onend", segment)
        self.assertNotIn(".start()", segment[segment.index("recognizer.onend") :])

    def test_it_stops_listening_when_the_page_hides(self) -> None:
        self.assertIn("visibilitychange", self.html)

    def test_a_second_tap_stops_rather_than_disables(self) -> None:
        segment = _segment(self.html, "const toggleListening", "const finishListening")
        self.assertIn("if (state.listening) { stopListening(); return; }", segment)
        self.assertNotIn("micButton.disabled", segment)


class TestItWorksWithoutSpeech(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = page()

    def test_both_vendor_prefixes_are_tried(self) -> None:
        self.assertIn("window.SpeechRecognition", self.html)
        self.assertIn("window.webkitSpeechRecognition", self.html)

    def test_the_adapter_is_chosen_through_one_seam(self) -> None:
        self.assertIn("const createRecognizer", self.html)
        self.assertIn("SPEECH_ADAPTERS", self.html)

    def test_an_unavailable_microphone_is_explained_not_thrown(self) -> None:
        self.assertIn("Voice recognition is not available on this device", self.html)
        self.assertNotIn("throw new Error('no speech", self.html)

    def test_synthesis_absence_is_labelled_not_assumed(self) -> None:
        self.assertIn("Voice feedback: unavailable here", self.html)

    def test_the_text_path_is_always_available(self) -> None:
        segment = self.html[self.html.index('<textarea id="transcript"') : self.html.index('id="messageStatus"')]
        self.assertIn('id="sendMessage"', segment)
        self.assertNotIn("disabled", segment)

    def test_the_recognizer_cannot_overwrite_an_edited_transcript(self) -> None:
        self.assertIn("transcriptDirty", self.html)


class TestEverythingIsRenderedAsText(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = page()

    def test_no_innerhtml_anywhere(self) -> None:
        self.assertNotIn("innerHTML", self.html)

    def test_task_rows_are_built_with_text_content(self) -> None:
        segment = _segment(self.html, "const renderTask", "const loadTask")
        self.assertIn("line.textContent", segment)
        self.assertIn("document.createElement", segment)

    def test_server_text_lands_as_text(self) -> None:
        for start, end in (
            ("const appendOutput", "let eventStream"),
            ("const refreshApprovals", "const decideApproval"),
            ("const renderNodes", "const appendOutput"),
            ("const renderConversation", "const renderNotes"),
        ):
            segment = _segment(self.html, start, end)
            self.assertNotIn("insertAdjacentHTML", segment)
            self.assertIn("textContent", segment)

    def test_a_refusal_is_visible_before_anything_is_spoken(self) -> None:
        segment = _segment(self.html, "const runTask", "el('runTask').addEventListener")
        self.assertIn("setMessage(error.message, true)", segment)

    def test_the_page_is_a_document_not_a_fragment(self) -> None:
        self.assertTrue(self.html.lstrip().startswith("<!doctype html>"))
        self.assertIn('<meta name="viewport"', self.html)


class TestThePageComesBack(unittest.TestCase):
    """C35: a reload must recover the truth, and must not act on it.

    The phone closing is now an expected event, not an error state. What the page is
    allowed to do about it is read, subscribe and say what the record says -- because the
    one thing a reconnecting client must never do is run a step nobody pressed.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = page()

    def test_resuming_reads_and_never_runs(self) -> None:
        resuming = _segment(self.html, "const reattach = async () => {", "const resumeSession")
        self.assertNotIn("/run", resuming, "a reconnect ran a step on its own")
        self.assertNotIn("post(", resuming, "a reconnect wrote to the server")
        boot = _segment(self.html, "const resumeSession = async () => {", "// -- execution")
        self.assertNotIn("/run", boot)
        self.assertNotIn("post(", boot)

    def test_an_interrupted_run_is_terminal_and_says_what_it_was(self) -> None:
        self.assertIn("'interrupted']", self.html, "interrupted was not added to the terminal set")
        self.assertIn("Reconnecting to execution", self.html)
        self.assertIn("Execution status recovered from the authoritative record", self.html)
        self.assertIn("Reattached to the active execution", self.html)
        self.assertIn("Interrupted by a restart", self.html)
        self.assertIn("checking whether the action has stopped", self.html.lower())
        self.assertIn("The dispatched effect is unconfirmed", self.html)

    def test_the_recovery_path_offers_no_success_it_did_not_witness(self) -> None:
        # The green class is only ever given to a `success` with verified nodes; a
        # reconnect that ends in `interrupted` must not be able to reach it.
        self.assertIn(
            "line.className = result.state === 'success' && verified.length"
            " ? 'result-line verified'",
            self.html,
        )
        self.assertIn("!STEP_SETTLED.includes(step.state)", self.html)


class TestAReconnectDoesNotStealTheRun(unittest.TestCase):
    """C35 made the timeline durable, which is exactly when a replay can mislead."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = page()

    def test_the_page_only_acts_on_the_execution_it_is_watching(self) -> None:
        segment = _segment(self.html, "const bindStream = source => {", "const openStream")
        self.assertIn("data.execution_id !== state.executionId", segment)
        # The old rule was "the first execution id to arrive is mine". With a retained
        # timeline, the first one to arrive after a reconnect is a run that finished
        # before this tab existed -- measured in a real browser, where the page then
        # announced an hours-old success as the result of the step it had just pressed.
        self.assertNotIn("pendingRun", segment)

    def test_nothing_names_an_execution_from_a_timer_or_a_page_load(self) -> None:
        self.assertNotIn("setInterval", _segment(self.html, "const bindStream", "const openStream"))
        self.assertIn("state.executionId = last.execution_id", self.html)


class TestTheOfflineShell(unittest.TestCase):
    def test_the_voice_surface_is_precached_with_the_dashboard(self) -> None:
        source = service_worker_source()
        self.assertIn("'/dashboard'", source)
        self.assertIn("'/voice'", source)

    def test_the_shell_still_caches_no_api_response(self) -> None:
        declaration = service_worker_source().split("const SHELL")[1].split("];")[0]
        self.assertEqual(
            ["/dashboard", "/voice", "/icon.svg"],
            re.findall(r"'(/[a-z./-]*)'", declaration),
        )


if __name__ == "__main__":
    unittest.main()
