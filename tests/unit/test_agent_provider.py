"""C19 laptop provider tests — real files, honest flags, and independent verification.

Everything here runs against a real temporary directory. The point is not coverage of
the file API; it is that a capability advertised as real behaves as real, that a
result the verifier cannot observe is never reported as a success, and that nothing a
caller sends can talk its way past a boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import socket
import tempfile
import time
import unittest

from actionmesh.agent.commands import CommandPolicy
from actionmesh.agent.definitions import CAPABILITIES, IMPLEMENTED, PROVIDER
from actionmesh.agent.paths import Workspace
from actionmesh.agent.processes import ProcessRegistry, is_listening
from actionmesh.agent.provider import MAX_READ_BYTES, MAX_WRITE_BYTES, LaptopProvider
from actionmesh.agent.runner import CommandRunner
from actionmesh.agent.verification import register_device_strategies
from actionmesh.core.verification.engine import default_engine
from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ProviderError, ProviderResult


def _action(action_type: str, **parameters: object) -> Action:
    return Action(type=action_type, provider=PROVIDER, parameters=dict(parameters))


class _Fs(unittest.TestCase):
    """One real workspace per test, and a `run_action` that asserts the result is real."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.mkdir(os.path.join(self.root, "notes"))
        with open(
            os.path.join(self.root, "notes", "existing.txt"), "w", encoding="utf-8"
        ) as handle:
            handle.write("already here\n")
        self.provider = LaptopProvider(Workspace(self.root))

    def run_action(self, action_type: str, **params: object) -> dict:
        result = asyncio.run(self.provider.execute(_action(action_type, **params)))
        self.assertTrue(result.ok, result)
        self.assertFalse(result.simulated)
        return result.data

    def failure(self, action_type: str, **params: object) -> ProviderError:
        with self.assertRaises(ProviderError) as ctx:
            asyncio.run(self.provider.execute(_action(action_type, **params)))
        return ctx.exception


def free_port() -> int:
    """Ask the OS for a port nothing is using, then release it.

    Fixed test ports collide: two files in one test run can each bind 8971 and then
    blame each other's results. There is a small race in taking a port and handing it
    back, and it is still better than a hardcoded number that another file may own.
    """
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])

def _noop() -> None: ...


class TestSelection(unittest.TestCase):
    def test_every_implemented_capability_is_claimed(self) -> None:
        # A capability in IMPLEMENTED with no handler would be advertised, approved,
        # dispatched, and then fail. These two sets may not drift.
        self.assertEqual(LaptopProvider(None).SUPPORTED, IMPLEMENTED)

    def test_the_provider_declares_itself_real(self) -> None:
        self.assertFalse(LaptopProvider(None).simulated)
        self.assertEqual(LaptopProvider(None).name, PROVIDER)

    def test_mock_capabilities_are_not_claimed(self) -> None:
        provider = LaptopProvider(None)
        self.assertFalse(provider.supports(_action("file.read")))
        self.assertFalse(provider.supports(_action("message.send")))

    def test_a_device_action_is_claimed_whatever_the_provider_field_says(self) -> None:
        # A request that omits `provider` arrives as provider="mock" — which is
        # exactly how a real capability gets silently simulated by the catch-all
        # fallback. Selection is on the action type, not on a caller's hint.
        self.assertTrue(LaptopProvider(None).supports(Action(type="device.file.read")))


class TestList(_Fs):
    def test_the_root_lists(self) -> None:
        data = self.run_action("device.file.list")
        self.assertEqual(data["count"], 1)
        self.assertEqual([entry["name"] for entry in data["entries"]], ["notes"])

    def test_nested_listing(self) -> None:
        data = self.run_action("device.file.list", path="notes")
        self.assertEqual([entry["name"] for entry in data["entries"]], ["existing.txt"])
        self.assertEqual(data["entries"][0]["path"], "notes/existing.txt")

    def test_a_link_that_escapes_is_hidden_and_counted(self) -> None:
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        os.symlink(os.path.join(outside, "s.txt"), os.path.join(self.root, "escape"))
        data = self.run_action("device.file.list")
        self.assertNotIn("escape", [entry["name"] for entry in data["entries"]])
        self.assertEqual(data["hidden_outside_root"], 1)

    def test_a_bad_limit_is_an_input_error(self) -> None:
        for bad in (0, -3, "many", True):
            with self.subTest(bad=bad):
                self.assertIs(
                    self.failure("device.file.list", limit=bad).kind,
                    FailureKind.INVALID_INPUT,
                )

    def test_listing_a_missing_directory_is_invalid_input(self) -> None:
        self.assertIs(
            self.failure("device.file.list", path="nope").kind, FailureKind.INVALID_INPUT
        )

    def test_listing_is_capped(self) -> None:
        for index in range(6):
            with open(os.path.join(self.root, f"f{index}.txt"), "w", encoding="utf-8") as handle:
                handle.write("x")
        self.assertEqual(len(self.run_action("device.file.list", limit=3)["entries"]), 3)


