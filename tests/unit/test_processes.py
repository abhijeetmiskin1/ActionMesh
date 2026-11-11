"""C19 process registry tests — ownership, the child cap, and the loopback probe.

The property under test is that stopping is only ever possible for something this
agent launched. Pids are small, sequential and guessable; a capability that accepted
one would let a phone reach the ssh daemon with the same call it uses for a test
runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from actionmesh.agent.commands import CommandPolicy
from actionmesh.agent.errors import ProcessError
from actionmesh.agent.processes import (
    EXITED,
    RUNNING,
    STOPPED,
    TERMINATED,
    TRIM_MARKER,
    ProcessObservation,
    ProcessRegistry,
    is_listening,
    loopback_http_observation,
    observe_process,
    stop_group,
)
from actionmesh.agent.runner import CommandRunner


class TestProcessIdentityObservation(unittest.TestCase):
    def test_pid_reuse_is_gone_and_cannot_be_stopped(self) -> None:
        from actionmesh.agent.processes import ProcessRecord

        record = ProcessRecord(
            process_id="proc_old", kind="process", label="old", command="python",
            pid=321, pgid=321, identity="linux:boot:old:S",
        )
        with patch("actionmesh.agent.processes.process_identity", return_value="linux:boot:new:S"):
            self.assertEqual(observe_process(321, record.identity), ProcessObservation.GONE)
            with patch("actionmesh.agent.processes.os.killpg") as killpg:
                self.assertFalse(stop_group(record, grace_s=0))
                killpg.assert_not_called()

    def test_unavailable_ownership_evidence_is_unknown_and_not_signalled(self) -> None:
        from actionmesh.agent.processes import ProcessRecord

        record = ProcessRecord(
            process_id="proc_old", kind="process", label="old", command="python",
            pid=321, pgid=321, identity=None,
        )
        with patch("actionmesh.agent.processes.os.kill", side_effect=PermissionError):
            self.assertEqual(observe_process(321, None), ProcessObservation.UNKNOWN)
        with patch("actionmesh.agent.processes.os.killpg") as killpg:
            self.assertFalse(stop_group(record, grace_s=0))
            killpg.assert_not_called()

    def test_a_temporary_identity_read_failure_is_unknown(self) -> None:
        with patch("actionmesh.agent.processes.process_identity", return_value=None), \
             patch("actionmesh.agent.processes.os.kill", return_value=None):
            self.assertEqual(observe_process(321, "linux:boot:start:S"), ProcessObservation.UNKNOWN)


class _FakeProcess:
    """Enough of an asyncio subprocess for registry bookkeeping.

    Only the fields the registry touches: a pid it can look up (and fail to, which is
    the point of the fallback) and a close() the reaper calls.
    """

    def __init__(self, pid: int = 999_999) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def close(self) -> None:
        return None


class _Spawn(unittest.TestCase):
    """Real children, started through the policy so ownership is realistic."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.policy = CommandPolicy(
            allowed=("python3", "sleep", "git"), workspace_root=self.root
        )
        self.runner = CommandRunner(max_concurrent=4, timeout_s=20)
        self.registry = ProcessRegistry(max_children=3)
        # One loop for the whole test. A child started here belongs to the loop that
        # started it, and `asyncio.run` would close that loop between calls -- which is
        # fine in a server that keeps one running, but not in a test that hops loops.
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self._close_loop)

    def _close_loop(self) -> None:
        # Every record, not just the running ones: a finished child whose transport was
        # never closed holds a pipe fd and a child watch until the garbage collector
        # gets around to it, which is exactly the leak this cleanup exists to prove.
        for record in list(self.registry._records.values()):
            with contextlib.suppress(Exception):
                self.loop.run_until_complete(self.registry.stop(record.process_id))
        for process in list(self.registry._live.values()):
            with contextlib.suppress(Exception):
                process.close()
        # The reaper closes children that were killed but not yet reaped.
        self.loop.run_until_complete(asyncio.sleep(0.05))
        self.registry.drain()
        self.loop.close()

    def await_(self, coro):
        return self.loop.run_until_complete(coro)

    def start(self, command: str, *args: str, kind: str = "process", name: str = "", **kw: object):
        decision = self.policy.evaluate(command, list(args))
        if decision.refused:
            raise AssertionError(f"policy refused {command}: {decision.reasons}")
        process = self.await_(self.runner.start(decision))
        return self.registry.adopt(
            kind=kind,
            decision=decision,
            process=process,
            label=name,
            working_directory=".",
            **kw,  # type: ignore[arg-type]
        )

    def stop(self, process_id: str):
        return self.await_(self.registry.stop(process_id))

    def sleeper(self) -> str:
        """A script that stays alive, so stopping and capping can be measured."""
        path = os.path.join(self.root, "hold.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("import time\nfor _ in range(3000):\n    time.sleep(0.01)\n")
        return path


def free_port() -> int:
    """Ask the OS for a port nothing is using, then release it.

    Fixed test ports collide: two files in one test run can each bind 8971 and then
    blame each other's results. There is a small race in taking a port and handing it
    back, and it is still better than a hardcoded number that another file may own.
    """
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class TestOwnership(_Spawn):
    def test_an_adopted_child_is_listed_and_addressable(self) -> None:
        record = self.start("python3", self.sleeper())
        self.await_(asyncio.sleep(0.1))
        self.assertEqual(record.state, RUNNING)
        self.assertIsNotNone(record.pid)
        self.assertEqual([r.process_id for r in self.registry.records()], [record.process_id])
        self.assertEqual(self.registry.running(), 1)

    def test_stopping_only_works_for_ids_this_registry_issued(self) -> None:
        for bad in ("proc_9999", "1", "pid-1234", "", "  "):
            with self.subTest(bad=bad):
                with self.assertRaises(ProcessError) as ctx:
                    self.stop(bad)
                self.assertIn("started by this laptop agent", str(ctx.exception))

    def test_a_stopped_child_is_gone_from_the_process_table(self) -> None:
        record = self.start("python3", self.sleeper())
        pid = record.pid
        assert pid is not None
        # Prove it was really running first. Without this, a `start` that returned an
        # already-dead child would make the stop assertion pass for the wrong reason.
        self.await_(asyncio.sleep(0.2))
        self.assertIsNone(self.registry._live[record.process_id].returncode)
        try:
            os.kill(pid, 0)
        except OSError:  # pragma: no cover - the child never started
            self.fail(f"child {pid} was not running before the stop")
        stopped = self.stop(record.process_id)
        self.assertEqual(stopped.state, STOPPED)
        with self.assertRaises(OSError):
            os.kill(pid, 0)

    def test_stopping_twice_reports_the_same_finished_record(self) -> None:
        record = self.start("python3", self.sleeper())
        self.assertEqual(self.stop(record.process_id).state, STOPPED)
        self.assertEqual(self.stop(record.process_id).state, STOPPED)

    def test_a_child_that_exited_cleanly_is_reaped_as_exited(self) -> None:
        record = self.start("git", "--version")
        for _ in range(50):
            self.registry.reap()
            if record.state != RUNNING:
                break
            self.await_(asyncio.sleep(0.02))
        self.assertEqual(record.state, EXITED)
        self.assertEqual(record.exit_code, 0)

    def test_a_child_that_failed_is_reaped_as_terminated_not_exited(self) -> None:
        record = self.start("git", "not-a-command")
        for _ in range(50):
            self.registry.reap()
            if record.state != RUNNING:
                break
            self.await_(asyncio.sleep(0.02))
        self.assertEqual(record.state, TERMINATED)
        self.assertNotEqual(record.exit_code, 0)

    def test_finished_children_are_hidden_from_the_default_listing(self) -> None:
        record = self.start("git", "--version")
        for _ in range(50):
            self.registry.reap()
            if record.state != RUNNING:
                break
            self.await_(asyncio.sleep(0.02))
        self.assertEqual(self.registry.records(), [])
        self.assertEqual(len(self.registry.records(include_finished=True)), 1)

class TestChildCap(_Spawn):
    def test_the_cap_refuses_rather_than_queues(self) -> None:
        started = [self.start("python3", self.sleeper()) for _ in range(3)]
        with self.assertRaises(ProcessError) as ctx:
            self.start("python3", self.sleeper())
        self.assertIn("already running 3 of 3", str(ctx.exception))
        for record in started:
            self.stop(record.process_id)
        self.assertEqual(self.registry.running(), 0)
        # Freed capacity is usable again, so the cap is not a one-way door.
        self.start("git", "--version")

    def test_the_registry_needs_at_least_one_slot(self) -> None:
        with self.assertRaises(ProcessError):
            ProcessRegistry(max_children=0)


class TestServers(_Spawn):
    def test_a_server_is_named_and_findable(self) -> None:
        self.port = free_port()
        record = self.start(
            "python3", "-m", "http.server", str(self.port), "--bind", "127.0.0.1",
            kind="server", name="api", port=self.port,
        )
        self.addCleanup(lambda: self.stop(record.process_id))
        self.assertEqual(self.registry.find_server("api"), record)
        self.assertEqual([r.label for r in self.registry.servers()], ["api"])

    def test_loopback_http_observation_reports_status_and_mismatch(self) -> None:
        async def check() -> None:
            async def respond(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                writer.write(b"HTTP/1.0 503 Service Unavailable\r\n\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(respond, "127.0.0.1", 0)
            port = int(server.sockets[0].getsockname()[1])
            try:
                observation = await loopback_http_observation(port, "/health", expected_status=200)
                self.assertEqual(observation.state, "UNHEALTHY")
                self.assertEqual(observation.status, 503)
                self.assertGreaterEqual(observation.elapsed_ms, 0)
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(check())

    def test_loopback_http_observation_does_not_follow_redirects(self) -> None:
        async def check() -> None:
            async def respond(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                writer.write(b"HTTP/1.0 302 Found\r\nLocation: http://example.invalid/\r\n\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(respond, "127.0.0.1", 0)
            port = int(server.sockets[0].getsockname()[1])
            try:
                observation = await loopback_http_observation(port, "/health")
                self.assertEqual(observation.state, "UNHEALTHY")
                self.assertEqual(observation.status, 302)
                self.assertEqual(observation.error, "redirect_not_followed")
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(check())

    def test_loopback_http_observation_times_out_without_claiming_violation(self) -> None:
        async def check() -> None:
            async def ignore(_reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
                await asyncio.Event().wait()

            server = await asyncio.start_server(ignore, "127.0.0.1", 0)
            port = int(server.sockets[0].getsockname()[1])
            try:
                observation = await loopback_http_observation(port, "/health", timeout_s=0.05)
                self.assertEqual(observation.state, "TIMEOUT")
                self.assertIsNone(observation.status)
                self.assertEqual(observation.error, "request_timeout")
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(check())

    def test_loopback_http_observation_distinguishes_unreachable(self) -> None:
        observation = asyncio.run(loopback_http_observation(free_port(), "/health"))
        self.assertEqual(observation.state, "UNREACHABLE")
        self.assertEqual(observation.error, "connection_refused")

    def test_a_second_server_with_the_same_name_is_refused(self) -> None:
        self.port = free_port()
        record = self.start(
            "python3", "-m", "http.server", str(self.port), "--bind", "127.0.0.1",
            kind="server", name="api", port=self.port,
        )
        self.addCleanup(lambda: self.stop(record.process_id))
        with self.assertRaises(ProcessError) as ctx:
            self.start(
                "python3",
                "-m", "http.server",
                str(free_port()),
                "--bind",
                "127.0.0.1",
                kind="server",
                name="api",
                port=self.port + 1,
            )
        self.assertIn("already running", str(ctx.exception))

    def test_a_server_needs_a_name(self) -> None:
        with self.assertRaises(ProcessError):
            self.start("git", "--version", kind="server", name="  ")

    def test_stop_server_by_name(self) -> None:
        self.port = free_port()
        record = self.start(
            "python3", "-m", "http.server", str(self.port), "--bind", "127.0.0.1",
            kind="server", name="web", port=self.port,
        )
        stopped = self.await_(self.registry.stop_server("web"))
        self.assertEqual(stopped.process_id, record.process_id)
        self.assertEqual(stopped.state, STOPPED)

    def test_an_unknown_server_name_is_refused(self) -> None:
        with self.assertRaises(ProcessError):
            self.await_(self.registry.stop_server("nope"))


class TestLoopbackProbe(unittest.TestCase):
    def test_a_real_listener_is_detected(self) -> None:
        port = free_port()
        listener = subprocess.Popen(
            ["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: (listener.terminate(), listener.wait(5)))
        for _ in range(50):
            if asyncio.run(is_listening(port)):
                return
            asyncio.run(asyncio.sleep(0.05))
        self.fail("a real loopback listener was not detected")

    def test_an_unused_port_is_reported_as_not_listening(self) -> None:
        # A port the OS just handed out and immediately released is the closest thing
        # to a provably closed port there is.
        self.assertFalse(asyncio.run(is_listening(free_port())))

    def test_a_bogus_port_is_reported_as_not_listening_rather_than_raising(self) -> None:
        for bad in (0, -1, 70000, "80", None, True):
            with self.subTest(bad=bad):
                self.assertFalse(asyncio.run(is_listening(bad)))  # type: ignore[arg-type]

    def test_the_probe_signature_has_no_host(self) -> None:
        """`port` is the only thing that can vary, so this cannot dial out."""
        import inspect

        params = list(inspect.signature(is_listening).parameters)
        self.assertEqual(params, ["port", "timeout_s"])


class TestRecordShape(unittest.TestCase):
    def test_public_view_carries_relative_paths_only(self) -> None:
        from actionmesh.agent.processes import ProcessRecord

        record = ProcessRecord(
            process_id="proc_0001",
            kind="process",
            label="tests",
            command="python3",
            arguments=("-m", "pytest"),
            working_directory="src/project",
            started_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        public = record.public()
        self.assertEqual(public["working_directory"], "src/project")
        self.assertNotIn("/home", str(public))
        self.assertEqual(public["arguments"], ["-m", "pytest"])

    def test_finished_children_keep_their_exit_code_for_the_audit(self) -> None:
        from actionmesh.agent.processes import ProcessRecord

        record = ProcessRecord(
            process_id="proc_0002",
            kind="server",
            label="api",
            command="uvicorn",
            state=TERMINATED,
            exit_code=3,
        )
        self.assertEqual(record.public()["exit_code"], 3)


class TestCapturedOutput(unittest.TestCase):
    """A child launched with a log says so, and the file stays the registry's own."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def _record(self, **kw: object):
        from actionmesh.agent.processes import ProcessRecord

        return ProcessRecord(
            process_id="proc_0001", kind="server", label="api", command="uvicorn", **kw
        )  # type: ignore[arg-type]

    def test_a_captured_child_reports_that_its_output_is_kept(self) -> None:
        log = os.path.join(self.root, "api.log")
        with open(log, "w", encoding="utf-8") as handle:
            handle.write("listening\n")
        public = self._record(log_path=log).public()
        self.assertTrue(public["output_captured"])

    def test_the_log_path_never_leaves_the_machine(self) -> None:
        # A phone does not need to know where the agent keeps its files, and the path
        # would name this user's home directory.
        log = os.path.join(self.root, "api.log")
        self.assertNotIn(self.root, str(self._record(log_path=log).public()))

    def test_an_uncaptured_child_still_says_false(self) -> None:
        self.assertFalse(self._record().public()["output_captured"])


class TestLogBounding(unittest.TestCase):
    """Capture is only safe if it cannot fill the disk."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.registry = ProcessRegistry(max_children=4)

    def _adopt(self, name: str, body: bytes):
        from actionmesh.agent.commands import CommandPolicy

        policy = CommandPolicy(allowed=("sleep",), workspace_root=self.root)
        decision = policy.evaluate("sleep", ["30"])
        log = os.path.join(self.root, name)
        with open(log, "wb") as handle:
            handle.write(body)
        record = self.registry.adopt(
            kind="server", decision=decision, process=_FakeProcess(), label="api",
            log_path=log,
        )
        return record, log

    def test_an_oversized_log_keeps_its_tail(self) -> None:
        _record, log = self._adopt("big.log", b"x" * 4000 + b"THE-LAST-LINE\n")
        self.assertEqual(self.registry.trim_logs(cap_bytes=1024), 1)
        with open(log, "rb") as handle:
            kept = handle.read()
        self.assertLessEqual(len(kept), 1024)
        self.assertTrue(kept.endswith(b"THE-LAST-LINE\n"))
        # The dropped bytes announce themselves: a short log must never look like a
        # complete one.
        self.assertTrue(kept.startswith(TRIM_MARKER))

    def test_a_log_under_the_cap_is_left_alone(self) -> None:
        _record, log = self._adopt("small.log", b"short\n")
        self.assertEqual(self.registry.trim_logs(cap_bytes=4096), 0)
        with open(log, "rb") as handle:
            self.assertEqual(handle.read(), b"short\n")

    def test_reading_a_log_returns_the_tail_for_its_owner_only(self) -> None:
        record, _log = self._adopt("read.log", b"first\nsecond\n")
        text, truncated = self.registry.read_log(record.process_id, max_bytes=4096)
        self.assertEqual(text, "first\nsecond\n")
        self.assertFalse(truncated)
        # Nothing outside the registry's own issued ids can be read at all.
        self.assertIsNone(self.registry.read_log("proc_9999", max_bytes=64))

    def test_a_child_without_a_log_reads_as_nothing(self) -> None:
        from actionmesh.agent.commands import CommandPolicy

        policy = CommandPolicy(allowed=("sleep",), workspace_root=self.root)
        record = self.registry.adopt(
            kind="process", decision=policy.evaluate("sleep", ["30"]),
            process=_FakeProcess(), label="bare",
        )
        self.assertIsNone(self.registry.read_log(record.process_id, max_bytes=64))


if __name__ == "__main__":
    unittest.main()
