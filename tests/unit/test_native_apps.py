"""VoiceOS can launch only supported Mac apps through the existing policy pipeline."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from actionmesh.agent.apps import MAC_APP_ACTION, MAC_APP_STATUS, launch
from actionmesh.agent.config import AgentConfig
from actionmesh.api import create_app
from actionmesh.providers.base import ProviderError


def test_native_app_task_requires_permission_and_live_observation(monkeypatch) -> None:
    monkeypatch.setenv("ACTIONMESH_AGENT", "1")
    monkeypatch.setenv("ACTIONMESH_ALLOW_NATIVE_APPS", "1")
    with (
        patch("actionmesh.agent.config.sys.platform", "darwin"),
        patch("actionmesh.agent.apps.launch") as launch_app,
        patch("actionmesh.agent.apps.running", side_effect=[False, True]) as observe,
        TestClient(create_app(), client=("127.0.0.1", 12345)) as client,
    ):
        actor = {"id": "voiceos-local"}

        def run() -> dict:
            launch_count = launch_app.call_count
            message = client.post("/voiceos/messages", json={
                "actor": actor, "text": "Jarvis, open the Calculator app.",
            }).json()
            assert message["kind"] == "task"
            assert launch_app.call_count == launch_count
            return client.post(f"/voiceos/sessions/{message['session_id']}/run", json={
                "actor": actor, "stop_on_unverified": True,
            }).json()

        denied = run()
        assert denied["steps"][0]["state"] == "denied"
        launch_app.assert_not_called()
        observe.assert_not_called()

        consent = client.post("/consents", json={
            "actor_id": actor["id"], "scope": "system.app.launch", "max_risk": "medium",
        })
        assert consent.is_success
        unconfirmed = run()
        assert unconfirmed["status"] == "failed"
        assert not unconfirmed["steps"][0]["verified"]

        verified = run()
        assert verified["status"] == "completed"
        assert verified["steps"][0]["verified"] is True
        assert verified["steps"][0]["action_type"] == MAC_APP_ACTION
        assert [call.args for call in launch_app.call_args_list] == [("calculator",)] * 2


def test_native_apps_do_not_enable_the_shell() -> None:
    with patch("actionmesh.agent.config.sys.platform", "darwin"):
        config = AgentConfig(device_id="mac", allow_native_apps=True)
        assert config.advertise() == (MAC_APP_ACTION, MAC_APP_STATUS)


def test_launcher_rejects_arbitrary_bundle_id_before_spawning() -> None:
    with patch("actionmesh.agent.apps.subprocess.run") as spawn:
        with pytest.raises(ProviderError, match="supported Mac app"):
            launch("com.apple.Terminal")
        spawn.assert_not_called()


def test_app_list_runs_in_order_and_stops_on_unconfirmed_launch(monkeypatch) -> None:
    monkeypatch.setenv("ACTIONMESH_AGENT", "1")
    monkeypatch.setenv("ACTIONMESH_ALLOW_NATIVE_APPS", "1")
    with (
        patch("actionmesh.agent.config.sys.platform", "darwin"),
        patch("actionmesh.agent.apps.launch") as launch_app,
        patch("actionmesh.agent.apps.running", side_effect=[False, True, True, True]),
        TestClient(create_app(), client=("127.0.0.1", 12345)) as client,
    ):
        actor = {"id": "voiceos-local"}
        assert client.post("/consents", json={
            "actor_id": actor["id"], "scope": "system.app.launch", "max_risk": "medium",
        }).is_success

        def run(text: str) -> dict:
            proposal = client.post("/voiceos/messages", json={"actor": actor, "text": text}).json()
            return client.post(f"/voiceos/sessions/{proposal['session_id']}/run", json={
                "actor": actor, "stop_on_unverified": True,
            }).json()

        failed = run("Open Calculator and Clock")
        assert failed["status"] == "failed"
        launch_app.assert_called_once_with("calculator")
        launch_app.reset_mock()

        result = run("Open Clock and Calculator and then open Stickies")
        assert result["status"] == "completed"
        assert all(step["verified"] for step in result["steps"])
        assert [call.args for call in launch_app.call_args_list] == [("clock",), ("calculator",), ("stickies",)]


def test_app_status_distinguishes_closed_from_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("ACTIONMESH_AGENT", "1")
    monkeypatch.setenv("ACTIONMESH_ALLOW_NATIVE_APPS", "1")
    with (
        patch("actionmesh.agent.config.sys.platform", "darwin"),
        patch("actionmesh.agent.apps.launch") as launch_app,
        patch("actionmesh.agent.apps.running", side_effect=[False, False, None]),
        TestClient(create_app(), client=("127.0.0.1", 12345)) as client,
    ):
        actor = {"id": "voiceos-local"}

        def check() -> dict:
            proposal = client.post("/voiceos/messages", json={
                "actor": actor, "text": "Is the Clock app running?",
            }).json()
            return client.post(f"/voiceos/sessions/{proposal['session_id']}/run", json={
                "actor": actor, "stop_on_unverified": True,
            }).json()

        assert check()["steps"][0]["state"] == "denied"
        assert client.post("/consents", json={
            "actor_id": actor["id"], "scope": "system.app.status", "max_risk": "low",
        }).is_success

        closed = check()["steps"][0]
        assert closed["verified"] is True
        assert closed["observed"] == {"app": "clock", "running": False}
        unavailable = check()
        assert unavailable["status"] == "failed"
        assert "status is unavailable" in unavailable["steps"][0]["error"]
        launch_app.assert_not_called()
