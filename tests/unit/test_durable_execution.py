"""C35 — an execution must outlive the request, the tab and the process that started it.

The claims this checkpoint is judged on, written before the code:

1. a non-terminal execution is **loadable** after a restart, from the durable store;
2. reconciliation **never re-executes** a dispatched action — the side effect may already
   have happened, and running it twice is the exact failure this system exists to avoid;
3. cancellation is addressed at the **execution**, so a request that does not own the run
   can still ask for it to stop, and the owner honours it;
4. **actor isolation** survives the move from memory to the store — a record that is no
   longer in this process's memory is still not someone else's to read;
5. the event stream stays **cursor-correct** across a reconnect;
6. VoiceOS folds the authoritative state, including `interrupted`, into its task.

The distinction that runs through all of them: *unknown* is a real answer. A run the
server lost is not a failure (which would claim knowledge of the outcome) and certainly
not a success.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.core.events import EventHub
from actionmesh.core.executor import ExecutionEngine, IdempotencyLedger
from actionmesh.core.graph import graph_from_actions
from actionmesh.core.permissions import PermissionGrant, PolicyContext, PolicyEngine
from actionmesh.core.registry import ActionRegistry
from actionmesh.core.storage import SQLiteStore
from actionmesh.protocol.models import (
    Action,
    ActionState,
    ApprovalRequest,
    ExecutionRecord,
    Identity,
    MeshEvent,
    Policy,
    PolicyRule,
    RiskLevel,
)
from actionmesh.providers import MockProvider
from actionmesh.providers.base import ActionProvider, ProviderResult
from actionmesh.voiceos.models import TaskSession, TaskStep
from actionmesh.voiceos.orchestrator import Orchestrator
from tests.unit.test_executor import (
    ALLOW_ALL,
    _BlockingProvider,
    context,
    linear_graph,
    remind,
    run,
    send,
)


async def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    """Wait for a plain flag, on the poll interval the engine is allowed to use."""
    deadline = timeout
    stepped = 0.0
    while not predicate():
        if stepped >= deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.02)
        stepped += 0.02


LOCAL = ("127.0.0.1", 45123)
REMOTE = ("192.168.1.50", 55555)
SCOPES = ["message.send", "calendar.write"]


class _BlockingAndVerifiable(_BlockingProvider):
    """C31's blocking provider, plus the observation a completed run is verified against.

    `_BlockingProvider` has no `observe`, which is right for a run cancelled
    mid-flight: nothing finished, so there is nothing to look for. A run that is
    *released* does finish, and verification of a finished action with no observable
    state honestly reports failure. So the durable tests that expect a settled success
    have to offer the state their own `execute` produced.
    """

    def __init__(self) -> None:
        super().__init__()
        self.observed: dict[str, dict[str, Any]] = {}

    async def execute(self, action: Action) -> ProviderResult:
        result = await super().execute(action)
        if result.ok:
            self.observed[action.idempotency_key] = {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "receipt_id": f"rcpt_{action.idempotency_key}",
                "parameters": dict(action.parameters),
            }
        return result

    def observe(self, action: Action) -> dict[str, Any] | None:
        return self.observed.get(action.idempotency_key)


class _DurableCase(unittest.TestCase):
    """One SQLite file, and engines that can be thrown away like processes."""

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="am_c35_")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.path = str(Path(self.directory) / "executions.db")

    def store(self) -> SQLiteStore:
        opened = SQLiteStore(self.path)
        self.addCleanup(opened.close)
        return opened

    def engine(
        self, provider: ActionProvider, *, owner: str, store: SQLiteStore
    ) -> ExecutionEngine:
        return ExecutionEngine(
            ActionRegistry.with_seeds(),
            PolicyEngine(),
            [provider],
            store=store,
            owner_id=owner,
            ledger=IdempotencyLedger(),
        )

    def two_node(self, engine: ExecutionEngine):
        return engine.execute(
            linear_graph(send("hi"), remind("call")), context(SCOPES, policy=ALLOW_ALL)
        )

    def one_node(self, engine: ExecutionEngine):
        return engine.execute(
            graph_from_actions([send("hi")]), context(["message.send"], policy=ALLOW_ALL)
        )


class TestDurableRegistry(_DurableCase):
    def test_a_running_execution_is_persisted_before_it_ends(self) -> None:
        provider = _BlockingAndVerifiable()
        store = self.store()
        engine = self.engine(provider, owner="proc-a", store=store)

        async def scenario():
            task = asyncio.ensure_future(self.one_node(engine))
            await asyncio.wait_for(provider.entered.wait(), timeout=2)
            live = store.list_non_terminal()
            snapshot = (
                len(live),
                live[0].state if live else None,
                live[0].owner_id if live else None,
                bool(live and live[0].updated_at),
                bool(live and live[0].lease_expires_at),
                bool(live and live[0].cancel_requested),
            )
            provider.release.set()
            return snapshot, await task

        snapshot, result = run(scenario())
        self.assertEqual(snapshot[0], 1, "the running execution was not recoverable")
        self.assertEqual(snapshot[1], ActionState.EXECUTING)
        self.assertEqual(snapshot[2], "proc-a", "nobody owned the run")
        self.assertTrue(snapshot[3], "the record has no updated_at")
        self.assertTrue(snapshot[4], "the record has no lease")
        self.assertFalse(snapshot[5])
        self.assertEqual(result.state, ActionState.SUCCESS)

    def test_a_finished_execution_is_not_offered_as_non_terminal(self) -> None:
        store = self.store()
        result = run(self.one_node(self.engine(MockProvider(), owner="proc-a", store=store)))
        reopened = self.store()
        self.assertEqual(reopened.list_non_terminal(), [], "a finished run came back for reconciliation")
        recovered = reopened.get_record(result.execution_id)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.state, ActionState.SUCCESS)
        self.assertIsNone(recovered.lease_expires_at, "a terminal run must not hold a live lease")


class TestReconciliationNeverReruns(_DurableCase):
    def test_a_lost_run_becomes_interrupted_and_is_never_re_dispatched(self) -> None:
        provider = _BlockingProvider()
        store = self.store()
        first = self.engine(provider, owner="proc-a", store=store)

        async def crash_mid_run():
            task = asyncio.ensure_future(self.two_node(first))
            await asyncio.wait_for(provider.entered.wait(), timeout=2)
            execution_id = store.list_non_terminal()[0].execution_id
            # The crash: the request, the task and the process are gone, and the
            # provider call was never released -- whether it landed is genuinely unknown.
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            return execution_id

        execution_id = run(crash_mid_run())
        # The process that owned it is going away for good, and says so: releasing the
        # lease is what tells the next process that nobody is watching any more. A hard
        # crash instead leaves a live lease, and that run is deliberately left alone
        # until it expires -- see `test_a_run_still_leased_by_a_live_owner_is_not_touched`.
        first.release_leases()
        store.close()

        reopened = self.store()
        second_provider = _BlockingProvider()
        second = self.engine(second_provider, owner="proc-b", store=reopened)
        summary = run(second.reconcile_interrupted())
        self.assertEqual(summary["interrupted"], 1)
        self.assertEqual(second_provider.calls, 0, "reconciliation re-dispatched the action")
        record = reopened.get_record(execution_id)
        self.assertEqual(record.state, ActionState.INTERRUPTED)
        self.assertTrue(record.error, "an interrupted run must say why it is unknown")
        again = run(second.reconcile_interrupted())
        self.assertEqual(again["interrupted"], 0, "a second reconciliation changed the story")

    def test_a_terminal_execution_is_left_alone_by_reconciliation(self) -> None:
        store = self.store()
        run(self.one_node(self.engine(MockProvider(), owner="proc-a", store=store)))
        reopened = self.store()
        summary = run(
            self.engine(_BlockingProvider(), owner="proc-b", store=reopened).reconcile_interrupted()
        )
        self.assertEqual(summary["examined"], 0)
        self.assertEqual(summary["interrupted"], 0)

    def test_a_run_still_leased_by_a_live_owner_is_not_touched(self) -> None:
        store = self.store()
        now = datetime.now(UTC)
        store.save_record(
            ExecutionRecord(
                execution_id="graph_other",
                graph_id="graph_other",
                actor_id="a",
                state=ActionState.EXECUTING,
                updated_at=now,
                owner_id="proc-alive",
                lease_expires_at=now + timedelta(seconds=120),
            )
        )
        reopened = self.store()
        summary = run(
            self.engine(_BlockingProvider(), owner="proc-b", store=reopened).reconcile_interrupted()
        )
        self.assertEqual(summary["owned_elsewhere"], 1)
        self.assertEqual(summary["interrupted"], 0)
        self.assertEqual(reopened.get_record("graph_other").state, ActionState.EXECUTING)


class TestParkedApprovalSurvivesTheRestart(_DurableCase):
    """CASE A: a run stopped for a human is preserved, and nothing re-dispatches it."""

    def payment(self) -> tuple[object, PolicyContext]:
        graph = graph_from_actions(
            [{"type": "payment.prepare", "parameters": {"recipient": "alex", "amount": 5}}]
        )
        context = PolicyContext(
            actor=Identity(id="payer"),
            grants=[PermissionGrant(scope="payment.prepare", max_risk=RiskLevel.CRITICAL)],
            policy=Policy(rules=[PolicyRule(action="payment.prepare", decision="allow")]),
        )
        return graph, context

    def test_a_parked_run_is_still_parked_and_can_still_be_decided(self) -> None:
        store = self.store()
        graph, context = self.payment()
        parked = run(self.engine(MockProvider(), owner="proc-a", store=store).execute(graph, context))
        self.assertEqual(parked.state, ActionState.WAITING_PERMISSION)
        execution_id = parked.execution_id
        store.close()

        reopened = self.store()
        second = self.engine(MockProvider(), owner="proc-b", store=reopened)
        summary = run(second.reconcile_interrupted())
        self.assertEqual(summary["interrupted"], 0, "a parked run was declared lost")
        self.assertEqual(summary["preserved"], 1)
        self.assertEqual(reopened.get_record(execution_id).state, ActionState.WAITING_PERMISSION)
        waiting = second.awaiting_approval()
        self.assertEqual([item["execution_id"] for item in waiting], [execution_id])
        self.assertEqual([item["risk_level"] for item in waiting[0]["actions"]], ["critical"])
        # The action id survived the restart, which is the only reason a decision can
        # still be attached to it: ids are minted by the server, never sent by a client,
        # and a prompt answered against a different id than the run stopped on is no
        # answer at all.
        action_id = waiting[0]["actions"][0]["action_id"]
        self.assertEqual(action_id, graph.nodes[0].action.action_id)
        second.submit_approval(ApprovalRequest(action_id=action_id, status="approved"))
        resumed = run(second.resume(execution_id))
        self.assertIsNotNone(resumed, "the new process could not resume the parked run")
        self.assertEqual(resumed.state, ActionState.SUCCESS)
        self.assertEqual(reopened.get_record(execution_id).state, ActionState.SUCCESS)


class TestTheCancelRouteIsDurable(_DurableCase):
    """The route is where a phone's Cancel actually lands."""

    def test_the_http_cancel_writes_the_flag_on_the_execution(self) -> None:
        from actionmesh.voiceos.models import TaskSession  # noqa: F401  (keep import order honest)

        graph = graph_from_actions(
            [{"type": "payment.prepare", "parameters": {"recipient": "alex", "amount": 5}}]
        )
        policy_context = PolicyContext(
            actor=Identity(id="payer"),
            grants=[PermissionGrant(scope="payment.prepare", max_risk=RiskLevel.CRITICAL)],
            policy=Policy(rules=[PolicyRule(action="payment.prepare", decision="allow")]),
        )
        store = self.store()
        engine = self.engine(MockProvider(), owner="proc-a", store=store)
        parked = run(engine.execute(graph, policy_context))
        client = TestClient(create_app(engine=engine), client=LOCAL)

        first = client.post(f"/executions/{parked.execution_id}/cancel")
        self.assertEqual(first.json()["status"], "requested")
        # Not "this loop has a flag": any other process reading the same store now sees
        # the request, which is the whole difference between C31 and C35 cancellation.
        self.assertTrue(store.get_record(parked.execution_id).cancel_requested)
        second = client.post(f"/executions/{parked.execution_id}/cancel")
        self.assertEqual(second.json()["status"], "already_requested")
        self.assertEqual(store.get_record(parked.execution_id).state, ActionState.WAITING_PERMISSION)


