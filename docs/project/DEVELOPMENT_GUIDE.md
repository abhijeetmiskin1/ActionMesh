# Development Guide

## Prerequisites (from actual config)

| Requirement | Source |
|---|---|
| Python **≥ 3.11** (verified on 3.12.10 and 3.14 per `PROJECT.md`/`HANDOFF.md`) | `pyproject.toml` `requires-python` |
| Node.js (Electron 45.2.1) | `desktop/package.json` |
| Package managers | `pip` (Python), `npm` (desktop), setuptools build backend |
| Runtime dependency | `fastapi>=0.115` only (`pyproject.toml`) |
| Dev dependencies | `pytest>=8.0`, `ruff>=0.5`, `mypy>=1.10`, `httpx>=0.27` (`pip install -e ".[dev]"`) |
| Server dependency | `uvicorn>=0.30` (`pip install -e ".[server]"`) |
| Database | SQLite — **standard library**, no server |
| OS dependencies | Linux D-Bus session bus (media MPRIS), `xdg-open`/gio equivalents (file open/trash) |
| Browser | Electron's built-in Chromium (no separate browser install) |

## Setup

```bash
# Python backend
python -m venv .venv
. .venv/bin/activate              # Linux/macOS; Windows: .\.venv\Scripts\python.exe
pip install -e ".[dev]"           # pytest, ruff, mypy, httpx
pip install -e ".[server]"        # uvicorn — needed to run the server

# Desktop app
cd desktop
npm install                       # electron + electron-builder

# Configuration — copy the template and fill in what you need
cp .env.example .env              # 33 ACTIONMESH_* vars; all optional, all fail-closed
```

## Startup

```bash
# Backend (http://127.0.0.1:8000)
python -m actionmesh.cli serve
# or: actionmesh serve --host 127.0.0.1 --port 8080

# Health check + dashboard
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/dashboard

# Desktop (starts Electron; main process supervises the backend)
cd desktop && npm start           # node scripts/start.js
```

**Device/agent pairing:** the pairing code is minted **on the laptop**
(`POST /pairing/code`); the ceiling (max risk + scopes) is fixed at
issuance and cannot be redeemed upward. Remote pairing requires
`ACTIONMESH_ALLOW_REMOTE_PAIRING=1`.

**Workspace authorization:** set `ACTIONMESH_WORKSPACE_ROOT` to the
directory the files agent may touch. Unset ⇒ no file capability
(fail-closed). Shell access requires `ACTIONMESH_ALLOW_INTERPRETER_CODE`
plus `ACTIONMESH_ALLOWED_COMMANDS` / `ACTIONMESH_DENIED_COMMANDS` /
`ACTIONMESH_ALLOWED_WORKDIRS`.

## Development

### Desktop speech input with 9router

Speech input uses the **Audio** provider lane in Settings. Select
`9router / compatible speech-to-text`, enter the router API base and a working
transcription model, and save the API key into the OS keyring. Test Connection
sends 100 ms of synthetic silence to `/audio/transcriptions`; it never records
the microphone. Chat and Reasoning use the separate compatible text-provider
lanes and `/chat/completions`.

On 2026-11-05 this machine's `/v1/models` timed out, while the same router's
`http://127.0.0.1:20128/api/v1/models` answered. The saved base consequently uses
`http://127.0.0.1:20128/api/v1`. The `power` route returned a valid structured
proposal; `groq/whisper-large-v3-turbo` transcribed generated spoken audio.
These are observed working choices, not a guarantee about every advertised model
or future provider availability. No key is included in this documentation.

Remote provider endpoints require HTTPS on port 443. HTTP is accepted only for
loopback addresses; `localhost` is pinned to `127.0.0.1`. Redirects are refused.
The renderer captures WebM/Opus only after a microphone click, stops after 30
seconds or 2 MiB, and releases microphone tracks before upload. Cancel discards
pending results; a recording already uploaded cannot be recalled from the provider.
The transcript remains editable and requires Send before the existing proposal
flow. Dictation never grants permissions, approves an action, or executes a plan.

The updated desktop build must be restarted to load code and saved settings.
Browser-native recognition remains a fallback only where cloud capture APIs are
absent; its network error does not establish that the laptop is offline.

### Web Voice cloud dictation

