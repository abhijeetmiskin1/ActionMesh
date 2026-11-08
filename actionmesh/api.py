"""C8 REST API around the trusted ActionMesh core boundaries."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from actionmesh.agent.browser_bridge import browser_bridge_config_from_env
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
from actionmesh.core.devices import DeviceRegistry, SQLiteDeviceRegistry
from actionmesh.core.executor import ExecutionEngine
from actionmesh.core.graph.errors import GraphError
from actionmesh.core.graph.serialization import graph_from_dict
from actionmesh.core.identity import IdentityError, IdentityStore, SQLiteIdentityStore
from actionmesh.core.output import (
    MAX_DOCUMENT_BYTES,
    OUTPUT_EVENT_TYPE,
    WHOLE_DOCUMENT_STREAMS,
)
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
    model_bridge,
)
from actionmesh.voiceos.automation_run import Runner, schedule_next
from actionmesh.voiceos.automation_scheduler import Scheduler, summarize_run
from actionmesh.voiceos.automation_store import AutomationStore
from actionmesh.voiceos.diagnosis import unobserved
from actionmesh.voiceos.diagnosis_flow import (
    analysis_step,
    analyze,
    describe,
    last_log_step,
    log_excerpt,
)
from actionmesh.voiceos.llm import (
    BridgeError,
    BridgeUnavailable,
    bridge_config_from_env,
    build_analyst,
    build_catalogue,
    build_planner,
    build_provider,
    llm_config_from_env,
)
from actionmesh.voiceos.models import Conversation
from actionmesh.voiceos.reasoning import describe_session
from actionmesh.voiceos.targeting import TargetChoice, default_target
from actionmesh.voiceos.targeting import current as targeting_current
from actionmesh.voiceos.targeting import resolve as resolve_target
from actionmesh.voiceos.workflow_draft import draft as workflow_draft
from actionmesh.voiceos.workflow_draft import draft_to_body
from actionmesh.voiceos.workflows import (
    Workflow,
    WorkflowError,
    describe_workflow,
    new_workflow_id,
    validate_workflow,
)

#: Loopback addresses, and nothing else. A deployment behind a relay sets
#: ``ACTIONMESH_ALLOW_REMOTE_PAIRING`` deliberately rather than adding an address
#: here, because "which networks count as being in the room" is not something the
#: protocol should pretend to know.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1"})


_background_tasks: set[asyncio.Task[Any]] = set()
"""Retained so a fire-and-forget automation notification is not left to the
garbage collector mid-flight (RUF006): referenced until the done-callback
discards it."""


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
    # Startup reconciliation is a snapshot. A process killed hard leaves a lease that
    # has not expired yet, so the replacement declines to touch its runs *now* -- and
    # without a sweep nothing ever looks again, stranding them at ``executing`` for good.
    sweeper = getattr(app.state.engine, "run_lease_sweeper", None)
    sweep_task = asyncio.create_task(sweeper(stop)) if sweeper is not None else None
    # C39: the automation ticker, and the startup reconciliation that must precede it.
    # Both are no-ops until an automation exists, so the cost of wiring them
    # unconditionally is one dictionary lookup per event and one wake-up every 30s.
    scheduler_getter = getattr(app.state, "automation_scheduler", None)
    scheduler = scheduler_getter() if callable(scheduler_getter) else None
    if scheduler is not None:
        # Before the ticker runs: an occurrence a previous process claimed and never
        # started is recorded `missed`, and a stale `next_run_at` is re-armed. Doing
        # this *after* starting the loop would let the first tick claim what the
        # reconciliation was about to account for.
        app.state.startup_automation_missed = scheduler.reconcile()
        scheduler.start()
    try:
        yield
    finally:
        stop.set()
        # Stop the ticker before anything it could be writing to is closed. Its own
        # lock guarantees it is not mid-`execute` when this returns.
        if scheduler is not None:
            await scheduler.stop()
        if sweep_task is not None:
            await asyncio.gather(sweep_task, return_exceptions=True)
        if task is not None:
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
        automation_store = getattr(app.state, "automation_store_instance", None)
        if automation_store is not None:
            automation_store.close()
        execution_store = getattr(app.state, "execution_store", None)
        if execution_store is not None:
            execution_store.close()
        # C36: durable device plane, closed only if this app opened it. An injected
        # store is the caller's, and closing it here would be this process reaching
        # into something it does not own.
        if getattr(app.state, "devices_owned", False):
            close_devices = getattr(app.state.devices, "close", None)
            if close_devices is not None:
                close_devices()
        if getattr(app.state, "identities_owned", False):
            close_identities = getattr(app.state.identities, "close", None)
            if close_identities is not None:
                close_identities()
        # C37: the consent ledger is a SQLite connection when a durable location is
        # configured. Releasing it here is what lets a replacement process open the
        # same file immediately, instead of waiting for the dying process to be reaped.
        if getattr(app.state, "consents_owned", False):
            close_consents = getattr(app.state.consent_store, "close", None)
            if close_consents is not None:
                close_consents()


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
        "app_version": device.app_version,
        "arch": device.arch,
        "capabilities": list(device.capabilities),
        "online": registry.is_online(device),
        # C36: the heartbeat the dashboard reads as "last activity", and the
        # connection time kept apart from it so "it came back" is visible.
        "last_seen": device.last_seen.isoformat() if device.last_seen else None,
        "last_connected_at": (
            device.last_connected_at.isoformat() if device.last_connected_at else None
        ),
        "registered_at": (
            device.registered_at.isoformat() if device.registered_at else None
        ),
        "risk_ceiling": device.risk_ceiling.value,
        "enrolled": bool(device.identity_id),
    }


#: A single serialized event may not exceed this. Provider payloads already pass
#: through the safe result view, so this is a backstop against a future publisher
#: that forgets to bound itself -- a noisy actor must not be able to grow a phone's
#: buffer or the socket it reads from.
MAX_EVENT_BYTES = 16384

#: Bounds for ``POST /voiceos/guide``. The route answers a question *about* the
#: command list the page already shows, so it takes that list rather than keeping a
#: second copy of it here: one list, one source, nothing to drift.
GUIDE_MAX_PHRASES = 40
GUIDE_MAX_PHRASE_CHARS = 200


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
    """Serialize one event, refusing rather than truncating what does not fit.

    Most events are small and the cap below is a bound on a hostile or runaway
    payload. Whole-document output streams are the exception, and they are the reason
    this is not a plain ``if len > MAX: replace``: an element list is one JSON array,
    and a *cut* array does not parse, so a reader would draw an empty list beside a
    non-zero count -- the panel saying the tree held nothing when the wire simply
    chopped it. Those streams are delivered entire or replaced whole, exactly as
    :mod:`actionmesh.core.output` frames them, under the document budget that module
    owns. The two layers have to agree; a framing rule enforced at one layer only is
    enforced nowhere.

    The replacement keeps the identity fields and the stream name, so a client can
    still correlate the event and say *which* observation was refused instead of
    guessing from a payload-less event.
    """
    text = event.model_dump_json()
    stream = event.payload.get("stream") if isinstance(event.payload, dict) else None
    framing = (
        MAX_DOCUMENT_BYTES if stream in WHOLE_DOCUMENT_STREAMS else MAX_EVENT_BYTES
    )
    if len(text.encode("utf-8")) <= framing:
        return text
    replacement: dict[str, Any] = {
        "truncated": True,
        "reason": "event exceeded the size limit",
    }
    if event.event_type == OUTPUT_EVENT_TYPE:
        # Keep the shape ``execution.output`` consumers already expect, so the one
        # rendered by the client is the whole-document refusal path and not a stray
        # line in the program-output console.
        replacement = {
            "stream": stream,
            "node_id": event.payload.get("node_id"),
            "device_id": event.payload.get("device_id"),
            "sequence": event.payload.get("sequence"),
            "chunk": "",
            "truncated": True,
            "final": bool(stream in WHOLE_DOCUMENT_STREAMS),
        }
        if event.payload.get("reason"):
            replacement["reason"] = event.payload["reason"]
    safe = MeshEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        timestamp=event.timestamp,
        execution_id=event.execution_id,
        action_id=event.action_id,
        actor=event.actor,
        payload=replacement,
    )
    return safe.model_dump_json()


def _build_consent_store() -> ConsentStore:
    """The consent ledger this process is allowed to keep.

    Consents are the user's standing decisions, and a decision that evaporates when
    the app restarts is not a decision -- it is a question the product asks again
    every launch with no way for the user to answer it. Where a durable VoiceOS store
    is configured the ledger goes beside it; without one (tests, a bare development
    process) it stays in memory, which is the honest default for a process that has
    nowhere to write.
    """
    from pathlib import Path

    from actionmesh.core.permissions import SQLiteConsentStore

    voiceos_db = os.getenv("ACTIONMESH_VOICEOS_DB", "").strip()
    if not voiceos_db:
        audit_db = os.getenv("ACTIONMESH_AUDIT_DB", "").strip()
        if audit_db:
            voiceos_db = str(Path(audit_db).with_name("voiceos.db"))
    if not voiceos_db:
        return ConsentStore()
    path = Path(voiceos_db).with_name("consents.db")
    try:
        return SQLiteConsentStore(path)
    except (OSError, sqlite3.Error):
        # An unwritable consent file must not turn into "every action is allowed"
        # and must not stop the app from starting: fall back to the in-memory ledger,
        # which fails closed exactly as before.
        return ConsentStore()


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
    engine_was_injected = engine is not None
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
    app.state.consent_store = consent_store if consent_store is not None else _build_consent_store()
    #: Whether this app opened the consent ledger. An injected one belongs to the
    #: caller, exactly like an injected engine, and is not closed here.
    app.state.consents_owned = consent_store is None
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
    active_voice_runs: set[str] = set()
    #: C39 — automations. A sixth store family, beside the VoiceOS one and opened the
    #: same lazy way: a process that never touches an automation should not be holding
    #: a database handle. What it stores is *intent* (a name, a plan, a trigger) plus
    #: the record of what each occurrence actually did. It carries no grants field, so
    #: "an automation confers no permission" is a property of the shape, not a rule
    #: someone has to remember.
    automations_db = os.getenv("ACTIONMESH_AUTOMATIONS_DB", "").strip()
    if not automations_db and app_config.audit_db:
        automations_db = str(Path(str(app_config.audit_db)).with_name("automations.db"))

    def _automation_store() -> AutomationStore:
        opened = getattr(app.state, "automation_store_instance", None)
        if opened is None:
            try:
                opened = AutomationStore(automations_db or ":memory:")
            except sqlite3.Error as exc:
                # An unwritable automation file must not stop the service. Refusing to
                # start would take the whole product down over a feature nobody used
                # yet; falling back to memory means automations do not survive a
                # restart, which the store's own `mode` says out loud.
                app.state.automation_store_note = (
                    f"automations are not durable: {automations_db} could not be "
                    f"opened ({exc}); this process keeps them in memory only"
                )
                opened = AutomationStore(":memory:")
            else:
                app.state.automation_store_note = (
                    "" if automations_db else
                    "automations are not durable: no database path is configured, "
                    "so this process keeps them in memory only"
                )
            app.state.automation_store_instance = opened
        return opened

    app.state.automation_store_note = ""

    def _automation_runner() -> Runner:
        runner = getattr(app.state, "automation_runner_instance", None)
        if runner is None:
            runner = Runner(
                orchestrator=app.state.voiceos_orchestrator,
                save_session=_voiceos_store().save_session,
            )
            app.state.automation_runner_instance = runner
        return runner

    def _automation_scheduler() -> Scheduler:
        scheduler = getattr(app.state, "automation_scheduler_instance", None)
        if scheduler is None:
            scheduler = Scheduler(store=_automation_store(), runner=_automation_runner())
            app.state.automation_scheduler_instance = scheduler
        return scheduler

    app.state.automation_scheduler = _automation_scheduler

    def _automation_listener(event: object) -> None:
        """Turn a published outcome into an automation trigger, if one is waiting.

        Registered on the hub once, at startup. It runs inside whichever task called
        ``publish`` -- the executor, usually -- so it must not block: the actual work
        is a task of its own, and the event's own id is the run slot, so a redelivery
        or a second listen cannot start the run twice.
        """
        from actionmesh.voiceos.automation_scheduler import TRIGGERABLE_EVENTS

        event_type = str(getattr(event, "event_type", ""))
        if event_type not in TRIGGERABLE_EVENTS:
            return
        payload = getattr(event, "payload", None)
        state = str(payload.get("state", "")) if isinstance(payload, dict) else ""
        actor = str(getattr(event, "actor", "") or "")
        if not actor or actor == "system":
            # An event nobody is attributed for cannot own a workflow. Without this the
            # only workflows it could match are those of an actor literally named
            # "system", which nobody registered.
            return
        loop = asyncio.get_running_loop()
        event_id = str(getattr(event, "event_id", "") or "")
        timestamp = getattr(event, "timestamp", None)
        occurred_at = (
            timestamp.isoformat()
            if isinstance(timestamp, datetime)
            else ""
        )

        async def _fire() -> None:
            with suppress(Exception):
                await _automation_scheduler().on_event(
                    event_type,
                    event_id=event_id,
                    actor=actor,
                    state=state,
                    occurred_at=occurred_at,
                )

        task = loop.create_task(_fire(), name=f"automation-event:{event_id}")
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    app.state.automation_listener = _automation_listener
    hub = getattr(engine, "events", None)
    add_listener = getattr(hub, "add_listener", None)
    if callable(add_listener):
        add_listener(_automation_listener)
    # C19 device plane. The identity store is the authority on *which machine* an
    # action ran on; the registry is presence. They are wired to the same store the
    # agent and pairing flows use, so an identity minted here is the one a credential
    # later binds to — not a second, disagreeing copy.
    #
    # C36: "identities survive restarts" is a property of the *deployment*, so the
    # durable stores are opened here whenever the process has a durable root to put
    # them beside, exactly as the VoiceOS store does. Development mode keeps the
    # in-memory pair and pays nothing. A caller that injected either store keeps
    # ownership of it — including the obligation to restore its own devices.
    app.state.identities_owned = False
    app.state.devices_owned = False
    if identities is not None:
        app.state.identities = identities
    elif app_config.audit_db:
        app.state.identities = SQLiteIdentityStore(
            str(Path(str(app_config.audit_db)).with_name("identities.db"))
        )
        app.state.identities_owned = True
    else:
        app.state.identities = IdentityStore()
        app.state.identities_owned = True
    if devices is not None:
        app.state.devices = devices
    elif app_config.audit_db:
        app.state.devices = SQLiteDeviceRegistry(
            str(Path(str(app_config.audit_db)).with_name("devices.db")),
            identities=app.state.identities,
            known_capability=engine.registry.has,
        )
        app.state.devices_owned = True
    else:
        app.state.devices = DeviceRegistry(
            identities=app.state.identities,
            known_capability=engine.registry.has,
        )
        app.state.devices_owned = True
    # C36 — the engine routes through this registry, whether it was injected or built
    # here. A graph addressed to a computer is only honour-able once the executor can
    # resolve that computer, and attaching it after the fact would leave a window in
    # which a named target was silently run locally.
    if not engine_was_injected:
        engine.devices = app.state.devices
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
                    # C36: which registered computer this adapter *is*. Without this a
                    # graph addressed to this machine cannot be told apart from one
                    # addressed to a Windows box, and both would run here.
                    device_id=agent.config.device_id,
                    # C42: the browser drive socket main wrote at spawn, when the
                    # service was started by the Electron shell. Environment only:
                    # the socket path and a per-launch token, never a credential.
                    # A deployment started any other way has neither variable
                    # set, the config is unconfigured, and the browser answers
                    # ``unavailable`` honestly rather than dialling a dead path.
                    browser_bridge=browser_bridge_config_from_env(),
                ),
            )
            register_device_strategies(engine.verifier)
        # The machine this process runs on, for the same reason: routing needs to know
        # which resolved device the in-process providers serve.
        if not engine_was_injected:
            engine.local_device_id = agent.config.device_id
        agent.register()
        agent.heartbeat()
        app.state.agent = agent
        # Phase 2 Agent 3: wire the forget-cascade. DeviceRegistry.forget() now
        # clears the agent-side cached presence record so a forgotten device
        # cannot resurrect via a stale heartbeat (Agent 4 gap 1). The registry
        # was built before the agent existed, so the wiring is post-construction.
        if getattr(app.state.devices, "presence", None) is None:
            app.state.devices.presence = agent
    else:
        app.state.agent = None
    # The rule-based planner proposes only what this deployment can actually carry
    # out. A rule can name a phrasing whose action this build never registered, and
    # nothing downstream would notice until dispatch -- long after the user pressed
    # Run. The "what is my laptop running" rules are the ones that matters for: the
    # device actions exist only while an agent advertises them, so on a service with
    # no agent the sentence matches a capability nobody opened.
    #
    # The decline has to be *said*, though. A planner that silently drops the clause
    # leaves an empty plan, and the user gets the generic "I could not map that" --
    # which reads as a parsing failure when the real answer is "this machine has no
    # agent, so it cannot see its own processes". So the filter reports what it
    # dropped and why, and the reply layer can repeat it.
    #
    # It runs after the agent block on purpose: the registry is only final once
    # ``register_device_actions`` has seen the advertised capabilities. Only the
    # default planner is rebuilt; a caller that injected its own keeps whatever
    # filter it chose, and the engine still checks its contract.
    # Rebuilt rather than mutated so the filter is fixed at construction and nothing
    # that captured the earlier instance is left holding an unfiltered one.
    if planner is None:
        app.state.planner = RuleBasedPlanner(known_action=engine.registry.has)

    #: C37 — the reasoning provider, resolved once at startup.
    #:
    #: The provider *object* is kept, not just its reason for failing: before C37
    #: only the note was retained, so even a correctly configured model was never
    #: consulted. The second element is user-visible text and empty when the model
    #: is live, so a note on someone's screen now means what it says.
    #: C41 — the desktop bridge, when this service was started by the Electron shell.
    #:
    #: The shell holds the API key in the OS keyring and makes the outbound call; this
    #: process gets a socket path and a per-launch token, and the token authorises
    #: "talk to that socket" — never "act as that provider". A deployment started any
    #: other way (the CLI, a test, a bare uvicorn) has neither variable set, so the
    #: bridge config is `None` and the planner behaves exactly as it did before C41.
    bridge_config = bridge_config_from_env()

    voiceos_llm_config = llm_config_from_env()
    voiceos_provider, app.state.voiceos_llm_note = build_provider(
        voiceos_llm_config,
        # Only a registered action's name and description cross the wire; see
        # `build_catalogue`. The registry is static after startup, so describing it
        # once is enough and keeps the per-request payload small.
        catalog=build_catalogue(engine.registry.list()),
        bridge=bridge_config,
    )
    # What `/voiceos/status` reports when it has no provider object to name.
    app.state.voiceos_llm_config = voiceos_llm_config
    app.state.voiceos_provider = voiceos_provider
    # The socket the desktop planner calls. Kept on state so the status route can ask it
    # what the shell actually has configured, rather than inferring from a construct.
    app.state.voiceos_bridge = bridge_config
    #: What the model may propose. A model-backed planner *wraps* the deterministic
    #: one rather than replacing it: every failure path answers from the rules and
    #: says so, so a request is never silently answered by a machine that stayed
    #: quiet. A deployment with no provider gets the rules alone, unchanged.
    voiceos_planner = build_planner(
        voiceos_provider, app.state.planner, engine.registry.has
    )
    app.state.voiceos_planner = voiceos_planner
    #: C36 — the analyst, the object that explains a log the system actually read.
    #:
    #: A separate provider from the planner on purpose: they share a transport and a
    #: credential, but a plan is a *proposal* the pipeline validates and a diagnosis is
    #: *prose about evidence*. Keeping them separable means a deployment can have one,
    #: the other, or neither without either silently standing in for the other.
    voiceos_analyst, app.state.voiceos_analyst_note = build_analyst(
        llm_config_from_env(), bridge=bridge_config
    )
    app.state.voiceos_analyst = voiceos_analyst
    app.state.voiceos_template_refusals = ()

    def _voiceos_reasoner(device_id: str = "") -> Reasoner:
        # Templates are re-read per turn on purpose: an operator editing their own
        # task file should see the next request use it, and a refused entry has to
        # surface rather than sit in a stale cache.
        source = load_templates(os.getenv("ACTIONMESH_VOICEOS_TASKS", "").strip() or None, engine.registry.has)
        app.state.voiceos_template_refusals = source.refusals
        return Reasoner(
            # C37: the model-backed planner when one is live, the rule-based planner
            # otherwise. Either way the plan is a proposal; this argument changes who
            # writes it, never what may be done with it.
            planner=app.state.voiceos_planner,
            known_action=engine.registry.has,
            templates=lambda: source.templates,
            # The selected computer is per-conversation, so it is passed in rather
            # than captured: a reasoner built once at startup would keep aiming at
            # whichever machine happened to be first.
            device_id=device_id,
            resolve=lambda name: resolve_target(app.state.devices, name, phrases=True),
            local_device_id=_voiceos_local_device_id,
        )

    app.state.voiceos_reasoner = _voiceos_reasoner
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
        def record_credential_event(
            event: str, subject: str, payload: dict[str, object]
        ) -> None:
            """Chain a credential lifecycle event. Same sink shape as pairing."""
            audit.append(
                kind="credential",
                actor="credentials",
                subject=subject,
                result=event,
                payload=payload,
            )

        credentials = CredentialStore(
            app.state.identities,
            key=app.state.server_key.material,
            consents=app.state.consent_store.grants_for,
            audit=record_credential_event,
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
        # Phase 4 Agent 3 — credential cascade on forget: the registry revokes
        # live credentials when a device is forgotten, not just identity/presence.
        # Post-construction assignment (registry is built before credentials);
        # defensive so a registry without the slot can never break pairing.
        with suppress(Exception):
            app.state.devices.credentials = credentials
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
                # Which machine this service serves, when one declared itself. The
                # desktop shell addresses its screen requests by this id rather than
                # guessing a hostname: an execution naming a device nobody enrolled is
                # refused before policy runs, and a hardcoded id is wrong the moment an
                # operator names their laptop something else. Empty on a control plane
                # with no agent attached, which is an honest "I do not know".
                "local_device_id": _voiceos_local_device_id(),
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

    @app.delete("/consents/{scope}")
    def revoke_consent(request: Request, scope: str, actor_id: str | None = None) -> dict[str, Any]:
        """Withdraw a standing consent.

        Revoking is the *safe* direction, so it is deliberately not gated the way
        granting is: there is no authority to gain by removing a permission. The
        actor is still named -- a device withdraws only its own, and an operator
        says whose -- so one client cannot strip another's ability to work.
        """
        caller = _record_scope(request)
        if caller is None:
            if not actor_id or not actor_id.strip():
                raise HTTPException(status_code=400, detail="actor_id is required")
            wanted = actor_id.strip()
        else:
            if actor_id and actor_id.strip() and actor_id.strip() != caller:
                raise HTTPException(
                    status_code=403, detail="a device may only revoke its own consent"
                )
            wanted = caller
        store = app.state.consent_store
        owned = [record for record in store.consents(scope) if record.granted_by == wanted]
        if not owned:
            raise HTTPException(status_code=404, detail="no such consent for that actor")
        # The actor is named on the removal itself: a scope two actors happen to hold
        # must not lose the other's grant when this one withdraws.
        removed = store.revoke(scope, granted_by=wanted)
        return {"scope": scope, "revoked": removed}

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

    def _voiceos_local_device_id() -> str:
        """The machine this control service actually serves.

        Only a *declared* id counts — the engine's own, or the one a real agent
        adapter carries. A device record that merely resembles this host is not
        proof, so a service with no agent attached returns ``""`` and says so.
        """
        engine = app.state.engine
        declared = str(getattr(engine, "local_device_id", "") or "")
        if declared:
            return declared
        for provider in getattr(engine, "providers", ()):
            device_id = str(getattr(provider, "device_id", "") or "")
            if device_id:
                return device_id
        return ""

    def _voiceos_default_target() -> TargetChoice:
        return default_target(app.state.devices, _voiceos_local_device_id())

    def _voiceos_selected_target(conversation: Conversation) -> TargetChoice:
        """The conversation's selected computer, re-validated from the registry.

        Re-validated, not remembered: a device revoked since it was selected must
        not keep being named as the target of new work.
        """
        if not conversation.device_id:
            return _voiceos_default_target()
        choice = targeting_current(app.state.devices, conversation.device_id)
        if choice.resolved:
            return choice
        # A stale selection is reported, not silently replaced by the default.
        return TargetChoice(reason=choice.reason)

    def _voiceos_remember_target(conversation: Conversation, target: str) -> None:
        conversation.device_id = target
        for step in (conversation.active_session.steps if conversation.active_session else ()):
            if not step.device_id:
                step.device_id = target

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

    def _diagnosis_body(
        result: Any, *, computer: str = "", step: Any = None
    ) -> dict[str, Any]:
        """The diagnosis response: the facts first, the prose second.

        ``analysis`` is the machine-readable record (evidence, status, the model that
        spoke and what was redacted); ``description`` is the one sentence a person
        reads. A client that renders only the sentence still gets the evidence in it,
        because :func:`describe` always leads with what was read.
        """
        out = result.to_dict()
        out["description"] = describe(result, computer=computer)
        out["computer"] = computer
        # Which authorised read this came from — the useful half of "where did this
        # come from?" — and never the log itself, which is not in this response or the
        # store at all.
        out["source"] = {
            "step": step.index if step is not None else None,
            "label": step.label if step is not None else "",
            "execution_id": step.execution_id if step is not None else "",
            "process_id": str((step.parameters or {}).get("process_id") or "")
            if step is not None
            else "",
        }
        # Why the answer is what it is when it was not a diagnosis: "no credential is
        # configured" and "the model's answer was refused" are different problems, and
        # a user fixing one must not be sent to look at the other.
        if result.status == "diagnosed":
            out["analyst"] = ""
        else:
            out["analyst"] = " ".join(
                part
                for part in (str(app.state.voiceos_analyst_note or ""), *result.notes)
                if part
            )
        return out

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
        # C36: which computer this turn is about -- the conversation's own selection,
        # or the default for a fresh one. The default is the machine this service
        # serves or the single online computer; it is never a guess between several.
        # A selection that no longer resolves keeps its own reason and is *not*
        # replaced by the default: that substitution is the silent re-targeting the
        # work order forbids, and the user can see the reason and pick again.
        target = _voiceos_selected_target(conversation)
        reasoner = app.state.voiceos_reasoner(target.device_id)
        # C37: `understand` calls the planner, which may make one blocking HTTPS call
        # to a model. On this event loop that would stall every other request in the
        # process for up to the configured timeout, so the whole turn runs in a worker
        # thread. `plan` refuses to dial on a loop thread and falls back with a note,
        # which means this offload is the difference between a working model and a
        # stated non-answer rather than a silent stall.
        understanding = await asyncio.to_thread(reasoner.understand, text, conversation)
        reply = understanding.reply
        if understanding.kind == "clear":
            store.clear_conversation(actor)
            # Clearing forgets the conversation, not the computer the user selected:
            # a control surface that silently re-aimed itself would be a target change
            # nobody asked for.
            kept = conversation.device_id if target.resolved else ""
            conversation = Conversation(actor_id=actor, device_id=kept)
        else:
            conversation.remember("user", text)
            conversation.remember("voiceos", reply)
        if understanding.kind == "device" and understanding.target is not None:
            # A switch is the one turn whose *own* result is the new selection, so the
            # response reports it rather than what the turn was addressed to. The
            # reasoner already resolved and validated this id; writing it here makes it
            # the conversation's -- not a grant, just where future work is addressed.
            _voiceos_remember_target(conversation, understanding.target.device_id)
            target = understanding.target
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
        if understanding.kind == "task" and not target.resolved:
            # Nothing is being aimed at a guessed machine. The plan exists, the run
            # will refuse it, and the sentence says both.
            notes.append(
                target.reason or "no computer is selected; pick one before running this plan"
            )
        return {
            "kind": understanding.kind,
            "reply": reply,
            "session_id": session_id,
            "notes": notes,
            "unresolved": list(understanding.unresolved),
            "target_device": target.device_id,
            "target_name": target.name,
            "target_online": target.online,
            # Always false from this route. Running is a separate press on a session,
            # so a sentence — heard or typed — can never set a machine going.
            "ran": False,
        }

    @app.post("/voiceos/guide")
    def voiceos_guide(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """Which of the page's example phrasings this deployment would actually route.

        The command guide used to carry a hand-written "supported" column, and it was
        wrong: on a service with no laptop agent, half the listed phrases matched no
        rule at all and the next turn came back "I could not map that". A label that
        is typed in is a claim; this route makes it a *measurement*.

        It runs each phrase through exactly the planner the conversation uses — the
        same ``app.state.planner``, with the same registry filter — and reports what
        happened to it. Nothing is proposed, nothing is authorized, nothing runs;
        ``plan`` is a pure function of intent text and the live rule table.

        The phrases come from the caller so there is one list in the product, not two
        that drift. They are untrusted text like any other body: bounded in count and
        length, and echoed back only as the same string that was sent.
        """
        _throttle("voiceos", _credential_key(request))
        raw = payload.get("phrases")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(status_code=400, detail="phrases must be a non-empty list")
        if len(raw) > GUIDE_MAX_PHRASES:
            raise HTTPException(
                status_code=400,
                detail=f"phrases must contain at most {GUIDE_MAX_PHRASES} entries",
            )
        phrases: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise HTTPException(status_code=400, detail="each phrase must be a non-empty string")
            if len(item) > GUIDE_MAX_PHRASE_CHARS:
                raise HTTPException(
                    status_code=400,
                    detail=f"each phrase must be at most {GUIDE_MAX_PHRASE_CHARS} characters",
                )
            phrases.append(item)

        results: list[dict[str, Any]] = []
        for phrase in phrases:
            plan = app.state.planner.plan(phrase)
            results.append({
                "phrase": phrase,
                # What the rule table proposes, and what survived the registry filter.
                # An empty list is the honest answer: no rule, or no capability.
                "actions": [action.type for action in plan.actions],
                "notes": list(plan.notes),
            })
        return {"phrases": results}

    # The shell's `state` is the only field that separates *configured* from *usable*.
    # A key the provider rejected still reads configured and implemented; without this
    # set the route would claim a model that every turn then refuses to be. Unknown
    # values are not errors: the shell has already folded unrecognised states into
    # `provider_unavailable`, so anything here is something the shell deliberately said
    # the call will still make.
    _PROVIDER_STATE_REFUSES = frozenset({
        "missing_key",
        "invalid_credentials",
        "rate_limited",
        "timeout",
        "provider_unavailable",
        "malformed",
    })

    @app.get("/voiceos/status")
    def voiceos_status(request: Request) -> dict[str, Any]:
        """Whether the next turn is answered by a model or by the deterministic planner.

        The distinction this route has to keep honest is *configured* versus *usable*.
        Under the desktop app the planner is always a ``desktop`` provider, because the
        Electron shell supplies a bridge whenever it can — but the user may have chosen
        no provider at all, or one whose credential was never entered. Constructing a
        provider object therefore proves nothing; the shell is asked, over the same
        authenticated socket, what it actually has. A call that cannot be verified is
        reported as the fallback, which is what the next turn will be.

        Even a verified provider is a claim about *configuration*: the per-turn note in
        ``/voiceos/messages`` is what proves a call went out and came back, and the UI
        appends those notes verbatim.
        """
        # Authentication is the process-wide middleware's job (API key, or a device
        # credential); this route adds nothing on top of it.
        note = str(getattr(app.state, "voiceos_llm_note", "") or "")
        provider = getattr(app.state, "voiceos_provider", None)
        config = getattr(app.state, "voiceos_llm_config", None)
        mode = "ai_model" if provider is not None else "deterministic"
        provider_name = str(
            getattr(provider, "name", "") or (config.provider if config else "")
        )
        model = str(getattr(provider, "model", "") or (config.model if config else ""))
        reason = "" if provider is not None else note
        # The shell's last verified connection state, when the shell is the provider.
        # Empty elsewhere: no bridge, nothing to verify against.
        provider_state = ""

        # The desktop provider's usability lives in the shell, not in this process.
        if provider is not None and provider_name == "desktop":
            bridge = getattr(app.state, "voiceos_bridge", None)
            verdict = {"usable": False, "detail": "the desktop model bridge did not answer"}
            if bridge is not None and getattr(bridge, "configured", False):
                try:
                    reply = model_bridge.status(bridge)
                except (BridgeError, BridgeUnavailable) as exc:
                    # A bridge that cannot answer is a fallback, and the reason is the
                    # one the client already wrote for a user to read.
                    verdict = {"usable": False, "detail": str(exc)}
                else:
                    state = str(reply.get("state", ""))
                    provider_state = state
                    # `configured` and `implemented` say the wiring exists; `state` says
                    # what the last contact with the provider produced. A key the
                    # provider rejected is still configured and implemented — only the
                    # state knows it will not answer.
                    ready = (
                        bool(reply.get("configured"))
                        and bool(reply.get("implemented"))
                        and state not in _PROVIDER_STATE_REFUSES
                    )
                    detail = "" if ready else (
                        "the desktop app has no usable provider configured"
                        if not reply.get("configured")
                        else "the configured provider cannot generate in this build"
                        if state in _PROVIDER_STATE_REFUSES
                        else ""
                    )
                    if state in _PROVIDER_STATE_REFUSES and not ready:
                        # The shell wrote this sentence for a user to read — it is the
                        # same table the header bar uses — so prefer it over the generic
                        # one above.
                        detail = str(reply.get("detail", "")) or detail
                    if ready and state not in ("ok", "connected"):
                        # A call will still go out — the mode is honest — but the
                        # connection has not been verified, and the chip's tooltip is
                        # where that caveat belongs.
                        detail = (
                            "the provider is configured but has not been verified; "
                            "press Test Connection in Settings"
                        )
                    verdict = {
                        "usable": ready,
                        "detail": detail,
                        "provider": str(reply.get("provider", "")),
                        "model": str(reply.get("model", "")),
                        "state": state,
                    }
            if verdict.get("usable"):
                provider_name = str(verdict.get("provider") or provider_name)
                model = str(verdict.get("model") or model)
            else:
                mode = "deterministic"
            # The detail is worth showing whether the mode changed or not: a refusing
            # state explains the fallback, an unverified-but-active one carries the
            # "press Test Connection" caveat into the chip's tooltip.
            reason = str(verdict.get("detail") or reason)

        return {
            "mode": mode,
            "mode_label": "AI model active" if mode == "ai_model" else "Deterministic fallback",
            "provider": provider_name,
            "model": model,
            # Only meaningful in deterministic mode: names what was asked for and why it
            # could not be used. Empty when nothing was configured at all.
            "reason": reason,
            # The shell's last verified connection state for the chosen provider, when
            # the shell is the provider. Safe metadata, not a credential.
            "state": provider_state,
        }

    @app.get("/voiceos/conversation")
    def voiceos_conversation(request: Request) -> dict[str, Any]:
        actor = _voiceos_actor(request, {})
        # The operator view names its actor in the query; a credential does not need to.
        conversation = _voiceos_store().get_conversation(actor)
        out = conversation.to_dict()
        # Return-only: the chip on the voice page reads this, and it is the same
        # resolution the next turn will use. It is not persisted here -- selection
        # is written by the turn that changes it.
        target = _voiceos_selected_target(conversation)
        out["target_device"] = target.device_id
        out["target_name"] = target.name
        out["target_online"] = target.online
        out["target_candidates"] = [
            {"device_id": device_id, "name": name} for device_id, name in target.candidates
        ]
        out["target_reason"] = target.reason
        # The default, separately: "you have selected nothing" and "this service
        # serves X" are different facts, and collapsing them would make a selected
        # device look like a default or the reverse.
        fallback = _voiceos_default_target()
        out["default_target"] = fallback.device_id
        out["default_target_name"] = fallback.name
        return out

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
                "automatic_approval_execution_id": next(
                    (step.execution_id for step in row.steps if step.state == "awaiting_approval"),
                    "",
                ) if row.automatic else "",
                "settled": sum(1 for step in row.steps if step.settled),
                "updated_at": row.updated_at,
            }
            for row in rows
        ]

    @app.get("/voiceos/sessions/{session_id}")
    async def voiceos_session(request: Request, session_id: str) -> dict[str, Any]:
        session = _voiceos_load(session_id, _record_scope(request))
        # Fold any live step from its execution record first. This reads only -- it
        # cannot start anything -- but it is what stops a cancelled run from being
        # shown as still awaiting approval.
        if session_id not in active_voice_runs:
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
        if session_id in active_voice_runs:
            raise HTTPException(status_code=409, detail="this task is already running")
        context = _context(
            payload,
            app.state.consent_store,
            credential=credential,
            credentials=app.state.credentials,
        )
        store: VoiceOsStore = _voiceos_store()
        active_voice_runs.add(session_id)
        try:
            # Write the name of each execution to the step *before* awaiting it. If this
            # request dies mid-step, the session on disk still points at the run, and the
            # next reader can ask the durable record what became of it instead of guessing.
            session.automatic = payload.get("stop_on_unverified") is True
            outcome = await app.state.voiceos_orchestrator.advance(
                session, context, checkpoint=store.save_session,
                stop_on_unverified=session.automatic,
            )
            updated: TaskSession = outcome["session"]
            store.save_session(updated)
            conversation = store.get_conversation(updated.actor_id)
            conversation.active_session_id = updated.session_id
            if outcome.get("references"):
                # Remember what was *observed*, so the next turn can say "the second one".
                conversation.references = list(outcome["references"])
            conversation.remember("voiceos", updated.summary or describe_session(updated))
            store.save_conversation(conversation)
            return updated.to_dict()
        finally:
            active_voice_runs.discard(session_id)

    @app.post("/voiceos/sessions/{session_id}/diagnose")
    async def voiceos_session_diagnose(
        request: Request, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Explain a log this session already read, through an ordinary read's output.

        Three things this route deliberately does **not** do:

        * It does not read the log. The log body only ever exists on the transient,
          process-local output channel of an authorised ``device.process.logs``
          execution, which this route draws from by that execution's id. There is no
          second path to a file here, and none is invented.
        * It does not spend an ActionMesh approval. Explaining evidence is not an
          action; there is nothing for the policy engine to authorise and nothing for
          a human to approve. What it *does* spend is a model call, so it is explicit
          and on its own route — the user pressing it is the consent to send the
          bounded excerpt.
        * It does not let the answer widen anything. The analysis is prose; the
          validator refuses an answer that asserts a risk, a permission or an approval,
          and nothing downstream treats the prose as a plan.
        """
        credential = getattr(request.state, "credential", None)
        _throttle("voiceos", _credential_key(request))
        # The execution's actor is the principal, computed exactly as the run route
        # computes it: a credential pins it, and the local operator is only local.
        # `_record_scope` would give `None` for the operator while the executions were
        # stamped with the actor named in the run body, so the transcript is read with
        # the two honest sources this route actually has.
        scope = _record_scope(request)
        session = _voiceos_load(session_id, scope)
        # The actor the executions were stamped with. A credential pins it; the local
        # operator is the session's own actor, not `None` — the transcript check in
        # `log_excerpt` is exact, and "the operator" is not a value any execution
        # carries.
        actor = (
            credential.actor_id
            if credential is not None
            else session.actor_id or "system"
        )
        step = last_log_step(session)
        if step is None:
            # Not an error: a session that has only proposed the work has nothing to
            # explain yet, and saying so is better than explaining an empty string.
            result = unobserved(
                "this session has not finished reading a log yet, so there is nothing "
                "to explain; run the plan first"
            )
            return _diagnosis_body(result, computer=session.device_id)
        excerpt, reason = log_excerpt(
            # The log body lives on the transient output channel, which `history()`
            # deliberately does not retain: it was never audited, persisted or replayed,
            # and asking for it by execution id draws only from the bounded in-memory
            # copy of that one execution.
            app.state.engine.events.transient(step.execution_id),
            execution_id=step.execution_id,
            actor=actor,
            device_id=step.device_id or session.device_id,
        )
        if reason:
            result = unobserved(reason)
            return _diagnosis_body(result, computer=session.device_id, step=step)
        # The question lives on the analysis step, not on the read: the read is just the
        # evidence, and the explanation is the separate thing asked for.
        asked = analysis_step(session)
        meta = asked.analysis if asked is not None else {}
        question = str(meta.get("question") or session.request or "").strip()
        computer = str(meta.get("computer") or session.device_id or "")
        # The analyst makes one blocking HTTPS call; this must not happen on the loop.
        result = await asyncio.to_thread(
            analyze,
            question=question,
            excerpt=excerpt,
            analyst=app.state.voiceos_analyst,
            analyst_note=str(app.state.voiceos_analyst_note or ""),
            computer=computer,
            source=step.label,
        )
        body = _diagnosis_body(result, computer=computer, step=step)
        if asked is not None and result.status == "diagnosed":
            # The analysis step counts as answered only when a *cause or fix* was
            # actually produced. An "unavailable" answer is not an explanation, and
            # recording it as one would make the session's status line claim a finding
            # the system never made.
            asked.analysis["result"] = str(body["description"])
            session.touch()
            _voiceos_store().save_session(session)
        # The spoken sentence is the answer; the record of it is remembered so the next
        # turn ("what was the error again?") is answered from what was said, not rerun.
        conversation = _voiceos_store().get_conversation(session.actor_id)
        conversation.remember("voiceos", body["description"])
        _voiceos_store().save_conversation(conversation)
        return body

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

    # -- automations (C39) -------------------------------------------------
    #
    # A saved automation is *intent*: a name, a plan, a trigger. Creating one grants
    # nothing, editing one grants nothing, and scheduling one grants nothing — there is
    # no field in the stored shape where a grant could live. Every run is a fresh
    # session through the same orchestrator, so every step is authorised against
    # whoever is running it, at the moment it runs.

    def _automation_load(request: Request, workflow_id: str) -> Workflow:
        """One workflow, scoped exactly like an execution record.

        ``None`` from ``_record_scope`` is the machine's own operator and sees every
        automation on it; a credential sees only the ones its actor owns. One answer
        for "never existed" and "exists, not yours", as everywhere else.
        """
        workflow = _automation_store().get_workflow(workflow_id)
        scope = _record_scope(request)
        if workflow is None or (scope is not None and workflow.actor_id != scope):
            raise HTTPException(status_code=404, detail="automation not found")
        return workflow

    def _automation_view(workflow: Workflow) -> dict[str, Any]:
        out = workflow.to_dict()
        # The description is prose *about* the stored plan, generated on read. It is
        # not stored, so an edited plan cannot keep saying what it used to do.
        try:
            names = {
                device.device_id: device.name
                for device in app.state.devices.all_devices()
            }
        except Exception:
            names = {}
        out["description_text"] = describe_workflow(workflow, devices=names)
        out["trigger_kind"] = workflow.trigger_kind()
        return out

    def _automation_save(workflow: Workflow) -> Workflow:
        schedule_next(workflow, now=None)
        workflow.touch()
        _automation_store().save_workflow(workflow)
        return workflow

    @app.post("/automations/draft")
    def automation_draft(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """Read a sentence into a *draft*. **Nothing is stored and nothing runs.**

        The steps can only come from an operator-authored template; a sentence with no
        matching template gets a trigger and an empty plan, plus the reason. A model is
        never asked to write a step here — see ``workflow_draft`` for why.
        """
        _throttle("voiceos", _credential_key(request))
        sentence = payload.get("text")
        if not isinstance(sentence, str):
            raise HTTPException(status_code=400, detail="text must be a string")
        actor = _voiceos_actor(request, payload)
        source = load_templates(
            os.getenv("ACTIONMESH_VOICEOS_TASKS", "").strip() or None,
            app.state.engine.registry.has,
        )
        proposed = workflow_draft(sentence, templates=source.templates, actor_id=actor)
        out = proposed.to_dict()
        out["refused_templates"] = list(source.refusals)
        # The exact body a save would accept, so the review screen shows what would be
        # written rather than a summary that could differ from it.
        out["proposal"] = draft_to_body(proposed)
        return out

    @app.get("/automations")
    def automations_list(request: Request, limit: int = 100) -> dict[str, Any]:
        """The library. Scoped to the caller, with the honest durability note."""
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
        scope = _record_scope(request)
        workflows = _automation_store().list_workflows(scope, limit=limit)
        scheduler = _automation_scheduler()
        return {
            "workflows": [_automation_view(workflow) for workflow in workflows],
            # An in-memory store means these do not survive a restart, and saying so
            # here is the only place the user would find out before it matters.
            "note": str(getattr(app.state, "automation_store_note", "") or ""),
            "scheduler": scheduler.status(),
            "missed_at_startup": len(
                getattr(app.state, "startup_automation_missed", ()) or ()
            ),
        }

    @app.get("/automations/status")
    def automations_status(request: Request) -> dict[str, Any]:
        """Whether the local ticker is alive, and what can trigger a run."""
        _record_scope(request)
        out = _automation_scheduler().status()
        out["note"] = str(getattr(app.state, "automation_store_note", "") or "")
        out["missed_cutoff"] = _automation_scheduler().missed_cutoff()
        return out

    @app.get("/automations/runs")
    def automations_runs(
        request: Request, limit: int = 50, workflow_id: str = ""
    ) -> dict[str, Any]:
        """Run history: what actually happened, including what was missed."""
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
        scope = _record_scope(request)
        runs = _automation_store().list_runs(
            actor_id=scope, workflow_id=workflow_id or None, limit=limit
        )
        return {
            "runs": [dict(run.to_dict(), summary=summarize_run(run)) for run in runs]
        }

    @app.post("/automations")
    def automations_create(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """Store a plan. It confers no permission and starts nothing."""
        _throttle("voiceos", _credential_key(request))
        actor = _voiceos_actor(request, payload)
        if "grants" in payload or "permissions" in payload:
            raise HTTPException(
                status_code=400,
                detail="an automation cannot carry permissions; consent is granted at /consents",
            )
        try:
            workflow = validate_workflow(
                payload,
                actor_id=actor,
                known_action=app.state.engine.registry.has,
                workflow_id=new_workflow_id(),
            )
        except WorkflowError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _automation_save(workflow)
        out = _automation_view(workflow)
        out["runs_nothing"] = (
            "saved. Nothing has run and no permission was granted; a run is "
            "authorised when it happens."
        )
        return out

    @app.get("/automations/{workflow_id}")
    def automations_get(request: Request, workflow_id: str) -> dict[str, Any]:
        _record_scope(request)
        return _automation_view(_automation_load(request, workflow_id))

    @app.put("/automations/{workflow_id}")
    def automations_edit(
        request: Request, workflow_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace a plan. The same validation a create goes through, no shortcuts."""
        _throttle("voiceos", _credential_key(request))
        existing = _automation_load(request, workflow_id)
        if "grants" in payload or "permissions" in payload:
            raise HTTPException(
                status_code=400,
                detail="an automation cannot carry permissions; consent is granted at /consents",
            )
        body = dict(payload)
        # The owner is the stored one, never the body's. An edit cannot transfer an
        # automation to another actor, which would be one actor arming another's run.
        body.pop("actor_id", None)
        body.pop("actor", None)
        try:
            workflow = validate_workflow(
                body,
                actor_id=existing.actor_id,
                known_action=app.state.engine.registry.has,
                workflow_id=workflow_id,
            )
        except WorkflowError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Creation time and the last run's outcome are facts about the past; an edit
        # does not rewrite them.
        workflow.created_at = existing.created_at
        workflow.last_run_at = existing.last_run_at
        workflow.last_run_id = existing.last_run_id
        workflow.last_state = existing.last_state
        _automation_save(workflow)
        return _automation_view(workflow)

    @app.post("/automations/{workflow_id}/enabled")
    def automations_enabled(
        request: Request, workflow_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Arm or disarm. Disarming stops *future* occurrences; a live run is not killed."""
        _throttle("voiceos", _credential_key(request))
        workflow = _automation_load(request, workflow_id)
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled must be true or false")
        workflow.enabled = enabled
        _automation_save(workflow)
        out = _automation_view(workflow)
        if not enabled:
            out["detail"] = (
                "disabled. No schedule or event will start it; a run already in "
                "progress is not stopped by this."
            )
        return out

    def _local_automation_target(request: Request, workflow: Workflow) -> str:
        """The computer a press that named none should be aimed at, or ``""``.

        The orchestrator refuses a step with no target once an execution device
        exists, which is correct: an unaimed step must not silently become a local
        one. But the operator pressing "run now" inside the desktop shell *is* on this
        laptop, and a plan saved without a target would otherwise be unrunnable from
        the one button that exists for running it.

        So exactly one reading is offered, and only where it cannot mean something
        else: a request that carries a credential came from a device, and a request on
        loopback came from the machine's own operator. Either way the press is local.

        The target itself is *not* re-derived here. It is the same
        ``default_target`` a VoiceOS conversation with no selection gets: the declared
        local device, else the single online execution device, else nothing — an
        offline or ambiguous set is never chosen, it is left unanswered. An ambiguous
        answer is reported in the run rather than resolved by picking one.

        Anything else gets ``""`` — unchanged behaviour, so a press from elsewhere
        with no target is still refused or reported rather than quietly aimed. A step
        that names its own computer is never reached here (the orchestrator prefers
        it), and a workflow that already names one is honoured as stored.
        """
        if workflow.device_id:
            return workflow.device_id
        credential = getattr(request.state, "credential", None)
        if credential is None and not _is_local(request):
            return ""
        return _voiceos_default_target().device_id

    @app.post("/automations/{workflow_id}/run")
    async def automations_run(
        request: Request, workflow_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Press run. This is a fresh session authorised against an actor, now.

        A previous approval does not carry over: the run builds its context from the
        credential (or the automation's owner) exactly as
        ``/voiceos/sessions/{id}/run`` does, so every step is decided again, now.
        """
        credential = getattr(request.state, "credential", None)
        _throttle("execute", _credential_key(request))
        workflow = _automation_load(request, workflow_id)
        # A bare press from the machine's own operator names nobody, and an execution
        # with no actor identity is denied by the policy engine before its grants are
        # even consulted -- every step would fail with "missing actor identity" and a
        # correctly-granted operator could never run their own automation. So the local
        # operator runs it *as the automation's owner*, which is the actor that owns
        # whatever consent exists. The owner is read from the stored workflow, never
        # from the body; a credential still pins the actor to itself, and a body that
        # names someone else is still refused by ``_context``.
        body = dict(payload)
        if credential is None and body.get("actor") is None and workflow.actor_id:
            body["actor"] = {"id": workflow.actor_id, "kind": "user"}
        try:
            context = _context(
                body,
                app.state.consent_store,
                credential=credential,
                credentials=app.state.credentials,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # The same reasoning one layer down: a plan whose steps name no computer has
        # no target, and the orchestrator refuses an unselected step once an execution
        # device exists -- which would make every saved plan unrunnable from this button.
        # The machine's own operator pressing "run now" in the desktop shell *is* on
        # the laptop, so "this laptop" is the honest reading of that press.
        #
        # That reading is only taken when it cannot mean something else: the request
        # carries a credential (so it comes from a device, not a person elsewhere), or
        # it arrives on loopback (so it is the local operator). A body that names a
        # computer still wins, a step that names one is never rewritten, and an
        # unselected press that is neither is left unselected -- it is then refused or
        # refused-and-reported exactly as before, rather than quietly aimed somewhere.
        default_target = _local_automation_target(request, workflow)
        run = await _automation_scheduler().run_now(
            workflow, context=context, default_device_id=default_target
        )
        return dict(run.to_dict(), summary=summarize_run(run))

    @app.delete("/automations/{workflow_id}")
    def automations_delete(request: Request, workflow_id: str) -> dict[str, Any]:
        """Remove a plan. Its run history stays: it is a record, not a child."""
        _throttle("voiceos", _credential_key(request))
        _automation_load(request, workflow_id)
        deleted = _automation_store().delete_workflow(workflow_id)
        return {
            "deleted": deleted,
            "runs_kept": True,
            "detail": "the plan is gone; what it did in the past is still in the history",
        }

    return app


app = create_app()

__all__ = ["app", "create_app"]
