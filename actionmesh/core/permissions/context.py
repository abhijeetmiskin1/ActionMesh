"""Authorization context types (C3) — the only things the engine may read.

Keeping this a typed model (rather than a free dict) is a security measure: the
engine cannot accidentally read a caller-supplied field such as
``parameters["authorized"]`` and treat it as a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from actionmesh.core.permissions.risk import risk_rank
from actionmesh.protocol.models import (
    Action,
    ActionDefinition,
    ApprovalRequest,
    Identity,
    MeshModel,
    Policy,
    PolicyDecision,
    RiskLevel,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class PermissionGrant(MeshModel):
    """A consent record: *this* scope, for *this* actor, capped at *this* risk."""

    scope: str = ""
    granted_by: str = "user"
    #: Ceiling on what this grant can authorize. A grant capped at HIGH can never
    #: authorize a CRITICAL action, no matter what the policy says.
    max_risk: RiskLevel = RiskLevel.HIGH
    granted_at: datetime = field(default_factory=_utcnow)
    expires_at: datetime | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or _utcnow()) >= self.expires_at

    def is_active(self, now: datetime | None = None, risk: RiskLevel | None = None) -> bool:
        """Active = not expired, and (if given) covering the required risk."""
        moment = now or _utcnow()
        if self.is_expired(moment):
            return False
        return not (risk is not None and risk_rank(risk) > risk_rank(self.max_risk))


@dataclass
class Consent(MeshModel):
    """A recorded user consent: a grant plus why/when it was collected."""

    scope: str = ""
    granted_by: str = "user"
    reason: str = ""
    max_risk: RiskLevel = RiskLevel.HIGH
    granted_at: datetime = field(default_factory=_utcnow)
    expires_at: datetime | None = None

    def to_grant(self) -> PermissionGrant:
        return PermissionGrant(
            scope=self.scope,
            granted_by=self.granted_by,
            max_risk=self.max_risk,
            granted_at=self.granted_at,
            expires_at=self.expires_at,
        )


@dataclass
class PolicyContext(MeshModel):
    """Everything the engine needs — and nothing it does not."""

    action: Action | None = None
    definition: ActionDefinition | None = None
    actor: Identity | None = None
    policy: Policy = field(default_factory=Policy)
    grants: list[PermissionGrant] = field(default_factory=list)
    approvals: list[ApprovalRequest] = field(default_factory=list)
    #: Explicit, server-side opt-in required before HIGH risk may auto-allow.
    #: This is never read from a request body.
    allow_high_risk: bool = False
    now: datetime | None = None

    def moment(self) -> datetime:
        return self.now or _utcnow()

    def is_approved(self, action_id: str) -> bool:
        return any(
            a.action_id == action_id and a.status == "approved" for a in self.approvals
        )


@dataclass
class PolicyOutcome(MeshModel):
    """The engine's deterministic verdict. It NEVER executes anything."""

    decision: PolicyDecision = PolicyDecision.DENY
    action: str = ""
    effective_risk: RiskLevel = RiskLevel.HIGH
    requires_approval: bool = False
    matched_rule: str | None = None
    required_permissions: list[str] = field(default_factory=list)
    missing_permissions: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)

    def authorizes_execution(self) -> bool:
        """True only for a granted decision that is not awaiting approval."""
        return (
            self.decision
            in (PolicyDecision.ALLOW, PolicyDecision.ALLOW_WITH_LIMIT)
            and not self.requires_approval
        )


def build_context(
    action: Action | None,
    definition: ActionDefinition | None,
    actor: Identity | None,
    *,
    grants: list[PermissionGrant] | None = None,
    policy: Policy | None = None,
    approvals: list[ApprovalRequest] | None = None,
    allow_high_risk: bool = False,
    now: datetime | None = None,
) -> PolicyContext:
    """Ergonomic constructor — callers cannot forget a typed field."""
    return PolicyContext(
        action=action,
        definition=definition,
        actor=actor,
        policy=policy if policy is not None else Policy(),
        grants=list(grants or []),
        approvals=list(approvals or []),
        allow_high_risk=allow_high_risk,
        now=now,
    )


__all__ = [
    "Consent",
    "PermissionGrant",
    "PolicyContext",
    "PolicyOutcome",
    "build_context",
]
