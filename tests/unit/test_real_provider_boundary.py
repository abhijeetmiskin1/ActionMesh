"""C13 official-provider boundary tests."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from actionmesh.protocol.models import FailureKind
from actionmesh.providers import ProviderError
from actionmesh.providers.real_base import IntegrationConfig


class TestIntegrationConfig(unittest.TestCase):
    def test_missing_credential_fails_as_authentication(self) -> None:
        config = IntegrationConfig("vendor", "https://api.vendor.example", "VENDOR_TOKEN")
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(ProviderError) as raised,
        ):
            config.credential()
        self.assertIs(raised.exception.kind, FailureKind.AUTHENTICATION)

    def test_http_and_wildcard_scope_are_rejected(self) -> None:
        with self.assertRaises(ProviderError):
            IntegrationConfig("vendor", "http://api.vendor.example", "TOKEN").validate()
        with self.assertRaises(ProviderError):
            IntegrationConfig(
                "vendor", "https://api.vendor.example", "TOKEN", scopes=("*",)
            ).validate()

    def test_configuration_never_contains_the_secret_value(self) -> None:
        config = IntegrationConfig("vendor", "https://api.vendor.example", "TOKEN")
        self.assertNotIn("secret", repr(config).lower())


if __name__ == "__main__":
    unittest.main()