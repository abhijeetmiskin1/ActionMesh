"""C19 editors, projects and applications through the provider.

Every assertion here is about what actually happened on the machine: which argv the
fake editor received, what a project scan could see, and which processes the agent
will agree to stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from actionmesh.agent.commands import CommandPolicy
from actionmesh.agent.paths import Workspace
from actionmesh.agent.projects import kinds_of, scan, scripts_of
from actionmesh.agent.provider import LaptopProvider
from actionmesh.agent.runner import CommandRunner
from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ProviderError, ProviderResult

_RECORD = (
    "here = os.path.dirname(os.path.abspath(__file__))\n"
    'with open(os.path.join(here, "calls.log"), "a", encoding="utf-8") as handle:\n'
    '    handle.write(sys.argv[0].split("/")[-1] + " " + " ".join(sys.argv[1:]) + "\\n")\n'
)

SHEBANG = "#!/usr/bin/env python3\nimport os, sys\n"
# A CLI returns; a GUI application does not. Two shapes because the tests need both:
# one proves what was executed, the other gives something to stop.
CLI_TAIL = SHEBANG + _RECORD + 'print("done")\n'
APP_TAIL = (
    SHEBANG
    + _RECORD
    + 'print("up", flush=True)\nimport time\nfor _ in range(3000):\n    time.sleep(0.01)\n'
)


def _action(action_type: str, **parameters: object) -> Action:
    return Action(type=action_type, provider="laptop", parameters=dict(parameters))


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin, True)
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        # `code` is a CLI that returns; `hold` is an application that stays up, so a
        # "close it" test has something that was actually running.
        for name, tail in (("code", CLI_TAIL), ("hold", APP_TAIL)):
            path = os.path.join(self.bin, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(tail)
            os.chmod(path, stat.S_IRWXU)
        original = os.environ.get("PATH", "")
        self.addCleanup(os.environ.__setitem__, "PATH", original)
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{original}"
        os.makedirs(os.path.join(self.root, "ActionMesh"), exist_ok=True)
        # One loop for the whole test. A child an action starts (an app or server that
        # stays up) belongs to the loop that started it, and `asyncio.run` would close
        # that loop the instant the call returned -- leaking the transport and, worse,
        # orphaning the child. The providers registered here are shut down on this loop
        # before it closes.
        self.loop = asyncio.new_event_loop()
        self._providers: list[LaptopProvider] = []
        self.addCleanup(self._close_loop)

    def _close_loop(self) -> None:
        for provider in self._providers:
            for record in list(provider.processes.records(include_finished=True)):
                with contextlib.suppress(Exception):
                    self.loop.run_until_complete(provider.processes.stop(record.process_id))
            for process in list(provider.processes._live.values()):
                with contextlib.suppress(Exception):
                    process.close()
        # Give the reaper a beat to collect the children it just signalled.
        self.loop.run_until_complete(asyncio.sleep(0.05))
        for provider in self._providers:
            provider.processes.drain()
        self.loop.close()

    def provider(self, *allowed: str) -> LaptopProvider:
        policy = CommandPolicy(
            allowed=allowed or ("code", "hold"),
            workspace_root=self.root,
        )
        provider = LaptopProvider(
            Workspace(self.root),
            command_policy=policy,
            runner=CommandRunner(max_concurrent=3, timeout_s=10),
        )
        self._providers.append(provider)
        return provider

    def call(self, provider: LaptopProvider, action_type: str, **params: object) -> dict:
        result = self.loop.run_until_complete(provider.execute(_action(action_type, **params)))
        self.assertTrue(result.ok, result)
        self.assertFalse(result.simulated)
        return result.data

    def reject(
        self, provider: LaptopProvider, action_type: str, **params: object
    ) -> ProviderError:
        try:
            self.loop.run_until_complete(provider.execute(_action(action_type, **params)))
        except ProviderError as exc:
            return exc
        self.fail(f"{action_type}({params}) should have been refused")
        raise AssertionError

    def calls(self) -> list[str]:
        path = os.path.join(self.bin, "calls.log")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def verify(self, provider: LaptopProvider, action: Action, result: ProviderResult):
        from actionmesh.agent.verification import register_device_strategies
        from actionmesh.core.verification.engine import default_engine

        engine = default_engine()
        register_device_strategies(engine)
        return engine.verify(action, result, provider)


class TestEditorListing(_Fixture):
    def test_installed_editors_are_listed_with_nothing_absolute(self) -> None:
        data = self.call(self.provider(), "device.ide.list")
        names = [entry["ide"] for entry in data["editors"]]
        self.assertIn("vscode", names)
        self.assertNotIn(self.bin, str(data))
        self.assertNotIn(self.root, str(data))

    def test_the_version_is_only_asked_for_permitted_editors(self) -> None:
        data = self.call(self.provider("code"), "device.ide.list")
        entry = next(item for item in data["editors"] if item["ide"] == "vscode")
        self.assertTrue(entry["version_known"])
        self.assertEqual(entry["version"], "done")

    def test_the_known_list_includes_editors_that_are_absent(self) -> None:
        data = self.call(self.provider(), "device.ide.list")
        self.assertIn("jetbrains", data["known"])
        self.assertNotIn("jetbrains", [item["ide"] for item in data["editors"]])


class TestEditorOpening(_Fixture):
    def test_opening_a_file_runs_the_editor_with_one_argument(self) -> None:
        with open(os.path.join(self.root, "ActionMesh", "main.py"), "w", encoding="utf-8") as handle:
            handle.write("print(1)\n")
        data = self.call(
            self.provider("code"), "device.ide.open", ide="vscode", path="ActionMesh/main.py"
        )
        self.assertEqual(data["exit_code"], 0)
        executed = self.calls()
        self.assertEqual(len(executed), 1, executed)
        argv = executed[0].split(" ", 1)[1]
        # One argument, the absolute path inside the workspace: the confinement
        # resolved it, and nothing about the caller's spelling was interpolated into a
        # shell command.
        self.assertTrue(argv.startswith("/"), argv)
        self.assertIn("ActionMesh/main.py", argv)
        self.assertNotIn(";", argv)

    def test_an_editor_that_is_not_allowlisted_cannot_be_run(self) -> None:
        error = self.reject(
            self.provider("hold"), "device.ide.open", ide="vscode", path="ActionMesh"
        )
        self.assertIs(error.kind, FailureKind.AUTHORIZATION)
        self.assertIn("policy refused", str(error))
        self.assertEqual(self.calls(), [])

    def test_a_path_outside_the_workspace_is_refused_before_the_editor_runs(self) -> None:
        for path in ("../../etc/passwd", "/etc/hosts", "ActionMesh/../../.ssh"):
            with self.subTest(path=path):
                error = self.reject(
                    self.provider("code"), "device.ide.open", ide="vscode", path=path
                )
                self.assertIs(error.kind, FailureKind.AUTHORIZATION)
        self.assertEqual(self.calls(), [])

    def test_opening_a_credential_file_is_refused(self) -> None:
        os.makedirs(os.path.join(self.root, ".ssh"), exist_ok=True)
        with open(os.path.join(self.root, ".ssh", "id_rsa"), "w", encoding="utf-8") as handle:
            handle.write("key\n")
        error = self.reject(
            self.provider("code"), "device.ide.open", ide="vscode", path=".ssh/id_rsa"
        )
        self.assertIs(error.kind, FailureKind.AUTHORIZATION)

    def test_an_unknown_editor_is_an_input_error_naming_the_alternatives(self) -> None:
        error = self.reject(
            self.provider("code"), "device.ide.open", ide="notepad", path="ActionMesh"
        )
        self.assertIs(error.kind, FailureKind.INVALID_INPUT)
        self.assertIn("device.ide.list", str(error))

    def test_the_default_editor_is_the_first_installed_one(self) -> None:
        data = self.call(self.provider("code"), "device.ide.open", path="ActionMesh")
        self.assertEqual(data["exit_code"], 0)
        self.assertTrue(data["command"].startswith("vscode:"))

    def test_a_launch_is_verified_as_a_command_exit_and_says_so(self) -> None:
        action = _action("device.ide.open", ide="vscode", path="ActionMesh")
        provider = self.provider("code")
        result = self.loop.run_until_complete(provider.execute(action))
        outcome = self.verify(provider, action, result)
        self.assertTrue(outcome.verified, outcome.detail)
        # The scope of the claim is part of the evidence: an editor exiting 0 is not
        # proof that a window opened, and the observation says so.
        observed = provider.observe(action)
        assert observed is not None
        self.assertIn("window", str(observed["state"]["scope"]))


class TestEditorCommands(_Fixture):
    def test_a_documented_subcommand_runs(self) -> None:
        data = self.call(
            self.provider("code"), "device.ide.command", ide="vscode", command="--status"
        )
        self.assertEqual(data["exit_code"], 0)
        self.assertIn("--status", self.calls()[0])

    def test_installing_an_extension_is_refused(self) -> None:
        error = self.reject(
            self.provider("code"),
            "device.ide.command",
            ide="vscode",
            command="--install-extension",
            arguments=["evil.vsix"],
        )
        self.assertIs(error.kind, FailureKind.INVALID_INPUT)
        self.assertEqual(self.calls(), [])

    def test_arguments_are_still_an_argument_vector(self) -> None:
        error = self.reject(
            self.provider("code"),
            "device.ide.command",
            ide="vscode",
            command="--status",
            arguments="| sh",
        )
        self.assertIs(error.kind, FailureKind.INVALID_INPUT)


class TestApplications(_Fixture):
    def test_an_allowed_application_is_started_and_owned(self) -> None:
        data = self.call(self.provider("hold"), "device.app.open", app="hold")
        self.assertEqual(data["state"], "running")
        self.assertIsNotNone(data["pid"])

    def test_a_refused_application_launch_refuses_before_spawning(self) -> None:
        error = self.reject(self.provider("code"), "device.app.open", app="hold")
        self.assertIs(error.kind, FailureKind.AUTHORIZATION)
        self.assertEqual(self.provider("code").processes.running(), 0)

    def test_only_an_application_this_agent_started_can_be_closed(self) -> None:
        provider = self.provider("hold")
        self.call(provider, "device.app.open", app="hold")
        closed = self.call(provider, "device.app.close", app="hold")
        self.assertEqual(closed["count"], 1)
        self.assertEqual(closed["closed"][0]["state"], "stopped")

    def test_closing_someone_elses_process_is_refused(self) -> None:
        # No pid argument exists on this capability at all, so there is nothing to
        # guess at: the only thing a phone can name is an app the agent launched.
        provider = self.provider("hold")
        error = self.reject(provider, "device.app.close", app="sshd")
        self.assertIs(error.kind, FailureKind.AUTHORIZATION)
        self.assertIn("started by this laptop agent", str(error))

    def test_a_closed_application_stops_counting_as_alive(self) -> None:
        provider = self.provider("hold")
        self.call(provider, "device.app.open", app="hold")
        closed = self.call(provider, "device.app.close", app="hold")
        self.assertEqual(provider.processes.running(), 0)
        record = provider.processes.get(closed["closed"][0]["process_id"])
        assert record is not None
        # Liveness, not pid existence. A killed child whose parent never reaped it is
        # a zombie: `os.kill(pid, 0)` still succeeds on one, and a registry that
        # treated that as "still running" would refuse to stop anything twice.
        self.assertFalse(provider.processes.alive(record))
        self.assertEqual(record.state, "stopped")


class TestProjectScanning(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.make("ActionMesh", {"pyproject.toml": "[project]\nname='x'\n[project.scripts]\nmypy='x:main'\n"})
        self.make("shop", {"package.json": '{"scripts": {"build": "webpack --config secrets/webpack.js", "test": "jest"}}'})
        self.make("shop/inner", {"go.mod": "module x\n"})
        os.makedirs(os.path.join(self.root, ".config"))
        os.makedirs(os.path.join(self.root, "empty"))
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        self.make("victim", {"Cargo.toml": "[package]\nname='y'\n"})
        os.symlink(os.path.join(outside, "victim"), os.path.join(self.root, "linked"))

    def make(self, name: str, files: dict[str, str]) -> None:
        directory = os.path.join(self.root, name)
        os.makedirs(directory, exist_ok=True)
        for filename, body in files.items():
            with open(os.path.join(directory, filename), "w", encoding="utf-8") as handle:
                handle.write(body)

    def test_projects_are_found_by_marker(self) -> None:
        found = {project["name"]: project for project in scan(self.root)}
        self.assertIn("ActionMesh", found)
        self.assertIn("shop", found)
        self.assertEqual(found["ActionMesh"]["kind"], "python")
        self.assertEqual(found["shop"]["kind"], "node")

    def test_scripts_are_names_only_never_the_command(self) -> None:
        found = {project["name"]: project for project in scan(self.root)}
        self.assertEqual(found["shop"]["scripts"], ["build", "test"])
        self.assertNotIn("webpack", str(found["shop"]))
        self.assertNotIn("secrets", str(found))
        self.assertEqual(found["ActionMesh"]["scripts"], ["mypy"])

    def test_dot_directories_are_skipped(self) -> None:
        self.assertNotIn(".config", [project["name"] for project in scan(self.root)])

    def test_a_symlinked_directory_is_not_followed(self) -> None:
        names = [project["name"] for project in scan(self.root)]
        self.assertNotIn("linked", names)

    def test_nested_projects_are_found(self) -> None:
        names = [project["path"] for project in scan(self.root)]
        self.assertIn("shop/inner", names)

    def test_an_empty_directory_is_not_a_project(self) -> None:
        self.assertEqual(kinds_of(os.path.join(self.root, "empty")), ())
        self.assertEqual(scripts_of(os.path.join(self.root, "empty")), [])

    def test_a_malformed_manifest_is_not_fatal(self) -> None:
        self.make("broken", {"package.json": "{not json"})
        found = {project["name"] for project in scan(self.root)}
        self.assertIn("broken", found)


class TestProjectStatus(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.project = os.path.join(self.root, "ActionMesh")
        with open(os.path.join(self.project, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("# real\n")
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=self.project, check=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        subprocess.run(["git", "add", "README.md"], cwd=self.project, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"], cwd=self.project, check=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        with open(os.path.join(self.project, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("# changed\n")
        with open(os.path.join(self.project, "new-file.txt"), "w", encoding="utf-8") as handle:
            handle.write("x\n")

    def test_branch_and_change_counts_come_from_real_git(self) -> None:
        data = self.call(self.provider("code", "git"), "device.project.status", project="ActionMesh")
        self.assertEqual(data["branch"], "main")
        self.assertEqual(data["modified"], 1)
        self.assertEqual(data["untracked"], 1)
        self.assertEqual(data["changed"], 2)

    def test_no_commit_messages_or_paths_are_exposed(self) -> None:
        # `git log` subjects name clients and codenames; the count is the useful part.
        data = self.call(self.provider("code", "git"), "device.project.status", project="ActionMesh")
        self.assertNotIn("seed", str(data))
        self.assertNotIn(self.root, str(data))
        self.assertNotIn("files changed", str(data))

    def test_git_is_not_run_when_it_is_not_allowed(self) -> None:
        data = self.call(self.provider("code"), "device.project.status", project="ActionMesh")
        self.assertNotIn("branch", data)
        self.assertIn("scripts", data)

    def test_a_project_outside_the_workspace_is_refused(self) -> None:
        self.assertIs(
            self.reject(
                self.provider("code", "git"), "device.project.status", project="/etc"
            ).kind,
            FailureKind.AUTHORIZATION,
        )

    def test_a_project_lists_the_services_running_under_it(self) -> None:
        provider = self.provider("code", "git", "hold")
        self.call(
            provider,
            "device.server.start",
            name="api",
            command="hold",
            arguments=[],
            working_directory="ActionMesh",
        )
        data = self.call(provider, "device.project.status", project="ActionMesh")
        self.assertEqual(len(data["services"]), 1)
        self.assertEqual(data["services"][0]["label"], "api")

    def test_project_list_is_discoverable_through_the_capability(self) -> None:
        data = self.call(self.provider("code"), "device.project.list")
        names = [project["name"] for project in data["projects"]]
        self.assertIn("ActionMesh", names)


if __name__ == "__main__":
    unittest.main()
