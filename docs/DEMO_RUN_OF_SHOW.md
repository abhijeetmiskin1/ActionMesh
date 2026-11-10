# VoiceOS Demo Run of Show

A three-minute walkthrough of voice input, real desktop actions, follow-up questions, and visible results. The main flow uses Calculator so the outcome is easy to see and verify. Web search is a second, read-only example.

## Before the demo

Do this once before presenting:

- Open the packaged ActionMesh desktop app and select **Voice Commands**.
- In **Voice readiness**, run **Check setup**. Confirm the backend is ready and macOS microphone permission is granted.
- In **Settings → Providers → Audio**, confirm the transcription connection is ready and the recognition language is **English**.
- In **Approvals**, grant the scopes needed for this walkthrough to the local operator:
  - `system.app.launch` — launch Calculator.
  - `system.app.status` — confirm Calculator is running.
  - `browser.control` and `search.web` — open and search in the controlled browser.
- Confirm the controlled browser opens a public search page. If a sign-in, CAPTCHA, or MFA screen appears during the demo, stop that browser flow and return to the Calculator result. Do not try to bypass it.
- If you want to show the conversational controls at the end, enable **Keep listening after replies**.

Grant only these permissions for the demo. The app may show an approval pause if a permission or per-action approval is missing. That pause is a policy result, not a speech-recognition result.

## Three-minute walkthrough

| Time | What to do | What to say or show |
|---|---|---|
| **0:00–0:20** | Introduce the result screen. | “I can speak or type a request. The task result shows what ran and what the computer independently confirmed.” |
| **0:20–1:15** | Press the microphone and say the two-step command below. Keep **Task Results** visible while it runs. | **“Open Calculator, then is Calculator running?”** |
| **1:15–1:35** | Point to both settled steps and the observation. | Show that Calculator opened, then its running state was checked as a separate step. |
| **1:35–2:30** | Start the follow-up search below. Answer the product question with a short phrase. | **“Check the best deal.”** Then: **“USB-C charger.”** |
| **2:30–3:00** | Show the browser results and close with the evidence boundary. | “This finds search links. It does not claim that a seller, price, or delivered total was verified.” |

## Command 1 · Open and verify Calculator

**Say:**

> Open Calculator, then is Calculator running?

**Expected Task Results:**

1. **Open Calculator** — succeeded and verified.
2. **Check Calculator running** — succeeded and verified, with the observed state `running: true`.

The Calculator app should be visible. Both steps should appear in order. This is the strongest opening because it demonstrates a real desktop action and a separate check of the resulting state.

If the task stops at a permission, show the named permission in Task Results and grant it before retrying. If the recognized text is wrong, correct it in the transcript or use the typed fallback below.

## Command 2 · Ask a follow-up, then search

**Say:**

> Check the best deal.

When Jarvis asks which product, answer:

> USB-C charger.

**Expected result:** Jarvis uses the short answer to prepare a bounded browser search. When the browser and search steps complete, Task Results should show the search navigation and the number of result links observed.

**Be precise about the result:** this flow finds search links. It does not compare verified sellers, confirm current stock, calculate delivery, or identify a guaranteed best price. Keep the demo read-only; do not proceed to checkout.

## Optional close · Natural voice controls

With **Keep listening after replies** enabled, try:

1. **“Jarvis, repeat that.”** The last spoken reply should be repeated without starting a task.
2. **“Jarvis, stop listening.”** The microphone should return to **Idle**.

## Typed fallback

If the room is noisy or speech recognition is unavailable, type the same command in Voice Commands and choose **Send & run**. This uses the same task interpretation and execution path. A typed run demonstrates task execution, but it does not demonstrate live speech recognition.

## What to call out on screen

| Screen state | Explain it as |
|---|---|
| **Listening / Heard** | The microphone is open or the recognized words are visible. Confirm the transcript before discussing the plan. |
| **Running** | The named step is executing. Wait for the outcome before describing it. |
| **Awaiting approval / permission** | The task is paused at a specific control. Show the action and requirement. |
| **Completed · verified** | The independent observation confirmed the result. Point to the observed value. |
| **Failed / unverified** | The result was not confirmed. Say what blocked it; do not present it as a success. |

## Short rehearsal checklist

- Say the Calculator command once at normal speaking volume.
- Check that the transcript preserves **“then”** and both clauses.
- Confirm the app opens and both steps are verified in order.
- Run the product follow-up once; confirm search links appear, or skip this optional flow if the public search page is unavailable.
- If using voice controls, check **repeat that** and **stop listening**.
- Keep the typed command ready as a fallback.

The demo should be described from the Task Results visible during the run. These are expected outcomes, not a claim that a new live rehearsal has already been performed.
