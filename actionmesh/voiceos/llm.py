"""The LLM seam: configurable, validated, and honest about what is wired up.

The product story needs a reasoning layer that can be swapped between providers without
touching ActionMesh. The *security* story needs the same thing from the other side: a
model's output must be treated as untrusted input no matter which vendor produced it.
Both are satisfied here — a provider returns action types and parameters, those are
checked against the registry, and anything else it invented is dropped with a note
rather than executed.

What this build actually wires
------------------------------

Two providers, both real:

* **Anthropic**, through its official Messages endpoint, via the single reviewed
  transport in :mod:`actionmesh.voiceos.transport`. The credential is read from the
  environment (``ACTIONMESH_VOICEOS_LLM_API_KEY``, falling back to the conventional
  ``ANTHROPIC_API_KEY``); it is never written to a config file, never logged, and never
  returned to a client.
* **desktop** (C41), through :mod:`actionmesh.voiceos.model_bridge`. This process holds
  no credential at all: the Electron main process keeps the key in the OS keyring and
  makes the call, and VoiceOS asks it over a private Unix socket authorised by a
  per-launch token. What comes back is the model's answer and nothing else — the token
  authorises "talk to this socket", not "use this key". That bridge is the only route
  to the desktop app's ``openai``, ``gemini`` and ``openai_compatible`` providers;
  they have no direct Python transport, and :data:`AVAILABLE_PROVIDERS` says so.

What crosses the wire is deliberately small and stated to the user:

* the user's own request text, which the model cannot answer without;
* the **names, one-line descriptions, and static argument names/types** of the actions
  this deployment registers. No argument values/defaults are included.

It does not receive logs, file contents, process output, device inventory or any other
observation. Those are collected by ActionMesh *after* a plan exists, through authorized
capabilities, and an observation is only ever sent to a model by an explicit analysis
step the user can see — never as a side effect of planning.

What the model cannot do, regardless of what it says
----------------------------------------------------

:func:`validate_proposal` is the only door its answer comes through. It accepts action
types and parameters, drops any entry that asserts a risk level, permission, approval or
policy decision, and drops types the registry does not have. The plan that results is a
*proposal*; the executor re-resolves it and the policy engine decides. A model that has
been prompt-injected into "approving" its own action produces text that this module
throws away.

When something is not wired, it says so. A configured provider this build cannot reach
yields no proposal plus a stated reason, and that reason reaches the user's screen. A
half-wired integration that *looked* like it worked would be worse than none: the whole
product is built on the difference between what actually happened and what was claimed.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from actionmesh.core.planner import Plan, Planner, ProposedAction
from actionmesh.core.registry import ActionRegistry, check_params
from actionmesh.protocol.validate import ValidationError
from actionmesh.voiceos import model_bridge
from actionmesh.voiceos.model_bridge import BridgeConfig, BridgeError, BridgeUnavailable
from actionmesh.voiceos.tasks import MAX_STEPS
from actionmesh.voiceos.transport import TransportError, post_json

ENV_PROVIDER = "ACTIONMESH_VOICEOS_LLM_PROVIDER"
ENV_MODEL = "ACTIONMESH_VOICEOS_LLM_MODEL"
ENV_TIMEOUT_S = "ACTIONMESH_VOICEOS_LLM_TIMEOUT_S"
ENV_MAX_TOKENS = "ACTIONMESH_VOICEOS_LLM_MAX_TOKENS"

#: The credential. An environment variable, never a file this module writes, never a
#: value this module echoes.
ENV_API_KEY = "ACTIONMESH_VOICEOS_LLM_API_KEY"

#: The conventional variable the official SDK documents, accepted as a fallback so an
#: operator who already exports it does not have to duplicate a secret.
ENV_API_KEY_FALLBACK = "ANTHROPIC_API_KEY"

#: The C41 desktop bridge. These name a *socket* and the per-launch capability that
#: authorises one conversation with it — never a model credential. The Electron main
#: process holds the key; the Python process only ever learns where to ask.
ENV_BRIDGE_SOCKET = "ACTIONMESH_PROVIDER_BRIDGE_SOCKET"
ENV_BRIDGE_TOKEN = "ACTIONMESH_PROVIDER_BRIDGE_TOKEN"

#: The one official endpoint this build calls. It also has to appear in
#: ``transport.OFFICIAL_ENDPOINTS``, which is the closed list the transport dials.
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

#: Used when the operator names a provider but not a model. Overridable with
#: ``ENV_MODEL``; there is no hidden fallback chain of deprecated model names.
DEFAULT_MODEL = "claude-sonnet-5-5"

#: How many registered actions the catalogue description may carry. A deployment with
#: hundreds of actions should summarise them rather than mail the whole registry.
MAX_CATALOG_ACTIONS = 128

#: Provider names that :func:`build_provider` accepts.
#:
#: ``"anthropic"`` reaches the official Messages endpoint from this process,
#: with a key from the environment. ``"openai"``, ``"gemini"`` and
#: ``"openai_compatible"`` have **no direct Python transport**: their calls,
#: when they exist, are made by the Electron main process in
#: ``desktop/app/providers.js`` (where all three are ``implemented: true``),
#: and this process reaches them only over the C41 desktop bridge. Naming
#: them here would claim a direct API that the closed transport
#: (``OFFICIAL_ENDPOINTS``) does not carry, so they are deliberately **not**
#: added until an endpoint review approves them. ``"desktop"`` is the one
#: bridge-routed name: the provider the bridge picks comes from the desktop
#: app's own settings and may be OpenAI, Gemini or an OpenAI-compatible
#: endpoint, but this process never sees the key or the provider name.
#: The names below are therefore the Python facts; the desktop capability
#: table lives in ``providers.js``.
AVAILABLE_PROVIDERS: frozenset[str] = frozenset({"anthropic", "desktop"})


class LlmError(ValueError):
    """A provider answered in a shape that must not be acted on."""


@dataclass(frozen=True)
class LlmConfig:
    provider: str = ""
    model: str = ""
    timeout_s: float = 12.0
    max_tokens: int = 1024

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
    raw_tokens = env.get(ENV_MAX_TOKENS, "")
    try:
        max_tokens = int(raw_tokens) if raw_tokens else 1024
    except ValueError:
        max_tokens = 1024
    return LlmConfig(
        provider=(env.get(ENV_PROVIDER) or "").strip(),
        model=(env.get(ENV_MODEL) or "").strip(),
        timeout_s=max(0.5, min(timeout_s, 120.0)),
        max_tokens=max(64, min(max_tokens, 8192)),
    )


def api_key_from_env(environ: Mapping[str, str] | None = None) -> str:
    """The credential, or ``""``. Never raises, never logs, never returns a prefix."""
    env = os.environ if environ is None else environ
    for name in (ENV_API_KEY, ENV_API_KEY_FALLBACK):
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True)
class LlmProposal:
    """Validated proposals: action types the registry knows, and their parameters."""

    actions: tuple[tuple[str, dict[str, Any]], ...] = ()
    notes: tuple[str, ...] = ()
    reply: str = ""


class LlmProvider(Protocol):
    """Structural interface: a request in, an untrusted proposal out, or nothing.

    ``name`` is declared as a read-only property on purpose. Both implementations are
    frozen dataclasses, so ``name`` cannot be assigned; declaring it settable here made
    neither of them satisfy the protocol, which mypy reported as an error at the one
    place that returns a provider.
    """

    @property
    def name(self) -> str: ...  # pragma: no cover - protocol

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
    reply = raw.get("reply", "")
    if not isinstance(reply, str) or len(reply) > 4000:
        raise LlmError("the conversational reply must be text of at most 4000 characters")
    # A discarded action must never be disguised as a successful conversational turn.
    # Text is used only when the model explicitly proposed no actions at all.
    if actions or set(raw) - {"actions", "notes", "reply"}:
        reply = ""
    return LlmProposal(actions=tuple(kept), notes=tuple(notes), reply=reply.strip())


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


# --------------------------------------------------------------------------------------
# Anthropic Messages API adapter
# --------------------------------------------------------------------------------------

#: name -> argument description for the one tool this adapter exposes. The model can
#: only answer by filling this in, which turns "please reply with JSON" (a request a
#: model may ignore) into a shape the API itself enforces.
PLAN_TOOL_NAME = "propose_plan"

_PLAN_TOOL: dict[str, Any] = {
    "name": PLAN_TOOL_NAME,
    "description": (
        "Propose the ActionMesh actions that carry out the request. Use only action "
        "types listed in the catalogue. You may not state a risk level, a permission, "
        "an approval or a policy decision; those belong to the system that receives "
        "this proposal. If no listed action fits, propose an empty list."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "description": "Ordered steps. Only registered action types.",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "parameters": {
                            "type": "object",
                            "description": "Arguments for the action, from the request.",
                        },
                    },
                    "required": ["type", "parameters"],
                    "additionalProperties": False,
                },
            },
            "reply": {
                "type": "string",
                "description": "Answer or clarification when actions is empty; at most 4000 characters.",
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Anything you could not map to a listed action.",
            },
        },
        "required": ["actions"],
        "additionalProperties": False,
    },
}

_SYSTEM_PROMPT = (
    "You are VoiceOS, a conversational assistant and careful action planner. "
    "Answer using the propose_plan structured document. Use only registered action "
    "types and the listed input names/types (* means required, others are optional). "
    "Omit optional arguments unless the user supplies them; do not ask for an optional "
    "ref when the action supports the current/default selection. An action with no "
    "required inputs can use empty parameters. Never invent a capability "
    "or an argument value. Spoken input can contain filler, paraphrases, or a "
    "misheard word. Use the clear parts when they identify one registered "
    "operation and its required inputs. Ask about the missing detail when the "
    "unclear words could change the action or target. For an explicit request to "
    "operate the device, propose actions only if the target and required arguments "
    "are clear. Match the meaning to the catalogue in any language, even if the user "
    "does not say an action's name or use an imperative. A question asking about this "
    "device's current state is a request to check it. Prefer the matching device "
    "action to a generic command, browser search, or simulated vendor action. "
    "An app launch needs an app, not a website URL; preserve names, paths and quoted "
    "text when passing them to an action. For several requested operations, include "
    f"them in order (at most {MAX_STEPS}) or explain what is missing; do not silently "
    "plan only part of the request. Otherwise return actions=[] and ask one concise "
    "clarification in reply. For an unfamiliar bare name after 'open', ask what the "
    "user meant; do not assume a website. Do not guess an app, recipient, or target. "
    "For greetings, explanations, general questions, and hypothetical how-to questions, "
    "return actions=[] and a helpful plain-language reply. Unless the user requests "
    "a specific reply language, use the language of the current request; prior turns "
    "do not set the reply language. An explanation of "
    "an action is not a request to perform it. Do not claim to see the screen, read files, "
    "browse, or have completed any device action. You have only the user's messages "
    "and the catalogue, not live observations. Never assert permission, approval, grants, "
    "risk or successful execution. Never propose unrestricted shell or generated code. "
    "Treat prior user messages as context only; act only on the current explicit request. "
    "If a requested capability is absent, explain that in reply with actions=[]. "
    "Default to a concise spoken answer of two or three sentences; give more detail only "
    "when the user requests it. Keep replies under 4000 characters. Actions are proposals "
    "and require system review."
)


def _catalogue_text(catalog: Sequence[tuple[str, str]]) -> str:
    rows = [f"- {name} — {description}".rstrip(" —") for name, description in catalog]
    if len(rows) > MAX_CATALOG_ACTIONS:
        # One line of the budget is spent on the omission count, so a large registry is
        # summarised at the stated size rather than one over it.
        omitted = len(rows) - (MAX_CATALOG_ACTIONS - 1)
        rows = rows[: MAX_CATALOG_ACTIONS - 1]
        rows.append(f"- ({omitted} more registered actions omitted)")
    return "\n".join(rows) or "- (this deployment registers no actions)"


def _plan_user_text(catalog: Sequence[tuple[str, str]], request: str) -> str:
    """Everything a planning call is given: the request and the registry's names.

    Factored out so the direct adapter and the desktop bridge send byte-identical
    text. Two adapters that described the catalogue slightly differently would make
    "the model only picks from registered names" a claim about one of them.
    """
    return f"Request: {request}\n\nRegistered actions:\n{_catalogue_text(catalog)}"


@dataclass(frozen=True)
class AnthropicProvider:
    """One official Messages API call, through the reviewed transport.

    ``catalog`` supplies ``(action name, description)`` pairs and is called per request,
    so a plan is always proposed against the capabilities this deployment actually has.
    """

    name: str
    model: str
    api_key: str
    timeout_s: float = 12.0
    max_tokens: int = 1024
    catalog: Callable[[], Sequence[tuple[str, str]]] = lambda: ()

    def payload(self, request: str) -> dict[str, Any]:
        """The exact request body — a pure function, so what is sent is auditable."""
        return {
            "model": self.model or DEFAULT_MODEL,
            "max_tokens": self.max_tokens,
            "system": _SYSTEM_PROMPT,
            "tools": [_PLAN_TOOL],
            "tool_choice": {"type": "tool", "name": PLAN_TOOL_NAME},
            "messages": [
                {
                    "role": "user",
                    "content": _plan_user_text(self.catalog(), request),
                }
            ],
        }

    def propose(self, request: str) -> Mapping[str, Any] | None:
        """One blocking call. Refuses rather than answering on an unknown footing."""
        if not self.api_key:
            raise LlmError("no credential is configured for the model provider")
        try:
            answer = asyncio.run(
                post_json(
                    endpoint=ANTHROPIC_ENDPOINT,
                    headers={
                        "x-api-key": self.api_key,
                        # Pinned, not "latest": an interface that changes underneath a
                        # deployment is one whose validation story is not reproducible.
                        "anthropic-version": ANTHROPIC_VERSION,
                    },
                    payload=self.payload(request),
                    timeout_s=self.timeout_s,
                )
            )
        except TransportError as exc:
            # The transport's messages are already scrubbed of request contents.
            raise LlmError(str(exc)) from exc
        return _tool_input(answer)


def _tool_input(answer: Mapping[str, Any], *, tool_name: str = PLAN_TOOL_NAME) -> Mapping[str, Any]:
    """Pull the forced tool call's input out of a Messages API response.

    Only a ``tool_use`` block is accepted. A model that answered in prose despite being
    told otherwise is a malformed answer, not a proposal — and guessing at its JSON
    would be exactly the "looks like it worked" shorthand this module exists to avoid.
    """
    content = answer.get("content")
    if not isinstance(content, list):
        raise LlmError("the model response had no content")
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "tool_use" and block.get("name") == tool_name:
            data = block.get("input")
            if isinstance(data, Mapping):
                return data
            raise LlmError(f"the model called the {tool_name} tool without an object")
    raise LlmError(f"the model answered without calling the {tool_name} tool")


# --------------------------------------------------------------------------------------
# Diagnosis adapter — the explicit analysis step
# --------------------------------------------------------------------------------------

#: The second tool, and the reason it is a *separate* tool rather than a mode of the
#: first: a plan is a proposal to act and a diagnosis is prose about something already
#: observed. Sharing one tool would let an explanation arrive carrying action steps,
#: which is the "the model explained it, so let us run it" shortcut this whole product
#: is built to refuse.
DIAGNOSE_TOOL_NAME = "diagnose"

_DIAGNOSE_TOOL: dict[str, Any] = {
    "name": DIAGNOSE_TOOL_NAME,
    "description": (
        "Explain what the supplied log excerpt shows. You are given only the excerpt "
        "and facts measured from it by another program. Do not claim to have inspected "
        "anything else. You may not propose, approve or authorise actions, and you may "
        "not state a risk level, permission, grant or policy decision; those belong to "
        "the system that will act on your explanation after a human approves. If the "
        "excerpt is insufficient, say what is missing in 'unanswered' rather than guessing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "finding": {
                "type": "string",
                "description": "What the excerpt shows. Quote the decisive line.",
            },
            "likely_cause": {
                "type": "string",
                "description": "The most likely cause, or empty if the excerpt cannot support one.",
            },
            "fixes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Concrete next steps a person could take, in order. Describe them; "
                    "do not assume any of them has been done."
                ),
            },
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "unanswered": {
                "type": "string",
                "description": "What you could not determine from this excerpt.",
            },
        },
        "required": ["finding"],
        "additionalProperties": False,
    },
}

_DIAGNOSIS_SYSTEM_PROMPT = (
    "You explain log excerpts on a person's own computer. Answer only by calling the "
    "diagnose tool. The text you are given has already been redacted and is a bounded "
    "tail of one source; it may start midway through a line and is not the whole log. "
    "Do not invent anything you were not shown: no file contents, no configuration, no "
    "other process, no version. If the excerpt does not support a cause, say so and use "
    "'low' confidence. Describe fixes as instructions to a person, never as steps you "
    "have taken or will take."
)


@dataclass(frozen=True)
class AnalysisRequest:
    """Everything that may cross the wire for one diagnosis, assembled by the caller.

    The caller — not the model and not this module — decides what ``excerpt`` is; it is
    already redacted, already bounded, and is the *only* observation in the payload.
    Making this a value object means the exact bytes are inspectable in a test.
    """

    question: str
    excerpt: str
    facts: str = ""
    computer: str = ""
    source: str = ""
    truncated: bool = False


def _diagnosis_user_text(request: AnalysisRequest) -> str:
    """Everything a diagnosis call is given — pure, so both adapters send one text.

    Only the caller-assembled :class:`AnalysisRequest` appears: an already-redacted,
    already-bounded excerpt, the facts another program measured from it, and where it
    came from. Factored out of :class:`AnthropicAnalyst` so the desktop bridge sends
    byte-identical content rather than a near-copy that drifted.
    """
    where = request.computer or "an unnamed computer"
    source = request.source or "a log the agent read"
    lines = [
        f"Request from the person: {request.question}",
        f"Computer: {where}",
        f"Source: {source}",
    ]
    if request.truncated:
        lines.append(
            "Note: this is the tail of a larger log; earlier lines were not read."
        )
    if request.facts:
        lines.append(f"Facts measured from the excerpt:\n{request.facts}")
    lines.append(f"Redacted excerpt:\n```\n{request.excerpt}\n```")
    return "\n\n".join(lines)


class AnalysisProvider(Protocol):
    """Structural interface: an observation in, an untrusted explanation out."""

    @property
    def name(self) -> str: ...  # pragma: no cover - protocol

    def explain(self, request: AnalysisRequest) -> Mapping[str, Any] | None:  # pragma: no cover
        ...


@dataclass(frozen=True)
class AnthropicAnalyst:
    """One official Messages API call that can only return an explanation."""

    name: str
    model: str
    api_key: str
    timeout_s: float = 20.0
    max_tokens: int = 1024

    def payload(self, request: AnalysisRequest) -> dict[str, Any]:
        """The exact request body — pure, so what is sent is auditable in a test."""
        return {
            "model": self.model or DEFAULT_MODEL,
            "max_tokens": self.max_tokens,
            "system": _DIAGNOSIS_SYSTEM_PROMPT,
            "tools": [_DIAGNOSE_TOOL],
            "tool_choice": {"type": "tool", "name": DIAGNOSE_TOOL_NAME},
            "messages": [{"role": "user", "content": _diagnosis_user_text(request)}],
        }

    def explain(self, request: AnalysisRequest) -> Mapping[str, Any] | None:
        """One blocking call. Refuses rather than answering on an unknown footing."""
        if not self.api_key:
            raise LlmError("no credential is configured for the model provider")
        try:
            answer = asyncio.run(
                post_json(
                    endpoint=ANTHROPIC_ENDPOINT,
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": ANTHROPIC_VERSION,
                    },
                    payload=self.payload(request),
                    timeout_s=self.timeout_s,
                )
            )
        except TransportError as exc:
            raise LlmError(str(exc)) from exc
        return _tool_input(answer, tool_name=DIAGNOSE_TOOL_NAME)


# --------------------------------------------------------------------------------------
# C41 — the desktop bridge adapters
# --------------------------------------------------------------------------------------
#
# The third and fourth adapters, and the two that never see a credential. The Electron
# main process holds the key, makes the HTTPS call itself, and answers over a Unix
# socket; these classes only describe the request and hand back what came back.
#
# The important property is what they *do not* do. `propose` returns the bridge's
# content mapping untouched — no filtering, no defaulting, no pre-validation. Every
# other adapter in this file is trusted to be a faithful reporter of the model's
# answer rather than a helpful editor of it, and this one has to be the same or
# `validate_proposal` would be policing different bytes depending on the provider.


def bridge_config_from_env(environ: Mapping[str, str] | None = None) -> BridgeConfig:
    """The bridge address, from the child's environment, or an unconfigured value.

    Both names are written by Electron main when it spawns the backend. A missing pair
    yields an empty :class:`BridgeConfig`, whose ``configured`` is ``False`` — that is
    how the builders tell "no bridge" from "a bridge that will fail".
    """
    env = os.environ if environ is None else environ
    return BridgeConfig(
        socket_path=(env.get(ENV_BRIDGE_SOCKET) or "").strip(),
        token=(env.get(ENV_BRIDGE_TOKEN) or "").strip(),
    )


def _bridge_failure(exc: BridgeError | BridgeUnavailable) -> LlmError:
    """Translate a bridge failure into the seam's own error, keeping the state text.

    The message is what arrives in a planner's fallback note, so it has to say enough
    for a person to act on — "missing_key" rather than "something went wrong" — while
    carrying nothing the bridge did not already make public. Neither exception's text
    contains the token; see :mod:`actionmesh.voiceos.model_bridge`.
    """
    if isinstance(exc, BridgeError):
        return LlmError(f"the desktop bridge reported {exc.state}: {exc.detail}".strip())
    return LlmError(str(exc))


@dataclass(frozen=True)
class DesktopProvider:
    """Planning through the desktop bridge: the key stays in Electron main.

    ``catalog`` is called per request, like :class:`AnthropicProvider`'s, so a proposal
    is always made against the capabilities this deployment currently has.
    """

    name: str
    bridge: BridgeConfig
    model: str = ""
    catalog: Callable[[], Sequence[tuple[str, str]]] = lambda: ()

    def propose(self, request: str) -> Mapping[str, Any] | None:
        """One blocking socket call. Returns the bridge's content **verbatim**.

        No shape checking happens here on purpose: the one validator downstream is
        :func:`validate_proposal`, and it can only be tested against the untrusted
        shape if every adapter passes that shape through unchanged.
        """
        try:
            return model_bridge.generate(
                self.bridge,
                purpose="plan",
                system=_SYSTEM_PROMPT,
                user=_plan_user_text(self.catalog(), request),
            )
        except (BridgeError, BridgeUnavailable) as exc:
            raise _bridge_failure(exc) from exc


@dataclass(frozen=True)
class DesktopAnalyst:
    """Diagnosis through the desktop bridge — the same payload shape, same socket."""

    name: str
    bridge: BridgeConfig
    model: str = ""

    def explain(self, request: AnalysisRequest) -> Mapping[str, Any] | None:
        """One blocking socket call. Returns the bridge's content verbatim."""
        try:
            return model_bridge.generate(
                self.bridge,
                purpose="diagnose",
                system=_DIAGNOSIS_SYSTEM_PROMPT,
                user=_diagnosis_user_text(request),
            )
        except (BridgeError, BridgeUnavailable) as exc:
            raise _bridge_failure(exc) from exc


