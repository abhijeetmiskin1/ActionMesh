"""Untrusted-input validation for AMP payloads (stdlib-only).

Everything crossing a trust boundary — LLM output, HTTP bodies, files — is
validated here *before* it becomes protocol data. Validation is fail-closed:
anything unexpected raises :class:`ValidationError` rather than being coerced.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from actionmesh.protocol.models import (
    Action,
    ActionState,
    ActionTarget,
    FailureKind,
    Identity,
    NodeType,
    RiskLevel,
    new_id,
)


class ValidationError(ValueError):
    """Raised for any malformed/untrusted payload. Always fails closed."""

    def __init__(self, reason: str, path: str, value: Any = None) -> None:
        super().__init__(f"invalid actionmesh payload at {path}: {reason}")
        self.reason = reason
        self.path = path
        self.value = value


def _fail(reason: str, path: str, value: Any = None) -> ValidationError:
    return ValidationError(reason, path, value)


def require_type(value: Any, expected: type, path: str) -> None:
    if not isinstance(value, expected):
        raise _fail(f"expected {expected.__name__}, got {type(value).__name__}", path, value)


def _coerce_enum(value: Any, enum_cls: type[Enum], path: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value))
    except ValueError as exc:
        allowed = sorted(m.value for m in enum_cls)  # type: ignore[attr-defined]
        raise _fail(f"must be one of {allowed}, got {value!r}", path, value) from exc


def validate_target(raw: Any, path: str = "target") -> ActionTarget | None:
    if raw is None:
        return None
    if isinstance(raw, ActionTarget):
        return raw
    require_type(raw, dict, path)
    target_type = raw.get("type", "person")
    ident = raw.get("id", "")
    if not isinstance(target_type, str) or not isinstance(ident, str):
        raise _fail("target.type and target.id must be strings", path, raw)
    return ActionTarget(type=target_type, id=ident)


def validate_action(raw: Any, path: str = "action") -> Action:
    """Validate an untrusted action payload into a trusted :class:`Action`."""
    if isinstance(raw, Action):
        raw = raw.model_dump()
    require_type(raw, dict, path)

    action_type = raw.get("type")
    if not isinstance(action_type, str) or not action_type.strip():
        raise _fail("action.type must be a non-empty string", f"{path}.type", action_type)

    params = raw.get("parameters", {})
    if not isinstance(params, dict):
        raise _fail("action.parameters must be an object", f"{path}.parameters", params)

    # A caller may supply an idempotency key so a retry of the *same* logical
    # request is deduplicated. It is preserved here; the executor (C5) is what
    # actually enforces deduplication. ``action_id`` is deliberately NOT taken
    # from untrusted input — identity is assigned by the system.
    idempotency = raw.get("idempotency_key")
    if not isinstance(idempotency, str) or not idempotency.strip():
        idempotency = new_id("idem")

    return Action(
        type=action_type.strip(),
        provider=str(raw.get("provider", "mock")),
        target=validate_target(raw.get("target"), f"{path}.target"),
        parameters=dict(params),
        risk_level=_coerce_enum(raw.get("risk_level", RiskLevel.MEDIUM), RiskLevel, f"{path}.risk_level"),
        requires_confirmation=bool(raw.get("requires_confirmation", False)),
        idempotency_key=idempotency,
        state=ActionState.CREATED,  # untrusted input may never set a lifecycle state
    )


def validate_identity(raw: Any, path: str = "actor") -> Identity | None:
    if raw is None:
        return None
    if isinstance(raw, Identity):
        return raw
    require_type(raw, dict, path)
    ident = raw.get("id", "")
    if not isinstance(ident, str) or not ident.strip():
        raise _fail("actor.id must be a non-empty string", f"{path}.id", ident)
    return Identity(id=ident.strip(), kind=str(raw.get("kind", "user")), display=str(raw.get("display", "")))


def validate_failure_kind(value: Any, path: str = "failure") -> FailureKind:
    return _coerce_enum(value, FailureKind, path)


__all__ = [
    "NodeType",
    "ValidationError",
    "require_type",
    "validate_action",
    "validate_failure_kind",
    "validate_identity",
    "validate_target",
]
