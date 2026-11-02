"""C19d realtime streaming primitives: hub scoping/resume, watch tickets, safe results.

These are the pieces the event endpoints are built from, tested directly so a
regression in the boundary is caught where it lives rather than through a browser.
"""

from __future__ import annotations

import asyncio
import time
import unittest

from actionmesh.core.events import EventHub
from actionmesh.core.executor.serialization import (
    MAX_LIST_ROWS,
    safe_result_view,
)
from actionmesh.core.streaming import StreamTicketStore
from actionmesh.protocol.models import MeshEvent


def _collect(hub: EventHub, **kwargs: object) -> list[MeshEvent]:
    async def scenario() -> list[MeshEvent]:
        stream = hub.subscribe(**kwargs)  # type: ignore[arg-type]
        events: list[MeshEvent] = []
        while True:
            # Drain the replayed window, then stop once the live tail goes quiet --
            # subscribe never ends on its own, so an unbounded await would hang.
            try:
                event = await asyncio.wait_for(stream.__anext__(), timeout=0.2)
            except (TimeoutError, StopAsyncIteration):
                break
            events.append(event)
        await stream.aclose()
        return events

    return asyncio.run(scenario())


class TestHubScoping(unittest.TestCase):
    def test_allow_filters_both_history_and_live(self) -> None:
        hub = EventHub()
        hub.publish(MeshEvent(event_type="mine", execution_id="e1", actor="alice"))
        hub.publish(MeshEvent(event_type="theirs", execution_id="e2", actor="bob"))
        seen = _collect(hub, allow=lambda event: event.actor == "alice")
        self.assertEqual([event.event_type for event in seen], ["mine"])

    def test_execution_id_and_allow_compose_and_only_narrow(self) -> None:
        hub = EventHub()
        hub.publish(MeshEvent(event_type="a1", execution_id="e1", actor="alice"))
        hub.publish(MeshEvent(event_type="a2", execution_id="e2", actor="alice"))
        # execution_id narrows to e1; allow cannot widen it back to e2.
        seen = _collect(hub, execution_id="e1", allow=lambda event: event.actor == "alice")
        self.assertEqual([event.event_type for event in seen], ["a1"])


class TestHubResume(unittest.TestCase):
    def test_since_event_id_replays_only_what_follows_the_cursor(self) -> None:
        hub = EventHub()
        first = MeshEvent(event_type="one", execution_id="e1")
        second = MeshEvent(event_type="two", execution_id="e1")
        third = MeshEvent(event_type="three", execution_id="e1")
        for event in (first, second, third):
            hub.publish(event)
        seen = _collect(hub, since_event_id=first.event_id)
        self.assertEqual([event.event_type for event in seen], ["two", "three"])

    def test_an_unknown_or_aged_out_cursor_replays_the_whole_window(self) -> None:
        # A gap is worse than a duplicate: if the cursor is not in the retained
        # history, replay everything rather than silently dropping events.
        hub = EventHub()
        hub.publish(MeshEvent(event_type="one", execution_id="e1"))
        hub.publish(MeshEvent(event_type="two", execution_id="e1"))
        seen = _collect(hub, since_event_id="evt_not_a_real_id")
        self.assertEqual([event.event_type for event in seen], ["one", "two"])


class TestStreamTickets(unittest.TestCase):
    def test_ticket_resolves_to_the_actor_it_was_minted_for(self) -> None:
        store = StreamTicketStore()
        token, ttl = store.mint("alice")
        self.assertGreater(ttl, 0)
        self.assertEqual(store.resolve(token), "alice")

    def test_ticket_is_reusable_until_it_expires(self) -> None:
        # A reconnecting EventSource must be able to re-present the same ticket.
        store = StreamTicketStore()
        token, _ = store.mint("alice")
        self.assertEqual(store.resolve(token), "alice")
        self.assertEqual(store.resolve(token), "alice")

    def test_unknown_ticket_resolves_to_none(self) -> None:
        self.assertIsNone(StreamTicketStore().resolve("nope"))

    def test_expired_ticket_resolves_to_none_and_is_dropped(self) -> None:
        store = StreamTicketStore(ttl_s=0.01)
        token, _ = store.mint("alice")
        time.sleep(0.02)
        self.assertIsNone(store.resolve(token))
        self.assertEqual(len(store), 0)

    def test_store_is_bounded_and_evicts_the_soonest_to_expire(self) -> None:
        store = StreamTicketStore(ttl_s=100, max_tickets=3)
        tokens = [store.mint(f"a{i}")[0] for i in range(3)]
        newest, _ = store.mint("newest")  # forces one eviction
        self.assertEqual(len(store), 3)
        self.assertEqual(store.resolve(newest), "newest")
        self.assertEqual(sum(1 for token in tokens if store.resolve(token)), 2)

    def test_ticket_tokens_are_not_predictable(self) -> None:
        store = StreamTicketStore()
        tokens = {store.mint("alice")[0] for _ in range(20)}
        self.assertEqual(len(tokens), 20)


