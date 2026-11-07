"""C38 screen awareness: real desktop capture and real accessibility inspection.

Two machines are reached here, and both are reached *for real* — nothing in this
module is simulated, and nothing it cannot do is pretended to be done.

**Screen capture goes through the xdg-desktop-portal.** On Wayland an application may
not grab the framebuffer directly; the compositor owns it. The only sanctioned path is
the portal's ``org.freedesktop.portal.Screenshot`` method, which asks the compositor's
screenshot UI for the user's per-capture consent. That is the whole point of routing
through it rather than shelling out: a capture is a *gated, per-call* consent, and a
user who declines is reported as a refusal (``cancelled``), not as an empty image and
not as a success. A machine that shows no prompt because it is headless answers the
same way — ``cancelled`` — which is the honest answer to "did the screen get captured",
not a guess dressed up as one.

**GUI inspection walks the AT-SPI tree.** The accessibility bus (``Atspi``) is the
machine's own description of what is on screen — application, window, element role,
label and screen-space bounds. It is the native layer ``device.gui.*`` is built on,
deliberately preferred over raw coordinates because a label and a role are evidence an
element *is* what the caller thinks it is in a way a pixel position never is. Every
field the tree cannot supply (a role the app did not expose, an element with no
geometry) is reported as missing, because an element the tree cannot describe is an
element that cannot be safely targeted.

Both halves share one rule: **the result says what happened, and only what happened.**
A capture that the user declined is ``{"status": "cancelled"}``. A GUI inspection of a
tree that has no actionable element in the asked-for window is an empty list, not a
"close enough" one.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

#: Screen-space coordinates AT-SPI reports when it has none. They are the GLib sentinel
#: for "no geometry", and a caller that saw them as a real position would aim a click at
#: the corner of the negative quadrant of a screen that has no such corner.
_NO_GEOMETRY = (-(2**31), -(2**31))

#: How long one capture waits for the portal to answer. The prompt is the user deciding,
#: and a decision takes real time, but a prompt that never resolves is a capture that
#: never happens and must not hold an execution open.
_CAPTURE_TIMEOUT_S = 30.0
#: How deep the tree is walked in one inspection. Deeper is more complete and more
#: expensive; a window a user points at is a few layers in. Bounded on purpose — an
#: unbounded walk of a deep application is a way to freeze the agent on a machine
#: because it was asked to describe it.
_MAX_DEPTH = 8
#: Elements one inspection returns, in document order. A window has a few dozen; a
#: whole desktop has a few thousand. One call describes one window.
_MAX_ELEMENTS = 200
#: How many matching elements a GUI action will tolerate before refusing. Exactly one
#: is the only safe answer: "the button called Save" identifies a control when there is
#: one of them, and identifies nothing when three windows each have one.
_MAX_MATCHES = 1

#: Action names that mean "activate this element", in the vocabulary the toolkits on
#: this machine actually publish. AT-SPI's own name is ``click``; GTK publishes ``Tap``
#: for touch-shaped controls; Chromium publishes ``doDefault``. A name outside this set
#: (``menu.popup``, ``selection.select-all``, ``clipboard.copy``) is a different
#: operation, and invoking it because it happened to be first would be the agent doing
#: something nobody asked for.
_CLICK_ACTIONS = ("click", "tap", "press", "activate", "doDefault")

#: The scroll edge directions, mapped onto AT-SPI's own enum at call time.
_SCROLL_EDGES = ("top", "bottom", "left", "right", "anywhere")


# -- the gi boundary -----------------------------------------------------
def ensure_gi_importable() -> None:
    """Make the system PyGObject visible to this interpreter, if the launcher says how.

    A virtualenv keeps its own site-packages closed on purpose. ``gi`` is the exception
    that has no honest alternative: Debian ships it as a compiled extension bound to the
    system GLib and AT-SPI, and there is no wheel that would work inside an isolated
    venv. The desktop launcher therefore reports the directory in
    ``ACTIONMESH_SYSTEM_PACKAGES`` — and only for a development runtime, where the
    machine's own desktop stack *is* the thing being exercised.

    The directory is **appended**, never prepended. Entries later in ``sys.path`` only
    fill gaps: every package the venv already installed still wins, so a system `click`
    or `typing_extensions` cannot shadow the one the server is running on.

    A packaged runtime sets no such variable and gets nothing extra, which is the
    boundary: what ships is exactly what runs.
    """
    import sys

    extra = (os.environ.get("ACTIONMESH_SYSTEM_PACKAGES") or "").strip()
    if not extra or not os.path.isdir(extra):
        return
    if extra in sys.path:
        return
    sys.path.append(extra)


def _gi_available() -> bool:
    """Whether this process can even import the desktop APIs.

    ``gi`` (PyGObject) is not part of ActionMesh's own dependencies; on the desktop it
    is reached through the system packages via ``PYTHONPATH``. Checking here — and
    failing with a named, honest reason — is what keeps a host without it from
    advertising a capability it cannot run.
    """
    ensure_gi_importable()
    try:
        import gi  # noqa: F401
    except ImportError:
        return False
    return True


def _gi_unavailable() -> dict[str, Any]:
    return {
        "reason": "gi_unavailable",
        "detail": (
            "PyGObject (gi) is not importable in this process, so the desktop's "
            "screenshot portal and accessibility tree cannot be reached"
        ),
    }


def _desktop_session() -> str | None:
    """Which compositor session this is. Wayland and X11 are different capture worlds."""
    value = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if value:
        return value
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return None


def _portal_available() -> bool:
    """Whether the xdg-desktop-portal Screenshot interface is reachable.

    Probed by asking the session bus whether the name has an owner, not by issuing a
    capture: a probe that triggers a screenshot prompt in order to find out whether it
    can take screenshots has already taken one. Name ownership is the honest,
    side-effect-free version of "the portal is there".
    """
    ensure_gi_importable()
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError):
        return False
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if bus is None:
            return False
        reply = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", ("org.freedesktop.portal.Desktop",)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        return bool(reply.unpack()[0])
    except Exception:
        return False


def _atspi_available() -> bool:
    """Whether the accessibility bus is up and describing anything."""
    ensure_gi_importable()
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError):
        return False
    try:
        Atspi.init()
        desktop = Atspi.get_desktop(0)
        return desktop is not None
    except Exception:
        return False


# -- screen capture ------------------------------------------------------
def capture_screenshot(timeout_s: float = _CAPTURE_TIMEOUT_S) -> dict[str, Any]:
    """Ask the portal for one screenshot and report exactly what it answered.

    Blocking: it runs a GLib main loop until the portal's ``Response`` signal arrives or
    the deadline passes. Call it from a worker thread, not the event loop.

    The return never contains an image. It contains the file URI the portal gave back
    and the shape of the file at that URI — which is all a verification and a preview
    can honestly say about a frame it has not itself chosen to keep. The URI is a
    per-capture, access-controlled path the compositor issued; it is not copied into
    storage and it is not logged, so nothing here retains a frame beyond its own life.
    """
    if not _gi_available():
        return {"status": "unavailable", **_gi_unavailable()}
    session = _desktop_session()
    if session is None:
        return {
            "status": "unavailable",
            "reason": "no_desktop_session",
            "detail": "no graphical session is attached to this process",
        }
    if not _portal_available():
        return {
            "status": "unavailable",
            "reason": "no_portal",
            "detail": (
                f"the xdg-desktop-portal Screenshot interface is not reachable in "
                f"this {session} session, so the screen cannot be captured"
            ),
        }

    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    loop = GLib.MainLoop()
    box: dict[str, Any] = {}

    def on_response(
        _conn: Any,
        _sender: Any,
        _path: Any,
        _iface: Any,
        name: str,
        params: Any,
        _user_data: Any = None,
    ) -> None:
        if name != "Response":
            return
        code, out = params.unpack()
        # 0 is success; every other code is a refusal or a fault, and the difference
        # matters: a user who declined is not an error, and neither is a portal that is
        # absent — but all of them are "no image", and the status says which one.
        if code == 0:
            box["ok"] = True
            box["uri"] = out.get("uri", "")
        else:
            box["ok"] = False
            box["code"] = code
        loop.quit()

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    token = f"actionmesh_{uuid4().hex}"
    sender = bus.get_unique_name()[1:].replace(".", "_")
    expected_handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    def subscribe(handle: str) -> int:
        return int(bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            handle,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
        ))

    subscription = subscribe(expected_handle)
    timer = 0
    try:
        result = bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Screenshot",
            "Screenshot",
            GLib.Variant("(sa{sv})", ("", {
                "handle_token": GLib.Variant("s", token),
                "modal": GLib.Variant("b", True),
                "interactive": GLib.Variant("b", False),
            })),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            int(timeout_s * 1000),
            None,
        )
        handle = result.unpack()[0]
        if handle != expected_handle:
            old_subscription = subscription
            subscription = subscribe(handle)
            bus.signal_unsubscribe(old_subscription)
        timer = GLib.timeout_add_seconds(int(max(timeout_s, 1)), loop.quit)
        loop.run()
    except GLib.Error:
        return {
            "status": "unavailable",
            "reason": "portal_call_failed",
            "detail": "the OS screenshot portal could not start the capture",
        }
    finally:
        bus.signal_unsubscribe(subscription)
        if timer and box:
            GLib.source_remove(timer)

    if not box.get("ok"):
        code = box.get("code", -1)
        return {
            "status": "cancelled",
            "session": session,
            "portal_code": code,
            "detail": _portal_refusal_detail(code),
        }

    uri = box.get("uri")
    file = _describe_file(uri) if isinstance(uri, str) else {}
    if not file.get("format"):
        return {
            "status": "unavailable",
            "reason": "invalid_portal_frame",
            "detail": "the screenshot portal returned no readable image",
        }
    return {
        "status": "captured",
        "session": session,
        "uri": uri,
        "file": file,
        # The bytes are not returned and not kept: a frame is shown in the moment it is
        # captured and then the only record of it is that it happened and how large it
        # was. Retaining it would be storing a screenshot, which this capability does
        # not do.
        "retained": False,
    }


def _portal_refusal_detail(code: int) -> str:
    """The portal's refusal code as a sentence, using the portal's own vocabulary."""
    if code == 1:
        return "the screenshot request was cancelled"
    if code == 2:
        return "the OS ended the screenshot request without granting a capture"
    if code == -1:
        return "the screenshot portal did not respond before the deadline"
    return "the screenshot request was not granted"


def _describe_file(uri: str) -> dict[str, Any]:
    """The file at a ``file://`` URI, by its own shape. Empty when it is not readable."""
    try:
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc or not parsed.path.startswith("/"):
            return {}
        path = unquote(parsed.path)
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            head = handle.read(64)
    except (OSError, ValueError):
        return {}
    return _decode_image(head, size)


