"""ParallelWorld's bounded, isolated file-plan preview evaluator.

This module is only called by the laptop provider while executing the registered
``device.files.preview_plan`` capability. It never touches the configured workspace
except to take a bounded, descriptor-pinned snapshot. Proposed writes are evaluated
in a private temporary directory; real writes remain ordinary ``device.files.*``
actions dispatched by the ActionMesh execution engine.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from actionmesh.agent.files import MAX_WRITE_BYTES, FilesEngine
from actionmesh.agent.paths import Workspace
from actionmesh.protocol.models import FailureKind
from actionmesh.providers.base import ProviderError

MAX_FILES = 100
MAX_DIRECTORIES = 250
MAX_DEPTH = 24
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_OPERATIONS = 24
_LOCK = threading.RLock()


@dataclass
class PreviewRecord:
    preview_id: str
    operations: list[dict[str, Any]]
    before: dict[str, dict[str, Any]]
    after: dict[str, dict[str, Any]]
    diff: str
    apply_started: bool = False
    apply_execution_id: str = ""
    actor_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


_PREVIEWS: dict[str, PreviewRecord] = {}


def _fail(message: str) -> None:
    raise ProviderError(message, FailureKind.INVALID_INPUT)


def _normalise_operations(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_OPERATIONS:
        _fail(f"operations must contain 1-{MAX_OPERATIONS} entries")
    allowed = {
        "create": {"op", "path", "content"},
        "edit": {"op", "path", "find", "replace", "all"},
        "copy": {"op", "path", "to"},
        "rename": {"op", "path", "to"},
        "move": {"op", "path", "to"},
        "trash": {"op", "path"},
    }
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or item.get("op") not in allowed:
            _fail(f"operation {index + 1} has an unsupported operation")
        verb = item["op"]
        if set(item) - allowed[verb]:
            _fail(f"operation {index + 1} has unknown fields")
        for key in ("path", "to", "find", "replace", "content"):
            if key in item and not isinstance(item[key], str):
                _fail(f"operation {index + 1} field {key!r} must be text")
        if not item.get("path"):
            _fail(f"operation {index + 1} needs a path")
        if verb in {"copy", "rename", "move"} and not item.get("to"):
            _fail(f"operation {index + 1} needs a destination")
        if verb == "edit" and not item.get("find"):
            _fail(f"operation {index + 1} needs a non-empty find value")
        if verb == "create" and len(item.get("content", "").encode("utf-8")) > MAX_WRITE_BYTES:
            _fail(f"operation {index + 1} exceeds the {MAX_WRITE_BYTES}-byte content limit")
        if verb == "edit" and (len(item["find"].encode()) > MAX_FILE_BYTES or len(item["replace"].encode()) > MAX_FILE_BYTES):
            _fail(f"operation {index + 1} exceeds the text edit limit")
        result.append(dict(item))
    return result


def _snapshot(workspace: Workspace) -> dict[str, dict[str, Any]]:
    """Read regular files using Workspace's no-follow fd boundary and strict caps."""
    root_fd = workspace.walk_fd("")
    found: dict[str, dict[str, Any]] = {}
    total = 0
    directories = 0
    try:
        stack: list[tuple[int, str]] = [(os.dup(root_fd), "")]
        while stack:
            directory_fd, prefix = stack.pop()
            try:
                for entry in os.scandir(directory_fd):
                    rel = f"{prefix}/{entry.name}".strip("/")
                    if len(rel.split("/")) > MAX_DEPTH:
                        _fail(f"workspace exceeds the {MAX_DEPTH}-level preview depth limit")
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise ProviderError(f"symlink is not allowed in preview workspace: {rel}", FailureKind.AUTHORIZATION)
                    if stat.S_ISDIR(info.st_mode):
                        directories += 1
                        if directories > MAX_DIRECTORIES:
                            _fail(f"workspace exceeds the {MAX_DIRECTORIES}-directory preview limit")
                        child = workspace.walk_fd(rel)
                        stack.append((child, rel))
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        _fail(f"unsupported filesystem object in preview workspace: {rel}")
                    if len(found) >= MAX_FILES:
                        _fail(f"workspace exceeds the {MAX_FILES}-file preview limit")
                    if info.st_size > MAX_FILE_BYTES or total + info.st_size > MAX_TOTAL_BYTES:
                        _fail("workspace exceeds ParallelWorld's per-file or total snapshot size limit")
                    resolved = workspace.resolve(rel, must_exist=True)
                    fd = workspace.open_fd(resolved, os.O_RDONLY)
                    try:
                        pinned = os.fstat(fd)
                        if (pinned.st_dev, pinned.st_ino) != (info.st_dev, info.st_ino):
                            _fail(f"file changed while snapshotting: {rel}")
                        chunks: list[bytes] = []
                        read_total = 0
                        while True:
                            block = os.read(fd, min(64 * 1024, MAX_FILE_BYTES + 1))
                            if not block:
                                break
                            chunks.append(block)
                            read_total += len(block)
                            if read_total > MAX_FILE_BYTES:
                                _fail(f"file exceeds the {MAX_FILE_BYTES}-byte snapshot limit: {rel}")
                        body = b"".join(chunks)
                        after = os.fstat(fd)
                        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                            pinned.st_dev, pinned.st_ino, pinned.st_size, pinned.st_mtime_ns
                        ):
                            _fail(f"file changed during snapshot: {rel}")
                    finally:
                        os.close(fd)
                    total += len(body)
                    found[rel] = {
                        "identity": f"{info.st_dev}:{info.st_ino}",
                        "size": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "mode": stat.S_IMODE(info.st_mode),
                        "content": body,
                    }
            finally:
                os.close(directory_fd)
    finally:
        os.close(root_fd)
    return found


