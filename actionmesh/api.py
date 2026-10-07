"""C8 REST API around the trusted ActionMesh core boundaries."""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from actionmesh.core.audit import AuditLog, FileAuditAnchor, SqliteAuditBackend
from actionmesh.core.executor import ExecutionEngine
from actionmesh.core.graph.errors import GraphError
from actionmesh.core.graph.serialization import graph_from_dict
from actionmesh.core.permissions import Consent, ConsentStore, PolicyContext, PolicyEngine
from actionmesh.core.planner import Planner, RuleBasedPlanner
from actionmesh.core.registry import ActionRegistry
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
    Policy,
    PolicyDecision,
    PolicyRule,
    RiskLevel,
)
from actionmesh.protocol.validate import validate_action, validate_identity
from actionmesh.providers import default_mock_providers


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


def _context(payload: dict[str, Any], consent_store: ConsentStore) -> PolicyContext:
    from actionmesh.core.permissions.context import build_context

    if "grants" in payload:
        raise ValueError("permission grants are server-side; use /consents")
    actor = validate_identity(payload.get("actor"))
    if actor is None:
        return build_context(None, None, None, policy=_policy(payload.get("policy")))
    return build_context(
        None,
        None,
        actor,
        grants=consent_store.grants_for(actor.id),
        policy=_policy(payload.get("policy")),
    )


def create_app(
    engine: ExecutionEngine | None = None,
    consent_store: ConsentStore | None = None,
    audit: AuditLog | None = None,
    planner: Planner | None = None,
) -> FastAPI:
    """Create an API instance with injectable state for tests and deployments.

    Every event the engine publishes is sealed into ``app.state.audit``, so the
    service audits itself by default rather than only when someone remembers to
    opt in. Set ``ACTIONMESH_AUDIT_DB`` to persist the chain to SQLite; a
    persisted chain that does not verify **stops startup** instead of being
    silently inherited.
    """
    if engine is None:
        registry = ActionRegistry.with_seeds()
        engine = ExecutionEngine(registry, PolicyEngine(), default_mock_providers())
    if audit is None:
        audit_path = os.getenv("ACTIONMESH_AUDIT_DB", "").strip()
        if audit_path:
            # AuditLog.open() verifies the restored chain and refuses to start on
            # tampered history. The backend is closed on that failure path, so a
            # rejected store does not stay locked.
            #
            # ACTIONMESH_AUDIT_ANCHOR, when set, adds truncation detection: a
            # hash chain cannot notice that its own tail was deleted. It must
            # point somewhere the audit database cannot rewrite, or it buys
            # nothing.
            anchor_path = os.getenv("ACTIONMESH_AUDIT_ANCHOR", "").strip()
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
    app = FastAPI(title="ActionMesh API", version="AMP/1.0")
    app.state.engine = engine
    app.state.audit = audit
    #: Created here when ACTIONMESH_AUDIT_DB is set, so a shutdown handler or a
    #: test can release it. Ownership stays with whoever created it.
    app.state.audit_backend = audit_backend
    app.state.consent_store = consent_store if consent_store is not None else ConsentStore()
    #: Intent -> untrusted candidate plan. The default planner is deterministic
    #: and offline, exactly like the default mock providers.
    app.state.planner = planner if planner is not None else RuleBasedPlanner()
    configured_key = os.getenv("ACTIONMESH_API_KEY", "")
    try:
        max_request_bytes = int(os.getenv("ACTIONMESH_MAX_REQUEST_BYTES", "1048576"))
    except ValueError as exc:
        raise ValueError("ACTIONMESH_MAX_REQUEST_BYTES must be an integer") from exc
    if max_request_bytes < 1024:
        raise ValueError("ACTIONMESH_MAX_REQUEST_BYTES must be at least 1024")

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

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        return page()

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
    async def execute(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            graph = _validated_graph(payload.get("graph"))
            context = _context(payload, app.state.consent_store)
            result = await engine.execute(graph, context)
        except (GraphError, TypeError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.to_dict()

    @app.get("/executions/{execution_id}")
    def execution(execution_id: str) -> dict[str, Any]:
        record = engine.record(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return record.model_dump()

    @app.get("/executions")
    def executions(limit: int = 25) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
        return [record.model_dump() for record in engine.records()[:limit]]

    @app.get("/events")
    async def events(execution_id: str | None = None) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            async for event in engine.events.subscribe(execution_id):
                yield f"event: {event.event_type}\ndata: {event.model_dump_json()}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket, execution_id: str | None = None) -> None:
        if configured_key and not hmac.compare_digest(
            websocket.headers.get("x-api-key", ""), configured_key
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            async for event in engine.events.subscribe(execution_id):
                await websocket.send_json(event.model_dump())
        except WebSocketDisconnect:
            return

    @app.post("/approvals")
    def approval(payload: dict[str, Any]) -> dict[str, Any]:
        action_id = payload.get("action_id")
        status = payload.get("status", "pending")
        if not isinstance(action_id, str) or not action_id.strip():
            raise HTTPException(status_code=400, detail="action_id is required")
        if status not in {"pending", "approved", "rejected", "expired"}:
            raise HTTPException(status_code=400, detail="invalid approval status")
        request = ApprovalRequest(
            action_id=action_id,
            action_type=str(payload.get("action_type", "")),
            summary=str(payload.get("summary", "")),
            status=status,
        )
        return engine.submit_approval(request).model_dump()

    @app.post("/consents")
    def consent(payload: dict[str, Any]) -> dict[str, Any]:
        actor_id = payload.get("actor_id")
        scope = payload.get("scope")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise HTTPException(status_code=400, detail="actor_id is required")
        if not isinstance(scope, str) or not scope.strip():
            raise HTTPException(status_code=400, detail="scope is required")
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
    def consents(actor_id: str) -> list[dict[str, Any]]:
        if not actor_id.strip():
            raise HTTPException(status_code=400, detail="actor_id is required")
        return [
            record.model_dump()
            for record in app.state.consent_store.consents()
            if record.granted_by == actor_id
        ]

    @app.post("/executions/{execution_id}/cancel")
    def cancel(execution_id: str) -> dict[str, str]:
        if engine.record(execution_id) is None:
            raise HTTPException(status_code=404, detail="execution not found")
        engine.cancel(execution_id)
        return {"execution_id": execution_id, "status": "cancelled"}

    @app.get("/audit")
    def audit_trail(limit: int = 25) -> dict[str, Any]:
        if limit < 1 or limit > 1000:
            raise HTTPException(
                status_code=400, detail="limit must be between 1 and 1000"
            )
        chain = app.state.audit.verify()
        return {
            "chain": {
                "ok": chain.ok,
                "records": chain.records,
                "broken_at": chain.broken_at,
                "problem": chain.problem,
            },
            "head": app.state.audit.head,
            "records": [r.model_dump() for r in app.state.audit.records[-limit:]],
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

    return app


app = create_app()

__all__ = ["app", "create_app"]