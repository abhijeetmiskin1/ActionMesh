"""C19 process and server registry: the things *this agent* started.

Two boundaries, and the second is the one that matters.

**Stop is limited to what this agent launched.** `device.process.stop` takes a
``process_id`` issued by :meth:`ProcessRegistry.adopt`, never a pid. Exposing pid-based
killing to a phone is the "process termination abuse" threat with the handle removed:
pid numbers are small, guessable, sequential, and the ssh daemon's is not much further
away than the test runner's. An id this registry minted cannot name anything it does
not own, and a stop request for an id it has never issued is a refusal rather than a
mistake.

**A server is verified by asking it, not by remembering spawning it.** "Start the dev
server" is not done when a process exists — the process can outlive a crash on an
unbound port, which is the failure that looks like success for thirty seconds until
someone opens the URL. So :func:`is_listening` connects back to the loopback address on
the port the server declared and reports what the *network* says. If the declaration
was wrong, the answer is `not yet listening`, and the action reports
executed-but-unverified rather than success.

The probe takes an ``int`` port and nothing else. There is no host parameter to pass,
so it cannot be turned into an outbound request to somewhere else; that is a structural
answer to an SSRF question, which is sturdier than a validation function.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from actionmesh.agent.commands import CommandDecision
from actionmesh.agent.errors import ProcessError
from actionmesh.agent.runner import CommandRunner

#: Live children this agent will hold. Beyond this, starting another is refused rather
#: than queued: a queue hides how many processes are already running.
MAX_CHILDREN = 16
#: Seconds to wait for a loopback probe before calling a port unbound.
PROBE_TIMEOUT_S = 1.0
#: How much of a captured log one child may accumulate before it is trimmed from the
#: front. Capture is only safe if it cannot fill the disk: a dev server with an
#: unhandled exception in a request loop prints until the machine stops.
MAX_LOG_BYTES = 512 * 1024
#: What replaces the bytes that were dropped, so a short log is never mistaken for a
#: complete one.
TRIM_MARKER = b"[earlier output discarded]\n"

RUNNING = "running"
EXITED = "exited"
STOPPED = "stopped"
TERMINATED = "terminated"


def _noop() -> None:
    return None


def _pgid(pid: int | None) -> int | None:
    if pid is None:
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _close(process: asyncio.subprocess.Process) -> None:
    """Release the transport's fds, tolerating one that is already closed.

    ``Process.close`` exists at runtime and is absent from typeshed, hence the
    indirection through Any rather than a blanket ignore.
    """
    with contextlib.suppress(Exception):
        cast("Any", process).close()


def _kill_group(record: ProcessRecord) -> None:
    """SIGKILL the process group, from any loop.

    Group, not pid: the point of `start_new_session=True` in the runner was that a dev
    server's helpers go down with it. Killing only the parent would leave the real
    server holding the port, which is the failure that looks like it worked.
    """
    target = record.pgid or record.pid
    if target is None:
        return
    try:
        os.killpg(target, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone, or not ours to kill. Either way the record is finished.
        return


def stop_group(record: ProcessRecord, *, grace_s: float = 2.0) -> bool:
    """Stop a child's whole group without an event loop, and say whether it stopped.

    Needed by C35's cross-process cancellation: after a server restart there is no
    ``asyncio.Process`` left for the new process to await, only a pid and a group that
    the previous one recorded. Group rather than pid for the same reason ``stop`` uses
    it -- a dev server's helpers must go with it.

    Returns whether the group is gone. ``False`` is an honest answer, not a retry: the
    caller reports the effect as unconfirmed rather than claiming a stop it did not see.
    """
    target = record.pgid or record.pid
    if target is None:
        return False
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(target, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        if not _alive(record):
            return True
        time.sleep(0.02)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(target, signal.SIGKILL)
    return not _alive(record)


def _alive(record: ProcessRecord) -> bool:
    """Is this pid still a running process -- not a zombie the kernel has not buried?

    ``kill(pid, 0)`` succeeds for a zombie, so a signalled child whose parent has not
    reaped it yet looks alive forever. ``/proc`` says what the process actually is, and
    ``Z`` means the work is finished even if the entry is. Where ``/proc`` does not
    exist, liveness is not answerable from here and the caller is told so.
    """
    pid = record.pid
    if pid is None:
        return record.state == RUNNING
    if os.path.exists("/proc"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                state = handle.read().rsplit(b")", 1)[-1].split()
        except OSError:
            return False
        return bool(state) and state[0] != b"Z"
    # macOS: no /proc. kill(pid, 0) with EPERM means the pid is gone (or not ours);
    # success means it exists. Zombies are handled by the caller's reaper.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


async def is_listening(port: int, *, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """Ask the loopback address whether anything is accepting on ``port``.

    No host argument exists on purpose. The only thing this can reach is this machine,
    which is what a laptop agent reporting its own servers should be able to touch.
    """
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return False
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=timeout_s
        )
    except (OSError, TimeoutError, ValueError):
        return False
    writer.close()
    # The socket may already be gone; the answer to "was it listening" is settled.
    with contextlib.suppress(OSError, asyncio.CancelledError):
        await writer.wait_closed()
    return True


@dataclass
class ProcessRecord:
    """One child this agent started, and what became of it."""

    process_id: str
    kind: str  # "process" | "server"
    label: str
    command: str
    arguments: tuple[str, ...] = ()
    working_directory: str = ""
    port: int | None = None
    pid: int | None = None
    #: The process group to signal. Equal to the pid because children are started in
    #: their own session, and it is what makes a stop work from any event loop.
    pgid: int | None = None
    #: C35 -- which ActionMesh execution ordered this child, and which action within it.
    #: Recorded at the moment of the spawn, because a restart removes every other place
    #: that association existed, and reconciliation has to answer "is the work that
    #: execution started still running?" These are the only handles that question has.
    execution_id: str = ""
    action_id: str = ""
    #: Where this child's stdout/stderr are being kept, if anywhere. Never reported to a
    #: client: it is an absolute path on this machine, and the id above is the handle.
    log_path: str = ""
    state: str = RUNNING
    exit_code: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        return {
            "process_id": self.process_id,
            "kind": self.kind,
            "label": self.label,
            "command": self.command,
            "arguments": list(self.arguments),
            "working_directory": self.working_directory,
            "port": self.port,
            "pid": self.pid,
            "execution_id": self.execution_id,
            "state": self.state,
            "exit_code": self.exit_code,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            # Whether anything is keeping this child's output. It used to be a flat
            # ``false`` with a long comment, because nothing ever was; now it reflects
            # whether the operator configured a log directory, and the panel that shows
            # an empty log still needs to say why it is empty.
            "output_captured": bool(self.log_path),
        }


class MissingServerName(ProcessError):
    """A server was started without the name that would later stop it."""


class ProcessRegistry:
    """Owns the children this agent launched. Nothing else is addressable."""

    def __init__(self, *, max_children: int = MAX_CHILDREN) -> None:
        if max_children < 1:
            raise ProcessError("max_children must be at least 1")
        self.max_children = max_children
        self._records: dict[str, ProcessRecord] = {}
        #: Called whenever a child is created or changes state, so whoever owns the
        #: durable record of this agent's work can be told without this registry knowing
        #: what a database is. Set by the provider; ``None`` in tests that have no ledger.
        self.journal: Callable[[ProcessRecord], None] | None = None
        self._live: dict[str, asyncio.subprocess.Process] = {}
        self._servers: dict[str, str] = {}  # server name -> process_id
        # Killed children whose transport is still owed a close. They are not records:
        # nothing may address them, which is the whole reason they are here.
        self._rejected: list[asyncio.subprocess.Process] = []
        self._counter = 0

    # -- pre-flight -------------------------------------------------------
    def _validate_adoption(self, *, kind: str, label: str) -> None:
        """The checks both entry points share, without touching the process table."""
        self.reap()
        if kind == "server" and not label.strip():
            # A missing required parameter, not a denial: the caller can fix it.
            raise MissingServerName(
                "a server needs a name, so it can be stopped later"
            )
        if kind == "server":
            existing = self.find_server(label)
            if existing is not None and existing.state == RUNNING:
                raise ProcessError(
                    f"a server named {label!r} is already running as "
                    f"{existing.process_id}"
                )
        live = self.running()
        if live >= self.max_children:
            raise ProcessError(
                f"this laptop agent is already running {live} of {self.max_children} "
                "processes; stop one before starting another"
            )

    def reserve(self, *, kind: str, label: str) -> None:
        """Refuse a child *before* one exists.

        Checked here as well as in :meth:`adopt`, and the order is the reason: a
        spawn that is then rejected leaves a running process on the laptop that
        nothing holds a reference to. It keeps the port, it keeps the workspace
        open, and the only way to stop it is to guess its pid -- which is the
        thing this registry exists to prevent.
        """
        self.reap()
        if kind == "server" and not label.strip():
            raise MissingServerName(
                "a server needs a name, so it can be stopped later"
            )
        if kind == "server":
            existing = self.find_server(label)
            if existing is not None and existing.state == RUNNING:
                raise ProcessError(
                    f"a server named {label!r} is already running as "
                    f"{existing.process_id}"
                )
        live = self.running()
        if live >= self.max_children:
            raise ProcessError(
                f"this laptop agent is already running {live} of {self.max_children} "
                "processes; stop one before starting another"
            )

    # -- adoption ---------------------------------------------------------
    def adopt(
        self,
        *,
        kind: str,
        decision: CommandDecision,
        process: asyncio.subprocess.Process,
        label: str = "",
        port: int | None = None,
        working_directory: str = "",
        log_path: str = "",
        execution_id: str = "",
        action_id: str = "",
    ) -> ProcessRecord:
        """Take ownership of a running child.

        Refuses at the cap rather than starting one more: the point of the cap is to
        bound what the agent is holding, and a queue that silently grows it defeats
        that.
        """
        try:
            self._validate_adoption(kind=kind, label=label)
        except ProcessError as exc:
            # The process was already started by the time we got here, and handing it
            # back to the caller unowned is how an orphan ends up holding a port with
            # nothing left that can stop it. Kill what we were given, then re-raise.
            _kill_group(
                ProcessRecord(
                    process_id="?pid", kind=kind, label=label, command="",
                    pid=process.pid, pgid=_pgid(process.pid),
                )
            )
            # The transport is *not* closed here. `close()` on a process the kernel has
            # not reaped yet warns and leaves the pid a zombie, so ownership of it goes
            # to the reaper, which closes it once the exit code has arrived.
            self._rejected.append(process)
            raise exc
        self._counter += 1
        record = ProcessRecord(
            process_id=f"proc_{self._counter:04d}",
            kind=kind,
            label=label.strip() or decision.executable,
            command=decision.executable,
            arguments=tuple(decision.arguments),
            working_directory=working_directory,
            port=port,
            pid=process.pid,
            pgid=_pgid(process.pid),
            log_path=log_path,
            execution_id=execution_id,
            action_id=action_id,
        )
        self._records[record.process_id] = record
        self._live[record.process_id] = process
        if kind == "server":
            self._servers[record.label] = record.process_id
        self._tell(record)
        return record

    def _tell(self, record: ProcessRecord) -> None:
        """Hand the current state of one child to the ledger, if there is one."""
        if self.journal is not None:
            self.journal(record)

    # -- lookup -----------------------------------------------------------
    def get(self, process_id: str) -> ProcessRecord | None:
        return self._records.get(process_id)

    def find_server(self, name: str) -> ProcessRecord | None:
        process_id = self._servers.get(name)
        return self._records.get(process_id) if process_id else None

    def records(
        self, *, include_finished: bool = False, pattern: str = ""
    ) -> list[ProcessRecord]:
        out: list[ProcessRecord] = []
        needle = (pattern or "").lower()
        ordered = sorted(self._records.values(), key=lambda r: r.process_id)
        for record in ordered:
            if not include_finished and record.state != RUNNING:
                continue
            if needle and needle not in f"{record.label} {record.command}".lower():
                continue
            out.append(record)
        return out

    def servers(self, *, include_finished: bool = False) -> list[ProcessRecord]:
        """The servers this agent was told about.

        Finished ones are included on request: a dashboard that shows "Server started"
        and then silently drops the row when it stops leaves the reader guessing
        whether it stopped or the list simply changed shape.
        """
        return [
            record
            for record in self.records(include_finished=include_finished)
            if record.kind == "server"
        ]

    # -- lifecycle --------------------------------------------------------
    @staticmethod
    def alive(record: ProcessRecord) -> bool:
        """Is this child still a process? Asked of the kernel where it can be.

        ``/proc`` is checked because the exit code and liveness a transport reports
        belong to the loop that created it, and a control plane must still be able to
        tell whether its own server is running after that loop has gone. Where the
        pid is unknown, the recorded state is all there is and is used as-is.
        """
        return _alive(record)

    def reap(self) -> int:
        """Pull in exit codes for children that have finished. Returns how many.

        Also closes the transport. asyncio keeps a watch and a pipe fd open until
        `close()` is called, so a laptop that runs a hundred short commands without
        reaping would accumulate a hundred dead file descriptors -- an fd leak earned
        entirely by bookkeeping, which is the kind of defect a long-lived server is
        supposed to be free of.
        """
        self._drain_rejected()
        # Every pass over the table is also the bounding pass: any agent action keeps a
        # captured log from growing without limit, so no background reader is needed
        # and none can die at the wrong moment.
        self.trim_logs()
        changed = 0
        for process_id, process in list(self._live.items()):
            record = self._records.get(process_id)
            if record is None or record.state != RUNNING:
                continue
            code = process.returncode
            if code is None:
                # The transport belongs to the loop that made it; if that loop is gone
                # the returncode will never arrive. Fall back to the pid itself.
                if not self.alive(record):
                    record.state = EXITED
                    record.ended_at = datetime.now(UTC)
                    self._live.pop(process_id, None)
                    _close(process)
                    self._tell(record)
                    changed += 1
                continue
            record.exit_code = code
            record.state = EXITED if code == 0 else TERMINATED
            record.ended_at = datetime.now(UTC)
            self._live.pop(process_id, None)
            _close(process)
            self._tell(record)
            changed += 1
        return changed

    async def stop(self, process_id: str, *, grace_s: float = 3.0) -> ProcessRecord:
        """Stop a child this registry adopted. Anything else is a refusal."""
        record = self._records.get(process_id)
        if record is None:
            # Not a missing file and not a typo to retry: a stop request for an id this
            # agent never issued is the shape of a pid-guessing attack, and refusing
            # without revealing whether the id looks plausible is the right answer.
            raise ProcessError(
                f"no process {process_id!r} was started by this laptop agent; "
                "processes may only be stopped by the agent that launched them"
            )
        if record.state != RUNNING:
            return record
        process = self._live.pop(process_id, None)
        if process is not None:
            try:
                await CommandRunner.stop(process)
                await asyncio.wait_for(process.wait(), timeout=grace_s)
            except TimeoutError:  # pragma: no cover - unstoppable child
                pass
            except OSError as exc:
                raise ProcessError(f"could not stop {process_id}: {exc}") from exc
            except RuntimeError:
                # asyncio can only await a child created on the loop that owns it, so a
                # stop from a later request (or after a reload) raises here. Falling back
                # to a plain group kill is what makes "stop my server" work regardless of
                # which loop is running; the exit code is then simply unknown, and is left
                # as None rather than guessed. The transport joins the rejected list so
                # the next reap closes it -- nobody in this loop may await it.
                _kill_group(record)
                self._rejected.append(process)
            else:
                record.exit_code = process.returncode
            _close(process)
        record.state = STOPPED
        record.ended_at = datetime.now(UTC)
        self._tell(record)
        return record

    async def stop_server(self, name: str) -> ProcessRecord:
        record = self.find_server(name)
        if record is None:
            raise ProcessError(f"no server named {name!r} is known to this agent")
        return await self.stop(record.process_id)

    def trim_logs(self, *, cap_bytes: int = MAX_LOG_BYTES) -> int:
        """Cut every oversized log back to its tail; return how many were cut.

        The child holds the file open in append mode, so the front is rewritten in place
        and the next write lands after the tail that was kept. A write arriving between
        the read and the truncate is lost — log lines, bounded, and said plainly rather
        than hidden: the alternative to trimming is the file filling the disk, and the
        alternative to a file is a pipe that stops the server.
        """
        trimmed = 0
        for record in self._records.values():
            if not record.log_path:
                continue
            try:
                size = os.path.getsize(record.log_path)
                if size <= cap_bytes:
                    continue
                keep = max(cap_bytes // 2, 1024)
                with open(record.log_path, "rb") as handle:
                    handle.seek(max(0, size - keep))
                    tail = handle.read()
                body = TRIM_MARKER + tail[-(cap_bytes - len(TRIM_MARKER)):]
                with open(record.log_path, "r+b") as handle:
                    handle.seek(0)
                    handle.write(body)
                    handle.truncate(len(body))
            except OSError:
                # A log that cannot be read or written is not a reason to fail the
                # action that merely looked at the process table.
                continue
            trimmed += 1
        return trimmed

    def read_log(self, process_id: str, *, max_bytes: int) -> tuple[str, bool] | None:
        """The tail of one child's log, or ``None`` when there is nothing to read.

        Addressed by the id this registry issued, exactly like stopping: there is no way
        to name a file here, and no way to reach a child this agent did not start.
        """
        record = self._records.get(process_id)
        if record is None or not record.log_path:
            return None
        try:
            size = os.path.getsize(record.log_path)
            with open(record.log_path, "rb") as handle:
                if size > max_bytes:
                    handle.seek(size - max_bytes)
                body = handle.read()
        except OSError as exc:
            raise ProcessError(f"cannot read the log for {process_id}: {exc}") from exc
        return body.decode("utf-8", errors="replace"), size > max_bytes

    def _drain_rejected(self) -> None:
        still_open: list[asyncio.subprocess.Process] = []
        for process in self._rejected:
            if process.returncode is not None:
                _close(process)
            else:
                still_open.append(process)
        self._rejected = still_open

    def drain(self) -> None:
        """One more pass over children this agent owns but can no longer await.

        Called when the service shuts down. Children that are *deliberately* running
        (a dev server the user started) are left alone on purpose -- a restart of the
        API process is not a reason to kill someone's server -- but a transport that
        was already killed and never closed should not be handed to the garbage
        collector to complain about.
        """
        self.reap()

    def running(self) -> int:
        self.reap()
        return sum(1 for record in self._records.values() if record.state == RUNNING)


__all__ = [
    "EXITED",
    "MAX_CHILDREN",
    "MAX_LOG_BYTES",
    "RUNNING",
    "STOPPED",
    "TERMINATED",
    "TRIM_MARKER",
    "MissingServerName",
    "ProcessRecord",
    "ProcessRegistry",
    "is_listening",
    "stop_group",
]
