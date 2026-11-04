"""Running an automation — as the press the user would have made, not instead of it.

A stored workflow is a *plan*. This module turns one into an ordinary VoiceOS task
session and hands it to the existing orchestrator, so every step still passes through
registry resolution, the policy engine, consent, risk, the approval gate, the executor,
the verifier and the audit chain. There is no second engine here: :class:`Runner` only
decides who is asking, and it is deliberately careful about that question.

**Who is asking.** A manual run has a caller with a real credential, and its context is
built exactly as ``/voiceos/sessions/{id}/run`` builds one — ``_context`` in the API.
A scheduled or event run has *nobody pressing*; the honest actor is the workflow's
owner, whose identity is real, but whose consent is not being exercised by this
process. So :func:`_automation_context` builds a context with the owner's identity and
**no grants**: the policy engine then refuses any step that needs a scope, with the
real reason (``missing permission(s): [...]``) rather than running it. That is the
intended behaviour, not a limitation to work around — "scheduling grants no
permissions" is true because there is no path from a stored workflow to a grant.

**What a run records.** The step-level truth comes from the orchestrator, but the
shape is simpler: ``state``, and for a device that could not take a step, the fact that
it was unavailable *and no other computer was used*. Nothing here rewrites a step's
outcome to something cheerier.

**No retry, no catch-up.** An occurrence that could not run is recorded as it happened
and the next occurrence is scheduled. Replaying an occurrence later would be a run the
user did not ask for, at a time they did not choose.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from actionmesh.core.permissions.context import PolicyContext
from actionmesh.protocol.models import Identity, Policy
from actionmesh.voiceos.models import TaskSession, TaskStep
from actionmesh.voiceos.orchestrator import Orchestrator
from actionmesh.voiceos.workflows import (
    AutomationRun,
    Workflow,
    next_occurrence,
    parse_when,
)

#: How many of a run's steps the history carries. A step is a few small scalars; a
#: workflow is capped at MAX_STEPS anyway, so this is a belt-and-braces bound.
MAX_RECORDED_STEPS = 16

#: Step states that mean "over", mapped to the run state they imply. Mirrors
#: :data:`actionmesh.voiceos.models.STEP_STATES`; a step state that is not here is
#: treated as unfinished, which is what it is.
_DONE = {
    "succeeded": "done",
    "succeeded_unverified": "unverified",
    "failed": "failed",
    "denied": "denied",
    "cancelled": "cancelled",
    "skipped": "skipped",
    "interrupted": "interrupted",
}


def run_state_for(session: TaskSession) -> str:
    """The run state a finished (or parked) session implies.

    ``awaiting_approval`` is reported as itself, not as a failure: the run is alive,
    a human has not answered yet, and calling that "failed" would teach the history to
    lie in the other direction.
    """
    if session.status == "awaiting_approval":
        return "awaiting_approval"
    if session.status in {"running", "proposed"}:
        return "running"
    if session.status == "completed":
        # A completed session can still have skipped steps. "Partial" is the only
        # honest word when some steps did not run because their condition was false.
        if any(step.state == "skipped" for step in session.steps):
            return "partial"
        if any(step.state == "succeeded_unverified" for step in session.steps):
            return "partial"
        return "succeeded"
    if session.status == "cancelled":
        return "cancelled"
    if session.status == "interrupted":
        return "interrupted"
    # Any denial, refusal or device failure.
    if any(step.state == "denied" for step in session.steps):
        return "denied"
    return "failed"


def _automation_context(actor_id: str, *, policy: Policy | None = None) -> PolicyContext:
    """A context for a run nobody pressed: the owner's identity, no grants.

    ``grants`` is empty **by construction**, not by filtering. That is what makes
    "scheduling grants no permissions" a structural property: whatever the owner has
    consented to elsewhere, this object cannot carry it, so the policy engine's own
    missing-permission branch is the answer for every gated step. The rule in
    ``policy`` only declares which action types the workflow is willing to *consider*,
    exactly as the orchestrator does for its own steps — it authorises nothing.
    """
    return PolicyContext(
        actor=Identity(id=actor_id, kind="user", display=actor_id),
        policy=policy or Policy(),
    )


class Runner:
    """Turns a workflow into a session, runs it, and records what happened.

    Owns no execution logic: :class:`Orchestrator` is passed in, which owns no
    execution logic either. The chain ends at ``ExecutionEngine``, as it must.
    """

    def __init__(
        self,
        *,
        orchestrator: Orchestrator,
        save_session: Callable[[TaskSession], None] | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self._save_session = save_session

    # -- turning a plan into a session -------------------------------------

    def build_session(
        self,
        workflow: Workflow,
        run: AutomationRun,
        *,
        default_device_id: str = "",
    ) -> TaskSession:
        """The session a press would have produced, stamped with where it came from.

        ``request`` is a sentence rather than an empty string so a session reloaded
        from the execution store can still say which automation it was.

        ``default_device_id`` is only used when the plan, and the step itself, name no
        computer. It is the caller's resolved answer for *this* press — never a
        fallback chosen here, because a scheduler must not invent a target. A step that
        names a computer keeps it.
        """
        base_target = workflow.device_id or default_device_id
        return TaskSession(
            session_id=f"ses_{run.run_id.removeprefix('run_')}",
            actor_id=workflow.actor_id,
            # A stable, readable provenance line. It never carries the run's outcome;
            # the outcome is whatever the steps below actually did.
            request=f"automation {workflow.name!r} ({workflow.workflow_id})",
            device_id=base_target,
            steps=[
                TaskStep(
                    index=step.index,
                    label=step.label,
                    action_type=step.action_type,
                    parameters=dict(step.parameters),
                    when=dict(step.when),
                    # Per-step aim. Empty follows the workflow's own target; a step
                    # that names a computer is never rewritten by the workflow's.
                    device_id=step.device_id or base_target,
                )
                for step in workflow.steps
            ],
        )

    def _context_for(self, workflow: Workflow, context: PolicyContext | None) -> PolicyContext:
        if context is not None:
            # A manual run: the caller's real credential, which may carry consent.
            return context
        return _automation_context(workflow.actor_id)

    # -- running -----------------------------------------------------------

    async def execute(
        self,
        workflow: Workflow,
        run: AutomationRun,
        *,
        context: PolicyContext | None = None,
        save_run: Callable[[AutomationRun], None] | None = None,
        default_device_id: str = "",
    ) -> AutomationRun:
        """Run the workflow's steps once and return the run with its outcome.

        ``context`` is present only when a human pressed run; its absence is itself the
        statement "nobody is pressing", and the context built then carries no grants.

        ``default_device_id`` is the caller's resolved target for a plan that names
        none. A scheduled run passes nothing: a tick has no operator behind it and
        must not pick a machine, so an untargeted plan is refused by the orchestrator
        and reported, never aimed.
        """
        session = self.build_session(workflow, run, default_device_id=default_device_id)
        run.session_id = session.session_id
        run.state = "running"
        run.started_at = run.started_at or datetime.now(UTC).isoformat()
        _save = save_run or (lambda _run: None)
        _save(run)
        if self._save_session is not None:
            self._save_session(session)

        def checkpoint(updated: TaskSession) -> None:
            # The execution id reaches disk *before* the step is awaited, so a crash
            # mid-step leaves a session that can be reconciled against the record —
            # the same ordering the VoiceOS run route relies on.
            if self._save_session is not None:
                self._save_session(updated)

        try:
            outcome = await self.orchestrator.advance(
                session,
                self._context_for(workflow, context),
                checkpoint=checkpoint,
            )
        except Exception as exc:
            # An exception the orchestrator did not expect is still an outcome, and the
            # history has to say so. Swallowing it into "succeeded" would be the one
            # unrecoverable lie; re-raising it would leave the run row stuck at
            # "running" with nobody to move it.
            run.state = "failed"
            run.error = f"the run stopped with an error: {exc}"
            run.finished_at = datetime.now(UTC).isoformat()
            _save(run)
            return run

        updated: TaskSession = outcome["session"]
        run.steps = [
            {
                "index": step.index,
                "label": step.label,
                "action_type": step.action_type,
                "state": _DONE.get(step.state, "pending"),
                "verified": step.verified,
                "device_id": step.device_id,
                "error": step.error,
            }
            for step in updated.steps[:MAX_RECORDED_STEPS]
        ]
        run.state = run_state_for(updated)
        run.error = self._error_for(updated)
        if run.state not in {"running", "awaiting_approval"}:
            run.finished_at = datetime.now(UTC).isoformat()
        if self._save_session is not None:
            self._save_session(updated)
        _save(run)
        return run

    @staticmethod
    def _error_for(session: TaskSession) -> str:
        """The one sentence a history row shows, taken from the step that stopped it."""
        for step in session.steps:
            if step.state in {"failed", "denied", "cancelled", "interrupted"}:
                if step.error:
                    return f"step {step.index + 1} ({step.label}): {step.error}"
                return f"step {step.index + 1} ({step.label}) was {step.state}"
        return ""


def schedule_next(workflow: Workflow, *, now: datetime | None = None) -> None:
    """Set ``next_run_at`` from the schedule. Pure arithmetic, no clock service.

    Called when a workflow is created, edited, enabled, or after a run. A workflow
    with no schedule keeps an empty field, and an interval schedule recomputes from
    *now* rather than from the occurrence that just fired: a run that took ten minutes
    must not make the next one due immediately.
    """
    moment = now or datetime.now(UTC)
    if not workflow.schedule or not workflow.enabled:
        workflow.next_run_at = ""
        return
    upcoming = next_occurrence(workflow.schedule, moment)
    workflow.next_run_at = upcoming.isoformat() if upcoming is not None else ""


def slot_for_schedule(workflow: Workflow, due: datetime, now: datetime | None = None) -> str:
    """The occurrence's identity: the due time it was scheduled for.

    Two ticks that both see the same ``next_run_at`` compute the same slot, so the
    store's unique constraint lets exactly one of them claim it. When a workflow is
    badly overdue — the machine slept for a week — the *stored* due time is used, so
        the missed occurrence is recorded once and the workflow then moves on, rather
    than the ticker inventing one run per missed interval.
    """
    stored = parse_when(due.isoformat())
    return (stored or due).astimezone(UTC).isoformat(timespec="seconds")


__all__ = [
    "MAX_RECORDED_STEPS",
    "Runner",
    "run_state_for",
    "schedule_next",
    "slot_for_schedule",
]