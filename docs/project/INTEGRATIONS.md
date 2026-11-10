# Integrations

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

## VoiceOS assistant capability review

| User goal | Current coverage | Missing capability |
| --- | --- | --- |
| Control the Mac | Supported app launches and running-status checks have live macOS read-back. Workspace, process and editor actions have separate configuration gates. | Broader native app operations need typed capabilities and independent observations. |
| Complete a request across tools | A command joined with “then” can combine supported Mac app commands, named browser pages and deal discovery. Automatic execution follows the ordered steps and stops on policy or verification failures. | The system cannot yet choose a new tool or revise its plan based on intermediate results. |
| Research and compare products | Browser search can verify navigation and count result links. | Verified comparisons require structured product identity, seller, price, currency, delivery, availability and time of observation. |
| Use GitHub or Google Workspace | Public GitHub repository and open-issue reads use the official API. Drive name search uses the metadata endpoint. | Live private GitHub and Drive acceptance requires scoped account credentials. |
| Place an order | The execution system has a critical approval gate and a submit action. | A retailer adapter must observe the exact cart and total, honor approval for the specific action and verify the resulting receipt. |
| Continue after an approval | The task store keeps the link to a pending automatic approval. A reopened desktop can recover the link and continue after the approved step verifies. | Continuation is initiated by the desktop approval screen and its recovery checks the latest 100 tasks. |

Keep tool calls inside the existing registry, policy, executor and verification
path. Build each external integration around a small set of typed capabilities
and test success, denial, unavailable service and incomplete verification. This
follows the distinction between ordered workflows and adaptive agents in
[Building effective agents](https://www.anthropic.com/research/building-effective-agents).
The [MCP tool interface](https://csharp.sdk.modelcontextprotocol.io/v1/concepts/tools/tools.html)
can be a connector boundary while local policy controls which operations are exposed.

### GitHub and Google Drive reads

- In desktop Settings, enable the desired read-only connector and restart the backend after saving. Grant `github.read` or `google.drive.read` at low risk in Approvals.
- Public GitHub reads do not need a token. For private issues, use a fine-grained token restricted to the intended repositories with Issues read access. Repository discovery searches public repositories only.
- Google Drive needs an OAuth access token from a Google Cloud project with the Drive API enabled and the `https://www.googleapis.com/auth/drive.metadata.readonly` scope. Enter it in the Google Drive password field in Settings. Refresh the access token when it expires. The adapter lists file-name matches by count and links to the Drive search. It does not fetch file bodies or retain file IDs.
- The equivalent headless configuration uses `ACTIONMESH_ALLOW_GITHUB_READ`, `ACTIONMESH_ALLOW_DRIVE_READ`, `ACTIONMESH_GITHUB_TOKEN`, and `ACTIONMESH_GOOGLE_DRIVE_TOKEN`.
- Each step reads at most ten entries and verifies page membership with a second independent read. “Find GitHub repository named NAME, then show its open issues” continues only when one public repository is verified. Ambiguous or empty results leave the issues step skipped.
