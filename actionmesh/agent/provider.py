"""C19 laptop provider: the executor's adapter for this machine.

This is where an authorized ActionMesh action becomes a system call. Three rules
govern everything below, and they are here because they are easy to violate by
accident while adding the next capability.

**The policy engine has already said yes by the time this code runs.** Nothing here
decides whether an action is allowed — no risk assessment, no consent lookup, no
"if the caller looks trusted". The one thing the provider does decide is whether the
request is *physically* possible inside the boundaries it was configured with, and
that is a capability check, not an authorization check. Conflating the two is how a
second policy engine gets built by someone who meant well.

**A refusal quotes the caller's spelling, never a resolved path.** Every error goes
out as :class:`~actionmesh.agent.errors.AgentError` with the relative path or the
argv as supplied, because these strings reach an approval prompt and an audit record.

**``observe()`` is the verification channel, and it re-reads the world.** C6 will not
believe this provider's ``execute()`` — a claim of success is weight 1 and cannot
confirm anything by itself. So ``observe()`` stats, reads, and hashes again *after*
the fact, which is what makes "the write was reported but the file is not there" a
refutation rather than a story.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from actionmesh.agent import apps, connectors, parallelworld
from actionmesh.agent.browser import BridgeDriver, BrowserEngine
from actionmesh.agent.browser_bridge import BrowserBridgeConfig
from actionmesh.agent.browser_bridge import make_request as _make_browser_request
from actionmesh.agent.commands import CommandDecision, CommandPolicy
from actionmesh.agent.definitions import PROVIDER
from actionmesh.agent.errors import (
    AgentError,
    PathNotFound,
    ProcessError,
)
from actionmesh.agent.files import FilesEngine
from actionmesh.agent.ides import KNOWN_IDES, EditorDiscovery, EditorNotFound
from actionmesh.agent.media import MediaEngine, MprisMediaDriver
from actionmesh.agent.paths import ResolvedPath, Workspace
from actionmesh.agent.processes import (
    CONTINUUM_ARGUMENTS,
    CONTINUUM_NAME,
    CONTINUUM_PORT,
    EXITED,
    RUNNING,
    STOPPED,
    MissingServerName,
    ProcessObservation,
    ProcessRecord,
    ProcessRegistry,
    continuum_http_ready,
    is_listening,
    loopback_http_observation,
    stop_group,
)
from actionmesh.agent.projects import scan as scan_projects
from actionmesh.agent.runner import CommandOutcome, CommandRunner
from actionmesh.core.output import output_callback
from actionmesh.core.scope import current_execution_id
from actionmesh.protocol.models import Action, FailureKind, VerificationResult
from actionmesh.providers.base import ActionProvider, ProviderError, ProviderResult

#: Read ceiling for one ``device.file.read``. A phone asking for a 40 GB log is a
#: resource-exhaustion attempt whether or not anyone meant it as one.
MAX_READ_BYTES = 256 * 1024
#: Write ceiling for one ``device.file.write``. Bounded so the read-back that
#: verifies the write stays a cheap, exact comparison rather than a sample.
MAX_WRITE_BYTES = 1024 * 1024
#: Entries returned by one listing.
MAX_LIST_ENTRIES = 500
#: How much of a captured log one read may return. It matches the per-node budget on the
#: transient channel that carries it, so a read cannot become a way to stream a whole
#: disk to a phone through repeated calls.
MAX_LOG_READ_BYTES = 64 * 1024
#: How long a declared server port gets to start answering before the launch is
#: reported as not listening. Interpreter startup plus a bind is normally well under a
#: second; this is a ceiling, not a wait.
SERVER_READY_S = 5.0


class LaptopProvider(ActionProvider):
    """Real, local execution inside the configured boundaries."""

    name = PROVIDER
    #: Not a simulator. Every result that says otherwise would be a lie about the
    #: machine, and ``simulated`` is the flag the whole reporting layer turns on.
    simulated = False

    #: Capabilities this provider handles. Must match
    #: ``definitions.IMPLEMENTED`` — a test asserts it, because the two lists drifting
    #: means either an advertised capability nobody serves or a served one nobody can
    #: ask for.
    SUPPORTED = frozenset(
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
            *connectors.CONNECTOR_ACTIONS,
            apps.MAC_APP_ACTION,
            apps.MAC_APP_STATUS,
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
            # C42: the consequential web submits. Claimed here because a handler exists
            # for them — one that refuses with ``PERMANENT`` when no payment backend is
            # configured. ``SUPPORTED == definitions.IMPLEMENTED`` is asserted by a test,
            # and the alternative to claiming them is a capability the agent can be
            # asked for and has no answer for at all.
            "booking.submit",
            "order.place",
            # C42: the controlled browser and its thirteen named operations.
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
            # C42: the richer confined file verbs.
            "device.files.list",
            "device.files.search",
            "device.files.read",
            "device.files.compare",
            "device.files.open",
            "device.files.info",
            "device.files.hash",
            "device.files.create",
            "device.files.rename",
            "device.files.move",
            "device.files.copy",
            "device.files.edit",
            "device.files.trash",
            "device.files.preview_plan",
            # C42: local playback through the MPRIS adapter (read-back verified).
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

    #: Actions that must be awaited: they create or stop a process, or probe a socket.
    ASYNC_ACTIONS = frozenset(
        {
            "device.terminal.execute",
            "device.process.logs",
            "device.process.start",
            "device.process.stop",
            "device.server.start",
            "device.server.stop",
            "device.server.status",
            "device.ide.list",
            "device.ide.open",
            "device.ide.command",
            *connectors.CONNECTOR_ACTIONS,
            apps.MAC_APP_ACTION,
            apps.MAC_APP_STATUS,
            "device.app.open",
            "device.app.close",
            "device.project.status",
            "device.project.open",
            # C38: capture and inspect block on D-Bus / the accessibility bus, so they
            # are awaited off the event loop like the socket probes above.
            "device.screen.capture",
            "device.screen.inspect",
            # C38: the GUI actions walk and drive the accessibility tree over D-Bus.
            # Every call is a blocking round-trip to another process, so they are
            # awaited off the event loop for the same reason as the probes above.
            "device.gui.click",
            "device.gui.type",
            "device.gui.scroll",
            # C42: a submit blocks on a remote checkout, so it is awaited. It is in
            # ``ASYNC_ACTIONS`` for the same reason the socket probes are — off the event
            # loop — and, incidentally, that is also the path that has no workspace
            # guard, which is correct: a submit drives a website, not a path.
            "booking.submit",
            "order.place",
            # C42: browser operations block on IPC to another process (the
            # desktop window), so they are awaited off the event loop.
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
            # C42: file verbs walk directories and copy/edit/trash, which block
            # on I/O and D-Bus: awaited off the event loop.
            "device.files.list",
            "device.files.search",
            "device.files.read",
            "device.files.compare",
            "device.files.open",
            "device.files.info",
            "device.files.hash",
            "device.files.create",
            "device.files.rename",
            "device.files.move",
            "device.files.copy",
            "device.files.edit",
            "device.files.trash",
            "device.files.preview_plan",
            # C42: media operations round-trip to the player over D-Bus.
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

    def __init__(
        self,
        workspace: Workspace | None = None,
        *,
        command_policy: CommandPolicy | None = None,
        runner: CommandRunner | None = None,
        processes: ProcessRegistry | None = None,
        server_ready_s: float = SERVER_READY_S,
        log_dir: str = "",
        ledger: Any = None,
        device_id: str = "",
        browser_bridge: BrowserBridgeConfig | None = None,
        managed_service_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.workspace = workspace
        #: The laptop's capability boundary. `None` means no program may be run at
        #: all, which is the state a freshly installed agent is in.
        self.command_policy = command_policy
        self.runner = runner or CommandRunner()
        #: Children this agent started. Deliberately owned by the provider: the only
        #: processes this capability may touch are the ones in here.
        self.processes = processes or ProcessRegistry()
        #: Persistent project service records are an allowlist for post-restart HTTP
        #: observations. They never grant process-control authority.
        self.managed_service_lookup = managed_service_lookup
        #: How long a declared port gets to start answering before the launch reports
        # `not listening`. Configurable because it is a patience setting, not a
        # security one -- and a test should not wait five seconds for a verdict.
        self.server_ready_s = float(server_ready_s)
        #: Where a detached child's output goes, when the operator opened a place for
        #: it. Empty means the child is launched with its output discarded, which is how
        #: it worked before logs existed and remains the default for a reason: a log is
        #: whatever some program chose to print, kept on disk.
        self.log_dir = log_dir
        #: Effects this provider produced, keyed by ``action_id``, so ``observe()``
        #: knows what to look for. Cleared by the caller between runs if it wants to.
        self._effects: dict[str, dict[str, Any]] = {}
        #: C35 -- where the work this agent actually started is written down, so a
        #: process that never saw the request can still answer for it. ``None`` keeps a
        #: bare provider (a test, a simulation, an agent without durable storage) exactly
        #: as it was: nothing is journalled, and reconciliation then honestly reports
        #: that it has no evidence.
        self.ledger: Any = ledger
        #: C36 — the registered computer this provider *is*. The executor refuses to
        #: hand it a graph addressed to a different one; an empty value means "an
        #: embedded provider that never declared itself", which is fine for a test and
        #: is never enough to accept a named target.
        self.device_id = device_id
        self.processes.journal = self._journal_child
        #: C42 — the controlled browser session, driven only by the named
        #: operations. The driver is chosen by the host's *configuration*, never
        #: by the request: when a bridge config is present the engine drives the
        #: hardened window over the drive socket; otherwise it is a ``FakeDriver``
        #: whose verbs answer ``unavailable`` honestly rather than claiming a
        #: bridge that is not there. ``api.py`` builds the config from the env
        #: main wrote at spawn, so the provider core never opens IPC itself.
        self.browser: BrowserEngine = (
            BrowserEngine(BridgeDriver(_make_browser_request(browser_bridge)))
            if browser_bridge is not None and browser_bridge.configured
            else BrowserEngine()
        )
        #: C42 — the confined richer file verbs. ``FilesEngine`` reuses
        #: ``Workspace`` for every resolution; the provider delegates and records
        #: effects on the way back, exactly as it does for ``device.file.*``.
        self.files: FilesEngine | None = (
            FilesEngine(workspace) if workspace is not None else None
        )
        #: C42 — local playback through MPRIS. ``MediaEngine`` is constructed
        #: with the driver, the configured workspace (for ``open_file``
        #: confinement), and a ``browser_open`` delegate that routes
        #: ``open_url`` through the browser capability. Constructed eagerly so a
        #: capability that is not advertised is still honestly ``unavailable``
        #: rather than ``not supported``.
        #: Legacy sync delegate: the engine's ``open_url`` takes a callable
        #: returning a plain dict, and this handler runs in ``_execute_async``
        #: as an awaited call, so the async path (below, ``_media_open_url``)
        #: is the one that runs. Kept honest by reading the live engine state
        #: rather than navigating: it never claims "opened".
        _browser = self.browser
        def _media_open_url(url: str) -> dict[str, Any]:  # sync delegate for MediaEngine
            if not _browser.is_open:
                return {"status": "unavailable", "reason": "no browser session — call open first"}
            return {"status": "ok", "url": url, "note": "media.open_url delegates to the controlled browser; use browser.navigate for a verified navigation"}
        #: The media engine is constructed eagerly so a capability that is not
        #: advertised is still honestly ``unavailable`` rather than ``not
        #: supported``. Its ``open_url`` delegates through the *browser
        #: capability*, and the provider's ``_media_open_url`` is async — the
        #: engine's sync callable is only the legacy delegate above; the real
        #: async navigation happens in the provider wrapper.
        self.media: MediaEngine = MediaEngine(
            MprisMediaDriver(),
            workspace=workspace,
            browser_open=_media_open_url,
        )

    # -- dispatch ---------------------------------------------------------
    def supports(self, action: Action) -> bool:
        # Matched on the action *type*, not on `action.provider`. That field arrives
        # from the request and defaults to "mock" when a caller omits it — so an
        # adapter that only answered for provider="laptop" would watch the generic mock
        # fallback pick up its capabilities and simulate them. The capability name is
        # the contract; the provider string is a hint from a caller.
        return action.type in self.SUPPORTED

    async def execute(self, action: Action) -> ProviderResult:
        if action.type in self.ASYNC_ACTIONS:
            return await self._execute_async(action)
        handler: dict[str, Any] | None
        try:
            if action.type not in self.SUPPORTED:
                raise ProviderError(
                    f"this build cannot perform {action.type!r}", FailureKind.PERMANENT
                )
            workspace = self._require_workspace()
            handler = await asyncio.to_thread(self._perform, action, workspace)
            self._stream_content(action, handler)
        except ProviderError:
            raise
        except (AgentError, OSError) as exc:
            # A confinement refusal is a denial, not a transient fault: retrying it
            # reproduces it, and the executor must not treat it as worth another go.
            raise ProviderError(str(exc), FailureKind.AUTHORIZATION) from exc
        assert handler is not None
        self._journal_effect(action)
        # `simulated` comes from the class, not the default. ProviderResult defaults
        # to True, and a real adapter that forgets to say otherwise reports its work
        # as a simulation — the one flag every later layer reads to decide whether a
        # success was real.
        return ProviderResult(
            ok=True,
            provider=self.name,
            action_id=action.action_id,
            data=handler,
            simulated=self.simulated,
        )

    async def _execute_async(self, action: Action) -> ProviderResult:
        """Awaitable handlers: they spawn, stop, or probe, and none of that is sync.

        Kept apart from the filesystem half because a synchronous ``_perform`` cannot
        await a child, and pretending otherwise would mean blocking the server's event
        loop on whatever the phone asked to run.
        """
        handlers: dict[str, Any] = {
            "device.terminal.execute": lambda: self._terminal(action),
            "device.process.logs": lambda: self._process_logs(action),
            "device.process.start": lambda: self._start_child(action, server=False),
            "device.server.start": lambda: self._start_child(action, server=True),
            "device.process.stop": lambda: self._stop_process(action),
            "device.server.stop": lambda: self._stop_server(action),
            "device.server.status": lambda: self._server_status(action),
            "device.ide.list": lambda: self._ide_list(),
            "device.ide.open": lambda: self._ide_open(action),
            "device.ide.command": lambda: self._ide_command(action),
            "device.project.status": lambda: self._project_status(action),
            "device.project.open": lambda: self._ide_open(action, project=True),
            **{name: lambda: self._connector_read(action) for name in connectors.CONNECTOR_ACTIONS},
            apps.MAC_APP_ACTION: lambda: self._launch_native_app(action),
            apps.MAC_APP_STATUS: lambda: self._native_app_status(action),
            "device.app.open": lambda: self._app_open(action),
            "device.app.close": lambda: self._app_close(action),
            "device.screen.capture": lambda: self._screen_capture(action),
            "device.screen.inspect": lambda: self._screen_inspect(action),
            "device.gui.click": lambda: self._gui_click(action),
            "device.gui.type": lambda: self._gui_type(action),
            "device.gui.scroll": lambda: self._gui_scroll(action),
            "booking.submit": lambda: self._submit(action),
            "order.place": lambda: self._submit(action),
            # C42 — the controlled browser. Each named operation is one method
            # on ``BrowserEngine`` and one registered capability. There is no
            # ``browser.eval`` and no ``browser.run_python`` here.
            "browser.open": lambda: self._browser_open(action),
            "browser.navigate": lambda: self._browser_navigate(action),
            "browser.search": lambda: self._browser_search(action),
            "browser.inspect": lambda: self._browser_inspect(action),
            "browser.snapshot": lambda: self._browser_snapshot(action),
            "browser.click": lambda: self._browser_click(action),
            "browser.type": lambda: self._browser_type(action),
            "browser.select": lambda: self._browser_select(action),
            "browser.scroll": lambda: self._browser_scroll(action),
            "browser.back": lambda: self._browser_back(action),
            "browser.forward": lambda: self._browser_forward(action),
            "browser.refresh": lambda: self._browser_refresh(action),
            "browser.wait": lambda: self._browser_wait(action),
            "browser.stop": lambda: self._browser_stop(action),
            # C42 — the richer confined file verbs. Each is sync and confined
            # to the workspace; it runs off the loop in a worker thread.
            "device.files.list": lambda: self._files_list(action),
            "device.files.search": lambda: self._files_search(action),
            "device.files.read": lambda: self._files_read(action),
            "device.files.compare": lambda: self._files_compare(action),
            "device.files.open": lambda: self._files_open(action),
            "device.files.info": lambda: self._files_info(action),
            "device.files.hash": lambda: self._files_hash(action),
            "device.files.create": lambda: self._files_create(action),
            "device.files.rename": lambda: self._files_rename(action),
            "device.files.move": lambda: self._files_move(action),
            "device.files.copy": lambda: self._files_copy(action),
            "device.files.edit": lambda: self._files_edit(action),
            "device.files.trash": lambda: self._files_trash(action),
            "device.files.preview_plan": lambda: self._files_preview_plan(action),
            # C42 — local playback through MPRIS. Each is a read-back
            # verified command on the media engine.
            "media.status": lambda: self._media_status(action),
            "media.play": lambda: self._media_play(action),
            "media.pause": lambda: self._media_pause(action),
            "media.next": lambda: self._media_next(action),
            "media.previous": lambda: self._media_previous(action),
            "media.volume": lambda: self._media_volume(action),
            "media.open_file": lambda: self._media_open_file(action),
            "media.open_url": lambda: self._media_open_url(action),
        }
        handler = handlers.get(action.type)
        if handler is None:
            raise ProviderError(
                f"this build cannot perform {action.type!r}", FailureKind.PERMANENT
            )
        try:
            data = await handler()
        except ProviderError:
            raise
        except AgentError as exc:
            raise ProviderError(str(exc), FailureKind.AUTHORIZATION) from exc
        # Async handlers produce content too: an accessibility walk's element list is
        # the whole point of `device.screen.inspect`, and it is stripped from the result
        # payload by the safe view. Emitted after the handler returns, on the loop, for
        # the same reason as the sync path.
        self._stream_content(action, data)
        self._journal_effect(action)
        return ProviderResult(
            ok=True,
            provider=self.name,
            action_id=action.action_id,
            data=data,
            simulated=self.simulated,
        )

    # -- C42: browser, files, media dispatch -----------------------------
    #
    # Each method below is one thin named wrapper around an engine. The
    # provider's job here is argument extraction and effect recording; the
    # engine owns the operation. ``_effects`` recording is the provider's
    # job (engines do not thread action ids), exactly as for ``device.file.*``.
    # No ``verify()`` override — verification stays the engine's job.

    async def _browser_open(self, action: Action) -> dict[str, Any]:
        url = action.parameters.get("url")
        if not isinstance(url, str) or not url:
            raise ProviderError("browser.open requires a url", FailureKind.INVALID_INPUT)
        result = await self.browser.open(url)
        self._record_browser_effect(action, result)
        return result

    async def _browser_navigate(self, action: Action) -> dict[str, Any]:
        url = action.parameters.get("url")
        if not isinstance(url, str) or not url:
            raise ProviderError("browser.navigate requires a url", FailureKind.INVALID_INPUT)
        result = await self.browser.navigate(url)
        self._record_browser_effect(action, result)
        return result

    async def _browser_search(self, action: Action) -> dict[str, Any]:
        query = action.parameters.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ProviderError("browser.search requires a query", FailureKind.INVALID_INPUT)
        engine = action.parameters.get("engine")
        if engine is not None and not isinstance(engine, str):
            raise ProviderError("browser.search engine must be a string", FailureKind.INVALID_INPUT)
        result = await self.browser.search(query, engine)
        self._record_browser_effect(action, result)
        return {key: value for key, value in result.items() if key != "results"}

    async def _browser_inspect(self, action: Action) -> dict[str, Any]:
        result = await self.browser.inspect()
        self._record_browser_effect(action, result)
        return result

    async def _browser_snapshot(self, action: Action) -> dict[str, Any]:
        result = await self.browser.snapshot()
        self._record_browser_effect(action, result)
        return result

    async def _browser_click(self, action: Action) -> dict[str, Any]:
        raw_ref = action.parameters.get("ref")
        if not isinstance(raw_ref, int) or isinstance(raw_ref, bool):
            raise ProviderError("browser.click requires an integer ref", FailureKind.INVALID_INPUT)
        ref = raw_ref
        double = bool(action.parameters.get("double", False))
        result = await self.browser.click(ref, double)
        self._record_browser_effect(action, result)
        return result

    async def _browser_type(self, action: Action) -> dict[str, Any]:
        ref = action.parameters.get("ref")
        text = action.parameters.get("text", "")
        if ref is None:
            raise ProviderError("browser.type requires a ref", FailureKind.INVALID_INPUT)
        if not isinstance(text, str):
            raise ProviderError("browser.type text must be a string", FailureKind.INVALID_INPUT)
        result = await self.browser.type_(ref, text)
        self._record_browser_effect(action, result)
        return result

    async def _browser_select(self, action: Action) -> dict[str, Any]:
        ref = action.parameters.get("ref")
        value = action.parameters.get("value")
        if ref is None:
            raise ProviderError("browser.select requires a ref", FailureKind.INVALID_INPUT)
        if not isinstance(value, str):
            raise ProviderError("browser.select value must be a string", FailureKind.INVALID_INPUT)
        result = await self.browser.select(ref, value)
        self._record_browser_effect(action, result)
        return result

    async def _browser_scroll(self, action: Action) -> dict[str, Any]:
        ref = action.parameters.get("ref")
        direction = action.parameters.get("direction", "down")
        amount = action.parameters.get("amount", 300)
        result = await self.browser.scroll(ref, direction, amount)
        self._record_browser_effect(action, result)
        return result

    async def _browser_back(self, action: Action) -> dict[str, Any]:
        result = await self.browser.back()
        self._record_browser_effect(action, result)
        return result

    async def _browser_forward(self, action: Action) -> dict[str, Any]:
        result = await self.browser.forward()
        self._record_browser_effect(action, result)
        return result

    async def _browser_refresh(self, action: Action) -> dict[str, Any]:
        result = await self.browser.refresh()
        self._record_browser_effect(action, result)
        return result

    async def _browser_wait(self, action: Action) -> dict[str, Any]:
        ms = action.parameters.get("ms")
        until = action.parameters.get("until")
        result = await self.browser.wait(ms, until)
        self._record_browser_effect(action, result)
        return result

    async def _browser_stop(self, action: Action) -> dict[str, Any]:
        # The engine's `stop()` is a deliberate zero-argument session teardown:
        # it takes no reason, because no page learns why a session ended. The
        # caller's reason is read (so a malformed parameter fails as input)
        # but never forwarded to the driver or the page.
        reason = action.parameters.get("reason", "stopped")
        if reason is not None and not isinstance(reason, str):
            raise ProviderError("browser.stop reason must be a string", FailureKind.INVALID_INPUT)
        result = await self.browser.stop()
        self._record_browser_effect(action, result)
        return result

    def _record_browser_effect(self, action: Action, result: dict[str, Any]) -> None:
        """Record the engine's read-back as the verifier's witness, iff it is ``ok``.

        The browser engine already reads the page back as part of every operation
        (a navigation refreshes the facts, a click refreshes them after the
        click, a snapshot rebuilds the ref map). Wait — the click handler does
        not itself re-read; observe() therefore must not be the one to do so
        either. What is recorded here is the *result the engine handed back*,
        captured as a structural copy, so observe() can answer from the recorded
        effect without issuing a second network request.

        Only ``status == "ok"`` is a witness. Every refusal — ``stale``,
        ``needs_user``, ``unavailable``, ``error`` — is no evidence at all, and
        recording it as an effect would let verify() match on the label alone.
        On a refusal we record nothing, exactly as the filesystem handlers do
        when they raise.

        Bounded and sanitized: the stored dict carries url/title and small
        counts/flags only — never page text, element bodies, cookies, or
        credentials. The engine already redacts those before returning, but the
        record is re-sanitized here so a future engine change cannot leak them
        into the ledger.
        """
        if not isinstance(result, dict) or result.get("status") != "ok":
            return
        effect: dict[str, Any] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "status": "ok",
        }
        # Only fixed, bounded evidence keys. Anything else (page text, refs,
        # references into the window subtree, credentials) stays out.
        for key in (
            "url", "title", "url_after", "changed", "inserted",
            "element_count", "count", "ref_map_size", "truncated",
        ):
            if key in result:
                effect[key] = result[key]
        self._effects[action.action_id] = effect

    # -- C42: files -------------------------------------------------------
    def _files_engine(self) -> FilesEngine:
        engine = self.files
        if engine is None:
            raise ProviderError("no workspace configured", FailureKind.AUTHORIZATION)
        return engine

    async def _files_list(self, action: Action) -> dict[str, Any]:
        engine = self._files_engine()
        path = action.parameters.get("path", "")
        limit = action.parameters.get("limit")
        return await asyncio.to_thread(engine.list_, path, limit)

    async def _files_search(self, action: Action) -> dict[str, Any]:
        engine = self._files_engine()
        path = action.parameters.get("path", "")
        pattern = action.parameters.get("pattern", "")
        limit = action.parameters.get("limit")
        return await asyncio.to_thread(engine.search, path, pattern, limit)

    async def _files_read(self, action: Action) -> dict[str, Any]:
        engine = self._files_engine()
        path = action.parameters.get("path", "")
        if not isinstance(path, str) or not path:
            raise ProviderError("device.files.read requires a path", FailureKind.INVALID_INPUT)
        result = await asyncio.to_thread(engine.read, path)
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "path": path,
            "size": result.get("size"),
            "sha256": result.get("sha256"),
        }
        return result

    async def _files_compare(self, action: Action) -> dict[str, Any]:
        changed = await asyncio.to_thread(
            self._files_engine().compare, action.parameters["left"], action.parameters["right"],
        )
        return {"changed": changed}

    async def _files_open(self, action: Action) -> dict[str, Any]:
        engine = self._files_engine()
        path = action.parameters.get("path", "")
        return await asyncio.to_thread(engine.open_, path)

    async def _files_info(self, action: Action) -> dict[str, Any]:
        engine = self._files_engine()
        path = action.parameters.get("path", "")
        return await asyncio.to_thread(engine.info, path)

    async def _files_hash(self, action: Action) -> dict[str, Any]:
        engine = self._files_engine()
        path = action.parameters.get("path", "")
        return await asyncio.to_thread(engine.hash, path)

    async def _files_create(self, action: Action) -> dict[str, Any]:
        engine = self._files_engine()
        path = action.parameters.get("path", "")
        content = action.parameters.get("content", "")
        result = await asyncio.to_thread(engine.create, path, content)
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "path": path,
            "bytes": result.get("bytes"),
            "sha256": result.get("sha256"),
        }
        return result

    async def _files_rename(self, action: Action) -> dict[str, Any]:
        engine = self._files_engine()
        path = action.parameters.get("path", "")
        to = action.parameters.get("to", "")
        result = await asyncio.to_thread(engine.rename, path, to)
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "path": path,
            "to": to,
        }
        return result

    async def _files_move(self, action: Action) -> dict[str, Any]:
        engine = self._files_engine()
        path = action.parameters.get("path", "")
        to = action.parameters.get("to", "")
        result = await asyncio.to_thread(engine.move, path, to)
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "path": path,
            "to": to,
        }
        return result

    async def _files_copy(self, action: Action) -> dict[str, Any]:
        engine = self._files_engine()
        path = action.parameters.get("path", "")
        to = action.parameters.get("to", "")
        result = await asyncio.to_thread(engine.copy, path, to)
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "path": path,
            "to": to,
            "copied": result.get("copied"),
        }
        return result

    async def _files_edit(self, action: Action) -> dict[str, Any]:
        engine = self._files_engine()
        path = action.parameters.get("path", "")
        find = action.parameters.get("find", "")
        replace = action.parameters.get("replace", "")
        replace_all = bool(action.parameters.get("all", False))
        result = await asyncio.to_thread(engine.edit, path, find, replace, replace_all)
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "path": path,
            "sha256": result.get("sha256"),
            "replacements": result.get("replacements"),
        }
        return result

    async def _files_trash(self, action: Action) -> dict[str, Any]:
        engine = self._files_engine()
        path = action.parameters.get("path", "")
        result = await asyncio.to_thread(engine.trash, path)
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "path": path,
            "trashed": result.get("trashed", False),
        }
        return result

    async def _files_preview_plan(self, action: Action) -> dict[str, Any]:
        if self.workspace is None:
            raise ProviderError("no workspace configured", FailureKind.AUTHORIZATION)
        record, summary = await asyncio.to_thread(
            parallelworld.preview, self.workspace, action.parameters.get("operations")
        )
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "preview_id": record.preview_id,
            "operations": action.parameters.get("operations"),
        }
        return {**summary, "diff": record.diff}

    # -- C42: media -------------------------------------------------------
    async def _media_status(self, action: Action) -> dict[str, Any]:
        result = await asyncio.to_thread(self.media.status)
        self._record_media_effect(action, result)
        return result

    async def _media_play(self, action: Action) -> dict[str, Any]:
        ref = action.parameters.get("ref")
        result = await asyncio.to_thread(self.media.play, ref)
        self._record_media_effect(action, result)
        return result

    async def _media_pause(self, action: Action) -> dict[str, Any]:
        ref = action.parameters.get("ref")
        result = await asyncio.to_thread(self.media.pause, ref)
        self._record_media_effect(action, result)
        return result

    async def _media_next(self, action: Action) -> dict[str, Any]:
        result = await asyncio.to_thread(self.media.next_track)
        self._record_media_effect(action, result)
        return result

    async def _media_previous(self, action: Action) -> dict[str, Any]:
        result = await asyncio.to_thread(self.media.previous_track)
        self._record_media_effect(action, result)
        return result

    async def _media_volume(self, action: Action) -> dict[str, Any]:
        level = action.parameters.get("level")
        muted = action.parameters.get("muted")
        result = await asyncio.to_thread(self.media.volume, level, muted)
        self._record_media_effect(action, result)
        return result

    async def _media_open_file(self, action: Action) -> dict[str, Any]:
        path = action.parameters.get("path", "")
        result = await asyncio.to_thread(self.media.open_file, path)
        self._record_media_effect(action, result)
        return result

    def _record_media_effect(self, action: Action, result: dict[str, Any]) -> None:
        """Record the engine's read-back as the verifier's witness, iff it is ``ok``.

        The media engine already reads the player back after every command
        (a play is confirmed by the reported ``playing`` state, a volume set
        by the reported level). What is recorded here is the *result the
        engine handed back*, captured as a structural copy, so observe() can
        answer from the recorded effect without issuing a second MPRIS query.

        Only ``status == "ok"`` is a witness. Every refusal —
        ``unavailable``, ``ambiguous``, ``no_effect``, ``unsupported``,
        ``invalid_input`` — is no evidence at all, and recording it as an
        effect would let verify() match on the label alone. On a refusal we
        record nothing, exactly as the filesystem handlers do when they raise.

        Bounded and sanitized: the stored dict carries player identity (the
        bus name, already a public identifier), track title, state, level,
        muted, and flags only — never arbitrary metadata, file paths, or
        secrets. The engine already reports only what a player announced, but
        the record is re-sanitized here so a future engine change cannot leak
        more into the ledger.
        """
        if not isinstance(result, dict) or result.get("status") != "ok":
            return
        effect: dict[str, Any] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "status": "ok",
        }
        # Only fixed, bounded evidence keys. Anything else (file URIs, raw
        # metadata blobs, position counters) stays out.
        for key in (
            "player", "track", "state", "level", "muted", "changed",
            "players", "position_s", "duration_s", "opened", "path", "url",
        ):
            if key in result:
                effect[key] = result[key] if key != "players" else result[key][:8]
        # ``players`` is a bounded list of bus names; cap it like ``results``.
        if isinstance(effect.get("players"), list):
            effect["players"] = effect["players"][:8]
        self._effects[action.action_id] = effect

    async def _media_open_url(self, action: Action) -> dict[str, Any]:
        url = action.parameters.get("url", "")
        # ``open_url`` drives the controlled browser, not a media pipeline.
        # The engine is async, so await it here rather than through a sync
        # delegate that cannot verify the navigation.
        if not isinstance(url, str) or not url:
            return {"status": "unavailable", "reason": "url must be a non-empty string"}
        return await self.browser.navigate(url)

    def _perform(self, action: Action, workspace: Workspace) -> dict[str, Any]:
        params = action.parameters
        if action.type == "device.file.list":
            return self._list(action, workspace, params)
        if action.type == "device.file.read":
            return self._read(action, workspace, params)
        if action.type == "device.file.write":
            return self._write(action, workspace, params)
        if action.type == "device.file.delete":
            return self._delete(action, workspace, params)
        if action.type == "device.process.list":
            return self._list_processes(action, params)
        if action.type == "device.project.list":
            return self._project_list(action, params)
        raise ProviderError(
            f"unsupported action {action.type!r}", FailureKind.PERMANENT
        )

    def _stream_content(self, action: Action, data: dict[str, Any]) -> None:
        """Send content to the observer that is already allowed to watch this execution.

        Two capabilities produce content: a file body, and the element list an
        accessibility walk returns. The provider's own result keeps both, because the
        verifier needs the real thing to check against; the safe result view then strips
        them, because a result payload reaches clients that never asked for that content.
        This is the one channel built for content: transient, bounded, actor-scoped,
        never audited and never replayed.

        Screen element *names* are content -- a window title, a document name, the text
        of a label -- so they travel here and not in the result. The result keeps the
        count and the refusal reason, which is what a caller needs to know that an
        observation happened at all.

        Emitted here, on the event loop, and not from inside ``_perform``: the sync
        handlers run in a worker thread, and waking a queue belonging to another thread
        is not something ``asyncio.Queue`` survives.
        """
        if action.type == "device.file.read":
            if data.get("binary"):
                # A PNG into a <pre> is not a preview; it is a wall of replacement
                # characters wearing someone's filename.
                return
            emit = output_callback()
            if emit is not None:
                emit("content", str(data.get("content") or ""))
            return
        if action.type == "device.screen.inspect" and data.get("status") == "ok":
            emit = output_callback()
            if emit is not None:
                # One whole JSON array in one event. The `elements` stream is framed as a
                # document, not as bytes: a reader given the first 4 KB of this array has
                # nothing, and the count in the result would then sit beside an empty
                # list the reader could only read as "the tree held nothing".
                emit("elements", json.dumps(data.get("elements") or [], default=str))
                if data.get("truncated"):
                    # The walk itself hit its element or depth ceiling. The names that
                    # did arrive are real; the ones past the ceiling were never read,
                    # and the panel is told so rather than left to infer completeness.
                    emit("elements.note", "the walk reached its element ceiling; "
                         "elements past it are not listed")
        # C42 — the richer file verbs. ``device.files.read`` carries its body
        # under ``content`` and ``device.files.edit`` carries a unified diff
        # under ``preview`` (diff only, never surrounding context). Both ride
        # the same transient channel as ``device.file.read``.
        if action.type == "device.files.read":
            if data.get("binary"):
                return
            emit = output_callback()
            if emit is not None:
                emit("content", str(data.get("content") or ""))
            return
        if action.type == "device.files.edit":
            emit = output_callback()
            if emit is not None:
                emit("files.preview", str(data.get("preview") or ""))
            return
        if action.type == "device.files.preview_plan":
            emit = output_callback()
            if emit is not None:
                emit("parallelworld.diff", str(data.get("diff") or ""))
            return
        # C42 — the browser. ``BrowserEngine`` already emits ``browser.url``,
        # ``browser.title`` and ``browser.elements`` itself via the same
        # output callback, and ``browser.page`` on inspect. The provider does
        # not re-emit those (no double-emit); it only emits the media meta
        # label here because the media engine does not own a stream.
        if action.type.startswith("media."):
            label = self.media_meta_label(data)
            if label is not None:
                emit = output_callback()
                if emit is not None:
                    emit("media.meta", label)

    def media_meta_label(self, data: dict[str, Any]) -> str | None:
        """The ``media.meta`` transient-stream payload for one result.

        Wins the race between "the engine's own label" and "a formatted
        '<player> — <track>' pair": the engine returns its own
        :meth:`MediaEngine.meta_label`, so this is a thin passthrough that
        keeps ``_stream_content`` from reaching into the engine's private
        shape. Returns ``None`` unless the action succeeded.
        """
        if data.get("status") != "ok":
            return None
        return self.media.meta_label(data)

    # -- files ------------------------------------------------------------
    def _list(self, action: Action, workspace: Workspace, params: dict[str, Any]) -> dict[str, Any]:
        raw = params.get("path", "")
        if not isinstance(raw, (str, type(None))):
            raise ProviderError("path must be a string", FailureKind.INVALID_INPUT)
        limit = params.get("limit", MAX_LIST_ENTRIES)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ProviderError("limit must be a positive integer", FailureKind.INVALID_INPUT)
        capped = min(limit, MAX_LIST_ENTRIES)
        try:
            entries = workspace.list(raw or "", limit=capped)
        except AgentError as exc:
            raise ProviderError(str(exc), _classify(exc)) from exc
        confined = [entry for entry in entries if entry.get("confined")]
        result = {
            "path": raw or ".",
            "entries": confined,
            "count": len(confined),
            # Reported rather than hidden: an entry below the root that is a link to
            # somewhere outside it is visible as *hidden*, because a listing that
            # silently drops entries teaches the reader nothing about the boundary.
            "hidden_outside_root": len(entries) - len(confined),
            "truncated": len(entries) >= capped,
        }
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "path": raw or ".",
            "count": len(confined),
        }
        return result

    def _read(self, action: Action, workspace: Workspace, params: dict[str, Any]) -> dict[str, Any]:
        path = self._path(params)
        resolved = self._resolve(workspace, path, must_exist=True)
        if os.path.getsize(resolved.absolute) > MAX_READ_BYTES:
            # No override, and none offered: a caller that could ask to raise the
            # ceiling is a caller that can stream the whole disk to a phone.
            raise ProviderError(
                f"{path!r} is larger than the {MAX_READ_BYTES} byte read limit; "
                "refusing to stream a whole file to a phone",
                FailureKind.INVALID_INPUT,
            )
        fd = workspace.open_fd(resolved, os.O_RDONLY)
        try:
            with os.fdopen(fd, "rb") as handle:
                payload = handle.read(MAX_READ_BYTES + 1)
        except OSError as exc:
            raise ProviderError(f"cannot read {path!r}: {exc}", FailureKind.PROVIDER) from exc
        truncated = len(payload) > MAX_READ_BYTES
        body = payload[:MAX_READ_BYTES]
        digest = hashlib.sha256(body).hexdigest()
        text, binary = _decode(body)
        result = {
            "path": resolved.relative,
            "size": len(body),
            "sha256": digest,
            "truncated": truncated,
            "binary": binary,
            "content": text if not binary else "",
        }
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "path": resolved.relative,
            "sha256": digest,
            "size": len(body),
        }
        return result

    def _write(self, action: Action, workspace: Workspace, params: dict[str, Any]) -> dict[str, Any]:
        path = self._path(params)
        content = params.get("content")
        if not isinstance(content, str):
            raise ProviderError(
                "content must be a string", FailureKind.INVALID_INPUT
            )
        body = content.encode("utf-8")
        if len(body) > MAX_WRITE_BYTES:
            raise ProviderError(
                f"content is {len(body)} bytes, over the {MAX_WRITE_BYTES} byte "
                "write limit",
                FailureKind.INVALID_INPUT,
            )
        append = bool(params.get("append"))
        resolved = self._resolve(workspace, path)
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
        fd = workspace.open_fd(resolved, flags)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ProviderError(f"cannot write {path!r}: {exc}", FailureKind.PROVIDER) from exc
        written = os.path.getsize(resolved.absolute)
        result = {
            "path": resolved.relative,
            "bytes": len(body),
            "appended": append,
            "size": written,
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "path": resolved.relative,
            "content": content,
            "sha256": result["sha256"],
        }
        return result

    def _delete(self, action: Action, workspace: Workspace, params: dict[str, Any]) -> dict[str, Any]:
        path = self._path(params)
        resolved = self._resolve(workspace, path, must_exist=True)
        if os.path.isdir(resolved.absolute):
            # Directory removal is recursive destruction with one word in it. Not
            # offered: an explicit, separate capability would need its own policy.
            raise ProviderError(
                f"{path!r} is a directory; this capability deletes files only",
                FailureKind.INVALID_INPUT,
            )
        try:
            workspace.unlink(resolved)
        except AgentError as exc:
            raise ProviderError(str(exc), _classify(exc)) from exc
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "path": resolved.relative,
            "deleted": True,
        }
        return {"path": resolved.relative, "deleted": True}

    # -- terminal ---------------------------------------------------------
    async def _terminal(self, action: Action) -> dict[str, Any]:
        params = action.parameters
        requested_timeout = params.get("timeout_s")
        if requested_timeout is not None and (
            not isinstance(requested_timeout, (int, float))
            or isinstance(requested_timeout, bool)
        ):
            raise ProviderError(
                "timeout_s must be a number", FailureKind.INVALID_INPUT
            )
        decision = self._decide(params)

        def noticed(process: Any) -> None:
            # The runner starts children in their own session, so the group id is the
            # pid: that is the handle a cancellation after a restart has to have, and
            # this is the only moment the child is both alive and attributable.
            self._save_row(
                action.action_id, kind="command", pid=process.pid or 0, pgid=process.pid or 0
            )

        try:
            outcome = await self.runner.run(
                decision,
                timeout_s=float(requested_timeout) if requested_timeout else None,
                on_output=output_callback(),
                on_spawn=noticed,
            )
        except ProcessError as exc:
            raise ProviderError(str(exc), FailureKind.PROVIDER) from exc
        result = self._record_terminal(action, decision, outcome)
        self._save_row(
            action.action_id,
            exit_code=outcome.exit_code,
            state="exited" if outcome.exit_code == 0 else "terminated",
        )
        return result

    def _new_log_path(self) -> str:
        """A fresh log file for a child about to be launched, or ``""`` for none.

        The name is unguessable on purpose. A log is addressed by the process id this
        registry issued and never by a path, so the filename carries nothing a client
        needs, and a name nobody can predict means a stray file in the directory cannot
        be reached by guessing.
        """
        if not self.log_dir:
            return ""
        os.makedirs(self.log_dir, mode=0o700, exist_ok=True)
        return os.path.join(self.log_dir, f"child-{secrets.token_hex(6)}.log")

    async def _process_logs(self, action: Action) -> dict[str, Any]:
        params = action.parameters
        process_id = params.get("process_id")
        if not isinstance(process_id, str) or not process_id.strip():
            raise ProviderError("process_id is required", FailureKind.INVALID_INPUT)
        wanted = params.get("max_bytes", MAX_LOG_READ_BYTES)
        if isinstance(wanted, bool) or not isinstance(wanted, int) or wanted < 1:
            raise ProviderError(
                "max_bytes must be a positive integer", FailureKind.INVALID_INPUT
            )
        self.processes.reap()
        if self.processes.get(process_id) is None:
            # The same refusal as stopping: an id this agent never issued is the shape
            # of a guess, and there is no path from here to some other program's output.
            raise ProviderError(
                f"no process {process_id!r} was started by this laptop agent; only "
                "processes it launched have logs here",
                FailureKind.AUTHORIZATION,
            )
        read = self.processes.read_log(
            process_id, max_bytes=min(wanted, MAX_LOG_READ_BYTES)
        )
        if read is None:
            raise ProviderError(
                "this process was launched with its output discarded; run it as a "
                "command instead to watch its output",
                FailureKind.INVALID_INPUT,
            )
        body, truncated = read
        emit = output_callback()
        if emit is not None and body:
            # The bytes travel the transient channel, exactly as a file body does: the
            # result payload keeps only the shape of what was read.
            emit("logs", body)
        result = {
            "process_id": process_id,
            "bytes": len(body.encode("utf-8", errors="replace")),
            "lines": body.count("\n"),
            "truncated": truncated,
        }
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "process_id": process_id,
            "bytes": result["bytes"],
        }
        return result

    def _observe_child(
        self, action: Action, recorded: dict[str, Any], *, started: bool
    ) -> dict[str, Any] | None:
        """Re-check the process table for a child this agent owns.

        Started: confirmed while the child is alive. Stopped: confirmed only once it is
        actually gone. Either way the answer comes from the registry's view of the
        process, refreshed here, and not from what the handler returned a moment ago --
        which is the difference between "I ran the spawn call" and "there is a server".
        """
        process_id = recorded.get("process_id")
        if not isinstance(process_id, str):
            return None
        self.processes.reap()
        record = self.processes.get(process_id)
        if record is None:
            return None
        alive = record.state == RUNNING
        if started and not alive:
            # The child exited before it could be observed. Reporting nothing lets C6
            # refute the launch instead of accepting a spawn that produced no process.
            return None
        if not started and alive:
            return None
        parameters: dict[str, Any] = {"process_id": process_id}
        if started:
            parameters["label"] = record.label
        return {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "parameters": parameters,
            "state": {"state": record.state, "exit_code": record.exit_code},
        }

    def _record_terminal(
        self,
        action: Action,
        decision: CommandDecision,
        outcome: CommandOutcome,
    ) -> dict[str, Any]:
        data = outcome.public()
        # The program is named as the caller asked for it, never as a resolved system
        # path: an absolute path in a response tells a phone how the machine is laid
        # out, which is the disclosure the rest of this module works to avoid.
        data["command"] = decision.executable
        data["arguments"] = list(decision.arguments)
        data["working_directory"] = _relative_directory(
            decision.working_directory, self.workspace
        )
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "command": decision.executable,
            "exit_code": outcome.exit_code,
            "ran": not outcome.timed_out,
        }
        return data

    def _list_processes(self, action: Action, params: dict[str, Any]) -> dict[str, Any]:
        """The children *this agent* started, and what became of them.

        A full ``ps`` is not offered. A list of every process on the machine tells a
        phone which editor, vault, and ssh session is running, and none of that is
        needed to answer "is my server up". The narrow reading is deliberate, and the
        capability name says exactly what it returns.
        """
        self.processes.reap()
        params = action.parameters
        pattern = params.get("filter")
        if pattern is not None and not isinstance(pattern, str):
            raise ProviderError("filter must be a string", FailureKind.INVALID_INPUT)
        records = self.processes.records(
            include_finished=bool(action.parameters.get("include_finished")),
            pattern=pattern or "",
        )
        entries = [record.public() for record in records]
        result = {
            "processes": entries,
            "count": len(entries),
            "running": self.processes.running(),
            "capacity": self.processes.max_children,
        }
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "count": len(entries),
        }
        return result

    # -- processes and servers --------------------------------------------
    async def _start_child(self, action: Action, *, server: bool) -> dict[str, Any]:
        params = action.parameters
        decision = self._decide(params)
        name = str(params.get("name") or "").strip() if server else ""
        port = self._port(params) if server else None
        label = name or str(params.get("label") or "").strip()
        if name == CONTINUUM_NAME and (
            params.get("command") != "python3" or decision.arguments != CONTINUUM_ARGUMENTS
            or port != CONTINUUM_PORT or params.get("working_directory")
        ):
            raise ProviderError("The Continuum server name is reserved.", FailureKind.INVALID_INPUT)
        if server and port is not None and await is_listening(port):
            raise ProviderError(
                "the requested loopback port is already occupied; refusing to claim another process as this server",
                FailureKind.PROVIDER,
            )
        # Everything that could make this request impossible is settled *before* the
        # process exists. `reserve` covers the name and the child cap; a refusal
        # afterwards would leave an owned-by-nobody process holding a port.
        try:
            self.processes.reserve(kind="server" if server else "process", label=label)
        except MissingServerName as exc:
            raise ProviderError(str(exc), FailureKind.INVALID_INPUT) from exc
        except ProcessError as exc:
            raise ProviderError(str(exc), FailureKind.AUTHORIZATION) from exc
        log_path = self._new_log_path()
        try:
            process = await self.runner.start(decision, log_path=log_path)
        except ProcessError:
            if log_path:
                # The spawn never happened; do not leave an empty log claiming it did.
                with contextlib.suppress(OSError):
                    os.unlink(log_path)
            raise
        try:
            record = self._adopt(
                kind="server" if server else "process",
                decision=decision,
                process=process,
                label=label,
                action_id=action.action_id,
                port=port,
                log_path=log_path,
                working_directory=_relative_directory(
                    decision.working_directory, self.workspace
                ),
            )
        except MissingServerName as exc:
            raise ProviderError(str(exc), FailureKind.INVALID_INPUT) from exc
        data = record.public()
        if server and port is not None:
            data["listening"] = await self._await_port(
                record, port, deadline_s=self.server_ready_s
            )
            self.processes.reap()
        data["state"] = record.state
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "process_id": record.process_id,
            "label": record.label,
            "command": record.command,
            "kind": record.kind,
            "port": record.port,
        }
        return data

    async def _stop_process(self, action: Action) -> dict[str, Any]:
        process_id = action.parameters.get("process_id")
        if not isinstance(process_id, str) or not process_id.strip():
            raise ProviderError(
                "process_id is required", FailureKind.INVALID_INPUT
            )
        try:
            record = await self.processes.stop(process_id)
        except ProcessError as exc:
            raise ProviderError(str(exc), FailureKind.AUTHORIZATION) from exc
        self._observe_stop(action, record)
        return record.public()

    async def _stop_server(self, action: Action) -> dict[str, Any]:
        name = action.parameters.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProviderError("name is required", FailureKind.INVALID_INPUT)
        try:
            record = await self.processes.stop_server(name)
        except ProcessError as exc:
            raise ProviderError(str(exc), FailureKind.AUTHORIZATION) from exc
        self._observe_stop(action, record)
        return record.public()

    def _observe_stop(self, action: Action, record: ProcessRecord) -> None:
        """Record what to look for, so C6 can check the process is really gone.

        Keyed by this action's id, not by the process id: the launch action has its
        own effect under its own key, and overwriting it (which an earlier draft of
        this line did) would make the *stop* unobservable and the launch's evidence
        vanish at the same time.
        """
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "process_id": record.process_id,
            "label": record.label,
        }

    async def _server_status(self, action: Action) -> dict[str, Any]:
        self.processes.reap()
        params = action.parameters
        name = params.get("name")
        known = self.processes.servers(include_finished=True)
        records = (
            known
            if not isinstance(name, str) or not name.strip()
            else [record for record in known if record.label == name.strip()]
        )
        entries: list[dict[str, Any]] = []
        for record in records:
            entry = record.public()
            # Re-probed now, not remembered from launch: a server that died after the
            # start succeeded would otherwise still look like it was serving.
            entry["listening"] = (
                await is_listening(record.port) if record.port else None
            )
            health_path = params.get("health_path")
            if health_path is not None and params.get("service_id") is None:
                if not isinstance(health_path, str) or record.port is None:
                    raise ProviderError(
                        "HTTP health observation requires a registered named server and path",
                        FailureKind.INVALID_INPUT,
                    )
                expected_status = params.get("expected_status")
                if (expected_status is not None and (
                    isinstance(expected_status, bool) or not isinstance(expected_status, (int, float))
                    or int(expected_status) != expected_status or not 100 <= int(expected_status) <= 599
                )):
                    raise ProviderError("expected_status must be an HTTP status code", FailureKind.INVALID_INPUT)
                observation = await loopback_http_observation(
                    record.port, health_path,
                    expected_status=int(expected_status) if expected_status is not None else 200,
                )
                http_healthy = (
                    None if observation.state in {"TIMEOUT", "UNKNOWN"}
                    else observation.state == "HEALTHY"
                )
                entry.update({
                    "http_status": observation.status,
                    "http_healthy": http_healthy,
                    "http_state": observation.state,
                    "http_elapsed_ms": observation.elapsed_ms,
                    "http_error": observation.error,
                })
            if (name == CONTINUUM_NAME and record.port == CONTINUUM_PORT
                and record.arguments == CONTINUUM_ARGUMENTS and record.state == "running"):
                entry["ready"] = await continuum_http_ready()
            entries.append(entry)
        service_id = params.get("service_id")
        if service_id is not None:
            if not isinstance(service_id, str) or self.managed_service_lookup is None:
                raise ProviderError("a persistent managed service registration is required", FailureKind.AUTHORIZATION)
            service = self.managed_service_lookup(service_id)
            if service is None:
                raise ProviderError("the managed service registration is unavailable", FailureKind.AUTHORIZATION)
            if (name != service["name"] or params.get("health_path") != service["health_path"]
                or params.get("expected_status") != service["expected_status"]):
                raise ProviderError("health parameters must match the registered service", FailureKind.INVALID_INPUT)
            service_entry: dict[str, Any] | None = None
            for candidate in entries:
                if candidate.get("label") == service["name"]:
                    service_entry = candidate
                    break
            if service_entry is None:
                service_entry = {
                    "service_id": service["service_id"], "label": service["name"],
                    "kind": "server", "state": "unknown", "process_state": "unknown",
                    "port": service["port"], "process_id": "",
                    "listening": await is_listening(service["port"]),
                }
                entries.append(service_entry)
            process_state = str(service_entry.get("state", "unknown"))
            observation = await loopback_http_observation(
                int(service["port"]), str(service["health_path"]),
                expected_status=int(service["expected_status"]),
                timeout_s=int(service["timeout_ms"]) / 1000,
            )
            listener_not_owned = (
                process_state in {"stopped", "exited", "terminated"}
                and service_entry.get("listening") is True
            )
            http_healthy = (
                None if observation.state in {"TIMEOUT", "UNKNOWN"}
                else observation.state == "HEALTHY"
            )
            http_state = "UNKNOWN" if listener_not_owned else observation.state
            if listener_not_owned:
                http_healthy = None
            service_entry.update({
                "service_id": service["service_id"], "process_state": process_state,
                "http_status": observation.status,
                "http_healthy": http_healthy,
                "http_state": http_state,
                "http_elapsed_ms": observation.elapsed_ms,
                "http_error": "listener_not_owned" if listener_not_owned else observation.error,
            })
        result = {
            "servers": entries,
            "count": len(entries),
            "running": self.processes.running(),
        }
        if name == CONTINUUM_NAME:
            result["listening"] = await is_listening(CONTINUUM_PORT)
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "name": str(params.get("name") or ""),
        }
        return result

    async def _await_port(self, record: ProcessRecord, port: int, *, deadline_s: float = SERVER_READY_S) -> bool:
        """Wait for a declared port to answer, or for the child to stop being one.

        A server that is still importing is not a server that is not coming, so this
        polls rather than checking once. It also stops polling the moment the child is
        gone: a process that died at startup should be reported in a few tens of
        milliseconds, not after the full grace, and "still launching" versus "there is
        nothing launching" are different answers to give a phone.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_s
        while True:
            if await is_listening(port):
                return True
            self.processes.reap()
            if record.state != RUNNING:
                return False
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.05, remaining))

    def _adopt(self, **kwargs: Any) -> ProcessRecord:
        # The execution this child belongs to is not a parameter a caller can pass, and
        # not one every handler has to thread through: the engine installed it for the
        # duration of this provider call, which is the only window in which a child can
        # be attributed. An empty id means "started outside an execution", and is
        # reported as that rather than guessed at.
        kwargs.setdefault("execution_id", current_execution_id() or "")
        return self.processes.adopt(**kwargs)

    def _decide(self, params: dict[str, Any]) -> CommandDecision:
        policy = self.command_policy
        if policy is None:
            raise ProviderError(
                "no command policy is configured on this laptop, so no program may "
                "be run",
                FailureKind.PERMANENT,
            )
        working = params.get("working_directory")
        try:
            decision = policy.evaluate(
                params.get("command"),
                params.get("arguments") or (),
                working_directory=str(working or ""),
                env=params.get("env"),
            )
        except AgentError as exc:
            raise ProviderError(str(exc), FailureKind.AUTHORIZATION) from exc
        if decision.refused:
            raise ProviderError(
                "command policy refused this request: " + "; ".join(decision.reasons),
                FailureKind.AUTHORIZATION,
            )
        return decision

    @staticmethod
    def _port(params: dict[str, Any]) -> int | None:
        raw = params.get("port")
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ProviderError("port must be a number", FailureKind.INVALID_INPUT)
        port = int(raw)
        if port != raw or not 1024 <= port <= 65535:
            # Below 1024 a bind needs root, which this agent does not have; refusing
            # the request says so instead of failing obscurely inside the child.
            raise ProviderError(
                "port must be between 1024 and 65535", FailureKind.INVALID_INPUT
            )
        return port

    # -- editors, projects, applications ----------------------------------
    def _editors(self) -> EditorDiscovery:
        """Discovery wired to the same confinement the phone-driven actions use.

        ``policy_for`` builds a policy whose allowlist is the one absolute binary
        being probed. Probing an editor's version is running it, and running things
        belongs to the runner and the policy -- not to a convenient ``Popen`` that
        quietly becomes the second execution path this package promises does not
        exist.
        """
        policy = self.command_policy

        def policy_for(binary: str) -> CommandPolicy:
            assert policy is not None
            return replace(policy, allowed=(binary,), allow_interpreter_code_flags=False)

        def permitted(spec: object) -> bool:
            # Any of the adapter's binary names being on the allowlist is enough:
            # what is probed afterwards is the resolved file behind that name.
            if policy is None:
                return False
            allowed = {os.path.basename(entry) for entry in policy.allowed}
            return bool(allowed & set(getattr(spec, "binaries", ())))

        return EditorDiscovery(
            runner=self.runner,
            policy_for=policy_for if policy is not None else None,
            permitted=permitted,
        )

    async def _ide_list(self) -> dict[str, Any]:
        discovery = self._editors()
        installed = discovery.installed()
        entries = []
        for spec, binary in installed:
            entries.append(discovery.describe(spec, binary, await discovery.version(spec, binary)))
        result = {
            "editors": entries,
            "count": len(entries),
            "known": [spec.ide for spec in KNOWN_IDES],
        }
        return result

    async def _ide_open(self, action: Action, *, project: bool = False) -> dict[str, Any]:
        params = action.parameters
        discovery = self._editors()
        requested_ide = str(params.get("ide") or "")
        if not requested_ide.strip():
            chosen = discovery.installed()
            if not chosen:
                raise ProviderError(
                    "no known editor is installed on this laptop",
                    FailureKind.PERMANENT,
                )
            requested_ide = chosen[0][0].ide
        target = self._resolve_target(
            params.get("project") if project else params.get("path"),
            project=project,
        )
        try:
            spec, binary, argv = discovery.open_argv(requested_ide, target.absolute)
        except EditorNotFound as exc:
            raise ProviderError(str(exc), FailureKind.INVALID_INPUT) from exc
        return await self._launch(action, binary, argv, label=f"{spec.ide}:open")

    async def _ide_command(self, action: Action) -> dict[str, Any]:
        params = action.parameters
        discovery = self._editors()
        arguments = params.get("arguments") or ()
        if not isinstance(arguments, (list, tuple)):
            raise ProviderError(
                "arguments must be a list of strings", FailureKind.INVALID_INPUT
            )
        try:
            spec, binary, argv = discovery.command_argv(
                str(params.get("ide") or ""),
                str(params.get("command") or ""),
                tuple(str(item) for item in arguments),
            )
        except EditorNotFound as exc:
            raise ProviderError(str(exc), FailureKind.INVALID_INPUT) from exc
        return await self._launch(action, binary, list(argv), label=f"{spec.ide}:{params.get('command')}")

    def _resolve_target(self, raw: object, *, project: bool) -> ResolvedPath:
        """A path inside the workspace, or a refusal. Never an attacker's absolute path."""
        workspace = self.workspace
        if workspace is None:
            raise ProviderError(
                "no workspace root is configured on this laptop", FailureKind.PERMANENT
            )
        if not isinstance(raw, str) or not raw.strip():
            raise ProviderError(
                "a path is required", FailureKind.INVALID_INPUT
            )
        if project and raw.strip() == ".":
            return ResolvedPath(workspace.root, "")
        try:
            resolved = workspace.resolve(raw, must_exist=True)
        except AgentError as exc:
            raise ProviderError(str(exc), _classify(exc)) from exc
        if project and not os.path.isdir(resolved.absolute):
            raise ProviderError(
                f"{resolved.relative!r} is not a directory", FailureKind.INVALID_INPUT
            )
        return resolved

    async def _launch(
        self, action: Action, binary: str, argv: list[str], *, label: str
    ) -> dict[str, Any]:
        """Run one already-resolved program through the policy and the runner.

        The binary is passed to the policy as an absolute path, so it is confined by
        the same allowlist as everything else that executes here. A phone cannot use
        ``device.ide.open`` to run a program the laptop has not permitted, only the
        editor it did.
        """
        policy = self.command_policy
        if policy is None:
            raise ProviderError(
                "no command policy is configured on this laptop", FailureKind.PERMANENT
            )
        decision = policy.evaluate(binary, argv[1:])
        if decision.refused:
            raise ProviderError(
                "command policy refused this request: " + "; ".join(decision.reasons),
                FailureKind.AUTHORIZATION,
            )
        outcome = await self.runner.run(decision)
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "command": label,
            "exit_code": outcome.exit_code,
            "ran": not outcome.timed_out,
        }
        return {
            "command": label,
            "exit_code": outcome.exit_code,
            "stdout": outcome.stdout,
            "stderr": outcome.stderr,
            "timed_out": outcome.timed_out,
            "failed": outcome.failed,
        }

    def _project_list(self, action: Action, params: dict[str, Any]) -> dict[str, Any]:
        workspace = self._require_workspace()
        projects = scan_projects(workspace.root)
        for project in projects:
            project["services"] = self._services_under(project["path"])
        result = {"projects": projects, "count": len(projects)}
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "count": len(projects),
        }
        return result

    async def _project_status(self, action: Action) -> dict[str, Any]:
        params = action.parameters
        target = self._resolve_target(params.get("project"), project=True)
        from actionmesh.agent.projects import kinds_of, scripts_of

        status: dict[str, Any] = {
            "name": os.path.basename(target.absolute),
            "path": target.relative,
            "kinds": list(kinds_of(target.absolute)),
            "scripts": scripts_of(target.absolute),
            "services": self._services_under(target.relative),
        }
        # Branch and change counts come from git *through the policy*, and are
        # reported as counts rather than as the raw output: `git log` subjects are
        # the sort of thing that names a client or a codename.
        git = await self._run_git(target.absolute, ["rev-parse", "--abbrev-ref", "HEAD"])
        if git is not None:
            status["branch"] = git.strip()[:80] or None
        porcelain = await self._run_git(target.absolute, ["status", "--porcelain"])
        if porcelain is not None:
            lines = [line for line in porcelain.splitlines() if line.strip()]
            status["changed"] = len(lines)
            status["modified"] = sum(1 for line in lines if line.startswith(" M"))
            status["untracked"] = sum(1 for line in lines if line.startswith("??"))
            status["staged"] = sum(1 for line in lines if line[:1] in {"M", "A", "R", "D"})
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            # Keep the exact typed selector used by this request.  The workspace
            # root resolves internally to an empty relative path, while callers
            # address it as "."; recording the resolved form made independent
            # verification reject a successful root-project inspection.
            "project": params.get("project"),
            "observed": True,
        }
        return status

    async def _run_git(self, cwd: str, args: list[str]) -> str | None:
        """A read-only git invocation in one directory, or None if git is not permitted."""
        policy = self.command_policy
        if policy is None or "git" not in [os.path.basename(entry) for entry in policy.allowed]:
            return None
        try:
            decision = policy.evaluate("git", args, working_directory=cwd)
        except AgentError:
            return None
        if decision.refused:
            return None
        try:
            outcome = await self.runner.run(decision, timeout_s=5.0)
        except (ProcessError, OSError):
            return None
        return outcome.stdout if outcome.exit_code == 0 else None

    def _services_under(self, relative: str) -> list[dict[str, Any]]:
        self.processes.reap()
        prefix = relative.strip("/")
        return [
            record.public()
            for record in self.processes.servers()
            if prefix
            and (
                record.working_directory == prefix
                or record.working_directory.startswith(prefix + "/")
                or (record.working_directory == "."
                and prefix == "")
            )
        ]

    async def _connector_read(self, action: Action) -> dict[str, Any]:
        first = await connectors.read(action)
        observed = await connectors.read(action)
        if first != observed:
            raise ProviderError(
                "The result changed between reads. Narrow the search and retry.",
                FailureKind.PROVIDER,
            )
        observed.pop("_membership")
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "parameters": dict(action.parameters),
            "state": {"count": observed["count"], "truncated": observed["truncated"]},
        }
        return observed

    async def _launch_native_app(self, action: Action) -> dict[str, Any]:
        app = action.parameters.get("app")
        await asyncio.to_thread(apps.launch, app)
        return {"app": app}

    async def _native_app_status(self, action: Action) -> dict[str, Any]:
        app = action.parameters.get("app")
        running = await asyncio.to_thread(apps.running, app)
        if running is None:
            raise ProviderError("macOS app status is unavailable.", FailureKind.PERMANENT)
        return {"app": app, "running": running}

    async def _app_open(self, action: Action) -> dict[str, Any]:
        params = action.parameters
        app = params.get("app")
        if not isinstance(app, str) or not app.strip():
            raise ProviderError("app is required", FailureKind.INVALID_INPUT)
        arguments = params.get("arguments") or ()
        if not isinstance(arguments, (list, tuple)):
            raise ProviderError(
                "arguments must be a list of strings", FailureKind.INVALID_INPUT
            )
        policy = self.command_policy
        if policy is None:
            raise ProviderError(
                "no command policy is configured on this laptop", FailureKind.PERMANENT
            )
        decision = policy.evaluate(app, [str(item) for item in arguments])
        if decision.refused:
            raise ProviderError(
                "command policy refused this request: " + "; ".join(decision.reasons),
                FailureKind.AUTHORIZATION,
            )
        self.processes.reserve(kind="process", label=app.strip())
        process = await self.runner.start(decision)
        record = self._adopt(
            kind="process",
            decision=decision,
            process=process,
            label=app.strip(),
            action_id=action.action_id,
            working_directory=_relative_directory(decision.working_directory, self.workspace),
        )
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "process_id": record.process_id,
            "command": app.strip(),
            "ran": True,
        }
        return record.public()

    async def _app_close(self, action: Action) -> dict[str, Any]:
        """Close an application *this agent launched*, by name.

        There is no path from here to a pid the agent did not create, for the same
        reason `device.process.stop` has one: pids are guessable, and "close the app
        called X" resolves to a kill of whichever process happens to answer to X
        today.
        """
        params = action.parameters
        app = params.get("app")
        if not isinstance(app, str) or not app.strip():
            raise ProviderError("app is required", FailureKind.INVALID_INPUT)
        wanted = app.strip().lower()
        self.processes.reap()
        matches = [
            record
            for record in self.processes.records()
            if record.state == RUNNING
            and (record.label.lower() == wanted or record.command.lower() == wanted)
        ]
        if not matches:
            raise ProviderError(
                f"no application named {app!r} was started by this laptop agent; "
                "only applications it launched can be closed",
                FailureKind.AUTHORIZATION,
            )
        records = []
        for record in matches:
            stopped = await self.processes.stop(record.process_id)
            records.append(stopped.public())
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "process_id": matches[0].process_id,
        }
        return {"closed": records, "count": len(records)}

    # -- screen + GUI (C38) -----------------------------------------------
    async def _screen_capture(self, action: Action) -> dict[str, Any]:
        """One screenshot, through the OS portal, never a retained image.

        The blocking D-Bus round-trip runs in a worker thread so the server's event loop
        is not held while the portal waits for a person to answer. The result is what the
        portal said: ``captured`` with a transient file reference and its dimensions, or
        ``cancelled`` / ``unavailable`` with the reason. There is no simulated image and
        no "close enough" fallback — a screen that was not captured says so.
        """
        from actionmesh.agent import device_screen

        data = await asyncio.to_thread(device_screen.capture_screenshot)
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "status": data.get("status"),
            # Only the shape is remembered, never the pixels and never the URI's
            # lifetime-bounded path as anything more than a witness that a capture
            # happened.
            "width": (data.get("file") or {}).get("width"),
            "height": (data.get("file") or {}).get("height"),
        }
        return data

    async def _screen_inspect(self, action: Action) -> dict[str, Any]:
        """Describe the accessibility tree, honestly and bounded."""
        from actionmesh.agent import device_screen

        params = action.parameters
        window = params.get("window")
        if window is not None and not isinstance(window, str):
            raise ProviderError("window must be a string", FailureKind.INVALID_INPUT)
        raw_max = params.get("max_elements")
        if raw_max is not None:
            if not isinstance(raw_max, int) or isinstance(raw_max, bool) or raw_max < 1:
                raise ProviderError(
                    "max_elements must be a positive integer", FailureKind.INVALID_INPUT
                )
            max_elements = min(raw_max, device_screen._MAX_ELEMENTS)
        else:
            max_elements = device_screen._MAX_ELEMENTS
        data = await asyncio.to_thread(
            device_screen.inspect_gui, window or "", max_elements
        )
        self._effects[action.action_id] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "status": data.get("status"),
            "window": (window or "").strip(),
            "count": data.get("element_count"),
        }
        return data

    # -- GUI actions (C38) -------------------------------------------------
    # One shape for all three: validate the target parameters, call the adapter in a
    # worker thread, and record only what the adapter *witnessed*. The adapter's
    # return value distinguishes "the toolkit accepted the call" from "the effect is
    # visible", and both halves are kept -- verification reads the recorded witness,
    # while the caller reads `status`.
    #
    # None of these re-read the screen a second time from here. The adapter already
    # read the state before and after the operation (a click's before/after states, a
    # type's character counts, a scroll's offsets and anchor geometry), and that
    # read-back is the independent observation. Doing it again against a tree that a
    # real application may have changed in the meantime would produce a second, later
    # answer and then present it as if it were the first.

    def _gui_target(self, params: dict[str, Any]) -> tuple[str, str, str | None]:
        role = params.get("role")
        name = params.get("name")
        window = params.get("window")
        if not isinstance(role, str) or not role:
            raise ProviderError("role must be a non-empty string", FailureKind.INVALID_INPUT)
        if not isinstance(name, str):
            raise ProviderError("name must be a string", FailureKind.INVALID_INPUT)
        if window is not None and not isinstance(window, str):
            raise ProviderError("window must be a string", FailureKind.INVALID_INPUT)
        return role, name, (window or None)

    def _record_gui(
        self,
        action: Action,
        data: dict[str, Any],
        *,
        role: str,
        name: str,
        window: str | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Remember the target and the witnessed outcome, never the payload.

        The text a ``device.gui.type`` inserts is deliberately absent: it may be a
        message body, a query, or a credential, and the effect record is read by the
        verifier, journalled for restart recovery, and surfaced in the dashboard. What
        survives is the length, which is enough to check the write landed.
        """
        record: dict[str, Any] = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "status": data.get("status"),
            "role": role,
            "name": name,
        }
        if window:
            record["window"] = window
        if extra:
            record.update(extra)
        self._effects[action.action_id] = record

    async def _gui_click(self, action: Action) -> dict[str, Any]:
        from actionmesh.agent import device_screen

        role, name, window = self._gui_target(action.parameters)
        data = await asyncio.to_thread(
            device_screen.click_element, role, name, window
        )
        observed = data.get("observed") or {}
        self._record_gui(
            action, data, role=role, name=name, window=window,
            extra={
                "dispatched": bool(data.get("dispatched")),
                "state_changed": observed.get("state_changed"),
            },
        )
        return data

    async def _gui_type(self, action: Action) -> dict[str, Any]:
        from actionmesh.agent import device_screen

        role, name, window = self._gui_target(action.parameters)
        text = action.parameters.get("text")
        if not isinstance(text, str) or not text:
            raise ProviderError("text must be a non-empty string", FailureKind.INVALID_INPUT)
        data = await asyncio.to_thread(
            device_screen.type_text, role, name, text, window
        )
        lengths = data.get("length") or {}
        self._record_gui(
            action, data, role=role, name=name, window=window,
            # Lengths only. `inserted` is the adapter's own arithmetic over the
            # character count it read back, not a count of characters it was handed.
            extra={
                "typed": bool(data.get("typed")),
                "inserted": lengths.get("inserted"),
            },
        )
        return data

    async def _gui_scroll(self, action: Action) -> dict[str, Any]:
        from actionmesh.agent import device_screen

        role, name, window = self._gui_target(action.parameters)
        direction = action.parameters.get("direction")
        if not isinstance(direction, str) or not direction:
            raise ProviderError(
                "direction must be one of top/bottom/left/right", FailureKind.INVALID_INPUT
            )
        data = await asyncio.to_thread(
            device_screen.scroll_element, role, name, direction, window
        )
        self._record_gui(
            action, data, role=role, name=name, window=window,
            extra={
                "direction": direction,
                "moved": bool(data.get("moved")),
                "offset_after": (data.get("offset") or {}).get("after"),
            },
        )
        return data

    async def _submit(self, action: Action) -> dict[str, Any]:
        """Refuse a payment submit honestly: there is no backend to submit through.

        This handler exists so that the two CRITICAL capabilities have a real one
        (``SUPPORTED == IMPLEMENTED`` is a structural invariant of this build), and it
        does the only faithful thing a build without a payment integration can do —
        it raises ``PERMANENT`` naming what is missing.

        Three temptations are refused here on purpose:

        * **Fabricating a reference.** Returning ``{"status": "ok", "reference":
          "BK-1234"}`` would let the verifier and the audit trail record a booking
          that does not exist. There is no booking to read back, so there is no
          success to report.
        * **Recording an effect.** ``self._effects`` is what ``observe()`` re-reads;
          an entry for a submit that never left this process would be a witness to
          nothing, and ``_journal_effect`` would copy it into the durable ledger.
        * **Retrying.** ``max_attempts`` is 1 at the definition, and correctly so: a
          submit that half-ran may have taken the money already, and a second attempt
          is a second booking.

        The parameter check above the refusal is not a security boundary — the
        registry's ``check_params`` already refused anything undeclared — it is the
        same defensive shape every other handler here uses when called directly.
        """
        required = {
            "booking.submit": ("journey", "passenger", "fare"),
            "order.place": ("items", "total"),
        }[action.type]
        for name in required:
            value = action.parameters.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ProviderError(
                    f"{name} must be a non-empty string", FailureKind.INVALID_INPUT
                )
        raise ProviderError(
            "this build has no payment backend, so it cannot submit a booking or place "
            "an order; nothing was submitted and no confirmation was recorded",
            FailureKind.PERMANENT,
        )

    # -- verification -----------------------------------------------------
    # -- dispatch ledger --------------------------------------------------
    def _save_row(self, action_id: str, **changes: Any) -> None:
        """Write one action's dispatched work to the durable ledger.

        Merged rather than replaced: the registry, the runner and the effect recorder
        each know part of a child's story, and the last one to speak must not erase what
        the first one saw.
        """
        if self.ledger is None or not action_id:
            return
        row = self.ledger.get_dispatch(action_id) or {}
        moment = datetime.now(UTC).isoformat()
        merged = {
            "action_id": action_id,
            "execution_id": row.get("execution_id") or current_execution_id() or "",
            "process_id": row.get("process_id", ""),
            "kind": row.get("kind", ""),
            "pid": row.get("pid", 0),
            "pgid": row.get("pgid", 0),
            "identity": row.get("identity", ""),
            "state": row.get("state", RUNNING),
            "exit_code": row.get("exit_code"),
            "observation": row.get("observation", ""),
            "started_at": row.get("started_at") or moment,
            "ended_at": row.get("ended_at", ""),
        }
        merged.update({key: value for key, value in changes.items() if value is not None})
        self.ledger.save_dispatch(merged)

    def _journal_child(self, record: ProcessRecord) -> None:
        """The registry's callback: this child exists, and here is what it looks like."""
        if self.ledger is None:
            return
        self._save_row(
            record.action_id,
            execution_id=record.execution_id,
            process_id=record.process_id,
            kind=record.kind,
            pid=record.pid or 0,
            pgid=record.pgid or record.pid or 0,
            identity=record.identity or "",
            state=record.state,
            exit_code=record.exit_code,
            ended_at=record.ended_at.isoformat() if record.ended_at else "",
        )

    def _journal_effect(self, action: Action) -> None:
        """Copy the verifier's witness where a restart cannot wipe it.

        What is stored is ``observe()``'s answer, not the raw effect record: the two
        shapes differ, and a verifier arriving after a restart must read the same
        description it would have read before one. Otherwise the same successful action
        would be evidenced two different ways depending on who was watching.
        """
        if action.type == "device.files.preview_plan":
            # Preview operations may contain proposed replacement text. The preview
            # itself is verified live, but those parameters must never be serialized
            # into the process/effect ledger.
            return
        if self._effects.get(action.action_id) is None:
            return
        seen = self.observe(action)
        if seen is None:
            return
        self._save_row(action.action_id, observation=json.dumps(seen, default=str, sort_keys=True))

    @staticmethod
    def _evidence_row(row: dict[str, Any]) -> ProcessRecord:
        return ProcessRecord(
            process_id=row.get("process_id") or "",
            kind=row.get("kind") or "process",
            label="",
            command="",
            pid=row.get("pid") or None,
            identity=row.get("identity") or None,
            pgid=row.get("pgid") or None,
            state=row.get("state") or RUNNING,
            exit_code=row.get("exit_code"),
            execution_id=row.get("execution_id") or "",
            action_id=row.get("action_id") or "",
        )

    @staticmethod
    def _observed_action_state(
        row: dict[str, Any], *, running: bool, uncertain: bool = False
    ) -> str:
        """What is true of this action now, read from the kernel rather than the row.

        The ledger's ``state`` column is written when something *notices* a child
        ending. When the process that would have noticed is killed -- which is exactly
        the crash this evidence exists for -- the row keeps saying ``running`` about a
        pid that no longer exists. Repeating that text to the control plane would make
        a finished run permanently un-settleable, so a dead pid is reported as
        ``exited`` with an exit code the machine never supplied: the effect happened,
        and its outcome is unknown.
        """
        if running:
            return RUNNING
        if uncertain:
            return "unknown"
        recorded = row.get("state") or RUNNING
        if recorded != RUNNING:
            return str(recorded)
        return EXITED if row.get("pid") else STOPPED

    def execution_evidence(self, execution_id: str) -> dict[str, Any] | None:
        """What became of the work this execution ordered, as the machine says now.

        Keyed only by an execution id the server minted. There is no pid argument here,
        and no path from a caller's number to a signal: the children this can name are
        the ones this agent recorded starting.

        ``None`` -- rather than ``{"state": "gone"}`` -- when this execution never
        dispatched anything through this agent. Silence in the ledger is not evidence
        that a process ended; it is evidence that nobody saw one, and the two answers
        must not be collapsed into each other.
        """
        if self.ledger is None:
            return None
        rows = self.ledger.list_dispatch(execution_id)
        if not rows:
            return None
        self.processes.reap()
        live: list[int] = []
        handles: list[str] = []
        actions: list[dict[str, Any]] = []
        last_exit: int | None = None
        for row in rows:
            record = self._evidence_row(row)
            observation = self.processes.observe(record)
            running = observation == ProcessObservation.RUNNING
            if running:
                live.append(record.pid or 0)
                handles.append(record.process_id or row["action_id"])
            if record.exit_code is not None:
                last_exit = record.exit_code
            observed = self._observed_action_state(
                row, running=running, uncertain=observation == ProcessObservation.UNKNOWN
            )
            actions.append(
                {
                    "action_id": row["action_id"],
                    "process_id": record.process_id,
                    # What the machine says now, not what the ledger last wrote. A row
                    # still says `running` when the process that started it died before
                    # reaping, and forwarding that text would tell the control plane a
                    # dead child is alive -- so it can never settle the run.
                    "state": observed,
                    # An exit code only exists if something reaped the child. A dead
                    # pid found by reading /proc has *ended*, but nothing saw it end:
                    # copying the last code this agent remembers from some other run
                    # would attribute a verdict to a process that never reported one,
                    # and verification would then confirm or refute the wrong claim.
                    "exit_code": None if observed == EXITED else record.exit_code,
                }
            )
        if live:
            state = "running"
        elif any(
            row["exit_code"] is not None
            or self._observed_action_state(
                row,
                running=False,
                uncertain=self.processes.observe(self._evidence_row(row))
                == ProcessObservation.UNKNOWN,
            ) not in (RUNNING, "unknown")
            for row in rows
        ):
            # Nothing is alive and something finished: that is an answer, and the exit
            # code in it is the kernel's, not this process's memory of one.
            state = "exited"
        else:
            state = (
                "unknown"
                if any(action["state"] == "unknown" for action in actions)
                else "gone"
            )
        return {
            "state": state,
            "exit_code": last_exit,
            "pids": live,
            "process_ids": handles,
            "actions": actions,
            "detail": f"{len(rows)} dispatched action(s), {len(live)} alive",
        }

    def stop_execution(self, execution_id: str, *, grace_s: float = 2.0) -> dict[str, Any]:
        """Stop the children one execution owns, and say which ones are confirmed gone.

        The C35 shape of cancellation: the request asking for the stop is not the one
        that started the work, and may not even be the same process. What it needs is
        the ledger's list, not this loop's task map.

        ``unconfirmed`` is not a retry list. It says the signal was sent and this agent
        could not see the group die, which the caller reports as an unconfirmed stop.
        """
        stopped: list[str] = []
        already_gone: list[str] = []
        unconfirmed: list[str] = []
        if self.ledger is None:
            return {"stopped": stopped, "already_gone": already_gone, "unconfirmed": unconfirmed}
        for row in self.ledger.list_dispatch(execution_id):
            record = self._evidence_row(row)
            handle = record.process_id or row["action_id"]
            identity_state = self.processes.observe(record)
            if record.state != RUNNING or identity_state == ProcessObservation.GONE:
                already_gone.append(handle)
                continue
            if identity_state != ProcessObservation.RUNNING:
                unconfirmed.append(handle)
                continue
            if stop_group(record, grace_s=grace_s):
                stopped.append(handle)
                self._save_row(row["action_id"], state="stopped", ended_at=datetime.now(UTC).isoformat())
            else:
                unconfirmed.append(handle)
        return {"stopped": stopped, "already_gone": already_gone, "unconfirmed": unconfirmed}

    def _observe_gui(
        self, action: Action, recorded: dict[str, Any]
    ) -> dict[str, Any] | None:
        """What the machine's own read-back showed, for one GUI action.

        ``recorded`` holds only what the adapter witnessed. The parameter echo below
        carries the *restricted* parameter set -- role, label and, when one was given,
        the window -- and deliberately never the inserted text. ``_assess`` compares
        every key the request supplied that also appears here, so a mismatch in the
        echo is refuted; omitting a key skips the comparison rather than failing it.
        """
        params: dict[str, Any] = {
            "role": recorded.get("role"),
            "name": recorded.get("name"),
        }
        window = action.parameters.get("window")
        if window:
            params["window"] = window
        direction = action.parameters.get("direction")
        if direction:
            params["direction"] = direction

        status = recorded.get("status")
        base = {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "parameters": params,
        }

        if action.type == "device.gui.click":
            # Only a toolkit call that was actually made is evidence. `refused` means
            # `do_action` returned False; the rest are refusals in which no call was
            # made at all.
            if not recorded.get("dispatched"):
                return None
            return {
                **base,
                "state": {"dispatched": True,
                          "state_changed": recorded.get("state_changed")},
            }
        if action.type == "device.gui.type":
            if not recorded.get("typed"):
                return None
            # `inserted` is the difference between the character count read before the
            # write and the one read after it, so it is the field's own answer to "did
            # this land" -- not a count of what was handed over.
            return {**base, "state": {"typed": True,
                                      "inserted": recorded.get("inserted")}}
        if action.type == "device.gui.scroll":
            if status == "scrolled":
                return {
                    **base,
                    "state": {"moved": bool(recorded.get("moved")),
                              "offset": recorded.get("offset_after")},
                }
            if status == "offset_changed":
                # The bar moved and the anchor's reported bounds did not. Saying so is
                # the honest report; the accessibility tree cannot witness the content
                # moving, and this does not claim it did.
                return {
                    **base,
                    "state": {"moved": False, "offset": recorded.get("offset_after"),
                              "bounds_unchanged": True},
                }
            if status == "already_at_edge":
                # Nothing was dispatched because the region was already where the
                # request asked it to be. That is the requested state, observed --
                # confirmation here is a statement about the edge, never about motion.
                return {
                    **base,
                    "state": {"moved": False,
                              "offset": recorded.get("offset_after"),
                              "already_at_edge": True},
                }
            return None
        return None

    def observe(self, action: Action) -> dict[str, Any] | None:
        """Re-read the effect from the filesystem. Nothing here is remembered.

        C6 weights this observation above any claim this provider makes, so it has to
        be an answer to "what is true now", not a recording of what the code intended.
        """
        if action.type in {apps.MAC_APP_ACTION, apps.MAC_APP_STATUS}:
            app = action.parameters.get("app")
            running = apps.running(app)
            if running is None or (action.type == apps.MAC_APP_ACTION and not running):
                return None
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"app": app},
                "state": {"running": running},
            }
        if action.type == "device.files.preview_plan":
            effect = self._effects.get(action.action_id)
            if self.workspace is None or effect is None:
                return None
            record = parallelworld.get_preview(str(effect.get("preview_id") or ""))
            if record is None or not parallelworld.check_fresh(self.workspace, record):
                return None
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"operations": action.parameters.get("operations")},
                "state": {"preview_id": record.preview_id},
            }
        # -- C42 readonly file probes (live re-read, no effect, no ledger) ----
        # list/search/info/open are reads: the handlers deliberately record no
        # effect, and test_browser_files_media_provider pins that ("Reads do not
        # mutate, so they journal nothing."). The gate above therefore hides them
        # from the recorded-effect branches below. This branch sits ahead of the
        # gate so the verifier can still confirm them from a live, confined
        # re-read — the same live answer observe() gives for the legacy
        # device.file.* reads below — while writing nothing to _effects and
        # nothing to the ledger. A refusal (confinement, missing path where the
        # request needs one) is no evidence, so every failure path returns None
        # and the verifier reads the step as unconfirmed.
        if action.type == "device.files.list":
            if self.files is None:
                return None
            try:
                fresh = self.files.list_(action.parameters.get("path", ""), action.parameters.get("limit"))
            except (AgentError, OSError):
                return None
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"path": fresh.get("path")},
                "state": {"count": fresh.get("count")},
            }
        if action.type == "device.files.search":
            if self.files is None:
                return None
            try:
                fresh = self.files.search(
                    action.parameters.get("path", ""),
                    action.parameters.get("pattern", ""),
                    action.parameters.get("limit"),
                )
            except (AgentError, OSError):
                return None
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"path": fresh.get("path"), "pattern": action.parameters.get("pattern", "")},
                "state": {"count": fresh.get("count")},
            }
        if action.type == "device.files.compare":
            if self.files is None:
                return None
            try:
                changed = self.files.compare(action.parameters["left"], action.parameters["right"])
            except (AgentError, ProviderError, OSError):
                return None
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": dict(action.parameters),
                "result_fields": {"changed": changed},
            }
        if action.type == "device.files.info":
            if self.files is None:
                return None
            try:
                fresh = self.files.info(action.parameters.get("path", ""))
            except (AgentError, OSError):
                return None
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"path": fresh.get("path") or action.parameters.get("path", "")},
                "state": {
                    "exists": fresh.get("exists"),
                    "is_dir": fresh.get("is_dir"),
                    "size": fresh.get("size"),
                    "kind": fresh.get("kind"),
                },
            }
        if action.type == "device.files.hash":
            if self.files is None:
                return None
            try:
                fresh = self.files.hash(action.parameters.get("path", ""))
            except (AgentError, ProviderError, OSError):
                return None
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"path": fresh.get("path")},
                "state": {"sha256": fresh.get("sha256"), "size": fresh.get("size")},
            }
        if action.type == "device.files.open":
            if self.files is None:
                return None
            try:
                fresh = self.files.open_(action.parameters.get("path", ""))
            except (AgentError, OSError):
                return None
            if fresh.get("status") != "ok":
                # No opener, failed launch, non-zero exit: nothing was opened,
                # so there is no witness to record. Re-running the opener here
                # is also never an observation: it performs the act a second time.
                return None
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"path": fresh.get("path") or action.parameters.get("path", "")},
                "state": {"opened": fresh.get("opened")},
            }
        recorded = self._effects.get(action.action_id)
        if not isinstance(recorded, dict):
            # C35: this process may not be the one that did the work. The ledger holds
            # the same effect description the executor wrote when it ran, so a verifier
            # arriving after a restart reads what was witnessed instead of being told
            # there is nothing to see.
            stored = (
                self.ledger.get_dispatch(action.action_id)
                if self.ledger is not None
                else None
            )
            text = stored.get("observation") if stored else ""
            if not text:
                return None
            try:
                recovered = json.loads(text)
            except ValueError:
                return None
            if not isinstance(recovered, dict):
                return None
            # Returned as it was written. The ledger holds `observe()`'s own answer, not
            # a raw effect record, so running it through the derivation below a second
            # time would build a description of a description -- and report an exit code
            # of None for a command that exited 0.
            return recovered
        if action.type in connectors.CONNECTOR_ACTIONS:
            return recorded
        # -- screen capabilities, deliberately above the workspace guard ---------
        # `observe()` below starts by refusing to answer without a workspace root,
        # because most branch below rebuilds a path. Screen work has no path: it reads
        # the accessibility tree and the OS portal, neither of which lives under the
        # configured root. Answering these before the guard is what lets an agent with
        # a desktop and no workspace (a perfectly ordinary configuration) still have
        # its screen actions verified.
        action_type = recorded.get("action_type") or ""
        if not isinstance(action_type, str):
            return None
        if action_type == "device.screen.capture":
            # A capture is evidenced by the portal having returned a frame, and by that
            # frame's own dimensions -- never by the image, which is not retained. A
            # capture the portal declined is *not* confirmable: returning a shape for it
            # would let verification confirm a screen that was never read, which is the
            # one thing this capability must never do.
            if recorded.get("status") != "captured":
                return None
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {},
                "state": {
                    "width": recorded.get("width"),
                    "height": recorded.get("height"),
                },
            }
        if action_type == "device.screen.inspect":
            # The observation is that a tree was walked and how many elements it held.
            # An inspection that could not read the tree (no bus, no gi) was not an
            # inspection, and observing nothing says exactly that.
            if recorded.get("status") != "ok":
                return None
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"window": recorded.get("window", "")},
                "state": {"elements": recorded.get("count")},
            }
        # -- GUI actions, same reason for sitting above the workspace guard ---------
        # These read and drive the accessibility tree, which has no path under the
        # configured root. The observation is the adapter's own read-back (the click's
        # before/after states, the field's character counts, the scroll bar's offsets),
        # narrowed to the keys the caller actually supplied.
        #
        # A refusal is *not* an observation. Every branch that reports "nothing was
        # invoked" -- the target was missing, ambiguous, disabled, uneditable, or
        # changed under us -- returns None here rather than returning its own name and
        # letting the verifier count that as a match. `observe()` answering "yes" to a
        # request that was refused is the exact shape of a false confirmation.
        if action_type in {"device.gui.click", "device.gui.type", "device.gui.scroll"}:
            return self._observe_gui(action, recorded)
        # -- consequential submits, same reason for sitting above the guard ----------
        # A submit drives a website; it has no path under the configured root. And a
        # submit this build refused is *not* an observation: the handler raises before
        # recording anything, so there is normally no record here at all. The branch is
        # written anyway so that a future handler which records a refusal cannot have
        # that refusal counted as a witness — "nothing was submitted" must read as
        # unconfirmed, never as confirmed.
        if action_type in {"booking.submit", "order.place"}:
            if recorded.get("status") != "submitted":
                return None
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {
                    key: value
                    for key, value in action.parameters.items()
                    if key in ("journey", "passenger", "fare", "items", "total")
                },
                "state": {"reference": recorded.get("reference")},
            }
        # -- C42: the controlled browser and local playback, above the guard ------
        # Browser and media have no path under the configured root: the browser
        # lives in its own window and the player on its own session bus. Both
        # engines already read back after every operation (the browser refreshes
        # its page facts, the media engine re-reads the player), so observe()
        # answers from the recorded read-back without issuing a second network
        # request or a second MPRIS query. That is the design's whole point: a
        # second read would be both more work and a second source of truth.
        #
        # Every refusal — ``stale``, ``needs_user``, ``unavailable``, ``error``,
        # ``ambiguous``, ``no_effect``, ``unsupported``, ``invalid_input`` — is
        # no evidence at all, and it returns None here, because the wrappers
        # record *nothing* on a refusal. A browser refusal that somehow did
        # record an effect cannot have it counted as a witness either: only
        # ``status == "ok"`` observed through the recorded effect counts.
        if action_type.startswith("browser."):
            if recorded.get("status") != "ok":
                return None
            # The engine already redacted page content before returning it;
            # this narrows it further to the bounded evidence keys the effect
            # recorder itself captured. The ledger never grows a page.
            params: dict[str, Any] = {}
            for key in ("url", "title", "url_after"):
                if recorded.get(key) is not None:
                    params[key] = recorded[key]
            # There is no honest echo for ``ref`` above all: it is an opaque
            # session-scoped handle, and the recorded effect does not carry the
            # resolved driver element id. Echoing the requested ref against
            # itself is a tautology the verifier would pass for the wrong
            # element, so ``ref`` is simply carried into ``state`` as an opaque
            # identifier for audit — it never participates in the parameter
            # comparison, and verification relies on the other read-back fields.
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": params,
                "state": {
                    key: recorded[key]
                    for key in ("changed", "inserted", "element_count",
                                "count", "ref_map_size", "truncated")
                    if key in recorded
                },
            }
        if action_type.startswith("media."):
            if recorded.get("status") != "ok":
                return None
            # The engine already read the player back; observe() trusts that
            # read-back and never re-queries MPRIS. ``unavailable`` /
            # ``ambiguous`` / ``no_effect`` / ``unsupported`` / ``invalid_input``
            # never reach this dict (the wrapper records nothing for them), so
            # they read as unconfirmed rather than as a confirmed empty result.
            params = {}
            for key in ("player", "track", "state", "url", "path"):
                if recorded.get(key) is not None:
                    params[key] = recorded[key]
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": params,
                "state": {
                    key: recorded[key]
                    for key in ("level", "muted", "changed", "opened",
                                "position_s", "duration_s", "players")
                    if key in recorded
                },
            }
        workspace = self.workspace
        if workspace is None:
            return None
        if action_type in {"device.process.list", "device.project.list"}:
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"count": recorded.get("count")},
            }
        if action_type in {"device.project.status", "device.ide.command"}:
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {
                    key: value
                    for key, value in (
                        ("project", recorded.get("project")),
                        ("command", recorded.get("command")),
                    )
                    if value is not None
                },
                "state": {"exit_code": recorded.get("exit_code")},
            }
        if action_type in {"device.app.open", "device.app.close"}:
            return self._observe_child(
                action, recorded, started=action_type == "device.app.open"
            )
        if action_type == "device.ide.open":
            # Launching an editor is observed as "the editor's CLI ran and exited";
            # nothing here can see the window appear. Saying so in the evidence is the
            # honest version of a claim the verification engine would otherwise make
            # about a GUI the agent has no view of.
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"command": recorded.get("command")},
                "state": {
                    "exit_code": recorded.get("exit_code"),
                    "scope": "command exit only; the editor window was not observed",
                },
            }
        if action_type == "device.process.list":
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"filter": action.parameters.get("filter", "")},
            }
        if action_type in {"device.process.start", "device.server.start"}:
            return self._observe_child(action, recorded, started=True)
        if action_type in {"device.process.stop", "device.server.stop"}:
            return self._observe_child(action, recorded, started=False)
        if action_type == "device.server.status":
            status_parameters: dict[str, Any] = {"name": action.parameters.get("name", "")}
            for field in ("health_path", "expected_status"):
                if field in action.parameters:
                    status_parameters[field] = action.parameters[field]
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": status_parameters,
            }
        if action_type == "device.terminal.execute" and not recorded.get("ran"):
            # The runner stopped this program at its ceiling, so nothing witnessed it
            # finish. Returning the shape anyway would let the verifier match on the
            # command name alone and confirm a run it had watched being killed: no
            # observed state is the honest answer, and it reads as unconfirmed rather
            # than as success.
            return None
        if action_type == "device.terminal.execute":
            # What is observed here is that a process was created, ran to completion,
            # and exited with a code the kernel reported -- not the program's own claim
            # about its work. For a command like a test suite that exit code *is* the
            # effect; for one that mutates the world, the world is what to observe, and
            # a terminal run alone cannot confirm it. Reporting the exit code and the
            # file it touched is honest about which of those this is.
            #
            # Checked before the path guards below because a command has no path: a
            # terminal run ordered after them would report "no observable state" and be
            # refuted for the shape of its own evidence.
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"command": recorded.get("command")},
                "state": {
                    "exit_code": recorded.get("exit_code"),
                    "ran": recorded.get("ran"),
                },
            }
        if action_type == "device.process.logs":
            # What is observed is that a log was read and how much of it came back. The
            # bytes themselves are not evidence and are not repeated here.
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"process_id": recorded.get("process_id")},
                "state": {"bytes": recorded.get("bytes")},
            }
        path = recorded.get("path")
        if not isinstance(path, str):
            return None
        if action_type == "device.file.delete":
            # Absence is the observation. If the file is still there, the effect did
            # not happen, and returning nothing refutes the claim.
            if os.path.exists(os.path.join(workspace.root, path)):
                return None
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"path": path},
            }
        if action_type == "device.file.list":
            return {
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
                "parameters": {"path": path},
            }
        try:
            resolved = workspace.resolve(path, must_exist=True)
            with open(resolved.absolute, "rb") as handle:
                body = handle.read(MAX_WRITE_BYTES + 1)
        except (AgentError, OSError):
            return None
        digest = hashlib.sha256(body).hexdigest()
        parameters: dict[str, Any] = {"path": path}
        # A write is confirmed by the bytes on disk *now*. Echoing the live content
        # is what lets the verifier compare it with what was asked for, so a write
        # that did not land — or landed differently — produces a mismatch rather than
        # a pass. Beyond the cap the live bytes are not compared and the observation
        # falls back to identity, which is what the rest of the system does; refusing
        # to read a huge file is better than a false refutation of a real write.
        if (
            action_type == "device.file.write"
            and "content" in recorded
            and len(body) <= MAX_WRITE_BYTES
        ):
            parameters["content"] = body.decode("utf-8", errors="replace")
        return {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "parameters": parameters,
            "state": {"sha256": digest, "size": len(body)},
        }

    async def verify(self, action: Action, result: ProviderResult) -> VerificationResult:
        """Honest default: this provider does not confirm its own work.

        C6's engine does the confirming, from :meth:`observe`. Overriding this to
        return ``verified=True`` would be the one change that quietly turns
        "independent verification" back into "the executor said so".
        """
        return VerificationResult(
            verified=False,
            method="observe_by_verification_engine",
            detail="the laptop provider does not self-certify; C6 observes the effect",
            simulated=self.simulated,
        )

    # -- shared helpers ---------------------------------------------------
    def _require_workspace(self) -> Workspace:
        if self.workspace is None:
            raise ProviderError(
                "no workspace root is configured for this laptop agent, so file "
                "capabilities are unavailable",
                FailureKind.PERMANENT,
            )
        return self.workspace

    @staticmethod
    def _path(params: dict[str, Any]) -> str:
        path = params.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ProviderError(
                "path must be a non-empty string", FailureKind.INVALID_INPUT
            )
        return path

    @staticmethod
    def _resolve(workspace: Workspace, path: str, *, must_exist: bool = False) -> ResolvedPath:
        try:
            return workspace.resolve(path, must_exist=must_exist)
        except AgentError as exc:
            raise ProviderError(str(exc), _classify(exc)) from exc


def _classify(exc: AgentError) -> FailureKind:
    """Turn a refusal into the kind of failure the executor should report.

    A missing file is a typo: fixing the argument helps. An escape is a denial: it
    must be audited and never retried. Collapsing the two would make a wrong filename
    look like an attack in the audit trail, and an attack indistinguishable from a
    wrong filename.
    """
    if isinstance(exc, PathNotFound):
        return FailureKind.INVALID_INPUT
    return FailureKind.AUTHORIZATION


def _relative_directory(absolute: str, workspace: Workspace | None) -> str:
    """Report the directory the program ran in, as the caller named it."""
    if workspace is None or not absolute:
        return ""
    root = workspace.root
    if absolute == root:
        return "."
    if absolute.startswith(root + os.sep):
        return absolute[len(root) + 1 :]
    # Outside the workspace: permitted only when the operator configured an extra
    # directory, and reported by name rather than by path so the layout stays private.
    return os.path.basename(absolute)


def _decode(body: bytes) -> tuple[str, bool]:
    try:
        return body.decode("utf-8"), False
    except UnicodeDecodeError:
        return "", True


__all__ = ["MAX_LIST_ENTRIES", "MAX_READ_BYTES", "MAX_WRITE_BYTES", "LaptopProvider"]
