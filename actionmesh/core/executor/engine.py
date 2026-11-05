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

from actionmesh.core.devices import (
    EXECUTION_KINDS,
    DeviceInfo,
    DeviceRegistry,
    DeviceUnavailable,
)
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
from actionmesh.core.identity import IdentityError
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
from actionmesh.core.permissions.risk import risk_rank
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
    RiskLevel,
    VerificationResult,
    _utcnow,
    new_id,
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
    #: C36 — the computer this run was routed to, resolved before dispatch. Empty is
    #: the local machine, which is what every run before C36 meant. Carried in the
    #: result, not only in the record, so a caller learns *where* it ran from the same
    #: object that tells it *whether* it ran.
    device_id: str = ""

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
            "device_id": self.device_id,
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

#: How long a run's ownership lease stays credible. A live run renews it every
#: ``CANCEL_POLL_SECONDS``; a dead one simply stops renewing, and the next process
#: can reconcile the execution instead of waiting forever for an owner that is gone.
#:
#: The window has to be comfortably longer than the time between renewals *and*
#: long enough that a brief stall on a loaded laptop is not mistaken for a death.
#: It is deliberately not longer than a few of these: the honest answer for a run
#: nobody is watching is `interrupted`, not `probably fine`.
EXECUTION_LEASE_SECONDS = 30.0

#: How often a run checks whether someone else asked it to stop.
CANCEL_POLL_SECONDS = 0.25

#: How often a server re-examines runs whose owner's lease has lapsed.
#:
#: `reconcile_interrupted` runs once at startup, which leaves a hole: a process killed
#: *hard* (SIGKILL, OOM, a pulled plug) leaves a lease that has not expired yet, so the
#: process that starts one second later correctly declines to touch it -- and then
#: nothing ever looks again. The run sits at `executing` forever, and a client asking
#: "did my command finish?" is told "still going" by a system with no owner for it.
#: This sweep closes that: the moment a lease lapses, the run is reconciled by the
#: same rules as startup. It re-dispatches nothing.
LEASE_SWEEP_SECONDS = 5.0


def _unobserved_outcome(node: GraphNode, action: Action) -> NodeOutcome:
    """A node whose action ended with nobody watching it end.

    The evidence proved the process is gone and proved nothing about what it did. That
    is neither success nor failure: ``interrupted`` is the state that says so, and the
    node is given no verdict *and* no exit code, because both would be inventions.
    """
    return NodeOutcome(
        node_id=node.node_id,
        node_type="action",
        state=ActionState.INTERRUPTED,
        action_type=action.type,
        action_id=action.action_id,
        verified=None,
        attempts=0,
        error=(
            "the process ended without reporting an exit code, so whether the "
            "action took effect is unknown"
        ),
        failure_kind=FailureKind.UNKNOWN,
    )


