"""Create a zipped snapshot of the working tree.

Stands in for git while no git binary is available, so a repeat of the earlier
data loss cannot destroy the work again. Excludes the venv, caches, secrets —
and any earlier snapshot, so archive size cannot compound.

By default the archive is written *beside* the working tree, where it survives
the loss of the tree itself. Passing a path overrides that; writing inside the
tree is allowed but warns, because such a backup is destroyed by the incident
it exists to survive.

Usage:
    python scripts/snapshot.py [output.zip]
"""

from __future__ import annotations

import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

EXCLUDE_DIRS = {
    ".venv",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    # Downloaded Android toolchain (JDK + SDK, hundreds of MiB) and setuptools
    # build output. Both are regenerable from source, so a backup that included
    # them would be enormous for no benefit — the same failure mode as the
    # 39.8 GB of compounding archives described below.
    "android-toolchain",
    "build",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}
# Never snapshot secrets even if someone created them locally.
EXCLUDE_NAMES = {".env", ".env.local", "credentials.json"}
# A backup must never contain another backup. Without this, an archive written
# into the working tree is copied into the next one and the size compounds on
# every run. This actually happened: 16 archives reached 39.8 GB.
EXCLUDE_PATTERNS = ("actionmesh-snapshot-",)


def should_skip(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES:
        return True
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    return any(pattern in path.name for pattern in EXCLUDE_PATTERNS)


def build(root: Path, target: Path) -> int:
    root = root.resolve()
    target = target.resolve()
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.resolve() == target:
                continue
            if should_skip(path.relative_to(root)):
                continue
            zf.write(path, path.relative_to(root))
            count += 1
    return count


def default_target(root: Path) -> Path:
    """A backup belongs beside the tree, not inside it.

    Inside, it is destroyed by the very incident it exists to survive, and it
    inflates every subsequent backup.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return root.resolve().parent / f"actionmesh-snapshot-{stamp}.zip"


def main(argv: list[str]) -> int:
    root = Path.cwd()
    target = (Path(argv[1]) if len(argv) > 1 else default_target(root)).resolve()
    if root.resolve() in target.parents:
        print(f"warning: writing inside the working tree: {target}",
              file=sys.stderr)
    count = build(root, target)
    size_kib = target.stat().st_size / 1024
    print(f"snapshot: {count} files -> {target}")
    print(f"  size: {size_kib:,.1f} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
