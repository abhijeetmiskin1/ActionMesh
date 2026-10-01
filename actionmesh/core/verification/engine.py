"""C6 — VerificationEngine.

Owns the verification *strategy* per action type and reaches its own conclusion
from observable state, independently of what the provider claims.

The design rule: a provider's `verify()` is **one input**, not the answer. This
engine inspects the same backing state the provider mutated and builds
`Evidence` from it. If the provider claims success but nothing is actually
present, the engine refutes it.

An action type with no registered strategy is reported ``UNSUPPORTED`` — not
``CONFIRMED``. That is the difference between "we checked" and "we didn't look".
"""

from __future__ import annotations

from typing import Any, Protocol

from actionmesh.core.verification.verdicts import (
    Evidence,
    VerificationOutcome,
    VerificationStatus,
    decide,
    unsupported,
)
from actionmesh.protocol.models import Action
from actionmesh.providers.base import ActionProvider, ProviderResult


class Observable(Protocol):
    """A provider that exposes the state a verifier can inspect.

    `MockProvider` satisfies this. A real adapter needs the equivalent — usually
    a read-back API call.
    """

    def observe(self, action: Action) -> dict[str, Any] | None:
        """Current real-world state for this action, or None."""


def _observed(provider: ActionProvider, action: Action) -> dict[str, Any] | None:
    getter = getattr(provider, "observe", None)
    return getter(action) if callable(getter) else None


class VerificationEngine:
    """Registry of verification strategies, keyed by action type."""

    def __init__(self) -> None:
        self._strategies: dict[str, str] = {}

    def register(self, action_type: str, strategy: str) -> None:
        """Declare how ``action_type`` is verified. Names a strategy, not code."""
        self._strategies[action_type] = strategy

    def strategy_for(self, action_type: str) -> str | None:
        return self._strategies.get(action_type)

    def known(self) -> tuple[str, ...]:
        return tuple(sorted(self._strategies))

    def __len__(self) -> int:
        return len(self._strategies)

    def verify(
        self,
        action: Action,
        result: ProviderResult,
        provider: ActionProvider,
    ) -> VerificationOutcome:
        """Decide independently whether the action's effect actually happened.

        Deliberately *not* ``await provider.verify(...)``. The provider's claim
        is one piece of evidence; the observed state is another, and it is what
        actually decides the outcome.
        """
        strategy = self._strategies.get(action.type)
        if strategy is None:
            return unsupported(action.type)

        evidence: list[Evidence] = []

        # 1. What the provider claims. Weight 1 — corroborating, not proof.
        if result.ok:
            evidence.append(
                Evidence("provider_claim", f"provider {provider.name} reported success")
            )
        else:
            evidence.append(
                Evidence("contradiction", f"provider {provider.name} reported failure")
            )

        # 2. What the world looks like now. Weight 3 — the real check.
        verdict, detail = self._assess(action, _observed(provider, action))
        if verdict == "match":
            evidence.append(Evidence("observed_state", detail))
        elif verdict == "mismatch":
            evidence.append(Evidence("contradiction", detail))
        # "uninformative" contributes nothing: it neither confirms nor refutes.

        return decide(strategy, evidence, simulated=provider.simulated)

    def _assess(self, action: Action, observed: dict[str, Any] | None) -> tuple[str, str]:
        """Tri-state assessment of the observed state.

        ``match``         the effect is observable and is this action's.
        ``mismatch``      something is observable, and it is *not* this action's.
        ``uninformative`` the observation carries nothing that ties it to this
                          action, so it proves nothing either way.
        """
        if observed is None:
            return "mismatch", f"no observable state for {action.type!r} after execution"

        if observed.get("action_type") not in (None, action.type):
            return "mismatch", f"observed state is for a different action than {action.type!r}"

        key = observed.get("idempotency_key")
        recorded = observed.get("parameters")
        if key is None and not isinstance(recorded, dict):
            return "uninformative", "observed state carries no identifying fields"

        if key is not None and key != action.idempotency_key:
            return "mismatch", "observed state belongs to a different request"

        if isinstance(recorded, dict):
            for field, value in (action.parameters or {}).items():
                if field in recorded and recorded[field] != value:
                    return "mismatch", f"observed {field!r} does not match what was requested"

        return "match", f"observed state matches: {self._summarise(observed)}"

    def _summarise(self, observed: dict[str, Any]) -> str:
        ident = observed.get("idempotency_key") or observed.get("receipt_id") or "record"
        return f"{observed.get('action_type', 'record')} {ident}"


#: Every seeded mock capability must be provable by observed state.
SEEDED_ACTIONS = (
    "message.send",
    "message.read",
    "calendar.create",
    "calendar.update",
    "calendar.delete",
    "reminder.create",
    "file.read",
    "file.move",
    "file.share",
    "device.notify",
    "device.send_file",
    "payment.prepare",
    "payment.confirm",
    "payment.verify",
    "search.web",
    "system.launch_app",
)


def default_engine() -> VerificationEngine:
    """Strategies for the seeded mock capabilities.

    Read this as the specification of *what must be provable* for each action,
    independent of which provider eventually implements it.
    """
    engine = VerificationEngine()
    for action_type in SEEDED_ACTIONS:
        engine.register(action_type, "observed_state")
    return engine


__all__ = [
    "SEEDED_ACTIONS",
    "VerificationEngine",
    "VerificationStatus",
    "default_engine",
]