def _decode_image(head: bytes, size: int) -> dict[str, Any]:
    """What kind of image these bytes are, and how big, from the header alone.

    A header is a promise about the bytes, not a description of their contents, and the
    two are kept apart: the file's dimensions are reported only when the header says
    what they are, and never read off the pixels, which would be interpreting a screen
    the agent has no right to.
    """
    info: dict[str, Any] = {"size": size}
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        width = int.from_bytes(head[16:20], "big")
        height = int.from_bytes(head[20:24], "big")
        info.update(format="png", width=width, height=height)
    elif head.startswith(b"\xff\xd8\xff"):
        info.update(format="jpeg")
    elif head.startswith(b"GIF8"):
        info.update(format="gif")
        if len(head) >= 10:
            info.update(
                width=int.from_bytes(head[6:8], "little"),
                height=int.from_bytes(head[8:10], "little"),
            )
    elif head[:4] in (b"RIFF", b"WebP"):
        info.update(format="webp")
    else:
        info.update(format="unknown")
    return info


# -- GUI inspection ------------------------------------------------------
@dataclass
class _Node:
    role: str
    name: str
    depth: int
    bounds: tuple[int, int, int, int] | None
    actions: list[str]
    enabled: bool | None
    index: int  # one-based path within its parent, the only stable address an AT-SPI node has


