"""C39 — automations: a stored plan, and every way it can be dishonest.

An automation is the durable form of a task session. The work order's constraints are
all of one kind — *do not claim what did not happen* — so these tests are about the
places where a claim could be manufactured:

* **storing a plan is not granting anything.** The shape has no grants field, the run
  route stamps the real owner, and the policy engine denies a step with no consent
  exactly as it would from a sentence typed a moment ago.
* **an occurrence runs at most once, across restarts.** The slot is the due time, the
  store's unique constraint is the guard, and a claim whose process died is recorded
  ``missed`` — never silently re-run.
* **a late occurrence is missed, not run late.** A laptop that slept through 09:00 and
  woke at noon is told so; it is not handed a run it did not schedule.
* **a computer is aimed at, never redirected.** A step naming an offline laptop is
  unavailable, and no other machine receives the action. A target the caller resolved
  never overwrites one the step named.
* **a condition is evidence.** ``when`` may only name an observed key on an earlier
  step; there is no expression language and no model in the path.
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.executor import ExecutionEngine
from actionmesh.core.identity import IdentityStore
from actionmesh.core.permissions import PolicyEngine
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import DeviceKind, RiskLevel
from actionmesh.providers import MockProvider
from actionmesh.voiceos.automation_run import (
    Runner,
    run_state_for,
    schedule_next,
    slot_for_schedule,
)
from actionmesh.voiceos.automation_scheduler import (
    TRIGGERABLE_EVENTS,
    Scheduler,
)
from actionmesh.voiceos.automation_store import AutomationStore
from actionmesh.voiceos.models import TaskSession, TaskStep, _now
from actionmesh.voiceos.orchestrator import Orchestrator
from actionmesh.voiceos.workflow_draft import draft, draft_to_body
from actionmesh.voiceos.workflows import (
    AutomationRun,
    Workflow,
    WorkflowError,
    next_occurrence,
    validate_event,
    validate_schedule,
    validate_workflow,
)
from tests.unit.test_executor import run

LOCAL = ("127.0.0.1", 45123)
REMOTE = ("192.168.1.50", 55555)

def _clean_env(test: unittest.TestCase) -> None:
    for name in (
        "ACTIONMESH_API_KEY",
        "ACTIONMESH_ALLOW_REMOTE_PAIRING",
        "ACTIONMESH_SECRET_KEY",
        "ACTIONMESH_SECRET_FILE",
        "ACTIONMESH_VOICEOS_DB",
        "ACTIONMESH_VOICEOS_TASKS",
        "ACTIONMESH_VOICEOS_LLM_PROVIDER",
        "ACTIONMESH_AUTOMATIONS_DB",
    ):
        test.addCleanup(os.environ.pop, name, None)
        os.environ.pop(name, None)

def known(action_type: str) -> bool:
    return action_type in {"message.send", "reminder.create", "device.process.list"}

#: The parameters each seeded action actually requires. A test that names an action
#: with the wrong shape fails in the registry rather than in policy, which would make
#: a "denied" assertion pass for the wrong reason.
_PARAMS: dict[str, dict[str, object]] = {
    "message.send": {"message": "hello"},
    "reminder.create": {"text": "x"},
    "device.process.list": {},
}

def step(action: str = "reminder.create", **extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "label": f"do {action}",
        "action": action,
        "parameters": dict(_PARAMS.get(action, {})),
    }
    body.update(extra)
    return body

def workflow(**overrides: object) -> Workflow:
    body: dict[str, object] = {"name": "nightly", "steps": [step()]}
    body.update(overrides)
    return validate_workflow(body, actor_id="owner", known_action=known)

# ---------------------------------------------------------------------------
# what may be stored
# ---------------------------------------------------------------------------

class TestWhatMayBeStored(unittest.TestCase):
    def test_a_plan_with_an_unregistered_action_is_refused_at_save(self) -> None:
        """A plan naming an action this machine cannot run must fail now, not at 07:00."""
        with self.assertRaises(WorkflowError) as raised:
            validate_workflow(
                {"name": "x", "steps": [step("device.teleport")]},
                actor_id="owner",
                known_action=known,
            )
        self.assertIn("unregistered action", str(raised.exception))

    def test_a_plan_with_no_steps_is_refused(self) -> None:
        with self.assertRaises(WorkflowError):
            validate_workflow({"name": "x", "steps": []}, actor_id="owner", known_action=known)

    def test_a_plan_with_no_name_is_refused(self) -> None:
        with self.assertRaises(WorkflowError):
            validate_workflow({"steps": [step()]}, actor_id="owner", known_action=known)

    def test_a_condition_on_a_later_step_is_refused(self) -> None:
        """A step cannot read the future; "if the next step worked" is not evidence."""
        with self.assertRaises(WorkflowError) as raised:
            validate_workflow(
                {
                    "name": "x",
                    "steps": [
                        step(when={"step": 1, "exit_code": 0}),
                        step("message.send"),
                    ],
                },
                actor_id="owner",
                known_action=known,
            )
        self.assertIn("not earlier than it", str(raised.exception))

    def test_a_condition_on_something_not_observed_is_refused(self) -> None:
        with self.assertRaises(WorkflowError) as raised:
            validate_workflow(
                {
                    "name": "x",
                    "steps": [
                        step(),
                        step("message.send", when={"step": 0, "vibes": "good"}),
                    ],
                },
                actor_id="owner",
                known_action=known,
            )
        self.assertIn("not observed evidence", str(raised.exception))

    def test_a_nested_parameter_is_refused(self) -> None:
        with self.assertRaises(WorkflowError):
            validate_workflow(
                {"name": "x", "steps": [step(parameters={"text": {"deep": "object"}})]},
                actor_id="owner",
                known_action=known,
            )

    def test_a_workflow_has_no_field_a_grant_could_live_in(self) -> None:
        """The shape itself is the argument: there is nowhere to put a permission."""
        stored = set(workflow().to_dict())
        for forbidden in ("grants", "permissions", "approval", "credential", "policy", "max_risk"):
            self.assertNotIn(forbidden, stored)

    def test_two_triggers_on_one_plan_is_ambiguous_and_refused(self) -> None:
        with self.assertRaises(WorkflowError) as raised:
            workflow(
                schedule={"kind": "daily", "at": "09:00"},
                event={"event_type": "execution.completed"},
            )
        self.assertIn("not both", str(raised.exception))

class TestScheduleValidation(unittest.TestCase):
    def test_a_busy_loop_interval_is_refused(self) -> None:
        with self.assertRaises(WorkflowError):
            validate_schedule({"kind": "interval", "seconds": 5})

    def test_an_unrecognised_kind_is_refused(self) -> None:
        with self.assertRaises(WorkflowError) as raised:
            validate_schedule({"kind": "cron", "at": "09:00"})
        self.assertIn("not one of", str(raised.exception))

    def test_a_time_that_is_not_a_time_is_refused(self) -> None:
        with self.assertRaises(WorkflowError):
            validate_schedule({"kind": "daily", "at": "25:99"})
        with self.assertRaises(WorkflowError):
            validate_schedule({"kind": "daily", "at": "nine"})

    def test_an_empty_schedule_means_no_schedule(self) -> None:
        self.assertEqual(validate_schedule({}), {})
        self.assertEqual(validate_schedule(None), {})

    def test_weekly_days_are_sorted_and_deduplicated(self) -> None:
        self.assertEqual(
            validate_schedule({"kind": "weekly", "days": [4, 0, 4, 2], "at": "08:30"}),
            {"kind": "weekly", "days": [0, 2, 4], "at": "08:30"},
        )

class TestEventValidation(unittest.TestCase):
    def test_only_published_event_types_are_triggerable(self) -> None:
        """A trigger on an event nobody publishes would silently never fire."""
        with self.assertRaises(WorkflowError):
            validate_event({"event_type": "execution.failed"})

    def test_a_state_may_only_narrow_a_completion(self) -> None:
        with self.assertRaises(WorkflowError):
            validate_event({"event_type": "execution.cancelled", "state": "failed"})

    def test_a_state_must_be_one_the_executor_records(self) -> None:
        with self.assertRaises(WorkflowError):
            validate_event({"event_type": "execution.completed", "state": "happy"})

    def test_the_scheduler_may_only_start_a_closed_list(self) -> None:
        """Two closed lists must agree, or one of them is a lie."""
        self.assertTrue(TRIGGERABLE_EVENTS)
        for event_type in TRIGGERABLE_EVENTS:
            self.assertEqual(validate_event({"event_type": event_type})["event_type"], event_type)

# ---------------------------------------------------------------------------
# scheduling arithmetic
# ---------------------------------------------------------------------------

def local(*parts: int) -> datetime:
    """A wall-clock moment in this machine's timezone.

    ``next_occurrence`` deliberately works in local time — "every day at 09:00" means
    09:00 where the laptop is, not 09:00 UTC — so the arithmetic is exercised with
    local-aware inputs rather than pretending the test runs in UTC.
    """
    return datetime(*parts).astimezone()  # type: ignore[arg-type]

class TestSchedulingArithmetic(unittest.TestCase):
    def test_daily_after_its_time_is_tomorrow(self) -> None:
        after = local(2026, 11, 3, 10, 0)
        upcoming = next_occurrence({"kind": "daily", "at": "09:00"}, after)
        self.assertEqual(upcoming, local(2026, 11, 4, 9, 0))

    def test_daily_before_its_time_is_today(self) -> None:
        after = local(2026, 11, 3, 7, 0)
        upcoming = next_occurrence({"kind": "daily", "at": "09:00"}, after)
        self.assertEqual(upcoming, local(2026, 11, 3, 9, 0))

    def test_weekly_picks_the_next_named_day(self) -> None:
        # 2026-11-03 is a Tuesday (weekday 1); Monday is 0.
        after = local(2026, 11, 3, 10, 0)
        upcoming = next_occurrence({"kind": "weekly", "days": [0], "at": "08:00"}, after)
        self.assertEqual(upcoming, local(2026, 11, 9, 8, 0))

    def test_an_interval_recomputes_from_now(self) -> None:
        after = local(2026, 11, 3, 10, 0)
        upcoming = next_occurrence({"kind": "interval", "seconds": 900}, after)
        self.assertEqual(upcoming - after, timedelta(seconds=900))

    def test_a_disabled_workflow_has_no_next_run(self) -> None:
        item = workflow(schedule={"kind": "daily", "at": "09:00"}, enabled=False)
        schedule_next(item, now=local(2026, 11, 3, 8, 0))
        self.assertEqual(item.next_run_at, "")

    def test_schedule_next_writes_an_iso_timestamp(self) -> None:
        item = workflow(schedule={"kind": "daily", "at": "09:00"})
        schedule_next(item, now=local(2026, 11, 3, 8, 0))
        self.assertTrue(item.next_run_at.startswith("2026-11-03T09:00"))

    def test_the_slot_is_the_due_time_itself(self) -> None:
        """Two ticks that see the same due time must compute the same slot."""
        item = workflow(schedule={"kind": "daily", "at": "09:00"})
        due = local(2026, 11, 3, 9, 0)
        first = slot_for_schedule(item, due)
        second = slot_for_schedule(item, due)
        self.assertEqual(first, second)
        # The slot is the same instant in UTC — it is an identity, not a display string.
        self.assertEqual(first, due.astimezone(UTC).isoformat(timespec="seconds"))

# ---------------------------------------------------------------------------
# the store: one occurrence, once
# ---------------------------------------------------------------------------

def _run(workflow_id: str = "wf_1", slot: str = "2026-11-03T09:00:00+00:00") -> AutomationRun:
    # The run id is unique per workflow *and* slot: the primary key is the run row,
    # while ``(workflow_id, slot)`` is the occurrence constraint under test.
    stamp = f"{workflow_id}_{slot}".replace(":", "").replace("+", "").replace(" ", "")
    return AutomationRun(
        run_id=f"run_{stamp}",
        workflow_id=workflow_id,
        workflow_name="nightly",
        actor_id="owner",
        slot=slot,
        trigger="schedule",
        created_at=_now(),
    )

class TestTheStoreRunsAnOccurrenceOnce(unittest.TestCase):
    def setUp(self) -> None:
        self.store = AutomationStore(":memory:")
        self.addCleanup(self.store.close)

    def test_the_first_claim_wins_and_the_second_is_refused(self) -> None:
        self.assertTrue(self.store.claim(_run()))
        self.assertFalse(self.store.claim(_run(slot="2026-11-03T09:00:00+00:00")))

    def test_a_different_slot_is_a_different_occurrence(self) -> None:
        self.assertTrue(self.store.claim(_run()))
        self.assertTrue(self.store.claim(_run(slot="2026-11-04T09:00:00+00:00")))

    def test_a_different_workflow_may_use_the_same_slot(self) -> None:
        # A distinct run_id per workflow: the primary key is the run, and the
        # (workflow_id, slot) constraint is what keeps an occurrence single.
        self.assertTrue(self.store.claim(_run("wf_1")))
        self.assertTrue(self.store.claim(_run("wf_2", slot="2026-11-03T09:00:00+00:00")))

    def test_a_schema_version_outside_the_range_is_refused_before_it_reaches_sql(self) -> None:
        # The PRAGMA cannot take a bound parameter, so the value is interpolated. The
        # guard is what keeps that safe: only a small non-negative int gets through,
        # so no caller-controlled text can ever be formatted into the statement.
        for bad in (-1, 2**31, 2**63, True, 1.5, "3", "3; DROP TABLE automation_runs"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store._set_user_version(bad)
        self.assertEqual(self.store.schema_version(), self.store.schema_version())

    def test_a_claim_that_never_started_becomes_missed(self) -> None:
        claimed = _run()
        self.store.claim(claimed)
        missed = self.store.reconcile_claimed()
        self.assertEqual([run_.run_id for run_ in missed], [claimed.run_id])
        stored = self.store.get_run(claimed.run_id)
        self.assertEqual(stored.state, "missed")
        self.assertIn("before starting it", stored.error)
        self.assertIn("it was not run", stored.error)

    def test_reconciliation_is_idempotent(self) -> None:
        self.store.claim(_run())
        self.assertEqual(len(self.store.reconcile_claimed()), 1)
        self.assertEqual(self.store.reconcile_claimed(), [], "a missed run is settled")

    def test_only_a_claimed_run_is_reconciled(self) -> None:
        """A run that finished is not rewritten by a restart."""
        finished = _run()
        self.store.claim(finished)
        finished.state = "succeeded"
        self.store.save_run(finished)
        self.assertEqual(self.store.reconcile_claimed(), [])
        self.assertEqual(self.store.get_run(finished.run_id).state, "succeeded")

    def test_due_workflows_excludes_disabled_and_future(self) -> None:
        due = workflow(schedule={"kind": "daily", "at": "09:00"})
        due.next_run_at = "2026-11-03T09:00:00+00:00"
        self.store.save_workflow(due)

        later = workflow(schedule={"kind": "daily", "at": "10:00"})
        later.next_run_at = "2027-01-01T00:00:00+00:00"
        self.store.save_workflow(later)

        off = workflow(schedule={"kind": "daily", "at": "09:00"}, enabled=False)
        off.next_run_at = "2026-11-03T09:00:00+00:00"
        self.store.save_workflow(off)

        found = self.store.due_workflows("2026-11-03T12:00:00+00:00")
        self.assertEqual([w.workflow_id for w in found], [due.workflow_id])

    def test_workflows_are_scoped_by_actor(self) -> None:
        mine = workflow()
        mine.actor_id = "owner"
        self.store.save_workflow(mine)
        theirs = workflow()
        theirs.actor_id = "someone"
        self.store.save_workflow(theirs)
        self.assertEqual([w.actor_id for w in self.store.list_workflows("owner")], ["owner"])
        self.assertEqual(len(self.store.list_workflows(None)), 2)

# ---------------------------------------------------------------------------
# a run state never claims more than happened
# ---------------------------------------------------------------------------

class TestRunStateIsTheTruth(unittest.TestCase):
    def _session(self, *states: str, status: str = "completed") -> TaskSession:
        return TaskSession(
            session_id="ses_1",
            actor_id="owner",
            request="r",
            steps=[
                TaskStep(index=i, label=f"s{i}", action_type="reminder.create", state=state)
                for i, state in enumerate(states)
            ],
            status=status,
        )

    def test_every_step_succeeding_is_succeeded(self) -> None:
        self.assertEqual(run_state_for(self._session("succeeded")), "succeeded")

    def test_a_skipped_step_makes_the_run_partial(self) -> None:
        self.assertEqual(run_state_for(self._session("succeeded", "skipped")), "partial")

    def test_an_unverified_step_is_not_a_success(self) -> None:
        self.assertEqual(
            run_state_for(self._session("succeeded", "succeeded_unverified")), "partial"
        )

    def test_awaiting_approval_is_not_a_failure(self) -> None:
        session = self._session("awaiting_approval", status="awaiting_approval")
        self.assertEqual(run_state_for(session), "awaiting_approval")

    def test_a_denied_step_reports_denied(self) -> None:
        self.assertEqual(run_state_for(self._session("succeeded", "denied", status="failed")), "denied")

    def test_a_failed_step_reports_failed(self) -> None:
        self.assertEqual(run_state_for(self._session("failed", status="failed")), "failed")

    def test_an_interrupted_session_reports_interrupted(self) -> None:
        self.assertEqual(
            run_state_for(self._session("interrupted", status="interrupted")), "interrupted"
        )

# ---------------------------------------------------------------------------
# the runner: a scheduled run has no grants
# ---------------------------------------------------------------------------

class _Engine(unittest.TestCase):
    """One engine, one mock provider, optionally a device plane."""

    def setUp(self) -> None:
        self.identities = IdentityStore()
        self.devices = DeviceRegistry(identities=self.identities)
        self.provider = MockProvider()
        self.engine = ExecutionEngine(
            ActionRegistry.with_seeds(),
            PolicyEngine(),
            [self.provider],
            devices=self.devices,
        )
        self.local_id = "laptop-1"
        self.devices.enroll(
            device_id=self.local_id,
            kind=DeviceKind.LAPTOP,
            name="Laptop",
            platform="linux",
            capabilities=["message.send", "reminder.create"],
            risk_ceiling=RiskLevel.HIGH,
        )
        self.devices.heartbeat(self.local_id)
        self.engine.local_device_id = self.local_id
        self.orchestrator = Orchestrator(engine=self.engine)
        self.runner = Runner(orchestrator=self.orchestrator)

class TestScheduledRunsGrantNothing(_Engine):
    def test_a_background_run_carries_no_grants(self) -> None:
        item = workflow(steps=[step("message.send")])
        item.device_id = self.local_id
        automation_run = _run()

        result = run(self.runner.execute(item, automation_run))

        self.assertEqual(result.state, "denied")
        self.assertEqual(self.provider.calls, [], "nothing may run without consent")
        self.assertIn("missing permission(s)", result.error)

    def test_a_manual_run_with_a_grants_context_can_run(self) -> None:
        from actionmesh.core.permissions import PermissionGrant, PolicyContext
        from actionmesh.protocol.models import Identity, Policy

        item = workflow(steps=[step("message.send")])
        item.device_id = self.local_id
        context = PolicyContext(
            actor=Identity(id="owner"),
            policy=Policy(),
            grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
        )

        result = run(self.runner.execute(item, _run(), context=context))

        self.assertEqual(result.state, "succeeded")
        self.assertEqual(len(self.provider.calls), 1)

    def test_scheduling_a_workflow_does_not_add_a_grant(self) -> None:
        """The same plan, run twice — a stored schedule is not an authorisation."""
        item = workflow(steps=[step("message.send")], schedule={"kind": "daily", "at": "09:00"})
        item.device_id = self.local_id
        self.assertEqual(run(self.runner.execute(item, _run())).state, "denied")
        self.assertEqual(run(self.runner.execute(item, _run(slot="later"))).state, "denied")

# ---------------------------------------------------------------------------
# multi-device: aimed, never redirected
# ---------------------------------------------------------------------------

class TestADeviceIsAimedAtNeverRedirected(_Engine):
    def other_laptop(self, *, online: bool = True) -> str:
        device_id = "laptop-2"
        self.devices.enroll(
            device_id=device_id,
            kind=DeviceKind.LAPTOP,
            name="Spare",
            platform="linux",
            capabilities=["message.send"],
            risk_ceiling=RiskLevel.LOW,
        )
        if online:
            self.devices.heartbeat(device_id)
        return device_id

    def _context(self):
        from actionmesh.core.permissions import PermissionGrant, PolicyContext
        from actionmesh.protocol.models import Identity, Policy

        return PolicyContext(
            actor=Identity(id="owner"),
            policy=Policy(),
            grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
        )

    def test_an_offline_step_is_unavailable_and_nothing_is_sent_elsewhere(self) -> None:
        offline = self.other_laptop(online=False)
        item = workflow(steps=[step("message.send", device_id=offline)])
        item.device_id = self.local_id

        result = run(self.runner.execute(item, _run(), context=self._context()))

        # The engine refuses the *target*, so the run state is ``denied``: no permission
        # question arose, the chosen computer simply was not there.
        self.assertEqual(result.state, "denied")
        self.assertEqual(self.provider.calls, [], "an offline target is not routed elsewhere")
        self.assertIn("offline", result.error)
        self.assertIn("not being sent to another computer", result.error)
        self.assertEqual(result.steps[0]["device_id"], offline)

    def test_a_default_target_never_overwrites_a_step_that_named_one(self) -> None:
        """The caller's resolved default is a fallback, not an override."""
        offline = self.other_laptop(online=False)
        item = workflow(steps=[step("message.send", device_id=offline)])
        item.device_id = ""  # the plan names nothing at the workflow level

        session = self.runner.build_session(
            item, _run(), default_device_id=self.local_id
        )

        self.assertEqual(session.device_id, self.local_id)
        self.assertEqual(
            session.steps[0].device_id, offline,
            "a step that names its own computer keeps it",
        )

    def test_a_default_target_fills_an_unaimed_plan(self) -> None:
        item = workflow(steps=[step("message.send")])
        session = self.runner.build_session(item, _run(), default_device_id=self.local_id)
        self.assertEqual(session.steps[0].device_id, self.local_id)

    def test_no_default_leaves_the_step_unaimed_and_the_orchestrator_refuses(self) -> None:
        """A scheduler tick is not an operator: it must not pick a machine."""
        item = workflow(steps=[step("message.send")])

        result = run(self.runner.execute(item, _run(), context=self._context()))

        self.assertEqual(result.state, "denied")
        self.assertEqual(self.provider.calls, [])
        self.assertIn("no computer is selected", result.error)

    def test_a_workflow_naming_its_own_device_is_honoured(self) -> None:
        item = workflow(steps=[step("message.send")])
        item.device_id = self.other_laptop(online=True)

        session = self.runner.build_session(item, _run(), default_device_id=self.local_id)
        self.assertEqual(session.device_id, "laptop-2")

    def test_several_online_computers_are_not_guessed(self) -> None:
        """Two reachable laptops and no selection is a question, not a coin toss."""
        self.other_laptop(online=True)
        item = workflow(steps=[step("message.send")])

        result = run(self.runner.execute(item, _run(), context=self._context()))

        self.assertEqual(result.state, "denied")
        self.assertEqual(self.provider.calls, [])
        self.assertIn("no computer is selected", result.error)

