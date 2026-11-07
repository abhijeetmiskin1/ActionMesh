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

import hashlib
from typing import Any, Protocol
from urllib.parse import urlsplit

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


def _norm_url(value: Any) -> tuple | None:
    """Canonical form for URL-typed fields, or None for non-URL values.

    Browsers canonicalize URLs: 'https://example.com' becomes
    'https://example.com/' on load. The requested spelling and the
    observed spelling of the same place must not read as a mismatch,
    while a different scheme/host/path/query is still a mismatch.
    """
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    path = parts.path.rstrip("/") or "/"
    return (scheme, host, port, path, parts.query)


def _fields_match(requested: Any, observed: Any) -> bool:
    """True when a requested field and its observed value agree.

    URLs compare in canonical form; everything else compares exactly.
    """
    if requested == observed:
        return True
    left, right = _norm_url(requested), _norm_url(observed)
    if left is not None and right is not None:
        return left == right
    return False


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

        if observed.get("postcondition_observed") is False:
            return "uninformative", "the requested effect has no independent read-back"
        key = observed.get("idempotency_key")
        recorded = observed.get("parameters")
        if key is None and not isinstance(recorded, dict):
            return "uninformative", "observed state carries no identifying fields"

        if key is not None and key != action.idempotency_key:
            return "mismatch", "observed state belongs to a different request"

        if isinstance(recorded, dict):
            for field, value in (action.parameters or {}).items():
                if field in recorded and not _fields_match(value, recorded[field]):
                    return "mismatch", f"observed {field!r} does not match what was requested"

        if key is None and (
            not action.parameters or not isinstance(recorded, dict)
            or any(field not in recorded for field in action.parameters)
        ):
            return "uninformative", "observation lacks request identity or complete identifying fields"
        matched_fields = isinstance(recorded, dict) and any(
            field in recorded for field in (action.parameters or {})
        )
        state = observed.get("state")
        if not matched_fields and not (key and isinstance(state, dict) and state):
            return "uninformative", "observation contains no identifying postcondition"
        if action.type in {"device.file.write", "device.files.create"}:
            content = action.parameters.get("content")
            if isinstance(content, str):
                digest = state.get("sha256") if isinstance(state, dict) else None
                if digest is None:
                    return "uninformative", "write observation has no content digest"
                if digest != hashlib.sha256(content.encode("utf-8")).hexdigest():
                    return "mismatch", "observed file digest does not match requested content"
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
