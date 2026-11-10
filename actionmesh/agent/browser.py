"""Browser drive for ActionMesh (C42) — one controlled session, named
operations only.

Backend choice, and why
----------------------
The session is a **hardened Electron ``BrowserWindow`` owned by the desktop
main process** (``desktop/app/browser_window.js``), reached from this engine
over a local newline-delimited-JSON bridge whose frames are the typed
commands and observations in :mod:`actionmesh.agent.browser_ipc`. This is the
``actionmesh`` web-pages partition FAMILY with the same hardening
(``sandbox: true``, ``contextIsolation: true``, ``webSecurity: true``, no
preload on the web contents, never ``--no-sandbox``) — deliberately a sibling
partition (``actionmesh-web-drive``) rather than the exact
``actionmesh-web-pages`` session partition, because that one attaches the
loopback API key to every request to ``127.0.0.1`` and untrusted web pages
must not be able to read it. See the lead report; the deviation is an
extra hardening step, not a weakening.

What we did NOT do: launch a second browser stack (Playwright/Chrome binary
via a driver) — that would be a second browser system, which the contract
forbids (the desktop shell already IS the browser host). Nor does anything
here accept model-supplied JavaScript: every drive is one of the closed set
of named operations, and element addressing goes through **opaque refs**
minted by ``snapshot``/``inspect``, never through selectors, XPath, or
injected scripts. The window side reads a fixed, bounded DOM summary.

Rules this module enforces (contract §2.1, §4, §13):
- Page content is untrusted DATA. Every string that came from a page
  (titles, element names, visible text, href hosts) is redacted for
  credential-shaped material and length-capped before it is returned,
  emitted on a transient stream, or logged. A website's own text never
  reaches the engine as an instruction.
- Login / CAPTCHA / OTP / MFA screens are **user-handled handoffs**: the
  engine detects the gate, returns ``status:"needs_user"`` with a reason,
  and refuses to interact until a page without a gate is reached. It never
  fills a password field and never attempts to bypass a challenge.
- ``search`` is read-only: it navigates a fixed engine allowlist URL and
  reads result links. It never clicks anything — certainly never a
  "buy"/"subscribe" control.
- ``stop`` halts browser actions for this task: after it, every operation
  reports ``unavailable`` until a fresh ``open``.

Statuses are the contract's closed set:
``"ok" | "stale" | "needs_user" | "unavailable" | "error"``.

``stale`` (ref unknown, ref from an earlier page epoch, or the element has
disappeared) is a normal answer, never a crash. ``error`` means the call
violated the operation contract before it was attempted (bad URL scheme,
unknown scroll direction, unknown wait condition) or the driver answered
malformed.

Transient streams emitted when an ``OutputPublisher`` is installed (same
``output_callback()`` channel the provider uses to stream content):
``browser.elements`` (one whole JSON document per snapshot — registered
whole-document in ``core/output.py``), ``browser.title``, ``browser.url``,
``browser.page`` (bounded, redacted visible text). Streams carry counts and
redacted text only — never credentials, never raw HTML.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlsplit

from actionmesh.core.output import output_callback

from .browser_bridge import BrowserBridgeUnavailable
from .browser_ipc import (
    SCROLL_DIRECTIONS,
    WAIT_UNTIL,
    BackCommand,
    ClickCommand,
    CommandResult,
    ForwardCommand,
    NavigateCommand,
    OpenCommand,
    PageTextCommand,
    RefreshCommand,
    ScrollCommand,
    SelectCommand,
    SnapshotCommand,
    StateCommand,
    StopCommand,
    TypeCommand,
    WaitCommand,
    encode,
)

#: The contract's closed status set for browser operations.
STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_NEEDS_USER = "needs_user"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"
#: The drive found the element and read it back, but the write did nothing
#: observable (nothing landed in the field). The same refusal the media
#: engine uses: an unconfirmed effect must never read as ok.
STATUS_NO_EFFECT = "no_effect"

MAX_SNAPSHOT_ELEMENTS = 200
#: Refs handed back in one snapshot's ``refs`` list — bounded; the full
#: redacted record list goes to the transient stream.
MAX_REFS_RETURNED = 50
MAX_RESULTS = 10
MAX_PAGE_TEXT_CHARS = 6000
MAX_FIELD_NAME_CHARS = 200
MAX_WAIT_MS = 10_000
DEFAULT_WAIT_MS = 500

#: Search is read-only and goes to one of these three engines, nothing else.
SEARCH_ENGINES = {
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "google": "https://www.google.com/search?q={query}",
    "bing": "https://www.bing.com/search?q={query}",
}
DEFAULT_SEARCH_ENGINE = "duckduckgo"

# Handoff gates. A page presenting any of these is handed to the user; the
# engine reports and stops. This is a denylist of *controls we refuse to
# drive*, not a way to classify pages.
_GATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("captcha", re.compile(r"captcha|recaptcha|human verification|are you a robot", re.I)),
    ("otp", re.compile(r"\bone[\s-]?time\b|\botp\b|verification code|\bsms code\b", re.I)),
    ("mfa", re.compile(r"two[\s-]?factor|\bmfa\b|authenticator", re.I)),
    ("login", re.compile(r"sign in|log in|login|password required", re.I)),
)

# Credential-shaped material is stripped from every page-derived string
# before it is returned, emitted, or recorded. These are deliberately coarse:
# the point is that a secret can't ride a stream, not that it parses cleanly.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer\s+)[a-z0-9._\-]{8,}"), r"\1<redacted>"),
    (re.compile(r"(?i)\b(api[\s_-]?key|access[\s_-]?token|secret|token)\b(\s*[:=]\s*)\S+"), r"\1\2<redacted>"),
    (re.compile(r"(?i)\b(password|passwd|passphrase)\b(\s*[:=]\s*)\S+"), r"\1\2<redacted>"),
    (re.compile(r"(?i)\b(otp|mfa[\s_-]?code|verification[\s_-]?code)\b(\s*[:=]\s*)\S+"), r"\1\2<redacted>"),
    # 13-19 digit runs with optional spaces/dashes: card-shaped numbers.
    (re.compile(r"\b(?:\d[ \-]?){12,18}\d\b"), "<redacted-card?>"),
)


def _redact(text: Any, limit: int = MAX_FIELD_NAME_CHARS) -> str:
    """Page content is untrusted data: cap length, strip credential-shaped
    material, flatten control characters. Applied to EVERY string this module
    returns or emits that originated in a page."""
    raw = str(text or "")
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", raw)
    out = raw
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    if len(out) > limit:
        # The cap is a promise about the length of what a caller receives. Appending the
        # ellipsis *after* the slice would break that promise by one character; the rest
        # of this package (`voiceos/diagnosis.py`) spends one character of the budget on
        # the marker instead, so `len(_redact(x, limit=n)) <= n` holds for every n.
        out = out[: max(limit - 1, 0)] + "…"
    return out


def _detect_gate(*texts: str) -> str | None:
    """Return the handoff reason for a page presenting login/CAPTCHA/OTP/MFA,
    scanning only redacted, capped page facts. Presence of the gate stops all
    interaction; nothing here ever tries to satisfy it."""
    joined = " | ".join(texts)
    if not joined:
        return None
    safe = _redact(joined, limit=4000)
    for reason, pattern in _GATE_PATTERNS:
        if pattern.search(safe):
            return reason
    return None


def _search_results(records: list[dict[str, Any]], page_url: str) -> list[dict[str, str]]:
    """Read-only results for a search page: the link anchors on the page.

    ``href_host`` is the only link data the snapshot carries (there is no
    URL beyond the host, by design), and its provenance here is still the
    page's own hrefs — page-authored text, filtered (the gate) and bounded
    (``MAX_RESULTS``), never a claim of rank or endorsement.

    A search results page lists the engine's own chrome (home, language,
    ads) *before* the results, so anchors on the page's own host are the
    engine's chrome and are not results.
    """
    engine_host = urlsplit(page_url or "").hostname or ""
    results: list[dict[str, str]] = []
    for item in records:
        if item.get("role") != "link" or not item.get("href_host"):
            continue
        if item["href_host"] == engine_host:
            continue
        results.append({"title": item["name"], "link": item["href_host"]})
    return results[:MAX_RESULTS]


def _is_bridge_unavailable(exc: Exception) -> bool:
    """True when a driver call failed because the bridge could not be reached at
    all, as opposed to a refusal, a malformed reply, or a window error.

    The bridge client names this failure with its own exception type; matching
    by that type keeps a future rename loud (import fails) rather than silent
    (every outage reading as a generic error again)."""
    return isinstance(exc, BrowserBridgeUnavailable)


def _http_url(url: Any) -> str | None:
    """Only absolute http(s) may reach the window. Anything else — file://,
    javascript:, data:, relative — is refused before dispatch."""
    text = str(url or "").strip()
    if not text or len(text) > 4096:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return text


@dataclass(frozen=True)
class PageFacts:
    """What the driver reports about the live page. All strings here are
    page-authored and untrusted; consumers must pass them through ``_redact``
    before returning or emitting."""

    url: str = "about:blank"
    title: str = ""
    epoch: int = 0
    loading: bool = False


class BrowserDriver:
    """The minimal contract any backend must satisfy — the closed set of
    physical reads and drives, each taking a driver-local element id (the
    engine maps opaque session refs onto these). No method accepts a selector,
    a script, a shell command, or a path.

    ``snapshot_elements`` returns ``(epoch, records)`` where each record is a
    plain dict with keys ``id, tag, role, name, href_host, disabled,
    password, value_len`` — a bounded, structured summary, never raw HTML.
    """

    async def open(self, url: str) -> PageFacts:
        raise NotImplementedError

    async def navigate(self, url: str) -> PageFacts:
        raise NotImplementedError

    async def go_back(self) -> PageFacts:
        raise NotImplementedError

    async def go_forward(self) -> PageFacts:
        raise NotImplementedError

    async def reload(self) -> PageFacts:
        raise NotImplementedError

    async def snapshot_elements(self, max_elements: int = MAX_SNAPSHOT_ELEMENTS) -> tuple[int, list[dict[str, Any]]]:
        raise NotImplementedError

    async def page_text(self, limit: int = MAX_PAGE_TEXT_CHARS) -> str:
        raise NotImplementedError

    async def click(self, element_id: int, double: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    async def type_text(self, element_id: int, text: str) -> dict[str, Any]:
        raise NotImplementedError

    async def select(self, element_id: int, value: str) -> dict[str, Any]:
        raise NotImplementedError

    async def scroll(self, direction: str, element_id: int | None = None, amount: float | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def wait_for(self, ms: int | None = None, until: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def stop(self, reason: str = "stopped") -> dict[str, Any]:
        raise NotImplementedError

    async def state(self) -> PageFacts:
        raise NotImplementedError


class FakeDriver(BrowserDriver):
    """A scripted, in-memory page for unit tests. No network, no Electron, no
    Chrome — the tests in ``tests/unit/test_browser_agent.py`` run against
    this driver, and nothing in this sprint claims otherwise.

    Tests script pages with :meth:`add_page` (elements + visible text), then
    inspect :attr:`actions` — every drive the engine performed, so proofs
    like "search never clicked" are direct. Navigation bumps ``epoch``, which
    is what makes old refs stale; :meth:`remove_element` makes an element
    vanish under a still-current epoch, which must also read as stale.
    Password fields report only ``value_len``; their stored value never
    leaves the driver.
    """

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        self.history: list[str] = []
        self.history_index: int = -1
        self.epoch: int = 0
        self.current_url: str = "about:blank"
        self.current_title: str = ""
        self.loading: bool = False
        self.stopped: bool = False
        self.actions: list[dict[str, Any]] = []
        self._ids: dict[str, dict[Any, int]] = {}
        self._next_id = 1
        # Real sleeping is deliberately absent: the fake driver answers at once
        # unless a test raises this to exercise an awaited path.
        self.sleep_scale: float = 0.0
        self.text_limit: int = MAX_PAGE_TEXT_CHARS

    # -- scripting ---------------------------------------------------------

    def add_page(
        self,
        url: str,
        title: str,
        elements: list[dict[str, Any]],
        text: str = "",
    ) -> None:
        """Register a scriptable page. Each element dict may carry
        ``tag, name, href, disabled, password, value``; everything else the
        snapshot reports is derived."""
        ids: dict[Any, int] = {}
        records = []
        for index, spec in enumerate(elements):
            element_id = self._next_id
            self._next_id += 1
            ids[index] = element_id
            records.append(
                {
                    "tag": str(spec.get("tag") or "div"),
                    "name": str(spec.get("name") or ""),
                    "href": str(spec.get("href") or ""),
                    "disabled": bool(spec.get("disabled")),
                    "password": bool(spec.get("password")),
                    "value": str(spec.get("value") or ""),
                }
            )
        self.pages[url] = {"title": title, "elements": records, "text": text, "ids": ids}
        self._ids[url] = ids

    def remove_element(self, url: str, index: int) -> None:
        """Make an element vanish under the *same* epoch — refs to it must
        come back stale, not crash."""
        page = self.pages.get(url)
        if page and 0 <= index < len(page["elements"]):
            page["elements"][index] = None  # type: ignore[index]

    # -- driver contract ----------------------------------------------------

    def _facts(self) -> PageFacts:
        return PageFacts(
            url=self.current_url,
            title=self.current_title,
            epoch=self.epoch,
            loading=self.loading,
        )

    def _load(self, url: str) -> None:
        page = self.pages.get(url)
        self.history = self.history[: self.history_index + 1]
        self.history.append(url)
        self.history_index = len(self.history) - 1
        self.current_url = url
        self.current_title = page["title"] if page else urlsplit(url).hostname or url
        self.epoch += 1
        self.loading = False

    async def open(self, url: str) -> PageFacts:
        self.actions.append({"action": "open", "url": url})
        await asyncio.sleep(0)
        self.stopped = False
        self._load(url)
        return self._facts()

    async def navigate(self, url: str) -> PageFacts:
        self.actions.append({"action": "navigate", "url": url})
        await asyncio.sleep(0)
        self._load(url)
        return self._facts()

    async def go_back(self) -> PageFacts:
        self.actions.append({"action": "back"})
        await asyncio.sleep(0)
        if self.history_index > 0:
            self.history_index -= 1
            self._set_to(self.history[self.history_index])
        return self._facts()

    async def go_forward(self) -> PageFacts:
        self.actions.append({"action": "forward"})
        await asyncio.sleep(0)
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self._set_to(self.history[self.history_index])
        return self._facts()

    def _set_to(self, url: str) -> None:
        page = self.pages.get(url)
        self.current_url = url
        self.current_title = page["title"] if page else urlsplit(url).hostname or url
        self.epoch += 1

    async def reload(self) -> PageFacts:
        self.actions.append({"action": "reload"})
        await asyncio.sleep(0)
        self.epoch += 1
        return self._facts()

    async def snapshot_elements(self, max_elements: int = MAX_SNAPSHOT_ELEMENTS) -> tuple[int, list[dict[str, Any]]]:
        self.actions.append({"action": "snapshot"})
        await asyncio.sleep(0)
        page = self.pages.get(self.current_url)
        records: list[dict[str, Any]] = []
        if page:
            for index, element in enumerate(page["elements"]):
                if element is None:
                    continue
                records.append(
                    {
                        "id": page["ids"][index],
                        "tag": element["tag"],
                        "role": _role_for(element["tag"], element),
                        "name": element["name"],
                        "href_host": urlsplit(element["href"]).hostname or "" if element["href"] else "",
                        "disabled": element["disabled"],
                        "password": element["password"],
                        # A length, never the value — even for password fields.
                        "value_len": len(element["value"]),
                    }
                )
        return self.epoch, records[: max(1, max_elements)]

    async def page_text(self, limit: int = MAX_PAGE_TEXT_CHARS) -> str:
        self.actions.append({"action": "page_text"})
        page = self.pages.get(self.current_url)
        return (page["text"] if page else "")[:limit]

    async def click(self, element_id: int, double: bool = False) -> dict[str, Any]:
        self.actions.append({"action": "click", "id": element_id, "double": double})
        await asyncio.sleep(0)
        element, _ = self._find(element_id)
        if element is None:
            return {"found": False}
        if element["disabled"]:
            return {"found": True, "changed": False, "refused": "disabled"}
        changed = True
        navigated_to = ""
        if element["tag"] == "a" and element["href"]:
            navigated_to = element["href"]
            self._load(element["href"])
        elif element["tag"] == "button":
            element["value"] = (element["value"] or "") + "|pressed"
        return {"found": True, "changed": changed, "navigated_to": navigated_to}

    async def type_text(self, element_id: int, text: str) -> dict[str, Any]:
        # Never log or echo `text` itself — only what the drive did to counts.
        self.actions.append({"action": "type", "id": element_id, "chars": len(text)})
        await asyncio.sleep(0)
        element, _ = self._find(element_id)
        if element is None:
            return {"found": False}
        if element["password"]:
            return {"found": True, "refused": "password_field"}
        before = len(element["value"])
        element["value"] = (element["value"] + text)[:4000]
        return {"found": True, "changed": len(element["value"]) > before, "inserted": len(element["value"]) - before, "value_len": len(element["value"])}

    async def select(self, element_id: int, value: str) -> dict[str, Any]:
        self.actions.append({"action": "select", "id": element_id, "value_len": len(value)})
        await asyncio.sleep(0)
        element, _ = self._find(element_id)
        if element is None:
            return {"found": False}
        changed = element["value"] != value
        element["value"] = value
        return {"found": True, "changed": changed}

    async def scroll(self, direction: str, element_id: int | None = None, amount: float | None = None) -> dict[str, Any]:
        self.actions.append({"action": "scroll", "direction": direction, "id": element_id})
        await asyncio.sleep(0)
        if element_id is not None:
            element, _ = self._find(element_id)
            if element is None:
                return {"found": False}
        return {"found": True, "moved": True}

    async def wait_for(self, ms: int | None = None, until: str | None = None) -> dict[str, Any]:
        bounded = max(0, min(int(ms if ms is not None else DEFAULT_WAIT_MS), MAX_WAIT_MS))
        self.actions.append({"action": "wait", "ms": bounded, "until": until})
        # A fake driver must not spend real wall-clock time; the scale exists
        # so the awaited path is genuinely async while staying instant in tests.
        if self.sleep_scale:
            await asyncio.sleep(bounded * self.sleep_scale / 1000.0)
        if until == "load":
            self.loading = False
        return {"ok": True, "loading": self.loading}

    async def stop(self, reason: str = "stopped") -> dict[str, Any]:
        self.actions.append({"action": "stop", "reason": reason})
        self.stopped = True
        self.loading = False
        return {"ok": True}

    async def state(self) -> PageFacts:
        return self._facts()

    def _find(self, element_id: int) -> tuple[dict[str, Any] | None, int]:
        page = self.pages.get(self.current_url)
        if not page:
            return None, -1
        for index, stored_id in page["ids"].items():
            if stored_id == element_id:
                element = page["elements"][index]
                return element, index
        return None, -1


def _role_for(tag: str, element: dict[str, Any]) -> str:
    tag = tag.lower()
    if tag == "input":
        return "textbox"
    if tag == "select":
        return "combobox"
    if tag == "a":
        # The search result extractor keys on this exact word, and the bridge driver
        # reports anchors the same way, so the two drivers agree on what a link is.
        return "link"
    if tag in ("button", "form", "textarea"):
        return {"textarea": "textbox"}.get(tag, tag)
    if tag.startswith("h"):
        return "heading"
    return "generic"


class BridgeDriver(BrowserDriver):
    """The real adapter — clearly marked, wired by the integration lead.

    It talks the typed frames of :mod:`actionmesh.agent.browser_ipc` to
    ``desktop/app/browser_window.js`` through an injected ``request`` function
    (the lead supplies the bridge client; this module never imports Electron
    and never opens the socket itself). Every command is built from a frozen
    dataclass and length-capped by ``encode``; every reply is parsed into
    fixed fields — a malformed reply is an error, never a guess. This class
    contains no execution of page-authored text: the DOM summary is produced
    by the window's fixed reader, and drives arrive as named operations.
    """

    def __init__(self, request: Callable[[str], Awaitable[dict[str, Any]]]) -> None:
        self._request = request
        self._element_epoch = 0

    async def _drive(self, command: Any) -> CommandResult:
        payload = dict(command.__dict__)
        payload["op"] = command.op
        reply = await self._request(encode(payload))
        return CommandResult.from_wire(reply)

    def _facts(self, result: CommandResult) -> PageFacts:
        if result.epoch is not None:
            self._element_epoch = result.epoch
        return PageFacts(url=result.url, title=result.title, epoch=self._element_epoch)

    async def open(self, url: str) -> PageFacts:
        return self._facts(await self._drive(OpenCommand(url=url, request_id="open")))

    async def navigate(self, url: str) -> PageFacts:
        return self._facts(await self._drive(NavigateCommand(url=url, request_id="navigate")))

    async def go_back(self) -> PageFacts:
        return self._facts(await self._drive(BackCommand(request_id="back")))

    async def go_forward(self) -> PageFacts:
        return self._facts(await self._drive(ForwardCommand(request_id="forward")))

    async def reload(self) -> PageFacts:
        return self._facts(await self._drive(RefreshCommand(request_id="refresh")))

    async def snapshot_elements(self, max_elements: int = MAX_SNAPSHOT_ELEMENTS) -> tuple[int, list[dict[str, Any]]]:
        result = await self._drive(SnapshotCommand(request_id="snapshot", max_elements=max_elements))
        self._element_epoch = result.epoch or self._element_epoch
        # The driver contract (see ``BrowserDriver``) promises records keyed by
        # ``id`` — the driver-local element id the engine maps opaque refs
        # onto. ``ElementRecord.to_wire`` instead reports the window-local
        # stamp as ``ref`` (the IPC/Observation shape), so rename it here.
        records = []
        for element in result.elements:
            wire = element.to_wire()
            wire["id"] = wire.pop("ref")
            records.append(wire)
        return self._element_epoch, records

    async def page_text(self, limit: int = MAX_PAGE_TEXT_CHARS) -> str:
        bounded = max(1, min(int(limit), MAX_PAGE_TEXT_CHARS))
        result = await self._drive(PageTextCommand(request_id="page_text", limit=bounded))
        if result.status != STATUS_OK:
            return ""
        return (result.text or "")[:bounded]

    async def click(self, element_id: int, double: bool = False) -> dict[str, Any]:
        result = await self._drive(ClickCommand(ref=element_id, request_id="click", double=double))
        return {"found": result.status == STATUS_OK, "status": result.status, "changed": bool(result.changed), "navigated_to": result.url if result.changed else ""}

    async def type_text(self, element_id: int, text: str) -> dict[str, Any]:
        result = await self._drive(TypeCommand(ref=element_id, text=text, request_id="type"))
        if result.note == "password_field":
            return {"found": True, "refused": "password_field"}
        inserted = result.inserted if result.inserted is not None else None
        return {"found": result.status == STATUS_OK, "status": result.status, "changed": bool(result.changed), "inserted": inserted}

    async def select(self, element_id: int, value: str) -> dict[str, Any]:
        result = await self._drive(SelectCommand(ref=element_id, value=value, request_id="select"))
        return {"found": result.status == STATUS_OK, "status": result.status, "changed": bool(result.changed)}

    async def scroll(self, direction: str, element_id: int | None = None, amount: float | None = None) -> dict[str, Any]:
        result = await self._drive(ScrollCommand(direction=direction, request_id="scroll", ref=element_id, amount=amount))
        return {"found": result.status == STATUS_OK, "status": result.status}

    async def wait_for(self, ms: int | None = None, until: str | None = None) -> dict[str, Any]:
        result = await self._drive(WaitCommand(request_id="wait", ms=ms, until=until))
        return {"ok": result.status == STATUS_OK}

    async def stop(self, reason: str = "stopped") -> dict[str, Any]:
        result = await self._drive(StopCommand(request_id="stop", reason=reason))
        return {"ok": result.status == STATUS_OK}

    async def state(self) -> PageFacts:
        return self._facts(await self._drive(StateCommand(request_id="state")))


class BrowserEngine:
    """One controlled browser session, driven only by the named operations
    below — one method per capability in contract §2.1. Each returns a plain
    dict whose ``status`` is one of
    ``"ok" | "stale" | "needs_user" | "unavailable" | "no_effect" | "error"``.

    Element refs are opaque integers, session-scoped and monotonic, minted by
    ``snapshot``/``inspect``. A ref is valid only while its page epoch lives:
    any navigation or DOM-mutating action invalidates the whole map, and a ref
    that is unknown, from an earlier epoch, or whose element has since
    disappeared answers ``stale`` — never a crash, never a fallback guess.
    """

    def __init__(self, driver: BrowserDriver | None = None) -> None:
        self._driver = driver if driver is not None else FakeDriver()
        self._open = False
        self._gate: str | None = None
        self._next_ref = 1
        #: ref -> (epoch at mint, driver element id)
        self._refs: dict[int, tuple[int, int]] = {}
        self._last_facts = PageFacts()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _unavailable(self, note: str = "no browser session — call open first") -> dict[str, Any]:
        return {"status": STATUS_UNAVAILABLE, "reason": note}

    def _bridge_unavailable(self, note: str = "the browser bridge is unreachable") -> dict[str, Any]:
        return {"status": STATUS_UNAVAILABLE, "reason": note}

    def _needs_user(self, reason: str) -> dict[str, Any]:
        return {
            "status": STATUS_NEEDS_USER,
            "reason": reason,
            "note": "a login/CAPTCHA/OTP/MFA control was detected; it must be handled by the user. No attempt was made to bypass it.",
        }

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def gate(self) -> str | None:
        return self._gate

    async def _refresh_facts(self) -> PageFacts:
        self._last_facts = await self._driver.state()
        return self._last_facts

    async def _rebuild_refs(self, max_elements: int = MAX_SNAPSHOT_ELEMENTS) -> tuple[int, list[dict[str, Any]], bool]:
        """Re-read the page, mint a fresh ref map (old refs are dead), scan
        the (redacted) facts for a handoff gate, and emit the transient
        streams. Returns (count, redacted records, truncated)."""
        epoch, records = await self._driver.snapshot_elements(max_elements)
        # Navigation or action since the driver's read: keep epoch honest.
        facts = await self._refresh_facts()
        self._refs.clear()  # any rebuild invalidates every previous ref
        safe: list[dict[str, Any]] = []
        for record in records[:max_elements]:
            ref = self._next_ref
            self._next_ref += 1
            try:
                element_id = int(record["id"])  # type: ignore[typeddict-item]
            except (TypeError, ValueError, KeyError):
                continue
            self._refs[ref] = (epoch, element_id)
            name = _redact(record.get("name"))
            safe.append(
                {
                    "ref": ref,
                    "role": _redact(record.get("role"), limit=40),
                    "tag": _redact(record.get("tag"), limit=40),
                    "name": name,
                    "href_host": _redact(record.get("href_host"), limit=200),
                    "disabled": bool(record.get("disabled")),
                    "password": bool(record.get("password")),
                    "value_len": int(record.get("value_len") or 0),
                }
            )
        truncated = len(records) > len(safe)
        self._gate = _detect_gate(
            facts.title,
            *[item["name"] for item in safe],
            *[item["role"] for item in safe],
        )
        self._emit_streams(facts, safe, truncated)
        return len(safe), safe, truncated

    def _emit_streams(self, facts: PageFacts, records: list[dict[str, Any]], truncated: bool) -> None:
        emit = output_callback()
        if emit is None:
            return
        try:
            emit("browser.url", _redact(facts.url, limit=2048))
            emit("browser.title", _redact(facts.title))
            document = json.dumps(
                {"url": _redact(facts.url, limit=2048), "count": len(records), "truncated": truncated, "elements": records},
                default=str,
            )
            emit("browser.elements", document)
        except Exception:
            # A stream failure must never fail the operation that produced it.
            pass

    async def _emit_page_text(self) -> None:
        emit = output_callback()
        if emit is None:
            return
        try:
            text = await self._driver.page_text(MAX_PAGE_TEXT_CHARS)
            emit("browser.page", _redact(text, limit=MAX_PAGE_TEXT_CHARS))
        except Exception:
            pass

    async def _resolve(self, ref: Any) -> tuple[int | None, dict[str, Any] | None]:
        """Map an opaque ref to its driver id. ``None, None`` = stale."""
        try:
            key = int(ref)
        except (TypeError, ValueError):
            return None, {"status": STATUS_STALE, "reason": "ref must be an integer"}
        mapped = self._refs.get(key)
        if mapped is None:
            return None, {"status": STATUS_STALE, "reason": "unknown ref — take a fresh snapshot"}
        epoch, element_id = mapped
        try:
            facts = await self._refresh_facts()
        except BrowserBridgeUnavailable:
            return None, self._bridge_unavailable()
        except Exception:
            return None, {"status": STATUS_ERROR, "reason": "the page could not be read"}
        if epoch != facts.epoch:
            return None, {"status": STATUS_STALE, "reason": "ref is from an earlier page — take a fresh snapshot"}
        return element_id, None

    async def _refused_by_gate(self) -> dict[str, Any] | None:
        if self._gate:
            return self._needs_user(self._gate)
        try:
            facts = await self._refresh_facts()
        except BrowserBridgeUnavailable:
            return self._bridge_unavailable()
        except Exception:
            return {"status": STATUS_ERROR, "reason": "the page could not be read"}
        gate = _detect_gate(facts.title)
        if gate:
            self._gate = gate
            return self._needs_user(gate)
        return None

    def _page_result(self, facts: PageFacts) -> dict[str, Any]:
        return {"status": STATUS_OK, "url": _redact(facts.url, limit=2048), "title": _redact(facts.title)}

    # ------------------------------------------------------------------
    # The named operations — contract §2.1
    # ------------------------------------------------------------------

    async def open(self, url: str) -> dict[str, Any]:
        target = _http_url(url)
        if target is None:
            return {"status": STATUS_ERROR, "reason": "url must be absolute http(s)"}
        try:
            facts = await self._driver.open(target)
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "the browser window refused the page"}
        self._open = True
        self._last_facts = facts
        try:
            count, _, _ = await self._rebuild_refs()
        except BrowserBridgeUnavailable:
            return self._bridge_unavailable()
        except Exception:
            return {"status": STATUS_ERROR, "reason": "the page could not be read"}
        if self._gate:
            result = self._needs_user(self._gate)
            result.update({"url": _redact(facts.url, limit=2048), "title": _redact(facts.title)})
            return result
        return {"status": STATUS_OK, "url": _redact(facts.url, limit=2048), "title": _redact(facts.title), "ref_map_size": count}

    async def navigate(self, url: str) -> dict[str, Any]:
        if not self._open:
            return self._unavailable()
        target = _http_url(url)
        if target is None:
            return {"status": STATUS_ERROR, "reason": "url must be absolute http(s)"}
        try:
            facts = await self._driver.navigate(target)
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "navigation failed"}
        self._last_facts = facts
        try:
            await self._rebuild_refs()
        except BrowserBridgeUnavailable:
            return self._bridge_unavailable()
        except Exception:
            return {"status": STATUS_ERROR, "reason": "the page could not be read"}
        if self._gate:
            result = self._needs_user(self._gate)
            result.update({"url": _redact(facts.url, limit=2048), "title": _redact(facts.title)})
            return result
        return self._page_result(facts)

    async def search(self, query: str, engine: str | None = None) -> dict[str, Any]:
        """Read-only web search: navigate a fixed allowlist engine URL, read
        the result links, return them. It NEVER clicks a result, and a search
        can never book, order, buy, or subscribe anything — those controls
        belong to a different, higher-risk action that this module has no
        name for."""
        if not self._open:
            return self._unavailable()
        refusal = await self._refused_by_gate()
        if refusal:
            return refusal
        text = str(query or "").strip()
        if not text or len(text) > 512:
            return {"status": STATUS_ERROR, "reason": "query must be 1..512 characters"}
        name = (str(engine or DEFAULT_SEARCH_ENGINE).strip().lower())
        template = SEARCH_ENGINES.get(name)
        if template is None:
            return {"status": STATUS_ERROR, "reason": f"engine must be one of {sorted(SEARCH_ENGINES)}"}
        target = template.format(query=quote_plus(text))
        try:
            facts = await self._driver.navigate(target)
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "navigation to the search engine failed"}
        self._last_facts = facts
        try:
            _, records, _ = await self._rebuild_refs()
        except BrowserBridgeUnavailable:
            return self._bridge_unavailable()
        except Exception:
            return {"status": STATUS_ERROR, "reason": "the page could not be read"}
        if self._gate:  # the SERP itself asked to log in / prove humanity
            result = self._needs_user(self._gate)
            result["url"] = _redact(facts.url, limit=2048)
            return result
        results = _search_results(records, facts.url)
        return {
            "status": STATUS_OK,
            "url": _redact(facts.url, limit=2048),
            "count": len(results),
            "results": results,
        }

    async def inspect(self) -> dict[str, Any]:
        """Bounded page summary; the full (redacted) element document goes to
        the transient ``browser.elements`` stream, the count is what returns."""
        if not self._open:
            return self._unavailable()
        try:
            facts = await self._refresh_facts()
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "the page could not be read"}
        try:
            count, _, truncated = await self._rebuild_refs()
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "the page could not be read"}
        await self._emit_page_text()
        result: dict[str, Any] = {
            "status": STATUS_OK,
            "url": _redact(facts.url, limit=2048),
            "title": _redact(facts.title),
            "element_count": count,
            "truncated": truncated,
        }
        if self._gate:
            result["needs_user"] = self._gate
        return result

    async def snapshot(self) -> dict[str, Any]:
        """Mint the ref map for the current page. The returned ``refs`` list
        is bounded; the full redacted records ride the transient stream."""
        if not self._open:
            return self._unavailable()
        try:
            facts = await self._refresh_facts()
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "the page could not be read"}
        try:
            count, records, _ = await self._rebuild_refs()
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "the page could not be read"}
        await self._emit_page_text()
        result: dict[str, Any] = {
            "status": STATUS_OK,
            "url": _redact(facts.url, limit=2048),
            "title": _redact(facts.title),
            "refs": [item["ref"] for item in records][:MAX_REFS_RETURNED],
            "count": count,
        }
        if self._gate:
            result["needs_user"] = self._gate
        return result

    async def click(self, ref: int, double: bool = False) -> dict[str, Any]:
        if not self._open:
            return self._unavailable()
        refusal = await self._refused_by_gate()
        if refusal:
            return refusal
        element_id, stale = await self._resolve(ref)
        if stale:
            return stale
        if element_id is None:
            return {"status": STATUS_STALE, "reason": "the element is gone — take a fresh snapshot"}
        try:
            outcome = await self._driver.click(element_id, bool(double))
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "the click could not be performed"}
        if not outcome.get("found"):
            if outcome.get("status") == STATUS_NEEDS_USER:
                return self._needs_user("login")
            if outcome.get("status") == STATUS_UNAVAILABLE:
                return self._bridge_unavailable()
            if outcome.get("status") == STATUS_ERROR:
                return {"status": STATUS_ERROR, "reason": "the element is gone — take a fresh snapshot"}
            return {"status": STATUS_STALE, "reason": "the element is gone — take a fresh snapshot"}
        try:
            facts = await self._refresh_facts()
            if outcome.get("navigated_to"):
                await self._rebuild_refs()
        except BrowserBridgeUnavailable:
            return self._bridge_unavailable()
        except Exception:
            return {"status": STATUS_ERROR, "reason": "the page could not be read"}
        if outcome.get("navigated_to"):
            pass
        else:
            self._refs.clear()  # any DOM-mutating action invalidates refs
            self._gate = _detect_gate(facts.title)
        result: dict[str, Any] = {
            "status": STATUS_OK,
            "url_after": _redact(facts.url, limit=2048),
            "changed": bool(outcome.get("changed")),
        }
        if self._gate:
            return self._merged_needs_user(result)
        return result

    async def type_(self, ref: int, text: str) -> dict[str, Any]:
        """Type into one addressed field. The reply reports whether the
        character count grew and by how much — never the text itself, which
        is not echoed, logged, streamed, or recorded anywhere here. Password
        fields are refused outright; credentials are a user-handled handoff."""
        if not self._open:
            return self._unavailable()
        refusal = await self._refused_by_gate()
        if refusal:
            return refusal
        if not isinstance(text, str) or len(text) > 4000:
            return {"status": STATUS_ERROR, "reason": "text must be a string up to 4000 characters"}
        element_id, stale = await self._resolve(ref)
        if stale:
            return stale
        if element_id is None:
            return {"status": STATUS_STALE, "reason": "the element is gone — take a fresh snapshot"}
        try:
            outcome = await self._driver.type_text(element_id, text)
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "the text could not be entered"}
        if not outcome.get("found"):
            if outcome.get("status") == STATUS_NEEDS_USER:
                return self._needs_user("login")
            if outcome.get("status") == STATUS_UNAVAILABLE:
                return self._bridge_unavailable()
            if outcome.get("status") == STATUS_ERROR:
                return {"status": STATUS_ERROR, "reason": "the element is gone — take a fresh snapshot"}
            return {"status": STATUS_STALE, "reason": "the element is gone — take a fresh snapshot"}
        if outcome.get("refused") == "password_field":
            self._gate = "login"
            return self._needs_user("login")
        self._refs.clear()  # typing mutates the DOM
        # A drive that found the field but landed nothing is not a success:
        # ``ok`` here would be recorded as the verifier's witness, and the
        # verifier would confirm a write that never happened. None (the
        # window did not witness a count) also refuses — an unwitnessed
        # write is unconfirmed, never ok-by-default.
        if not outcome.get("changed") or not outcome.get("inserted"):
            return {
                "status": STATUS_NO_EFFECT,
                "changed": False,
                "inserted": outcome.get("inserted"),
                "reason": "nothing was entered — the drive read the field back unchanged",
            }
        return {
            "status": STATUS_OK,
            "changed": bool(outcome.get("changed")),
            # The count itself, or None when the drive did not witness it. The
            # engine never substitutes 0 for unwitnessed: downstream consumers
            # (observe/state) read None as "not a witness", never as "nothing
            # landed".
            "inserted": outcome.get("inserted"),
        }

    async def select(self, ref: int, value: str) -> dict[str, Any]:
        if not self._open:
            return self._unavailable()
        refusal = await self._refused_by_gate()
        if refusal:
            return refusal
        if not isinstance(value, str) or len(value) > 1024:
            return {"status": STATUS_ERROR, "reason": "value must be a string up to 1024 characters"}
        element_id, stale = await self._resolve(ref)
        if stale:
            return stale
        if element_id is None:
            return {"status": STATUS_STALE, "reason": "the element is gone — take a fresh snapshot"}
        try:
            outcome = await self._driver.select(element_id, value)
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "the selection could not be made"}
        if not outcome.get("found"):
            if outcome.get("status") == STATUS_NEEDS_USER:
                return self._needs_user("login")
            if outcome.get("status") == STATUS_UNAVAILABLE:
                return self._bridge_unavailable()
            if outcome.get("status") == STATUS_ERROR:
                return {"status": STATUS_ERROR, "reason": "the element is gone — take a fresh snapshot"}
            return {"status": STATUS_STALE, "reason": "the element is gone — take a fresh snapshot"}
        self._refs.clear()
        # A select that lands on the value already there is a confirmed
        # no-op, not a silent success: it stays unrecorded so observe() and
        # the verifier see an honest refusal instead of an effect.
        if not outcome.get("changed"):
            return {
                "status": STATUS_NO_EFFECT,
                "changed": False,
                "reason": "the field already held this value — nothing changed",
            }
        return {"status": STATUS_OK, "changed": bool(outcome.get("changed"))}

    async def scroll(self, ref: int | None = None, direction: str = "down", amount: float | None = None) -> dict[str, Any]:
        if not self._open:
            return self._unavailable()
        refusal = await self._refused_by_gate()
        if refusal:
            return refusal
        if direction not in SCROLL_DIRECTIONS:
            return {"status": STATUS_ERROR, "reason": f"direction must be one of {SCROLL_DIRECTIONS}"}
        element_id: int | None = None
        if ref is not None:
            resolved, stale = await self._resolve(ref)
            if stale:
                return stale
            element_id = int(resolved) if resolved is not None else None
        bounded_amount = None
        if amount is not None:
            try:
                bounded_amount = max(1.0, min(float(amount), 10_000.0))
            except (TypeError, ValueError):
                return {"status": STATUS_ERROR, "reason": "amount must be a number"}
        try:
            outcome = await self._driver.scroll(direction, element_id, bounded_amount)
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "the scroll could not be performed"}
        if not outcome.get("found"):
            if outcome.get("status") == STATUS_NEEDS_USER:
                return self._needs_user("login")
            if outcome.get("status") == STATUS_UNAVAILABLE:
                return self._bridge_unavailable()
            if outcome.get("status") == STATUS_ERROR:
                return {"status": STATUS_ERROR, "reason": "the element is gone — take a fresh snapshot"}
            return {"status": STATUS_STALE, "reason": "the element is gone — take a fresh snapshot"}
        return {"status": STATUS_OK}

    async def back(self) -> dict[str, Any]:
        return await self._history_op("go_back")

    async def forward(self) -> dict[str, Any]:
        return await self._history_op("go_forward")

    async def refresh(self) -> dict[str, Any]:
        return await self._history_op("reload")

    async def _history_op(self, which: str) -> dict[str, Any]:
        if not self._open:
            return self._unavailable()
        try:
            facts = await getattr(self._driver, which)()
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": f"{which} failed"}
        self._last_facts = facts
        try:
            await self._rebuild_refs()
        except BrowserBridgeUnavailable:
            return self._bridge_unavailable()
        except Exception:
            return {"status": STATUS_ERROR, "reason": "the page could not be read"}
        if self._gate:
            return self._merged_needs_user(self._page_result(facts))
        return self._page_result(facts)

    async def wait(self, ms: int | None = None, until: str | None = None) -> dict[str, Any]:
        if not self._open:
            return self._unavailable()
        if ms is not None:
            try:
                ms = int(ms)
            except (TypeError, ValueError):
                return {"status": STATUS_ERROR, "reason": "ms must be an integer"}
            if ms < 0 or ms > MAX_WAIT_MS:
                return {"status": STATUS_ERROR, "reason": f"ms must be 0..{MAX_WAIT_MS}"}
        if until is not None and until not in WAIT_UNTIL:
            return {"status": STATUS_ERROR, "reason": f"until must be one of {WAIT_UNTIL}"}
        try:
            await self._driver.wait_for(ms, until)
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "the wait failed"}
        try:
            facts = await self._refresh_facts()
        except BrowserBridgeUnavailable:
            return self._bridge_unavailable()
        except Exception:
            return {"status": STATUS_ERROR, "reason": "the page could not be read"}
        return self._page_result(facts)

    async def stop(self) -> dict[str, Any]:
        """Halt this task's browser actions. Every later operation reports
        ``unavailable`` until a fresh ``open``."""
        if not self._open:
            return self._unavailable("no browser session")
        try:
            await self._driver.stop("halted by task")
        except Exception as exc:
            if _is_bridge_unavailable(exc):
                self._open = False
                self._gate = None
                self._refs.clear()
                return self._bridge_unavailable()
            return {"status": STATUS_ERROR, "reason": "stop failed"}
        self._open = False
        self._gate = None
        self._refs.clear()
        return {"status": STATUS_OK}

    def _merged_needs_user(self, base: dict[str, Any]) -> dict[str, Any]:
        handoff = self._needs_user(self._gate or "login")
        for key in ("url", "url_after", "title"):
            if key in base:
                handoff[key] = base[key]
        return handoff


__all__ = [
    "MAX_SNAPSHOT_ELEMENTS",
    "MAX_WAIT_MS",
    "SEARCH_ENGINES",
    "STATUS_ERROR",
    "STATUS_NEEDS_USER",
    "STATUS_NO_EFFECT",
    "STATUS_OK",
    "STATUS_STALE",
    "STATUS_UNAVAILABLE",
    "BridgeDriver",
    "BrowserDriver",
    "BrowserEngine",
    "FakeDriver",
    "PageFacts",
]
