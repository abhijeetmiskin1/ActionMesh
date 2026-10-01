"""C7 — remaining mock providers: device file transfer, search, system launch.

Small, honest, state-keeping:

* ``MockDeviceProvider`` — ``device.send_file`` transfers a recorded file to a
  registered device. Unknown devices and unknown paths are INVALID_INPUT.
* ``MockSearchProvider`` — deterministic canned answers per query: ``search.web``
  never touches a network. Unknown queries return an empty list, not an error.
* ``MockSystemProvider`` — ``system.launch_app`` records launches of a small
  allowlist of apps. Anything else is INVALID_INPUT (we will not pretend to
  launch arbitrary programs, even as a mock).
"""

from __future__ import annotations

from typing import Any

from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ActionProvider, ProviderError
from actionmesh.providers.mock_base import MockHarnessMixin


class MockDeviceProvider(MockHarnessMixin, ActionProvider):
    """MOCK devices: a registry plus a transfer log."""

    name = "mock-devices"
    SUPPORTED = frozenset({"device.send_file"})

    def supports(self, action: Action) -> bool:
        return action.type in self.SUPPORTED and action.provider in ("mock", self.name)

    def __init__(
        self,
        devices: tuple[str, ...] = ("laptop", "phone"),
        files: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.devices: set[str] = set(devices)
        self.files: dict[str, str] = dict(files or {"/notes/todo.txt": "buy milk"})
        self.transfers: list[dict[str, Any]] = []

    async def _handle(self, action: Action) -> dict[str, Any]:
        params = action.parameters
        device_id = params.get("device_id")
        path = params.get("path")
        if device_id not in self.devices:
            raise ProviderError(f"unknown device {device_id!r}", FailureKind.INVALID_INPUT)
        if not isinstance(path, str) or path not in self.files:
            raise ProviderError(f"no such file {path!r}", FailureKind.INVALID_INPUT)
        entry = {
            "transfer_id": f"tr_{len(self.transfers) + 1:04d}",
            "device_id": device_id,
            "path": path,
        }
        self.transfers.append(entry)
        return entry


class MockSearchProvider(MockHarnessMixin, ActionProvider):
    """MOCK search: canned answers. Never touches a network."""

    name = "mock-search"
    SUPPORTED = frozenset({"search.web"})

    def supports(self, action: Action) -> bool:
        return action.type in self.SUPPORTED and action.provider in ("mock", self.name)

    def __init__(self) -> None:
        super().__init__()
        self.index: dict[str, list[str]] = {
            "actionmesh": ["ActionMesh: the execution layer for AI (mock result)"],
            "weather": ["Sunny, 24°C (mock result)"],
        }

    async def _handle(self, action: Action) -> dict[str, Any]:
        query = action.parameters.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ProviderError("query must be a non-empty string", FailureKind.INVALID_INPUT)
        limit = action.parameters.get("limit", 5)
        if not isinstance(limit, int) or limit < 0:
            raise ProviderError("limit must be a non-negative integer",
                                FailureKind.INVALID_INPUT)
        return {"results": self.index.get(query.strip().lower(), [])[:limit]}


class MockSystemProvider(MockHarnessMixin, ActionProvider):
    """MOCK system: records launches of an allowlisted set of apps.

    The allowlist is deliberate: even a mock should not claim it can launch
    arbitrary programs, because that claim would become the spec for the real
    adapter later.
    """

    name = "mock-system"
    SUPPORTED = frozenset({"system.launch_app"})

    def supports(self, action: Action) -> bool:
        return action.type in self.SUPPORTED and action.provider in ("mock", self.name)

    def __init__(self, allowed: tuple[str, ...] = ("browser", "calendar", "notes")) -> None:
        super().__init__()
        self.allowed: set[str] = set(allowed)
        self.launches: list[str] = []

    async def _handle(self, action: Action) -> dict[str, Any]:
        app = action.parameters.get("app")
        if not isinstance(app, str) or app not in self.allowed:
            raise ProviderError(f"cannot launch {app!r}", FailureKind.INVALID_INPUT)
        self.launches.append(app)
        return {"app": app, "launched": True}


__all__ = ["MockDeviceProvider", "MockSearchProvider", "MockSystemProvider"]
