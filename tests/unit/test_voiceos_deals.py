"""Deal discovery and browser typos use the existing execution pipeline."""

from urllib.parse import quote_plus

from fastapi.testclient import TestClient

from actionmesh.agent.browser import BrowserEngine, FakeDriver
from actionmesh.agent.provider import LaptopProvider
from actionmesh.api import create_app


def test_voice_deal_discovery_runs_read_only(monkeypatch) -> None:
    monkeypatch.setenv("ACTIONMESH_AGENT", "1")
    monkeypatch.setenv("ACTIONMESH_ALLOW_BROWSER", "1")
    monkeypatch.setenv("ACTIONMESH_ALLOW_NATIVE_APPS", "1")
    monkeypatch.setattr("actionmesh.agent.config.sys.platform", "darwin")
    monkeypatch.setattr("actionmesh.agent.apps.running", lambda _app: True)
    driver = FakeDriver()
    home = "https://duckduckgo.com/"
    query = "USB-C charger deals prices reputable retailers"
    search = f"{home}?q={quote_plus(query)}"
    driver.add_page(home, "Search", [])
    driver.add_page(search, "Results", [
        {"tag": "a", "name": "Sample price", "href": "https://example.com/item"},
    ])
    app = create_app()
    provider = next(p for p in app.state.engine.providers if isinstance(p, LaptopProvider))
    provider.browser = BrowserEngine(driver)
    actor = {"id": "voiceos-local"}

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        def post(path: str, body: dict) -> dict:
            response = client.post(path, json=body)
            assert response.is_success, response.text
            return response.json()

        def propose(text: str) -> dict:
            return post("/voiceos/messages", {"actor": actor, "text": text})

        def run(proposal: dict) -> dict:
            return post(f"/voiceos/sessions/{proposal['session_id']}/run", {
                "actor": actor, "stop_on_unverified": True,
            })

        assert propose("check best vrified deal")["kind"] == "answer"
        followup = propose("USB-C charger")
        assert followup["kind"] == "task"
        followup_session = client.get(f"/voiceos/sessions/{followup['session_id']}").json()
        assert followup_session["steps"][1]["parameters"]["query"] == query
        assert propose("Find the best deal")["kind"] == "answer"

        denied = run(propose("open brawser"))
        assert denied["steps"][0]["action_type"] == "browser.open"
        assert denied["steps"][0]["state"] == "denied"
        assert not driver.actions

        for scope in ("browser.control", "search.web"):
            post("/consents", {"actor_id": actor["id"], "scope": scope, "max_risk": "low"})
        opened = run(propose("open brawser"))
        assert opened["steps"][0]["verified"] is True
        result = run(propose("Check the best verified deal for USB-C charger and order it"))
        assert result["status"] == "completed", result
        assert [step["action_type"] for step in result["steps"]] == ["browser.open", "browser.search"]
        assert result["steps"][1]["parameters"]["query"] == query
        assert result["steps"][1]["observed"]["count"] == 1
        assert "results" not in result["steps"][1]["observed"]
        assert "Sample price" not in str(result)
        assert not any(action["action"] == "click" for action in driver.actions)

        command = "Open browser, then check the best deal for USB-C charger, and then is Clock running?"
        assert propose("Open browser then open Unknown App")["kind"] == "answer"
        plan = propose(command)
        denied = run(plan)
        assert denied["status"] == "failed"
        assert denied["steps"][-1]["state"] == "denied"

        post("/consents", {
            "actor_id": actor["id"], "scope": "system.app.status", "max_risk": "low",
        })
        combined = run(propose(command))
        assert combined["status"] == "completed", combined
        assert [step["index"] for step in combined["steps"]] == [0, 1, 2, 3]
        assert all(step["verified"] for step in combined["steps"])
        assert combined["steps"][-1]["observed"]["running"] is True
