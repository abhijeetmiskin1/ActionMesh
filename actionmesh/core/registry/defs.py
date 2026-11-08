"""Action definition + parameter validation (C2).

The registry *describes* and *resolves* actions. It never executes them and never
makes authorization decisions — it only guarantees that a resolved action
matches a registered, well-formed capability description.
"""

from __future__ import annotations

from typing import Any

from actionmesh.core.permissions.catalog import is_concrete, is_known
from actionmesh.core.registry.errors import DefinitionError
from actionmesh.protocol.models import ActionDefinition, RiskLevel
from actionmesh.protocol.validate import ValidationError, require_type

#: Values feed ``isinstance``, which accepts either a single type or a tuple of
#: them — ``number`` deliberately maps to ``(int, float)`` to accept both.
_TYPE_MAP: dict[str, type[Any] | tuple[type[Any], ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}

_VALID_BACKOFF = ("none", "linear", "exponential")


def version_key(version: str) -> tuple[int, ...]:
    """Numeric sort key so ``1.10`` sorts above ``1.9`` (string sort would not)."""
    parts: list[int] = []
    for chunk in str(version).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def validate_definition(defn: ActionDefinition) -> ActionDefinition:
    """Validate a definition before it may be registered. Fail-closed."""
    require_type(defn, ActionDefinition, "definition")

    if not isinstance(defn.name, str) or not defn.name.strip():
        raise DefinitionError("definition.name must be a non-empty string")
    if not isinstance(defn.version, str) or not defn.version.strip():
        raise DefinitionError(f"definition.version is malformed: {defn.version!r}")
    if not any(c.isdigit() for c in defn.version):
        raise DefinitionError(f"definition.version has no numeric component: {defn.version!r}")
    if not isinstance(defn.risk_level, RiskLevel):
        raise DefinitionError(f"definition.risk_level must be a RiskLevel, got {defn.risk_level!r}")
    if not isinstance(defn.timeout_s, (int, float)) or defn.timeout_s <= 0:
        raise DefinitionError("definition.timeout_s must be > 0")

    permissions = list(defn.permissions or [])
    unknown = [p for p in permissions if not is_known(p)]
    if unknown:
        raise DefinitionError(f"definition declares unknown permission(s): {unknown}")
    wildcards = [p for p in permissions if not is_concrete(p)]
    if wildcards:
        raise DefinitionError(
            f"definitions may not require wildcard permission(s): {wildcards}"
        )

    for label, schema in (
        ("input_schema", defn.input_schema),
        ("output_schema", defn.output_schema),
    ):
        if not isinstance(schema, dict):
            raise DefinitionError(f"definition.{label} must be an object")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise DefinitionError(f"definition.{label}.properties must be an object")
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            raise DefinitionError(f"definition.{label}.required must be a list of strings")
        missing = [r for r in required if r not in properties]
        if missing:
            raise DefinitionError(f"definition.{label}.required names unknown field(s): {missing}")

    policy = defn.retry_policy
    if policy.backoff not in _VALID_BACKOFF:
        raise DefinitionError(f"definition.retry_policy.backoff invalid: {policy.backoff!r}")
    if policy.max_attempts < 1:
        raise DefinitionError("definition.retry_policy.max_attempts must be >= 1")

    return defn


def check_params(
    defn: ActionDefinition, params: dict[str, Any], path: str = "action"
) -> list[str]:
    """Check untrusted parameters against a definition's input schema.

    Returns human-readable warnings. Raises
    :class:`~actionmesh.protocol.validate.ValidationError` when a required
    parameter is missing or a value has the wrong type.

    Fail-closed note: a property specification that is not a well-formed object
    raises rather than being skipped. Silently ignoring an unreadable schema
    would let arbitrary input through.
    """
    if not isinstance(params, dict):
        raise ValidationError("parameters must be an object", path, params)

    schema = defn.input_schema or {}
    properties = schema.get("properties", {}) or {}
    if not isinstance(properties, dict):
        raise DefinitionError(f"{defn.name}: input_schema.properties must be an object")

    warnings: list[str] = []
    for name in schema.get("required", []) or []:
        if name not in params:
            raise ValidationError(
                f"missing required parameter {name!r} for {defn.name}",
                f"{path}.parameters.{name}",
            )

    for name, value in params.items():
        spec = properties.get(name)
        if spec is None:
            # Fail closed. A parameter the action does not declare is not carried
            # through "as opaque data" — it is refused. A warning here is a sentence
            # nobody acts on: the action still dispatches, with the extra value bound
            # into the call. That is the second door for untrusted input, and the one
            # an LLM proposal is most likely to walk through (``{"command": "rm -rf /"}``
            # smuggled beside a legitimate ``{"to", "message"}``).
            raise ValidationError(
                f"unrecognized parameter {name!r} for {defn.name}",
                f"{path}.parameters.{name}",
            )
        if not isinstance(spec, dict):
            raise DefinitionError(
                f"{defn.name}: property spec for {name!r} must be an object, "
                f"got {type(spec).__name__}"
            )
        expected = spec.get("type")
        if expected is None:
            # Untyped property: the shape cannot be verified. Warn loudly but do
            # not fail — the value is still carried through as opaque data.
            warnings.append(
                f"untyped parameter {name!r} for {defn.name}; not type-checked"
            )
            continue
        py_type = _TYPE_MAP.get(str(expected))
        if py_type is None:
            raise DefinitionError(
                f"{defn.name}: unknown parameter type {expected!r} for {name!r}"
            )
        if expected == "number" and isinstance(value, bool):
            warnings.append(
                f"parameter {name!r} for {defn.name} should be a number, got bool"
            )
        elif not isinstance(value, py_type):
            raise ValidationError(
                f"parameter {name!r} for {defn.name} must be {expected}, "
                f"got {type(value).__name__}",
                f"{path}.parameters.{name}",
            )
    return warnings


__all__ = ["check_params", "validate_definition", "version_key"]
