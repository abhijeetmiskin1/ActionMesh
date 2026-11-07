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
#: A single whole-document event carries at most this many bytes. See
#: :data:`WHOLE_DOCUMENT_STREAMS` for why this channel is framed differently.
#: Streams that carry one *whole* JSON document rather than a byte stream. A program's
#: stdout is a sequence, so cutting it between two bytes and continuing is what a reader
#: wants; an element list is a single document, and a fragment of it is not "less of the
#: answer" -- it is unparseable, so the reader gets nothing at all. These streams are
#: therefore delivered entire or refused, never cut.
WHOLE_DOCUMENT_STREAMS = frozenset({"elements", "browser.elements"})
#: The node budget for a whole-document stream. An accessibility walk returns at most
#: ``device_screen._MAX_ELEMENTS`` (200) elements, and one element measured on a real
#: desktop is about 170 bytes of JSON, so the cap that fits the walk is roughly 35 KB.
#: 256 KB leaves room for a tree of unusually talkative elements while still being a
#: hard bound: the walk itself, not this channel, is what limits how many there are.
MAX_DOCUMENT_BYTES = 256 * 1024


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
        max_document_bytes: int = MAX_DOCUMENT_BYTES,
    ) -> None:
        self._hub = hub
        self._execution_id = execution_id
        self._action_id = action_id
        self._node_id = node_id
        self._actor = actor
        self._device_id = device_id
        self._max_chunk = max_chunk_bytes
        self._max_node = max_node_bytes
        self._max_document = max_document_bytes
        self._sequence = 0
        self._retained = 0
        self.truncated = False

    def emit(self, stream: str, chunk: str) -> None:
        """Publish one output chunk; called by the runner as the process produces it."""
        if not chunk:
            return
        if stream in WHOLE_DOCUMENT_STREAMS:
            self._emit_document(stream, chunk)
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

    def _emit_document(self, stream: str, chunk: str) -> None:
        """Publish a whole-document stream: entire, or not at all.

        A reader that receives half a JSON array has no elements — not few elements —
        because the document does not parse. Sending the working half would be worse
        than sending nothing: the result view would show an empty list next to a
        non-zero count, and the reader would be told the tree held nothing when what
        happened is that this channel cut it.

        So the document travels as one event, and its own budget caps it. A document that
        does not fit is reported as truncated with an empty payload: the caller still has
        the element *count* from the result, and the missing names are stated rather than
        silently short.
        """
        data = chunk.encode("utf-8", errors="replace")
        if len(data) > self._max_document:
            if not self.truncated:
                self.truncated = True
                self._publish(stream=stream, text="", final_truncation=True)
            return
        self._retained += len(data)
        self._publish(stream=stream, text=chunk, final_truncation=False)

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
    "WHOLE_DOCUMENT_STREAMS",
    "OutputPublisher",
    "current_publisher",
    "install",
    "output_callback",
    "uninstall",
]
