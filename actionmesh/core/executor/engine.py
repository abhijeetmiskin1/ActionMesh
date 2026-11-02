"""C5 — Execution Engine.

Runs an **authorized** graph. The engine decides nothing: it re-validates
structure, re-resolves every action against the registry, and refuses to run a
node the policy engine has not authorized.

Guarantees
----------
* **Authorization is re-checked at dispatch time**, not trusted from planning.
* A node awaiting approval is not executed until a real approved
  ``ApprovalRequest`` exists for its ``action_id``.
* Independent nodes in the same topological layer run **concurrently**.
* Retries happen only for failures the policy lists as retryable, and are
  bounded by the definition's ``retry_policy``.
* Timeouts and cancellation are honoured.
* **Execution success and verification success are reported separately.** An
  action that ran but was not confirmed is ``REQUIRES_USER``, never
  ``SUCCESS``.

What the engine does *not* do: verify business outcomes itself (C6 owns the
verification strategy), compensate on partial failure, or persist state (C15).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from actionmesh.core.events import EventHub
from actionmesh.core.executor.idempotency import IdempotencyLedger
from actionmesh.core.executor.lifecycle import ActionState, is_terminal
from actionmesh.core.executor.serialization import safe_result_view
from actionmesh.core.graph import (
    authorize_graph,
    execution_order,
    graph_from_dict,
    topological_layers,
    validate_graph_structure,
)
from actionmesh.core.observability import Metrics
from actionmesh.core.output import OutputPublisher
from actionmesh.core.output import install as install_output
from actionmesh.core.output import uninstall as uninstall_output
from actionmesh.core.permissions.context import (
    PermissionGrant,
    PolicyContext,
    build_context,
    context_from_payload,
)
from actionmesh.core.permissions.engine import PolicyEngine
from actionmesh.core.registry.registry import ActionRegistry
from actionmesh.core.scope import install_execution, release_execution
from actionmesh.core.storage import SQLiteStore
from actionmesh.core.verification.engine import VerificationEngine, default_engine
from actionmesh.core.verification.recovery import (
    CompensationAttempt,
    CompensationRunner,
    RecoveryPlan,
    RecoveryPlanner,
)
from actionmesh.core.verification.verdicts import VerificationStatus
from actionmesh.protocol.models import (
    Action,
    ActionGraph,
    ApprovalRequest,
    ExecutionRecord,
    FailureKind,
    GraphNode,
    Identity,
    MeshEvent,
    NodeType,
    Policy,
    VerificationResult,
    _utcnow,
)
from actionmesh.protocol.validate import validate_action
from actionmesh.providers.base import ActionProvider, ProviderError, ProviderResult

#: Failure kinds worth another attempt. Everything else — invalid input,
#: authorization, explicit denial, permanent provider failure — fails at once,
#: because retrying only reproduces the same failure.
DEFAULT_RETRYABLE = frozenset(
    {
        FailureKind.TRANSIENT,
        FailureKind.NETWORK,
        FailureKind.RATE_LIMIT,
        FailureKind.TIMEOUT,
    }
)



@dataclass
class NodeOutcome:
    """Per-node result. ``verified`` is independent of ``executed``."""

    node_id: str
    node_type: str
    state: ActionState
    action_type: str = ""
    #: The id approval is matched on. Reported so a `waiting_permission` result can
    #: actually be answered by whoever is being asked.
    action_id: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    verified: bool | None = None
    verification: VerificationResult | None = None
    error: str = ""
    failure_kind: FailureKind = FailureKind.UNKNOWN
    attempts: int = 0
    simulated: bool = True
    #: Which provider ran the node, when one did -- reported so a client can tell a
    #: real laptop action from a simulated one without reading the raw payload.
    provider: str = ""
    #: Wall-clock milliseconds the node spent in dispatch + verification.
    duration_ms: int = 0
    #: What to do about this node if it did not succeed. C6.
    recovery: RecoveryPlan | None = None


@dataclass
class ExecutionResult:
    execution_id: str
    state: ActionState
    nodes: dict[str, NodeOutcome] = field(default_factory=dict)
    error: str = ""
    #: True when any node ran against a simulated provider.
    simulated: bool = True
    #: C16 — every undo that was considered, and what became of it. Populated
    #: only after a forward failure; a successful run has nothing to undo.
    compensations: list[CompensationAttempt] = field(default_factory=list)
    #: True when a side effect may still be standing because an undo was refused
    #: (irreversible action) or the undo itself failed. A human must be told.
    compensation_required: bool = False

    @property
    def succeeded(self) -> bool:
        return self.state is ActionState.SUCCESS

    @property
    def all_verified(self) -> bool:
        """True only if every executed node independently verified."""
        return all(n.verified for n in self.nodes.values() if n.verified is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "state": self.state.value,
            "simulated": self.simulated,
            "error": self.error,
            "compensations": [c.to_dict() for c in self.compensations],
            "compensation_required": self.compensation_required,
            "nodes": {nid: self._node_view(n) for nid, n in sorted(self.nodes.items())},
        }

    @staticmethod
    def _node_view(n: NodeOutcome) -> dict[str, Any]:
        """One node's outcome, safe to hand to a phone.

        The provider's raw ``result`` is deliberately absent: for a file read it holds
        the file, for a terminal command the whole transcript. What is reported instead
        is the *shape* of what happened -- state, whether it verified, how long it took,
        and a metadata-only projection of the payload -- so a client can distinguish a
        success from a failure from an unconfirmed effect without ever seeing content.
        """
        view: dict[str, Any] = {
            "node_type": n.node_type,
            "state": n.state.value,
            "action_type": n.action_type,
            # The id approval is matched on. Without it a client can read a
            # `waiting_permission` result and still have no way to say which
            # action it is approving -- and approval is matched on `action_id`,
            # so the number has to come back from the run that asked for it.
            "action_id": n.action_id,
            "provider": n.provider,
            "attempts": n.attempts,
            "verified": n.verified,
            "simulated": n.simulated,
            "duration_ms": n.duration_ms,
            "error": n.error,
            "failure_kind": n.failure_kind.value,
            "result": safe_result_view(n.result),
        }
        if n.verification is not None:
            # `evidence` is intentionally omitted: it is where a verification strategy
            # may have stashed observed content, and the boolean verdict plus a short
            # reason is all a client needs to distinguish verified from unverified.
            view["verification"] = {
                "verified": n.verification.verified,
                "method": n.verification.method,
                "simulated": n.verification.simulated,
                "detail": (n.verification.detail or "")[:200],
            }
        if n.recovery is not None:
            view["recovery"] = n.recovery.to_dict()
        return view


#: Waiting executions kept in memory. Bounded, because an execution that awaits
#: approval is a small object an unauthenticated caller can create one request at a
#: time, and "wait for the user" must not become a way to grow the process forever.
MAX_PENDING_EXECUTIONS = 100

#: How long a run's ownership lease stays credible. It is renewed on every write,
#: so a live owner never has to think about it; a dead one simply stops renewing,
#: and the next process can reconcile the execution instead of waiting forever for
#: an owner that is gone. It is deliberately short: the honest answer for a run
#: nobody is watching is `interrupted`, not `probably fine`.
EXECUTION_LEASE_SECONDS = 30.0

#: How often a run checks whether someone else asked it to stop.
CANCEL_POLL_SECONDS = 0.25


class ExecutionEngine:
    """Executes an authorized graph. Stateful only in its ledger and records."""

    def __init__(
        self,
        registry: ActionRegistry,
        engine: PolicyEngine,
        providers: list[ActionProvider],
        *,
        ledger: IdempotencyLedger | None = None,
        retryable: frozenset[FailureKind] = DEFAULT_RETRYABLE,
        verifier: VerificationEngine | None = None,
        events: EventHub | None = None,
        store: SQLiteStore | None = None,
        metrics: Metrics | None = None,
        max_pending: int = MAX_PENDING_EXECUTIONS,
        owner_id: str = "",
    ) -> None:
        self.registry = registry
        self.engine = engine
        self.providers = list(providers)
        self.ledger = ledger if ledger is not None else IdempotencyLedger()
        self.retryable = retryable
        #: C6 owns the verification decision; the provider's own claim is only
        #: one input to it.
        self.verifier = verifier if verifier is not None else default_engine()
        self.events = events if events is not None else EventHub()
        self.store = store
        self.metrics = metrics if metrics is not None else Metrics()
        #: Which process is responsible for observing the runs it started. Written
        #: into every record, so "who owns this?" is answerable after this process
        #: is gone -- and so a second process knows it must reconcile, not re-run.
        self.owner_id = owner_id or f"proc-{uuid.uuid4().hex[:8]}"
        self._cancelled: set[str] = set()
        #: The provider call currently in flight for an execution, so a cancellation can
        #: reach it. Without this a cancel is only noticed at a step boundary, which for a
        #: single long command means never — measured in C30, where a cancelled 60s command
        #: printed all 60 seconds.
        self._inflight: dict[str, asyncio.Future] = {}
        self._approvals: dict[str, ApprovalRequest] = {}
        self._records: dict[str, ExecutionRecord] = {}
        self._max_pending = max(1, int(max_pending))
        # Executions that stopped because a human has to answer. Insertion-ordered,
        # so the cap evicts the stalest rather than the newest.
        self._pending: dict[str, tuple[ActionGraph, PolicyContext]] = {}
        #: C35: runs this process inherited from the one that started them. Each has a
        #: watcher, because adoption is not a one-off verdict -- the work may still be
        #: happening, and somebody has to notice when it stops or when a cancellation
        #: arrives for it.
        self._reowned: dict[str, asyncio.Task[None]] = {}
        # Set by `execute` for the duration of one run.
        self._actor: Identity | None = None
        self._grants: list[PermissionGrant] = []
        self._policy: Policy | None = None
        self._allow_high_risk = False

    # -- approvals --------------------------------------------------------
    def submit_approval(self, request: ApprovalRequest) -> ApprovalRequest:
        """Record an approval decision. C8 exposes this over HTTP."""
        self._approvals[request.action_id] = request
        return request

    def approved(self, action_id: str) -> bool:
        request = self._approvals.get(action_id)
        return bool(request and request.status == "approved")

    # -- waiting executions -----------------------------------------------
    def awaiting_approval(self) -> list[dict[str, Any]]:
        """Executions stopped for a human, and the action ids they are waiting on.

        Reported rather than implied. A caller that received `waiting_permission`
        needs to know which id to approve; without this the approval loop has no
        answer, because action ids are minted by the server and never accepted from
        a request.
        """
        out: list[dict[str, Any]] = []
        for execution_id, (graph, context) in self._pending.items():
            out.append(
                {
                    "execution_id": execution_id,
                    "actor": context.actor.id if context.actor else "system",
                    "actions": [
                        {
                            "action_id": node.action.action_id,
                            "action_type": node.action.type,
                            "parameters": dict(node.action.parameters),
                            # The registry's risk, not the one the caller carried: an
                            # omitted `risk_level` defaults to MEDIUM on validation, so
                            # reporting the action's own field labelled a CRITICAL
                            # command execution as "medium" on the prompt a human reads
                            # before approving it. Same raise-only rule as authorization.
                            "risk_level": self.registry.resolve(node.action).effective_risk.value,
                        }
                        for node in graph.nodes
                        if node.action is not None
                    ],
                }
            )
        return out

    def _stamp(self, record: ExecutionRecord, *, terminal: bool = False) -> ExecutionRecord:
        """Say when this was written and who wrote it. The lease is the second half
        of the first: a timestamp without an owner is a clue, not an answer.
        """
        moment = _utcnow()
        record.updated_at = moment
        record.owner_id = self.owner_id
        record.lease_expires_at = None if terminal else moment + timedelta(seconds=EXECUTION_LEASE_SECONDS)
        return record

    def _remember_pending(
        self, execution_id: str, graph: ActionGraph, context: PolicyContext
    ) -> None:
        if execution_id in self._pending:
            self._pending[execution_id] = (graph, context)
            return
        if len(self._pending) >= self._max_pending:
            # Evict the oldest rather than refusing the newest: the oldest has been
            # sitting unanswered the longest, and a stale approval request is the
            # least useful thing to keep.
            stalest = next(iter(self._pending))
            self._pending.pop(stalest, None)
            self.metrics.increment("executions.pending_evicted")
        self._pending[execution_id] = (graph, context)
        self.metrics.increment("executions.awaiting_approval")
        if self.store is not None:
            # The graph and the context are what make the approval answerable later.
            # Without them a restart forgets the run ever stopped, and the honest
            # fallback would be to declare it lost while a human is still deciding.
            self.store.save_body(
                execution_id,
                graph.model_dump_json(),
                context.model_dump_json(),
            )

    async def resume(self, execution_id: str) -> ExecutionResult | None:
        """Re-run a waiting execution, now that an approval may have arrived.

        The stored graph is the one already validated and authorized once — nothing
        is re-read from a caller, which is also what keeps the action ids stable
        enough for an approval to match one. Authorization runs again at dispatch as
        always, so an approval for a *different* action leaves this one waiting.
        """
        stored = self._pending.get(execution_id)
        if stored is None:
            return None
        graph, context = stored
        result = await self.execute(graph, context)
        if result.state is not ActionState.WAITING_PERMISSION:
            self._pending.pop(execution_id, None)
        return result

    # -- cancellation -----------------------------------------------------
    def in_flight(self) -> tuple[str, ...]:
        """Executions with a provider call running right now, on this loop."""
        return tuple(self._inflight)

    def cancel(self, execution_id: str) -> None:
        """Ask an execution to stop, and interrupt the call it is waiting on.

        Idempotent: a second request finds the future already cancelled and does nothing
        more. If nothing is in flight the flag alone is enough, and the step-boundary
        checks stop whatever comes next.
        """
        self._cancelled.add(execution_id)
        call = self._inflight.get(execution_id)
        if call is not None and not call.done():
            # The call may belong to a loop that is already closing (a request whose
            # transport went away, a test that built its own loop). The flag still stands,
            # so the run is cancelled as far as it can be from here, and a cancel request
            # must never turn into a 500 because of whom it raced.
            with contextlib.suppress(RuntimeError):
                call.cancel()

    @staticmethod
    async def _await_cancelled(call: asyncio.Future) -> None:
        """Let an interrupted provider call run its own cleanup, then swallow its error.

        The cancellation is already decided by the time this runs; what is still owed is
        the *completion* of the cancelled call, so the state reported afterwards describes
        a run whose side effects have finished unwinding rather than one mid-flight.
        """
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await call

    def is_cancelled(self, execution_id: str) -> bool:
        return execution_id in self._cancelled

    def _restored_graph(self, raw: dict[str, Any]) -> ActionGraph:
        """Rebuild a stored graph through the same validation an HTTP body passes.

        A graph that sat in our own store is not more trustworthy than one a client
        just sent: skipping `validate_action` left enum fields as bare strings and the
        first thing that touched one -- the risk the approval prompt shows -- broke.
        """
        graph = graph_from_dict(raw)
        sources = {
            node.get("node_id"): node
            for node in raw.get("nodes", [])
            if isinstance(node, dict)
        }
        for node in graph.nodes:
            source = sources.get(node.node_id) or {}
            for attribute in ("action", "compensation"):
                value = getattr(node, attribute)
                if value is None:
                    continue
                checked = validate_action(value)
                # The id is the one exception, and it has to be: `validate_action`
                # refuses to take an identity from untrusted input, and rightly so. This
                # payload came out of our own store, and the id in it is the one the
                # server minted -- for an approval to stay answerable, for the ledger to
                # mean the same thing twice, and for the agent's rows to line up with the
                # nodes they describe. Minting a fresh one breaks all three.
                payload = source.get(attribute)
                stored_id = payload.get("action_id") if isinstance(payload, dict) else None
                if isinstance(stored_id, str) and stored_id:
                    checked = replace(checked, action_id=stored_id)
                setattr(node, attribute, checked)
        return graph

    async def _watch_cancellation(self, execution_id: str) -> None:
        """Turn a cancellation recorded elsewhere into one acted on here.

        A quarter second is the compromise: fast enough that a phone's Cancel is
        not a mystery, cheap enough that a run never becomes a polling problem. When
        the flag appears this process calls its own `cancel`, which is the C31 path
        -- the in-flight provider call is interrupted and its cleanup awaited.
        """
        while True:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
            if self._cancel_requested_elsewhere(execution_id):
                self.cancel(execution_id)
                self.metrics.increment("executions.cancelled_by_other_process")
                return

    def _cancel_requested_elsewhere(self, execution_id: str) -> bool:
        """Has any process recorded a cancellation for this execution?

        Read from the durable record rather than this loop's memory, which is the
        whole point: the request that cancels is not required to be the request that
        runs. Kept separate from `is_cancelled` so a run that finished before the
        flag landed is never rewritten into `cancelled` after the fact.
        """
        if self.store is None:
            return False
        stored = self.store.get_record(execution_id)
        return bool(stored and stored.cancel_requested)

    def request_cancel(self, execution_id: str) -> dict[str, Any]:
        """Ask for an execution to stop, whoever currently owns it.

        Three answers, and they mean different things: `recorded` (the flag is now
        on the execution, and if this process owns the run its in-flight call is
        interrupted too), `already_requested` (idempotent -- a second press changes
        nothing and is not reported as progress), and `terminal` (the run is over;
        it is *not* converted to cancelled, because that would be a story about an
        outcome this system already witnessed).
        """
        record = self.record(execution_id)
        if record is None:
            return {"recorded": False, "already_requested": False, "reason": "unknown"}
        if is_terminal(record.state):
            return {"recorded": False, "already_requested": False, "reason": "terminal"}
        already = bool(record.cancel_requested) or execution_id in self._cancelled
        self.cancel(execution_id)
        # The record this process is holding says the same thing as the record on disk
        # now. Setting only the column would leave `GET /executions/{id}` answering a
        # cancellation nobody asked it about, which is how a flag becomes a rumour.
        record.cancel_requested = True
        if self.store is not None:
            self.store.mark_cancel_requested(execution_id)
            saved = self.store.get_record(execution_id)
            if saved is not None:
                self._records[execution_id] = saved
        return {
            "recorded": not already,
            "already_requested": already,
            "reason": "requested" if not already else "already_requested",
        }

    async def reconcile_interrupted(self) -> dict[str, int]:
        """Decide what a fresh process can honestly say about runs it inherited.

        Called at startup. For every execution the store still calls live:

        * a run still owned by a *live* lease is left alone -- two processes cannot
          both observe one execution;
        * a run parked for a human is **preserved**, with its graph and context put
          back, so the approval can still be answered exactly once;
        * anything else is marked `interrupted`: the action was dispatched, its
          outcome is unknown, and it is never re-dispatched to find out.
        """
        summary = {
            "examined": 0,
            "interrupted": 0,
            "preserved": 0,
            "owned_elsewhere": 0,
            "adopted": 0,
            "settled": 0,
        }
        if self.store is None:
            return summary
        now = _utcnow()
        for record in self.store.list_non_terminal():
            summary["examined"] += 1
            if (
                record.owner_id
                and record.owner_id != self.owner_id
                and record.lease_expires_at is not None
                and record.lease_expires_at > now
            ):
                summary["owned_elsewhere"] += 1
                continue
            if record.state is ActionState.WAITING_PERMISSION:
                stored = self.store.load_body(record.execution_id)
                if stored is not None:
                    graph = self._restored_graph(json.loads(stored[0]))
                    context = context_from_payload(json.loads(stored[1]))
                    self._pending[record.execution_id] = (graph, context)
                    self._records[record.execution_id] = self._stamp(record)
                    self.store.save_record(self._records[record.execution_id])
                    summary["preserved"] += 1
                    continue
            evidence = self._evidence_for(record.execution_id)
            if evidence is not None and evidence.get("state") == "running":
                # The action is not lost: it is happening, on the machine, right now,
                # under this execution id. The previous process was watching it; this one
                # takes over the watching -- the lease, the observers, and the duty to
                # stop it if somebody asks. Nothing is re-dispatched to find out.
                self._records[record.execution_id] = self._stamp(record)
                self.store.save_record(self._records[record.execution_id])
                self._watch_reowned(record.execution_id)
                summary["adopted"] += 1
                self.metrics.increment("executions.adopted")
                self.events.publish(
                    MeshEvent(
                        event_type="execution.reconciled",
                        execution_id=record.execution_id,
                        actor=record.actor_id or "system",
                        payload={
                            "state": record.state.value,
                            "observed": "running",
                            "owner_id": self.owner_id,
                            "detail": str(evidence.get("detail", "")),
                        },
                    )
                )
                continue
            if evidence is not None:
                settled = await self._settle_from_evidence(record, evidence)
                if settled is not None:
                    summary["settled"] += 1
                    continue
            record.state = ActionState.INTERRUPTED
            record.error = (
                "the process that owned this execution is gone; the action had been "
                "dispatched, so its effect is unknown and it was deliberately not "
                "re-run"
            )
            self._records[record.execution_id] = self._stamp(record, terminal=True)
            self.store.save_record(record)
            summary["interrupted"] += 1
            self.metrics.increment("executions.interrupted")
            self.events.publish(
                MeshEvent(
                    event_type="execution.interrupted",
                    execution_id=record.execution_id,
                    actor=record.actor_id or "system",
                    payload={"state": record.state.value, "owner_id": record.owner_id},
                )
            )
        return summary

    def _evidence_for(self, execution_id: str) -> dict[str, Any] | None:
        """Ask whoever might be holding the real work what became of it.

        Optional on every provider, like ``observe``: a mock provider has no machine to
        look at, and answering "no evidence" for it is exactly right. Providers that do
        run real processes -- the laptop agent -- report from their durable ledger.
        """
        for provider in self.providers:
            getter = getattr(provider, "execution_evidence", None)
            if not callable(getter):
                continue
            found = getter(execution_id)
            if found is not None:
                return found
        return None

    def _watch_reowned(self, execution_id: str) -> None:
        existing = self._reowned.get(execution_id)
        if existing is not None and not existing.done():
            return
        self._reowned[execution_id] = asyncio.ensure_future(
            self._supervise_reowned(execution_id)
        )

    async def _supervise_reowned(self, execution_id: str) -> None:
        """Watch a run this process inherited but did not start.

        There is no task to interrupt and no provider call in flight, so the two things
        a watcher can still do are the two that matter: act on a cancellation anyone
        records, and notice when the work ends. Without this, a restarted server could
        *say* a run was still going and then never find out that it stopped.
        """
        ticks = 0
        while self.store is not None:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
            ticks += 1
            record = self.store.get_record(execution_id)
            if record is None or is_terminal(record.state):
                return
            if record.cancel_requested:
                self._cancel_reowned(record)
                return
            evidence = self._evidence_for(execution_id)
            if evidence is None or evidence.get("state") != "running":
                await self._settle_from_evidence(record, evidence or {"actions": []})
                return
            if ticks % 20 == 0:
                # Keep the lease live while the watching is real, so a third process
                # does not conclude this one has gone and reconcile underneath it.
                self._records[execution_id] = self._stamp(record)
                self.store.save_record(self._records[execution_id])

    def _cancel_reowned(self, record: ExecutionRecord) -> None:
        """Forward a cancellation to the processes that actually hold the work.

        The execution is marked cancelled when the machine agrees the children are gone.
        Where a group did not answer, the record says so: the request to stop was
        delivered, whether the side effect stopped with it is a separate claim.
        """
        stopped: list[str] = []
        unconfirmed: list[str] = []
        for provider in self.providers:
            stopper = getattr(provider, "stop_execution", None)
            if not callable(stopper):
                continue
            answer = stopper(record.execution_id)
            stopped.extend(answer.get("stopped", []))
            unconfirmed.extend(answer.get("unconfirmed", []))
        record.state = ActionState.CANCELLED
        record.node_states = dict.fromkeys(record.node_states, ActionState.CANCELLED.value)
        record.error = (
            "cancelled after a restart: stopped "
            + ", ".join(stopped)
            if stopped
            else "cancelled after a restart: no live child was found to stop"
        )
        if unconfirmed:
            record.error += (
                f"; the effect of {', '.join(unconfirmed)} could not be confirmed stopped"
            )
        self._cancelled.add(record.execution_id)
        record.finished_at = _utcnow()
        self._records[record.execution_id] = self._stamp(record, terminal=True)
        if self.store is not None:
            self.store.save_record(self._records[record.execution_id])
            self.store.drop_body(record.execution_id)
        self.events.publish(
            MeshEvent(
                event_type="execution.cancelled",
                execution_id=record.execution_id,
                actor=record.actor_id or "system",
                payload={
                    "state": record.state.value,
                    "stopped": stopped,
                    "unconfirmed": unconfirmed,
                    "after_restart": True,
                },
            )
        )

    async def _settle_from_evidence(
        self, record: ExecutionRecord, evidence: dict[str, Any]
    ) -> ActionState | None:
        """Finish a run from what the machine reports, not from what memory said.

        Returns the state written, or ``None`` when the evidence covers nothing this
        process can attribute to a node -- in which case the honest answer is still
        ``interrupted`` and the caller must not be told a verdict was reached.
        """
        stored = self.store.load_body(record.execution_id) if self.store else None
        if stored is None:
            return None
        graph = self._restored_graph(json.loads(stored[0]))
        by_action: dict[str, tuple[GraphNode, Action]] = {
            node.action.action_id: (node, node.action)
            for node in graph.nodes
            if node.action is not None
        }
        outcomes: dict[str, NodeOutcome] = {}
        for item in evidence.get("actions", []):
            found = by_action.get(item.get("action_id", ""))
            if found is None or item.get("state") == "running":
                continue
            node, action = found
            outcomes[node.node_id] = await self._verify_from_evidence(action, item)
        if not outcomes:
            return None
        unaccounted = [
            node.node_id
            for node in graph.nodes
            if node.action is not None and node.node_id not in outcomes
        ]
        if unaccounted:
            record.error = (
                "the agent reported no evidence for "
                + ", ".join(sorted(unaccounted))
                + "; those nodes were not re-run to find out"
            )
        ran = [o for o in outcomes.values() if o.state is ActionState.SUCCESS]
        if unaccounted:
            record.state = ActionState.INTERRUPTED
        elif len(ran) == len(outcomes):
            record.state = ActionState.SUCCESS
        elif ran:
            record.state = ActionState.PARTIALLY_COMPLETED
        else:
            record.state = next(
                (
                    o.state
                    for o in outcomes.values()
                    if o.state in (ActionState.FAILED, ActionState.REQUIRES_USER)
                ),
                ActionState.FAILED,
            )
        record.node_states = {
            **record.node_states,
            **{nid: outcome.state.value for nid, outcome in outcomes.items()},
        }
        record.results = {
            **record.results,
            **{nid: ExecutionResult._node_view(outcome) for nid, outcome in outcomes.items()},
        }
        record.finished_at = _utcnow()
        self._records[record.execution_id] = self._stamp(record, terminal=True)
        if self.store is not None:
            self.store.save_record(self._records[record.execution_id])
            self.store.drop_body(record.execution_id)
        self.events.publish(
            MeshEvent(
                event_type="execution.completed",
                execution_id=record.execution_id,
                actor=record.actor_id or "system",
                payload={
                    "state": record.state.value,
                    "reconciled": True,
                    "nodes": sorted(outcomes),
                    "detail": str(evidence.get("detail", "")),
                },
            )
        )
        self.metrics.increment(f"executions.state.{record.state.value}")
        return record.state

    async def _verify_from_evidence(
        self, action: Action, item: dict[str, Any]
    ) -> NodeOutcome:
        """Run the normal verification over an exit code this process never watched.

        The claim being verified is the agent's own record -- that a process existed,
        ended, and with which code -- handed to the same independent verifier that a live
        run goes through. Verification that cannot confirm is reported as unconfirmed,
        not as success, exactly as it would be mid-flight.
        """
        provider = self.provider_for(action)
        exit_code = item.get("exit_code")
        result = ProviderResult(
            ok=exit_code == 0,
            provider=provider.name,
            action_id=action.action_id,
            data={"reconciled": True, "exit_code": exit_code},
            simulated=provider.simulated,
        )
        outcome = NodeOutcome(
            node_id="",
            node_type="action",
            state=ActionState.VERIFYING,
            action_type=action.type,
            action_id=action.action_id,
            provider=provider.name,
            simulated=provider.simulated,
            attempts=0,
        )
        try:
            verification = await asyncio.to_thread(
                self.verifier.verify, action, result, provider
            )
        except ProviderError as exc:
            outcome.state = ActionState.FAILED
            outcome.error = str(exc)
            outcome.failure_kind = exc.kind
            return outcome
        outcome.verification = verification.to_result()
        outcome.verified = verification.verified
        outcome.result = dict(result.data)
        if verification.verified:
            outcome.state = ActionState.SUCCESS
        elif verification.status is VerificationStatus.REFUTED:
            outcome.state = ActionState.FAILED
            outcome.error = verification.detail
            outcome.failure_kind = verification.failure_kind
        else:
            outcome.state = ActionState.REQUIRES_USER
            outcome.error = verification.detail or "verification did not confirm"
        return outcome

    def record(self, execution_id: str) -> ExecutionRecord | None:
        return self._records.get(execution_id) or (
            self.store.get_record(execution_id) if self.store is not None else None
        )

    def records(self) -> list[ExecutionRecord]:
        """Every record this deployment can show, newest first.

        In-memory runs come first because they are the ones being watched right now;
        then whatever the durable store holds that memory has never seen. That second
        half is the whole point: after a restart, "does this execution exist?" has to be
        answerable about a run this process did not start.
        """
        merged: dict[str, ExecutionRecord] = {
            record.execution_id: record for record in self._records.values()
        }
        if self.store is not None:
            for record in self.store.list_recent(limit=100):
                merged.setdefault(record.execution_id, record)
        return sorted(
            merged.values(),
            key=lambda record: record.updated_at or record.started_at,
            reverse=True,
        )

    def release_leases(self) -> int:
        """Stop claiming to watch the runs this process is about to leave behind.

        Called on shutdown. Without it a restarted server would sit out the lease and
        report nothing about runs it used to own, which is the silence this checkpoint
        exists to replace. The records stay exactly as they were apart from the lease:
        the next process reconciles them, and can only mark them `interrupted`.
        """
        for task in self._reowned.values():
            if not task.done():
                task.cancel()
        self._reowned.clear()
        if self.store is None:
            return 0
        return self.store.release_leases(self.owner_id)

    # -- provider selection ----------------------------------------------
    def provider_for(self, action: Action) -> ActionProvider:
        for provider in self.providers:
            if provider.supports(action):
                return provider
        raise ProviderError(
            f"no provider supports {action.type!r} (provider={action.provider!r})",
            FailureKind.PERMANENT,
        )

    async def _run_node(
        self, graph: ActionGraph, node: GraphNode, execution_id: str
    ) -> NodeOutcome:
        loop = asyncio.get_running_loop()
        start = loop.time()
        outcome = await self._run_node_inner(graph, node, execution_id)
        outcome.duration_ms = int((loop.time() - start) * 1000)
        return outcome

    async def _run_node_inner(
        self, graph: ActionGraph, node: GraphNode, execution_id: str
    ) -> NodeOutcome:
        outcome = NodeOutcome(
            node_id=node.node_id,
            node_type=node.node_type.value,
            state=ActionState.PLANNED,
        )

        if self.is_cancelled(execution_id):
            outcome.state = ActionState.CANCELLED
            return outcome

        if node.node_type is NodeType.END:
            # A terminal marker has no side effect; its dependencies already
            # succeeded, because the scheduler guarantees that.
            outcome.state = ActionState.SUCCESS
            return outcome

        if node.action is None:
            # Control node (WAIT / CONDITION / APPROVAL): no side effect here.
            outcome.state = ActionState.SUCCESS
            return outcome

        # Re-resolve immediately before dispatch. Planning-time metadata is
        # never trusted at the point of execution.
        resolved = self.registry.resolve(node.action)
        outcome.action_type = resolved.definition.name
        outcome.action_id = resolved.action.action_id

        # --- authorization re-check at dispatch time ---------------------

        approvals = [a for a in self._approvals.values() if a.status == "approved"]
        verdict = self.engine.evaluate(
            build_context(
                resolved.action, resolved.definition, self._actor,
                grants=self._grants, policy=self._policy, approvals=approvals,
                allow_high_risk=self._allow_high_risk,
            )
        )
        # Every dispatch decision is published, because "policy decides" is only a
        # design claim until the decision is observable. EventHub seals published
        # events into the audit chain when one is attached, so this is also what makes
        # a denial part of the tamper-evident record rather than a transient value —
        # and what lets a phone show the reason it was refused.
        self.events.publish(
            MeshEvent(
                event_type="authorization.decided",
                execution_id=execution_id,
                action_id=resolved.action.action_id,
                actor=self._actor.id if self._actor else "system",
                payload={
                    "node_id": node.node_id,
                    "action_type": resolved.action.type,
                    "decision": verdict.decision.value,
                    "authorizes_execution": verdict.authorizes_execution(),
                    "requires_approval": verdict.requires_approval,
                    "effective_risk": verdict.effective_risk.value,
                    "matched_rule": verdict.matched_rule,
                    "missing_permissions": list(verdict.missing_permissions),
                    "reasons": list(verdict.reasons),
                },
            )
        )
        if not verdict.authorizes_execution():
            outcome.state = (
                ActionState.WAITING_PERMISSION
                if verdict.requires_approval
                else ActionState.DENIED
            )
            outcome.error = "; ".join(verdict.reasons)
            outcome.failure_kind = (
                FailureKind.TRANSIENT
                if verdict.requires_approval
                else FailureKind.AUTHORIZATION
            )
            if verdict.requires_approval:
                # A distinct, actionable event: not "policy said no" but "a human
                # must answer, and here is the exact id to answer for". The payload
                # describes the request; it authorizes nothing -- granting still goes
                # through POST /approvals and the server-side consent path.
                self.events.publish(
                    MeshEvent(
                        event_type="approval.required",
                        execution_id=execution_id,
                        action_id=resolved.action.action_id,
                        actor=self._actor.id if self._actor else "system",
                        payload={
                            "node_id": node.node_id,
                            "action_type": resolved.action.type,
                            "risk_level": verdict.effective_risk.value,
                            "reasons": list(verdict.reasons),
                        },
                    )
                )
            return outcome

        provider = self.provider_for(resolved.action)
        outcome.simulated = provider.simulated
        outcome.provider = getattr(provider, "name", "") or type(provider).__name__
        # The node is authorized and about to touch the world. For a long-running
        # laptop action this is the moment the phone stops seeing "queued" and starts
        # seeing "running" -- the payload names the action, never its parameters, so
        # a terminal command's argument vector never rides on the wire.
        self.events.publish(
            MeshEvent(
                event_type="node.started",
                execution_id=execution_id,
                action_id=resolved.action.action_id,
                actor=self._actor.id if self._actor else "system",
                payload={
                    "node_id": node.node_id,
                    "action_type": resolved.action.type,
                    "provider": outcome.provider,
                    "simulated": provider.simulated,
                    "risk_level": verdict.effective_risk.value,
                },
            )
        )

        # --- idempotency -------------------------------------------------
        key = resolved.action.idempotency_key
        duplicate, previous = self.ledger.begin(key)
        if duplicate:
            if self.ledger.state(key) == "running":
                outcome.state = ActionState.REQUIRES_USER
                outcome.error = "idempotency key is already in flight"
                outcome.failure_kind = FailureKind.TRANSIENT
                return outcome
            # A completed claim can safely replay its recorded result without
            # dispatching the side effect a second time.
            outcome.state = ActionState.SUCCESS
            outcome.result = previous if isinstance(previous, dict) else {}
            outcome.verified = True
            return outcome

        max_attempts = max(1, resolved.definition.retry_policy.max_attempts)
        provider_result: ProviderResult | None = None
        last_error, last_kind = "", FailureKind.UNKNOWN

        # Install this node's output publisher for the duration of the provider call.
        # The laptop provider reads it and streams bounded chunks; every other provider
        # ignores it. The publisher is transient (never audited, never replayed) and
        # actor-scoped, so a watcher sees output only if it could already watch this
        # execution.
        publisher = OutputPublisher(
            self.events,
            execution_id=execution_id,
            action_id=resolved.action.action_id,
            node_id=node.node_id,
            actor=self._actor.id if self._actor else "system",
        )
        output_token = install_output(publisher)
        # ... and this node's execution id, for the same reason and by the same route.
        # A provider that starts real work -- a laptop agent launching a child program --
        # stamps the execution it belongs to, so the work stays answerable after the
        # request, the task and this process are all gone.
        execution_token = install_execution(execution_id)
        try:
            for attempt in range(1, max_attempts + 1):
                if self.is_cancelled(execution_id) or self._cancel_requested_elsewhere(execution_id):
                    # A cancellation addressed at the execution, not at this task:
                    # another request or process asked it to stop. Honour it here and
                    # remember it locally, so the summary of this run agrees with the
                    # flag the whole system now carries.
                    self._cancelled.add(execution_id)
                    self.ledger.release(key)
                    outcome.state = ActionState.CANCELLED
                    return outcome
                outcome.attempts = attempt
                call = asyncio.ensure_future(provider.execute(resolved.action))
                self._inflight[execution_id] = call
                try:
                    provider_result = await asyncio.wait_for(
                        call, timeout=max(1.0, resolved.definition.timeout_s)
                    )
                    break
                except TimeoutError:
                    last_error, last_kind = "provider timed out", FailureKind.TIMEOUT
                except ProviderError as exc:
                    last_error, last_kind = str(exc), exc.kind
                except asyncio.CancelledError:
                    # Wait for the interrupted call to finish its own cleanup before
                    # saying anything about its state. The laptop runner group-kills the
                    # child process on cancellation, and reporting "cancelled" while that
                    # kill is still in flight would be a claim about a machine the executor
                    # has not finished watching.
                    await self._await_cancelled(call)
                    if not self.is_cancelled(execution_id):
                        # Not a user's request: the coroutine running this execution was
                        # cancelled from outside (shutdown, disconnected client). Cleaning
                        # up is right; swallowing it and returning a tidy CANCELLED result
                        # would hide that from whoever cancelled.
                        raise
                    # The claim is released exactly as an unverified dispatch releases it,
                    # so a deliberate retry remains possible and a replay as "already done"
                    # is not. The node still carries attempts > 0, which is what makes the
                    # unconfirmed-effect report include it.
                    self.ledger.fail(key)
                    outcome.state = ActionState.CANCELLED
                    outcome.error = (
                        "cancelled while the provider was running; the action was "
                        "dispatched, so its effect is unconfirmed"
                    )
                    outcome.failure_kind = FailureKind.TRANSIENT
                    return outcome
                finally:
                    self._inflight.pop(execution_id, None)
                if last_kind not in self.retryable or attempt == max_attempts:
                    break
                # Exponential backoff, capped so tests stay fast.
                await asyncio.sleep(min(0.01 * (2 ** (attempt - 1)), 0.2))
        except ProviderError as exc:
            last_error, last_kind = str(exc), exc.kind
        finally:
            release_execution(execution_token)
            uninstall_output(output_token)

        if provider_result is None:
            # Failed. The claim is released so a deliberate retry may proceed,
            # but the node is reported as FAILED, never as success.
            self.ledger.fail(key)
            outcome.state = ActionState.FAILED
            outcome.error = last_error
            outcome.failure_kind = last_kind
            return outcome

        outcome.result = dict(provider_result.data)
        outcome.state = ActionState.VERIFYING
        try:
            # C6's verification is a synchronous, in-process inspection of
            # observable state, so it is not awaited. A network-backed
            # strategy (C13) will need an async interface.
            verification = await asyncio.wait_for(
                asyncio.to_thread(
                    self.verifier.verify, resolved.action, provider_result, provider
                ),
                timeout=10.0,
            )
        except TimeoutError:
            # A verification timeout is NOT a success.
            self.ledger.fail(key)
            outcome.state = ActionState.REQUIRES_USER
            outcome.verified = False
            outcome.error = "verification timed out"
            outcome.failure_kind = FailureKind.TIMEOUT
            return outcome
        except ProviderError as exc:
            self.ledger.fail(key)
            outcome.state = ActionState.FAILED
            outcome.error = str(exc)
            outcome.failure_kind = exc.kind
            return outcome

        outcome.verification = verification.to_result()
        outcome.verified = verification.verified
        if verification.verified:
            # Only independently verified work is safe to replay as complete.
            self.ledger.complete(key, provider_result.data)
            outcome.state = ActionState.SUCCESS
        elif verification.status is VerificationStatus.REFUTED:
            # The effect demonstrably did not happen.
            self.ledger.fail(key)
            outcome.state = ActionState.FAILED
            outcome.error = verification.detail
            outcome.failure_kind = verification.failure_kind
        else:
            # Executed, but not confirmed. Deliberately NOT success.
            self.ledger.fail(key)
            outcome.state = ActionState.REQUIRES_USER
            outcome.error = verification.detail or "verification did not confirm"
        return outcome

    def _report_unconfirmed(
        self, result: ExecutionResult, graph: ActionGraph, execution_id: str
    ) -> bool:
        """Record every dispatched-but-unconfirmed node as a possible live effect.

        These are nodes a provider was actually called for, where verification could
        neither confirm nor refute the effect: ``REQUIRES_USER`` (a verification
        timeout) and ``CANCELLED`` after dispatch (C31). Auto-compensating would be
        guessing, so instead each one is reported and ``compensation_required`` is set.
        Returns True if anything was found.
        """
        index = {n.node_id: n for n in graph.nodes}
        found = False
        for nid, node in sorted(result.nodes.items()):
            dispatched = node.state in {
                ActionState.REQUIRES_USER,
                # A call cancelled while it was running may or may not have landed. It is
                # reported for the same reason a verification timeout is: the caller must
                # not read a clean result while an effect might be standing.
                ActionState.CANCELLED,
            }
            if not dispatched or node.attempts == 0:
                # attempts == 0 means the node never reached a provider, so it
                # cannot have left an effect behind.
                continue
            graph_node = index.get(nid)
            action_type = ""
            if graph_node is not None and graph_node.action is not None:
                action_type = graph_node.action.type
            result.compensations.append(
                CompensationAttempt(
                    node_id="",
                    target_node=nid,
                    action_type=action_type,
                    compensated=False,
                    state=node.state.value,
                    error=node.error,
                    reason=(
                        "action was dispatched but not confirmed; its effect may "
                        "still exist and was deliberately not auto-undone"
                    ),
                )
            )
            self.metrics.increment("compensations.unconfirmed")
            self.events.publish(
                MeshEvent(
                    event_type="compensation.unconfirmed",
                    execution_id=execution_id,
                    action_id=(
                        graph_node.action.action_id
                        if graph_node is not None and graph_node.action is not None
                        else ""
                    ),
                    actor=self._actor.id if self._actor else "system",
                    payload={
                        "node_id": nid,
                        "action_type": action_type,
                        "state": node.state.value,
                    },
                )
            )
            found = True
        return found

    # -- compensation (C16) ----------------------------------------------
    async def _compensate(
        self, graph: ActionGraph, result: ExecutionResult, execution_id: str
    ) -> None:
        """Undo the verified side effects of a failed forward run.

        Runs only after a failure, only for nodes that actually produced a
        verified effect, and never for an irreversible action. A rollback is
        damage control: it does **not** upgrade the result to success, and a
        refused or failed undo sets ``compensation_required`` so a human finds
        out that an effect may still be standing.
        """
        verified = {
            nid
            for nid, n in result.nodes.items()
            if n.state is ActionState.SUCCESS and n.verified
        }

        # C16 (fix): a node that was DISPATCHED but not confirmed may still have
        # produced a real-world effect. It must never be auto-undone — we do not
        # know the effect exists — but it must be *reported*, or the caller reads
        # a clean result while an unconfirmed side effect is still standing.
        unconfirmed = self._report_unconfirmed(result, graph, execution_id)
        result.compensation_required = result.compensation_required or unconfirmed

        if not verified:
            return

        runner = CompensationRunner()
        pairs = runner.select(graph, execution_order(graph), verified)
        skipped = runner.skipped(graph, verified)

        for comp_node, target in pairs:
            self.metrics.increment("compensations.attempted")
            outcome = await self._run_node(graph, comp_node, execution_id)
            ok = outcome.state is ActionState.SUCCESS
            # CompensationRunner.select() skips nodes that carry no action, so
            # this is not None in practice; the guard keeps that invariant local.
            target_action = target.action
            attempt = CompensationAttempt(
                node_id=comp_node.node_id,
                target_node=target.node_id,
                action_type=(target_action.type or "") if target_action else "",
                compensated=ok,
                state=outcome.state.value,
                error=outcome.error,
                reason="" if ok else (outcome.error or f"undo ended {outcome.state.value}"),
            )
            result.compensations.append(attempt)
            if not ok:
                self.metrics.increment("compensations.failed")
                result.compensation_required = True
            self.events.publish(
                MeshEvent(
                    event_type="compensation.completed",
                    execution_id=execution_id,
                    action_id=target.action.action_id if target.action else "",
                    actor=self._actor.id if self._actor else "system",
                    payload=attempt.to_dict(),
                )
            )

        for attempt in skipped:
            result.compensations.append(attempt)
            result.compensation_required = True
            self.metrics.increment("compensations.refused")
            self.events.publish(
                MeshEvent(
                    event_type="compensation.refused",
                    execution_id=execution_id,
                    action_id="",
                    actor=self._actor.id if self._actor else "system",
                    payload=attempt.to_dict(),
                )
            )

    # -- graph execution --------------------------------------------------
    async def execute(
        self, graph: ActionGraph, context: PolicyContext
    ) -> ExecutionResult:
        """Execute a graph, but only if the policy engine authorizes it."""
        validate_graph_structure(graph)
        try:
            authorization = authorize_graph(graph, self.engine, self.registry, context)
        except Exception as exc:
            execution_id = f"exec_denied_{abs(hash(str(exc))) & 0xFFFFFF:06x}"
            # A denial is exactly the outcome an audit trail must never miss, so
            # it is published like any other terminal event. Without this, the
            # most security-relevant execution outcome left no trace at all.
            self.events.publish(
                MeshEvent(
                    event_type="execution.denied",
                    execution_id=execution_id,
                    actor=context.actor.id if context.actor else "system",
                    payload={"error": str(exc)},
                )
            )
            return ExecutionResult(
                execution_id=execution_id,
                state=ActionState.DENIED,
                error=str(exc),
            )

        execution_id = authorization.graph_id
        self._actor = context.actor
        self._grants = list(context.grants)
        self._policy = context.policy
        self._allow_high_risk = context.allow_high_risk

        result = ExecutionResult(execution_id=execution_id, state=ActionState.EXECUTING)
        self.metrics.increment("executions.started")
        self._records[execution_id] = self._stamp(
            ExecutionRecord(
                execution_id=execution_id,
                graph_id=graph.graph_id,
                actor_id=context.actor.id if context.actor else "",
                state=ActionState.EXECUTING,
            )
        )
        if self.store is not None:
            # Persisted *before* the first provider call, not after: an execution
            # that only exists in this process's memory cannot be recovered, and an
            # observer arriving after a crash has to be able to ask whether it ran.
            self.store.save_record(self._records[execution_id])
            # The graph and the context too, not just the summary. A process that
            # inherits this run has to know what was asked to answer for it: to show a
            # human the approval still outstanding, to verify what the agent reports
            # about the child it started, and to name what it is refusing to re-run.
            # Restoring it grants nothing -- authorization runs again from scratch.
            self.store.save_body(
                execution_id, graph.model_dump_json(), context.model_dump_json()
            )
        self.events.publish(
            MeshEvent(
                event_type="execution.started",
                execution_id=execution_id,
                actor=context.actor.id if context.actor else "system",
                payload={"graph_id": graph.graph_id},
            )
        )

        index = {n.node_id: n for n in graph.nodes}
        total_actions = sum(1 for n in graph.nodes if n.action is not None)
        # C35: another process can only *write* the cancellation on the execution.
        # Somebody has to act on it, and the process holding the provider call is the
        # only one that can interrupt it.
        watcher = (
            asyncio.create_task(self._watch_cancellation(execution_id))
            if self.store is not None
            else None
        )
        try:
            for layer in topological_layers(graph):
                if self.is_cancelled(execution_id):
                    break
                # Every node in a layer depends only on earlier layers, so the
                # whole layer may run concurrently.
                gathered = await asyncio.gather(
                    *(self._run_node(graph, index[nid], execution_id) for nid in layer),
                    return_exceptions=True,
                )
                for nid, value in zip(layer, gathered, strict=True):
                    if isinstance(value, BaseException):
                        outcome = NodeOutcome(
                            node_id=nid, node_type="error", state=ActionState.FAILED,
                            error=f"{type(value).__name__}: {value}",
                            failure_kind=FailureKind.UNKNOWN,
                        )
                    else:
                        outcome = value
                    if outcome.state in (
                        ActionState.FAILED,
                        ActionState.REQUIRES_USER,
                    ):
                        # C6: say what should happen next, and be honest when
                        # the action cannot be undone.
                        node = index.get(nid)
                        max_attempts = 1
                        if node is not None and node.action is not None:
                            defn = self.registry.get(node.action.type)
                            max_attempts = defn.retry_policy.max_attempts
                        outcome.recovery = RecoveryPlanner().plan(
                            graph, nid, outcome.failure_kind,
                            attempts_used=outcome.attempts,
                            max_attempts=max_attempts,
                        )
                    result.nodes[nid] = outcome
                    self.metrics.increment("nodes.completed")
                    self.metrics.increment(f"nodes.state.{outcome.state.value}")
                    result.simulated = result.simulated and outcome.simulated
                    # Bind the node once: re-subscripting ``index`` inside the
                    # conditional would defeat narrowing of ``.action``.
                    node = index[nid]
                    self.events.publish(
                        MeshEvent(
                            event_type="node.completed",
                            execution_id=execution_id,
                            action_id=(
                                node.action.action_id if node.action is not None else ""
                            ),
                            actor=self._actor.id if self._actor else "system",
                            payload={
                                "node_id": outcome.node_id,
                                "state": outcome.state.value,
                                "action_type": outcome.action_type,
                                "provider": outcome.provider,
                                "attempts": outcome.attempts,
                                "verified": outcome.verified,
                                "simulated": outcome.simulated,
                                "duration_ms": outcome.duration_ms,
                                "error": outcome.error,
                                "failure_kind": outcome.failure_kind.value,
                                # Metadata only -- the raw provider result (file
                                # contents, command output) never enters the stream.
                                "result": safe_result_view(outcome.result),
                            },
                        )
                    )
                done = sum(1 for o in result.nodes.values() if o.node_type != "end")
                self.events.publish(
                    MeshEvent(
                        event_type="execution.progress",
                        execution_id=execution_id,
                        actor=self._actor.id if self._actor else "system",
                        payload={
                            "completed": done,
                            "total": total_actions,
                            "state": ActionState.EXECUTING.value,
                        },
                    )
                )
        except Exception as exc:  # scheduler-level failure
            result.state = ActionState.FAILED
            result.error = f"execution aborted: {type(exc).__name__}: {exc}"
            return result
        finally:
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher

        result.state = self._summarise(result, execution_id)

        # An execution that stopped for a human stays reachable: the answer arrives
        # on a different request, and without the stored graph there is nothing to
        # resume. Anything that finished is forgotten, so a completed run cannot be
        # re-driven.
        if result.state is ActionState.WAITING_PERMISSION:
            self._remember_pending(execution_id, graph, context)
        else:
            self._pending.pop(execution_id, None)
            if self.store is not None and is_terminal(result.state):
                # Nothing left for another process to inherit. The record stays --
                # this is the run's history -- the working copy of its graph does not.
                self.store.drop_body(execution_id)

        # C16: a forward failure may have left verified side effects standing.
        # Undo them, then keep the *forward* verdict — a rollback is not success.
        if result.state is not ActionState.SUCCESS:
            try:
                await self._compensate(graph, result, execution_id)
            except Exception as exc:  # rollback must never mask the original error
                # Recorded as a failed attempt, not just logged: a rollback that
                # could not even start is still a standing side effect.
                result.compensation_required = True
                result.compensations.append(
                    CompensationAttempt(
                        node_id="",
                        target_node="",
                        action_type="",
                        compensated=False,
                        state=ActionState.FAILED.value,
                        error=f"{type(exc).__name__}: {exc}",
                        reason="compensation could not be attempted",
                    )
                )
                result.error = (
                    f"{result.error}; compensation failed: "
                    f"{type(exc).__name__}: {exc}"
                ).strip("; ")

        record = self._stamp(self._records[execution_id], terminal=True)
        record.state = result.state
        record.node_states = {k: v.state.value for k, v in result.nodes.items()}
        # The safe per-node view is persisted on the record too, so a phone that
        # reconnects and reconciles via GET /executions/{id} recovers the same result
        # detail it would have watched stream by -- never the raw provider payload.
        record.results = result.to_dict()["nodes"]
        record.finished_at = _utcnow()
        self.metrics.increment("executions.completed")
        self.metrics.increment(f"executions.state.{result.state.value}")
        if self.store is not None:
            self.store.save_record(record)
        self.events.publish(
            MeshEvent(
                event_type="execution.completed",
                execution_id=execution_id,
                actor=self._actor.id if self._actor else "system",
                payload={"state": result.state.value, "simulated": result.simulated},
            )
        )
        return result

    def _summarise(self, result: ExecutionResult, execution_id: str) -> ActionState:
        if self.is_cancelled(execution_id):
            return ActionState.CANCELLED

        # The END marker is bookkeeping, not work. Including it would make a
        # single failed action look "partially completed" merely because the
        # terminator succeeded.
        outcomes = [n for n in result.nodes.values() if n.node_type != "end"]
        if not outcomes:
            return ActionState.FAILED

        blocked = [n for n in outcomes if n.state is ActionState.DENIED]
        if blocked:
            result.error = f"denied: {', '.join(sorted(n.node_id for n in blocked))}"
            return ActionState.DENIED

        failed = [n for n in outcomes if n.state is ActionState.FAILED]
        waiting = [n for n in outcomes if n.state is ActionState.WAITING_PERMISSION]
        needs_user = [n for n in outcomes if n.state is ActionState.REQUIRES_USER]
        ran = [n for n in outcomes if n.state is ActionState.SUCCESS]

        if failed and ran:
            result.error = f"partial: {len(ran)} ok, {len(failed)} failed"
            return ActionState.PARTIALLY_COMPLETED
        if failed:
            result.error = failed[0].error
            return ActionState.FAILED
        if waiting:
            result.error = "awaiting user approval"
            return ActionState.WAITING_PERMISSION
        if needs_user:
            # Executed but unconfirmed. NOT a success — the user must be told.
            result.error = needs_user[0].error or "verification did not confirm"
            return ActionState.REQUIRES_USER
        if len(ran) == len(outcomes):
            return ActionState.SUCCESS
        result.error = "incomplete"
        return ActionState.PARTIALLY_COMPLETED