def _node_from_atspi(app: Any, index: int, depth: int, counter: dict[str, int]) -> _Node | None:
    try:
        node = app.get_child_at_index(index)
    except Exception:
        return None
    if node is None:
        return None
    counter["n"] += 1
    role = _safe(node.get_role_name, "")
    name = _safe(node.get_name, "")
    bounds = _extents(node)
    actions = _action_names(node)
    enabled = _is_enabled(node)
    return _Node(
        role=role or "unknown",
        name=(name or "")[:120],
        depth=depth,
        bounds=bounds,
        actions=actions,
        enabled=enabled,
        index=index + 1,
    )


def _safe(caller: Any, default: Any) -> Any:
    try:
        value = caller()
    except Exception:
        return default
    return default if value is None else value


def _extents(node: Any) -> tuple[int, int, int, int] | None:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    try:
        ext = node.get_extents(Atspi.CoordType.SCREEN)
    except Exception:
        return None
    if (ext.x, ext.y) == _NO_GEOMETRY or ext.width <= 0 or ext.height <= 0:
        return None
    return (ext.x, ext.y, ext.width, ext.height)


def _action_names(node: Any) -> list[str]:
    try:
        count = node.get_n_actions()
    except Exception:
        return []
    out: list[str] = []
    for i in range(count):
        try:
            out.append(node.get_action_description(i) or f"action{i}")
        except Exception:
            out.append(f"action{i}")
    return out


def _is_enabled(node: Any) -> bool | None:
    """Whether the element is enabled, or ``None`` when the tree does not say so.

    A state set that is absent (an element without a state interface) is *not* the same
    answer as a state set that reports the enabled bit as off. Collapsing the two would
    let a later click reason that a control was disabled when the tree simply had no
    opinion about it.
    """
    try:
        states = node.get_state_set()
    except Exception:
        return None
    if states is None:
        return None
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        return bool(states.contains(Atspi.StateType.ENABLED))
    except Exception:
        return None


