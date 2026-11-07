"""The packaged host binding adapter is fixed, bounded, and credential-free."""

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from actionmesh.agent import device_screen, native_desktop
from actionmesh.agent.runner import CommandOutcome


def test_in_process_path_still_uses_the_existing_adapter(monkeypatch) -> None:
    monkeypatch.delenv("ACTIONMESH_NATIVE_DESKTOP", raising=False)
    with patch.object(device_screen, "inspect_gui", return_value={"status": "ok", "elements": []}) as inspect:
        answer = asyncio.run(native_desktop.call("inspect", {"window": "test-app", "max_elements": 10}))
    assert answer["status"] == "ok"
    inspect.assert_called_once_with(window="test-app", max_elements=10)


def test_native_transport_uses_only_fixed_code_and_a_small_session_environment(monkeypatch) -> None:
    monkeypatch.setattr(native_desktop, "enabled", lambda: True)
    monkeypatch.setenv("ACTIONMESH_API_KEY", "do-not-send-backend-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-send-provider-secret")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/modules")
    mock = AsyncMock(return_value=CommandOutcome(exit_code=0, stdout='{"status":"ok","typed":true}'))
    with patch.object(native_desktop.CommandRunner, "run", mock):
        answer = asyncio.run(native_desktop.call("type", {"role": "text", "name": "Test", "text": "private message"}))
    assert answer["typed"]
    decision = mock.call_args.args[0]
    assert decision.resolved_path == "/usr/bin/python3"
    assert decision.arguments[0] == "-I"
    assert decision.arguments[-1] == "--native-request"
    assert "private message" not in repr(decision.arguments)
    assert "ACTIONMESH_API_KEY" not in decision.environment
    assert "OPENAI_API_KEY" not in decision.environment
    assert "PYTHONPATH" not in decision.environment
    assert set(decision.environment) <= {*native_desktop.SESSION_ENV, "ACTIONMESH_NATIVE_REQUEST"}
    assert json.loads(decision.environment["ACTIONMESH_NATIVE_REQUEST"])["parameters"]["text"] == "private message"


@pytest.mark.parametrize("operation,parameters", [
    ("eval", {"code": "anything"}), ("capture", {"command": "anything"}),
    ("type", {"role": "text", "name": "Test", "text": "x" * 16001}),
    ("inspect", {"max_elements": True}), ("inspect", {"window": "x" * 501}),
    ("scroll", {"role": "text", "name": "Test", "direction": "execute"}),
])
def test_invalid_native_requests_never_start_a_helper(monkeypatch, operation, parameters) -> None:
    monkeypatch.setattr(native_desktop, "enabled", lambda: True)
    with patch.object(native_desktop.CommandRunner, "run", AsyncMock()) as run:
        assert asyncio.run(native_desktop.call(operation, parameters))["status"] == "invalid"
    run.assert_not_called()


@pytest.mark.parametrize("outcome", [
    CommandOutcome(exit_code=0, stdout="private invalid output"),
    CommandOutcome(exit_code=0, stdout='{"status":"ok"}', truncated=True),
    CommandOutcome(exit_code=None, timed_out=True),
    CommandOutcome(exit_code=1, stderr="secret stderr"),
])
def test_incomplete_native_answers_fail_closed_without_echoing_content(monkeypatch, outcome) -> None:
    monkeypatch.setattr(native_desktop, "enabled", lambda: True)
    with patch.object(native_desktop.CommandRunner, "run", AsyncMock(return_value=outcome)):
        answer = asyncio.run(native_desktop.call("capture", {}))
    assert answer["status"] == "unavailable"
    assert "private" not in repr(answer)
    assert "secret" not in repr(answer)


@pytest.mark.skipif(not Path(native_desktop.PYTHON).is_file(), reason="Linux host Python is unavailable")
def test_standalone_helper_refuses_unknown_operations_without_loading_application() -> None:
    helper = Path(device_screen.__file__)
    answer = subprocess.run([native_desktop.PYTHON, "-I", str(helper), "--native-request"],
        env={"ACTIONMESH_NATIVE_REQUEST": json.dumps({"operation": "eval", "parameters": {"code": "canary"}})},
        capture_output=True, text=True, timeout=5)
    assert answer.returncode == 0
    assert json.loads(answer.stdout)["status"] == "unavailable"
    assert "canary" not in answer.stdout + answer.stderr


def test_named_inspection_never_falls_back_to_other_applications() -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    other = Mock()
    other.get_name.return_value = "some-other-app"
    desktop = Mock()
    desktop.get_child_count.return_value = 1
    desktop.get_child_at_index.return_value = other
    atspi = SimpleNamespace(init=lambda: None, get_desktop=lambda _index: desktop)
    gi = Mock()
    repository = SimpleNamespace(Atspi=atspi)
    with patch.object(device_screen, "_gi_available", return_value=True), \
         patch.object(device_screen, "_atspi_available", return_value=True), \
         patch.dict("sys.modules", {"gi": gi, "gi.repository": repository}):
        answer = device_screen.inspect_gui("missing-test-app")
    assert answer["status"] == "window_not_found"
    assert answer["elements"] == []
    other.get_child_count.assert_not_called()