def _apply_shadow(root: str, operations: list[dict[str, Any]]) -> None:
    workspace = Workspace(root)
    files = FilesEngine(workspace)
    for item in operations:
        verb = item["op"]
        if verb == "create":
            if os.path.lexists(os.path.join(root, item["path"])):
                _fail(f"create destination already exists: {item['path']}")
            files.create(item["path"], item["content"])
        elif verb == "edit":
            files.edit(item["path"], item["find"], item["replace"], bool(item.get("all", False)))
        elif verb == "copy":
            files.copy(item["path"], item["to"])
        elif verb in {"rename", "move"}:
            (files.rename if verb == "rename" else files.move)(item["path"], item["to"])
        elif verb == "trash":
            # Preview simulates removal only inside its disposable shadow. It never
            # calls gio, Finder, or any OS trash integration.
            resolved = workspace.resolve(item["path"], must_exist=True)
            info = os.lstat(resolved.absolute)
            if not stat.S_ISREG(info.st_mode):
                _fail("trash preview supports regular files only")
            workspace.unlink(resolved)


def _public(snapshot: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        path: {key: value for key, value in entry.items() if key != "content"}
        for path, entry in snapshot.items()
    }


def summary_counts(record: PreviewRecord) -> dict[str, int]:
    before, after = set(record.before), set(record.after)
    moved = {
        item["to"] for item in record.operations
        if item["op"] in {"rename", "move"} and item.get("to")
    }
    created = len((after - before) - moved)
    modified = sum(
        1 for path in before & after
        if record.before[path]["sha256"] != record.after[path]["sha256"]
    )
    moved_count = sum(
        1 for item in record.operations
        if item["op"] in {"rename", "move"}
        and item["path"] not in after and item["to"] in after
    )
    trashed = sum(
        1 for item in record.operations
        if item["op"] == "trash" and item["path"] not in after
    )
    return {
        "files_created": created,
        "files_modified": modified,
        "files_moved": moved_count,
        "files_proposed_for_trash": trashed,
    }


