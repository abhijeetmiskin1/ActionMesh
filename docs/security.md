# Security Model

## Threat model

ActionMesh exists because "ask an LLM to do things" is a dangerous default.
The threats below are the ones the architecture is built against.

| # | Threat | Attack surface | Mitigation | Test | Residual risk |
|---|--------|----------------|------------|------|---------------|
| 1 | **LLM self-authorization** | LLM emits `"authorized": true` or a policy hint | Policy context is a typed model; the engine never reads action parameters | `test_graph_invariants.py` | **Low** — rests on no layer ever consulting `parameters` for authorization |
| 2 | **Permission escalation via injection** | `parameters.permissions = ["*"]` | Requirements come only from the registry definition | `test_graph_invariants.py` | **Low** |
| 3 | **Risk downgrade** | `"risk_level": "low"` on a CRITICAL action | Effective risk is `max(action, definition)`; grants carry a `max_risk` ceiling | `test_registry.py`, `test_policy_engine.py` | **Low** — trusted metadata can only raise risk |
| 4 | **Wildcard consent** | user (or attacker) grants `*` | `ConsentStore` refuses non-concrete scopes; grants match exactly | `test_permissions.py` | **Low** |
| 5 | **Unknown action execution** | LLM invents an action name | `registry.resolve` raises on unknown actions | `test_registry.py` | **Low** |
| 6 | **Policy shadowing** | an `allow` rule shadowing a `deny` rule | Equally specific rules resolve to the most restrictive decision | `test_policy_engine.py` | **Low** — depends on deterministic specificity ordering |
| 7 | **Lifecycle forgery** | `"state": "success"` in a request | `validate_action` forces `state = CREATED` | `test_registry.py` | **Low** |
| 8 | **Approval bypass** | approving a different action, or approving a DENY | Approval is matched on `action_id`; DENY is terminal | `test_policy_engine.py` | **Low** |
| 9 | **Graph injection** | cycle, dangling dep, orphan node to stall or loop the executor | `validate_graph_structure` rejects; DAG enforced | `test_graph.py` | **Low** |
| 10 | **Idempotency loss** | retry double-sends a message or double-books | `idempotency_key` preserved end-to-end; ledger releases on failure | `test_registry.py`, `test_executor.py` | **Medium** — only protects actions that actually supply a key |
| 11 | **Command injection** | a terminal request reaches a shell, or an argv string is split by one | Spawning is confined to `actionmesh/agent/runner.py`, which is granted exactly one call (`asyncio.create_subprocess_exec`) by the AST guard. There is no shell on any path: `shell=True`, `create_subprocess_shell`, `os.system` and `os.popen` are refused on every path with no exemption, an `arguments` **string** is refused before resolution, and the program is a PATH-resolved real path on an operator allowlist | `test_dangerous_primitives.py`, `test_command_policy.py`, `test_command_runner.py` | **Medium** — the guard parses names, so a call routed through a locally bound alias (`spawn = asyncio.create_subprocess_exec`) evades it; the argv-only design, not the parser, is what actually prevents shell interpretation |
| 12 | **SSRF / outbound network** | core dials an attacker-chosen URL | Network modules banned package-wide; allowlist empty by default | `test_dangerous_primitives.py` | **Low** — adding a path to `NETWORK_ALLOWED` is a deliberate, reviewable edit |
| 13 | **Unsafe deserialization** | `pickle`/`marshal` loads attacker data | Import ban; untrusted payloads use typed Pydantic-free validation | `test_dangerous_primitives.py`, `test_registry.py` | **Low** |
| 14 | **Dynamic code execution** | `eval` / `exec` / `__import__` | Call ban that the allowlist **cannot** exempt | `test_dangerous_primitives.py` | **Low** |
| 15 | **Malicious provider response** | provider claims success but state disagrees | Weighted evidence: a claim alone is below threshold; any contradiction is decisive | `test_verification.py` | **Medium** — a provider that owns `observe()` is trusted within its own domain |
| 16 | **API key guessing** | unauthenticated non-health route | Optional key, `hmac.compare_digest`, rejected before routing | `test_security_api.py` | **High** — key is optional by default and there is no rate limiting |
| 17 | **Oversized request** | memory/CPU exhaustion | Size limit enforced before routing | `test_security_api.py` | **Medium** — bounded per request only; no global rate limit |
| 18 | **Credential leakage** | secret in source, config, log or response | No credential exists; boundary test asserts config never contains a secret value | `test_real_provider_boundary.py` | **Low today** — C13 must keep this invariant using env/secret stores |
| 19 | **Reversed/wildcard adapter scope** | adapter asks for `*` or an `http://` base | Contract rejects non-HTTPS base URLs and non-concrete scopes | `test_real_provider_boundary.py` | **Low** |
| 20 | **Audit tampering** | attacker edits, deletes or reorders the audit trail | Append-only hash chain: each record holds a hash of its content *and* of its predecessor; `verify()` recomputes the whole chain and never trusts stored hashes | `test_audit.py` | **Medium** — a chain stored beside its own data cannot stop an attacker who rewrites the entire store. Real tamper evidence needs an anchor *outside* the store (append-only medium, signed checkpoint, or write-once storage) |
| 21 | **Path traversal** | `device.file.*` names `../../etc/passwd`, an absolute path, or a percent-encoded separator | Every path resolves through `agent.paths.Workspace`: `..` and absolute paths refused lexically, then containment tested on the canonicalised path at component boundaries, then denied-name checks | `test_agent_paths.py` | **Low** — the boundary is the configured root; a root set at `$HOME` makes the whole home tree in scope by design |
| 22 | **Symlink escape (TOCTOU)** | a link is swapped in *after* the path is validated | Reads and writes are opened by walking one component at a time with `openat`+`O_NOFOLLOW` from a root descriptor, so the kernel decides containment; a swapped link fails to open | `test_agent_paths.py::test_a_symlink_swapped_in_after_resolve_cannot_carry_a_write` | **Low** — holds only while the root itself is not attacker-writable |
| 23 | **Credential-directory access** | a phone reads `~/.ssh`, browser profiles, or a project `.env` | Denied names are matched against the *resolved* path, so a symlink into `.ssh` lands on a denied component; `.env*` is refused outright | `test_agent_paths.py::test_a_denied_name_reached_through_a_symlink_is_still_denied` | **Low** — a name-based list; an unusual credential path can fall outside it |
| 24 | **Stolen pairing credential** | a leaked token is replayed, or a lost phone is used | Authentication resolves through the identity store, so `POST /devices/{device_id}/revoke` invalidates every credential for that device; credentials expire and rotate | `test_pairing_adversarial.py`, `test_credentials.py` | **Medium** — a token is a bearer secret: whoever holds it *is* that device until revoked |
| 25 | **Pairing-code guessing** | 10^6 codes, brute-forced over the LAN | Codes live 5 minutes, are single-use, one at a time per laptop, and every attempt (valid or not) counts against a 20-per-minute window; tripping it revokes all live codes and locks minting too | `test_pairing_adversarial.py` | **Medium** — roughly 1 in 10,000 per code window; the residual assumes no TLS, so a network observer can also see the code |
| 26 | **Environment-variable leakage** | a spawned command inherits `ACTIONMESH_SECRET_KEY` and can offline-crack the code database | The child environment is **constructed**, never `os.environ`; only base variables plus operator-listed ones, and any secret-shaped name is refused even when listed | `test_command_policy.py`, `test_command_runner.py::test_the_child_sees_nothing_but_the_allowed_set` | **Low** — a value placed in `PATH` or `HOME` is still visible, as it must be |
| 27 | **Allow-list escape by renaming** | `pkill` resolves to a binary named `pgrep`; `rm` resolves to `gnurm` | Both the requested name and the resolved basename are checked against the refuse-even-if-allowed set, plus suffix forms (`nc.openbsd`) | `test_command_policy.py::test_a_rename_or_multicall_of_a_denied_binary_is_still_denied` | **Medium** — the name sets are a maintained list; a denied program reached under a third name needs a new entry |
| 28 | **Resource exhaustion via commands** | many simultaneous spawns, a hung process, an unbounded output stream | Bounded semaphore, timeout that kills the whole process group, output capped while reading rather than after. The ceiling a command is actually given is the agent's own (`ACTIONMESH_COMMAND_TIMEOUT_S`) and a caller's `timeout_s` can only lower it; the enforced value is reported (`timeout_requested_s`, `timeout_applied_s`, `timeout_clipped`) rather than applied silently | `test_command_runner.py`, `test_agent_provider.py::test_the_result_says_the_callers_timeout_was_clipped` | **Medium** — bounded per agent process; there is still no global rate limit across devices |
| 29 | **Cross-device event leakage / operator-fallback escalation** | a phone watches another device's execution, or a rejected token silently becomes the unscoped operator view | The stream is pinned to the authenticated principal's `actor`; `execution_id`/`Last-Event-ID` only narrow. The unscoped operator view is **enforced server-side to loopback only** — a remote caller with no credential or valid ticket is refused (401 / WS close), so clearing a token can never upgrade a client to operator authority | `test_event_stream.py::TestWebsocketScoping`, `TestStreamDenial`, `test_security_api.py::test_a_remote_anonymous_websocket_is_denied` | **Low** — the loopback boundary is on the server, not in the page |
| 30 | **A stream event grants itself authority** | `approval.required` or a progress event treated as authorization | Events are observational: `approval.required` carries only `{node_id, action_type, risk_level, reasons}` and no grant/decision field; approval still runs through `POST /approvals` + the consent store, and injected authority fields are ignored | `test_event_stream.py::TestApprovalEventCarriesNoAuthority` | **Low** |
| 31 | **Content or oversized payload over the event channel** | file contents / terminal stdout streamed to every phone, or a huge event exhausting a client | Provider results pass through an allowlist safe-view (no `content`/`stdout`/`stderr`); a serialized event over 16 KiB has its payload replaced by a truncation notice; subscriber queues and history are bounded and slow consumers are disconnected. Mid-command output is a separate, opt-in channel: `execution.output` is transient (never audited, never replayed), bounded to 4 KiB per chunk and 64 KiB per node, and actor-scoped like every other event. Inventory rows (`processes`, `servers`, `projects`, `editors`, `services`, `entries`) are re-projected through the same allowlist — scalars only, ≤120-char strings, no nested list or dict, ≤20 rows — so a list cannot become a hiding place (C27b). A file **body** reaches its reader the same way command output does: as transient `execution.output` with `stream: "content"`, never in a result payload, never in the audit chain, and nothing at all for a binary file (C28) | `test_streaming.py`, `test_output.py`, `test_event_stream.py::TestEventSizeGuard` | **Low** — the streamed body is whatever the file held, unchecked: a phone that was allowed to read it now holds it, exactly as a command that prints its own secrets reaches the observer already allowed to watch |
| 32 | **Endpoint flooding / auth spray** | one caller mints pairing codes, redeems them, spawns executions, or opens streams without limit | A bounded sliding-window limiter (`core/ratelimit.py`) caps pairing-mint, redeem, ticket-mint, execute and approval per actor/source; it prunes expired keys and hard-caps tracked keys so the limiter itself cannot grow. `ACTIONMESH_RATE_LIMIT=0` disables it for local dev/tests | `test_ratelimit.py` | **Medium** — process-local only; a multi-process deployment must enforce the same policy at the proxy |
| 33 | **Cross-device read of another actor's work, or a cancel that is not yours** | a paired phone fetches another device's execution records, its pending approvals (which carry the action's **parameters**), or the whole audit trail — or stops a run it does not own | The stream's rule is reused, not re-invented: `_record_scope` resolves the caller through `_stream_principal`, so a credential sees only its own actor, a foreign execution id answers 404 (never "exists but not yours"), and a remote caller with no credential is denied. Cancel is actor-checked and throttled like its neighbours, and answers `requested` rather than claiming an outcome | `test_record_scoping.py` | **Low** — scoping is in-process; a multi-process deployment must share one identity store for it to mean anything |
| 34 | **Approval, consent and revocation reachable by the wrong device** | any paired phone approves another actor's parked CRITICAL action (and receives their resumed record), records a consent naming someone else's actor, or revokes a device it does not own | All three resolve their caller through `_record_scope` and refuse: a device may decide only an action in its own waiting set (403), may name only its own actor in `/consents` (403), and may revoke only its own device (foreign names answer the same 404 as unknown ones). An anonymous remote caller is denied outright, as on the stream | `test_record_scoping.py::TestWriteScoping` | **Low** — the check is in-process; a multi-process deployment needs one shared identity store for it to hold |
| 35 | **Captured logs: program output written to disk and read back** | a launched server's log fills the disk, is readable by others on the machine, is reachable through the file API, or is collected by a device that should not see it | Capture is **off unless the operator sets `ACTIONMESH_AGENT_LOG_DIR`**; each log is `0600`, named with an unguessable token, and the directory is refused at startup if it sits inside the workspace (or the confined `device.file.read` could reach it). Size is bounded to 512 KiB by front-trimming on every pass over the process table, and the file — not a pipe — is what keeps a child with no reader from blocking in `write`. Reading it is a MEDIUM capability addressed by the issued `process_id`, actor-scoped, and its bytes travel the transient channel: never audited, never replayed | `test_processes.py::TestLogBounding`, `TestCapturedOutput`, `test_agent_provider.py::test_a_launched_child_keeps_its_output_when_a_log_dir_is_set`, `test_agent.py::TestConfig` | **Medium** — a log is whatever the program chose to print, kept on this machine until trimmed; a secret printed by a server is a secret in that file |
| 36 | **A cancellation that lies about what it stopped** | the user presses Cancel and the run keeps executing anyway; or an action already dispatched is reported as a clean stop, hiding an effect that may still be standing; or the interrupted call is retried because the wait looked like a transient fault | The executor holds the provider call it is awaiting and interrupts it, then **awaits that call's own unwind** — the runner's process-group kill — before recording anything, so the state describes a stopped operation. The action was dispatched, so the node reads `cancelled` with `verified: null`, `compensation_required` is set and `compensation.unconfirmed` is published; the ledger key is failed, which refuses replay-as-success without forbidding a deliberate retry, and no automatic retry is issued. A genuine outer `asyncio.CancelledError` propagates instead of being absorbed as a user stop, and no second state machine was added: `ActionState.CANCELLED` already existed | `TestInFlightCancellation` (8) + a subprocess test that proves the command's parent **and its grandchild** are gone; browser pass on the real server — cancelled at tick 97 of a 60s stream, output stopped there, LIVE panel and REST agreed, audit chain still `ok` | **Low-Medium** — the guarantee is in-process: `_inflight` is loop-local, so a run in another process stops at its next boundary; a child that left its process group escapes the kill; a third-party effect already sent is reported unconfirmed, not undone |
| 37 | **A voice layer that runs what it heard** | a misheard or crafted sentence executes without being read, the page approves its own request, transcript text defines its own permissions, or audio is captured somewhere nobody agreed to | Running is a separate press on a plan that was previewed first, built from the planner's own actions with policy rules mapped over exactly those action types; each approval is a separate press naming one `action_id`; the consent surface is not reachable from the page at all, so it cannot grant itself a permission; risk and permission labels come from `/capabilities`, not from the proposal; recognition is user-initiated, never continuous, never self-restarting, and no audio-capture API appears in the page. `GET /approvals` reports the registry's effective risk, so the prompt cannot read "medium" over a CRITICAL command, and a parked run is reported as parked rather than as an outcome | `tests/unit/test_voice.py` (54) + `test_api.py` for the risk label, plus the browser pass recorded under C33 | **Medium** — recognition quality is the vendor's, and a mishearing still reaches a preview a user may approve without reading; the authority to run never leaves the press, and consent still has to exist server-side |

Threats **not yet addressed** — the relevant layer does not exist yet. The
exposure column is the honest answer to *"how bad is this today, before the
missing layer lands?"* rather than a comfortable "n/a".

| Threat | Why open | Exposure today |
|--------|----------|----------------|
| Prompt injection via the intent box | `POST /plan` and the control room turn **user text** into `ProposedAction` values — the first point where untrusted text becomes structured data. No retrieval layer exists; when one lands, retrieved text must be modelled as untrusted data too | **Low** — `ProposedAction` carries only `{type, parameters}`, so injected text has no risk/permission/state field to occupy; `plan_to_graph` re-validates every action, unmapped clauses surface in `Plan.notes`, and the browser can never hand the server a permission. A plan can still be *wrong*; policy and approval bound the damage |
| Replay of a signed request | No request signing; the API-key boundary is optional by default | **High if exposed** — terminate TLS and require a key at the edge |
| Resource exhaustion beyond request size | A process-local sliding-window limiter now caps the auth-sensitive and execution endpoints (row 32), but it is per-process, not distributed; a multi-process or relayed deployment still needs a proxy/WAF quota | **Medium** — enforce a global per-caller quota at the reverse proxy |
| Secret at rest | No secret store exists to protect | **None today** — C13 must use env/secret stores only |

### C14 status

Threat-model pass completed: **20 mitigated threats**, each with a named test
and an explicit residual-risk rating, and **5 open threats** with their real
exposure.

C19 (the mobile control plane) added eight more — rows 21-28 above — and retired one
open threat: **device impersonation** is now bounded by persistent identity plus
pairing rather than being absent. Row 11 was rewritten rather than left as a
comfortable line: it used to read "the package never spawns a process", which a
control plane for a laptop cannot honestly say. It now describes the mechanism that
replaces that claim, and rates the residual **Medium** because the guard that backs
it parses names. The genuinely uncomfortable row is **16** — API-key protection is
optional by default and there is no rate limiting — and it is left rated
**High** rather than talked down.

Additions this pass:

- `tests/security/test_dangerous_primitives.py` (8 tests) converts the prose
  claim "there is no subprocess, eval or exec" into an executable guard. It
  parses every module in the package with `ast`, so comments and docstrings
  cannot make it pass or fail spuriously.
- The guard distinguishes `urllib.parse` (string handling, used by
  `providers/real_base.py` to enforce HTTPS) from `urllib.request` (network
  access). That distinction was found by the guard firing on real code and
  being investigated rather than suppressed.
- Network/process imports are banned package-wide behind an **empty**
  `NETWORK_ALLOWED` allowlist, so a future official adapter (C13) requires an
  explicit, reviewable edit rather than silently gaining network access. The
  allowlist relaxes imports only — `eval`/`exec` stay banned even there.

C15 then closed the largest remaining open item with `actionmesh.core.audit`:
an append-only, hash-chained `AuditLog` whose `verify()` recomputes every hash
rather than trusting stored ones. `EventHub(audit=...)` seals each published
event and `record_decision()` chains a `PolicyEngine.explain()` result. The
chain **persists to SQLite** and reopens fail-closed, so tampered history
stops the process instead of being silently inherited. **Audit tampering moved
out of the open list and into row 20**, with its residual risk stated plainly:
a chain stored beside its own data resists partial tampering, not a full
rewrite — that needs an external anchor.

## Invariants

These are the properties the platform guarantees. Each has a test.

```text
AI proposes.  Policy decides.  User controls.
Executor acts.  Verifier confirms.  Audit remembers.
```

1. **An unknown action can never resolve or execute.** `resolve()` raises.
2. **An unknown permission can never authorize.** The catalog is closed;
   anything outside it is a DENY.
3. **The LLM cannot authorize itself.** Authorization is computed from the
   registry definition, the actor, the policy and server-side consent — never
   from action parameters.
4. **Risk cannot be downgraded.** Trusted metadata can only raise it.
5. **DENY cannot execute**, and cannot be upgraded by approval state.
6. **Required approval cannot be bypassed.** CRITICAL always asks; the
   `allow_high_risk` opt-in is server-side only and does not apply to CRITICAL.
7. **Consent cannot be wildcarded**, and expired consent authorizes nothing.
8. **Malformed input fails closed.** Unreadable schemas raise rather than being
   skipped, because a silently-ignored schema is an open door.
9. **Graphs cannot contain cycles**, so the executor cannot be trapped in one.
10. **Ambiguity resolves to the most restrictive outcome** — in policy rules, in
    graph node config, and in deserialization.
11. **Audit history is tamper-evident.** Editing, deleting, reordering or
    re-sealing any record makes `AuditLog.verify()` report the exact `seq` where
    the chain broke.
12. **An effect that was dispatched is never reported as clean.** A node that reached the
    provider without independent confirmation — refused rollback, a run that parked, or a call
    interrupted by a cancellation — stays `verified: false`/`null`, is named in a
    `compensation.unconfirmed` event and sets `compensation_required`. It is never auto-undone
    (the effect may not exist), never retried because someone pressed Cancel, and never
    replayed from the ledger as a success.

## Secrets

- No credential, API key, token or private key exists anywhere in this
  repository.
- No module reads `os.environ` for secrets, because no module needs one yet.
- When C13 adds real integrations, credentials must come from environment
  variables or a secret store, must never be committed, and must never appear
  in logs, audit records, error messages or API responses.

## What is honest about the current state

The core and simulated providers are local logic. There is:

- no provider network I/O,
- no provider filesystem I/O outside the interpreter,
- no `subprocess`, `eval` or `exec`,
- no dynamic imports driven by untrusted input,
- no credentials.

That is not an accident — it is what lets the core security properties above be
stated precisely today. The API, SQLite stores, and deployment surfaces now
exist, so each deployment must re-run the threat model and add environment-level
controls (C14/C15).

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
