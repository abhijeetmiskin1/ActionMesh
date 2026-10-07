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
from dataclasses import dataclass, field
from typing import Any

from actionmesh.core.events import EventHub
from actionmesh.core.executor.idempotency import IdempotencyLedger
from actionmesh.core.executor.lifecycle import ActionState
from actionmesh.core.graph import (
    authorize_graph,
    execution_order,
    topological_layers,
    validate_graph_structure,
)
from actionmesh.core.observability import Metrics
from actionmesh.core.permissions.context import (
    PermissionGrant,
    PolicyContext,
    build_context,
)
from actionmesh.core.permissions.engine import PolicyEngine
from actionmesh.core.registry.registry import ActionRegistry
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
)
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
    result: dict[str, Any] = field(default_factory=dict)
    verified: bool | None = None
    verification: VerificationResult | None = None
    error: str = ""
    failure_kind: FailureKind = FailureKind.UNKNOWN
    attempts: int = 0
    simulated: bool = True
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
            "nodes": {
                nid: {
                    "state": n.state.value,
                    "action_type": n.action_type,
                    "attempts": n.attempts,
                    "verified": n.verified,
                    "simulated": n.simulated,
                    "error": n.error,
                    "failure_kind": n.failure_kind.value,
                }
                for nid, n in sorted(self.nodes.items())
            },
        }


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
        self._cancelled: set[str] = set()
        self._approvals: dict[str, ApprovalRequest] = {}
        self._records: dict[str, ExecutionRecord] = {}
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

    # -- cancellation -----------------------------------------------------
    def cancel(self, execution_id: str) -> None:
        self._cancelled.add(execution_id)

    def is_cancelled(self, execution_id: str) -> bool:
        return execution_id in self._cancelled

    def record(self, execution_id: str) -> ExecutionRecord | None:
        return self._records.get(execution_id) or (
            self.store.get_record(execution_id) if self.store is not None else None
        )

    def records(self) -> list[ExecutionRecord]:
        """Return in-process execution records newest first."""
        return list(reversed(list(self._records.values())))

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

        # --- authorization re-check at dispatch time ---------------------

        approvals = [a for a in self._approvals.values() if a.status == "approved"]
        verdict = self.engine.evaluate(
            build_context(
                resolved.action, resolved.definition, self._actor,
                grants=self._grants, policy=self._policy, approvals=approvals,
                allow_high_risk=self._allow_high_risk,
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
            return outcome

        provider = self.provider_for(resolved.action)
        outcome.simulated = provider.simulated

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

        try:
            for attempt in range(1, max_attempts + 1):
                if self.is_cancelled(execution_id):
                    self.ledger.release(key)
                    outcome.state = ActionState.CANCELLED
                    return outcome
                outcome.attempts = attempt
                try:
                    provider_result = await asyncio.wait_for(
                        provider.execute(resolved.action),
                        timeout=max(1.0, resolved.definition.timeout_s),
                    )
                    break
                except TimeoutError:
                    last_error, last_kind = "provider timed out", FailureKind.TIMEOUT
                except ProviderError as exc:
                    last_error, last_kind = str(exc), exc.kind
                if last_kind not in self.retryable or attempt == max_attempts:
                    break
                # Exponential backoff, capped so tests stay fast.
                await asyncio.sleep(min(0.01 * (2 ** (attempt - 1)), 0.2))
        except ProviderError as exc:
            last_error, last_kind = str(exc), exc.kind

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

        These are ``REQUIRES_USER`` nodes: the provider was called, so the action
        may exist in the world, but verification could neither confirm nor refute
        it. Auto-compensating would be guessing, so instead each one is reported
        and ``compensation_required`` is set. Returns True if anything was found.
        """
        index = {n.node_id: n for n in graph.nodes}
        found = False
        for nid, node in sorted(result.nodes.items()):
            if node.state is not ActionState.REQUIRES_USER or node.attempts == 0:
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
        self._records[execution_id] = ExecutionRecord(
            execution_id=execution_id, graph_id=graph.graph_id, state=ActionState.EXECUTING
        )
        if self.store is not None:
            self.store.save_record(self._records[execution_id])
        self.events.publish(
            MeshEvent(
                event_type="execution.started",
                execution_id=execution_id,
                actor=context.actor.id if context.actor else "system",
                payload={"graph_id": graph.graph_id},
            )
        )

        index = {n.node_id: n for n in graph.nodes}
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
                                "attempts": outcome.attempts,
                                "verified": outcome.verified,
                                "simulated": outcome.simulated,
                                "error": outcome.error,
                                "failure_kind": outcome.failure_kind.value,
                            },
                        )
                    )
        except Exception as exc:  # scheduler-level failure
            result.state = ActionState.FAILED
            result.error = f"execution aborted: {type(exc).__name__}: {exc}"
            return result

        result.state = self._summarise(result, execution_id)

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

        record = self._records[execution_id]
        record.state = result.state
        record.node_states = {k: v.state.value for k, v in result.nodes.items()}
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