class TestRead(_Fs):
    def test_content_and_checksum_come_from_the_file(self) -> None:
        data = self.run_action("device.file.read", path="notes/existing.txt")
        self.assertEqual(data["content"], "already here\n")
        self.assertEqual(data["sha256"], hashlib.sha256(b"already here\n").hexdigest())
        self.assertFalse(data["binary"])
        self.assertFalse(data["truncated"])

    def test_binary_files_are_not_rendered_as_text(self) -> None:
        with open(os.path.join(self.root, "blob.bin"), "wb") as handle:
            handle.write(bytes(range(256)))
        data = self.run_action("device.file.read", path="blob.bin")
        self.assertTrue(data["binary"])
        self.assertEqual(data["content"], "")
        self.assertEqual(data["size"], 256)

    def test_a_file_over_the_read_ceiling_is_refused_not_streamed(self) -> None:
        with open(os.path.join(self.root, "big.txt"), "wb") as handle:
            handle.write(b"x" * (MAX_READ_BYTES + 10))
        self.assertIn("read limit", str(self.failure("device.file.read", path="big.txt")))

    def test_there_is_no_client_supplied_way_past_the_ceiling(self) -> None:
        # `force` is not a parameter of the action. A key outside the schema must not
        # turn into an override once it reaches the provider.
        with open(os.path.join(self.root, "big.txt"), "wb") as handle:
            handle.write(b"x" * (MAX_READ_BYTES + 10))
        self.failure("device.file.read", path="big.txt", force=True)

    def test_a_missing_file_is_invalid_input(self) -> None:
        self.assertIs(
            self.failure("device.file.read", path="ghost.txt").kind,
            FailureKind.INVALID_INPUT,
        )

    def test_an_empty_path_is_invalid_input(self) -> None:
        for bad in ("", "   ", None, 5):
            with self.subTest(bad=bad):
                self.assertIs(
                    self.failure("device.file.read", path=bad).kind,
                    FailureKind.INVALID_INPUT,
                )


class TestWrite(_Fs):
    def test_a_new_file_is_created_inside_the_root(self) -> None:
        data = self.run_action("device.file.write", path="made.txt", content="hello\n")
        self.assertEqual(data["bytes"], 6)
        with open(os.path.join(self.root, "made.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello\n")
        mode = os.stat(os.path.join(self.root, "made.txt")).st_mode & 0o777
        self.assertFalse(mode & 0o077)

    def test_overwrite_replaces_and_append_extends(self) -> None:
        self.run_action("device.file.write", path="log.txt", content="one\n")
        appended = self.run_action(
            "device.file.write", path="log.txt", content="two\n", append=True
        )
        self.assertTrue(appended["appended"])
        self.assertEqual(appended["size"], 8)
        over = self.run_action("device.file.write", path="log.txt", content="three\n")
        self.assertFalse(over["appended"])
        self.assertEqual(over["size"], 6)

    def test_no_directory_is_created_for_you(self) -> None:
        error = self.failure("device.file.write", path="new/deep.txt", content="x")
        self.assertIn("does not create directories", str(error))
        self.assertFalse(os.path.exists(os.path.join(self.root, "new")))

    def test_content_over_the_write_ceiling_is_refused(self) -> None:
        error = self.failure(
            "device.file.write", path="big.txt", content="x" * (MAX_WRITE_BYTES + 1)
        )
        self.assertIn("over the", str(error))

    def test_non_string_content_is_invalid_input(self) -> None:
        for bad in (None, 7, {"a": 1}, ["x"]):
            with self.subTest(bad=bad):
                self.assertIs(
                    self.failure("device.file.write", path="f.txt", content=bad).kind,
                    FailureKind.INVALID_INPUT,
                )

    def test_an_escape_attempt_is_a_denial_not_a_typo(self) -> None:
        for path in ("../escaped.txt", "/etc/hosts", "notes/../../x"):
            with self.subTest(path=path):
                self.assertIs(
                    self.failure("device.file.write", path=path, content="x").kind,
                    FailureKind.AUTHORIZATION,
                )
        self.assertFalse(os.path.exists(os.path.join(self.root, "..", "escaped.txt")))

    def test_a_secret_location_is_a_denial(self) -> None:
        self.assertIs(
            self.failure("device.file.write", path=".env", content="A=1").kind,
            FailureKind.AUTHORIZATION,
        )
        self.assertFalse(os.path.exists(os.path.join(self.root, ".env")))


class TestDelete(_Fs):
    def test_a_file_goes_away(self) -> None:
        self.assertTrue(self.run_action("device.file.delete", path="notes/existing.txt")["deleted"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "notes", "existing.txt")))

    def test_directories_are_not_deletable(self) -> None:
        error = self.failure("device.file.delete", path="notes")
        self.assertIn("deletes files only", str(error))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "notes")))

    def test_deleting_a_secret_named_file_is_refused(self) -> None:
        os.mkdir(os.path.join(self.root, ".ssh"))
        with open(os.path.join(self.root, ".ssh", "id_rsa"), "w", encoding="utf-8") as handle:
            handle.write("k")
        self.assertIs(
            self.failure("device.file.delete", path=".ssh/id_rsa").kind,
            FailureKind.AUTHORIZATION,
        )
        self.assertTrue(os.path.exists(os.path.join(self.root, ".ssh", "id_rsa")))


