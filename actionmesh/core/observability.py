"""C15 low-overhead execution metrics."""

from __future__ import annotations

from collections import Counter
from threading import Lock


class Metrics:
    """Thread-safe counters suitable for a process-local health endpoint."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = Lock()

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._counts.items()))


__all__ = ["Metrics"]