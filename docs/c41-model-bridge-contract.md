# C41 — Model Bridge Contract (v1)

This file is the interface contract for the C41 sprint. Four agents implement against it.
**Do not change this file** — if something here is wrong, report it to the integration
lead; the lead owns this document.

---

## The shape of the problem

The desktop app stores the provider API key in the OS keyring (`safeStorage`), inside the
Electron **main** process. VoiceOS — the planner — runs in a **separate Python process**
(the managed backend). The Python process must be able to ask a model for a plan without
ever receiving the credential.

So the Python backend asks the Electron main process to make the call, over a private
Unix domain socket. The key never leaves main; Python gets back only the model's answer,
which it treats as untrusted input.

```
renderer  --(IPC, no key)-->  Electron main  --(https, key)-->  provider
                                   ^
                                   |  Unix socket, per-launch token
                                   |
                            Python backend (VoiceOS planner)
```

## Who owns what

| Agent | Owns (exclusive write) |
|---|---|
| A | `desktop/app/providers.js`, `desktop/app/providers.test.js` |
| B | `actionmesh/voiceos/llm.py`, `actionmesh/voiceos/model_bridge.py` (new), `tests/unit/test_llm_generation.py` (new), `tests/unit/test_model_bridge.py` (new) |
| C | `desktop/app/provider_bridge.js` (new), `desktop/app/provider_bridge.test.js` (new) |
| D | `tests/security/test_model_authority.py` (new), `tests/integration/test_ai_voiceos_pipeline.py` (new), `desktop/test/pipeline.test.js` (new) |
| Lead | everything else, incl. `main.js`, `backend.js`, `preload.js`, `app.js`, `index.html`, `api.py`, `package.json` |

Nobody writes a file they do not own. Nobody reformats a file they do not own.

---

## 1. Generation schema — one shape, all providers

Every provider is asked for the **same JSON object**. A provider adapter's whole job is to
get the model to emit this and to hand back the parsed object.

### Planning (`purpose: "plan"`)

```json
{
  "actions": [
    { "type": "<registered action name>", "parameters": { } }
  ],
  "notes": ["anything the request asked for that no listed action covers"]
}
```

### Diagnosis (`purpose: "diagnose"`)

```json
{
  "finding": "string (required)",
  "likely_cause": "string",
  "fixes": ["string"],
  "confidence": "low | medium | high",
  "unanswered": "string"
}
```

Both are schema-described to the provider where the provider supports it:

| provider | mechanism |
|---|---|
| anthropic | forced `tool_use` with `input_schema`; read `content[].input` |
| openai | `response_format: {type: "json_schema", json_schema: {...}}` |
| gemini | `generationConfig.responseMimeType: "application/json"` + `responseSchema` |
| openai_compatible | `response_format: {type: "json_object"}` + strict endpoint validation |

`★` The schema is the same for all four on purpose: the *validator* downstream is one
function (`validate_proposal`), and a provider that only "usually" returns the right shape
would make that validator's job untestable.

---

## 2. `providers.js` — the new `generate()` entry point (Agent A)

Existing exports (`test`, `publicSpecs`, `specFor`, `STATE_TEXT`, `compatibleEndpoint`,
`SPECS`) stay exactly as they are — `main.js` and the C40 tests depend on them.

Add:

```js
/**
 * @param {{provider, model, baseUrl, apiKey, timeoutSeconds, maxTokens}} config
 * @param {{purpose: 'plan'|'diagnose', system: string, user: string}} request
 * @param {{request?: Function}} [options]   // https.request-shaped, for tests
 * @returns {Promise<{state: string, content?: object, detail: string}>}
 */
async function generate(config, request, options = {})
```

`state` is one of, and **only** one of:

```
'ok'                   -> content is the parsed, schema-shaped object
'missing_key'          -> no credential; no network call was made
'not_configured'       -> provider unknown / model blank / base URL missing
'provider_unavailable' -> provider says it is not implemented, or 4xx/5xx/redirect
'invalid_credentials'  -> 401, 403
'rate_limited'         -> 429
'timeout'              -> no answer within the timeout
'malformed'            -> 2xx, but the body was not the schema-shaped JSON object
```

Rules that are not negotiable:

* `content` is present **only** when `state === 'ok'`.
* The parsed object is shallow-validated here (`actions` is an array / `finding` is a
  string) but **not** authorised here. Authority lives in Python.
* Response body ceiling for generation: 256 KiB. Anything larger is `malformed`.
* No redirects. No port other than 443. No `http:`. No userinfo.
* Never put the key, an `authorization`/`x-api-key` header, or the request body into a
  thrown error, a returned `detail`, or a log.
* `maxTokens` default 1024, clamped `[16, 8192]`.
* `timeoutSeconds` default 20 for generation, clamped `[1, 60]`.

## 3. The socket — Agent C's server, Agent B's client

### Where

`path.join(app.getPath('userData'), 'provider-bridge.sock')`, mode `0600`.
`app.getPath('userData')` is passed in; the module does not import `app`.

### Authentication

