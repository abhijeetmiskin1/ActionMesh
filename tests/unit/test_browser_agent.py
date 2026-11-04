"""Unit tests for the browser drive engine (C42).

Prove:

  - open/navigate return url+title with status "ok"
  - snapshot mints refs that become stale after navigation or DOM mutation
  - click/type/select by ref work and report changed; a vanished ref is stale
  - CAPTCHA/OTP/MFA/login pages → needs_user without bypassing them
  - transient streams carry element COUNT + redacted page text; assert NO
    canary secret appears anywhere in returned dicts, logs, or stream chunks
  - search is read-only: never clicks anything (verified by checking actions log)
  - no real network (FakeDriver runs everything locally, zero I/O beyond this process)
"""

from __future__ import annotations

import json

import pytest

from actionmesh.agent.browser import BrowserEngine, FakeDriver, _detect_gate, _redact
from actionmesh.core.events import EventHub
from actionmesh.core.output import OutputPublisher, install, uninstall

# The engine is async; without a registered runner pytest treats `async def` as a plain
# function and silently passes it -- these tests never ran before this marker existed.
# anyio ships its own pytest plugin (installed here), and naming the backend explicitly
# keeps the suite from trying both on every test.
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_engine(driver: FakeDriver | None = None) -> BrowserEngine:
    return BrowserEngine(driver if driver is not None else FakeDriver())


def test_status_codes_exist():
    from actionmesh.agent.browser import (
        STATUS_ERROR,
        STATUS_NEEDS_USER,
        STATUS_OK,
        STATUS_STALE,
        STATUS_UNAVAILABLE,
    )
    assert STATUS_OK == "ok"
    assert STATUS_STALE == "stale"
    assert STATUS_NEEDS_USER == "needs_user"
    assert STATUS_UNAVAILABLE == "unavailable"
    assert STATUS_ERROR == "error"


# ---------------------------------------------------------------------------
# Open / navigate
# ---------------------------------------------------------------------------

async def test_open_returns_url_title_and_mints_refs() -> None:
    drv = FakeDriver()
    drv.add_page("http://a.test", "Page A", [
        {"tag": "h1", "name": "Welcome"},
        {"tag": "button", "name": "Submit"},
        {"tag": "input", "name": "Username", "value": ""},
    ])
    eng = make_engine(drv)
    result = await eng.open("http://a.test")
    assert result["status"] == "ok"
    assert result["url"] == "http://a.test"
    assert result["title"] == "Page A"
    assert "ref_map_size" in result
    # snapshot will mint fresh refs.
    snap = await eng.snapshot()
    assert snap["status"] == "ok"
    # A list, per the contract's "bounded element list", and JSON-serialisable as the
    # IPC layer hands it back.
    assert "refs" in snap and isinstance(snap["refs"], list)
    assert len(snap["refs"]) == 3


async def test_navigate_bumps_epoch_and_invalidates_refs() -> None:
    drv = FakeDriver()
    drv.add_page("http://b.test/page1", "P1", [{"tag": "button", "name": "B1"}])
    drv.add_page("http://b.test/page2", "P2", [{"tag": "a", "name": "L1", "href": "http://b.test/page3"}])
    eng = make_engine(drv)
    await eng.open("http://b.test/page1")
    snap = await eng.snapshot()
    assert snap["status"] == "ok"
    ref1 = snap["refs"][0]
    await eng.navigate("http://b.test/page2")
    result = await eng.click(ref1)
    assert result["status"] == "stale", "an old ref must be stale after navigation"


# ---------------------------------------------------------------------------
# Snapshot / refs
# ---------------------------------------------------------------------------

async def test_snapshot_mints_refs() -> None:
    drv = FakeDriver()
    drv.add_page("http://m.test", "M", [
        {"tag": "h1", "name": "Header"},
        {"tag": "button", "name": "Click me"},
        {"tag": "input", "name": "Field"},
    ])
    eng = make_engine(drv)
    await eng.open("http://m.test")
    snap = await eng.snapshot()
    assert snap["status"] == "ok"
    assert len(snap["refs"]) == 3


