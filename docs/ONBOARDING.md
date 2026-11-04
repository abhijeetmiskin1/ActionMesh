# First-run onboarding

The onboarding wizard (`desktop/ui/onboarding.js`) is renderer-only: it reads
verified state from `window.actionmesh` (the single `contextBridge` in
`preload.js`) and paints it in the mandated vocabulary **READY / UNAVAILABLE /
NEEDS CONFIG**. It grants nothing, stores no keys in plain text, requests no
unnecessary permissions, and can call no network APIs from the renderer
(CSP `connect-src 'none'`).

## Steps

| # | Step | Optional | What it checks |
|---|---|---|---|
| 1 | Welcome | — | The intro. No checks. |
| 2 | Service | no | The local backend is running (`app:state` → `backend.status === 'ready'`). |
| 3 | Device | no | This laptop has a device name (`settings.deviceName`). |
| 4 | Permissions | no | OS boundaries (`allowShell`/`allowDesktop`/`allowBrowser`/`allowMedia`). Off is correct; the step is READY by default and never blocks. |
| 5 | AI providers | **yes** | Per-purpose lanes: **Chat / Reasoning / Image / Audio**. A lane is READY only after a real classified test (`purpose:test`), never from "configured" alone. |
| 6 | Browser | **yes** | `allowBrowser`; the shell browser window stays available regardless. |
| 7 | Media | **yes** | `allowMedia`. |
| 8 | Security & consent | no | The OS keyring is available (`encryptionAvailable`). If it is not, keys are refused — never written in plain text. |
| 9 | Readiness | — | The final aggregate. Optional steps never block; UNAVAILABLE on a required step makes the whole final UNAVAILABLE. |

## Provider lane states

Main-process `purpose:status` speaks its own vocabulary
(`not_configured`, `missing_key`, `connecting`, `connected`,
`invalid_credentials`, `provider_unavailable`, `timeout`, `rate_limited`).
Onboarding maps it:

| Provider state | Onboarding |
|---|---|
| `connected` (real 2xx test) | READY |
| `not_configured`, `missing_key`, `connecting` | NEEDS CONFIG |
| `invalid_credentials`, `rate_limited`, `provider_unavailable`, `timeout` | UNAVAILABLE |
| *(unimplemented adapter: image, audio, ollama)* | UNAVAILABLE — can never be READY in this build |

The Providers step itself is READY only when chat **or** reasoning is
READY. Image/audio lanes are honest: "Not wired in this build."

## Consent model

Consent checkboxes live on the Permissions step and are **off by default**.
Toggling one calls `settings:update` with exactly that one key — each
boundary is independent (shell ≠ desktop ≠ browser ≠ media), and a grant is
never an approval: critical actions still require per-`action_id` approval
through the policy engine. Skipped optional steps are recorded locally for
the session only; nothing is persisted except the single
`onboardingCompleted: true` write on Finish.

## What is NOT wired (lead-owned — see the diff below)

`desktop/ui/onboarding.js` is mount-independent on purpose: the mount, the
`onboardingCompleted` settings key, and the `.onboarding-*` styles live in
lead-owned files (`index.html`, `app.js`, `styles.css`, `settings.js`).
Until the lead wires them, the module is inert — exactly the safe failure
mode. The exact wiring diff is re-stated below so the lead can apply it
unchanged.

### (a) `desktop/app/settings.js` — DEFAULTS (add after `lastActor`)

```diff
   lastActor: 'desktop-user',
+  onboardingCompleted: false,
```

`update()` only writes keys present in DEFAULTS, so without this the Finish
write is a no-op (safe failure, preserved settings until applied).

### (b) `desktop/ui/index.html` — script + mount point

```diff
 <script src="app.js"></script>
+<script src="onboarding.js"></script>
 ...
+<div id="onboardingMount" hidden></div>
```

### (c) `desktop/ui/styles.css` — append-only `.onboarding-*` styles

Add classes using existing light-theme tokens only (`--bg`, `--surface`,
`--green`, `--ink`, `--ok`, `--warn`, `--bad`); no new global restyle, no
dark mode.

### (d) `desktop/app/main.js` + `desktop/app/preload.js` — no new IPC; optional

Optionally send `ui:navigate` with `{ view: 'onboarding' }` when
`!settings.get('onboardingCompleted')` so the shell can route to the wizard
on first launch. No preload surface changes are required: the wizard only
uses the existing `state`, `updateSettings`, `purposeTest` and
`browserState` calls.

## Tests

`desktop/test/onboarding.test.js` (14 tests, run via `npm test`) covers
the vocabulary mapping, the "unimplemented can never be READY" rule, the
optional-never-blocks aggregation, the no-credentials / no-storage /
no-`innerHTML` / no-mount-I/O invariants, the single `onboardingCompleted`
write, and the `package.json` packaging metadata (`desktopName`, secret
exclusions, `ui/**/*` shipping).
