# Repository Map

*Generated from the actual tree. Do not list imaginary files — every path
below was verified to exist.*

## Top level

| Path | Purpose | Status |
|---|---|---|
| `actionmesh/` | Python backend + laptop agent + VoiceOS | Production |
| `desktop/` | Electron app (`app/`, `ui/`, `test/`) | Production |
| `tests/` | pytest suites (`unit/`, `security/`, `integration/`, `e2e/`, `fixtures/`) | Tests |
| `docs/` | Existing design docs (see note) | Documentation |
| `scripts/` | `build_apk.py`, `snapshot.py`, `bundle_runtime.py`, `make_icons.py`, `android_toolchain.ps1`, `fetch_resumable.ps1` | Tooling |
| `android/` | WebView APK shell, `android/README.md` | Production |
| `PROJECT.md`, `CHANGELOG.md`, `README.md`, `SECURITY.md` | Existing root docs | Documentation (see discrepancies) |
| `.env.example` | 33 `ACTIONMESH_*` env vars (host/port, DB, API key, LLM, agent, workspace, commands, pairing) | Config template |
| `pyproject.toml` | Python ≥3.11, `fastapi>=0.115`; dev: pytest/ruff/mypy/httpx; server: uvicorn | Config |
| `desktop/package.json` | Electron 45.2.1, electron-builder 26.18.0; scripts: `start`, `test`, `pack`, `dist:*` | Config |
| `Dockerfile` | Container image | Packaging |
| `pytest-run.txt` | Last recorded run fragment: 40 passed (a single-file run, **not** the full suite) | Historical |

## `actionmesh/` (Python backend)

