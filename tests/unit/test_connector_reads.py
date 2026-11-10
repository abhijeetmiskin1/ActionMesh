"""Read-only connector acceptance through VoiceOS, policy and the laptop verifier."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from actionmesh.agent import connectors
from actionmesh.api import create_app
from actionmesh.protocol.models import Action
from actionmesh.providers.base import ProviderError
from actionmesh.voiceos import transport
from actionmesh.voiceos.transport import ReadTransportError, TransportError, checked_endpoint


def test_connector_destinations_are_fixed() -> None:
    assert checked_endpoint("https://api.github.com/search/issues", read=True)[0] == "api.github.com"
    for endpoint in (
        "https://api.github.com/repos/other/repo",
        "https://api.github.com/search/issues?redirect=example.com",
        "https://attacker.example/search/issues",
    ):
        with pytest.raises(TransportError):
            checked_endpoint(endpoint, read=True)
    with pytest.raises(TransportError):
        checked_endpoint("https://api.github.com/search/issues")


@pytest.mark.parametrize(
    "status, message",
    [(401, "rejected the credential"), (403, "refused access"),
     (429, "rate limit"), (503, "HTTP 503")],
)
def test_connector_maps_http_errors(monkeypatch, status, message) -> None:
    monkeypatch.setattr(transport, "get_json", AsyncMock(side_effect=ReadTransportError(status)))
    with pytest.raises(ProviderError, match=message):
        asyncio.run(connectors.read(Action(type=connectors.GITHUB_ISSUES, parameters={"repository": "o/r"})))


def test_drive_query_is_metadata_only_and_reports_more_pages(monkeypatch) -> None:
    monkeypatch.delenv(connectors.DRIVE_TOKEN, raising=False)
    action = Action(type=connectors.DRIVE_SEARCH, parameters={"query": "O'Brien"})
    request = AsyncMock(return_value={"files": [{"id": "private-id", "name": "private"}],
                                      "nextPageToken": "private-token"})
    monkeypatch.setattr(transport, "get_json", request)
    with pytest.raises(ProviderError, match="missing credential"):
        asyncio.run(connectors.read(action))
    request.assert_not_awaited()

    monkeypatch.setenv(connectors.DRIVE_TOKEN, "fixture-only-token")
    result = asyncio.run(connectors.read(action))
    assert result["count"] == 1 and result["truncated"] is True
    assert "private" not in str(result)
    query = request.await_args.kwargs["query"]
    assert "O\\'Brien" in query["q"]
    assert query["fields"] == "nextPageToken,incompleteSearch,files(id)"


def test_repository_result_can_supply_the_next_verified_step(monkeypatch) -> None:
    monkeypatch.setenv("ACTIONMESH_AGENT", "1")
    monkeypatch.setenv("ACTIONMESH_ALLOW_GITHUB_READ", "1")
    monkeypatch.delenv(connectors.GITHUB_TOKEN, raising=False)
    repo = {"total_count": 1, "items": [{"id": 1, "full_name": "owner/demo", "private": False}]}
    issues = {"total_count": 0, "items": []}
    request = AsyncMock(side_effect=[repo, repo, issues, issues, issues, issues, repo, issues, repo, repo])
    monkeypatch.setattr(transport, "get_json", request)
    actor = {"id": "voiceos-local"}

    with TestClient(create_app(), client=("127.0.0.1", 12345)) as client:
        def run(text: str, **options) -> dict:
            proposal = client.post("/voiceos/messages", json={"actor": actor, "text": text}).json()
            return client.post(f"/voiceos/sessions/{proposal['session_id']}/run", json={
                "actor": actor, "stop_on_unverified": True, **options,
            }).json()

        command = "Find GitHub repository named demo, then show its open issues"
        denied = run(command)
        assert denied["steps"][0]["state"] == "denied"
        assert denied["steps"][1]["state"] == "pending"
        request.assert_not_awaited()
        assert client.post("/consents", json={
            "actor_id": actor["id"], "scope": "github.read", "max_risk": "low",
        }).is_success

        found = run(command)
        assert found["status"] == "completed", found
        assert all(step["verified"] for step in found["steps"])
        assert found["steps"][1]["parameters"] == {"repository": "owner/demo"}
        assert found["steps"][1]["observed"]["count"] == 0

        empty = run(command)
        assert empty["steps"][1]["state"] == "skipped"
        assert request.await_count == 6

        changed = run(command)
        assert changed["status"] == "failed"
        assert changed["steps"][1]["state"] == "pending"

        denied_later = run(command, policy={"rules": [
            {"action": "github.repositories.search", "decision": "allow"},
            {"action": "github.issues.list", "decision": "deny"},
        ]})
        assert denied_later["steps"][0]["verified"] is True
        assert denied_later["steps"][1]["state"] == "denied", denied_later["steps"][1]["error"]
        assert request.await_count == 10
