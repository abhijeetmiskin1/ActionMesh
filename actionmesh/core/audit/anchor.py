"""External chain anchor — makes **truncation** detectable (C16).

Why this exists
---------------
The hash chain detects editing, deleting and reordering *within* the records it
still holds. It cannot, on its own, detect **truncating the tail**:

    DELETE FROM audit_records WHERE seq >= 4   -- drop the last records

The surviving prefix is internally consistent, so ``verify()`` returns ``ok``.
An attacker can then append a fresh record whose ``prev_hash`` matches the new
tail, and the whole chain verifies again. This was measured, not assumed — see
``tests/unit/test_audit_anchor.py``.

A chain cannot anchor itself, so something outside the store has to remember how
much history *should* exist. That is this module: a periodically-sealed
checkpoint of ``(record_count, head_hash)`` kept somewhere the audit store
cannot rewrite. On open, a mismatch is a hard error.

What this does and does not buy
-------------------------------
* Does: detect tail truncation, and detect a chain being rolled back to an
  earlier state, as long as the anchor itself is intact.
* Does not: protect an anchor stored next to the audit database. Anyone who can
  edit the store can edit a sibling file. A real deployment should point
  ``FileAuditAnchor`` at separate, append-only, or write-once storage (and
  ideally sign the checkpoint with a KMS-held key). The type exists so that
  swap is a constructor argument rather than a rewrite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # `log` imports this module, so only the type is needed here
    pass


class AnchorMismatchError(RuntimeError):
    """The chain does not match the external anchor.

    Deliberately a ``RuntimeError`` rather than a subclass of ``AuditError``:
    importing ``AuditError`` at runtime would be circular, because
    :mod:`actionmesh.core.audit.log` imports this module to reach
    :func:`verify_anchor`. Callers catch ``AnchorMismatchError`` directly, and
    it still sits beside the other audit failures conceptually — the records
    present may verify perfectly, and the *absence* of records is the finding.
    """


class AuditAnchor(Protocol):
    """Somewhere outside the audit store that remembers the expected head."""

    def seal(self, records: int, head: str) -> None:
        """Record that the chain had ``records`` entries ending at ``head``."""

    def check(self) -> tuple[int, str] | None:
        """Return the sealed ``(records, head)``, or ``None`` if never sealed."""


class FileAuditAnchor:
    """A checkpoint stored in a small JSON file.

    Deliberately separate from the audit database so the two can be given
    different storage. Standard library only, matching the rest of the package.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def seal(self, records: int, head: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"records": records, "head": head}, sort_keys=True),
            encoding="utf-8",
        )

    def check(self) -> tuple[int, str] | None:
        if not self.path.exists():
            return None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return int(raw["records"]), str(raw["head"])


def verify_anchor(
    anchor: AuditAnchor | None, records: int, head: str
) -> None:
    """Raise :class:`AnchorMismatchError` if the chain contradicts the anchor.

    A chain that is *longer* than the anchor is fine — the anchor is a periodic
    checkpoint, not a high-water mark — so only a chain that is shorter, or whose
    head at the anchored length differs, is a finding.
    """
    if anchor is None:
        return
    sealed = anchor.check()
    if sealed is None:
        return
    sealed_records, sealed_head = sealed
    if records < sealed_records:
        raise AnchorMismatchError(
            f"audit chain is shorter than its anchor: {records} records on disk "
            f"but the anchor sealed {sealed_records} — history was truncated"
        )
    if records == sealed_records and head != sealed_head:
        raise AnchorMismatchError(
            f"audit chain head {head!r} does not match the anchored head "
            f"{sealed_head!r} at {sealed_records} records — history was rewritten"
        )


__all__ = [
    "AnchorMismatchError",
    "AuditAnchor",
    "FileAuditAnchor",
    "verify_anchor",
]
