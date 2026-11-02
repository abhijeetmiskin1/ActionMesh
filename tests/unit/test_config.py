"""P11: the configuration system must fail closed for insecure production setups."""

from __future__ import annotations

import unittest

from actionmesh.core.config import AppConfig, ConfigError, DeploymentMode, _sqlite_path

# A fully-formed, safe production environment; individual tests break one piece.
GOOD_PROD = {
    "ACTIONMESH_ENV": "production",
    "ACTIONMESH_API_KEY": "shared-edge-key",
    "ACTIONMESH_DATABASE_URL": "sqlite:////var/lib/actionmesh/app.db",
    "ACTIONMESH_AUDIT_DB": "/var/lib/actionmesh/audit.db",
    "ACTIONMESH_SECRET_KEY": "a-long-enough-secret",
    "ACTIONMESH_PUBLIC_BASE_URL": "https://mesh.example.com",
    "ACTIONMESH_HOST": "0.0.0.0",
    "ACTIONMESH_TRUSTED_PROXIES": "10.0.0.1,10.0.0.2",
}


class TestDefaults(unittest.TestCase):
    def test_empty_environment_is_development_and_valid(self) -> None:
        config = AppConfig.from_env({})
        self.assertIs(config.mode, DeploymentMode.DEVELOPMENT)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8000)
        self.assertFalse(config.durable_execution)
        self.assertFalse(config.durable_audit)

    def test_development_never_demands_production_hardening(self) -> None:
        # No key, ephemeral secret, in-memory stores -- allowed, because this is the
        # developer's own laptop. The point is that it is *explicit*, not the default
        # a remote deployment quietly inherits.
        AppConfig.from_env({"ACTIONMESH_ENV": "development"})


class TestParsing(unittest.TestCase):
    def test_values_are_read_from_the_environment(self) -> None:
        config = AppConfig.from_env(
            {
                "ACTIONMESH_ENV": "lan",
                "ACTIONMESH_API_KEY": "k",
                "ACTIONMESH_HOST": "192.168.1.10",
                "ACTIONMESH_PORT": "9000",
                "ACTIONMESH_MAX_REQUEST_BYTES": "4096",
                "ACTIONMESH_ALLOW_REMOTE_PAIRING": "true",
            }
        )
        self.assertIs(config.mode, DeploymentMode.LAN)
        self.assertEqual(config.port, 9000)
        self.assertEqual(config.max_request_bytes, 4096)
        self.assertTrue(config.allow_remote_pairing)
        self.assertTrue(config.binds_publicly)

    def test_unknown_mode_is_rejected_not_downgraded(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env({"ACTIONMESH_ENV": "stagings"})
        self.assertIn("not one of", str(ctx.exception))

    def test_non_integer_port_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            AppConfig.from_env({"ACTIONMESH_PORT": "http"})

    def test_sqlite_url_forms_resolve_to_a_path(self) -> None:
        self.assertEqual(_sqlite_path("sqlite:////var/x.db"), "/var/x.db")
        self.assertEqual(_sqlite_path("sqlite:///relative.db"), "relative.db")
        self.assertEqual(_sqlite_path("plain.db"), "plain.db")
        self.assertEqual(_sqlite_path("sqlite:///:memory:"), ":memory:")
        self.assertEqual(_sqlite_path(""), "")


class TestLanMode(unittest.TestCase):
    def test_lan_requires_a_shared_key(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env({"ACTIONMESH_ENV": "lan"})
        self.assertIn("ACTIONMESH_API_KEY", str(ctx.exception))

    def test_lan_with_a_key_is_allowed_without_durable_state(self) -> None:
        AppConfig.from_env({"ACTIONMESH_ENV": "lan", "ACTIONMESH_API_KEY": "k"})


class TestProductionHardening(unittest.TestCase):
    def test_a_complete_production_config_passes(self) -> None:
        config = AppConfig.from_env(dict(GOOD_PROD))
        self.assertTrue(config.durable_execution)
        self.assertTrue(config.durable_audit)

    def test_production_without_durable_execution_is_refused(self) -> None:
        env = dict(GOOD_PROD)
        env["ACTIONMESH_DATABASE_URL"] = "sqlite:///:memory:"
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env(env)
        self.assertIn("durable execution", str(ctx.exception))

    def test_production_without_durable_audit_is_refused(self) -> None:
        env = dict(GOOD_PROD)
        env["ACTIONMESH_AUDIT_DB"] = ""
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env(env)
        self.assertIn("durable audit", str(ctx.exception))

    def test_production_with_an_ephemeral_signing_key_is_refused(self) -> None:
        env = dict(GOOD_PROD)
        env["ACTIONMESH_SECRET_KEY"] = ""
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env(env)
        self.assertIn("durable signing key", str(ctx.exception))

    def test_production_without_a_shared_key_is_refused(self) -> None:
        env = dict(GOOD_PROD)
        env["ACTIONMESH_API_KEY"] = ""
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env(env)
        self.assertIn("ACTIONMESH_API_KEY", str(ctx.exception))

    def test_production_reports_every_problem_at_once(self) -> None:
        # A single error listing all gaps beats fixing them one restart at a time.
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env({"ACTIONMESH_ENV": "production"})
        message = str(ctx.exception)
        for expected in ("ACTIONMESH_API_KEY", "durable execution", "durable audit", "durable signing key"):
            self.assertIn(expected, message)

    def test_public_binding_without_trusted_proxies_is_refused(self) -> None:
        env = dict(GOOD_PROD)
        env["ACTIONMESH_TRUSTED_PROXIES"] = ""
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env(env)
        self.assertIn("forgeable client address", str(ctx.exception))

    def test_loopback_production_needs_no_proxy_list(self) -> None:
        env = dict(GOOD_PROD)
        env["ACTIONMESH_HOST"] = "127.0.0.1"
        env["ACTIONMESH_TRUSTED_PROXIES"] = ""
        AppConfig.from_env(env)

    def test_production_requires_a_public_base_url(self) -> None:
        env = dict(GOOD_PROD)
        env["ACTIONMESH_PUBLIC_BASE_URL"] = ""
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_env(env)
        self.assertIn("PUBLIC_BASE_URL", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
