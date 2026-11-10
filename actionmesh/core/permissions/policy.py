"""Policy construction from untrusted config (C3).

Two entry points, both fail-closed:

* :func:`policy_from_rules` — untrusted config (YAML/JSON/HTTP) → :class:`Policy`.
  Malformed entries become DENY rather than being dropped, so a typo can never
  silently widen access.
* :func:`default_policy` — the local-first baseline. Unlisted actions fall
  through to DENY, so a newly registered capability is never auto-allowed.
"""

from __future__ import annotations

from typing import Any

from actionmesh.protocol.models import Policy, PolicyDecision, PolicyRule

#: External spellings accepted for a decision.
_NORMALIZE: dict[str, PolicyDecision] = {
    "allow": PolicyDecision.ALLOW,
    "allow_with_limit": PolicyDecision.ALLOW_WITH_LIMIT,
    "ask": PolicyDecision.ASK,
    "deny": PolicyDecision.DENY,
    # Aliases: expressing "no without a human" as DENY + confirmation is the
    # same outcome as ASK, so they normalize to one canonical decision rather
    # than creating contradictory semantics.
    "deny_without_confirmation": PolicyDecision.ASK,
    "require_confirmation": PolicyDecision.ASK,
    "confirm": PolicyDecision.ASK,
}


def normalize_decision(value: Any) -> PolicyDecision:
    """Map an external spelling onto a canonical decision. Unknown → DENY."""
    if isinstance(value, PolicyDecision):
        return value
    return _NORMALIZE.get(
        str(getattr(value, "value", value)).strip().lower(), PolicyDecision.DENY
    )


def _coerce_rule(entry: Any) -> PolicyRule:
    """Normalize one rule entry. Never raises: bad input becomes a DENY rule."""
    if isinstance(entry, PolicyRule):
        limits = entry.limits if isinstance(entry.limits, dict) else {}
        permissions = [p for p in (entry.permissions or []) if isinstance(p, str)]
        return PolicyRule(
            action=str(entry.action),
            decision=normalize_decision(entry.decision),
            note=str(entry.note or ""),
            limits=dict(limits),
            permissions=permissions,
        )
    if not isinstance(entry, dict):
        return PolicyRule(
            action="*", decision=PolicyDecision.DENY, note="malformed rule entry"
        )
    action = entry.get("action")
    if not isinstance(action, str) or not action.strip():
        return PolicyRule(action="*", decision=PolicyDecision.DENY, note="rule without action")
    raw_limits = entry.get("limits")
    raw_permissions = entry.get("permissions")
    return PolicyRule(
        action=action.strip(),
        decision=normalize_decision(entry.get("decision")),
        note=str(entry.get("note", "")),
        limits=dict(raw_limits) if isinstance(raw_limits, dict) else {},
        permissions=[p for p in raw_permissions if isinstance(p, str)]
        if isinstance(raw_permissions, list)
        else [],
    )


def policy_from_rules(raw_rules: Any, raw_default: Any = "deny") -> Policy:
    """Build a Policy from untrusted config. Accepts dicts or ``PolicyRule`` objects."""
    rules = [_coerce_rule(entry) for entry in list(raw_rules or [])]
    return Policy(rules=rules, default_decision=normalize_decision(raw_default))


def default_policy() -> Policy:
    """Local-first baseline policy. Deliberately conservative.

    Unlisted actions fall through to DENY, so a newly registered capability is
    never silently auto-allowed. A matching rule is still subject to the risk
    gate: HIGH needs a server-side opt-in and CRITICAL always asks.
    """
    allow = ("calendar.create", "calendar.update", "reminder.create", "device.notify",
             "search.web", "payment.verify", "device.app.launch_native", "device.app.status_native",
             "browser.open", "browser.search", "github.repositories.search",
             "github.issues.list", "google.drive.search")
    ask = ("message.read", "message.send", "file.read", "file.move", "calendar.delete",
           "file.share", "device.send_file", "system.launch_app", "payment.prepare",
           "payment.confirm")
    rules = [PolicyRule(action=a, decision=PolicyDecision.ALLOW) for a in allow]
    rules += [PolicyRule(action=a, decision=PolicyDecision.ASK) for a in ask]
    return Policy(rules=rules, default_decision=PolicyDecision.DENY)


__all__ = ["default_policy", "normalize_decision", "policy_from_rules"]
