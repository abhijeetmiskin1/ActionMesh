"""C10 memory and entity-resolution tests."""

from __future__ import annotations

import unittest

from actionmesh.core.memory import MemoryStore


class TestMemoryStore(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = MemoryStore()
        self.mom = self.memory.upsert(
            "person-1", kind="person", name="Mom", aliases={"mother"}
        )

    def test_exact_alias_resolves_case_insensitively(self) -> None:
        result = self.memory.resolve("  MOTHER ")
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.entity, self.mom)

    def test_unknown_name_fails_closed(self) -> None:
        result = self.memory.resolve("someone else")
        self.assertEqual(result.status, "unknown")
        self.assertIsNone(result.entity)

    def test_ambiguous_name_does_not_guess(self) -> None:
        self.memory.upsert("person-2", kind="person", name="Alex")
        self.memory.upsert("person-3", kind="person", name="Alex")
        result = self.memory.resolve("Alex")
        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.entity)
        self.assertEqual(len(result.candidates), 2)

    def test_kind_filter_disambiguates_only_when_explicit(self) -> None:
        self.memory.upsert("device-1", kind="device", name="Mom")
        self.assertEqual(self.memory.resolve("Mom").status, "ambiguous")
        result = self.memory.resolve("Mom", kind="person")
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.entity.entity_id, "person-1")

    def test_upsert_replaces_existing_entity(self) -> None:
        entity = self.memory.upsert("person-1", kind="person", name="Mother")
        self.assertEqual(len(self.memory.entities), 1)
        self.assertEqual(self.memory.resolve("Mother").entity, entity)


if __name__ == "__main__":
    unittest.main()
