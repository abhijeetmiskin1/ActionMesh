"""C8 REST API around the trusted ActionMesh core boundaries."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from actionmesh.agent.commands import CommandPolicy
from actionmesh.agent.config import AgentConfig, AgentConfigError
from actionmesh.agent.definitions import PROVIDER, register_device_actions
from actionmesh.agent.paths import Workspace
from actionmesh.agent.presence import LaptopAgent
from actionmesh.agent.provider import LaptopProvider
from actionmesh.agent.runner import CommandRunner
from actionmesh.agent.verification import register_device_strategies
from actionmesh.core.audit import AuditLog, FileAuditAnchor, SqliteAuditBackend
from actionmesh.core.config import AppConfig
from actionmesh.core.credentials import Credential, CredentialError, CredentialStore
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.executor import ExecutionEngine
from actionmesh.core.graph.errors import GraphError
from actionmesh.core.graph.serialization import graph_from_dict
from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.core.output import OUTPUT_EVENT_TYPE
from actionmesh.core.pairing import PairingError, PairingService, format_code
from actionmesh.core.permissions import Consent, ConsentStore, PolicyContext, PolicyEngine
from actionmesh.core.planner import Planner, RuleBasedPlanner
from actionmesh.core.ratelimit import RateLimiter
from actionmesh.core.registry import ActionRegistry
from actionmesh.core.secret import load_key
from actionmesh.core.storage import SQLiteStore
from actionmesh.core.streaming import StreamTicketStore
from actionmesh.dashboard import (
    icon as icon_svg,
)
from actionmesh.dashboard import (
    manifest,
    page,
)
from actionmesh.dashboard import (
    service_worker as service_worker_source,
)
from actionmesh.protocol.models import (
    ActionGraph,
    ApprovalRequest,
    DeviceInfo,
    Identity,
    MeshEvent,
    Policy,
    PolicyDecision,
    PolicyRule,
    RiskLevel,
)
from actionmesh.protocol.validate import validate_action, validate_identity
from actionmesh.providers import default_mock_providers
from actionmesh.voice import page as voice_page
from actionmesh.voiceos import (
    Orchestrator,
    Reasoner,
    TaskSession,
    VoiceOsStore,
    load_templates,
)
from actionmesh.voiceos.llm import build_provider, llm_config_from_env
from actionmesh.voiceos.models import Conversation
from actionmesh.voiceos.reasoning import describe_session

#: Loopback addresses, and nothing else. A deployment behind a relay sets
#: ``ACTIONMESH_ALLOW_REMOTE_PAIRING`` deliberately rather than adding an address
#: here, because "which networks count as being in the room" is not something the
#: protocol should pretend to know.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1"})


def _enum(enum_type: type, value: Any, field: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc


def _policy(raw: Any) -> Policy:
    if raw is None:
        return Policy()
    if not isinstance(raw, dict):
        raise ValueError("policy must be an object")
    rules = []
    for index, item in enumerate(raw.get("rules", [])):
        if not isinstance(item, dict):
            raise ValueError(f"policy.rules[{index}] must be an object")
        rules.append(
            PolicyRule(
                action=str(item.get("action", "")),
                decision=_enum(PolicyDecision, item.get("decision", "deny"), "decision"),
                note=str(item.get("note", "")),
                limits=dict(item.get("limits", {})),
                permissions=list(item.get("permissions", [])),
            )
        )
    return Policy(
        rules=rules,
        default_decision=_enum(
            PolicyDecision, raw.get("default_decision", "deny"), "default_decision"
        ),
    )


def _validated_graph(raw: Any) -> ActionGraph:
    graph = graph_from_dict(raw)
    for node in graph.nodes:
        if node.action is not None:
            node.action = validate_action(node.action)
        if node.compensation is not None:
            node.compensation = validate_action(node.compensation)
    return graph


def _context(
    payload: dict[str, Any],
    consent_store: ConsentStore,
    *,
    credential: Credential | None = None,
    credentials: CredentialStore | None = None,
) -> PolicyContext:
    from actionmesh.core.permissions.context import build_context

    if "grants" in payload:
        raise ValueError("permission grants are server-side; use /consents")
    policy = _policy(payload.get("policy"))
    if credential is not None:
        # A paired device's principal comes from its credential, not from the body.
        # Letting the request name its own actor would make "who asked for this"
        # whatever the caller said it was, which is the opposite of C19.
        actor = Identity(
            id=credential.actor_id, kind="device", display=credential.device_id
        )
        claimed = payload.get("actor")
        if claimed is not None:
            named = validate_identity(claimed)
            if named is not None and named.id != actor.id:
                raise ValueError(
                    f"actor {named.id!r} does not match the paired device "
                    f"({actor.id!r}); a credential cannot act as someone else"
                )
        grants = (
            credentials.grants_for(credential) if credentials is not None else []
        )
        return build_context(None, None, actor, grants=grants, policy=policy)
    requested = validate_identity(payload.get("actor"))
    if requested is None:
        return build_context(None, None, None, policy=policy)
    return build_context(
        None, None, requested, grants=consent_store.grants_for(requested.id), policy=policy
    )


def _is_local(request: Request) -> bool:
    """True for a request that arrived over loopback.

    Pairing-code *issuance* is gated on this. A code is the artifact a human reads
    off the laptop's screen, so the screen and the request have to be the same
    machine; anyone who can mint a code from the other side of the room has removed
    the human from a ceremony whose entire job is to include one.

    This is a transport-level check and it is deliberately the only one in the
    service: the protocol must not care how a request arrived, but admission has to
    care who could have sent it.
    """
    client = request.client
    return client is not None and client.host in _LOCAL_HOSTS


def _remote_pairing_allowed() -> bool:
    return os.getenv("ACTIONMESH_ALLOW_REMOTE_PAIRING", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Keep the local agent's presence fresh while the service is up.

    Registration happens when the app is built, so the device list is never empty
    during startup; only the *loop* belongs to the lifespan. It is stopped explicitly
    rather than abandoned, because an agent that dies with the process never says
    ``device.disconnected`` and leaves a dashboard showing a laptop that has gone.
    """
    agent: LaptopAgent | None = getattr(app.state, "agent", None)
    # Before anything is served: decide what can honestly be said about runs the
    # previous process left behind. Doing this at startup rather than lazily means a
    # client asking "does this execution exist?" gets the same answer as the operator
    # reading the audit -- not an answer that depends on who asked first.
    reconcile = getattr(app.state.engine, "reconcile_interrupted", None)
    if reconcile is not None:
        app.state.startup_reconciliation = await reconcile()
    stop = asyncio.Event()
    task = asyncio.create_task(agent.run(stop)) if agent is not None else None
    try:
        yield
    finally:
        if task is not None:
            stop.set()
            await task
        provider = next(
            (
                candidate
                for candidate in app.state.engine.providers
                if candidate.name == PROVIDER
            ),
            None,
        )
        processes = getattr(provider, "processes", None)
        if processes is not None:
            processes.drain()
        # Give up the lease on anything still running here, so the next process can
        # reconcile it immediately instead of waiting for a lease to expire.
        release = getattr(app.state.engine, "release_leases", None)
        if release is not None:
            release()
        voiceos_store = getattr(app.state, "voiceos_store_instance", None)
        if voiceos_store is not None:
            voiceos_store.close()
        execution_store = getattr(app.state, "execution_store", None)
        if execution_store is not None:
            execution_store.close()