class TestSafeResultView(unittest.TestCase):
    def test_content_bearing_keys_are_dropped(self) -> None:
        # The whole point: a file read or a terminal command never ships its body.
        data = {
            "path": "src/app.py",
            "bytes": 12,
            "content": "the actual file",
            "stdout": "hello",
            "stderr": "boom",
            "entries": ["a", "b", "c"],
        }
        view = safe_result_view(data)
        # `entries` became a projected key in C28, but a list of bare strings is not a
        # list of rows, so it is still summarized rather than carried: the names do not
        # cross, only how many there are.
        self.assertEqual(view, {"path": "src/app.py", "bytes": 12, "entries_count": 3})
        self.assertNotIn("content", view)
        self.assertNotIn("stdout", view)
        self.assertNotIn("stderr", view)

    def test_scalars_and_flags_survive(self) -> None:
        view = safe_result_view({"exit_code": 0, "truncated": True, "ready": False, "count": 5})
        self.assertEqual(view, {"exit_code": 0, "truncated": True, "ready": False, "count": 5})

    def test_the_limits_the_agent_enforced_survive_the_safe_view(self) -> None:
        # These are the numbers that explain *why* a run stopped, so a phone can be
        # told about them; the program's output beside them is still dropped.
        view = safe_result_view({
            "timeout_requested_s": 600.0,
            "timeout_applied_s": 1.0,
            "timeout_clipped": True,
            "output_captured": False,
            "stdout": "what the program printed",
        })
        self.assertEqual(
            view,
            {
                "timeout_requested_s": 600.0,
                "timeout_applied_s": 1.0,
                "timeout_clipped": True,
                "output_captured": False,
            },
        )

    def test_long_strings_are_dropped_not_truncated_into_the_view(self) -> None:
        # A long value under an otherwise-safe key is the content we refuse to carry.
        self.assertEqual(safe_result_view({"branch": "x" * 500}), {})

    def test_nested_structures_are_summarized_not_walked(self) -> None:
        # A dict under a safe key is where a payload hides; it is not recursed into.
        self.assertEqual(safe_result_view({"state": {"secret": "x"}}), {})

    def test_a_list_under_a_safe_key_becomes_a_count_not_its_contents(self) -> None:
        self.assertEqual(safe_result_view({"branch": ["main", "dev"]}), {"branch_count": 2})

    def test_an_inventory_list_is_projected_field_by_field(self) -> None:
        # device.process.list and friends return rows the caller genuinely needs (which
        # id do I stop? what port? is it listening?). Each row is re-projected through
        # the same allowlist rather than being summarized away.
        view = safe_result_view({
            "processes": [
                {"process_id": "proc_0001", "state": "running", "port": 8080,
                 "label": "api", "arguments": ["-m", "http.server"], "pid_alive": True},
            ],
            "count": 1,
        })
        self.assertEqual(
            view,
            {
                "count": 1,
                "processes": [{"process_id": "proc_0001", "state": "running",
                               "port": 8080, "label": "api", "pid_alive": True}],
            },
        )

    def test_a_projected_row_carries_no_content_and_no_nesting(self) -> None:
        # The whole reason the view does not recurse is that a dict under a safe key is
        # where a payload hides. Projecting rows cannot reopen that door: a row keeps
        # only allowlisted *scalar* fields, so bodies and nested structures drop out.
        view = safe_result_view({
            "servers": [
                {"label": "web", "content": "the file", "stdout": "transcript",
                 "meta": {"hidden": "payload"}, "rows": ["a", "b"],
                 "listening": True, "path": "apps/web"},
            ]
        })
        self.assertEqual(view, {"servers": [{"label": "web", "listening": True,
                                             "path": "apps/web"}]})

    def test_projection_is_bounded_in_rows(self) -> None:
        rows = [{"label": f"s{i}", "state": "running"} for i in range(200)]
        view = safe_result_view({"servers": rows})
        self.assertEqual(len(view["servers"]), MAX_LIST_ROWS)
        self.assertEqual(view["servers"][0]["label"], "s0")

    def test_a_long_string_is_dropped_inside_a_row_too(self) -> None:
        # A 500-character "label" is not a label; it is content wearing a safe name.
        self.assertEqual(
            safe_result_view({"projects": [{"name": "x" * 500, "path": "ok"}]}),
            {"projects": [{"path": "ok"}]},
        )

    def test_a_directory_listing_survives_as_rows(self) -> None:
        # device.file.list is confined, consented and audited; the phone still could not
        # see a single name in it. Entries are rows, so they project like the rest.
        view = safe_result_view({
            "path": "notes",
            "entries": [
                {"name": "a.txt", "path": "notes/a.txt", "type": "file",
                 "size": 12, "modified": 1.5, "confined": True},
            ],
            "count": 1,
            "hidden_outside_root": 0,
        })
        self.assertEqual(
            view["entries"],
            [{"name": "a.txt", "path": "notes/a.txt", "type": "file",
              "size": 12, "modified": 1.5, "confined": True}],
        )
        # `hidden_outside_root` is not an allowlisted key: the count is reported by the
        # provider's own `count` field, and the rest stays where it is.
        self.assertNotIn("hidden_outside_root", view)

    def test_non_dict_input_is_an_empty_view(self) -> None:
        self.assertEqual(safe_result_view(["not", "a", "dict"]), {})
        self.assertEqual(safe_result_view(None), {})


if __name__ == "__main__":
    unittest.main()
