"""C15 CLI tests.

The CLI is deliberately network-free (the security guard forbids an outbound client in
this package), so these tests exercise configuration validation and diagnostics and the
in-process capability listing -- never a live server.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

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


class TestConfigValidate(unittest.TestCase):
    def test_development_config_is_valid(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            redirect_stdout(output),
        ):
            code = main(["config", "validate"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "development")

    def test_broken_production_config_exits_nonzero(self) -> None:
        errors = io.StringIO()
        with (
            patch.dict(os.environ, {"ACTIONMESH_ENV": "production"}, clear=True),
            patch("sys.stderr", errors),
        ):
            code = main(["config", "validate"])
        self.assertEqual(code, 1)
        self.assertIn("production", errors.getvalue())


class TestDoctor(unittest.TestCase):
    def test_doctor_reports_mode_without_touching_the_network(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            redirect_stdout(output),
        ):
            code = main(["doctor"])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("config: valid", text)
        self.assertIn("mode: development", text)
        # An ephemeral key is reported honestly, never as if it were durable.
        self.assertIn("ephemeral", text)

    def test_doctor_fails_on_invalid_config(self) -> None:
        errors = io.StringIO()
        with (
            patch.dict(os.environ, {"ACTIONMESH_ENV": "production"}, clear=True),
            patch("sys.stderr", errors),
        ):
            code = main(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("INVALID", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