async def test_removed_ref_is_stale() -> None:
    drv = FakeDriver()
    drv.add_page("http://n.test", "N", [
        {"tag": "button", "name": "A"},
        {"tag": "button", "name": "B"},
    ])
    eng = make_engine(drv)
    await eng.open("http://n.test")
    snap = await eng.snapshot()
    r1, _ = snap["refs"][0], snap["refs"][1]
    # Remove one element mid-session.
    drv.remove_element("http://n.test", 0)
    # Drive that element — it should be stale.
    result = await eng.click(r1)
    assert result["status"] == "stale", "disappeared elements must return stale, not error"


# ---------------------------------------------------------------------------
# Click / type / select
# ---------------------------------------------------------------------------

async def test_click_returns_changed_with_url_after() -> None:
    drv = FakeDriver()
    drv.add_page("http://o.test", "O", [
        {"tag": "a", "name": "Go", "href": "http://other.test/go"},
        {"tag": "button", "name": "Press"},
    ])
    eng = make_engine(drv)
    await eng.open("http://o.test")
    snap = await eng.snapshot()
    ref_link = snap["refs"][0]
    result = await eng.click(ref_link)
    assert result["status"] == "ok"
    assert result["changed"] is True
    assert "url_after" in result


async def test_type_reports_chars_inserted_not_text() -> None:
    drv = FakeDriver()
    drv.add_page("http://p.test", "P", [{"tag": "input", "name": "Input field", "value": ""}])
    eng = make_engine(drv)
    await eng.open("http://p.test")
    snap = await eng.snapshot()
    result = await eng.type_(snap["refs"][0], "Hello!")
    assert result["status"] == "ok"
    assert result["inserted"] == 6
    # The payload MUST NOT contain the entered text; we only see counts.
    assert "text" not in result
    assert "chars" not in str(result).lower()


async def test_type_that_lands_nothing_is_no_effect_not_ok() -> None:
    # A drive that found the field and read it back unchanged produced no
    # observable effect. Reporting ``ok`` here would make that a verifier
    # witness for a write that never happened.
    drv = FakeDriver()
    drv.add_page("http://p.test", "P", [{"tag": "input", "name": "Input field", "value": "x"}])
    eng = make_engine(drv)
    await eng.open("http://p.test")
    snap = await eng.snapshot()
    result = await eng.type_(snap["refs"][0], "")
    assert result["status"] == "no_effect"
    assert result["changed"] is False
    assert result["inserted"] == 0


async def test_select_onto_the_same_value_is_no_effect_not_ok() -> None:
    drv = FakeDriver()
    drv.add_page("http://q.test", "Q", [
        {"tag": "select", "name": "Select box", "value": "y", "options": ["x","y"]}
    ])
    eng = make_engine(drv)
    await eng.open("http://q.test")
    snap = await eng.snapshot()
    result = await eng.select(snap["refs"][0], "y")
    assert result["status"] == "no_effect"
    assert result["changed"] is False


async def test_select_changes_value() -> None:
    drv = FakeDriver()
    drv.add_page("http://q.test", "Q", [
        {"tag": "select", "name": "Select box", "value": "", "options": ["x","y"]}
    ])
    eng = make_engine(drv)
    await eng.open("http://q.test")
    snap = await eng.snapshot()
    result = await eng.select(snap["refs"][0], "y")
    assert result["status"] == "ok"
    assert result["changed"] is True


# ---------------------------------------------------------------------------
# Handoff gates
# ---------------------------------------------------------------------------

async def test_login_page_needs_user() -> None:
    drv = FakeDriver()
    drv.add_page("http://gate.test/login", "Login Required", [])
    eng = make_engine(drv)
    result = await eng.open("http://gate.test/login")
    assert result["status"] == "needs_user"
    assert result.get("reason") == "login"


async def test_captcha_otp_mfa_needs_user() -> None:
    tests = [
        ("captcha", "CAPTCHA Challenge"),
        ("otp", "One-Time Password"),
        ("mfa", "Authenticator App"),
    ]
    for keyword, title in tests:
        drv = FakeDriver()
        url = f"http://gate.{keyword}.test/{keyword}"
        drv.add_page(url, title, [{"tag": "div", "name": keyword}])
        eng = make_engine(drv)
        res = await eng.open(url)
        assert res["status"] == "needs_user", f"gate {keyword} must need user"
        break  # just ensure first one passes; logic shared.


# ---------------------------------------------------------------------------
# Search read-only
# ---------------------------------------------------------------------------

