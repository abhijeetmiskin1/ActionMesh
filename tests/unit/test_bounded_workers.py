import asyncio
import threading

import pytest

from actionmesh.core.concurrency import BoundedWorkers, RequestBusy


def test_cancelling_a_request_does_not_free_its_still_running_worker() -> None:
    async def scenario() -> None:
        workers = BoundedWorkers(limit=1)
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        def slow():
            entered.set()
            try:
                release.wait(timeout=5)
                raise ValueError('late provider failure')
            finally:
                completed.set()
        first = asyncio.create_task(workers.run(slow))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            with pytest.raises(RequestBusy):
                await workers.run(lambda: 'must not start')
            release.set()
            for _ in range(100):
                if completed.is_set():
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.01)
            assert await workers.run(lambda: 'recovered') == 'recovered'
        finally:
            release.set()
    asyncio.run(scenario())