With the desktop's managed backend running, open its Voice page and configure
AI Providers → Audio. A microphone press checks configuration, opens one capture,
and uploads WebM/Opus through the authenticated audio endpoints. The web limit is
30 seconds / 128 KiB; the desktop's direct IPC capture keeps its separate 2 MiB
limit. The private bridge frame limit remains 256 KiB. Dictation returns editable
text and requires Ask VoiceOS. Camera and cross-origin microphone requests are
denied in the desktop web-page session.

Opt-in acceptance uses a generated WAV as Chromium's fake microphone input and
the real stored Audio provider. It never records physical microphone audio:

```bash
ACTIONMESH_LIVE_AUDIO=1 ACTIONMESH_LIVE_AUDIO_WAV=/absolute/path/to/synthetic.wav .venv/bin/python -m pytest tests/e2e/test_web_voice_cloud_live.py -q
```

Add `ACTIONMESH_LIVE_PACKAGED=1` to exercise the current assembled package's
bundled Python runtime and ASAR provider bridge instead of the development files.


### Image generation and inspected-window explanation

The Home image card uses AI Providers → Image → `9router / compatible images` and
`/images/generations`. On this router the working model is
`cloudflare-ai/@cf/black-forest-labs/flux-2-klein-4b`. The adapter requests one
512×512 raster, accepts bounded inline PNG/JPEG/WebP only, and refuses remote
image URLs. The preview remains local memory; Clear removes it and discards late
responses. Test Connection generates a small image and may incur provider usage.

Inspect a window in Screen session before asking a question. Pressing Ask first
reveals the question and disclosure; submitting explicitly sends up to 100
bounded role/name/enabled records to the configured Reasoning provider. Values,
bounds and screenshot pixels are excluded. Answers are suggestions and do not
execute anything. Clear observation discards pending answers for that inspection.
The capture-details card reports metadata; it does not render screenshot pixels.

### Native Linux desktop bindings in packaged builds

The bundled CPython stays isolated from the system site-packages. Compiled
PyGObject has the host Python's ABI, so the Linux shell probes `/usr/bin/python3`
against the shipped `agent/device_screen.py` before enabling a fixed host adapter.
It runs only the existing capture/inspect/click/type/scroll functions after the
normal policy/approval path. Parameters are bounded JSON, never executable code;
only named session environment variables are forwarded, with no backend or
provider credentials. The existing agent runner enforces timeouts and output caps.

Install the package with `apt install --reinstall /absolute/path/to/ActionMesh.deb` as an
administrator so recommended `python3-gi` and `gir1.2-atspi-2.0` can be resolved.
Enable desktop access in Settings, restart the backend, and grant the required
scopes in Approvals. A missing host binding remains unavailable. A named
application that is not on the accessibility bus is refused without reading other
applications. Generic GUI and portal capture receipts remain unverified when no
independent effect observation exists.

```bash
ACTIONMESH_LIVE_ACCEPTANCE=1 .venv/bin/python -m pytest tests/e2e/test_native_desktop_live.py -q
```

This opt-in acceptance owns a temporary GTK window, inspects only that named
application, clicks its test button and types into its test field. It never
captures the user's desktop or operates another application.

### Live local browser check

```bash
ACTIONMESH_LIVE_ACCEPTANCE=1 .venv/bin/python -m pytest tests/e2e/test_browser_live_controls.py -q
```

This opt-in check starts the real Electron drive with an isolated temporary
profile and local HTTP page. It runs through the existing execution/policy path,
verifies navigation and snapshot read-back, and witnesses click/type/select
without upgrading their generic effects to independently verified success.
It does not use user cookies or external websites. Electron and a desktop display
must be available; no sandbox-disabling argument is used.

### Add a capability

Follow `docs/c42-execution-contract.md` §1 — the registration pattern is
authoritative:

1. **Definition** in `actionmesh/agent/definitions.py`:
   `_def(name, risk, permissions, properties, required, description,
   timeout_s=…, max_attempts=1)`. Risk is set here, here only;
   permissions must be concrete catalog scopes; every param not listed
   is refused.
2. Register the name in `CAPABILITIES` and (if implemented) `IMPLEMENTED`
   and the relevant `REQUIRING_*` gate. `register_device_actions`
   refuses anything outside `IMPLEMENTED`.
3. **Handler** in `actionmesh/agent/provider.py::LaptopProvider`: add
   the id to `SUPPORTED` (and `ASYNC_ACTIONS` if it blocks);
   sync → branch in `_perform`; async → entry in `_execute_async`.
   Return a plain dict; record `self._effects[action.action_id]`;
   raise `ProviderError(msg, FailureKind.X)` on failure. **Do not touch
   `verify()`.**
