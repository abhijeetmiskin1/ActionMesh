"""A small, bounded sliding-window rate limiter.

The gap this closes is the one the security model kept pointing at: pairing, ticket
minting and execution were each individually bounded, but nothing stopped one caller
from hammering them. This is deliberately a *process-local* reference implementation,
not a distributed one -- an in-memory limiter on a single laptop agent is exactly the
right size for the deployment, and pretending otherwise (or shipping Redis to look
thorough) would be the dishonest move. A multi-process deployment enforces the same
policy at the proxy; the interface is the same, only the store differs.

Two properties matter and are tested: the window actually slides (a burst is allowed,
then refused, then allowed again once the oldest hits expire), and the structure cannot
grow without bound -- expired keys are pruned and a hard cap on tracked keys is enforced.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

MAX_TRACKED_KEYS = 10_000


class RateLimiter:
    """Allow up to ``limit`` hits per ``window_s`` for each independent key."""

    def __init__(
        self,
        *,
        limit: int,
        window_s: float,
        max_keys: int = MAX_TRACKED_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        if max_keys < 1:
            raise ValueError("max_keys must be at least 1")
        self.limit = limit
        self.window_s = float(window_s)
        self._max_keys = int(max_keys)
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for key in [k for k, hits in self._hits.items() if not hits or now - hits[-1] >= self.window_s]:
            self._hits.pop(key, None)

    def allow(self, key: str) -> bool:
        """Record a hit for ``key`` and say whether it is within the limit."""
        now = self._clock()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self._max_keys:
                    self._prune(now)
                    # Still full after pruning: the newest key wins, so an attacker
                    # spraying fresh keys cannot evict a legitimate caller's window.
                    if len(self._hits) >= self._max_keys:
                        return False
                hits = deque()
                self._hits[key] = hits
            while hits and now - hits[0] >= self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def remaining(self, key: str) -> int:
        now = self._clock()
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return self.limit
            live = [t for t in hits if now - t < self.window_s]
            return max(0, self.limit - len(live))

    def tracked_keys(self) -> int:
        return len(self._hits)


__all__ = ["MAX_TRACKED_KEYS", "RateLimiter"]
