"""P10 §8: the rate limiter's window, its bounds, and that an endpoint enforces it."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.core.ratelimit import RateLimiter
from actionmesh.protocol.models import DeviceKind


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestRateLimiter(unittest.TestCase):
    def test_allows_up_to_limit_then_refuses(self) -> None:
        limiter = RateLimiter(limit=3, window_s=60, clock=FakeClock())
        self.assertTrue(all(limiter.allow("k") for _ in range(3)))
        self.assertFalse(limiter.allow("k"))

    def test_the_window_slides_so_a_waiter_is_not_locked_out_forever(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(limit=2, window_s=60, clock=clock)
        self.assertTrue(limiter.allow("k"))
        self.assertTrue(limiter.allow("k"))
        self.assertFalse(limiter.allow("k"))
        clock.advance(61)
        self.assertTrue(limiter.allow("k"))

    def test_keys_are_independent(self) -> None:
        limiter = RateLimiter(limit=1, window_s=60, clock=FakeClock())
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))

    def test_remaining_counts_down(self) -> None:
        limiter = RateLimiter(limit=5, window_s=60, clock=FakeClock())
        limiter.allow("k")
        self.assertEqual(limiter.remaining("k"), 4)

    def test_tracked_keys_are_bounded(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(limit=1, window_s=60, max_keys=3, clock=clock)
        for index in range(3):
            self.assertTrue(limiter.allow(f"k{index}"))
        # Cap reached with nothing expired yet: a brand-new key is refused rather than
        # growing the map without bound.
        self.assertFalse(limiter.allow("overflow"))
        self.assertLessEqual(limiter.tracked_keys(), 3)

    def test_expired_keys_are_pruned_not_leaked(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(limit=1, window_s=10, max_keys=2, clock=clock)
        limiter.allow("a")
        limiter.allow("b")
        clock.advance(20)  # both windows expired
        self.assertTrue(limiter.allow("c"))
        self.assertLessEqual(limiter.tracked_keys(), 2)

    def test_rejects_bad_configuration(self) -> None:
        for kwargs in ({"limit": 0, "window_s": 60}, {"limit": 1, "window_s": 0}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                RateLimiter(**kwargs)  # type: ignore[arg-type]


class TestEndpointEnforcement(unittest.TestCase):
    def setUp(self) -> None:
        for name in ("ACTIONMESH_API_KEY", "ACTIONMESH_SECRET_KEY", "ACTIONMESH_RATE_LIMIT"):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        from actionmesh.core.identity import IdentityStore

        identities = IdentityStore()
        from actionmesh.core.devices import DeviceRegistry

        devices = DeviceRegistry(identities=identities)
        self.app = create_app(identities=identities, devices=devices)
        self.local = TestClient(self.app, client=("127.0.0.1", 45123))
        devices.enroll(device_id="laptop-1", kind=DeviceKind.LAPTOP, capabilities=["message.send"])

    def test_pairing_mint_is_capped_per_device_and_source(self) -> None:
        # limit is 10/min; the 11th from the same source+device is refused 429.
        statuses = [
            self.local.post("/pairing/code", json={"device_id": "laptop-1"}).status_code
            for _ in range(11)
        ]
        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10], 429)

    def test_disabling_rate_limiting_lets_the_burst_through(self) -> None:
        with patch.dict(os.environ, {"ACTIONMESH_RATE_LIMIT": "0"}):
            app = create_app()
        client = TestClient(app, client=("127.0.0.1", 45123))
        app.state.devices.enroll(device_id="laptop-1", kind=DeviceKind.LAPTOP, capabilities=[])
        codes = [
            client.post("/pairing/code", json={"device_id": "laptop-1"}).status_code
            for _ in range(15)
        ]
        self.assertTrue(all(code == 200 for code in codes), codes)


if __name__ == "__main__":
    unittest.main()
