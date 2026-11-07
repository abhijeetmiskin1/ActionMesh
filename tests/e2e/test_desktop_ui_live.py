"""Opt-in Electron navigation, tab, draft and modal accessibility acceptance."""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(os.environ.get("ACTIONMESH_LIVE_UI") != "1", reason="live Electron UI is opt-in")
@pytest.mark.parametrize("width,height", [(1280, 900), (960, 640)])
def test_desktop_ui_navigation_and_forms(tmp_path: Path, width: int, height: int) -> None:
    electron = ROOT / "desktop/node_modules/electron/dist/electron"
    if not electron.exists() or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("Electron and a graphical session are required")
    result = subprocess.run(
        [str(electron), str(ROOT / "tests/e2e/helpers/desktop_ui.cjs")],
        env={**os.environ, "UI_ROOT": str(ROOT), "UI_WIDTH": str(width), "UI_HEIGHT": str(height),
             "UI_QA": str(ROOT / "tests/e2e/helpers/desktop_ui_checks.js"),
             "UI_PREFIX": str(tmp_path / "ui")},
        capture_output=True, text=True, timeout=45,
    )
    failures = [line for line in result.stdout.splitlines()
                if line.startswith(("UI_FAILURE", "UI_ERROR"))]
    records = [json.loads(line.split(" ", 1)[1]) for line in result.stdout.splitlines()
               if line.startswith("UI_QA ")]
    assert result.returncode == 0 and not failures and records, failures or result.stderr
    assert len(records[0]["checks"]) == 5
    assert width <= records[0]["width"] <= width + 32  # Native frame/minimum-size adjustment.
    assert records[0]["overflow"] is False


@pytest.mark.skipif(
    os.environ.get("ACTIONMESH_LIVE_AUDIO") != "1", reason="live synthetic audio is opt-in"
)
def test_desktop_speech_auto_finishes_without_manual_stop(tmp_path: Path) -> None:
    import wave

    source = Path(os.environ.get("ACTIONMESH_LIVE_AUDIO_WAV", ""))
    electron = ROOT / "desktop/node_modules/electron/dist/electron"
    if not source.is_file() or not electron.exists() or not os.environ.get("DISPLAY"):
        pytest.skip("provide a generated WAV and a graphical Electron session")
    wav = tmp_path / "speech-with-pause.wav"
    with wave.open(str(source), "rb") as reader:
        params = reader.getparams()
        frames = reader.readframes(reader.getnframes())
    with wave.open(str(wav), "wb") as writer:
        writer.setparams(params)
        writer.writeframes(frames + b"\x00" * params.framerate * 3 * params.sampwidth * params.nchannels)
    result = subprocess.run(
        [str(electron), str(ROOT / "tests/e2e/helpers/desktop_ui.cjs")],
        env={**os.environ, "UI_ROOT": str(ROOT), "UI_AUDIO_WAV": str(wav),
             "UI_QA": str(ROOT / "tests/e2e/helpers/desktop_voice_pause_checks.js"),
             "UI_PREFIX": str(tmp_path / "pause")},
        capture_output=True, text=True, timeout=45,
    )
    records = [json.loads(line.split(" ", 1)[1]) for line in result.stdout.splitlines()
               if line.startswith("UI_QA ")]
    assert result.returncode == 0 and records, result.stdout
    assert 0 < records[0]["recordingMs"] < 8000
    assert records[0]["editable"] is True
    assert records[0]["policyDenied"] is True
    assert records[0]["executionCount"] == 0


@pytest.mark.skipif(os.environ.get("ACTIONMESH_LIVE_TTS") != "1", reason="live local speech is opt-in")
def test_desktop_local_speech_playback(tmp_path: Path) -> None:
    electron = ROOT / "desktop/node_modules/electron/dist/electron"
    if not electron.exists() or not os.environ.get("DISPLAY"):
        pytest.skip("Electron and a graphical Linux speech session are required")
    result = subprocess.run(
        [str(electron), str(ROOT / "tests/e2e/helpers/desktop_ui.cjs")],
        env={**os.environ, "UI_ROOT": str(ROOT),
             "UI_QA": str(ROOT / "tests/e2e/helpers/desktop_speech_checks.js"),
             "UI_PREFIX": str(tmp_path / "speech")},
        capture_output=True, text=True, timeout=45,
    )
    records = [json.loads(line.split(" ", 1)[1]) for line in result.stdout.splitlines()
               if line.startswith("UI_QA ")]
    assert result.returncode == 0 and records, result.stdout
    assert records[0]["completed"] == ["English", "Hindi"]
    assert records[0]["stopWorks"] is True
    assert records[0]["localVoices"] > 0