# ---------------------------------------------------------------------------
# the scheduler: a tick does what it says
# ---------------------------------------------------------------------------

class TestTheTicker(_Engine):
    def setUp(self) -> None:
        super().setUp()
        self.store = AutomationStore(":memory:")
        self.addCleanup(self.store.close)
        self.scheduler = Scheduler(store=self.store, runner=self.runner)

    def _due(self, **overrides: object) -> Workflow:
        item = workflow(schedule={"kind": "daily", "at": "09:00"}, **overrides)
        item.device_id = self.local_id
        item.next_run_at = "2026-11-03T09:00:00+00:00"
        self.store.save_workflow(item)
        return item

    def test_a_missed_occurrence_is_recorded_once_and_not_run_late(self) -> None:
        item = self._due()
        now = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)  # six hours late

        started = run(self.scheduler.tick(now=now))

        self.assertEqual([r.state for r in started], ["missed"])
        self.assertEqual(self.provider.calls, [], "a missed occurrence is not run")
        self.assertIn("was missed", started[0].error)
        stored = self.store.get_workflow(item.workflow_id)
        self.assertNotEqual(stored.next_run_at, "2026-11-03T09:00:00+00:00")

    def test_a_second_tick_does_not_run_the_same_occurrence_again(self) -> None:
        item = self._due()
        now = datetime(2026, 11, 3, 9, 1, tzinfo=UTC)

        first = run(self.scheduler.tick(now=now))
        second = run(self.scheduler.tick(now=now))

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "the slot is taken")
        self.assertEqual(len(self.store.list_runs(workflow_id=item.workflow_id)), 1)

    def test_the_store_refuses_a_second_run_for_the_same_slot(self) -> None:
        """The guard is the database constraint, not a lookup-then-insert."""
        item = self._due()
        run_a = _run(item.workflow_id, "2026-11-03T09:00:00+00:00")
        self.assertTrue(self.store.claim(run_a))
        self.assertFalse(self.store.claim(_run(item.workflow_id, "2026-11-03T09:00:00+00:00")))

    def test_the_scheduler_advances_the_schedule_before_running(self) -> None:
        """A crash mid-run cannot leave the same due time for a restart to claim."""
        item = self._due()
        run(self.scheduler.tick(now=datetime(2026, 11, 3, 9, 1, tzinfo=UTC)))
        stored = self.store.get_workflow(item.workflow_id)
        self.assertNotEqual(stored.next_run_at, "2026-11-03T09:00:00+00:00")

    def test_an_event_trigger_matches_its_own_owner_only(self) -> None:
        item = workflow(event={"event_type": "execution.completed"})
        item.actor_id = "owner"
        item.device_id = self.local_id
        self.store.save_workflow(item)

        started = run(
            self.scheduler.on_event(
                "execution.completed", event_id="evt_1", actor="someone-else"
            )
        )
        self.assertEqual(started, [], "an event cannot start another actor's workflow")

    def test_an_unpublished_event_type_starts_nothing(self) -> None:
        item = workflow(event={"event_type": "execution.completed"})
        item.device_id = self.local_id
        self.store.save_workflow(item)
        self.assertEqual(
            run(self.scheduler.on_event("execution.failed", event_id="e", actor="owner")), []
        )

    def test_an_event_starts_its_workflow_once(self) -> None:
        item = workflow(event={"event_type": "execution.completed"})
        item.actor_id = "owner"
        item.device_id = self.local_id
        self.store.save_workflow(item)

        first = run(self.scheduler.on_event("execution.completed", event_id="evt_1", actor="owner"))
        second = run(self.scheduler.on_event("execution.completed", event_id="evt_1", actor="owner"))

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "one event, one run, however many subscribers see it")

    def test_a_run_nobody_granted_is_denied_not_run(self) -> None:
        self._due(steps=[step("message.send")])
        started = run(self.scheduler.tick(now=datetime(2026, 11, 3, 9, 1, tzinfo=UTC)))
        self.assertEqual([r.state for r in started], ["denied"])
        self.assertEqual(self.provider.calls, [])

    def test_run_now_is_not_suppressed_by_a_slot(self) -> None:
        """A human pressing run twice gets two runs; slots guard occurrences."""
        item = self._due()
        first = run(self.scheduler.run_now(item))
        second = run(self.scheduler.run_now(item))
        self.assertNotEqual(first.run_id, second.run_id)

