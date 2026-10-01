"""Server-side consent store (C3).

Holds the grants the user has actually given. This is the *only* place a grant
is allowed to come from: callers cannot pass grants through an HTTP request body.

Wildcard scopes are refused outright. Consenting to ``*`` would make every
future capability retroactively authorized, which defeats the purpose of consent.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from actionmesh.core.permissions.catalog import is_concrete
from actionmesh.core.permissions.context import Consent, PermissionGrant
from actionmesh.protocol.models import RiskLevel


class ConsentError(ValueError):
    """Raised for an invalid consent request."""


class ConsentStore:
    """In-memory consent ledger (C3). C15 will back this with SQLite."""

    def __init__(self) -> None:
        self._consents: dict[str, list[Consent]] = {}

    def grant(self, consent: Consent) -> Consent:
        if not isinstance(consent, Consent):
            raise ConsentError("consent must be a Consent")
        if not consent.scope or not is_concrete(consent.scope):
            raise ConsentError(
                f"refusing to grant non-concrete scope {consent.scope!r}; "
                "wildcard consent is not permitted"
            )
        bucket = self._consents.setdefault(consent.scope, [])
        bucket.append(consent)
        return consent

    def revoke(self, scope: str) -> int:
        """Remove every consent for a scope. Returns how many were removed."""
        return len(self._consents.pop(scope, []))

    def consents(self, scope: str | None = None) -> list[Consent]:
        if scope is not None:
            return list(self._consents.get(scope, []))
        return [c for bucket in self._consents.values() for c in bucket]

    def grants(
        self, *, now: datetime | None = None, for_risk: RiskLevel | None = None
    ) -> list[PermissionGrant]:
        """Flatten consents into grants for a policy context.

        ``for_risk`` is the risk the caller intends to authorize. When given, a
        consent whose own ``max_risk`` ceiling is below that risk is excluded —
        a user who capped consent at HIGH has not consented to CRITICAL. When
        omitted, every non-expired consent is returned and each grant keeps its
        own ceiling for the engine to enforce.
        """
        moment = now or datetime.now(UTC)
        out: list[PermissionGrant] = []
        for consent in self.consents():
            grant = consent.to_grant()
            if for_risk is not None:
                if not grant.is_active(moment, for_risk):
                    continue
            elif grant.is_expired(moment):
                continue
            out.append(grant)
        return out

    def grants_for(
        self,
        actor_id: str,
        *,
        now: datetime | None = None,
        for_risk: RiskLevel | None = None,
    ) -> list[PermissionGrant]:
        """Return only active grants explicitly granted to ``actor_id``."""
        moment = now or datetime.now(UTC)
        out: list[PermissionGrant] = []
        for consent in self.consents():
            if consent.granted_by != actor_id:
                continue
            grant = consent.to_grant()
            if for_risk is not None:
                if not grant.is_active(moment, for_risk):
                    continue
            elif grant.is_expired(moment):
                continue
            out.append(grant)
        return out

    def __len__(self) -> int:
        return sum(len(v) for v in self._consents.values())


class SQLiteConsentStore(ConsentStore):
    """Durable consent store preserving the same validation and grant API."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS consents "
            "(consent_id TEXT PRIMARY KEY, scope TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self.connection.commit()

    def grant(self, consent: Consent) -> Consent:
        if not isinstance(consent, Consent):
            raise ConsentError("consent must be a Consent")
        if not consent.scope or not is_concrete(consent.scope):
            raise ConsentError(
                f"refusing to grant non-concrete scope {consent.scope!r}; "
                "wildcard consent is not permitted"
            )
        self.connection.execute(
            "INSERT OR REPLACE INTO consents(consent_id, scope, payload) VALUES (?, ?, ?)",
            (consent.granted_at.isoformat() + consent.scope, consent.scope, consent.model_dump_json()),
        )
        self.connection.commit()
        return consent

    def revoke(self, scope: str) -> int:
        cursor = self.connection.execute("DELETE FROM consents WHERE scope = ?", (scope,))
        self.connection.commit()
        return cursor.rowcount

    def consents(self, scope: str | None = None) -> list[Consent]:
        query = "SELECT payload FROM consents"
        params: tuple[str, ...] = ()
        if scope is not None:
            query += " WHERE scope = ?"
            params = (scope,)
        rows = self.connection.execute(query, params).fetchall()
        return [self._decode(row[0]) for row in rows]

    @staticmethod
    def _decode(payload: str) -> Consent:
        raw = json.loads(payload)
        return Consent(
            scope=raw["scope"],
            granted_by=raw["granted_by"],
            reason=raw["reason"],
            max_risk=RiskLevel(raw["max_risk"]),
            granted_at=datetime.fromisoformat(raw["granted_at"]),
            expires_at=(
                datetime.fromisoformat(raw["expires_at"])
                if raw.get("expires_at") else None
            ),
        )

    def close(self) -> None:
        self.connection.close()


__all__ = ["ConsentError", "ConsentStore", "SQLiteConsentStore"]