def inspect_gui(
    window: str | None = None, max_elements: int = _MAX_ELEMENTS
) -> dict[str, Any]:
    """Describe what is on the accessibility tree, and only that.

    ``window`` narrows the walk to the application (and, where one window is named,
    that window). The result is a flat, document-ordered list of elements, each with
    the fields the tree actually reported: role, label, screen bounds, the actions the
    accessibility interface exposes, and whether the element is enabled. A field the
    tree did not report is absent, never invented — because an element with a guessed
    bound or a guessed role is a target a later click would aim at on no evidence.
    """
    if not _gi_available():
        return {"status": "unavailable", "session": _desktop_session(), **_gi_unavailable()}
    if not _atspi_available():
        session = _desktop_session()
        return {
            "status": "unavailable",
            "session": session,
            "reason": "no_accessibility_bus",
            "detail": "the AT-SPI accessibility bus is not reachable, so no GUI tree can be read",
        }

    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    Atspi.init()
    desktop = Atspi.get_desktop(0)
    apps = [
        (index, desktop.get_child_at_index(index))
        for index in range(desktop.get_child_count())
        if desktop.get_child_at_index(index) is not None
    ]
    # Pick the application to walk. A named one is matched by accessibility name; with
    # no name, the walk is the set of top-level windows, which is what "inspect the
    # active window" means without a compositor that tells us which one is focused.
    chosen: list[tuple[int, Any]] = apps
    if window and window.strip():
        wanted = window.strip().lower()
        named = [
            (index, app)
            for index, app in apps
            if (app.get_name() or "").strip().lower() == wanted
        ]
        if not named:
            return {"status": "window_not_found", "session": _desktop_session(),
                    "window": window.strip(), "element_count": 0, "elements": [],
                    "detail": "The named accessibility application was not found; no other window was read."}
        chosen = named

    elements: list[dict[str, Any]] = []
    counter = {"n": 0}

    def walk(app: Any, app_index: int, depth: int, app_name: str) -> None:
        for index in range(app.get_child_count()):
            if counter["n"] >= max_elements:
                return
            node = _node_from_atspi(app, index, depth, counter)
            if node is None:
                continue
            elements.append(
                _element_payload(node, app_index + 1, app_name, f"{app_index + 1}/{node.index}")
            )
            if node.depth + 1 <= _MAX_DEPTH and counter["n"] < max_elements:
                # Recurse into the element's own children through the same accessor.
                try:
                    child_app = app.get_child_at_index(index)
                except Exception:
                    child_app = None
                if child_app is not None:
                    walk(child_app, app_index, depth + 1, app_name)

    for app_index, app in chosen:
        if counter["n"] >= max_elements:
            break
        app_name = _safe(app.get_name, "")
        walk(app, app_index, 0, app_name or "")

    truncated = counter["n"] >= max_elements
    return {
        "status": "ok",
        "session": _desktop_session(),
        "window": (window or "").strip(),
        "element_count": len(elements),
        "truncated": truncated,
        "elements": elements,
        # An honest count of what this walk could not see: elements past the cap or
        # the depth ceiling are not described, and saying so is what keeps "200
        # elements" from reading as "these are all of them".
        "note": "document-ordered; elements past the cap or depth ceiling are omitted"
        if truncated
        else "",
    }


# -- GUI interaction (C38) -----------------------------------------------
#: Nodes one *target lookup* will visit before giving up. Larger than an inspection's
#: cap because locating a named control is a different question from describing a
#: window: the caller named one element and the walk has to find it or say it is absent.
_MAX_SCAN = 5000


@dataclass
class _Hit:
    """One element the tree reported, kept with the live node so it can be driven.

    Deliberately not a ``_Node``: an action needs the accessibility object itself, and
    re-deriving it from a path would be re-deriving a guess. A hit is only valid for the
    walk that produced it, which is why every action re-locates immediately before it
    invokes anything.
    """

    app: Any
    node: Any
    app_name: str
    role: str
    name: str
    bounds: tuple[int, int, int, int] | None
    #: Human-facing descriptions, as ``inspect_gui`` reports them.
    actions: list[str]
    #: Machine action names paired with their index, as ``get_action_name`` reports
    #: them. Kept separately because the two vocabularies disagree on this machine —
    #: a GTK button's description is ``click`` while its action name may be ``click``,
    #: ``Tap`` or ``doDefault`` depending on the toolkit — and ``do_action`` addresses
    #: by index, so the index has to come from the same list the name was read from.
    action_slots: list[tuple[int, str]]


def _action_slots(node: Any) -> list[tuple[int, str]]:
    """Every action the element exposes, paired with the index ``do_action`` takes."""
    try:
        count = node.get_n_actions()
    except Exception:
        return []
    slots: list[tuple[int, str]] = []
    for index in range(count):
        try:
            slot_name = node.get_action_name(index) or ""
        except Exception:
            slot_name = ""
        slots.append((index, slot_name))
    return slots


