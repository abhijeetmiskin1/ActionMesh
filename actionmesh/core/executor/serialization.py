"""The safe serialization boundary between a provider's raw result and a client.

A laptop provider's ``data`` is whatever the real action produced: for a file read
that is the file's contents, for a terminal command the whole stdout and stderr.
None of that belongs in an event stream or a result payload that a phone can read --
and it is not merely a privacy question, because the entire point of the platform is
that the *phone* is the untrusted side.

So nothing here is a denylist. A value reaches a client only if its key is on the
allowlist below and it is a small scalar. Everything else -- nested structures, long
strings, and every key that could carry file or command output -- is dropped. The
result is a metadata view: enough to tell a user *what happened* (a file at
``src/app.py`` was written, 42 bytes, exit code 0, output truncated) without handing
them the file or the output.
"""

from __future__ import annotations

from typing import Any

#: Provider-result keys that describe an outcome without carrying its content.
#: Anything not listed here is never serialized to a client.
SAFE_RESULT_KEYS = frozenset(
    {
        "path",
        "relative",
        "bytes",
        "size",
        "count",
        "total",
        "truncated",
        "exit_code",
        "deleted",
        "written",
        "ok",
        "status",
        "state",
        "name",
        "kind",
        "label",
        "modified",
        "untracked",
        "changed",
        "branch",
        "running",
        "capacity",
        "ready",
        "listening",
        "port",
        "found",
        "exists",
        "is_dir",
        "is_file",
        "binary",
        "closed",
        "opened",
        "stopped",
        "pid_alive",
        "version",
        "version_known",
        "simulated",
        # Which limit stopped this run, and whether the agent's ceiling rather than the
        # caller's request was the binding one. Diagnostics about the boundary, never
        # the program's output.
        "timeout_requested_s",
        "timeout_applied_s",
        "timeout_clipped",
        # Whether anything is reading this process's output at all.
        "output_captured",
        # Inventory fields. A process cannot be stopped by anything but the id this
        # agent issued it, so a list of processes is useless to a client unless the id
        # and the basics of each row survive.
        "process_id",
        "command",
        "working_directory",
        "started_at",
        "ide",
        # Listing rows: what kind of entry it is, and whether it escapes the root.
        # (`name`, `path`, `size` and `modified` were already carried.)
        "type",
        "confined",
        # The inventory containers. They are only ever emitted as projected rows (or,
        # if a provider returns something that is not a list of dicts, as a count) --
        # listing them here is what lets the projection branch be reached at all.
        "processes",
        "servers",
        "projects",
        "editors",
        "services",
        "entries",
        # C38: the *shape* of a screen observation. "the OS declined the capture",
        # "1920x1080", "the tree had 41 elements", "the walk hit its cap" are facts
        # about the boundary, not about the screen. Element names, element bounds and
        # image bytes are the screen's content and are deliberately absent from this
        # list: they travel the transient channel to the one observer already allowed
        # to watch the execution (see `actionmesh.core.output`), and a result payload
        # that a paired phone could read never carries them.
        "session",
        "portal_code",
        "retained",
        "element_count",
        "reason",
        "width",
        "height",
        "format",
        # C38 GUI actions. The same rule as the screen keys above: what a client needs
        # to know is *whether the machine did the thing*, never what was on the screen.
        # `role` and `name` identify the element that was targeted -- the same target
        # the caller itself named in the request, so it discloses nothing it did not
        # already hold. `detail` is the adapter's sentence, bounded by MAX_SCALAR_STRING
        # like every other string here. Deliberately absent: `observed` (element state
        # and geometry), `anchor_bounds` (geometry), `offset` (a nested dict, dropped by
        # the projection, and is a scroll position not a fact about a client's work),
        # and `text` -- which a type action must never inflict on a result that a
        # different device could read.
        "role",
        "dispatched",
        "typed",
        "element_changed",
        "moved",
        "detail",
        # Media read-back. The engine only surfaces fields a real MPRIS
        # read-back produced (player id, track title capped by
        # MAX_SCALAR_STRING, volume level, mute flag, positions). Track text
        # is whatever the player reports — metadata, never audio bytes — and
        # a name the caller itself asked to open is not a disclosure.
        "player",
        "track",
        "level",
        "muted",
        "position_s",
        "duration_s",
        "players",
        "candidates",
        "requested_level",
    }
)

#: A scalar string is only carried when it is short. Long strings are, in practice,
#: the content the allowlist exists to keep out.
MAX_SCALAR_STRING = 120
#: How many rows an inventory list may carry. A device with 5,000 processes is either
#: real and hostile or broken and hostile; either way the event stays small.
MAX_LIST_ROWS = 20

#: Keys whose value is a list of *inventory rows* rather than a payload. A row is
#: re-projected through the same allowlist, one level deep and no deeper. Every other
#: key keeps the old rule: a list is summarized to a count and never walked.
PROJECTED_LIST_KEYS = frozenset(
    {
        "processes",
        "servers",
        "projects",
        "editors",
        "services",
        # A directory listing. The names are the point of the capability, which is
        # confined to the workspace, consent-gated, and audited; the *body* of any file
        # they name still cannot come through here.
        "entries",
    }
)


def _project_row(row: dict[str, Any]) -> dict[str, Any]:
    """One inventory row, filtered by the same allowlist and kept flat.

    Only scalars survive. A list or a dict inside a row is dropped rather than
    summarized, because "one level deep, never two" is a rule a reviewer can check at a
    glance — and a nested dict is exactly where a payload has always hidden.
    """
    projected: dict[str, Any] = {}
    for key, value in row.items():
        if key not in SAFE_RESULT_KEYS:
            continue
        if isinstance(value, (bool, int, float)) or (
            isinstance(value, str) and len(value) <= MAX_SCALAR_STRING
        ):
            projected[key] = value
    return projected


def safe_result_view(data: Any) -> dict[str, Any]:
    """Project a provider result onto the small, non-content fields a client may see.

    Nested values are dropped rather than recursed into: a dict under a safe key is
    exactly where a payload hides. The one exception is a list, which is summarized by
    its length under a ``*_count`` key so "12 files listed" survives without the list.
    """
    if not isinstance(data, dict):
        return {}
    view: dict[str, Any] = {}
    for key, value in data.items():
        if key not in SAFE_RESULT_KEYS:
            continue
        if (
            key in PROJECTED_LIST_KEYS
            and isinstance(value, list)
            and value
            and all(isinstance(row, dict) for row in value)
        ):
            view[key] = [_project_row(row) for row in value[:MAX_LIST_ROWS]]
        # bool is a subclass of int, so this carries flags (truncated, deleted) and
        # counts alike; a nested dict is dropped, never recursed into.
        elif isinstance(value, (bool, int, float)):
            view[key] = value
        elif isinstance(value, str):
            if len(value) <= MAX_SCALAR_STRING:
                view[key] = value
        elif isinstance(value, list):
            view[f"{key}_count"] = len(value)
    return view


__all__ = [
    "MAX_LIST_ROWS",
    "MAX_SCALAR_STRING",
    "PROJECTED_LIST_KEYS",
    "SAFE_RESULT_KEYS",
    "safe_result_view",
]
