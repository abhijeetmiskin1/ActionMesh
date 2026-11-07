"""Linux host bindings for an authorized desktop action in a bundled runtime.

Only a fixed system interpreter and the shipped device_screen module are executed.
This is an OS transport, not an executor or authorization surface. Policy and
approval run before LaptopProvider reaches this adapter. No model code, command,
credentials, or inherited backend environment is handed to the helper.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from actionmesh.agent import device_screen
from actionmesh.agent.commands import CommandDecision
from actionmesh.agent.runner import CommandRunner

PYTHON = "/usr/bin/python3"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
# OS session addresses are required by Gio/AT-SPI. No ActionMesh credentials,
# provider keys, PYTHONPATH, or arbitrary user environment is inherited.
SESSION_ENV = (
    "HOME", "USER", "LOGNAME", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE", "LANG", "LC_ALL",
    "XAUTHORITY", "AT_SPI_BUS_ADDRESS",
)


def enabled() -> bool:
    """The desktop launcher sets this only after a successful host binding probe."""
    return os.environ.get("ACTIONMESH_NATIVE_DESKTOP") == "1" and Path(PYTHON).is_file()


async def call(operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
    functions: dict[str, Callable[..., dict[str, Any]]] = {
        "capture": device_screen.capture_screenshot,
        "inspect": device_screen.inspect_gui,
        "click": device_screen.click_element,
        "type": device_screen.type_text,
        "scroll": device_screen.scroll_element,
    }
    if operation not in functions:
        return {"status": "invalid", "detail": "Unknown native desktop operation."}
    # Reject unexpected fields and unbounded values on both sides of the transport.
    if not device_screen.valid_native_request(operation, parameters):
        return {"status": "invalid", "detail": "Invalid native desktop parameters."}
    if not enabled():
        return await asyncio.to_thread(functions[operation], **parameters)
    return await host_call("desktop", {"operation": operation, "parameters": parameters})


async def host_call(lane: str, request: dict[str, Any]) -> dict[str, Any]:
    """Fixed host transports; neither caller nor model can supply a program path."""
    scripts = {"desktop": "device_screen.py", "media": "mpris.py"}
    if lane not in scripts:
        return {"status": "unavailable"}
    payload = json.dumps(request)
    if len(payload.encode("utf-8")) > MAX_REQUEST_BYTES:
        return {"status": "invalid", "detail": "Native request is too large."}
    environment = {key: os.environ[key] for key in SESSION_ENV if key in os.environ}
    # Data only, held by the short-lived child; no credentials, file, shell or argv.
    environment["ACTIONMESH_NATIVE_REQUEST"] = payload
    decision = CommandDecision(allowed=True, executable=PYTHON, resolved_path=PYTHON,
        arguments=("-I", str(Path(__file__).resolve().with_name(scripts[lane])), "--native-request"),
        environment=environment)
    try:
        outcome = await CommandRunner(timeout_s=35, max_output_bytes=MAX_RESPONSE_BYTES).run(decision)
        if outcome.exit_code != 0 or outcome.truncated or outcome.timed_out:
            raise ValueError("helper unavailable")
        answer = json.loads(outcome.stdout)
        if not isinstance(answer, dict) or not isinstance(answer.get("status"), str):
            raise ValueError("invalid helper answer")
        return answer
    except (ValueError, OSError, RuntimeError):
        return {"status": "unavailable", "reason": "native_helper_unavailable",
                "detail": "The host adapter did not return a complete observation."}
