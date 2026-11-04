"""Resilience: bridge outage, timeout, stale-ref and navigation read-back."""

from __future__ import annotations

import pytest

from actionmesh.agent.browser import STATUS_UNAVAILABLE, BrowserDriver, BrowserEngine
from actionmesh.agent.browser_bridge import BrowserBridgeUnavailable

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _DeadDriver(BrowserDriver):
    async def open(self, url: str):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")

    async def navigate(self, url: str):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")

    async def go_back(self):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")

    async def go_forward(self):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")

    async def reload(self):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")

    async def snapshot_elements(self, max_elements: int = 200):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")

    async def page_text(self, limit: int = 6000) -> str:
        return ""

    async def click(self, element_id: int, double: bool = False):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")

    async def type_text(self, element_id: int, text: str):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")

    async def select(self, element_id: int, value: str):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")

    async def scroll(self, direction: str, element_id=None, amount=None):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")

    async def wait_for(self, ms=None, until=None):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")

    async def stop(self, reason: str = "stopped"):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")

    async def state(self):
        raise BrowserBridgeUnavailable("the browser bridge socket does not exist (/tmp/x.sock)")


async def test_open_with_dead_bridge_is_unavailable_not_error() -> None:
    result = await BrowserEngine(_DeadDriver()).open("https://example.test/")
    assert result["status"] == STATUS_UNAVAILABLE
    assert "unreachable" in result["reason"]


async def test_stale_ref_after_navigation_is_stale() -> None:
    from actionmesh.agent.browser import FakeDriver

    drv = FakeDriver()
    drv.add_page("http://b.test/page1", "P1", [{"tag": "button", "name": "B1"}])
    drv.add_page("http://b.test/page2", "P2", [{"tag": "a", "name": "L1"}])
    eng = BrowserEngine(drv)
    await eng.open("http://b.test/page1")
    snap = await eng.snapshot()
    ref = snap["refs"][0]
    await eng.navigate("http://b.test/page2")
    assert (await eng.click(ref))["status"] == "stale"


async def test_unknown_ref_is_stale_never_error_or_crash() -> None:
    from actionmesh.agent.browser import FakeDriver

    drv = FakeDriver()
    drv.add_page("http://b.test/page1", "P1", [{"tag": "button", "name": "B1"}])
    eng = BrowserEngine(drv)
    await eng.open("http://b.test/page1")
    assert (await eng.snapshot())["status"] == "ok"
    assert (await eng.click(99999))["status"] == "stale"


async def test_operations_after_stop_are_unavailable_until_reopen() -> None:
    from actionmesh.agent.browser import FakeDriver

    drv = FakeDriver()
    drv.add_page("http://b.test/page1", "P1", [{"tag": "button", "name": "B1"}])
    eng = BrowserEngine(drv)
    await eng.open("http://b.test/page1")
    assert (await eng.stop())["status"] == "ok"
    for call in (eng.snapshot(), eng.inspect(), eng.click(1), eng.type_(1, "x")):
        assert (await call)["status"] == STATUS_UNAVAILABLE
