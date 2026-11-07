"""Audio input is bounded and authenticated, and never becomes an execution."""

import base64
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.voiceos import model_bridge
from actionmesh.voiceos.model_bridge import BridgeConfig


@pytest.fixture
def audio_app(monkeypatch):
    for key in ['ACTIONMESH_AGENT', 'ACTIONMESH_AUDIT_DB', 'ACTIONMESH_DATABASE_URL',
                'ACTIONMESH_VOICEOS_DB', 'ACTIONMESH_AUTOMATIONS_DB']:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('ACTIONMESH_API_KEY', 'audio-api-fixture-key')
    monkeypatch.setenv('ACTIONMESH_ENV', 'development')
    app = create_app()
    monkeypatch.setattr('actionmesh.api.bridge_config_from_env', lambda: BridgeConfig('/fixture', 'fixture'))
    return app


def test_cloud_transcription_is_explicit_and_has_no_persisted_audio_or_actions(audio_app, monkeypatch):
    transcribe = Mock(return_value='List files in my workspace.')
    monkeypatch.setattr(model_bridge, 'transcribe', transcribe)
    client = TestClient(audio_app, client=('127.0.0.1', 12345))
    response = client.post('/voiceos/audio/transcribe', headers={'x-api-key': 'audio-api-fixture-key'},
        json={'audio': base64.b64encode(b'synthetic audio').decode()})
    assert response.status_code == 200
    assert response.json() == {'text': 'List files in my workspace.', 'ran': False, 'stored': False}
    assert transcribe.call_args.args[1] == b'synthetic audio'
    assert audio_app.state.engine.records() == []
    assert getattr(audio_app.state, 'voiceos_store_instance', None) is None
    assert all('synthetic audio' not in event.model_dump_json() for event in audio_app.state.engine.events.history())
    client.close()


@pytest.mark.parametrize('payload', [None, [], {'audio': ''}, {'audio': '%bad'},
    {'audio': 'YQ==', 'grants': ['*']}, {'audio': 'YQ==', 'apiKey': 'not accepted'},
    {'audio': base64.b64encode(b'x' * (128 * 1024 + 1)).decode()}])
def test_invalid_audio_never_reaches_a_provider(audio_app, monkeypatch, payload):
    transcribe = Mock()
    monkeypatch.setattr(model_bridge, 'transcribe', transcribe)
    client = TestClient(audio_app, client=('127.0.0.1', 12345))
    answer = client.post('/voiceos/audio/transcribe', json=payload, headers={'x-api-key': 'audio-api-fixture-key'})
    assert answer.status_code in (400, 415)
    transcribe.assert_not_called()
    client.close()


def test_missing_auth_and_remote_anonymous_callers_cannot_upload(audio_app, monkeypatch):
    transcribe = Mock()
    monkeypatch.setattr(model_bridge, 'transcribe', transcribe)
    with TestClient(audio_app, client=('203.0.113.3', 12345)) as client:
        assert client.post('/voiceos/audio/transcribe', json={'audio': 'YQ=='}).status_code == 401
        assert client.post('/voiceos/audio/transcribe', json={'audio': 'YQ=='},
            headers={'x-api-key': 'audio-api-fixture-key'}).status_code == 401
    transcribe.assert_not_called()


def test_chunked_oversize_is_refused_before_json_decoding(audio_app, monkeypatch):
    transcribe = Mock()
    monkeypatch.setattr(model_bridge, 'transcribe', transcribe)
    client = TestClient(audio_app, client=('127.0.0.1', 12345))
    answer = client.post('/voiceos/audio/transcribe', content=iter([b'x' * 100000, b'x' * 100000]),
        headers={'content-type': 'application/json', 'x-api-key': 'audio-api-fixture-key'})
    assert answer.status_code == 413
    transcribe.assert_not_called()
    client.close()


def test_bridge_audio_frames_are_bounded_and_status_is_projected():
    import json

    from tests.unit.test_model_bridge import _Server

    server = _Server(lambda _raw: json.dumps({"ok": True, "text": "fixture text"}).encode() + b"\n")
    try:
        config = BridgeConfig(server.path, "private-token")
        assert model_bridge.transcribe(config, b"recording") == "fixture text"
        frame = json.loads(server.received[0])
        assert frame["op"] == "transcribe"
        assert base64.b64decode(frame["audio"]) == b"recording"
        assert "private-token" not in model_bridge.transcribe(config, b"recording")
    finally:
        server.close()
    server = _Server(lambda _raw: b'{"ok":true,"configured":true,"apiKey":"never-return-this"}\n')
    try:
        assert model_bridge.audio_status(BridgeConfig(server.path, "private-token")) == {"configured": True, "max_bytes": 131072}
    finally:
        server.close()