| File | Purpose | Important symbols | Status |
|---|---|---|---|
| `api.py` (2,540 lines) | FastAPI app: all HTTP routes, auth boundary, `_context()`, `_stream_principal`, `_record_scope` | `create_app` (implied), route handlers | Production |
| `cli.py` | CLI: `capabilities`, `dashboard`, `serve`, `config`, `doctor` | `main`, `_parser`, `_doctor` | Production |
| `dashboard.py` (1,125) | Dependency-free dashboard page + PWA assets | page handlers | Production |
| `voice.py` (1,162) | `GET /voice` page (`page()`) | `page` | Production |
| `sdk.py` | Developer SDK facade (C12) | — | Production |
| `protocol/models.py` (441) | AMP/1.0 models: `Action`, `GraphNode`, `ActionGraph`, `Identity`, `RiskLevel`, enums | `MeshModel` | Production |
| `protocol/validate.py` | `validate_action` — never accepts `action_id`, forces `state=CREATED` | `validate_action` | Production |
| `protocol/graphs.py`, `version.py` | Graph protocol, AMP/1.0 version negotiation | — | Production |
| `core/registry/registry.py` | `ActionRegistry`: register/get/versions/discover/resolve | `ActionRegistry` | Production |
| `core/registry/defs.py` | `check_params` — fails closed on undeclared parameters | `check_params`, `validate_definition` | Production |
| `core/registry/seeds.py` | 16 mock seed definitions | `seed_definitions` | Production |
| `core/permissions/catalog.py` | Closed scope catalog (`_SCOPES`), `is_known/is_concrete/is_namespace/is_wildcard` | `permission()`, `permission_scope()` | Production |
| `core/permissions/engine.py` | `PolicyEngine.evaluate` — the only authorization decision producer | `PolicyEngine` | Production |
| `core/permissions/policy.py`, `risk.py`, `matching.py`, `context.py`, `store.py` | Default policy, risk escalation (`max(action, definition)`), grant matching, `build_context`, `ConsentStore` | — | Production |
| `core/graph/` | `GraphBuilder`, validator (cycles/structure), `execution_order`, serialization, pre-flight authorization | — | Production |
| `core/executor/engine.py` (2,039) | `ExecutionEngine`: `execute`, `resume`, `cancel`, `request_cancel`, lease sweeper, reconciliation, `_run_node` | `ExecutionEngine`, `ExecutionResult`, `NodeOutcome` | Production |
| `core/executor/lifecycle.py`, `idempotency.py`, `serialization.py` | Lifecycle state, idempotency, `safe_result_view` | `safe_result_view` | Production |
| `core/verification/engine.py`, `verdicts.py`, `recovery.py` | `VerificationEngine`, `VerificationOutcome`/`decide`, `RecoveryPlanner`, `CompensationRunner` | — | Production |
| `core/audit/log.py`, `sqlite.py`, `anchor.py` | `AuditLog` (hash chain), `SqliteAuditBackend`, `FileAuditAnchor`/`verify_anchor` | `AuditLog`, `AuditRecord` | Production |
| `core/storage.py` | `SQLiteStore`: `execution_records`, `mesh_events`, `execution_body`, `agent_dispatch` (v0→v3 migrations) | `SQLiteStore` | Production |
| `core/events.py`, `streaming.py` | `EventHub.publish(transient=…)`, bounded in-memory transient capture, `subscribe` | `EventHub`, `MeshEvent` | Production |
| `core/credentials.py`, `secret.py` | Credential store; fail-closed secrets | — | Production |
| `core/identity.py`, `pairing.py`, `devices.py` | `DeviceIdentity`/`SQLiteIdentityStore`, `PairingService` (ceiling fixed at issuance), device registry | — | Production |
| `core/planner.py` | Intent → untrusted candidate plan (proposes only) | `plan_to_graph` | Production |
| `core/config.py`, `output.py`, `observability.py`, `ratelimit.py`, `memory.py`, `scope.py` | Config, RGB/output views, metrics, rate limiting, entity memory, scoping | — | Production |
| `providers/base.py` | `ActionProvider`, `ProviderResult`, `ProviderError` | — | Production |
| `providers/mock_base.py` | `MockHarnessMixin` — harness boundary; `observe()` re-reads; `verify()` = `verified=False` | — | Production |
| `providers/__init__.py`, `messaging.py`, `calendar.py`, `files.py`, `notifications.py`, `payments.py`, `misc.py` | Mock domain providers; `default_mock_providers()` | `MockProvider`, … | Production (simulated) |
| `providers/real_base.py` | `RealProviderBase`, `IntegrationConfig` — safety boundary for real vendors | — | Production (boundary only) |
| `voiceos/orchestrator.py` | `Orchestrator.advance` — step-by-step task execution through the engine | `Orchestrator` | Production |
| `voiceos/llm.py` (927) | `AnthropicProvider`, `AnthropicAnalyst`, `DesktopProvider`, `DesktopAnalyst`, `UnwiredProvider`, `validate_proposal`, `LlmPlanner` | `LlmConfig`, `validate_proposal` | Production |
| `voiceos/model_bridge.py`, `transport.py`, `reasoning.py`, `tasks.py`, `workflows.py` | Desktop bridge config, transport, reasoning, task templates (`match_template`), workflows | — | Production |
| `voiceos/website_workflows.py` (649) | Site-specific workflow templates (restaurant/train fixtures) | — | Production |
| `voiceos/automation_*.py`, `workflow_draft.py`, `diagnosis*.py`, `targeting.py`, `store.py`, `models.py` | Automation draft/run/schedule/store, diagnosis flow, device targeting, sessions | `TaskSession`, `TaskStep` | Production |

## `actionmesh/agent/` (laptop agent — the only real OS access)

