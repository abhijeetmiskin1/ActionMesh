"""C18 dashboard: planning must stay a proposal, all the way to the UI.

The control room gained a "Plan from natural language" panel. The dangerous
mistake would be to let the browser send its own policy when executing a plan —
that would let a proposal carry its own authority around the server's policy
engine. The tests pin that it does not.
"""

from __future__ import annotations

import re
import unittest

from actionmesh.dashboard import page


class TestPlannerPanel(unittest.TestCase):
    def setUp(self) -> None:
        self.html = page()

    def _execute_plan_handler(self) -> str:
        start = self.html.index("document.querySelector('#executePlan')")
        end = self.html.index("const eventFeed")
        return self.html[start:end]

    def test_it_offers_an_intent_and_a_propose_control(self) -> None:
        self.assertIn('id="intent"', self.html)
        self.assertIn('id="plan"', self.html)
        self.assertIn("fetch('/plan'", self.html)

    def test_executing_a_plan_is_a_separate_disabled_step(self) -> None:
        # Planning and executing must not be one click.
        self.assertIn('id="executePlan"', self.html)
        self.assertIn("data-idle=\"Execute planned actions\" disabled", self.html)

    def test_the_panel_states_that_planning_does_not_execute(self) -> None:
        self.assertIn("The planner proposes; policy decides", self.html)

    def test_planned_execution_cannot_grant_itself_a_permission(self) -> None:
        # Policy rules are fine — the workspace declaring what it will consider is
        # the existing pattern. What must never happen is the browser handing the
        # server a permission or grant: those come only from the consent store.
        segment = self._execute_plan_handler()
        self.assertIn("/execute", segment)
        self.assertNotIn("permissions:", segment)
        self.assertNotIn("grants:", segment)

    def test_planned_policy_rules_cover_only_the_proposed_actions(self) -> None:
        # Bounded to the proposal: the browser cannot blanket-allow everything.
        self.assertIn(
            "plannedActions.map(action => ({action: action.type, decision: 'allow'}))",
            self._execute_plan_handler(),
        )

    def test_proposed_actions_are_rendered_without_inner_html(self) -> None:
        # Planner output is untrusted text; it must land via textContent.
        start = self.html.index("document.querySelector('#plan')")
        end = self.html.index("document.querySelector('#executePlan')")
        segment = self.html[start:end]
        self.assertIn("row.textContent = text", segment)
        self.assertNotIn("innerHTML", segment)


class TestWebViewCompatibility(unittest.TestCase):
    """The dashboard must render inside the Chrome 83 WebView the app ships on.

    ``Element.replaceChildren`` is a Chrome 86+ API. The Android 11 system
    WebView is Chrome 83 and throws a TypeError on it, and because that render
    sat inside the same try-block as the fetches, the catch handler reported the
    service as "Offline" while every request was answering 200. These tests pin
    the renderer to the DOM subset an old WebView actually has, so a list added
    later cannot quietly reintroduce the failure.
    """

    def setUp(self) -> None:
        self.html = page()

    def _helper(self) -> str:
        start = self.html.index("const replaceAll = (element, nodes) => {")
        end = self.html.index("const renderList =")
        return self.html[start:end]

    def test_the_page_never_calls_replace_children(self) -> None:
        # A comment may name the API; no call may.
        self.assertNotIn(".replaceChildren(", self.html)

    def test_children_are_replaced_through_a_webview_safe_helper(self) -> None:
        self.assertIn("const replaceAll = (element, nodes) => {", self.html)

    def test_the_helper_clears_and_appends_without_replace_children(self) -> None:
        segment = self._helper()
        self.assertIn("element.textContent = '';", segment)
        self.assertIn("element.appendChild(node)", segment)

    def test_every_dynamic_list_renders_through_the_helper(self) -> None:
        # capabilities, executions and the proposed plan each called the
        # unsupported API directly before; each must go through the helper now.
        for marker in (
            "replaceAll(actions,",
            "renderList('#executions'",
            "replaceAll(planFeed,",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.html)

    def test_rendered_rows_still_use_text_content(self) -> None:
        # The helper is a compatibility shim, not a licence to trust markup.
        segment = self.html[self.html.index("const renderList ="):]
        self.assertIn("row.textContent =", segment)
        self.assertNotIn("innerHTML", segment)


