# ActionMesh for Android

A thin native shell that turns the ActionMesh control room into a real
home-screen app on Android — no browser "Add to home screen" needed.

## What it is (and is not)

The app is a single `WebView` pointed at your ActionMesh server's `/dashboard`.
It contains **no application logic**: it renders exactly what the trusted server
sends. Policy, execution and audit stay in one place — the server — so the shell
can never disagree with it.

It deliberately avoids AndroidX/AppCompat, so there is nothing to resolve from
Maven and the APK is built straight from the Android SDK build-tools.

```
android/
├── AndroidManifest.xml                     one activity, one permission (INTERNET)
├── java/app/actionmesh/control/
│   └── MainActivity.java                   the WebView shell
└── res/
    ├── values/{strings,colors,styles}.xml  name, brand palette, framework theme
    ├── drawable/ic_launcher_foreground.xml the mesh mark (same as the PWA icon)
    └── mipmap-anydpi-v26/ic_launcher*.xml  adaptive launcher icon
```

## Build

```bash
python scripts/build_apk.py                      # fetch toolchain if needed, then build
python scripts/build_apk.py --url http://192.168.1.10:8080
python scripts/build_apk.py --no-download        # offline; fail if toolchain missing
```

The result is written to `dist/actionmesh-<version>.apk`. The script is
standard-library only; it downloads a portable JDK and the Android command-line
tools into `android-toolchain/` (git-ignored) and installs the platform and
build-tools through `sdkmanager`.

## Install

```bash
adb install -r dist/actionmesh-0.1.0.apk
```

The app defaults to `http://10.0.2.2:8000` — the host machine as seen from the
Android emulator — and the control room lives at `/dashboard`. On a physical
phone, open the overflow menu → **Server…** and enter your server's LAN
address (for example `http://192.168.1.10:8080`).

`usesCleartextTraffic` is enabled because a LAN ActionMesh server is normally
plain HTTP. Put the server behind TLS for anything beyond a trusted network.

## Signing

The build signs with a local debug keystore (`android-toolchain/debug.keystore`,
password `android`). That is fine for sideloading and testing. A Play Store
release needs your own keystore held outside this repository — never commit a
signing key.
