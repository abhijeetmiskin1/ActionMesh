"""C5/C7 — provider interface.

A provider is the only place a side effect can happen. The interface is
deliberately small and *async*, so a real client can be dropped in later
without changing the executor.

Every implementation must declare ``simulated = True`` unless it talks to a
real service. The executor propagates that flag so a simulated run is never
reported as a real one.

``base.py`` holds the interface plus error/result types only. The deterministic
mock harness is ``mock_base.py``; the generic fallback ``MockProvider`` lives
in ``actionmesh.providers`` so this module stays free of mock machinery.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from actionmesh.protocol.models import Action, FailureKind, VerificationResult


class ProviderError(Exception):
    """A provider could not complete the action.

    Carries a ``FailureKind`` so the executor can decide whether retrying is
    even sensible — retrying an authorization failure just fails again.
    """

    def __init__(self, message: str, kind: FailureKind = FailureKind.PROVIDER) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class ProviderResult:
    """What a provider reports back. NOT proof that the action took effect."""

    ok: bool
    provider: str = ""
    action_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    #: True when the provider is a simulator. The executor surfaces this.
    simulated: bool = True
    kind: FailureKind = FailureKind.UNKNOWN


class ActionProvider(abc.ABC):
    """Adapter interface. Implementations must be free of business logic."""

    #: Provider identifier used in definitions and audit records.
    name: str = "abstract"
    #: Honest flag: a simulator must never claim to be a real integration.
    simulated: bool = True

    @abc.abstractmethod
    def supports(self, action: Action) -> bool:
        """Can this provider handle that action type?"""

    @abc.abstractmethod
    async def execute(self, action: Action) -> ProviderResult:
        """Perform the side effect."""

    async def verify(
        self, action: Action, result: ProviderResult
    ) -> VerificationResult:
        """Independently confirm the effect happened.

        The default is honest: a provider that has not implemented real
        verification says so, rather than assuming success.
        """
        return VerificationResult(
            verified=False,
            method="none",
            detail="provider does not implement verification",
            simulated=self.simulated,
        )


__all__ = ["ActionProvider", "ProviderError", "ProviderResult"]


