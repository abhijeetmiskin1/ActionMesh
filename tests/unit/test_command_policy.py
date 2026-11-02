"""C19 command policy tests — the boundary that decides what this laptop can run.

The rule every test here defends: the allowlist is checked against the binary that
would *actually* execute, after resolution, and every other question is answered by
refusing. Nothing in this module is an authorization decision — that is C3's job — and
a test that conflated the two would let a second policy engine appear here unnoticed.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

from actionmesh.agent.commands import (
    DEFAULT_ALLOWED,
    DEFAULT_DENIED,
    MAX_ARGUMENTS,
    CommandDenied,
    CommandPolicy,
    merge_allowed,
)
from actionmesh.agent.config import AgentConfig
from actionmesh.agent.errors import CommandDenied as ErrorCommandDenied


class TestExecutableAllowlist(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.policy = CommandPolicy(allowed=("git", "python3"), workspace_root=self.root)

    def test_an_allowed_name_resolves_and_is_permitted(self) -> None:
        decision = self.policy.evaluate("git", ["--version"])
        self.assertTrue(decision.allowed, decision.reasons)
        self.assertTrue(os.path.isabs(decision.resolved_path))
        self.assertEqual(decision.arguments, ("--version",))
        self.assertNotIn(decision.resolved_path, decision.arguments)

    def test_an_unlisted_name_is_refused(self) -> None:
        decision = self.policy.evaluate("id", ["-u"])
        self.assertTrue(decision.refused)
        self.assertTrue(decision.reasons)

    def test_a_denied_name_is_refused_even_when_allowlisted(self) -> None:
        # The operator's allowlist cannot overrule the refuse-even-if-allowed set:
        # that is what makes `rm` unavailable rather than a configuration accident.
        policy = CommandPolicy(allowed=("rm", "pkill"), workspace_root=self.root)
        self.assertTrue(policy.evaluate("rm", ["-rf", self.root]).refused)
        self.assertTrue(policy.evaluate("pkill", ["x"]).refused)

    def test_a_rename_or_multicall_of_a_denied_binary_is_still_denied(self) -> None:
        """The hole a single-name list leaves open, measured on this machine.

        `/usr/bin/rm` resolves to a file named `gnurm`, and `pkill` is one binary with
        `pgrep`. Both spellings are refused whichever way the lookup lands.
        """
        for name in ("rm", "pkill", "nc", "init"):
            with self.subTest(name=name):
                policy = CommandPolicy(allowed=(name,), workspace_root=self.root)
                decision = policy.evaluate(name, ["x"])
                self.assertTrue(decision.refused, decision.reasons)
                self.assertIn("refused regardless", decision.reasons[0])

    def test_a_symlink_named_like_a_allowed_program_is_refused_as_what_it_points_at(self) -> None:
        fake = os.path.join(self.root, "git")
        os.symlink("/bin/rm", fake)
        policy = CommandPolicy(allowed=("git",), workspace_root=self.root)
        decision = policy.evaluate(fake)
        self.assertTrue(decision.refused, decision.reasons)

    def test_a_program_that_does_not_exist_is_refused(self) -> None:
        self.assertTrue(self.policy.evaluate("nosuchbinaryatall").refused)

    def test_an_absolute_path_outside_the_allowlist_is_refused(self) -> None:
        decision = self.policy.evaluate("/bin/echo", ["hi"])
        self.assertTrue(decision.refused)

    def test_every_default_denied_name_stays_denied_when_allowlisted(self) -> None:
        for name in sorted(DEFAULT_DENIED):
            with self.subTest(name=name):
                policy = CommandPolicy(allowed=(name,), workspace_root=self.root)
                self.assertTrue(policy.evaluate(name, ["x"]).refused)

    def test_defaults_cover_the_development_tools_the_spec_names(self) -> None:
        for name in ("python", "node", "npm", "git", "pytest", "ruff", "uvicorn"):
            self.assertIn(name, DEFAULT_ALLOWED)

    def test_merge_allowed_is_deduplicated_and_sorted(self) -> None:
        merged = merge_allowed(["make", "git", " ", "cmake"])
        self.assertEqual(merged, tuple(sorted({"make", "cmake", *DEFAULT_ALLOWED})))


class TestArguments(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.policy = CommandPolicy(allowed=("git", "python3"), workspace_root=self.root)

    def test_one_string_arguments_are_refused(self) -> None:
        # There is no shell to split it, and accepting it would invite someone to add
        # one. The API is an argument vector or nothing.
        decision = self.policy.evaluate("git", "status && rm -rf /")
        self.assertTrue(decision.refused)
        self.assertIn("no shell", decision.reasons[0])

    def test_non_string_members_are_refused(self) -> None:
        for bad in (["ok", 7], [{"a": 1}], [None]):
            with self.subTest(bad=bad):
                self.assertTrue(self.policy.evaluate("git", bad).refused)

    def test_nul_and_oversized_arguments_are_refused(self) -> None:
        for bad in (["a\x00b"], ["x" * 5000], ["y"] * (MAX_ARGUMENTS + 1)):
            with self.subTest(count=len(bad)):
                self.assertTrue(self.policy.evaluate("git", bad).refused)

    def test_a_missing_command_is_refused(self) -> None:
        for bad in ("", "   ", None, 12):
            with self.subTest(bad=bad):
                self.assertTrue(self.policy.evaluate(bad).refused)

    def test_arguments_may_be_absent(self) -> None:
        self.assertTrue(self.policy.evaluate("git", None).allowed)


class TestInlineCode(unittest.TestCase):
    """`python -m pytest` runs an installed module. `python -c SRC` runs source."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.policy = CommandPolicy(allowed=("python3", "git", "node"), workspace_root=self.root)

    def test_running_a_module_is_allowed(self) -> None:
        self.assertTrue(self.policy.evaluate("python3", ["-m", "pytest", "-q"]).allowed)

    def test_running_a_file_is_allowed(self) -> None:
        script = os.path.join(self.root, "tasks.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("print('ok')\n")
        self.assertTrue(self.policy.evaluate("python3", [script]).allowed)

    def test_inline_source_is_refused(self) -> None:
        for args in (["-c", "import os"], ["--version", "-c", "x"]):
            with self.subTest(args=args):
                decision = self.policy.evaluate("python3", args)
                self.assertTrue(decision.refused)
                self.assertIn("not code given on the command line", decision.reasons[0])

    def test_a_combined_option_hiding_the_code_flag_is_refused(self) -> None:
        self.assertTrue(self.policy.evaluate("python3", ["-uc", "x"]).refused)

    def test_a_filename_containing_a_flag_letter_is_not_refused(self) -> None:
        # `-e` is a node code flag; `app.js` merely contains the letter. A guard that
        # refuses real work here gets switched off in practice.
        script = os.path.join(self.root, "app.js")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("console.log(1)\n")
        self.assertTrue(self.policy.evaluate("node", [script]).allowed)

    def test_an_explicitly_enabled_interpreter_allows_code(self) -> None:
        permissive = CommandPolicy(
            allowed=("python3",),
            workspace_root=self.root,
            allow_interpreter_code_flags=True,
        )
        self.assertTrue(permissive.evaluate("python3", ["-c", "print(1)"]).allowed)

    def test_git_config_override_is_refused_because_the_pager_is_a_shell(self) -> None:
        self.assertTrue(self.policy.evaluate("git", ["-c", "core.pager=cat", "log"]).refused)

    def test_git_transport_and_alias_escapes_are_refused(self) -> None:
        for args in (["clone", "ext::sh -c id"], ["config", "alias.x", "!id"]):
            with self.subTest(args=args):
                self.assertTrue(self.policy.evaluate("git", args).refused)


class TestWorkingDirectory(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.outside, True)
        self.policy = CommandPolicy(allowed=("git",), workspace_root=self.root)

    def test_the_default_is_the_workspace_root(self) -> None:
        decision = self.policy.evaluate("git", ["status"])
        self.assertEqual(decision.working_directory, os.path.realpath(self.root))

    def test_a_subdirectory_of_the_root_is_allowed(self) -> None:
        nested = os.path.join(self.root, "project")
        os.mkdir(nested)
        decision = self.policy.evaluate("git", working_directory=nested)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.working_directory, os.path.realpath(nested))

    def test_a_relative_directory_is_resolved_against_the_root(self) -> None:
        # The phone's only path vocabulary is workspace-relative; a relative cwd must
        # mean the same thing here, not wherever the server process happens to sit.
        nested = os.path.join(self.root, "project")
        os.mkdir(nested)
        decision = self.policy.evaluate("git", working_directory="project")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.working_directory, os.path.realpath(nested))

    def test_a_relative_directory_does_not_follow_the_process_cwd(self) -> None:
        # A name that exists under the process CWD but not in the workspace must not
        # be honoured: resolving "outside" against os.getcwd() would let a phone reach
        # a directory it was never told about, just by guessing where the server runs.
        elsewhere = os.path.join(self.outside, "outside")
        os.mkdir(elsewhere)
        original = os.getcwd()
        os.chdir(self.outside)
        self.addCleanup(os.chdir, original)
        decision = self.policy.evaluate("git", working_directory="outside")
        self.assertTrue(decision.refused)

    def test_relative_traversal_out_of_the_root_is_refused(self) -> None:
        os.makedirs(os.path.join(self.root, "project"))
        decision = self.policy.evaluate("git", working_directory="project/../../..")
        self.assertTrue(decision.refused)

    def test_a_directory_outside_the_root_is_refused(self) -> None:
        decision = self.policy.evaluate("git", working_directory=self.outside)
        self.assertTrue(decision.refused)
        self.assertIn("outside the directories", decision.reasons[0])

    def test_traversal_to_a_real_directory_outside_is_refused(self) -> None:
        decision = self.policy.evaluate("git", working_directory=self.root + "/../../etc")
        self.assertTrue(decision.refused)

    def test_a_missing_directory_is_refused(self) -> None:
        self.assertTrue(
            self.policy.evaluate("git", working_directory=self.root + "/nope").refused
        )

    def test_an_extra_configured_directory_is_honoured(self) -> None:
        policy = CommandPolicy(
            allowed=("git",), workspace_root=self.root, extra_working_dirs=(self.outside,)
        )
        self.assertTrue(policy.evaluate("git", working_directory=self.outside).allowed)

    def test_no_root_configured_means_no_working_directory_is_legal(self) -> None:
        policy = CommandPolicy(allowed=("git",))
        decision = policy.evaluate("git")
        self.assertTrue(decision.refused)
        self.assertIn("no working directory", decision.reasons[0])


