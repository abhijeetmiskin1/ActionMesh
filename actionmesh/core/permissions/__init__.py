"""C3 — Permission + Policy Engine: the authorization boundary.

The LLM proposes actions; this package decides whether they may proceed.
It never executes an action and never performs I/O.
"""

from __future__ import annotations

from actionmesh.core.permissions.catalog import (
    NAMESPACES,
    is_concrete,
    is_known,
    is_namespace,
    is_wildcard,
    known_permissions,
    permission,
    permission_names,
    permission_scope,
)
from actionmesh.core.permissions.context import (
    Consent,
    PermissionGrant,
    PolicyContext,
    PolicyOutcome,
    build_context,
)
from actionmesh.core.permissions.engine import PolicyEngine
from actionmesh.core.permissions.matching import (
    decision_rank,
    rule_matches,
    rule_specificity,
    scope_satisfied,
)
from actionmesh.core.permissions.policy import (
    default_policy,
    normalize_decision,
    policy_from_rules,
)
from actionmesh.core.permissions.risk import (
    RISK_ORDER,
    max_risk,
    risk_at_least,
    risk_rank,
)
from actionmesh.core.permissions.store import ConsentError, ConsentStore, SQLiteConsentStore

__all__ = [
    "NAMESPACES",
    "RISK_ORDER",
    "Consent",
    "ConsentError",
    "ConsentStore",
    "PermissionGrant",
    "PolicyContext",
    "PolicyEngine",
    "PolicyOutcome",
    "SQLiteConsentStore",
    "build_context",
    "decision_rank",
    "default_policy",
    "is_concrete",
    "is_known",
    "is_namespace",
    "is_wildcard",
    "known_permissions",
    "max_risk",
    "normalize_decision",
    "permission",
    "permission_names",
    "permission_scope",
    "policy_from_rules",
    "risk_at_least",
    "risk_rank",
    "rule_matches",
    "rule_specificity",
    "scope_satisfied",
]
