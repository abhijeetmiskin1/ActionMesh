#!/usr/bin/env python3
"""Stage a self-contained Python runtime and the application for the desktop build.

The host matters: this machine has Python 3.14 only, and a desktop artifact must not
assume the user has any Python at all. So the bundle is a *standalone* CPython
(python-build-standalone's ``install_only`` archive), dependencies installed into it
with ``pip --target``-style semantics, and the app's own source tree copied beside it.

What this script does, in order:

1. Downloads (or reuses) the pinned standalone CPython for the target platform.
2. Installs the app's *runtime* dependencies into the bundled site-packages.
3. Copies ``actionmesh/`` into ``desktop/runtime/app/`` next to the interpreter.

It does nothing if the inputs it needs are already staged, so a rebuild is cheap.

Usage:
    python scripts/bundle_runtime.py             # linux-x64, network allowed
    python scripts/bundle_runtime.py --offline   # use a pre-downloaded archive
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
RUNTIME = DESKTOP / "runtime"
PBS_TAG = "20261026"
PBS_BASE = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PBS_TAG}"

#: target -> (asset name, interpreter path inside the archive, python version)
TARGETS = {
    "linux-x64": (
        f"cpython-3.12.16+{PBS_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz",
        "python/bin/python3",
        "3.12",
    ),
    "windows-x64": (
        f"cpython-3.12.16+{PBS_TAG}-x86_64-pc-windows-msvc-shared-install_only.tar.gz",
        "python/python.exe",
        "3.12",
    ),
    "macos-x64": (
        f"cpython-3.12.16+{PBS_TAG}-x86_64-apple-darwin-install_only.tar.gz",
        "python/bin/python3",
        "3.12",
    ),
    "macos-arm64": (
        f"cpython-3.12.16+{PBS_TAG}-aarch64-apple-darwin-install_only.tar.gz",
        "python/bin/python3",
        "3.12",
    ),
}

#: Runtime dependencies only. Test and lint tooling has no place in a shipped binary.
RUNTIME_DEPS = ["fastapi>=0.115", "uvicorn>=0.30"]


def host_target() -> str:
    if sys.platform.startswith("linux"):
        return "linux-x64"
    if sys.platform == "darwin":
        machine = os.uname().machine
        return "macos-arm64" if machine in {"arm64", "aarch64"} else "macos-x64"
    raise SystemExit(f"this script is for linux/macos builds; got {sys.platform!r}")


def _archive_is_intact(path: Path) -> bool:
    """A truncated download must never be mistaken for a cached one.

    Size is not evidence: an interrupted transfer leaves a plausible-looking file
    that fails only later, inside extraction, as a confusing tar error. Read the
    archive's own structure instead, which is cheap for the footer of a gzip.
    """
    try:
        if path.name.endswith(".tar.gz"):
            with tarfile.open(path, "r:gz") as tar:
                # Walking every member is the only way gzip surfaces a truncated
                # stream; the argument-less getmembers() stops at the first bad block.
                return bool(tar.getmembers())
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zipped:
                return zipped.testzip() is None
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError):
        return False
    return False


def download(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if _archive_is_intact(destination):
            print(f"reusing {destination.name} ({destination.stat().st_size // 1_000_000} MB)")
            return destination
        # An interrupted run left this behind. Delete it and fetch again rather than
        # handing a corrupt archive to the extractor.
        print(f"discarding incomplete {destination.name}; re-downloading")
        destination.unlink()
    print(f"downloading {url}")
    # Write to a sibling .part and rename only once the relay has closed cleanly, so
    # an interrupted run can never leave a file that looks complete to the next one.
    partial = destination.with_name(destination.name + ".part")
    with urllib.request.urlopen(url, timeout=120) as response:
        total = int(response.headers.get("content-length") or 0)
        with open(partial, "wb") as handle:
            read = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
                read += len(chunk)
                if total:
                    print(f"\r  {read * 100 // total:3d}%  {read // 1_000_000} MB", end="")
    if total and read != total:
        partial.unlink(missing_ok=True)
        raise SystemExit(
            f"download incomplete: got {read} of {total} bytes from {url}"
        )
    partial.replace(destination)
    print()
    return destination


def extract(archive: Path, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                # The archive is trusted input, but a path that escapes the target
                # is not worth trusting any archive with.
                target = (into / member.name).resolve()
                if not str(target).startswith(str(into.resolve())):
                    raise SystemExit(f"archive member escapes target: {member.name}")
            tar.extractall(into)
    elif archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(into)
    else:
        raise SystemExit(f"unsupported archive {archive.name}")


def stage_python(target: str, offline: Path | None) -> Path:
    asset, interpreter, _ = TARGETS[target]
    python_dir = RUNTIME / "python"
    # The archive's own root is `python/`, so the interpreter lands at
    # RUNTIME/python/bin/python3 — which is also the path backend.js resolves in a
    # packaged build. `python_dir / interpreter` would double that prefix and leave a
    # runtime the shell could never launch.
    marker = RUNTIME / interpreter
    if marker.exists():
        print(f"runtime already staged at {marker}")
        return marker
    if offline is not None:
        archive = offline
    else:
        archive = download(f"{PBS_BASE}/{asset}", ROOT / "dist" / "runtime-cache" / asset)
    if python_dir.exists():
        shutil.rmtree(python_dir)
    extract(archive, RUNTIME)
    if not marker.exists():
        raise SystemExit(f"expected interpreter at {marker} after extracting {archive.name}")
    return marker


def pip_install(python: Path, requirements: list[str]) -> None:
    """Install the runtime dependencies into the bundled interpreter itself."""
    probe = subprocess.run(
        [str(python), "-c", "import fastapi, uvicorn; print(fastapi.__version__)"],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        print(f"dependencies already present (fastapi {probe.stdout.strip()})")
        return
    print("installing runtime dependencies")
    command = [str(python), "-m", "pip", "install", "--no-warn-script-location", *requirements]
    result = subprocess.run(command)
    if result.returncode != 0:
        raise SystemExit("pip install failed; the bundled runtime is not usable")


def copy_app() -> None:
    """The application tree, minus caches and anything a previous stage left."""
    destination = RUNTIME / "app" / "actionmesh"
    if destination.exists():
        shutil.rmtree(destination)
    source = ROOT / "actionmesh"
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copy2(ROOT / "pyproject.toml", RUNTIME / "app" / "pyproject.toml")
    shutil.copy2(DESKTOP / "app" / "sidecar.py", RUNTIME / "app" / "sidecar.py")
    print(f"application staged at {destination}")


def write_manifest(target: str) -> None:
    import json

    version = subprocess.run(
        [str(RUNTIME / TARGETS[target][1]), "--version"], capture_output=True, text=True
    ).stdout.strip()
    manifest = {
        "target": target,
        "python": version,
        "standalone": True,
        "dependencies": RUNTIME_DEPS,
        "sidecar": "app/sidecar.py",
    }
    (RUNTIME / "runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"manifest: {version} for {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=host_target(), choices=sorted(TARGETS))
    parser.add_argument("--offline", type=Path, default=None,
                        help="use this archive instead of downloading")
    parser.add_argument("--skip-deps", action="store_true")
    args = parser.parse_args()

    if args.target != host_target() and args.target.startswith("macos") and sys.platform != "darwin":
        print(f"warning: staging {args.target} on {sys.platform}; the build host must run it", file=sys.stderr)

    interpreter = stage_python(args.target, args.offline)
    if not args.skip_deps and args.target == host_target():
        # A foreign-architecture interpreter cannot be executed on this host, so its
        # dependencies have to be installed by the runner that owns that platform.
        pip_install(interpreter, RUNTIME_DEPS)
    copy_app()
    write_manifest(args.target)
    digest = hashlib.sha256()
    for path in sorted(RUNTIME.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(RUNTIME)).encode())
    print(f"staged {sum(1 for p in RUNTIME.rglob('*') if p.is_file())} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())