def _device_reason(target_device_id: str, exc: BaseException) -> str:
    """One sentence for "why this run did not go to that computer".

    The device id is quoted from the graph, never from the registry, so a refusal
    cannot confirm which ids exist. The underlying reason is preserved because an
    owner asking "why didn't my Linux build run?" needs the real one.
    """
    detail = str(exc).strip() or type(exc).__name__
    return f"target device {target_device_id!r} refused the execution: {detail}"


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
        devices: DeviceRegistry | None = None,
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
        #: C36 — the computers this engine may route to. ``None`` keeps a bare engine
        #: (a unit test, an embedded use) exactly as it was, but a graph that *names* a
        #: target device is refused rather than quietly run here: a routing request that
        #: cannot be honoured is not permission to use the local machine instead.
        self.devices = devices
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
        #: The last reason the periodic sweep failed, if it ever has. Kept as a plain
        #: attribute (not a metric label) so a support log can say *why*.
        self.last_sweep_error: str | None = None
        # Set by `execute` for the duration of one run.
        self._actor: Identity | None = None
        self._grants: list[PermissionGrant] = []
        self._policy: Policy | None = None
        self._allow_high_risk = False
        #: C36 — the device this run was resolved to, and the one the local provider
        #: serves. Both belong to the run, so a concurrent `execute` (the orchestrator
        #: and a direct request share one engine) cannot retarget a node mid-flight.
        self._target: DeviceInfo | None = None
        #: C36 — which registered computer the in-process providers actually are. Set
        #: by whoever installs the real agent; empty means "this process cannot prove
        #: it is any particular registered device", which is a reason to refuse a named
        #: target, never a licence to assume the target is this machine.
        self.local_device_id = ""

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

        The same tick renews this process's lease while the run is genuinely in
        flight. Without that, a command longer than the lease makes a live run look
        abandoned: another server would be entitled to reconcile it, and a restarted
        one would refuse to adopt it because the lease looked stale. The lease means
        "I am still watching this", and the watcher is the only thing that knows.
        """
        while True:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
            if self._cancel_requested_elsewhere(execution_id):
                self.cancel(execution_id)
                self.metrics.increment("executions.cancelled_by_other_process")
                return
            self._renew_lease(execution_id)

    def _renew_lease(self, execution_id: str) -> None:
        """Push this process's claim on a live run forward, if it still owns it.

        Cheap and idempotent: the in-memory record is authoritative for a run this
        process started, and the write only happens when the lease is at least
        half gone. Re-writing every tick would turn a watchdog into a disk load.
        """
        record = self._records.get(execution_id)
        if record is None or self.store is None:
            return
        now = _utcnow()
        lease = record.lease_expires_at
        if lease is not None and lease - now > timedelta(
            seconds=EXECUTION_LEASE_SECONDS / 2
        ):
            return
        self._records[execution_id] = self._stamp(record)
        self.store.save_record(self._records[execution_id])

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
            await self._reconcile_one(record, summary)
        return summary

    async def sweep_lapsed_leases(self) -> dict[str, int]:
        """Reconcile runs whose owner's lease has run out, in a process that is up.

        Startup reconciliation answers "what did the process before me leave behind?".
        It cannot answer "what did the process *that was running alongside me* leave
        behind when it died?", because the answer changes while this process lives: a
        lease that was still valid at startup lapses a few seconds later.

        This is the same decision as ``reconcile_interrupted``, reached at the moment
        the evidence changes -- not a second set of rules.

        A lease that has not lapsed yet is somebody else's claim, and two processes must
        never observe one execution -- so the sweep waits. It does not wait blindly:
        the run in flight renews its lease every quarter second, so a lease that stops
        moving is a lease whose owner has gone, and it lapses on its own. Runs owned by
        this process are never touched here.

        Everything else is adopted, settled from the machine's own evidence, preserved
        for a human, or honestly marked ``interrupted``. **Nothing is ever
        re-dispatched.**
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
            if record.owner_id == self.owner_id:
                # Ours, and this process is up and watching it. Note that ownership is
                # the check, not presence in memory: every run this process owns has a
                # fresh lease, and an owner that had lost track of its own run would
                # still be the only process entitled to observe it.
                continue
            if record.lease_expires_at is not None and record.lease_expires_at > now:
                # Somebody else is still saying they are watching it. A live owner
                # renews before this lapses; a dead one lets it go, and then the state
                # says what it always was. Neither is resolved here.
                summary["owned_elsewhere"] += 1
                continue
            await self._reconcile_one(record, summary)
        return summary

    async def _reconcile_one(
        self, record: ExecutionRecord, summary: dict[str, int]
    ) -> None:
        """The startup decision, applied to one record. Shared, not copied."""
        if self.store is None:
            return
        if record.execution_id in self._reowned and not self._reowned[
            record.execution_id
        ].done():
            return
        if record.state is ActionState.WAITING_PERMISSION:
            if self._pending.get(record.execution_id) is not None:
                # Already restored by startup, and still genuinely parked: its
                # approval has not been answered yet, so there is nothing to decide.
                return
            stored = self.store.load_body(record.execution_id) if self.store else None
            if stored is not None:
                graph = self._restored_graph(json.loads(stored[0]))
                context = context_from_payload(json.loads(stored[1]))
                self._pending[record.execution_id] = (graph, context)
                self._records[record.execution_id] = self._stamp(record)
                self.store.save_record(self._records[record.execution_id])
                summary["preserved"] += 1
                return
        evidence = self._evidence_for(record.execution_id)
        if evidence is not None and evidence.get("state") == "running":
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
            return
        if evidence is not None:
            settled = await self._settle_from_evidence(record, evidence)
            if settled is not None:
                summary["settled"] += 1
                return
        await self._mark_interrupted(record)
        summary["interrupted"] += 1

    async def _mark_interrupted(self, record: ExecutionRecord) -> None:
        """Say the only thing that is true: it was dispatched, and nobody watched it end."""
        record.state = ActionState.INTERRUPTED
        record.error = (
            "the process that owned this execution is gone; the action had been "
            "dispatched, so its effect is unknown and it was deliberately not "
            "re-run"
        )
        record.finished_at = _utcnow()
        self._records[record.execution_id] = self._stamp(record, terminal=True)
        if self.store is not None:
            self.store.save_record(self._records[record.execution_id])
            self.store.drop_body(record.execution_id)
        self.metrics.increment("executions.interrupted")
        self.events.publish(
            MeshEvent(
                event_type="execution.interrupted",
                execution_id=record.execution_id,
                actor=record.actor_id or "system",
                payload={"state": record.state.value, "owner_id": record.owner_id},
            )
        )

    async def run_lease_sweeper(self, stop: asyncio.Event) -> None:
        """Keep reconciling while this process is up. Started by the lifespan."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=LEASE_SWEEP_SECONDS)
                return
            except TimeoutError:
                pass
            try:
                await self.sweep_lapsed_leases()
            except Exception as exc:  # pragma: no cover - a sweep must not kill the server
                # A store that is briefly locked or a provider that raises must not
                # take the control plane down; the next sweep tries again. The failure
                # is counted rather than swallowed silently, so a persistent fault is
                # visible in metrics instead of only as runs that never settle.
                self.metrics.increment("executions.sweep_failed")
                self.last_sweep_error = f"{type(exc).__name__}: {exc}"

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
                settled = await self._settle_from_evidence(
                    record, evidence or {"actions": []}
                )
                if settled is None:
                    # The evidence says the work is over, but does not cover the nodes
                    # this run ordered -- the stored graph is gone, or it names an
                    # action the ledger has never heard of. Reporting "still running"
                    # here would be a lie that never expires; this process has taken
                    # responsibility for the run, so it must also end it honestly.
                    # ``interrupted`` is the state for exactly this: dispatched, effect
                    # unknown, deliberately not re-run.
                    await self._mark_interrupted(record)
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
            if item.get("exit_code") is None:
                # The action ended, but nothing watched it end. An agent that was
                # killed mid-run can see that a pid is gone and cannot know why, so
                # there is no code here to verify against -- and a node with no
                # verdict is not a success and not a failure.
                outcomes[node.node_id] = _unobserved_outcome(node, action)
                continue
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
        unknown = [
            nid for nid, o in outcomes.items() if o.state is ActionState.INTERRUPTED
        ]
        if unaccounted or unknown:
            # One node whose effect is unknown makes the whole run's outcome
            # unknown. Saying ``failed`` here would claim the action did not
            # happen, which is the one thing this evidence cannot establish.
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
        """The provider that may perform ``action`` for the run in progress.

        Refuses, before any adapter is consulted, an action that belongs to another
        computer. The local adapter is an adapter for *this machine*: handing it a graph
        addressed elsewhere would perform the side effect on the wrong computer, which
        is the one failure C36 says must never happen. "Do not route an execution to
        another device just because the requested device is offline" has a second half
        -- do not route it *here* either.
        """
        target = self._target
        if target is not None and not self._serves_locally(target):
            raise ProviderError(
                f"action {action.type!r} is addressed to device {target.device_id!r}, "
                "which is not this machine; it cannot be run by the local agent",
                FailureKind.PERMANENT,
            )
        for provider in self.providers:
            if provider.supports(action):
                return provider
        raise ProviderError(
            f"no provider supports {action.type!r} (provider={action.provider!r})",
            FailureKind.PERMANENT,
        )

    # -- device routing (C36) ---------------------------------------------
    def resolve_device(self, target_device_id: str) -> DeviceInfo | None:
        """Resolve a graph's target device, or raise with a reason a user can read.

        This is the registry resolution leg of the pipeline. It runs before the record
        is written and before any node is dispatched, because an execution that names a
        computer has to fail as a whole when that computer cannot take it -- not
        per-node, and not by falling back to the machine we happen to be on.

        Raises ``KeyError`` (with ``DeviceUnavailable`` for the offline case),
        ``IdentityError`` for an unattributed device, and ``PermissionError`` for a
        device that is present but may not run this. Empty means "the local machine",
        which is what every run before C36 meant.
        """
        wanted = (target_device_id or "").strip()
        if not wanted:
            return None
        if self.devices is None:
            raise KeyError(
                f"this execution is addressed to device {wanted!r}, but no device "
                "registry is attached; a named target is never run locally instead"
            )
        device = self.devices.require(wanted)
        # A phone is a control surface. Targeting one as an execution device is a
        # category error, not a permission to grant -- the registry records the role,
        # routing respects it.
        if device.kind not in EXECUTION_KINDS:
            raise PermissionError(
                f"device {wanted!r} is a {device.kind.value} and cannot execute actions"
            )
        return device

    def _serves_locally(self, device: DeviceInfo) -> bool:
        """Is this the machine the in-process providers actually run on?

        Only a declared device id counts -- the engine's own, or the one a real agent
        adapter carries. A device record that merely *looks* like this host (same
        platform, same kind, even the same name) is not proof, and guessing on
        resemblance is how a command lands on the wrong computer. When nothing declared
        itself, a named target is refused instead.
        """
        local = self.local_device_id or next(
            (
                str(getattr(provider, "device_id", "") or "")
                for provider in self.providers
                if getattr(provider, "device_id", "")
            ),
            "",
        )
        return bool(local) and device.device_id == local

    def _device_admits_risk(self, device: DeviceInfo, risk: RiskLevel) -> None:
        """Refuse a node whose risk is above the target's ceiling.

        The device ceiling is the *target-side* limit and the credential ceiling is
        the *actor-side* one; both are checked, and neither substitutes for the other.
        """
        if risk_rank(risk) > risk_rank(device.risk_ceiling):
            raise PermissionError(
                f"device {device.device_id!r} has risk ceiling "
                f"{device.risk_ceiling.value}; this action is {risk.value}-risk"
            )

    def _refuse_device(
        self,
        execution_id: str,
        graph: ActionGraph,
        context: PolicyContext,
        exc: BaseException,
    ) -> ExecutionResult:
        """Fail an execution whose named computer cannot take it.

        Deliberately one answer for every reason -- unknown, offline, unattributed,
        not allowed -- because the caller does not get to probe which computers exist
        by reading different refusals. The *reason* still reaches the event stream and
        the operator's own records, which is where an owner finds out why their run
        did not happen.
        """
        wanted = graph.target_device_id
        reason = _device_reason(wanted, exc)
        actor = context.actor.id if context.actor else "system"
        self.metrics.increment("executions.device_refused")
        self.events.publish(
            MeshEvent(
                event_type="execution.device_refused",
                execution_id=execution_id,
                actor=actor,
                payload={
                    "target_device_id": wanted,
                    "reason": reason,
                    # Categorised so a dashboard can say "offline" without parsing a
                    # sentence, and so a test can assert the condition, not the prose.
                    "cause": (
                        "offline"
                        if isinstance(exc, DeviceUnavailable)
                        else "unattributed"
                        if isinstance(exc, IdentityError)
                        else "not_permitted"
                        if isinstance(exc, PermissionError)
                        else "unknown"
                    ),
                },
            )
        )
        self.events.publish(
            MeshEvent(
                event_type="execution.denied",
                execution_id=execution_id,
                actor=actor,
                payload={"error": reason, "target_device_id": wanted},
            )
        )
        # Named after the refusal the same way the denial path is, so a caller can
        # look up why the run it asked for did not happen.
        self._records[execution_id] = self._stamp(
            ExecutionRecord(
                execution_id=execution_id,
                graph_id=graph.graph_id,
                actor_id=context.actor.id if context.actor else "",
                state=ActionState.DENIED,
                error=reason,
                device_id=wanted,
                finished_at=_utcnow(),
            ),
            terminal=True,
        )
        if self.store is not None:
            self.store.save_record(self._records[execution_id])
        return ExecutionResult(
            execution_id=execution_id,
            state=ActionState.DENIED,
            error=reason,
            device_id=wanted,
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

        # --- device admission at dispatch time ---------------------------
        # The target was resolved for the whole execution, but the *risk* is per
        # node: a graph may mix a read with a restart. A device whose owner set a
        # LOW ceiling refuses the restart and still allows the read, and it refuses
        # it here rather than after an approval prompt that cannot help.
        if self._target is not None:
            try:
                self._device_admits_risk(self._target, resolved.effective_risk)
            except PermissionError as exc:
                outcome.state = ActionState.DENIED
                outcome.error = str(exc)
                outcome.failure_kind = FailureKind.AUTHORIZATION
                self.events.publish(
                    MeshEvent(
                        event_type="authorization.decided",
                        execution_id=execution_id,
                        action_id=resolved.action.action_id,
                        actor=self._actor.id if self._actor else "system",
                        payload={
                            "node_id": node.node_id,
                            "action_type": resolved.action.type,
                            "decision": "deny",
                            "authorizes_execution": False,
                            "requires_approval": False,
                            "effective_risk": resolved.effective_risk.value,
                            "device_id": self._target.device_id,
                            "device_risk_ceiling": self._target.risk_ceiling.value,
                            "reasons": [str(exc)],
                        },
                    )
                )
                return outcome

        # --- authorization re-check at dispatch time ---------------------

        # A recorded rejection is a decision, not the absence of one. Without this
        # the run re-evaluates to the same "requires approval", parks again, and
        # `POST /approvals {status: rejected}` becomes a no-op that leaves the
        # execution waiting forever — listed as needing an answer nobody can give.
        # Rejecting must end the run at that node, the same way a DENY does.
        rejection = self._approvals.get(resolved.action.action_id)
        if rejection is not None and rejection.status == "rejected":
            # A user's refusal is a decision, so the node reads DENIED. Marking it
            # FAILED would also put it in the retry/escalation path, which is the
            # opposite of what "no" means: nothing is retried, nothing is escalated.
            outcome.state = ActionState.DENIED
            outcome.error = "rejected by the user"
            outcome.failure_kind = FailureKind.AUTHORIZATION
            self.events.publish(
                MeshEvent(
                    event_type="authorization.decided",
                    execution_id=execution_id,
                    action_id=resolved.action.action_id,
                    actor=self._actor.id if self._actor else "system",
                    payload={
                        "node_id": node.node_id,
                        "action_type": resolved.action.type,
                        "decision": "deny",
                        "authorizes_execution": False,
                        "requires_approval": False,
                        "effective_risk": resolved.effective_risk.value,
                        "reasons": ["rejected by the user"],
                    },
                )
            )
            return outcome

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
            # The machine this node was routed to, so a subscriber reading two
            # computers' streams side by side can tell them apart without consulting
            # the execution record. Empty means the local machine, as everywhere else.
            device_id=getattr(self._target, "device_id", "") or "",
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
    async def prepare_execution(
        self, graph: ActionGraph, context: PolicyContext
    ) -> tuple[ActionGraph, PolicyContext]:
        """Authorize, name, persist and announce a graph -- then stop.

        ``execute`` used to do this and the dispatch in one unbroken await, so a
        caller could not learn the execution id until the work it named was already
        over. That is fine for a caller that only wants the answer, and fatal for a
        caller that has to write the id down: a task session pointing at a run has to
        record the run *before* it starts, because the crash that strands the session
        is by definition the one that leaves nobody alive to record it afterwards.

        Splitting it here does not weaken anything. Authorization still runs in full,
        against the caller's own context, before anything is named; the record and its
        body are still persisted before the first provider call; and ``run_execution``
        authorizes again at dispatch, exactly as it did when the two halves were one
        function.

        The graph's ``graph_id`` is assigned here when the caller left it blank, so
        naming a run and running it always refer to the same execution. A graph that
        already carries an id keeps it -- a replay is the same execution, not a new one.
        """
        validate_graph_structure(graph)
        try:
            authorize_graph(graph, self.engine, self.registry, context)
        except Exception as exc:
            # A denial is a real outcome with a real id, and it is why this returns a
            # graph rather than raising: a caller that names runs needs the name of
            # the run that was refused just as much as the name of one that was
            # allowed. ``execute`` turns it into a DENIED result.
            if not graph.graph_id:
                graph.graph_id = f"exec_denied_{abs(hash(str(exc))) & 0xFFFFFF:06x}"
            return graph, context

        if not graph.graph_id:
            graph.graph_id = new_id("graph")
        return graph, context

    async def execute(
        self, graph: ActionGraph, context: PolicyContext
    ) -> ExecutionResult:
        """Execute a graph, but only if the policy engine authorizes it."""
        graph, context = await self.prepare_execution(graph, context)
        return await self.run_execution(graph, context)

    async def run_execution(
        self, graph: ActionGraph, context: PolicyContext
    ) -> ExecutionResult:
        """Dispatch a graph that ``prepare_execution`` has already named.

        Authorization runs again here, from the same ``graph`` and ``context`` that
        were authorized a moment ago. That is not redundant: the policy engine is the
        only thing standing between a persisted name and a side effect, and a name is
        not an authority. A graph that was refused is refused again -- and returns the
        same denied result ``execute`` always returned, so callers cannot tell which
        half refused them.
        """
        execution_id = graph.graph_id
        try:
            authorize_graph(graph, self.engine, self.registry, context)
        except Exception as exc:
            # Same id the denial was named with in prepare_execution, so a caller
            # holding that name can look up why the run it asked for did not happen.
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

        self._actor = context.actor
        self._grants = list(context.grants)
        self._policy = context.policy
        self._allow_high_risk = context.allow_high_risk

        # --- device resolution, before anything is named or dispatched ------
        # C36: the graph names a computer, not a place to try. Resolution happens
        # here rather than per node so an offline, unknown or unattributed target
        # refuses the whole execution with one honest reason, and so a partially
        # dispatched graph cannot exist for a machine that was never reachable.
        target: DeviceInfo | None
        try:
            target = self.resolve_device(graph.target_device_id)
        except (KeyError, PermissionError, IdentityError) as exc:
            return self._refuse_device(execution_id, graph, context, exc)
        self._target = target

        result = ExecutionResult(
            execution_id=execution_id,
            state=ActionState.EXECUTING,
            device_id=target.device_id if target is not None else "",
        )
        self.metrics.increment("executions.started")
        self._records[execution_id] = self._stamp(
            ExecutionRecord(
                execution_id=execution_id,
                graph_id=graph.graph_id,
                actor_id=context.actor.id if context.actor else "",
                state=ActionState.EXECUTING,
                device_id=target.device_id if target is not None else "",
                device_name=target.name if target is not None else "",
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
            # Prefer the node's own reason when it has one. A rejection is recorded as
            # DENIED with "rejected by the user"; overwriting that with "denied: a1"
            # would erase the only part of the record that says *who* refused and why.
            reasons = [n.error for n in blocked if n.error]
            result.error = "; ".join(reasons) if reasons else (
                f"denied: {', '.join(sorted(n.node_id for n in blocked))}"
            )
            return ActionState.DENIED

        # A rejection is a decision to refuse, and it has to read as one. Scoped to
        # the exact sentence a rejection produces: an AUTHORIZATION-kind failure is
        # not automatically a refusal — a laptop refusing a command it will not run
        # (`rm`, say) is a capability refusal, and re-labelling that DENIED would
        # collapse "the human said no" into "the machine could not", the distinction
        # this platform exists to keep.
        refused = [
            n for n in outcomes
            if n.state is ActionState.FAILED and n.error == "rejected by the user"
        ]
        if refused:
            result.error = refused[0].error
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
