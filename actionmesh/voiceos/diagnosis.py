"""Evidence, then explanation — in that order, and never the other way round.

The request behind this module is the product's first vertical slice: *"check my
backend, find the latest error and explain how to fix it."* The hard part is not the
sentence. It is the honesty of the middle: a control plane that "found the latest
error" must be able to show which line it read, on which computer, from which
execution — and must be able to say "I could not read anything" instead of dressing a
guess as an observation.

So this module is split at a visible seam:

* :func:`scan` is **deterministic**. It reads an already-authorized excerpt and reports
  what is literally in it: which lines carry an error marker, where the last one is,
  the traceback head and its final exception line, and how much of the log was read.
  It runs with no model, no network and no credential, so the product still tells the
  truth on an offline laptop.
* :func:`redact` runs on the excerpt *before* anything leaves the process, and removes
  the shapes a log uses to carry a credential.
* :func:`validate_diagnosis` turns a model's answer into a :class:`Diagnosis`, or
  refuses it. The model receives the excerpt and the facts — nothing else — and returns
  prose. It cannot return an action, a permission, a risk level or an approval; an
  answer that asserts one is discarded whole rather than filtered.

The two are separate types on purpose: an :class:`Evidence` is something measured, a
:class:`Diagnosis` is something said. A reader can always tell which one they are
holding, and a missing model degrades to the evidence rather than to a story.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: How much of a log one analysis may send to a model. The agent already bounds a read
#: at 64 KiB; this is the smaller, separate bound for what crosses the network. It is
#: the *tail* that is kept, because the latest error is the one the user asked about
#: and the newest lines are at the end.
MAX_ANALYSIS_BYTES = 12 * 1024
#: Error-marked lines reported as evidence. Enough to see a cascade, few enough that a
#: summary stays a summary rather than becoming a second copy of the log.
MAX_ERROR_LINES = 12
#: The tail kept verbatim alongside the marked lines: the frames and the final line.
MAX_TAIL_LINES = 40
#: A fix is a sentence, not an essay. Bounded so a model cannot use the field to smuggle
#: a page of instructions past a reader who skimmed it.
MAX_FIX_CHARS = 400
MAX_FIXES = 5
MAX_FIELD_CHARS = 1200

#: What an error looks like across the runtimes this product actually sees logs from.
#: Deliberately broad: a missed error is worse than a shown warning, because the whole
#: point of the scan is that the *model* is not the only thing that looked.
_ERROR_MARKER = re.compile(
    r"(?:\berror\b|\bexception\b|\btraceback\b|\bfatal\b|\bcritical\b|\bpanic\b|"
    r"\bfailed\b|\bfailure\b|connection refused|\bdenied\b|\btimed? ?out\b|"
    r"\bsegfault\b|\bundefined\b|\bnot found\b|\bcannot\b|\bcan't\b|"
    r"\bEADDRINUSE\b|\bENOENT\b|\bECONNREFUSED\b|\bEACCES\b|\bOOMKilled\b)",
    re.I,
)
#: Python/Ruby/Java-style traceback heads, which carry the frames after them.
_TRACEBACK_HEAD = re.compile(r"^\s*(?:Traceback \(most recent call last\)|.*\bTraceback\b)", re.I)
#: The final line of a traceback: ``ModuleError: message`` (possibly of the form
#: ``pkg.mod.TypeError: ...``).
_EXCEPTION_LINE = re.compile(r"^\s*(?:[\w.]*[A-Za-z]\w*(?:Error|Exception|Fault|Panic)\b[^\n]*)$")
#: A severity-prefixed line, the shape most application loggers emit.
_SEVERITY_PREFIX = re.compile(r"\b(?:ERROR|FATAL|CRITICAL|PANIC)\b")


@dataclass(frozen=True)
class ErrorLine:
    """One line the scan judged to carry an error, with its position in the excerpt."""

    line: int
    text: str


@dataclass(frozen=True)
class Evidence:
    """What was literally read. Pure measurement — no interpretation, no advice."""

    #: Lines in the excerpt that was actually scanned (not necessarily the whole log).
    lines: int = 0
    #: Bytes in the excerpt that was actually scanned.
    bytes_read: int = 0
    #: The excerpt is a tail: lines were dropped before it.
    truncated: bool = False
    #: Error-marked lines, oldest first, bounded to :data:`MAX_ERROR_LINES`.
    errors: tuple[ErrorLine, ...] = ()
    #: The last error-marked line, whatever the bound. This is "the latest error".
    latest: ErrorLine | None = None
    #: The final line of the last traceback in the excerpt, when one is present.
    exception: str = ""
    #: The traceback head that introduced :attr:`exception`.
    traceback_head: str = ""
    #: The last :data:`MAX_TAIL_LINES` lines verbatim: what a reader would see if they
    #: scrolled to the bottom, which is usually where the story is.
    tail: str = ""
    #: The severity counts, so "3 errors, 1 fatal" can be stated as a number.
    error_count: int = 0
    fatal_count: int = 0

    @property
    def found_error(self) -> bool:
        return self.latest is not None

    def facts(self) -> dict[str, Any]:
        """The machine-checkable summary, as a plain dict a response can carry."""
        return {
            "lines": self.lines,
            "bytes": self.bytes_read,
            "truncated": self.truncated,
            "error_lines": self.error_count,
            "fatal_lines": self.fatal_count,
            "latest_error_line": self.latest.line if self.latest else None,
            "latest_error": self.latest.text if self.latest else "",
            "exception": self.exception,
        }

    def summary(self) -> str:
        """One deterministic sentence. Used when there is no model, and beside one."""
        if not self.lines:
            return "The excerpt was empty; there is no line to call an error."
        scope = f"the last {self.lines} line(s)" + (" of a longer log" if self.truncated else "")
        if self.latest is None:
            return f"I read {scope} and found no line that carries an error marker."
        parts = [
            f"I read {scope}. The latest error marker is at line {self.latest.line}: "
            f"{_clip(self.latest.text, 240)}"
        ]
        # The traceback's terminating line is usually the most informative thing in the
        # excerpt, but it is *not* the latest error marker, and saying it was would
        # misattribute the line number. It is reported as its own fact, or not at all.
        if self.exception and self.exception not in self.latest.text:
            parts.append(f"The last traceback ended with: {_clip(self.exception, 240)}")
        return " ".join(parts)


def _clip(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clip_line(value: str, limit: int) -> str:
    """Clip one log line, preserving its indentation.

    ``_clip`` collapses whitespace because it is used on prose. A log line's leading
    indentation is not decoration — it is how a traceback shows which frame raised —
    so a stack trace run through the prose collapser reads as one long sentence and
    loses the only structure it had.
    """
    text = value.rstrip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def scan(text: str, *, truncated: bool = False) -> Evidence:
    """Report the error markers in ``text``. Deterministic and side-effect free.

    ``truncated`` says whether the caller already knows it is holding a tail of a
    larger log (the agent's read returns that flag); it is reported, never inferred.
    """
    if not text:
        return Evidence(truncated=truncated)
    raw_lines = text.splitlines()
    errors: list[ErrorLine] = []
    error_count = 0
    fatal_count = 0
    last_traceback_head = ""
    last_exception = ""
    for index, raw in enumerate(raw_lines, start=1):
        line = raw.rstrip()
        if not line.strip():
            continue
        if _SEVERITY_PREFIX.search(line):
            error_count += 1
            if re.search(r"\b(?:FATAL|CRITICAL|PANIC)\b", line):
                fatal_count += 1
        elif _ERROR_MARKER.search(line):
            error_count += 1
        if _ERROR_MARKER.search(line) or _SEVERITY_PREFIX.search(line):
            errors.append(ErrorLine(line=index, text=_clip_line(line, 300)))
        if _TRACEBACK_HEAD.match(line):
            last_traceback_head = _clip_line(line, 300)
            last_exception = ""
        elif _EXCEPTION_LINE.match(line) and last_traceback_head:
            # The final line of a traceback is the exception itself. Later frames are
            # indented and are not matched, so this settles on the terminating line.
            last_exception = _clip_line(line, 300)
    # Keep the *last* MAX_ERROR_LINES: a cascade is read from where it ended.
    kept = tuple(errors[-MAX_ERROR_LINES:])
    tail_start = max(0, len(raw_lines) - MAX_TAIL_LINES)
    return Evidence(
        lines=len(raw_lines),
        bytes_read=len(text.encode("utf-8", errors="replace")),
        truncated=truncated,
        errors=kept,
        latest=errors[-1] if errors else None,
        exception=last_exception,
        traceback_head=last_traceback_head,
        tail="\n".join(raw_lines[tail_start:]),
        error_count=error_count,
        fatal_count=fatal_count,
    )


def trim_for_model(text: str, *, limit: int = MAX_ANALYSIS_BYTES) -> tuple[str, bool]:
    """Cut an excerpt to the bounded tail that may cross the network.

    Returns ``(excerpt, cut)``. The *tail* is kept because it is the most recent part
    of the log — the part the user's "latest error" lives in — and a cut is reported so
    the model is told it is not looking at the beginning of a file.
    """
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text, False
    kept = data[-limit:]
    # A cut in the middle of a UTF-8 sequence would produce a replacement character at
    # the very start of the excerpt; dropping the leading partial line avoids it.
    newline = kept.find(b"\n")
    if 0 <= newline < 512:
        kept = kept[newline + 1 :]
    return kept.decode("utf-8", errors="replace"), True


@dataclass(frozen=True)
class Diagnosis:
    """What was said about the evidence, and by whom.

    ``status`` is the field a caller must read before the prose:

    * ``"diagnosed"`` — a model explained the evidence.
    * ``"unavailable"`` — there is evidence, but no model to explain it this build; the
      evidence summary stands on its own and no cause is claimed.
    * ``"unobserved"`` — nothing was read, so nothing can be said.
    """

    status: str
    evidence: Evidence = field(default_factory=Evidence)
    finding: str = ""
    likely_cause: str = ""
    fixes: tuple[str, ...] = ()
    confidence: str = ""
    unanswered: str = ""
    model: str = ""
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"diagnosed", "unavailable", "unobserved"}:
            raise ValueError(f"unknown diagnosis status {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "finding": self.finding,
            "likely_cause": self.likely_cause,
            "fixes": list(self.fixes),
            "confidence": self.confidence,
            "unanswered": self.unanswered,
            "model": self.model,
            "evidence": self.evidence.facts(),
            "evidence_summary": self.evidence.summary(),
            "notes": list(self.notes),
        }


def _text_field(raw: Mapping[str, Any], key: str, limit: int = MAX_FIELD_CHARS) -> str:
    value = raw.get(key)
    return _clip(value, limit) if isinstance(value, str) else ""


def _fixes(raw: Mapping[str, Any]) -> tuple[str, ...]:
    value = raw.get("fixes")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(_clip(item, MAX_FIX_CHARS))
        if len(out) >= MAX_FIXES:
            break
    return tuple(out)


#: Fields a *diagnosis* may never carry. The model is a narrator here; if its answer
#: asserts authority — a risk level, an approval, a permission — the whole answer is
#: refused rather than filtered, for the same reason a plan entry that asserts one is.
_REFUSED_FIELDS = frozenset(
    {
        "risk_level", "risk", "permissions", "permission", "grant", "grants",
        "approved", "approval", "authorized", "authorisation", "authorization",
        "policy", "decision", "allow_high_risk", "max_risk", "consent",
    }
)


def validate_diagnosis(raw: Any) -> tuple[Diagnosis | None, tuple[str, ...]]:
    """Turn a model's answer into a :class:`Diagnosis`, or refuse it.

    Returns ``(diagnosis, notes)``. A ``None`` diagnosis means the answer was unusable
    and the caller should fall back to the evidence — never to a guess.
    """
    if raw is None:
        return None, ("the model returned nothing",)
    if not isinstance(raw, Mapping):
        return None, ("the model's answer was not an object",)
    asserted = sorted(_REFUSED_FIELDS.intersection(raw))
    if asserted:
        return None, (
            "the model's answer asserted fields it does not own "
            f"({asserted}); it was discarded whole",
        )
    finding = _text_field(raw, "finding")
    cause = _text_field(raw, "likely_cause")
    fixes = _fixes(raw)
    if not finding:
        return None, ("the model's answer had no finding",)
    if not cause and not fixes:
        return None, ("the model's answer gave neither a cause nor a fix",)
    confidence = _text_field(raw, "confidence", 20).lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = ""
    return (
        Diagnosis(
            status="diagnosed",
            finding=finding,
            likely_cause=cause,
            fixes=fixes,
            confidence=confidence,
            unanswered=_text_field(raw, "unanswered", 600),
        ),
        (),
    )


def unobserved(reason: str) -> Diagnosis:
    """Nothing was read. Saying so is the whole answer."""
    return Diagnosis(status="unobserved", notes=(reason,))


def unavailable(evidence: Evidence, reason: str) -> Diagnosis:
    """Evidence exists but nothing can explain it. The measurement still stands."""
    return Diagnosis(status="unavailable", evidence=evidence, notes=(reason,))


# --------------------------------------------------------------------------------------
# What may leave the process
# --------------------------------------------------------------------------------------

#: The shapes a log uses to carry a credential. Each is replaced with a marker that keeps
#: the *kind* of secret visible, because "there was a bearer token here" is diagnostically
#: useful while the token itself is not. Order matters: a longer, more specific pattern
#: runs before the generic ``key=value`` one, or the generic rule would rewrite only part
#: of a URL and leave the rest of the secret in place.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # An Authorization header, however it was logged.
    (
        re.compile(r"(?i)\b(authorization\s*:\s*)(?:bearer|basic|token)\s+[^\s\"',;]+"),
        r"\1[REDACTED]",
    ),
    # A bearer token that was logged without its header.
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [REDACTED]"),
    # Provider-shaped keys, which have recognisable prefixes and long bodies.
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"), "[REDACTED:anthropic-key]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), "[REDACTED:api-key]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED:github-token]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED:slack-token]"),
    # A JWT: three base64url segments. Recognisable, and never useful in a log.
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
        "[REDACTED:jwt]",
    ),
    # A credential passed as a query parameter, in a URL that found its way into a log.
    (
        re.compile(
            r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|secret|password|passwd|pwd|"
            r"auth|key|sig|signature)=)[^&\s\"']+"
        ),
        r"\1[REDACTED]",
    ),
    # A ``key=value`` or ``key: value`` pair whose key names a secret. The value is
    # bounded so a line that happens to start with ``password`` does not eat the file.
    (
        re.compile(
            r"(?i)\b([\w.-]*(?:api[_-]?key|secret|token|password|passwd|pwd|"
            r"client[_-]?secret|private[_-]?key|access[_-]?key)[\w.-]*\s*[=:]\s*)"
            r"(\"[^\"]{4,}\"|'[^']{4,}'|[^\s\"',;]{4,})"
        ),
        r"\1[REDACTED]",
    ),
    # The same, inside a JSON string: ``"password": "..."`` -- the quote survives on both
    # sides so the excerpt stays parseable-looking rather than becoming mangled text.
    (
        re.compile(
            r"(?i)(\"[\w.-]*(?:api[_-]?key|secret|token|password|passwd|"
            r"private[_-]?key|access[_-]?key)[\w.-]*\"\s*:\s*)\"[^\"]{4,}\""
        ),
        r'\1"[REDACTED]"',
    ),
    # A URL carrying userinfo: ``postgres://user:password@host``.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+(@)"), r"\1[REDACTED]\2"),
)

#: A home directory is a username, which is personal data that a diagnosis does not
#: need. ``/home/alice/app`` becomes ``~/app``; ``C:\Users\Alice\app`` becomes
#: ``~\app``. The shape of the path -- and the file name -- is what matters here.
_HOME_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\w.])/home/[^/\s\"':]+"),
    re.compile(r"(?<![\w.])/Users/[^/\s\"':]+"),
    re.compile(r"(?i)(?<![\w.])[A-Z]:\\Users\\[^\\\s\"':]+"),
)


def redact(text: str) -> tuple[str, int]:
    """Remove credential-shaped and personal strings. Returns ``(text, replacements)``.

    Deterministic, and applied to every excerpt before it is measured, shown or sent.
    It is deliberately *not* a promise that the excerpt is safe — a program can print a
    secret in a shape no rule anticipates — so the caller still reports the excerpt size
    and still sends only a bounded tail. Redaction lowers the chance of leaking a
    credential; the bound is what keeps the cost of being wrong small.
    """
    if not text:
        return "", 0
    count = 0
    out = text
    for pattern, replacement in _REDACTIONS:
        out, hits = pattern.subn(replacement, out)
        count += hits
    for pattern in _HOME_PATHS:
        out, hits = pattern.subn("~", out)
        count += hits
    return out, count


def prepare(text: str) -> tuple[str, Evidence, int, bool]:
    """Redact, bound and measure in the one order that is safe.

    Returns ``(excerpt, evidence, redactions, cut)``. Redaction happens **before**
    measurement, so the byte count a user is shown is the byte count that would leave
    the process — not the size of the unredacted original.
    """
    clean, redactions = redact(text)
    excerpt, cut = trim_for_model(clean)
    return excerpt, scan(excerpt, truncated=cut), redactions, cut


def evidence_text(evidence: Evidence) -> str:
    """The facts, as text for a model. No paths, no command lines, no inventory."""
    facts = evidence.facts()
    rows = [
        f"- lines read: {facts['lines']}",
        f"- bytes read: {facts['bytes']}",
        f"- the excerpt starts partway through a longer log: "
        f"{'yes' if facts['truncated'] else 'no'}",
        f"- lines carrying an error marker: {facts['error_lines']}",
        f"- lines carrying FATAL/CRITICAL/PANIC: {facts['fatal_lines']}",
    ]
    if facts["latest_error_line"] is not None:
        rows.append(
            f"- the latest error marker is at line {facts['latest_error_line']}: "
            f"{facts['latest_error']}"
        )
    if facts["exception"]:
        rows.append(f"- the final line of the last traceback: {facts['exception']}")
    return "\n".join(rows)