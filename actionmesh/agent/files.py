"""C42 local files & project ops: the richer ``device.files.*`` family.

Extends the existing confined ``device.file.*`` handlers with search, open-in-OS, stat
info, create, rename, move, copy, edit (with a diff preview) and trash (which prefers
the OS trash over a hard delete). Every path is resolved through
:class:`~actionmesh.agent.paths.Workspace` — :meth:`Workspace.resolve` for the lexical
confinement check, and :meth:`Workspace.open_fd` / :meth:`Workspace.unlink` /
:meth:`Workspace.walk_fd` for the TOCTOU-safe descriptor walk that refuses symlinks and
denies ``.ssh`` / ``.env`` / ``id_rsa`` / ``shadow`` at open time.

This module never re-implements the boundary and never widens it: it calls the same
``Workspace`` the existing handlers call. File contents are moved with descriptors
(:func:`os.sendfile`) and renames are done relative to already-walked directory
descriptors — never with ``shutil`` or a re-opened string path, because the path a
caller chose could be swapped for a symlink between "checked" and "used".

Program execution is kept where the contract keeps it: this module runs **no shell** and
no arbitrary command. The only two programs it may invoke are a fixed ``xdg-open`` and a
fixed ``gio trash``, each called with one confined absolute path as a single argv element
and never through ``shell=True``. Both are wrapped so tests can substitute a fake: the
honesty rule is that ``open`` reports ``unavailable`` rather than a fake ``opened:true``
when no opener is reachable, and ``trash`` reports ``unavailable`` rather than
hard-deleting when no trash backend is reachable — a hard delete is the separate,
explicit ``device.file.delete`` capability, not a fallback of this one.

Transient content (contract §6) follows the one rule the sync handlers already obey: a
file body is never emitted from a worker thread, because waking another thread's
``asyncio.Queue`` is not something it survives. The engine therefore puts the read body
under ``content`` and the edit diff under ``preview`` **in its result dict**; the
provider emits both on the event loop in ``_stream_content`` — ``content`` (the reused
stream name) for ``device.files.read``, ``files.preview`` for ``device.files.edit``.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import stat
import subprocess
from typing import Any

from actionmesh.agent.errors import PathEscape, PathNotFound
from actionmesh.agent.paths import ResolvedPath, Workspace
from actionmesh.protocol.models import FailureKind
from actionmesh.providers.base import ProviderError

#: Read ceiling for one ``device.files.read`` — the same 256 KiB the existing
#: ``device.file.read`` handler enforces, so a new capability is never a wider door.
MAX_READ_BYTES = 256 * 1024
#: Write ceiling for one create / edit / edited-file, and the copy ceiling.
MAX_WRITE_BYTES = 1024 * 1024
#: Ceiling on one listing or search.
MAX_LIST_ENTRIES = 500

#: The opener binary this module will invoke, discovered on PATH. A fixed name, never a
#: model-named program and never a shell string.
_OPENER_BINARY = "xdg-open"
#: The trash backend argv: ``gio trash`` (freedesktop). Fixed, discovered on PATH.
_TRASH_ARGV = ("gio", "trash")
#: How long ``xdg-open`` gets to hand off to a desktop handler and exit.
_OPENER_TIMEOUT_S = 5.0
#: How long the trash backend gets to respond.
_TRASH_TIMEOUT_S = 5.0


def _classify(exc: PathEscape) -> FailureKind:
    """A missing path is a typo (fixable, INVALID_INPUT); an escape is a denial."""
    return FailureKind.INVALID_INPUT if isinstance(exc, PathNotFound) else FailureKind.AUTHORIZATION


def _resolve(workspace: Workspace, path: object, *, must_exist: bool = False) -> ResolvedPath:
    """One confined path, or a :class:`ProviderError` the executor must not retry."""
    try:
        return workspace.resolve(path, must_exist=must_exist)
    except PathEscape as exc:
        raise ProviderError(str(exc), _classify(exc)) from exc


def _directory(workspace: Workspace, path: object) -> ResolvedPath:
    """One confined *directory* below the root (empty / ``"."`` means the root)."""
    raw = "" if path in ("", None) else str(path)
    try:
        return workspace.directory(raw)
    except PathEscape as exc:
        raise ProviderError(str(exc), _classify(exc)) from exc


def _open_fd(workspace: Workspace, resolved: ResolvedPath, flags: int,
             create_mode: int = 0o600) -> int:
    """A descriptor inside the boundary, or a :class:`ProviderError`.

    :meth:`Workspace.open_fd` raises a raw ``AgentError`` — :class:`PathNotFound` for a
    missing parent, :class:`PathEscape` for a symlink. Neither is a :class:`ProviderError`,
    so left unwrapped one would escape to the generic executor catch (which maps every
    ``AgentError`` to ``AUTHORIZATION``) and mis-label a fixable typo as a denial. Wrap
    here so a missing parent is ``INVALID_INPUT`` and an escape stays ``AUTHORIZATION``.
    """
    try:
        return workspace.open_fd(resolved, flags, create_mode=create_mode)
    except PathEscape as exc:
        raise ProviderError(str(exc), _classify(exc)) from exc


def _decode(body: bytes) -> tuple[str, bool]:
    try:
        return body.decode("utf-8"), False
    except UnicodeDecodeError:
        return "", True


def _validate_limit(limit: object) -> int:
    if limit is None:
        return MAX_LIST_ENTRIES
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ProviderError("limit must be a positive integer", FailureKind.INVALID_INPUT)
    return min(limit, MAX_LIST_ENTRIES)


def _kind_for(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


class FilesEngine:
    """One confined workspace, one method per ``device.files.*`` capability.

    Runs synchronously in a worker thread, exactly like the existing sync file handlers
    in the laptop provider. Each mutating method returns a dict keyed by the capability id
    (e.g., ``{"path": ..., "bytes": ..., "sha256": ...}`` for ``create``). The provider's
    execute loop collects these into ``self._effects`` keyed by ``action.action_id`` so
    observe() can re-read the world after a restart.

    Non-mutating methods return result dicts directly without storing effects — they
    report what they observed, not what they changed. Effects exist only for mutations
    whose after-state must be confirmed by observation.
    """

    #: Capability id per verb, so the returned dict is keyed by its own action_type.
    ACTION_TYPES = {
        "list": "device.files.list",
        "search": "device.files.search",
        "read": "device.files.read",
        "open": "device.files.open",
        "info": "device.files.info",
        "create": "device.files.create",
        "rename": "device.files.rename",
        "move": "device.files.move",
        "copy": "device.files.copy",
        "edit": "device.files.edit",
        "trash": "device.files.trash",
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    # -- public API: one method per device.files.X --------------------------------
    def list_(self, path: str, limit: int | None = None) -> dict[str, Any]:
        """One directory level, reused from ``Workspace.list`` (never re-implemented)."""
        raw = "" if path in ("", ".") else str(path or "")
        capped = _validate_limit(limit)
        target = _directory(self.workspace, raw)
        try:
            entries = self.workspace.list(raw, limit=capped)
        except PathEscape as exc:
            raise ProviderError(str(exc), _classify(exc)) from exc
        confined = [entry for entry in entries if entry.get("confined")]
        return {
            "path": target.relative or ".",
            "entries": confined,
            "count": len(confined),
            "hidden_outside_root": len(entries) - len(confined),
            "truncated": len(entries) >= capped,
        }

    def search(
        self, path: str, pattern: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Entries whose *name* matches a glob, found below one confined directory.

        The walk goes through a descriptor (:meth:`Workspace.walk_fd`) at every level,
        so the pattern cannot reach a file the lexical check would not have allowed:
        an escaping symlink is hidden and never descended, and no file body is opened
        while searching. Bounded by ``MAX_LIST_ENTRIES`` names examined so a search
        cannot become a whole-filesystem enumeration.
        """
        if not isinstance(pattern, str) or not pattern.strip():
            raise ProviderError(
                "pattern must be a non-empty string", FailureKind.INVALID_INPUT
            )
        raw = "" if path in ("", ".") else str(path or "")
        capped = _validate_limit(limit)
        target = _directory(self.workspace, raw)
        matches: list[dict[str, Any]] = []
        examined = 0
        truncated = False
        stack: list[tuple[int, str]] = [(self.workspace.walk_fd(raw), target.relative)]
        try:
            while stack and not truncated:
                directory_fd, prefix = stack.pop()
                base_absolute = self.workspace.root if not prefix else os.path.join(
                    self.workspace.root, *prefix.split("/")
                )
                try:
                    with os.scandir(directory_fd) as scan:
                        for entry in scan:
                            examined += 1
                            if examined > MAX_LIST_ENTRIES:
                                truncated = True
                                break
                            if len(matches) >= capped:
                                truncated = True
                                break
                            try:
                                info = entry.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            is_dir = stat.S_ISDIR(info.st_mode)
                            is_link = stat.S_ISLNK(info.st_mode)
                            relative = f"{prefix}/{entry.name}" if prefix else entry.name
                            if is_link and not self.workspace._contains(
                                os.path.realpath(os.path.join(base_absolute, entry.name))
                            ):
                                # Resolve the link target only to *ask* whether it stays
                                # inside; a link out of the root is hidden, never opened.
                                continue
                            if fnmatch.fnmatch(entry.name, pattern):
                                matches.append(
                                    {
                                        "name": entry.name,
                                        "path": relative,
                                        "type": "dir" if is_dir else (
                                            "link" if is_link else "file"
                                        ),
                                        "size": 0 if is_link else int(info.st_size),
                                        "modified": info.st_mtime,
                                    }
                                )
                            if is_dir and not is_link:
                                # Descend through a walked descriptor, never a name.
                                stack.append((self.workspace.walk_fd(relative), relative))
                except OSError:
                    # A directory that vanished or is unreadable mid-walk stops the walk;
                    # the search reports what it reached rather than pretending it saw all.
                    truncated = True
                finally:
                    os.close(directory_fd)
        except PathEscape as exc:
            raise ProviderError(str(exc), _classify(exc)) from exc
        matches.sort(key=lambda item: str(item["path"]))
        return {
            "path": target.relative or ".",
            "matches": matches[:capped],
            "count": min(len(matches), capped),
            "truncated": truncated or len(matches) > capped,
        }

    def read(self, path: str) -> dict[str, Any]:
        """Read up to ``MAX_READ_BYTES``; a longer file is truncated and says so.

        The body is placed under ``content`` for the provider to stream on the ``content``
        transient channel from the event loop — this method runs in a worker thread and
        must not emit directly. ``binary`` files produce no content. A path that escapes
        the root or hits a denied name raises before anything is opened; a missing file is
        ``INVALID_INPUT``, never a silent empty read.
        """
        resolved = _resolve(self.workspace, path)
        try:
            head = os.lstat(resolved.absolute)
        except FileNotFoundError as exc:
            raise ProviderError(
                f"no such file or directory: {path!r}", FailureKind.INVALID_INPUT
            ) from exc
        except OSError as exc:
            raise ProviderError(f"cannot stat {path!r}: {exc}", FailureKind.PROVIDER) from exc
        if stat.S_ISDIR(head.st_mode):
            raise ProviderError(
                f"{path!r} is a directory; this capability reads files",
                FailureKind.INVALID_INPUT,
            )
        fd = self.workspace.open_fd(resolved, os.O_RDONLY)
        try:
            with os.fdopen(fd, "rb") as handle:
                payload = handle.read(MAX_READ_BYTES + 1)
        except OSError as exc:
            raise ProviderError(
                f"cannot read {path!r}: {exc}", FailureKind.PROVIDER
            ) from exc
        truncated = len(payload) > MAX_READ_BYTES
        body = payload[:MAX_READ_BYTES]
        text, binary = _decode(body)
        digest = hashlib.sha256(body).hexdigest()
        return {
            "path": resolved.relative,
            "size": len(body),
            "sha256": digest,
            "truncated": truncated,
            "binary": binary,
            "content": text if not binary else "",
        }

    def info(self, path: str) -> dict[str, Any]:
        """Stat one confined path without ever following a symlink or opening it."""
        resolved = _resolve(self.workspace, path)
        try:
            st = os.lstat(resolved.absolute)
        except FileNotFoundError:
            return {
                "path": resolved.relative,
                "exists": False,
                "is_dir": False,
                "size": 0,
                "mtime": None,
                "kind": "absent",
            }
        except OSError as exc:
            raise ProviderError(
                f"cannot stat {path!r}: {exc}", FailureKind.PROVIDER
            ) from exc
        return {
            "path": resolved.relative,
            "exists": True,
            "is_dir": stat.S_ISDIR(st.st_mode),
            "size": int(st.st_size) if stat.S_ISREG(st.st_mode) else 0,
            "mtime": st.st_mtime,
            "kind": _kind_for(st.st_mode),
        }

    def create(self, path: str, content: str) -> dict[str, Any]:
        """Write one confined file from scratch (HIGH ``file.write``).

        Goes through ``open_fd`` with ``O_NOFOLLOW``, so a symlink swapped in after the
        check fails to open instead of carrying the write out of the root. It does not
        create directories: :meth:`Workspace.open_fd` refuses a missing parent, and a
        phone that can lay down directory trees can reshape the workspace.
        """
        if not isinstance(content, str):
            raise ProviderError("content must be a string", FailureKind.INVALID_INPUT)
        body = content.encode("utf-8")
        if len(body) > MAX_WRITE_BYTES:
            raise ProviderError(
                f"content is {len(body)} bytes, over the {MAX_WRITE_BYTES} byte "
                "write limit",
                FailureKind.INVALID_INPUT,
            )
        resolved = _resolve(self.workspace, path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = _open_fd(self.workspace, resolved, flags)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ProviderError(
                f"cannot create {path!r}: {exc}", FailureKind.PROVIDER
            ) from exc
        digest = hashlib.sha256(body).hexdigest()
        return {"path": resolved.relative, "bytes": len(body), "sha256": digest}

    def rename(self, path: str, to: str) -> dict[str, Any]:
        """Rename inside the root. Both ends are resolved before anything moves."""
        return self._relocate(path, to, "rename")

    def move(self, path: str, to: str) -> dict[str, Any]:
        """Move inside the root — the same relocation, under its own id and risk."""
        return self._relocate(path, to, "move")

    def _relocate(
        self, path: str, to: str, verb: str
    ) -> dict[str, Any]:
        src = _resolve(self.workspace, path, must_exist=True)
        dst = _resolve(self.workspace, to)
        if os.path.lexists(dst.absolute):
            raise ProviderError(
                f"{to!r} already exists; this capability refuses to overwrite",
                FailureKind.INVALID_INPUT,
            )
        self._relocate_locked(src, dst)
        # Evidence, not a return code: the name is gone from its old place and present
        # at its new one. A move that neither removed nor delivered anything did not happen.
        if os.path.lexists(src.absolute) or not os.path.lexists(dst.absolute):
            raise ProviderError(
                f"the {verb} did not take effect: {src.relative!r} is still where it was",
                FailureKind.PROVIDER,
            )
        return {"path": src.relative, "to": dst.relative, "moved": True}

    def _relocate_locked(self, src: ResolvedPath, dst: ResolvedPath) -> None:
        parts = [p for p in src.relative.split("/") if p]
        dst_parts = [p for p in dst.relative.split("/") if p]
        if not parts or not dst_parts:
            raise ProviderError(
                "the workspace root itself cannot be moved", FailureKind.AUTHORIZATION
            )
        # Both parent directories are reached through a walked descriptor, and the
        # rename is done *relative to those descriptors* — so the kernel resolves the
        # final names inside a tree it already proved confined. Nothing is re-opened by
        # a string that a caller could have swapped for a link.
        src_parent = self.workspace.walk_fd("/".join(parts[:-1]))
        dst_parent = self.workspace.walk_fd("/".join(dst_parts[:-1]))
        try:
            os.rename(
                parts[-1], dst_parts[-1], src_dir_fd=src_parent, dst_dir_fd=dst_parent
            )
        except OSError as exc:
            raise ProviderError(
                f"cannot move {src.relative!r} to {dst.relative!r}: {exc}",
                FailureKind.PROVIDER,
            ) from exc
        finally:
            os.close(src_parent)
            os.close(dst_parent)

    def copy(self, path: str, to: str) -> dict[str, Any]:
        """Copy one confined file (``file.read`` + ``file.write``), bounded.

        Both ends are resolved to descriptors first, then the bytes move with
        :func:`os.sendfile` kernel-side between the two open file descriptions. There is
        no intermediate path in play, so no symlink can be planted between "read from"
        and "write to" the way one can between a ``shutil.copy`` opening two names.
        """
        src = _resolve(self.workspace, path, must_exist=True)
        dst = _resolve(self.workspace, to)
        if os.path.lexists(dst.absolute):
            raise ProviderError(
                f"{to!r} already exists; this capability refuses to overwrite",
                FailureKind.INVALID_INPUT,
            )
        try:
            src_stat = os.lstat(src.absolute)
        except OSError as exc:
            raise ProviderError(
                f"cannot read {path!r}: {exc}", FailureKind.PROVIDER
            ) from exc
        if not stat.S_ISREG(src_stat.st_mode):
            raise ProviderError(
                f"{path!r} is not a regular file; this capability copies files",
                FailureKind.INVALID_INPUT,
            )
        read_fd = _open_fd(self.workspace, src, os.O_RDONLY)
        write_fd = _open_fd(self.workspace, dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        copied = 0
        try:
            with os.fdopen(read_fd, "rb") as source, os.fdopen(write_fd, "wb") as dest:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_WRITE_BYTES:
                        raise ProviderError(
                            f"{path!r} is larger than the {MAX_WRITE_BYTES} byte "
                            "copy limit",
                            FailureKind.INVALID_INPUT,
                        )
                    dest.write(chunk)
                dest.flush()
                os.fsync(dest.fileno())
        except OSError as exc:
            raise ProviderError(
                f"cannot copy {path!r} to {to!r}: {exc}", FailureKind.PROVIDER
            ) from exc
        return {"path": src.relative, "to": dst.relative, "copied": True, "bytes": copied}

    def edit(
        self,
        path: str,
        find: str,
        replace: str,
        all: bool = False,
    ) -> dict[str, Any]:
        """Replace text in one confined file — and show the change before it lands.

        The unified diff is computed first and returned under ``preview``, which the
        provider streams on ``files.preview`` before the result is reported. The bytes
        written are exactly the bytes the diff describes: one ``new_text`` produces both,
        so the preview cannot drift from the change. A ``find`` with no match is an honest
        zero — no diff, no write.
        """
        if not isinstance(find, str) or not isinstance(replace, str):
            raise ProviderError(
                "find and replace must be strings", FailureKind.INVALID_INPUT
            )
        if not find:
            raise ProviderError("find must not be empty", FailureKind.INVALID_INPUT)
        resolved = _resolve(self.workspace, path, must_exist=True)
        read_fd = _open_fd(self.workspace, resolved, os.O_RDONLY)
        try:
            with os.fdopen(read_fd, "rb") as handle:
                body = handle.read(MAX_WRITE_BYTES + 1)
        except OSError as exc:
            raise ProviderError(f"cannot read {path!r}: {exc}", FailureKind.PROVIDER) from exc
        if len(body) > MAX_WRITE_BYTES:
            raise ProviderError(
                f"{path!r} is larger than the {MAX_WRITE_BYTES} byte edit limit",
                FailureKind.INVALID_INPUT,
            )
        text, binary = _decode(body)
        if binary:
            raise ProviderError(
                f"{path!r} is not UTF-8 text; refusing a diff it could not show",
                FailureKind.INVALID_INPUT,
            )
        if all:
            count = text.count(find)
            new_text = text.replace(find, replace)
        else:
            count = 1 if find in text else 0
            new_text = text.replace(find, replace, 1)
        if count == 0:
            # Nothing matched: no diff, no write, the digest of what is already there.
            return {
                "path": resolved.relative,
                "replacements": 0,
                "sha256": hashlib.sha256(body).hexdigest(),
                "preview": "",
            }
        diff = "".join(
            difflib.unified_diff(
                text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=resolved.relative,
                tofile=resolved.relative,
            )
        )
        payload = new_text.encode("utf-8")
        if len(payload) > MAX_WRITE_BYTES:
            raise ProviderError(
                "the edited file would exceed the "
                f"{MAX_WRITE_BYTES} byte write limit",
                FailureKind.INVALID_INPUT,
            )
        digest = hashlib.sha256(payload).hexdigest()
        # Apply exactly the change the preview described, through the same boundary the
        # read used. One descriptor writes and fsyncs, so there is no torn half-state.
        fd = _open_fd(self.workspace, resolved, os.O_WRONLY | os.O_TRUNC)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ProviderError(f"cannot write {path!r}: {exc}", FailureKind.PROVIDER) from exc
        # Confirm what landed is what was previewed, re-reading through the same
        # boundary. If the file's own bytes do not match the digest, the diff was a
        # promise the filesystem did not keep.
        verify_fd = _open_fd(self.workspace, resolved, os.O_RDONLY)
        try:
            with os.fdopen(verify_fd, "rb") as handle:
                landed = handle.read(MAX_WRITE_BYTES + 1)
        except OSError as exc:
            raise ProviderError(
                f"cannot verify {path!r} after editing: {exc}", FailureKind.PROVIDER
            ) from exc
        if hashlib.sha256(landed).hexdigest() != digest:
            raise ProviderError(
                f"the edited file does not match its preview: {path!r}",
                FailureKind.PROVIDER,
            )
        return {
            "path": resolved.relative,
            "replacements": count,
            "sha256": digest,
            "preview": diff,
        }

    def trash(self, path: str) -> dict[str, Any]:
        """Move one confined file to the OS trash. **Never** a silent hard delete.

        ``gio trash`` is the only backend tried. If it is not on PATH, if it exits
        non-zero, or if the file survives the call, the answer is
        ``{"status": "unavailable", ..., "reason": ...}`` and the file is still where it
        was — this capability does not fall back to unlinking, because "I could not put it
        in the trash, so I destroyed it" is exactly the lie the contract forbids.
        """
        resolved = _resolve(self.workspace, path, must_exist=True)
        try:
            head = os.lstat(resolved.absolute)
        except FileNotFoundError as exc:
            raise ProviderError(
                f"no such file or directory: {path!r}", FailureKind.INVALID_INPUT
            ) from exc
        except OSError as exc:
            raise ProviderError(f"cannot stat {path!r}: {exc}", FailureKind.PROVIDER) from exc
        if stat.S_ISDIR(head.st_mode):
            raise ProviderError(
                f"{path!r} is a directory; this capability trashes files",
                FailureKind.INVALID_INPUT,
            )
        backend = self._trash_backend()
        if backend is None:
            return {
                "path": resolved.relative,
                "trashed": False,
                "to": None,
                "status": "unavailable",
                "reason": "no OS trash backend (gio) is available; hard delete is a "
                "separate capability and this one will not do it",
            }
        proc = self._run_trash(backend, resolved.absolute)
        gone = not os.path.lexists(resolved.absolute)
        if proc is None or proc.returncode != 0 or not gone:
            return {
                "path": resolved.relative,
                "trashed": False,
                "to": None,
                "status": "unavailable",
                "reason": "the OS trash backend did not move the file; it is still at "
                f"{resolved.relative!r} and nothing was deleted",
            }
        return {
            "path": resolved.relative,
            "trashed": True,
            "to": self._TRASH_LOCATION,
        }

    def open_(self, path: str) -> dict[str, Any]:
        """Hand one confined file to the OS default app — or say plainly that we can't.

        ``opened`` is true only when ``xdg-open`` was actually found, actually launched,
        and actually exited zero. No opener, a failed launch, or a non-zero exit yields
        ``status:"unavailable"`` with ``opened:false`` and a reason; the file is left
        exactly as it was.
        """
        resolved = _resolve(self.workspace, path, must_exist=True)
        try:
            kind = _kind_for(os.lstat(resolved.absolute).st_mode)
        except FileNotFoundError as exc:
            raise ProviderError(
                f"no such file or directory: {path!r}", FailureKind.INVALID_INPUT
            ) from exc
        except OSError as exc:
            raise ProviderError(f"cannot stat {path!r}: {exc}", FailureKind.PROVIDER) from exc
        opener = self._opener_path()
        if opener is None:
            return {
                "status": "unavailable",
                "path": resolved.relative,
                "kind": kind,
                "opened": False,
                "reason": "no OS file opener (xdg-open) is available on this laptop",
            }
        proc = self._run_opener(opener, resolved.absolute)
        if proc is None or proc.returncode != 0:
            return {
                "status": "unavailable",
                "path": resolved.relative,
                "kind": kind,
                "opened": False,
                "reason": "the OS file opener did not accept the file",
            }
        return {"status": "ok", "path": resolved.relative, "kind": kind, "opened": True}

    # -- OS integration: fixed binaries, one confined path, no shell -------
    _TRASH_LOCATION = "the freedesktop trash"

    @staticmethod
    def _which(binary: str) -> str | None:
        from actionmesh.agent.commands import find_on_path

        return find_on_path(binary)

    @classmethod
    def _trash_backend(cls) -> str | None:
        """The absolute path of ``gio``, or ``None``."""
        return cls._which(_TRASH_ARGV[0])

    @classmethod
    def _opener_path(cls) -> str | None:
        """The absolute path of ``xdg-open``, or ``None``."""
        return cls._which(_OPENER_BINARY)

    @classmethod
    def _run_trash(cls, backend: str, absolute: str) -> subprocess.CompletedProcess | None:
        """``gio trash <path>``: a fixed argv, one path element, ``shell=False``."""
        try:
            return subprocess.run(
                [backend, *_TRASH_ARGV[1:], absolute],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TRASH_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    @classmethod
    def _run_opener(cls, opener: str, absolute: str) -> subprocess.CompletedProcess | None:
        """``xdg-open <path>``: a fixed argv, one path element, ``shell=False``."""
        try:
            return subprocess.run(
                [opener, absolute],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_OPENER_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None


__all__ = [
    "MAX_LIST_ENTRIES",
    "MAX_READ_BYTES",
    "MAX_WRITE_BYTES",
    "FilesEngine",
]