# --------------------------------------------------------------------------------------
# Planner adapter
# --------------------------------------------------------------------------------------


@dataclass
class LlmPlanner:
    """A :class:`~actionmesh.core.planner.Planner` backed by a model, with a fallback.

    The fallback is not decoration. When the model cannot be reached, or answers with
    nothing this deployment registers, the deterministic planner still answers — and
    the returned plan's notes say which one did. A user is never told a model planned
    something it did not.
    """

    provider: LlmProvider
    fallback: Planner
    known_action: Callable[[str], bool]
    registry: ActionRegistry | None = None

    def plan(self, intent: str) -> Plan:
        return self._plan(intent, intent)

    def plan_with_context(self, intent: str, previous_requests: Sequence[str]) -> Plan:
        # Only prior user utterances, never observations, tool results or diagnostics.
        history = [text[:1000] for text in previous_requests[-3:] if isinstance(text, str)]
        request = (
            "Prior user messages (context only, do not carry out these old requests): "
            + json.dumps(history, ensure_ascii=False)
            + "\nCurrent user request: " + intent
        ) if history else intent
        return self._plan(intent, request)

    def _plan(self, intent: str, provider_request: str) -> Plan:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # Called on the server's event loop. A blocking HTTPS round trip here would
            # stall every other request in the process, so the honest answer is the
            # fallback plus a note, not a silent stall.
            return self._fallback(
                intent, "the model call must run off the event loop; this request was "
                "planned deterministically"
            )
        try:
            raw = self.provider.propose(provider_request)
        except LlmError as exc:
            return self._fallback(
                intent, f"the model did not answer this request ({exc})"
            )
        try:
            proposal = validate_proposal(raw, self.known_action)
        except LlmError as exc:
            return self._fallback(intent, f"the model's answer was discarded ({exc})")
        if not proposal.actions:
            if proposal.reply:
                return Plan(
                    intent=intent, reply=proposal.reply,
                    notes=(f"answered by {getattr(self.provider, 'name', 'the model')}",),
                )
            detail = "; ".join(proposal.notes) or "it proposed no action this build registers"
            # The proposal's own notes are carried as *separate* notes rather than
            # folded into the parenthetical. Downstream code recognises a dropped
            # capability by the note's opening words, and squashing them into one
            # sentence would turn "you asked for X, which is not offered here" into
            # "I did not understand you" — a different problem, and the wrong one.
            return self._fallback(
                intent,
                f"the model proposed nothing usable ({detail})",
                proposal.notes,
            )
        if self.registry is not None:
            if proposal.notes or len(proposal.actions) > MAX_STEPS:
                return Plan(
                    intent=intent,
                    reply="I couldn't build a complete plan for that request. "
                          "Try asking for one operation at a time.",
                )
            for name, parameters in proposal.actions:
                try:
                    check_params(self.registry.get(name), parameters)
                except ValidationError as exc:
                    return Plan(
                        intent=intent,
                        reply=f"I couldn't prepare the action: {exc.reason}. "
                              "Please clarify the request.",
                    )
        return Plan(
            intent=intent,
            actions=tuple(
                ProposedAction(type=action_type, parameters=dict(parameters))
                for action_type, parameters in proposal.actions
            ),
            notes=(
                f"planned by {getattr(self.provider, 'name', 'the model')}",
                *proposal.notes,
            ),
        )

    async def plan_async(self, intent: str) -> Plan:
        """Off-loop entry for an ``async`` route. Never blocks the server."""
        return await asyncio.to_thread(self.plan, intent)

    def _fallback(self, intent: str, reason: str, extra: tuple[str, ...] = ()) -> Plan:
        base = self.fallback.plan(intent)
        return Plan(
            intent=base.intent,
            actions=base.actions,
            notes=(reason, "the deterministic planner answered instead", *extra, *base.notes),
        )


