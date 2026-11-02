"""C5 — action lifecycle state machine.

Transitions are explicit and validated. An invalid transition is a programming
error and is rejected rather than forced, because "the state quietly skipped
WAITING_PERMISSION" is exactly the class of bug that turns a permission system
into a decoration.

Happy path::

    CREATED → VALIDATING → PLANNED → WAITING_PERMISSION → AUTHORIZED
            → EXECUTING → VERIFYING → SUCCESS

Terminal states: ``SUCCESS``, ``FAILED``, ``CANCELLED``, ``DENIED``, ``TIMEOUT``.
"""

from __future__ import annotations

from actionmesh.protocol.models import ActionState

#: Transitions the executor is allowed to make.
ALLOWED: dict[ActionState, frozenset[ActionState]] = {
    ActionState.CREATED: frozenset({ActionState.VALIDATING, ActionState.CANCELLED}),
    ActionState.VALIDATING: frozenset(
        {ActionState.PLANNED, ActionState.FAILED, ActionState.DENIED, ActionState.CANCELLED}
    ),
    ActionState.PLANNED: frozenset(
        {
            ActionState.WAITING_PERMISSION,
            ActionState.AUTHORIZED,  # only when the policy granted outright
            ActionState.DENIED,
            ActionState.CANCELLED,
        }
    ),
    ActionState.WAITING_PERMISSION: frozenset(
        {
            ActionState.AUTHORIZED,
            ActionState.DENIED,
            ActionState.CANCELLED,
            ActionState.TIMEOUT,
            ActionState.INTERRUPTED,
        }
    ),
    ActionState.AUTHORIZED: frozenset(
        {ActionState.EXECUTING, ActionState.DENIED, ActionState.CANCELLED, ActionState.TIMEOUT}
    ),
    ActionState.EXECUTING: frozenset(
        {
            ActionState.VERIFYING,
            ActionState.RETRYING,
            ActionState.FAILED,
            ActionState.CANCELLED,
            ActionState.TIMEOUT,
            ActionState.PARTIALLY_COMPLETED,
            # C35: the owner died. Reconciliation may only ever move a live run here.
            ActionState.INTERRUPTED,
        }
    ),
    ActionState.RETRYING: frozenset(
        {ActionState.EXECUTING, ActionState.FAILED, ActionState.CANCELLED, ActionState.TIMEOUT}
    ),
    ActionState.VERIFYING: frozenset(
        {
            ActionState.SUCCESS,
            ActionState.FAILED,
            ActionState.REQUIRES_USER,
            ActionState.CANCELLED,
            ActionState.PARTIALLY_COMPLETED,
            ActionState.INTERRUPTED,
        }
    ),
    # Terminal states permit nothing.
    ActionState.SUCCESS: frozenset(),
    ActionState.FAILED: frozenset(),
    ActionState.CANCELLED: frozenset(),
    ActionState.DENIED: frozenset(),
    ActionState.TIMEOUT: frozenset(),
    ActionState.REQUIRES_USER: frozenset({ActionState.AUTHORIZED, ActionState.CANCELLED}),
    ActionState.PARTIALLY_COMPLETED: frozenset(),
    # Reconciled once, and reconciled once only: an interrupted run is not re-dispatched
    # and not re-decided by whichever process reads it next.
    ActionState.INTERRUPTED: frozenset(),
}

#: No further work will be done from these states.
TERMINAL = frozenset(
    {
        ActionState.SUCCESS,
        ActionState.FAILED,
        ActionState.CANCELLED,
        ActionState.DENIED,
        ActionState.TIMEOUT,
        ActionState.PARTIALLY_COMPLETED,
        ActionState.INTERRUPTED,
    }
)


class InvalidTransition(RuntimeError):
    """An illegal lifecycle transition was attempted."""


def can_transition(current: ActionState, target: ActionState) -> bool:
    return target in ALLOWED.get(current, frozenset())


def ensure_transition(current: ActionState, target: ActionState) -> ActionState:
    """Validate a transition, or raise. Returns ``target`` on success."""
    if not can_transition(current, target):
        raise InvalidTransition(f"cannot move from {current.value} to {target.value}")
    return target


def is_terminal(state: ActionState) -> bool:
    return state in TERMINAL


__all__ = [
    "ALLOWED",
    "TERMINAL",
    "InvalidTransition",
    "can_transition",
    "ensure_transition",
    "is_terminal",
]
