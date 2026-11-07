"""P13 §5-8: incremental process output, streamed live to the authorized observer.

The load-bearing claims are: a *real* process produces chunks as it runs (not a
synthetic timer); the chunks are transient (never audited, never replayed); they are
bounded on every axis; and they carry the execution's actor so the existing stream
authorization decides who sees them.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest

from actionmesh.agent.commands import CommandPolicy
from actionmesh.agent.paths import Workspace
from actionmesh.agent.provider import LaptopProvider
from actionmesh.agent.runner import CommandRunner
from actionmesh.core.audit import AuditLog
from actionmesh.core.events import EventHub
from actionmesh.core.output import (
    MAX_CHUNK_BYTES,
    MAX_DOCUMENT_BYTES,
    WHOLE_DOCUMENT_STREAMS,
    OutputPublisher,
    install,
    output_callback,
    uninstall,
)
from actionmesh.protocol.models import Action, MeshEvent


class TestRunnerStreamsChunks(unittest.TestCase):
    """A real process: the callback fires per chunk while it is still running."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        script = os.path.join(self.root, "chatter.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(
                "import sys, time\n"
                "for i in range(4):\n"
                "    print('line', i, flush=True)\n"
                "    time.sleep(0.05)\n"
                "print('to-err', file=sys.stderr, flush=True)\n"
            )
        self.script = script
        self.policy = CommandPolicy(allowed=("python3",), workspace_root=self.root)
        self.runner = CommandRunner(max_concurrent=2, timeout_s=10)

    def test_callback_receives_incremental_stdout_and_stderr(self) -> None:
        decision = self.policy.evaluate("python3", [self.script])
        self.assertFalse(decision.refused)
        seen: list[tuple[str, str]] = []

        async def scenario() -> object:
            return await self.runner.run(decision, on_output=lambda s, t: seen.append((s, t)))

        outcome = asyncio.run(scenario())
        streams = {name for name, _ in seen}
        self.assertIn("stdout", streams)
        self.assertIn("stderr", streams)
        joined = "".join(text for _, text in seen)
        self.assertIn("line 0", joined)
        self.assertIn("line 3", joined)
        self.assertIn("to-err", joined)
        # Streaming does not change the authoritative captured result.
        self.assertEqual(outcome.exit_code, 0)
        self.assertIn("line 0", outcome.stdout)

    def test_no_callback_is_the_old_behaviour(self) -> None:
        decision = self.policy.evaluate("python3", [self.script])
        outcome = asyncio.run(self.runner.run(decision))
        self.assertEqual(outcome.exit_code, 0)

    def test_a_broken_observer_cannot_kill_the_process(self) -> None:
        decision = self.policy.evaluate("python3", [self.script])

        def explode(_stream: str, _chunk: str) -> None:
            raise RuntimeError("observer is broken")

        outcome = asyncio.run(self.runner.run(decision, on_output=explode))
        self.assertEqual(outcome.exit_code, 0)  # the command still ran to completion


