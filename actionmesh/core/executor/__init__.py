"""C5 — Execution Engine: the only layer that causes side effects.

Everything above this layer decides *what should happen*. This layer is
responsible for doing it safely: re-checking authorization at dispatch time,
running independent work concurrently, bounding retries, deduplicating by
idempotency key, and reporting execution and verification separately.
"""

from __future__ import annotations

from actionmesh.core.executor.engine import (
    DEFAULT_RETRYABLE,
    ExecutionEngine,
    ExecutionResult,
    NodeOutcome,
)
from actionmesh.core.executor.idempotency import IdempotencyLedger, SQLiteIdempotencyLedger
from actionmesh.core.executor.lifecycle import (
    ALLOWED,
    TERMINAL,
    InvalidTransition,
    can_transition,
    ensure_transition,
    is_terminal,
)

__all__ = [
    "ALLOWED",
    "DEFAULT_RETRYABLE",
    "TERMINAL",
    "ExecutionEngine",
    "ExecutionResult",
    "IdempotencyLedger",
    "InvalidTransition",
    "NodeOutcome",
    "SQLiteIdempotencyLedger",
    "can_transition",
    "ensure_transition",
    "is_terminal",
]
