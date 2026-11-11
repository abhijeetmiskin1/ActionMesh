"""Real local project registration and durable independent Goal Contracts."""

from __future__ import annotations

import socket
import sqlite3
import subprocess
import time

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.voiceos.project_goals import ProjectGoalStore


def test_multiple_goals_and_event_pages_survive_store_reopen(tmp_path):
    database = tmp_path / "continuum.db"
    store = ProjectGoalStore(database)
    project = store.register_project(
        name="workspace", root="", kind="folder", kinds=[], scripts=[], owner_id="operator",
    )
    first = store.create_goal(
        project_id=project["project_id"], owner_id="operator", device_id="laptop",
        description="file exists", resource_identity="one.txt", contract_type="file_exists",
        expected={"value": True}, observer="workspace.file.exists", interval_seconds=30,
        recovery={"enabled": False}, attempt_budget=0,
    )
    second = store.create_goal(
        project_id=project["project_id"], owner_id="operator", device_id="laptop",
        description="app running", resource_identity="TextEdit", contract_type="app_running",
        expected={"value": True}, observer="macos.application.running", interval_seconds=60,
        recovery={"enabled": False}, attempt_budget=0,
    )
    assert first["goal_id"] != second["goal_id"]
    for _ in range(45):
        store.record_observation(
            first["goal_id"], observer="workspace.file.exists", target_identity="one.txt",
            expected={"value": True}, observed={"exists": False}, verdict="VIOLATED",
            fresh_until="2099-01-01T00:00:00+00:00", execution_id="exec-read",
        )
    store.close()

    reopened = ProjectGoalStore(database)
    assert [row["goal_id"] for row in reopened.list_goals()] == [second["goal_id"], first["goal_id"]]
    assert reopened.get_goal(second["goal_id"])["current_observation"] == "UNKNOWN"
    assert reopened.get_goal(first["goal_id"])["current_observation"] == "VIOLATED"
    page1 = reopened.list_events(first["goal_id"], limit=40, offset=0)
    page2 = reopened.list_events(first["goal_id"], limit=40, offset=40)
    assert len(page1) == 40
    assert len(page2) == 6  # created event plus 45 observations
    assert page1[0]["execution_id"] == "exec-read"
    reopened.close()


