"""The server's own key material.

Pairing codes and credentials are stored as **keyed** digests. That key is what
makes a copied database useless: without it, an attacker holding the store can
compute the digest of every guess and read back a live 6-digit code. So the key
needs three properties that pull against each other — it must survive a restart (or
a phone re-pairs every time the laptop reboots), it must never appear in a log, and
it must not be committed to a repository.

Resolution order is explicit:

1. ``ACTIONMESH_SECRET_KEY`` — deployment-supplied, preferred, nothing written.
2. A file at ``ACTIONMESH_SECRET_FILE``, created on first use with mode ``0600``.
3. An **ephemeral** random key. Nothing is written, and pairing does not survive the
   process — which is the direction that fails safe: a restarted server has fewer
   live credentials than it had, never more.

The ephemeral default exists because importing this package must not create files:
``actionmesh.api`` builds a default app at import time, and a module-level
``create_app()`` that wrote a key to disk would silently turn an import into a
side effect. Durability is one environment variable away, and ``docs/deployment.md``
says so.

A generated key is random, not derived from the hostname, a machine id, or the
current time, so two fresh installs cannot produce the same value. A file that
already exists but is group- or world-readable is **refused** rather than silently
used: on a shared machine, a readable key file is the same as no key.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass

ENV_KEY = "ACTIONMESH_SECRET_KEY"
ENV_FILE = "ACTIONMESH_SECRET_FILE"

#: 32 bytes, so the value can be used as an HMAC key directly and hashed into a
#: fixed-width form without padding.
KEY_BYTES = 32


class SecretError(RuntimeError):
    """Raised when key material is unusable. Fails closed: no implicit fallback."""


@dataclass(frozen=True)
class ServerKey:
    """Key material plus how it was obtained, so the server can be honest about it.

    ``durable`` is the part a deployment needs to see: with an ephemeral key a
    restart unpairs every device. Reported rather than assumed, because "my phone
    stopped working after I rebooted" is otherwise a mystery to debug.
    """

    material: bytes
    source: str
    durable: bool

    def __repr__(self) -> str:  # never print the key
        return f"ServerKey(source={self.source!r}, durable={self.durable})"


def _from_env_value(raw: str) -> bytes:
    """Derive a stable key from a passphrase.

    Hashed rather than used verbatim so the key length does not depend on how
    someone typed it, and so the raw value is never held in memory longer than the
    digest.
    """
    if not raw.strip():
        raise SecretError(f"{ENV_KEY} is set but empty; unset it or give it a value")
    return hashlib.sha256(raw.encode("utf-8")).digest()


def load_key(path: str | os.PathLike[str] | None = None) -> ServerKey:
    """Return the server key: from the environment, from a private file, or fresh."""
    configured = os.environ.get(ENV_KEY)
    if configured is not None:
        # Set-but-blank is a mistake, not an opt-out. Falling through to an
        # ephemeral key here would let a deployment believe it had a durable key
        # while every restart quietly unpaired its devices.
        if not configured.strip():
            raise SecretError(f"{ENV_KEY} is set but empty; unset it or give it a value")
        return ServerKey(_from_env_value(configured), ENV_KEY, durable=True)

    explicit = path if path is not None else os.environ.get(ENV_FILE, "").strip()
    if not explicit:
        # Ephemeral. Nothing touches the disk, and nothing survives the process.
        return ServerKey(secrets.token_bytes(KEY_BYTES), "ephemeral", durable=False)

    target = str(explicit)
    try:
        with open(target, "rb") as handle:
            stored = handle.read()
    except FileNotFoundError:
        stored = b""
    except OSError as exc:
        raise SecretError(f"cannot read {target}: {exc}") from exc

    if stored:
        _refuse_shared_permissions(target)
        if len(stored) < KEY_BYTES:
            raise SecretError(
                f"{target} is shorter than {KEY_BYTES} bytes; a truncated key is a "
                "weaker key, and regenerating it would silently unpair every device"
            )
        # Read as bytes, undelimited. A key is random, so its first or last byte may be
        # one the ASCII whitespace set would match, and an earlier `.strip()` here threw
        # that byte away: the file then measured 31 bytes, was refused as truncated, and
        # the server could not start -- on about one fresh installation in twenty. A
        # hand-written file's trailing newline costs nothing either way, because the key
        # is the first KEY_BYTES bytes whether or not a newline follows them.
        return ServerKey(stored[:KEY_BYTES], target, durable=True)

    # Fresh key. Create the parent directory first with the tightest mode the
    # filesystem allows, then write atomically so a crash cannot leave a half-written
    # key that reads back as a different one.
    directory = os.path.dirname(os.path.abspath(target))
    try:
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise SecretError(f"cannot create the key directory for {target}: {exc}") from exc
    material = secrets.token_bytes(KEY_BYTES)
    temporary = f"{target}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(material)
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except OSError as exc:
        raise SecretError(f"cannot write {target}: {exc}") from exc
    _refuse_shared_permissions(target)
    return ServerKey(material, target, durable=True)


def _refuse_shared_permissions(target: str | os.PathLike[str]) -> None:
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError as exc:
        raise SecretError(f"cannot check permissions on {target}: {exc}") from exc
    if mode & 0o077:
        raise SecretError(
            f"{target} is readable or writable by group or other (mode {mode:04o}); "
            "a shared key file protects nothing. chmod 600 it, or supply "
            f"{ENV_KEY}"
        )


__all__ = [
    "ENV_FILE",
    "ENV_KEY",
    "KEY_BYTES",
    "SecretError",
    "ServerKey",
    "load_key",
]
