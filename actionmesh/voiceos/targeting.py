"""Which computer did the user mean? — answered by matching, never by guessing.

This module is the device-resolution leg of C36's routing pipeline. It turns the
words a person said ("the linux workstation", "my Mac", "stop the server on the
MacBook") into the *identifier* of one registered computer. It makes no
authorization decision and it never runs anything: the executor re-resolves the id
through :class:`~actionmesh.core.devices.DeviceRegistry`, and refuses an unknown,
offline, revoked or out-of-role target regardless of what this module returned.

Two rules shape the matching:

* **Only what the registry records is a name.** The vocabulary searched is the
  device's own id, name, platform, role, OS version and architecture. There is no
  hidden synonym table, because a synonym table is a list of assumptions about
  what the user calls their machines, and a wrong assumption here runs a command
  on the wrong computer.
* **A tie is a question, not a coin toss.** Two devices can score the same — two
  laptops both called "MacBook", or "server" matching two names. The caller gets
  the candidates and asks; it does not get the first one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from actionmesh.core.devices import EXECUTION_KINDS, DeviceRegistry
from actionmesh.protocol.models import DeviceInfo

#: Words that carry the sentence, not the machine. Removed before a span is scored,
#: so "run the tests on my linux workstation" is searched for "linux workstation"
#: rather than for "on" and "my".
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "at", "for", "from", "in", "into", "is", "it", "my", "of",
        "on", "our", "please", "the", "then", "this", "that", "to", "with", "you",
        "your", "can", "could", "would", "will", "should", "me", "i", "we",
    }
)

#: The longest device name worth matching as one phrase. Four words covers
#: "david's old windows laptop"; longer spans only add cost.
_MAX_SPAN = 4

_SPLIT = re.compile(r"[^0-9a-z]+")

#: Words that describe a *kind* of machine rather than identify one. They are
#: legitimate in an explicit "switch to the linux one" and never a name on their
#: own in a plain request: "show my linux processes" says what to look at, not
#: which computer to look at it on.
_GENERIC = frozenset(
    {
        "laptop", "desktop", "server", "machine", "computer", "workstation",
        "linux", "windows", "macos", "mac", "android", "phone", "win",
    }
)


def words(text: str) -> list[str]:
    return [token for token in _SPLIT.split(text.lower()) if token]


def _name_words(device: DeviceInfo) -> list[str]:
    return words(device.name)


def _searchable(device: DeviceInfo) -> set[str]:
    """Everything the registry records that a person might say out loud.

    The device id is included so an operator debugging a deployment can type it,
    and the platform/role are included because "the linux one" and "the mac" are
    how people actually refer to their machines.
    """
    return set(
        words(
            " ".join(
                (
                    device.device_id,
                    device.name,
                    device.platform,
                    device.kind.value,
                    device.os_version,
                    device.arch,
                )
            )
        )
    )


def _name_score(device: DeviceInfo, span: tuple[str, ...]) -> int:
    """How well ``span`` names ``device`` **by its own name or id**. 0 if it does not.

    These are the tiers that mean "you said the computer's name". They are kept apart
    from the weaker "some word appears somewhere in the record" tier because the two
    are used in different situations: an explicit ``switch to`` can lean on the
    platform ("the linux one"), while a device name merely appearing *inside* an
    ordinary request must be a real name -- otherwise "show my linux processes" would
    silently become "run that on the Linux workstation".
    """
    if not span:
        return 0
    text = " ".join(span)
    # An id is often punctuated ("dev_mac", "workstation-2") and `words` splits on
    # punctuation, so compare the space-joined *and* both punctuated readings. Without
    # this, typing an id that the registry itself minted matched only through the weak
    # "a word appears somewhere" tier -- and tied with an unrelated device.
    slug = "_".join(span)
    if device.device_id.lower() in {text, slug, "-".join(span)}:
        return 100
    name_words = _name_words(device)
    name = " ".join(name_words)
    if name and text == name:
        return 95
    if name and text == "".join(name_words):
        return 92
    # "mac" -> "MacBook": a prefix claims the word it abbreviates. A *generic* word
    # is exempt -- "linux" is not short for "Linux Workstation", it is a description,
    # and only an explicit selection asks for one by kind.
    if name and name.startswith(text + " ") and text not in _GENERIC:
        return 85
    if len(span) > 1 and set(span) <= set(name_words):
        # Several words that all belong to one name, in any order: "workstation
        # linux" for a device called "Linux Workstation". A single word is
        # deliberately excluded -- one common word ("laptop") is not a name.
        return 80
    return 0


def _score(device: DeviceInfo, span: tuple[str, ...]) -> int:
    """How well ``span`` names ``device``. Higher is more specific; 0 is no match.

    The tiers are ordered so a more exact statement always beats a looser one:
    an id beats a full name, which beats a prefix ("mac" -> "MacBook"), which
    beats every word being somewhere in the record ("windows desktop"). A span
    is deliberately never a *suffix* match: "development" must not name a device
    called "dev".
    """
    named = _name_score(device, span)
    if named or not span:
        return named
    if all(token in _searchable(device) for token in span):
        return 70
    return 0


def _spans(text: str) -> list[tuple[str, ...]]:
    """Contiguous runs of non-stopword tokens, up to ``_MAX_SPAN`` long."""
    tokens = words(text)
    out: list[tuple[str, ...]] = []
    for start in range(len(tokens)):
        token = tokens[start]
        if token in _STOPWORDS or len(token) < 2:
            continue
        out.append((token,))
        for end in range(start + 1, min(start + _MAX_SPAN, len(tokens))):
            if tokens[end] in _STOPWORDS:
                break
            out.append(tuple(tokens[start : end + 1]))
    return out


@dataclass(frozen=True)
class TargetChoice:
    """The computer the request names — or the reason none could be chosen."""

    device_id: str = ""
    name: str = ""
    online: bool = False
    #: ``(device_id, label)`` for every device that matched as well as the winner.
    #: Non-empty only when the choice is ambiguous; the caller asks.
    candidates: tuple[tuple[str, str], ...] = ()
    #: Why nothing was chosen, in a sentence a person can act on. Empty on success.
    reason: str = ""
    #: True when the text *did* name at least one registered device, even if the
    #: choice could not be completed (a tie, or a phone). This is what lets a caller
    #: tell "you named a computer" from "you named none", which are different
    #: answers: one asks or explains, the other carries on as ordinary work.
    named: bool = False

    @property
    def resolved(self) -> bool:
        return bool(self.device_id)

    @property
    def ambiguous(self) -> bool:
        return bool(self.candidates)


def label(device: DeviceInfo) -> str:
    return (device.name or device.device_id).strip() or device.device_id


def executable(registry: DeviceRegistry) -> list[DeviceInfo]:
    """Every device that could legitimately run an action, online or not.

    Offline devices stay in: naming one and then reporting that it is offline is
    the honest answer, and hiding it would turn "your Mac is asleep" into "there
    is no Mac".
    """
    return [device for device in registry.all_devices() if device.kind in EXECUTION_KINDS]


def resolve(
    registry: DeviceRegistry, text: str, *, phrases: bool = False
) -> TargetChoice:
    """Find the one device ``text`` names, or explain why there isn't one.

    Matching runs over every registered device (including phones) so that naming
    a phone gets the truthful answer — "that is a control surface" — instead of
    "no such computer".

    ``phrases=True`` additionally lets the platform and role fields name a device:
    "switch to the linux one" is an explicit request to pick a machine, and refusing
    it would be pedantry. Plain requests do not get that tier, because "show my
    linux processes" is an observation whose *own* parameters say where to look --
    turning a descriptive word into a target would run it on a machine the user
    never mentioned. Same function, two vocabularies, and the caller states which it
    is asking in.
    """
    spans = _spans(text)
    if not spans:
        return TargetChoice(reason="that request does not name a computer")
    best = 0
    winners: list[DeviceInfo] = []
    for device in registry.all_devices():
        score = max(
            (
                _name_score(device, span)
                or (_score(device, span) if phrases else 0)
                for span in spans
            ),
            default=0,
        )
        if score > best:
            best, winners = score, [device]
        elif score and score == best:
            winners.append(device)
    if not winners:
        return TargetChoice(reason="no registered computer is named in that request")
    if len(winners) > 1:
        return TargetChoice(
            candidates=tuple((device.device_id, label(device)) for device in winners),
            reason="more than one registered computer matches that name",
            named=True,
        )
    chosen = winners[0]
    if chosen.kind not in EXECUTION_KINDS:
        return TargetChoice(
            reason=(
                f"{label(chosen)} is a {chosen.kind.value} — a control surface. "
                "It cannot run actions itself."
            ),
            named=True,
        )
    return TargetChoice(
        device_id=chosen.device_id,
        name=label(chosen),
        online=registry.is_online(chosen),
        named=True,
    )


def current(registry: DeviceRegistry, device_id: str) -> TargetChoice:
    """Validate an explicitly selected target, without ever substituting another.

    A selection that has been revoked or forgotten resolves to nothing with a
    reason; it does not become "the first device", because that is precisely the
    silent re-targeting the work order forbids.
    """
    wanted = (device_id or "").strip()
    if not wanted:
        return TargetChoice()
    device = registry.devices.get(wanted)
    if device is None:
        return TargetChoice(reason=f"the selected computer {wanted!r} is no longer registered")
    if device.kind not in EXECUTION_KINDS:
        return TargetChoice(
            reason=(
                f"{label(device)} is a {device.kind.value} and cannot be an execution "
                "target; choose a laptop or server"
            )
        )
    return TargetChoice(
        device_id=device.device_id, name=label(device), online=registry.is_online(device)
    )


def default_target(registry: DeviceRegistry, local_device_id: str = "") -> TargetChoice:
    """The target a conversation starts with — never a guess between machines.

    Precedence is deliberate:

    1. the machine this process *serves* (the declared local device), because that
       is the only id whose reachability is not in question;
    2. the single **online** execution device, when there is exactly one;
    3. nothing.

    An offline device is never chosen by default: a registry restored after a
    restart briefly has every device offline, and defaulting to one would make
    every first request refuse for a reason the user never chose. Several online
    devices yields their names, so the caller asks instead of picking.
    """
    local = (local_device_id or "").strip()
    if local and local in registry.devices:
        device = registry.devices[local]
        if device.kind in EXECUTION_KINDS:
            return TargetChoice(
                device_id=device.device_id, name=label(device), online=registry.is_online(device)
            )
    online = [
        device
        for device in executable(registry)
        if registry.is_online(device)
    ]
    if len(online) == 1:
        device = online[0]
        return TargetChoice(device_id=device.device_id, name=label(device), online=True)
    if len(online) > 1:
        return TargetChoice(
            candidates=tuple((device.device_id, label(device)) for device in online),
            reason="several computers are online",
        )
    registered = executable(registry)
    if registered:
        return TargetChoice(
            reason="no registered computer is online; the registered ones are "
            + ", ".join(f"{label(device)} ({device.device_id})" for device in registered)
        )
    return TargetChoice(reason="no computer is registered as an execution target")


def describe_choice(choice: TargetChoice, *, registry: DeviceRegistry | None = None) -> str:
    """One sentence naming the target, including its state when that matters."""
    if choice.resolved:
        state = "online" if choice.online else "offline"
        return f"{choice.name} ({choice.device_id}) — {state}"
    if choice.candidates:
        names = ", ".join(name for _, name in choice.candidates)
        return f"Which computer did you mean: {names}?"
    return choice.reason or "no computer was named"


__all__ = [
    "TargetChoice",
    "current",
    "default_target",
    "describe_choice",
    "executable",
    "label",
    "resolve",
    "words",
]