class TestCancellationIsAboutTheExecution(_DurableCase):
    def test_a_request_that_does_not_own_the_run_can_cancel_it(self) -> None:
        provider = _BlockingProvider()
        store = self.store()
        owner = self.engine(provider, owner="proc-a", store=store)
        other = self.engine(_BlockingProvider(), owner="proc-b", store=store)

        async def scenario():
            task = asyncio.ensure_future(self.two_node(owner))
            await asyncio.wait_for(provider.entered.wait(), timeout=2)
            execution_id = store.list_non_terminal()[0].execution_id
            # A different process, holding the same durable store, asks it to stop.
            first_answer = other.request_cancel(execution_id)
            recorded = bool(store.get_record(execution_id).cancel_requested)
            # Idempotent: a second press records nothing new and says so.
            second_answer = other.request_cancel(execution_id)
            # The claim is not that a flag was written. The owner has to *act* on it,
            # which means interrupting the provider call it is currently awaiting --
            # and nothing here releases that call, so an interrupt is the only way the
            # run can end while it is still blocked inside the first node.
            await _wait_until(lambda: provider.interrupted)
            return first_answer, recorded, second_answer, await task

        first_answer, recorded, second_answer, result = run(scenario())
        self.assertTrue(first_answer["recorded"])
        self.assertTrue(recorded, "the cancellation never reached the execution")
        self.assertTrue(second_answer["already_requested"])
        self.assertFalse(second_answer["recorded"])
        self.assertTrue(provider.interrupted, "the cancellation never reached the live call")
        self.assertEqual(result.state, ActionState.CANCELLED)
        self.assertEqual(provider.calls, 1, "the second node ran after cancellation was requested")

    def test_cancelling_a_terminal_execution_does_not_convert_it(self) -> None:
        store = self.store()
        engine = self.engine(MockProvider(), owner="proc-a", store=store)
        result = run(self.one_node(engine))
        answer = engine.request_cancel(result.execution_id)
        self.assertFalse(answer["recorded"])
        self.assertEqual(answer["reason"], "terminal")
        self.assertEqual(store.get_record(result.execution_id).state, ActionState.SUCCESS)