def _device_view(registry: DeviceRegistry, device: DeviceInfo) -> dict[str, Any]:
    """The public shape of a device.

    ``identity_id`` is deliberately absent: it is the handle the *server* uses to
    attribute an action, not something a client needs to name a device, and an
    identifier that only ever appears in an attacker-visible list is a identifier
    an attacker can aim at. Presence is reported from the heartbeat, so the
    ``online`` field the device asserted is never echoed back as fact.
    """
    return {
        "device_id": device.device_id,
        "name": device.name,
        "kind": device.kind.value,
        "platform": device.platform,
        "os_version": device.os_version,
        "agent_version": device.agent_version,
        "capabilities": list(device.capabilities),
        "online": registry.is_online(device),
        "last_seen": device.last_seen.isoformat() if device.last_seen else None,
        "enrolled": bool(device.identity_id),
    }


#: A single serialized event may not exceed this. Provider payloads already pass
#: through the safe result view, so this is a backstop against a future publisher
#: that forgets to bound itself -- a noisy actor must not be able to grow a phone's
#: buffer or the socket it reads from.
MAX_EVENT_BYTES = 16384


def _sse_frame(event: MeshEvent) -> str:
    """One SSE frame, with a resume cursor only where there is something to resume from.

    ``id:`` is how EventSource decides what to send in ``Last-Event-ID`` after a drop.
    Transient output is published live and deliberately never retained -- so handing it
    an id would make the next connection ask for a point in a timeline nothing can
    replay, and the safe fallback (replay the retained window) would then re-send
    everything the client already had.
    """
    head = "" if event.event_type == OUTPUT_EVENT_TYPE else f"id: {event.event_id}\n"
    return f"{head}event: {event.event_type}\ndata: {_event_json(event)}\n\n"


def _event_json(event: MeshEvent) -> str:
    """Serialize one event, replacing any payload that outran the size cap.

    The identity fields survive so a client can still correlate the event; only the
    payload is dropped, and it is dropped loudly rather than truncated mid-object.
    """
    text = event.model_dump_json()
    if len(text.encode("utf-8")) <= MAX_EVENT_BYTES:
        return text
    safe = MeshEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        timestamp=event.timestamp,
        execution_id=event.execution_id,
        action_id=event.action_id,
        actor=event.actor,
        payload={"truncated": True, "reason": "event exceeded the size limit"},
    )
    return safe.model_dump_json()