class TestEnvironment(unittest.TestCase):
    """A child process is handed a built environment, never this process's."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.policy = CommandPolicy(allowed=("git",), workspace_root=self.root)

    def test_the_server_secret_never_reaches_a_child(self) -> None:
        os.environ["ACTIONMESH_SECRET_KEY"] = "a-real-looking-secret"
        self.addCleanup(os.environ.pop, "ACTIONMESH_SECRET_KEY", None)
        decision = self.policy.evaluate("git", ["--version"])
        self.assertTrue(decision.allowed)
        self.assertNotIn("ACTIONMESH_SECRET_KEY", decision.environment)

    def test_the_environment_is_small(self) -> None:
        decision = self.policy.evaluate("git")
        self.assertLessEqual(
            set(decision.environment), {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM"}
        )

    def test_client_supplied_environment_is_refused_by_default(self) -> None:
        decision = self.policy.evaluate("git", env={"LANG": "C"})
        self.assertTrue(decision.refused)
        self.assertIn("does not accept environment variables", decision.reasons[0])

    def test_even_an_enabled_env_request_cannot_name_a_secret(self) -> None:
        policy = CommandPolicy(
            allowed=("git",), workspace_root=self.root, allow_client_env=True,
            extra_env=("LANG",),
        )
        for key in ("AWS_SECRET_ACCESS_KEY", "MY_API_KEY", "SSH_PRIVATE_KEY", "PASSWORD"):
            with self.subTest(key=key):
                decision = policy.evaluate("git", env={key: "x"})
                self.assertTrue(decision.refused)
                self.assertIn("secret", decision.reasons[0].lower())

    def test_an_unlisted_but_innocuous_key_is_still_refused(self) -> None:
        policy = CommandPolicy(
            allowed=("git",), workspace_root=self.root, allow_client_env=True
        )
        self.assertTrue(policy.evaluate("git", env={"EDITOR": "vim"}).refused)

    def test_a_listed_key_passes_when_env_requests_are_allowed(self) -> None:
        policy = CommandPolicy(
            allowed=("git",), workspace_root=self.root, allow_client_env=True
        )
        decision = policy.evaluate("git", env={"LANG": "C.UTF-8"})
        self.assertTrue(decision.allowed, decision.reasons)
        self.assertEqual(decision.environment.get("LANG"), "C.UTF-8")

    def test_an_operator_extra_key_is_passed_but_never_a_secret_shaped_one(self) -> None:
        os.environ["PROJECT_MODE"] = "ci"
        self.addCleanup(os.environ.pop, "PROJECT_MODE", None)
        policy = CommandPolicy(allowed=("git",), workspace_root=self.root, extra_env=("PROJECT_MODE",))
        self.assertEqual(policy.evaluate("git").environment.get("PROJECT_MODE"), "ci")
        os.environ["PROJECT_TOKEN"] = "leak"
        self.addCleanup(os.environ.pop, "PROJECT_TOKEN", None)
        strict = CommandPolicy(allowed=("git",), workspace_root=self.root, extra_env=("PROJECT_TOKEN",))
        self.assertTrue(strict.evaluate("git").refused)

    def test_a_non_mapping_env_is_refused(self) -> None:
        policy = CommandPolicy(
            allowed=("git",), workspace_root=self.root, allow_client_env=True
        )
        self.assertTrue(policy.evaluate("git", env=["LANG=C"]).refused)


class TestFromConfig(unittest.TestCase):
    def test_the_policy_is_built_from_the_agent_config(self) -> None:
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        config = AgentConfig(
            device_id="laptop-1",
            workspace_root=root,
            allow_shell=True,
            allowed_commands=("git",),
            denied_commands=("node",),
        )
        policy = CommandPolicy.from_config(config)
        self.assertEqual(policy.allowed, ("git",))
        self.assertIn("node", policy.denied)
        self.assertEqual(policy.workspace_root, root)

    def test_an_empty_allowed_list_falls_back_to_the_defaults_not_to_anything(self) -> None:
        config = AgentConfig(device_id="laptop-1", workspace_root=tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, config.workspace_root, True)
        policy = CommandPolicy.from_config(config)
        self.assertEqual(policy.allowed, DEFAULT_ALLOWED)

    def test_a_non_config_object_is_refused(self) -> None:
        with self.assertRaises((CommandDenied, ErrorCommandDenied)):
            CommandPolicy.from_config(object())


class TestShape(unittest.TestCase):
    def test_python3_resolves_on_this_platform(self) -> None:
        policy = CommandPolicy(allowed=("python3",), workspace_root=os.getcwd())
        decision = policy.evaluate("python3", ["--version"])
        self.assertTrue(decision.allowed, decision.reasons)
        self.assertEqual(decision.resolved_path, os.path.realpath(sys.executable))

    def test_every_refusal_carries_a_reason(self) -> None:
        policy = CommandPolicy(allowed=("git",), workspace_root="/nonexistent-root")
        for args in ("nosuch", "rm", "curl"):
            self.assertTrue(policy.evaluate(args).refused)


if __name__ == "__main__":
    unittest.main()
