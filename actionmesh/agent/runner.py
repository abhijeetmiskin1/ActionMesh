"""C19 process runner: the only module in ActionMesh that starts a program.

Everything that executes on this laptop funnels through these two calls, and the
point of that is reviewability. The dangerous-primitive guard
(``tests/security/test_dangerous_primitives.py``) forbids ``subprocess`` everywhere in
the package and permits exactly this path; widening that list is a visible edit to a
security test, not a quiet import in a new file.

What is fixed here, and why it cannot be loosened from a request
----------------------------------------------------------------
``shell=False``, and there is no code path that could set it otherwise. The program is
argv[0] and each argument is one argv element; nothing is ever parsed by a shell, so
``;``, ``&&``, pipes and redirection are *characters in an argument* rather than
instructions. This is why :class:`~actionmesh.agent.commands.CommandPolicy` refuses an
``arguments`` string outright — the only thing a string could be waiting for is a
shell.

``env`` is the policy's constructed environment, never ``os.environ``. This process
holds the key that hashes every pairing code and credential; handing it to a child
would let anything the phone runs brute-force a 6-digit code offline with a copy of
the database.

Output is capped while it is read, not after. ``communicate()`` on an unbounded stream
is a memory-exhaustion primitive: ``yes`` written to a log file that a phone asked for
is 4 GB of the laptop's RAM before any truncation happens.

A timeout kills the whole process group. A child that spawned helpers and then died
would leave them running against the workspace, holding its files, with nothing left
that reports them.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from actionmesh.agent.commands import CommandDecision
from actionmesh.agent.errors import ProcessError

#: Bytes kept from each stream. Anything past it is read and discarded, because
#: leaving it unread deadlocks the pipe.
MAX_OUTPUT_BYTES = 64 * 1024
#: Grace between SIGTERM and SIGKILL when a command is stopped.
TERMINATE_GRACE_S = 3.0


@dataclass(frozen=True)
class CommandOutcome:
    """What actually happened to a process. Exit code included, opinion excluded."""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    duration_s: float = 0.0
    timed_out: bool = False
    #: True when the exit code was non-zero. Reported as a fact for the verifier to
    #: observe; a failing test is a *successful* execution of a failing test.
    failed: bool = False
    #: The timeout the caller asked for, and the one actually enforced. Kept apart
    #: because the agent's configured ceiling wins when the two disagree, and a run
    #: stopped at 60s by the laptop reads exactly like a command that hung for the
    #: two minutes its caller requested unless both numbers travel.
    timeout_requested_s: float | None = None
    timeout_applied_s: float = 0.0

    @property
    def timeout_clipped(self) -> bool:
        """True when this laptop cut the caller's timeout short."""
        return (
            self.timeout_requested_s is not None
            and self.timeout_applied_s < self.timeout_requested_s
        )

    def public(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "duration_s": round(self.duration_s, 3),
            "timed_out": self.timed_out,
            "failed": self.failed,
            "timeout_requested_s": self.timeout_requested_s,
            "timeout_applied_s": self.timeout_applied_s,
            "timeout_clipped": self.timeout_clipped,
        }


class _Streams:
    """Mutable byte buffers that outlive the task that filled them.

    Deliberately an instance, not a dataclass with mutable defaults or class
    attributes: two runs that share a buffer interleave each other's output, which in
    a control plane means one caller reads another caller's data.
    """

    def __init__(self) -> None:
        self.out = bytearray()
        self.err = bytearray()
        self.truncated = False

    @staticmethod
    def text(buffer: bytearray) -> str:
        return bytes(buffer).decode("utf-8", errors="replace")