class TestDeviceControlPlane(unittest.TestCase):
    """C19 mobile control dashboard: the phone sees devices, pairs, and approves.

    These pin the *security-relevant* shape of the new UI, not its layout: the
    device token's storage, that the browser never picks its own ceiling, and that
    an action is reported by the server's real state rather than by the fact that a
    button was clicked.
    """

    def setUp(self) -> None:
        self.html = page()

    def _between(self, start: str, end: str) -> str:
        return self.html[self.html.index(start):self.html.index(end)]

    def test_devices_and_approvals_are_bound_to_real_routes(self) -> None:
        self.assertIn('id="deviceList"', self.html)
        self.assertIn('id="approvalList"', self.html)
        self.assertIn("get('/devices')", self.html)
        self.assertIn("get('/approvals')", self.html)

    def test_pairing_mints_locally_and_redeems_for_a_token(self) -> None:
        self.assertIn("fetch('/pairing/code'", self.html)
        self.assertIn("fetch('/pair'", self.html)
        self.assertIn("fetch(`/devices/${encodeURIComponent(deviceId)}/revoke`", self.html)

    def test_the_ceiling_is_chosen_at_issuance_and_cannot_be_redeemed_in(self) -> None:
        # The laptop picks the ceiling when minting; the redeeming phone may not ask
        # for a higher one -- a limit the requester sets is not a limit. The request
        # body is the place that would matter; reading the granted value back is fine.
        self.assertIn("max_risk: document.querySelector('#pairRisk').value", self.html)
        request = self._between("fetch('/pair'", "const body = await response.json()")
        self.assertNotIn("max_risk", request)

    def test_the_laptop_can_pair_a_phone_that_may_run_programs(self) -> None:
        # device.terminal.execute is CRITICAL, so a phone minted below critical can
        # never reach the flagship flow at all. The choice belongs to the human standing
        # at the laptop -- and that choice has to say out loud what it unlocks.
        self.assertIn('<option value="critical">', self.html)
        self.assertIn("run a program", self.html)

    def test_the_token_lives_in_session_storage_never_persistent_storage(self) -> None:
        self.assertIn("sessionStorage.setItem(tokenKey", self.html)
        self.assertIn("sessionStorage.getItem(tokenKey)", self.html)
        self.assertNotIn("localStorage", self.html)

    def test_the_token_is_sent_as_a_bearer_header(self) -> None:
        self.assertIn("headers.authorization = 'Bearer ' + token", self.html)
        # Every mutating call goes through the auth-aware header builder.
        self.assertNotIn("headers: {'content-type': 'application/json'}", self.html)

    def test_the_token_is_never_rendered_or_logged(self) -> None:
        # The token string is touched exactly once in the whole page: to store it.
        # It is never interpolated into a status message, a log, or a fetch body.
        self.assertNotIn("console.log", self.html)
        self.assertEqual(self.html.count("body.token"), 1)
        self.assertIn("setToken(body.token)", self.html)

    def test_results_are_reported_from_the_server_state_not_the_click(self) -> None:
        # "requested" must never be dressed up as "successful": the displayed verdict
        # is the state the server returned, and approval resumes on the echoed run.
        self.assertIn("Execution ${body.state}", self.html)
        self.assertIn("Execution ${run.state}", self.html)
        self.assertIn("const run = body.execution", self.html)

    def test_dynamic_device_and_approval_rows_use_text_content(self) -> None:
        segment = self._between("const renderDevices", "const load = async")
        self.assertNotIn("innerHTML", segment)
        self.assertIn("label.textContent =", segment)
        self.assertIn("summary.textContent =", segment)

    def test_a_poll_of_unchanged_data_does_not_rebuild_the_panels(self) -> None:
        # The ten-second poll must not wipe the Approve/Reject buttons out from under
        # a tap that landed a moment earlier; each panel re-renders only when the
        # server's data actually changed.
        self.assertIn("if (signature === lastDeviceSig) return;", self.html)
        self.assertIn("if (signature === lastApprovalSig) return;", self.html)

    def test_a_dead_token_is_dropped_not_replayed_forever(self) -> None:
        # When the server restarts on an ephemeral key (or the device is revoked), the
        # stored token stops working. The dashboard must drop it on a 401 and fall back
        # to the local view, rather than presenting a credential that never works again.
        self.assertIn("if (response.status === 401) setToken('');", self.html)

    def test_output_events_are_listened_for_and_rendered_as_text(self) -> None:
        # The live output stream is the Phase 13 feature; it must arrive over SSE and
        # land via textContent, never markup.
        self.assertIn("'execution.output'", self.html)
        segment = self._between("const renderOutput", "const reconcile")
        self.assertIn("outputBox.textContent = text;", segment)
        self.assertNotIn("innerHTML", segment)

    def test_output_buffer_is_bounded_and_scroll_is_preserved(self) -> None:
        # A chatty command must not grow the page without limit, and the reader's
        # scroll position must survive new chunks unless they asked to follow.
        self.assertIn("state.output.slice(-8000)", self.html)
        segment = self._between("const renderOutput", "const limitNote")
        self.assertIn("scrollHeight - outputBox.scrollTop", segment)
        self.assertIn("atBottom && follow", segment)

    def test_the_phone_is_told_which_limit_stopped_a_run(self) -> None:
        # C21's reported limits have to reach the reader, or an empty output panel is
        # still silent about why. Read from the authoritative record, rendered as text.
        segment = self._between("const limitNote", "const noteLimits")
        # `GET /executions/{id}` carries the per-node views under `results`; the
        # `POST /execute` response calls the same thing `nodes`. Reading the wrong one
        # `POST /execute` response calls the same thing `nodes`. Reading the wrong one
        # is silent: the note simply never appears.
        self.assertIn("Object.entries(record.results || {})", segment)
        self.assertIn("result.timeout_clipped", segment)
        self.assertIn("result.timeout_applied_s", segment)
        self.assertIn("result.timeout_requested_s", segment)
        self.assertIn("result.output_captured === false", segment)
        self.assertNotIn("innerHTML", segment)

    def test_limits_are_pulled_on_completion_and_on_reconnect(self) -> None:
        # Live: the run just finished, so ask for its record. After a sleep or a
        # reconnect the same extraction runs over every execution being watched.
        completed = self._between(
            "} else if (type === 'execution.completed') {",
            "} else if (type === 'execution.denied') {",
        )
        self.assertIn("noteLimits(id)", completed)
        segment = self._between("const reconcile", "let eventStream")
        self.assertIn("state.limits = limitsOf(record)", segment)

    def test_the_phone_can_start_a_laptop_command(self) -> None:
        # Watching and approving a laptop execution was possible; starting one from the
        # phone was not. Same server, same policy check, same approval path.
        for field in ('id="cmdCommand"', 'id="cmdArguments"', 'id="cmdWorkingDir"',
                      'id="cmdTimeout"', 'id="runCommand"'):
            self.assertIn(field, self.html)
        segment = self._between("const runLaptopCommand", "const eventFeed")
        self.assertIn("device.terminal.execute", segment)
        self.assertIn("parameters: {command, arguments: argv, working_directory: workingDirectory, timeout_s: timeoutS}", segment)
        self.assertNotIn("innerHTML", segment)

    def test_command_arguments_travel_as_argv_lines(self) -> None:
        # One argument per line, sent as a list. A single string is the shape only a
        # shell could make sense of, and the command policy refuses it outright.
        segment = self._between("const runLaptopCommand", "const eventFeed")
        self.assertIn("split('\\n')", segment)
        self.assertIn("map(line => line.trim()).filter", segment)
        # Never reassembled into one string on the way out.
        self.assertNotIn(".join(", segment)
        self.assertIn("arguments: argv", segment)

    def test_the_command_panel_carries_no_authority(self) -> None:
        # Consent is server-side and CRITICAL risk still asks. The request says what to
        # run and nothing about whether it may.
        segment = self._between("const runLaptopCommand", "const eventFeed")
        for forbidden in ("grants", "authorized", "role", "max_risk", "allow_high_risk"):
            self.assertNotIn(forbidden, segment)

    def test_a_run_parked_for_approval_says_so(self) -> None:
        segment = self._between("const runLaptopCommand", "const eventFeed")
        self.assertIn("waiting_permission", segment)

    def test_the_operator_can_consent_a_laptop_scope(self) -> None:
        # A command panel whose scope cannot be consented from the same page is a form
        # that only ever parks. The laptop's own scopes are offered alongside the mock.
        for scope in ('value="file.read"', 'value="device.process.control"',
                      'value="system.shell"'):
            self.assertIn(scope, self.html)

    def test_every_scope_the_laptop_needs_can_be_granted_from_the_page(self) -> None:
        # The real rule, not a sample: a capability whose scope is missing from this
        # select can never run, and the failure looks like a policy refusal rather than
        # the form being short one option.
        from actionmesh.agent.definitions import device_definitions

        required = {p for d in device_definitions() for p in d.permissions}
        offered = set(re.findall(r'<option value="([^"]+)"', self.html))
        self.assertEqual(required - offered, set())
        self.assertGreaterEqual(len(required), 13)

    def test_a_paired_phone_s_actor_comes_from_its_credential(self) -> None:
        # The server refuses a body `actor` that disagrees with the bearer token, so a
        # panel that always sends one works only for the operator at the laptop. The
        # actor is sent when there is no token to decide it, and omitted when there is.
        segment = self._between("const identityBody", "const get =")
        self.assertIn("getToken() ? {} : {actor:", segment)
        self.assertGreaterEqual(self.html.count("...identityBody()"), 2)

    def test_the_phone_asks_for_its_own_consents(self) -> None:
        # The refresh reads consents. With a device token the credential already says
        # whose, so naming the actor typed in the box would ask for someone else's.
        segment = self._between("const consentsPath", "const get =")
        self.assertIn("getToken() ? '/consents'", segment)
        self.assertIn("get(consentsPath())", self.html)
        self.assertIn("...consentBody(),", self.html)

    def test_the_phone_can_see_what_the_laptop_is_running(self) -> None:
        # The projection landed, so an inventory list finally reaches the client as
        # rows. Without a panel the phone still cannot ask the question.
        for field in ('id="refreshProcs"', 'id="procList"'):
            self.assertIn(field, self.html)
        self.assertIn('value="device.process.read"', self.html)
        segment = self._between("const loadProcesses", "const stopProcess")
        self.assertIn("device.process.list", segment)
        self.assertIn("result.processes", segment)
        self.assertIn(".textContent = text", segment)
        self.assertNotIn("innerHTML", segment)

    def test_a_listed_process_is_stopped_by_its_issued_id(self) -> None:
        # Stopping is by the id this agent issued and never by a pid, which is the
        # guessable-handle attack the registry exists to prevent.
        segment = self._between("const stopProcess", "document.querySelector('#refreshProcs')")
        self.assertIn("device.process.stop", segment)
        self.assertIn("{process_id: processId}", segment)
        self.assertNotIn("pid:", segment)

    def test_a_captured_process_can_have_its_logs_read(self) -> None:
        # device.process.logs exists now, and a row whose output is not being kept must
        # not offer a button that can only fail.
        segment = self._between("const loadProcesses", "const stopProcess")
        self.assertIn("readLogs", segment)
        self.assertIn("row.output_captured", segment)
        reader = self._between("const readLogs", "const loadFiles")
        self.assertIn("device.process.logs", reader)
        self.assertIn("{process_id: processId}", reader)

    def test_a_running_execution_can_be_cancelled_from_the_phone(self) -> None:
        # The server has answered POST /executions/{id}/cancel since C19 and the page
        # never offered it, so "User controls" stopped at starting things.
        segment = self._between("const cancelExecution", "const limitNote")
        self.assertIn("/cancel", segment)
        self.assertIn("Cancellation requested", segment)
        # C31 made cancellation interrupt the running call, so the C30 wording that
        # promised only next-step effect is now the inaccurate one.
        self.assertNotIn("takes effect at the next step", segment)
        self.assertNotIn("innerHTML", segment)
        live = self._between("const renderLive", "const renderOutput")
        self.assertIn("cancelExecution(state.id)", live)
        self.assertIn("state.final", live)

    def test_an_interrupted_run_is_over_and_says_so(self) -> None:
        # C35: `interrupted` is a terminal answer about an execution the server lost. If
        # the control room reads it as still-running, it keeps offering Cancel on a run
        # nothing is watching, and shows a spinner where the truth is "unknown".
        self.assertIn("interrupted: 'Interrupted'", self.html)
        self.assertIn(
            "'partially_completed', 'interrupted'", self.html,
            "the control room still counts an interrupted run as live",
        )

    def test_a_reconciled_run_is_reported_as_recovered(self) -> None:
        # The event the server publishes when it adopts a run it did not start.
        self.assertIn("execution.reconciled", self.html)
        self.assertIn("execution.cancelled", self.html)

    def test_a_parked_run_is_not_treated_as_finished(self) -> None:
        # A run that parks for approval finishes its *request*, not its execution. If the
        # page believes the run is over, the Cancel control disappears from a run that is
        # still streaming after it is approved -- which is what it did in a browser.
        completed = self._between(
            "} else if (type === 'execution.completed') {",
            "} else if (type === 'execution.denied') {",
        )
        self.assertIn("TERMINAL_STATES.has(detail.state)", completed)
        started = self._between(
            "} else if (type === 'node.started') {",
            "} else if (type === 'node.completed') {",
        )
        self.assertIn("state.final = null", started)

    def test_the_inventory_requests_carry_no_authority(self) -> None:
        # Every laptop call is built in one place, so that is where the check belongs:
        # the body says what to run and nothing about whether it may.
        segment = self._between("const runCapability", "const procFeed")
        for forbidden in ("grants", "authorized", "allow_high_risk", '"role"'):
            self.assertNotIn(forbidden, segment)
        self.assertIn("policy: {rules:", segment)

    def test_pairing_reopens_the_stream_for_the_new_credential(self) -> None:
        # The stream opened before pairing is the operator's view of everything. Kept
        # alive after pairing, it goes on receiving other actors' executions -- and then
        # 404s trying to read the records behind them.
        window = self.html[self.html.index("setToken(body.token);"):][:600]
        self.assertIn("eventStream.close()", window)
        self.assertIn("openStream()", window)

    def test_the_phone_can_browse_and_open_a_file(self) -> None:
        # The listing rows survive the boundary (C28) and a file body now arrives over
        # the transient channel, so the phone needs a way to ask for either.
        for field in ('id="browsePath"', 'id="browseGo"', 'id="browseList"'):
            self.assertIn(field, self.html)
        self.assertIn('value="device.file.list"', self.html)
        segment = self._between("const loadFiles", "const readFile")
        self.assertIn("device.file.list", segment)
        self.assertIn("result.entries", segment)
        self.assertNotIn("innerHTML", segment)
        reader = self._between("const readFile", "document.querySelector('#browseGo')")
        self.assertIn("device.file.read", reader)
        self.assertIn("{path: filePath}", reader)


if __name__ == "__main__":
    unittest.main()