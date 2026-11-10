# ParallelWorld — Preview Before Execution

ParallelWorld evaluates supported file-operation plans against a bounded snapshot in
a private temporary shadow. It returns metadata through the normal execution result
and sends the actual unified diff only on ActionMesh's transient output stream. It
does not write the source workspace during preview. Applying a preview rechecks file
identity, size, mode and SHA-256, then submits the exact stored operation list as a
server-minted sequence of registered `device.files.*` actions. The policy engine,
permission grants, per-action approvals, executor and independent verifier remain in
charge of real changes.

## Demo with the native macOS application

Create a small disposable workspace and keep it free of private files:

```bash
demo_workspace=$(mktemp -d /tmp/actionmesh-parallelworld.XXXXXX)
printf 'candidate build\n' > "$demo_workspace/release.txt"
printf 'artifact candidate\n' > "$demo_workspace/candidate.txt"
printf 'review notes\n' > "$demo_workspace/notes.txt"
printf '%s\n' "$demo_workspace"
```

Open the V4 package at `desktop/release/continuum-v4/mac-arm64/ActionMesh.app`
with a fresh disposable profile and set the workspace in **Settings**. Enable the
laptop agent and restart its backend. In **Approvals → Permissions**, grant the local
`voiceos-local` actor `file.read` and `file.write`. These grants only make the action
eligible for policy; the HIGH-risk edit still requires its own named approval.

Open **ParallelWorld**. Its example plan edits `release.txt` and copies
`candidate.txt` to `candidate-copy.txt`. Choose **Preview Changes** and inspect the
diff, file counts, source hashes and unchanged-source indicator. Preview is a
`device.files.preview_plan` action and requires `file.read`. If the permission is
missing, policy refuses it and no shadow preview is produced.

Choose **Apply Approved Plan**. ActionMesh rechecks the workspace snapshot before
dispatch and submits the server-held operations through the existing execution
pipeline. Approve each named file action in **Approvals**. Return to ParallelWorld to
see execution state and the independent verification results. A changed source file
marks the preview stale and blocks Apply; run a new preview instead.

## Supported operations and boundaries

`create`, `edit`, `copy`, `rename`, `move` and `trash` operate on regular files only.
Directories, symlinks, special files, workspace escapes, missing parents, existing
copy/rename/move destinations, invalid UTF-8 edit sources and unknown operation
fields are rejected. Preview simulates `trash` inside the shadow only; applying it
uses the existing OS trash provider and may report unavailable if that platform has
no supported trash backend. It never falls back to permanent deletion.

Each preview is capped at 100 files, 250 directories, 24 path levels, 256 KiB per
source file, 2 MiB total source content and 24 operations. Diff output is capped at
200 KiB; transient output has the existing executor stream limit. A preview is held
in memory for up to one hour (maximum 20 concurrent records) so proposed file text
and source bytes do not become a second on-disk content store. Preview history is
therefore scoped to the running backend and disappears at restart or expiry.

Application is sequential, not transactional. If a later operation fails or is
denied, earlier verified changes may remain; inspect execution history and the
workspace before preparing a new plan. Every executed action has its own independent
verification result. A waiting or unverified operation is never described as
successful.

## V4 build

The verified V3 build remains at `desktop/release/continuum/mac-arm64/ActionMesh.app`.
Build V4 to a separate directory:

```bash
.venv/bin/python scripts/bundle_runtime.py --target macos-arm64 --skip-deps
cd desktop
./node_modules/.bin/electron-builder --mac --arm64 --dir \
  -c.directories.output=release/continuum-v4 \
  -c.electronDist=node_modules/electron/dist
```

The local package is unsigned without a Developer ID. Use a new
`--user-data-dir` for rehearsal; do not point it at the original ActionMesh profile.