4. Add a focused test (see §Testing).

### Add an engine operation / permission / API route

- **Engine operation:** provider handler (above) — never a second
  executor.
- **Permission:** add the scope to `core/permissions/catalog.py`
  `_SCOPES` with a `RiskLevel`; grants match exactly
  (`core/permissions/matching.py`); namespaces (`scope.*`) are
  policy-rule-only.
- **API route:** add to `actionmesh/api.py`; it must go through
  `_context()` (which rejects client-supplied grants) and actor scoping
  (`_record_scope`). Check `docs/rest-api.md` for conventions.
- **VoiceOS task:** add a `TaskTemplate` in `voiceos/tasks.py` (or a
  workflow in `workflows.py`); steps are ordinary graphs — no new
  execution path.
- **Electron screen:** add a `data-view` section in
  `desktop/ui/index.html`, wire it in `desktop/ui/app.js`, ship IPC
  through `preload.js` (single bridge) + a handler in `main.js`.
- **IPC channel:** `ipcMain.handle` in `main.js`, expose in
  `preload.js`, call via `window.actionmesh`. Never expose `ipcRenderer`
  directly.

## Debugging

| Symptom | Where to look |
|---|---|
| Backend logs | Backend stdout (or `backend:logs` IPC in the desktop app) |
| Electron logs | DevTools in the renderer; main-process stdout |
| Browser observations | `browser:snapshot` through `/execute`; actor-scoped `browser.*` output streams (`page_text` has no registered renderer capability) |
| Event streams | `GET /events` (ticket from `GET /events/stream-ticket`); `EventHub.transient(execution_id)` for live output |
| Failed execution | `GET /executions/{id}`; `GET /audit/verify`; `POST /voiceos/sessions/{id}/diagnose` |
| Verification failures | `VerificationOutcome` in the result; `NodeOutcome.verified` |
| Credential problems | `GET /voiceos/status`; desktop provider status (`provider:status`); `actionmesh doctor` CLI |

## Testing

```bash
python -m pytest tests -q                 # full suite (do not rerun casually)
python -m pytest tests/security -q        # security invariants
python -m pytest tests/unit/test_xxx.py -q  # one file
ruff check actionmesh tests scripts
mypy actionmesh
cd desktop && npm test
```

See [TESTING_AND_ACCEPTANCE.md](TESTING_AND_ACCEPTANCE.md) for suite map
and historical counts.

## Packaging

```bash
cd desktop
npm run pack              # electron-builder --dir (unpackaged)
npm run dist:linux        # deb
npm run dist:linux-all    # deb + AppImage + rpm
npm run dist:win          # nsis + msi
npm run dist:mac          # dmg (x64 + arm64)

python scripts/build_apk.py --url http://<lan-ip>:8080 --version-name 0.1.0-lan
adb install -r dist/actionmesh-0.1.0-lan.apk
python scripts/snapshot.py   # offline zip backup
```


### Live voice-understanding acceptance

The live test reads saved credentials through Electron safeStorage and sends only
synthetic requests through an isolated backend/profile. It never executes proposals:

```bash
ACTIONMESH_LIVE_VOICE_MODEL=1 .venv/bin/python -m pytest tests/e2e/test_voice_intelligence_live.py -q
```

The tested voice model is `groq/openai/gpt-oss-120b` through the existing 9router
compatible endpoint. This does not require fine-tuning. The Audio lane remains
`groq/whisper-large-v3-turbo`. Test model quality and latency on actual commands before
switching routes; a connected probe alone does not verify understanding or plan shape.

Add `ACTIONMESH_LIVE_PACKAGED=1` to run this acceptance against the rebuilt package.


### Linux speech playback

The app enables Chromium's local Speech Dispatcher integration before startup.
Linux requires `speech-dispatcher`, `speech-dispatcher-espeak-ng`, and `libspeechd2`
(recommended by the Debian installer). This is local speech output; Audio provider
configuration continues to control speech recognition. English and Hindi replies
select a matching local voice when installed. Run the synthetic playback acceptance:

```bash
ACTIONMESH_LIVE_TTS=1 .venv/bin/python -m pytest tests/e2e/test_desktop_ui_live.py -k local_speech -q
```
