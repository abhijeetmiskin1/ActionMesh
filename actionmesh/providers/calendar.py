"""C7 — Calendar mock provider.

Keeps an event book keyed by event id, so lifecycle transitions are explicit:
an event must exist before it can be updated or deleted, and `calendar.delete`
removes it for real — a subsequent read-back fails, which is exactly what C6's
verification engine checks.
"""

from __future__ import annotations

from typing import Any

from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ActionProvider, ProviderError
from actionmesh.providers.mock_base import MockHarnessMixin


class MockCalendarProvider(MockHarnessMixin, ActionProvider):
    """MOCK calendar: an event book with real create/update/delete semantics."""

    name = "mock-calendar"
    SUPPORTED = frozenset({"calendar.create", "calendar.update", "calendar.delete"})

    def supports(self, action: Action) -> bool:
        return action.type in self.SUPPORTED and action.provider in ("mock", self.name)

    def __init__(self) -> None:
        super().__init__()
        self.events: dict[str, dict[str, Any]] = {}

    async def _handle(self, action: Action) -> dict[str, Any]:
        params = action.parameters
        if action.type == "calendar.create":
            title = params.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ProviderError("title must be a non-empty string",
                                    FailureKind.INVALID_INPUT)
            event_id = f"evt_{len(self.events) + 1:04d}"
            self.events[event_id] = {
                "event_id": event_id,
                "title": title,
                "time": params.get("time"),
                "duration_min": params.get("duration_min"),
                "status": "active",
            }
            return {"event_id": event_id}
        if action.type == "calendar.update":
            raw_event_id = params.get("event_id")
            event = self.events.get(str(raw_event_id))
            if event is None:
                # Updating something that does not exist is a caller error.
                raise ProviderError(f"unknown event {raw_event_id!r}", FailureKind.INVALID_INPUT)
            for field in ("title", "time", "duration_min"):
                if field in params:
                    event[field] = params[field]
            return {"event_id": raw_event_id}
        if action.type == "calendar.delete":
            raw_event_id = params.get("event_id")
            if str(raw_event_id) not in self.events:
                raise ProviderError(f"unknown event {raw_event_id!r}", FailureKind.INVALID_INPUT)
            del self.events[str(raw_event_id)]
            return {"event_id": raw_event_id, "deleted": True}
        raise ProviderError(f"unsupported action {action.type!r}", FailureKind.PERMANENT)


__all__ = ["MockCalendarProvider"]
