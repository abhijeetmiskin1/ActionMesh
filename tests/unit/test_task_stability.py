"""Duplicate task requests and simulation provenance must remain honest."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from actionmesh.protocol.models import ActionState
from actionmesh.voiceos.models import TaskSession, TaskStep
from actionmesh.voiceos.orchestrator import Orchestrator, SessionBusyError
from actionmesh.voiceos.reasoning import describe_session


def test_duplicate_task_advance_is_rejected_and_cancellation_releases_claim() -> None:
    async def scenario() -> None:
        orchestrator = Orchestrator(engine=None)
        session = TaskSession(session_id="same-task", actor_id="operator", request="test")
        entered = asyncio.Event()
        release = asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"session": session}
        with patch.object(orchestrator, "_advance", side_effect=slow):
            first = asyncio.create_task(orchestrator.advance(session, None))
            await entered.wait()
            with pytest.raises(SessionBusyError):
                await orchestrator.advance(session, None)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            assert (await orchestrator.advance(session, None))["session"] is session
    asyncio.run(scenario())


def test_simulation_provenance_survives_storage_and_is_never_narrated_as_real() -> None:
    step = TaskStep(index=0, label="send a message", action_type="message.send")
    session = TaskSession(session_id="simulation", actor_id="operator", request="test", steps=[step])
    result = SimpleNamespace(execution_id="execution", state=ActionState.SUCCESS, error="", nodes={
        "action": {"action_type": "message.send", "simulated": True, "verified": True, "result": {}}})
    orchestrator = Orchestrator(engine=None)
    orchestrator._apply_result(session, step, result)
    restored = TaskStep.from_dict(step.to_storage())
    assert restored.simulated is True
    session.steps = [restored]
    assert "Simulation only" in orchestrator._summary(session)
    assert "simulation only (no real-world effects)" in describe_session(session)
