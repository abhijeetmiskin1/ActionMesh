# API Reference

*Derived from `actionmesh/api.py` (2,540 lines). No route is documented
here unless it exists in source. Auth: every route runs through
`_context()` (actor identity) and `_record_scope` (record scoping);
client-supplied `grants` are **rejected**. Loopback/event-stream
principal checks are server-side.*

| Method | Path | Purpose | Source |
|---|---|---|---|
| GET | `/health` | Liveness | `api.py:1006` |
| GET | `/ready` | Readiness | `api.py:1010` |
| GET | `/dashboard` | Dashboard page (HTML) | `api.py:1049` |
| GET | `/voice` | Voice page (HTML) | `api.py:1053` |
| GET | `/manifest.webmanifest` | PWA manifest | `api.py:1068` |
| GET | `/sw.js` | Service worker (shell only) | `api.py:1072` |
| GET | `/icon.svg` | PWA icon | `api.py:1083` |
| GET | `/metrics` | Metrics | `api.py:1091` |
| GET | `/capabilities` | Registered actions | `api.py:1095` |
| GET | `/devices` | List devices | `api.py:1107` |
| GET | `/devices/{device_id}` | One device | `api.py:1112` |
| POST | `/devices/{device_id}/revoke` | Revoke a device | `api.py:1120` |
| POST | `/pairing/code` | Mint a pairing code (laptop only) | `api.py:1171` |
| POST | `/pairing/revoke` | Revoke pairing codes | `api.py:1213` |
| POST | `/pair` | Redeem a pairing code | `api.py:1220` |
| POST | `/plan` | Intent → candidate graph (untrusted) | `api.py:1254` |
| POST | `/execute` | Execute a validated graph | `api.py:1273` |
| GET | `/executions/{execution_id}` | One execution (safe view) | `api.py:1293` |
| GET | `/executions` | List executions | `api.py:1303` |
| POST | `/executions/{execution_id}/cancel` | Cancel | `api.py:1617` |
| GET | `/events/stream-ticket` | Mint an SSE ticket | `api.py:1359` |
| GET | `/events` | SSE event stream | `api.py:1377` |
| GET | `/approvals` | Pending approvals | `api.py:1456` |
| POST | `/approvals` | Approve/deny one `action_id` | `api.py:1474` |
| POST | `/consents` | Create a consent/grant | `api.py:1531` |
| DELETE | `/consents/{scope}` | Revoke a consent | `api.py:1565` |
| GET | `/consents` | List consents | `api.py:1594` |
| GET | `/audit` | Audit records | `api.py:1640` |
| GET | `/audit/verify` | Verify the hash chain | `api.py:1662` |
| POST | `/voiceos/messages` | VoiceOS message turn | `api.py:1801` |
| POST | `/voiceos/guide` | Command guide/help | `api.py:1881` |
| GET | `/voiceos/status` | Provider/planner status | `api.py:1946` |
| GET | `/voiceos/conversation` | Conversation state | `api.py:2050` |
| GET | `/voiceos/sessions` | List task sessions | `api.py:2075` |
| GET | `/voiceos/sessions/{session_id}` | One session | `api.py:2093` |
| POST | `/voiceos/sessions/{session_id}/run` | Run/advance a session | `api.py:2105` |
| POST | `/voiceos/sessions/{session_id}/diagnose` | Diagnose a failed session | `api.py:2137` |
| POST | `/voiceos/sessions/{session_id}/cancel` | Cancel a session | `api.py:2230` |
| POST | `/automations/draft` | Draft an automation | `api.py:2287` |
| GET | `/automations` | List automations | `api.py:2312` |
| GET | `/automations/status` | Scheduler status | `api.py:2331` |
| GET | `/automations/runs` | Automation runs | `api.py:2340` |
| POST | `/automations` | Create an automation | `api.py:2355` |
| GET | `/automations/{workflow_id}` | One automation | `api.py:2382` |
| PUT | `/automations/{workflow_id}` | Update an automation | `api.py:2387` |
| POST | `/automations/{workflow_id}/enabled` | Enable/disable | `api.py:2422` |
| POST | `/automations/{workflow_id}/run` | Run now | `api.py:2473` |
| DELETE | `/automations/{workflow_id}` | Delete | `api.py:2524` |

## Error semantics

- Validation failures (unknown action, unknown parameter, client-supplied
  grants) → 4xx, fail-closed, audited.
- `DENY` decisions → the execution is denied and recorded; nothing runs.
- `ASK` decisions → the node parks in `awaiting_approval()` until
  `POST /approvals` names its `action_id`.
- Provider failures → `ProviderError` → node `failed`, result carries the
  failure kind; compensation may follow.
- Unverified/`REQUIRES_USER` → reported as such, never as success.

## SSE / event formats

- `GET /events/stream-ticket` → `{ticket}` (actor-scoped, expiring).
- `GET /events?ticket=…` → `text/event-stream` of `MeshEvent` JSON:
  `{execution_id, type, payload, timestamp}`. Non-transient events are
  persisted (`mesh_events` table); transient events (live process
  output) reach only subscribers and a bounded in-memory buffer
  (`core/events.py`: `MAX_TRANSIENT_EXECUTIONS`), never the audit chain.

## Related tests

`tests/unit/test_api.py`, `test_device_api.py`, `test_pairing_api.py`,
`test_audit_api.py`, `test_security_api.py`, `test_voiceos.py`,
`test_automations.py`.
