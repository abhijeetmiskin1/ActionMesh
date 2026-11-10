"""Compare workspace files through the real local provider and verifier."""

from fastapi.testclient import TestClient

from actionmesh.agent.files import MAX_READ_BYTES, FilesEngine
from actionmesh.api import create_app


def test_workspace_comparison_is_verified_confined_and_content_free(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONMESH_AGENT", "1")
    monkeypatch.setenv("ACTIONMESH_WORKSPACE_ROOT", str(tmp_path))
    left = tmp_path / "release notes.txt"
    right = tmp_path / "candidate.txt"
    left.write_text("release v1")
    right.write_text("release v1")
    actor = {"id": "voiceos-local"}
    with TestClient(create_app(), client=("127.0.0.1", 12345)) as client:
        def compare(target="candidate.txt"):
            proposal = client.post("/voiceos/messages", json={
                "actor": actor, "text": f'Compare workspace files "release notes.txt" and "{target}"',
            }).json()
            assert proposal["kind"] == "task"
            result = client.post(f"/voiceos/sessions/{proposal['session_id']}/run", json={
                "actor": actor, "stop_on_unverified": True,
            }).json()
            assert "release v" not in str(result)
            return result["steps"][0]

        assert compare()["state"] == "denied"
        assert client.post("/consents", json={
            "actor_id": actor["id"], "scope": "file.read", "max_risk": "medium",
        }).is_success
        match = compare()
        assert match["verified"] is True
        assert match["observed"] == {"changed": False}
        right.write_text("release v2")
        difference = compare()
        assert difference["verified"] is True
        assert difference["observed"] == {"changed": True}

        for path in ("../outside.txt", "missing.txt"):
            assert compare(path)["state"] == "failed"
        right.write_bytes(b"x" * (MAX_READ_BYTES + 1))
        assert compare()["state"] == "failed"

        right.write_text("release v1")
        original = FilesEngine.compare

        def change_after_read(self, first, second):
            changed = original(self, first, second)
            right.write_text("release v2")
            return changed

        monkeypatch.setattr(FilesEngine, "compare", change_after_read)
        unstable = compare()
        assert unstable["state"] == "failed"
        assert "changed during independent verification" in unstable["error"]
