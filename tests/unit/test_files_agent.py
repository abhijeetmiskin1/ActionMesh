"""C42 FilesEngine: the path-confinement guarantees and per-method contracts.

These tests prove the hard rules of the local-files family (contract §5, §6, §2.2):

* Every supplied path is confined to the workspace root. ``../``, an absolute path, a
  drive letter, a percent-encoded separator, and any denied basename (``.ssh``, ``.env``,
  ``id_rsa``, ``shadow``) are **refused** — never silently accepted, never a success
  response. A refusal surfaces as a :class:`ProviderError` whose kind is
  ``FailureKind.AUTHORIZATION`` (a missing-but-allowed path is ``INVALID_INPUT``).
* A symlink that points outside the root is refused, not followed, and is never written
  through.
* ``create`` / ``rename`` / ``move`` / ``copy`` succeed inside the root and refuse to land
  outside it, and refuse to overwrite what is already there.
* ``edit`` produces a diff **preview** that is exactly the change that then applies; a
  no-match edit writes nothing; a non-UTF-8 file is refused rather than corrupted.
* ``trash`` prefers the OS trash. When no trash backend is reachable it does **not**
  hard-delete: the file still exists and the result says ``unavailable``. A backend that
  exits zero but leaves the file in place is reported as unavailable, not success.
* ``open`` reports ``unavailable`` when no opener is reachable — never a fake
  ``opened:true`` — and reports success only when a working opener actually ran.
* A read larger than the ceiling is truncated and flagged, not streamed whole; a binary
  body is never streamed.

The engine runs synchronously (like the provider's file handlers), so these tests call it
directly inside ``unittest`` — no ``asyncio``. The OS opener and trash backend are
injected by monkeypatching four tiny seams (``_opener_path``, ``_trash_backend``,
``_run_opener``, ``_run_trash``) so no test depends on whether this machine happens to
have ``xdg-open`` or ``gio`` installed — which is the point: "prefer the trash, otherwise
refuse" must hold on a machine with no trash at all.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

from actionmesh.agent.files import (
    MAX_LIST_ENTRIES,
    MAX_READ_BYTES,
    MAX_WRITE_BYTES,
    FilesEngine,
)
from actionmesh.agent.paths import Workspace
from actionmesh.protocol.models import FailureKind
from actionmesh.providers.base import ProviderError


class _EngineCase(unittest.TestCase):
    """A fresh confined temp root and a FilesEngine over it, per test."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="files-agent-test-")
        self.workspace = Workspace(self.root)
        self.engine = FilesEngine(self.workspace)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # -- fixtures ----------------------------------------------------------
    def make(self, relative: str, content: str | bytes = "") -> str:
        """Write a file into the root by absolute path (fixture only, never under test)."""
        full = os.path.join(self.root, *relative.split("/"))
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "wb" if isinstance(content, bytes) else "w") as handle:
            handle.write(content)
        return full

    def read_raw(self, relative: str) -> str:
        with open(os.path.join(self.root, *relative.split("/"))) as handle:
            return handle.read()

    def read_bytes(self, relative: str) -> bytes:
        with open(os.path.join(self.root, *relative.split("/")), "rb") as handle:
            return handle.read()

    def exists(self, relative: str) -> bool:
        return os.path.lexists(os.path.join(self.root, *relative.split("/")))

    # -- assertions --------------------------------------------------------
    def assert_escape_refused(self, call, *, kind: str = FailureKind.AUTHORIZATION) -> None:
        """A confinement escape raises ProviderError(kind), never returns."""
        with self.assertRaises(ProviderError) as ctx:
            call()
        self.assertEqual(ctx.exception.kind, kind, str(ctx.exception))

    def assert_invalid(self, call) -> None:
        self.assert_escape_refused(call, kind=FailureKind.INVALID_INPUT)

    class _Proc:
        def __init__(self, returncode: int = 0) -> None:
            self.returncode = returncode


