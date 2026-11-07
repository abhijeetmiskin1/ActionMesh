# VoiceOS

VoiceOS is the voice **interaction** layer over ActionMesh. It has no execution engine,
no policy engine and no approval path of its own: it puts a microphone and a speaker in
front of the pipeline that already exists, and inherits every decision that pipeline
makes.

```text
SPEECH → TRANSCRIPT → CONVERSATION → TASK SESSION → START → STEP (execute → approve
        → verify) → observed result → next step or stop → summary → VOICE
  VoiceOS drives the interaction; ActionMesh makes every one of those decisions.
```

Cloud recording finishes after a short pause following speech (about one second),
with manual Stop and a 30-second maximum as fallbacks. Eight seconds without detected
speech cancels without upload. Audio level monitoring is local and exists only during
an explicit recording. On desktop, the final spoken transcript is submitted
automatically. Eligible tasks for the local laptop start through the normal task
runner, and VoiceOS reads out the result. Typed requests and other plans stay in
review. The web Voice page retains its separate review flow.

**Current status (2026-11-07):** cloud dictation uses the configured Audio lane;
editable transcripts reach `/voiceos/messages`. The model can answer ordinary questions,
ask clarifications, or propose task steps. Every execution still goes through policy
checks and any required approval. Earlier slice-1 limitations below describe the web
Voice page and historical desktop behavior.

The tested desktop configuration uses 9router at `http://127.0.0.1:20128/api/v1`:
Chat and Reasoning use `groq/openai/gpt-oss-120b`; Audio uses
`groq/whisper-large-v3-turbo`. No model training is required. A bounded JSON schema is
included in compatible-endpoint prompts; model replies are at most 4,000 characters.
Only the current message, up to three prior **user** messages (1,000 characters each),
and registered action names/descriptions with static input names/types enter planning. Assistant diagnostics, file
contents and observed device state are excluded from this conversational context.
Clear conversation also clears the remembered requests.

Ordinary replies and clarifications create no task and run nothing. A dropped or invalid
action cannot be disguised as a conversational success. Model answers are advisory;
they do not establish facts about the live computer. The catalogue budget now includes
all 76 definitions in the full test registry, rather than silently clipping at 60.

---

## The boundary this surface exists inside

`ACTIONMESH: secure execution. VOICEOS: natural interaction.` The rules, in the order that
matters:

1. **A transcript is untrusted input, exactly like an HTTP body.** It reaches `/plan` and
   nothing else. The planner's output has no field for a permission, a risk level, an
   approval state or an identity, so a sentence cannot talk its way into any of them
   (`actionmesh/core/planner.py` is the boundary; `docs/architecture.md` states it).
2. **Every privileged step is its own press.** Running is a click on a plan that was
   previewed first — `runPlan` has exactly one caller, the button, and the recognition path
   stops at `previewPlan`. An approval is a second press, naming the `action_id` the server
   minted. The consent surface is not reachable from this page at all, so it cannot grant
   itself a permission. A parked run is reported as parked: `execution.completed` carrying
   `waiting_permission` means the *request* ended, and is never spoken as an outcome. Each
   of these is asserted against the HTML the server serves.
3. **What the card shows about risk comes from the server.** Each proposed action is
   labelled from `GET /capabilities` — the registry's own `risk_level` and `permissions` —
   never from the proposal's or the transcript's words about itself. An action the registry
   does not know is labelled as unregistered rather than quietly implied safe.
4. **Speech text is rendered as text.** The page has no `innerHTML`; every transcript,
   parameter and note lands via `textContent`. A misheard sentence that happens to contain
   markup cannot become markup.
5. **Nothing here is a second authority.** No new endpoint that grants, approves, revokes
   or executes was added. `/voice` serves a document.

## Microphone and cloud dictation (2026-11-07)

Recording starts only from an explicit microphone press. The web Voice page uses
WebM/Opus capture when available: at most 30 seconds / 128 KiB at 32 kbit/s. A
second press stops capture; Cancel dictation or hiding the page cancels the local
session and discards late answers. Microphone tracks stop before uploading.

The authenticated audio endpoints forward transient audio through the existing
AF_UNIX provider bridge. Only Electron main reads the stored Audio API key and
calls the configured transcription provider. Python and the page never receive
that credential. Audio is not stored, audited, added to a conversation, or treated
as an action. Uploaded audio cannot be recalled after cancellation. The page
explains this boundary before recording.

Dictation inserts editable text. A manual edit made while transcription is pending
is preserved. Pressing Ask VoiceOS is still required to propose anything; running
and approvals remain separate per-action presses. Neither cloud nor native
recognition auto-submits the transcript. Browser-native recognition is a fallback
when capture APIs are absent, using the browser vendor's speech service. Typing
always remains available. Secure-context and platform microphone permissions are
required; Android WebView/physical-device acceptance is not implied.

The desktop's web-page session grants audio-only microphone access only to the
current backend's Voice page. Camera, other origins and other pages are refused.
The session attaches its API header only to the exact backend origin and port.

## Historical slice-1 omissions (superseded)

The following paragraphs record the original slice-1 design, not current provider
or device support. See CURRENT_STATUS.md for current acceptance evidence.


* **No real LLM provider.** The seam exists (`actionmesh/voiceos/llm.py`), the
  configuration exists, and the answer is a visible note rather than a call — see *Why no
  model is called*.
* **No "do it" anaphora across tasks.** References resolve to items the user was shown in
  this conversation; a pronoun pointing at anything else is answered as a question.
* **No barge-in.** Speech is stopped with the button; it does not stop itself when the user
  starts talking.
* **No device picker.** One laptop agent serves one server, so the header names that agent
  rather than offering a control that would change nothing.
* **No wake word.** "Hey Computer" is continuous listening by another name, which is the
  first thing this surface refuses to do.
* **Language is pinned to `en-US`** in the adapter; there is no language negotiation.
* **Offline:** the page is in the installed shell's cache list, so an offline handset opens
  it and sees "Service unreachable" — the microphone still works locally, the planner does
  not.

## Historical slice-1 model decision (superseded)

VoiceOS has an LLM provider seam (`actionmesh/voiceos/llm.py`), configuration for it
(`ACTIONMESH_VOICEOS_LLM_PROVIDER`, `..._MODEL`, `..._TIMEOUT_S`), and validation of
whatever a provider returns. What it does not have is a transport: the package bans
outbound network imports outside a reviewed allowlist, so naming a provider in the
environment produces **no proposal and a visible note**, and the deterministic reasoning
layer answers instead.

That is the honest state, not a stub waiting to be papered over. The alternative — a
provider class that looks wired and quietly returns canned text — would make every later
claim about "the AI understood it" unverifiable. Wiring a real one means an approved
endpoint, credentials from the environment, an entry in `AVAILABLE_PROVIDERS`, and a
deliberate widening of the network guard in review.

Model output is stripped to `{type, parameters}` by `validate_proposal`: an entry that
asserts its own risk level, permission or approval is dropped whole, because honouring
even the "harmless" part of it would teach the system that those fields are negotiable.

## Verifying this file

```bash
python -m unittest tests.unit.test_voice      # the surface's invariants, 55 tests
python -m unittest tests.unit.test_voiceos    # sessions, steps, references, the LLM seam
python -m unittest tests.unit.test_docs_freshness
```

The in-browser pass for slice 1 is recorded on `TASK_BOARD.md` under C32: what was driven
with a real recognizer stubbed (no microphone hardware was available in that environment),
which requests the page actually made, and what therefore remains unverified.
