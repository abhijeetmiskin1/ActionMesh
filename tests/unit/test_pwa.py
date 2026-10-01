"""C16 — installable-app support (manifest, service worker, icon).

The point of these is that a phone must be able to install the dashboard to its
home screen, and that installing it must never let a phone show *stale* data.
The second half matters more than the first: a cached execution record or audit
chain would be a lie in a system whose entire purpose is truthful reporting.
"""

from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.dashboard import SHELL_VERSION, manifest, page


class TestManifest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app())

    def test_manifest_is_served(self) -> None:
        response = self.client.get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["display"], "standalone")
        self.assertEqual(body["start_url"], "/dashboard")
        self.assertTrue(body["name"])

    def test_manifest_declares_an_icon(self) -> None:
        icons = self.client.get("/manifest.webmanifest").json()["icons"]
        self.assertTrue(icons)
        self.assertTrue(any("maskable" in i.get("purpose", "") for i in icons))

    def test_dashboard_links_the_manifest(self) -> None:
        # Without this link the browser will not offer installation.
        html = self.client.get("/dashboard").text
        self.assertIn('rel="manifest"', html)
        self.assertIn("/manifest.webmanifest", html)
        self.assertIn("apple-touch-icon", html)

    def test_manifest_is_json_serialisable(self) -> None:
        json.dumps(manifest())


class TestServiceWorker(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app())

    def test_service_worker_is_served_as_javascript(self) -> None:
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])

    def test_it_is_not_cached_so_updates_land(self) -> None:
        # A stale worker would keep serving an old shell indefinitely.
        self.assertIn("no-cache", self.client.get("/sw.js").headers["cache-control"])

    def test_it_caches_the_shell_but_never_api_data(self) -> None:
        source = self.client.get("/sw.js").text
        self.assertIn("/dashboard", source)
        # The dangerous thing to get wrong: caching a response the user would
        # read as "this is what happened".
        for route in ("/audit", "/executions", "/capabilities", "/execute"):
            self.assertNotIn(f"'{route}'", source, f"{route} must not be cached")

    def test_cache_name_is_versioned(self) -> None:
        self.assertIn(SHELL_VERSION, self.client.get("/sw.js").text)


class TestIcon(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app())

    def test_icon_is_served_as_svg(self) -> None:
        response = self.client.get("/icon.svg")
        self.assertEqual(response.status_code, 200)
        self.assertIn("svg", response.headers["content-type"])

    def test_icon_is_well_formed_svg(self) -> None:
        body = self.client.get("/icon.svg").text
        self.assertTrue(body.strip().startswith("<svg"))
        self.assertIn("</svg>", body)


class TestPageIntegrity(unittest.TestCase):
    def test_service_worker_is_optional_not_required(self) -> None:
        # Registration must not be able to break the page.
        html = page()
        self.assertIn("serviceWorker", html)
        self.assertIn(".catch(", html)

    def test_page_stays_responsive_on_a_phone(self) -> None:
        html = page()
        self.assertIn("width=device-width", html)
        self.assertIn("@media (max-width: 620px)", html)


if __name__ == "__main__":
    unittest.main()