| File | Purpose | Important symbols | Status |
|---|---|---|---|
| `provider.py` (2,702) | `LaptopProvider`: `SUPPORTED == definitions.IMPLEMENTED` (test-asserted), `_perform`, `_execute_async`, `_effects`, `observe()` | `LaptopProvider` | Production |
| `definitions.py` (819) | `device_definitions()` — device/browser/files/media/booking actions, `CAPABILITIES`, `IMPLEMENTED`, `REQUIRING_*` gates, `register_device_actions`, `advertise()` | — | Production |
| `browser.py` (1,041) | `BrowserDriver`, `FakeDriver`, `BridgeDriver`, `BrowserEngine`, `PageFacts` | — | Production |
| `browser_ipc.py` (544), `browser_bridge.py` (179) | Python↔Electron browser IPC | — | Production |
| `files.py` (719) | `FilesEngine`, `Workspace`, `_open_fd`, `_resolve`, `_classify` | — | Production |
| `media.py` (929) | `MediaDriver`, `FakeMediaDriver`, `MprisMediaDriver`, `PlayerInfo`, `MediaEngine` | — | Production (real MPRIS **not live-verified**) |
| `commands.py` (547), `processes.py` (581), `ides.py`, `device_screen.py` (1,219), `projects.py`, `paths.py`, `presence.py`, `runner.py`, `config.py`, `verification.py`, `errors.py` | Shell commands, process registry (agent-issued ids only), IDE, screen/GUI, projects, paths, presence, runner, agent config (fail-closed), `observe()` verification | `ProcessRegistry` | Production |
| `agent/config.py` | Agent config: workspace root, allowed/denied commands, interpreter flag — all fail-closed | — | Production |

## `desktop/`

| File | Purpose | Important symbols | Status |
|---|---|---|---|
| `app/main.js` | Main process: windows, backend lifecycle, IPC handlers (`ipcMain.handle`) | 42 handlers | Production |
| `app/preload.js` | `contextBridge.exposeInMainWorld('actionmesh', …)` — the only renderer bridge | — | Production |
| `app/backend.js` | Backend spawn/supervise | — | Production |
| `app/provider_bridge.js`, `providers.js`, `settings.js` | Provider credential bridge + config UI logic + settings | — | Production |
| `app/browser_window.js`, `browser_channel.js`, `browser_bridge.js` | Browser `WebContentsView`, partition `actionmesh-web-drive`, hardened `webPreferences` (`sandbox/contextIsolation/nodeIntegration:false`) | — | Production |
| `app/*.test.js` | `providers.test.js`, `provider_bridge.test.js` (run by `npm test`) | — | Tests |
| `ui/index.html`, `ui/app.js`, `ui/voice.js`, `ui/about.html`, `ui/about.js`, `ui/styles.css` | Renderer: Home/Voice/Tasks/Automations/History/Devices/Settings views; light theme tokens (`--bg:#F7F9F5`, `--green:#16834A`, …) | — | Production |
| `test/` | `pipeline.test.js`, `backend_claim.test.js`, `purpose_ipc.test.js`, `purpose_legacy.test.js`, `ui_render.test.js` + stubs (`backend_stub.js`, `electron_stub.js`, `dom_harness.js`, `main_harness.js`, `browser_bridge_stub.js`, `provider_bridge_stub.js`) | — | Tests |
| `scripts/start.js` | `npm start` entry | — | Tooling |

## `tests/`

| Directory | Contents | Status |
|---|---|---|
| `unit/` | ~73 files: registry, permissions, executor, verification, audit, storage, events, devices, pairing, providers, API, CLI, SDK, browser (agent/bridge/files/media), VoiceOS, automations, credentials, ratelimit, snapshot, docs-freshness | Tests |
| `security/` | `test_dangerous_primitives.py` (AST guard), `test_model_authority.py`, `test_submit_bypass.py`, `test_browser_security.py`, `test_device_end_to_end.py`, `test_event_stream.py`, `test_graph_invariants.py`, `test_pairing_adversarial.py`, `test_record_scoping.py` | Tests |
| `integration/` | `test_ai_voiceos_pipeline.py` | Tests |
| `e2e/` | `test_c42_vertical_slices.py` (C42 fixtures) | Tests |
| `fixtures/` | `pizza.html`, `train.html`, `checkout.html`, `evil.html` + README | Test data |

## Existing documentation (`docs/`)

`architecture.md`, `security.md`, `permissions.md`, `registry.md`,
`action-graph.md`, `providers.md`, `provider-contract.md`,
`provider-requirements.md`, `provider-evidence.md`,
`provider-decision-readiness.md`, `voiceos.md`, `audit.md`, `rest-api.md`,
`deployment.md`, `c41-model-bridge-contract.md`, `c42-execution-contract.md`,
`c42-per-purpose-providers.md`. These remain the design record; the new
`docs/project/` package consolidates and cross-links them without
duplicating them.