class _EvidenceProvider(MockProvider):
    """A provider that can answer for work its own process never watched.

    The agent-side file (`test_execution_identity.py`) proves the evidence is real. What
    is under test here is the engine's *decisions*: that it consults the evidence before
    declaring a loss, that it never re-dispatches to find out, and that a cancellation it
    records is forwarded to whoever is holding the child.
    """

    def __init__(self, evidence: dict[str, dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.evidence = evidence or {}
        self.stopped: list[str] = []

    def execution_evidence(self, execution_id: str) -> dict[str, Any] | None:
        return self.evidence.get(execution_id)

    def stop_execution(self, execution_id: str) -> dict[str, Any]:
        self.stopped.append(execution_id)
        # A stopped child is no longer evidence of a run: the answer changes under the
        # engine's feet, the way it does when a real process group goes away.
        self.evidence[execution_id] = {"state": "gone"}
        return {"stopped": [f"proc_{execution_id}"], "already_gone": []}

    def observe(self, action: Any) -> dict[str, Any]:
        return {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "parameters": dict(action.parameters),
            "receipt_id": f"rcpt_{action.idempotency_key}",
        }


class TestReconciliationUsesTheAgentsEvidence(_DurableCase):
    """CASE B: a dispatched side effect may still be happening outside this process."""

    def running_run(self) -> tuple[str, SQLiteStore, ExecutionEngine, _EvidenceProvider]:
        """One execution, mid-flight, whose owning process has just gone away."""
        blocker = _BlockingAndVerifiable()
        store = self.store()
        first = self.engine(blocker, owner="proc-a", store=store)

        async def crash_mid_run():
            task = asyncio.ensure_future(self.one_node(first))
            await asyncio.wait_for(blocker.entered.wait(), timeout=2)
            execution_id = store.list_non_terminal()[0].execution_id
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            return execution_id

        execution_id = run(crash_mid_run())
        first.release_leases()
        return execution_id, store, first, blocker

    def test_a_child_still_running_keeps_the_execution_running(self) -> None:
        execution_id, store, _, _ = self.running_run()
        watcher = _EvidenceProvider({execution_id: {"state": "running", "pids": [4242]}})
        second = self.engine(watcher, owner="proc-b", store=store)
        summary = run(second.reconcile_interrupted())
        self.assertEqual(summary["adopted"], 1)
        self.assertEqual(summary["interrupted"], 0, "a live process was declared lost")
        self.assertFalse(watcher.calls, "reconciliation re-dispatched the action")
        record = store.get_record(execution_id)
        self.assertEqual(record.state, ActionState.EXECUTING)
        self.assertEqual(record.owner_id, "proc-b", "nobody took responsibility for it")
        self.assertIsNotNone(record.lease_expires_at)
        second.release_leases()

    def test_a_child_that_finished_is_recorded_from_its_own_evidence(self) -> None:
        execution_id, store, _, _ = self.running_run()
        # A real ledger row names the action it was written for -- the id the server
        # minted, which is why the body is read back rather than the id typed in.
        stored_graph = json.loads(store.load_body(execution_id)[0])
        action_id = stored_graph["nodes"][0]["action"]["action_id"]
        watcher = _EvidenceProvider(
            {
                execution_id: {
                    "state": "exited",
                    "exit_code": 0,
                    "actions": [
                        {
                            "action_id": action_id,
                            "process_id": "proc_0001",
                            "state": "exited",
                            "exit_code": 0,
                        }
                    ],
                    "detail": "1 dispatched action(s), 0 alive",
                }
            }
        )
        second = self.engine(watcher, owner="proc-b", store=store)
        summary = run(second.reconcile_interrupted())
        self.assertEqual(summary["settled"], 1)
        self.assertFalse(watcher.calls, "a settled run was dispatched again first")
        record = store.get_record(execution_id)
        self.assertEqual(record.state, ActionState.SUCCESS, record.error)
        self.assertTrue(record.results, "a settled run kept no account of what it saw")
        self.assertIsNotNone(record.finished_at, "a terminal record without an end time")
        self.assertIsNone(record.lease_expires_at, "a settled run still holds a lease")

    def test_a_cancellation_reaches_a_child_this_process_never_started(self) -> None:
        execution_id, store, _, _ = self.running_run()
        watcher = _EvidenceProvider({execution_id: {"state": "running", "pids": [4242]}})
        second = self.engine(watcher, owner="proc-b", store=store)

        async def scenario():
            await second.reconcile_interrupted()
            answer = second.request_cancel(execution_id)
            # The new owner holds no task and no in-flight call, so the only thing that
            # can honour this is the supervisor it started when it adopted the run.
            await _wait_until(lambda: bool(watcher.stopped))
            await _wait_until(
                lambda: store.get_record(execution_id).state is ActionState.CANCELLED
            )
            return answer

        answer = run(scenario())
        self.assertTrue(answer["recorded"])
        self.assertEqual(watcher.stopped, [execution_id])
        second.release_leases()

    def test_no_evidence_is_still_an_honest_interrupted(self) -> None:
        execution_id, store, _, _ = self.running_run()
        watcher = _EvidenceProvider()
        second = self.engine(watcher, owner="proc-b", store=store)
        summary = run(second.reconcile_interrupted())
        self.assertEqual(summary["interrupted"], 1)
        self.assertEqual(summary["adopted"], 0)
        self.assertEqual(store.get_record(execution_id).state, ActionState.INTERRUPTED)
        self.assertFalse(store.get_record(execution_id).cancel_requested)

    def test_a_run_the_agent_has_no_record_of_is_never_made_up(self) -> None:
        """`reconcile` must not turn a provider's silence into a success."""
        store = self.store()
        store.save_record(
            ExecutionRecord(
                execution_id="graph_no_child",
                graph_id="graph_no_child",
                actor_id="a",
                state=ActionState.EXECUTING,
                updated_at=datetime.now(UTC),
            )
        )
        summary = run(
            self.engine(_EvidenceProvider(), owner="proc-b", store=store).reconcile_interrupted()
        )
        self.assertEqual(summary["interrupted"], 1)
        self.assertEqual(summary["settled"], 0)


class TestActorIsolationSurvivesTheRestart(unittest.TestCase):
    def test_a_record_read_from_the_store_is_still_not_someone_elses(self) -> None:
        directory = tempfile.mkdtemp(prefix="am_c35_api_")
        self.addCleanup(shutil.rmtree, directory, True)
        saved = {}
        for name, value in (
            ("ACTIONMESH_AUDIT_DB", str(Path(directory) / "audit.db")),
            ("ACTIONMESH_DATABASE_URL", f"sqlite:///{Path(directory) / 'executions.db'}"),
        ):
            saved[name] = os.environ.get(name)
            os.environ[name] = value
        self.addCleanup(self._restore, saved)
        apps: list = []

        def build(client) -> TestClient:
            app = create_app()
            apps.append(app)
            return TestClient(app, client=client)

        owner = build(LOCAL)
        owner.post(
            "/consents",
            json={"actor_id": "owner-1", "scope": "message.send", "max_risk": "high"},
        )
        execution_id = owner.post(
            "/execute",
            json={
                "actor": {"id": "owner-1"},
                "graph": {"nodes": [
                    {"node_id": "a1", "action": {
                        "type": "message.send", "parameters": {"message": "hi"}}},
                    {"node_id": "end", "node_type": "end", "depends_on": ["a1"]},
                ]},
                "policy": {"rules": [{"action": "message.send", "decision": "allow"}]},
            },
        ).json()["execution_id"]
        self.addCleanup(self._close, apps)

        # A second process on the same durable store: this record has never been in its
        # memory, so every read below goes through the store.
        remote = build(REMOTE)
        self.assertEqual(
            remote.get(f"/executions/{execution_id}").status_code, 401,
            "a remote caller with no credential must not read a stored record",
        )
        local = build(LOCAL)
        self.assertEqual(local.get(f"/executions/{execution_id}").status_code, 200)
        listed = local.get("/executions").json()
        self.assertIn(execution_id, [row["execution_id"] for row in listed])

    @staticmethod
    def _restore(saved: dict) -> None:
        for name, previous in saved.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    @staticmethod
    def _close(apps: list) -> None:
        for app in apps:
            backend = getattr(app.state, "audit_backend", None)
            if backend is not None:
                backend.close()
            store = getattr(app.state.engine, "store", None)
            if store is not None:
                store.close()


class TestTheTimelineSurvivesTheProcess(_DurableCase):
    """A reconnecting client should reconstruct the timeline, not just the verdict.

    Phase 7 builds on the replay that already exists rather than replacing it: the
    difference is that the events now outlive the process that published them, and the
    transient ones still do not -- a phone that comes back gets the decisions and the
    outcomes, and never gets a dead process's stdout handed to it again.
    """

    def hub(self, store: SQLiteStore) -> EventHub:
        made = EventHub()
        made.attach_store(store)
        return made

    def test_a_later_process_replays_what_the_first_one_published(self) -> None:
        store = self.store()
        first = self.hub(store)
        for index in range(3):
            first.publish(
                MeshEvent(
                    event_type="execution.progress",
                    execution_id="graph_live",
                    actor="watcher-1",
                    payload={"completed": index},
                )
            )
        first.publish(
            MeshEvent(
                event_type="node.output",
                execution_id="graph_live",
                actor="watcher-1",
                payload={"text": "the body of a file"},
            ),
            transient=True,
        )
        reopened = self.hub(self.store())
        replayed = run(collect(reopened, None))
        self.assertEqual([event.event_type for event in replayed], ["execution.progress"] * 3)
        self.assertEqual([event.payload["completed"] for event in replayed], [0, 1, 2])

    def test_transient_output_is_not_brought_back_from_the_dead_process(self) -> None:
        store = self.store()
        self.hub(store).publish(
            MeshEvent(
                event_type="node.output",
                execution_id="graph_secret",
                actor="watcher-1",
                payload={"text": "transient"},
            ),
            transient=True,
        )
        self.assertEqual(store.event_count("graph_secret"), 0)
        self.assertEqual(run(collect(self.hub(self.store()), None)), [])

    def test_a_cursor_held_across_the_restart_resumes_rather_than_repeats(self) -> None:
        store = self.store()
        first = self.hub(store)
        first.publish(
            MeshEvent(event_type="execution.started", execution_id="g", actor="w")
        )
        seen = run(collect(first, None))
        reopened = self.hub(self.store())
        tail = run(collect(reopened, seen[-1].event_id))
        self.assertEqual(tail, [], "the event the client already had was sent again")


class TestStreamCursorStaysCorrect(unittest.TestCase):
    """The cursor is how a dropped stream resumes; test it where that logic lives.

    ``/events`` is an infinite body, so the route itself cannot be drained by a test.
    ``EventHub.subscribe(since_event_id=...)`` is the production code the route calls,
    and its replay is bounded, so this asserts the real behaviour rather than a copy.
    """

    def test_a_reconnect_replays_only_what_the_cursor_has_not_seen(self) -> None:
        hub = EventHub()
        for index in range(4):
            hub.publish(MeshEvent(
                event_type="execution.progress", execution_id="graph_1",
                actor="cursor-1", payload={"completed": index},
            ))
        hub.publish(MeshEvent(
            event_type="execution.completed", execution_id="graph_1",
            actor="cursor-1", payload={"state": "success"},
        ))
        seen = run(collect(hub, None))
        self.assertEqual(len(seen), 5)
        self.assertEqual(run(collect(hub, seen[-1].event_id)), [], "the cursor was replayed twice")
        middle = run(collect(hub, seen[1].event_id))
        self.assertEqual([event.event_id for event in middle], [item.event_id for item in seen[2:]])

    def test_a_transient_event_does_not_advance_the_clients_cursor(self) -> None:
        # The frame is what teaches a client where to resume from. Output chunks are
        # published live and never retained, so advertising one as a cursor would have
        # the next connection ask for a point in a timeline nothing can replay -- and
        # be handed the whole window again, duplicates included.
        from actionmesh.api import _sse_frame
        from actionmesh.core.output import OUTPUT_EVENT_TYPE

        retained = _sse_frame(
            MeshEvent(event_type="node.started", event_id="evt_keep", execution_id="g")
        )
        transient = _sse_frame(
            MeshEvent(event_type=OUTPUT_EVENT_TYPE, event_id="evt_drop", execution_id="g")
        )
        self.assertIn("id: evt_keep", retained)
        self.assertNotIn("id:", transient)
        self.assertIn("event: execution.output", transient)

    def test_a_cursor_that_aged_out_replays_the_window_rather_than_losing_events(self) -> None:
        hub = EventHub()
        hub.publish(MeshEvent(event_type="execution.started", execution_id="g", actor="a"))
        hub.publish(MeshEvent(event_type="node.completed", execution_id="g", actor="a"))
        # An id the hub has never retained must not silently mean "nothing to send".
        tail = run(collect(hub, "evt_never_retained"))
        self.assertEqual(len(tail), 2, "an unknown cursor dropped history instead of resending it")


async def collect(hub: EventHub, since: str | None) -> list[MeshEvent]:
    """Drain the replayed history only -- the live tail after it is an infinite stream."""
    events: list[MeshEvent] = []
    stream = hub.subscribe(None, since_event_id=since)
    try:
        while True:
            events.append(await asyncio.wait_for(stream.__anext__(), timeout=0.2))
    except (TimeoutError, StopAsyncIteration):
        pass
    finally:
        await stream.aclose()
    return events


class TestVoiceOSFoldsTheAuthoritativeState(_DurableCase):
    def test_an_interrupted_execution_folds_as_interrupted_not_succeeded(self) -> None:
        store = self.store()
        store.save_record(ExecutionRecord(
            execution_id="graph_lost", graph_id="graph_lost", actor_id="voice-user",
            state=ActionState.INTERRUPTED,
            error="the server restarted while this action was active",
        ))
        session = TaskSession(
            session_id="task_1",
            actor_id="voice-user",
            request="run my tests",
            steps=[TaskStep(
                index=0, label="Run the tests", action_type="device.terminal.execute",
                state="awaiting_approval", execution_id="graph_lost",
            )],
            status="awaiting_approval",
        )
        engine = ExecutionEngine(
            ActionRegistry.with_seeds(), PolicyEngine(), [MockProvider()], store=store
        )
        folded = Orchestrator(engine=engine).fold_records(session)
        self.assertEqual(folded.steps[0].state, "interrupted")
        self.assertIsNone(folded.steps[0].verified)
        self.assertEqual(folded.status, "interrupted")

    def test_a_step_that_finished_is_reported_even_when_the_task_continues(self) -> None:
        # Measured in a browser: the reattached page had every right to say "1 of 2 steps
        # settled", and said "0 of 2" -- the fold updated the step and left the summary and
        # the status describing a run that had already stopped waiting for approval.
        store = self.store()
        store.save_record(
            ExecutionRecord(
                execution_id="graph_done",
                graph_id="graph_done",
                actor_id="voice-user",
                state=ActionState.SUCCESS,
                node_states={"n": "success"},
            )
        )
        session = TaskSession(
            session_id="task_two",
            actor_id="voice-user",
            request="run the long build then read the notes",
            steps=[
                TaskStep(
                    index=0,
                    label="Run the long build",
                    action_type="device.terminal.execute",
                    state="awaiting_approval",
                    execution_id="graph_done",
                ),
                TaskStep(
                    index=1, label="Read the notes", action_type="device.file.read", state="pending"
                ),
            ],
            status="awaiting_approval",
            summary="0 of 2 steps settled.",
        )
        engine = ExecutionEngine(
            ActionRegistry.with_seeds(), PolicyEngine(), [MockProvider()], store=store
        )
        folded = Orchestrator(engine=engine).fold_records(session)
        # A SUCCESS record with no per-node verification evidence is reported as
        # succeeded-but-unverified, exactly as it would be live.
        self.assertEqual(folded.steps[0].state, "succeeded_unverified")
        self.assertTrue(folded.steps[0].settled)
        self.assertIn("1 of 2 steps settled", folded.summary)
        self.assertNotEqual(folded.status, "awaiting_approval")
        self.assertEqual(folded.status, "proposed")

    def test_a_denied_step_does_not_become_a_task_waiting_to_be_advanced(self) -> None:
        # Measured on the live server: a step refused for a missing consent settled as
        # `denied`, and the fold's "nothing is running" branch relabelled the whole task
        # as merely proposed. A refusal is a stop, and the task must say so.
        session = TaskSession(
            session_id="task_denied",
            actor_id="voice-user",
            request="run the long build",
            steps=[
                TaskStep(
                    index=0,
                    label="Run the long build",
                    action_type="device.terminal.execute",
                    state="denied",
                    error="missing permission(s): ['system.shell']",
                ),
                TaskStep(index=1, label="Read the notes", action_type="device.file.read", state="pending"),
            ],
            status="failed",
        )
        engine = ExecutionEngine(
            ActionRegistry.with_seeds(), PolicyEngine(), [MockProvider()], store=self.store()
        )
        folded = Orchestrator(engine=engine).fold_records(session)
        self.assertEqual(folded.status, "failed")
        self.assertIn("Stopped at step 1 (denied)", folded.summary)


if __name__ == "__main__":
    unittest.main()
