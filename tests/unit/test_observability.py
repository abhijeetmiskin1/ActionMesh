"""C15 observability tests."""

from __future__ import annotations

import unittest

from actionmesh.core.observability import Metrics


class TestMetrics(unittest.TestCase):
    def test_snapshot_is_sorted_and_copy_isolated(self) -> None:
        metrics = Metrics()
        metrics.increment("z")
        metrics.increment("a", 2)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot, {"a": 2, "z": 1})
        snapshot["a"] = 99
        self.assertEqual(metrics.snapshot()["a"], 2)


if __name__ == "__main__":
    unittest.main()