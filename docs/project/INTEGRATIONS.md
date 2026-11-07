# Integrations

## Desktop discovery and service shortcuts

VoiceOS recognizes explicit flight-route requests and “search the public web for”
requests. The desktop provider transport sends a bounded public query to the
configured loopback 9router `/search` endpoint using the stored Chat credential.
The live-tested upstream is Antigravity. The reply shows up to five HTTPS
sources and gives a separate link to look up the route and date. These sources
are web discovery, with no live fare inventory or booking integration.

“Directions from … to …” and “find … on Google Maps” prepare Maps links.
The **Services** button opens browser shortcuts for Gmail, Calendar, Google
Tasks, Drive, Maps, Docs, Sheets, Keep, Contacts, Todoist, Microsoft To Do and
LinkedIn. Shortcuts use the browser's sign-in.

**Todoist** also supports a direct read-only connection. In Voice, open
**Services → Manage Todoist connection** to verify and save a Todoist developer
token using the desktop keyring. “Show my Todoist tasks” reads the first ten
active tasks. The connector displays titles and due dates, keeps the token in
the main process after setup, and does not send tasks to a model or persist
their contents. Disconnect removes the saved token. Other in-app account
reads still need separate per-service integrations and authorization.

**GitHub repository search** is available with “Search GitHub for …”. Without a
saved token it uses GitHub's public REST API. **Services → Manage GitHub MCP
connection** can verify and save a GitHub personal access token for the hosted
GitHub MCP server. The desktop transport negotiates the session, restricts
the server to read-only mode and calls only `search_repositories`. The reply
labels the source and displays up to five public repository names,
descriptions and links. The token stays in desktop storage. Other MCP servers
and toolsets require separate implementations.

## Browser

| Aspect | Detail |
|---|---|
| Components | `BrowserDriver` (interface), `FakeDriver` (tests), `BridgeDriver` (real, via Electron IPC), `BrowserEngine` (refs, gates, effect journal) — all in `actionmesh/agent/browser.py`; Python↔Electron IPC in `agent/browser_ipc.py` + `agent/browser_bridge.py`; Electron side in `desktop/app/browser_window.js`, `browser_channel.js`, `browser_bridge.js` |
| Session isolation | Partition `actionmesh-web-drive` (controlled session); memory-only cookies via `clearStorageData`; never `--no-sandbox` |
| Operations | open, navigate, search, inspect, snapshot, click, type, select, scroll, back, forward, refresh, wait, stop — each a registered `browser.*` action (LOW/MEDIUM risk, `browser.control`) |
| Targeting | Opaque session-scoped `ref` ids (monotonic); never coordinates or model-invented selectors |
| Observation | `PageFacts` (url/title), bounded element snapshots, page text (capped); transient channel for large content |
| Safety | `_http_url` restricts schemes; `_detect_gate` flags login/paywall/OTP gates → `REQUIRES_USER`; no `executeJavaScript` on model-supplied code |
| Tests | `tests/unit/test_browser_agent.py`, `test_browser_bridge.py`, `tests/security/test_browser_security.py`, fixtures `tests/fixtures/{pizza,train,checkout,evil}.html`, `tests/e2e/test_c42_vertical_slices.py` |
| Live acceptance | **Not live-verified** in this pass — fixture/fake-driver tested only |

## Files

| Aspect | Detail |
|---|---|
| Component | `FilesEngine` + `Workspace` in `actionmesh/agent/files.py`; path safety in `agent/paths.py` |
| Operations | `device.files.list/search/read/open/info/create/rename/move/copy/edit/trash` (registered in `agent/definitions.py`) |
| Safety | `_resolve` confines to workspace; `_open_fd` is fd-pinned (symlink/TOCTOU resistant); `_classify` maps escapes → `AUTHORIZATION`, missing → `INVALID_INPUT`; `_relocate_locked` for rename/move |
| Verification | `observe()` re-reads via `info()`; content streaming via transient channel; safe result view strips bodies |
| Trash/open | `_trash_backend` / `_opener_path` resolved at runtime (gio/xdg-open equivalents) |
| Tests | `tests/unit/test_files_agent.py`, `test_agent_paths.py` |
| Live acceptance | Not live-verified in this pass |

## Media

| Aspect | Detail |
|---|---|
| Component | `MediaEngine` (`agent/media.py`): `MediaDriver` interface, `FakeMediaDriver` (tests), `MprisMediaDriver` (real, `org.mpris.MediaPlayer2` over session D-Bus) |
| Operations | `media.status/play/pause/next/previous/volume` (LOW, `media.control`), `media.open_file` (MEDIUM), `media.open_url` (LOW) |
| Data | `PlayerInfo` (track metadata, playback state, volume); `xesam:url` handled as a filesystem path |
| Safety | No metadata dump; never raises or grabs keyboard focus (no `Raise`) |
| Tests | `tests/unit/test_media_agent.py` |
| Live acceptance | **Not live-verified against a real player** — source comment at `media.py:326-328` states the driver was written against the MPRIS 2.0 spec but not exercised live |

## AI providers

| Provider | Where | Status |
|---|---|---|
| Anthropic (chat/reasoning + analysis) | `voiceos/llm.py::AnthropicProvider` (`https://api.anthropic.com/v1/messages`), `AnthropicAnalyst` | Real transport; requires `ACTIONMESH_LLM_API_KEY` + `ACTIONMESH_LLM_MODEL` |
| Desktop bridge (chat/reasoning/analysis) | `voiceos/llm.py::DesktopProvider`/`DesktopAnalyst` via `model_bridge.py`; Electron `desktop/app/provider_bridge.js`, `providers.js` | Real; keys stay in the desktop credential store, never logged |
| OpenAI / Gemini / generic OpenAI-compatible | **Not in this build.** `AVAILABLE_PROVIDERS = frozenset({"anthropic", "desktop"})` | NOT IMPLEMENTED |
| Configured-but-unwired | `UnwiredProvider` — names the gap, proposes nothing | Implemented (honest fallback) |
| Per-purpose lanes | Desktop `provider:*` / `purpose:*` IPC (`docs/c42-per-purpose-providers.md`); purposes: chat, reasoning, image, audio | Implemented in UI + bridge; image/audio lanes depend on the configured provider |
| Deterministic fallback | `core/planner.py` + `voiceos/tasks.py` templates | Implemented; shown as `data-mode="deterministic"` |

**Setup:** configure a provider in the desktop app (Home → Configure chat,
or Settings) or via env (`ACTIONMESH_LLM_PROVIDER=anthropic`,
`ACTIONMESH_LLM_API_KEY=…`, `ACTIONMESH_LLM_MODEL=…`). No key = honest
deterministic mode, never a fabricated plan.

## External services

| Service | Status |
|---|---|
| Real messaging/calendar/files/payments vendors (C13) | **NOT IMPLEMENTED** — `providers/real_base.py` is only the safety boundary. All 16 seed actions are `provider="mock"` (`simulated: true`) |
| Web search | Via `browser.search` (real browsing, untrusted content) |
| Booking/ordering | `booking.submit` / `order.place` definitions + confirmation gates exist; payment execution is mock; not live-verified |
| Android APK | Built+ signed (v2+v3, C17) — `scripts/build_apk.py`; sideload |

## Failure behavior (all integrations)

Handlers raise `ProviderError(message, FailureKind)`; confinement
refusals → `AUTHORIZATION`; missing input → `INVALID_INPUT`; unavailable
backends return `{"status": "unavailable"}` (e.g. `BrowserEngine._unavailable`
before `browser.open`). Verification never self-certifies.
