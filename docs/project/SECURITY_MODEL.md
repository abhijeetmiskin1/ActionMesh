# Security Model

*Every claim below names its enforcement point. No control is claimed
without a source implementation.*

## Threat model

- The **model is untrusted.** Its output can be prompt-injected by page
  content, files, or conversation. It proposes; it never authorizes.
- **Browser content is untrusted** (`actionmesh/agent/browser.py`:
  `_detect_gate`, `_redact` — page text passes through a gate detector
  before reaching the model).
- **The client is untrusted** as an authority source: a request body can
  name no grants, roles, or risk decisions (`api.py::_context`).
- **The local network is untrusted:** loopback trust is enforced
  server-side (`_stream_principal`), never in page JavaScript.

## Controls

| Control | Implementation file | Important symbol | Enforcement point | Test coverage | Known limitation |
|---|---|---|---|---|---|
| AI cannot grant permissions | `actionmesh/api.py` | `_context()` raises on `grants` | Every HTTP route | `tests/security/test_model_authority.py`, `test_dangerous_primitives.py` | — |
| AI cannot elevate authority | `core/permissions/risk.py` | risk = `max(action, definition)` | `PolicyEngine.evaluate` | `test_policy_engine.py`, `test_registry.py` | — |
| Unknown actions fail validation | `protocol/validate.py` | `validate_action` | `POST /plan`, `/execute` | `test_graph_invariants.py` | — |
| Unknown parameters fail validation | `core/registry/defs.py` | `check_params` | `ActionRegistry.resolve` | `test_registry.py` | — |
| LLM proposal door | `voiceos/llm.py:177` | `validate_proposal` | Planner output | `test_llm_generation.py`, `test_model_bridge.py` | — |
| CRITICAL requires explicit approval | `core/permissions/engine.py` | `PolicyEngine` (`ASK`) | Per-node, per `action_id` | `test_permissions.py` | — |
| Grant ≠ approval for a specific execution | `core/permissions/store.py` | `ConsentStore` | Approval is per-execution | `test_submit_bypass.py` | — |
| Unverified execution never shown as verified | `core/verification/` | `VerificationStatus`, `_report_unconfirmed` | Executor → result view | `test_verification.py` | — |
| Browser content untrusted | `agent/browser.py` | `_detect_gate`, `_redact` | Before model/result | `test_browser_security.py` | Gate heuristics are not a proof |
| Sensitive transient content not in permanent audit | `core/events.py`, `core/executor/serialization.py` | `EventHub.publish(transient=True)`, `safe_result_view` | Event/result boundary | `test_event_stream.py`, `test_streaming.py` | Transient buffer is in-memory and bounded |
| Workspace confinement | `agent/paths.py`, `agent/files.py` | `Workspace`, `_resolve`, `_open_fd` | Every file op | `test_agent_paths.py`, `test_files_agent.py` | — |
| Path traversal / symlink safety | `agent/files.py` | `_open_fd` (fd-pinned) | Open-time | `test_agent_paths.py` | — |
| TOCTOU protection | `agent/files.py` | `_relocate_locked`, fd-based I/O | Relocate/edit | `test_files_agent.py` | — |
| IPC security | `desktop/app/preload.js` | single `contextBridge` | Renderer↔main | `desktop/test/purpose_ipc.test.js` | — |
| Electron sandbox | `desktop/app/main.js:242,953,1016` | `webPreferences` | Window creation | `desktop/test/` | — |
| Context isolation | `desktop/app/main.js` | `contextIsolation: true`, `nodeIntegration: false` | Window creation | — | — |
| CSP | Renderer HTML | *(verify before claiming)* | — | — | **Not verified in source during this pass — treat as unverified** |
| Credential storage | `core/credentials.py`, `core/secret.py` | fail-closed secret handling | Load-time | `test_credentials.py`, `test_secret.py` | — |
| Secrets in logs/events | `agent/provider.py`, `core/output.py` | `_stream_content`, safe views | Boundary crossing | `test_real_provider_boundary.py` | — |
| Actor/device identity | `core/identity.py`, `core/devices.py` | `DeviceIdentity`, `SQLiteIdentityStore` | Enrollment | `test_identity.py`, `test_devices.py` | — |
| Pairing ceiling fixed at issuance | `core/pairing.py`, `api.py::pairing_code` | `PairingService.issue` | Minted on laptop only | `test_pairing*.py`, `test_pairing_adversarial.py` | Remote pairing gated by `ACTIONMESH_ALLOW_REMOTE_PAIRING` |
| Revocation | `core/identity.py::revoke`, `api.py` `POST /devices/{id}/revoke`, `DELETE /consents/{scope}` | — | Explicit | `test_device_api.py` | — |
| Audit integrity | `core/audit/log.py`, `anchor.py` | `compute_hash`, `AuditLog.verify`, `FileAuditAnchor` | Append/verify | `test_audit*.py` | External anchor file is local |
| SSE authorization | `api.py` | `_stream_principal`, stream tickets | Ticket minting | `test_event_stream.py`, `test_record_scoping.py` | — |
| Session expiry | `core/pairing.py` | `PairingCode.is_expired` | Redeem-time | `test_pairing.py` | — |
| Cancellation/retry safety | `core/executor/engine.py` | `cancel`, `request_cancel`, `max_attempts=1` for side effects | Executor | `test_executor.py`, `test_durable_execution.py` | — |
| No outbound network client | package-wide | test-enforced (`urllib.request`/`socket`/`httpx` forbidden) | AST guard | `test_dangerous_primitives.py` | Provider transports are the approved exception |
| Process safety | `agent/processes.py` | `ProcessRegistry` (agent-issued ids only, no pid killing) | Spawn/kill | `test_processes.py` | — |
| Fail-closed config | `agent/config.py`, `core/secret.py` | unset root ⇒ no files; unset flag ⇒ no terminal | Startup | `test_config.py` | — |
| Submit/bypass resistance | `api.py` | `_record_scope`, actor scoping | Every route | `test_submit_bypass.py`, `test_record_scoping.py` | — |

## Invariants (confirmed by source)

- AI cannot grant permissions. — `_context()` raises on `grants`.
- AI cannot independently elevate authority. — risk = `max(action, definition)`.
- Unknown actions fail validation. — `validate_action`.
- Unknown parameters fail validation. — `check_params`.
- CRITICAL operations require explicit approval. — `PolicyEngine` `ASK`.
- A permission grant is not an approval for a specific critical
  execution. — consent vs. per-`action_id` approval are separate.
- Unverified execution is not presented as verified. — `REQUIRES_USER`.
- Browser content is untrusted. — `_detect_gate` / `_redact`.
- Sensitive transient content is not stored in permanent audit records.
  — `publish(transient=True)` bypasses audit + store.
- Workspace confinement cannot be bypassed by model-generated paths.
  — `Workspace` + fd-pinned opens.

## Known limitations / unverified

- **CSP** was not verified in source during this pass.
- `MprisMediaDriver` was not exercised against a live player (source
  comment `media.py:326-328`).
- The `evil.html` fixture exists for browser-security tests; live
  prompt-injection against a real site is not demonstrated.
- Historical reports (C42 counts) are labeled historical; see
  [TESTING_AND_ACCEPTANCE.md](TESTING_AND_ACCEPTANCE.md).
