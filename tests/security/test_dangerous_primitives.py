"""Regression guard for dangerous primitives inside the ActionMesh package.

Threats covered: **command injection**, **SSRF / outbound network access**,
**unsafe deserialization**, **dynamic import execution**, **FFI abuse**.

None of these had a test before this file. ``docs/security.md`` asserts "there
is no subprocess, eval or exec" by reading the code; a regression guard asserts
it by parsing every module in the package.

It is a guard rather than a style check: comments and docstrings are invisible
to ``ast``, so it cannot fire on prose. It fails when the *code* imports a
dangerous module or calls a dangerous builtin — exactly what would matter if
someone later added ``subprocess.run(user_input)``.

``test_guard_detects_a_violation`` proves the guard is not vacuous: a scanner
that has never seen a violation cannot be trusted to reject one.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "actionmesh"

# Importing any of these from application code is itself the finding: the core
# has no legitimate need to shell out, dial the network, or unpickle.
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
}

# Builtins that evaluate or compile code at runtime. Banned everywhere,
# including inside any path listed on the allowlist below.
DANGEROUS_CALLS = {"eval", "exec", "__import__", "compile"}

# Network/process access is forbidden package-wide by default. A future official
# adapter (C13) will legitimately need an outbound client, but only by adding a
# path here — which is deliberately empty, so widening this is a visible,
# reviewable security decision rather than a quiet loosening of the guard.
NETWORK_ALLOWED: frozenset[str] = frozenset()


def _is_dangerous(name: str) -> bool:
    """Dotted-name match: ``urllib.request`` is banned, ``urllib.parse`` is not."""
    return any(name == entry or name.startswith(entry + ".")
               for entry in DANGEROUS_MODULES)


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
    normalised = rel.replace("\\", "/")
    if any(normalised.startswith(prefix) for prefix in NETWORK_ALLOWED):
        return []
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_dangerous(alias.name):
                    problems.append(f"{rel}:{node.lineno} imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if _is_dangerous(node.module or ""):
                problems.append(f"{rel}:{node.lineno} imports from {node.module}")
            banned = DANGEROUS_MEMBERS.get(node.module or "")
            if banned:
                for alias in node.names:
                    if alias.name in banned:
                        problems.append(
                            f"{rel}:{node.lineno} imports "
                            f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Attribute):
            # ``os.system(...)`` / ``pickle.load(...)`` written as attributes.
            owner = node.value.id if isinstance(node.value, ast.Name) else ""
            if owner and node.attr in DANGEROUS_MEMBERS.get(owner, set()):
                problems.append(f"{rel}:{node.lineno} uses {owner}.{node.attr}")
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


class DangerousPrimitiveTests(unittest.TestCase):
    def test_package_files_parse(self):
        # Guards against the scanner silently finding nothing to scan.
        self.assertGreater(len(_modules()), 5)

    def test_core_never_imports_dangerous_modules(self):
        problems = [flag
                    for rel, tree in _modules()
                    for flag in _flag_imports(rel, tree)]
        self.assertEqual(
            problems, [],
            "ActionMesh must not import process, network or unsafe-decoding "
            "modules; these are the attack surface for command injection, SSRF "
            "and unsafe deserialization:\n  " + "\n  ".join(problems))

    def test_core_never_calls_dangerous_builtins(self):
        problems = [flag
                    for rel, tree in _modules()
                    for flag in _flag_calls(rel, tree)]
        self.assertEqual(
            problems, [],
            "eval/exec/__import__ let untrusted input become code:\n  "
            + "\n  ".join(problems))

    def test_guard_detects_a_violation(self):
        # Prove the scanner is not vacuous: feed it code that *does* shell out.
        sample = ast.parse("import subprocess\nsubprocess.run(x)\neval(x)\n")
        self.assertTrue(_flag_imports("sample", sample))
        self.assertTrue(_flag_calls("sample", sample))

    def test_guard_accepts_safe_code(self):
        sample = ast.parse("import json\ndef f(d):\n    return json.loads(d)\n")
        self.assertEqual(_flag_imports("sample", sample), [])
        self.assertEqual(_flag_calls("sample", sample), [])

    def test_urlparse_is_allowed_but_urlrequest_is_not(self):
        # The distinction that flagged real_base.py. Parsing a URL to enforce
        # HTTPS is not network access; urllib.request is.
        parsing = ast.parse("from urllib.parse import urlparse\n"
                            "urlparse('https://x')\n")
        self.assertEqual(_flag_imports("actionmesh/providers/real_base.py", parsing), [])
        dialing = ast.parse("import urllib.request\n"
                            "urllib.request.urlopen('http://x')\n")
        self.assertTrue(_flag_imports("actionmesh/core/x.py", dialing))

    def test_dotted_imports_are_matched_by_dotted_name(self):
        # ``import http.client`` must be caught, not missed by a first-segment check.
        tree = ast.parse("import http.client\n")
        self.assertTrue(_flag_imports("actionmesh/core/x.py", tree))

    def test_allowlist_never_exempts_dangerous_calls(self):
        # Even an authorized network adapter must not be able to eval code:
        # the allowlist relaxes imports only, never calls.
        tree = ast.parse("eval(data)\n")
        self.assertTrue(_flag_calls("actionmesh/providers/real/adapter.py", tree))


if __name__ == "__main__":
    unittest.main()
