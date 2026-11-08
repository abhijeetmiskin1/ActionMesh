"""Tests for scripts/snapshot.py.

The backup script is the only thing standing in for version control, so its
correctness matters more than its size suggests. These tests pin the two bugs
that were found in it:

1. the default target was written *inside* the tree (a filename beginning with
   ``..`` rather than a parent-directory path), so a lost tree lost its backup;
2. snapshots were not excluded from later snapshots, so each archive contained
   all previous ones and the size compounded — 16 archives reached 39.8 GB.
"""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "snapshot.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("am_snapshot", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise unittest.SkipTest("snapshot.py is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SCRIPT.exists():  # pragma: no cover - repository guard
            raise unittest.SkipTest("scripts/snapshot.py is absent")
        cls.mod = _load_module()

    # -- default location --------------------------------------------------
    def test_default_target_is_outside_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            target = self.mod.default_target(root)
            self.assertNotIn(root.resolve(), target.resolve().parents)
            self.assertEqual(target.parent.resolve(), root.parent.resolve())

    def test_default_target_name_has_no_leading_dots(self):
        # The original bug produced a file literally named "..actionmesh-...".
        with tempfile.TemporaryDirectory() as tmp:
            name = self.mod.default_target(pathlib.Path(tmp)).name
            self.assertFalse(name.startswith("."))
            self.assertTrue(name.startswith("actionmesh-snapshot-"))
            self.assertTrue(name.endswith(".zip"))

    def test_default_target_format_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = {self.mod.default_target(pathlib.Path(tmp)).name
                     for _ in range(2)}
            self.assertTrue(all(n.startswith("actionmesh-snapshot-")
                                for n in names))

    # -- exclusions --------------------------------------------------------
    def test_previous_snapshots_are_excluded(self):
        for name in ("actionmesh-snapshot-20260101T000000Z.zip",
                     "..actionmesh-snapshot-20260101T000000Z.zip"):
            self.assertTrue(self.mod.should_skip(pathlib.Path(name)))

    def test_secrets_and_caches_are_excluded(self):
        for name in (".env", "credentials.json", "__pycache__/x.pyc", "a.log"):
            self.assertTrue(self.mod.should_skip(pathlib.Path(name)))

    def test_ordinary_source_is_included(self):
        for name in ("README.md", "actionmesh/cli.py", "scripts/snapshot.py"):
            self.assertFalse(self.mod.should_skip(pathlib.Path(name)))

    # -- end to end --------------------------------------------------------
    def test_build_creates_a_readable_archive_of_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            (root / "pkg").mkdir(parents=True)
            (root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
            (root / "README.md").write_text("hi\n", encoding="utf-8")
            (root / "credentials.json").write_text("{}", encoding="utf-8")
            # a stale backup inside the tree, as the bug used to produce
            (root / "..actionmesh-snapshot-old.zip").write_bytes(b"junk")

            target = pathlib.Path(tmp) / "out.zip"
            count = self.mod.build(root, target)

            self.assertTrue(target.exists())
            with zipfile.ZipFile(target) as zf:
                names = set(zf.namelist())
            self.assertIn("README.md", names)
            self.assertIn("pkg/mod.py", names)
            self.assertNotIn("credentials.json", names)
            self.assertFalse(any("snapshot" in n for n in names))
            self.assertEqual(count, len(names))

    def test_build_does_not_include_the_target_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "a.txt").write_text("a\n", encoding="utf-8")
            target = root / "out.zip"
            self.mod.build(root, target)
            with zipfile.ZipFile(target) as zf:
                self.assertNotIn("out.zip", zf.namelist())

    def test_archive_size_does_not_compound_across_runs(self):
        # Two successive backups of a tree that already holds a backup must
        # stay comparable. Under the old behaviour the second contained the
        # first, and each run roughly doubled the archive.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "payload.txt").write_text("data " * 500, encoding="utf-8")

            first = pathlib.Path(tmp) / "one.zip"
            self.mod.build(root, first)
            # Drop a snapshot into the tree, as the old default did.
            (root / "..actionmesh-snapshot-stale.zip").write_bytes(
                first.read_bytes())

            second = pathlib.Path(tmp) / "two.zip"
            self.mod.build(root, second)
            self.assertLess(second.stat().st_size, first.stat().st_size * 2)

    def test_tree_is_detected_as_containing_an_inside_target(self):
        # The warning condition in main(): a target under the root.
        root = pathlib.Path("/repo")
        inside = root / "x.zip"
        self.assertIn(root.resolve(), inside.resolve().parents)

    def test_downloaded_toolchain_and_build_output_are_skipped(self):
        # android-toolchain/ holds a ~775 MiB JDK + SDK and build/ holds
        # setuptools output; neither belongs in a source backup.
        self.assertTrue(self.mod.should_skip(pathlib.Path("android-toolchain") / "jdk" / "bin" / "javac"))
        self.assertTrue(self.mod.should_skip(pathlib.Path("build") / "apk" / "classes.dex"))
        # but real sources are kept
        self.assertFalse(self.mod.should_skip(pathlib.Path("actionmesh") / "api.py"))
        self.assertFalse(self.mod.should_skip(pathlib.Path("android") / "java" / "MainActivity.java"))


if __name__ == "__main__":
    unittest.main()