class TestFileContentStreamsOverTheOutputChannel(unittest.TestCase):
    """A phone that reads a file on the laptop has to see the bytes it asked for.

    The result boundary strips content by design, so the body travels the one channel
    built for content: transient, bounded, actor-scoped. Nothing here widens who may
    see it -- without a publisher installed the read still works and streams nothing.
    """

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        with open(os.path.join(self.root, "note.txt"), "w", encoding="utf-8") as handle:
            handle.write("hello from the laptop\n")
        with open(os.path.join(self.root, "pic.png"), "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n\x00\x01\xff")
        self.hub = EventHub()
        self.publisher = OutputPublisher(
            self.hub, execution_id="exec-1", action_id="act-1", node_id="n1", actor="abhi"
        )
        self.provider = LaptopProvider(Workspace(self.root))

    def _read(self, path: str) -> tuple[dict, list[MeshEvent]]:
        async def scenario() -> tuple[dict, list[MeshEvent]]:
            token = install(self.publisher)
            received: list[MeshEvent] = []
            try:
                stream = self.hub.subscribe()
                # subscribe() is an async generator: the subscriber only registers when
                # it is first advanced, so publishing before this yields nothing.
                pending = asyncio.create_task(stream.__anext__())
                await asyncio.sleep(0)
                result = await self.provider.execute(
                    Action(type="device.file.read", provider="laptop", parameters={"path": path})
                )
                try:
                    received.append(await asyncio.wait_for(pending, timeout=0.5))
                except (TimeoutError, asyncio.CancelledError):
                    pending.cancel()
                while True:
                    try:
                        received.append(await asyncio.wait_for(stream.__anext__(), timeout=0.1))
                    except (TimeoutError, StopAsyncIteration):
                        break
                await stream.aclose()
            finally:
                uninstall(token)
            return result.data, received

        return asyncio.run(scenario())

    def test_reading_a_text_file_streams_its_body(self) -> None:
        data, events = self._read("note.txt")
        self.assertIn("hello from the laptop", data["content"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "execution.output")
        self.assertEqual(events[0].payload["stream"], "content")
        self.assertEqual(events[0].payload["chunk"], "hello from the laptop\n")
        # Actor-scoped like every other event: whoever may watch this execution may
        # read this body, and nobody else may.
        self.assertEqual(events[0].actor, "abhi")

    def test_a_binary_file_streams_nothing(self) -> None:
        # A PNG to a `<pre>` is not a preview; it is a wall of replacement characters.
        _data, events = self._read("pic.png")
        self.assertEqual(events, [])

    def test_body_and_stream_never_reach_the_audit_chain(self) -> None:
        audit = AuditLog()
        hub = EventHub(audit=audit)
        publisher = OutputPublisher(
            hub, execution_id="exec-1", action_id="act-1", node_id="n1", actor="abhi"
        )
        token = install(publisher)
        try:
            self.provider = LaptopProvider(Workspace(self.root))
            self.provider._stream_content(
                Action(type="device.file.read", parameters={"path": "note.txt"}),
                {"binary": False, "content": "hello from the laptop"},
            )
        finally:
            uninstall(token)
        self.assertEqual(hub.history(), [])
        self.assertEqual(len(audit.records), 0)


class TestOutputPublisher(unittest.TestCase):
    def _hub(self) -> EventHub:
        return EventHub()

    def _publisher(self, hub: EventHub, **kw: object) -> OutputPublisher:
        return OutputPublisher(
            hub,
            execution_id="exec-1",
            action_id="act-1",
            node_id="n1",
            actor="abhi",
            **kw,  # type: ignore[arg-type]
        )

    def test_emit_publishes_bounded_sequenced_actor_scoped_events(self) -> None:
        hub = self._hub()
        publisher = self._publisher(hub)
        received: list[MeshEvent] = []

        async def scenario() -> None:
            stream = hub.subscribe()
            task = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(0)
            publisher.emit("stdout", "hello")
            received.append(await task)
            task2 = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(0)
            publisher.emit("stderr", "oops")
            received.append(await task2)
            await stream.aclose()

        asyncio.run(scenario())
        self.assertEqual(len(received), 2)
        first = received[0]
        self.assertEqual(first.event_type, "execution.output")
        self.assertEqual(first.actor, "abhi")
        self.assertEqual(first.execution_id, "exec-1")
        self.assertEqual(first.action_id, "act-1")
        self.assertEqual(first.payload["node_id"], "n1")
        self.assertEqual(first.payload["sequence"], 1)
        self.assertEqual(first.payload["stream"], "stdout")
        self.assertEqual(first.payload["chunk"], "hello")
        self.assertEqual(received[1].payload["sequence"], 2)
        self.assertEqual(received[1].payload["stream"], "stderr")

    def test_transient_output_never_enters_history_or_audit(self) -> None:
        audit = AuditLog()
        hub = EventHub(audit=audit)
        publisher = self._publisher(hub)
        publisher.emit("stdout", "secret-ish output")
        # Not replayable...
        self.assertEqual(hub.history(), [])
        # ...and not sealed into the audit chain.
        self.assertEqual(len(audit.records), 0)

    def test_chunk_is_bounded_to_max_chunk_bytes(self) -> None:
        hub = self._hub()
        publisher = self._publisher(hub, max_chunk_bytes=16, max_node_bytes=1024)
        received: list[MeshEvent] = []

        async def scenario() -> None:
            stream = hub.subscribe()
            task = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(0)
            publisher.emit("stdout", "x" * 100)
            received.append(await task)
            await stream.aclose()

        asyncio.run(scenario())
        self.assertLessEqual(len(received[0].payload["chunk"].encode()), 16)
        self.assertTrue(received[0].payload["truncated"])

    def test_total_output_is_bounded_and_reports_truncation(self) -> None:
        hub = self._hub()
        publisher = self._publisher(hub, max_chunk_bytes=1024, max_node_bytes=10)
        received: list[MeshEvent] = []

        async def scenario() -> None:
            stream = hub.subscribe()
            task = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(0)
            publisher.emit("stdout", "0123456789ABCDE")  # over the 10-byte node budget
            received.append(await task)
            await stream.aclose()

        asyncio.run(scenario())
        self.assertTrue(publisher.truncated)
        self.assertEqual(len(received), 1)
        self.assertLessEqual(len(received[0].payload["chunk"].encode()), 10)
        self.assertTrue(received[0].payload["truncated"])

        # Once the budget is spent, further output is dropped quietly rather than
        # streamed or buffered -- a flood cannot grow the server.
        before = len(received)
        publisher.emit("stdout", "still more output")
        self.assertEqual(len(received), before)


class TestWholeDocumentStreams(unittest.TestCase):
    """An element list is one document, and a fragment of it parses to nothing.

    The regression this pins down: a 200-element accessibility walk serialises to about
    35 KB, and the byte-stream rule cut it at the 4 KB chunk ceiling. The UI received an
    unparseable prefix, drew an empty list beside a non-zero element count, and told the
    reader "the tree had no elements" -- contradicting the count printed next to it.
    """

    def _publisher(self, hub: EventHub, **kwargs: object) -> OutputPublisher:
        return OutputPublisher(
            hub, execution_id="e", action_id="a", node_id="n", actor="x", **kwargs  # type: ignore[arg-type]
        )

    def _emit_and_receive(
        self, hub: EventHub, publisher: OutputPublisher, stream: str, chunk: str
    ) -> list[MeshEvent]:
        """Emit after the subscriber has registered, and collect what it saw."""
        received: list[MeshEvent] = []

        async def scenario() -> None:
            subscription = hub.subscribe()
            # subscribe() is an async generator: it registers on first advance, so the
            # emit has to come after one turn of the loop.
            task = asyncio.create_task(subscription.__anext__())
            await asyncio.sleep(0)
            publisher.emit(stream, chunk)
            received.append(await asyncio.wait_for(task, timeout=1.0))
            await subscription.aclose()

        asyncio.run(scenario())
        return received

    def test_a_document_larger_than_the_chunk_ceiling_arrives_whole(self) -> None:
        hub = EventHub()
        # ~35 KB, the real shape of a full walk, and far past MAX_CHUNK_BYTES.
        document = "[" + ",".join(['{"role":"push button"}'] * 1600) + "]"
        self.assertGreater(len(document), MAX_CHUNK_BYTES)

        got = self._emit_and_receive(hub, self._publisher(hub), "elements", document)

        self.assertEqual(len(got), 1)  # one event, not nine chunks
        self.assertEqual(got[0].payload["stream"], "elements")
        self.assertFalse(got[0].payload["truncated"])
        self.assertEqual(got[0].payload["chunk"], document)
        # The claim under test: what the reader receives parses as a list.
        self.assertEqual(len(json.loads(got[0].payload["chunk"])), 1600)

    def test_browser_elements_arrive_as_a_complete_document(self) -> None:
        hub = EventHub()
        document = json.dumps({"elements": [{"ref": i, "name": "item" * 10} for i in range(150)]})
        self.assertGreater(len(document), MAX_CHUNK_BYTES)
        got = self._emit_and_receive(hub, self._publisher(hub), "browser.elements", document)
        self.assertFalse(got[0].payload["truncated"])
        self.assertEqual(len(json.loads(got[0].payload["chunk"])["elements"]), 150)

    def test_a_document_over_its_ceiling_is_refused_rather_than_cut(self) -> None:
        hub = EventHub()
        publisher = self._publisher(hub, max_document_bytes=64)
        got = self._emit_and_receive(hub, publisher, "elements", "[" + "1," * 200 + "1]")

        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].payload["truncated"])
        self.assertEqual(got[0].payload["chunk"], "")
        # Never a fragment: an empty payload is honest, half a JSON array is not.
        self.assertTrue(got[0].payload["final"])

    def test_the_default_ceiling_is_the_document_constant(self) -> None:
        # Guards the bound itself: a publisher with no override refuses at the constant.
        hub = EventHub()
        document = "[" + "0," * MAX_DOCUMENT_BYTES + "0]"
        got = self._emit_and_receive(hub, self._publisher(hub), "elements", document)
        self.assertTrue(got[0].payload["truncated"])
        self.assertEqual(got[0].payload["chunk"], "")

    def test_program_output_is_still_streamed_in_chunks(self) -> None:
        # The whole-document rule must not turn a chatty program into one giant event.
        hub = EventHub()
        got = self._emit_and_receive(hub, self._publisher(hub), "stdout", "y" * 10000)
        self.assertEqual(len(got[0].payload["chunk"]), MAX_CHUNK_BYTES)
        self.assertTrue(got[0].payload["truncated"])

    def test_element_documents_are_framed_whole(self) -> None:
        # The rule is a set, not a special case buried in a branch: this pins membership
        # so adding a stream to it is a deliberate act with a test to match.
        self.assertEqual(WHOLE_DOCUMENT_STREAMS, frozenset({"elements", "browser.elements"}))


class TestContextVarSeam(unittest.TestCase):
    def test_callback_is_none_without_a_publisher(self) -> None:
        self.assertIsNone(output_callback())

    def test_installed_publisher_is_reachable_and_torn_down(self) -> None:
        hub = EventHub()
        publisher = OutputPublisher(
            hub, execution_id="e", action_id="a", node_id="n", actor="x"
        )
        token = install(publisher)
        try:
            callback = output_callback()
            self.assertIsNotNone(callback)
            self.assertEqual(callback, publisher.emit)  # bound methods compare by (obj, func)
        finally:
            uninstall(token)
        self.assertIsNone(output_callback())


if __name__ == "__main__":
    unittest.main()
