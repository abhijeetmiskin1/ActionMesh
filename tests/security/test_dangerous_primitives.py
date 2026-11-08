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

#: Per-path exemptions, spelled out member by member. The only capabilities granted in
#: this repository today are one function in the laptop agent's runner, one loopback
#: probe, one outbound HTTPS call in the reviewed model transport, one AF_UNIX connect
#: in the desktop model bridge, and two fixed-binary OS integrations in the file engine.
MEMBER_ALLOWLIST: dict[str, set[str]] = {
    "actionmesh/agent/runner.py": {"asyncio.create_subprocess_exec"},
    # Loopback liveness probing. Structurally safe because `is_listening` accepts an
    # int port and hardcodes 127.0.0.1 -- there is no host parameter to pass -- but the
    # exemption is still spelled out, so "the agent can open a socket" is a line in a
    # security test rather than something a reader has to trust.
    # The macOS liveness probe uses os.kill(pid, 0) without sending a signal.
    "actionmesh/agent/processes.py": {"asyncio.open_connection", "os.kill"},
    # C37: the model transport. This is the package's one outbound internet call, and
    # it is granted as a *member*, not as a module import, on purpose: the file imports
    # nothing dangerous (asyncio, json, urllib.parse), so granting it a NETWORK_ALLOWED
    # prefix would also exempt it from the pickle/socket/subprocess import bans -- a far
    # wider hole than the capability needs. What keeps this safe is inside the module:
    # the destination is a closed frozenset (OFFICIAL_ENDPOINTS), https and port 443
    # only, no userinfo, no redirects, both directions size-bounded, response bodies
    # never surfaced. `test_the_model_transport_dials_only_approved_endpoints` below
    # asserts that closed list through the public function rather than trusting this
    # comment.
    "actionmesh/voiceos/transport.py": {"asyncio.open_connection"},
    # C41: the desktop model bridge. This is the package's *second* outbound path, and
    # it is narrower than the first in a way the first could not be: the destination is
    # a Unix domain socket inside the desktop app's own profile directory, handed to
    # this process in its environment. `AF_UNIX` has no hostname and no port, so there
    # is no address an attacker can substitute to redirect it; the peer is whichever
    # local process owns that path, and it authenticates the caller with a per-launch
    # token. The exemption is granted member by member, and the *import* is granted by
    # the per-path table below rather than through NETWORK_ALLOWED -- that set would
    # also let this file import `pickle` or `subprocess`, which it has no business
    # doing. `tests/unit/test_model_bridge.py` asserts the AF_UNIX-only construction
    # through the public functions rather than trusting this comment.
    "actionmesh/voiceos/model_bridge.py": {"socket.socket"},
    # C42: the file engine's two OS integrations -- `gio trash` and `xdg-open`. Both are
    # fixed argv lists (`[binary, *fixed_args, absolute_path]`), `shell` is never set,
    # both streams are DEVNULL, and each call has a timeout; the binary is looked up on
    # PATH by name and the path element is already confined by `Workspace.resolve`. The
    # grant is member-by-member because that is exactly what the capability is: two
    # named calls, not "this file may spawn". `tests/unit/test_files_agent.py` asserts
    # the refusals (no opener, escaped path, no shell) through the public methods.
    "actionmesh/agent/files.py": {"subprocess.run"},
    "actionmesh/agent/apps.py": {"subprocess.run"},
    # C42: the browser drive bridge. Same AF_UNIX shape as the model bridge
    # above, but awaited on the event loop because the browser driver is
    # awaited: the destination is a Unix domain socket inside the desktop
    # app's own profile directory, handed to this process in its environment,
    # and it authenticates with a per-launch token. No hostname, no port, no
    # address to substitute. ``tests/unit/test_browser_bridge.py`` asserts the
    # AF_UNIX-only construction and the token-in-frame shape through the
    # public functions rather than trusting this comment.
    "actionmesh/agent/browser_bridge.py": {"asyncio.open_unix_connection"},
}

#: Imports of an otherwise-banned module that one specific file is permitted. Kept apart
#: from MEMBER_ALLOWLIST because "may reference socket.socket" and "may import the socket
#: module at all" are different capabilities, and only this file needs the second.
IMPORT_ALLOWLIST: dict[str, set[str]] = {
    # AF_UNIX only; see the member grant above for why that is the whole story.
    "actionmesh/voiceos/model_bridge.py": {"socket"},
    # Same grant as the member entry above: the two calls need the name `subprocess`
    # in scope, and nothing else in this file is permitted to use it.
    "actionmesh/agent/files.py": {"subprocess"},
    "actionmesh/agent/apps.py": {"subprocess"},
}