# ---------------------------------------------------------------------------
# drafts: a proposal and nothing more
# ---------------------------------------------------------------------------

class TestADraftCreatesNothing(unittest.TestCase):
    def test_a_draft_is_never_marked_created(self) -> None:
        out = draft("every day at 9am check the running processes", templates=())
        self.assertFalse(out.created)
        self.assertFalse(out.to_dict()["created"])

    def test_no_template_means_no_steps_and_a_reason(self) -> None:
        out = draft("every day at 9am do something clever", templates=())
        self.assertEqual(out.steps, [])
        self.assertEqual(out.source, "none")
        self.assertTrue(out.reason, "an empty plan has to say why it is empty")

    def test_a_schedule_is_recognised_without_a_model(self) -> None:
        out = draft("every day at 9am check the disk", templates=())
        self.assertEqual(out.schedule, {"kind": "daily", "at": "09:00"})

    def test_the_body_a_draft_proposes_is_a_body_the_save_route_could_accept(self) -> None:
        out = draft("every day at 9am check the disk", templates=())
        body = draft_to_body(out)
        self.assertEqual(set(body), {"name", "steps", "schedule", "enabled"})
        self.assertIs(body["enabled"], False, "a draft proposes a stored plan, never an armed one")

    def test_an_overlong_sentence_is_refused_not_truncated(self) -> None:
        out = draft("x" * 5000, templates=())
        self.assertEqual(out.steps, [])
        self.assertIn("longer than", out.reason)

