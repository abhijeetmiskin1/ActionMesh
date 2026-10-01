"""C7 — Notifications + contacts mock providers.

Two small providers in one module:

* ``MockNotificationProvider`` — reminders and device notifications. A reminder
  is a dated note that can be listed, completed and cancelled. A notification
  is a fire-and-record dispatch to a known device.
* ``MockContactsProvider`` — a tiny address book. Currently exposes no
  executable registry action; it exists so the executor/integration tests have
  a directory to resolve people and devices against. It is honest about that:
  everything here says MOCK, and there is no backing store.
"""

from __future__ import annotations

from typing import Any

from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ActionProvider, ProviderError
from actionmesh.providers.mock_base import MockHarnessMixin


class MockNotificationProvider(MockHarnessMixin, ActionProvider):
    """MOCK notifications: a reminder book plus a device dispatch log."""

    name = "mock-notifications"
    SUPPORTED = frozenset({"reminder.create", "device.notify"})

    def supports(self, action: Action) -> bool:
        return action.type in self.SUPPORTED and action.provider in ("mock", self.name)

    def __init__(self, devices: tuple[str, ...] = ("laptop", "phone")) -> None:
        super().__init__()
        self.devices: set[str] = set(devices)
        self.reminders: dict[str, dict[str, Any]] = {}
        self.dispatches: list[dict[str, Any]] = []

    async def _handle(self, action: Action) -> dict[str, Any]:
        params = action.parameters
        if action.type == "reminder.create":
            text = params.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ProviderError("text must be a non-empty string",
                                    FailureKind.INVALID_INPUT)
            reminder_id = f"rem_{len(self.reminders) + 1:04d}"
            self.reminders[reminder_id] = {
                "reminder_id": reminder_id,
                "text": text,
                "time": params.get("time"),
                "repeat": params.get("repeat"),
                "status": "scheduled",
            }
            return {"reminder_id": reminder_id}
        if action.type == "device.notify":
            device_id = params.get("device_id")
            title = params.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ProviderError("title must be a non-empty string",
                                    FailureKind.INVALID_INPUT)
            if device_id not in self.devices:
                raise ProviderError(f"unknown device {device_id!r}",
                                    FailureKind.INVALID_INPUT)
            entry = {
                "notification_id": f"ntf_{len(self.dispatches) + 1:04d}",
                "device_id": device_id,
                "title": title,
                "body": params.get("body"),
            }
            self.dispatches.append(entry)
            return entry
        raise ProviderError(f"unsupported action {action.type!r}", FailureKind.PERMANENT)


class MockContactsProvider(MockHarnessMixin, ActionProvider):
    """MOCK contacts: a read-only directory the planner can resolve against.

    Exposes no executable action of its own — ``SUPPORTED`` is intentionally
    empty, so it can never be selected by ``provider_for``. It is a lookup
    helper: `resolve(name)` returns the directory entry, or `None` when the
    name is unknown. Ambiguous or unknown names return nothing rather than
    guessing, per the entity-resolution rules in C10.
    """

    name = "mock-contacts"
    SUPPORTED = frozenset()

    def supports(self, action: Action) -> bool:
        return False

    def __init__(self) -> None:
        super().__init__()
        self.directory: dict[str, dict[str, Any]] = {
            "mom": {"name": "Mom", "channels": ["message"], "preferred": "message"},
            "rahul": {"name": "Rahul", "channels": ["message"], "preferred": "message"},
            "laptop": {"name": "Laptop", "channels": ["file"], "preferred": "file"},
        }

    def resolve(self, name: Any) -> dict[str, Any] | None:
        """Directory lookup. Unknown names return None; never a guess."""
        if not isinstance(name, str):
            return None
        return self.directory.get(name.strip().lower())


__all__ = ["MockContactsProvider", "MockNotificationProvider"]
