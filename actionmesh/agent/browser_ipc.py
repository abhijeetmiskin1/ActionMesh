"""Typed wire contract for the browser drive channel (C42).

The commands and observations exchanged between the laptop-side engine
(:mod:`actionmesh.agent.browser`) and the Electron main process that owns the
hardened window (``desktop/app/browser_window.js``) over the drive socket.
This module imports nothing from Electron and starts nothing — it defines the
closed set of messages both sides may send, so a malformed or unexpected frame
fails closed at parse time instead of reaching the web contents.

The wire is newline-delimited JSON, mirroring the provider bridge
(``desktop/app/provider_bridge.js``): one frame per line, one frame per
request, a length cap, a per-launch token on connect. Two doors guard it, the
same way the handler registry is guarded twice (``validate_definition`` at
definition, ``check_params`` at call): the engine validates arguments before
building a command, and ``parse_command`` validates again on the wire.

There is deliberately no field anywhere in this contract that accepts
arbitrary JavaScript, CSS selectors, XPath, HTML, shell text, Python, or
file paths. Drive by *named operation + opaque ref* only. The one string a
command carries that leaves the machine is the URL, and ``resolve_http_url``
is the only thing allowed to turn an address into bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

MAX_FRAME_BYTES = 256 * 1024
"""Same ceiling as the provider bridge: an observation is bounded or refused."""

SCROLL_DIRECTIONS = ("up", "down", "left", "right")
WAIT_UNTIL = ("load", "idle", "title", "elements")
OBSERVATION_KINDS = ("navigation", "title", "loading", "elements", "state")

_OP_NAMES = (
    "open",
    "navigate",
    "back",
    "forward",
    "refresh",
    "snapshot",
    "click",
    "type",
    "select",
    "scroll",
    "wait",
    "stop",
    "state",
    "page_text",
)

#: Ceiling for the visible-text read. Bounded and redacted; the same budget
#: that gates the ``browser.page`` transient stream so a read cannot become a
#: way to stream a whole document.
MAX_PAGE_TEXT_CHARS = 32 * 1024


class BrowserIpcError(ValueError):
    """A frame violates this contract. The receiver refuses and stops."""


def _one_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BrowserIpcError(f"{key} must be a non-empty string")
    return value


def _opt_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BrowserIpcError(f"{key} must be a string")
    return value


def _opt_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise BrowserIpcError(f"{key} must be a boolean")
    return value


def _opt_num(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrowserIpcError(f"{key} must be a number")
    return float(value)


# --------------------------------------------------------------------------
# Drive commands — exactly one per operation in the contract's browser.* set,
# plus the state read the engine polls. Frozen: nothing rewrites a command
# after it passed the engine's own validation.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenCommand:
    op: ClassVar[str] = "open"
    url: str
    request_id: str


@dataclass(frozen=True)
class NavigateCommand:
    op: ClassVar[str] = "navigate"
    url: str
    request_id: str


@dataclass(frozen=True)
class BackCommand:
    op: ClassVar[str] = "back"
    request_id: str


@dataclass(frozen=True)
class ForwardCommand:
    op: ClassVar[str] = "forward"
    request_id: str


@dataclass(frozen=True)
class RefreshCommand:
    op: ClassVar[str] = "refresh"
    request_id: str


@dataclass(frozen=True)
class SnapshotCommand:
    """Re-read the page's addressable elements and mint fresh refs."""

    op: ClassVar[str] = "snapshot"
    request_id: str
    max_elements: int = 200


@dataclass(frozen=True)
class ClickCommand:
    op: ClassVar[str] = "click"
    ref: int
    request_id: str
    double: bool = False


@dataclass(frozen=True)
class TypeCommand:
    """Text to insert into one already-addressed field. Never a password
    field — that refusal is the engine's, and the window enforces it too."""

    op: ClassVar[str] = "type"
    ref: int
    text: str
    request_id: str


@dataclass(frozen=True)
class SelectCommand:
    op: ClassVar[str] = "select"
    ref: int
    value: str
    request_id: str