class TestUnconfigured(unittest.TestCase):
    def test_no_workspace_means_no_file_execution(self) -> None:
        provider = LaptopProvider(None)
        with self.assertRaises(ProviderError) as ctx:
            asyncio.run(provider.execute(_action("device.file.read", path="a")))
        self.assertIs(ctx.exception.kind, FailureKind.PERMANENT)
        self.assertIn("no workspace root", str(ctx.exception))

    def test_a_capability_with_no_handler_is_refused_rather_than_simulated(self) -> None:
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        provider = LaptopProvider(Workspace(root))
        # Whatever is not claimed by SUPPORTED must be refused outright. The
        # alternative is the generic mock fallback answering with a simulated
        # success for a capability the laptop has never implemented.
        with self.assertRaises(ProviderError) as ctx:
            asyncio.run(provider.execute(_action("device.not.a.real.capability")))
        self.assertIs(ctx.exception.kind, FailureKind.PERMANENT)
        self.assertIn("cannot perform", str(ctx.exception))

    def test_every_device_capability_the_agent_claims_has_a_handler(self) -> None:
        # The registry can be asked for anything registered; SUPPORTED is the set the
        # provider will act on. A gap between them is a capability that would be
        # advertised and approved and then fail, so the two must be identical.
        self.assertEqual(LaptopProvider(None).SUPPORTED, frozenset(CAPABILITIES))


