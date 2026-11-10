"""Bounded macOS app launches and independent Launch Services observation."""

from __future__ import annotations

import subprocess
import sys

from actionmesh.protocol.models import FailureKind
from actionmesh.providers.base import ProviderError

MAC_APP_ACTION = "device.app.launch_native"
MAC_APP_STATUS = "device.app.status_native"

# Neither the executable, flags, nor the bundle ID comes from free-form input.
MAC_APPS = {
    "activity monitor": "com.apple.ActivityMonitor",
    "calculator": "com.apple.calculator",
    "calendar": "com.apple.iCal",
    "claude": "com.anthropic.claudefordesktop",
    "clock": "com.apple.clock",
    "dictionary": "com.apple.Dictionary",
    "finder": "com.apple.finder",
    "notes": "com.apple.Notes",
    "preview": "com.apple.Preview",
    "reminders": "com.apple.reminders",
    "stickies": "com.apple.Stickies",
    "system settings": "com.apple.systempreferences",
    "textedit": "com.apple.TextEdit",
}


def launch(app: object) -> None:
    bundle_id = MAC_APPS.get(app) if isinstance(app, str) else None
    if bundle_id is None:
        raise ProviderError("Choose a supported Mac app.", FailureKind.INVALID_INPUT)
    if sys.platform != "darwin":
        raise ProviderError("This launcher requires macOS.", FailureKind.PERMANENT)
    try:
        result = subprocess.run(
            ["/usr/bin/open", "-b", bundle_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderError("macOS did not respond to the app launch.", FailureKind.PERMANENT) from exc
    if result.returncode:
        raise ProviderError("macOS could not open the requested app.", FailureKind.PERMANENT)


def running(app: object) -> bool | None:
    bundle_id = MAC_APPS.get(app) if isinstance(app, str) else None
    if bundle_id is None or sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/lsappinfo", "find", f"bundleID={bundle_id}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # lsappinfo also exits zero when the app is absent, with an empty result.
    if result.returncode:
        return None
    if not result.stdout.strip():
        return False
    return True if result.stdout.startswith(b"ASN:") else None
