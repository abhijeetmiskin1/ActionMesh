"""Application configuration and deployment modes.

ActionMesh runs in three deliberately different ways, and the whole point of making
them explicit is that the unsafe one is never chosen by accident. ``development`` is
the today-behaviour: in-memory stores, an ephemeral signing key, no API key -- fine on
a laptop, wrong everywhere else. ``lan`` is that plus a required shared key, for a
phone talking to a laptop across a room over cleartext that must not be mistaken for a
secure deployment. ``production`` additionally refuses to start unless the state that
is supposed to survive a restart actually will: durable execution storage, a durable
audit chain, a durable signing key, and -- if it binds somewhere reachable -- a named
set of trusted proxies that terminate TLS.

The rule this module exists to enforce is the one a README cannot: **a production
deployment must not silently fall back to a development default.** Every gap is a
startup error that lists what is missing, not a server that comes up quietly insecure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

ENV_MODE = "ACTIONMESH_ENV"
ENV_HOST = "ACTIONMESH_HOST"
ENV_PORT = "ACTIONMESH_PORT"
ENV_PUBLIC_BASE_URL = "ACTIONMESH_PUBLIC_BASE_URL"
ENV_API_KEY = "ACTIONMESH_API_KEY"
ENV_MAX_REQUEST_BYTES = "ACTIONMESH_MAX_REQUEST_BYTES"
ENV_AUDIT_DB = "ACTIONMESH_AUDIT_DB"
ENV_AUDIT_ANCHOR = "ACTIONMESH_AUDIT_ANCHOR"
ENV_DATABASE_URL = "ACTIONMESH_DATABASE_URL"
ENV_ALLOW_REMOTE_PAIRING = "ACTIONMESH_ALLOW_REMOTE_PAIRING"
ENV_TRUSTED_PROXIES = "ACTIONMESH_TRUSTED_PROXIES"
ENV_SECRET_KEY = "ACTIONMESH_SECRET_KEY"
ENV_SECRET_FILE = "ACTIONMESH_SECRET_FILE"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_MAX_REQUEST_BYTES = 1_048_576
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class ConfigError(ValueError):
    """Configuration is missing, contradictory, or insecure for the chosen mode."""


class DeploymentMode(StrEnum):
    DEVELOPMENT = "development"
    LAN = "lan"
    PRODUCTION = "production"


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _sqlite_path(database_url: str) -> str:
    """The file behind a ``sqlite://`` URL, or the bare path if given as one.

    ``sqlite:///rel.db`` is relative, ``sqlite:////var/x.db`` is absolute -- the
    scheme is ``sqlite://`` plus a path that itself starts with ``/``. Stripping the
    ten-character ``sqlite:///`` leaves the path exactly as a caller meant it.
    """
    url = database_url.strip()
    if not url:
        return ""
    if url.startswith("sqlite:///"):
        url = url[len("sqlite:///") :]
    elif url.startswith("sqlite://"):
        url = url[len("sqlite://") :]
    if url == "" or url == ":memory:":
        return ":memory:"
    return url


@dataclass(frozen=True)
class AppConfig:
    """Validated application configuration for one process."""

    mode: DeploymentMode
    host: str
    port: int
    public_base_url: str
    api_key: str
    max_request_bytes: int
    audit_db: str
    audit_anchor: str
    database_url: str
    allow_remote_pairing: bool
    trusted_proxies: tuple[str, ...]
    #: Whether a durable signing key is configured. Computed from the same env the
    #: rest of the config reads, so `validate` is a pure function of this record.
    secret_durable: bool = False

    @property
    def durable_execution(self) -> bool:
        path = _sqlite_path(self.database_url)
        return bool(path) and path != ":memory:"

    @property
    def execution_db_path(self) -> str:
        """The SQLite file for durable execution records, or ``''`` if in-memory."""
        path = _sqlite_path(self.database_url)
        return path if path and path != ":memory:" else ""

    @property
    def durable_audit(self) -> bool:
        return bool(self.audit_db.strip())

    @property
    def binds_publicly(self) -> bool:
        return self.host not in _LOOPBACK

    def validate(self) -> None:
        """Raise :class:`ConfigError` listing every way this config is unsafe."""
        problems: list[str] = []

        if self.port < 1 or self.port > 65535:
            problems.append(f"port {self.port} is not in 1..65535")
        if self.max_request_bytes < 1024:
            problems.append("max request bytes must be at least 1024")
        if self.mode is DeploymentMode.PRODUCTION and not self.public_base_url.strip():
            problems.append("production requires ACTIONMESH_PUBLIC_BASE_URL")

        # A shared key is the minimum for any non-loopback reach; without it the
        # first person on the network is the operator.
        if self.mode in (DeploymentMode.LAN, DeploymentMode.PRODUCTION) and not self.api_key:
            problems.append(f"{self.mode.value} mode requires ACTIONMESH_API_KEY")

        if self.mode is DeploymentMode.PRODUCTION:
            if not self.durable_execution:
                problems.append(
                    "production requires durable execution storage "
                    "(ACTIONMESH_DATABASE_URL pointing at a SQLite file)"
                )
            if not self.durable_audit:
                problems.append(
                    "production requires a durable audit chain (ACTIONMESH_AUDIT_DB)"
                )
            if not self.secret_durable:
                problems.append(
                    "production requires a durable signing key "
                    f"({ENV_SECRET_KEY} or {ENV_SECRET_FILE}); an ephemeral key "
                    "unpairs every device on restart"
                )
            if self.binds_publicly and not self.trusted_proxies:
                problems.append(
                    f"binding to {self.host!r} in production without "
                    f"{ENV_TRUSTED_PROXIES} trusts a forgeable client address; "
                    "name the proxies that terminate TLS"
                )

        if problems:
            raise ConfigError(
                f"{self.mode.value} configuration is invalid:\n- " + "\n- ".join(problems)
            )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AppConfig:
        source = os.environ if env is None else env
        raw_mode = source.get(ENV_MODE, "development").strip().lower() or "development"
        try:
            mode = DeploymentMode(raw_mode)
        except ValueError as exc:
            raise ConfigError(
                f"{ENV_MODE}={raw_mode!r} is not one of "
                f"{', '.join(m.value for m in DeploymentMode)}"
            ) from exc
        try:
            port = int(source.get(ENV_PORT, str(DEFAULT_PORT)))
        except ValueError as exc:
            raise ConfigError(f"{ENV_PORT} must be an integer") from exc
        try:
            max_bytes = int(
                source.get(ENV_MAX_REQUEST_BYTES, str(DEFAULT_MAX_REQUEST_BYTES))
            )
        except ValueError as exc:
            raise ConfigError(f"{ENV_MAX_REQUEST_BYTES} must be an integer") from exc
        proxies = tuple(
            part.strip()
            for part in source.get(ENV_TRUSTED_PROXIES, "").split(",")
            if part.strip()
        )
        config = cls(
            mode=mode,
            host=source.get(ENV_HOST, DEFAULT_HOST).strip() or DEFAULT_HOST,
            port=port,
            public_base_url=source.get(ENV_PUBLIC_BASE_URL, "").strip(),
            api_key=source.get(ENV_API_KEY, ""),
            max_request_bytes=max_bytes,
            audit_db=source.get(ENV_AUDIT_DB, "").strip(),
            audit_anchor=source.get(ENV_AUDIT_ANCHOR, "").strip(),
            database_url=source.get(ENV_DATABASE_URL, "").strip(),
            allow_remote_pairing=_truthy(source.get(ENV_ALLOW_REMOTE_PAIRING, "")),
            trusted_proxies=proxies,
            secret_durable=bool(source.get(ENV_SECRET_KEY, "").strip())
            or bool(source.get(ENV_SECRET_FILE, "").strip()),
        )
        config.validate()
        return config


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ENV_MODE",
    "ENV_TRUSTED_PROXIES",
    "AppConfig",
    "ConfigError",
    "DeploymentMode",
]
