"""Incremental process output, streamed to the authorized observer of one node.

The provider runs the process; the engine owns the execution's identity and actor; the
event hub owns delivery. This module is the seam between them: the engine installs an
:class:`OutputPublisher` for the duration of one node's dispatch, and the laptop
provider -- the only code that reads a live process -- hands that publisher's ``emit``
to the runner as a callback. Providers that return only a final result never touch it,
so the existing provider contract is unchanged.

Three rules make this safe enough to ship:

* Output is **transient** -- published with ``transient=True`` so it reaches live
  subscribers but is never sealed into the audit chain or kept in the replay history.
  A control plane records decisions and outcomes, not a copy of whatever a program
  printed.
* It is **bounded** on every axis that a hostile or chatty command can push: per-chunk
  bytes, per-node total bytes, and a hard stop that flips a ``truncated`` flag rather
  than growing memory.
* It is **actor-scoped** -- every event carries the execution's actor, so the existing
  stream authorization decides who sees it. A publisher is never handed to a client
  that did not already earn the execution's event stream.

Output is treated as potentially sensitive: it may contain secrets the *program*
printed even though ActionMesh never transmits environment variables. Streaming is
therefore opt-in per capability and the honest posture is "bounded, live, to the
authorized observer only" -- not "sanitized."
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token

from actionmesh.core.events import EventHub
from actionmesh.protocol.models import MeshEvent

#: The one event type the output channel publishes. It is also the only transient
#: event in the system, which is why the SSE layer can name it rather than guess: a
#: consumer must not be handed a resume cursor that nothing can replay.
OUTPUT_EVENT_TYPE = "execution.output"
#: A single output event carries at most this many bytes of a program's output.
MAX_CHUNK_BYTES = 4096
#: Total output retained/streamed for one node before it is marked truncated.
MAX_NODE_BYTES = 64 * 1024


class OutputPublisher:
    """Turns runner output chunks into bounded, transient, actor-scoped events."""

    def __init__(
        self,
        hub: EventHub,
        *,
        execution_id: str,
        action_id: str,
        node_id: str,
        actor: str,
        device_id: str = "",
        max_chunk_bytes: int = MAX_CHUNK_BYTES,
        max_node_bytes: int = MAX_NODE_BYTES,
    ) -> None:
        self._hub = hub
        self._execution_id = execution_id
        self._action_id = action_id
        self._node_id = node_id
        self._actor = actor
        self._device_id = device_id
        self._max_chunk = max_chunk_bytes
        self._max_node = max_node_bytes
        self._sequence = 0
        self._retained = 0
        self.truncated = False

    def emit(self, stream: str, chunk: str) -> None:
        """Publish one output chunk; called by the runner as the process produces it."""
        if not chunk:
            return
        if self._retained >= self._max_node:
            # Budget spent: keep supervising the process, stop shipping bytes, and say
            # so once. Silently dropping output would look like the program went quiet.
            if not self.truncated:
                self.truncated = True
                self._publish(stream=stream, text="", final_truncation=True)
            return
        data = chunk.encode("utf-8", errors="replace")
        room = min(self._max_chunk, self._max_node - self._retained)
        cut = len(data) > room
        if cut:
            data = data[:room]
            self.truncated = True
        text = data.decode("utf-8", errors="replace")
        self._retained += len(data)
        self._publish(stream=stream, text=text, final_truncation=False)

    def _publish(self, *, stream: str, text: str, final_truncation: bool) -> None:
        self._sequence += 1
        self._hub.publish(
            MeshEvent(
                event_type=OUTPUT_EVENT_TYPE,
                execution_id=self._execution_id,
                action_id=self._action_id,
                actor=self._actor,
                payload={
                    "node_id": self._node_id,
                    "device_id": self._device_id,
                    "stream": stream,
                    "sequence": self._sequence,
                    "chunk": text,
                    "truncated": self.truncated,
                    "final": final_truncation,
                },
            ),
            transient=True,
        )


#: The publisher for the node currently being dispatched, if any. Set by the engine
#: around ``provider.execute`` and read by the laptop provider -- same task, so a plain
#: context var is enough and no provider signature has to change.
_CURRENT: ContextVar[OutputPublisher | None] = ContextVar("actionmesh_output", default=None)


def install(publisher: OutputPublisher) -> Token[OutputPublisher | None]:
    return _CURRENT.set(publisher)


def uninstall(token: Token[OutputPublisher | None]) -> None:
    _CURRENT.reset(token)


def current_publisher() -> OutputPublisher | None:
    return _CURRENT.get()


def output_callback() -> Callable[[str, str], None] | None:
    publisher = _CURRENT.get()
    return publisher.emit if publisher is not None else None


__all__ = [
    "MAX_CHUNK_BYTES",
    "MAX_NODE_BYTES",
    "OUTPUT_EVENT_TYPE",
    "OutputPublisher",
    "current_publisher",
    "install",
    "output_callback",
    "uninstall",
]
