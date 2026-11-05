"""Approval expiry: a CRITICAL approval must not live forever.

Credential, consent and pairing all carry an expiry that the authorization
path enforces (PermissionGrant.is_active, Credential.is_expired,
PairingCode.is_expired). ApprovalRequest names an "expired" status in its
docstring but carries no expires_at, PolicyContext.is_approved() checks
only action_id+status, and ExecutionEngine._approvals is never evicted —
so an approval granted once authorizes its action_id indefinitely.

These tests encode the invariant that a stale approval must NOT authorize
a CRITICAL execution. They FAIL against current production code; that
failure is the finding. Worker D must not edit production code — the lead
owns the fix (e.g. an approval TTL checked in is_approved / dispatch).
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from actionmesh.core.permissions.context import PermissionGrant, build_context
from actionmesh.core.permissions.engine import PolicyEngine
from actionmesh.protocol.models import (
    Action,
    ActionDefinition,
    ApprovalRequest,
    Identity,
    Policy,
    PolicyDecision,
    PolicyRule,
    RiskLevel,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _critical_context(*, approvals: list[ApprovalRequest]) -> object:
    return build_context(
        Action(action_id="act-crit-1", type="booking.submit", risk_level=RiskLevel.CRITICAL, parameters={}),
        ActionDefinition(name="booking.submit", risk_level=RiskLevel.CRITICAL, permissions=["payment.execute"]),
        Identity(id="alice", kind="user", display="Alice"),
        grants=[PermissionGrant(scope="payment.execute", granted_by="alice", max_risk=RiskLevel.CRITICAL)],
        policy=Policy(rules=[PolicyRule(action="booking.submit", decision=PolicyDecision.ALLOW)]),
        approvals=approvals,
        now=NOW,
    )


class TestApprovalExpiry(unittest.TestCase):
    def test_stale_approval_does_not_authorize_critical(self) -> None:
        """A 30-day-old approval for the same action_id must not flip ASK->ALLOW."""
        stale = ApprovalRequest(
            action_id="act-crit-1", status="approved", created_at=NOW - timedelta(days=30)
        )
        outcome = PolicyEngine().evaluate(_critical_context(approvals=[stale]))  # type: ignore[arg-type]
        self.assertFalse(
            outcome.authorizes_execution(),
            f"stale approval still authorizes execution: {outcome}",
        )

    def test_fresh_approval_still_authorizes_critical(self) -> None:
        """Control: a just-granted approval must keep working (not over-blocking)."""
        fresh = ApprovalRequest(action_id="act-crit-1", status="approved", created_at=NOW)
        outcome = PolicyEngine().evaluate(_critical_context(approvals=[fresh]))  # type: ignore[arg-type]
        self.assertTrue(outcome.authorizes_execution(), outcome)

    def test_rejected_approval_never_authorizes(self) -> None:
        rejected = ApprovalRequest(action_id="act-crit-1", status="rejected", created_at=NOW)
        outcome = PolicyEngine().evaluate(_critical_context(approvals=[rejected]))  # type: ignore[arg-type]
        self.assertFalse(outcome.authorizes_execution(), outcome)


if __name__ == "__main__":
    unittest.main()
