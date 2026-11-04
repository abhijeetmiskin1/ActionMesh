"""C19 laptop agent configuration.

The agent is the only part of ActionMesh that touches a real machine, so its
configuration is a set of **boundaries**, not a feature toggle list. Each field
answers "what may this process reach?", and the answer defaults to nothing.

Two rules govern every default here:

* **Nothing enabled by absence.** An unset root means no file capabilities; an unset
  shell flag means no terminal, process, server, application or editor
  capabilities. A laptop that starts up with a control plane attached to it is a
  laptop someone configured, not one that guessed.
* **A configured path must exist.** A typo in ``ACTIONMESH_WORKSPACE_ROOT`` that
  silently produces an empty, unreachable root would look like a working agent with
  nothing in it. Refusing to start is the honest failure.

Capabilities are *derived* from configuration (:meth:`AgentConfig.advertise`) rather
than listed by the caller. That matters: the brief's strict capability registry means
"what this agent can actually do", and an operator-declared list would let a machine
with no workspace still advertise ``device.file.read`` — a capability that then fails
at execution time, which is a lie told twice.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from actionmesh import __version__
from actionmesh.agent.commands import DEFAULT_ALLOWED
from actionmesh.agent.definitions import (
    CAPABILITIES,
    IMPLEMENTED,
    REQUIRING_BROWSER,
    REQUIRING_DESKTOP,
    REQUIRING_FILE_ROOT,
    REQUIRING_MEDIA,
    REQUIRING_PAYMENT,
    REQUIRING_SHELL,
)

ENV_ENABLED = "ACTIONMESH_AGENT"
ENV_DEVICE_ID = "ACTIONMESH_DEVICE_ID"
ENV_DEVICE_NAME = "ACTIONMESH_DEVICE_NAME"
ENV_WORKSPACE_ROOT = "ACTIONMESH_WORKSPACE_ROOT"
ENV_ALLOW_SHELL = "ACTIONMESH_ALLOW_SHELL"
ENV_ALLOW_DESKTOP = "ACTIONMESH_ALLOW_DESKTOP"
ENV_ALLOW_BROWSER = "ACTIONMESH_ALLOW_BROWSER"
ENV_ALLOW_MEDIA = "ACTIONMESH_ALLOW_MEDIA"
ENV_ALLOW_PAYMENT = "ACTIONMESH_ALLOW_PAYMENT"
ENV_HEARTBEAT_S = "ACTIONMESH_HEARTBEAT_S"
ENV_ALLOWED_COMMANDS = "ACTIONMESH_ALLOWED_COMMANDS"
ENV_DENIED_COMMANDS = "ACTIONMESH_DENIED_COMMANDS"
ENV_ALLOW_INTERPRETER_CODE = "ACTIONMESH_ALLOW_INTERPRETER_CODE"
ENV_ALLOWED_WORKDIRS = "ACTIONMESH_ALLOWED_WORKDIRS"
ENV_EXTRA_ENV = "ACTIONMESH_EXTRA_ENV"
ENV_MAX_CONCURRENT = "ACTIONMESH_MAX_CONCURRENT_PROCESSES"
ENV_COMMAND_TIMEOUT_S = "ACTIONMESH_COMMAND_TIMEOUT_S"
ENV_LOG_DIR = "ACTIONMESH_AGENT_LOG_DIR"

#: Default presence window. The registry treats a device as offline after this
#: long without a heartbeat, so the loop has to beat faster than the expiry.
DEFAULT_HEARTBEAT_S = 15.0
MAX_HEARTBEAT_S = 300.0
#: A command that runs past this is killed. Long-lived work is a *server*, which is
#: started with device.server.start and watched, not a terminal call held open.
MAX_COMMAND_TIMEOUT_S = 900.0


class AgentConfigError(ValueError):
    """Raised when the agent's boundaries are missing or unusable."""


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AgentConfig:
    """What this laptop agent is allowed to reach."""

    device_id: str = "laptop-1"
    name: str = ""
    #: Absolute, existing directory. File and project actions resolve inside it and
    #: nothing outside it — see ``paths.py``.
    workspace_root: str = ""
    # -- command policy (see ``commands.py``) --------------------------------
    #: Executables this laptop will run, by name or absolute path. Empty means the
    #: built-in defaults, never "anything": a blank list is the most dangerous value
    #: the field could take.
    allowed_commands: tuple[str, ...] = ()
    denied_commands: tuple[str, ...] = ()
    #: Interpreter flags that turn an argument into a program (`python -c`). Off,
    #: because "may run pytest" and "may run source the phone typed" are different.
    allow_interpreter_code: bool = False
    #: Extra absolute directories a command may start in, beside the workspace root.
    extra_working_dirs: tuple[str, ...] = ()
    #: Environment variables a child may receive from this laptop's own environment.
    extra_env: tuple[str, ...] = ()
    #: Simultaneous commands. Bounded, because a phone can ask for many at once and
    #: each one is a process holding this machine's file table open.
    max_concurrent: int = 4
    command_timeout_s: float = 60.0
    #: Where a detached child's stdout/stderr go. Empty means nowhere: the child is
    #: launched with its output discarded, exactly as before this existed, because a
    #: long-lived server's log is a file on this machine and files are how secrets end
    #: up in the wrong hands. Set it and launched processes write to a `0600` file here
    #: instead, which is also the only way to capture a server's output without a live
    #: reader -- a pipe nobody drains stops the server that writes to it.
    log_dir: str = ""

    #: Terminal, process, server, application and editor actions. Off by default:
    #: the difference between "this laptop can be told to run tests" and "this
    #: laptop runs whatever a phone sends it" is exactly this flag plus the command
    #: policy.
    allow_shell: bool = False
    #: Screen capture and GUI inspection (C38). Off by default: "this laptop can be
    #: asked what is on its screen" is a distinct boundary from "this laptop can be
    #: told to run tests", and a machine that showed its screen because a stranger set
    #: ACTIONMESH_ALLOW_SHELL would be a boundary that leaked. Even with this on, the
    #: OS screenshot portal still gates every capture, and policy still gates the action.
    allow_desktop: bool = False
    #: Browser session (C42). Off by default: the controlled browser is a distinct
    #: boundary from desktop or shell, and a machine that drives a website because a
    #: stranger set ACTIONMESH_ALLOW_SHELL would be a boundary that leaked. Even with
    #: this on, ``browser.click`` can never submit a booking — the engine has no
    #: submit verb and the two CRITICAL submits sit behind ``allow_payment``.
    allow_browser: bool = False
    #: Local media player control (C42). Off by default: a headless agent must not
    #: advertise a player it cannot reach, and media control is a distinct boundary
    #: from shell/desktop/browser.
    allow_media: bool = False
    #: Consequential web submits — ``booking.submit`` and ``order.place`` (C42). Off by
    #: default, and unlike the other two flags this one is not a capability *permission*:
    #: it is the operator saying "a CRITICAL submit may be announced by this machine at
    #: all". Turning it on does not create a submit backend, and the handler refuses
    #: with ``PERMANENT`` when one is missing, so an agent that opens this boundary on a
    #: deployment with no payment integration announces the capability and then honestly
    #: declines to fabricate a booking. Policy still gates every call: these actions are
    #: CRITICAL, so the policy engine forces an approval regardless of this flag.
    allow_payment: bool = False
    heartbeat_s: float = DEFAULT_HEARTBEAT_S
    agent_version: str = __version__
    platform: str = field(default_factory=lambda: os.uname().sysname.lower())
    #: C36: the CPU architecture, so a control plane can refuse to send an x86 build
    #: to an arm machine *before* dispatch instead of reporting a failure afterwards.
    arch: str = field(default_factory=lambda: os.uname().machine)
    #: The desktop application version, distinct from the agent's: the two are
    #: upgraded independently and a dashboard that conflates them cannot say which
    #: half of a machine is stale.
    app_version: str = __version__
    os_version: str = field(
        default_factory=lambda: " ".join(
            part for part in (os.uname().sysname, os.uname().release) if part
        )
    )

    def __post_init__(self) -> None:
        if not self.device_id.strip():
            raise AgentConfigError("device_id is required")
        if self.max_concurrent < 1:
            raise AgentConfigError("max_concurrent must be at least 1")
        if not 0 < self.command_timeout_s <= MAX_COMMAND_TIMEOUT_S:
            raise AgentConfigError(
                f"command_timeout_s must be between 0 and {MAX_COMMAND_TIMEOUT_S}"
            )
        for entry in self.extra_working_dirs:
            if not os.path.isabs(entry):
                raise AgentConfigError(
                    f"extra working directory {entry!r} must be absolute"
                )
        if not 0 < self.heartbeat_s <= MAX_HEARTBEAT_S:
            raise AgentConfigError(
                f"heartbeat_s must be between 0 and {MAX_HEARTBEAT_S} seconds"
            )
        if self.workspace_root:
            if not os.path.isabs(self.workspace_root):
                raise AgentConfigError(
                    f"workspace root {self.workspace_root!r} must be absolute; a "
                    "relative root resolves against whatever directory the server "
                    "happens to start in"
                )
            if not os.path.isdir(self.workspace_root):
                raise AgentConfigError(
                    f"workspace root {self.workspace_root!r} is not a directory"
                )
        if self.log_dir:
            if not os.path.isabs(self.log_dir):
                raise AgentConfigError(
                    f"log directory {self.log_dir!r} must be absolute"
                )
            if self.workspace_root and self._inside_workspace():
                # Agent-owned logs are not source. Under the root they would be readable
                # through the confined file API, which turns "capture what the server
                # printed" into "let any phone with file.read collect it".
                raise AgentConfigError(
                    f"log directory {self.log_dir!r} must not be inside the workspace "
                    f"root {self.workspace_root!r}"
                )

    def _inside_workspace(self) -> bool:
        log = os.path.realpath(self.log_dir)
        root = os.path.realpath(self.workspace_root)
        return log == root or log.startswith(root + os.sep)

    @property
    def capture_logs(self) -> bool:
        """Whether launched children keep a log at all."""
        return bool(self.log_dir)

    # -- env --------------------------------------------------------------
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AgentConfig:
        source = env if env is not None else dict(os.environ)

        def get(key: str, default: str = "") -> str:
            return (source.get(key) or default).strip()

        def listing(key: str) -> tuple[str, ...]:
            return tuple(
                part.strip() for part in get(key).split(",") if part.strip()
            )

        raw_heartbeat = get(ENV_HEARTBEAT_S, str(DEFAULT_HEARTBEAT_S))
        try:
            heartbeat = float(raw_heartbeat)
        except ValueError as exc:
            raise AgentConfigError(
                f"{ENV_HEARTBEAT_S} must be a number of seconds, got {raw_heartbeat!r}"
            ) from exc
        root = get(ENV_WORKSPACE_ROOT)
        raw_timeout = get(ENV_COMMAND_TIMEOUT_S, "60")
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise AgentConfigError(
                f"{ENV_COMMAND_TIMEOUT_S} must be a number of seconds, got {raw_timeout!r}"
            ) from exc
        raw_max = get(ENV_MAX_CONCURRENT, "4")
        try:
            max_concurrent = int(raw_max)
        except ValueError as exc:
            raise AgentConfigError(
                f"{ENV_MAX_CONCURRENT} must be a whole number, got {raw_max!r}"
            ) from exc
        return cls(
            device_id=get(ENV_DEVICE_ID, "laptop-1"),
            name=get(ENV_DEVICE_NAME) or get(ENV_DEVICE_ID, "laptop-1"),
            workspace_root=os.path.abspath(root) if root else "",
            allow_shell=_truthy(get(ENV_ALLOW_SHELL)),
            allow_desktop=_truthy(get(ENV_ALLOW_DESKTOP)),
            allow_browser=_truthy(get(ENV_ALLOW_BROWSER)),
            allow_media=_truthy(get(ENV_ALLOW_MEDIA)),
            allow_payment=_truthy(get(ENV_ALLOW_PAYMENT)),
            heartbeat_s=heartbeat,
            allowed_commands=listing(ENV_ALLOWED_COMMANDS),
            denied_commands=listing(ENV_DENIED_COMMANDS),
            allow_interpreter_code=_truthy(get(ENV_ALLOW_INTERPRETER_CODE)),
            extra_working_dirs=tuple(
                os.path.abspath(entry) for entry in listing(ENV_ALLOWED_WORKDIRS)
            ),
            extra_env=listing(ENV_EXTRA_ENV),
            max_concurrent=max_concurrent,
            command_timeout_s=timeout,
            # Deliberately not abspath()'d: a relative value here would otherwise be
            # resolved against whatever directory the server happened to start in and
            # pass validation as an absolute path.
            log_dir=get(ENV_LOG_DIR),
        )

    @staticmethod
    def enabled(env: dict[str, str] | None = None) -> bool:
        """Whether an operator asked for an agent at all."""
        source = env if env is not None else dict(os.environ)
        return _truthy(source.get(ENV_ENABLED) or "")

    # -- capability registry ------------------------------------------------
    def advertise(self) -> tuple[str, ...]:
        """The capabilities this configuration actually supports.

        Five gates, all of which must pass: the capability is a registered action
        (:data:`CAPABILITIES`), the operator opened its boundary (root configured
        for files, shell flag for execution, desktop flag plus a reachable session
        for screen work, browser/media flags for the controlled browser and player,
        and payment flag for the CRITICAL submits), and this build can
        carry it out (:data:`IMPLEMENTED`). A capability that passes the first two but
        not the third would be advertised, approved, dispatched, and then fail — which
        is a worse outcome than not offering it, because the user has already said yes.

        ``DeviceRegistry.register`` re-checks every advertised name against the
        action registry, so a capability nobody registered cannot be announced.
        """
        out: list[str] = []
        for capability in CAPABILITIES:
            if capability not in IMPLEMENTED:
                continue
            if capability in REQUIRING_FILE_ROOT and not self.workspace_root:
                continue
            if capability in REQUIRING_SHELL and not self.allow_shell:
                continue
            if capability in REQUIRING_DESKTOP and (
                not self.allow_desktop or not self.desktop_reachable
            ):
                # Two gates: the operator opened the boundary, *and* the machine can
                # actually reach a desktop. Checking reachability lazily here (not at
                # import) means an agent on a headless server advertises no screen
                # capability at all, rather than advertising one that would authorize
                # and then fail to capture.
                continue
            if capability in REQUIRING_BROWSER and not self.allow_browser:
                # The controlled browser is unreachable without the bridge process
                # and its hardened window; "announced but not servable" would be the
                # same authorized-then-failed lie the other gates exist to prevent.
                # media.open_url shares this gate because it drives the browser.
                continue
            if capability in REQUIRING_MEDIA and not self.allow_media:
                # A headless agent must not advertise a player it cannot reach.
                continue
            if capability in REQUIRING_PAYMENT and not self.allow_payment:
                # A CRITICAL submit is the one capability where "announced but not
                # servable" is worst: the approval card would ask the user to approve a
                # payment this machine cannot complete. Closed unless the operator
                # opened it, and even then the handler refuses rather than pretending.
                continue
            out.append(capability)
        return tuple(out)

    @property
    def desktop_reachable(self) -> bool:
        """Whether this process can realistically reach a graphical session.

        Cheap and side-effect-free: it looks at the session environment, not at the
        portal (probing the portal is the capture capability's job, and a heartbeat
        should not be taking screenshots to decide what to advertise).
        """
        import os as _os

        if not _os.environ.get("DISPLAY") and not _os.environ.get("WAYLAND_DISPLAY"):
            return False
        # The desktop APIs are reached through the system PyGObject. If this process
        # cannot import gi, it cannot inspect or capture regardless of the display.
        # Under a development venv the launcher hands over the system package
        # directory; give the walker its chance to append it before deciding.
        try:
            from actionmesh.agent.device_screen import ensure_gi_importable

            ensure_gi_importable()
        except Exception:
            pass
        try:
            import gi  # noqa: F401
        except ImportError:
            return False
        return True

    def permits(self, capability: str) -> bool:
        return capability in self.advertise()

    def public(self) -> dict[str, object]:
        """Configuration summary safe to display. No paths beyond the root, no env."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "platform": self.platform,
            "os_version": self.os_version,
            "agent_version": self.agent_version,
            "workspace_root": self.workspace_root,
            "allow_shell": self.allow_shell,
            "allow_browser": self.allow_browser,
            "allow_media": self.allow_media,
            "heartbeat_s": self.heartbeat_s,
            "allowed_commands": list(self.allowed_commands or DEFAULT_ALLOWED),
            "denied_commands": list(self.denied_commands),
            "allow_interpreter_code": self.allow_interpreter_code,
            "max_concurrent": self.max_concurrent,
            "command_timeout_s": self.command_timeout_s,
            # The directory itself is not reported: it is a local path, and a heartbeat
            # does not need to say where the laptop keeps its files.
            "capture_logs": self.capture_logs,
            "capabilities": list(self.advertise()),
        }


__all__ = [
    "DEFAULT_HEARTBEAT_S",
    "ENV_ALLOW_BROWSER",
    "ENV_ALLOW_MEDIA",
    "ENV_ALLOW_SHELL",
    "ENV_DEVICE_ID",
    "ENV_DEVICE_NAME",
    "ENV_ENABLED",
    "ENV_HEARTBEAT_S",
    "ENV_WORKSPACE_ROOT",
    "MAX_HEARTBEAT_S",
    "AgentConfig",
    "AgentConfigError",
]