def _locate(
    role: str, name: str, window: str | None = None, *, allow_all_apps: bool = False
) -> dict[str, Any]:
    """Find the one element with exactly this role and label, or say why not.

    Matching is **exact**: the same role name and the same label, compared literally.
    A fuzzy match is how "Save" becomes "Save As" and a click lands somewhere nobody
    named. A ``window`` that names no running application is a refusal, not a licence to
    search the whole desktop -- unlike an inspection, which falls back to every app
    because describing more is harmless, an action that searched everywhere after
    failing to find its window would be driving a window the caller never mentioned.
    """
    if not _gi_available():
        return {"status": "unavailable", **_gi_unavailable()}
    if not _atspi_available():
        return {
            "status": "unavailable",
            "reason": "no_accessibility_bus",
            "detail": "the AT-SPI accessibility bus is not reachable, so no element can be found",
        }

    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    Atspi.init()
    desktop = Atspi.get_desktop(0)
    apps = []
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is not None:
            apps.append(app)

    wanted = (window or "").strip()
    if wanted:
        named = [
            app
            for app in apps
            if (_safe(app.get_name, "") or "").strip().lower() == wanted.lower()
        ]
        if not named:
            return {
                "status": "window_not_found",
                "window": wanted,
                "detail": (
                    f"no accessibility application is named {wanted!r}; nothing was "
                    "searched, and nothing was touched"
                ),
            }
        apps = named

    hits: list[_Hit] = []
    scanned = {"n": 0}
    exhausted = {"hit": False}

    def walk(app: Any, app_name: str, depth: int) -> None:
        if len(hits) > _MAX_MATCHES or exhausted["hit"]:
            return
        count = _safe(app.get_child_count, 0)
        for index in range(count):
            if scanned["n"] >= _MAX_SCAN:
                exhausted["hit"] = True
                return
            try:
                node = app.get_child_at_index(index)
            except Exception:
                node = None
            if node is None:
                continue
            scanned["n"] += 1
            node_role = _safe(node.get_role_name, "") or "unknown"
            node_name = (_safe(node.get_name, "") or "")[:120]
            # A match stops the walk early only after one more is ruled out: the caller
            # needs to know whether a second element shares this label.
            if node_role == role and node_name == name:
                hits.append(
                    _Hit(
                        app=app,
                        node=node,
                        app_name=app_name,
                        role=node_role,
                        name=node_name,
                        bounds=_extents(node),
                        actions=_action_names(node),
                        action_slots=_action_slots(node),
                    )
                )
            if depth + 1 <= _MAX_DEPTH:
                walk(node, app_name, depth + 1)

    for app in apps:
        if exhausted["hit"] or len(hits) > _MAX_MATCHES:
            break
        walk(app, _safe(app.get_name, "") or "", 0)

    if not hits:
        if exhausted["hit"]:
            return {
                "status": "scan_truncated",
                "detail": (
                    f"the tree was only partly read ({scanned['n']} elements) before the "
                    "lookup stopped, so the element may exist past the point the walk "
                    "reached; nothing was touched"
                ),
            }
        return {
            "status": "not_found",
            "detail": f"no element has role {role!r} and name {name!r}",
        }
    if len(hits) > _MAX_MATCHES:
        return {
            "status": "ambiguous",
            "count": len(hits),
            "detail": (
                f"{len(hits)} elements have role {role!r} and name {name!r}; refusing to "
                "choose one, because naming a target by label is only an address when "
                "the label is unique"
            ),
            "candidates": [
                {"app": hit.app_name, "role": hit.role, "name": hit.name,
                 "bounds": _bounds_payload(hit.bounds)}
                for hit in hits[:10]
            ],
        }
    hit = hits[0]
    return {"status": "ok", "hit": hit, "detail": ""}


def _bounds_payload(bounds: tuple[int, int, int, int] | None) -> dict[str, Any] | None:
    if bounds is None:
        return None
    x, y, w, h = bounds
    return {"x": x, "y": y, "width": w, "height": h}


def _states(node: Any) -> dict[str, bool] | None:
    """The element's exposed state flags, or ``None`` when it exposes none."""
    try:
        state_set = node.get_state_set()
    except Exception:
        return None
    if state_set is None:
        return None
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    out: dict[str, bool] = {}
    for label, member in (
        ("active", "ACTIVE"),
        ("enabled", "ENABLED"),
        ("focused", "FOCUSED"),
        ("focusable", "FOCUSABLE"),
        ("checked", "CHECKED"),
        ("expanded", "EXPANDED"),
        ("showing", "SHOWING"),
        ("visible", "VISIBLE"),
        ("editable", "EDITABLE"),
        ("password", "PASSWORD_TEXT"),
    ):
        value = getattr(Atspi.StateType, member, None)
        if value is None:
            continue
        try:
            out[label] = bool(state_set.contains(value))
        except Exception:
            continue
    return out


def _recheck(node: Any, role: str, name: str) -> bool:
    """Re-read the element's identity immediately before acting on it.

    The tree can change between the lookup and the invocation -- an application
    repaints, a window closes, a dialog appears over the control. Re-reading is not
    ceremony: it is the difference between invoking the element the caller named and
    invoking whatever the accessibility bus is holding at that index now.
    """
    try:
        live_role = node.get_role_name()
        live_name = (node.get_name() or "")[:120]
    except Exception:
        return False
    return live_role == role and live_name == name


def _identity(hit: _Hit) -> dict[str, Any]:
    return {
        "app": hit.app_name,
        "role": hit.role,
        "name": hit.name,
        "bounds": _bounds_payload(hit.bounds),
    }


