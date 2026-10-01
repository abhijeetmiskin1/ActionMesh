"""Closed permission catalog (C3).

The catalog is the *complete* set of authorization scopes ActionMesh understands.
A closed set is what makes "unknown permission" a detectable, fail-closed
condition rather than a silent pass.

Scope grammar
-------------
``message.send``   concrete scope — a grant may name it
``message.*``      namespace wildcard — usable only inside a trusted policy rule
``*``              global wildcard — usable only inside a trusted policy rule
"""

from __future__ import annotations

from actionmesh.protocol.models import Permission, RiskLevel

_SCOPES: dict[str, Permission] = {
    # calendar
    "calendar.read": Permission("calendar.read", "Read calendar events", RiskLevel.LOW),
    "calendar.write": Permission("calendar.write", "Create or modify events", RiskLevel.LOW),
    "calendar.delete": Permission("calendar.delete", "Delete calendar events", RiskLevel.HIGH),
    # messaging
    "message.read": Permission("message.read", "Read messages", RiskLevel.MEDIUM),
    "message.send": Permission("message.send", "Send messages", RiskLevel.MEDIUM),
    # files
    "file.read": Permission("file.read", "Read file contents", RiskLevel.MEDIUM),
    "file.write": Permission("file.write", "Modify files", RiskLevel.MEDIUM),
    "file.share": Permission("file.share", "Share files externally", RiskLevel.HIGH),
    "file.delete": Permission("file.delete", "Delete files", RiskLevel.HIGH),
    # devices
    "device.notify": Permission("device.notify", "Show notifications on a device", RiskLevel.LOW),
    "device.file_transfer": Permission(
        "device.file_transfer", "Transfer files to another device", RiskLevel.MEDIUM
    ),
    "device.control": Permission("device.control", "Control a device remotely", RiskLevel.HIGH),
    # location
    "location.read": Permission("location.read", "Read device location", RiskLevel.HIGH),
    # payments
    "payment.prepare": Permission("payment.prepare", "Prepare a payment intent", RiskLevel.MEDIUM),
    "payment.execute": Permission("payment.execute", "Execute a payment", RiskLevel.CRITICAL),
    "payment.verify": Permission("payment.verify", "Verify a transaction", RiskLevel.LOW),
    # system
    "system.app.launch": Permission("system.app.launch", "Launch an application", RiskLevel.MEDIUM),
    "system.shell": Permission("system.shell", "Run shell commands", RiskLevel.CRITICAL),
    # misc
    "search.web": Permission("search.web", "Perform a web search", RiskLevel.LOW),
    "reminder.write": Permission("reminder.write", "Create reminders", RiskLevel.LOW),
}

#: Namespaces (the part before the dot) that are valid.
NAMESPACES: tuple[str, ...] = tuple(sorted({name.split(".")[0] for name in _SCOPES}))


def known_permissions() -> tuple[str, ...]:
    """Every concrete scope, sorted."""
    return tuple(sorted(_SCOPES))


def permission_names() -> tuple[str, ...]:
    """Alias of :func:`known_permissions` for CLI display."""
    return known_permissions()


def is_known(scope: str) -> bool:
    """True only for an exact, concrete catalog entry."""
    return isinstance(scope, str) and scope in _SCOPES


def is_concrete(scope: str) -> bool:
    """True when the scope names one exact capability (no wildcards)."""
    return is_known(scope)


def is_namespace(scope: str) -> bool:
    """True for ``namespace.*`` wildcards (policy rules only)."""
    if not isinstance(scope, str) or not scope.endswith(".*"):
        return False
    return scope[:-2] in NAMESPACES


def is_wildcard(scope: str) -> bool:
    """True for the global ``*`` wildcard (policy rules only)."""
    return scope == "*"


def permission(scope: str) -> Permission:
    """Look up a scope. Raises ``KeyError`` for anything outside the catalog."""
    return _SCOPES[scope]


def permission_scope(namespace: str) -> str:
    """All concrete scopes inside a namespace, sorted."""
    if namespace not in NAMESPACES:
        raise KeyError(namespace)
    return tuple(sorted(n for n in _SCOPES if n.startswith(f"{namespace}.")))


__all__ = [
    "NAMESPACES",
    "is_concrete",
    "is_known",
    "is_namespace",
    "is_wildcard",
    "known_permissions",
    "permission",
    "permission_names",
    "permission_scope",
]