# --------------------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------------------


def build_provider(
    config: LlmConfig,
    *,
    environ: Mapping[str, str] | None = None,
    catalog: Callable[[], Sequence[tuple[str, str]]] | None = None,
    bridge: BridgeConfig | None = None,
) -> tuple[LlmProvider | None, str]:
    """Resolve the configured provider, or explain why nothing was resolved.

    The second element is always user-visible text. It is empty only when no provider
    was requested; it names what is configured and why it cannot be used otherwise.

    ``bridge`` is the C41 desktop socket (:class:`~actionmesh.voiceos.model_bridge.
    BridgeConfig`). It only matters for ``provider == "desktop"``; passing it when
    another provider is configured is harmless and ignored, so a caller can construct
    it once at startup without branching.
    """
    if not config.requested:
        return None, ""
    provider = config.provider.strip().lower()
    if provider not in AVAILABLE_PROVIDERS:
        unwired = UnwiredProvider(name=config.provider, model=config.model)
        return None, unwired.reason
    if provider == "desktop":
        # The bridge is the credential's stand-in: no socket/token pair means there is
        # nothing to ask, and the reason has to name both variables so an operator can
        # fix it without reading this file.
        if bridge is None or not bridge.configured:
            return None, (
                "provider 'desktop' is configured but no model bridge is available "
                f"(set {ENV_BRIDGE_SOCKET} and {ENV_BRIDGE_TOKEN} in this process's "
                "environment; the desktop app supplies both at launch), so the "
                "deterministic planner answered this request instead"
            )
        return (
            DesktopProvider(
                name="desktop",
                bridge=bridge,
                model=config.model or DEFAULT_MODEL,
                catalog=catalog or (lambda: ()),
            ),
            "",
        )
    if provider == "anthropic":
        key = api_key_from_env(environ)
        if not key:
            return None, (
                "provider 'anthropic' is configured but no credential is present "
                f"(set {ENV_API_KEY}, or the conventional {ENV_API_KEY_FALLBACK}), so the "
                "deterministic planner answered this request instead"
            )
        return (
            AnthropicProvider(
                name="anthropic",
                model=config.model or DEFAULT_MODEL,
                api_key=key,
                timeout_s=config.timeout_s,
                max_tokens=config.max_tokens,
                catalog=catalog or (lambda: ()),
            ),
            "",
        )
    # Unreachable while AVAILABLE_PROVIDERS and this branch agree; a mismatch means a
    # provider was added to the set without an adapter, which must fail loudly.
    raise LlmError(f"provider {config.provider!r} is listed but not implemented")


