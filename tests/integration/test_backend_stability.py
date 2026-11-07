"""Concurrent requests exercise the real app, with only the slow model substituted."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.voiceos.store import VoiceOsStore


def isolate(monkeypatch) -> None:
    for name in ['ACTIONMESH_API_KEY', 'ACTIONMESH_AUDIT_DB', 'ACTIONMESH_DATABASE_URL',
                 'ACTIONMESH_VOICEOS_DB', 'ACTIONMESH_AUTOMATIONS_DB', 'ACTIONMESH_AGENT',
                 'ACTIONMESH_SECRET_FILE', 'ACTIONMESH_SECRET_KEY']:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('ACTIONMESH_ENV', 'development')


def test_concurrent_first_reads_open_one_voice_store(monkeypatch) -> None:
    isolate(monkeypatch)
    opened = []
    class DelayedStore(VoiceOsStore):
        def __init__(self, *args, **kwargs):
            time.sleep(0.03)
            super().__init__(*args, **kwargs)
            opened.append(self)
    app = create_app()
    client = TestClient(app, client=('127.0.0.1', 45123))
    try:
        with patch('actionmesh.api.VoiceOsStore', DelayedStore), \
             ThreadPoolExecutor(max_workers=12) as pool:
            # Exercise concurrent first access without a lifespan eagerly opening
            # the runner's store; this reproduces the embedding/request race.
            responses = list(pool.map(lambda _: client.get('/voiceos/conversation'), range(24)))
            assert all(response.status_code == 200 for response in responses)
            assert len(opened) == 1
    finally:
        client.close()
        for store in opened:
            store.close()


def test_slow_model_load_is_bounded_health_stays_responsive_and_turns_do_not_race(monkeypatch) -> None:
    isolate(monkeypatch)
    app = create_app()
    entered = 0
    lock = threading.Lock()
    release = threading.Event()
    class SlowReasoner:
        def understand(self, text, conversation):
            nonlocal entered
            with lock:
                entered += 1
            release.wait(timeout=5)
            return SimpleNamespace(kind='answer', reply='fixture answer', session=None,
                                   notes=(), unresolved=(), target=None, target_execution_id='')
    app.state.voiceos_reasoner = lambda _target: SlowReasoner()
    async def journey() -> None:
        tasks = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            try:
                for index in range(8):
                    tasks.append(asyncio.create_task(client.post('/voiceos/messages',
                        json={'text': 'fixture', 'actor': {'id': f'actor-{index}'}})))
                for _ in range(200):
                    with lock:
                        if entered == 8:
                            break
                    await asyncio.sleep(0.01)
                assert entered == 8
                health = await asyncio.wait_for(client.get('/health'), timeout=2)
                assert health.status_code == 200
                assert not release.is_set()
                assert not any(task.done() for task in tasks)
                duplicate = await client.post('/voiceos/messages', json={'text': 'duplicate', 'actor': {'id': 'actor-0'}})
                assert duplicate.status_code == 409
                overload = await client.post('/voiceos/messages', json={'text': 'overflow', 'actor': {'id': 'extra'}})
                assert overload.status_code == 503
                release.set()
                responses = await asyncio.gather(*tasks)
                assert all(response.status_code == 200 for response in responses)
                assert entered == 8, 'refused turns must never reach the model'
                again = await client.post('/voiceos/messages', json={'text': 'new turn', 'actor': {'id': 'actor-0'}})
                assert again.status_code == 200
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
    try:
        asyncio.run(journey())
    finally:
        store = getattr(app.state, 'voiceos_store_instance', None)
        if store:
            store.close()