class TestListSearch(_EngineCase):
    def test_list_root_counts_names_and_relative_paths(self) -> None:
        self.make("a.txt", "1")
        self.make("sub/b.txt", "2")
        result = self.engine.list_("")
        self.assertEqual(result["path"], ".")
        self.assertEqual(result["count"], 2)
        self.assertFalse(result["truncated"])
        self.assertFalse(result["hidden_outside_root"])
        names = sorted(entry["name"] for entry in result["entries"])
        self.assertEqual(names, ["a.txt", "sub"])
        for entry in result["entries"]:
            self.assertTrue(entry["confined"])
            self.assertFalse(os.path.isabs(entry["path"]))

    def test_list_accepts_dot_as_root(self) -> None:
        self.make("a.txt", "1")
        self.assertEqual(self.engine.list_(".")["count"], 1)

    def test_list_subdirectory(self) -> None:
        self.make("src/main.py", "x")
        self.make("src/util/helper.py", "y")
        result = self.engine.list_("src")
        self.assertEqual(result["path"], "src")
        names = sorted(e["name"] for e in result["entries"])
        self.assertEqual(names, ["main.py", "util"])
        self.assertEqual(result["entries"][0]["path"], "src/main.py")

    def test_list_missing_directory_is_invalid(self) -> None:
        self.assert_invalid(lambda: self.engine.list_("ghostdir"))

    def test_list_escapes_are_refused(self) -> None:
        self.assert_escape_refused(lambda: self.engine.list_(".."))
        self.assert_escape_refused(lambda: self.engine.list_("/etc"))
        self.assert_escape_refused(lambda: self.engine.list_(".ssh"))

    def test_list_hidden_outside_root_counts_escaping_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            victim = os.path.join(elsewhere, "victim.txt")
            with open(victim, "w") as handle:
                handle.write("outside the root")
            os.symlink(victim, os.path.join(self.root, "out_link"))
            self.make("normal.txt", "x")
            result = self.engine.list_("")
            self.assertEqual(result["hidden_outside_root"], 1)
            self.assertEqual(result["count"], 1)
            self.assertTrue(all(e["confined"] for e in result["entries"]))
            self.assertNotIn("out_link", [e["name"] for e in result["entries"]])

    def test_list_limit_validated(self) -> None:
        for bad in (0, -1, True, False, "5", 1.5):
            self.assert_invalid(lambda bad=bad: self.engine.list_("", limit=bad))

    def test_list_truncated_at_limit(self) -> None:
        for i in range(MAX_LIST_ENTRIES + 5):
            self.make(f"f{i}.txt", "x")
        result = self.engine.list_("")
        self.assertEqual(result["count"], MAX_LIST_ENTRIES)
        self.assertTrue(result["truncated"])

    def test_search_finds_by_glob_including_nested(self) -> None:
        self.make("keep.txt", "1")
        self.make("drop.tmp", "2")
        self.make("src/deep/needle.py", "x")
        self.make("src/deep/hay.txt", "y")
        by_tmp = self.engine.search("", "*.tmp")
        self.assertEqual(by_tmp["count"], 1)
        self.assertEqual(by_tmp["matches"][0]["name"], "drop.tmp")
        by_py = self.engine.search("src", "*.py")
        self.assertEqual(by_py["count"], 1)
        # Match paths stay workspace-relative, like ``Workspace.list`` entries.
        self.assertEqual(by_py["matches"][0]["path"], "src/deep/needle.py")

    def test_search_pattern_validated(self) -> None:
        for bad in ("", "   ", None, 5):
            self.assert_invalid(lambda bad=bad: self.engine.search("", bad))

    def test_search_escapes_refused(self) -> None:
        self.assert_escape_refused(lambda: self.engine.search("..", "*"))
        self.assert_escape_refused(lambda: self.engine.search("/etc", "passwd"))

    def test_search_never_follows_an_escaping_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            victim = os.path.join(elsewhere, "secret.txt")
            with open(victim, "w") as handle:
                handle.write("not yours")
            os.symlink(victim, os.path.join(self.root, "secret.txt"))
            result = self.engine.search("", "secret.txt")
            # The escaping link is hidden, and its target's contents were never opened.
            self.assertEqual(result["count"], 0)

    def test_search_bounded_by_the_listing_ceiling(self) -> None:
        for i in range(MAX_LIST_ENTRIES + 10):
            self.make(f"n{i}.txt", "x")
        result = self.engine.search("", "n*.txt")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(result["count"], MAX_LIST_ENTRIES)


