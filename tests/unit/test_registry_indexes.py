"""Registry indexes preserve version semantics across registration and initial data."""
from dataclasses import replace

import pytest

from actionmesh.core.registry import ActionRegistry, DuplicateError, seed_definitions


def test_indexes_follow_semantic_versions_and_late_registration() -> None:
    base = seed_definitions()[0]
    older = replace(base, name="indexed.action", version="2.9")
    newer = replace(base, name="indexed.action", version="2.10")
    registry = ActionRegistry({(older.name, older.version): older, (newer.name, newer.version): newer})
    assert registry.has("indexed.action")
    assert not registry.has("missing.action")
    assert registry.get("indexed.action") is newer
    assert registry.get("indexed.action", "2.9") is older
    assert registry.versions("indexed.action") == ["2.9", "2.10"]
    registry.register(replace(older, version="2.8"))
    assert registry.get("indexed.action") is newer
    latest = replace(newer, version="3.0")
    registry.register(latest)
    assert registry.get("indexed.action") is latest
    with pytest.raises(DuplicateError):
        registry.register(replace(latest))
    assert registry.get("indexed.action") is latest
    assert [item.version for item in registry.discover(name="indexed.action")] == ["3.0"]