def _diff(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> str:
    chunks: list[str] = []
    for path in sorted(set(before) | set(after)):
        left, right = before.get(path), after.get(path)
        if left and right and left["sha256"] == right["sha256"]:
            continue
        try:
            old = left["content"].decode("utf-8").splitlines(keepends=True) if left else []
            new = right["content"].decode("utf-8").splitlines(keepends=True) if right else []
        except UnicodeDecodeError:
            chunks.append(f"Binary change: {path} ({left['sha256'] if left else 'absent'} → {right['sha256'] if right else 'absent'})\n")
            continue
        chunks.extend(difflib.unified_diff(old, new, fromfile=f"before/{path}" if left else "/dev/null", tofile=f"after/{path}" if right else "/dev/null"))
    diff = "".join(chunks)
    return diff[:200_000] if len(diff) > 200_000 else diff


def preview(workspace: Workspace, raw_operations: object) -> tuple[PreviewRecord, dict[str, Any]]:
    operations = _normalise_operations(raw_operations)
    before = _snapshot(workspace)
    for index, item in enumerate(operations):
        if item["op"] != "create" and item["path"] not in before:
            _fail(f"operation {index + 1} must name an existing regular file")
        if item["op"] in {"copy", "rename", "move"} and item["to"] in before:
            _fail(f"operation {index + 1} destination already exists")
    with tempfile.TemporaryDirectory(prefix="actionmesh-parallelworld-") as shadow:
        for path, entry in before.items():
            target = os.path.join(shadow, *path.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "xb") as handle:
                handle.write(entry["content"])
            os.chmod(target, entry["mode"])
        _apply_shadow(shadow, operations)
        shadow_workspace = Workspace(shadow)
        after = _snapshot(shadow_workspace)
        diff = _diff(before, after)
    record = PreviewRecord(uuid.uuid4().hex, operations, before, after, diff)
    if _public(_snapshot(workspace)) != _public(before):
        _fail("source workspace changed while preview was being prepared; preview discarded")
    with _LOCK:
        _PREVIEWS[record.preview_id] = record
        # Bounded in-memory lifetime, newest first. Plans may contain proposed text.
        while len(_PREVIEWS) > 20:
            _PREVIEWS.pop(next(iter(_PREVIEWS)))
    return record, {
        "preview_id": record.preview_id,
        **summary_counts(record),
        "files_changed": len(set(before) ^ set(after)) + sum(1 for p in set(before) & set(after) if before[p]["sha256"] != after[p]["sha256"]),
        "diff_bytes": len(diff.encode("utf-8")),
        "permission_requirements": sorted({"file.read", *({"file.delete"} if any(op["op"] == "trash" for op in operations) else set()), *({"file.write"} if any(op["op"] != "trash" for op in operations) else set())}),
    }


def get_preview(preview_id: str) -> PreviewRecord | None:
    with _LOCK:
        record = _PREVIEWS.get(preview_id)
        if record is not None and datetime.now(UTC) - record.created_at > timedelta(hours=1):
            _PREVIEWS.pop(preview_id, None)
            return None
        return record


def list_previews() -> list[PreviewRecord]:
    with _LOCK:
        now = datetime.now(UTC)
        expired = [key for key, value in _PREVIEWS.items() if now - value.created_at > timedelta(hours=1)]
        for key in expired:
            _PREVIEWS.pop(key, None)
        return list(reversed(tuple(_PREVIEWS.values())))


def check_fresh(workspace: Workspace, record: PreviewRecord) -> bool:
    return check_expected(workspace, record.before, require_identity=True)


def check_expected(
    workspace: Workspace,
    expected: dict[str, dict[str, Any]],
    *,
    require_identity: bool,
) -> bool:
    """Compare current file metadata to a captured state, optionally pinning inodes."""
    observed = _public(_snapshot(workspace))
    wanted = _public(expected)
    if observed.keys() != wanted.keys():
        return False
    for path, item in wanted.items():
        current = observed[path]
        keys = ("identity", "size", "sha256", "mode") if require_identity else ("size", "sha256", "mode")
        if any(current[key] != item[key] for key in keys):
            return False
    return True


def reserve_apply(workspace: Workspace, preview_id: str) -> PreviewRecord:
    """Revalidate the exact captured source and atomically make this preview one-shot."""
    with _LOCK:
        record = _PREVIEWS.get(preview_id)
        if record is None:
            raise ProviderError("preview is missing or expired; create a new preview", FailureKind.INVALID_INPUT)
        if record.apply_started:
            raise ProviderError("this preview has already been submitted for application", FailureKind.INVALID_INPUT)
        if not check_fresh(workspace, record):
            raise ProviderError("stale preview: workspace file identities or hashes changed; preview again", FailureKind.INVALID_INPUT)
        record.apply_started = True
        return record


def unchanged_proof(record: PreviewRecord) -> dict[str, Any]:
    return {
        path: {"identity": item["identity"], "size": item["size"], "sha256": item["sha256"]}
        for path, item in _public(record.before).items()
    }
