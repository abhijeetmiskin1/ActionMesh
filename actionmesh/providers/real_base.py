"""C13 boundary for official, credentialed provider integrations.

This module contains no vendor client and performs no network I/O. It defines
the configuration and safety contract that a future official adapter must use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from actionmesh.protocol.models import FailureKind
from actionmesh.providers.base import ActionProvider, ProviderError


@dataclass(frozen=True)
class IntegrationConfig:
    """Non-secret integration metadata; credentials remain in the environment."""

    provider_name: str
    base_url: str
    credential_env: str
    scopes: tuple[str, ...] = ()

    def validate(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ProviderError(
                "real integrations require an HTTPS base URL", FailureKind.PROVIDER
            )
        if not self.provider_name.strip() or not self.credential_env.strip():
            raise ProviderError(
                "provider_name and credential_env are required", FailureKind.INVALID_INPUT
            )
        if any(not scope.strip() or scope == "*" for scope in self.scopes):
            raise ProviderError(
                "integration scopes must be non-empty and concrete",
                FailureKind.INVALID_INPUT,
            )

    def credential(self) -> str:
        """Read the credential only when the adapter is about to use it."""
        self.validate()
        value = os.getenv(self.credential_env, "")
        if not value:
            raise ProviderError(
                f"missing credential environment variable {self.credential_env}",
                FailureKind.AUTHENTICATION,
            )
        return value


class RealProviderBase(ActionProvider):
    """Base for a future official provider; never silently simulates success."""

    simulated = False

    def __init__(self, config: IntegrationConfig) -> None:
        config.validate()
        self.config = config
        self.name = config.provider_name

    def require_credential(self) -> str:
        return self.config.credential()


__all__ = ["IntegrationConfig", "RealProviderBase"]