@dataclass(frozen=True)
class ScrollCommand:
    op: ClassVar[str] = "scroll"
    direction: str
    request_id: str
    ref: int | None = None
    amount: float | None = None

    def __post_init__(self) -> None:
        if self.direction not in SCROLL_DIRECTIONS:
            raise BrowserIpcError(
                f"direction must be one of {SCROLL_DIRECTIONS}, got {self.direction!r}"
            )


@dataclass(frozen=True)
class WaitCommand:
    op: ClassVar[str] = "wait"
    request_id: str
    ms: int | None = None
    until: str | None = None

    def __post_init__(self) -> None:
        if self.until is not None and self.until not in WAIT_UNTIL:
            raise BrowserIpcError(f"until must be one of {WAIT_UNTIL}, got {self.until!r}")


@dataclass(frozen=True)
class StopCommand:
    """Halt browser work for this task: no further drive reaches the page."""

    op: ClassVar[str] = "stop"
    request_id: str
    reason: str = "stopped"


@dataclass(frozen=True)
class StateCommand:
    """Read back where the page is. Carries no code and changes nothing."""

    op: ClassVar[str] = "state"
    request_id: str


@dataclass(frozen=True)
class PageTextCommand:
    """Read the page's *visible* text, as the window's fixed reader returns
    it. The window never executes page-authored code: it reads a bounded,
    redacted text dump, and this command is a request for that dump, not for
    the page to supply its own words."""

    op: ClassVar[str] = "page_text"
    request_id: str
    limit: int | None = None


COMMAND_TYPES: tuple[type, ...] = (
    OpenCommand,
    NavigateCommand,
    BackCommand,
    ForwardCommand,
    RefreshCommand,
    SnapshotCommand,
    ClickCommand,
    TypeCommand,
    SelectCommand,
    ScrollCommand,
    WaitCommand,
    StopCommand,
    StateCommand,
    PageTextCommand,
)
_BY_OP: dict[str, type] = {cls.__dict__["op"]: cls for cls in COMMAND_TYPES}


def _scroll_direction(data: dict[str, Any]) -> str:
    value = data.get("direction")
    return value if isinstance(value, str) else "down"


