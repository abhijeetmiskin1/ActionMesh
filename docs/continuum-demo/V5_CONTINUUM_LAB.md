# Continuum Lab V5

## Workspaces

- **Mission Control** reads backend readiness, the saved Goal Contract, parked approval graphs, active executions, online devices, and recent independently verified execution receipts.
- **ChaosStage** controls only the dedicated local Continuum HTTP demo service (`127.0.0.1:18743`). Fault injection and recovery use the existing `device.server.stop`, `device.server.start`, and `device.server.status` actions. Review and approve each action in Approvals.
- **ProofLens** compares the managed-service observation with a clearly labeled healthy demo dashboard fixture claim. The fixture is not an observer. Process registry and HTTP probe evidence are produced through the same managed-service adapter and are not presented as fully independent observers.
- **IntentCanvas** selects one supported first-party demo target and previews a typed `server_health` Goal Contract before saving it through `/continuum`.
- **GoalGuard** remains available as the original contract and evidence timeline.

## Three minute packaged demo

1. Open Mission Control and select **Create Goal**. In IntentCanvas select **ActionMesh demo service health**, inspect the mapping, then save and enable monitoring.
2. In ChaosStage select **Prepare Environment**. If a managed start is required, inspect and approve the exact action.
3. Confirm HTTP 200 is observed. Select **Inject Controlled Fault** and approve the named stop action.
4. Show the violated observation in ChaosStage. GoalGuard will propose a bounded managed restart; inspect the exact approval request and approve it.
5. Open ProofLens while the service is down to compare the labeled fixture claim against the managed observation. Evidence is stale or insufficient unless the latest observation is fresh.
6. After recovery, select **Double-Check Reality** or **Verify State**, then show RECOVERED, the HTTP health observation, and the execution receipt in Mission Control / History.

The monitor records pending execution IDs and attempt limits to prevent duplicate repair dispatch. Approval, missing permission, and unavailable-device states are surfaced rather than treated as success.

## Build macOS V5 separately

```bash
.venv/bin/python scripts/bundle_runtime.py --target macos-arm64 --skip-deps
cd desktop
./node_modules/.bin/electron-builder --mac --arm64 --dir \
  -c.directories.output=release/continuum-v5 \
  -c.electronDist=node_modules/electron/dist
```

Use an isolated `--user-data-dir` and a disposable workspace for packaged acceptance. Do not reuse the V4 acceptance profile; its pending approval is intentionally preserved.