def test_existing_database_is_backed_up_before_v6_tables_are_added(tmp_path):
    path = tmp_path / "continuum.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE continuum (id INTEGER PRIMARY KEY, body TEXT)")
    raw.execute("INSERT INTO continuum VALUES (1, 'legacy goal')")
    raw.commit()
    raw.close()

    store = ProjectGoalStore(path)
    store.close()

    backups = list(tmp_path.glob("continuum.db.pre-v6-2-*.bak"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    assert backup.execute("SELECT body FROM continuum WHERE id=1").fetchone()[0] == "legacy goal"
    backup.close()
    migrated = sqlite3.connect(path)
    assert migrated.execute("SELECT body FROM continuum WHERE id=1").fetchone()[0] == "legacy goal"
    migrated.close()


def test_service_registration_and_http_goal_are_typed_and_project_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONMESH_AGENT", "1")
    monkeypatch.setenv("ACTIONMESH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ACTIONMESH_AUDIT_DB", str(tmp_path / "audit.db"))
    monkeypatch.setenv("ACTIONMESH_ALLOW_SHELL", "1")
    monkeypatch.delenv("ACTIONMESH_ALLOWED_COMMANDS", raising=False)
    project_dir = tmp_path / "service-repo"
    project_dir.mkdir()
    (project_dir / "health").write_text("ready\n", encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    with TestClient(create_app(), client=("127.0.0.1", 12345)) as client:
        project = client.post("/api/v1/projects", json={"path": "service-repo"}).json()
        service_response = client.post(
            f"/api/v1/projects/{project['project_id']}/services",
            json={
                "name": "sample-api", "command": "python3",
                "arguments": ["-m", "http.server", str(port), "--bind", "127.0.0.1"],
                "working_directory": ".", "port": port,
                "health_path": "/health", "expected_status": 200,
            },
        )
        assert service_response.status_code == 200, service_response.text
        service = service_response.json()
        assert service["timeout_ms"] == 1500
        assert service["project_id"] == project["project_id"]
        assert client.get(f"/api/v1/projects/{project['project_id']}/services").json()["services"][0]["service_id"] == service["service_id"]
        goal_response = client.post("/api/v1/goals", json={
            "project_id": project["project_id"], "contract_type": "http_service",
            "resource": service["service_id"], "expected": True,
            "interval_seconds": 10, "monitoring_enabled": False,
            "recovery": {"enabled": True}, "attempt_budget": 2,
        })
        assert goal_response.status_code == 200, goal_response.text
        goal = goal_response.json()
        assert goal["observer"] == "loopback.http.status"
        assert goal["recovery"] == {"enabled": True, "mode": "manual_approval"}
        for scope, risk in (("device.server.read", "low"), ("device.server.control", "high")):
            client.post("/consents", json={
                "actor_id": "voiceos-local", "scope": scope, "max_risk": risk,
            }).raise_for_status()
        first_observation = client.post(f"/api/v1/goals/{goal['goal_id']}/observe")
        assert first_observation.status_code == 200, first_observation.text
        assert first_observation.json()["verdict"] == "VIOLATED"
        proposed = client.post(f"/api/v1/goals/{goal['goal_id']}/recover")
        assert proposed.status_code == 200, proposed.text
        proposal = proposed.json()
        assert proposal["state"] == "awaiting_approval", proposal
        assert proposal["attempt"]["status"] == "PENDING_APPROVAL"
        duplicate = client.post(f"/api/v1/goals/{goal['goal_id']}/recover")
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["state"] == "already_pending"
        assert duplicate.json()["execution_id"] == proposal["execution_id"]
        waiting = next(
            row for row in client.get("/approvals").json()
            if row["execution_id"] == proposal["execution_id"]
        )
        action = waiting["actions"][0]
        approved = client.post("/approvals", json={
            "action_id": action["action_id"], "action_type": action["action_type"],
            "execution_id": proposal["execution_id"], "status": "approved",
        })
        assert approved.status_code == 200, approved.text
        assert approved.json()["execution"]["state"] == "success"
        verified = client.post(f"/api/v1/goals/{goal['goal_id']}/observe")
        assert verified.status_code == 200, verified.text
        assert verified.json()["verdict"] == "VERIFIED"
        assert verified.json()["observed"]["http_status"] == 200
        process_goal_response = client.post("/api/v1/goals", json={
            "project_id": project["project_id"], "contract_type": "managed_process",
            "resource": service["service_id"], "expected": "running", "interval_seconds": 10,
        })
        assert process_goal_response.status_code == 200, process_goal_response.text
        process_goal = process_goal_response.json()
        assert process_goal["observer"] == "actionmesh.managed_process.lifecycle"
        process_observation = client.post(f"/api/v1/goals/{process_goal['goal_id']}/observe")
        assert process_observation.status_code == 200, process_observation.text
        assert process_observation.json()["verdict"] == "VERIFIED"
        assert process_observation.json()["observed"]["condition"] == "running"
        events = client.get(f"/api/v1/goals/{goal['goal_id']}/events?limit=50")
        assert events.status_code == 200
        recovery_success = next(
            event for event in events.json()["events"]
            if event["event_type"] == "recovery.success"
        )
        assert recovery_success["execution_id"] == proposal["execution_id"]
        recoveries = client.get(f"/api/v1/goals/{goal['goal_id']}/recoveries")
        assert recoveries.status_code == 200, recoveries.text
        assert recoveries.json()["attempts"][0]["status"] == "SUCCESS"
        stop = client.post(
            f"/api/v1/projects/{project['project_id']}/services/{service['service_id']}/actions",
            json={"operation": "stop"},
        )
        assert stop.status_code == 200, stop.text
        assert stop.json()["state"] == "awaiting_approval"
        stop_approval = next(
            row for row in client.get("/approvals").json()
            if row["execution_id"] == stop.json()["execution_id"]
        )
        stop_action = stop_approval["actions"][0]
        stopped = client.post("/approvals", json={
            "action_id": stop_action["action_id"], "action_type": stop_action["action_type"],
            "execution_id": stop.json()["execution_id"], "status": "approved",
        })
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["execution"]["state"] == "success"
        process_stopped_observation = client.post(
            f"/api/v1/goals/{process_goal['goal_id']}/observe"
        )
        assert process_stopped_observation.status_code == 200, process_stopped_observation.text
        assert process_stopped_observation.json()["verdict"] == "VIOLATED"
        assert process_stopped_observation.json()["observed"]["process_state"] == "stopped"
        down = client.post(f"/api/v1/goals/{goal['goal_id']}/observe")
        assert down.status_code == 200, down.text
        assert down.json()["verdict"] == "VIOLATED"
        assert down.json()["observed"]["http_state"] == "UNREACHABLE"
        assert down.json()["observed"]["http_error"] == "connection_refused"
        service_events = client.get(f"/api/v1/goals/{goal['goal_id']}/events?limit=50").json()["events"]
        assert any(event["event_type"] == "service.control.awaiting_approval" for event in service_events)
        assert any(event["execution_id"] == stop.json()["execution_id"] for event in service_events)
        terminal = [
            event for event in service_events
            if event["execution_id"] == stop.json()["execution_id"]
            and event["event_type"] == "service.control.success"
        ]
        assert len(terminal) == 1
        client.get(f"/api/v1/goals/{goal['goal_id']}/events?limit=50")
        repeated = client.get(f"/api/v1/goals/{goal['goal_id']}/events?limit=50").json()["events"]
        assert sum(
            event["execution_id"] == stop.json()["execution_id"]
            and event["event_type"] == "service.control.success"
            for event in repeated
        ) == 1
        refused = client.post(
            f"/api/v1/projects/{project['project_id']}/services",
            json={"name": "bad-target", "command": "python3", "arguments": [],
                  "port": port, "health_path": "//example.com"},
        )
        assert refused.status_code == 422
        refused_exec = client.post(
            f"/api/v1/projects/{project['project_id']}/services",
            json={"name": "bad-exec", "command": "bash", "arguments": [], "port": port},
        )
        assert refused_exec.status_code == 422


def test_project_goal_schema_v4_preserves_existing_v2_records(tmp_path):
    path = tmp_path / "continuum.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE project_goal_schema (singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL)")
    raw.execute("INSERT INTO project_goal_schema VALUES(1,2)")
    raw.execute("CREATE TABLE prior_data (value TEXT NOT NULL)")
    raw.execute("INSERT INTO prior_data VALUES('keep me')")
    raw.commit()
    raw.close()
    store = ProjectGoalStore(path)
    assert store.db.execute("SELECT version FROM project_goal_schema WHERE singleton=1").fetchone()[0] == 4
    assert store.db.execute("SELECT value FROM prior_data").fetchone()[0] == "keep me"
    assert store.db.execute("SELECT COUNT(*) FROM managed_services").fetchone()[0] == 0
    assert list(tmp_path.glob("continuum.db.pre-v6-2-*.bak"))
    store.close()


def test_v3_service_rows_gain_bounded_timeout_without_data_loss(tmp_path):
    path = tmp_path / "continuum.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE project_goal_schema (singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL);"
        "INSERT INTO project_goal_schema VALUES(1,3);"
        "CREATE TABLE projects (project_id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL UNIQUE, "
        "kind TEXT NOT NULL, kinds_json TEXT NOT NULL, scripts_json TEXT NOT NULL, owner_id TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT);"
        "INSERT INTO projects VALUES('p','project','repo','git','[]','[]','owner','now','now',NULL);"
        "CREATE TABLE managed_services (service_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
        "name TEXT NOT NULL, command TEXT NOT NULL, arguments_json TEXT NOT NULL, working_directory TEXT NOT NULL, "
        "port INTEGER NOT NULL, health_path TEXT NOT NULL, expected_status INTEGER NOT NULL, owner_id TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        "INSERT INTO managed_services VALUES('s','p','api','python3','[]','repo',9000,'/health',200,'owner','now','now');"
    )
    raw.commit()
    raw.close()
    store = ProjectGoalStore(path)
    service = store.get_service("s")
    assert service is not None
    assert service["timeout_ms"] == 1500
    assert service["port"] == 9000
    assert store.db.execute("SELECT version FROM project_goal_schema WHERE singleton=1").fetchone()[0] == 4
    assert list(tmp_path.glob("continuum.db.pre-v6-2-*.bak"))
    store.close()


def test_versioned_api_registers_project_creates_and_observes_independent_goal(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONMESH_AGENT", "1")
    monkeypatch.setenv("ACTIONMESH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ACTIONMESH_AUDIT_DB", str(tmp_path / "audit.db"))
    monkeypatch.setenv("ACTIONMESH_ALLOW_SHELL", "1")
    monkeypatch.delenv("ACTIONMESH_ALLOWED_COMMANDS", raising=False)
    monkeypatch.delenv("ACTIONMESH_DENIED_COMMANDS", raising=False)
    project_dir = tmp_path / "sample-repo"
    project_dir.mkdir()
    subprocess.run(["git", "init", str(project_dir)], check=True, capture_output=True)
    (project_dir / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    (project_dir / "present.txt").write_text("actual file\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(project_dir), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "add", "pyproject.toml", "present.txt"], check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(project_dir), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "initial"], check=True, capture_output=True,
    )
    with TestClient(create_app(), client=("127.0.0.1", 12345)) as client:
        root_registration = client.post("/api/v1/projects", json={"path": "."})
        assert root_registration.status_code == 200, root_registration.text
        assert root_registration.json()["root"] == ""
        assert client.post("/consents", json={
            "actor_id": "voiceos-local", "scope": "device.project.read", "max_risk": "low",
        }).status_code == 200
        root_status = client.get(
            f"/api/v1/projects/{root_registration.json()['project_id']}/status"
        )
        assert root_status.status_code == 200, root_status.text
        assert root_status.json()["verified"] is True
        registration = client.post("/api/v1/projects", json={"path": "sample-repo"})
        assert registration.status_code == 200, registration.text
        project = registration.json()
        assert project["kinds"] == ["git", "python"]
        assert project["location"] == str(project_dir)
        selected = client.post(f"/api/v1/projects/{project['project_id']}/select")
        assert selected.status_code == 200
        second_project = client.get("/api/v1/projects").json()
        assert second_project["selected"]["project_id"] == project["project_id"]
        assert client.post("/consents", json={
            "actor_id": "voiceos-local", "scope": "device.project.read", "max_risk": "low",
        }).status_code == 200
        status = client.get(f"/api/v1/projects/{project['project_id']}/status")
        assert status.status_code == 200, status.text
        assert status.json()["verified"] is True
        assert status.json()["status"].get("branch") in {"main", "master"}, status.json()
        assert status.json()["status"]["changed"] == 0
        created = client.post("/api/v1/goals", json={
            "contract_type": "file_exists", "resource": "present.txt", "expected": True,
            "description": "Required file exists", "interval_seconds": 3,
        })
        assert created.status_code == 200, created.text
        goal = created.json()
        assert goal["project_id"] == project["project_id"]
        assert goal["observer"] == "workspace.file.exists"
        assert client.post("/consents", json={
            "actor_id": "voiceos-local", "scope": "device.file.list", "max_risk": "low",
        }).status_code == 200
        denied_baseline = client.post(
            f"/api/v1/projects/{project['project_id']}/files/hash",
            json={"path": "present.txt"},
        )
        assert denied_baseline.status_code == 409
        assert client.post("/consents", json={
            "actor_id": "voiceos-local", "scope": "file.read", "max_risk": "medium",
        }).status_code == 200
        baseline = client.post(
            f"/api/v1/projects/{project['project_id']}/files/hash",
            json={"path": "present.txt"},
        )
        assert baseline.status_code == 200, baseline.text
        assert baseline.json()["verified"] is True
        assert len(baseline.json()["sha256"]) == 64
        hash_goal_response = client.post("/api/v1/goals", json={
            "project_id": project["project_id"], "contract_type": "file_sha256",
            "resource": "present.txt", "expected": baseline.json()["sha256"],
            "baseline_execution_id": baseline.json()["execution_id"],
            "interval_seconds": 10,
        })
        assert hash_goal_response.status_code == 200, hash_goal_response.text
        hash_goal = hash_goal_response.json()
        assert hash_goal["expected"]["sha256"] == baseline.json()["sha256"]
        created_hash_event = client.get(
            f"/api/v1/goals/{hash_goal['goal_id']}/events?limit=10"
        ).json()["events"][-1]
        assert created_hash_event["execution_id"] == baseline.json()["execution_id"]
        hash_observation = client.post(f"/api/v1/goals/{hash_goal['goal_id']}/observe")
        assert hash_observation.status_code == 200, hash_observation.text
        assert hash_observation.json()["verdict"] == "VERIFIED"
        (project_dir / "present.txt").write_text("changed after baseline\n", encoding="utf-8")
        changed_observation = client.post(f"/api/v1/goals/{hash_goal['goal_id']}/observe")
        assert changed_observation.status_code == 200, changed_observation.text
        assert changed_observation.json()["verdict"] == "VIOLATED"
        assert changed_observation.json()["observed"]["sha256"] != baseline.json()["sha256"]
        observed = client.post(f"/api/v1/goals/{goal['goal_id']}/observe")
        assert observed.status_code == 200, observed.text
        assert observed.json()["verdict"] == "VERIFIED"
        absent_goal = client.post("/api/v1/goals", json={
            "contract_type": "file_exists", "resource": "missing.txt", "expected": True,
        })
        assert absent_goal.status_code == 200, absent_goal.text
        absent_observation = client.post(
            f"/api/v1/goals/{absent_goal.json()['goal_id']}/observe"
        )
        assert absent_observation.status_code == 200, absent_observation.text
        assert absent_observation.json()["verdict"] == "VIOLATED"
        events = client.get(f"/api/v1/goals/{goal['goal_id']}/events?limit=10")
        assert events.status_code == 200
        assert [entry["event_type"] for entry in events.json()["events"]][:2] == [
            "goal.observed", "goal.created",
        ]
        monitor_on = client.post(f"/api/v1/goals/{goal['goal_id']}/monitor", json={"enabled": True})
        assert monitor_on.status_code == 200
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            observations = client.get(f"/api/v1/goals/{goal['goal_id']}/evidence?limit=10").json()
            if observations["count"] >= 2:
                break
            time.sleep(0.2)
        assert observations["count"] >= 2, "opt-in monitor did not create a real second observation"
        assert client.post(f"/api/v1/goals/{goal['goal_id']}/monitor", json={"enabled": False}).status_code == 200
        # A client cannot smuggle authority into a stored contract.
        refused = client.post("/api/v1/goals", json={
            "project_id": project["project_id"], "contract_type": "file_exists",
            "resource": "present.txt", "grants": [],
        })
        assert refused.status_code == 400

    with TestClient(create_app(), client=("127.0.0.1", 12345)) as reopened:
        restored = reopened.get(f"/api/v1/goals/{goal['goal_id']}")
        assert restored.status_code == 200
        assert restored.json()["current_observation"] == "VERIFIED"
        assert reopened.get(f"/api/v1/goals/{goal['goal_id']}/events?limit=50").json()["count"] >= 5