def build_planner(
    provider: LlmProvider | None,
    fallback: Planner,
    known_action: Callable[[str], bool],
    *,
    registry: ActionRegistry | None = None,
) -> Planner:
    """Wrap a live provider in a planner, or hand back the deterministic one."""
    if provider is None:
        return fallback
    return LlmPlanner(
        provider=provider, fallback=fallback, known_action=known_action, registry=registry
    )


def build_analyst(
    config: LlmConfig,
    *,
    environ: Mapping[str, str] | None = None,
    bridge: BridgeConfig | None = None,
) -> tuple[AnalysisProvider | None, str]:
    """Resolve the provider for the explicit analysis step, or explain why not.

    Kept separate from :func:`build_provider` because the two answer different
    questions. Planning asks "what could carry out this request?" and needs the action
    catalogue. Diagnosis asks "what does this excerpt show?" and needs nothing but the
    excerpt — so the analyst is built even in a deployment that has stopped planning
    with a model, and its absence never changes what a plan is allowed to be.

    The second element is user-visible text, empty when a provider was resolved.
    """
    if not config.requested:
        return None, "no model provider is configured for analysis"
    provider = config.provider.strip().lower()
    if provider not in AVAILABLE_PROVIDERS:
        return None, (
            f"provider {config.provider!r} is configured, but this build has no approved "
            "outbound transport for it, so nothing was sent"
        )
    if provider == "desktop":
        if bridge is None or not bridge.configured:
            return None, (
                "provider 'desktop' is configured but no model bridge is available "
                f"(set {ENV_BRIDGE_SOCKET} and {ENV_BRIDGE_TOKEN} in this process's "
                "environment; the desktop app supplies both at launch), so the excerpt "
                "was not sent anywhere"
            )
        return (
            DesktopAnalyst(
                name="desktop",
                bridge=bridge,
                model=config.model or DEFAULT_MODEL,
            ),
            "",
        )
    key = api_key_from_env(environ)
    if not key:
        return None, (
            f"provider {config.provider!r} is configured but no credential is present "
            f"(set {ENV_API_KEY}, or the conventional {ENV_API_KEY_FALLBACK}), so the "
            "excerpt was not sent anywhere"
        )
    return (
        AnthropicAnalyst(
            name="anthropic",
            model=config.model or DEFAULT_MODEL,
            api_key=key,
            timeout_s=max(config.timeout_s, 20.0),
            max_tokens=max(config.max_tokens, 1024),
        ),
        "",
    )