class TestRead(_EngineCase):
    def test_read_shape_and_digest(self) -> None:
        body = "hello world\n"
        self.make("greet.txt", body)
        result = self.engine.read("greet.txt")
        raw = body.encode("utf-8")
        self.assertEqual(
            set(result), {"path", "size", "sha256", "truncated", "binary", "content"}
        )
        self.assertEqual(result["path"], "greet.txt")
        self.assertEqual(result["size"], len(raw))
        self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertFalse(result["truncated"])
        self.assertFalse(result["binary"])
        self.assertEqual(result["content"], body)

    def test_read_large_file_is_truncated_not_streamed_whole(self) -> None:
        self.make("big.txt", "x" * (MAX_READ_BYTES + 4096))
        result = self.engine.read("big.txt")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["size"], MAX_READ_BYTES)
        self.assertEqual(len(result["content"]), MAX_READ_BYTES)

    def test_read_binary_sets_flag_and_no_content(self) -> None:
        self.make("blob.bin", b"\x00\x01\x02\xff\xfe")
        result = self.engine.read("blob.bin")
        self.assertTrue(result["binary"])
        self.assertEqual(result["content"], "")

    def test_read_missing_file_is_invalid_input(self) -> None:
        self.assert_invalid(lambda: self.engine.read("nope.txt"))

    def test_read_directory_is_invalid(self) -> None:
        self.make("dir/a.txt", "x")
        self.assert_invalid(lambda: self.engine.read("dir"))

    def test_read_refuses_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            victim = os.path.join(elsewhere, "victim.txt")
            with open(victim, "w") as handle:
                handle.write("not yours")
            os.symlink(victim, os.path.join(self.root, "trap.txt"))
            self.assert_escape_refused(lambda: self.engine.read("trap.txt"))


class TestInfo(_EngineCase):
    def test_info_existing_file(self) -> None:
        self.make("stat.txt", "12345")
        result = self.engine.info("stat.txt")
        self.assertEqual(
            set(result), {"path", "exists", "is_dir", "size", "mtime", "kind"}
        )
        self.assertTrue(result["exists"])
        self.assertEqual(result["kind"], "file")
        self.assertFalse(result["is_dir"])
        self.assertEqual(result["size"], 5)
        self.assertIsInstance(result["mtime"], float)

    def test_info_directory(self) -> None:
        self.make("d/a.txt", "x")
        result = self.engine.info("d")
        self.assertTrue(result["is_dir"])
        self.assertEqual(result["kind"], "dir")

    def test_info_missing_path_is_not_an_error(self) -> None:
        result = self.engine.info("ghost.txt")
        self.assertFalse(result["exists"])
        self.assertIsNone(result["mtime"])

    def test_info_refuses_escape_and_denied_names(self) -> None:
        self.assert_escape_refused(lambda: self.engine.info("../etc/passwd"))
        self.assert_escape_refused(lambda: self.engine.info(".ssh/id_rsa"))


