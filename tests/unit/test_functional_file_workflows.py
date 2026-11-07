"""Real temporary-file journeys; only the OS trash transport is a fixture."""

import asyncio
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from actionmesh.agent.definitions import register_device_actions
from actionmesh.agent.files import MAX_WRITE_BYTES, FilesEngine
from actionmesh.agent.paths import Workspace
from actionmesh.agent.provider import LaptopProvider
from actionmesh.agent.verification import register_device_strategies
from actionmesh.core.executor import ExecutionEngine
from actionmesh.core.graph import GraphBuilder
from actionmesh.core.permissions import (
    PermissionGrant,
    PolicyContext,
    PolicyEngine,
    policy_from_rules,
)
from actionmesh.core.registry import ActionRegistry
from actionmesh.core.storage import SQLiteStore
from actionmesh.core.verification import default_engine
from actionmesh.protocol.models import Action, ApprovalRequest, Identity, RiskLevel
from actionmesh.providers import ProviderError


def verifier():
    engine = default_engine()
    register_device_strategies(engine)
    return engine


def test_complete_file_journey_through_authorization_execution_and_verification() -> None:
    async def journey(root: str) -> None:
        registry = ActionRegistry.with_seeds()
        register_device_actions(registry)
        provider = LaptopProvider(Workspace(root))
        engine = ExecutionEngine(registry, PolicyEngine(), [provider])
        register_device_strategies(engine.verifier)
        steps = [
            ("create", {"path": "start.txt", "content": "hello world"}),
            ("rename", {"path": "start.txt", "to": "renamed.txt"}),
            ("copy", {"path": "renamed.txt", "to": "copy.txt"}),
            ("edit", {"path": "copy.txt", "find": "world", "replace": "ActionMesh"}),
            ("move", {"path": "copy.txt", "to": "done.txt"}),
            ("read", {"path": "done.txt"}),
            ("trash", {"path": "done.txt"}),
        ]
        for verb, parameters in steps:
            action = Action(type=f"device.files.{verb}", provider="laptop", parameters=parameters)
            definition = registry.get(action.type)
            context = PolicyContext(actor=Identity(id="file-test"),
                grants=[PermissionGrant(scope=scope, max_risk=RiskLevel.HIGH) for scope in definition.permissions],
                policy=policy_from_rules([{"action": "*", "decision": "allow"}]))
            builder = GraphBuilder(verb)
            builder.action(action, node_id="operation")
            builder.end(depends_on=["operation"])
            graph = builder.build()
            result = await engine.execute(graph, context)
            if result.state.value == "waiting_permission":
                engine.submit_approval(ApprovalRequest(action_id=action.action_id, status="approved"))
                result = await engine.resume(graph.graph_id)
            assert result.state.value == "success", (verb, result.error)
            assert result.nodes["operation"].verified, verb
        assert Path(root, "renamed.txt").read_text() == "hello world"
        assert not Path(root, "done.txt").exists()

    def trash(_backend: str, absolute: str):
        Path(absolute).unlink()
        return CompletedProcess([], 0)

    with tempfile.TemporaryDirectory() as root, \
         patch.object(FilesEngine, "_trash_backend", return_value="/fixture/gio"), \
         patch.object(FilesEngine, "_run_trash", side_effect=trash):
        asyncio.run(journey(root))


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_copy_verifies_destination_and_source(damage: str) -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "source.txt").write_text("original")
        provider = LaptopProvider(Workspace(root))
        action = Action(type="device.files.copy", provider="laptop", parameters={"path": "source.txt", "to": "copy.txt"})
        result = asyncio.run(provider.execute(action))
        assert verifier().verify(action, result, provider).verified
        if damage == "missing":
            Path(root, "copy.txt").unlink()
        else:
            Path(root, "copy.txt").write_text("damaged")
        assert not verifier().verify(action, result, provider).verified


def test_edit_does_not_verify_later_unrelated_content() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "note.txt").write_text("hello")
        provider = LaptopProvider(Workspace(root))
        action = Action(type="device.files.edit", provider="laptop", parameters={"path": "note.txt", "find": "hello", "replace": "updated"})
        result = asyncio.run(provider.execute(action))
        assert verifier().verify(action, result, provider).verified
        Path(root, "note.txt").write_text("unrelated")
        assert not verifier().verify(action, result, provider).verified


def test_restart_rechecks_file_evidence_against_disk() -> None:
    with tempfile.TemporaryDirectory() as root:
        store = SQLiteStore()
        provider = LaptopProvider(Workspace(root), ledger=store)
        action = Action(type="device.files.create", provider="laptop", parameters={"path": "note.txt", "content": "expected"})
        result = asyncio.run(provider.execute(action))
        fresh = LaptopProvider(Workspace(root), ledger=store)
        assert verifier().verify(action, result, fresh).verified
        Path(root, "note.txt").write_text("changed")
        assert not verifier().verify(action, result, fresh).verified
        store.close()


def test_unavailable_trash_never_records_a_successful_effect() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "note.txt").write_text("keep")
        provider = LaptopProvider(Workspace(root))
        action = Action(type="device.files.trash", provider="laptop", parameters={"path": "note.txt"})
        with patch.object(FilesEngine, "_trash_backend", return_value=None):
            result = asyncio.run(provider.execute(action))
        assert Path(root, "note.txt").read_text() == "keep"
        assert action.action_id not in provider._effects
        assert not verifier().verify(action, result, provider).verified


def test_create_does_not_overwrite_an_existing_file() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "note.txt").write_text("keep")
        with pytest.raises(ProviderError):
            FilesEngine(Workspace(root)).create("note.txt", "replacement")
        assert Path(root, "note.txt").read_text() == "keep"


def test_oversized_copy_creates_no_partial_destination() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "large.bin").write_bytes(b"x" * (MAX_WRITE_BYTES + 1))
        with pytest.raises(ProviderError):
            FilesEngine(Workspace(root)).copy("large.bin", "partial.bin")
        assert not Path(root, "partial.bin").exists()
