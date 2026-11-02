"""Errors the laptop agent raises, kept apart so they can be quoted safely.

Agent failures reach a phone: an approval prompt, an execution record, a dashboard
card. A message that quotes the absolute path it refused is a map of the machine
handed to whoever holds the token — and "the token" includes the phone that was left
on a train. So these exceptions carry the *caller's own spelling* of what they
objected to, never a resolved system path.
"""

from __future__ import annotations


class AgentError(ValueError):
    """Base for agent refusals.

    A ``ValueError`` because an agent refusal is an invalid request, not an
    infrastructure fault: the executor's retry rules must not treat it as transient,
    and the API's 400 boundary already understands ``ValueError``.
    """


class PathEscape(AgentError):
    """A path was refused: it leaves the root, names a secret, or cannot be checked."""


class PathNotFound(PathEscape):
    """The path is confined and legitimate, it simply is not there.

    A separate type because the two answers mean different things to every layer
    above: an escape is a denial worth auditing, a missing file is a typo the caller
    can fix. Reporting both as "authorization failure" would let a wrong filename
    look like an attack, and hiding an attack inside a wrong filename is worse.
    """


class CommandDenied(AgentError):
    """A program or argument combination the command policy does not allow."""


class ProcessError(AgentError):
    """A process or server could not be started, found, or stopped."""


__all__ = [
    "AgentError",
    "CommandDenied",
    "PathEscape",
    "PathNotFound",
    "ProcessError",
]