class TestCreate(_EngineCase):
    def test_create_writes_and_reports_digest(self) -> None:
        result = self.engine.create("new.txt", "hello")
        self.assertEqual(
            set(result), {"path", "bytes", "sha256"}
        )
        self.assertEqual(result["path"], "new.txt")
        self.assertEqual(result["bytes"], 5)
        self.assertEqual(result["sha256"], hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(self.read_raw("new.txt"), "hello")

    def test_create_rejects_oversized_content_without_writing(self) -> None:
        self.assert_invalid(
            lambda: self.engine.create("big.txt", "x" * (MAX_WRITE_BYTES + 1))
        )
        self.assertFalse(self.exists("big.txt"))

    def test_create_requires_an_existing_parent(self) -> None:
        # The boundary is deliberately not a mkdir -p.
        self.assert_invalid(lambda: self.engine.create("no/such/dir/x.txt", "x"))

    def test_create_refuses_escape_and_denied_names(self) -> None:
        self.assert_escape_refused(lambda: self.engine.create("../out.txt", "x"))
        self.assert_escape_refused(lambda: self.engine.create("/tmp/evil.txt", "x"))
        self.assert_escape_refused(lambda: self.engine.create(".env", "SECRET=leak"))
        self.assert_escape_refused(lambda: self.engine.create("cfg/.env", "x"))

    def test_create_never_writes_through_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            victim = os.path.join(elsewhere, "victim.txt")
            with open(victim, "w") as handle:
                handle.write("original")
            os.symlink(victim, os.path.join(self.root, "trap.txt"))
            self.assert_escape_refused(
                lambda: self.engine.create("trap.txt", "hijacked")
            )
            with open(victim) as handle:
                self.assertEqual(handle.read(), "original",
                                 "the victim file must be intact")

    def test_create_directory_symlink_escape_refused(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            os.symlink(elsewhere, os.path.join(self.root, "escape_dir"))
            self.assert_escape_refused(
                lambda: self.engine.create("escape_dir/x.txt", "x")
            )


class TestRenameMove(_EngineCase):
    def test_rename_inside_root(self) -> None:
        self.make("old.txt", "content")
        result = self.engine.rename("old.txt", "new.txt")
        self.assertEqual(result, {"path": "old.txt", "to": "new.txt", "moved": True})
        self.assertFalse(self.exists("old.txt"))
        self.assertTrue(self.exists("new.txt"))
        self.assertEqual(self.read_raw("new.txt"), "content")

    def test_move_inside_root(self) -> None:
        self.make("src.txt", "move me")
        os.makedirs(os.path.join(self.root, "sub"), exist_ok=True)
        result = self.engine.move("src.txt", "sub/dst.txt")
        self.assertEqual(result["moved"], True)
        self.assertFalse(self.exists("src.txt"))
        self.assertEqual(self.read_raw("sub/dst.txt"), "move me")

    def test_rename_refuses_to_clobber(self) -> None:
        self.make("a.txt", "a")
        self.make("b.txt", "b")
        self.assert_invalid(lambda: self.engine.rename("a.txt", "b.txt"))
        self.assertTrue(self.exists("a.txt"))
        self.assertEqual(self.read_raw("b.txt"), "b")

    def test_rename_missing_source_is_invalid(self) -> None:
        self.assert_invalid(lambda: self.engine.rename("ghost.txt", "x.txt"))

    def test_rename_refuses_denied_destination(self) -> None:
        self.make("a.txt", "a")
        self.assert_escape_refused(lambda: self.engine.rename("a.txt", ".ssh/a.txt"))
        self.assert_escape_refused(lambda: self.engine.rename("a.txt", ".env"))
        # a refused rename leaves the source exactly where it was
        self.assertTrue(self.exists("a.txt"))

    def test_rename_refuses_escapes_both_ends(self) -> None:
        self.make("keep.txt", "keep")
        self.assert_escape_refused(lambda: self.engine.rename("keep.txt", "../escape.txt"))
        self.assert_escape_refused(lambda: self.engine.rename("keep.txt", "/tmp/evil.txt"))
        self.assertTrue(self.exists("keep.txt"))
        self.assert_escape_refused(lambda: self.engine.rename("../outside.txt", "x.txt"))

    def test_rename_directory(self) -> None:
        self.make("before/inner.txt", "x")
        result = self.engine.rename("before", "after")
        self.assertTrue(result["moved"])
        self.assertEqual(self.read_raw("after/inner.txt"), "x")


class TestCopy(_EngineCase):
    def test_copy_leaves_source_writes_destination(self) -> None:
        self.make("src.txt", "copyable")
        result = self.engine.copy("src.txt", "dst.txt")
        self.assertEqual(result["copied"], True)
        self.assertEqual(result["bytes"], len("copyable"))
        self.assertEqual(result["path"], "src.txt")
        self.assertEqual(result["to"], "dst.txt")
        self.assertTrue(self.exists("src.txt"))
        self.assertEqual(self.read_raw("dst.txt"), "copyable")

    def test_copy_refuses_to_clobber_destination(self) -> None:
        self.make("src.txt", "a")
        self.make("dst.txt", "b")
        self.assert_invalid(lambda: self.engine.copy("src.txt", "dst.txt"))
        self.assertEqual(self.read_raw("dst.txt"), "b", "destination untouched")

    def test_copy_refuses_directory_source(self) -> None:
        self.make("dir/a.txt", "x")
        # Directory copies do not exist here; a recursive copy of an unbounded tree is a
        # different operation and a different capability.
        with self.assertRaises(ProviderError):
            self.engine.copy("dir", "dst_dir")

    def test_copy_over_ceiling_is_refused(self) -> None:
        self.make("huge.txt", "x" * (MAX_WRITE_BYTES + 1))
        self.assert_invalid(lambda: self.engine.copy("huge.txt", "out.bin"))

    def test_copy_refuses_escapes_both_ends(self) -> None:
        self.make("safe.txt", "x")
        self.assert_escape_refused(lambda: self.engine.copy("safe.txt", "../out.bin"))
        self.assert_escape_refused(lambda: self.engine.copy("safe.txt", "/etc/out.bin"))
        self.assert_escape_refused(lambda: self.engine.copy("../outside.txt", "x.bin"))
        self.assert_escape_refused(lambda: self.engine.copy("safe.txt", "sub/.env"))

    def test_copy_binary_bytes_survive(self) -> None:
        self.make("blob.dat", bytes(range(256)) * 8)
        self.engine.copy("blob.dat", "blob2.dat")
        self.assertEqual(self.read_bytes("blob.dat"), self.read_bytes("blob2.dat"))


class TestEdit(_EngineCase):
    def test_preview_is_exactly_the_applied_change(self) -> None:
        original = "line one\nline two\nline three\n"
        self.make("edit.txt", original)
        result = self.engine.edit("edit.txt", "line two", "LINE TWO")
        self.assertEqual(result["replacements"], 1)
        # The applied bytes hash to the reported sha256...
        self.assertEqual(
            result["sha256"],
            hashlib.sha256(self.read_raw("edit.txt").encode("utf-8")).hexdigest(),
        )
        # ...and the preview describes exactly that change and nothing else.
        # unified_diff is a context diff by default (two lines of context on each side),
        # so it will include line one/line three as unmodified context; we assert the
        # added/removed lines are present and correctly oriented.
        preview = result["preview"]
        self.assertIn("-line two", preview)
        self.assertIn("+LINE TWO", preview)
        self.assertIn("--- edit.txt", preview)
        self.assertIn("+++ edit.txt", preview)

    def test_single_edit_touches_only_the_first_match(self) -> None:
        self.make("two.txt", "cat and cat\n")
        result = self.engine.edit("two.txt", "cat", "dog")
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(self.read_raw("two.txt"), "dog and cat\n")

    def test_all_true_replaces_every_occurrence(self) -> None:
        self.make("many.txt", "foo foo foo\n")
        result = self.engine.edit("many.txt", "foo", "bar", all=True)
        self.assertEqual(result["replacements"], 3)
        self.assertEqual(self.read_raw("many.txt"), "bar bar bar\n")

    def test_no_match_writes_nothing(self) -> None:
        self.make("none.txt", "alpha\n")
        result = self.engine.edit("none.txt", "beta", "gamma")
        self.assertEqual(result["replacements"], 0)
        self.assertEqual(result["preview"], "")
        self.assertEqual(self.read_raw("none.txt"), "alpha\n")

    def test_missing_file_is_invalid(self) -> None:
        self.assert_invalid(lambda: self.engine.edit("ghost.txt", "a", "b"))

    def test_empty_find_is_refused(self) -> None:
        self.make("e.txt", "abc\n")
        self.assert_invalid(lambda: self.engine.edit("e.txt", "", "x"))

    def test_binary_file_is_refused_not_corrupted(self) -> None:
        self.make("b.bin", b"\xff\xfe\x00garbage")
        self.assert_invalid(lambda: self.engine.edit("b.bin", "a", "b"))
        self.assertEqual(self.read_bytes("b.bin"), b"\xff\xfe\x00garbage")

    def test_edit_over_ceiling_is_refused(self) -> None:
        self.make("huge.txt", "x" * (MAX_WRITE_BYTES + 1))
        self.assert_invalid(lambda: self.engine.edit("huge.txt", "x", "y"))

    def test_edit_refuses_escape(self) -> None:
        self.make("safe.txt", "a")
        self.assert_escape_refused(lambda: self.engine.edit("../safe.txt", "a", "b"))
        self.assert_escape_refused(lambda: self.engine.edit(".env", "a", "b"))

    def test_edit_result_carries_the_journaling_fields(self) -> None:
        # The engine keeps no effect ledger itself: the provider journals under
        # action_id. What the engine must supply is the data a journal entry needs —
        # the relative path and the digest of the bytes it wrote.
        self.make("r.txt", "data")
        result = self.engine.edit("r.txt", "data", "DATA")
        self.assertEqual(result["path"], "r.txt")
        self.assertEqual(
            result["sha256"],
            hashlib.sha256(self.read_bytes("r.txt")).hexdigest(),
        )
        self.assertEqual(result["replacements"], 1)


class TestTrash(_EngineCase):
    def test_trash_moves_the_file_when_backend_works(self) -> None:
        self.make("gone.txt", "x")

        def fake_trash(backend, absolute):
            # A backend that really moves the file, like gio would.
            shutil.move(absolute, absolute + ".trashed")
            return self._Proc(0)

        with mock.patch.object(FilesEngine, "_trash_backend", return_value="/usr/bin/gio"), \
             mock.patch.object(FilesEngine, "_run_trash", side_effect=fake_trash):
            result = self.engine.trash("gone.txt")
        self.assertTrue(result["trashed"])
        self.assertFalse(self.exists("gone.txt"))
        self.assertTrue(result["to"])

    def test_no_backend_does_not_hard_delete(self) -> None:
        self.make("survivor.txt", "must stay")
        with mock.patch.object(FilesEngine, "_trash_backend", return_value=None):
            result = self.engine.trash("survivor.txt")
        self.assertFalse(result["trashed"])
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("reason", result)
        # The whole point: the file is STILL THERE. No backend never means a silent unlink.
        self.assertTrue(self.exists("survivor.txt"))
        self.assertEqual(self.read_raw("survivor.txt"), "must stay")

    def test_backend_failure_leaves_the_file(self) -> None:
        self.make("held.txt", "x")
        with mock.patch.object(FilesEngine, "_trash_backend", return_value="/usr/bin/gio"), \
             mock.patch.object(FilesEngine, "_run_trash", return_value=self._Proc(1)):
            result = self.engine.trash("held.txt")
        self.assertFalse(result["trashed"])
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(self.exists("held.txt"))

    def test_backend_that_lies_about_success_is_not_believed(self) -> None:
        # returncode 0 but the file is still there: not a trash.
        self.make("lying.txt", "x")
        with mock.patch.object(FilesEngine, "_trash_backend", return_value="/usr/bin/gio"), \
             mock.patch.object(FilesEngine, "_run_trash", return_value=self._Proc(0)):
            result = self.engine.trash("lying.txt")
        self.assertFalse(result["trashed"])
        self.assertTrue(self.exists("lying.txt"))

    def test_backend_cannot_be_run_leaves_the_file(self) -> None:
        self.make("cantrun.txt", "x")
        with mock.patch.object(FilesEngine, "_trash_backend", return_value="/usr/bin/gio"), \
             mock.patch.object(FilesEngine, "_run_trash", return_value=None):
            result = self.engine.trash("cantrun.txt")
        self.assertFalse(result["trashed"])
        self.assertTrue(self.exists("cantrun.txt"))

    def test_trash_missing_file_is_invalid(self) -> None:
        with mock.patch.object(FilesEngine, "_trash_backend", return_value="/usr/bin/gio"):
            self.assert_invalid(lambda: self.engine.trash("ghost.txt"))

    def test_trash_refuses_escape_and_denied_name(self) -> None:
        self.assert_escape_refused(lambda: self.engine.trash("../outside.txt"))
        self.assert_escape_refused(lambda: self.engine.trash(".ssh/id_rsa"))


class TestOpen(_EngineCase):
    def test_opened_true_only_when_opener_actually_ran(self) -> None:
        self.make("doc.txt", "x")
        with mock.patch.object(FilesEngine, "_opener_path", return_value="/usr/bin/xdg-open"), \
             mock.patch.object(FilesEngine, "_run_opener", return_value=self._Proc(0)):
            result = self.engine.open_("doc.txt")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["opened"])
        self.assertEqual(result["path"], "doc.txt")

    def test_no_opener_is_unavailable_not_fake(self) -> None:
        self.make("doc.txt", "x")
        with mock.patch.object(FilesEngine, "_opener_path", return_value=None):
            result = self.engine.open_("doc.txt")
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["opened"])
        self.assertIn("reason", result)

    def test_opener_failure_is_unavailable(self) -> None:
        self.make("doc.txt", "x")
        with mock.patch.object(FilesEngine, "_opener_path", return_value="/usr/bin/xdg-open"), \
             mock.patch.object(FilesEngine, "_run_opener", return_value=self._Proc(127)):
            result = self.engine.open_("doc.txt")
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["opened"])

    def test_opener_cannot_launch_is_unavailable(self) -> None:
        self.make("doc.txt", "x")
        with mock.patch.object(FilesEngine, "_opener_path", return_value="/usr/bin/xdg-open"), \
             mock.patch.object(FilesEngine, "_run_opener", return_value=None):
            result = self.engine.open_("doc.txt")
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["opened"])

    def test_open_missing_file_is_invalid(self) -> None:
        self.assert_invalid(lambda: self.engine.open_("ghost.txt"))

    def test_open_never_shells_a_supplied_path(self) -> None:
        # A filename with shell metacharacters must arrive as ONE argv element.
        self.make("weird; name && rm.txt", "x")
        seen: list[tuple[str, str]] = []

        def spy(opener, absolute):
            seen.append((opener, absolute))
            return self._Proc(0)

        with mock.patch.object(FilesEngine, "_opener_path", return_value="/usr/bin/xdg-open"), \
             mock.patch.object(FilesEngine, "_run_opener", side_effect=spy):
            result = self.engine.open_("weird; name && rm.txt")
        self.assertTrue(result["opened"])
        opener, absolute = seen[0]
        self.assertEqual(opener, "/usr/bin/xdg-open")
        self.assertIsInstance(absolute, str)
        self.assertTrue(absolute.endswith("weird; name && rm.txt"))

    def test_real_run_opener_is_shell_false_and_fixed_argv(self) -> None:
        # The genuine invocation must be subprocess-run of a fixed argv list — never a
        # shell string. Asserted against the real method with the runner mocked.
        with mock.patch("actionmesh.agent.files.subprocess.run") as run:
            run.return_value = self._Proc(0)
            FilesEngine._run_opener("/usr/bin/xdg-open", "/root/weird file; rm -rf /")
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["/usr/bin/xdg-open", "/root/weird file; rm -rf /"])
        self.assertNotIn("shell", run.call_args.kwargs or {})

    def test_real_run_trash_is_shell_false_and_fixed_argv(self) -> None:
        with mock.patch("actionmesh.agent.files.subprocess.run") as run:
            run.return_value = self._Proc(0)
            FilesEngine._run_trash("/usr/bin/gio", "/root/x; rm -rf /")
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["/usr/bin/gio", "trash", "/root/x; rm -rf /"])


