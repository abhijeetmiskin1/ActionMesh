"""C15 durable consent and idempotency tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actionmesh.core.executor import SQLiteIdempotencyLedger
from actionmesh.core.permissions import Consent, ConsentStore, SQLiteConsentStore
from actionmesh.protocol.models import RiskLevel


class TestDurableIdempotency(unittest.TestCase):
    def test_done_key_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "mesh.db")
            first = SQLiteIdempotencyLedger(path)
            self.assertEqual(first.begin("fixed"), (False, None))
            first.complete("fixed", {"receipt_id": "r1"})
            first.close()
            second = SQLiteIdempotencyLedger(path)
            self.assertEqual(second.begin("fixed"), (True, {"receipt_id": "r1"}))
            second.close()


class TestDurableConsent(unittest.TestCase):
    def test_consent_survives_reopen_and_rejects_wildcards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "consent.db"
            first = SQLiteConsentStore(path)
            first.grant(Consent(scope="message.send", max_risk=RiskLevel.HIGH))
            with self.assertRaises(ValueError):
                first.grant(Consent(scope="*"))
            first.close()
            second = SQLiteConsentStore(path)
            self.assertEqual([grant.scope for grant in second.grants()], ["message.send"])
            second.close()

    def test_regrant_replaces_an_actors_row_instead_of_stacking(self) -> None:
        # The desktop grants a scope, then grants it again with a wider risk ceiling.
        # The ledger must hold one decision, not two: two rows would be two grants,
        # and the older, narrower one would silently outlive the decision to widen it.
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteConsentStore(Path(directory) / "consent.db")
            store.grant(Consent(scope="device.server.read", max_risk=RiskLevel.LOW))
            store.grant(Consent(scope="device.server.read", max_risk=RiskLevel.HIGH))
            rows = store.consents("device.server.read")
            self.assertEqual(len(rows), 1)
            self.assertIs(rows[0].max_risk, RiskLevel.HIGH)
            store.close()

    def test_regrant_never_touches_another_actors_row_for_the_same_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteConsentStore(Path(directory) / "consent.db")
            store.grant(Consent(scope="message.send", granted_by="alice"))
            store.grant(Consent(scope="message.send", granted_by="bob"))
            store.grant(Consent(scope="message.send", granted_by="alice"))
            holders = sorted(c.granted_by for c in store.consents("message.send"))
            self.assertEqual(holders, ["alice", "bob"])
            store.close()

    def test_revoke_named_actor_leaves_the_other_actors_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "consent.db"
            first = SQLiteConsentStore(path)
            first.grant(Consent(scope="message.send", granted_by="alice"))
            first.grant(Consent(scope="message.send", granted_by="bob"))
            self.assertEqual(first.revoke("message.send", granted_by="alice"), 1)
            self.assertEqual(first.revoke("message.send", granted_by="alice"), 0)
            first.close()
            # The surviving grant is durable, not just visible in the open handle.
            second = SQLiteConsentStore(path)
            self.assertEqual(
                [c.granted_by for c in second.consents("message.send")], ["bob"]
            )
            second.close()

    def test_revoke_without_an_actor_clears_the_whole_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteConsentStore(Path(directory) / "consent.db")
            store.grant(Consent(scope="message.send", granted_by="alice"))
            store.grant(Consent(scope="message.send", granted_by="bob"))
            self.assertEqual(store.revoke("message.send"), 2)
            self.assertEqual(store.consents("message.send"), [])
            store.close()


class TestConsentStoreLocation(unittest.TestCase):
    """Where the API process keeps consents, and what it does when it cannot."""

    def test_consents_are_durable_beside_the_voiceos_database(self) -> None:
        from actionmesh.api import _build_consent_store

        with tempfile.TemporaryDirectory() as directory:
            voiceos_db = str(Path(directory) / "voiceos.db")
            with patch.dict(os.environ, {"ACTIONMESH_VOICEOS_DB": voiceos_db}, clear=True):
                store = _build_consent_store()
            self.assertIsInstance(store, SQLiteConsentStore)
            store.grant(Consent(scope="device.server.read"))
            store.close()
            self.assertTrue((Path(directory) / "consents.db").exists())

    def test_audit_database_location_places_consents_beside_it(self) -> None:
        from actionmesh.api import _build_consent_store

        with tempfile.TemporaryDirectory() as directory:
            audit_db = str(Path(directory) / "audit.db")
            with patch.dict(os.environ, {"ACTIONMESH_AUDIT_DB": audit_db}, clear=True):
                store = _build_consent_store()
            self.assertIsInstance(store, SQLiteConsentStore)
            store.close()
            self.assertTrue((Path(directory) / "consents.db").exists())

    def test_no_configured_store_stays_in_memory(self) -> None:
        from actionmesh.api import _build_consent_store

        with patch.dict(os.environ, {}, clear=True):
            store = _build_consent_store()
        self.assertIs(type(store), ConsentStore)

    def test_unwritable_location_falls_back_instead_of_failing_to_start(self) -> None:
        from actionmesh.api import _build_consent_store

        with tempfile.TemporaryDirectory() as directory:
            # A regular file where the parent directory should be: sqlite cannot
            # open a database underneath it.
            blocker = Path(directory) / "blocker"
            blocker.write_text("not a directory")
            voiceos_db = str(blocker / "voiceos.db")
            with patch.dict(os.environ, {"ACTIONMESH_VOICEOS_DB": voiceos_db}, clear=True):
                store = _build_consent_store()
        self.assertIs(type(store), ConsentStore)


if __name__ == "__main__":
    unittest.main()
