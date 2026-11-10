"""C19 runner tests — real processes, real timeouts, and no inherited secrets.

These spawn actual children. A timeout that is asserted rather than measured is a
timeout that does not exist, and the environment isolation is the property that keeps
a phone-run command from reading the key that hashes every credential on the server.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest

from actionmesh.agent.commands import CommandPolicy
from actionmesh.agent.errors import ProcessError
from actionmesh.agent.runner import MAX_OUTPUT_BYTES, CommandRunner

TIMEOUT_COMMANDS = ("sleep", "python3")


class _Spawn(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.policy = CommandPolicy(
            allowed=("git", "sleep", "python3"),
            workspace_root=self.root,
        )
        self.runner = CommandRunner(max_concurrent=2, timeout_s=10)

    def run_command(self, command: str, *args: str, timeout_s: float | None = None):
        decision = self.policy.evaluate(command, list(args))
        if decision.refused:
            raise AssertionError(f"policy refused {command}: {decision.reasons}")
        return asyncio.run(self.runner.run(decision, timeout_s=timeout_s))

    def write_script(self, name: str, body: str) -> str:
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path


class TestRealExecution(_Spawn):
    def test_a_command_runs_and_its_output_is_captured(self) -> None:
        outcome = self.run_command("git", "--version")
        self.assertEqual(outcome.exit_code, 0)
        self.assertIn("git version", outcome.stdout)
        self.assertFalse(outcome.failed)
        self.assertFalse(outcome.timed_out)
        self.assertGreater(outcome.duration_s, 0)

    def test_a_failing_command_reports_its_exit_code_as_a_fact(self) -> None:
        outcome = self.run_command("git", "not-a-real-subcommand")
        self.assertNotEqual(outcome.exit_code, 0)
        self.assertTrue(outcome.failed)
        # The run happened; the program's opinion is the payload, not the verdict.
        self.assertEqual(outcome.exit_code, 1)

    def test_stderr_is_kept_separate_from_stdout(self) -> None:
        script = self.write_script(
            "streams.py",
            "import sys\nsys.stdout.write('out\\n')\nsys.stderr.write('err\\n')\n",
        )
        outcome = self.run_command("python3", script)
        self.assertEqual(outcome.stdout.strip(), "out")
        self.assertEqual(outcome.stderr.strip(), "err")

    def test_a_program_that_fills_both_pipes_does_not_deadlock(self) -> None:
        # A child writing more than a pipe buffer to stderr while we read only stdout
        # is the classic deadlock. Both are drained concurrently.
        script = self.write_script(
            "loud.py",
            "import sys\n"
            "sys.stdout.write('a' * 200000)\n"
            "sys.stderr.write('b' * 200000)\n"
            "sys.stdout.flush(); sys.stderr.flush()\n",
        )
        outcome = self.run_command("python3", script, timeout_s=20)
        self.assertEqual(outcome.exit_code, 0)
        self.assertTrue(outcome.truncated)

    def test_output_is_capped_at_the_declared_limit(self) -> None:
        script = self.write_script(
            "big.py", "import sys\nsys.stdout.write('x' * 4_000_000)\n"
        )
        outcome = self.run_command("python3", script, timeout_s=30)
        self.assertLessEqual(len(outcome.stdout), MAX_OUTPUT_BYTES)
        self.assertTrue(outcome.truncated)


class TestTimeout(_Spawn):
    def test_a_slow_command_is_stopped_and_reported(self) -> None:
        started = time.monotonic()
        outcome = self.run_command("sleep", "30", timeout_s=1)
        elapsed = time.monotonic() - started
        self.assertTrue(outcome.timed_out)
        self.assertIsNone(outcome.exit_code)
        self.assertLess(elapsed, 8, "the timeout should cut the wait, not wait it out")

    def test_a_requested_timeout_above_the_ceiling_is_reported_as_clipped(self) -> None:
        # The ceiling is the agent's configured number, not the caller's, and the
        # reader has to be able to tell "my command hung for the time I asked for"
        # from "this laptop would not wait longer than its own limit".
        runner = CommandRunner(max_concurrent=1, timeout_s=1)
        decision = self.policy.evaluate("sleep", ["30"])
        outcome = asyncio.run(runner.run(decision, timeout_s=30))
        self.assertTrue(outcome.timed_out)
        self.assertEqual(outcome.timeout_requested_s, 30)
        self.assertEqual(outcome.timeout_applied_s, 1)
        self.assertTrue(outcome.timeout_clipped)

    def test_a_requested_timeout_below_the_ceiling_is_the_one_applied(self) -> None:
        outcome = self.run_command("sleep", "30", timeout_s=1)
        self.assertEqual(outcome.timeout_requested_s, 1)
        self.assertEqual(outcome.timeout_applied_s, 1)
        self.assertFalse(outcome.timeout_clipped)

    def test_an_unrequested_timeout_applies_the_ceiling_without_clipping(self) -> None:
        outcome = self.run_command("git", "--version")
        self.assertIsNone(outcome.timeout_requested_s)
        self.assertEqual(outcome.timeout_applied_s, 10)
        self.assertFalse(outcome.timeout_clipped)

    def test_the_killed_child_is_actually_gone(self) -> None:
        decision = self.policy.evaluate("python3", [self._sleeper()])
        process_pid: list[int] = []

        async def drive() -> None:
            process = await self.runner.start(decision)
            process_pid.append(process.pid)
            await asyncio.sleep(0.2)
            await CommandRunner.stop(process)

        asyncio.run(drive())
        self.assertTrue(process_pid)
        with self.assertRaises(OSError):
            os.killpg(os.getpgid(process_pid[0]), 0)

    def test_cancelling_the_call_stops_the_command_and_its_children(self) -> None:
        # C31 reaches a running command by cancelling the provider call, so the runner's
        # own cancellation path is what a user's Cancel actually hits. Two things are
        # required: the group is killed, and the kill has *finished* before the
        # cancellation surfaces -- a report of "cancelled" while the child still holds the
        # port would be the executor grading its own homework again.
        script = self.write_script(
            "spawner_cancel.py",
            "import os, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
            "with open(sys.argv[1], 'w') as handle:\n"
            "    handle.write(f'{os.getpid()} {child.pid}\\n')\n"
            "time.sleep(300)\n",
        )
        probe = os.path.join(self.root, "pids.txt")
        decision = self.policy.evaluate("python3", [script, probe])

        async def scenario() -> None:
            call = asyncio.ensure_future(self.runner.run(decision, timeout_s=300))
            for _ in range(200):
                if os.path.exists(probe):
                    break
                await asyncio.sleep(0.05)
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call

        asyncio.run(scenario())
        self.assertTrue(os.path.exists(probe), "the command never started")
        with open(probe, encoding="utf-8") as handle:
            parent_pid, child_pid = (int(x) for x in handle.read().split())
        deadline = time.monotonic() + 5
        for pid in (parent_pid, child_pid):
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"pid {pid} survived the cancellation")

    def _sleeper(self) -> str:
        return self.write_script(
            "sleepy.py", "import time\nfor _ in range(3000):\n    time.sleep(0.01)\n"
        )

    def test_a_started_child_writes_its_output_to_the_log(self) -> None:
        script = self.write_script(
            "talk.py", "import sys\nprint('serving on :8080')\nsys.stdout.flush()\n"
        )
        log = os.path.join(self.root, "talk.log")
        decision = self.policy.evaluate("python3", [script])

        async def scenario() -> None:
            process = await self.runner.start(decision, log_path=log)
            await process.wait()

        asyncio.run(scenario())
        with open(log, encoding="utf-8") as handle:
            self.assertIn("serving on :8080", handle.read())

    def test_a_detached_child_that_floods_its_output_does_not_block(self) -> None:
        # The whole reason capture is a file and not a pipe: an OS pipe holds ~64 KiB,
        # and a server nobody is reading stops in write() the moment it fills. A file
        # applies no backpressure, so the child finishes instead of hanging.
        script = self.write_script(
            "flood.py",
            "import sys\n"
            "sys.stdout.write('x' * 400_000)\n"
            "sys.stdout.flush()\n"
            "print('DONE')\n",
        )
        log = os.path.join(self.root, "flood.log")
        decision = self.policy.evaluate("python3", [script])

        async def scenario() -> int:
            process = await self.runner.start(decision, log_path=log)
            return await asyncio.wait_for(process.wait(), timeout=15)

        self.assertEqual(asyncio.run(scenario()), 0)
        with open(log, encoding="utf-8") as handle:
            self.assertTrue(handle.read().endswith("DONE\n"))

    def test_starting_without_a_log_discards_output_as_before(self) -> None:
        script = self.write_script("talk.py", "print('into the void')\n")
        decision = self.policy.evaluate("python3", [script])
        log = os.path.join(self.root, "never-written.log")

        async def scenario() -> None:
            process = await self.runner.start(decision)
            await process.wait()

        asyncio.run(scenario())
        self.assertFalse(os.path.exists(log))

    def test_a_child_that_forked_is_taken_down_with_it(self) -> None:
        # The dev-server case: a parent that spawns helpers must not leave them
        # holding a port after the user pressed stop.
        script = self.write_script(
            "spawner.py",
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(300)\n",
        )
        outcome = self.run_command("python3", script, timeout_s=2)
        self.assertTrue(outcome.timed_out)
        child_pid = int(outcome.stdout.strip())
        # Poll for the grandchild to disappear rather than asserting immediately: a
        # killed process is a *zombie* until its (now-dead) parent's replacement reaps
        # it, and os.kill(pid, 0) succeeds on a zombie. Under a busy test runner that
        # reap can lag; waiting for it keeps the real guarantee -- the group kill took
        # the helper down -- without turning it into a timing coin-flip.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except OSError:
                break
            time.sleep(0.05)
        else:
            self.fail("forked grandchild survived the process-group kill")


class TestRefusals(_Spawn):
    def test_a_refused_decision_never_reaches_the_spawn(self) -> None:
        decision = self.policy.evaluate("rm", ["-rf", self.root])
        self.assertTrue(decision.refused)
        with self.assertRaises(ProcessError) as ctx:
            asyncio.run(self.runner.run(decision))
        self.assertIn("did not allow", str(ctx.exception))

    def test_a_program_that_disappears_between_check_and_exec_is_a_process_error(self) -> None:
        decision = self.policy.evaluate("python3", ["--version"])
        object.__setattr__(decision, "resolved_path", os.path.join(self.root, "vanished"))
        with self.assertRaises(ProcessError):
            asyncio.run(self.runner.run(decision))

    def test_the_runner_rejects_nonsense_bounds(self) -> None:
        for bad in (0, -1):
            with self.subTest(bad=bad), self.assertRaises(ProcessError):
                CommandRunner(max_concurrent=bad)
        with self.assertRaises(ProcessError):
            CommandRunner(timeout_s=0)


class TestEnvironmentIsolation(_Spawn):
    def test_the_child_sees_nothing_but_the_allowed_set(self) -> None:
        secret = "a-secret-that-must-not-travel"
        os.environ["ACTIONMESH_SECRET_KEY"] = secret
        os.environ["AWS_SECRET_ACCESS_KEY"] = secret
        os.environ["GITHUB_TOKEN"] = secret
        for key in (
            "ACTIONMESH_SECRET_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "GITHUB_TOKEN",
        ):
            self.addCleanup(os.environ.pop, key, None)
        script = self.write_script(
            "dumpenv.py", "import os\nfor key in sorted(os.environ):\n    print(key)\n"
        )
        outcome = self.run_command("python3", script)
        seen = set(outcome.stdout.split())
        self.assertNotIn("ACTIONMESH_SECRET_KEY", seen)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", seen)
        self.assertNotIn("GITHUB_TOKEN", seen)
        self.assertLessEqual(
            seen,
            {
                "PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM", "PWD", "OLDPWD", "_",
                "__CF_USER_TEXT_ENCODING", "LC_CTYPE",  # macOS injects these
            },
        )

    def test_the_child_runs_in_the_configured_directory(self) -> None:
        script = self.write_script("pwd.py", "import os\nprint(os.getcwd())\n")
        outcome = self.run_command("python3", script)
        self.assertEqual(
            os.path.realpath(outcome.stdout.strip()), os.path.realpath(self.root)
        )


class TestConcurrency(_Spawn):
    def test_the_concurrency_bound_is_enforced(self) -> None:
        runner = CommandRunner(max_concurrent=1, timeout_s=30)

        async def drive() -> float:
            decisions = [self.policy.evaluate("sleep", ["0.3"]) for _ in range(3)]
            started = time.monotonic()
            await asyncio.gather(*(runner.run(d) for d in decisions))
            return time.monotonic() - started

        elapsed = asyncio.run(drive())
        self.assertGreaterEqual(
            elapsed, 0.85, "one permit means the three runs cannot overlap"
        )

    def test_two_loops_do_not_deadlock_on_a_stale_semaphore(self) -> None:
        runner = CommandRunner(max_concurrent=2, timeout_s=5)
        decision = self.policy.evaluate("git", ["--version"])
        for _ in range(2):
            asyncio.run(runner.run(decision))


class TestLongRunning(_Spawn):
    def test_start_launches_without_waiting_and_can_be_stopped(self) -> None:
        script = self.write_script(
            "serve.py", "import time\nfor _ in range(3000):\n    time.sleep(0.01)\n"
        )
        decision = self.policy.evaluate("python3", [script])

        async def drive() -> bool:
            process = await CommandRunner(max_concurrent=4, timeout_s=5).start(decision)
            await asyncio.sleep(0.1)
            running = process.returncode is None
            await CommandRunner.stop(process)
            return running

        self.assertTrue(asyncio.run(drive()))


if __name__ == "__main__":
    unittest.main()