# ---------------------------------------------------------------------------
# the HTTP surface: scoping and the run route
# ---------------------------------------------------------------------------

class _AutomationApp(unittest.TestCase):
    """One laptop with two paired phones and the machine's own operator view."""

    def setUp(self) -> None:
        _clean_env(self)
        self.identities = IdentityStore()
        self.devices = DeviceRegistry(identities=self.identities)
        self.app = create_app(identities=self.identities, devices=self.devices)
        self.local = TestClient(self.app, client=LOCAL)
        self.remote = TestClient(self.app, client=REMOTE)
        self.devices.enroll(
            device_id="laptop-1",
            kind=DeviceKind.LAPTOP,
            name="Laptop",
            platform="linux",
            capabilities=["message.send", "reminder.create"],
            risk_ceiling=RiskLevel.HIGH,
        )
        self.devices.heartbeat("laptop-1")
        self.app.state.engine.local_device_id = "laptop-1"
        self.token_a = self._pair("phone-a")
        self.token_b = self._pair("phone-b")
        self.addCleanup(self._close)

    def _close(self) -> None:
        for name in ("automation_store_instance", "voiceos_store_instance"):
            store = getattr(self.app.state, name, None)
            if store is not None:
                store.close()

    def _pair(self, phone: str) -> str:
        grant = self.local.post(
            "/consents",
            json={
                "actor_id": phone,
                "scope": "device.control",
                "max_risk": "critical",
                "reason": "fixture consent before pairing",
            },
        )
        self.assertEqual(grant.status_code, 200, grant.text)
        code = self.local.post(
            "/pairing/code", json={"device_id": "laptop-1", "max_risk": "critical"}
        ).json()["code"]
        response = self.remote.post(
            "/pair", json={"code": code, "device_id": phone, "actor_id": phone}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def as_phone(self, token: str) -> TestClient:
        return TestClient(self.app, client=REMOTE, headers={"authorization": f"Bearer {token}"})

class TestTheAutomationSurface(_AutomationApp):
    def _save(self, client: TestClient, **body: object) -> dict:
        payload: dict[str, object] = {"name": "nightly", "steps": [step()]}
        payload.update(body)
        response = client.post("/automations", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_saving_a_plan_says_nothing_ran(self) -> None:
        saved = self._save(self.local)
        self.assertIn("Nothing has run", saved["runs_nothing"])
        self.assertEqual(self.local.get("/automations/runs").json()["runs"], [])

    def test_a_body_carrying_grants_is_refused(self) -> None:
        response = self.local.post(
            "/automations",
            json={"name": "x", "steps": [step()], "grants": [{"scope": "message.send"}]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot carry permissions", response.text)

    def test_an_automation_is_visible_only_to_its_owner(self) -> None:
        saved = self._save(self.as_phone(self.token_a))
        other = self.as_phone(self.token_b)
        self.assertEqual(other.get(f"/automations/{saved['workflow_id']}").status_code, 404)
        self.assertEqual(
            other.get("/automations").json()["workflows"],
            [],
            "another actor's library is not mine",
        )

    def test_an_edit_cannot_transfer_ownership(self) -> None:
        saved = self._save(self.as_phone(self.token_a))
        client_a = self.as_phone(self.token_a)
        response = client_a.put(
            f"/automations/{saved['workflow_id']}",
            json={"name": "renamed", "steps": [step()], "actor_id": "phone-b"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["actor_id"], "phone-a")

    def test_an_unknown_automation_is_a_404(self) -> None:
        self.assertEqual(self.local.get("/automations/wf_nope").status_code, 404)

    def test_an_unaimed_plan_pressed_locally_runs_on_this_laptop(self) -> None:
        """The operator pressing run is on the laptop; that is the honest reading."""
        saved = self._save(self.local, steps=[step("message.send")])
        result = self.local.post(f"/automations/{saved['workflow_id']}/run", json={}).json()
        self.assertEqual(result["state"], "denied", "no consent was ever granted")
        self.assertEqual(result["steps"][0]["device_id"], "laptop-1")

    def test_an_unaimed_press_from_a_phone_runs_as_that_device(self) -> None:
        client_a = self.as_phone(self.token_a)
        saved = self._save(client_a, steps=[step("message.send")])
        result = client_a.post(f"/automations/{saved['workflow_id']}/run", json={}).json()
        # The plan names no computer. The one declared execution device on this machine
        # is laptop-1, so that is the target the route offers — and the run is still an
        # honest denial, because no consent was ever granted for message.send.
        self.assertEqual(result["state"], "denied")
        self.assertEqual(result["steps"][0]["device_id"], "laptop-1")

    def test_a_run_route_requires_a_scope(self) -> None:
        saved = self._save(self.local)
        anonymous = TestClient(self.app, client=REMOTE)
        self.assertEqual(
            anonymous.post(f"/automations/{saved['workflow_id']}/run", json={}).status_code,
            401,
        )

    def test_disabling_says_what_it_does_not_do(self) -> None:
        saved = self._save(self.local, schedule={"kind": "daily", "at": "09:00"})
        response = self.local.post(
            f"/automations/{saved['workflow_id']}/enabled", json={"enabled": False}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("not stopped", response.json()["detail"])
        self.assertEqual(response.json()["next_run_at"], "")

    def test_deleting_a_plan_keeps_its_history(self) -> None:
        saved = self._save(self.local)
        self.local.post(f"/automations/{saved['workflow_id']}/run", json={})
        before = len(self.local.get("/automations/runs").json()["runs"])
        self.local.delete(f"/automations/{saved['workflow_id']}")
        after = len(self.local.get("/automations/runs").json()["runs"])
        self.assertEqual(before, after, "a run is a record, not a child of the plan")
        self.assertEqual(self.local.get(f"/automations/{saved['workflow_id']}").status_code, 404)

    def test_the_list_says_whether_the_ticker_is_alive(self) -> None:
        listed = self.local.get("/automations").json()
        self.assertIn("running", listed["scheduler"])
        self.assertIn("triggerable_events", listed["scheduler"])

    def test_a_press_updates_the_stored_last_run_the_list_renders(self) -> None:
        """The row's "last run" chip is only honest if the value it reads moved.

        The renderer re-fetches ``GET /automations`` after every press, so the contract
        this test pins is the server's: the workflow record itself carries the outcome
        of the most recent run, not a cached copy of the one before it.
        """
        saved = self._save(self.local, steps=[step("message.send")])
        self.assertEqual(saved["last_state"], "", "a new plan has no run to report")

        first = self.local.post(f"/automations/{saved['workflow_id']}/run", json={}).json()
        after_first = self.local.get(f"/automations/{saved['workflow_id']}").json()
        self.assertEqual(after_first["last_state"], first["state"])
        self.assertEqual(after_first["last_run_id"], first["run_id"])
        self.assertTrue(after_first["last_run_at"])

        # A second press must move the record again rather than leaving the first one.
        second = self.local.post(f"/automations/{saved['workflow_id']}/run", json={}).json()
        after_second = self.local.get(f"/automations/{saved['workflow_id']}").json()
        self.assertNotEqual(second["run_id"], first["run_id"])
        self.assertEqual(after_second["last_run_id"], second["run_id"])

    def test_the_history_keeps_both_presses(self) -> None:
        """A stored "last run" is a pointer; the runs themselves are not overwritten."""
        saved = self._save(self.local)
        self.local.post(f"/automations/{saved['workflow_id']}/run", json={})
        self.local.post(f"/automations/{saved['workflow_id']}/run", json={})
        runs = self.local.get(
            "/automations/runs", params={"workflow_id": saved["workflow_id"]}
        ).json()["runs"]
        self.assertEqual(len(runs), 2)

if __name__ == "__main__":
    unittest.main()