"""PolicyEngine (C3) — the trusted authorization boundary.

The LLM (or any caller) *proposes* an action; only this engine *decides*.
It never executes anything and never performs I/O.

Deterministic precedence (documented in ``docs/permissions.md``)
---------------------------------------------------------------
1. Missing/invalid context, unknown action, definition mismatch, no actor, or a
   definition that declares no permissions -> **DENY**.
2. Effective risk = max(action risk, definition risk). Risk is raised, never lowered.
3. Permission requirements are gathered (definition + matching rule) and matched
   **exactly** against active grants (expiry + ``max_risk`` cap honoured).
   Any missing requirement -> **DENY**.
4. A rule is selected: highest specificity wins (exact > ``ns.*`` > ``*``);
   equally specific rules resolve to the most restrictive decision.
5. No matching rule -> ``policy.default_decision`` (default **DENY**).
6. Risk gate: CRITICAL always requires approval; HIGH requires approval unless
   the policy context explicitly opts in via ``allow_high_risk`` (server-side only).
7. A DENY can never be upgraded into ALLOW by approval state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from actionmesh.core.permissions.catalog import is_concrete
from actionmesh.core.permissions.context import (
    PermissionGrant,
    PolicyContext,
    PolicyOutcome,
)
from actionmesh.core.permissions.matching import (
    decision_rank,
    rule_matches,
    rule_specificity,
    scope_satisfied,
)
from actionmesh.core.permissions.risk import max_risk, risk_at_least
from actionmesh.protocol.models import (
    Policy,
    PolicyDecision,
    PolicyRule,
    RiskLevel,
)


class PolicyEngine:
    """Stateless, side-effect-free, deterministic policy evaluator."""

    def select_rule(self, policy: Policy | None, action_name: str) -> PolicyRule | None:
        """Highest-specificity matching rule; ties break to the most restrictive."""
        rules = list(getattr(policy, "rules", None) or [])
        candidates = [
            (i, r)
            for i, r in enumerate(rules)
            if rule_matches(getattr(r, "action", ""), action_name)
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda pair: (
                -rule_specificity(pair[1].action),
                # Descending restrictiveness: when two rules are equally specific
                # the MOST restrictive decision must win. Sorting ascending here
                # would let an `allow` shadow a `deny` — a real authorization bypass.
                -decision_rank(pair[1].decision),
                pair[0],
            )
        )
        return candidates[0][1]

    def _granted(
        self, context: PolicyContext, scope: str, now: datetime, risk: RiskLevel
    ) -> bool:
        for grant in context.grants:
            if not isinstance(grant, PermissionGrant):
                continue
            if scope_satisfied(grant.scope, scope) and grant.is_active(now, risk):
                return True
        return False

    def _deny(self, action_name: str, reasons: list[str], **kw: Any) -> PolicyOutcome:
        return PolicyOutcome(
            decision=PolicyDecision.DENY,
            action=action_name,
            requires_approval=False,
            reasons=reasons,
            **kw,
        )

    def evaluate(self, context: PolicyContext) -> PolicyOutcome:
        """Decide whether ``context`` may proceed. Deterministic and auditable."""
        action = context.action
        definition = context.definition
        name = getattr(action, "type", "") or ""

        # 1. Structural preconditions — everything unknown fails closed.
        if action is None or not name:
            return self._deny(name, ["missing action in policy context"])
        if definition is None:
            return self._deny(name, [f"unknown action {name!r}: no registry definition"])
        if definition.name != name:
            return self._deny(
                name, [f"definition mismatch: {definition.name!r} != {name!r}"]
            )
        if context.actor is None or not getattr(context.actor, "id", ""):
            return self._deny(name, ["missing actor identity (fails closed)"])

        now = context.moment()

        # 2. Risk: authoritative metadata can only raise it.
        effective = max_risk(action.risk_level, definition.risk_level)
        reasons: list[str] = []
        if effective is not action.risk_level:
            reasons.append(
                f"risk lifted {action.risk_level.value} -> {effective.value} (never lowered)"
            )

        # 3. Requirements: definition + any permissions the matched rule adds.
        rule = self.select_rule(context.policy, name)
        requirements = list(definition.permissions or [])
        if rule is not None:
            requirements.extend(list(rule.permissions or []))
        requirements = sorted({r for r in requirements if isinstance(r, str)})

        if not requirements:
            return self._deny(
                name,
                ["definition declares no permissions; refusing to authorize"],
                effective_risk=effective,
            )

        invalid = [r for r in requirements if not is_concrete(r)]
        if invalid:
            return self._deny(
                name,
                [f"invalid permission requirement(s): {invalid}"],
                effective_risk=effective,
                required_permissions=requirements,
            )

        missing = [r for r in requirements if not self._granted(context, r, now, effective)]
        if missing:
            return self._deny(
                name,
                [f"missing permission(s): {missing}"],
                effective_risk=effective,
                matched_rule=getattr(rule, "action", None) if rule is not None else None,
                required_permissions=requirements,
                missing_permissions=missing,
            )

        # 4/5. Decision: matched rule, else the policy default (DENY).
        decision = self._resolve_decision(context.policy, rule, reasons)
        limits = dict(getattr(rule, "limits", None) or {}) if rule is not None else {}
        matched = getattr(rule, "action", None) if rule is not None else None
        requires_approval = False

        # 6. Risk gate. CRITICAL is unconditional.
        if decision is not PolicyDecision.DENY:
            if risk_at_least(effective, RiskLevel.CRITICAL):
                decision = PolicyDecision.ASK
                requires_approval = True
                reasons.append("CRITICAL risk always requires explicit user approval")
            elif risk_at_least(effective, RiskLevel.HIGH) and not context.allow_high_risk:
                decision = PolicyDecision.ASK
                requires_approval = True
                reasons.append("HIGH risk requires approval unless policy opts in")

        if decision is PolicyDecision.ASK:
            requires_approval = True
            if context.is_approved(action.action_id):
                decision = PolicyDecision.ALLOW
                requires_approval = False
                reasons.append("approval already granted for this action")
            else:
                reasons.append("awaiting user approval")

        if not reasons:
            # A decision with no explanation is the one a reader most needs to
            # understand, so it is never left blank -- but it must not be narrated as
            # the decision it is not. Falling through to the policy default (DENY)
            # arrives here with nothing appended, and calling that "policy allowed"
            # would seal the opposite of what happened into the audit chain.
            if decision is PolicyDecision.DENY:
                reasons.append(
                    "no policy rule allows this action; the default decision is deny"
                )
            elif decision is PolicyDecision.ASK:
                reasons.append("awaiting user approval")
            else:
                reasons.append("policy allowed")

        return PolicyOutcome(
            decision=decision,
            action=name,
            effective_risk=effective,
            requires_approval=requires_approval,
            matched_rule=matched,
            required_permissions=requirements,
            reasons=reasons,
            limits=limits,
        )

    def _resolve_decision(
        self, policy: Policy | None, rule: PolicyRule | None, reasons: list[str]
    ) -> PolicyDecision:
        from actionmesh.core.permissions.policy import normalize_decision

        if rule is not None:
            raw = getattr(rule, "decision", None)
            if isinstance(raw, PolicyDecision):
                return raw
            resolved = normalize_decision(raw)
            if resolved is not PolicyDecision.DENY:
                return resolved
            reasons.append("rule decision unrecognized; treated as DENY")
            return PolicyDecision.DENY
        return PolicyDecision(getattr(policy, "default_decision", PolicyDecision.DENY))

    def explain(self, context: PolicyContext) -> dict[str, Any]:
        """JSON-ready audit record of a decision."""
        outcome = self.evaluate(context)
        return {
            "action": outcome.action,
            "decision": outcome.decision.value,
            "authorizes_execution": outcome.authorizes_execution(),
            "requires_approval": outcome.requires_approval,
            "effective_risk": outcome.effective_risk.value,
            "matched_rule": outcome.matched_rule,
            "required_permissions": list(outcome.required_permissions),
            "missing_permissions": list(outcome.missing_permissions),
            "limits": dict(outcome.limits),
            "reasons": list(outcome.reasons),
        }


__all__ = ["PolicyEngine"]
