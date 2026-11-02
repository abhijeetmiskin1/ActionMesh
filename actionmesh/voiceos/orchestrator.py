"""Step-by-step orchestration through ActionMesh — never a second executor.

A task session is advanced one step at a time, and every step is an ordinary ActionMesh
execution: registry resolution, parameter validation, the policy engine, consent, risk,
the approval gate, the executor, the independent verifier, the audit chain. The
orchestrator adds nothing to that pipeline and subtracts nothing from it. It decides
*which step is next* and *what the observed result means for the step after it* — the
two things a multi-step task needs — and nothing else.

The caller's ``PolicyContext`` is passed in, not built here. That is deliberate: the
context is what a credential resolved to, so a step can only ever be authorised with the
authority of the person who pressed run. A session resumed after a restart, or by a
different device, gets a different context and therefore a different answer — which is
the correct outcome, not a bug to smooth over.
"""

from __future__ import annotations

from typing import Any

from actionmesh.core.executor.engine import ExecutionEngine, ExecutionResult
from actionmesh.core.graph.serialization import graph_from_dict
from actionmesh.core.permissions.context import PolicyContext
from actionmesh.protocol.models import ActionState, ExecutionRecord
from actionmesh.voiceos.models import TaskSession, TaskStep
from actionmesh.voiceos.reasoning import observed_references

#: States an execution can leave. Anything else means "still happening".
TERMINAL_STATES = frozenset(
    {
        # C35: the owner died and the outcome is unknown. It is over -- nothing will
        # ever legitimately move it again -- and folding it is the only way a task
        # session can say so instead of hanging on "awaiting approval" forever.
        ActionState.INTERRUPTED,
        ActionState.SUCCESS,
        ActionState.FAILED,
        ActionState.DENIED,
        ActionState.CANCELLED,
        ActionState.TIMEOUT,
        ActionState.PARTIALLY_COMPLETED,
        ActionState.REQUIRES_USER,
    }
)


