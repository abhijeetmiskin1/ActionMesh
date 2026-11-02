"""The LLM seam: configurable, validated, and honest about what is not wired up.

The product story needs a reasoning layer that can be swapped between providers without
touching ActionMesh. The *security* story needs the same thing from the other side: a
model's output must be treated as untrusted input no matter which vendor produced it.
Both are satisfied here — a provider returns action types and parameters, those are
checked against the registry, and anything else it invented is dropped with a note
rather than executed.

What this build does **not** do is pretend. There is no outbound transport in the
package (the AST guard in ``tests/security/test_dangerous_primitives.py`` forbids network
imports outside a reviewed allowlist, deliberately), so configuring a remote provider
yields no proposal plus a stated reason. That reason reaches the user's screen. A
half-wired integration that *looked* like it worked would be worse than none: the whole
product is built on the difference between what actually happened and what was claimed.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

ENV_PROVIDER = "ACTIONMESH_VOICEOS_LLM_PROVIDER"
ENV_MODEL = "ACTIONMESH_VOICEOS_LLM_MODEL"
ENV_TIMEOUT_S = "ACTIONMESH_VOICEOS_LLM_TIMEOUT_S"

#: Providers that this build can actually reach. Empty is the honest default.
AVAILABLE_PROVIDERS: frozenset[str] = frozenset()


class LlmError(ValueError):
    """A provider answered in a shape that must not be acted on."""


@dataclass(frozen=True)
class LlmConfig:
    provider: str = ""
    model: str = ""
    timeout_s: float = 12.0

    @property
    def requested(self) -> bool:
        return bool(self.provider.strip())


def llm_config_from_env(environ: Mapping[str, str] | None = None) -> LlmConfig:
    env = os.environ if environ is None else environ
    raw_timeout = env.get(ENV_TIMEOUT_S, "")
    try:
        timeout_s = float(raw_timeout) if raw_timeout else 12.0
    except ValueError:
        # A garbage timeout is a configuration mistake, not a licence to wait forever.
        timeout_s = 12.0
    return LlmConfig(
        provider=(env.get(ENV_PROVIDER) or "").strip(),
        model=(env.get(ENV_MODEL) or "").strip(),
        timeout_s=max(0.5, min(timeout_s, 120.0)),
    )


@dataclass(frozen=True)
class LlmProposal:
    """Validated proposals: action types the registry knows, and their parameters."""

    actions: tuple[tuple[str, dict[str, Any]], ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


class LlmProvider(Protocol):
    """Structural interface: a request in, an untrusted proposal out, or nothing."""

    name: str

    def propose(self, request: str) -> Mapping[str, Any] | None:  # pragma: no cover - protocol
        ...


def validate_proposal(
    raw: Any, known_action: Callable[[str], bool]
) -> LlmProposal:
    """Strip a model answer down to what a proposal is allowed to be.

    Accepted shape: ``{"actions": [{"type": str, "parameters": {}}]}``. Anything else —
    a risk level, a permission, an approval decision, a policy rule — is dropped with a
    note, because the moment one of those fields is *honoured* the model has authorised
    itself.
    """
    if raw is None:
        return LlmProposal(notes=("provider returned nothing",))
    if not isinstance(raw, Mapping):
        raise LlmError("a proposal must be an object")
    actions = raw.get("actions")
    if not isinstance(actions, list):
        raise LlmError("proposal has no actions list")
    kept: list[tuple[str, dict[str, Any]]] = []
    notes: list[str] = []
    for entry in actions:
        if not isinstance(entry, Mapping):
            notes.append("dropped a malformed action entry")
            continue
        extra = set(entry) - {"type", "parameters"}
        if extra:
            # Refused whole. An entry that thinks it may state its own risk or grant is
            # exactly the shape a prompt-injected answer arrives in, and keeping the
            # "harmless" part of it would teach the next caller that those fields are
            # merely decorative.
            notes.append(
                f"dropped a proposal for {entry.get('type')!r} that asserted fields it does "
                f"not own: {sorted(extra)}"
            )
            continue
        action_type = entry.get("type")
        if not isinstance(action_type, str) or not action_type:
            notes.append("dropped an action with no type")
            continue
        if not known_action(action_type):
            notes.append(f"dropped unregistered action type {action_type!r}")
            continue
        parameters = entry.get("parameters") or {}
        if not isinstance(parameters, Mapping):
            notes.append(f"dropped non-object parameters for {action_type!r}")
            continue
        kept.append((action_type, dict(parameters)))
    return LlmProposal(actions=tuple(kept), notes=tuple(notes))


@dataclass
class UnwiredProvider:
    """A configured provider with no transport in this build. Says so, proposes nothing."""

    name: str
    model: str = ""

    @property
    def reason(self) -> str:
        return (
            f"provider {self.name!r} is configured, but this build has no approved "
            "outbound transport for it, so the deterministic planner answered this "
            "request instead"
        )

    def propose(self, request: str) -> Mapping[str, Any] | None:
        return None


def build_provider(
    config: LlmConfig,
) -> tuple[LlmProvider | None, str]:
    """Resolve the configured provider, or explain why nothing was resolved.

    Adding a real one is a deliberate act: an approved endpoint, credentials from the
    environment, and an entry in ``AVAILABLE_PROVIDERS`` — which is also the moment the
    outbound-network guard has to be widened on purpose, in review, rather than by
    whoever wrote the adapter.
    """
    if not config.requested:
        return None, ""
    if config.provider in AVAILABLE_PROVIDERS:  # pragma: no cover - none are available
        raise LlmError(f"provider {config.provider!r} is listed but not implemented")
    unwired = UnwiredProvider(name=config.provider, model=config.model)
    return None, unwired.reason


__all__ = [
    "AVAILABLE_PROVIDERS",
    "ENV_MODEL",
    "ENV_PROVIDER",
    "ENV_TIMEOUT_S",
    "LlmConfig",
    "LlmError",
    "LlmProposal",
    "LlmProvider",
    "UnwiredProvider",
    "build_provider",
    "llm_config_from_env",
    "validate_proposal",
]
