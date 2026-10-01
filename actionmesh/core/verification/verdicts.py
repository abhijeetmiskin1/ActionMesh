"""C6 — Verification.

Verification is a **separate question** from execution. A provider returning a
success response, or even an HTTP 200, is evidence that a request was *accepted*
— not that the intended effect happened.

This module owns the *decision*:

* each action type gets an explicit verification strategy;
* a strategy states what "confirmed" actually means for that action;
* an action with no registered strategy is **NOT verified** — absence of a check
  is not evidence of success;
* a provider's own claim is treated as one input among several, never as proof.

Recovery (retry, compensation, reconciliation) lives in
:mod:`actionmesh.core.verification.recovery`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from actionmesh.protocol.models import FailureKind, VerificationResult


class VerificationStatus(StrEnum):
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    #: The check could not be completed (timeout, provider unavailable).
    INDETERMINATE = "indeterminate"
    #: No strategy exists for this action type — deliberately not "confirmed".
    UNSUPPORTED = "unsupported"


#: How much a given piece of evidence counts towards confidence.
EVIDENCE_WEIGHT: dict[str, int] = {
    # Observed side effect in a system we control, checked post-hoc.
    "observed_state": 3,
    # The provider's own account of what it did.
    "provider_claim": 1,
    # A precondition the caller asserted; treated as a hint, not proof.
    "caller_assertion": 0,
}


@dataclass(frozen=True)
class Evidence:
    """One input to a verification decision, with an explicit weight."""

    kind: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def weight(self) -> int:
        return EVIDENCE_WEIGHT.get(self.kind, 0)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "weight": self.weight}


@dataclass(frozen=True)
class VerificationOutcome:
    """The verdict. ``confirmed`` requires positive evidence, never its absence."""

    status: VerificationStatus
    strategy: str
    detail: str = ""
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)
    failure_kind: FailureKind = FailureKind.UNKNOWN
    simulated: bool = True

    @property
    def verified(self) -> bool:
        return self.status is VerificationStatus.CONFIRMED

    @property
    def decidable(self) -> bool:
        """False when we could not reach a conclusion either way."""
        return self.status in (VerificationStatus.CONFIRMED, VerificationStatus.REFUTED)

    def to_result(self) -> VerificationResult:
        """Project onto the protocol-level result the executor consumes."""
        return VerificationResult(
            verified=self.verified,
            method=self.strategy,
            detail=self.detail,
            simulated=self.simulated,
            evidence={"strategy": self.strategy, "status": self.status.value},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "strategy": self.strategy,
            "detail": self.detail,
            "decidable": self.decidable,
            "simulated": self.simulated,
            "evidence": [e.to_dict() for e in self.evidence],
        }


#: An outcome is confirmed only when the evidence includes something beyond
#: the provider's own claim. `provider_claim` alone scores 1, so the threshold
#: sits above it: a provider saying "it worked" is corroboration, not proof.
CONFIRM_THRESHOLD = 2
REFUTE_THRESHOLD = 1


def decide(
    strategy: str, evidence: list[Evidence], *, simulated: bool = True
) -> VerificationOutcome:
    """Turn evidence into a verdict.

    The rule that matters: **no evidence is not confirmation.** An empty or
    purely-corroborating evidence set yields ``INDETERMINATE`` or
    ``UNSUPPORTED``, never ``CONFIRMED``.
    """
    if not evidence:
        return VerificationOutcome(
            status=VerificationStatus.INDETERMINATE,
            strategy=strategy,
            detail="no evidence collected; absence of evidence is not confirmation",
            simulated=simulated,
        )

    positive = sum(e.weight for e in evidence if e.kind != "contradiction")
    contradictions = [e for e in evidence if e.kind == "contradiction"]

    # Any contradiction is decisive. If part of the evidence says the effect did
    # not happen, we cannot report it as confirmed — being wrong in the safe
    # direction is the whole point of verifying separately.
    if contradictions:
        details = "; ".join(e.detail for e in contradictions)
        return VerificationOutcome(
            status=VerificationStatus.REFUTED,
            strategy=strategy,
            detail=details or "verification contradicted",
            evidence=tuple(evidence),
            failure_kind=FailureKind.PERMANENT,
            simulated=simulated,
        )

    if positive >= CONFIRM_THRESHOLD:
        best = max(evidence, key=lambda e: e.weight)
        return VerificationOutcome(
            status=VerificationStatus.CONFIRMED,
            strategy=strategy,
            detail=f"confirmed by {best.kind}: {best.detail}",
            evidence=tuple(evidence),
            simulated=simulated,
        )

    # Only zero-weight evidence (e.g. a caller assertion) was offered.
    return VerificationOutcome(
        status=VerificationStatus.INDETERMINATE,
        strategy=strategy,
        detail="evidence carries no independent weight",
        evidence=tuple(evidence),
        failure_kind=FailureKind.UNKNOWN,
        simulated=simulated,
    )


def unsupported(action_type: str) -> VerificationOutcome:
    """No strategy registered for this action type."""
    return VerificationOutcome(
        status=VerificationStatus.UNSUPPORTED,
        strategy="none",
        detail=(
            f"no verification strategy registered for {action_type!r}; "
            "refusing to report success"
        ),
        simulated=True,
    )


__all__ = [
    "CONFIRM_THRESHOLD",
    "EVIDENCE_WEIGHT",
    "REFUTE_THRESHOLD",
    "Evidence",
    "VerificationOutcome",
    "VerificationStatus",
    "decide",
    "unsupported",
]
