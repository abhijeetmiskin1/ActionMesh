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

from collections.abc import Callable
from typing import Any

from actionmesh.core.devices import EXECUTION_KINDS
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

    async def advance(
        self,
        session: TaskSession,
        context: PolicyContext,
        *,
        checkpoint: Callable[[TaskSession], None] | None = None,
    ) -> dict[str, Any]:
        """Run as far as this caller's authority allows, then stop and report.

        Returns ``{"session": ..., "references": [...]}``. It stops early — and says why
        — when a step parks for approval, fails, is denied, or is cancelled. It never
        pushes past a step whose outcome it does not actually know.

        ``checkpoint`` is called once a step has been *named* and before it is awaited.
        That ordering is the whole reason it exists: an execution id that is only
        written when the step finishes is an id nobody has if the process dies mid-step,
        and a session without it cannot ask the durable record what became of the work.
        A crash between the checkpoint and the result therefore strands the step at
        ``running`` -- which is the truth -- rather than at ``pending``, which would be
        a lie the next press would act on by running it a second time.
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
            if not self._bind_parameters(session, step):
                # A step whose parameter is an id an earlier step should have observed,
                # and did not. Running it with the id missing would ask the agent about
                # a process that was never named; running it with a guess would be
                # worse. Neither is an option, so the step is denied and the plan stops.
                step.state = "denied"
                step.error = (
                    "an earlier step did not report the id this step needs, so it was "
                    "not run"
                )
                session.status = "failed"
                session.summary = self._summary(session)
                return {"session": session, "references": references}
            target = self._target_for(session, step)
            if target is None:
                # Refused *before* an execution exists. There is nothing to name, so
                # nothing is dispatched and nothing can be attributed to the wrong
                # machine: a step with no resolvable computer is a denied step, not a
                # local one. This is the one refusal the orchestrator makes itself,
                # and it only ever subtracts a target.
                step.state = "denied"
                step.error = (
                    "no computer is selected for this step; select one with "
                    "'switch to <computer>' and run it again"
                )
                session.status = "failed"
                session.summary = self._summary(session)
                return {"session": session, "references": references}
            result = await self._run_step(session, step, context, checkpoint)
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
            if step.analysis:
                # An analysis step is a *place* for an explanation, not a capability:
                # the diagnose route fills it in from evidence an earlier read produced,
                # and the orchestrator must never dispatch it.
                continue
            if step.state not in {"running", "awaiting_approval"} or not step.execution_id:
                continue
            record = self.engine.record(step.execution_id)
            if record is not None:
                self._apply_record(session, step, record)
        settled = all(step.settled or step.analysis for step in session.steps)
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

    async def _run_step(
        self,
        session: TaskSession,
        step: TaskStep,
        context: PolicyContext,
        checkpoint: Callable[[TaskSession], None] | None,
    ) -> ExecutionResult:
        """Name the execution, record the name on the step, *then* run it.

        The two calls are the engine's own halves, not a reimplementation of them:
        ``prepare_execution`` authorizes and names, ``run_execution`` authorizes again
        and dispatches. All this adds is the write in between -- and that write is what
        makes the step findable after a crash.
        """
        graph, step_context = await self.engine.prepare_execution(
            self._graph(session, step), self._context_for(context, step)
        )
        step.execution_id = graph.graph_id
        step.state = "running"
        session.touch()
        if checkpoint is not None:
            checkpoint(session)
        result = await self.engine.run_execution(graph, step_context)
        return result

    def _target_for(self, session: TaskSession, step: TaskStep) -> str | None:
        """The id this step is addressed to; ``None`` when no computer is selected.

        A named target is passed through unchanged: *which* target is legitimate is
        the executor's resolution, not this layer's, and it must stay that way so a
        client-supplied id cannot gain anything by reaching VoiceOS first. What this
        method decides is only whether the step has a target *at all*.

        Two honest sources are accepted: the step's own id (an operator plan that
        names machines) or the session's id (the user's selected computer). The
        session and step are stamped so the answer is durable, but a step that
        already names a computer is never rewritten by a later selection.

        ``""`` is returned — meaning "this machine" — only when there is no device
        plane at all, or a device plane with nothing on it that could execute. That is
        the pre-C36 single-laptop shape, where local is the *only* meaning available.
        Once an execution device exists, an unselected session is a refusal, never an
        implicit local run: that implicit run is exactly the silent targeting C36
        forbids.
        """
        target = (step.device_id or session.device_id or "").strip()
        if not target:
            if self.engine.devices is None:
                return ""
            registry = self.engine.devices
            if not any(
                device.kind in EXECUTION_KINDS for device in registry.all_devices()
            ):
                return ""
            return None
        if not step.device_id:
            step.device_id = target
        if not session.device_id:
            session.device_id = target
        return target

    def _graph(self, session: TaskSession, step: TaskStep) -> Any:
        # Built through the same deserialisation and validation an HTTP body passes
        # through, so a step cannot be "more trusted" than a typed request.
        target = step.device_id or session.device_id
        return graph_from_dict(
            {
                "intent": step.label,
                # C36: the routing request. The executor resolves it against the
                # registry and refuses an unknown, offline, revoked or out-of-role
                # device -- so naming a target here aims the step, it does not
                # authorise it. Empty means "this machine", as it always has.
                "target_device_id": target,
                "nodes": [
                    {
                        "node_id": "s1",
                        "action": {
                            "type": step.action_type,
                            "provider": self._provider_for(step.action_type),
                            "parameters": dict(step.parameters),
                            **self._declared_risk(step.action_type),
                        },
                    },
                    {"node_id": "send", "node_type": "end", "depends_on": ["s1"]},
                ],
            }
        )

    def _declared_risk(self, action_type: str) -> dict[str, str]:
        """The risk the registry declares for this action, as the action's own field.

        Omitting it does not mean "low" or "whatever the definition says": an
        ``Action`` left to its default carries ``MEDIUM``, and the registry's
        ``max(action, definition)`` rule may only raise, never lower, so a
        ``LOW``-risk read becomes a medium-risk one by accident. Every device ships
        with a ``LOW`` ceiling, so that alone refuses the read before the provider is
        even consulted -- a policy denial produced entirely by a missing field.

        Naming the registered risk keeps the lift rule doing its job (a definition
        that really is MEDIUM still raises a step that claims LOW) while stopping it
        from inventing a raise that no definition asked for. An unregistered action
        contributes nothing here and is refused by resolution, as it should be.
        """
        try:
            return {"risk_level": self.engine.registry.get(action_type).risk_level.value}
        except Exception:
            # Unknown to the registry: resolution refuses it with the real reason a
            # moment later, which is a better message than anything this could invent.
            return {}

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
            if key == "state":
                actual_state = earlier.state
                if expected == "success":
                    # Templates name the executor's outcome, not the provider's
                    # ``status`` field. A step only reaches this branch after the
                    # executor returned SUCCESS, which the orchestrator records
                    # as ``succeeded`` (verified) or ``succeeded_unverified``.
                    # Anything else -- failed, denied, cancelled, skipped -- did
                    # not succeed, however the provider worded its own result.
                    if actual_state not in {"succeeded", "succeeded_unverified"}:
                        return False
                elif actual_state != expected and observed.get(key) != expected:
                    return False
                continue
            if key == "verified":
                if earlier.verified != expected:
                    return False
                continue
            actual = observed.get(key)
            if actual != expected:
                return False
        return True

    def _bind_parameters(self, session: TaskSession, step: TaskStep) -> bool:
        """Fill a step's id parameters from what an earlier step actually observed.

        ``bind`` is ``{parameter: {"from_step": int, "key": str, "field": str}}`` and
        nothing more. Resolving it means reading one field of one observation the
        executor already made: there is no expression to evaluate, no arithmetic, no
        path into the environment. That narrowness is the point — a substitution
        language general enough to be useful would also be general enough to compute a
        value nobody observed, and this layer's job is to *repeat* evidence, not
        produce it.

        ``False`` means the earlier step did not report what this step needs. The
        caller denies the step rather than running it against a guessed id. An
        already-concrete parameter is left alone, so a step that was bound and then
        parked keeps the id it was bound to instead of silently re-resolving to a
        different process after the user finally presses approve.
        """
        for name, spec in step.bind.items():
            if name in step.parameters:
                continue
            if not isinstance(spec, dict):
                return False
            try:
                index = int(spec.get("from_step", -1))
            except (TypeError, ValueError):
                return False
            earlier = session.step(index)
            if earlier is None:
                return False
            value: Any = earlier.observed.get(str(spec.get("key") or ""))
            if isinstance(value, list):
                # An observation that listed several things is only safe to bind from
                # when it listed exactly the one the step is about. Picking "the first"
                # of several would be a choice this layer is not entitled to make on
                # the user's behalf.
                if len(value) != 1:
                    return False
                value = value[0]
            if isinstance(value, dict):
                value = value.get(str(spec.get("field") or ""))
            if value is None or value == "":
                return False
            # Browser refs are opaque integers minted by the browser layer; the
            # registry schema for browser.* payloads declares every ``ref``
            # parameter as a string.  Cast a digit-only integer observation to
            # str here so the plan stays schema-correct; the browser engine's
            # ``_resolve`` re-parses it with ``int(ref)``.
            if name == "ref" and isinstance(value, int) and not isinstance(value, bool):
                value = str(value)
            step.parameters[name] = value
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
        # An analysis step is not work the orchestrator runs, so it is not counted in
        # "steps settled": it is filled in afterwards by the diagnose route, and the
        # last thing it should do is make a finished run look unfinished.
        analysed = len([step for step in session.steps if step.analysis])
        total = len(session.steps) - analysed
        unverified = [step.index + 1 for step in session.steps if step.verified is False]
        parts = [f"{len(settled)} of {total} steps settled."]
        if analysed:
            parts.append("The explanation step is filled in when you ask for it.")
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