@dataclass
class CommandRunner:
    """Bounded, capped, group-killed execution of an already-decided command."""

    max_concurrent: int = 4
    timeout_s: float = 60.0
    max_output_bytes: int = MAX_OUTPUT_BYTES
    _slots: asyncio.Semaphore | None = field(default=None, repr=False)
    _bound_to: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.max_concurrent < 1:
            raise ProcessError("max_concurrent must be at least 1")
        if self.timeout_s <= 0:
            raise ProcessError("timeout_s must be positive")

    def _semaphore(self) -> asyncio.Semaphore:
        """One semaphore per running loop.

        An ``asyncio.Semaphore`` binds to the loop it is first used on; a server that
        is started twice in one process (a test, then a reload) would otherwise
        deadlock on a permit taken by a loop that no longer exists.
        """
        loop = asyncio.get_running_loop()
        token = id(loop)
        if self._slots is None or self._bound_to != token:
            self._slots = asyncio.Semaphore(self.max_concurrent)
            self._bound_to = token
        return self._slots

    async def run(
        self,
        decision: CommandDecision,
        *,
        timeout_s: float | None = None,
        on_output: Callable[[str, str], None] | None = None,
        on_spawn: Callable[[asyncio.subprocess.Process], None] | None = None,
    ) -> CommandOutcome:
        """Execute an allowed decision to completion, with its output captured.

        ``on_output`` is called as ``(stream, text)`` for each chunk while the process
        runs, so a caller watching the execution sees progress live. It is optional and
        purely observational: it never changes what is captured, whether the command
        ran, or the returned outcome.
        """
        if decision.refused:
            raise ProcessError(
                "refusing to run a decision the policy did not allow: "
                + "; ".join(decision.reasons)
            )
        limit = min(
            self.timeout_s, timeout_s if timeout_s is not None else self.timeout_s
        )
        started = time.monotonic()
        # Collected outside the task so a cancelled read keeps what it saw. A run that
        # timed out is the run whose output matters most -- "the test suite hung at
        # test_login" is the answer, and dropping the buffer to make the timeout
        # tidier throws away the only evidence of where it stopped.
        collected = _Streams()
        async with self._semaphore():
            process = await _start(decision, pipe=True)
            if on_spawn is not None:
                # C35: the child exists now, and this is the only moment anybody knows
                # both its pid and which execution it was ordered for. A command that
                # outlives this process -- a restart during a test run -- is only
                # reconcilable if that was written down before it could be lost.
                on_spawn(process)
            try:
                truncated = await asyncio.wait_for(
                    _drain(process, self.max_output_bytes, collected, on_output),
                    timeout=limit,
                )
            except TimeoutError:
                await _stop(process)
                return CommandOutcome(
                    exit_code=None,
                    stdout=collected.text(collected.out),
                    stderr=(
                        collected.text(collected.err) + f"\n[stopped after {limit}s]"
                    ),
                    truncated=collected.truncated,
                    duration_s=time.monotonic() - started,
                    timed_out=True,
                    failed=True,
                    timeout_requested_s=timeout_s,
                    timeout_applied_s=limit,
                )
            except asyncio.CancelledError:
                # The caller gave up, not the program. Leaving it running would be a
                # side effect with no owner, so the whole group goes with it.
                await _stop(process)
                raise
            finally:
                if process.returncode is None:
                    await _stop(process)
        exit_code = process.returncode
        return CommandOutcome(
            exit_code=exit_code,
            stdout=collected.text(collected.out),
            stderr=collected.text(collected.err),
            truncated=truncated,
            duration_s=time.monotonic() - started,
            failed=(exit_code != 0),
            timeout_requested_s=timeout_s,
            timeout_applied_s=limit,
        )

    @staticmethod
    async def stop(process: asyncio.subprocess.Process) -> None:
        """Stop a process this runner started, and everything it spawned.

        Public because stopping is the other half of starting: a device that can
        launch a dev server must be able to shut the same tree down, and the one
        primitive that does it reliably is the group kill below.
        """
        await _stop(process)

    async def start(
        self, decision: CommandDecision, *, log_path: str | None = None
    ) -> asyncio.subprocess.Process:
        """Launch and detach: a long-running server, not a command that answers.

        Returns while the process is still running. With no ``log_path`` its output is
        discarded — a dev server that prints forever would otherwise grow memory until
        something died. Whoever starts it owns stopping it.

        With ``log_path`` the child writes to that file instead. A file, deliberately,
        and not a pipe: a pipe nobody drains fills at ~64 KiB and the server blocks in
        ``write``, which is the failure that looks like a hung machine. A file applies no
        backpressure, so capturing costs the child nothing -- and the file is bounded
        after the fact by the registry's trim rather than by stopping the reader.
        """
        if decision.refused:
            raise ProcessError(
                "refusing to start a decision the policy did not allow: "
                + "; ".join(decision.reasons)
            )
        handle = _open_log(log_path) if log_path else None
        try:
            return await _start(decision, pipe=False, log_handle=handle)
        finally:
            # The child inherited its own descriptor at exec; ours is ours to close.
            if handle is not None:
                handle.close()


