"""ActionRegistry (C2) — register, discover, resolve, version.

The registry is a *description* layer. It holds capability metadata, guarantees
that a resolved action matches a registered definition, and refuses unknown
actions. It never executes an action, never calls a provider and never decides
authorization (that is C3's :class:`PolicyEngine`).
"""

from __future__ import annotations

# ``ActionRegistry.list`` shadows the builtin ``list`` inside this class body, so a
# bare ``list[...]`` annotation written here resolves to that method rather than to
# the builtin. Alias the builtin so annotations on this class stay honest.
from builtins import list as _builtin_list
from dataclasses import dataclass, field
from typing import Any

from actionmesh.core.registry.defs import check_params, validate_definition, version_key
from actionmesh.core.registry.errors import DuplicateError, UnknownActionError
from actionmesh.core.registry.seeds import seed_definitions
from actionmesh.protocol.models import (
    Action,
    ActionDefinition,
    ActionState,
    Capability,
    RiskLevel,
)
from actionmesh.protocol.validate import validate_action

_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def max_risk(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    """Return the higher of two risk levels. Risk is only ever raised, never lowered."""
    return a if _RISK_ORDER[a] >= _RISK_ORDER[b] else b


@dataclass
class ResolvedAction:
    """An action bound to its registry definition, with effective risk."""

    action: Action
    definition: ActionDefinition
    effective_risk: RiskLevel
    warnings: list[str] = field(default_factory=list)


@dataclass
class ActionRegistry:
    _defs: dict[tuple[str, str], ActionDefinition] = field(default_factory=dict)

    def register(self, defn: ActionDefinition) -> ActionDefinition:
        validate_definition(defn)
        key = (defn.name, defn.version)
        if key in self._defs:
            raise DuplicateError(f"action {defn.name} v{defn.version} already registered")
        self._defs[key] = defn
        return defn

    @classmethod
    def with_seeds(cls) -> ActionRegistry:
        reg = cls()
        for d in seed_definitions():
            reg.register(d)
        return reg

    def versions(self, name: str) -> list[str]:
        available = sorted((v for n, v in self._defs if n == name), key=version_key)
        if not available:
            raise UnknownActionError(f"unknown action: {name!r}")
        return available

    def get(self, name: str, version: str | None = None) -> ActionDefinition:
        """Fetch a definition. Defaults to the highest registered version."""
        if version is None:
            version = self.versions(name)[-1]
        defn = self._defs.get((name, version))
        if defn is None:
            raise UnknownActionError(f"unknown action {name!r} version {version!r}")
        return defn

    def list(self) -> list[ActionDefinition]:
        """All definitions, name asc then version asc."""
        return [self._defs[k] for k in sorted(self._defs, key=lambda k: (k[0], version_key(k[1])))]

    def has(self, name: str) -> bool:
        """True when ``name`` is registered at any version.

        A membership test that does not raise, because device capability checks
        (C19) probe many names at once — turning each lookup into exception
        control flow would hide the one failure that matters.
        """
        return isinstance(name, str) and any(known == name for known, _ in self._defs)

    def discover(
        self,
        *,
        name: str | None = None,
        provider: str | None = None,
        risk: RiskLevel | None = None,
        permission: str | None = None,
    ) -> _builtin_list[Capability]:
        """Filter registered capabilities by name, provider, risk or permission.

        Only the *latest* version of each action is returned: a caller asking
        "what can I do?" wants current capabilities, not historical ones.
        """
        out: list[Capability] = []
        seen: set[str] = set()
        for defn in self.list():
            if defn.name in seen:
                continue
            seen.add(defn.name)
            if name and name not in defn.name:
                continue
            if provider and defn.provider != provider:
                continue
            if risk is not None and _RISK_ORDER[defn.risk_level] < _RISK_ORDER[risk]:
                continue
            if permission and not any(permission in p for p in defn.permissions):
                continue
            out.append(
                Capability(
                    action=defn.name,
                    version=defn.version,
                    provider=defn.provider,
                    permissions=list(defn.permissions),
                    risk_level=defn.risk_level,
                )
            )
        return out

    def resolve(self, raw: Any) -> ResolvedAction:
        """Bind an untrusted action to its definition.

        Order matters: the action is shape-validated first, then looked up, then
        parameter-checked. A malformed action can never reach execution, and an
        unknown action can never silently resolve.
        """
        action = validate_action(raw) if not isinstance(raw, Action) else raw
        # Untrusted input may not dictate lifecycle state.
        action.state = ActionState.CREATED

        defn = self.get(action.type)
        warnings = check_params(defn, action.parameters, "action")

        effective = max_risk(action.risk_level, defn.risk_level)
        if effective is not action.risk_level:
            warnings.append(
                f"risk lifted {action.risk_level.value} -> {effective.value} "
                "(registry metadata is authoritative; risk is never lowered)"
            )
        return ResolvedAction(
            action=action,
            definition=defn,
            effective_risk=effective,
            warnings=warnings,
        )

    def __len__(self) -> int:
        return len(self._defs)


__all__ = ["ActionRegistry", "ResolvedAction", "max_risk"]
