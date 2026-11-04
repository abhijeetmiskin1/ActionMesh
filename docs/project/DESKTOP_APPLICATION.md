# Desktop Application

## Architecture

- **Main process** — `desktop/app/main.js`: window management, backend
  process lifecycle, provider bridge, browser window. 42 `ipcMain.handle`
  channels. `webPreferences`: `contextIsolation: true`,
  `nodeIntegration: false`, `sandbox: true` (lines 242, 953, 1016).
- **Renderer** — `desktop/ui/app.js` + `index.html` + `voice.js`: UI
  only, no Node access.
- **Preload** — `desktop/app/preload.js`: a **single**
  `contextBridge.exposeInMainWorld('actionmesh', …)` exposing
  `backend:`, `browser:`, `provider:`, `settings:`, `window:` namespaces
  (plus voice/stream channels). This is the entire renderer↔main attack
  surface.
- **Backend connection** — `desktop/app/backend.js`: spawns/supervises the
  Python backend; IPC `backend:start/stop/restart/logs/open-data-dir`,
  `app:state`, `api:request`. The backend can be restarted from the UI.
- **Provider bridge** — `desktop/app/provider_bridge.js`,
  `providers.js`, `settings.js`: credential storage and per-purpose
  provider lanes (`provider:configure/model/remove/specs/status/test`,
  `purpose:configure/default/model/remove/specs/status/test`). Keys never
  reach Python logs or audit records.
- **Browser window** — `browser_window.js` (partition
  `actionmesh-web-drive`, hardened prefs, same sandbox posture as main
  windows), `browser_channel.js`, `browser_bridge.js`: 17 `browser:*`
  channels (open/navigate/search/inspect/snapshot/click/type/select/
  scroll/back/forward/refresh/wait/stop/state/page_text).
- **Voice controls** — `desktop/ui/voice.js`: speech recognition,
  transcript, spoken feedback. Mic text is the only thing sent to the
  backend.
- **Packaging** — electron-builder (`desktop/package.json` `build`):
  deb (Linux), NSIS/MSI (Windows), dmg (macOS). `appId:
  com.actionmesh.voiceos`, output `release/`.

## IPC channels (42, from `ipcMain.handle` in `desktop/app/*.js`)

`api:request`, `app:state`, `backend:logs`, `backend:open-data-dir`,
`backend:restart`, `backend:start`, `backend:stop`, `browser:back`,
`browser:click`, `browser:forward`, `browser:inspect`, `browser:navigate`,
`browser:open`, `browser:page_text`, `browser:refresh`, `browser:scroll`,
`browser:search`, `browser:select`, `browser:snapshot`, `browser:state`,
`browser:stop`, `browser:type`, `browser:wait`, `external:open`, `notify`,
`provider:configure`, `provider:model`, `provider:remove`,
`provider:specs`, `provider:status`, `provider:test`,
`purpose:configure`, `purpose:default`, `purpose:model`, `purpose:remove`,
`purpose:specs`, `purpose:status`, `purpose:test`, `settings:update`,
`stream:start`, `stream:stop`, `window:about`, `window:clear-web-session`,
`window:close`, `window:minimize`, `window:open-web`,
`window:toggle-maximize`.

## Screens and navigation (`desktop/ui/index.html`)

| View | Purpose | Key controls |
|---|---|---|
| Home (`data-view="home"`) | Stats (devices/backend/approvals/audit), ask box, recent executions/sessions, provider lanes | `#homeAsk`, `#homeAskMic`, `#providerStatus` |
| Voice (`data-view="voice"`) | Speech interaction | `#voiceMic`, `#voiceTranscript`, `#voiceSend`, `#voiceCancel`, `#voiceStopListening/Speaking` |
| Tasks (`data-view="tasks"`) | Task sessions list + detail | `#taskAsk`, `#taskList`, `#taskDetail` |
| Automations (`data-view="automations"`) | Automation workflows + draft form | `#automationDraftForm`, `#automationList`, `#automationDetail` |
| History (`data-view="history"`) | Past executions (from `/audit`, `/executions`) | — |
| Devices | Paired devices, pairing, revocation | `#devicePill` |
| Settings | Providers, pairing, workspace, theme | `#profileBtn`, `settings:update` |
| About (`about.html`) | App info | `window:about` |

Header: device pill, provider status + Configure button, stream pill,
notifications, refresh, settings. Sidebar collapses to 68px.
Shortcut `Ctrl+Shift+Space`.

## Theme (actual source values, `desktop/ui/styles.css`)

```css
--bg: #F7F9F5;        --surface: #FFFFFF;
--green: #16834A;     --green-dark: #11643A;  --green-soft: #E7F4EA;
--yellow: #F5CB4B;    --yellow-soft: #FFF6D9;
--ink: #203329;       --ink-dim: #718078;     --ink-soft: #5D6C63;
--line: #E5EBE5;
--ok: #198754;        --warn: #D99B16;       --bad: #D94C4C;
--warn-ink: #8A6208;
--radius: 14px;       --sidebar-w: 244px;     --header-h: 64px;
```

Light theme only — there is no dark mode in source. Do not change the
theme without an explicit requirement.

## Dead controls / incomplete screens

- No dark-mode toggle exists (light theme is the only theme).
- The `image`/`audio` purpose lanes exist in the provider UI but depend
  on the configured provider's capabilities.
- `about.html` is minimal (app info only).
- Screen completeness was not live-verified in this pass; test coverage
  for the renderer is fixture-based (`desktop/test/ui_render.test.js`).

## Tests

`npm test` (`node --test test/*.test.js app/providers.test.js
app/provider_bridge.test.js`): `pipeline.test.js`,
`backend_claim.test.js`, `purpose_ipc.test.js`,
`purpose_legacy.test.js`, `ui_render.test.js` + stubs. Historical C42
report: **106 passed** — see [TESTING_AND_ACCEPTANCE.md](TESTING_AND_ACCEPTANCE.md).
