"""The explicit analysis step: authorized observation in, explanation out.

This is the module that makes the product's first vertical slice real — *"check my
backend, find the latest error and explain how to fix it"* — without either of the two
dishonest shortcuts.

**It does not read the log itself.** Everything it examines has already been produced by
an ordinary ActionMesh execution: the step was authorised, the device was resolved, the
policy engine decided, the human approved where the risk asked for it, and the laptop
agent read only a process *it* launched. By the time anything reaches here, the question
"was this allowed?" has already been answered by the system that owns that answer.

**It does not persist the log.** ``device.process.logs`` deliberately keeps only the
*shape* of what it read in ``observed`` and sends the body down the transient
``execution.output`` channel. That channel is bounded, actor-scoped, never audited and
never replayed — and it is process-local, which is exactly the right lifetime for a
secret-adjacent blob. This module draws from that channel, into memory, for the duration
of one request, and hands on only a redacted, bounded tail. Nothing here writes log text
to the VoiceOS store, the audit chain, or a conversation.

The flow, and where each guarantee is enforced:

1. :func:`log_excerpt` collects the transient output for an execution id and refuses one
   whose actor does not match the caller.
2. :func:`actionmesh.voiceos.diagnosis.prepare` redacts, bounds and measures, in that
   order, so the size a user is told is the size that would leave.
3. :func:`analyze` sends that excerpt to the configured analyst and validates the answer
   through :func:`~actionmesh.voiceos.diagnosis.validate_diagnosis`, which refuses an
   answer that asserts authority.
4. With no analyst — the normal state of a laptop with no credential — the deterministic
   evidence is the answer, and the response says so. It never falls back to a guess.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from actionmesh.core.output import OUTPUT_EVENT_TYPE
from actionmesh.protocol.models import MeshEvent
from actionmesh.voiceos import diagnosis
from actionmesh.voiceos.llm import AnalysisProvider, AnalysisRequest, LlmError
from actionmesh.voiceos.models import TaskSession, TaskStep

#: The action whose transient output is a log body. Named once so a reader can see the
#: only capability this module will draw from.
LOG_ACTION = "device.process.logs"


@dataclass(frozen=True)
class Excerpt:
    """One log body, as it was collected from the transient channel."""

    text: str
    execution_id: str
    process_id: str
    device_id: str
    streams: tuple[str, ...] = ()

    @property
    def observed(self) -> bool:
        return bool(self.text)


def _actor_of(event: MeshEvent) -> str:
    return str(getattr(event, "actor", "") or "system")


def log_excerpt(
    events: Iterable[MeshEvent],
    *,
    execution_id: str,
    actor: str,
    device_id: str = "",
) -> tuple[Excerpt, str]:
    """Collect the transient output of one ``device.process.logs`` execution.

    Returns ``(excerpt, reason)``. A non-empty ``reason`` means the body was **not**
    collected — the caller has not run the read, it ran somewhere else, or it belonged
    to another actor — and the reason is safe to show: it names no content.

    The actor check is repeated here even though the events already carry an actor and
    the stream authorization applies it. Defence in depth on a path whose payload may be
    arbitrary program output is worth the two lines: a bug in the caller's scoping must
    not become a cross-actor log read.
    """
    if not execution_id:
        return Excerpt("", "", "", device_id), (
            "no log read has run for this step yet, so there is nothing to analyse"
        )
    chunks: list[str] = []
    streams: list[str] = []
    seen_device = device_id
    for event in events:
        if event.event_type != OUTPUT_EVENT_TYPE:
            continue
        if event.execution_id != execution_id:
            continue
        if _actor_of(event) != actor:
            return Excerpt("", execution_id, "", device_id), (
                "that log read belongs to a different actor, so its output is not "
                "available to this request"
            )
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        chunk = payload.get("chunk")
        if not isinstance(chunk, str) or not chunk:
            continue
        streams.append(str(payload.get("stream") or "logs"))
        seen_device = str(payload.get("device_id") or seen_device)
        chunks.append(chunk)
    if not chunks:
        return Excerpt("", execution_id, "", seen_device), (
            "the log read finished but reported no output to analyse — the program may "
            "not have printed anything this agent kept"
        )
    return (
        Excerpt(
            text="".join(chunks),
            execution_id=execution_id,
            process_id="",
            device_id=seen_device,
            streams=tuple(dict.fromkeys(streams)),
        ),
        "",
    )


def last_log_step(session: TaskSession) -> TaskStep | None:
    """The most recent settled ``device.process.logs`` step, or ``None``.

    Deliberately restricted to a *settled* step: analysing the output of a read that is
    still running would describe a file that is still being written as though it were
    final, and the excerpt's own line numbers would not mean what they claimed.
    """
    for step in reversed(session.steps):
        if step.action_type == LOG_ACTION and step.settled and step.execution_id:
            return step
    return None


def analysis_step(session: TaskSession) -> TaskStep | None:
    """The step that is a *place* for an explanation, or ``None``.

    It carries no action type on purpose, so no pipeline stage could dispatch it even by
    mistake. The question and the computer it was asked about live on it rather than on
    the log step, because the explanation is a distinct piece of work that happens after
    the read -- not a property of the read itself.
    """
    for step in reversed(session.steps):
        if step.analysis:
            return step
    return None


def analyze(
    *,
    question: str,
    excerpt: Excerpt,
    analyst: AnalysisProvider | None,
    analyst_note: str = "",
    computer: str = "",
    source: str = "",
) -> diagnosis.Diagnosis:
    """Measure the excerpt, then — if there is an analyst — explain it.

    Synchronous on purpose: the callers run it in a worker thread, because the analyst
    makes one blocking HTTPS call and this must never happen on the event loop.
    """
    prepared, evidence, redactions, cut = diagnosis.prepare(excerpt.text)
    if not prepared.strip():
        return diagnosis.unobserved(
            "the log read produced no text, so there is nothing to analyse"
        )
    notes: list[str] = []
    if redactions:
        notes.append(
            f"{redactions} credential-shaped or personal string(s) were redacted before "
            "analysis"
        )
    if cut:
        notes.append(
            f"only the last {len(prepared.encode('utf-8'))} bytes were kept; the log was "
            "longer"
        )
    if analyst is None:
        return diagnosis.Diagnosis(
            status="unavailable",
            evidence=evidence,
            notes=tuple(notes) + ((analyst_note,) if analyst_note else ()),
        )
    request = AnalysisRequest(
        question=question,
        excerpt=prepared,
        facts=diagnosis.evidence_text(evidence),
        computer=computer,
        source=source,
        truncated=cut,
    )
    try:
        raw = analyst.explain(request)
    except LlmError as exc:
        return diagnosis.Diagnosis(
            status="unavailable",
            evidence=evidence,
            notes=(*notes, f"the model did not answer ({exc})"),
        )
    parsed, refusals = diagnosis.validate_diagnosis(raw)
    if parsed is None:
        return diagnosis.Diagnosis(
            status="unavailable",
            evidence=evidence,
            notes=tuple(notes) + tuple(f"the model's answer was discarded: {item}" for item in refusals),
        )
    return diagnosis.Diagnosis(
        status=parsed.status,
        evidence=evidence,
        finding=parsed.finding,
        likely_cause=parsed.likely_cause,
        fixes=parsed.fixes,
        confidence=parsed.confidence,
        unanswered=parsed.unanswered,
        model=getattr(analyst, "name", ""),
        notes=tuple(notes),
    )


def describe(result: diagnosis.Diagnosis, *, computer: str = "") -> str:
    """The sentence a person reads. Always leads with what was actually read."""
    where = f" on {computer}" if computer else ""
    evidence = result.evidence.summary()
    if result.status == "unobserved":
        return f"I had nothing to analyse{where}. {evidence}"
    if result.status == "unavailable":
        detail = " ".join(result.notes)
        return f"{evidence}{where}{('. ' + detail) if detail else ''} No cause is claimed."
    parts = [f"{evidence}{where}."]
    if result.finding:
        parts.append(result.finding)
    if result.likely_cause:
        parts.append(f"Likely cause: {result.likely_cause}")
    if result.fixes:
        listed = "; ".join(f"{index}) {fix}" for index, fix in enumerate(result.fixes, start=1))
        parts.append(f"Suggested next steps: {listed}.")
    if result.unanswered:
        parts.append(f"I could not determine: {result.unanswered}")
    if result.confidence:
        parts.append(f"Confidence: {result.confidence}.")
    parts.append("Nothing was changed.")
    return " ".join(parts)


__all__ = [
    "LOG_ACTION",
    "Excerpt",
    "analysis_step",
    "analyze",
    "describe",
    "last_log_step",
    "log_excerpt",
]