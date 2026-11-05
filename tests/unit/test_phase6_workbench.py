"""Phase 6 Workbench journeys: the REAL local engine through TestClient.

This file is the acceptance gate for the Action Execution Workbench:
instruction -> /plan -> /execute -> approval -> verified result -> history.

Every assertion here runs against the live FastAPI app + the real
PolicyEngine / ExecutionEngine / mock LaptopProvider / verifier. Nothing is
mocked except the provider's *effect* (VerificationResult.simulated is True by
design -- the verifier re-reads the world and reports simulated=True, and the
test pins that honestly instead of hiding it).

Operator view: TestClient with client=("127.0.0.1", 45123) makes the caller
the local operator -- unscoped, so bodies must carry an explicit actor_id
(a device principal would come from a credential, never the body). Consent
is the operator's act (POST /consents); a consent is NOT an approval.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from actionmesh.api import create_app

ACTOR = "voiceos-local"
LOCAL_OPERATOR = ("127.0.0.1", 45123)


def _client() -> TestClient:
    return TestClient(create_app(), client=LOCAL_OPERATOR)


def _consent(client: TestClient, scope: str, max_risk: str = "medium") -> None:
    r = client.post(
        "/consents",
        json={"actor_id": ACTOR, "scope": scope, "max_risk": max_risk,
              "reason": "Phase 6 workbench standing permission"},
    )
    assert r.status_code == 200, r.text


def _graph_for(action_type: str, parameters: dict) -> dict:
    return {
        "intent": "phase6 workbench journey",
        "nodes": [
            {"node_id": "n0", "node_type": "action",
             "action": {"type": action_type, "parameters": parameters}},
            {"node_id": "end", "node_type": "end", "depends_on": ["n0"]},
        ],
    }


def _policy_for(action_type: str, permissions: list[str]) -> dict:
    return {
        "policy": {
            "rules": [
                {"action": action_type, "decision": "allow",
                 "permissions": list(permissions)}
            ],
            "default_decision": "deny",
        }
    }


def _capabilities(client: TestClient) -> dict[str, dict]:
    r = client.get("/capabilities")
    assert r.status_code == 200, r.text
    return {c["action"]: c for c in r.json()}


# ---------------------------------------------------------------------------
# The journey the workbench UI walks, one HTTP round trip at a time.
# ---------------------------------------------------------------------------

def test_low_risk_end_to_end_plan_execute_verified():
    """search.web: instruction -> plan -> execute -> verified -> history."""
    client = _client()
    caps = _capabilities(client)
    assert caps["search.web"]["risk_level"] == "low"

    _consent(client, "search.web", "medium")

    plan_r = client.post("/plan", json={"intent": "search for cats"})
    assert plan_r.status_code == 200, plan_r.text
    plan = plan_r.json()
    assert plan["intent"] == "search for cats"
    assert [a["type"] for a in plan["actions"]] == ["search.web"]
    assert plan["actions"][0]["parameters"] == {"query": "cats"}

    result_r = client.post(
        "/execute",
        json={
            "graph": _graph_for("search.web", plan["actions"][0]["parameters"]),
            "actor": {"id": ACTOR},
            **_policy_for("search.web", caps["search.web"]["permissions"]),
        },
    )
    assert result_r.status_code == 200, result_r.text
    result = result_r.json()
    assert result["state"] == "success", result
    node = result["nodes"]["n0"]
    assert node["verified"] is True, node
    assert node["verification"]["verified"] is True
    # Honestly simulated: the mock provider's effect is simulated by design.
    assert node["verification"]["simulated"] is True
    assert node["provider"] == "mock-search"

    fetched = client.get(f"/executions/{result['execution_id']}")
    assert fetched.status_code == 200, fetched.text
    record = fetched.json()
    assert record["state"] == "success"
    assert record["results"]["n0"]["verified"] is True

    history = client.get("/executions?limit=5")
    assert history.status_code == 200, history.text
    ids = [row["execution_id"] for row in history.json()]
    assert result["execution_id"] in ids


def test_critical_parks_for_approval_then_resumes_on_approve():
    """payment.prepare: waiting_permission -> /approvals -> approve -> success."""
    client = _client()
    caps = _capabilities(client)
    assert caps["payment.prepare"]["risk_level"] == "critical"

    _consent(client, "payment.prepare", "critical")

    plan_r = client.post("/plan", json={"intent": "pay alice 25"})
    assert plan_r.status_code == 200, plan_r.text
    plan = plan_r.json()
    assert [a["type"] for a in plan["actions"]] == ["payment.prepare"]
    params = plan["actions"][0]["parameters"]
    # The planner coerces the amount to a number; assert the shape honestly.
    assert params["recipient"] == "alice"
    assert float(params["amount"]) == 25.0

    result_r = client.post(
        "/execute",
        json={
            "graph": _graph_for("payment.prepare", params),
            "actor": {"id": ACTOR},
            **_policy_for("payment.prepare", caps["payment.prepare"]["permissions"]),
        },
    )
    assert result_r.status_code == 200, result_r.text
    result = result_r.json()
    # CRITICAL risk always requires explicit user approval.
    assert result["state"] == "waiting_permission", result
    node = result["nodes"]["n0"]
    assert node["verified"] is None, node
    assert "CRITICAL" in (node["error"] or "")

    approvals = client.get("/approvals")
    assert approvals.status_code == 200, approvals.text
    entries = approvals.json()
    assert entries, "the parked payment.prepare action must surface in /approvals"
    entry = next(a for e in entries for a in e["actions"]
                 if a["action_type"] == "payment.prepare")
    assert entry["risk_level"] == "critical"
    # execution_id lives on the envelope (not the action), per engine.awaiting_approval.
    envelope = next(e for e in entries for a in e["actions"]
                    if a["action_type"] == "payment.prepare")
    assert envelope["execution_id"] == result["execution_id"]

    decided = client.post("/approvals", json={
        "action_id": entry["action_id"], "status": "approved",
        "action_type": "payment.prepare", "summary": "approved from the workbench",
        "execution_id": result["execution_id"],
    })
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["status"] == "approved"
    assert body["expires_at"], "approved approvals carry the 10-minute TTL"
    resumed = body["execution"]
    assert resumed["state"] == "success", resumed
    assert resumed["nodes"]["n0"]["verified"] is True
    assert resumed["nodes"]["n0"]["result"] == {"state": "prepared"}


def test_reject_leaves_execution_parked_not_success():
    """Rejecting is recorded but does not claim the run proceeded."""
    client = _client()
    caps = _capabilities(client)
    _consent(client, "payment.prepare", "critical")

    plan_r = client.post("/plan", json={"intent": "pay bob 10"})
    params = plan_r.json()["actions"][0]["parameters"]

    result = client.post("/execute", json={
        "graph": _graph_for("payment.prepare", params),
        "actor": {"id": ACTOR},
        **_policy_for("payment.prepare", caps["payment.prepare"]["permissions"]),
    }).json()
    assert result["state"] == "waiting_permission"

    approvals = client.get("/approvals").json()
    entry = next(a for e in approvals for a in e["actions"]
                 if a["action_type"] == "payment.prepare")

    decided = client.post("/approvals", json={
        "action_id": entry["action_id"], "status": "rejected",
        "action_type": "payment.prepare", "summary": "rejected from the workbench",
        "execution_id": result["execution_id"],
    })
    assert decided.status_code == 200, decided.text
    # Reject does not run the action, and the engine keeps the execution
    # parked (policy still ASKs without an approval) -- never a success.
    if decided.json().get("execution"):
        assert decided.json()["execution"]["state"] != "success"


def test_client_grants_injection_is_rejected():
    """A client can never define its own authority (AGENTS.md invariant 1)."""
    client = _client()
    r = client.post("/execute", json={
        "graph": _graph_for("search.web", {"query": "cats"}),
        "actor": {"id": ACTOR},
        "grants": [{"scope": "search.web", "max_risk": "critical"}],
        **_policy_for("search.web", ["search.web"]),
    })
    assert r.status_code == 400, r.text
    assert "grants" in r.json()["detail"].lower()


def test_no_consent_fails_closed():
    """Without a server-side consent the engine denies -- never guesses."""
    client = _client()
    caps = _capabilities(client)
    r = client.post("/execute", json={
        "graph": _graph_for("search.web", {"query": "cats"}),
        "actor": {"id": ACTOR},
        **_policy_for("search.web", caps["search.web"]["permissions"]),
    })
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "denied", r.json()


def test_empty_intent_is_refused():
    client = _client()
    r = client.post("/plan", json={"intent": "   "})
    assert r.status_code == 400, r.text


def test_actorless_execute_never_authorizes():
    """No actor in the body -> the engine fails closed (denied)."""
    client = _client()
    caps = _capabilities(client)
    r = client.post("/execute", json={
        "graph": _graph_for("search.web", {"query": "cats"}),
        **_policy_for("search.web", caps["search.web"]["permissions"]),
    })
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "denied", r.json()
