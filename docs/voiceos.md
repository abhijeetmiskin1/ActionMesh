# VoiceOS

VoiceOS is the voice **interaction** layer over ActionMesh. It has no execution engine,
no policy engine and no approval path of its own: it puts a microphone and a speaker in
front of the pipeline that already exists, and inherits every decision that pipeline
makes.

```text
SPEECH → TRANSCRIPT → CONVERSATION → TASK SESSION → [press] → STEP (execute → approve
        → verify) → observed result → next step or stop → summary → VOICE
  VoiceOS drives the interaction; ActionMesh makes every one of those decisions.
```

**Status: C32 + C33 + C34.** `GET /voice` is a conversation: push-to-talk → transcript →
a proposed **task session** → a press that advances one authorised step → the existing
approval gate → SSE → verification → spoken result, with conditional next steps and
references to what was just observed. A real LLM provider is **not** wired up (see *Why
no model is called*), and neither is a device picker — see *What is not built*.

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

## Microphone: how it is opened, and what it costs

**User-initiated only.** One tap on the microphone opens it for **one utterance**. It is
never started on page load, `continuous` is explicitly `false`, `onend` deliberately does
not restart it (an `onend` that re-opens the recognizer turns one deliberate tap into
permanent listening), and the page stops recognition when it goes to the background. The
control stays clickable while it is listening, because the second tap is how you stop.

**No audio is collected by this product.** The page contains no `getUserMedia`, no
`mediaDevices`, no `MediaRecorder`, no `FormData`, no base64, no `ArrayBuffer` and no
`blob:` — pinned by tests. What leaves the browser is the recognizer's **text result**, and
that text is the only thing the page can send. Nothing is recorded, nothing is stored on
the device, and there is no `localStorage`; the pairing token lives in `sessionStorage` for
the tab, as in the control room.

**The honest part.** Browser-native speech recognition is *the browser vendor's* service.
On desktop Chrome the audio goes to Google's speech servers; on Android it is handled by
Google's speech service; a WebView or a browser without the API has **no** microphone path
at all and falls back to typing. This is stated on the page itself, not buried here,
because "voice, but private" would be a false claim. The ActionMesh server never sees the
audio — but the vendor may.

## Why browser-native recognition, and not the alternatives

The charter asks for an evaluation *before* enabling speech, so this is the record:

| Option | What it would mean | Verdict |
| --- | --- | --- |
| `SpeechRecognition` (Web Speech API) | Zero dependencies, zero new server surface, no audio leaves the ActionMesh host. Privacy boundary moves to the browser vendor. Not available in a plain Android WebView, and Chrome-desktop-only in practice. | **Chosen**, with the vendor disclosure above and a text path that is always usable. |
| Server-side STT (a cloud transcription API) | Outbound network calls from the server, an audio upload path, a credential in the deployment, and ActionMesh becoming the thing that hears you. The repository bans outbound-network imports outside an explicitly reviewed allowlist. | **Refused** for now. It contradicts "no credentials in source", the outbound-network guard, and the privacy posture the dashboard currently has. |
| On-device model (WASM/whisper.cpp binding) | A real dependency or a binary, plus a build step and a memory cost on a phone. | **Refused** for slice 1: "do not introduce unnecessary frameworks or dependencies." Revisit only with a product reason. |
| `MediaRecorder` + a recording button | Would capture audio ourselves and store or forward it. | **Refused**: the charter forbids storing audio by default, and a recorder is a new sensitive capability with no need in this slice. |

The adapter seam exists precisely so this choice is reversible: `SPEECH_ADAPTERS` is a list
of `{name, create}` providers consulted in order, and `createRecognizer()` is the only place
that knows which one answered. A future recognizer — a WebView bridge, an approved cloud
adapter — is one entry in that list, not a rewrite of the page.

## What is not built (and is not pretending to be)

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

## Why no model is called

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