def create_app(
    engine: ExecutionEngine | None = None,
    consent_store: ConsentStore | None = None,
    audit: AuditLog | None = None,
    planner: Planner | None = None,
    devices: DeviceRegistry | None = None,
    identities: IdentityStore | None = None,
    pairing: PairingService | None = None,
    agent: LaptopAgent | None = None,
) -> FastAPI:
    """Create an API instance with injectable state for tests and deployments.

    Every event the engine publishes is sealed into ``app.state.audit``, so the
    service audits itself by default rather than only when someone remembers to
    opt in. Set ``ACTIONMESH_AUDIT_DB`` to persist the chain to SQLite; a
    persisted chain that does not verify **stops startup** instead of being
    silently inherited.
    """
    # Deployment mode is decided once, here, and validated: a production process that
    # is missing durable storage, a key, or a durable signing secret does not start.
    app_config = AppConfig.from_env()
    owned_store: SQLiteStore | None = None
    if engine is None:
        registry = ActionRegistry.with_seeds()
        # Durable execution records only when the config names a file; development
        # keeps the in-memory ledger and pays nothing for it.
        store = (
            SQLiteStore(app_config.execution_db_path)
            if app_config.execution_db_path
            else None
        )
        engine = ExecutionEngine(
            registry, PolicyEngine(), default_mock_providers(), store=store
        )
        # This app opened it, so this app is the one that has to close it: the durable
        # store is handed to the lifespan, same as the audit backend above. An injected
        # engine keeps its own store -- ownership does not move by being passed around.
        owned_store = store
    if audit is None:
        audit_path = app_config.audit_db
        if audit_path:
            # AuditLog.open() verifies the restored chain and refuses to start on
            # tampered history. The backend is closed on that failure path, so a
            # rejected store does not stay locked.
            #
            # ACTIONMESH_AUDIT_ANCHOR, when set, adds truncation detection: a
            # hash chain cannot notice that its own tail was deleted. It must
            # point somewhere the audit database cannot rewrite, or it buys
            # nothing.
            anchor_path = app_config.audit_anchor
            anchor = FileAuditAnchor(anchor_path) if anchor_path else None
            audit_backend = SqliteAuditBackend(audit_path)
            try:
                audit = AuditLog.open(audit_backend, anchor=anchor)
            except Exception:
                audit_backend.close()
                raise
        else:
            audit_backend = None
            audit = AuditLog()
    else:
        # A caller-supplied log brings its own backend, if any.
        audit_backend = getattr(audit, "backend", None)
    # An injected engine may carry its own hub; only wire the ones that support it.
    attach = getattr(engine.events, "attach_audit", None)
    if callable(attach):
        attach(audit)
    # C35: the event timeline has to survive the process that wrote it, or a phone that
    # reconnects after a restart can read the verdict and nothing else. Same store the
    # records live in -- transient output is still not written, by the hub's own rule.
    keep_events = getattr(engine.events, "attach_store", None)
    if callable(keep_events) and owned_store is not None:
        keep_events(owned_store)
    app = FastAPI(title="ActionMesh API", version="AMP/1.0", lifespan=_lifespan)
    app.state.engine = engine
    #: The execution store this app created itself, if any. `None` for an injected
    #: engine, whose store belongs to whoever injected it.
    app.state.execution_store = owned_store
    app.state.audit = audit
    #: Created here when ACTIONMESH_AUDIT_DB is set, so a shutdown handler or a
    #: test can release it. Ownership stays with whoever created it.
    app.state.audit_backend = audit_backend
    app.state.consent_store = consent_store if consent_store is not None else ConsentStore()
    #: Intent -> untrusted candidate plan. The default planner is deterministic
    #: and offline, exactly like the default mock providers.
    app.state.planner = planner if planner is not None else RuleBasedPlanner()
    #: VoiceOS (C34) — conversation and task state, kept apart from every
    # authority-bearing store. A session remembers what was asked and what the
    # executor reported. It holds no grants, and nothing reads it when a permission
    # is decided, because a conversation that could describe its own authority would
    # be a second policy engine.
    voiceos_db = os.getenv("ACTIONMESH_VOICEOS_DB", "").strip()
    if not voiceos_db and app_config.audit_db:
        voiceos_db = str(Path(str(app_config.audit_db)).with_name("voiceos.db"))

    def _voiceos_store() -> VoiceOsStore:
        # Opened on first VoiceOS turn, not on import: a server that never serves one
        # should not be holding a database handle, and a handle nobody owns is how a
        # suite stops being warning-clean.
        opened = getattr(app.state, "voiceos_store_instance", None)
        if opened is None:
            opened = VoiceOsStore(voiceos_db or ":memory:")
            app.state.voiceos_store_instance = opened
        return opened
    app.state.voiceos_orchestrator = Orchestrator(engine=engine)
    #: A configured-but-unreachable LLM provider is stated, never hidden.
    app.state.voiceos_llm_note = build_provider(llm_config_from_env())[1]
    app.state.voiceos_template_refusals = ()

    def _voiceos_reasoner() -> Reasoner:
        # Templates are re-read per turn on purpose: an operator editing their own
        # task file should see the next request use it, and a refused entry has to
        # surface rather than sit in a stale cache.
        source = load_templates(os.getenv("ACTIONMESH_VOICEOS_TASKS", "").strip() or None, engine.registry.has)
        app.state.voiceos_template_refusals = source.refusals
        return Reasoner(
            planner=app.state.planner,
            known_action=engine.registry.has,
            templates=lambda: source.templates,
            device_id=_voiceos_device_id(),
        )

    app.state.voiceos_reasoner = _voiceos_reasoner
    # C19 device plane. The identity store is the authority on *which machine* an
    # action ran on; the registry is presence. They are wired to the same store the
    # agent and pairing flows use, so an identity minted here is the one a credential
    # later binds to — not a second, disagreeing copy.
    app.state.identities = identities if identities is not None else IdentityStore()
    app.state.devices = (
        devices
        if devices is not None
        else DeviceRegistry(
            identities=app.state.identities,
            known_capability=engine.registry.has,
        )
    )
    # C19 laptop agent: presence for this machine, enabled only when an operator
    # asks for it. A misconfigured boundary is a startup failure, not a quietly
    # reduced capability list — the difference between "this laptop has no
    # workspace" and "this laptop's workspace is elsewhere than you think".
    if agent is None and AgentConfig.enabled():
        try:
            config = AgentConfig.from_env()
        except AgentConfigError as exc:
            raise ValueError(f"agent configuration is invalid: {exc}") from exc
        agent = LaptopAgent(app.state.devices, config, events=engine.events)
    if agent is not None:
        # Only what the configuration opened is registered. A capability the operator
        # did not enable stays an unknown action, which is where the strongest
        # fail-closed boundary sits.
        register_device_actions(engine.registry, agent.config.advertise())
        # A real adapter goes *first*. The generic mock fallback answers for any action
        # whose provider field defaulted to "mock", so an adapter appended behind it
        # would never be consulted — and a real file write would be reported as a
        # simulated one that "succeeded". Specific beats general, and real beats
        # simulated, in that order.
        if not any(provider.name == PROVIDER for provider in engine.providers):
            workspace = (
                Workspace(agent.config.workspace_root)
                if agent.config.workspace_root
                else None
            )
            engine.providers.insert(
                0,
                LaptopProvider(
                    workspace,
                    command_policy=CommandPolicy.from_config(agent.config),
                    runner=CommandRunner(
                        max_concurrent=agent.config.max_concurrent,
                        timeout_s=agent.config.command_timeout_s,
                    ),
                    # Empty unless the operator opened a log directory; a launched child
                    # then writes there instead of to /dev/null.
                    log_dir=agent.config.log_dir,
                    # C35: the same durable store the records live in, so a restarted server can
                    # ask this agent what its executions actually dispatched.
                    ledger=getattr(engine, "store", None),
                ),
            )
            register_device_strategies(engine.verifier)
        agent.register()
        agent.heartbeat()
        app.state.agent = agent
    else:
        app.state.agent = None
    # C19b/c credentials and pairing. Key material decides whether a pairing
    # survives a restart, so the server keeps hold of it and reports the answer
    # instead of letting a phone discover it.
    app.state.server_key = load_key()
    if pairing is not None:
        app.state.pairing = pairing
        # An injected service already owns its store; a second copy would disagree
        # with the first about which credentials exist.
        app.state.credentials = pairing.credentials
    else:
        credentials = CredentialStore(
            app.state.identities,
            key=app.state.server_key.material,
            consents=app.state.consent_store.grants_for,
        )

        def record_pairing_event(
            event: str, subject: str, payload: dict[str, object]
        ) -> None:
            """Chain a pairing event. Returns nothing on purpose.

            The pairing service is handed a sink, not a value-returning hook: an
            ``AuditLog.append`` result leaking back would invite a caller to treat a
            sealed record as part of the pairing decision, and it is not.
            """
            audit.append(
                kind="pairing",
                actor="pairing",
                subject=subject,
                result=event,
                payload=payload,
            )

        app.state.credentials = credentials
        app.state.pairing = PairingService(
            identities=app.state.identities,
            credentials=credentials,
            devices=app.state.devices,
            key=app.state.server_key.material,
            audit=record_pairing_event,
        )
    configured_key = app_config.api_key
    max_request_bytes = app_config.max_request_bytes
    # Watch-only, actor-bound tickets for the SSE/WS stream (see core.streaming).
    app.state.stream_tickets = StreamTicketStore()
    app.state.config = app_config
    # Process-local sliding-window limits on the auth-sensitive and resource-hungry
    # endpoints. Generous enough that a human at a phone never feels them; tight enough
    # that a script cannot spray pairing codes, mint tickets, or spawn executions.
    # Bounds are per app instance; a multi-process deployment enforces the same policy
    # at the proxy (see docs/deployment.md). ACTIONMESH_RATE_LIMIT=0 disables them for
    # tests and local development that needs to hammer its own server.
    rate_limiting_on = os.getenv("ACTIONMESH_RATE_LIMIT", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    limiters: dict[str, RateLimiter] = {
        "pairing_mint": RateLimiter(limit=10, window_s=60),
        "pair": RateLimiter(limit=20, window_s=60),
        "ticket": RateLimiter(limit=60, window_s=60),
        "execute": RateLimiter(limit=120, window_s=60),
        "approval": RateLimiter(limit=120, window_s=60),
        "cancel": RateLimiter(limit=60, window_s=60),
        "voiceos": RateLimiter(limit=60, window_s=60),
    }
    app.state.rate_limiters = limiters

    def _throttle(name: str, key: str) -> None:
        if rate_limiting_on and not limiters[name].allow(key):
            raise HTTPException(
                status_code=429,
                detail="too many requests; slow down",
                headers={"retry-after": "60"},
            )

    def _peer(request: Request) -> str:
        client = request.client
        return client.host if client is not None else "unknown"

    def _credential_key(request: Request) -> str:
        """Who to charge for a request: the credential it carries, else the peer."""
        credential = getattr(request.state, "credential", None)
        return credential.actor_id if credential is not None else _peer(request)

    @app.middleware("http")
    async def require_api_key(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > max_request_bytes
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid content length"})
            if too_large:
                return JSONResponse(status_code=413, content={"detail": "request too large"})
        # C19b: a paired device presents its own credential. The shared API key,
        # when configured, still applies — a device token says *which* device sent
        # this, not that the port is open to the network.
        presented = request.headers.get("authorization", "")
        if presented:
            scheme, _, token = presented.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                return JSONResponse(
                    status_code=401, content={"detail": "unsupported authorization"}
                )
            try:
                request.state.credential = app.state.credentials.authenticate(
                    token.strip()
                )
            except (CredentialError, IdentityError):
                # Authentication resolves through the identity store, so a revoked
                # *device* arrives here as an identity failure rather than a
                # credential one. Both answer identically: which one it was would tell
                # an attacker that a credential or device id they guessed is real.
                return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        if configured_key and request.url.path != "/health":
            supplied_key = request.headers.get("x-api-key", "")
            if not hmac.compare_digest(supplied_key, configured_key):
                return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "protocol": "AMP/1.0"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        """Readiness, kept distinct from liveness and from device connectivity.

        A server can be perfectly healthy with every laptop offline, so ``online_devices``
        is reported but never gates readiness. What does gate it is whether the state this
        process depends on is actually reachable: the durable execution store and the
        audit chain. Nothing here leaks key material or private paths.
        """
        store = app.state.engine.store
        execution_ready = True if store is None else bool(getattr(store, "healthy", lambda: True)())
        audit_ready = True
        try:
            _ = app.state.audit.head  # cheap: the backend answers and has a head
        except Exception:
            audit_ready = False
        registry: DeviceRegistry = app.state.devices
        online = sum(1 for device in registry.all_devices() if registry.is_online(device))
        is_ready = execution_ready and audit_ready
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "ready" if is_ready else "degraded",
                "mode": app_config.mode.value,
                "durable_execution": app_config.durable_execution,
                "durable_audit": app_config.durable_audit,
                "execution_ready": execution_ready,
                "audit_ready": audit_ready,
                "online_devices": online,
            },
        )

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        return page()

    @app.get("/voice", response_class=HTMLResponse)
    def voice() -> str:
        """VoiceOS: a microphone on the existing pipeline, and nothing else.

        The page proposes through `/plan` and has no execution control at all, so it
        cannot run what it heard. Same boundary as every other surface: an API key, if
        configured, is required to reach it.
        """
        return voice_page()

    # -- installable-app support (C16/C17) --------------------------------
    # These make the dashboard installable to a phone's home screen. The PWA is
    # the zero-toolchain option; C17 also ships a native APK shell that loads the
    # same /dashboard.

    @app.get("/manifest.webmanifest")
    def webmanifest() -> dict[str, object]:
        return manifest()

    @app.get("/sw.js")
    def service_worker() -> Response:
        # Not under the API-key boundary in normal operation: a service worker
        # cannot present a custom header, and locking it out would simply stop
        # installability. It caches nothing sensitive.
        return Response(
            content=service_worker_source(),
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/icon.svg")
    def icon() -> Response:
        return Response(
            content=icon_svg(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/metrics")
    def metrics() -> dict[str, int]:
        return engine.metrics.snapshot()

    @app.get("/capabilities")
    def capabilities(provider: str | None = None) -> list[dict[str, Any]]:
        return [cap.model_dump() for cap in engine.registry.discover(provider=provider)]

    # -- devices (C19) ----------------------------------------------------
    #
    # Read, and revoke. There is deliberately **no** registration or heartbeat
    # route: presence is asserted by the agent that owns the laptop, and admission
    # of a remote controller is pairing's job (C19b). An HTTP endpoint that could
    # mark a device online, or enroll one, would let any client that reached the
    # port mint the principal that audit records then have to trust.

    @app.get("/devices")
    def devices_list() -> list[dict[str, Any]]:
        registry: DeviceRegistry = app.state.devices
        return [_device_view(registry, device) for device in registry.all_devices()]

    @app.get("/devices/{device_id}")
    def device_detail(device_id: str) -> dict[str, Any]:
        registry: DeviceRegistry = app.state.devices
        device = registry.devices.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="device not found")
        return _device_view(registry, device)

    @app.post("/devices/{device_id}/revoke")
    def device_revoke(
        request: Request, device_id: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Forget a device and revoke the identity it was bound to.

        The response for a stolen phone or a lost laptop. Both halves go together:
        deleting the presence record while leaving the identity live would keep the
        credential usable under a device the dashboard no longer shows.

        Revoking is destruction with a login attached, so a paired device may do it only
        to itself. The answer for "not yours" is the same 404 as for "not there": device
        ids are names the caller chose, so confirming which ones are enrolled is itself
        the leak.
        """
        caller = _record_scope(request)
        credential = getattr(request.state, "credential", None)
        if caller is not None and (
            credential is None or credential.device_id != device_id
        ):
            raise HTTPException(status_code=404, detail="device not found")
        registry: DeviceRegistry = app.state.devices
        device = registry.devices.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="device not found")
        identity_id = device.identity_id
        reason = str((payload or {}).get("reason", ""))[:200]
        try:
            forgotten = registry.forget(device_id)
        except IdentityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        app.state.audit.append(
            kind="device",
            actor="api",
            subject=device_id,
            result="revoked" if forgotten else "not_found",
            reason=reason,
            payload={
                "identity_id": identity_id,
                "kind": device.kind.value,
                "capabilities": list(device.capabilities),
            },
        )
        return {"device_id": device_id, "revoked": forgotten, "identity_id": identity_id}

    # -- pairing (C19c) ---------------------------------------------------
    #
    # Minting a code is loopback-only; spending one is not. That asymmetry is the
    # design: the code is what a human reads off the laptop's screen, and the phone
    # is remote by definition.

    @app.post("/pairing/code")
    def pairing_code(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """Mint a pairing code for an enrolled laptop, for display on its screen."""
        if not _is_local(request) and not _remote_pairing_allowed():
            raise HTTPException(
                status_code=403,
                detail=(
                    "pairing codes are issued on the laptop itself; open the "
                    "dashboard there, or set ACTIONMESH_ALLOW_REMOTE_PAIRING if the "
                    "laptop is reachable only through a relay you control"
                ),
            )
        device_id = payload.get("device_id")
        if not isinstance(device_id, str) or not device_id.strip():
            raise HTTPException(status_code=400, detail="device_id is required")
        _throttle("pairing_mint", f"{_peer(request)}:{device_id.strip()}")
        # The ceiling is chosen *here*, by whoever is standing at the laptop, and is
        # recorded on the code. `POST /pair` cannot set it: a limit the requester
        # picks is not a limit. LOW is the default, so asking for more is the visible
        # act — and the value is echoed back so the screen shows what is being granted.
        raw_risk = payload.get("max_risk", "low")
        try:
            ceiling = _enum(RiskLevel, raw_risk, "max_risk")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            record, code = app.state.pairing.issue(device_id, max_risk=ceiling)
        except PairingError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # The plaintext goes to this response and nowhere else: not to a log, not to
        # the audit chain (the pairing service screens it out), not to a query string.
        return {
            "code": format_code(code),
            "laptop_device_id": record.laptop_device_id,
            "expires_at": record.expires_at.isoformat(),
            "expires_in_s": max(
                0.0, (record.expires_at - record.created_at).total_seconds()
            ),
            "max_risk": record.max_risk.value,
            "pairing_durable": app.state.server_key.durable,
        }

    @app.post("/pairing/revoke")
    def pairing_revoke(request: Request) -> dict[str, Any]:
        """Burn every outstanding code. Reach for it after an unexpected prompt."""
        if not _is_local(request) and not _remote_pairing_allowed():
            raise HTTPException(status_code=403, detail="pairing codes are managed locally")
        return {"revoked": app.state.pairing.revoke_all()}

    @app.post("/pair")
    def pair(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """Spend a pairing code for a device credential.

        The token appears in this response exactly once. There is no route that
        reads a credential back, and no ceiling can be requested: pairing admits a
        device, it does not authorize one. What the paired phone may later ask for
        is decided by the consent store and the policy engine, exactly as it is for
        any other actor.
        """
        # A coarse per-source cap on top of the pairing service's own per-code attempt
        # budget: the code budget stops guessing one code; this stops spraying many.
        _throttle("pair", _peer(request))
        try:
            credential, token, device = app.state.pairing.redeem(
                payload.get("code"),
                phone_device_id=str(payload.get("device_id", "")),
                actor_id=str(payload.get("actor_id", "")),
                phone_name=str(payload.get("name", "")),
            )
        except (PairingError, IdentityError, CredentialError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "device_id": device.device_id,
            "actor_id": credential.actor_id,
            "credential_id": credential.credential_id,
            "token": token,
            "max_risk": credential.max_risk.value,
            "expires_at": credential.expires_at.isoformat()
            if credential.expires_at
            else None,
            "laptop_device_id": credential.label,
        }

    @app.post("/plan")
    def propose(payload: dict[str, Any]) -> dict[str, Any]:
        """Propose candidate actions from natural-language intent.

        This is the only endpoint model-shaped input reaches. It returns a
        *proposal*: nothing is authorized, approved or executed here. Send the
        plan to ``POST /execute`` to run it through the policy engine, where a
        CRITICAL-risk action still requires explicit user approval.
        """
        intent = payload.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            raise HTTPException(status_code=400, detail="intent must be a non-empty string")
        proposed = app.state.planner.plan(intent)
        return {
            "intent": proposed.intent,
            "actions": [action.as_dict() for action in proposed.actions],
            "notes": list(proposed.notes),
        }

    @app.post("/execute")
    async def execute(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        credential = getattr(request.state, "credential", None)
        _throttle(
            "execute",
            credential.actor_id if credential is not None else _peer(request),
        )
        try:
            graph = _validated_graph(payload.get("graph"))
            context = _context(
                payload,
                app.state.consent_store,
                credential=getattr(request.state, "credential", None),
                credentials=app.state.credentials,
            )
            result = await engine.execute(graph, context)
        except (GraphError, TypeError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.to_dict()

    @app.get("/executions/{execution_id}")
    def execution(request: Request, execution_id: str) -> dict[str, Any]:
        record = engine.record(execution_id)
        scope = _record_scope(request)
        if record is None or (scope is not None and record.actor_id != scope):
            # One answer for "never existed" and "exists, not yours": the difference
            # would tell a caller which execution ids are real.
            raise HTTPException(status_code=404, detail="execution not found")
        return record.model_dump()

    @app.get("/executions")
    def executions(request: Request, limit: int = 25) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
        scope = _record_scope(request)
        records = engine.records()
        if scope is not None:
            records = [record for record in records if record.actor_id == scope]
        return [record.model_dump() for record in records[:limit]]

    def _stream_principal(
        *, is_local: bool, ticket: str | None, credential: Credential | None
    ) -> tuple[str | None, bool]:
        """Resolve who a stream is scoped to, and whether the caller is denied.

        Returns ``(actor, denied)``. A ticket or bearer credential pins the stream to
        that actor and nothing can widen it. **A caller that presents neither is the
        operator only if it is on the machine the server runs on** -- the loopback
        boundary is enforced here, on the server, not in the page's JavaScript. A
        remote client that has no credential (a phone whose token was just rejected and
        cleared) is denied outright; losing a credential can never silently upgrade a
        client to the unscoped operator view.
        """
        if ticket:
            actor = app.state.stream_tickets.resolve(ticket)
            return (actor, actor is None)
        if credential is not None:
            return (credential.actor_id, False)
        if is_local:
            return (None, False)
        return (None, True)

    def _record_scope(request: Request) -> str | None:
        """Whose records this caller may read. ``None`` is the machine's own operator.

        This is the stream's rule and no other, on purpose: a boundary expressed twice
        is two boundaries, and they drift. Reading an execution record discloses the
        same metadata the stream would have -- the action types, the states, the paths a
        run touched -- so a phone that cannot *watch* another device's execution must
        not be able to fetch it either.
        """
        actor, denied = _stream_principal(
            is_local=_is_local(request),
            ticket=None,
            credential=getattr(request.state, "credential", None),
        )
        if denied:
            raise HTTPException(
                status_code=401,
                detail=(
                    "this request has to come from the laptop itself, or carry a "
                    "paired device's credential"
                ),
            )
        return actor

    @app.get("/events/stream-ticket")
    def stream_ticket(request: Request) -> dict[str, Any]:
        """Exchange a device credential for a short-lived, watch-only ticket.

        An ``EventSource`` cannot send an ``Authorization`` header, so this is how a
        paired phone authenticates its stream without the long-lived credential ever
        appearing in a URL, a referrer header, or a proxy log.
        """
        credential = getattr(request.state, "credential", None)
        if credential is None:
            raise HTTPException(
                status_code=401,
                detail="a device credential is required to watch the stream",
            )
        _throttle("ticket", credential.actor_id)
        token, ttl = app.state.stream_tickets.mint(credential.actor_id)
        return {"ticket": token, "expires_in_s": ttl, "actor": credential.actor_id}

    @app.get("/events")
    async def events(
        request: Request,
        execution_id: str | None = None,
        ticket: str | None = None,
    ) -> StreamingResponse:
        credential = getattr(request.state, "credential", None)
        principal, denied = _stream_principal(
            is_local=_is_local(request), ticket=ticket, credential=credential
        )
        if denied:
            raise HTTPException(
                status_code=401,
                detail="this stream requires a device credential or a valid ticket",
            )
        allow = None if principal is None else (lambda event: event.actor == principal)
        # ``Last-Event-ID`` is sent by EventSource automatically on reconnect, so a
        # phone that dropped resumes exactly where it left off instead of replaying
        # -- or missing -- the window it was asleep through. It narrows history; it
        # never authorizes anything.
        since = request.headers.get("last-event-id")

        async def stream() -> AsyncIterator[str]:
            async for event in engine.events.subscribe(
                execution_id, allow=allow, since_event_id=since
            ):
                yield _sse_frame(event)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.websocket("/ws/events")
    async def websocket_events(
        websocket: WebSocket,
        execution_id: str | None = None,
        ticket: str | None = None,
    ) -> None:
        # A WebSocket handshake bypasses the HTTP middleware, so authentication is
        # redone here, with the same rule as ``/events``: a credential or a valid
        # ticket scopes the stream; a caller with neither is the operator only over
        # loopback, and is denied from anywhere else.
        client = websocket.client
        is_local = client is not None and client.host in _LOCAL_HOSTS
        supplied_key = websocket.headers.get("x-api-key", "")
        key_ok = not configured_key or hmac.compare_digest(supplied_key, configured_key)
        principal: str | None = None
        authenticated = False
        presented = websocket.headers.get("authorization", "")
        if ticket:
            resolved = app.state.stream_tickets.resolve(ticket)
            if resolved is not None:
                principal = resolved
                authenticated = True
        elif presented:
            scheme, _, token = presented.partition(" ")
            try:
                if scheme.lower() != "bearer" or not token.strip():
                    raise CredentialError("unsupported authorization")
                principal = app.state.credentials.authenticate(token.strip()).actor_id
                authenticated = True
            except (CredentialError, IdentityError):
                authenticated = False
        if not authenticated and (not is_local or not key_ok):
            # Remote-and-anonymous, or a protected port without the key: never the
            # operator view. This is the loopback boundary, enforced server-side.
            await websocket.close(code=1008)
            return
        await websocket.accept()
        allow = None if principal is None else (lambda event: event.actor == principal)
        since = websocket.query_params.get("last_event_id") or websocket.headers.get(
            "last-event-id"
        )
        try:
            async for event in engine.events.subscribe(
                execution_id, allow=allow, since_event_id=since
            ):
                await websocket.send_json(json.loads(_event_json(event)))
        except WebSocketDisconnect:
            return

    @app.get("/approvals")
    def approvals_pending(request: Request) -> list[dict[str, Any]]:
        """Executions stopped for a human, and the ids they are waiting on.

        Needed because action ids are minted by the server and never accepted from a
        request body. A client that saw `waiting_permission` cannot name the action it
        means to approve unless the server says which one it asked about.

        Scoped to the caller: an approval entry carries the action's **parameters**,
        which is the message body, the file path, the command line. Handing those to a
        different device is a disclosure, not a to-do list.
        """
        scope = _record_scope(request)
        waiting = engine.awaiting_approval()
        if scope is None:
            return waiting
        return [entry for entry in waiting if entry.get("actor") == scope]

    @app.post("/approvals")
    async def approval(http_request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        _throttle("approval", _peer(http_request))
        action_id = payload.get("action_id")
        status = payload.get("status", "pending")
        if not isinstance(action_id, str) or not action_id.strip():
            raise HTTPException(status_code=400, detail="action_id is required")
        if status not in {"pending", "approved", "rejected", "expired"}:
            raise HTTPException(status_code=400, detail="invalid approval status")
        scope = _record_scope(http_request)
        if scope is not None:
            # The decision is the human-in-the-loop, so whose human it was is the whole
            # question: without this, any paired phone could approve another actor's
            # parked action and be handed back their resumed record.
            owner = next(
                (
                    entry.get("actor")
                    for entry in engine.awaiting_approval()
                    if any(
                        candidate.get("action_id") == action_id
                        for candidate in entry.get("actions", [])
                    )
                ),
                None,
            )
            if owner is None:
                raise HTTPException(
                    status_code=404, detail="no waiting action has that id"
                )
            if owner != scope:
                raise HTTPException(
                    status_code=403, detail="that action is not yours to decide"
                )
        request = ApprovalRequest(
            action_id=action_id,
            action_type=str(payload.get("action_type", "")),
            summary=str(payload.get("summary", "")),
            status=status,
        )
        recorded = engine.submit_approval(request)
        out: dict[str, Any] = recorded.model_dump()
        execution_id = payload.get("execution_id")
        if isinstance(execution_id, str) and execution_id.strip():
            # Resuming is only offered for an execution that is actually waiting, and
            # the approval is still matched on `action_id` at dispatch: approving some
            # other action in the graph changes nothing here.
            waiting = {item["execution_id"] for item in engine.awaiting_approval()}
            if execution_id not in waiting:
                raise HTTPException(
                    status_code=409,
                    detail="execution is not awaiting approval",
                )
            resumed = await engine.resume(execution_id)
            if resumed is not None:
                out["execution"] = resumed.to_dict()
        return out

    @app.post("/consents")
    def consent(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        scope = payload.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            raise HTTPException(status_code=400, detail="scope is required")
        # Consent is the human's act. A device may record one for the actor its
        # credential already names; naming someone else's actor is the operator's act,
        # and the operator -- having no actor of its own -- must say who it means.
        caller = _record_scope(request)
        named = payload.get("actor_id")
        if caller is None:
            if not isinstance(named, str) or not named.strip():
                raise HTTPException(status_code=400, detail="actor_id is required")
            actor_id = named.strip()
        else:
            if isinstance(named, str) and named.strip() and named.strip() != caller:
                raise HTTPException(
                    status_code=403,
                    detail="a device may only record its own consent",
                )
            actor_id = caller
        try:
            record = app.state.consent_store.grant(
                Consent(
                    scope=scope,
                    granted_by=actor_id,
                    reason=str(payload.get("reason", "")),
                    max_risk=_enum(RiskLevel, payload.get("max_risk", "high"), "max_risk"),
                )
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return record.model_dump()

    @app.get("/consents")
    def consents(request: Request, actor_id: str | None = None) -> list[dict[str, Any]]:
        caller = _record_scope(request)
        if caller is None:
            # The operator reads on behalf of whoever it names and has no actor of its
            # own to fall back to, so for it the name stays required.
            if not actor_id or not actor_id.strip():
                raise HTTPException(status_code=400, detail="actor_id is required")
            wanted = actor_id.strip()
        else:
            # A device's credential already says whose consents these are. It may repeat
            # that name, and it may not ask for anyone else's.
            if actor_id and actor_id.strip() and actor_id.strip() != caller:
                raise HTTPException(
                    status_code=403, detail="a device may only read its own consents"
                )
            wanted = caller
        return [
            record.model_dump()
            for record in app.state.consent_store.consents()
            if record.granted_by == wanted
        ]

    @app.post("/executions/{execution_id}/cancel")
    def cancel(request: Request, execution_id: str) -> dict[str, str]:
        scope = _record_scope(request)
        record = engine.record(execution_id)
        if record is None or (scope is not None and record.actor_id != scope):
            # Stopping someone else's run is a denial of service with a polite name.
            raise HTTPException(status_code=404, detail="execution not found")
        _throttle("cancel", scope or _peer(request))
        # C35: addressed at the execution, not at whichever task happens to hold it. The
        # flag goes on the durable record and the process that owns the run acts on it --
        # so a Cancel from a different request, or a different server, has the same
        # effect as one from the request that started the work.
        answer = engine.request_cancel(execution_id)
        # ``requested``, not ``cancelled``: the executor notices at its next await, and
        # a control plane that announces an outcome before it happened is the pattern
        # this whole platform exists to remove.
        status = (
            "requested"
            if answer["recorded"]
            else ("already_requested" if answer["already_requested"] else answer["reason"])
        )
        return {"execution_id": execution_id, "status": status}

    @app.get("/audit")
    def audit_trail(request: Request, limit: int = 25) -> dict[str, Any]:
        if limit < 1 or limit > 1000:
            raise HTTPException(
                status_code=400, detail="limit must be between 1 and 1000"
            )
        scope = _record_scope(request)
        chain = app.state.audit.verify()
        records = app.state.audit.records[-limit:] if scope is None else [
            entry for entry in app.state.audit.records if entry.actor == scope
        ][-limit:]
        return {
            "chain": {
                "ok": chain.ok,
                "records": chain.records,
                "broken_at": chain.broken_at,
                "problem": chain.problem,
            },
            "head": app.state.audit.head,
            "records": [r.model_dump() for r in records],
        }

    @app.get("/audit/verify")
    def audit_verify() -> dict[str, Any]:
        chain = app.state.audit.verify()
        return {
            "ok": chain.ok,
            "records": chain.records,
            "broken_at": chain.broken_at,
            "problem": chain.problem,
            "head": app.state.audit.head,
        }

    # -- VoiceOS conversation and task sessions (C34) ----------------------
    #
    # Thin routes on purpose: understand, store, and hand execution to the engine
    # through the *same* context builder every other write path uses. A session can
    # never carry more authority than the credential that pressed run, because it does
    # not carry any of its own.

    def _voiceos_device_id() -> str:
        registry: DeviceRegistry = app.state.devices
        devices = registry.all_devices()
        # Presence is the heartbeat's answer, not the device's own claim, so it
        # is the only honest thing to name in a header.
        for device in devices:
            if registry.is_online(device):
                return device.device_id
        return devices[0].device_id if devices else ""

    def _voiceos_actor(request: Request, payload: dict[str, Any]) -> str:
        """Whose session this is — the same precedence the execution path uses.

        A GET has no body, so the local operator view names its actor in `actor_id`
        instead — the same convention `/consents` already uses. A credential never
        reads its identity from either: the credential *is* the identity.
        """
        credential = getattr(request.state, "credential", None)
        if credential is not None:
            claimed = payload.get("actor")
            if claimed is not None:
                named = validate_identity(claimed)
                if named is not None and named.id != credential.actor_id:
                    raise HTTPException(
                        status_code=403,
                        detail="a paired device cannot speak for another actor",
                    )
            claimed = request.query_params.get("actor_id")
            if claimed is not None and claimed != credential.actor_id:
                raise HTTPException(
                    status_code=403,
                    detail="a paired device cannot speak for another actor",
                )
            return credential.actor_id
        _, denied = _stream_principal(is_local=_is_local(request), ticket=None, credential=None)
        if denied:
            raise HTTPException(
                status_code=401,
                detail="this request has to come from the laptop itself, or carry a credential",
            )
        requested = validate_identity(payload.get("actor"))
        if requested is not None:
            return requested.id
        query_actor = (request.query_params.get("actor_id") or "").strip()
        return query_actor or "voiceos-local"

    def _voiceos_load(session_id: str, actor: str | None) -> TaskSession:
        session = _voiceos_store().get_session(session_id)
        # One answer for "never existed" and "exists, not yours", exactly as with
        # execution records: the difference would confirm which ids are real.
        if session is None or (actor is not None and session.actor_id != actor):
            raise HTTPException(status_code=404, detail="session not found")
        return session

    @app.post("/voiceos/messages")
    async def voiceos_message(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """One conversational turn. It understands and proposes; it never runs."""
        _throttle("voiceos", _credential_key(request))
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="text must be a non-empty string")
        actor = _voiceos_actor(request, payload)
        store: VoiceOsStore = _voiceos_store()
        conversation = store.get_conversation(actor)
        conversation.active_session = (
            store.get_session(conversation.active_session_id)
            if conversation.active_session_id
            else None
        )
        understanding = app.state.voiceos_reasoner().understand(text, conversation)
        reply = understanding.reply
        if understanding.kind == "clear":
            store.clear_conversation(actor)
            conversation = Conversation(actor_id=actor, device_id=_voiceos_device_id())
        else:
            conversation.remember("user", text)
            conversation.remember("voiceos", reply)
        session_id = understanding.session.session_id if understanding.session else ""
        if understanding.session is not None:
            store.save_session(understanding.session)
            conversation.active_session_id = understanding.session.session_id
            conversation.active_session = understanding.session
        if understanding.kind == "stop" and understanding.target_execution_id:
            engine.request_cancel(understanding.target_execution_id)
        store.save_conversation(conversation)
        notes = list(understanding.notes)
        if app.state.voiceos_llm_note:
            notes.append(str(app.state.voiceos_llm_note))
        notes.extend(app.state.voiceos_template_refusals)
        return {
            "kind": understanding.kind,
            "reply": reply,
            "session_id": session_id,
            "notes": notes,
            "unresolved": list(understanding.unresolved),
            # Always false from this route. Running is a separate press on a session,
            # so a sentence — heard or typed — can never set a machine going.
            "ran": False,
        }

    @app.get("/voiceos/conversation")
    def voiceos_conversation(request: Request) -> dict[str, Any]:
        actor = _voiceos_actor(request, {})
        # The operator view names its actor in the query; a credential does not need to.
        return _voiceos_store().get_conversation(actor).to_dict()

    @app.get("/voiceos/sessions")
    def voiceos_sessions(request: Request, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
        rows = _voiceos_store().list_sessions(_record_scope(request), limit=limit)
        return [
            {
                "session_id": row.session_id,
                "actor_id": row.actor_id,
                "request": row.request,
                "status": row.status,
                "steps": len(row.steps),
                "settled": sum(1 for step in row.steps if step.settled),
                "updated_at": row.updated_at,
            }
            for row in rows
        ]

    @app.get("/voiceos/sessions/{session_id}")
    def voiceos_session(request: Request, session_id: str) -> dict[str, Any]:
        session = _voiceos_load(session_id, _record_scope(request))
        # Fold any live step from its execution record first. This reads only -- it
        # cannot start anything -- but it is what stops a cancelled run from being
        # shown as still awaiting approval.
        session = app.state.voiceos_orchestrator.fold_records(session)
        _voiceos_store().save_session(session)
        out = session.to_dict()
        out["description"] = describe_session(session)
        return out

    @app.post("/voiceos/sessions/{session_id}/run")
    async def voiceos_session_run(
        request: Request, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Advance a session, one authorised step at a time. This is the press."""
        credential = getattr(request.state, "credential", None)
        _throttle("voiceos", _credential_key(request))
        session = _voiceos_load(session_id, _record_scope(request))
        context = _context(
            payload,
            app.state.consent_store,
            credential=credential,
            credentials=app.state.credentials,
        )
        outcome = await app.state.voiceos_orchestrator.advance(session, context)
        updated: TaskSession = outcome["session"]
        store: VoiceOsStore = _voiceos_store()
        store.save_session(updated)
        conversation = store.get_conversation(updated.actor_id)
        conversation.active_session_id = updated.session_id
        if outcome.get("references"):
            # Remember what was *observed*, so the next turn can say "the second one".
            conversation.references = list(outcome["references"])
        conversation.remember("voiceos", updated.summary or describe_session(updated))
        store.save_conversation(conversation)
        return updated.to_dict()

    @app.post("/voiceos/sessions/{session_id}/cancel")
    def voiceos_session_cancel(request: Request, session_id: str) -> dict[str, Any]:
        _throttle("cancel", _credential_key(request))
        session = _voiceos_load(session_id, _record_scope(request))
        execution_id = app.state.voiceos_orchestrator.request_cancel(session)
        if not execution_id:
            return {
                "status": "nothing_running",
                "execution_id": "",
                "detail": "no step of this session is live, so there is nothing to stop",
            }
        # Requested, not confirmed. Whether it stopped is answered by the record on the
        # next read, exactly as the cancel route itself promises.
        return {"status": "requested", "execution_id": execution_id}

    return app


app = create_app()

__all__ = ["app", "create_app"]