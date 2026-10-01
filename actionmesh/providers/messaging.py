"""C7 — Messaging mock provider.

Keeps a thread of sent messages and a small address book of resolvable
recipients. Sending to an unknown recipient is INVALID_INPUT (a caller error),
not a network failure — the failure kind tells the executor whether retrying
makes sense, and here it should not.
"""

from __future__ import annotations

from typing import Any

from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ActionProvider, ProviderError
from actionmesh.providers.mock_base import MockHarnessMixin

_CONTACTS = ("mom", "rahul", "laptop", "user_rahul")


class MockMessagingProvider(MockHarnessMixin, ActionProvider):
    """MOCK messaging: a contact list plus a sent-message thread."""

    name = "mock-messaging"
    SUPPORTED = frozenset({"message.send", "message.read"})

    def supports(self, action: Action) -> bool:
        return action.type in self.SUPPORTED and action.provider in ("mock", self.name)

    def __init__(self) -> None:
        super().__init__()
        self.contacts: set[str] = set(_CONTACTS)
        # Thread of sent messages, oldest first. The harness `state` keeps the
        # idempotent receipts; this keeps the domain-visible effect.
        self.thread: list[dict[str, Any]] = []

    async def _handle(self, action: Action) -> dict[str, Any]:
        if action.type == "message.send":
            return self._send(
                action.parameters.get("to"), action.parameters.get("message")
            )
        if action.type == "message.read":
            limit = action.parameters.get("limit", 10)
            if not isinstance(limit, int) or limit < 0:
                raise ProviderError("limit must be a non-negative integer",
                                    FailureKind.INVALID_INPUT)
            return {"messages": list(reversed(self.thread[-limit:]))}
        raise ProviderError(f"unsupported action {action.type!r}", FailureKind.PERMANENT)

    def _send(self, to: Any, message: Any) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip():
            raise ProviderError("message must be a non-empty string",
                                FailureKind.INVALID_INPUT)
        if to is not None and str(to).lower() not in self.contacts:
            # A caller error: retrying would just fail the same way.
            raise ProviderError(f"unknown recipient {to!r}", FailureKind.INVALID_INPUT)
        entry = {
            "message_id": f"mm_{len(self.thread) + 1:04d}",
            "to": to,
            "message": message,
        }
        self.thread.append(entry)
        return entry


__all__ = ["MockMessagingProvider"]
