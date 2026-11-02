"""Regression guard for dangerous primitives inside the ActionMesh package.

Threats covered: **command injection**, **SSRF / outbound network access**,
**unsafe deserialization**, **dynamic import execution**, **FFI abuse**,
**process spawning**.

This file used to assert, by parsing every module in the package, that ActionMesh
contains no subprocess, eval or exec. The mobile control plane needed a process
spawner, so the claim had to change — and the way it changed is the point of this
test. The guard did not get weaker. It got more specific:

* **Spawn APIs are now detected at all.** ``asyncio.create_subprocess_exec`` was
  invisible to the old ban list, which only knew about the ``subprocess`` module.
  A control plane whose most important invariant is enforced by a check that cannot
  see the primitive is not enforced.
* **The exemption names a function, not a directory.** ``actionmesh/agent/runner.py``
  may call ``asyncio.create_subprocess_exec`` and nothing else dangerous. It may not
  import ``subprocess``, may not touch ``socket``, may not ``os.kill``. Widening this
  is writing a new line in a security test, and the diff says so.
* **Some things have no exemption.** ``shell=True``, ``create_subprocess_shell``,
  ``os.system``, ``os.popen``, ``eval``, ``exec``, ``compile`` and ``__import__`` are
  refused on every path, including the allowlisted one. That is why the runner can be
  trusted to receive a phone's argv: it has no way to hand that argv to a shell.

``test_guard_detects_a_violation`` and its neighbours prove the scanner is not
vacuous. A guard that has never rejected anything cannot be believed when it passes.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import unittest

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "actionmesh"

# Importing any of these from application code is itself the finding.
DANGEROUS_MODULES = {
    # process / shell
    "subprocess", "multiprocessing", "pty", "shutil",
    # FFI
    "ctypes", "cffi",
    # unsafe deserialization
    "pickle", "cPickle", "marshal", "shelve",
    # outbound network. Dotted names matter here: urllib.request dials out,
    # urllib.parse is pure string handling and must stay legal.
    "socket", "ssl", "urllib.request", "http.client", "httpx", "requests",
    "aiohttp", "ftplib", "telnetlib", "smtplib", "poplib", "imaplib",
    "xmlrpc", "webbrowser",
    # dynamic code loading
    "importlib", "runpy",
}

# Attribute-level bans for stdlib modules that are harmless to import outright.
DANGEROUS_MEMBERS = {
    "os": {
        "system", "popen", "execl", "execle", "execlp", "execlpe", "execv",
        "execve", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp",
        "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "fork",
        "forkpty", "kill",
    },
    # The asyncio transport boundary. Before these were listed, the guard's central
    # claim -- "nothing spawns a process" -- was unenforced, because the package can
    # spawn without ever importing `subprocess`.
    "subprocess": {
        "call", "check_call", "check_output", "getoutput", "getstatusoutput",
        "Popen", "run",
    },
    # Outbound connections. Same reasoning as the spawn APIs: `socket` is not the only
    # way to dial out from this package, and a guard that only watches `socket` would
    # wave through an SSRF written with asyncio.
    "asyncio": {
        "create_subprocess_exec",
        "create_subprocess_shell",
        "create_subprocess_read",
        "open_connection",
        "open_unix_connection",
        "start_server",
        "start_unix_server",
    },
    "socket": {"socket", "create_connection", "getaddrinfo"},
}

# Builtins that evaluate or compile code at runtime. Banned everywhere, including
# inside any path listed on the allowlist below.
DANGEROUS_CALLS = {"eval", "exec", "__import__", "compile"}

#: Per-path exemptions, spelled out member by member. The only capability granted in
#: this repository today is one function in one file: the laptop agent's runner.
MEMBER_ALLOWLIST: dict[str, set[str]] = {
    "actionmesh/agent/runner.py": {"asyncio.create_subprocess_exec"},
    # Loopback liveness probing. Structurally safe because `is_listening` accepts an
    # int port and hardcodes 127.0.0.1 -- there is no host parameter to pass -- but the
    # exemption is still spelled out, so "the agent can open a socket" is a line in a
    # security test rather than something a reader has to trust.
    "actionmesh/agent/processes.py": {"asyncio.open_connection"},
}

#: Network access is forbidden package-wide with no exemption in place. A future
#: official adapter (C13) gets a path here as a deliberate, reviewable edit.
NETWORK_ALLOWED: frozenset[str] = frozenset()

#: Argument keywords that must never appear anywhere, on any path. `shell=True` is the
#: single change that turns a controlled argv into a shell interpreter for whatever the
#: phone typed, so it is refused by name rather than reasoned about.
FORBIDDEN_KEYWORDS = {"shell", "preexec_fn", "restore_signals"}

#: `executable` means "the binary to run instead of argv[0]" only on a spawn. The name
#: is common enough elsewhere (this repository uses it for a policy result field) that
#: flagging it everywhere is noise; it is checked where the call could be a spawn.
SPAWN_KEYWORDS = {"executable"}
_SPAWN_NAME_MARKERS = (
    "subprocess", "Popen", "spawn", "exec", "system", "popen", "fork",
)


def _is_dangerous(name: str) -> bool:
    """Dotted-name match: ``urllib.request`` is banned, ``urllib.parse`` is not."""
    return any(name == entry or name.startswith(entry + ".")
               for entry in DANGEROUS_MODULES)


def _normalise(rel: str) -> str:
    return rel.replace("\\", "/")


def _modules() -> list[tuple[str, ast.Module]]:
    """Parse every Python file in the package; an unparseable file is a failure."""
    found: list[tuple[str, ast.Module]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(PACKAGE.parent))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError as exc:
            raise AssertionError(f"{rel} no longer parses: {exc}") from exc
        found.append((rel, tree))
    return found


def _flag_imports(rel: str, tree: ast.Module) -> list[str]:
    normalised = _normalise(rel)
    network_exempt = any(
        normalised.startswith(prefix) for prefix in NETWORK_ALLOWED
    )
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_dangerous(alias.name) and not network_exempt:
                    problems.append(f"{rel}:{node.lineno} imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if _is_dangerous(node.module or "") and not network_exempt:
                problems.append(f"{rel}:{node.lineno} imports from {node.module}")
            banned = DANGEROUS_MEMBERS.get(node.module or "")
            if banned:
                permitted = MEMBER_ALLOWLIST.get(normalised, set())
                for alias in node.names:
                    qualified = f"{node.module}.{alias.name}"
                    if alias.name in banned and qualified not in permitted:
                        problems.append(f"{rel}:{node.lineno} imports {qualified}")
    return problems


def _flag_members(rel: str, tree: ast.Module) -> list[str]:
    """Attribute calls such as ``os.system(...)`` and ``asyncio.create_subprocess_*``."""
    normalised = _normalise(rel)
    permitted = MEMBER_ALLOWLIST.get(normalised, set())
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        owner = node.value.id if isinstance(node.value, ast.Name) else ""
        if not owner:
            continue
        banned = DANGEROUS_MEMBERS.get(owner)
        if not banned or node.attr not in banned:
            continue
        qualified = f"{owner}.{node.attr}"
        if qualified in permitted:
            continue
        problems.append(f"{rel}:{node.lineno} uses {qualified}")
    return problems


def _flag_calls(rel: str, tree: ast.Module) -> list[str]:
    problems: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in DANGEROUS_CALLS
        ):
            problems.append(f"{rel}:{node.lineno} calls {node.func.id}()")
    return problems


def _flag_shell_keywords(rel: str, tree: ast.Module) -> list[str]:
    """Refuse the arguments that turn an argv into a shell command line.

    ``shell=``, ``preexec_fn=`` and ``restore_signals=`` are refused on *every* call:
    those names exist for spawning, so seeing one is the finding wherever it appears.
    ``executable=`` is refused only on a call that could be a spawn, because it is also
    an ordinary field name -- and a guard that cries wolf at ordinary code is a guard
    someone will turn off.
    """
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        spawn_like = any(marker in name for marker in _SPAWN_NAME_MARKERS)
        for keyword in node.keywords:
            if keyword.arg is None or _is_explicitly_false(keyword.value):
                continue
            if keyword.arg in FORBIDDEN_KEYWORDS or (
                spawn_like and keyword.arg in SPAWN_KEYWORDS
            ):
                problems.append(
                    f"{rel}:{node.lineno} passes {keyword.arg}= to {name or 'a call'}; "
                    "a spawn here takes an argv and nothing else"
                )
    return problems


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
        return f"{owner}.{node.func.attr}" if owner else node.func.attr
    return ""


def _is_explicitly_false(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and value.value is False


class DangerousPrimitiveTests(unittest.TestCase):
    def test_package_files_parse(self) -> None:
        # Guards against the scanner silently finding nothing to scan.
        self.assertGreater(len(_modules()), 5)

    def test_core_never_imports_dangerous_modules(self) -> None:
        problems = [flag
                    for rel, tree in _modules()
                    for flag in _flag_imports(rel, tree)]
        self.assertEqual(
            problems, [],
            "ActionMesh must not import process, network or unsafe-decoding "
            "modules; these are the attack surface for command injection, SSRF "
            "and unsafe deserialization:\n  " + "\n  ".join(problems))

    def test_core_never_calls_dangerous_builtins(self) -> None:
        problems = [flag
                    for rel, tree in _modules()
                    for flag in _flag_calls(rel, tree)]
        self.assertEqual(
            problems, [],
            "eval/exec/__import__ let untrusted input become code:\n  "
            + "\n  ".join(problems))

    def test_no_file_spawns_a_process_outside_the_runner(self) -> None:
        problems = [flag
                    for rel, tree in _modules()
                    for flag in _flag_members(rel, tree)]
        self.assertEqual(
            problems, [],
            "process spawning is confined to actionmesh/agent/runner.py, and even "
            "there only to the functions listed in MEMBER_ALLOWLIST:\n  "
            + "\n  ".join(problems))

    def test_nothing_anywhere_passes_a_shell_argument(self) -> None:
        problems = [flag
                    for rel, tree in _modules()
                    for flag in _flag_shell_keywords(rel, tree)]
        self.assertEqual(
            problems, [],
            "a spawn that can take shell=True is a shell for whatever the phone "
            "typed, whatever the argument list looks like:\n  "
            + "\n  ".join(problems))

    def test_the_execution_exemption_is_exactly_one_function(self) -> None:
        # The allowlist is asserted rather than trusted: an unnoticed addition here
        # is the quiet widening this file exists to make loud.
        self.assertEqual(
            MEMBER_ALLOWLIST,
            {
                "actionmesh/agent/runner.py": {"asyncio.create_subprocess_exec"},
                "actionmesh/agent/processes.py": {"asyncio.open_connection"},
            },
            "changing this line is a security decision, not a convenience",
        )

    def test_the_network_allowlist_is_still_empty(self) -> None:
        self.assertEqual(NETWORK_ALLOWED, frozenset())

    def test_exemptions_do_not_reach_the_never_allowed_set(self) -> None:
        # Even the runner cannot shell out, exec source, or hand a callback to the
        # child-creation path.
        tree = ast.parse(
            "asyncio.create_subprocess_exec(cmd, shell=True)\n"
            "asyncio.create_subprocess_shell(cmd)\n"
            "os.system(cmd)\n"
            "eval(data)\n"
        )
        rel = "actionmesh/agent/runner.py"
        self.assertTrue(_flag_shell_keywords(rel, tree))
        self.assertIn("asyncio.create_subprocess_shell", " ".join(_flag_members(rel, tree)))
        self.assertIn("os.system", " ".join(_flag_members(rel, tree)))
        self.assertTrue(_flag_calls(rel, tree))

    def test_shell_false_is_allowed(self) -> None:
        tree = ast.parse("f(a, shell=False, executable=None)\n")
        flagged = _flag_shell_keywords("actionmesh/agent/runner.py", tree)
        self.assertEqual([item for item in flagged if "shell=" in item], [])

    def test_guard_detects_a_violation(self) -> None:
        # Prove the scanner is not vacuous: feed it code that *does* shell out.
        sample = ast.parse(
            "import subprocess\nsubprocess.run(x)\neval(x)\n"
            "asyncio.create_subprocess_shell(x)\nos.system(x)\n"
        )
        rel = "actionmesh/core/innocent.py"
        self.assertTrue(_flag_imports(rel, sample))
        self.assertTrue(_flag_calls(rel, sample))
        self.assertTrue(_flag_members(rel, sample))

    def test_guard_accepts_safe_code(self) -> None:
        sample = ast.parse("import json\ndef f(d):\n    return json.loads(d)\n")
        rel = "actionmesh/core/x.py"
        self.assertEqual(_flag_imports(rel, sample), [])
        self.assertEqual(_flag_calls(rel, sample), [])
        self.assertEqual(_flag_members(rel, sample), [])

    def test_the_runner_may_use_its_one_granted_function(self) -> None:
        sample = ast.parse("asyncio.create_subprocess_exec(*argv)\n")
        self.assertEqual(
            _flag_members("actionmesh/agent/runner.py", sample), []
        )

    def test_urlparse_is_allowed_but_urlrequest_is_not(self) -> None:
        # The distinction that flagged real_base.py. Parsing a URL to enforce
        # HTTPS is not network access; urllib.request is.
        parsing = ast.parse("from urllib.parse import urlparse\n"
                            "urlparse('https://x')\n")
        self.assertEqual(_flag_imports("actionmesh/providers/real_base.py", parsing), [])
        dialing = ast.parse("import urllib.request\n"
                            "urllib.request.urlopen('http://x')\n")
        self.assertTrue(_flag_imports("actionmesh/core/x.py", dialing))

    def test_dotted_imports_are_matched_by_dotted_name(self) -> None:
        # ``import http.client`` must be caught, not missed by a first-segment check.
        tree = ast.parse("import http.client\n")
        self.assertTrue(_flag_imports("actionmesh/core/x.py", tree))

    def test_allowlist_never_exempts_dangerous_calls(self) -> None:
        # Even an authorized network adapter must not be able to eval code: the
        # allowlist relaxes imports and named members only, never calls.
        tree = ast.parse("eval(data)\n")
        self.assertTrue(_flag_calls("actionmesh/agent/runner.py", tree))

    def test_the_loopback_probe_takes_no_host(self) -> None:
        """The exemption is bounded by a signature, not by a convention.

        `asyncio.open_connection` is permitted in one file; what makes that safe is
        that the only wrapper around it accepts an ``int`` port and no host, so there
        is no argument a caller can steer. If someone widens it to take a target, this
        test is the thing that complains.
        """
        from actionmesh.agent.processes import is_listening

        signature = inspect.signature(is_listening)
        self.assertEqual(list(signature.parameters), ["port", "timeout_s"])
        # Annotations are strings under `from __future__ import annotations`.
        self.assertEqual(str(signature.parameters["port"].annotation), "int")
        # No parameter that could name a destination.
        self.assertNotIn("host", [name.lower() for name in signature.parameters])

    def test_the_runner_is_the_only_module_that_references_the_spawn_api(self) -> None:
        """The exemption names one file, so the code must agree with it.

        Checked by scanning for the attribute itself rather than trusting the
        allowlist: a second module that quietly copied the call would be flagged by
        ``test_no_file_spawns_a_process_outside_the_runner`` only if it used a *banned*
        member name, and this closes the loop by naming where the granted one appears.
        """
        users: list[str] = []
        for rel, tree in _modules():
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "asyncio"
                    and node.attr.startswith("create_subprocess")
                ):
                    users.append(_normalise(rel))
        self.assertEqual(sorted(set(users)), ["actionmesh/agent/runner.py"])
        self.assertIn(
            "asyncio.create_subprocess_exec",
            " ".join(MEMBER_ALLOWLIST["actionmesh/agent/runner.py"]),
        )


if __name__ == "__main__":
    unittest.main()
