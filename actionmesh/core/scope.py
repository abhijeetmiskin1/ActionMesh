"""Which execution a provider call belongs to, while it is running.

A provider is handed an ``Action`` and nothing else, which is correct: an action is the
work, and an execution id is bookkeeping about who asked. But C35 needs the bookkeeping
on the *other* side of that call. A laptop agent that launches a child program has to
record which execution that child belongs to, because the HTTP request that ordered it is
gone by the time anybody asks "is that still running?" — and the answer cannot come from
the request, the SSE connection, or whichever task map happens to be in memory.

So the engine installs the id around the call, the way it installs the output publisher
(``actionmesh.core.output``), and a provider that starts real work reads it and stamps it
on what it started. Same reasoning, same lifetime, deliberately the same mechanism:

* it is not a field on ``Action``. An action is built from a request body; an execution id
  is minted by the server after validation. A caller that could name its own execution
  could address someone else's.
* it is scoped to the call and reset afterwards, so a provider that keeps state between
  calls cannot carry one run's identity into another's.
* it is ``None`` outside an execution. A provider started by anything other than the
  engine records no identity, and a missing identity is reported as missing — never
  guessed at.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_CURRENT: ContextVar[str | None] = ContextVar("actionmesh_execution", default=None)


def install_execution(execution_id: str) -> Token[str | None]:
    """Bind the calling task to one execution, and return the token that unbinds it."""
    return _CURRENT.set(execution_id)


def release_execution(token: Token[str | None]) -> None:
    _CURRENT.reset(token)


def current_execution_id() -> str | None:
    """The execution the calling provider call belongs to, if any."""
    return _CURRENT.get()


__all__ = ["current_execution_id", "install_execution", "release_execution"]