class Orchestrator:
    """Runs a session's steps, and reports what actually happened to each one."""

    def __init__(self, *, engine: ExecutionEngine) -> None:
        self.engine = engine

    async def advance(self, session: TaskSession, context: PolicyContext) -> dict[str, Any]:
        """Run as far as this caller's authority allows, then stop and report.

        Returns ``{"session": ..., "references": [...]}``. It stops early — and says why
        — when a step parks for approval, fails, is denied, or is cancelled. It never
        pushes past a step whose outcome it does not actually know.
        """
        session.status = "running"
        references: list[dict[str, str]] = []
        while True:
            step = session.pending_step()
            if step is None:
                break
            if step.state in {"awaiting_approval", "running"}:
                record = self.engine.record(step.execution_id)
                if record is None or self._apply_record(session, step, record) is False:
                    session.status = (
                        "awaiting_approval" if step.state == "awaiting_approval" else "running"
                    )
                    session.summary = self._summary(session)
                    return {"session": session, "references": references}
                references = self._collect(step, references)
                continue
            if not self._condition_met(session, step):
                step.state = "skipped"
                step.error = "its condition was not met by the earlier step"
                references = self._collect(step, references)
                continue
            result = await self.engine.execute(self._graph(step), self._context_for(context, step))
            self._apply_result(session, step, result)
            references = self._collect(step, references)
            if step.state in {"awaiting_approval", "running"}:
                session.status = "awaiting_approval" if step.state == "awaiting_approval" else "running"
                session.summary = self._summary(session)
                return {"session": session, "references": references}
            if step.state in {"failed", "denied", "cancelled"}:
                session.status = "stopped" if step.state == "cancelled" else "failed"
                session.summary = self._summary(session)
                return {"session": session, "references": references}
        session.status = "completed"
        session.summary = self._summary(session)
        return {"session": session, "references": references}

    def fold_records(self, session: TaskSession) -> TaskSession:
        """Bring a live step up to date from its execution record. **Reads only.**

        A step can end while nobody is asking the session — the user cancelled, the
        server restarted, the approval route finished the run. Without this the task
        keeps saying `awaiting_approval` next to an execution that is already
        `cancelled`, and a control plane that contradicts itself is worse than one that
        says nothing. Nothing here executes: it reads records the caller can already read.
        """
        for step in session.steps:
            if step.state not in {"running", "awaiting_approval"} or not step.execution_id:
                continue
            record = self.engine.record(step.execution_id)
            if record is not None:
                self._apply_record(session, step, record)
        settled = all(step.settled for step in session.steps)
        if settled:
            if any(step.state == "interrupted" for step in session.steps):
                session.status = "interrupted"
            elif any(step.state == "cancelled" for step in session.steps):
                session.status = "cancelled"
            elif any(step.state in {"failed", "denied"} for step in session.steps):
                session.status = "failed"
            else:
                session.status = "completed"
        elif any(step.state == "awaiting_approval" for step in session.steps):
            session.status = "awaiting_approval"
        elif any(step.state == "running" for step in session.steps):
            session.status = "running"
        elif any(step.state in {"failed", "denied"} for step in session.steps):
            # A refused or failed step is a stop, not a pause: the later steps will not
            # run because their predecessor never produced what they depend on.
            session.status = "failed"
        elif any(step.state == "cancelled" for step in session.steps):
            session.status = "cancelled"
        elif any(step.state == "interrupted" for step in session.steps):
            session.status = "interrupted"
        else:
            # Nothing is executing and nothing is waiting for a decision: the task has
            # settled steps and a pending one, and only a press moves it. `proposed` is
            # the vocabulary's word for "planned, not running" -- and it is the one that
            # does not lie, where leaving `awaiting_approval` behind would tell a reader
            # an approval is outstanding when the approval was answered minutes ago.
            session.status = "proposed"
        # Recomputed on every fold, not only when the task finished. A step that settled
        # changes what the task is, and a summary that still says "0 of 2" next to a
        # finished step is the control plane contradicting the record it just read.
        session.summary = self._summary(session)
        session.touch()
        return session

    def request_cancel(self, session: TaskSession) -> str:
        """Ask ActionMesh to stop the step that is live. Confirmation comes from the record."""
        step = next(
            (item for item in session.steps if item.state in {"running", "awaiting_approval"}),
            None,
        )
        if step is None or not step.execution_id:
            return ""
        # The durable request, not this process's task map: the step may be running in
        # another request, another worker, or one the server restarted over.
        self.engine.request_cancel(step.execution_id)
        return step.execution_id

    # -- one step ----------------------------------------------------------

    def _graph(self, step: TaskStep) -> Any:
        # Built through the same deserialisation and validation an HTTP body passes
        # through, so a step cannot be "more trusted" than a typed request.
        return graph_from_dict(
            {
                "intent": step.label,
                "nodes": [
                    {
                        "node_id": "s1",
                        "action": {
                            "type": step.action_type,
                            "provider": self._provider_for(step.action_type),
                            "parameters": dict(step.parameters),
                        },
                    },
                    {"node_id": "send", "node_type": "end", "depends_on": ["s1"]},
                ],
            }
        )

    def _provider_for(self, action_type: str) -> str:
        """The provider the *registry* says serves this action.

        Hardcoding the laptop agent here would quietly make VoiceOS unable to run
        anything else, and "quietly unable" is how a feature ends up looking broken in a
        way nobody can explain. An action nobody registered falls back to the agent and
        is refused by resolution, which is where an unknown action belongs.
        """
        from actionmesh.core.registry.errors import UnknownActionError

        try:
            return self.engine.registry.get(action_type).provider or "mock"
        except UnknownActionError:
            return "laptop"

    def _context_for(self, context: PolicyContext, step: TaskStep) -> PolicyContext:
        from dataclasses import replace

        from actionmesh.protocol.models import Policy, PolicyDecision, PolicyRule

        # The same bounded rule the control room and the voice page send: the workspace
        # declares what it is willing to *consider*, for exactly this action type. It
        # authorises nothing — consent, risk and approval still decide.
        policy = Policy(rules=[PolicyRule(action=step.action_type, decision=PolicyDecision.ALLOW)])
        return replace(context, policy=policy, approvals=list(context.approvals))

    def _condition_met(self, session: TaskSession, step: TaskStep) -> bool:
        when = step.when
        if not when:
            return True
        earlier = session.step(int(when.get("step", -1)))
        if earlier is None:
            return False
        observed = earlier.observed
        for key, expected in when.items():
            if key == "step":
                continue
            actual = observed.get(key)
            if actual != expected:
                return False
        return True

    def _apply_result(self, session: TaskSession, step: TaskStep, result: ExecutionResult) -> None:
        step.execution_id = result.execution_id
        node = self._action_node(result.nodes)
        step.observed = dict(self._field(node, "result") or {})
        # Overwrite, never accumulate: the reason a step parked must not still be
        # printed next to it after it succeeded.
        step.error = str(self._field(node, "error") or result.error or "")
        state = result.state
        if state is ActionState.WAITING_PERMISSION:
            step.state = "awaiting_approval"
        elif state is ActionState.DENIED:
            step.state = "denied"
        elif state is ActionState.CANCELLED:
            step.state = "cancelled"
        elif state is ActionState.SUCCESS:
            verified = self._field(node, "verified")
            step.verified = bool(verified) if verified is not None else None
            step.state = "succeeded" if step.verified else "succeeded_unverified"
        elif state is ActionState.REQUIRES_USER:
            step.state = "succeeded_unverified"
            step.verified = False
        else:
            step.state = "failed"
        session.touch()

    def _apply_record(self, session: TaskSession, step: TaskStep, record: ExecutionRecord) -> bool:
        """Fold a stored record into a step. False while the execution is not over."""
        if record.state not in TERMINAL_STATES:
            return False
        node = self._action_node(record.results)
        step.observed = dict(self._field(node, "result") or {})
        step.error = str(self._field(node, "error") or "")
        if record.state is ActionState.DENIED:
            step.state = "denied"
        elif record.state is ActionState.CANCELLED:
            step.state = "cancelled"
        elif record.state is ActionState.INTERRUPTED:
            step.state = "interrupted"
            step.error = str(record.error or step.error)
        elif record.state is ActionState.SUCCESS:
            verified = self._field(node, "verified")
            step.verified = bool(verified) if verified is not None else None
            step.state = "succeeded" if step.verified else "succeeded_unverified"
        else:
            step.state = "failed"
        session.touch()
        return True

    @staticmethod
    def _field(node: Any, name: str, default: Any = None) -> Any:
        """Read a node's field whether it is a `NodeOutcome` or a stored dict.

        The engine hands back dataclasses and the record store hands back JSON-shaped
        dicts. Reading only one of the two silently produced `verified: None` for a step
        whose node had in fact verified — a lost piece of evidence, which is exactly the
        kind of thing this layer is not allowed to do.
        """
        if isinstance(node, dict):
            return node.get(name, default)
        return getattr(node, name, default)

    def _action_node(self, nodes: Any) -> Any:
        for node in (nodes or {}).values():
            if self._field(node, "action_type"):
                return node
        return None

    @staticmethod
    def _collect(step: TaskStep, references: list[dict[str, str]]) -> list[dict[str, str]]:
        found = observed_references(step.action_type, step.observed)
        return found or references

    @staticmethod
    def _summary(session: TaskSession) -> str:
        settled = [step for step in session.steps if step.settled]
        unverified = [step.index + 1 for step in session.steps if step.verified is False]
        parts = [f"{len(settled)} of {len(session.steps)} steps settled."]
        if unverified:
            parts.append(
                "Step(s) " + ", ".join(str(item) for item in unverified)
                + " ran but were not independently verified."
            )
        failed = [step for step in session.steps if step.state in {"failed", "denied", "cancelled"}]
        if failed:
            last = failed[-1]
            parts.append(f"Stopped at step {last.index + 1} ({last.state}): {last.error or 'no reason given'}.")
        return " ".join(parts)


__all__ = ["TERMINAL_STATES", "Orchestrator"]