class TestTerminal(_Fs):
    """A real program, run for real, with the policy boundary in front of it."""

    def setUp(self) -> None:
        super().setUp()
        self.policy = CommandPolicy(
            allowed=("git", "python3"), workspace_root=self.root
        )
        self.provider.command_policy = self.policy
        self.provider.runner = CommandRunner(max_concurrent=2, timeout_s=15)

    def test_a_permitted_command_runs_and_reports_its_exit_code(self) -> None:
        data = self.run_action("device.terminal.execute", command="git", arguments=["--version"])
        self.assertEqual(data["exit_code"], 0)
        self.assertIn("git version", data["stdout"])
        self.assertFalse(data["failed"])
        self.assertEqual(data["command"], "git")
        self.assertEqual(data["arguments"], ["--version"])

    def test_the_working_directory_is_reported_relative_not_absolute(self) -> None:
        data = self.run_action("device.terminal.execute", command="git", arguments=["--version"])
        self.assertNotIn(self.root, str(data))

    def test_a_denied_program_is_a_refusal_with_a_reason(self) -> None:
        error = self.failure("device.terminal.execute", command="rm", arguments=["-rf", "/"])
        self.assertIs(error.kind, FailureKind.AUTHORIZATION)
        self.assertIn("policy refused", str(error))

    def test_an_unlisted_program_is_a_refusal(self) -> None:
        self.assertIs(
            self.failure("device.terminal.execute", command="curl", arguments=["x"]).kind,
            FailureKind.AUTHORIZATION,
        )

    def test_inline_source_is_refused_even_for_an_allowed_interpreter(self) -> None:
        error = self.failure(
            "device.terminal.execute", command="python3", arguments=["-c", "print(1)"]
        )
        self.assertIn("not code given on the command line", str(error))

    def test_arguments_as_one_string_is_refused(self) -> None:
        # The shape that only a shell could make sense of.
        error = self.failure("device.terminal.execute", command="git", arguments="--version; id")
        self.assertIn("no shell", str(error))

    def test_no_policy_means_no_program_runs(self) -> None:
        bare = LaptopProvider(Workspace(self.root))
        with self.assertRaises(ProviderError) as ctx:
            asyncio.run(
                bare.execute(_action("device.terminal.execute", command="git"))
            )
        self.assertIs(ctx.exception.kind, FailureKind.PERMANENT)
        self.assertIn("no command policy", str(ctx.exception))

    def test_a_non_numeric_timeout_is_invalid_input(self) -> None:
        self.assertIs(
            self.failure(
                "device.terminal.execute", command="git", arguments=[], timeout_s="soon"
            ).kind,
            FailureKind.INVALID_INPUT,
        )

    def test_a_client_timeout_cannot_extend_the_configured_ceiling(self) -> None:
        # The runner takes the smaller of the two; asking for 10_000 seconds does not
        # buy 10_000 seconds.
        strict = CommandRunner(max_concurrent=1, timeout_s=1)
        self.provider.runner = strict
        data = self.run_action(
            "device.terminal.execute",
            command="python3",
            arguments=[self._sleeper()],
            timeout_s=10_000,
        )
        self.assertTrue(data["timed_out"])

    def test_the_result_says_the_callers_timeout_was_clipped(self) -> None:
        # A silent cut reads as a hung command. The two numbers have to arrive.
        self.provider.runner = CommandRunner(max_concurrent=1, timeout_s=1)
        data = self.run_action(
            "device.terminal.execute",
            command="python3",
            arguments=[self._sleeper()],
            timeout_s=600,
        )
        self.assertTrue(data["timed_out"])
        self.assertEqual(data["timeout_requested_s"], 600)
        self.assertEqual(data["timeout_applied_s"], 1)
        self.assertTrue(data["timeout_clipped"])

    def test_an_unrequested_timeout_is_not_reported_as_clipped(self) -> None:
        data = self.run_action(
            "device.terminal.execute", command="git", arguments=["--version"]
        )
        self.assertIsNone(data["timeout_requested_s"])
        self.assertFalse(data["timeout_clipped"])

    def test_a_command_stopped_at_the_ceiling_is_not_verified_as_finished(self) -> None:
        # The runner killed the program at its ceiling, so no exit code was witnessed --
        # and "no witnessed exit code" is not evidence that a command did its work. Before
        # C35 the observation matched on the command name alone and the verifier confirmed
        # a run it had watched being stopped, which is a green light without evidence.
        self.provider.runner = CommandRunner(max_concurrent=1, timeout_s=1)
        action = _action(
            "device.terminal.execute", command="python3", arguments=[self._sleeper()]
        )
        result = asyncio.run(self.provider.execute(action))
        self.assertTrue(result.data["timed_out"])
        self.assertIsNone(self.provider.observe(action))
        engine = default_engine()
        register_device_strategies(engine)
        verdict = engine.verify(action, result, self.provider)
        self.assertFalse(verdict.verified)

    def _sleeper(self) -> str:
        path = os.path.join(self.root, "slow.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("import time\ntime.sleep(30)\n")
        return path

    def test_a_run_is_verified_from_the_observed_exit_code(self) -> None:
        action = _action("device.terminal.execute", command="git", arguments=["--version"])
        result = asyncio.run(self.provider.execute(action))
        verifier = default_engine()
        register_device_strategies(verifier)
        outcome = verifier.verify(action, result, self.provider)
        self.assertTrue(outcome.verified, outcome.detail)

    def test_a_failing_program_is_still_a_completed_action(self) -> None:
        # "the tests failed" is a successful run of a test command. The exit code is
        # reported as the fact; the state says the work was done and observed.
        data = self.run_action(
            "device.terminal.execute", command="git", arguments=["definitely-not-a-command"]
        )
        self.assertTrue(data["failed"])
        self.assertNotEqual(data["exit_code"], 0)


class TestProcessesAndServers(_Fs):
    """Start, watch, and stop a real child through the capability surface."""


    def setUp(self) -> None:
        super().setUp()
        self.PORT = 0
        self.policy = CommandPolicy(
            allowed=("python3", "git"), workspace_root=self.root
        )
        self.provider.command_policy = self.policy
        self.provider.runner = CommandRunner(max_concurrent=3, timeout_s=20)
        self.provider.processes = ProcessRegistry(max_children=3)
        self.provider.server_ready_s = 0.6
        # One loop for the whole test. A child is bound to the loop that spawned it, so
        # start-listen-stop across three `asyncio.run` calls would fail on loop affinity
        # rather than on anything about the capability.
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self._close_loop)

    def _close_loop(self) -> None:
        for record in list(self.provider.processes.records()):
            with contextlib.suppress(Exception):
                self.loop.run_until_complete(
                    self.provider.processes.stop(record.process_id)
                )
        self.loop.close()

    def perform(self, action_type: str, **params: object):
        return self.loop.run_until_complete(
            self.provider.execute(_action(action_type, **params))
        )

    def call(self, action_type: str, **params: object) -> dict:
        result = self.perform(action_type, **params)
        self.assertTrue(result.ok, result)
        self.assertFalse(result.simulated)
        return result.data

    def reject(self, action_type: str, **params: object) -> ProviderError:
        try:
            self.perform(action_type, **params)
        except ProviderError as exc:
            return exc
        self.fail(f"{action_type} {params} was expected to be refused")
        raise AssertionError

    def new_port(self) -> int:
        """A fresh port per test.

        Counters and fixed numbers both collide: one test's server releases its socket
        a moment after the kill, so the next test that reuses the number sees "address
        already in use" and reads it as a failed launch.
        """
        return free_port()

    def start_server(self, port: int | None = None) -> dict:
        chosen = self.new_port() if port is None else port
        self.PORT = chosen
        return self.call(
            "device.server.start",
            name="api",
            command="python3",
            arguments=["-m", "http.server", str(chosen), "--bind", "127.0.0.1"],
            port=chosen,
        )

    def hold_script(self) -> str:
        """Relative to the workspace, so no absolute path passes through the API."""
        with open(os.path.join(self.root, "hold.py"), "w", encoding="utf-8") as handle:
            handle.write("import time\nfor _ in range(3000):\n    time.sleep(0.01)\n")
        return "hold.py"

    def test_a_process_starts_and_is_addressable_by_issued_id(self) -> None:
        script = self.hold_script()
        data = self.call(
            "device.process.start", command="python3", arguments=[script], label="hold"
        )
        self.assertTrue(data["process_id"].startswith("proc_"))
        self.assertEqual(data["state"], "running")
        self.assertIsNotNone(data["pid"])
        self.assertNotIn(self.root, str(data))

    def test_a_server_launch_says_its_output_is_not_captured(self) -> None:
        # A detached child has nothing reading its pipes, so its output goes to the
        # void by design. Saying so is the difference between an empty log panel that
        # means "nothing happened" and one that means "nothing is kept here".
        data = self.start_server()
        self.assertFalse(data["output_captured"])

    def test_a_listed_process_reports_its_output_posture(self) -> None:
        self.call(
            "device.process.start",
            command="python3",
            arguments=[self.hold_script()],
            label="hold",
        )
        listed = self.call("device.process.list")
        self.assertFalse(listed["processes"][0]["output_captured"])

    def test_the_child_appears_in_the_list(self) -> None:
        self.start_server()
        listed = self.call("device.process.list")
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["processes"][0]["kind"], "server")
        self.assertEqual(listed["capacity"], 3)

    def test_a_real_server_is_observed_listening(self) -> None:
        data = self.start_server()
        # Not "the spawn call returned" -- the loopback port actually answers.
        self.assertTrue(data["listening"], data)

    def test_a_wrong_port_declaration_is_reported_honestly(self) -> None:
        # Bind one port but declare the next one: the launch result must say it is not
        # listening on the declared port rather than echoing success.
        chosen = self.new_port()
        self.call(
            "device.server.start",
            name="web",
            command="python3",
            arguments=["-m", "http.server", str(chosen), "--bind", "127.0.0.1"],
            port=chosen + 1,
        )
        status = self.call("device.server.status", name="web")
        self.assertEqual(status["servers"][0]["listening"], False)

    def test_server_status_re_probes_rather_than_remembers(self) -> None:
        self.start_server()
        self.assertTrue(self.call("device.server.status")["servers"][0]["listening"])
        self.call("device.server.stop", name="api")
        entry = self.call("device.server.status")["servers"][0]
        # Still listed, now reported as stopped and no longer answering. A row that
        # simply disappeared would leave the reader guessing which happened.
        self.assertEqual(entry["state"], "stopped")
        self.assertFalse(entry["listening"])

    def test_a_stopped_server_is_really_stopped(self) -> None:
        data = self.start_server()
        stopped = self.call("device.server.stop", name="api")
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["process_id"], data["process_id"])
        self.assertFalse(self.loop.run_until_complete(is_listening(self.PORT)))
        self.assertEqual(self.provider.processes.running(), 0)

    def test_stopping_a_process_the_agent_never_started_is_a_denial(self) -> None:
        error = self.reject("device.process.stop", process_id="proc_7777")
        self.assertIs(error.kind, FailureKind.AUTHORIZATION)
        self.assertIn("started by this laptop agent", str(error))

    def test_privileged_ports_are_refused_before_the_child_is_spawned(self) -> None:
        for port in (80, 22, 1023, 99999, "8080", True):
            with self.subTest(port=port):
                error = self.reject(
                    "device.server.start", name="x", command="python3", port=port
                )
                self.assertIs(error.kind, FailureKind.INVALID_INPUT)
        self.assertEqual(self.provider.processes.running(), 0)

    def test_a_server_needs_a_name(self) -> None:
        self.assertIs(
            self.reject("device.server.start", name="", command="python3").kind,
            FailureKind.INVALID_INPUT,
        )

    def test_a_launched_child_keeps_its_output_when_a_log_dir_is_set(self) -> None:
        log_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, log_dir, True)
        script = os.path.join(self.root, "talk.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(
                "import time\n"
                "print('hello from the child', flush=True)\n"
                "for _ in range(3000):\n"
                "    time.sleep(0.01)\n"
            )
        self.provider.log_dir = log_dir
        data = self.call(
            "device.process.start",
            command="python3",
            arguments=["talk.py"],
            label="chatty",
        )
        self.assertTrue(data["output_captured"])
        # The path stays on the machine: the issued id is the only handle a client gets.
        self.assertNotIn(log_dir, str(data))
        deadline = time.monotonic() + 5
        body = ""
        while time.monotonic() < deadline:
            names = [name for name in os.listdir(log_dir) if name.endswith(".log")]
            if names:
                with open(
                    os.path.join(log_dir, names[0]), encoding="utf-8"
                ) as handle:
                    body = handle.read()
                if body.strip():
                    break
            time.sleep(0.05)
        self.assertIn("hello from the child", body)

    def test_an_uncaptured_launch_still_says_so(self) -> None:
        data = self.call(
            "device.process.start",
            command="python3",
            arguments=[self.hold_script()],
            label="quiet",
        )
        self.assertFalse(data["output_captured"])


    def test_no_command_policy_means_no_child_starts(self) -> None:
        bare = LaptopProvider(Workspace(self.root))
        with self.assertRaises(ProviderError) as ctx:
            asyncio.run(
                bare.execute(_action("device.process.start", command="python3", arguments=[]))
            )
        self.assertIs(ctx.exception.kind, FailureKind.PERMANENT)

    def test_the_cap_is_enforced_through_the_capability(self) -> None:
        script = self.hold_script()
        for _ in range(3):
            self.call("device.process.start", command="python3", arguments=[script])
        error = self.reject("device.process.start", command="python3", arguments=[script])
        self.assertIn("already running 3 of 3", str(error))

    def test_a_launch_is_verified_by_the_process_being_alive(self) -> None:
        action = _action(
            "device.server.start",
            name="v",
            command="python3",
            arguments=["-m", "http.server", "8964", "--bind", "127.0.0.1"],
            port=8964,
        )
        live_port = self.new_port()
        result = self.perform(
            "device.server.start",
            name="v",
            command="python3",
            arguments=["-m", "http.server", str(live_port), "--bind", "127.0.0.1"],
            port=live_port,
        )
        action = _action(
            "device.server.start",
            name="v",
            command="python3",
            arguments=["-m", "http.server", str(live_port), "--bind", "127.0.0.1"],
            port=live_port,
        )
        action.action_id = result.action_id
        verifier = default_engine()
        register_device_strategies(verifier)
        self.assertTrue(verifier.verify(action, result, self.provider).verified)

    def test_a_server_that_never_answers_is_refuted_not_confirmed(self) -> None:
        # `http.server` rejects a non-numeric port and exits at once. "I called spawn"
        # is not evidence of a server, so a declared port that never answers must make
        # the launch refutable.
        name, args, port = "dead", ["-m", "http.server", "not-a-port"], free_port()
        result = self.perform(
            "device.server.start", name=name, command="python3", arguments=args, port=port
        )
        self.assertFalse(result.data["listening"], result.data)
        self.assertEqual(result.data["state"], "terminated", result.data)
        action = _action(
            "device.server.start", name=name, command="python3", arguments=args, port=port
        )
        action.action_id = result.action_id
        verifier = default_engine()
        register_device_strategies(verifier)
        outcome = verifier.verify(action, result, self.provider)
        self.assertFalse(outcome.verified, outcome.detail)
        self.assertEqual(outcome.status.value, "refuted")

    def test_a_portless_child_that_dies_later_is_caught_by_the_next_call(self) -> None:
        """The honest limit: a launch with no port can only be checked for liveness.

        Nothing is being *claimed* about a service here, so nothing can be refuted at
        launch time. What the system must not do is keep calling it `running` forever.
        """
        script = os.path.join(self.root, "quick.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("raise SystemExit(4)\n")
        data = self.call("device.process.start", command="python3", arguments=[script])
        self.assertIn(data["state"], {"running", "terminated"}, data)
        for _ in range(40):
            self.call("device.process.list")
            if data["state"] != "running":
                break
            self.loop.run_until_complete(asyncio.sleep(0.05))
        listed = self.call("device.process.list")["processes"]
        if listed:
            self.assertNotEqual(listed[0]["state"], "running", listed)


class TestGuiEvidence(_Fs):
    """C38: what a GUI action may and may not claim as evidence.

    These do not need a desktop. The adapter is stubbed where a live tree would
    otherwise be required, because the contract under test is the *provider's*: which
    adapter outcomes are evidence and which are refusals that must not be counted as
    one. The live round-trips are exercised separately when a desktop is present.
    """

    def _record(self, action_type: str, **recorded: object) -> Action:
        # `direction` belongs to the request, not to the effect record; the echo
        # below mirrors what the caller supplied, so it has to be in both places.
        params = {"role": "button", "name": "Save", "window": "notes"}
        if "direction" in recorded:
            params["direction"] = str(recorded["direction"])
        action = _action(action_type, **params)
        self.provider._effects[action.action_id] = {
            "action_type": action_type,
            "idempotency_key": action.idempotency_key,
            **recorded,
        }
        return action

    def test_a_click_that_was_dispatchable_is_observable(self) -> None:
        action = self._record("device.gui.click", status="dispatched", dispatched=True,
                              role="button", name="Save", window="notes",
                              state_changed=True)
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertTrue(observed["state"]["dispatched"])
        # The echo carries the target, so a click aimed somewhere else is refutable.
        self.assertEqual(observed["parameters"]["role"], "button")
        self.assertEqual(observed["parameters"]["name"], "Save")

    def test_a_click_that_was_refused_observes_nothing(self) -> None:
        for status in ("not_found", "ambiguous", "disabled", "not_actionable",
                       "element_changed", "window_not_found"):
            with self.subTest(status=status):
                action = self._record("device.gui.click", status=status,
                                      dispatched=False, role="button", name="Save")
                self.assertIsNone(
                    self.provider.observe(action),
                    f"a {status!r} refusal must not be observable",
                )

    def test_a_typing_that_landed_observes_lengths_and_never_the_text(self) -> None:
        action = self._record("device.gui.type", status="typed", typed=True,
                              role="text", name="", inserted=19)
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertEqual(observed["state"]["inserted"], 19)
        # The text is not in the effect record, so it cannot leak through the
        # observation into the journal or the dashboard.
        self.assertNotIn("text", observed["parameters"])
        self.assertNotIn("inserted", observed["parameters"])

    def test_a_refused_typing_observes_nothing(self) -> None:
        for status in ("not_editable", "disabled", "refused", "password", "invalid"):
            with self.subTest(status=status):
                action = self._record("device.gui.type", status=status, typed=False,
                                      role="text", name="")
                self.assertIsNone(self.provider.observe(action))

    def test_a_scroll_that_moved_is_observed_as_moved(self) -> None:
        action = self._record("device.gui.scroll", status="scrolled", moved=True,
                              role="label", name="item 000", direction="bottom",
                              offset_after=2114.0)
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertTrue(observed["state"]["moved"])
        self.assertEqual(observed["parameters"]["direction"], "bottom")

    def test_an_offset_change_that_did_not_move_content_does_not_claim_motion(self) -> None:
        action = self._record("device.gui.scroll", status="offset_changed", moved=False,
                              role="label", name="item 000", direction="bottom",
                              offset_after=900.0)
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed)
        self.assertFalse(
            observed["state"]["moved"],
            "the bar moved and the tree did not; claiming motion here would be a lie",
        )
        self.assertTrue(observed["state"]["bounds_unchanged"])

    def test_a_scroll_that_could_not_scroll_observes_nothing(self) -> None:
        for status in ("not_scrollable", "refused", "element_changed", "invalid"):
            with self.subTest(status=status):
                action = self._record("device.gui.scroll", status=status, moved=False,
                                      role="label", name="item 000", direction="bottom")
                self.assertIsNone(self.provider.observe(action))

    def test_an_unknown_gui_status_observes_nothing(self) -> None:
        action = self._record("device.gui.click", status="something_new")
        self.assertIsNone(self.provider.observe(action))