def click_element(role: str, name: str, window: str | None = None) -> dict[str, Any]:
    """Activate one named element through its accessibility ``Action``.

    The result distinguishes three things that are routinely conflated:

    * ``dispatched`` -- the toolkit accepted the action. That is the *only* thing this
      function can witness, and it is what a success means here.
    * ``observed`` -- what the element's own exposed state looked like before and
      after. A button that flips to checked is evidence the click did something; a
      button whose state does not change is not evidence that it did nothing.
    * ``status`` -- whether the action was performed at all. ``not_found``,
      ``ambiguous``, ``disabled`` and ``not_actionable`` are refusals in which no
      toolkit call was made, and they are reported as such rather than as failures to
      click something that was never there.

    Coordinates are never used. A click aimed at a pixel is a click aimed at whatever
    happens to be under that pixel now.
    """
    located = _locate(role, name, window)
    if located["status"] != "ok":
        return {**located, "role": role, "name": name}
    hit: _Hit = located["hit"]
    result: dict[str, Any] = {"role": role, "name": name, **_identity(hit)}

    before = _states(hit.node)
    if before is not None and before.get("enabled") is False:
        return {**result, "status": "disabled",
                "detail": "the element is disabled; activating it would be a no-op at best"}

    # Which published action means "activate". The *name* is the machine vocabulary
    # (``click`` / ``Tap`` / ``doDefault``), the *description* is what the toolkit shows
    # a person and is what ``inspect_gui`` reported — either spelling counts, because
    # different toolkits put the same verb in different fields, and neither field is
    # guessed at: an action whose name and description are both something else is a
    # different operation and is deliberately not chosen.
    chosen: tuple[int, str] | None = None
    accepted = {n.lower() for n in _CLICK_ACTIONS}
    for slot_index, slot_name in hit.action_slots:
        description = hit.actions[slot_index] if slot_index < len(hit.actions) else ""
        if slot_name.strip().lower() in accepted or description.strip().lower() in accepted:
            chosen = (slot_index, slot_name or description)
            break
    if chosen is None:
        return {
            **result,
            "status": "not_actionable",
            "actions": list(hit.actions),
            "action_names": [slot for _, slot in hit.action_slots],
            "detail": (
                "the element exposes no activate action; the actions it does expose are "
                "listed and none of them is a click"
            ),
        }

    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    if not _recheck(hit.node, role, name):
        return {**result, "status": "element_changed",
                "detail": "the element's role or label changed between lookup and "
                          "invocation; nothing was invoked"}

    index, action_name = chosen
    try:
        dispatched = bool(Atspi.Action.do_action(hit.node, index))
    except Exception as exc:  # the toolkit refused or the object went away
        return {**result, "status": "refused", "action": action_name,
                "dispatched": False, "detail": f"the toolkit refused the action: {exc}"}

    after = _states(hit.node)
    changed = None
    if before is not None and after is not None:
        changed = any(before.get(key) != after.get(key) for key in before)
    return {
        **result,
        # `dispatched`, never `clicked`: the toolkit accepting an activation is what
        # happened. What the application then did with it is the application's business
        # and is not claimed here.
        "status": "dispatched" if dispatched else "refused",
        "action": action_name,
        "dispatched": dispatched,
        "observed": {"before": before, "after": after, "state_changed": changed},
        "detail": (
            "the toolkit accepted the activation; the element's exposed state changed"
            if changed
            else "the toolkit accepted the activation; the element's exposed state did "
                 "not change, which is not the same as saying nothing happened"
        ),
    }


def type_text(
    role: str, name: str, text: str, window: str | None = None
) -> dict[str, Any]:
    """Insert text into one named editable element.

    This is ``EditableText.insert_text``, not keystroke injection: the text is written
    into *that accessible object*, at its caret, and nowhere else. Measured on this
    machine's AT-SPI bridge, it succeeds on a control that does not hold keyboard focus
    and whose window is not the active one — which is exactly why focus is not part of
    the guard here. What matters for "did it go where the caller meant" is the element's
    identity, which is re-read immediately before the insertion.

    Password fields are refused outright. The text itself is never echoed back: it may
    be a message body, a search for something private, or a credential. Only lengths are
    reported, and the field is re-read afterwards so the caller can see whether the
    insertion actually landed rather than taking a boolean on trust.
    """
    if not isinstance(text, str) or not text:
        return {"status": "invalid", "detail": "text must be a non-empty string"}
    located = _locate(role, name, window)
    if located["status"] != "ok":
        return {**located, "role": role, "name": name}
    hit: _Hit = located["hit"]
    result: dict[str, Any] = {"role": role, "name": name, **_identity(hit)}

    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    if "EditableText" not in (_safe(hit.node.get_interfaces, []) or []):
        return {
            **result,
            "status": "not_editable",
            "detail": "the element does not expose an editable text interface; there is "
                      "no supported way to type into it",
        }
    before = _states(hit.node)
    if before is not None and before.get("password"):
        return {
            **result,
            "status": "refused",
            "detail": "the element is a password field; this agent does not type secrets",
        }
    if before is not None and before.get("enabled") is False:
        return {**result, "status": "disabled", "detail": "the element is disabled"}

    if not _recheck(hit.node, role, name):
        return {**result, "status": "element_changed",
                "detail": "the element's role or label changed between lookup and "
                          "typing; nothing was typed"}

    try:
        position = int(Atspi.Text.get_caret_offset(hit.node))
    except Exception:
        position = 0
    try:
        before_length = int(Atspi.Text.get_character_count(hit.node))
    except Exception:
        before_length = None
    try:
        typed = bool(Atspi.EditableText.insert_text(hit.node, position, text, len(text)))
    except Exception as exc:
        return {**result, "status": "refused", "typed": False,
                "detail": f"the toolkit refused the text insertion: {exc}"}

    try:
        after_length = int(Atspi.Text.get_character_count(hit.node))
    except Exception:
        after_length = None
    grew = (
        before_length is not None
        and after_length is not None
        and after_length > before_length
    )
    return {
        **result,
        "status": "typed" if typed else "refused",
        "typed": typed,
        # Lengths, never the text.
        "length": {"before": before_length, "after": after_length,
                   "inserted": len(text)},
        "observed": {"grew": grew},
        "detail": (
            "the toolkit accepted the insertion and the field holds more characters"
            if typed and grew
            else "the toolkit accepted the insertion; the field's character count did "
                 "not grow, so what the text now contains was not established here"
            if typed
            else "the toolkit did not accept the insertion"
        ),
    }