def build_catalogue(
    actions: Sequence[Any],
) -> Callable[[], Sequence[tuple[str, str]]]:
    """Cache static descriptions plus argument names/types, never argument values.

    Required names are marked with '*'. Defaults, examples, enums and observations
    are excluded: they can contain data, and the planner must not invent values.
    """
    rows: list[tuple[str, str]] = []
    for action in actions:
        name = str(getattr(action, "name", "") or "").strip()
        if not name:
            continue
        description = " ".join(str(getattr(action, "description", "") or "").split())[:160]
        schema = getattr(action, "input_schema", {}) or {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = schema.get("required", ()) if isinstance(schema, dict) else ()
        arguments: list[str] = []
        if isinstance(properties, dict):
            for key, spec in list(properties.items())[:16]:
                if not isinstance(key, str) or not isinstance(spec, dict):
                    continue
                kind = spec.get("type")
                if kind not in ("string", "number", "integer", "boolean", "object", "array"):
                    continue
                arguments.append(f"{key[:64]}:{kind}{'*' if key in required else ' (optional)'}")
        if arguments:
            description += " | Inputs (* required): " + ", ".join(arguments)
            if not required:
                description += "; no required inputs"
        rows.append((name, description))
    # Registry metadata is static after API startup. Copy only the small list,
    # rather than normalizing and re-describing every definition on each turn.
    return lambda: list(rows)


__all__ = [
    "ANTHROPIC_ENDPOINT",
    "ANTHROPIC_VERSION",
    "AVAILABLE_PROVIDERS",
    "DEFAULT_MODEL",
    "DIAGNOSE_TOOL_NAME",
    "ENV_API_KEY",
    "ENV_API_KEY_FALLBACK",
    "ENV_BRIDGE_SOCKET",
    "ENV_BRIDGE_TOKEN",
    "ENV_MAX_TOKENS",
    "ENV_MODEL",
    "ENV_PROVIDER",
    "ENV_TIMEOUT_S",
    "AnalysisProvider",
    "AnalysisRequest",
    "AnthropicAnalyst",
    "AnthropicProvider",
    "BridgeConfig",
    "BridgeError",
    "BridgeUnavailable",
    "DesktopAnalyst",
    "DesktopProvider",
    "LlmConfig",
    "LlmError",
    "LlmPlanner",
    "LlmProposal",
    "LlmProvider",
    "UnwiredProvider",
    "api_key_from_env",
    "bridge_config_from_env",
    "build_analyst",
    "build_catalogue",
    "build_planner",
    "build_provider",
    "llm_config_from_env",
    "validate_proposal",
]