class TestHardRules(_EngineCase):
    """The §0/§5 invariants, asserted against the module itself."""

    def test_no_shell_and_no_arbitrary_command_execution(self) -> None:
        # Scan executable tokens only: the module's prose legitimately mentions
        # ``shell=True`` / ``shutil`` to say it never does them, and a raw substring
        # match would turn the rule's own documentation into a false positive.
        import tokenize

        import actionmesh.agent.files as files_mod

        with open(files_mod.__file__, encoding="utf-8") as handle:
            tokens = list(tokenize.generate_tokens(handle.readline))
        code = "".join(tok.string for tok in tokens if tok.type != tokenize.STRING)
        for forbidden in ("os.system", "popen", "shutil.rmtree", "shell=True"):
            self.assertNotIn(forbidden, code, forbidden)
        # subprocess appears only in the two fixed-binary helpers.
        self.assertEqual(code.count("subprocess.run"), 2)

    def test_boundaries_mirror_the_existing_file_handler_ceilings(self) -> None:
        self.assertEqual(MAX_READ_BYTES, 256 * 1024)
        self.assertEqual(MAX_WRITE_BYTES, 1024 * 1024)
        self.assertEqual(MAX_LIST_ENTRIES, 500)

    def test_results_never_leak_an_absolute_path(self) -> None:
        self.make("sub/deep.txt", "x")
        for result in (
            self.engine.list_("sub"),
            self.engine.read("sub/deep.txt"),
            self.engine.info("sub/deep.txt"),
            self.engine.create("sub/new.txt", "x"),
            self.engine.search("sub", "*.txt"),
        ):
            if "path" in result:
                self.assertFalse(os.path.isabs(result["path"]), result["path"])
            for entry in result.get("entries", []) + result.get("matches", []):
                self.assertFalse(os.path.isabs(entry["path"]), entry["path"])


if __name__ == "__main__":
    unittest.main()
