"""C15 CLI tests."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from actionmesh.cli import main


class TestCli(unittest.TestCase):
    def test_dashboard_prints_url(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["dashboard", "--port", "9000"]), 0)
        self.assertEqual(output.getvalue().strip(), "http://127.0.0.1:9000/dashboard")

    def test_capabilities_print_json(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["capabilities", "--provider", "mock"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(len(payload), 16)


if __name__ == "__main__":
    unittest.main()
