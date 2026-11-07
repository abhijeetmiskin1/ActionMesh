"""Bounded, non-waiting claims for slow request work, independent of authorization."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar


class RequestBusy(RuntimeError):
    def __init__(self, *, capacity: bool) -> None:
        self.capacity = capacity
        super().__init__("The service is busy; try again shortly." if capacity
                         else "A conversation turn is already running; wait for its response.")


class RequestGate:
    """Reject duplicate work and excess load rather than queueing without a bound."""

    def __init__(self, max_active: int = 8) -> None:
        if max_active < 1:
            raise ValueError("max_active must be positive")
        self.max_active = max_active
        self._active: set[str] = set()
        self._lock = threading.Lock()

    @contextmanager
    def claim(self, key: str) -> Iterator[None]:
        with self._lock:
            if key in self._active:
                raise RequestBusy(capacity=False)
            if len(self._active) >= self.max_active:
                raise RequestBusy(capacity=True)
            self._active.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._active.discard(key)


T = TypeVar("T")


class BoundedWorkers:
    """Keep a worker permit until blocking work ends, even if its caller cancels."""

    def __init__(self, limit: int = 8) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self._slots = threading.BoundedSemaphore(limit)

    async def run(self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if not self._slots.acquire(blocking=False):
            raise RequestBusy(capacity=True)

        def invoke() -> T:
            try:
                return function(*args, **kwargs)
            finally:
                self._slots.release()

        task = asyncio.create_task(asyncio.to_thread(invoke))
        # An abandoned caller must neither free a running worker's permit nor leave
        # a late transport exception unobserved. The thread always releases its slot.
        def consume(done: asyncio.Task[T]) -> None:
            if not done.cancelled():
                done.exception()
        task.add_done_callback(consume)
        return await asyncio.shield(task)
