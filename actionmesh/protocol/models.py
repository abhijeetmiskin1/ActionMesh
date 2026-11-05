"""AMP/1.0 protocol models — single source of truth for ActionMesh contracts.

These are pure data types. They contain no provider logic, no I/O and no
authorization decisions: the LLM, the registry, the policy engine and the
executor all speak this vocabulary.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum, StrEnum
from typing import Any
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


#: First link of a hash chain. Every later record names its predecessor's hash,
#: so editing, deleting or reordering any entry invalidates the chain from that
#: point onward. See ``actionmesh.core.audit``.
GENESIS_HASH = "0" * 64


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


@dataclass
class MeshModel:
    """Base with pydantic-compatible dump helpers (stdlib dataclasses)."""

    def model_dump(self) -> dict[str, Any]:
        return _to_jsonable(asdict(self))  # type: ignore[return-value]

    def model_dump_json(self, indent: int | None = None) -> str:
        return json.dumps(self.model_dump(), indent=indent)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionState(StrEnum):
    CREATED = "created"
    VALIDATING = "validating"
    PLANNED = "planned"
    WAITING_PERMISSION = "waiting_permission"
    AUTHORIZED = "authorized"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DENIED = "denied"
    TIMEOUT = "timeout"
    REQUIRES_USER = "requires_user"
    RETRYING = "retrying"
    PARTIALLY_COMPLETED = "partially_completed"
    #: C35 — the process that owned this run is gone and its outcome cannot be
    #: established. Deliberately *not* FAILED: failure is a statement about the
    #: action, and a server that lost the thread has no such evidence. Nothing is
    #: re-dispatched from here either; a human decides what the unknown means.
    INTERRUPTED = "interrupted"


class NodeType(StrEnum):
    ACTION = "action"
    CONDITION = "condition"
    WAIT = "wait"
    APPROVAL = "approval"
    PARALLEL = "parallel"
    SEQUENCE = "sequence"
    RETRY = "retry"
    COMPENSATION = "compensation"
    END = "end"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    ALLOW_WITH_LIMIT = "allow_with_limit"


class FailureKind(StrEnum):
    TRANSIENT = "transient"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    INVALID_INPUT = "invalid_input"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    PROVIDER = "provider"
    TIMEOUT = "timeout"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


class DeviceKind(StrEnum):
    """Which participant a device record describes.

    A phone, a laptop and the ActionMesh server are *different* principals: an
    audit entry that says only "a device did this" cannot answer the question a
    control plane has to answer. ``UNKNOWN`` exists so a device that has not
    declared its role can be present but never authorized — it is the fail-closed
    default, not a synonym for "laptop".
    """

    UNKNOWN = "unknown"
    PHONE = "phone"
    LAPTOP = "laptop"
    SERVER = "server"


@dataclass
class Identity(MeshModel):
    """Who is acting. Required for any authorization decision."""

    id: str = ""
    kind: str = "user"  # user | device | service
    display: str = ""


@dataclass
class ActionTarget(MeshModel):
    type: str = "person"
    id: str = ""


@dataclass
class RetryPolicy(MeshModel):
    max_attempts: int = 3
    backoff: str = "exponential"
    retry_on: list[FailureKind] = field(
        default_factory=lambda: [
            FailureKind.TRANSIENT,
            FailureKind.NETWORK,
            FailureKind.RATE_LIMIT,
        ]
    )

    def __post_init__(self) -> None:
        if self.backoff not in ("none", "linear", "exponential"):
            raise ValueError(f"invalid backoff: {self.backoff}")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


@dataclass
class VerificationSpec(MeshModel):
    required: bool = True
    method: str = "provider_verify"


@dataclass
class ActionDefinition(MeshModel):
    """Registered capability template (see Action Registry, C2)."""

    name: str = ""
    version: str = "1.0"
    description: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    permissions: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    verification: VerificationSpec = field(default_factory=VerificationSpec)
    provider: str = "mock"
    timeout_s: float = 30.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass
class Action(MeshModel):
    """A structured, typed, permission-controlled operation instance.

    Produced by the planner (possibly from LLM output, which is UNTRUSTED) and
    always re-validated before use.
    """

    type: str = ""
    provider: str = "mock"
    target: ActionTarget | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    requires_confirmation: bool = False
    action_id: str = field(default_factory=lambda: new_id("act"))
    idempotency_key: str = field(default_factory=lambda: new_id("idem"))
    state: ActionState = ActionState.CREATED
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class GraphNode(MeshModel):
    """One node of an :class:`ActionGraph`.

    Node-type specific configuration lives in dedicated, typed fields so a graph
    round-trips through JSON without losing semantics.

    ``depends_on`` is the dependency edge set: a node may run only once every node
    it depends on has SUCCEEDED. ``depends_on`` must form a DAG — cycles are
    rejected by the graph validator. Bounded repetition is expressed with an
    explicit ``RETRY`` node instead of a dependency cycle.
    """

    node_id: str = field(default_factory=lambda: new_id("node"))
    node_type: NodeType = NodeType.ACTION
    action: Action | None = None
    depends_on: list[str] = field(default_factory=list)
    compensation: Action | None = None
    label: str = ""
    condition: str = ""
    wait_s: float = 0.0
    max_attempts: int = 1
    compensates: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.wait_s < 0:
            raise ValueError("wait_s must be >= 0")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


@dataclass
class ActionGraph(MeshModel):
    graph_id: str = field(default_factory=lambda: new_id("graph"))
    intent: str = ""
    nodes: list[GraphNode] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    #: C36 — which computer this graph is addressed to. This is a *routing
    #: request*, not an authority: the executor resolves it through the device
    #: registry and refuses to run if the device is unknown, offline, revoked, or
    #: belongs to someone else. The planner may propose it; only the registry
    #: decides whether it is a real target. Empty means "the local machine", which
    #: is what every pre-C36 caller already meant.
    target_device_id: str = ""


@dataclass
class Permission(MeshModel):
    """A granular, machine-readable authorization scope (e.g. ``message.send``)."""

    name: str = ""
    description: str = ""
    risk_level: RiskLevel = RiskLevel.LOW


@dataclass
class PolicyRule(MeshModel):
    action: str = "*"
    decision: PolicyDecision = PolicyDecision.DENY
    note: str = ""
    limits: dict[str, Any] = field(default_factory=dict)
    permissions: list[str] = field(default_factory=list)


@dataclass
class Policy(MeshModel):
    rules: list[PolicyRule] = field(default_factory=list)
    #: Unlisted actions fall through to this. Defaults to DENY (fail closed).
    default_decision: PolicyDecision = PolicyDecision.DENY


@dataclass
class ApprovalRequest(MeshModel):
    """A structured, auditable request for explicit user approval."""

    approval_id: str = field(default_factory=lambda: new_id("apr"))
    action_id: str = ""
    action_type: str = ""
    summary: str = ""
    risk_level: RiskLevel = RiskLevel.MEDIUM
    details: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending | approved | rejected | expired
    created_at: datetime = field(default_factory=_utcnow)
    #: Approval window: after this moment the approval no longer authorizes.
    #: Mirrors PermissionGrant.expires_at; None means the legacy default TTL.
    expires_at: datetime | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.status == "expired":
            return True
        if self.expires_at is not None:
            return (now or _utcnow()) >= self.expires_at
        # Legacy/test approvals without expires_at: 24h window from creation.
        return (now or _utcnow()) >= self.created_at + timedelta(hours=24)


@dataclass
class VerificationResult(MeshModel):
    """Outcome of independent verification (C6).

    Execution success is NOT verification success. A provider returning HTTP 200
    does not by itself prove the business effect happened.
    """

    verified: bool = False
    method: str = ""
    detail: str = ""
    #: True when the provider is a simulator, so verification is simulated.
    simulated: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class MeshEvent(MeshModel):
    event_id: str = field(default_factory=lambda: new_id("evt"))
    event_type: str = ""
    timestamp: datetime = field(default_factory=_utcnow)
    execution_id: str = ""
    action_id: str = ""
    actor: str = "system"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionRecord(MeshModel):
    execution_id: str = field(default_factory=lambda: new_id("exec"))
    graph_id: str = ""
    actor_id: str = ""
    state: ActionState = ActionState.CREATED
    started_at: datetime = field(default_factory=_utcnow)
    finished_at: datetime | None = None
    node_states: dict[str, str] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    #: C35 — the execution as a durable object, not as a request's local variable.
    #: ``updated_at`` is the only honest answer to "how recent is this?", and
    #: ``owner_id``/``lease_expires_at`` say which process is responsible for
    #: observing it. A process that disappears leaves both behind, which is how the
    #: next one knows to reconcile instead of guessing.
    updated_at: datetime | None = None
    owner_id: str = ""
    lease_expires_at: datetime | None = None
    #: Cancellation addressed at the execution rather than at whichever task happens
    #: to hold it: any request can record the request, the owner honours it.
    cancel_requested: bool = False
    #: C36 — which computer this run was routed to, resolved before dispatch. Kept on
    #: the record rather than only in the request so an offline target, a refusal, and
    #: a completed run can all be attributed to a machine after the fact. Empty means
    #: the local machine, which is what every pre-C36 record was.
    device_id: str = ""
    #: The device's human name as it stood when the run was admitted. Denormalised on
    #: purpose: a record that has to re-open the registry to be readable would change
    #: meaning when the device is renamed or forgotten.
    device_name: str = ""


@dataclass
class AuditRecord(MeshModel):
    """One append-only entry in a tamper-evident audit chain.

    ``record_hash`` covers every other field and ``prev_hash`` binds the entry
    to its predecessor, so editing, deleting or reordering an entry breaks the
    chain from that point on. Entries are produced by
    ``actionmesh.core.audit.AuditLog`` and are never mutated after append.
    """

    record_id: str = field(default_factory=lambda: new_id("aud"))
    seq: int = 0
    timestamp: datetime = field(default_factory=_utcnow)
    #: ``"event"`` for a published MeshEvent, ``"decision"`` for a policy
    #: evaluation. Kept as a plain string so the log accepts new kinds without
    #: a protocol change.
    kind: str = ""
    actor: str = "system"
    #: What the entry is about: an execution id, an action name, a device id.
    subject: str = ""
    #: ``ALLOW`` / ``DENY`` / ``ASK`` / ``""`` — blank when the entry records
    #: something other than an authorization.
    decision: str = ""
    #: Outcome, e.g. ``"success"`` or ``"action.completed"``.
    result: str = ""
    reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = GENESIS_HASH
    record_hash: str = ""


@dataclass
class Capability(MeshModel):
    action: str = ""
    version: str = "1.0"
    provider: str = "mock"
    permissions: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW


@dataclass
class DeviceInfo(MeshModel):
    """A device's presence record — metadata only, never authority.

    These fields say what a device *claims* to be and what it can do. They do not
    say what it is permitted to do: that is the policy engine's decision, and a
    device that lists a capability it was not granted still fails authorization.

    There is deliberately no address or port field. An IP address, a hostname and
    a user-agent string are not identities (see ``core.identity``).
    """

    device_id: str = ""
    name: str = ""
    kind: DeviceKind = DeviceKind.UNKNOWN
    platform: str = ""
    os_version: str = ""
    agent_version: str = ""
    capabilities: list[str] = field(default_factory=list)
    online: bool = False
    last_seen: datetime | None = None
    #: Opaque identity minted by :mod:`actionmesh.core.identity`. Empty until the
    #: device is enrolled; an unenrolled device can be *seen* but never authorized.
    identity_id: str = ""

    # -- C36: what a multi-computer registry has to keep per machine ------------
    #: CPU architecture, e.g. ``x86_64`` / ``arm64``. A capability list is not
    #: enough to decide what will run: a build for the wrong arch is a failure
    #: that happens after dispatch, which is exactly where it must not happen.
    arch: str = ""
    #: The desktop application version, kept apart from ``agent_version``: the UI
    #: and the managed agent can be upgraded independently, and a dashboard that
    #: conflates them cannot tell a stale app from a stale agent.
    app_version: str = ""
    #: The highest risk this device may be asked to perform. Chosen by its owner
    #: when it is enrolled and never raised by pairing — see
    #: :meth:`DeviceRegistry.set_risk_ceiling`. ``LOW`` is the fail-closed default.
    risk_ceiling: RiskLevel = RiskLevel.LOW
    #: When the device was admitted to this mesh. Registration is an event, not a
    #: property of the current heartbeat, so a device that goes quiet keeps its age.
    registered_at: datetime | None = None
    #: The last moment a *connection* was established, as opposed to
    #: ``last_seen`` (the last heartbeat). A device that reconnects after a long
    #: absence has a recent connection time and a stale-until-now presence, and
    #: the difference is what a human reads as "it came back".
    last_connected_at: datetime | None = None