def _scroll_bar_for(node: Any, axis: str) -> Any | None:
    """The scroll bar that moves ``node``'s content along ``axis``, or ``None``.

    Walking *up* from the element, this looks for the nearest ancestor that holds a
    ``Value``-bearing scroll bar: that bar's value is the scroll offset in pixels, and
    setting it is what actually scrolls the content. Measured on this machine's GTK
    bridge, ``Component.scroll_to`` reports failure and moves nothing, so a scroll
    built on it would be a call that returns False and a claim of "scrolled" with no
    movement behind it. The mechanism used here is the one measured to work.

    ``axis`` is ``vertical`` or ``horizontal``. A bar is vertical when it is taller
    than it is wide.
    """
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    def bars_under(container: Any, depth: int) -> Any | None:
        if depth > 2:
            return None
        try:
            count = container.get_child_count()
        except Exception:
            return None
        for index in range(count):
            try:
                child = container.get_child_at_index(index)
            except Exception:
                continue
            if child is None:
                continue
            try:
                if child.get_role_name() != "scroll bar":
                    continue
            except Exception:
                continue
            if "Value" not in (_safe(child.get_interfaces, []) or []):
                continue
            bounds = _extents(child)
            if bounds is None:
                continue
            _, _, width, height = bounds
            is_vertical = height > width
            if (axis == "vertical") == is_vertical:
                return child
        return None

    current = node
    for _ in range(8):
        try:
            current = Atspi.Accessible.get_parent(current)
        except Exception:
            return None
        if current is None:
            return None
        found = bars_under(current, 0)
        if found is not None:
            return found
    return None


def scroll_element(
    role: str, name: str, direction: str, window: str | None = None
) -> dict[str, Any]:
    """Scroll the container holding one named element to an edge.

    The named element is the *anchor*: it says which scrollable region is meant, so a
    window with two panes is not a guess. The scroll itself is performed by setting the
    offset on the scroll bar that governs that region -- the same value a person moves
    by dragging the bar -- because the accessibility ``scroll_to`` call reports failure
    and moves nothing on this machine's GTK bridge (measured; see ``_scroll_bar_for``).

    The direction names a destination, not a gesture: ``top`` means offset 0, ``bottom``
    means maximum offset. ``anywhere`` is rejected here rather than silently treated as
    ``top``, because "scroll somewhere" is not a request with a defined outcome.

    The evidence is the content's own reported position: the anchor's bounds before and
    after, plus the scroll bar's offset. A bar whose offset changed but whose content did
    not move is reported as ``offset_changed`` -- honest about the difference between
    "the scrollbar moved" and "the content moved".
    """
    edge = (direction or "").strip().lower()
    if edge not in _SCROLL_EDGES:
        return {
            "status": "invalid",
            "detail": f"direction must be one of {', '.join(_SCROLL_EDGES)}",
        }
    if edge == "anywhere":
        return {
            "status": "invalid",
            "detail": (
                "scroll needs a destination edge (top, bottom, left, right); "
                "'anywhere' names no position, so there is nothing to verify it against"
            ),
        }
    axis = "horizontal" if edge in ("left", "right") else "vertical"
    located = _locate(role, name, window)
    if located["status"] != "ok":
        return {**located, "role": role, "name": name}
    hit: _Hit = located["hit"]
    result: dict[str, Any] = {"role": role, "name": name, **_identity(hit)}

    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    bar = _scroll_bar_for(hit.node, axis)
    if bar is None:
        return {
            **result,
            "status": "not_scrollable",
            "detail": (
                f"no {axis} scroll bar governs this element; it is either not inside a "
                "scrollable region or the region fits its content"
            ),
        }

    try:
        before_offset = float(Atspi.Value.get_current_value(bar))
        maximum = float(Atspi.Value.get_maximum_value(bar))
        minimum = float(Atspi.Value.get_minimum_value(bar))
    except Exception as exc:
        return {**result, "status": "not_scrollable",
                "detail": f"the scroll bar's value could not be read: {exc}"}

    target = minimum if edge in ("top", "left") else maximum
    before = _extents(hit.node)
    if not _recheck(hit.node, role, name):
        return {**result, "status": "element_changed",
                "detail": "the element's role or label changed between lookup and "
                          "scrolling; nothing was scrolled"}
    try:
        Atspi.Value.set_current_value(bar, target)
    except Exception as exc:
        return {**result, "status": "refused", "scrolled": False,
                "detail": f"the toolkit refused the scroll offset: {exc}"}

    after_offset = _safe(lambda: float(Atspi.Value.get_current_value(bar)), None)
    after = _extents(hit.node)
    # An element scrolled out of view reports no usable geometry, so "it moved" has to
    # include the transition into or out of visibility -- otherwise scrolling an
    # off-screen row into view would read as "nothing moved" because both reads were
    # equally uninformative before and after.
    visible_before, visible_after = before is not None, after is not None
    moved = (visible_before != visible_after) or (
        visible_before and visible_after and before != after
    )
    offset_changed = (
        after_offset is not None and abs(after_offset - before_offset) > 0.5
    )
    if moved:
        status = "scrolled"
        detail = "the scroll offset changed and the content moved"
    elif offset_changed:
        status = "offset_changed"
        detail = (
            "the scroll bar's offset changed but the anchor's reported bounds did not; "
            "the content may have moved without the accessibility tree saying so"
        )
    else:
        status = "already_at_edge"
        detail = (
            f"the scroll bar was already at {before_offset:g} (target {target:g}); "
            "nothing needed to move"
        )
    return {
        **result,
        "status": status,
        "direction": edge,
        "offset": {"before": before_offset, "after": after_offset,
                   "target": target, "max": maximum},
        "anchor_bounds": {"before": _bounds_payload(before),
                          "after": _bounds_payload(after)},
        "moved": moved,
        "detail": detail,
    }


