# ActionMesh + VoiceOS

**A voice interface for real, permission-aware computer work.**

## The problem

A voice assistant can understand a request and still leave users unsure whether it actually did anything. Multi-step tasks are especially hard to trust when the assistant can’t explain what ran, what failed, or what requires approval.

## The solution

VoiceOS turns spoken or typed requests into structured tasks. ActionMesh checks each action against its capability registry and the user’s permissions, asks for approval when required, executes through a scoped provider, and independently checks the result. Task Results shows the recognized words, the outcome, and the evidence behind it.

## What the prototype demonstrates

Say **“Open Calculator, then is Calculator running?”** VoiceOS transcribes the command, launches the native Mac app, and checks macOS to confirm it’s running. Both steps appear in Task Results. The same flow supports a typed fallback. The prototype also supports controlled browser navigation, read-only web discovery with spoken follow-ups, and public GitHub repository and issue lookups.

## Why it matters

The same execution path handles voice and text. A proposed action carries no authority, and a failed verification never becomes a success message. This gives users a practical way to delegate supported work while retaining control over consequential actions.

**Build:** ActionMesh 0.2.0 for Apple Silicon, with Electron, Python, FastAPI, and SQLite. [Project overview](../README.md) · [Verification notes](project/TESTING_AND_ACCEPTANCE.md)
