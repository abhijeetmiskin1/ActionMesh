"""C6 — Verification + Recovery.

The rule this package exists to enforce: **execution success is not business
success.** A provider that accepted a request, or returned a 200, has only
shown that the request was received.

* `verdicts.py` — evidence weighting and the confirm/refute/indeterminate
  decision. No evidence is never confirmation.
* `engine.py` — per-action-type strategies that reach an independent conclusion
  from observable state rather than from the provider's own claim.
* `recovery.py` — classified responses: retry, compensate, escalate, or
  explicitly *not compensatable*.
"""

from __future__ import annotations

from actionmesh.core.verification.engine import (
    SEEDED_ACTIONS,
    VerificationEngine,
    default_engine,
)
from actionmesh.core.verification.recovery import (
    IRREVERSIBLE,
    RETRYABLE,
    Recovery,
    RecoveryPlan,
    RecoveryPlanner,
)
from actionmesh.core.verification.verdicts import (
    EVIDENCE_WEIGHT,
    Evidence,
    VerificationOutcome,
    VerificationStatus,
    decide,
    unsupported,
)

__all__ = [
    "EVIDENCE_WEIGHT",
    "IRREVERSIBLE",
    "RETRYABLE",
    "SEEDED_ACTIONS",
    "Evidence",
    "Recovery",
    "RecoveryPlan",
    "RecoveryPlanner",
    "VerificationEngine",
    "VerificationOutcome",
    "VerificationStatus",
    "decide",
    "default_engine",
    "unsupported",
]
