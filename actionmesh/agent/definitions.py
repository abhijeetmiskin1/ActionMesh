"""C19 device actions: the capability registry a laptop-side agent may expose.

Section 1 of the mobile-control brief is explicit that these are **ordinary
ActionMesh actions** — they go through the registry, the graph, the policy engine,
approval, the executor and the verifier, and nothing about them is special. That is
the entire reason this file is a list of ``ActionDefinition``s rather than a set of
routes: an action that skipped the registry would be an action that skipped policy.

Two things are worth reading before adding to this table.

**Risk is set here, and here only.** ``device.terminal.execute`` is ``CRITICAL``, so
the policy engine forces an explicit approval on every call regardless of what any
client says (``RiskLevel`` can be raised by the definition, never lowered by the
request). A capability that could choose its own risk would not have a policy engine.

**Permissions are catalog scopes, not strings invented here.** Each definition names
concrete scopes from ``core/permissions/catalog.py``, because an unknown permission
is a registration error and a wildcard requirement is refused. ``device.file.read``
asks for ``file.read``: the action is the operation, the scope is the consent the
user granted, and keeping them distinct is what lets a user consent to reading files
without consenting to shelling out.

Parameters are shape-checked by C2. Shape is not *meaning* — a path that resolves
outside the root and an argv that names ``rm`` are rejected by the agent, not by a
JSON schema, and both are fail-closed checks with their own tests.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from actionmesh.core.permissions.risk import max_risk
from actionmesh.protocol.models import (
    ActionDefinition,
    RetryPolicy,
    RiskLevel,
    VerificationSpec,
)

#: Provider id used by every device action. Not ``mock``: a simulated capability
#: must never be presented as a real one, and a real one must never hide behind the
#: default provider name.
PROVIDER = "laptop"


def _def(
    name: str,
    risk: RiskLevel,
    permissions: list[str],
    properties: dict[str, dict[str, Any]],
    required: list[str],
    description: str,
    *,
    timeout_s: float = 30.0,
    max_attempts: int = 1,
) -> ActionDefinition:
    """Build a device action definition.

    ``max_attempts`` defaults to 1 on purpose. A retry is only safe when the action
    is idempotent *and* a failed attempt leaves no effect behind — and "write this
    file", "run this command", "start this server" all fail that test. The mock seeds
    retry three times because they simulate; a device action that half-ran has
    touched the machine, and a second attempt is a second side effect.
    """
    return ActionDefinition(
        name=name,
        version="1.0",
        description=description,
        risk_level=risk,
        permissions=permissions,
        input_schema={"type": "object", "properties": properties, "required": required},
        output_schema={"type": "object", "properties": {}},
        verification=VerificationSpec(required=True, method="provider_verify"),
        provider=PROVIDER,
        timeout_s=timeout_s,
        # TRANSIENT failures only, and by default not even then: the executor
        # refuses to retry a kind that is not retryable, and one attempt is one
        # attempt.
        retry_policy=RetryPolicy(max_attempts=max_attempts, backoff="none"),
    )


_S = {"type": "string"}
_ARGS = {"type": "array"}
_NUM = {"type": "number"}
_OBJ = {"type": "object"}
_BOOL = {"type": "boolean"}


def device_definitions() -> list[ActionDefinition]:
    """Every capability the laptop agent can offer.

    ``description`` is user-visible — it appears in the approval prompt and the
    capability list, so it says what the action does to the machine rather than
    which module implements it.
    """
    return [
        # --- files, inside the configured root only -----------------------
        _def(
            "device.file.list", RiskLevel.MEDIUM, ["device.file.list"],
            {"path": _S, "limit": _NUM}, [],
            "List files inside the laptop's configured workspace root",
        ),
        _def(
            "device.file.read", RiskLevel.MEDIUM, ["file.read"],
            {"path": _S}, ["path"],
            "Read one file inside the configured workspace root",
        ),
        _def(
            "device.file.write", RiskLevel.HIGH, ["file.write"],
            {"path": _S, "content": _S, "append": _BOOL}, ["path", "content"],
            "Create or overwrite one file inside the configured workspace root",
        ),
        _def(
            "device.file.delete", RiskLevel.HIGH, ["file.delete"],
            {"path": _S}, ["path"],
            "Delete one file inside the configured workspace root",
        ),
        # --- terminal ------------------------------------------------------
        _def(
            "device.terminal.execute", RiskLevel.CRITICAL, ["system.shell"],
            {
                "command": _S,
                "arguments": _ARGS,
                "working_directory": _S,
                "timeout_s": _NUM,
                "env": _OBJ,
            },
            ["command"],
            "Run one program on the laptop and return its output",
            timeout_s=120.0,
        ),
        # --- processes ----------------------------------------------------
        _def(
            "device.process.list", RiskLevel.LOW, ["device.process.read"],
            {"filter": _S}, [],
            "List processes this laptop agent started",
        ),
        _def(
            "device.process.start", RiskLevel.HIGH, ["device.process.control"],
            {"command": _S, "arguments": _ARGS, "working_directory": _S, "label": _S},
            ["command"],
            "Start a program on the laptop and leave it running",
        ),
        _def(
            "device.process.stop", RiskLevel.HIGH, ["device.process.control"],
            {"process_id": _S}, ["process_id"],
            "Stop a program this laptop agent started",
        ),
        _def(
            "device.process.logs", RiskLevel.MEDIUM, ["device.process.read"],
            {"process_id": _S, "max_bytes": _NUM}, ["process_id"],
            "Read the output a program this laptop agent started has written so far",
        ),
        # --- dev servers --------------------------------------------------
        _def(
            "device.server.start", RiskLevel.HIGH, ["device.server.control"],
            {
                "name": _S,
                "command": _S,
                "arguments": _ARGS,
                "working_directory": _S,
                "port": _NUM,
            },
            ["name", "command"],
            "Start a named development server on the laptop",
        ),
        _def(
            "device.server.stop", RiskLevel.HIGH, ["device.server.control"],
            {"name": _S}, ["name"],
            "Stop a named development server this laptop agent started",
        ),
        _def(
            "device.server.status", RiskLevel.LOW, ["device.server.read"],
            {"name": _S}, [],
            "Report which development servers are running and their ports",
        ),
        # --- projects -----------------------------------------------------
        _def(
            "device.project.list", RiskLevel.LOW, ["device.project.read"],
            {}, [],
            "List projects in the laptop's configured workspace root",
        ),
        _def(
            "device.project.status", RiskLevel.LOW, ["device.project.read"],
            {"project": _S}, [],
            "Report a project's branch, changed files and running services",
        ),
        _def(
            "device.project.open", RiskLevel.MEDIUM, ["device.ide.open"],
            {"project": _S, "ide": _S}, ["project"],
            "Open a project in an editor on the laptop",
        ),
        # --- editors ------------------------------------------------------
        _def(
            "device.ide.list", RiskLevel.LOW, ["device.project.read"],
            {}, [],
            "List the editors actually installed on this laptop",
        ),
        _def(
            "device.ide.open", RiskLevel.MEDIUM, ["device.ide.open"],
            {"ide": _S, "path": _S}, ["path"],
            "Open a file or folder in an installed editor",
        ),
        _def(
            "device.ide.command", RiskLevel.HIGH, ["device.control"],
            {"ide": _S, "command": _S, "arguments": _ARGS, "project": _S},
            ["command"],
            "Run a documented editor command-line operation",
        ),
        # --- applications -------------------------------------------------
        _def(
            "device.app.open", RiskLevel.MEDIUM, ["system.app.launch"],
            {"app": _S, "arguments": _ARGS}, ["app"],
            "Launch an application on the laptop",
        ),
        _def(
            "device.app.close", RiskLevel.HIGH, ["device.control"],
            {"app": _S}, ["app"],
            "Close an application this laptop agent launched",
        ),
        # --- screen + GUI (C38) -------------------------------------------
        # Capture goes through the OS screenshot portal, which is the compositor's own
        # consent gate: the approval prompt this definition triggers is *in addition* to
        # that, never instead of it. The result carries a transient reference, not an
        # image, so a capture is not a way to exfiltrate a screen into a stored payload.
        _def(
            "device.screen.capture", RiskLevel.MEDIUM, ["device.screen.read"],
            {}, [],
            "Capture this laptop's screen once, through the OS screenshot portal",
            timeout_s=45.0,
        ),
        # Inspection reads the accessibility tree: the machine's own description of its
        # windows, elements and their bounds. Read-only and cheap; it never drives
        # anything, which is why it is a MEDIUM read scoped to screen.read.
        _def(
            "device.screen.inspect", RiskLevel.MEDIUM, ["device.screen.read"],
            {"window": _S, "max_elements": _NUM}, [],
            "Describe the windows and elements on this laptop's accessibility tree",
            timeout_s=20.0,
        ),
        # --- GUI actions (C38) --------------------------------------------
        # These drive the machine, so they are HIGH and scoped to screen.control -- the
        # scope that already exists for exactly this ("Drive this laptop's GUI (click,
        # type, scroll) via accessibility"). Nothing here widens a scope or lowers a
        # classification: a click is an arbitrary interaction with an arbitrary window,
        # which is what HIGH already means in this catalog.
        #
        # Each names its target by role and label, never by coordinate. The label must
        # identify exactly one element or the action refuses -- see
        # ``device_screen._MAX_MATCHES``. Verification is independent: after the action,
        # the executor re-observes the same element and confirms the machine's state
        # changed, so "the toolkit accepted the call" is not mistaken for "the effect
        # happened".
        _def(
            "device.gui.click", RiskLevel.HIGH, ["device.screen.control"],
            {"role": _S, "name": _S, "window": _S}, ["role", "name"],
            "Activate one named element on this laptop's GUI (a button, a menu item)",
            timeout_s=20.0,
        ),
        _def(
            "device.gui.type", RiskLevel.HIGH, ["device.screen.control"],
            {"role": _S, "name": _S, "text": _S, "window": _S}, ["role", "name", "text"],
            "Insert text into one named editable element on this laptop's GUI",
            timeout_s=20.0,
        ),
        _def(
            "device.gui.scroll", RiskLevel.HIGH, ["device.screen.control"],
            {"role": _S, "name": _S, "direction": _S, "window": _S},
            ["role", "name", "direction"],
            "Scroll the region holding a named element to an edge (top/bottom/left/right)",
            timeout_s=20.0,
        ),
        # --- consequential web submits (C42, contract §2.4/§3) ---------------
        # These are the two capabilities a website workflow may end on, and they are
        # the *only* way a booking or an order is submitted: ``browser.click`` is a
        # MEDIUM interaction and never becomes one of these (see
        # ``tests/security/test_submit_bypass.py``). The separation is structural —
        # the browser engine has no submit verb at all — because a click that
        # happened to land on a "Place order" button must not be an approval.
        #
        # CRITICAL here, in the definition, is what makes the policy engine force
        # ``WAITING_PERMISSION`` on every call regardless of what any client or model
        # says. Risk is set in this file and nowhere else; a request can raise it and
        # never lower it. ``payment.execute`` is the scope the contract assigns to a
        # consequential submit (contract §3) — it is already CRITICAL, so these
        # actions reuse it rather than inventing a weaker one.
        #
        # One attempt, for the same reason as every other device action and more so:
        # a submit that half-ran may have taken the money already, and a retry is a
        # second booking.
        #
        # Parameters are the exact fields the reviewed operation carried. They exist
        # so the executor can bind an approval to *this* journey/order rather than to
        # a description of one; a field the caller changes after the review is a
        # different operation, not the approved one.
        # --- browser (C42, contract §2.1) ----------------------------------
        # One controlled session, named operations only, targeted by opaque refs
        # minted by snapshot/inspect — never by selector or coordinate. Login /
        # CAPTCHA / OTP / MFA are user handoffs (status "needs_user"), and the
        # engine has no submit verb: browser.click can never become a booking.
        _def(
            "browser.open", RiskLevel.LOW, ["browser.control"],
            {"url": _S}, ["url"],
            "Open a website in the controlled browser session",
            timeout_s=45.0,
        ),
        _def(
            "browser.navigate", RiskLevel.LOW, ["browser.control"],
            {"url": _S}, ["url"],
            "Navigate the controlled browser session to a URL",
            timeout_s=45.0,
        ),
        _def(
            "browser.search", RiskLevel.LOW, ["browser.control", "search.web"],
            {"query": _S, "engine": _S}, ["query"],
            "Search the web from the controlled browser session (read-only)",
            timeout_s=45.0,
        ),
        _def(
            "browser.inspect", RiskLevel.LOW, ["browser.control"],
            {}, [],
            "Inspect the current page: URL, title and addressable elements",
            timeout_s=30.0,
        ),
        _def(
            "browser.snapshot", RiskLevel.LOW, ["browser.control"],
            {}, [],
            "Snapshot the current page's addressable elements as refs",
            timeout_s=30.0,
        ),
        _def(
            "browser.click", RiskLevel.MEDIUM, ["browser.control"],
            {"ref": _S, "double": _BOOL}, ["ref"],
            "Click one element identified by a snapshot ref",
            timeout_s=30.0,
        ),
        _def(
            "browser.type", RiskLevel.MEDIUM, ["browser.control"],
            {"ref": _S, "text": _S}, ["ref", "text"],
            "Type text into one element identified by a snapshot ref",
            timeout_s=30.0,
        ),
        _def(
            "browser.select", RiskLevel.MEDIUM, ["browser.control"],
            {"ref": _S, "value": _S}, ["ref", "value"],
            "Select a value in one element identified by a snapshot ref",
            timeout_s=30.0,
        ),
        _def(
            "browser.scroll", RiskLevel.LOW, ["browser.control"],
            {"ref": _S, "direction": _S, "amount": _NUM}, ["direction"],
            "Scroll the page or one element in a direction",
            timeout_s=30.0,
        ),
        _def(
            "browser.back", RiskLevel.LOW, ["browser.control"],
            {}, [],
            "Go back one page in the controlled browser session",
            timeout_s=30.0,
        ),
        _def(
            "browser.forward", RiskLevel.LOW, ["browser.control"],
            {}, [],
            "Go forward one page in the controlled browser session",
            timeout_s=30.0,
        ),
        _def(
            "browser.refresh", RiskLevel.LOW, ["browser.control"],
            {}, [],
            "Reload the current page in the controlled browser session",
            timeout_s=30.0,
        ),
        _def(
            "browser.wait", RiskLevel.LOW, ["browser.control"],
            {"ms": _NUM, "until": _S}, [],
            "Wait briefly for the page to settle",
            timeout_s=30.0,
        ),
        _def(
            "browser.stop", RiskLevel.MEDIUM, ["browser.control"],
            {}, [],
            "Stop the current browser task until a fresh open",
            timeout_s=30.0,
        ),
        # --- files (C42, contract §2.2) ------------------------------------
        # Richer verbs over the same confined Workspace boundary as
        # device.file.* — no new shell route, no widened root.
        _def(
            "device.files.list", RiskLevel.MEDIUM, ["device.file.list"],
            {"path": _S, "limit": _NUM}, ["path"],
            "List entries in a directory inside the workspace root",
        ),
        _def(
            "device.files.search", RiskLevel.LOW, ["device.file.list"],
            {"path": _S, "pattern": _S, "limit": _NUM}, ["path", "pattern"],
            "Search file names under a directory inside the workspace root",
        ),
        _def(
            "device.files.read", RiskLevel.MEDIUM, ["file.read"],
            {"path": _S}, ["path"],
            "Read one file inside the workspace root",
        ),
        _def(
            "device.files.open", RiskLevel.MEDIUM, ["device.file.list"],
            {"path": _S}, ["path"],
            "Open a file in its default application",
        ),
        _def(
            "device.files.info", RiskLevel.LOW, ["device.file.list"],
            {"path": _S}, ["path"],
            "Report metadata for one path inside the workspace root",
        ),
        _def(
            "device.files.create", RiskLevel.HIGH, ["file.write"],
            {"path": _S, "content": _S}, ["path", "content"],
            "Create one file inside the workspace root",
        ),
        _def(
            "device.files.rename", RiskLevel.HIGH, ["file.write"],
            {"path": _S, "to": _S}, ["path", "to"],
            "Rename one file inside the workspace root",
        ),
        _def(
            "device.files.move", RiskLevel.HIGH, ["file.write"],
            {"path": _S, "to": _S}, ["path", "to"],
            "Move one file inside the workspace root",
        ),
        _def(
            "device.files.copy", RiskLevel.MEDIUM, ["file.read", "file.write"],
            {"path": _S, "to": _S}, ["path", "to"],
            "Copy one file inside the workspace root",
        ),
        _def(
            "device.files.edit", RiskLevel.HIGH, ["file.write"],
            {"path": _S, "find": _S, "replace": _S, "all": _BOOL},
            ["path", "find", "replace"],
            "Replace text in one file inside the workspace root",
        ),
        _def(
            "device.files.trash", RiskLevel.HIGH, ["file.delete"],
            {"path": _S}, ["path"],
            "Move one file to the OS trash",
        ),
        # --- media (C42, contract §2.3) ------------------------------------
        # Playback is only claimed after the real player state is read back;
        # an unreachable player is "unavailable", never a simulated success.
        _def(
            "media.status", RiskLevel.LOW, ["media.control"],
            {}, [],
            "Report the current media player state",
        ),
        _def(
            "media.play", RiskLevel.LOW, ["media.control"],
            {"ref": _S}, [],
            "Play the current media selection",
        ),
        _def(
            "media.pause", RiskLevel.LOW, ["media.control"],
            {"ref": _S}, [],
            "Pause the current media selection",
        ),
        _def(
            "media.next", RiskLevel.LOW, ["media.control"],
            {}, [],
            "Play the next media item",
        ),
        _def(
            "media.previous", RiskLevel.LOW, ["media.control"],
            {}, [],
            "Play the previous media item",
        ),
        _def(
            "media.volume", RiskLevel.LOW, ["media.control"],
            {"level": _NUM, "muted": _BOOL}, [],
            "Set media volume or mute",
        ),
        _def(
            "media.open_file", RiskLevel.MEDIUM,
            ["media.control", "system.app.launch"],
            {"path": _S}, ["path"],
            "Open a media file inside the workspace root in its default app",
        ),
        _def(
            "media.open_url", RiskLevel.LOW, ["browser.control"],
            {"url": _S}, ["url"],
            "Open a media URL through the browser",
            timeout_s=45.0,
        ),
        # --- consequential web submits (C42, contract §2.4/§3) ---------------
        _def(
            "booking.submit", RiskLevel.CRITICAL, ["payment.execute"],
            {"journey": _S, "passenger": _S, "fare": _S},
            ["journey", "passenger", "fare"],
            "Submit a reviewed train booking — consequential, requires explicit final "
            "confirmation",
            timeout_s=90.0,
        ),
        _def(
            "order.place", RiskLevel.CRITICAL, ["payment.execute"],
            {"items": _S, "total": _S}, ["items", "total"],
            "Place a reviewed food order — consequential, requires explicit final "
            "confirmation",
            timeout_s=90.0,
        ),
    ]


#: Everything the agent may advertise. Presence in this tuple is what makes a
#: capability *claimable*; the agent still only advertises the ones its configuration
#: actually enables, and ``DeviceRegistry.register`` re-checks every advertised name
#: against the registry, so a capability nobody registered cannot be announced.
CAPABILITIES: tuple[str, ...] = tuple(d.name for d in device_definitions())

#: Capabilities that reach outside the ActionMesh process into the machine. The
#: agent advertises them only when an operator has configured the corresponding
#: boundary, because "installed" is not "authorized".
REQUIRING_FILE_ROOT: frozenset[str] = frozenset(
    {
        "device.file.list",
        "device.file.read",
        "device.file.write",
        "device.file.delete",
        "device.project.list",
        "device.project.status",
        "device.project.open",
        # C42: the richer file verbs resolve inside the same confined root.
        "device.files.list",
        "device.files.search",
        "device.files.read",
        "device.files.open",
        "device.files.info",
        "device.files.create",
        "device.files.rename",
        "device.files.move",
        "device.files.copy",
        "device.files.edit",
        "device.files.trash",
        # C42: media.open_file opens a confined local file.
        "media.open_file",
    }
)
REQUIRING_SHELL: frozenset[str] = frozenset(
    {
        "device.terminal.execute",
        "device.process.start",
        "device.process.stop",
        "device.process.list",
        # Reading a child's log only means something if the agent was allowed to launch
        # children in the first place, so it belongs behind the same gate.
        "device.process.logs",
        "device.server.start",
        "device.server.stop",
        "device.server.status",
        "device.app.open",
        "device.app.close",
        "device.ide.list",
        "device.ide.open",
        "device.ide.command",
    }
)


#: Capabilities this build can actually carry out.
#:
#: A capability is advertised only when it is both *permitted* by configuration and
#: *implemented* here, because the alternative is worse than it looks: an agent that
#: announces ``device.terminal.execute`` with no handler behind it produces a graph
#: that authorizes, an approval prompt that promises something, and an execution that
#: fails at the last step. "The phone asked for it and the laptop said no such
#: capability exists" is the honest answer, and it is this set that makes it true.
#:
#: Kept as data rather than derived from the provider so the two can be compared in a
#: test — see ``tests/unit/test_agent_provider.py``.
IMPLEMENTED: frozenset[str] = frozenset(
    {
        "device.file.list",
        "device.file.read",
        "device.file.write",
        "device.file.delete",
        "device.terminal.execute",
        "device.process.list",
        "device.process.logs",
        "device.process.start",
        "device.process.stop",
        "device.server.start",
        "device.server.stop",
        "device.server.status",
        "device.ide.list",
        "device.ide.open",
        "device.ide.command",
        "device.app.open",
        "device.app.close",
        "device.project.list",
        "device.project.status",
        "device.project.open",
        "device.screen.capture",
        "device.screen.inspect",
        "device.gui.click",
        "device.gui.type",
        "device.gui.scroll",
        # C42: the two consequential submits. They are in this set because this build
        # *does* carry them out — through a fail-closed handler that refuses, naming the
        # missing payment backend, when none is configured — and
        # ``SUPPORTED == IMPLEMENTED`` is the invariant that keeps an advertised
        # capability from having no handler at all. Whether one may be *advertised* is a
        # separate question, answered by :data:`REQUIRING_PAYMENT` and by
        # ``config.advertise()``.
        "booking.submit",
        "order.place",
        # C42: browser, richer files and media engines. Each id here is backed by a
        # real handler in ``LaptopProvider`` — ``browser.*`` by ``BrowserEngine``,
        # ``device.files.*`` by ``FilesEngine``, ``media.*`` by ``MediaEngine`` — so
        # adding them to this set is what lets ``SUPPORTED == IMPLEMENTED`` stay true
        # while the 33 new capabilities become resolvable. Whether each one is
        # *advertised* is answered separately by ``REQUIRING_BROWSER`` /
        # ``REQUIRING_MEDIA`` / ``REQUIRING_FILE_ROOT`` and by ``config.advertise()``.
        "browser.open",
        "browser.navigate",
        "browser.search",
        "browser.inspect",
        "browser.snapshot",
        "browser.click",
        "browser.type",
        "browser.select",
        "browser.scroll",
        "browser.back",
        "browser.forward",
        "browser.refresh",
        "browser.wait",
        "browser.stop",
        "device.files.list",
        "device.files.search",
        "device.files.read",
        "device.files.open",
        "device.files.info",
        "device.files.create",
        "device.files.rename",
        "device.files.move",
        "device.files.copy",
        "device.files.edit",
        "device.files.trash",
        "media.status",
        "media.play",
        "media.pause",
        "media.next",
        "media.previous",
        "media.volume",
        "media.open_file",
        "media.open_url",
    }
)


#: Capabilities that need a live graphical session, not a configured root or shell.
#: They are advertised only when the process can actually reach the desktop — because a
#: ``device.screen.capture`` on a headless host is a capability that authorizes, prompts,
#: and then has nothing to capture, the failure mode this gate exists to stop.
REQUIRING_DESKTOP: frozenset[str] = frozenset(
    {
        "device.screen.capture",
        "device.screen.inspect",
        "device.gui.click",
        "device.gui.type",
        "device.gui.scroll",
    }
)


#: Capabilities that reach the browser session. Closed unless the operator opens
#: the ``allow_browser`` boundary (contract §4): a build without a browser
#: partition must not advertise ``browser.*`` — same rule as shell/desktop.
REQUIRING_BROWSER: frozenset[str] = frozenset(
    {
        "browser.open",
        "browser.navigate",
        "browser.search",
        "browser.inspect",
        "browser.snapshot",
        "browser.click",
        "browser.type",
        "browser.select",
        "browser.scroll",
        "browser.back",
        "browser.forward",
        "browser.refresh",
        "browser.wait",
        "browser.stop",
        # media.open_url drives the browser capability, so it shares this gate.
        "media.open_url",
    }
)

#: Capabilities that control a local media player. Closed unless the operator
#: opens the ``allow_media`` boundary (contract §4), so a headless agent never
#: advertises a player it cannot reach. ``media.open_file`` additionally needs
#: a workspace root (it opens a confined local file).
REQUIRING_MEDIA: frozenset[str] = frozenset(
    {
        "media.status",
        "media.play",
        "media.pause",
        "media.next",
        "media.previous",
        "media.volume",
        "media.open_file",
        "media.open_url",
    }
)

#: Capabilities that spend money. **Closed by default, and unlike every other gate that
#: is the whole point.**
#:
#: A file root buys file actions a directory to work in; a shell flag buys a command
#: policy to run under. A payment boundary buys nothing this build can honour: there is
#: no booking site, no payment processor, and no stored card behind either capability.
#: So an agent that advertised them by default would announce a CRITICAL action, force a
#: user through an approval card that promises a booking, and then fail at the last step
#: — the "authorized, prompted, then failed" outcome the other gates exist to prevent,
#: made worse by the fact that the consequential thing about it is the money.
#:
#: Advertising them requires an operator to open the boundary *and* the deployment to
#: have a backend that can actually submit; the handler below (``provider.py``) refuses
#: with ``PERMANENT`` when it does not, so "advertised" never becomes "pretended".
REQUIRING_PAYMENT: frozenset[str] = frozenset(
    {
        "booking.submit",
        "order.place",
    }
)


def register_device_actions(
    registry: Any, capabilities: Iterable[str] | None = None
) -> int:
    """Register device capabilities into ``registry``. Returns how many were new.

    ``capabilities`` is the subset the operator's configuration actually opens.
    **Registering a capability that is not enabled would make it resolvable, and a
    resolvable action is one the policy engine will consider.** Failing closed at the
    registry is the strongest place to fail: an unconfigured capability is then an
    unknown action — the same as one an LLM invented — and it never reaches a provider.

    Anything outside :data:`IMPLEMENTED` is ignored rather than registered, so a
    definition with no handler cannot be advertised into existence. When
    ``capabilities`` is omitted, exactly ``IMPLEMENTED`` is registered.

    Additive and idempotent: a registry that already knows one of these actions is
    left alone rather than double-registered, so an application that wires the agent
    twice cannot fail at startup for a reason that has nothing to do with security.
    """
    requested = set(IMPLEMENTED if capabilities is None else capabilities) & set(
        IMPLEMENTED
    )
    added = 0
    for definition in device_definitions():
        if definition.name not in requested:
            continue
        if registry.has(definition.name):
            continue
        registry.register(definition)
        added += 1
    return added


def advertised_ceiling(advertised: Iterable[str]) -> RiskLevel:
    """The risk ceiling this machine's own enrolment should carry.

    A device's ceiling is what that machine may **execute**; it is not a permission
    to do anything. Every node still passes the policy engine, approval, consent and
    the credential ceiling, exactly as before. What the ceiling must not do is make a
    capability impossible to use: ``DeviceInfo.risk_ceiling`` defaults to ``LOW``
    as a fail-closed default, so a laptop that honestly advertises a
    ``MEDIUM``-risk capability and enrols at ``LOW`` has announced a capability that
    can never run — the registry would refuse it after every approval has been given,
    which is the "authorized, prompted, then failed" outcome the capability gates
    exist to prevent.

    So the ceiling is derived from what this agent actually advertises, and nothing
    wider. An agent with no workspace, no shell and no desktop advertises only
    ``LOW``-risk reads and enrols at ``LOW``; turning on ``allow_shell`` raises it to
    ``CRITICAL`` because ``device.terminal.execute`` is genuinely critical, and
    ``allow_desktop`` alone raises it to ``MEDIUM``. The answer moves only when an
    operator moves a boundary in this process's own configuration.

    That is the correct owner for the value. The device ceiling is set by the machine's
    owner at enrolment; for this laptop the owner is the person who started the agent
    with those flags, so re-deriving it at startup is the owner restating a decision,
    not a third party raising one. Nothing else can move it upwards: pairing sets the
    *phone's* ceiling, never the laptop's, and :meth:`DeviceRegistry.set_risk_ceiling`
    still refuses every increase.

    It also means a boundary being closed lowers the ceiling, which is the direction
    that needs no ceremony.
    """
    catalog = {definition.name: definition.risk_level for definition in device_definitions()}
    ceiling = RiskLevel.LOW
    for capability in advertised:
        risk = catalog.get(capability)
        if risk is not None:
            ceiling = max_risk(ceiling, risk)
    return ceiling


__all__ = [
    "CAPABILITIES",
    "IMPLEMENTED",
    "PROVIDER",
    "REQUIRING_BROWSER",
    "REQUIRING_DESKTOP",
    "REQUIRING_FILE_ROOT",
    "REQUIRING_MEDIA",
    "REQUIRING_PAYMENT",
    "REQUIRING_SHELL",
    "advertised_ceiling",
    "device_definitions",
    "register_device_actions",
]
