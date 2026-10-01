"""Permission matching rules (C3).

Two distinct matching regimes, and the distinction is a security boundary:

* **Grants** (what the user consented to) are matched EXACTLY against a concrete
  requirement. A grant may never be a wildcard — otherwise consenting to
  ``message.*`` would silently authorize ``message.send`` forever.
* **Policy rules** (written by the operator) may use wildcards so rules stay
  readable (``message.*`` → ask). A wildcard rule is trusted configuration, and
  the engine resolves it to the *most restrictive* matching decision anyway.
"""

from __future__ import annotations

from actionmesh.protocol.models import PolicyDecision

#: More restrictive decisions win when two equally specific rules match.
DECISION_RESTRICTIVENESS: dict[PolicyDecision, int] = {
    PolicyDecision.ALLOW: 0,
    PolicyDecision.ALLOW_WITH_LIMIT: 1,
    PolicyDecision.ASK: 2,
    PolicyDecision.DENY: 3,
}


def decision_rank(decision: PolicyDecision) -> int:
    """Higher means more restrictive. Unknown → DENY (fail closed)."""
    if isinstance(decision, PolicyDecision):
        return DECISION_RESTRICTIVENESS[decision]
    return DECISION_RESTRICTIVENESS[PolicyDecision.DENY]


def rule_specificity(pattern: str) -> int:
    """3 = exact, 2 = namespace wildcard, 1 = global wildcard, 0 = unknown."""
    if pattern == "*":
        return 1
    if pattern.endswith(".*"):
        return 2
    if "." in pattern:
        return 3
    return 0


def rule_matches(pattern: str, action: str) -> bool:
    """Does a policy rule pattern apply to this action name?"""
    if not isinstance(pattern, str) or not pattern:
        return False
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return action.startswith(pattern[:-1])
    return pattern == action


def scope_satisfied(grant_scope: str, required_scope: str) -> bool:
    """Exact-only grant matching.

    A grant authorizes a requirement only when the strings are identical.
    Wildcards are never honoured on the grant side, which is what stops a
    ``*`` grant from authorizing the whole system.
    """
    if not isinstance(grant_scope, str) or not isinstance(required_scope, str):
        return False
    return grant_scope == required_scope


__all__ = [
    "DECISION_RESTRICTIVENESS",
    "decision_rank",
    "rule_matches",
    "rule_specificity",
    "scope_satisfied",
]
