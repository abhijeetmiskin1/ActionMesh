"""Short-lived, actor-bound tickets for the realtime event stream.

An ``EventSource`` cannot set an ``Authorization`` header, so a phone watching
``/events`` has no way to present its device credential the way every other call
does. Handing the credential itself to the query string would put the long-lived
secret in URLs, referrer headers and any proxy in between.

Instead the phone exchanges its credential, once, for a ticket that (a) grants only
the right to *watch* events, (b) is bound to the actor that minted it, and (c)
expires in minutes. It is reusable until it expires, because a reconnecting
``EventSource`` has to be able to re-present it, but it is worthless anywhere except
the stream endpoint.
"""

from __future__ import annotations

import secrets
import time

DEFAULT_TICKET_TTL_S = 120
MAX_TICKETS = 1000


class StreamTicketStore:
    """A bounded set of ``token -> (actor, expires_at)`` entries."""

    def __init__(self, *, ttl_s: float = DEFAULT_TICKET_TTL_S, max_tickets: int = MAX_TICKETS) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        if max_tickets < 1:
            raise ValueError("max_tickets must be at least 1")
        self._ttl = float(ttl_s)
        self._max = int(max_tickets)
        self._tickets: dict[str, tuple[str, float]] = {}

    def _prune(self, now: float) -> None:
        for token in [t for t, (_actor, expires) in self._tickets.items() if now >= expires]:
            self._tickets.pop(token, None)

    def mint(self, actor: str) -> tuple[str, float]:
        """Issue a fresh ticket for ``actor``; returns ``(token, ttl_seconds)``."""
        now = time.monotonic()
        self._prune(now)
        while len(self._tickets) >= self._max:
            # Evict the soonest-to-expire, so a flood of new mints cannot evict the
            # one ticket a client is actively reconnecting with.
            stalest = min(self._tickets.items(), key=lambda kv: kv[1][1])[0]
            self._tickets.pop(stalest, None)
        token = secrets.token_urlsafe(24)
        self._tickets[token] = (actor, now + self._ttl)
        return token, self._ttl

    def resolve(self, token: str) -> str | None:
        """The actor a valid, unexpired ticket was minted for, else ``None``."""
        entry = self._tickets.get(token)
        if entry is None:
            return None
        actor, expires = entry
        if time.monotonic() >= expires:
            self._tickets.pop(token, None)
            return None
        return actor

    def __len__(self) -> int:
        return len(self._tickets)


__all__ = ["DEFAULT_TICKET_TTL_S", "MAX_TICKETS", "StreamTicketStore"]
