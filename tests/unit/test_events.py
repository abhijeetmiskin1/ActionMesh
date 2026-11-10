"""C9 event hub tests."""

from __future__ import annotations

import asyncio
import unittest

from actionmesh.core.events import EventHub
from actionmesh.protocol.models import MeshEvent


class TestEventHub(unittest.TestCase):
    def test_subscriber_queue_size_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            EventHub(max_subscriber_queue=0)

    def test_history_is_bounded_and_filterable(self) -> None:
        hub = EventHub(max_history=2)
        hub.publish(MeshEvent(event_type="one", execution_id="e1"))
        hub.publish(MeshEvent(event_type="two", execution_id="e2"))
        hub.publish(MeshEvent(event_type="three", execution_id="e1"))
        self.assertEqual([event.event_type for event in hub.history()], ["two", "three"])
        self.assertEqual([event.event_type for event in hub.history("e1")], ["three"])

    def test_subscriber_receives_live_event(self) -> None:
        async def scenario() -> MeshEvent:
            hub = EventHub()
            stream = hub.subscribe("e1")
            pending = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(0)
            hub.publish(MeshEvent(event_type="live", execution_id="e1"))
            event = await asyncio.wait_for(pending, timeout=1)
            await stream.aclose()
            return event

        event = asyncio.run(scenario())
        self.assertEqual(event.event_type, "live")

    def test_slow_subscriber_is_disconnected_when_queue_overflows(self) -> None:
        async def scenario() -> str:
            hub = EventHub(max_history=1, max_subscriber_queue=1)
            stream = hub.subscribe("e1")
            first_pending = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(0)
            hub.publish(MeshEvent(event_type="first", execution_id="e1"))
            first = await first_pending
            hub.publish(MeshEvent(event_type="queued", execution_id="e1"))
            hub.publish(MeshEvent(event_type="overflow", execution_id="e1"))
            with self.assertRaises(StopAsyncIteration):
                await stream.__anext__()
            return first.event_type

        self.assertEqual(asyncio.run(scenario()), "first")


if __name__ == "__main__":
    unittest.main()
