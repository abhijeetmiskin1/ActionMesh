# VoiceOS

VoiceOS is a voice interaction layer **on ActionMesh**. It is a client of
the same API, planner, consent, and approval machinery — with **no engine
of its own**. "VoiceOS presses; it does not decide." (Source:
`docs/voiceos.md`, `PROJECT.md`.)

Current conversational behavior: the configured model can answer questions and ask
for missing details without creating a session. The same validated plan response
carries a bounded optional `reply`; actions still travel through the pipeline below.
Only up to three prior user messages are sent as context, never assistant observations.

## The route from a spoken command to an executed operation

```text
1. Speech recognition (renderer, desktop/ui/voice.js)
   — configured Audio provider through private IPC; editable transcript; text fallback
2. POST /voiceos/messages  (api.py)
3. Task template match — voiceos/tasks.py::match_template
   OR LLM plan — voiceos/llm.py::LlmPlanner.plan (untrusted)
4. validate_proposal (llm.py:177) — accepts only
   {"actions":[{"type","parameters"}]}; entries with extra fields
   are DROPPED whole; unregistered types are dropped
5. plan_to_graph (core/planner.py) — re-validates; unmapped items
   recorded, not dropped
6. ActionGraph → PolicyEngine (ALLOW/DENY/ASK)
7. If ASK: approval (same POST /approvals as the control room)
8. Orchestrator.advance (voiceos/orchestrator.py) — one step at a time
   through ExecutionEngine; each step is an ordinary ActionMesh graph
9. Verification (independent re-read) → audit
10. Spoken feedback (renderer TTS, desktop/ui/voice.js)
```

## Components

| Concern | Implementation |
|---|---|
| Voice input / speech recognition | `desktop/ui/voice.js` (Audio-provider capture through main IPC); web `/voice` uses bounded transient cloud dictation through the existing private provider bridge |
| Transcript editing | Editable textarea in the Voice view (`#voiceTranscript`); "Send Transcript" button |
| Text fallback | The same textarea + command input on Home/Tasks; quick-grid commands (`#quickGrid`) |
| Spoken feedback | Renderer speech synthesis; `#voiceStopSpeaking` |
| Conversation state | `voiceos/models.py` `TaskSession`/`TaskStep`; `voiceos/store.py`; `GET /voiceos/conversation`, `GET /voiceos/sessions`, `GET /voiceos/sessions/{id}` |
| Intent interpretation / reasoning | `voiceos/reasoning.py`; `voiceos/diagnosis.py` + `diagnosis_flow.py` ("why did that fail") |
| Proposal generation | `voiceos/llm.py` (`AnthropicProvider.plan`, `DesktopProvider`), `core/planner.py` deterministic fallback |
| Validation | `validate_proposal` (llm.py:177) — the three validation doors (see [ACTIONMESH.md](ACTIONMESH.md)) |
| Task orchestration | `voiceos/orchestrator.py::Orchestrator.advance` — step loop, `_run_step`, `_target_for`, `_bind_parameters`, `_apply_result`, condition checks (`CONDITION_KEYS = {exit_code, state, verified, error}`), `_declared_risk`, `_provider_for` |
| Workflow templates | `voiceos/tasks.py` (`TaskTemplate`, `load_templates`, `TemplateError`), `voiceos/workflows.py`, `voiceos/workflow_draft.py` |
| Conditional execution | Step conditions on `exit_code/state/verified/error`; `fold_records` folds prior step records into the session |
| Provider selection | Environment-driven (`ACTIONMESH_LLM_PROVIDER`, `..._API_KEY`, `..._MODEL`); per-purpose lanes in the desktop app (`provider:*` / `purpose:*` IPC); deterministic fallback when no provider is configured |
| Fallback behavior | `UnwiredProvider` — a configured-but-unwired provider says so and proposes nothing; the planner falls back to deterministic templates; the UI shows `data-mode="deterministic"` |
| Integration with ActionMesh | Every step is an ordinary graph executed by `ExecutionEngine` under the caller's own credential; consent/approval surfaces are **unreachable** from the voice surface (consent is a separate, deliberately unbridged surface) |

## References ("this", "that file", "the selected restaurant", "the previous result")

- **Contextual references** are resolved by the orchestrator's
  `_bind_parameters` / `_collect` against the session's folded records
  (`fold_records`): prior step results are bound into later steps' parameters.
- **Website workflows** (`voiceos/website_workflows.py`) resolve
  selections (e.g. a chosen restaurant) from the browser snapshot refs —
  opaque session-scoped ids, not model-invented names.
- **Ambiguity handling:** unresolvable references fail the step with a
  `REQUIRES_USER` outcome rather than guessing (honest reporting rule,
  `docs/c42-execution-contract.md` §0.7).
- **Missing information:** the template/LLM proposal is recorded with
  notes (`LlmProposal.notes`); unmapped items are recorded, not dropped.
- **Session scoping:** all state lives in `TaskSession` keyed by
  `session_id`; records are actor-scoped (`_record_scope`).
- **Reference expiration:** browser refs are session-scoped and rebuilt on
  page change (`BrowserEngine._rebuild_refs`, `_resolve`); `remove_element`
  invalidates ids (`FakeDriver.rememove_element`).
- **Permission separation:** the voice surface cannot modify consent and
  cannot approve — every privileged step is its own press (run = a click
  on a previewed plan; approve = a click naming one `action_id`).

## What VoiceOS can and cannot do

**Can:** propose plans from speech or text, run them step-by-step through
the full ActionMesh pipeline, diagnose failures (`POST
/voiceos/sessions/{id}/diagnose`), draft automations (`POST
/automations/draft`), run/cancel sessions, speak results.

**Cannot:** grant permissions, lower risk, bypass approval, reach the
consent surface, persist microphone recordings, or invent parameters/steps
outside registered definitions. There is no second execution engine.

## API surface (VoiceOS)

`GET /voiceos/audio/status`, `POST /voiceos/audio/transcribe`,
`POST /voiceos/messages`, `POST /voiceos/guide`, `GET /voiceos/status`,
`GET /voiceos/conversation`, `GET /voiceos/sessions`,
`GET /voiceos/sessions/{session_id}`, `POST
/voiceos/sessions/{session_id}/run`, `POST
/voiceos/sessions/{session_id}/diagnose`, `POST
/voiceos/sessions/{session_id}/cancel`, plus `/automations/*`. See
[API_REFERENCE.md](API_REFERENCE.md).
