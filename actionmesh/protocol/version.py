"""AMP protocol version negotiation.

Protocol changes are versioned so that a future incompatible revision can be
introduced without silently misinterpreting today's messages.
"""

from __future__ import annotations

import re

PROTOCOL_VERSION = "1.0"
SUPPORTED_PROTOCOLS = ("1.0",)

_PATTERN = re.compile(r"^(\d+)\.(\d+)$")


class VersionError(ValueError):
    """Raised for a malformed or unsupported protocol version."""


def parse_version(value: str) -> tuple[int, int]:
    match = _PATTERN.match(str(value).strip())
    if not match:
        raise VersionError(f"malformed protocol version: {value!r}")
    return int(match.group(1)), int(match.group(2))


def is_supported(value: str) -> bool:
    try:
        parse_version(value)
    except VersionError:
        return False
    return str(value).strip() in SUPPORTED_PROTOCOLS


def negotiate(requested: str | None) -> str:
    """Return the protocol version to speak, or raise if none is compatible.

    An absent request means "whatever this build speaks". An explicit request
    that this build cannot satisfy is an error — never a silent downgrade.
    """
    if requested is None or not str(requested).strip():
        return PROTOCOL_VERSION
    if not is_supported(requested):
        raise VersionError(
            f"unsupported protocol {requested!r}; this build speaks {PROTOCOL_VERSION}"
        )
    return str(requested).strip()


def version_header() -> str:
    return f"AMP/{PROTOCOL_VERSION}"


__all__ = [
    "PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOLS",
    "VersionError",
    "is_supported",
    "negotiate",
    "parse_version",
    "version_header",
]
