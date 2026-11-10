"""C7 — Files mock provider.

Keeps a tiny in-memory filesystem: paths map to content plus a checksum. Reads
return the checksum; moves relocate the entry; shares record a share ledger.

An absent path is INVALID_INPUT (caller error). One exception: `file.read` also
raises AUTHORIZATION for paths under ``/private`` — a deterministic auth
failure for executor/security tests.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ActionProvider, ProviderError
from actionmesh.providers.mock_base import MockHarnessMixin

_PRIVATE_PREFIX = "/private/"


class MockFileProvider(MockHarnessMixin, ActionProvider):
    """MOCK files: an in-memory filesystem with checksums and a share ledger."""

    name = "mock-files"
    SUPPORTED = frozenset({"file.read", "file.move", "file.share"})

    def supports(self, action: Action) -> bool:
        return action.type in self.SUPPORTED and action.provider in ("mock", self.name)

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        super().__init__()
        self.files: dict[str, str] = dict(initial or {"/notes/todo.txt": "buy milk"})
        # (path, target) pairs this provider has shared.
        self.shares: list[tuple[str, str]] = []

    @staticmethod
    def checksum(content: str) -> str:
        return sha256(content.encode("utf-8")).hexdigest()[:16]

    def _require(self, path: Any) -> str:
        if not isinstance(path, str) or not path:
            raise ProviderError("path must be a non-empty string", FailureKind.INVALID_INPUT)
        if path.startswith(_PRIVATE_PREFIX):
            raise ProviderError(f"access denied for {path!r}", FailureKind.AUTHORIZATION)
        if path not in self.files:
            raise ProviderError(f"no such file {path!r}", FailureKind.INVALID_INPUT)
        return path

    async def _handle(self, action: Action) -> dict[str, Any]:
        params = action.parameters
        if action.type == "file.read":
            path = self._require(params.get("path"))
            return {"path": path, "checksum": self.checksum(self.files[path])}
        if action.type == "file.move":
            source = self._require(params.get("path"))
            destination = params.get("destination")
            if not isinstance(destination, str) or not destination:
                raise ProviderError("destination must be a non-empty string",
                                    FailureKind.INVALID_INPUT)
            self.files[destination] = self.files.pop(source)
            return {"path": destination, "checksum": self.checksum(self.files[destination])}
        if action.type == "file.share":
            path = self._require(params.get("path"))
            target = params.get("target")
            if not isinstance(target, str) or not target:
                raise ProviderError("target must be a non-empty string",
                                    FailureKind.INVALID_INPUT)
            self.shares.append((path, target))
            return {"path": path, "target": target, "checksum": self.checksum(self.files[path])}
        raise ProviderError(f"unsupported action {action.type!r}", FailureKind.PERMANENT)


__all__ = ["MockFileProvider"]
