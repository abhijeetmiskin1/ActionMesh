"""C35 phase 6 — the laptop agent knows which execution its work belongs to.

A server restart takes the request, the task and this process's memory with it. What it
does *not* take is the child program the agent launched: children start in their own
session precisely so a stop can reach them from anywhere, so a `python -m pytest` ordered
by a phone is still running after the server that ordered it has been replaced.

That is the evidence phase 4 needs. Without it, "what happened to that execution?" can
only ever be answered `interrupted`; with it, the new process can say *still running*,
*finished with exit code 0*, or *never had a child*, and can forward a cancellation to the
right process group.

The boundary that keeps this safe: every handle here is keyed by an **execution id the
server minted**, and the only processes reachable are the ones this agent recorded
starting for that execution. Nothing in this module takes a pid from a caller.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from actionmesh.agent.commands import CommandPolicy
from actionmesh.agent.definitions import PROVIDER
from actionmesh.agent.paths import Workspace
from actionmesh.agent.processes import ProcessRegistry
from actionmesh.agent.provider import LaptopProvider
from actionmesh.agent.runner import CommandRunner
from actionmesh.core.scope import install_execution, release_execution
from actionmesh.core.storage import SQLiteStore
from actionmesh.protocol.models import Action


def _action(action_type: str, **parameters: object) -> Action:
    return Action(type=action_type, provider=PROVIDER, parameters=dict(parameters))


class _LedgerCase(unittest.TestCase):
    """A real workspace, a real child process, and one durable ledger file."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db = str(Path(self.root) / "ledger.db")
        self.policy = CommandPolicy(allowed=("python3", "git"), workspace_root=self.root)
        self.made: list[LaptopProvider] = []
        # One loop for the whole test. A child's transport belongs to the loop that
        # spawned it, and stopping it from another one closes nothing -- which is how a
        # suite that passes ends up leaking file descriptors and ResourceWarnings.
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self._close_loop)

    def _close_loop(self) -> None:
        self.loop.close()

    def _stop_execution(self, execution_id: str) -> None:
        """Stop what this test started, and close what it opened on the way out.

        Two paths, in order: the provider that owns the child stops it properly (its
        transport is closed), then the ledger path sweeps anything the first pass never
        saw. A test that only used the second would leave the first's transport for the
        garbage collector to complain about.
        """
        for made in self.made:
            for record in made.processes.records():
                if record.execution_id == execution_id:
                    self.loop.run_until_complete(made.processes.stop(record.process_id))
        self.provider(self.store()).stop_execution(execution_id)

    def store(self) -> SQLiteStore:
        # Closed through addCleanup rather than in tearDown: the registry's own cleanup
        # flushes what it knows about each child into this ledger, and a store that is
        # already shut by the time that runs turns tidy teardown into an error.
        opened = SQLiteStore(self.db)
        self.addCleanup(opened.close)
        return opened

    def provider(self, ledger: SQLiteStore | None = None) -> LaptopProvider:
        made = LaptopProvider(
            Workspace(self.root),
            command_policy=self.policy,
            processes=ProcessRegistry(),
        )
        made.runner = CommandRunner(max_concurrent=2, timeout_s=20)
        if ledger is not None:
            made.ledger = ledger
        self.made.append(made)
        return made

    def hold_script(self) -> str:
        with open(os.path.join(self.root, "hold.py"), "w", encoding="utf-8") as handle:
            handle.write("import time\nfor _ in range(3000):\n    time.sleep(0.01)\n")
        return "hold.py"

    def in_execution(self, provider: LaptopProvider, execution_id: str, **params: object):
        """Call a capability the way the engine does: with the execution installed."""
        # Whatever this call starts on the real machine gets stopped by the ledger, the
        # same way a restarted server would stop it -- which is itself part of the demo.
        self.addCleanup(self._stop_execution, execution_id)
        token = install_execution(execution_id)
        try:
            return self.loop.run_until_complete(
                provider.execute(_action(params.pop("type"), **params))  # type: ignore[arg-type]
            )
        finally:
            release_execution(token)


class TestDispatchIsAddressedByExecution(_LedgerCase):
    def test_a_child_remembers_the_execution_that_started_it(self) -> None:
        provider = self.provider(self.store())
        script = self.hold_script()
        self.in_execution(
            provider,
            "exec_mine",
            type="device.process.start",
            command="python3",
            arguments=[script],
            label="hold",
        )
        evidence = provider.execution_evidence("exec_mine")
        self.assertIsNotNone(evidence, "the agent denied starting work it did start")
        self.assertEqual(evidence["state"], "running")
        self.assertTrue(evidence["process_ids"], "running, but nothing to stop")

    def test_an_execution_the_agent_never_served_has_no_evidence(self) -> None:
        provider = self.provider(self.store())
        self.assertIsNone(provider.execution_evidence("exec_never"))

    def test_the_only_handle_a_caller_may_name_is_an_execution_id(self) -> None:
        # Nothing here reaches a process by number: an unknown execution is simply not
        # the agent's work, and refusing is the same answer as for a mistyped id.
        provider = self.provider(self.store())
        stopped = provider.stop_execution("exec_never")
        self.assertEqual(stopped["stopped"], [])
        self.assertEqual(stopped["already_gone"], [])


class TestIdentityOutlivesTheProcessThatRecordedIt(_LedgerCase):
    def test_a_fresh_agent_reads_a_child_its_predecessor_started(self) -> None:
        script = self.hold_script()
        first = self.provider(self.store())
        self.in_execution(
            first,
            "exec_survivor",
            type="device.process.start",
            command="python3",
            arguments=[script],
            label="hold",
        )
        pid = first.execution_evidence("exec_survivor")["pids"][0]

        # The owning process is gone: a new provider, a new registry, the same ledger.
        reopened = self.provider(self.store())
        evidence = reopened.execution_evidence("exec_survivor")
        self.assertEqual(
            evidence["state"], "running", "a live child was reported as something else"
        )
        self.assertEqual(evidence["pids"], [pid])

        # And the new process can stop it, which is what a cancellation needs.
        stopped = reopened.stop_execution("exec_survivor")
        self.assertEqual(len(stopped["stopped"]), 1)
        settled = reopened.execution_evidence("exec_survivor")
        self.assertIn(settled["state"], ("exited", "gone"))

    def test_a_finished_command_leaves_its_exit_code_behind(self) -> None:
        provider = self.provider(self.store())
        self.in_execution(
            provider,
            "exec_git",
            type="device.terminal.execute",
            command="git",
            arguments=["--version"],
        )
        evidence = provider.execution_evidence("exec_git")
        self.assertEqual(evidence["state"], "exited")
        self.assertEqual(evidence["exit_code"], 0)

    def test_the_effect_of_a_run_is_readable_by_an_agent_that_missed_it(self) -> None:
        """`observe()` is the verifier's only witness, so it must survive too."""
        first = self.provider(self.store())
        action = _action(
            "device.terminal.execute", command="git", arguments=["--version"]
        )
        token = install_execution("exec_observe")
        try:
            self.loop.run_until_complete(first.execute(action))
        finally:
            release_execution(token)
        after = self.provider(self.store())
        self.assertNotIn(action.action_id, after._effects)
        observed = after.observe(action)
        self.assertIsNotNone(observed, "a restart lost the evidence the verifier reads")
        self.assertEqual(observed["idempotency_key"], action.idempotency_key)
        self.assertEqual(observed["state"]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
