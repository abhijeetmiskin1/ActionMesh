# Development Guide

## Prerequisites (from actual config)

| Requirement | Source |
|---|---|
| Python **≥ 3.11** (verified on 3.12.10 and 3.14 per `PROJECT.md`) | `pyproject.toml` `requires-python` |
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
| Browser logs/snapshots | `browser:snapshot` / `browser:page_text` IPC; `PageFacts` |
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