def _open_log(path: str) -> Any:
    """Open (or create) a child's log, readable and writable by this user only.

    The mode is set twice on purpose: ``open`` applies the process umask, and a log of
    whatever a server printed is not something to leave group-readable on a shared
    machine.
    """
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "ab", buffering=0)
    except OSError as exc:
        raise ProcessError(f"cannot open the log for this process: {exc}") from exc


async def _start(
    decision: CommandDecision,
    *,
    pipe: bool,
    log_handle: Any = None,
) -> asyncio.subprocess.Process:
    argv = [decision.resolved_path, *decision.arguments]
    # One file receives both streams, interleaved. For a server log that is the useful
    # answer -- "what happened, in order" -- and separating them would mean two files
    # per child to bound and to clean up.
    sink = log_handle if log_handle is not None else (
        asyncio.subprocess.PIPE if pipe else asyncio.subprocess.DEVNULL
    )
    try:
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=decision.working_directory or None,
            env=dict(decision.environment),
            stdout=sink,
            stderr=sink,
            stdin=asyncio.subprocess.DEVNULL,
            # A new session, so the whole tree can be stopped together. Without it,
            # `uvicorn` outlives the "stop" the user pressed and keeps the port.
            start_new_session=True,
            limit=MAX_OUTPUT_BYTES,
        )
    except (OSError, ValueError) as exc:
        raise ProcessError(f"cannot start {argv[0]!r}: {exc}") from exc


async def _drain(
    process: asyncio.subprocess.Process,
    cap: int,
    into: _Streams,
    on_output: Callable[[str, str], None] | None = None,
) -> bool:
    """Read both streams, keeping at most ``cap`` bytes of each.

    Both are read concurrently and both are read *to the end*: a program that fills a
    pipe nobody drains blocks in ``write``, and a blocked child looks exactly like a
    slow one. ``on_output`` is called with ``(stream, text)`` for each chunk as it is
    read -- before the byte cap is applied to the retained buffer, so a watcher sees
    output even after the stored copy has filled -- and any exception it raises is
    swallowed: a broken observer must never kill the process it is watching.
    """

    def emit(name: str, chunk: bytes) -> None:
        if on_output is None:
            return
        with contextlib.suppress(Exception):
            on_output(name, chunk.decode("utf-8", errors="replace"))

    async def read(
        stream: asyncio.StreamReader | None, buffer: bytearray, name: str
    ) -> bool:
        if stream is None:
            return False
        dropped = False
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            emit(name, chunk)
            room = cap - len(buffer)
            if room > 0:
                buffer.extend(chunk[:room])
            if len(chunk) > room:
                dropped = True
        return dropped

    cut = await asyncio.gather(
        read(process.stdout, into.out, "stdout"),
        read(process.stderr, into.err, "stderr"),
    )
    await process.wait()
    into.truncated = any(cut)
    return into.truncated


async def _stop(process: asyncio.subprocess.Process) -> None:
    """SIGTERM the process *group*, then SIGKILL whatever ignored it."""
    try:
        await _signal_groups(process)
    finally:
        # Whatever happened above, nobody is going to await this child again, and an
        # unclosed transport holds a pipe fd and a child watch for the life of the
        # loop. Closing before the exit status arrives would only warn and abandon a
        # zombie, so an unreaped child is left to the loop that owns it.
        if process.returncode is not None:
            with contextlib.suppress(Exception):
                cast("Any", process).close()


async def _signal_groups(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    pid = process.pid
    try:
        group = os.getpgid(pid)
    except (OSError, AttributeError):
        group = None
    for signal_to_send in (signal.SIGTERM, signal.SIGKILL):
        if process.returncode is not None:
            return
        try:
            if group is not None:
                # killpg, not os.kill: the target is the session this module created,
                # and every helper that outlived the parent needs to go with it.
                os.killpg(group, signal_to_send)
            else:
                process.send_signal(signal_to_send)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_S)
            return
        except TimeoutError:
            continue
    with contextlib.suppress(TimeoutError):  # pragma: no cover - unstoppable child
        await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_S)
    if process.returncode is not None:
        with contextlib.suppress(Exception):
            cast("Any", process).close()


__all__ = [
    "MAX_OUTPUT_BYTES",
    "CommandOutcome",
    "CommandRunner",
]