class TestScreen(_Fs):
    """C38: the screen capabilities are real, and their evidence is honest.

    These run against the actual desktop when there is one, and skip when there is
    not — a test that invented a tree or a screenshot would be exactly the thing the
    brief forbids. What is asserted is the *contract*: an inspection is witnessed by
    its element count, and a capture the OS declined is never confirmable. A machine
    where the portal grants the request exercises the other half of the second test.
    """

    def setUp(self) -> None:
        super().setUp()
        try:
            from actionmesh.agent import device_screen
        except Exception:  # pragma: no cover - import failure means no gi at all
            self.skipTest("device_screen unavailable in this environment")
        if not device_screen._gi_available() or not device_screen._atspi_available():
            self.skipTest("no accessibility tree on this machine")
        self.device_screen = device_screen

    def test_an_inspection_is_witnessed_by_its_element_count(self) -> None:
        action = _action("device.screen.inspect", max_elements=5)
        result = asyncio.run(self.provider.execute(action))
        self.assertTrue(result.ok)
        self.assertFalse(result.simulated)
        self.assertEqual(result.data.get("status"), "ok")
        observed = self.provider.observe(action)
        self.assertIsNotNone(observed, "an inspection that read a tree must be observable")
        self.assertEqual(observed["state"]["elements"], result.data["element_count"])
        self.assertLessEqual(observed["state"]["elements"], 5)

    def test_a_declined_capture_is_never_confirmable(self) -> None:
        action = _action("device.screen.capture")
        result = asyncio.run(self.provider.execute(action))
        status = result.data.get("status")
        if status == "captured":
            # The portal granted it on this machine. Then, and only then, is there
            # something to witness: the frame's own dimensions.
            observed = self.provider.observe(action)
            self.assertIsNotNone(observed)
            self.assertEqual(observed["state"]["width"], result.data["file"]["width"])
        else:
            # Declined or unreachable: reporting an observation here would let the
            # verifier confirm a screen that was never read.
            self.assertIn(status, {"cancelled", "unavailable"})
            self.assertIsNone(
                self.provider.observe(action),
                f"a {status!r} capture must not be observable",
            )

    def test_an_inspection_of_no_tree_observes_nothing(self) -> None:
        # Simulate the no-bus outcome the handler records, without pretending a tree
        # was read: the observation must refuse to confirm it.
        action = _action("device.screen.inspect")
        self.provider._effects[action.action_id] = {
            "action_type": "device.screen.inspect",
            "status": "unavailable",
            "window": "",
            "count": None,
        }
        self.assertIsNone(self.provider.observe(action))


