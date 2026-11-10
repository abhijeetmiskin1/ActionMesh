import socket
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from actionmesh.agent.processes import CONTINUUM_PORT
from actionmesh.api import create_app


def test_continuum_live_approval_and_recovery(tmp_path, monkeypatch):
    # The demo intentionally uses one fixed loopback port. A live packaged demo may
    # already own it; never stop or misclassify that unrelated process as test state.
    try:
        with socket.create_connection(("127.0.0.1", CONTINUUM_PORT), timeout=0.2):
            pytest.skip(f"Continuum demo port {CONTINUUM_PORT} is already in use")
    except OSError:
        pass

    monkeypatch.setenv("ACTIONMESH_AGENT", "1")
    monkeypatch.setenv("ACTIONMESH_ALLOW_SHELL", "1")
    monkeypatch.setenv("ACTIONMESH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ACTIONMESH_AUDIT_DB", str(tmp_path / "audit.db"))
    folder = tmp_path / "continuum-demo"
    folder.mkdir()
    (folder / "health.txt").write_text("ready\n")
    actor = {"actor_id": "voiceos-local"}
    with TestClient(create_app(), client=("127.0.0.1", 12345)) as client:
        assert client.post("/continuum", json={"description": "demo", "invariant": "arbitrary"}).status_code == 422
        client.post("/continuum", json={"description": "demo", "interval": 3}).raise_for_status()
        blocked = client.post("/continuum/check").json()
        assert blocked["observation"] == "UNKNOWN"
        assert blocked["attempts"] == 0
        for scope, risk in (("device.server.read", "low"), ("device.server.control", "high")):
            client.post("/consents", json={**actor, "scope": scope, "max_risk": risk}).raise_for_status()

        def check():
            response = client.post("/continuum/check")
            response.raise_for_status()
            return response.json()

        def approve(execution_id):
            waiting = next(row for row in client.get("/approvals").json()
                           if row["execution_id"] == execution_id)
            response = client.post("/approvals", json={
                "action_id": waiting["actions"][0]["action_id"],
                "execution_id": execution_id, "status": "approved",
            })
            response.raise_for_status()
            assert response.json()["execution"]["state"] == "success", response.json()

        first = check()
        assert first["state"] == "AWAITING_APPROVAL", first
        approve(first["pending"])
        assert check()["observation"] == "VERIFIED"
        assert check()["attempts"] == 0
        stopped = client.post("/continuum/stop-demo")
        stopped.raise_for_status()
        approve(stopped.json()["execution_id"])
        deadline = monotonic() + 8
        while monotonic() < deadline:
            repair = client.get("/continuum").json()
            if repair["state"] == "AWAITING_APPROVAL":
                break
            sleep(0.1)
        assert repair["state"] == "AWAITING_APPROVAL", repair
        assert repair["observation"] == "VIOLATED"
        assert check()["pending"] == repair["pending"]
        assert check()["attempts"] == 1
        approve(repair["pending"])
        recovered = check()
        assert recovered["state"] == "RECOVERED"
        assert recovered["observation"] == "VERIFIED"
        cleanup = client.post("/continuum/stop-demo")
        cleanup.raise_for_status()
        approve(cleanup.json()["execution_id"])
        client.post("/continuum", json={"description": "demo", "enabled": False}).raise_for_status()

    with TestClient(create_app(), client=("127.0.0.1", 12345)) as client:
        assert client.get("/continuum").json()["state"] == "DISABLED"
