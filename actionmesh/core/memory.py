"""C10 in-memory entities and fail-closed name resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _normalise(value: str) -> str:
    return " ".join(value.casefold().split())


@dataclass
class Entity:
    entity_id: str
    kind: str
    name: str
    aliases: set[str] = field(default_factory=set)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "name": self.name,
            "aliases": sorted(self.aliases),
            "attributes": dict(self.attributes),
        }


@dataclass
class Resolution:
    status: str
    query: str
    entity: Entity | None = None
    candidates: list[Entity] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "query": self.query,
            "entity": self.entity.to_dict() if self.entity else None,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "reason": self.reason,
        }


class MemoryStore:
    """Small deterministic store; unknown and ambiguous names never resolve."""

    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}

    def upsert(
        self,
        entity_id: str,
        *,
        kind: str,
        name: str,
        aliases: set[str] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Entity:
        if not entity_id.strip() or not kind.strip() or not name.strip():
            raise ValueError("entity_id, kind, and name are required")
        entity = Entity(
            entity_id=entity_id.strip(),
            kind=kind.strip(),
            name=name.strip(),
            aliases={alias.strip() for alias in (aliases or set()) if alias.strip()},
            attributes=dict(attributes or {}),
        )
        self.entities[entity.entity_id] = entity
        return entity

    def resolve(self, query: str, *, kind: str | None = None) -> Resolution:
        if not isinstance(query, str) or not query.strip():
            return Resolution("unknown", str(query), reason="empty query")
        wanted = _normalise(query)
        candidates = [
            entity
            for entity in self.entities.values()
            if (kind is None or entity.kind == kind)
            and wanted in {_normalise(entity.name), *(_normalise(a) for a in entity.aliases)}
        ]
        if len(candidates) == 1:
            return Resolution("resolved", query, entity=candidates[0])
        if len(candidates) > 1:
            return Resolution(
                "ambiguous", query, candidates=candidates,
                reason="multiple entities match exactly; refusing to guess",
            )
        return Resolution("unknown", query, reason="no exact entity match")


__all__ = ["Entity", "MemoryStore", "Resolution"]