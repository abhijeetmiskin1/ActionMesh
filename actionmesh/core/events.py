"""C9 in-memory event publication for SSE and WebSocket consumers."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator

from actionmesh.core.audit import AuditLog
from actionmesh.protocol.models import MeshEvent


class EventHub:
    """Bounded event history plus bounded live async subscribers.

    This is intentionally process-local. C15 can replace it with a durable or
    broker-backed implementation without changing the API's event contract.

    Publishers never block on consumers. A subscriber that cannot keep up is
    disconnected once its bounded queue is full; it can reconnect and replay
    the retained history.

    Pass ``audit=AuditLog()`` to seal every published event into a tamper-evident
    chain. It is optional so existing wiring is unchanged; when supplied, an
    event is chained *before* it is published, so a publish that raises cannot
    leave an unrecorded event behind.
    """

    def __init__(
        self,
        max_history: int = 1000,
        max_subscriber_queue: int = 1000,
        audit: AuditLog | None = None,
    ) -> None:
        if max_history < 1:
            raise ValueError("max_history must be positive")
        if max_subscriber_queue < 1:
            raise ValueError("max_subscriber_queue must be positive")
        self._history: deque[MeshEvent] = deque(maxlen=max_history)
        self._max_subscriber_queue = max_subscriber_queue
        self._subscribers: set[asyncio.Queue[MeshEvent | None]] = set()
        self._audit = audit

    @property
    def audit(self) -> AuditLog | None:
        """The audit chain this hub writes to, if any."""
        return self._audit

    def attach_audit(self, audit: AuditLog) -> None:
        """Start sealing published events into an audit chain from now on.

        Events published before this call are **not** retro-audited, so a
        deployment should attach the log at startup rather than midway through
        a run — otherwise the chain silently starts partway through the history
        it is supposed to cover.
        """
        self._audit = audit

    def publish(self, event: MeshEvent) -> None:
        if self._audit is not None:
            self._audit.record_event(event)
        self._history.append(event)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Disconnect explicitly rather than silently losing an event
                # or allowing a slow client to grow memory without bound.
                self._subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)

    def history(self, execution_id: str | None = None) -> list[MeshEvent]:
        events = list(self._history)
        if execution_id is not None:
            events = [event for event in events if event.execution_id == execution_id]
        return events

    async def subscribe(self, execution_id: str | None = None) -> AsyncIterator[MeshEvent]:
        queue: asyncio.Queue[MeshEvent | None] = asyncio.Queue(self._max_subscriber_queue)
        self._subscribers.add(queue)
        try:
            for event in self.history(execution_id):
                yield event
            while True:
                event = await queue.get()
                if event is None:
                    return
                if execution_id is None or event.execution_id == execution_id:
                    yield event
        finally:
            self._subscribers.discard(queue)


__all__ = ["EventHub"]