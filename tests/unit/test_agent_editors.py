"""C19 editor discovery tests, against fake editors on a real PATH.

The fakes are executable scripts that record their argv, so a test can assert what
the machine was actually asked to run rather than what the code intended.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
import unittest

from actionmesh.agent.commands import CommandPolicy
from actionmesh.agent.errors import AgentError
from actionmesh.agent.ides import (
    KNOWN_IDES,
    EditorDiscovery,
    EditorNotFound,
    IDESpec,
)
from actionmesh.agent.runner import CommandRunner

SHIM = """#!/usr/bin/env python3
import os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, {log!r}), "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
{body}
"""


class _Bins(unittest.TestCase):
    """A private PATH containing two editors and nothing else."""

    def setUp(self) -> None:
        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin, True)
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self._make("code", 'print("1.90.2")', "code.log")
        self._make("nvim", 'print("NVIM v0.10")', "nvim.log")
        self._patch_path()

    def _make(self, name: str, body: str, log: str) -> str:
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(SHIM.format(body=body, log=log))
        os.chmod(path, stat.S_IRWXU)
        return path

    def _patch_path(self) -> None:
        original = os.environ.get("PATH", "")
        self.addCleanup(os.environ.__setitem__, "PATH", original)
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{original}"

    def calls(self, log: str) -> list[str]:
        path = os.path.join(self.bin, log)
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]


class TestDiscovery(_Bins):
    def setUp(self) -> None:
        super().setUp()
        self.discovery = EditorDiscovery()

    def test_every_reported_editor_really_is_on_the_path(self) -> None:
        # Not "equals exactly vscode+neovim": the host may genuinely have gedit or hx
        # installed, and a test that fails because of what is on the machine rather
        # than because of what the code did is a test nobody trusts after the third
        # time. What must hold is the correspondence in both directions.
        found = {spec.ide: binary for spec, binary in self.discovery.installed()}
        self.assertIn("vscode", found)
        self.assertIn("neovim", found)
        for ide, binary in found.items():
            self.assertTrue(os.path.isfile(binary), ide)
            self.assertTrue(os.access(binary, os.X_OK), ide)
        for spec in KNOWN_IDES:
            present = any(
                os.path.isfile(os.path.join(entry, name))
                for name in spec.binaries
                for entry in os.environ["PATH"].split(os.pathsep)
            )
            self.assertEqual(present, spec.ide in found, spec.ide)

    def test_the_first_matching_binary_wins(self) -> None:
        _, binary = next(
            (spec, path) for spec, path in self.discovery.installed() if spec.ide == "vscode"
        )
        self.assertEqual(os.path.basename(binary), "code")

    def test_a_known_ide_resolves(self) -> None:
        spec = self.discovery.spec("vscode")
        self.assertEqual(spec.ide, "vscode")
        self.assertEqual(spec.label, "Visual Studio Code")

    def test_an_unknown_ide_names_the_alternatives(self) -> None:
        with self.assertRaises(EditorNotFound) as ctx:
            self.discovery.spec("notepad")
        self.assertIn("notepad", str(ctx.exception))
        self.assertIn("device.ide.list", str(ctx.exception))

    def test_case_and_padding_in_the_name_are_tolerated(self) -> None:
        self.assertIs(self.discovery.spec("  VSCode ").ide, "vscode")

    def test_discovery_runs_nothing(self) -> None:
        self.discovery.installed()
        self.assertEqual(self.calls("code.log"), [])


class TestVersionProbe(_Bins):
    def policy(self, *allowed: str) -> CommandPolicy:
        return CommandPolicy(allowed=allowed or ("code",), workspace_root=self.root)

    def build(self, policy: CommandPolicy) -> EditorDiscovery:
        return EditorDiscovery(
            runner=CommandRunner(max_concurrent=2, timeout_s=5),
            policy_for=lambda binary: CommandPolicy(
                allowed=(binary,), workspace_root=self.root
            ),
            permitted=lambda spec: any(
                name in policy.allowed for name in spec.binaries
            ),
        )

    def test_a_permitted_editor_is_asked_for_its_version(self) -> None:
        spec = self.build(self.policy("code")).spec("vscode")
        binary = spec and os.path.join(self.bin, "code")
        version = asyncio.run(
            self.build(self.policy("code")).version(spec, str(binary))
        )
        self.assertEqual(version, "1.90.2")
        self.assertEqual(self.calls("code.log"), ["--version"])

    def test_an_unpermitted_editor_is_never_executed(self) -> None:
        # The whole point: a version probe *runs the editor*, so it is gated by the
        # operator's allowlist and not by the editor existing.
        discovery = self.build(self.policy("nvim"))
        spec = discovery.spec("vscode")
        version = asyncio.run(discovery.version(spec, os.path.join(self.bin, "code")))
        self.assertIsNone(version)
        self.assertEqual(self.calls("code.log"), [])

    def test_a_version_is_cached_so_a_dashboard_does_not_spawn_per_poll(self) -> None:
        discovery = self.build(self.policy("code"))
        spec = discovery.spec("vscode")
        binary = os.path.join(self.bin, "code")
        asyncio.run(discovery.version(spec, binary))
        asyncio.run(discovery.version(spec, binary))
        asyncio.run(discovery.version(spec, binary))
        self.assertEqual(self.calls("code.log"), ["--version"])

    def test_describe_reports_the_table_not_the_path(self) -> None:
        discovery = self.build(self.policy("code"))
        spec = discovery.spec("vscode")
        described = discovery.describe(spec, os.path.join(self.bin, "code"), "1.90.2")
        self.assertEqual(described["version"], "1.90.2")
        self.assertIn("--list-extensions", described["commands"])
        self.assertNotIn(self.bin, str(described))
        self.assertEqual(described["command"], "code")

    def test_no_runner_means_no_probe(self) -> None:
        discovery = EditorDiscovery()
        spec = discovery.spec("vscode")
        self.assertIsNone(asyncio.run(discovery.version(spec, "/somewhere/code")))


class TestArgvBuilding(_Bins):
    def setUp(self) -> None:
        super().setUp()
        self.discovery = EditorDiscovery()

    def test_open_passes_the_path_as_one_argument(self) -> None:
        _, binary, argv = self.discovery.open_argv("vscode", "/tmp/some dir/app.py")
        self.assertEqual(argv, [binary, "/tmp/some dir/app.py"])

    def test_opening_an_absent_editor_is_a_refusal_with_alternatives(self) -> None:
        with self.assertRaises(EditorNotFound) as ctx:
            self.discovery.open_argv("sublime", "/tmp/x")
        self.assertIn("not installed", str(ctx.exception))

    def test_only_documented_subcommands_are_accepted(self) -> None:
        _, binary, argv = self.discovery.command_argv("vscode", "--status", ())
        self.assertEqual(argv, [binary, "--status"])
        self.assertEqual(argv[0], binary)

    def test_extension_installing_is_refused(self) -> None:
        # `--install-extension` pulls third-party code and runs it with the editor's
        # privileges: a much larger claim on the machine than "open this folder".
        with self.assertRaises(EditorNotFound) as ctx:
            self.discovery.command_argv(
                "vscode", "--install-extension", ("malicious.vsix",)
            )
        self.assertIn("not a command", str(ctx.exception))

    def test_an_arbitrary_argument_list_cannot_hide_behind_a_good_subcommand(self) -> None:
        # Still refused: the subcommand itself is not in the table.
        with self.assertRaises(AgentError):
            self.discovery.command_argv("vscode", "--extensions-dir / --exec-code", ())


class TestKnownEditors(unittest.TestCase):
    def test_every_known_editor_has_a_version_flag_and_a_binary(self) -> None:
        for spec in KNOWN_IDES:
            with self.subTest(ide=spec.ide):
                self.assertTrue(spec.binaries)
                self.assertTrue(spec.version_flag.startswith("--"))
                self.assertIn("--version", spec.commands)

    def test_no_adapter_offers_a_flag_that_fetches_code(self) -> None:
        # A guard against the table drifting toward "install something" over time.
        forbidden = {"--install-extension", "--install", "--update", "update"}
        for spec in KNOWN_IDES:
            self.assertFalse(forbidden & set(spec.commands), spec.ide)

    def test_editor_names_are_unique(self) -> None:
        ids = [spec.ide for spec in KNOWN_IDES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_idespec_is_hashable_and_frozen(self) -> None:
        spec = IDESpec(ide="x", label="X", binaries=("x",))
        self.assertEqual(len({spec, spec}), 1)
        with self.assertRaises(AttributeError):
            spec.ide = "y"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
