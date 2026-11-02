"""C9 in-memory event publication for SSE and WebSocket consumers."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

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
        store: Any = None,
    ) -> None:
        if max_history < 1:
            raise ValueError("max_history must be positive")
        if max_subscriber_queue < 1:
            raise ValueError("max_subscriber_queue must be positive")
        self._history: deque[MeshEvent] = deque(maxlen=max_history)
        self._max_subscriber_queue = max_subscriber_queue
        self._subscribers: set[asyncio.Queue[MeshEvent | None]] = set()
        self._audit = audit
        #: C35: the durable store whose `mesh_events` table outlives this hub's deque.
        #: Publishing still ends here -- the store is a second copy for whoever arrives
        #: after this process is gone, not a queue to read from instead.
        self._store = store
        self._written = 0

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

    def attach_store(self, store: Any) -> None:
        """Start retaining published events beyond this process's bounded history.

        Like ``attach_audit``, a startup wiring step: events published before it are
        already only in memory, and will not appear in a later process's replay.
        """
        self._store = store

    def publish(self, event: MeshEvent, *, transient: bool = False) -> None:
        """Fan an event out to live subscribers.

        ``transient`` events (incremental process output) reach only the subscribers
        connected *right now*: they are not sealed into the audit chain and not kept in
        the replay history. Audit is a record of decisions and outcomes, not a copy of a
        program's stdout, and a phone that reconnects should recover the authoritative
        execution state -- not be re-sent output that may have contained secrets.
        """
        if not transient:
            if self._audit is not None:
                self._audit.record_event(event)
            self._history.append(event)
            if self._store is not None:
                self._store.save_event(event)
                self._written += 1
                if self._written % 64 == 0:
                    # Amortised, so retention does not cost an extra write per event.
                    self._store.trim_events()
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
        if self._store is None:
            return events
        # Anything this process published is already in `events`; the store contributes
        # only what it wrote before this hub existed. Keyed by `event_id`, so a run that
        # spans the restart replays whole and no event arrives twice.
        seen = {event.event_id for event in events}
        earlier = [
            event for event in self._store.list_events(execution_id)
            if event.event_id not in seen
        ]
        return earlier + events

    def _after(self, events: list[MeshEvent], since_event_id: str | None) -> list[MeshEvent]:
        """Drop everything up to and including ``since_event_id``.

        Used to resume a dropped stream without replaying events the client has
        already seen. When the id is no longer retained (it aged out of the bounded
        history) the whole window is replayed instead -- a duplicate the client
        de-duplicates by ``event_id`` is safer than a gap it would never notice.
        """
        if not since_event_id:
            return events
        for index, event in enumerate(events):
            if event.event_id == since_event_id:
                return events[index + 1 :]
        return events

    async def subscribe(
        self,
        execution_id: str | None = None,
        *,
        allow: Callable[[MeshEvent], bool] | None = None,
        since_event_id: str | None = None,
    ) -> AsyncIterator[MeshEvent]:
        """Stream retained history then live events.

        ``allow`` is a server-side authorization predicate applied to both the
        replayed history and the live tail; a subscriber that cannot pass it for an
        event simply never sees it. ``execution_id`` and ``since_event_id`` only ever
        narrow what a caller receives -- neither can widen the ``allow`` boundary.
        """
        queue: asyncio.Queue[MeshEvent | None] = asyncio.Queue(self._max_subscriber_queue)
        self._subscribers.add(queue)
        permitted = allow if allow is not None else (lambda _event: True)
        try:
            replayed = self._after(self.history(execution_id), since_event_id)
            for event in replayed:
                if permitted(event):
                    yield event
            while True:
                pending = await queue.get()
                if pending is None:
                    return
                if (execution_id is None or pending.execution_id == execution_id) and permitted(
                    pending
                ):
                    yield pending
        finally:
            self._subscribers.discard(queue)


__all__ = ["EventHub"]