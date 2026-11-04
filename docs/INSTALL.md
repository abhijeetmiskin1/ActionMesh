# Installation

ActionMesh ships as a Linux `.deb` (x64). The desktop app is an Electron
shell that manages a local Python backend on loopback. Everything runs
locally; no account is required.

## Build (from source)

```bash
cd desktop
npm ci
npm run dist:linux        # electron-builder --linux deb --x64
```

Output: `desktop/release/ActionMesh-0.1.0-amd64.deb` (~150 MB; most of it is
the bundled, standalone Python runtime under `resources/runtime/`).

## Install the .deb

```bash
sudo dpkg -i desktop/release/ActionMesh-0.1.0-amd64.deb
```

The postinstall registers the Chromium sandbox AppArmor profile
(`resources/apparmor-profile` in the package), so the sandbox runs without
`--no-sandbox`. If the package manager reports missing recommendations
(`libgtk-3-0`, `libnotify4`, `libnss3`), install them:

```bash
sudo apt-get install -f
```

## Launch

```bash
actionmesh          # /usr/bin/actionmesh
# or, in development:
cd desktop && npm start   # node scripts/start.js
```

In dev, `scripts/start.js` probes the sandbox in order: setuid
`chrome-sandbox` → unprivileged user namespaces → `--no-sandbox` with an
explicit warning. The packaged `.deb` does not need any of that fallback;
the AppArmor profile is the production answer.

On first launch the app starts its local backend automatically, then shows
the first-run onboarding wizard (see [ONBOARDING.md](ONBOARDING.md)). The
package `desktopName` is `actionmesh.desktop`, matching the installed
`.desktop` entry.

## What ships in the package

| Path | What |
|---|---|
| `app/`, `ui/`, `assets/` | The Electron shell + renderer (incl. the onboarding wizard) |
| `resources/runtime/python/bin/python3` | Bundled standalone Python 3.12 |
| `resources/runtime/app/` | `actionmesh/` package + `sidecar.py` (the uvicorn launcher) |
| `resources/apparmor-profile` | Chromium sandbox AppArmor policy |

The dev layout differs (`backend.js` falls back to `.venv/bin/python` or
system `python3` with `/usr/lib/python3/dist-packages`), but the packaged
layout is fixed and matches `backend.js resolveRuntime()`.

## Secrets are not packaged

`electron-builder` file globs exclude `**/*.key`, `**/settings.json`,
`**/.env`, `**/settings.json.bak` and `runtime/**/*.pem`. Running the app
can never smuggle local settings or keys into an artifact: credentials live
in the OS keyring (`safeStorage`) on the user's machine and are only ever
written there.

## Platforms

Only the **Linux x64 `.deb`** is built and verified in this environment.
`desktop/package.json` also declares Windows (`dist:win`, NSIS/MSI) and
macOS (`dist:mac`, dmg) targets, but those have **not** been built or
launched here — do not claim support until they are. Cross-building from
Linux is not done: no Wine/Xcode toolchain is present, and electron-builder
targets must be built on their own host OS.