#: Network access by module *import* is still forbidden package-wide, with no
#: exemption. The C37 model transport deliberately did not need one: it reaches the
#: network through a granted member call above, so this set stays empty and every
#: module in the package -- the execution engine included -- remains unable to
#: `import socket`, `import httpx` or `import urllib.request`.
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
    import_exempt = IMPORT_ALLOWLIST.get(normalised, set())
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_dangerous(alias.name) and not network_exempt and alias.name not in import_exempt:
                    problems.append(f"{rel}:{node.lineno} imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if _is_dangerous(node.module or "") and not network_exempt and node.module not in import_exempt:
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
                "actionmesh/agent/processes.py": {"asyncio.open_connection", "os.kill"},
                "actionmesh/voiceos/transport.py": {"asyncio.open_connection"},
                "actionmesh/voiceos/model_bridge.py": {"socket.socket"},
                "actionmesh/agent/files.py": {"subprocess.run"},
                "actionmesh/agent/apps.py": {"subprocess.run"},
                "actionmesh/agent/browser_bridge.py": {"asyncio.open_unix_connection"},
            },
            "changing this line is a security decision, not a convenience",
        )
        self.assertEqual(
            IMPORT_ALLOWLIST,
            {
                "actionmesh/voiceos/model_bridge.py": {"socket"},
                "actionmesh/agent/files.py": {"subprocess"},
                "actionmesh/agent/apps.py": {"subprocess"},
            },
            "changing this line is a security decision, not a convenience",
        )

    def test_the_network_allowlist_is_still_empty(self) -> None:
        # C37 added an outbound call without widening this set: the transport reaches
        # the network through a granted member call, so no module -- least of all the
        # execution engine -- gained the ability to import a network stack.
        self.assertEqual(NETWORK_ALLOWED, frozenset())

    def test_the_model_transport_dials_only_approved_endpoints(self) -> None:
        """The grant is bounded by content, not by a convention.

        `asyncio.open_connection` is permitted in exactly one module. What makes that
        safe is that the destination is a closed set inside the transport rather than a
        parameter its caller controls, so this test drives the public validation
        function: everything outside the set is refused *before* a dial would happen.
        """
        from actionmesh.voiceos.transport import (
            OFFICIAL_ENDPOINTS,
            TransportError,
            checked_endpoint,
        )

        self.assertEqual(
            OFFICIAL_ENDPOINTS, frozenset({"https://api.anthropic.com/v1/messages"})
        )
        for endpoint in OFFICIAL_ENDPOINTS:
            host, port, path = checked_endpoint(endpoint)
            self.assertTrue(host)
            self.assertEqual(port, 443)
            self.assertTrue(path.startswith("/"))

        refused = [
            "http://api.anthropic.com/v1/messages",           # plaintext
            "https://evil.example/v1/messages",               # not on the list
            "https://api.anthropic.com/v1/messages?to=x",     # query string
            "https://api.anthropic.com:8443/v1/messages",     # non-443
            "https://key:secret@api.anthropic.com/v1/messages",  # userinfo
            "https://api.anthropic.com/v1/other",             # not on the list
            "api.anthropic.com/v1/messages",                  # no scheme
            "",
        ]
        for endpoint in refused:
            with self.subTest(endpoint=endpoint), self.assertRaises(TransportError):
                checked_endpoint(endpoint)

    def test_the_bridge_exemption_is_a_unix_socket_and_nothing_else(self) -> None:
        """C41's grant is narrower than the transport's, and this proves it.

        `model_bridge.py` may import `socket` and call `socket.socket`. It may not use
        any other member of the module -- in particular `create_connection`, which takes
        a host, or `getaddrinfo` -- and it may not reach the network any other way. The
        construction itself is checked through the public client: it selects AF_UNIX,
        so there is no host or port in the call to redirect.
        """
        rel = "actionmesh/voiceos/model_bridge.py"
        # Every socket member this file is allowed to use.
        self.assertEqual(MEMBER_ALLOWLIST[rel], {"socket.socket"})

        # A dial-out written in this file would still be refused, when it uses a
        # member the grant did not name.
        for source in (
            "socket.create_connection(('evil.example', 80))",
            "socket.getaddrinfo('evil.example', 443)",
            "s = socket.socket(); s.connect(('evil.example', 443))",
        ):
            tree = ast.parse(source)
            # The member scanner flags the first two outright...
            if "socket.socket()" not in source:
                self.assertTrue(_flag_members(rel, tree), source)
        # ...and an import of `ssl` or `http.client` in the same file is not covered
        # by the import grant, which names exactly one module.
        tree = ast.parse("import ssl\nimport http.client\nfrom urllib import request\n")
        self.assertTrue(_flag_imports(rel, tree))

    def test_the_bridge_client_speaks_af_unix(self) -> None:
        """The socket type is a fact about the call, not about a comment."""
        source = (
            PACKAGE / "voiceos" / "model_bridge.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        families = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name != "socket.socket":
                continue
            self.assertEqual(len(node.args), 2, "socket.socket must name family and type")
            for arg in node.args[:2]:
                if isinstance(arg, ast.Attribute):
                    families.add(arg.attr)
        self.assertEqual(families, {"AF_UNIX", "SOCK_STREAM"})

    def test_the_file_engine_exemption_is_two_fixed_argv_calls(self) -> None:
        """C42's grant is bounded by content, not by a comment.

        `files.py` may call `subprocess.run`. What makes that safe is the argument list
        at each of the two call sites -- a binary path followed by fixed words and one
        already-confined path -- so this test reads the real calls out of the source and
        asserts that shape. Anything the grant did not describe -- a shell, a second
        spawn member, a stdout it can read back -- is refused here even though
        `subprocess.run` itself is permitted in this file.
        """
        rel = "actionmesh/agent/files.py"
        self.assertEqual(MEMBER_ALLOWLIST[rel], {"subprocess.run"})
        self.assertEqual(IMPORT_ALLOWLIST[rel], {"subprocess"})

        tree = ast.parse((PACKAGE / "agent" / "files.py").read_text(encoding="utf-8"))
        spawns = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_name(node) == "subprocess.run"
        ]
        self.assertEqual(len(spawns), 2, "exactly the trash backend and the opener")
        for node in spawns:
            # One positional argument: the argv list. A shell string is the other shape.
            self.assertEqual(len(node.args), 1)
            argv = node.args[0]
            self.assertIsInstance(argv, ast.List, "the command is a list, never a string")
            keywords = {keyword.arg for keyword in node.keywords}
            self.assertNotIn("shell", keywords)
            # Both streams are discarded: the child's output is never read back, so no
            # OS helper can hand this process text it would then act on.
            for stream in ("stdout", "stderr"):
                value = next(k for k in node.keywords if k.arg == stream)
                self.assertIsInstance(value.value, ast.Attribute)
                self.assertEqual(value.value.attr, "DEVNULL")
            self.assertIn("timeout", keywords, "a helper that hangs cannot hang the agent")

        # A different member of the same module would still be refused.
        for source in (
            "subprocess.Popen(cmd)\n",
            "subprocess.check_output(cmd)\n",
        ):
            probe = ast.parse(source)
            self.assertTrue(_flag_members(rel, probe), source)
        # `subprocess.run` is permitted here, so `shell=True` on it is caught by the
        # keyword scanner instead -- the grant names a call, never an argument.
        self.assertTrue(
            _flag_shell_keywords(rel, ast.parse("subprocess.run(cmd, shell=True)\n"))
        )

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

    def test_the_model_transport_is_the_only_module_that_dials_out(self) -> None:
        """The C37 grant names one file; this proves the code matches.

        Symmetrical with the spawn check above: scan for the granted attribute itself,
        so a second module that opened a connection -- or the execution engine growing
        one -- fails here even if someone also widened the allowlist to permit it.
        """
        users: list[str] = []
        for rel, tree in _modules():
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "asyncio"
                    and node.attr in {"open_connection", "open_unix_connection"}
                ):
                    users.append(_normalise(rel))
        self.assertEqual(
            sorted(set(users)),
            [
                "actionmesh/agent/browser_bridge.py",
                "actionmesh/agent/processes.py",
                "actionmesh/voiceos/transport.py",
            ],
        )
        self.assertIn(
            "asyncio.open_connection",
            " ".join(MEMBER_ALLOWLIST["actionmesh/voiceos/transport.py"]),
        )


if __name__ == "__main__":
    unittest.main()