@pytest.mark.parametrize("kind,receipt", [
    ("device.gui.click", {"dispatched": True, "state_changed": True}),
    ("device.gui.type", {"typed": True, "inserted": 9}),
    ("device.gui.scroll", {"status": "scrolled", "moved": True, "offset_after": 10}),
    ("device.screen.capture", {"status": "captured", "width": 100, "height": 100}),
])
def test_gui_and_capture_receipts_cannot_certify_their_own_effects(kind, receipt) -> None:
    from actionmesh.agent.provider import LaptopProvider
    from actionmesh.agent.verification import register_device_strategies
    from actionmesh.core.verification import default_engine
    from actionmesh.protocol.models import Action
    from actionmesh.providers.base import ProviderResult

    provider = LaptopProvider(None)
    action = Action(type=kind, provider="laptop", parameters={"role": "button", "name": "Fixture"})
    provider._effects[action.action_id] = {"action_type": kind,
        "idempotency_key": action.idempotency_key, "role": "button", "name": "Fixture", **receipt}
    engine = default_engine()
    register_device_strategies(engine)
    answer = engine.verify(action, ProviderResult(ok=True, action_id=action.action_id,
        provider="laptop", simulated=False, data=receipt), provider)
    assert not answer.verified


def test_inspection_verification_re_reads_the_native_adapter() -> None:
    from actionmesh.agent.provider import LaptopProvider
    from actionmesh.protocol.models import Action

    provider = LaptopProvider(None)
    action = Action(type="device.screen.inspect", parameters={"window": "Fixture"})
    provider._effects[action.action_id] = {"action_type": action.type, "status": "ok", "count": 99}
    mock = AsyncMock(return_value={"status": "ok", "window": "Fixture", "element_count": 3,
        "elements": [{"name": "private observation"}]})
    with patch.object(native_desktop, "call", mock):
        observation = provider.observe(action)
    assert observation["state"]["elements"] == 3
    assert "private observation" not in repr(observation)
    mock.assert_called_once()


@pytest.mark.parametrize("response,expected", [
    (0, "captured"), (1, "cancelled"), (2, "cancelled"),
])
def test_capture_subscribes_before_request_and_reads_portal_uri(tmp_path, response, expected) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    frame = tmp_path / "test frame.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(8) + b"\0\0\0\x02\0\0\0\x03")
    bus = Mock()
    bus.get_unique_name.return_value = ":1.4"
    bus.signal_subscribe.return_value = 7
    loop = Mock()

    def request(*args):
        token = args[4][1][1]["handle_token"][1]
        handle = f"/org/freedesktop/portal/desktop/request/1_4/{token}"
        assert bus.signal_subscribe.call_args.args[3] == handle
        callback = bus.signal_subscribe.call_args.args[6]
        params = Mock()
        params.unpack.return_value = (response, {"uri": frame.as_uri()})
        loop.run.side_effect = lambda: callback(None, None, handle, None, "Response", params)
        return Mock(unpack=lambda: (handle,))

    bus.call_sync.side_effect = request
    gio = SimpleNamespace(
        bus_get_sync=lambda *_: bus, BusType=SimpleNamespace(SESSION=1),
        DBusSignalFlags=SimpleNamespace(NONE=0), DBusCallFlags=SimpleNamespace(NONE=0),
    )
    glib = SimpleNamespace(
        MainLoop=lambda: loop, Variant=lambda *args: args, VariantType=lambda value: value,
        timeout_add_seconds=lambda *_: 3, source_remove=Mock(), Error=RuntimeError,
    )
    with patch.object(device_screen, "_gi_available", return_value=True), \
         patch.object(device_screen, "_desktop_session", return_value="wayland"), \
         patch.object(device_screen, "_portal_available", return_value=True), \
         patch.dict("sys.modules", {"gi": Mock(), "gi.repository": SimpleNamespace(Gio=gio, GLib=glib)}):
        answer = device_screen.capture_screenshot()
    assert answer["status"] == expected
    if expected == "captured":
        assert answer["file"]["width"] == 2
        assert answer["file"]["height"] == 3
    else:
        assert "uri" not in answer
    bus.signal_unsubscribe.assert_called_once_with(7)
    glib.source_remove.assert_called_once_with(3)


def test_portal_timeout_fails_the_action_with_a_user_facing_reason() -> None:
    from actionmesh.agent.provider import LaptopProvider
    from actionmesh.protocol.models import Action, FailureKind
    from actionmesh.providers.base import ProviderError

    detail = "the screenshot portal did not respond before the deadline"
    response = {"status": "cancelled", "portal_code": -1, "detail": detail}
    with (
        patch.object(native_desktop, "call", AsyncMock(return_value=response)),
        pytest.raises(ProviderError, match=detail) as exc,
    ):
        asyncio.run(LaptopProvider(None).execute(Action(type="device.screen.capture")))
    assert exc.value.kind is FailureKind.TIMEOUT
