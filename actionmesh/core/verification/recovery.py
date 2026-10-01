"""C6 — Recovery.

What to do when execution or verification fails. The important property is that
recovery is **classified**, never blanket: retrying an authorization failure
just reproduces it, and "rolling back" an irreversible operation is a lie.

Three responses:

* ``RETRY``     — the failure is transient and the operation is safe to repeat.
* ``COMPENSATE``— a compensating action exists and the effect is reversible.
* ``ESCALATE``  — a human must decide. The default for anything uncertain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from actionmesh.protocol.models import (
    ActionGraph,
    FailureKind,
    GraphNode,
    NodeType,
)


class Recovery(StrEnum):
    RETRY = "retry"
    COMPENSATE = "compensate"
    ESCALATE = "escalate"
    #: The operation cannot be undone; say so rather than pretending.
    NOT_COMPENSATABLE = "not_compensatable"


#: Failures worth another attempt.
RETRYABLE: frozenset[FailureKind] = frozenset(
    {FailureKind.TRANSIENT, FailureKind.NETWORK, FailureKind.RATE_LIMIT, FailureKind.TIMEOUT}
)

#: Operations with a real-world irreversible effect. Retrying or "rolling
#: back" these would duplicate a real-world consequence, so the only honest
#: response is to escalate to a human.
IRREVERSIBLE: frozenset[str] = frozenset(
    {"message.send", "payment.confirm", "payment.prepare", "device.send_file"}
)


@dataclass(frozen=True)
class RecoveryPlan:
    """What to do about one failed node, and why."""

    node_id: str
    recovery: Recovery
    reason: str
    compensation_node: str | None = None
    reversible: bool = True
    attempts_used: int = 0
    max_attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "recovery": self.recovery.value,
            "reason": self.reason,
            "compensation_node": self.compensation_node,
            "reversible": self.reversible,
            "attempts_used": self.attempts_used,
            "max_attempts": self.max_attempts,
        }


@dataclass
class RecoveryPlanner:
    """Chooses a response per failed node. Pure — performs no I/O."""

    #: Node ids that completed successfully, so a rollback knows the scope.
    completed: set[str] = field(default_factory=set)

    def plan(
        self,
        graph: ActionGraph,
        node_id: str,
        failure: FailureKind,
        *,
        attempts_used: int = 0,
        max_attempts: int = 0,
    ) -> RecoveryPlan:
        node = next((n for n in graph.nodes if n.node_id == node_id), None)
        action_type = node.action.type if node and node.action else ""
        reversible = action_type not in IRREVERSIBLE
        compensation = self._compensation_for(graph, node_id)

        # 1. A transient failure with attempts left, on a repeatable action.
        if failure in RETRYABLE and attempts_used < max_attempts:
            return RecoveryPlan(
                node_id=node_id,
                recovery=Recovery.RETRY,
                reason=f"{failure.value} is transient; attempt {attempts_used + 1} of {max_attempts}",
                reversible=reversible,
                attempts_used=attempts_used,
                max_attempts=max_attempts,
            )

        # 2. Reversible with a declared compensation.
        if compensation is not None and reversible:
            return RecoveryPlan(
                node_id=node_id,
                recovery=Recovery.COMPENSATE,
                reason=f"{failure.value} on a reversible action with a declared compensation",
                compensation_node=compensation,
                reversible=True,
                attempts_used=attempts_used,
                max_attempts=max_attempts,
            )

        # 3. Reversible, but nothing to compensate with.
        if reversible and not compensation:
            return RecoveryPlan(
                node_id=node_id,
                recovery=Recovery.ESCALATE,
                reason=(
                    f"{failure.value}; the action may be reversible but no compensation "
                    "is declared, so a human must decide"
                ),
                reversible=True,
                attempts_used=attempts_used,
                max_attempts=max_attempts,
            )

        # 4. Irreversible. Do not pretend it can be undone.
        return RecoveryPlan(
            node_id=node_id,
            recovery=Recovery.NOT_COMPENSATABLE,
            reason=(
                f"{failure.value} on irreversible action {action_type!r}; "
                "the effect stands and a human must be told"
            ),
            compensation_node=compensation,
            reversible=False,
            attempts_used=attempts_used,
            max_attempts=max_attempts,
        )

    def _compensation_for(self, graph: ActionGraph, node_id: str) -> str | None:
        for node in graph.nodes:
            if node.node_type is NodeType.COMPENSATION and node.compensates == node_id:
                return node.node_id
        return None

    def rollback_scope(self, failed_node: str) -> list[str]:
        """Completed nodes that would need undoing, in reverse execution order."""
        return sorted(self.completed - {failed_node}, reverse=True)


@dataclass(frozen=True)
class CompensationAttempt:
    """What happened when one undo was (or was not) run. C16.

    ``compensated=False`` with a populated ``reason`` is the honest outcome for
    the cases that cannot be undone. A skipped compensation must never be
    reported as a clean rollback.
    """

    node_id: str
    target_node: str
    action_type: str
    compensated: bool
    state: str = ""
    error: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "target_node": self.target_node,
            "action_type": self.action_type,
            "compensated": self.compensated,
            "state": self.state,
            "error": self.error,
            "reason": self.reason,
        }


class CompensationRunner:
    """Chooses which compensations to run. Pure — performs no I/O.

    C16 separation of concerns: the *executor* performs undos, this decides
    which ones are legitimate. A compensation runs only when every one of these
    holds:

    1. The target actually produced a verified side effect. Undoing work that
       never happened is a second, unnecessary side effect.
    2. A compensation is declared for it.
    3. The action is **not** in :data:`IRREVERSIBLE`. A declared undo for an
       irreversible action is refused — the effect stands and a human is told.

    Order is reverse of forward execution, so dependencies unwind the way they
    were built.
    """

    @staticmethod
    def _compensation_for(graph: ActionGraph, node_id: str) -> str | None:
        """The declared undo for ``node_id``, if the graph has one."""
        for node in graph.nodes:
            if node.node_type is NodeType.COMPENSATION and node.compensates == node_id:
                return node.node_id
        return None

    def select(
        self,
        graph: ActionGraph,
        execution_order: list[str],
        verified_successes: set[str],
    ) -> list[tuple[GraphNode, GraphNode]]:
        """Return ``(compensation_node, target_node)`` pairs to run, in order."""
        idx = {n.node_id: n for n in graph.nodes}
        pairs: list[tuple[GraphNode, GraphNode]] = []

        # Reverse forward order: the most recent effect is undone first.
        for target_id in reversed(execution_order):
            if target_id not in verified_successes:
                continue
            target = idx.get(target_id)
            if target is None or target.action is None:
                continue
            if target.action.type in IRREVERSIBLE:
                # Deliberately no compensation, even if one was declared.
                continue
            comp_id = self._compensation_for(graph, target_id)
            if comp_id is None:
                continue
            comp = idx[comp_id]
            if comp.compensation is None and comp.action is None:
                continue
            pairs.append((comp, target))

        return pairs

    def skipped(
        self,
        graph: ActionGraph,
        verified_successes: set[str],
    ) -> list[CompensationAttempt]:
        """Undos that were declared but deliberately **not** run.

        Reported so a caller can see *why* the system is not in a rolled-back
        state, rather than inferring cleanliness from the absence of an error.
        """
        idx = {n.node_id: n for n in graph.nodes}
        out: list[CompensationAttempt] = []
        for node in graph.nodes:
            if node.node_type is not NodeType.COMPENSATION or not node.compensates:
                continue
            target = idx.get(node.compensates)
            if target is None or target.action is None:
                continue
            if target.node_id not in verified_successes:
                continue
            if target.action.type in IRREVERSIBLE:
                out.append(
                    CompensationAttempt(
                        node_id=node.node_id,
                        target_node=target.node_id,
                        action_type=target.action.type,
                        compensated=False,
                        reason=(
                            f"{target.action.type} is irreversible; its effect stands "
                            "and a human must be told"
                        ),
                    )
                )
        return out


__all__ = [
    "IRREVERSIBLE",
    "RETRYABLE",
    "CompensationAttempt",
    "CompensationRunner",
    "Recovery",
    "RecoveryPlan",
    "RecoveryPlanner",
]
