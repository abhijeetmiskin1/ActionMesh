"""Behavioral regressions for independent, confined, read-only verification."""

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from actionmesh.agent.browser import BrowserEngine, FakeDriver
from actionmesh.agent.files import FilesEngine
from actionmesh.agent.media import FakeMediaDriver, MediaEngine
from actionmesh.agent.paths import Workspace
from actionmesh.agent.provider import LaptopProvider
from actionmesh.agent.verification import register_device_strategies
from actionmesh.core.registry import ActionRegistry, seed_definitions
from actionmesh.core.verification import VerificationEngine, default_engine
from actionmesh.protocol.models import Action, RiskLevel
from actionmesh.providers import ProviderResult


@pytest.mark.parametrize("ancestor", [False, True])
def test_observation_refuses_a_symlink_swap(ancestor: bool) -> None:
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
        directory = Path(root, "folder")
        directory.mkdir()
        Path(directory, "note.txt").write_text("inside")
        Path(outside, "note.txt").write_text("outside synthetic data")
        workspace = Workspace(root)
        provider = LaptopProvider(workspace)
        action = Action(type="device.file.read", provider="laptop",
                        parameters={"path": "folder/note.txt"})
        asyncio.run(provider.execute(action))
        assert provider.observe(action) is not None
        resolve = workspace.resolve

        def swap(path: object, **kwargs: object):
            resolved = resolve(path, **kwargs)
            if ancestor:
                directory.rename(Path(root, "original"))
                directory.symlink_to(outside, target_is_directory=True)
            else:
                Path(directory, "note.txt").unlink()
                Path(directory, "note.txt").symlink_to(Path(outside, "note.txt"))
            return resolved

        with patch.object(workspace, "resolve", side_effect=swap):
            assert provider.observe(action) is None


@pytest.mark.parametrize("observation", [{"parameters": {}}, {"parameters": {"other": 1}},
                                        {"idempotency_key": "key", "parameters": {}}])
def test_incomplete_observation_cannot_confirm(observation: dict) -> None:
    engine = VerificationEngine()
    action = Action(type="device.file.write", idempotency_key="key",
                    parameters={"path": "note.txt", "content": "expected"})
    assert engine._assess(action, observation)[0] == "uninformative"


def test_file_open_verification_never_launches_again() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "note.txt").write_text("note")
        provider = LaptopProvider(Workspace(root))
        action = Action(type="device.files.open", provider="laptop",
                        parameters={"path": "note.txt"})
        with patch.object(FilesEngine, "_opener_path", return_value="/fake/opener"), \
             patch.object(FilesEngine, "_run_opener", return_value=CompletedProcess([], 0)) as launch:
            asyncio.run(provider.execute(action))
            verifier = default_engine()
            register_device_strategies(verifier)
            outcome = verifier.verify(action, ProviderResult(ok=True), provider)
            assert not outcome.verified
            assert outcome.status.value == "indeterminate"
            assert launch.call_count == 1


def test_media_verification_reads_current_player_state() -> None:
    driver = FakeMediaDriver()
    player = driver.add_player("vlc")
    provider = LaptopProvider(None)
    provider.media = MediaEngine(driver)
    action = Action(type="media.play", provider="laptop")
    result = asyncio.run(provider.execute(action))
    verifier = default_engine()
    register_device_strategies(verifier)
    assert verifier.verify(action, result, provider).verified
    player.state = "paused"
    assert not verifier.verify(action, result, provider).verified
    driver.clear_players()
    assert not verifier.verify(action, result, provider).verified


def test_browser_verification_reads_current_page_without_navigation() -> None:
    driver = FakeDriver()
    driver.add_page("https://first.test/", "First", [])
    driver.add_page("https://second.test/", "Second", [])
    provider = LaptopProvider(None)
    provider.browser = BrowserEngine(driver)
    action = Action(type="browser.open", provider="laptop", parameters={"url": "https://first.test/"})
    result = asyncio.run(provider.execute(action))
    verifier = default_engine()
    register_device_strategies(verifier)
    assert verifier.verify(action, result, provider).verified
    asyncio.run(driver.navigate("https://second.test/"))
    before = list(driver.actions)
    assert not verifier.verify(action, result, provider).verified
    assert driver.actions == before


def test_discovery_filters_only_the_latest_definition() -> None:
    registry = ActionRegistry()
    definition = seed_definitions()[0]
    registry.register(replace(definition, version="1.0.0", risk_level=RiskLevel.LOW))
    registry.register(replace(definition, version="2.0.0", risk_level=RiskLevel.HIGH))
    assert registry.discover(name=definition.name)[0].version == "2.0.0"
    assert registry.discover(name=definition.name, risk=RiskLevel.HIGH)[0].risk_level is RiskLevel.HIGH


