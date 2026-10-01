"""C15 durable consent and idempotency tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from actionmesh.core.executor import SQLiteIdempotencyLedger
from actionmesh.core.permissions import Consent, SQLiteConsentStore
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


if __name__ == "__main__":
    unittest.main()
