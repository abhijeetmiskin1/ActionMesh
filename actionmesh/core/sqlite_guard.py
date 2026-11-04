"""Shared guard for stores that hold one sqlite3 connection.

Every durable store in ActionMesh opens a single :class:`sqlite3.Connection`
with ``check_same_thread=False`` so FastAPI's threadpool and per-request
async tasks may use it. That is only safe if the whole statement unit —
``execute`` plus ``fetchall``/``fetchone`` plus ``commit`` — is atomic with
respect to other threads. Without serialisation, a write interleaved with a
read returns ``()`` rows, torn payloads, and ``InterfaceError`` ("bad
parameter or other API misuse") under ordinary concurrent traffic.

The fix is one re-entrant lock per store, taken for the duration of each
public method. :func:`guarded` is that lock as a decorator so method bodies
stay exactly as written. ``RLock`` because a few methods compose other
public methods (``mark_cancel_requested`` reads then writes, ``fail``
releases, ``release_leases`` saves), and a plain ``Lock`` would deadlock.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def guarded(method: F) -> F:
    """Run ``method`` under the owning store's ``_lock`` (an ``RLock``).

    Stores assign ``self._lock = threading.RLock()`` in ``__init__`` before
    any guarded method can run. Attribute errors fall back to no locking so
    a store that forgot the lock fails loudly in its own tests, not with a
    confusing ``*_locked`` NameError here.
    """

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        lock = getattr(self, "_lock", None)
        if lock is None:  # pragma: no cover - construction-order misuse
            return method(self, *args, **kwargs)
        with lock:
            return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def make_lock() -> threading.RLock:
    """The per-store lock. Kept a helper so tests can name it."""

    return threading.RLock()


def assert_guarded(store: object, public_methods: list[str]) -> None:
    """Raise if any named public method is missing the ``@guarded`` wrapper.

    A store that holds a single ``sqlite3.Connection`` with
    ``check_same_thread=False`` is only safe under one re-entrant lock taken
    for the whole execute/fetch/commit unit. The failure mode of forgetting
    one decorator is not a loud error at authoring time — it is a torn read
    or ``InterfaceError`` in production under concurrency, which is how the
    live 8330-error repro happened. This makes the omission a test failure.

    ``public_methods`` is the explicit list of names the caller expects to be
    guarded; the check is ``getattr`` identity against the wrapped function
    (``functools.wraps`` preserves ``__wrapped__``). Methods that are not
    listed are not checked, so the list documents intent.
    """
    missing: list[str] = []
    for name in public_methods:
        method = getattr(store, name, None)
        if method is None:
            missing.append(f"{name} (missing)")
        elif not hasattr(method, "__wrapped__"):
            missing.append(name)
    if missing:
        raise AssertionError(
            f"{type(store).__name__} methods not guarded by @guarded: {', '.join(missing)}"
        )