def test_matching_identity_without_a_write_digest_is_not_proof() -> None:
    class Partial:
        name = "partial"
        simulated = False

        def observe(self, action: Action) -> dict:
            return {"idempotency_key": action.idempotency_key,
                    "parameters": {"path": "note.txt"}, "state": {"size": 8}}

    verifier = VerificationEngine()
    verifier.register("device.file.write", "observed_state")
    action = Action(type="device.file.write", parameters={"path": "note.txt", "content": "expected"})
    assert not verifier.verify(action, ProviderResult(ok=True), Partial()).verified


def test_observation_refuses_a_replaced_workspace_root() -> None:
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as outside:
        root = Path(parent, "workspace")
        root.mkdir()
        Path(root, "note.txt").write_text("inside")
        Path(outside, "note.txt").write_text("outside")
        workspace = Workspace(str(root))
        provider = LaptopProvider(workspace)
        action = Action(type="device.file.read", provider="laptop", parameters={"path": "note.txt"})
        asyncio.run(provider.execute(action))
        resolve = workspace.resolve

        def swap(path: object, **kwargs: object):
            resolved = resolve(path, **kwargs)
            root.rename(Path(parent, "original"))
            root.symlink_to(outside, target_is_directory=True)
            return resolved

        with patch.object(workspace, "resolve", side_effect=swap):
            assert provider.observe(action) is None


def test_partial_fields_without_identity_are_not_confirmation() -> None:
    action = Action(type="message.send", parameters={"to": "alice", "body": "hello"})
    verdict, _ = VerificationEngine()._assess(action, {"parameters": {"to": "alice"}})
    assert verdict == "uninformative"


@pytest.mark.parametrize("proof", ["https://fixture.invalid/start", "", "https://other.invalid/"])
def test_browser_redirect_needs_fresh_host_navigation_provenance(proof: str) -> None:
    class RedirectDriver(FakeDriver):
        async def open(self, url: str):
            await super().open(url)
            self.current_url = "https://fixture.invalid/canonical"
            return self._facts()

        async def state(self):
            return replace(await super().state(), requested_url=proof)

    async def journey():
        with tempfile.TemporaryDirectory() as root:
            driver = RedirectDriver()
            provider = LaptopProvider(Workspace(root))
            provider.browser = BrowserEngine(driver)
            action = Action(type="browser.open", provider="laptop",
                            parameters={"url": "https://fixture.invalid/start"})
            result = await provider.execute(action)
            observation = await provider.observe_async(action)
            verifier = default_engine()
            assert verifier._assess(action, observation)[0] == (
                "match" if proof == action.parameters["url"] else "mismatch"
            )
            driver.loading = True
            assert await provider.observe_async(action) is None
            driver.loading = False
            driver.current_url = "https://fixture.invalid/unrelated"
            assert await provider.observe_async(action) is None
            assert (await provider.verify(action, result)).verified is False

    asyncio.run(journey())


def test_browser_wire_retains_loading_and_host_navigation_metadata() -> None:
    from actionmesh.agent.browser_ipc import BrowserIpcError, CommandResult

    payload = {"status": "ok", "request_id": "state", "loading": True,
               "requested_url": "https://fixture.invalid/start"}
    result = CommandResult.from_wire(payload)
    assert result.loading is True
    assert result.requested_url == payload["requested_url"]
    assert CommandResult.from_wire(result.to_wire()).loading is True
    with pytest.raises(BrowserIpcError):
        CommandResult.from_wire({**payload, "loading": "false"})
    with pytest.raises(BrowserIpcError):
        CommandResult.from_wire({**payload, "requested_url": {"url": "forged"}})


@pytest.mark.parametrize("private", [".config/ActionMesh/settings.json", ".9router/db/data.sqlite"])
def test_home_workspace_excludes_app_and_router_private_state(private: str) -> None:
    from actionmesh.agent.errors import PathEscape

    with tempfile.TemporaryDirectory() as root:
        target = Path(root, private)
        target.parent.mkdir(parents=True)
        target.write_text("synthetic credential placeholder")
        with pytest.raises(PathEscape):
            Workspace(root).resolve(private)


def test_legacy_default_directory_list_gets_an_independent_readback() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "fixture.txt").write_text("synthetic")
        provider = LaptopProvider(Workspace(root))
        action = Action(type="device.file.list", provider="laptop", parameters={})
        result = asyncio.run(provider.execute(action))
        verifier = default_engine()
        register_device_strategies(verifier)
        assert provider.files is not None
        with patch.object(provider.files, "list_", wraps=provider.files.list_) as fresh_read:
            outcome = verifier.verify(action, result, provider)
        assert fresh_read.call_count == 1
        assert outcome.verified is True
