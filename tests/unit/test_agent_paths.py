"""C19 workspace confinement tests — every way a path can try to leave the root.

Section 4 of the brief lists them: `../`, absolute-path escape, symlink escape,
filesystem root access, credential directories, SSH keys, browser profiles. Each is
tested against a real directory on a real filesystem, because confinement that is
only exercised against a mock filesystem proves the mock.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from actionmesh.agent.errors import PathEscape, PathNotFound
from actionmesh.agent.paths import Workspace


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.outside, True)
        with open(os.path.join(self.root, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("inside\n")
        os.mkdir(os.path.join(self.root, "src"))
        with open(os.path.join(self.root, "src", "main.py"), "w", encoding="utf-8") as fh:
            fh.write("print('hi')\n")
        with open(os.path.join(self.outside, "secret.txt"), "w", encoding="utf-8") as fh:
            fh.write("outside\n")
        self.workspace = Workspace(self.root)


class TestHappyPath(_Fixture):
    def test_a_relative_path_resolves_inside(self) -> None:
        resolved = self.workspace.resolve("notes.txt", must_exist=True)
        self.assertEqual(resolved.relative, "notes.txt")
        self.assertEqual(resolved.absolute, os.path.realpath(os.path.join(self.root, "notes.txt")))

    def test_nested_paths_are_fine(self) -> None:
        resolved = self.workspace.resolve("src/main.py", must_exist=True)
        self.assertTrue(resolved.absolute.startswith(self.workspace.root + os.sep))

    def test_dot_segments_are_accepted_where_harmless(self) -> None:
        self.assertEqual(self.workspace.resolve("./notes.txt").relative, "notes.txt")
        self.assertEqual(self.workspace.resolve("src/./main.py").relative, "src/main.py")

    def test_the_workspace_root_must_be_absolute_and_real(self) -> None:
        with self.assertRaises(PathEscape):
            Workspace("relative/path")
        with self.assertRaises(PathEscape):
            Workspace("/definitely/not/a/directory")
        with self.assertRaises(PathEscape):
            Workspace("")


class TestTraversal(_Fixture):
    def test_parent_references_are_refused(self) -> None:
        for bad in (
            "../escape",
            "notes.txt/../../etc/passwd",
            "src/../outside",
            "..",
            "../" + os.path.basename(self.outside),
            "a/..",
            "./..",
        ):
            with self.subTest(bad=bad), self.assertRaises(PathEscape):
                self.workspace.resolve(bad)

    def test_encoded_looking_traversal_is_still_traversal(self) -> None:
        # The transport decodes before this sees it; nothing here re-decodes.
        for bad in ("..%2f..%2fetc", "….//.."):
            with self.subTest(bad=bad), self.assertRaises(PathEscape):
                self.workspace.resolve(bad)

    def test_absolute_paths_are_refused(self) -> None:
        for bad, why in (
            ("/etc/passwd", "absolute"),
            (self.outside + "/secret.txt", "absolute"),
            ("C:\\Windows\\win.ini", "drive-qualified"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(PathEscape) as ctx:
                    self.workspace.resolve(bad)
                self.assertIn(why, str(ctx.exception))

    def test_a_root_prefix_lookalike_does_not_count_as_inside(self) -> None:
        # `/tmp/abc` is a prefix of `/tmp/abcdef` but is a different directory.
        neighbour = self.root + "ish"
        os.mkdir(neighbour)
        self.addCleanup(shutil.rmtree, neighbour, True)
        try:
            os.symlink(os.path.join(neighbour, "x"), os.path.join(self.root, "trap"))
        except OSError:
            self.skipTest("symlinks unavailable here")
        with self.assertRaises(PathEscape):
            self.workspace.resolve("trap", must_exist=True)
        os.unlink(os.path.join(self.root, "trap"))

    def test_nul_bytes_and_empties_are_refused(self) -> None:
        for bad in ("", "   ", "notes\x00.txt", "a" * 5000):
            with self.subTest(bad=bad), self.assertRaises(PathEscape):
                self.workspace.resolve(bad)

    def test_non_strings_are_refused(self) -> None:
        for bad in (None, 7, ["a"], {"path": "a"}):
            with self.subTest(bad=bad), self.assertRaises(PathEscape):
                self.workspace.resolve(bad)

    def test_a_missing_file_is_a_distinct_refusal(self) -> None:
        # Not an escape: the caller can fix a typo, and an audit must not read a
        # wrong filename as an attack.
        with self.assertRaises(PathNotFound):
            self.workspace.resolve("nope.txt", must_exist=True)


class TestSymlinks(_Fixture):
    def _link(self, name: str, target: str) -> str:
        path = os.path.join(self.root, name)
        os.symlink(target, path)
        self.addCleanup(lambda: os.path.islink(path) and os.unlink(path))
        return path

    def test_a_link_out_of_the_root_is_refused(self) -> None:
        self._link("out", os.path.join(self.outside, "secret.txt"))
        with self.assertRaises(PathEscape):
            self.workspace.resolve("out", must_exist=True)

    def test_a_link_to_the_filesystem_root_is_refused(self) -> None:
        self._link("etc", "/etc")
        with self.assertRaises(PathEscape):
            self.workspace.resolve("etc/passwd")

    def test_a_directory_link_out_is_refused_for_writes(self) -> None:
        self._link("escape", self.outside)
        with self.assertRaises(PathEscape):
            self.workspace.resolve("escape/planted.txt")

    def test_a_link_inside_the_root_is_still_refused_by_default(self) -> None:
        # Harmless in principle, and the same mechanism as the dangerous case. The
        # conservative default is to refuse; a caller asks deliberately.
        self._link("alias", "notes.txt")
        with self.assertRaises(PathEscape):
            self.workspace.resolve("alias", must_exist=True)

    def test_follow_final_symlink_is_opt_in_and_still_confined(self) -> None:
        self._link("alias", "notes.txt")
        permissive = Workspace(self.root, follow_final_symlink=True)
        self.assertEqual(permissive.resolve("alias", must_exist=True).relative, "alias")
        self._link("out", os.path.join(self.outside, "secret.txt"))
        with self.assertRaises(PathEscape):
            permissive.resolve("out", must_exist=True)

    def test_a_symlink_swapped_in_after_resolve_cannot_carry_a_write(self) -> None:
        """The TOCTOU case a string check cannot close.

        `resolve()` is given a legitimate, absent path. Before it is opened, the name
        becomes a link out of the root. Opening happens by walking one component at a
        time with O_NOFOLLOW, so the kernel — not a comparison made earlier — decides.
        """
        target = self.workspace.resolve("late.txt")
        os.symlink(os.path.join(self.outside, "pwned.txt"), os.path.join(self.root, "late.txt"))
        with self.assertRaises(PathEscape):
            self.workspace.open_fd(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        self.assertFalse(os.path.exists(os.path.join(self.outside, "pwned.txt")))

    def test_a_directory_swapped_under_a_delete_is_refused(self) -> None:
        os.mkdir(os.path.join(self.root, "dir"))
        with open(os.path.join(self.root, "dir", "victim.txt"), "w", encoding="utf-8") as fh:
            fh.write("in\n")
        resolved = self.workspace.resolve("dir/victim.txt")
        os.rename(os.path.join(self.root, "dir"), os.path.join(self.outside, "dir"))
        os.symlink(os.path.join(self.outside, "dir"), os.path.join(self.root, "dir"))
        with self.assertRaises(PathEscape):
            self.workspace.unlink(resolved)
        self.assertTrue(os.path.exists(os.path.join(self.outside, "dir", "victim.txt")))


class TestDeniedLocations(_Fixture):
    def test_credential_named_directories_are_refused(self) -> None:
        for name in (".ssh", ".aws", ".gnupg", ".kube"):
            os.mkdir(os.path.join(self.root, name))
            with self.subTest(name=name), self.assertRaises(PathEscape):
                self.workspace.resolve(f"{name}/config")

    def test_key_files_are_refused_wherever_they_sit(self) -> None:
        os.mkdir(os.path.join(self.root, "project"))
        for name in ("id_rsa", "id_ed25519", ".netrc", ".git-credentials"):
            path = os.path.join(self.root, "project", name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x")
            with self.subTest(name=name), self.assertRaises(PathEscape):
                self.workspace.resolve(f"project/{name}", must_exist=True)

    def test_env_files_are_not_exposed(self) -> None:
        for name in (".env", ".env.local", "project/.env"):
            directory = os.path.join(self.root, os.path.dirname(name))
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(self.root, name), "w", encoding="utf-8") as fh:
                fh.write("API_KEY=leak\n")
            with self.subTest(name=name):
                with self.assertRaises(PathEscape) as ctx:
                    self.workspace.resolve(name)
                self.assertIn("secret files", str(ctx.exception))

    def test_a_browser_profile_is_refused(self) -> None:
        os.makedirs(os.path.join(self.root, ".config", "google-chrome", "Default"))
        with self.assertRaises(PathEscape):
            self.workspace.resolve(".config/google-chrome/Default/Cookies")

    def test_a_denied_name_reached_through_a_symlink_is_still_denied(self) -> None:
        os.mkdir(os.path.join(self.root, ".ssh"))
        os.symlink(
            os.path.join(self.root, ".ssh"), os.path.join(self.outside, "looks-safe")
        )
        self.addCleanup(lambda: os.path.islink(os.path.join(self.outside, "looks-safe")) and os.unlink(os.path.join(self.outside, "looks-safe")))
        # Even a permissive workspace must not read into .ssh by walking through a link.
        permissive = Workspace(self.outside, follow_final_symlink=True)
        with self.assertRaises(PathEscape):
            permissive.resolve("looks-safe/id_rsa")

    def test_extra_denied_names_are_honoured(self) -> None:
        os.mkdir(os.path.join(self.root, "secrets"))
        strict = Workspace(self.root, extra_denied=("secrets",))
        with self.assertRaises(PathEscape):
            strict.resolve("secrets/token")

    def test_a_refusal_quotes_the_callers_spelling_not_a_resolved_path(self) -> None:
        with self.assertRaises(PathEscape) as ctx:
            self.workspace.resolve("/etc/passwd")
        self.assertNotIn(self.workspace.root, str(ctx.exception))


class TestWalkingAndListing(_Fixture):
    def test_walk_fd_reaches_a_nested_directory(self) -> None:
        fd = self.workspace.walk_fd("src")
        os.close(fd)

    def test_walk_fd_refuses_a_link_component(self) -> None:
        os.symlink(self.outside, os.path.join(self.root, "via"))
        with self.assertRaises(PathEscape):
            self.workspace.walk_fd("via")

    def test_walk_fd_reports_a_missing_directory(self) -> None:
        with self.assertRaises(PathNotFound):
            self.workspace.walk_fd("nope")

    def test_listing_reports_kinds_and_relative_paths(self) -> None:
        entries = {entry["name"]: entry for entry in self.workspace.list()}
        self.assertEqual(entries["notes.txt"]["type"], "file")
        self.assertEqual(entries["src"]["type"], "dir")
        self.assertEqual(entries["src"]["path"], "src")

    def test_a_link_out_is_reported_but_not_followed(self) -> None:
        os.symlink(os.path.join(self.outside, "secret.txt"), os.path.join(self.root, "lnk"))
        entries = {entry["name"]: entry for entry in self.workspace.list()}
        self.assertEqual(entries["lnk"]["type"], "link")
        self.assertFalse(entries["lnk"]["confined"])
        # The listing reports the link, never its target's contents.
        self.assertEqual(entries["lnk"]["size"], 0)

    def test_listing_is_capped(self) -> None:
        for index in range(6):
            with open(os.path.join(self.root, f"f{index}.txt"), "w", encoding="utf-8") as fh:
                fh.write("x")
        self.assertEqual(len(self.workspace.list(limit=3)), 3)

    def test_listing_the_root_with_an_empty_relative(self) -> None:
        self.assertEqual(
            [e["path"] for e in self.workspace.list("")],
            ["notes.txt", "src"],
        )

    def test_unlink_removes_only_confined_files(self) -> None:
        with open(os.path.join(self.root, "gone.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        self.workspace.unlink(self.workspace.resolve("gone.txt"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "gone.txt")))

    def test_unlink_of_the_root_is_refused(self) -> None:
        with self.assertRaises(PathEscape):
            self.workspace.unlink(self.workspace.directory(""))

    def test_unlink_of_a_directory_is_not_offered_as_a_delete(self) -> None:
        # os.unlink fails EISDIR on a directory, which must surface as a refusal
        # rather than being retried or reported as success.
        with self.assertRaises(PathEscape):
            self.workspace.unlink(self.workspace.resolve("src"))

    def test_open_fd_refuses_to_open_the_root_as_a_file(self) -> None:
        with self.assertRaises(PathEscape):
            self.workspace.open_fd(self.workspace.directory(""), os.O_RDONLY)

    def test_writes_created_here_are_private(self) -> None:
        target = self.workspace.resolve("fresh.txt")
        fd = self.workspace.open_fd(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.close(fd)
        mode = os.stat(os.path.join(self.root, "fresh.txt")).st_mode & 0o777
        self.assertFalse(mode & 0o077)


if __name__ == "__main__":
    unittest.main()