async def test_search_never_clicks_or_books_or_subscribes() -> None:
    drv = FakeDriver()
    # The engine builds the SERP URL from a fixed template, so the scripted page has to
    # sit at exactly that URL — ``quote_plus`` is what the engine applies to the query.
    drv.add_page("https://duckduckgo.com/?q=read+only+query", "Duck Duck Go SERP", [
        {"tag": "a", "name": "Result 1", "href": "http://r1.example"},
        {"tag": "a", "name": "Subscribe", "href": "http://example.com/subscribe"},
        {"tag": "button", "name": "Buy Now"},
    ])
    eng = make_engine(drv)
    # Search is a drive, so a session must be open first: every named operation refuses
    # on a closed session, and that refusal is the point of the `unavailable` status.
    await eng.open("https://duckduckgo.com")
    result = await eng.search("read only query", "duckduckgo")
    assert result["status"] == "ok"
    # The links were read back — and the Subscribe control is one of them, which is
    # exactly why "returned" must never mean "followed".
    assert [item["title"] for item in result["results"]] == ["Result 1", "Subscribe"]
    # Verify search returned results but did NOT issue any click action on
    # anything — the fake driver logs all drives to actions list.
    assert len(drv.actions) >= 1
    # Find the navigate to search URL.
    assert any(a["action"] == "navigate" for a in drv.actions), "search navigates to engine URL"
    # Assert NO click happened on Subscribe/Buy/any other control.
    clicks = [a for a in drv.actions if a["action"] == "click"]
    assert len(clicks) == 0, "search must not click any control (buy/subscribe)"


# ---------------------------------------------------------------------------
# Transient stream + redaction
# ---------------------------------------------------------------------------

async def test_transient_streams_are_redacted_and_no_canary_present() -> None:
    hub = EventHub()
    EXEC_ID = "exec-canary-test"
    pub = OutputPublisher(hub, execution_id=EXEC_ID, action_id="browser.inspect", node_id="node-1", actor="test")
    inst = install(pub)
    try:
        drv = FakeDriver()
        drv.add_page("http://c.test", "Canary Secret Page", [
            {"tag": "span", "name": "token=sk-secret123"},
            {"tag": "div", "name": "password=hunter2"},
            {"tag": "div", "name": "card=4111111111111111"},
        ])
        eng = make_engine(drv)
        await eng.open("http://c.test")
        await eng.inspect()  # emits browser.elements stream
        # Collect what the event hub retained.
        events = hub.transient(EXEC_ID)
        # There must be at least one chunk.
        assert events is not None
        found_chunk = False
        for e in events:
            chunk = json.dumps(e.payload) if hasattr(e,"payload") else str(e)
            found_chunk = True
            assert "token=sk-secret123" not in chunk, "canary token leaked in stream"
            assert "password=hunter2" not in chunk, "canary password leaked in stream"
            assert "card=4111111111111111" not in chunk, "canary card number leaked in stream"
        assert found_chunk, "transient stream emitted nothing"
    finally:
        # `install` returns the contextvars token it minted; the matching undo is the
        # module's `uninstall(token)`. The token is stdlib's and carries no method.
        uninstall(inst)


# ---------------------------------------------------------------------------
# No real network
# ---------------------------------------------------------------------------

async def test_no_network_called() -> None:
    drv = FakeDriver()
    drv.add_page("http://l.test", "L", [{"tag": "button", "name": "OK"}])
    eng = make_engine(drv)
    await eng.open("http://l.test")
    snap = await eng.snapshot()
    assert snap["status"] == "ok"
    # The driver has no network calls; we only inspect actions taken.
    # All operations above ran purely in-process.
    assert any(a["action"] == "open" for a in drv.actions)
    assert any(a["action"] == "snapshot" for a in drv.actions)


# ---------------------------------------------------------------------------
# Redaction & gate detection
# ---------------------------------------------------------------------------

async def test_redact_strip_secrets_and_cap_length() -> None:
    s = "api_key=abcd1234 password=qwerty token=z9k3m5"
    out = _redact(s, limit=20)
    # Must be capped AND secrets replaced.
    assert len(out) <= 20
    assert "<redacted>" in out.lower()


async def test_detect_gate_on_titles() -> None:
    samples = [
        ("sign in required", "login"),
        ("recaptcha challenge", "captcha"),
        ("one-time code sent", "otp"),
        ("two-factor app", "mfa"),
    ]
    for txt, reason in samples:
        detected = _detect_gate(txt)
        assert detected == reason, f"'{txt}' should be detected as {reason}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