Main generates a 32-byte base64url token per launch and passes it to the Python child in
its environment as `ACTIONMESH_PROVIDER_BRIDGE_TOKEN`. The Python side passes it in the
request frame. The server compares with a constant-time compare and closes on mismatch.

`★` The token is **not** the API key: it authorises "talk to this socket", nothing more.
Its only channel is the child's environment — never argv (visible in `/proc/*/cmdline`),
never a file, never the renderer.

### Framing

One request per connection, newline-delimited JSON, then the server closes.
Request frame (≤ 256 KiB):

```json
{
  "token": "...",
  "op": "generate",
  "purpose": "plan",
  "system": "...",
  "user": "...",
  "timeout_s": 20
}
```

Response frame:

```json
{ "ok": true,  "content": { } }
{ "ok": false, "state": "missing_key", "detail": "No API key is stored for this provider." }
```

`state` uses the same eight values as §2. `content` is present only when `ok` is true.

Also accept `{"op": "status"}` (token required), answering:

```json
{ "ok": true, "configured": true, "provider": "anthropic", "model": "claude-sonnet-5-5",
  "implemented": true, "hasKey": true, "state": "connected|not_configured|missing_key|..." }
```

`state` here is the **last verified** state — a key on disk is not a connected provider.
`hasKey` is a boolean; no key material ever appears.

### Server requirements (C)

* Refuse to start if the socket path already exists and a live process answers on it;
  remove a stale socket file first (`net.connect` probe, then unlink).
* `fs.chmodSync(socketPath, 0o600)` after listen.
* Per-connection 30 s idle deadline; destroy the socket on timeout.
* Never `console.log` a frame. Count and drop over-sized frames.
* The module reads settings and calls `providers.generate()` **live**, per request, so a
  provider change takes effect on the next request without restarting anything.

Exports:

```js
// startBridge({ socketPath, token, settings, providers, log }) -> Promise<{close(): Promise<void>}>
async function startBridge(options)
```

### Client requirements (B)

New module `actionmesh/voiceos/model_bridge.py`:

* `BridgeUnavailable` / `BridgeError` exceptions with user-visible, key-free messages.
* `def generate(socket_path, token, *, purpose, system, user, timeout_s) -> dict`
  — blocking, returns the parsed `content` or raises `BridgeError(state, detail)`.
* `def status(socket_path, token) -> dict`
* Use `socket.AF_UNIX` + `socket.SOCK_STREAM`, `asyncio` is *not* required on this path
  (callers already offload to a thread); a simple blocking client with `settimeout` is
  the correct amount of machinery. Read exactly one frame, bounded to 256 KiB.
* **Never** log the token; never include it in an exception message.

---

## 4. VoiceOS planner wiring (Agent B, in `llm.py`)

* `AVAILABLE_PROVIDERS` gains `"desktop"` — the provider id meaning "the Electron main
  process holds the key and makes the call".
* `build_provider()` accepts `bridge: BridgeConfig | None` (socket path + token) in
  addition to `environ`. If `config.provider == "desktop"`:
  * no bridge configured → `(None, "<why>")` and the deterministic planner answers;
  * bridge configured → a `BridgeProvider` whose `propose()` calls
    `model_bridge.generate(..., purpose="plan", system=SYSTEM_PROMPT, user=catalogue_text)`
    and returns the raw content mapping for `validate_proposal` to police.
* `build_analyst()` similarly gains a `BridgeAnalyst` for `purpose="diagnose"`.
* Existing `AnthropicProvider` stays and keeps working — do not delete it.
* The `LlmPlanner` fallback behaviour is unchanged: any `LlmError`, malformed answer or
  empty proposal falls back with a note naming the reason.

`★` Note the asymmetry: the **only** thing the model can influence is the tuple
`(type, parameters)`. It cannot name a device (the target is resolved by
`_voiceos_selected_target` before the planner is called), cannot name an actor, cannot
name a risk level. This is why the bridge returns a bare JSON object and Python wraps it —
the wrapper is where authority has to be absent by construction.

---

## 5. Execution mode surface (Lead, `api.py` + UI)

`GET /voiceos/status` (new, authenticated like the other VoiceOS routes):

```json
{
  "mode": "ai_model" | "deterministic",
  "mode_label": "AI model active" | "Deterministic fallback",
  "provider": "desktop",
  "model": "claude-sonnet-5-5",
  "reason": ""   // non-empty only when mode is deterministic and a provider was asked for
}
```

`mode` is `ai_model` **only** when a provider object was actually constructed at startup
*and* its last call succeeded. The route reports `ai_model` optimistically from
construction; the per-request note in `/voiceos/messages` is what proves a real call.
The UI shows the mode chip from `/voiceos/status` and appends per-request notes verbatim.

---

## 6. What is forbidden, everywhere

1. The API key in: renderer JS, `localStorage`, `sessionStorage`, a log line, an error
   string, a task/graph/workflow payload, an audit record, or the Python process env.
2. A new registry, policy engine, executor, or approval path.
3. A renderer-side model call, or a renderer-side "connected" claim.
4. Faking a provider call in production code. Mocks live in tests only.
5. Weakening an assertion to make a gate green. If a test fails, diagnose it.
6. `console.log` of a request/response body in `provider_bridge.js`.