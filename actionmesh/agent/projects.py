"""C19 project discovery: the folders under the workspace that look like code.

This is a *listing*, not a search of the machine: everything here is reached through
:class:`~actionmesh.agent.paths.Workspace`, so a project outside the configured root
does not exist as far as this module is concerned.

Two things it deliberately does not report. It does not read file contents while
looking for markers -- a directory that contains ``package.json`` is a Node project,
and opening every candidate's manifest to be sure would read a phone through the
whole tree. And it does not expose git *output*, only derived counts: the subject
lines of recent commits are the kind of thing that turns out to name a client, a
codename, or a person.
"""

from __future__ import annotations

import json
import os
from typing import Any

#: Marker file -> ecosystem. Order matters only for display: the first match is the
#: primary kind, the rest are listed alongside.
MARKERS: tuple[tuple[str, str], ...] = (
    (".git", "git"),
    ("pyproject.toml", "python"),
    ("package.json", "node"),
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("Gemfile", "ruby"),
    ("composer.json", "php"),
    ("Makefile", "make"),
)
MAX_PROJECTS = 200
#: How many directory levels below the root to look. One level covers `~/projects/*`;
#: two covers the monorepo-with-children case; more starts walking into vendored
#: trees that are not projects.
MAX_DEPTH = 2


def scan(workspace_root: str, *, limit: int = MAX_PROJECTS) -> list[dict[str, Any]]:
    """Projects under ``workspace_root``, shallowest first."""
    found: list[dict[str, Any]] = []
    _walk(workspace_root, "", 0, found, limit)
    return found


def _walk(
    root: str, relative: str, depth: int, out: list[dict[str, Any]], limit: int
) -> None:
    if depth > MAX_DEPTH or len(out) >= limit:
        return
    absolute = os.path.join(root, relative) if relative else root
    try:
        names = sorted(os.listdir(absolute))
    except OSError:
        return
    children: list[str] = []
    for name in names:
        if len(out) >= limit:
            return
        if name.startswith(".") and name != ".git":
            # A dot-directory is configuration, a cache, or someone's editor state.
            # Nothing that should be offered as "a project to open".
            continue
        child_relative = f"{relative}/{name}" if relative else name
        child_absolute = os.path.join(root, child_relative)
        if not os.path.isdir(child_absolute) or os.path.islink(child_absolute):
            continue
        kinds = kinds_of(child_absolute)
        if kinds:
            out.append(
                {
                    "name": name,
                    "path": child_relative,
                    "kind": kinds[0],
                    "kinds": list(kinds),
                    "scripts": scripts_of(child_absolute),
                }
            )
        children.append(child_relative)
    for child_relative in children:
        _walk(root, child_relative, depth + 1, out, limit)


def kinds_of(directory: str) -> tuple[str, ...]:
    """Ecosystem markers present in this directory, in :data:`MARKERS` order."""
    # `exists`, not `isfile`: the most important marker of all is `.git`, which is a
    # directory on every repository that has ever been cloned. A `isfile` check here
    # silently reports a real project as not one.
    present: list[str] = []
    for marker, kind in MARKERS:
        try:
            if os.path.exists(os.path.join(directory, marker)):
                present.append(kind)
        except OSError:
            continue
    return tuple(present)


def scripts_of(directory: str) -> list[str]:
    """Declared task names, from `package.json` scripts and `pyproject` entry points.

    Names only, never the command strings. `scripts.build` in a manifest is exactly
    the kind of field that contains a shell line, and a phone does not need to see
    it to be offered "build".
    """
    names: list[str] = []
    manifest = os.path.join(directory, "package.json")
    if os.path.isfile(manifest):
        try:
            with open(manifest, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict):
            names.extend(str(key) for key in scripts)
    project_toml = os.path.join(directory, "pyproject.toml")
    if os.path.isfile(project_toml):
        names.extend(_pyproject_scripts(project_toml))
    # Sorted and de-duplicated: a dashboard lists these, and a manifest can hold both.
    return sorted(set(names))


def _pyproject_scripts(path: str) -> list[str]:
    try:
        import tomllib

        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError, ImportError):
        return []
    project = data.get("project") if isinstance(data, dict) else None
    out: list[str] = []
    if isinstance(project, dict):
        scripts = project.get("scripts")
        if isinstance(scripts, dict):
            out.extend(str(key) for key in scripts)
    tool = data.get("tool", {}) if isinstance(data, dict) else {}
    poetry = tool.get("poetry", {}) if isinstance(tool, dict) else {}
    if isinstance(poetry, dict):
        scripts = poetry.get("scripts")
        if isinstance(scripts, dict):
            out.extend(str(key) for key in scripts)
    return out


__all__ = ["MARKERS", "MAX_DEPTH", "MAX_PROJECTS", "kinds_of", "scan", "scripts_of"]