def parse_command(line: str | bytes) -> Any:
    """Decode one drive frame. Unknown op, missing field, or an extra field
    is a refusal — never a best-effort guess."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line:
        raise BrowserIpcError("empty frame")
    if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
        raise BrowserIpcError(f"frame exceeds {MAX_FRAME_BYTES} bytes")
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise BrowserIpcError(f"malformed frame: {exc}") from exc
    if not isinstance(data, dict):
        raise BrowserIpcError("frame must be a JSON object")
    op = data.get("op")
    if op not in _BY_OP:
        raise BrowserIpcError(f"unknown op {op!r}")
    cls = _BY_OP[op]
    known = set(_FIELDS[cls])
    extra = set(data) - known - {"op"}
    if extra:
        raise BrowserIpcError(f"unexpected field(s) for {op}: {sorted(extra)}")

    def req_str(key: str) -> str:
        return _one_str(data, key)

    def req_ref(key: str = "ref") -> int:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise BrowserIpcError(f"{key} must be an integer ref")
        return value

    builders: dict[type, Any] = {
        OpenCommand: lambda: OpenCommand(req_str("url"), req_str("request_id")),
        NavigateCommand: lambda: NavigateCommand(req_str("url"), req_str("request_id")),
        BackCommand: lambda: BackCommand(req_str("request_id")),
        ForwardCommand: lambda: ForwardCommand(req_str("request_id")),
        RefreshCommand: lambda: RefreshCommand(req_str("request_id")),
        SnapshotCommand: lambda: SnapshotCommand(
            req_str("request_id"), int(data.get("max_elements") or 200)
        ),
        ClickCommand: lambda: ClickCommand(
            req_ref(), req_str("request_id"), _opt_bool(data, "double")
        ),
        TypeCommand: lambda: TypeCommand(
            req_ref(), req_str("text"), req_str("request_id")
        ),
        SelectCommand: lambda: SelectCommand(
            req_ref(), req_str("value"), req_str("request_id")
        ),
        ScrollCommand: lambda: ScrollCommand(
            _scroll_direction(data),
            req_str("request_id"),
            data.get("ref") if isinstance(data.get("ref"), int) else None,
            _opt_num(data, "amount"),
        ),
        WaitCommand: lambda: WaitCommand(
            req_str("request_id"),
            int(data["ms"]) if isinstance(data.get("ms"), (int, float)) else None,
            _opt_str(data, "until"),
        ),
        StopCommand: lambda: StopCommand(
            req_str("request_id"), _opt_str(data, "reason") or "stopped"
        ),
        StateCommand: lambda: StateCommand(req_str("request_id")),
        PageTextCommand: lambda: PageTextCommand(
            req_str("request_id"),
            int(data["limit"]) if isinstance(data.get("limit"), (int, float)) else None,
        ),
    }
    return builders[cls]()


def encode(frame: Any) -> str:
    """Serialize a command (or an observation) to one wire line."""
    payload = frame.to_wire() if hasattr(frame, "to_wire") else dict(frame)
    line = json.dumps(payload, separators=(",", ":"), default=str)
    if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
        raise BrowserIpcError(f"frame exceeds {MAX_FRAME_BYTES} bytes")
    return line


_FIELDS: dict[type, frozenset[str]] = {
    OpenCommand: frozenset({"url", "request_id"}),
    NavigateCommand: frozenset({"url", "request_id"}),
    BackCommand: frozenset({"request_id"}),
    ForwardCommand: frozenset({"request_id"}),
    RefreshCommand: frozenset({"request_id"}),
    SnapshotCommand: frozenset({"request_id", "max_elements"}),
    ClickCommand: frozenset({"ref", "request_id", "double"}),
    TypeCommand: frozenset({"ref", "text", "request_id"}),
    SelectCommand: frozenset({"ref", "value", "request_id"}),
    ScrollCommand: frozenset({"direction", "request_id", "ref", "amount"}),
    WaitCommand: frozenset({"request_id", "ms", "until"}),
    StopCommand: frozenset({"request_id", "reason"}),
    StateCommand: frozenset({"request_id"}),
    PageTextCommand: frozenset({"request_id", "limit"}),
}

# --------------------------------------------------------------------------
# Observations — what the window sends back. These are *facts about the page*,
# and the engine treats every string in them as untrusted data.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ElementRecord:
    """One addressable node, as the fixed DOM reader describes it.

    ``name`` is page-authored text: untrusted, and redacted before it is
    shown to anyone. ``password`` and ``blocked`` are flags the window sets;
    the window refuses to report a password field's contents at all, and
    ``value_len`` is a length, never a value.
    """

    ref: int
    role: str
    name: str = ""
    tag: str = ""
    disabled: bool = False
    password: bool = False
    value_len: int = 0
    href_host: str = ""

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> ElementRecord:
        if not isinstance(data, dict):
            raise BrowserIpcError("element record must be an object")
        ref = data.get("ref")
        if isinstance(ref, bool) or not isinstance(ref, int):
            raise BrowserIpcError("element ref must be an integer")
        return cls(
            ref=ref,
            role=str(data.get("role") or ""),
            name=str(data.get("name") or ""),
            tag=str(data.get("tag") or ""),
            disabled=bool(data.get("disabled")),
            password=bool(data.get("password")),
            value_len=max(0, int(data.get("value_len") or 0)),
            href_host=str(data.get("href_host") or ""),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "role": self.role,
            "name": self.name,
            "tag": self.tag,
            "disabled": self.disabled,
            "password": self.password,
            "value_len": self.value_len,
            "href_host": self.href_host,
        }


@dataclass(frozen=True)
class Observation:
    """One page fact: kind plus a small, fixed payload."""

    kind: str
    url: str = ""
    title: str = ""
    loading: bool | None = None
    epoch: int | None = None
    elements: tuple[ElementRecord, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.kind not in OBSERVATION_KINDS:
            raise BrowserIpcError(
                f"kind must be one of {OBSERVATION_KINDS}, got {self.kind!r}"
            )

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> Observation:
        if not isinstance(data, dict):
            raise BrowserIpcError("observation must be an object")
        raw_elements = data.get("elements") or []
        if not isinstance(raw_elements, list):
            raise BrowserIpcError("elements must be a list")
        return cls(
            kind=str(data.get("kind") or ""),
            url=str(data.get("url") or ""),
            title=str(data.get("title") or ""),
            loading=(None if data.get("loading") is None else bool(data["loading"])),
            epoch=(None if data.get("epoch") is None else int(data["epoch"])),
            elements=tuple(ElementRecord.from_wire(item) for item in raw_elements),
            truncated=bool(data.get("truncated")),
        )

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind}
        if self.url:
            payload["url"] = self.url
        if self.title:
            payload["title"] = self.title
        if self.loading is not None:
            payload["loading"] = self.loading
        if self.epoch is not None:
            payload["epoch"] = self.epoch
        if self.elements:
            payload["elements"] = [element.to_wire() for element in self.elements]
        if self.truncated:
            payload["truncated"] = True
        return payload


@dataclass(frozen=True)
class CommandResult:
    """The window's reply to one command. ``status`` mirrors the contract's
    browser status set; ``note`` is a fixed reason string, never page HTML."""

    request_id: str
    status: str
    epoch: int | None = None
    url: str = ""
    title: str = ""
    changed: bool | None = None
    note: str = ""
    #: Character-count growth the drive captured AT THE MOMENT OF THE WRITE
    #: (before/after around the DOM mutation), never a later re-read. None
    #: when the window did not report it, which must read as "not witnessed",
    #: not as "nothing was inserted".
    inserted: int | None = None
    elements: tuple[ElementRecord, ...] = ()
    truncated: bool = False
    #: The page's visible text, for a ``page_text`` reply. It rides this
    #: field so the existing wire shape stays backward compatible: a reader
    #: that does not know ``page_text`` simply ignores it. The reader on the
    #: window side already redacts credential-shaped material before
    #: returning it; the engine re-redacts regardless.
    text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    loading: bool = False
    requested_url: str = ""

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"request_id": self.request_id, "status": self.status}
        if self.epoch is not None:
            payload["epoch"] = self.epoch
        if self.url:
            payload["url"] = self.url
        if self.title:
            payload["title"] = self.title
        if self.loading:
            payload["loading"] = True
        if self.requested_url:
            payload["requested_url"] = self.requested_url
        if self.changed is not None:
            payload["changed"] = self.changed
        if self.inserted is not None:
            payload["inserted"] = self.inserted
        if self.note:
            payload["note"] = self.note
        if self.elements:
            payload["elements"] = [element.to_wire() for element in self.elements]
        if self.truncated:
            payload["truncated"] = True
        if self.text:
            payload["text"] = self.text
        return payload

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> CommandResult:
        if not isinstance(data, dict):
            raise BrowserIpcError("result must be an object")
        if "loading" in data and not isinstance(data["loading"], bool):
            raise BrowserIpcError("loading must be a boolean")
        if "requested_url" in data and not isinstance(data["requested_url"], str):
            raise BrowserIpcError("requested_url must be a string")
        raw_elements = data.get("elements") or []
        return cls(
            request_id=str(data.get("request_id") or ""),
            status=str(data.get("status") or ""),
            epoch=(None if data.get("epoch") is None else int(data["epoch"])),
            url=str(data.get("url") or ""),
            title=str(data.get("title") or ""),
            loading=data.get("loading") is True,
            requested_url=str(data.get("requested_url") or "")[:2048],
            changed=(None if data.get("changed") is None else bool(data["changed"])),
            note=str(data.get("note") or ""),
            inserted=(None if data.get("inserted") is None else int(data["inserted"])),
            elements=tuple(ElementRecord.from_wire(item) for item in raw_elements),
            truncated=bool(data.get("truncated")),
            text=str(data.get("text") or ""),
        )


__all__ = [
    "MAX_FRAME_BYTES",
    "MAX_PAGE_TEXT_CHARS",
    "OBSERVATION_KINDS",
    "SCROLL_DIRECTIONS",
    "WAIT_UNTIL",
    "BackCommand",
    "BrowserIpcError",
    "ClickCommand",
    "CommandResult",
    "ElementRecord",
    "ForwardCommand",
    "NavigateCommand",
    "Observation",
    "OpenCommand",
    "PageTextCommand",
    "RefreshCommand",
    "ScrollCommand",
    "SelectCommand",
    "SnapshotCommand",
    "StateCommand",
    "StopCommand",
    "TypeCommand",
    "WaitCommand",
    "encode",
    "parse_command",
]
