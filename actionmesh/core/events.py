"""C9 in-memory event publication for SSE and WebSocket consumers."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from actionmesh.core.audit import AuditLog
from actionmesh.protocol.models import MeshEvent

#: How much transient output this hub keeps for one execution, in memory only.
#: It exists so a request that arrives *after* a read finished can still explain what
#: that read observed (the diagnosis flow draws from it). It does not change where a
#: transient event does or does not go: never the audit chain, never the store, never
#: the replay history and never ``history()``.
MAX_TRANSIENT_BYTES = 1024 * 1024
#: How many executions may hold captured transient output at once. When a new
#: execution makes one too many, the least recently published is dropped whole.
MAX_TRANSIENT_EXECUTIONS = 32


@dataclass
class _Capture:
    """Bounded in-memory copy of one execution's transient output."""

    events: deque[MeshEvent] = field(default_factory=deque)
    size: int = 0


def _chunk_bytes(event: MeshEvent) -> int:
    payload = event.payload if isinstance(event.payload, dict) else {}
    chunk = payload.get("chunk")
    if not isinstance(chunk, str):
        return 0
    return len(chunk.encode("utf-8", errors="replace"))


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
        #: C39: plain observers of published events, told after the event is durable
        #: and fanned out. A listener runs *after* the publish, so it cannot change
        #: what subscribers see, and it is called synchronously -- a listener that
        #: wants to start work schedules it rather than doing it inline. Kept separate
        #: from ``subscribe`` on purpose: a subscriber is a client's stream and may
        #: stop reading; a listener is in-process wiring that must not be conflated
        #: with one.
        self._listeners: list[Callable[[MeshEvent], None]] = []
        #: C36: a bounded, in-memory copy of recent transient output, keyed by execution.
        #: Process-local and deliberately short-lived -- see :meth:`transient`. Ordered so
        #: the least recently active execution is the one that is dropped.
        self._transient: OrderedDict[str, _Capture] = OrderedDict()

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

    def add_listener(self, listener: Callable[[MeshEvent], None]) -> None:
        """Observe every non-transient publish from now on. A startup wiring step.

        Unlike :meth:`subscribe` this is not a consumer: it has no queue to fill and
        cannot block a publisher. A listener that raises is removed from the list and
        its error swallowed -- one broken observer must not turn "an execution
        finished" into an exception inside the executor that published it. Listeners
        are deliberately never given transient events: program output is not an
        outcome, and nothing should be triggered by a line of stdout.
        """
        if listener not in self._listeners:
            self._listeners.append(listener)

    def publish(self, event: MeshEvent, *, transient: bool = False) -> None:
        """Fan an event out to live subscribers.

        ``transient`` events (incremental process output) reach only the subscribers
        connected *right now*: they are not sealed into the audit chain and not kept in
        the replay history. Audit is a record of decisions and outcomes, not a copy of a
        program's stdout, and a phone that reconnects should recover the authoritative
        execution state -- not be re-sent output that may have contained secrets.

        A transient event is still copied into a small in-memory buffer for its own
        execution (:meth:`transient`), so the request that arrives just after a read
        finished can explain what it observed. That buffer is bounded, process-local and
        never written anywhere: it is the same lifetime and the same audience the live
        stream already had, just long enough to survive the end of the read.
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
        else:
            self._capture(event)
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
        if not transient and self._listeners:
            # Last, and after the event is durable: a listener is told what happened,
            # never allowed to influence it. See :meth:`add_listener`.
            for listener in tuple(self._listeners):
                try:
                    listener(event)
                except Exception:  # noqa: BLE001 - an observer must not stop a publish
                    self._listeners.remove(listener)

    def _capture(self, event: MeshEvent) -> None:
        """Keep a bounded in-memory copy of one transient event for its execution.

        The bound is on *content* bytes, which is what a hostile or chatty program can
        push; an event whose content would overflow the cap is not kept, so the buffer
        never grows past ``MAX_TRANSIENT_BYTES``. When the copy is full the *oldest*
        events are dropped, leaving the tail -- which is what a caller reading a log is
        after, and what "the latest error" means.
        """
        if not event.execution_id:
            return
        capture = self._transient.get(event.execution_id)
        if capture is None:
            capture = _Capture()
            self._transient[event.execution_id] = capture
            while len(self._transient) > MAX_TRANSIENT_EXECUTIONS:
                self._transient.popitem(last=False)
        else:
            # Re-insert so the least recently *active* execution is the one evicted.
            self._transient.move_to_end(event.execution_id)
        size = _chunk_bytes(event)
        if size > MAX_TRANSIENT_BYTES:
            return
        capture.events.append(event)
        capture.size += size
        while capture.size > MAX_TRANSIENT_BYTES and len(capture.events) > 1:
            dropped = capture.events.popleft()
            capture.size -= _chunk_bytes(dropped)

    def transient(self, execution_id: str) -> list[MeshEvent]:
        """What one execution published on the transient channel, if still held.

        This is not ``history``: nothing here was audited, persisted, replayed or
        delivered to a late subscriber, and an execution with no captured output returns
        an empty list rather than an error -- "I no longer hold it" and "it printed
        nothing" are both honest answers, and the caller says which it can tell.

        The returned events carry their actor, so the caller still applies its own
        authorization. This method deliberately does not: it reports what it holds, and
        the code that knows who is asking decides what they may see.
        """
        if not execution_id:
            return []
        capture = self._transient.get(execution_id)
        if capture is None:
            return []
        self._transient.move_to_end(execution_id)
        return list(capture.events)

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


__all__ = ["MAX_TRANSIENT_BYTES", "MAX_TRANSIENT_EXECUTIONS", "EventHub"]