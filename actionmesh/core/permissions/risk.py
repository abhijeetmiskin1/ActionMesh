"""Risk helpers (C3). Risk may be raised by trusted metadata, never lowered."""

from __future__ import annotations

from actionmesh.protocol.models import RiskLevel

RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def risk_rank(risk: RiskLevel | str) -> int:
    """Numeric rank. An unrecognized risk is treated as CRITICAL (fail closed)."""
    if isinstance(risk, RiskLevel):
        return RISK_ORDER[risk]
    try:
        return RISK_ORDER[RiskLevel(str(risk))]
    except ValueError:
        return RISK_ORDER[RiskLevel.CRITICAL]


def max_risk(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    """Return the higher of two risk levels."""
    return a if risk_rank(a) >= risk_rank(b) else b


def risk_at_least(risk: RiskLevel, threshold: RiskLevel) -> bool:
    return risk_rank(risk) >= risk_rank(threshold)


__all__ = ["RISK_ORDER", "max_risk", "risk_at_least", "risk_rank"]
