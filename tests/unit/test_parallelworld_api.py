from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from actionmesh.agent.parallelworld import get_preview
from actionmesh.api import create_app


def _grant(client: TestClient, scope: str, risk: str = "high") -> None:
    response = client.post("/consents", json={
        "actor_id": "voiceos-local", "scope": scope, "max_risk": risk,
    })
    assert response.status_code == 200, response.text


def test_parallelworld_preview_apply_through_engine_and_approval(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ACTIONMESH_AGENT", "1")
    monkeypatch.setenv("ACTIONMESH_WORKSPACE_ROOT", str(tmp_path))
    release = tmp_path / "release.txt"
    candidate = tmp_path / "candidate.txt"
    release.write_text("candidate build\n", encoding="utf-8")
    candidate.write_text("artifact\n", encoding="utf-8")
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}

    with TestClient(create_app(), client=("127.0.0.1", 45123)) as client:
        _grant(client, "file.read")
        _grant(client, "file.write")
        operations = [
            {"op": "edit", "path": "release.txt", "find": "candidate", "replace": "ready"},
            {"op": "copy", "path": "candidate.txt", "to": "candidate-copy.txt"},
        ]
        preview = client.post("/parallelworld/previews", json={
            "actor": {"id": "voiceos-local"}, "operations": operations,
        })
        assert preview.status_code == 200, preview.text
        preview_body = preview.json()
        assert preview_body["state"] == "success"
        assert preview_body["preview_id"]
        assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()} == before
        saved = client.get(f"/parallelworld/previews/{preview_body['preview_id']}").json()
        assert saved["source_unchanged_during_preview"]
        assert "operations" not in saved
        assert "diff" not in saved
        assert "+ready build" in get_preview(preview_body["preview_id"]).diff
        assert client.get("/parallelworld/previews").json()[0]["preview_id"] == preview_body["preview_id"]

        applied = client.post(
            f"/parallelworld/previews/{preview_body['preview_id']}/apply",
            json={"actor": {"id": "voiceos-local"}},
        )
        assert applied.status_code == 200, applied.text
        run = applied.json()
        assert run["state"] == "waiting_permission", run
        # Earlier medium-risk steps must remain gated behind a pending HIGH-risk
        # edit; a parked graph cannot run ahead of its named approval.
        assert not (tmp_path / "candidate-copy.txt").exists()
        pending_state = client.get(f"/executions/{run['execution_id']}").json()
        assert len(pending_state["results"]) == 1
        while run["state"] == "waiting_permission":
            pending = next(item for item in client.get("/approvals").json()
                           if item["execution_id"] == run["execution_id"])
            action = next(action for action in pending["actions"]
                           if action["action_type"] == "device.files.edit")
            answer = client.post("/approvals", json={
                "action_id": action["action_id"], "action_type": action["action_type"],
                "execution_id": run["execution_id"], "status": "approved",
            })
            assert answer.status_code == 200, answer.text
            if answer.json().get("execution"):
                run = answer.json()["execution"]
        assert run["state"] == "success", run
        saved = client.get(f"/parallelworld/previews/{preview_body['preview_id']}").json()
        assert saved["execution"]["state"] == "success"
        assert saved["source_current"]
        assert all(item.get("verified") is True for item in saved["execution"]["results"].values()
                   if item.get("action_type"))
        assert release.read_text(encoding="utf-8") == "ready build\n"
        assert (tmp_path / "candidate-copy.txt").read_text(encoding="utf-8") == "artifact\n"


def test_parallelworld_without_read_permission_waits_without_snapshot_write(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ACTIONMESH_AGENT", "1")
    monkeypatch.setenv("ACTIONMESH_WORKSPACE_ROOT", str(tmp_path))
    source = tmp_path / "release.txt"
    source.write_text("candidate", encoding="utf-8")
    with TestClient(create_app(), client=("127.0.0.1", 45123)) as client:
        response = client.post("/parallelworld/previews", json={
            "actor": {"id": "voiceos-local"},
            "operations": [{"op": "edit", "path": "release.txt", "find": "candidate", "replace": "ready"}],
        })
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "denied"
        assert response.json()["preview_id"] == ""
        assert source.read_text(encoding="utf-8") == "candidate"


def test_parallelworld_api_rejects_stale_preview_before_dispatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ACTIONMESH_AGENT", "1")
    monkeypatch.setenv("ACTIONMESH_WORKSPACE_ROOT", str(tmp_path))
    source = tmp_path / "release.txt"
    source.write_text("candidate", encoding="utf-8")
    with TestClient(create_app(), client=("127.0.0.1", 45123)) as client:
        _grant(client, "file.read")
        response = client.post("/parallelworld/previews", json={
            "actor": {"id": "voiceos-local"},
            "operations": [{"op": "edit", "path": "release.txt", "find": "candidate", "replace": "ready"}],
        })
        preview_id = response.json()["preview_id"]
        source.write_text("external update", encoding="utf-8")
        stale = client.post(
            f"/parallelworld/previews/{preview_id}/apply",
            json={"actor": {"id": "voiceos-local"}},
        )
        assert stale.status_code == 409
        assert "stale preview" in stale.json()["detail"]
        assert source.read_text(encoding="utf-8") == "external update"