def _element_payload(
    node: _Node, app_index: int, app_name: str, path: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "app_index": app_index,
        "app": app_name,
        "path": path,
        "role": node.role,
        "name": node.name,
        "depth": node.depth,
        "enabled": node.enabled,
        "actions": node.actions,
    }
    if node.bounds is not None:
        x, y, w, h = node.bounds
        payload["bounds"] = {"x": x, "y": y, "width": w, "height": h}
        payload["center"] = {"x": x + w // 2, "y": y + h // 2}
    return payload


__all__ = [
    "capture_screenshot",
    "click_element",
    "inspect_gui",
    "scroll_element",
    "type_text",
]


# A bundled Python cannot load a host CPython's compiled gi extension. Executed
# as a standalone, fixed script by native_desktop, this module needs only stdlib
# and the host's installed desktop bindings. It exposes no arbitrary command/code.
def valid_native_request(operation: str, parameters: dict[str, Any]) -> bool:
    if not isinstance(parameters, dict):
        return False
    keys = {
        "capture": set(), "inspect": {"window", "max_elements"},
        "click": {"role", "name", "window"},
        "type": {"role", "name", "text", "window"},
        "scroll": {"role", "name", "direction", "window"},
    }
    if operation not in keys or set(parameters) - keys[operation]:
        return False
    for key in ("window", "role", "name"):
        value = parameters.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 500):
            return False
    if operation in {"click", "type", "scroll"} and (
        not parameters.get("role") or not isinstance(parameters.get("name"), str)
    ):
        return False
    if operation == "inspect":
        cap = parameters.get("max_elements", _MAX_ELEMENTS)
        if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= _MAX_ELEMENTS:
            return False
    if operation == "type":
        text = parameters.get("text")
        if not isinstance(text, str) or not text or len(text) > 16000:
            return False
    return operation != "scroll" or parameters.get("direction") in _SCROLL_EDGES


def _native_main() -> None:
    import json
    import sys

    if sys.argv[1:] == ["--native-probe"]:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi, Gio  # noqa: F401

            print(json.dumps({"available": True}))
        except (ImportError, ValueError):
            print(json.dumps({"available": False}))
        return
    if sys.argv[1:] != ["--native-request"]:
        return
    raw = os.environ.pop("ACTIONMESH_NATIVE_REQUEST", "")
    answer: dict[str, Any] = {"status": "invalid", "detail": "Invalid native desktop request."}
    try:
        if len(raw.encode("utf-8")) > 64 * 1024:
            raise ValueError("request limit")
        request = json.loads(raw)
        operation = request.get("operation")
        parameters = request.get("parameters")
        if not isinstance(operation, str) or not valid_native_request(operation, parameters):
            raise ValueError("invalid request")
        functions: dict[str, Callable[..., dict[str, Any]]] = {
                     "capture": capture_screenshot, "inspect": inspect_gui,
                     "click": click_element, "type": type_text, "scroll": scroll_element}
        answer = functions[operation](**parameters)
    except Exception:
        # Never print exception text or an input: labels and typed text are private.
        answer = {"status": "unavailable", "detail": "The host desktop operation was unavailable."}
    encoded = json.dumps(answer)
    if len(encoded.encode("utf-8")) > 256 * 1024:
        encoded = json.dumps({"status": "unavailable", "detail": "Desktop observation exceeded its limit."})
    print(encoded)


if __name__ == "__main__":
    _native_main()
