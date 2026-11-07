# Current Status

*Evidence-based inventory. Status labels are kept separate — never
combined. Inventory refreshed 2026-11-07; earlier validation entries below are dated snapshots.*

## Voice routing and Linux release — 2026-11-07

Food workflows require a recognizable food topic or supported site. Local file,
receipt and disk-space wording stays outside those workflows. Spoken plans that
navigate websites or send search queries require review in Task Center. Common
app names such as Calculator and browser use the real laptop launcher, which
checks the existing executable allowlist. Unrecognized single app names ask
for clarification. Booking, ordering and payments remain discovery and review only.

The Linux package at `desktop/release/voice-routing/ActionMesh-0.1.0-amd64.deb`
includes automatic voice execution, the language selector and these routing
changes. Its application archive and changed backend modules matched the staged
build. The packaged app started with a fresh isolated profile and with the existing
local profile. The live local Calculator launch verified, and an explicit web
search stayed pending for review without creating an execution.

CI now runs pytest and the desktop Node suite. Local validation recorded 2,059
Python tests passed, 25 skipped and 901 subtests passed, plus 211 desktop tests
passed. Ruff and mypy passed.

## Functional audit and hot-path cleanup — 2026-11-07

Recent execution history showed browser navigation failures after real redirects.
The verifier correctly reread the final page but then compared its final URL to the
pre-redirect input. The browser process now exposes a completed navigation's original
requested URL only while its final URL and navigation epoch still match fresh state.
Independent observation uses this provenance, with the original final-URL check still
required. Missing/wrong provenance, subsequent page changes and still-loading pages
fail closed. Browser bridge facts now also preserve the loading flag, which was lost
when decoding the reply. Live owned-fixture tests confirm redirected open/navigate,
inspection, snapshots, history navigation, typing and selection through the real engine.
Generic click/type/scroll still cannot claim independent domain success without an
observable postcondition; permission does not change that distinction.

Registry membership and latest-definition lookup now use standard-library dictionaries
(average O(1)); versions are indexed by name, preserving semantic version ordering.
In a synthetic 76-definition lookup benchmark, median get time was 2.394 µs before and
0.056 µs after. This is a microbenchmark, not an end-to-end network-speed claim.
Repeated UI refresh requests for one view share an in-flight promise, without adding
a debounce delay. Static model catalogue text is cached and includes argument names,
types and required/optional markers; it sends no defaults, examples or observations.
This avoids guessing names and unnecessary requests for optional media references.

Validation: full Python run 2,038 passed / 25 skipped / 895 subtests; all 207 desktop
tests passed. Additional targeted wire/redirect/catalogue tests passed after the full
run. Live owned browser and desktop interaction checks passed, along with six real
voice-message API/model checks and the two normal/compact UI acceptance cases.
Ruff and mypy passed. No dependency or alternate execution/authorization path was added.
These checks do not certify every external website, vendor service, or hardware action.

## Linux speech playback fix — 2026-11-07

The exact `synthesis-failed` failure was reproduced in Electron with zero available
voices. The installed Speech Dispatcher and eSpeak NG backend were present; Chromium
requires an explicit `enable-speech-dispatcher` switch on Linux. With the switch,
the same synthetic speech completed successfully. Main now enables it before app
readiness on Linux only. The installer recommends Speech Dispatcher, its eSpeak NG
module, and libspeechd. The browser sandbox and context isolation stay enabled.

