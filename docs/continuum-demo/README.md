# ActionMesh Continuum demo

## Setup

From the project root, prepare a disposable workspace with only a public readiness marker.

```bash
demo_workspace=$(mktemp -d /tmp/actionmesh-continuum.XXXXXX)
mkdir "$demo_workspace/continuum-demo"
printf 'ready\n' > "$demo_workspace/continuum-demo/health.txt"
printf '%s\n' "$demo_workspace"
cd desktop && npm start
```

In **Settings**, use the printed path as the workspace root. Enable the local laptop agent and the existing command capability with `python3` allowed. Restart the backend from Settings. The demo binds only to `127.0.0.1:18743`. Keep the workspace disposable and free of private files.

In **Approvals**, grant the default `voiceos-local` actor `device.server.read` at low risk and `device.server.control` at high risk. A grant makes the operation eligible for policy review. Approve each specific start or stop in the existing Approvals view when it appears.

In **Continuum**, save the default server-health goal with monitoring enabled. Click **Verify now**, approve the initial server start, return to **Continuum**, and click **Verify now** again. Rehearse with the service in the **HEALTHY** or **RECOVERED** state.

## Three-minute capture sequence

| Time | Show | Narration |
| --- | --- | --- |
| 0:00–0:25 | Open Continuum and show the enabled goal. | “ActionMesh monitors a typed goal. This one requires a managed local service to respond with HTTP 200.” |
| 0:25–0:50 | Click **Verify now**. Show the ProofRoute decision and attempt count. | “The service is healthy. ProofRoute chooses no action, and the repair budget stays unused.” |
| 0:50–1:20 | Click **Stop demo service** and approve the stop under **Approvals**. | “I’m deliberately stopping only the dedicated demo service. The stop passes through ActionMesh’s existing approval system.” |
| 1:20–1:55 | Return to Continuum and wait for the monitor or click **Verify now**. Show the violation, proposed strategy, and pending approval. | “GoalGuard observed that the managed service stopped. It proposed a bounded start because the loopback port is free.” |
| 1:55–2:25 | Approve the start and return to Continuum. Click **Verify now** if needed. | “Policy authorized the specific repair. ActionMesh still checks the service separately after execution.” |
| 2:25–3:00 | Show **RECOVERED**, **VERIFIED**, and the evidence timeline. | “The fresh HTTP response verified recovery. The timeline links the decision and repair to actual execution records.” |

If the timer is inconvenient, **Verify now** uses the same controller as scheduled monitoring. If the port is occupied, use a clean demo environment. Continuum leaves the occupying process alone. For recording, use macOS Screenshot’s **Record Selected Portion**, select only the ActionMesh window, and save the capture alongside this guide as `ActionMesh-Continuum-Demo.mov`.

After the demo, request and approve the dedicated service stop, then disable monitoring before another check.

## Rehearsal evidence

The local API rehearsal started a real HTTP service after approval, observed HTTP 200, selected no action while healthy, and approved a stop. The background monitor detected the stop and requested one repair approval. After approval, a fresh probe verified recovery. The test also checked that repeated verification requests did not create duplicate pending repairs and that the disabled goal reloaded from SQLite.

The Electron rehearsal verified goal creation and the displayed permission denial with an isolated profile. macOS reported that this session lacked Screen Recording access. The capture sequence above is ready for an operator with that permission.

## Packaged macOS rehearsal

The packaged build is `desktop/release/continuum/mac-arm64/ActionMesh.app`. The older running bundle at `desktop/release/mac-arm64/ActionMesh.app` uses the original profile. The Continuum rehearsal used the packaged update with a disposable profile and workspace.

The full visible flow passed in the packaged app. The initial service returned HTTP 200 and ProofRoute chose no action. The approved demo stop was `graph_b67ca8a4bdf2`. GoalGuard detected the violation on its timer and proposed repair `graph_63b8d202647c`. An extra **Verify now** left exactly one approval pending. After approval, the timeline showed repair success and the fresh health observation `graph_ec9c5b795c68` with **RECOVERED** and **VERIFIED**. The rehearsal ended with monitoring disabled and the demo service stopped.

To rebuild the packaged app from the repository root with the already installed Electron distribution:

```bash
.venv/bin/python scripts/bundle_runtime.py --target macos-arm64 --skip-deps
cd desktop
./node_modules/.bin/electron-builder --mac --arm64 --dir \
  -c.directories.output=release/continuum \
  -c.electronDist=node_modules/electron/dist
```

This local build lacks Developer ID signing. To use it with the usual profile, quit both ActionMesh windows before opening the new bundle. For a disposable rehearsal, launch the bundle binary with `--user-data-dir` pointed at a fresh profile and follow the workspace setup above.

### V3 baseline checkpoint

- Base Git revision `60711ab98530d1973f7baa8645025edd86dd4b2b`. The package includes local working-tree changes.
- Packaged renderer SHA-256 `d57ee7450b957c2b221bb875e68210ae772ecd9018da7d2661dc21a43a445304` for `Contents/Resources/app.asar`.
- Continuum source changes are in `actionmesh/voiceos/continuum.py`, `actionmesh/api.py`, `actionmesh/agent/processes.py`, `actionmesh/agent/provider.py`, `desktop/app/main.js`, `desktop/ui/app.js`, `desktop/ui/index.html`, and `tests/unit/test_continuum.py`. Some of those files also contain earlier features.
- The verified packaged window uses `/tmp/am-continuum-package.iub5pG/profile`. The original running app uses the usual ActionMesh profile under Library/Application Support. The two instances have separate writable state. The recovery event was confirmed in the demo profile's SQLite database. A metadata-only event copy is at `desktop/release/continuum/rehearsal-evidence.json`.

The recording preflight still returned false from the current Codex session. To record manually, use **Screenshot** (Shift–Command–5) and select only the packaged ActionMesh window area. If macOS requests access, enable the recorder you are using under **System Settings → Privacy & Security → Screen & System Audio Recording**. Save the real capture as `docs/continuum-demo/ActionMesh-Continuum-Demo.mov`.
