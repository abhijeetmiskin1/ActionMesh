"""C19 command policy: what this laptop is physically able to run.

Read this as a **capability boundary, not an authorization decision** — the
distinction the architecture depends on (§17: the agent must not become a second
policy engine).

    ActionMesh's policy engine (C3) decides whether a *request* is authorized:
        consent, risk, approval, the human's yes.
    This module decides whether the *laptop can perform it at all*:
        is that executable one the operator permitted, inside a directory it may
        work in, with an environment it may see.

Both must be true, and neither can override the other. A policy ALLOW for a program
that is not on the list is still refused — the boundary is not a policy the executor
can waive. A local ALLOW for a request the policy engine denies never runs — this code
is reached only after authorization. And there is deliberately **no ASK tier here**:
"ask the human" is C3's job, and a second place that asks would be a second place to
answer.

Why an allowlist and not the blacklist §3 warns about
-----------------------------------------------------
A deny list of command names is a speed bump: ``pytho``, ``/usr/bin/gi t``, a symlink
named ``ls`` that points at a script, ``sh`` invoked as ``bash``. Each is a different
spell ing of "run something arbitrary". The allowlist is checked **after** the name is
resolved to a real path through ``PATH``, so what is compared is the binary that will
actually be executed — not the string someone typed. The deny list below still exists,
but as a second gate on argument *shapes*, which an allowlist of names cannot express.

The environment is built, never inherited
----------------------------------------
``ACTIONMESH_SECRET_KEY`` is in this process's environment. Handing ``os.environ`` to a
child would ship the key that hashes every pairing code and credential to whatever the
phone asked to run. So a child gets a small, explicit set, and a request for anything
secret-shaped is refused rather than filtered.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from actionmesh.agent.errors import CommandDenied

#: Programs allowed when the operator named none. Development tooling for this
#: repository; note what is *not* here: no shell, no interpreter that can be handed a
#: URL, no package installer that reaches the network.
DEFAULT_ALLOWED: tuple[str, ...] = (
    "python",
    "python3",
    "pytest",
    "ruff",
    "mypy",
    "git",
    "node",
    "npm",
    "npx",
    "uvicorn",
)

#: Refused even if someone adds them to the allowlist. These are the primitives whose
#: only useful effect on a laptop is damage or privilege change; no development
#: workflow the brief describes needs them, and a phone should never be able to name
#: them. The check runs against the *resolved* binary, so `rm` reached through a
#: symlink to it is still refused.
DEFAULT_DENIED: frozenset[str] = frozenset(
    {
        "rm", "dd", "mkfs", "fdisk", "parted", "shred", "truncate",
        "sudo", "su", "doas", "runuser", "pkexec", "chroot",
        "chmod", "chown", "chgrp", "setfacl",
        "shutdown", "reboot", "halt", "poweroff", "init", "systemctl", "loginctl",
        "kill", "killall", "pkill",
        "iptables", "nft", "ufw", "ip", "route",
        "mount", "umount", "losetup",
        "crontab", "at",
        "ssh", "scp", "sftp", "rsync", "telnet", "nc", "netcat",
        "curl", "wget",
        "eval", "exec",
    }
)

#: Interpreters that can execute code handed to them on the command line. Their
#: presence in an allowlist means "may run this toolchain", not "may run arbitrary
#: source", so the inline-code flags below are refused by default.
_CODE_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c"}),
    "python3": frozenset({"-c"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "perl": frozenset({"-e", "-E", "-p"}),
    "ruby": frozenset({"-e", "--eval"}),
    "php": frozenset({"-r", "--run"}),
    "bash": frozenset({"-c", "--init-file", "--rcfile"}),
    "sh": frozenset({"-c"}),
    "zsh": frozenset({"-c"}),
    # `git -c core.pager='<command>' log` runs the pager through a shell, so the
    # one-letter config override is an execution vector wearing a config hat. Refused
    # with the same rule as an interpreter: set repository config on the laptop.
    "git": frozenset({"-c"}),
}

#: Argument substrings that turn an otherwise-safe program into a transport or alias
#: for something else. Matched on the resolved binary's base name prefix, so they
#: apply to `git` however the caller spelled it.
_EXEC_PATTERNS: dict[str, tuple[str, ...]] = {
    "git": ("ext::", "!"),  # `git clone ext::sh -c ...`, `git config alias.x !sh ...`
}

#: Interpreter *module* flags. ``python -m pytest`` runs a module that is already
#: installed in that environment; ``python -c`` runs source the caller typed. Only the
#: first is what "run the tests" means, and the difference is the whole policy.
_MODULE_FLAGS: frozenset[str] = frozenset({"-m", "--module"})

#: An argument that looks like an inline program, however the executable is spelled.
_INLINE_CODE = re.compile(r"^(-{1,2})(c|e|eval|r|p|print)$")

#: Environment keys a client may never ask for, matched on the *name* so a value that
#: happens to look secret-shaped is not the trigger.
_SECRET_ENV = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE[_-]?KEY|API[_-]?KEY|"
    r"ACCESS[_-]?KEY|AUTH|SESSION)",
    re.IGNORECASE,
)
#: Names refused outright, whatever the pattern above decides.
_DENIED_ENV_NAMES: frozenset[str] = frozenset(
    {
        "ACTIONMESH_SECRET_KEY",
        "ACTIONMESH_SECRET_FILE",
        "ACTIONMESH_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GPG_AGENT_INFO",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "XAUTHORITY",
    }
)
#: The only variables handed to a child process by default. Nothing else about this
#: machine — not its home directory listing, not its proxy settings, not its keys.
#: macOS injects a few of its own; they are benign but must not surprise the test.
_BASE_ENV: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM")
_PLATFORM_ENV: tuple[str, ...] = ("__CF_USER_TEXT_ENCODING", "LC_CTYPE")

MAX_ARGUMENTS: Final = 64
MAX_ARGUMENT_LENGTH: Final = 4096


@dataclass(frozen=True)
class CommandDecision:
    """The outcome of the policy check. Reasons come along so the refusal is auditable."""

    allowed: bool
    executable: str = ""
    resolved_path: str = ""
    #: The arguments only. The program is :attr:`resolved_path`; keeping the two
    #: apart is what lets the runner build an argv without a duplicated element.
    arguments: tuple[str, ...] = ()
    working_directory: str = ""
    environment: Mapping[str, str] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    @property
    def refused(self) -> bool:
        return not self.allowed


@dataclass(frozen=True)
class CommandPolicy:
    """The boundaries a terminal request is measured against."""

    allowed: tuple[str, ...] = DEFAULT_ALLOWED
    denied: frozenset[str] = DEFAULT_DENIED
    #: Root the working directory must live under. Empty means no directory is legal.
    workspace_root: str = ""
    #: Extra absolute directories permitted, for a laptop whose projects are not all
    #: under one root. Resolved and checked the same way as the root.
    extra_working_dirs: tuple[str, ...] = ()
    allow_interpreter_code_flags: bool = False
    #: Names added to the child environment beyond ``_BASE_ENV``. Still refused if the
    #: name is secret-shaped — the pattern is not bypassable by listing.
    extra_env: tuple[str, ...] = ()
    #: Environment values the caller may supply. Never merged raw: each key is checked
    #: against the secret rules and the allowlist.
    allow_client_env: bool = False

    @classmethod
    def from_config(cls, config: object) -> CommandPolicy:
        # Imported lazily: `config` imports DEFAULT_ALLOWED from this module, so a
        # top-level import here would be a cycle.
        from actionmesh.agent.config import AgentConfig

        if not isinstance(config, AgentConfig):
            raise CommandDenied("CommandPolicy.from_config needs an AgentConfig")
        return cls(
            allowed=config.allowed_commands or DEFAULT_ALLOWED,
            denied=DEFAULT_DENIED | frozenset(config.denied_commands),
            workspace_root=config.workspace_root,
            extra_working_dirs=config.extra_working_dirs,
            allow_interpreter_code_flags=config.allow_interpreter_code,
            extra_env=config.extra_env,
        )

    # -- the check --------------------------------------------------------
    def evaluate(
        self,
        command: object,
        arguments: object = (),
        *,
        working_directory: str = "",
        env: object = None,
    ) -> CommandDecision:
        """Resolve a request against the policy. Refusal is the default outcome."""
        reasons: list[str] = []
        name = self._validate_name(command, reasons)
        argv = self._validate_arguments(arguments, reasons)
        if name is None or reasons:
            return CommandDecision(False, reasons=tuple(reasons) or ("invalid request",))

        resolved = self.resolve_executable(name)
        if resolved is None:
            return CommandDecision(
                False,
                reasons=(
                    (
                        f"{name!r} is not on the allowed executable list for this laptop"
                        if name not in self.denied
                        else f"{name!r} is refused regardless of the allowlist"
                    ),
                ),
            )
        refused = self._refused_name(name, resolved)
        if refused:
            return CommandDecision(
                False,
                reasons=(
                    f"{refused!r} is refused regardless of the allowlist "
                    f"(runs as {resolved})",
                ),
            )
        base = os.path.basename(resolved)
        blocked = self._check_flags(base, resolved, argv)
        if blocked:
            return CommandDecision(False, reasons=(blocked,))

        directory, directory_reason = self.resolve_working_directory(working_directory)
        if directory is None:
            return CommandDecision(False, reasons=(directory_reason or "no working directory",))

        environment, env_reasons = self.build_environment(env)
        if env_reasons:
            return CommandDecision(False, reasons=tuple(env_reasons))

        return CommandDecision(
            allowed=True,
            executable=name,
            resolved_path=resolved,
            arguments=argv,
            working_directory=directory,
            environment=environment,
            reasons=(f"allowed: {base} at {resolved}",),
        )

    # -- executables ------------------------------------------------------
    def _refused_name(self, requested: str, resolved: str) -> str | None:
        """The denied name that applies to this binary, if any.

        Both spellings are checked, because they can differ on a real system and each
        is a way past a single-name list:

        * ``/usr/bin/pkill`` and ``/usr/bin/pgrep`` are one binary that reads its own
          ``argv[0]`` to decide whether to list or kill, so the name asked for matters.
        * ``rm`` here resolves to a file called ``gnurm``, and ``nc`` to
          ``nc.openbsd``: the name on disk is not the name in the deny list.

        Checking only one of the two is a deny list that a rename defeats.
        """
        for candidate in (
            os.path.basename(requested.replace("\\", "/")),
            os.path.basename(resolved),
        ):
            if candidate in self.denied:
                return candidate
            for entry in self.denied:
                # Alt-name forms (`nc.openbsd`, `rm.gnul`) are the same program with a
                # suffix, not a different one.
                if candidate.startswith(entry + "."):
                    return entry
        return None

    def resolve_executable(self, name: str) -> str | None:
        """Find the binary that would actually run, then decide.

        Discovery is shared with :func:`find_on_path` so that "is this editor
        installed" and "may this editor run" cannot give two different answers about
        the same file -- a split that is exactly where a symlink would slip through.
        """
        found = find_on_path(name)
        if found is None:
            return None
        return found if found in self._permitted_realpaths() else None

    def _permitted_realpaths(self) -> set[str]:
        out: set[str] = set()
        for entry in self.allowed:
            resolved = self._realpath_of(entry)
            if resolved:
                out.add(resolved)
        return out

    def _realpath_of(self, name: str) -> str:
        if os.sep in name:
            return os.path.realpath(name)
        for entry in os.environ.get("PATH", os.defpath).split(os.pathsep):
            if not entry:
                continue
            candidate = os.path.join(entry, name)
            if os.path.isfile(candidate):
                return os.path.realpath(candidate)
        # Unresolvable names are kept verbatim: refusing to remember them would let a
        # typo in the configuration silently widen the policy to nothing.
        return ""

    # -- arguments --------------------------------------------------------
    @staticmethod
    def _validate_name(command: object, reasons: list[str]) -> str | None:
        if not isinstance(command, str) or not command.strip():
            reasons.append("command must be a non-empty string")
            return None
        name = command.strip()
        if "\x00" in name or len(name) > MAX_ARGUMENT_LENGTH:
            reasons.append("command is malformed")
            return None
        return name

    @staticmethod
    def _validate_arguments(arguments: object, reasons: list[str]) -> tuple[str, ...]:
        if arguments is None:
            return ()
        if isinstance(arguments, str):
            # The one blacklists fail on. A single string is what a shell would split,
            # and there is no shell here; the caller must send an argument vector.
            reasons.append(
                "arguments must be a list of strings, not one string — there is no "
                "shell to split it"
            )
            return ()
        if not isinstance(arguments, (list, tuple)):
            reasons.append("arguments must be a list")
            return ()
        if len(arguments) > MAX_ARGUMENTS:
            reasons.append(f"too many arguments (limit {MAX_ARGUMENTS})")
            return ()
        out: list[str] = []
        for item in arguments:
            if not isinstance(item, str):
                reasons.append("every argument must be a string")
                return ()
            if "\x00" in item:
                reasons.append("an argument contains a NUL byte")
                return ()
            if len(item) > MAX_ARGUMENT_LENGTH:
                reasons.append(f"an argument exceeds {MAX_ARGUMENT_LENGTH} characters")
                return ()
            out.append(item)
        return tuple(out)

    def _check_flags(self, base: str, resolved: str, argv: Sequence[str]) -> str | None:
        """Refuse inline source for an interpreter that is otherwise allowed.

        ``python -m pytest`` and ``python -c '<arbitrary source>'`` differ by
        everything: the first runs a module already installed in that environment, the
        second makes the argument a program. The allowlist says which tool may run; it
        cannot say what source may be written into it, so this does.
        """
        if self.allow_interpreter_code_flags:
            return None
        flags: set[str] = set()
        for name in {base, os.path.basename(resolved)}:
            # Matched by prefix as well as exactly: `python3.14` is the same
            # interpreter as `python3`, and a key that only covers the plain name
            # would leave every versioned binary unprotected.
            for key, key_flags in _CODE_FLAGS.items():
                if name == key or name.startswith(key):
                    flags |= set(key_flags)
        if not flags:
            return None
        for item in argv:
            head = item.split("=", 1)[0]
            if head in flags:
                return (
                    f"{base!r} may run installed modules and files, but not code given "
                    f"on the command line ({item!r}); set ACTIONMESH_ALLOW_INTERPRETER_CODE "
                    "to allow it deliberately"
                )
            # Combined short options: `-uc "import os"` hides the flag inside a
            # cluster. Only single-dash arguments are option clusters, so a filename
            # like `app.js` is not refused for containing the letter of a flag.
            if head.startswith("-") and not head.startswith("--") and len(head) > 2:
                short = {flag.lstrip("-") for flag in flags if not flag.startswith("--")}
                if any(character in short for character in head[1:]):
                    return (
                        f"{base!r} may not take code from a combined option ({item!r})"
                    )
        patterns = next(
            (
                value
                for key, value in _EXEC_PATTERNS.items()
                if any(name.startswith(key) for name in {base, os.path.basename(resolved)})
            ),
            (),
        )
        for item in argv:
            for marker in patterns:
                if marker in item:
                    return (
                        f"{base!r} refuses {marker!r} in an argument: it turns the "
                        "program into a transport for another command"
                    )
        return None

    # -- working directory ------------------------------------------------
    def resolve_working_directory(self, requested: str) -> tuple[str | None, str | None]:
        """A directory the child may start in, or the reason there is none.

        Defaults to the workspace root. Checked the same way file paths are: resolved
        first, then contained, so ``projects/../../etc`` never reaches the spawn.
        """
        candidates = [self.workspace_root, *self.extra_working_dirs]
        real_directories = [os.path.realpath(entry) for entry in candidates if entry]
        if not requested or not requested.strip():
            if not real_directories:
                return None, "no working directory is configured for this laptop"
            return real_directories[0], None
        # A relative directory is workspace-relative, the same vocabulary the file
        # actions use. Resolving it against the process CWD instead would make the
        # phone's paths depend on where the server happens to run -- and let a client
        # that guesses that location reach a directory it was never told about.
        cleaned = requested.strip()
        if os.path.isabs(cleaned):
            resolved = os.path.realpath(cleaned)
        else:
            if not self.workspace_root:
                return None, "a relative working directory needs a configured workspace root"
            resolved = os.path.realpath(os.path.join(self.workspace_root, cleaned))
        if not os.path.isdir(resolved):
            return None, f"{requested!r} is not a directory"
        for allowed_dir in real_directories:
            if resolved == allowed_dir or resolved.startswith(
                allowed_dir.rstrip(os.sep) + os.sep
            ):
                return resolved, None
        return None, f"{requested!r} is outside the directories this laptop may work in"

    # -- environment ------------------------------------------------------
    def build_environment(self, requested: object) -> tuple[Mapping[str, str], list[str]]:
        """Construct the child environment from nothing.

        ``os.environ`` is deliberately never copied wholesale. This process holds the
        key that hashes every pairing code and device credential in the system; a
        child that inherited it could offline-brute-force a 6-digit code with the
        database in hand. A child gets the base variables, whatever the operator added,
        and nothing the phone asked for unless the name is both non-secret and listed.
        """
        env: dict[str, str] = {}
        for key in _BASE_ENV:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        # macOS injects these into every child even with a stripped env; pass them
        # through so the test's expected set matches what the OS actually does.
        for key in _PLATFORM_ENV:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        for key in self.extra_env:
            value = os.environ.get(key)
            if value is None:
                continue
            if _is_secret_env_name(key) or key in _DENIED_ENV_NAMES:
                return {}, [f"{key!r} is secret-shaped and is never passed to a command"]
            env[key] = value

        reasons: list[str] = []
        if requested in (None, {}, ""):
            return env, []
        if not isinstance(requested, Mapping):
            return {}, ["env must be an object"]
        if not self.allow_client_env:
            return {}, [
                "this laptop does not accept environment variables from a request; "
                "the command runs with the environment configured on the laptop"
            ]
        for key, value in requested.items():
            if not isinstance(key, str) or not isinstance(value, str):
                reasons.append("environment keys and values must be strings")
                continue
            if _is_secret_env_name(key) or key in _DENIED_ENV_NAMES:
                reasons.append(f"{key!r} looks like a secret and is never accepted")
                continue
            if key not in self.extra_env and key not in _BASE_ENV:
                reasons.append(f"{key!r} is not on this laptop's environment allowlist")
                continue
            if "\x00" in value or len(value) > MAX_ARGUMENT_LENGTH:
                reasons.append(f"{key!r} has an unusable value")
                continue
            env[key] = value
        return ({}, reasons) if reasons else (env, [])


def find_on_path(name: str) -> str | None:
    """The first executable named ``name`` on ``PATH``, canonicalised; else None.

    Bare names are searched on ``PATH``; anything with a separator is taken as a path.
    The result is a realpath, so a caller compares the file that would run rather than
    the name that was typed. Only the *first* hit is considered: if an earlier ``PATH``
    entry holds a same-named binary, falling through to a later one would let a
    writable directory steer the choice without ever being refused.
    """
    if os.sep in name or (os.altsep and os.altsep in name):
        candidates = [os.path.realpath(name)]
    else:
        candidates = [
            os.path.realpath(os.path.join(entry, name))
            for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
            if entry
        ]
    for candidate in candidates:
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        return candidate
    return None


def _is_secret_env_name(name: str) -> bool:
    return bool(_SECRET_ENV.search(name))


def merge_allowed(extra: Iterable[str]) -> tuple[str, ...]:
    """Add operator-named executables to the defaults, de-duplicated and sorted."""
    return tuple(sorted({*DEFAULT_ALLOWED, *(entry.strip() for entry in extra if entry.strip())}))


__all__ = [
    "DEFAULT_ALLOWED",
    "DEFAULT_DENIED",
    "MAX_ARGUMENTS",
    "MAX_ARGUMENT_LENGTH",
    "CommandDecision",
    "CommandDenied",
    "CommandPolicy",
    "merge_allowed",
]