Playback selects matching local English/Hindi voices rather than always forcing
English. Failure still reports unavailable speech while retaining the text answer;
no cloud TTS, extra model call, recording, or execution engine was added. This uses
the existing browser-native speech surface. See [Chromium's Linux TTS implementation](https://chromium.googlesource.com/chromium/src/+/HEAD/content/browser/speech/tts_linux.cc).

Validation: 207 desktop tests passed. A live real-Electron run completed English
and Hindi synthesis and verified cancellation via Stop Speaking. This confirms the
system speech completion events; physical speaker volume/hearing was not measured.
The opt-in live regression uses only synthetic sentences, never private conversation.

## Faster speech completion — 2026-11-07

Desktop and web cloud dictation now monitor microphone levels locally during an
explicit recording. After sustained speech, roughly 960 ms of quiet ends capture
and starts transcription, rather than requiring manual Stop or the 30-second cap.
No detected speech for eight seconds cancels locally without uploading. Manual Stop
and the cap remain available if Web Audio cannot run. Cleanup disconnects the analyser,
closes its context and stops microphone tracks; the detector never stores or transmits
samples. Desktop Opus capture now uses 32 kbit/s and 250 ms chunks. Replies default
to two or three sentences unless the user asks for detail. Transcript review and a
separate Ask VoiceOS press are preserved; recording never executes a device action.

Live baseline: synthetic speech transcription 892 ms; a short model response 407 ms.
Live desktop microphone → automatic pause detection → actual speech provider: a
2.025-second generated utterance stopped recording at 3,008 ms and yielded an editable
transcript at 3,513 ms, with zero executions and no manual Stop. These are synthetic
measurements, not physical-microphone guarantees. Existing selected models were kept.
Validation: 203 desktop tests passed, including pause, mid-command gap, cancellation,
no-speech and web endpointing checks; 81 focused Python tests passed. Live web speech
acceptance also passed. Both controllers retain the same approval and execution path.

## Dedicated AI Providers workspace — 2026-11-07

AI Providers is now its own sidebar page. It contains independent Chat, Reasoning,
Audio (speech-to-text), and Image configurations, each with its own provider, endpoint,
model, timeout, encrypted credential and connection test. Home shows read-only model
summaries and a link; the header no longer changes models or providers. General
Settings now contains App & permissions, Backend, and About & web pages.

Chat or Reasoning can be selected as the single planner preference. This controls
VoiceOS understanding, planning and diagnosis; the other text purpose is the
configuration fallback. Audio and Image remain independent. The page explains this
routing explicitly, rather than implying that task and chat requests are automatically
classified into different transports. Selecting a planner never grants execution rights.
Existing saved endpoints, models and credentials are preserved.

Validation: all 198 desktop tests passed, including separate endpoint/model/key
configurations for all four purposes and both text planner preferences. Live UI
acceptance covers all 12 pages at normal and compact window sizes.

## Desktop interface organization — 2026-11-07

Settings now has keyboard-accessible AI providers, App & permissions, Backend,
and About & web pages tabs. The sidebar groups Workspace, Activity and Manage.
App preferences and device access have separate fieldsets; Save and Discard changes
are explicit. Background refresh preserves unsaved form values and no longer rebuilds
unchanged provider cards. Provider labels distinguish configured-but-untested from
missing configuration. The initial backend-starting state no longer produces a false
offline toast.

The provider dialog makes the background inert, contains keyboard focus, supports
Escape, restores focus on close and keeps action buttons visible in short windows.
Buttons have clearer labels and visible focus states; the light theme and reduced
motion setting are preserved. Validation: 197 desktop tests passed; live Electron
checks passed at normal and compact native window sizes, covering all 11 main pages,
four settings tabs, keyboard navigation, unsaved edits/discard, modal focus and no
horizontal overflow. Screenshots were visually checked. No execution policy changed.

## Voice understanding and model selection — 2026-11-07

VoiceOS now accepts a bounded conversational `reply` from the same validated planning
call, for ordinary questions and missing-target clarification. These replies have no
actions and create no session; model text never authorizes or verifies an operation.
Only three prior user messages are included as context, not assistant diagnostics or
observations. Compatible endpoints now receive the actual required JSON schema;
previously JSON mode alone left the expected action format unspecified. The catalogue
budget includes all 76 definitions in the full test registry (previous limit: 60).

Live synthetic comparison after the schema fix: `groq/openai/gpt-oss-120b` passed
8/8 understanding-and-parameter checks, median 1,159 ms. `power` passed 6/8, median
2,766 ms. Cases cover Hindi questions, Hinglish commands, clarification, hypothetical
instructions, browser URLs, multi-step requests and capability questions. These are
small task-specific samples, not a universal model benchmark. Chat and Reasoning use
the tested Groq route through 9router; Audio stays on `groq/whisper-large-v3-turbo`.

Validation: 2,033 Python tests passed, 20 skipped, 895 subtests passed in the full run;
197 desktop tests passed. Live synthetic web dictation passed. Six additional live
voice-message API → private bridge → model checks passed without any executions.
Both live tests also passed against the rebuilt package (9.33 s for understanding,
7.20 s for synthetic dictation). The running installed app reports AI model active
with the selected Groq route; a Hindi greeting through its real bridge took 823 ms.
Physical microphone/hardware acceptance and every OS capability are distinct from
these speech/understanding checks. No real booking, order or payment was submitted.

## Desktop startup renderer fix — 2026-11-07

A live shell startup reproduced the window remaining on “Starting…” even though
the backend was ready. `history.js` and `app.js` shared classic-script globals:
`isTerminal` collided at parse time, preventing the main renderer from booting.
The history implementation is now scoped in a closure and exposes only its existing
`ActionMeshHistory` API (and CommonJS export for tests).

A regression test loads every classic script in the actual HTML order into one
browser-like global and checks that boot reaches “Backend ready.” This covers a
failure the previous isolated renderer tests missed. All 196 desktop tests passed.
A live Electron run with packaged Python, encrypted saved settings and an isolated
data directory showed the window at 457 ms and backend readiness at 999 ms; the
visible status also reached “Backend ready.” These are measurements of that run,
not guarantees for every machine or cloud request.

## Local router configuration deployment — 2026-11-07

The system-installed `/opt/ActionMesh` build still rejected HTTP with “The base URL
must use https.” The current compatible provider allows HTTP only for loopback
addresses and keeps remote endpoints on HTTPS. The configuration form now describes
that exception, with a regression check. The focused provider/renderer suite passed
61 tests; a live saved-credential Chat probe returned connected for model `power`
at `http://127.0.0.1:20128/api/v1`.

A current packaged build is installed for this user under
`~/.local/opt/actionmesh/current`; `~/.local/bin/actionmesh` and the user application
menu entry launch it. The launcher uses the existing root-owned Chromium sandbox
helper; sandboxing remains enabled. The old idle desktop/backend were restarted into
this build, with existing encrypted credentials and data preserved. Both `/health`
and `/ready` returned HTTP 200. The system installation remains older; the user
application menu overrides its launcher without requiring administrator access.

## Web cloud dictation and busy recovery — 2026-11-07

Two working additions are now available on the web Voice page:

- **Cloud dictation:** explicit microphone capture → authenticated transient audio
  endpoint → existing private provider bridge → configured desktop Audio provider.
  The limits are 30 seconds / 128 KiB, WebM/Opus at 32 kbit/s. Microphone tracks stop
  before upload; Cancel dictation and hiding the page discard late answers. Provider
  credentials remain exclusively in Electron main. Recordings are not persisted,
  audited or added to a task. Review the editable transcript and press Ask VoiceOS.
- **Busy recovery:** HTTP 409/503 keeps the request text, explains the busy state,
  and never resends automatically. Manual edits during pending dictation are kept.

The private bridge retains its 256 KiB frame limit. Audio calls have bounded worker
capacity and one active transcription in the bridge. Unknown fields, malformed or
oversize audio, and caller-supplied keys/grants are refused before provider calls.
Only one authenticated request is processed per socket connection.

The desktop web-page session grants audio-only microphone requests to the current
backend's Voice page. Camera, other pages, frames and origins are refused. Backend
API headers attach only to the exact configured origin and port.

**Live evidence:** the production web page captured generated speech through
Chromium's fake audio device, used the real configured 9router provider via the API
and private bridge, and received editable text mentioning the workspace. No task
or execution was created. The same live check exercised busy-response text
preservation, no automatic retry, and late dictation versus a manual edit. This is
synthetic-input acceptance, not a physical-microphone or Android-device claim.
Secure browser context and WebM/Opus support are required; text input remains
available. Audio sent upstream before cancellation cannot be recalled.

Validation: **2,022 Python tests passed / 20 skipped / 895 subtests passed**;
desktop **194 passed**; Ruff and mypy passed (113 source files). The live cloud
acceptance passed with both the development backend and the assembled package's
bundled CPython runtime and ASAR bridge. Packaged backend, controller, bridge and
permission files match current source bytes. One existing Starlette/httpx
TestClient deprecation warning remains.

The new endpoints are `GET /voiceos/audio/status` and
`POST /voiceos/audio/transcribe`; API route count is now **51**. The source contract
and REST reference were updated. The rebuilt installer remains in
`desktop/release/router-update/`. No commit was made.

## Backend responsiveness and concurrency fixes — 2026-11-06

- Serialized lazy initialization of VoiceOS/automation stores, runner and scheduler
  with one reentrant initialization lock. Concurrent first access opens one store
  instead of racing duplicate connections; nested initialization remains safe.
- Serialized conversation turns per actor. An overlapping turn returns HTTP 409
  before calling the model or overwriting conversation history.
- Bounded planning and diagnosis to eight blocking workers per app. Excess work
  returns HTTP 503 instead of building an unbounded queue. Cancelling an HTTP
  caller does not release its running worker's permit early; late results/errors
  are consumed without saving an abandoned conversational turn.
- Added execution-history ordering and per-execution event indexes in additive
  SQLite migration **v3 → v4**. Existing records survive upgrade and reopen;
  query-plan regressions require indexed access without a temporary history sort.
  Older runtimes intentionally refuse the newer schema, so deployments must run
  the updated backend after migration.

Measured isolated history-query improvement, 50,000 synthetic rows, median of
40 reads: **2.229 ms → 0.007 ms** for the recent-25 query. This measures the SQL
query only; it is not a claim about model latency or every endpoint.

Packaged runtime acceptance: 60 `/ready` requests at concurrency 4 completed with
**median 3.00 ms / p95 4.68 ms** on this machine. The updated bundled CPython
backend started successfully with desktop and media host bindings enabled.

Regression tests exercise 24 concurrent first reads, eight blocked model calls
with responsive health requests, duplicate/overflow refusal before model calls,
and cancellation while a blocking worker continues. New tests use no real
provider credentials and make no external model calls.

Final validation for the responsiveness changes: **2,012 Python tests passed,
19 skipped, 895 subtests passed; 181 desktop tests passed**. Ruff and mypy pass
(112 source files). An existing Starlette/httpx deprecation warning remains.
The Linux installer was rebuilt and changed runtime/shell files were compared
byte-for-byte with current source. Nothing was committed.

## Backend stability and real media acceptance — 2026-11-06

Release scope confirmed by the operator: booking, ordering and payments stop at
**discovery and review**. No transaction vendor or payment execution is required
for this release; existing consequential-submit handlers continue to refuse.

- Fixed actual MPRIS wire errors: no-argument signatures, typed property replies,
  and `OpenUri` on the Player interface. The standalone native adapter is now
  `agent/mpris.py`; the packaged Python reaches host PyGObject without importing
  incompatible compiled extensions. Media access is independent of desktop access.
- Deduplicated VLC's stable/instance aliases for one process, so one real player
  is not reported as an ambiguous choice. Track changes use an opaque digest of
  the player track identifier even when no track title is reported. Media URLs
  open the existing controlled browser session and receive its live read-back.
- Fixed mute/unmute using an observed pre-mute level; an unknown restoration level
  is refused instead of invented. A next/previous command with no changed track
  is reported as no effect. Track labels are bounded and redact credential shapes.
- A duplicate task-advance request now receives HTTP 409 instead of racing the
  first request's side effects. Active claims are bounded and released on error
  or cancellation; unrelated task IDs remain independent.
- VoiceOS carries the execution's simulation flag through storage, record folding,
  descriptions and the UI. A verified mock result cannot be narrated as real work.
- Provider lanes retain independent probe results. Configuration edits invalidate
  only their own lane, and ephemeral credentials do not certify a stored setup.
  Onboarding now describes the implemented router Image/Audio adapters correctly.

Validation: full Python suite **2,008 passed / 19 skipped / 895 subtests passed**;
desktop **181 passed**; Ruff and mypy passed (111 source files). Opt-in live
acceptance **3 passed**: production Electron browser, owned GTK window, and actual
VLC 3.0.23 decoding generated silent WAVs with an advancing playback clock.
The VLC run exercised policy denial, status, pause/play, volume, mute/unmute,
next/previous and file hand-over through ExecutionEngine. It used official Ubuntu
packages extracted temporarily without administrative installation. This is real
player/protocol evidence; it is not an audible-speaker or arbitrary-player claim.

Packaged backend measurement after packaging completed: **60 `/ready` requests,
concurrency 4, median 2.33 ms / p95 5.36 ms** on this machine. Both native media
and desktop binding probes were enabled. This is an observed local readiness
measurement, not an end-to-end model or hardware latency guarantee.

MPRIS reference: [Player interface specification](https://specifications.freedesktop.org/mpris/latest/Player_Interface.html).

The rebuilt artifact remains `desktop/release/router-update/ActionMesh-0.1.0-amd64.deb`.
Installation of the root-owned system copy still requires administrator authentication.
No commit was made. Web/PWA cloud transcription, physical microphone/speaker checks,
external-site acceptance, and Android/device install acceptance remain open. The
current web Voice page still relies on native recognition or editable text.

## Functional repair and release check — 2026-11-06

The following paths now have working implementations and fresh evidence:

| Feature | Evidence from this machine | Remaining limit |
|---|---|---|
| Chat and structured planning | Live 9router `power` response | Router must stay running; model availability can change |
| Speech input | Live `groq/whisper-large-v3-turbo` transcription of generated spoken audio; all desktop microphone buttons share the bounded capture controller | Physical microphone/permission prompt not exercised |
| Image generation | Live raster from `cloudflare-ai/@cf/black-forest-labs/flux-2-klein-4b`; encrypted Image lane configured; Home prompt/preview/clear tested | No automatic file save; Image Test Connection makes a metered generation request |
| Screen / GUI / explanation | Live host-binding inspection, click and typing against an owned GTK fixture; explicit label submission to Reasoning and cancellation tested | Host PyGObject/AT-SPI required; generic GUI effects and capture receipts stay unverified; no pixel analysis or screenshot preview |
| Files | Real temporary-file create → rename → copy → edit → move → read through policy/approval/execution and independent read-back | Trash transport is a fixture; OS open/trash availability depends on the host |
| Browser | Real isolated Electron page through ExecutionEngine: open, snapshots, inspect, navigation, back, forward, refresh, click, typing, selection | Local fixture page only; generic clicks/typing/selection remain unverified when no independent postcondition exists; login/CAPTCHA stay user handled |

Manual browser controls now use the existing `/execute` pipeline. A missing
browser bridge returns unavailable instead of using a production fake driver.
Element documents travel whole through the actor-scoped transient output channel,
using backend session refs. Canonical string refs accepted by the registry now
reach click dispatch correctly. History controls wait for completed navigation.

The packaged Linux runtime now reaches host PyGObject through a fixed OS adapter:
`/usr/bin/python3 -I` runs only the shipped `device_screen.py` and a closed set of
bounded JSON operations. The launcher probes bindings before enabling this path;
the existing agent runner supplies timeouts, output bounds and process cleanup.
Backend/provider credentials are excluded from the child environment. No second
policy engine, execution engine, consent surface, or network transport was added.
The package recommends `python3-gi` and `gir1.2-atspi-2.0`. A named accessibility
application that is absent is refused; inspection never falls back to other apps.
Capture verification never repeats the portal prompt. GUI dispatch/read-back
receipts are not relabelled as independent verification; the UI reports a real,
unconfirmed dispatch accurately.

File create refuses overwrite, oversized copy is rejected before creating a
destination, and file verification reads current confined descriptors. Copy
checks both source and destination; rename/move check the new and old names.
Restart reconciliation re-reads disk instead of trusting persisted receipts.
Unavailable trash records no successful effect. Legacy file observation no longer
copies raw content into an observation journal.

Validation: full Python suite **1,994 passed / 18 skipped / 895 subtests passed**.
Desktop **178 passed**. Separate opt-in live acceptance: real Electron browser
**1 passed**, including actual click/type/select effects; host-native GTK fixture
**1 passed**, including inspection/click/type. Ruff and mypy pass (110 source
files). One existing Starlette/httpx deprecation warning remains. The assembled
package's bundled CPython 3.12 backend starts ready, declares its local device,
and registers native inspection with the host binding adapter enabled. Packaged
JS/renderer files and changed Python runtime files match current source bytes.

The rebuilt `.deb` is
`desktop/release/router-update/ActionMesh-0.1.0-amd64.deb`. The installed
`/opt/ActionMesh` copy must be updated and fully quit/reopened to use these
changes. Administrator authentication is required to update the root-owned
installed copy; `sudo -n` could not authenticate in this session. Use `apt install
--reinstall` on the local package because the application version remains 0.1.0.
No commit was made.

This does **not** establish universal feature acceptance. Real booking, ordering,
and payment have no vendor backend or account; 9router cannot supply one.
At this earlier checkpoint no MPRIS player was reachable; the subsequent real VLC acceptance is recorded above.
Physical microphone, accessibility actions against user applications, portal
capture, device pairing, spoken playback, and non-Linux installers still need
their own live acceptance. The GTK fixture was owned by the test and is distinct
from acceptance against arbitrary user applications.

## Verification corrections — 2026-11-05 (earlier snapshot)

The project review's five reproduced issues have been addressed:

- File observation uses `Workspace.open_fd`; descriptor walks also refuse a
  symlink substituted for the workspace root. Regressions cover leaf, directory,
  and root swaps using synthetic temporary files.
- Empty or incomplete observations cannot confirm an action merely by carrying
  a parameter dictionary. File-write/create verification requires a matching
  live content digest.
- Browser observations read fresh bounded page facts without navigating or
  rebuilding session references. Media observations re-query the recorded player
  and compare current state. Historical browser/media receipts cannot certify a
  recovered execution.
- Verifying `device.files.open` never invokes the launcher again. File opening,
  media file opening, and browser interactions without a sufficient independent
  read-back remain **unverified**, rather than being labelled verified success.
  The browser driver exposes character counts rather than the field contents;
  `browser.type` is consequently `succeeded_unverified` in VoiceOS.
- Capability discovery chooses the latest definition before filtering, matching
  execution's version selection.

Validation from this working tree: full `pytest tests -q` **1,964 passed / 16
skipped / 888 subtests passed**; desktop `npm test` **145 passed**; Ruff and mypy
passed (109 source files). A temporary real filesystem write was also exercised
through the policy/approval/executor path: it parked for approval, resumed after
approval, wrote the expected bytes, and passed independent digest verification.
This is live filesystem evidence only; live browser/player and graphical release
acceptance were not performed in this fix pass. One Starlette/httpx deprecation
warning remains. No commit was made.

## 9router speech integration — 2026-11-05

Chat and Reasoning are configured to the live-tested `power` router alias.
Audio uses `groq/whisper-large-v3-turbo` through the new compatible transcription
adapter. Credentials were saved using the existing OS-keyring-backed settings
store; no plaintext key is included in source or documentation. This router's
direct API base, `http://127.0.0.1:20128/api/v1`, answered while `/v1/models`
timed out. HTTP transport is restricted to loopback; remote endpoints still
require HTTPS on port 443 and redirects are refused.

Live evidence: the router returned a valid structured file-list proposal, and
transcribed generated spoken audio as “List the files in my workspace.” This
does not claim a test of the user's physical microphone. Capture, track cleanup,
cancellation, late responses, recording limits, multipart request shape, response
validation, and credential boundaries are fixture-tested. Transcripts remain
editable and require Send; all actions retain the existing policy/approval path.

Current desktop validation: **157 tests passed**. Focused Python release,
deployment, and dangerous-primitives checks: **29 passed / 8 subtests passed**.
Ruff and mypy passed. The updated Linux build is under
`desktop/release/router-update/`; the installed `/opt/ActionMesh` application
must be updated and reopened to load the new capture code. Image generation was
subsequently implemented and live-tested (see latest release check above). Real
booking/payment still requires a vendor integration.

| Feature | Source exists | Registered | Connected | UI usable | Automated tests | Live verified | Status |
|---|---|---|---|---|---|---|---|
| Core ActionMesh (registry/policy/graph/executor/verification/audit) | ✓ | ✓ | ✓ | ✓ (dashboard/desktop) | ✓ | — | **COMPLETE** (fixture/integration level) |
| Permission engine | ✓ | ✓ | ✓ | ✓ (consents UI) | ✓ | — | **COMPLETE** |
| Execution + cancellation + durable resume | ✓ | ✓ | ✓ | ✓ | ✓ | — | **COMPLETE** |
| Verification + compensation | ✓ | ✓ | ✓ | ✓ | ✓ | — | **COMPLETE** |
| Event streaming (SSE, tickets) | ✓ | ✓ | ✓ | ✓ (stream pill) | ✓ | — | **COMPLETE** |
| Persistence (SQLite) | ✓ | ✓ | ✓ | — | ✓ | — | **COMPLETE** |
| HTTP API (49 routes) | ✓ | ✓ | ✓ | ✓ | ✓ | — | **COMPLETE** |
| Device agent (laptop provider, 60 ops) | ✓ | ✓ | ✓ | ✓ (devices view) | ✓ | — | **INTEGRATED** (not live-verified in this pass) |
| Electron app | ✓ | ✓ | ✓ | ✓ | ✓ (desktop tests) | — | **INTEGRATED** |
| VoiceOS (sessions, orchestration, diagnosis) | ✓ | ✓ | ✓ | ✓ (voice view) | ✓ | — | **INTEGRATED** |
| AI providers: Anthropic (Python) | ✓ | ✓ | ✓ | ✓ | ✓ | — | **INTEGRATED** (needs API key) |
| AI providers: desktop bridge (cwd `desktop`) | ✓ | ✓ | ✓ | ✓ | ✓ | — | **INTEGRATED** |
| AI providers: OpenAI / Gemini / generic (Python) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | **NOT IMPLEMENTED** in Python (`llm.py:115`); JS transports exist (`providers.js`) but are not Python providers |
| Browser agent | ✓ | ✓ | ✓ (via Electron IPC) | ✓ | ✓ | ✓ (isolated local page) | **LIVE LOCAL ACCEPTANCE**; external sites not accepted |
| Files agent | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (temporary filesystem) | **LIVE FILESYSTEM JOURNEY**; trash transport fixture only |
| Media (MPRIS) | ✓ | ✓ | ✓ (D-Bus code) | ✓ | ✓ (fake driver) | ✗ | **TESTED WITH FIXTURES** — real player explicitly not exercised (`media.py:326-328`) |
| Workflows / automations | ✓ | ✓ | ✓ | ✓ | ✓ | — | **INTEGRATED** |
| Website discovery (restaurant/train flows) | ✓ | ✓ | ✓ | ✓ | ✓ (fixtures) | ✗ | **TESTED WITH FIXTURES** |
| Booking / ordering (real) | Partial (definitions + gates) | ✓ | Refuses (no backend) | — | ✓ (refusal tested) | ✗ | **PARTIAL / EXTERNALLY BLOCKED** — `booking.submit`/`order.place` fail closed (`provider.py:_submit`, `PERMANENT`); no real vendor |
| Approvals | ✓ | ✓ | ✓ | ✓ | ✓ | — | **COMPLETE** |
| History | ✓ (`/audit`, `/executions`) | ✓ | ✓ | ✓ | ✓ | — | **COMPLETE** |
| Packaging (deb) | ✓ (electron-builder config) | — | — | — | ✓ (`test_release.py`, `test_android_apk.py`) | deb present (150 MB, 2026-11-04) | **INTEGRATED** — `desktop/release/ActionMesh-0.1.0-amd64.deb`; AppImage/NSIS/dmg/APK install not freshly verified in this pass |
| Renderer CSP | ✓ | — | — | ✓ | — | — | **VERIFIED in source** — `desktop/ui/index.html:5`: `default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none'` |
| Onboarding (PWA/APK) | ✓ | ✓ | ✓ | ✓ | ✓ | — | **INTEGRATED** |
| Credential lifecycle | ✓ | ✓ | ✓ | ✓ | ✓ | — | **COMPLETE** |
| Real vendor integrations (C13) | ✗ (boundary only) | ✗ | ✗ | ✗ | ✗ | ✗ | **NOT IMPLEMENTED** (intentionally external) |

## Verified numbers (2026-11-04)

| Number | Value | Command / source |
|---|---|---|
| Implemented ops | 60 | `.venv/bin/python -c "from actionmesh.agent.definitions import IMPLEMENTED; print(len(IMPLEMENTED))"` → `60` |
| `SUPPORTED == IMPLEMENTED` | True | `.venv/bin/python` — `LaptopProvider.SUPPORTED == IMPLEMENTED` |
| HTTP routes | 49 | `grep "@app\.\(get\|post\|put\|delete\|websocket\)" actionmesh/api.py \| wc -l` → `49` (middleware excluded) |
| CSP | Present | `desktop/ui/index.html:5` |
| Python providers | 2 (`anthropic`, `desktop`) | `actionmesh/voiceos/llm.py:115` |
| Desktop JS providers | 5 listed, 4 `implemented: true`, 1 `false` (`ollama`) | `desktop/app/providers.js:49-91` |
| `.deb` size | 150 MB | `ls -lh desktop/release/ActionMesh-0.1.0-amd64.deb` |
| Focused test | 40 passed | `.venv/bin/python -m pytest tests/unit/test_voiceos.py -q` |

Python/JS provider split: JS implements 4 transports (`anthropic`,
`openai`, `gemini`, `openai_compatible`); Python's `AVAILABLE_PROVIDERS`
exposes only `anthropic` directly plus `desktop`, which routes back into JS
via the C41 bridge. The extra JS transports are not Python providers.

## What is NOT done / BLOCKED

- **MPRIS live verification (P0-1)** — needs Linux desktop + real player + session bus. Label: BLOCKED in this pass, TESTED WITH FIXTURES in source.
- **External-site browser acceptance (P0-2)** — isolated real Electron local-page acceptance passed (`tests/e2e/test_browser_live_controls.py`). External website flows and authenticated sessions are not live accepted.
- **Real booking / ordering (P1-2)** — handler refuses (`PERMANENT`, no payment backend). Needs a real vendor + credentials.
- **Real vendor integrations (P1-1, C13)** — NOT IMPLEMENTED by design; `RealProviderBase` boundary only.
- **Second Python AI provider (P1-3)** — OpenAI/Gemini Python transports NOT IMPLEMENTED; JS transports exist but are only reachable via the desktop bridge.
- **Live device pairing and fresh AppImage/NSIS/dmg/APK installs** — not recorded in this pass. Live 9router text, speech and image calls are recorded above.
- **Dark theme** — explicitly out of scope (light only).

## Known discrepancies (doc vs. source) — resolved this pass

1. ~~`README.md` test counts (425, unittest) and phase table are stale~~ —
   README rewritten 2026-11-04 with source-verified numbers; historical
   counts are labeled historical in
   [TESTING_AND_ACCEPTANCE.md](TESTING_AND_ACCEPTANCE.md).
2. ~~CSP was not verified in source during this documentation pass~~ —
   **verified**: `desktop/ui/index.html:5` (see table above).
3. ~~HTTP routes shown as 47~~ — actual **49** (`api.py`, excl. middleware).
4. The repository is a **reconstruction** (original tree + `.git` lost);
   git history is authoritative only from the first reconstruction commit.
5. The C42 execution contract (`docs/c42-execution-contract.md`) is
   explicitly "a map, not a second source of truth — if the code and
   this document disagree, the code wins."

## How to run

```bash
.venv/bin/python -m pytest tests -q                          # full suite — don't rerun casually
.venv/bin/python -m pytest tests/security -q
.venv/bin/python -m pytest tests/unit/test_voiceos.py -q     # 40 passed (2026-11-04)
cd desktop && npm test
cd desktop && npm run dist:linux   # electron-builder --linux deb --x64 → desktop/release/*.deb
ACTIONMESH_LIVE_ACCEPTANCE=1 .venv/bin/python -m pytest tests/e2e/test_phase2_live_acceptance.py -q  # opt-in live; BLOCKED otherwise
```