class TestVerification(_Fs):
    """C6 must be able to check this provider's work without believing it."""

    def setUp(self) -> None:
        super().setUp()
        self.verifier = default_engine()
        register_device_strategies(self.verifier)

    def _verify(self, action: Action, result: ProviderResult):
        return self.verifier.verify(action, result, self.provider)

    def test_a_write_that_landed_is_confirmed(self) -> None:
        action = _action("device.file.write", path="v.txt", content="settled\n")
        result = asyncio.run(self.provider.execute(action))
        self.assertTrue(self._verify(action, result).verified)

    def test_a_write_that_did_not_land_is_refuted(self) -> None:
        action = _action("device.file.write", path="v.txt", content="settled\n")
        result = asyncio.run(self.provider.execute(action))
        # Something else changed the file between the write and the check.
        with open(os.path.join(self.root, "v.txt"), "w", encoding="utf-8") as handle:
            handle.write("not what was asked\n")
        outcome = self._verify(action, result)
        self.assertFalse(outcome.verified)
        self.assertEqual(outcome.status.value, "refuted")

    def test_a_delete_is_confirmed_by_absence_and_refuted_by_presence(self) -> None:
        action = _action("device.file.delete", path="notes/existing.txt")
        result = asyncio.run(self.provider.execute(action))
        self.assertEqual(self._verify(action, result).status.value, "confirmed")
        # Put the file back: the same claim must now be refutable, because the
        # observation re-stats the path instead of replaying what the code did.
        with open(
            os.path.join(self.root, "notes", "existing.txt"), "w", encoding="utf-8"
        ) as handle:
            handle.write("back\n")
        self.assertEqual(self._verify(action, result).status.value, "refuted")

    def test_observation_never_mentions_an_absolute_path(self) -> None:
        action = _action("device.file.read", path="notes/existing.txt")
        asyncio.run(self.provider.execute(action))
        self.assertNotIn(self.root, str(self.provider.observe(action)))

    def test_the_provider_does_not_certify_itself(self) -> None:
        action = _action("device.file.read", path="notes/existing.txt")
        result = asyncio.run(self.provider.execute(action))
        claim = asyncio.run(self.provider.verify(action, result))
        self.assertFalse(claim.verified)

    def test_an_unknown_action_id_observes_nothing(self) -> None:
        self.assertIsNone(self.provider.observe(_action("device.file.read", path="x")))

    def test_results_carry_the_real_flag(self) -> None:
        action = _action("device.file.read", path="notes/existing.txt")
        result = asyncio.run(self.provider.execute(action))
        self.assertFalse(result.simulated)
        self.assertEqual(result.provider, PROVIDER)


if __name__ == "__main__":
    unittest.main()
