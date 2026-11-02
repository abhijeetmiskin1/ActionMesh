"""C19 path confinement: the workspace root, and why a phone cannot name a file.

Section 4 of the brief is short and correct: *do not trust a path supplied by the
phone*. This module is where that becomes true rather than asserted. Everything that
reads or writes the laptop's filesystem resolves through here first.

What "resolve" has to mean, and what each step defends against
---------------------------------------------------------------
``../`` segments
    Rejected lexically, before anything else, because the shape is never what the
    caller meant. This is a fast rejection, **not** the defence — the defence is the
    canonical check two steps down, because ``a/../b`` and ``%2e%2e`` and a symlink
    all arrive at the same place by different roads.
Absolute paths
    Rejected. An agent that accepts ``/etc/passwd`` is a file reader for the whole
    machine with an extra step.
Canonical containment
    ``os.path.realpath`` of the candidate *and* of the root, then a component-boundary
    prefix test — not a string ``startswith``, because ``/home/abhi/project`` is a
    prefix of ``/home/abhi/projects`` while being a different directory, and a
    substring test would wave that through.
Symlinks
    Containment is checked on the resolved path, so a link inside the root that points
    outside it lands outside and is refused. For **mutations** the check is stronger
    than that: the path is walked with ``openat`` and ``O_NOFOLLOW`` one component at
    a time, so a link swapped in after the check cannot carry the write out of the
    root. A check whose result can be stale by the time it is used is a check that
    races, and TOCTOU is the classic way to turn "validated, then opened" into an
    arbitrary write.
Secret locations
    Denied by name against the *resolved* path, so ``project/../.ssh/id_rsa`` and a
    symlink into ``~/.ssh`` both land on a denied path rather than on something that
    looks allowed.

The rule this leaves is worth naming: **a path that resolves through here is inside
the root, or nothing happens with it.** Callers get a :class:`ResolvedPath`, and the
only way to obtain one is by passing every check above.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass
from typing import NoReturn

from actionmesh.agent.errors import PathEscape, PathNotFound

#: Denied by basename anywhere under the root. Matched against the resolved path's
#: components, so a symlink cannot carry a name through a different door.
DENIED_BASENAMES: frozenset[str] = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".gcloud",
        ".kube",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        "authorized_keys",
        "known_hosts",
        "shadow",
        "secring.gpg",
    }
)

#: Denied as a root-relative prefix. These are the directories that hold a signed-in
#: session someone can steal, rather than a key someone typed.
DENIED_RELATIVE_PREFIXES: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".config/gcloud",
    ".config/google-chrome",
    ".config/chromium",
    ".config/microsoft-edge",
    ".mozilla",
    ".cache/ms-playwright",
)

#: A ``*.env`` / ``.env.local`` file in a project is the project's credentials, and
#: the phone is the least trusted place to read them from.
DENIED_SECRETFLE_PREFIXES: tuple[str, ...] = (".env",)

#: ``C:\`` or ``D:`` as the start of a path.
_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")
#: Percent-encoded ``.`` ``/`` ``\`` — the canonical smuggling alphabet for paths.
_ENCODED_SEPARATOR = re.compile(r"%2e|%2f|%5c", re.IGNORECASE)

_MAX_COMPONENTS = 256


def _fail(reason: str, supplied: object) -> NoReturn:
    raise PathEscape(f"{reason}: {supplied!r}")


@dataclass(frozen=True)
class ResolvedPath:
    """A path that already passed confinement.

    ``relative`` is what a response or an audit record should quote — the caller's own
    spelling, so a log never becomes a map of the machine's layout, and never an
    absolute path someone else chose.
    """

    absolute: str
    relative: str

    @property
    def name(self) -> str:
        return os.path.basename(self.absolute)


class Workspace:
    """One confined root plus the rules that keep every access inside it."""

    def __init__(
        self,
        root: str,
        *,
        extra_denied: tuple[str, ...] = (),
        follow_final_symlink: bool = False,
    ) -> None:
        if not isinstance(root, str) or not root.strip():
            raise PathEscape("a workspace root is required")
        if not os.path.isabs(root):
            raise PathEscape("the workspace root must be an absolute path")
        if not os.path.isdir(root):
            raise PathEscape(f"workspace root {root!r} is not a directory")
        #: Canonical root. Every containment decision is taken against this value, so
        #: a root that is itself a symlink (macOS ``/tmp`` -> ``/private/tmp``) does
        #: not put its own contents outside the boundary.
        self.root = os.path.realpath(root)
        self._extra = tuple(extra_denied)
        #: Off by default. Reading *through* a symlink that points inside the root is
        #: harmless, but it is the same mechanism that reads out of it, so the
        #: conservative answer is to refuse until a caller asks deliberately.
        self.follow_final_symlink = follow_final_symlink

    # -- the check --------------------------------------------------------
    def resolve(self, supplied: object, *, must_exist: bool = False) -> ResolvedPath:
        """Confinement: turn an untrusted path into a :class:`ResolvedPath` or raise.

        Raises :class:`PathEscape` for anything that does not land, strictly inside,
        and is written so that no branch of it ends in "could not tell, proceed
        anyway".
        """
        if not isinstance(supplied, str):
            _fail("path must be a string", supplied)
        cleaned = supplied.strip()
        if not cleaned:
            _fail("path is empty", supplied)
        if "\x00" in cleaned:
            _fail("path contains a NUL byte", supplied)
        if os.path.isabs(cleaned) or cleaned.startswith(("/", "\\")):
            _fail("absolute paths are not allowed", supplied)
        if _DRIVE_LETTER.match(cleaned):
            # Harmless on Linux, an absolute path on Windows. The agent runs on the
            # laptop, and the laptop may not be this platform in six months.
            _fail("drive-qualified paths are not allowed", supplied)
        if _ENCODED_SEPARATOR.search(cleaned):
            # ``..%2f..%2fetc`` is not traversal to *this* function, because decoding
            # is the transport's job and the transport decodes first. It is still
            # worth refusing: a name that only means "escape" after somebody else
            # normalises it is a value that survives one layer's check and not the
            # next, and no legitimate file in a project is named like that.
            _fail("percent-encoded path separators are not allowed", supplied)
        if len(cleaned) > 4096:
            _fail("path is unreasonably long", supplied)

        # Normalise separators, then reject traversal lexically. `realpath` below is
        # what actually decides containment; this is the cheap, loud rejection.
        normalised = cleaned.replace("\\", "/")
        components = [part for part in normalised.split("/") if part not in ("", ".")]
        if not components:
            _fail("path resolves to nothing", supplied)
        if len(components) > _MAX_COMPONENTS:
            _fail("path has too many components", supplied)
        if os.pardir in components:
            _fail("'..' is not allowed", supplied)

        candidate = os.path.join(self.root, *components)
        resolved = os.path.realpath(candidate)

        if not self._contains(resolved):
            # Reached for `..` smuggled through a symlink, for a link whose target
            # escapes, and for a root that moved underneath the call. All three are
            # the same answer.
            _fail("path escapes the workspace root", supplied)
        if os.path.islink(candidate) and not self.follow_final_symlink:
            _fail("symlinks are not followed", supplied)
        self._check_denied(resolved, components, supplied)

        if must_exist:
            if not os.path.exists(resolved):
                raise PathNotFound(f"no such file or directory: {supplied!r}")
        else:
            # For a write, the *parent* has to be inside too — otherwise
            # `newdir/link` can be created where `newdir` is itself a symlink out.
            parent = os.path.realpath(os.path.dirname(resolved))
            if not self._contains(parent):
                _fail("the containing directory escapes the workspace root", supplied)
            if os.path.exists(resolved) and os.path.islink(resolved) and not self.follow_final_symlink:
                _fail("path is a symlink", supplied)

        return ResolvedPath(absolute=resolved, relative="/".join(components))

    def _contains(self, path: str) -> bool:
        """True when ``path`` is the root or below it, on component boundaries."""
        if path == self.root:
            return True
        prefix = self.root if self.root.endswith(os.sep) else self.root + os.sep
        return path.startswith(prefix)

    def _check_denied(
        self, resolved: str, components: list[str], supplied: str
    ) -> None:
        lowered = [part.lower() for part in resolved.split(os.sep) if part]
        for part in lowered:
            if part in DENIED_BASENAMES or part in self._extra:
                _fail("this location is not readable or writable", supplied)
        relative = "/".join(components).lower()
        for denied in DENIED_RELATIVE_PREFIXES:
            if relative == denied or relative.startswith(denied + "/"):
                _fail("this location is not readable or writable", supplied)
        base = components[-1].lower()
        for denied in DENIED_SECRETFLE_PREFIXES:
            if base == denied or base.startswith(denied + "."):
                _fail("environment and secret files are not exposed", supplied)

    # -- mutation-safe walking --------------------------------------------
    def open_fd(self, resolved: ResolvedPath, flags: int, *, create_mode: int = 0o600) -> int:
        """Open ``resolved`` relative to the root descriptor, refusing every symlink.

        This is the part a string check cannot do. The root is opened once as a
        directory descriptor and each component below it is looked up with
        ``openat``/``O_NOFOLLOW``, so the kernel — not a comparison made earlier —
        decides that no step of the walk leaves the tree. A link planted *after*
        :meth:`resolve` returns therefore fails to open rather than racing past the
        check.

        Platforms without ``O_NOFOLLOW`` are refused outright. Silently dropping the
        one flag that makes this atomic would turn a hard guarantee into a
        best-effort one, which is the kind of weakening that survives code review.
        """
        if not (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")):
            raise PathEscape(
                "this platform cannot refuse symlinks while opening; refusing to "
                "write through a check that can race"
            )
        parts = [part for part in resolved.relative.split("/") if part]
        if not parts:
            raise PathEscape("cannot open the workspace root as a file")
        for part in parts:
            if part in (os.curdir, os.pardir) or part.startswith("/"):
                raise PathEscape(f"unsafe component {part!r}")

        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, part in enumerate(parts):
                if index == len(parts) - 1:
                    try:
                        return os.open(part, flags | os.O_NOFOLLOW, create_mode, dir_fd=fd)
                    except OSError as exc:
                        # ELOOP is the refusal: something in the walk is a link and
                        # links are not followed here. Anything else (a missing file,
                        # a wrong node type) is the caller's problem to report, not a
                        # security decision, so it travels unchanged.
                        if exc.errno == errno.ELOOP:
                            raise PathEscape(
                                f"path is a symlink and symlinks are not followed: "
                                f"{resolved.relative!r}"
                            ) from exc
                        raise
                try:
                    child = os.open(
                        part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd
                    )
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise PathEscape(
                            f"a directory component is a symlink: {part!r}"
                        ) from exc
                    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                        # Deliberately not a `mkdir -p`. A phone that can create
                        # directory trees can reshape the workspace whether or not
                        # anything is ever written into them, and "the folder does
                        # not exist yet" is an answer the caller can act on.
                        raise PathNotFound(
                            f"the directory {part!r} does not exist inside the "
                            "workspace root; this capability does not create "
                            "directories"
                        ) from exc
                    raise
                os.close(fd)
                fd = child
            raise PathEscape("unreachable: the final component always returns")
        finally:
            os.close(fd)

    def walk_fd(self, relative: str = "") -> int:
        """Open a *directory* below the root by walking it with ``O_NOFOLLOW``.

        The descriptor is the safe handle: from the moment this returns, every entry
        read through it is inside the tree, because getting to it required each
        component to be a real directory rather than a link. Listing or writing
        through a path re-opened by name would re-enter the string check that can go
        stale.
        """
        target = self.directory(relative)
        parts = [part for part in target.relative.split("/") if part]
        if not (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")):
            raise PathEscape(
                "this platform cannot refuse symlinks while opening; refusing to "
                "walk a path that cannot be checked"
            )
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts:
                if part in (os.curdir, os.pardir) or part.startswith("/"):
                    raise PathEscape(f"unsafe component {part!r}")
                try:
                    child = os.open(
                        part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd
                    )
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise PathEscape(
                            f"a directory component is a symlink: {part!r}"
                        ) from exc
                    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                        raise PathNotFound(
                            f"no such directory: {relative!r}"
                        ) from exc
                    raise
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
        return fd

    def unlink(self, resolved: ResolvedPath) -> None:
        """Remove one confined file, arriving by :meth:`walk_fd`.

        Deleting through a re-opened parent path would leave the classic gap: check
        the path, then a link is swapped in, then the link is followed out of the
        tree and someone else's file is gone. Opening the parent descriptor-first and
        unlinking *relative to it* closes the gap, because the name resolution the
        kernel performs is the one that was checked.
        """
        parts = [part for part in resolved.relative.split("/") if part]
        if not parts:
            raise PathEscape("cannot delete the workspace root")
        parent_relative = "/".join(parts[:-1])
        directory_fd = self.walk_fd(parent_relative)
        try:
            os.unlink(parts[-1], dir_fd=directory_fd)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise PathNotFound(f"no such file: {resolved.relative!r}") from exc
            raise PathEscape(f"cannot delete {resolved.relative!r}: {exc}") from exc
        finally:
            os.close(directory_fd)

    # -- listing ----------------------------------------------------------
    def directory(self, relative: str = "") -> ResolvedPath:
        """The root itself, or a confined directory below it.

        An empty or ``"."`` argument means the root, which :meth:`resolve` cannot
        express — resolve refuses a path that names nothing, and it should: the root
        has to be *meant*, not arrived at by omission.
        """
        if (relative or "").strip() in ("", ".", "/"):
            return ResolvedPath(absolute=self.root, relative="")
        resolved = self.resolve(relative, must_exist=True)
        if not os.path.isdir(resolved.absolute):
            _fail("not a directory", relative)
        return resolved

    def list(self, relative: str = "", *, limit: int = 500) -> list[dict[str, object]]:
        """One directory level, read through a descriptor that was walked, not opened.

        Symlinks are reported as symlinks and never followed, so a listing cannot
        become a way to learn what lives outside the root.
        """
        target = self.directory(relative)
        directory_fd = self.walk_fd(relative)
        prefix = f"{str(target.relative).strip('/')}/" if target.relative else ""
        entries: list[dict[str, object]] = []
        try:
            with os.scandir(directory_fd) as scan:
                for entry in sorted(scan, key=lambda item: item.name):
                    if len(entries) >= limit:
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    is_link = stat.S_ISLNK(info.st_mode)
                    confined = True
                    if is_link:
                        # Resolving the link target is only ever a *question* about it:
                        # the answer decides what we report, never what we open.
                        confined = self._contains(
                            os.path.realpath(os.path.join(target.absolute, entry.name))
                        )
                    entries.append(
                        {
                            "name": entry.name,
                            "path": f"{prefix}{entry.name}",
                            "type": (
                                "link"
                                if is_link
                                else "dir"
                                if stat.S_ISDIR(info.st_mode)
                                else "file"
                            ),
                            "size": 0 if is_link else int(info.st_size),
                            "modified": info.st_mtime,
                            "confined": confined,
                        }
                    )
        except OSError as exc:
            raise PathEscape(f"cannot list {relative!r}: {exc}") from exc
        return entries

    def __repr__(self) -> str:
        return f"Workspace(root={self.root!r})"


__all__ = [
    "DENIED_BASENAMES",
    "DENIED_RELATIVE_PREFIXES",
    "ResolvedPath",
    "Workspace",
]
