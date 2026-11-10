# VoiceOS Demo Run of Show

A three-minute walkthrough of voice input, real desktop actions, follow-up questions, and visible results. The main flow uses Calculator so the outcome is easy to see and verify. Web search is a second, read-only example.

## Judge demo · Verify a release file without displaying it

Set the desktop Workspace root to a demo folder and grant the local operator `file.read` up to medium risk. Put two small files in that workspace named `release.txt` and `candidate.txt`, both containing `release v1`.

1. In Voice Commands, enter **Compare workspace files release.txt and candidate.txt** and choose **Send & run**. Task Results shows **Files match**.
2. Change only the candidate from `release v1` to `release v2` and run the same command. Task Results shows **Files differ** even though the file size stayed the same.
3. Open **Execution receipt** to show the policy-controlled action and its verification flag. The file comparison returns one boolean. ActionMesh reads both files again to check that the answer still holds.

The comparison accepts regular workspace files up to 256 KiB each. For names with spaces, wrap each path in double quotes. The matching and same-size difference scenarios were verified through the VoiceOS API with real local files.

## Before the demo

Do this once before presenting:

- Start the current checkout with `cd desktop && npm start` from the repo root, or open a packaged build you have rehearsed. Select **Voice Commands**.
- In **Voice readiness**, run **Check setup**. Confirm the backend is ready and macOS microphone permission is granted.
- In **Settings → Providers → Audio**, confirm the transcription connection is ready and the recognition language is **English**.
- In **Approvals**, grant the scopes needed for this walkthrough to the local operator:
  - `system.app.launch` — launch Calculator.
  - `system.app.status` — confirm Calculator is running.
  - `browser.control` and `search.web` — open and search in the controlled browser.
- For the optional search segment, enable **Allow the browser drive (this desktop)** in Settings and confirm the controlled browser opens a public search page. If the search hits sign-in, CAPTCHA, or MFA, return to the Calculator result.
- If you want to show the conversational controls at the end, enable **Keep listening after replies**.

For a policy-first opening, start with `system.app.launch` ungranted and submit **“Open Calculator”**. Show the denied step and its missing permission, grant the scope in **Approvals**, then submit the two-step command. Use an isolated demo profile for this variant. Keep Calculator closed before the opening so the audience can see the change.

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

## Judge showcase · One request across desktop and web

Use the **SHOWCASE** quick command in the current checkout, or say:

> Open Calculator, then check the best deal for USB-C charger, then is Calculator running?

The four ordered steps launch Calculator, open the controlled browser, search for the product, and independently check that Calculator is running. Return to Voice Commands after showing the search page. The **Verification timeline** shows each step's state, the verified step count, the search link count, and the observed app state. Expand **Execution details** for execution IDs and observations.

If a step pauses for permission or browser review, show that state in the timeline. Any steps that have yet to run remain visible.

To show how a user controls execution, enter **“Is Calculator running?”** and choose **Preview plan**. The step stays pending until you choose **Run: Check Calculator**. After it finishes, open **Execution receipt** to inspect the task ID, execution ID, action type, state, and verification flag. Use **Check audit integrity** to verify the backend ledger. The ledger check covers the chain as a whole, and the receipt records the individual task outcome.

To show a plan that adapts to the machine, start with Calculator closed and run **“Ensure Calculator is running”** twice. The first run verifies that Calculator is closed, then launches it through policy and verifies the launch. The second run verifies that Calculator is already running and skips the launch. Open the execution receipt for the second run to show that the skipped step has no execution ID. Both branches were rehearsed through the VoiceOS API on macOS using Clock.

## Command 2 · Ask a follow-up, then search

**Say:**

> Check the best deal.

When Jarvis asks which product, answer:

> USB-C charger.

**Expected result:** Jarvis uses the short answer to prepare a bounded browser search. When the browser and search steps complete, Task Results should show the search navigation and the number of result links observed.

If the follow-up turn is missed, type **“Check the best deal for USB-C charger”** and choose **Send & run**. If the browser is unavailable, use **“Is Clock running?”** as a read-only fallback and show the observed `running` value.

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

## Rehearsal evidence · 10 November 2026

| Flow | Observed result | Coverage |
|---|---|---|
| **Open Calculator** before consent | Launch denied with missing `system.app.launch`. Calculator was closed at the start. | VoiceOS API with real macOS app provider and temporary consent state |
| **Open Calculator, then is Calculator running?** | Both steps succeeded and verified. The status step observed `running: true`. | VoiceOS API with real macOS app provider |
| **Is Clock running?** | The status step succeeded and verified `running: false` on the rehearsal Mac. | VoiceOS API with real macOS app provider |
| **Check the best deal** → **USB-C charger** | Asked which product, opened DuckDuckGo in the controlled browser, and reported 10 links in Task Results. | Live packaged desktop with typed input |
| **Open browser** | Opened DuckDuckGo and completed in Task Results. | Live packaged desktop |
| **Show open issues in abhijeetmiskin1/ActionMesh** | Completed and reported zero matching issues. | Live packaged desktop |
| **Open WhatsApp** | Paused for review as a simulated action. Leave this command out of the packaged demo. | Live packaged desktop |
| **SHOWCASE** preset | Without launch permission, the timeline showed **0/4 verified**, with launch denied and the other steps pending. After granting the four scopes and retrying, it showed **4/4 verified**, **10 result links**, and Calculator **Running**. | Live desktop from the current checkout, using an isolated profile |

The packaged desktop also completed the two-step Calculator and Clock status commands. Setup reported macOS microphone access and a previous successful transcription. Rehearse a fresh spoken turn in the presentation build. Use the values shown on screen for the app status and link count.
