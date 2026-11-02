"""C19 editor adapters: what is installed, and how to ask it to open something.

Two rules, both from the brief.

**Use the editor's documented CLI, never simulated keystrokes.** Keyboard injection
would mean the agent can type anything into any window on the screen, including a
terminal the user has open, and it cannot be scoped per application. A CLI is a
bounded interface: argv, exit code, no focus stealing. So the generic adapter runs
``<binary> <path>`` and the command adapter only accepts a documented subcommand from
a table -- and the table is deliberately read-mostly.

**Discovery may not invent a second execution path.** Reporting an editor's version
means running it. That is done through the same ``CommandRunner`` with a policy whose
allowlist is the single absolute binary being probed, rather than a direct spawn from
here. It sounds like a detail. It is the difference between "everything that runs on
this machine goes through the confinement code" and "almost everything".

Adapters are data, not subclasses: an adapter with its own class per editor tends to
accumulate behaviour differences, and what varies here is a name, a flag, and a set of
permitted subcommands.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from actionmesh.agent.commands import CommandPolicy, find_on_path
from actionmesh.agent.errors import AgentError
from actionmesh.agent.runner import CommandRunner

#: How long a discovered version string is trusted. Editors do not change version
#: while the laptop is awake, and re-probing on every dashboard poll would spawn a
#: process per refresh.
VERSION_CACHE_S = 600.0


@dataclass(frozen=True)
class IDESpec:
    """One editor: how to find it, and what it may be asked to do."""

    ide: str
    label: str
    binaries: tuple[str, ...]
    version_flag: str = "--version"
    #: Flags placed before the path when opening. `--new-window` and friends.
    open_flags: tuple[str, ...] = ()
    #: Subcommands ``device.ide.command`` may pass through. Kept explicit and
    #: read-mostly on purpose: ``--install-extension`` downloads third-party code and
    #: runs it with the editor's privileges, which is a much larger claim on the
    #: machine than "open this folder", and belongs at the laptop.
    commands: tuple[str, ...] = ("--version", "--help")


KNOWN_IDES: tuple[IDESpec, ...] = (
    IDESpec(
        ide="vscode",
        label="Visual Studio Code",
        binaries=("code", "code-insiders", "codium"),
        commands=("--version", "--help", "--status", "--list-extensions"),
    ),
    IDESpec(
        ide="jetbrains",
        label="JetBrains IDE",
        # `gateway` is the JetBrains launcher; the per-product binaries are listed so
        # a machine with only PyCharm still reports an editor.
        binaries=("gateway", "idea", "pycharm", "webstorm", "goland", "rider"),
        commands=("--version", "--help"),
    ),
    IDESpec(ide="sublime", label="Sublime Text", binaries=("subl", "sublime-text")),
    IDESpec(ide="helix", label="Helix", binaries=("hx",)),
    IDESpec(ide="neovim", label="Neovim", binaries=("nvim",)),
    IDESpec(ide="gtk-editor", label="GNOME Text Editor", binaries=("gnome-text-editor", "gedit")),
)

_BY_NAME = {spec.ide: spec for spec in KNOWN_IDES}


class EditorNotFound(AgentError):
    """The requested editor is not installed, or not one ActionMesh knows."""


@dataclass
class EditorDiscovery:
    """Installed editors, with a short-lived version cache."""

    runner: CommandRunner | None = None
    policy_for: Callable[[str], CommandPolicy] | None = None
    #: Whether this editor may be *run at all* on this laptop. Version discovery
    #: executes the editor, so it is gated by the operator's allowlist rather than
    #: by the editor merely existing: "installed" is a fact about the machine,
    #: "permitted to execute" is a decision someone made.
    permitted: Callable[[IDESpec], bool] = lambda spec: False
    _cache: dict[str, tuple[float, str | None]] = field(default_factory=dict)

    def find(self, spec: IDESpec) -> str | None:
        """The absolute path of the first installed binary for ``spec``."""
        for name in spec.binaries:
            found = find_on_path(name)
            if found is not None:
                return found
        return None

    def spec(self, ide: str) -> IDESpec:
        spec = _BY_NAME.get((ide or "").strip().lower())
        if spec is None:
            raise EditorNotFound(
                f"no editor named {ide!r} is known to this laptop agent; ask "
                f"device.ide.list for what is installed ({', '.join(_BY_NAME)})"
            )
        return spec

    def installed(self) -> list[tuple[IDESpec, str]]:
        out: list[tuple[IDESpec, str]] = []
        for spec in KNOWN_IDES:
            found = self.find(spec)
            if found is not None:
                out.append((spec, found))
        return out

    async def version(self, spec: IDESpec, binary: str) -> str | None:
        """Ask the editor for its version, through the confined runner.

        Returns None rather than raising when it cannot be obtained: an editor that
        is installed but slow, or refuses to answer, is still installed, and a
        dashboard should show that rather than lose the row.
        """
        cached = self._cache.get(spec.ide)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        if (
            self.runner is None
            or self.policy_for is None
            or not spec.version_flag
            or not self.permitted(spec)
        ):
            return None
        try:
            decision = self.policy_for(binary).evaluate(binary, [spec.version_flag])
            if decision.refused:
                return None
            outcome = await self.runner.run(decision, timeout_s=5.0)
        except (AgentError, OSError, asyncio.CancelledError):
            return None
        text = (outcome.stdout or outcome.stderr).strip()
        version = text.splitlines()[0][:80] if text else None
        self._cache[spec.ide] = (time.monotonic() + VERSION_CACHE_S, version)
        return version

    def describe(self, spec: IDESpec, binary: str, version: str | None) -> dict[str, Any]:
        """The capability report shape: what is there, and what it will do."""
        return {
            "ide": spec.ide,
            "label": spec.label,
            "installed": True,
            # None means "not permitted to ask", not "unknown version" -- and the
            # dashboard must be able to tell those apart.
            "version": version,
            "version_known": version is not None,
            # Reported as the caller's own spelling, never the resolved path: the
            # absolute location of an editor is not something a phone needs.
            "command": spec.binaries[0],
            "commands": list(spec.commands),
            "can_open": True,
        }

    def open_argv(self, ide: str, path: str) -> tuple[IDESpec, str, list[str]]:
        """``(spec, binary, argv)`` for opening a confined path in an editor."""
        spec = self.spec(ide)
        binary = self.find(spec)
        if binary is None:
            raise EditorNotFound(
                f"{spec.label} is not installed on this laptop; installed editors are "
                + ", ".join(s.ide for s, _ in self.installed())
                or "none"
            )
        return spec, binary, [binary, *spec.open_flags, path]

    def command_argv(
        self, ide: str, command: str, arguments: tuple[str, ...]
    ) -> tuple[IDESpec, str, list[str]]:
        """``(spec, binary, argv)`` for a documented CLI subcommand.

        The subcommand must be in the table. This is the difference between "run the
        editor's documented command line" and "run the editor with whatever the phone
        typed after it", and the table is where that line is drawn.
        """
        spec = self.spec(ide)
        name = (command or "").strip()
        if name not in spec.commands:
            raise EditorNotFound(
                f"{name!r} is not a command this laptop will pass to {spec.ide!r}; "
                f"allowed: {', '.join(spec.commands) or 'none'}"
            )
        binary = self.find(spec)
        if binary is None:
            raise EditorNotFound(f"{spec.label} is not installed on this laptop")
        return spec, binary, [binary, name, *arguments]


__all__ = [
    "KNOWN_IDES",
    "VERSION_CACHE_S",
    "EditorDiscovery",
    "EditorNotFound",
    "IDESpec",
]
