import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_terminal_config import ConfigError, load_runtime_config
from proxy_auth import AuthenticationRequest, TokenAuthenticator
from server import AppServer, terminal_child_environment, terminal_command


ROOT = Path(__file__).parents[1]
BACKEND_SERVICE = (ROOT / "systemd" / "mobile-terminal@.service").read_text()
DEPLOY_SH = (ROOT / "deploy.sh").read_text()
INSTALL_SH = (ROOT / "install.sh").read_text()
SERVER_PY = (ROOT / "server.py").read_text()


class RuntimeConfigTest(unittest.TestCase):
    def write_config(self, root, payload):
        path = Path(root) / "config.json"
        path.write_text(json.dumps(payload))
        return path

    def base_config(self):
        return {
            "mode": "proxy",
            "listen": "127.0.0.1:8085",
            "stateDir": "state/proxy",
            "internalTokenEnv": "MT_INTERNAL",
            "authRealms": {
                "mine": {"tokenEnv": "MT_TOKEN", "principal": "ben"},
            },
            "profiles": [
                {
                    "id": "powerhouse",
                    "label": "Powerhouse",
                    "backend": "ws://127.0.0.1:8090",
                    "authRealm": "mine",
                    "accent": "#FFD166",
                },
                {
                    "id": "behuman",
                    "label": "Behuman",
                    "backend": None,
                    "authRealm": "mine",
                    "status": "down",
                },
            ],
        }

    def test_absent_or_backend_config_preserves_legacy_runtime(self):
        self.assertIsNone(load_runtime_config({}))
        with tempfile.TemporaryDirectory() as root:
            path = self.write_config(root, {"mode": "backend"})
            self.assertIsNone(load_runtime_config({"MOBILE_TERMINAL_CONFIG": str(path)}))

    def test_standalone_keeps_silent_keys_and_adds_passkey_authentication(self):
        self.assertIn("PendingDeviceEnrollment.issue", SERVER_PY)
        self.assertIn("enrollment.message()", SERVER_PY)
        self.assertIn('if message_type == "register-key":', SERVER_PY)
        self.assertIn("PasskeyAuth", SERVER_PY)
        self.assertIn("register_passkey", SERVER_PY)
        self.assertIn('"passkeyAuth": True', SERVER_PY)

    def test_proxy_config_loads_env_secrets_and_stub_profile(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.write_config(root, self.base_config())
            config = load_runtime_config(
                {
                    "MOBILE_TERMINAL_CONFIG": str(path),
                    "MT_INTERNAL": "internal-secret",
                    "MT_TOKEN": "external-secret",
                }
            )

        self.assertIsNotNone(config)
        self.assertEqual((config.host, config.port), ("127.0.0.1", 8085))
        self.assertEqual(config.active_profile, "powerhouse")
        self.assertEqual(config.auth_realms["mine"].token, "external-secret")
        self.assertEqual(config.internal_token, "internal-secret")
        self.assertEqual(config.internal_token_for(config.profiles[0]), "internal-secret")
        self.assertEqual(config.profiles[0].accent, "#ffd166")
        self.assertFalse(config.profiles[1].available)
        self.assertEqual(config.state_dir, (Path(root) / "state/proxy").resolve())

    def test_passkey_config_requires_explicit_rp_id_and_origin(self):
        with tempfile.TemporaryDirectory() as root:
            payload = self.base_config()
            payload["authRealms"]["mine"]["deviceKeyAuth"] = True
            path = self.write_config(root, payload)
            environ = {
                "MOBILE_TERMINAL_CONFIG": str(path),
                "MT_INTERNAL": "internal-secret",
                "MT_TOKEN": "external-secret",
            }
            with self.assertRaisesRegex(ConfigError, "rpId.*origin"):
                load_runtime_config(environ)

            payload["rpId"] = "example.ts.net"
            payload["origin"] = "https://terminal.example.ts.net"
            path.write_text(json.dumps(payload))
            config = load_runtime_config(environ)
            self.assertEqual(config.rp_id, "example.ts.net")
            self.assertEqual(config.expected_origin, "https://terminal.example.ts.net")

    def test_passkey_origin_must_be_scoped_to_the_configured_rp(self):
        with tempfile.TemporaryDirectory() as root:
            payload = self.base_config()
            payload["rpId"] = "example.ts.net"
            payload["origin"] = "https://attacker.example"
            path = self.write_config(root, payload)
            with self.assertRaisesRegex(ConfigError, "subdomain"):
                load_runtime_config(
                    {
                        "MOBILE_TERMINAL_CONFIG": str(path),
                        "MT_INTERNAL": "internal-secret",
                        "MT_TOKEN": "external-secret",
                    }
                )

    def test_non_loopback_backend_and_missing_internal_token_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            payload = self.base_config()
            payload["profiles"][0]["backend"] = "ws://192.0.2.10:8090"
            path = self.write_config(root, payload)
            with self.assertRaisesRegex(ConfigError, "loopback"):
                load_runtime_config(
                    {
                        "MOBILE_TERMINAL_CONFIG": str(path),
                        "MT_INTERNAL": "internal-secret",
                        "MT_TOKEN": "external-secret",
                    }
                )

            payload["profiles"][0]["backend"] = "ws://127.0.0.1:8090"
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ConfigError, "MT_INTERNAL"):
                load_runtime_config(
                    {
                        "MOBILE_TERMINAL_CONFIG": str(path),
                        "MT_TOKEN": "external-secret",
                    }
                )
    def test_profile_internal_tokens_override_shared_default_and_must_be_distinct(self):
        with tempfile.TemporaryDirectory() as root:
            payload = self.base_config()
            payload["profiles"][0]["internalTokenEnv"] = "MT_POWERHOUSE_INTERNAL"
            payload["profiles"][1]["internalTokenEnv"] = "MT_BEHUMAN_INTERNAL"
            path = self.write_config(root, payload)
            environ = {
                "MOBILE_TERMINAL_CONFIG": str(path),
                "MT_INTERNAL": "shared-secret",
                "MT_POWERHOUSE_INTERNAL": "powerhouse-secret",
                "MT_BEHUMAN_INTERNAL": "behuman-secret",
                "MT_TOKEN": "external-secret",
            }
            config = load_runtime_config(environ)
            self.assertEqual(config.internal_token_for(config.profiles[0]), "powerhouse-secret")
            self.assertEqual(config.internal_token_for(config.profiles[1]), "behuman-secret")
            self.assertNotIn("shared-secret", repr(config))
            self.assertNotIn("powerhouse-secret", repr(config))
            self.assertNotIn("behuman-secret", repr(config))

            environ["MT_BEHUMAN_INTERNAL"] = "powerhouse-secret"
            with self.assertRaisesRegex(ConfigError, "must not share"):
                load_runtime_config(environ)

            environ["MT_BEHUMAN_INTERNAL"] = "shared-secret"
            with self.assertRaisesRegex(ConfigError, "must differ"):
                load_runtime_config(environ)

    def test_direct_secrets_make_proxy_config_owner_only(self):
        with tempfile.TemporaryDirectory() as root:
            payload = self.base_config()
            payload.pop("internalTokenEnv")
            payload["internalToken"] = "internal-secret"
            payload["authRealms"]["mine"] = {"token": "external-secret"}
            path = self.write_config(root, payload)
            path.chmod(0o644)
            load_runtime_config({"MOBILE_TERMINAL_CONFIG": str(path)})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_secret_environment_name_fails_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as root:
            payload = self.base_config()
            payload["profiles"][0]["internalTokenEnv"] = "NOT VALID"
            path = self.write_config(root, payload)
            with self.assertRaisesRegex(ConfigError, "valid environment variable"):
                load_runtime_config(
                    {
                        "MOBILE_TERMINAL_CONFIG": str(path),
                        "MT_INTERNAL": "shared-secret",
                        "MT_TOKEN": "external-secret",
                    }
                )


class DeploymentRuntimeTest(unittest.TestCase):
    def test_deploy_copies_and_import_checks_runtime_modules(self):
        for module in ("mobile_terminal_config.py", "proxy.py", "proxy_auth.py"):
            self.assertIn(module, DEPLOY_SH.split("FILES=(", 1)[1].split(")", 1)[0])
        self.assertIn("from mobile_terminal_config import ConfigError, load_runtime_config", DEPLOY_SH)
        self.assertIn("from proxy import ProxyServer", DEPLOY_SH)
        self.assertIn("if [ -x .venv/bin/python ]", DEPLOY_SH)

    def test_installer_enforces_owner_only_environment_file(self):
        self.assertGreaterEqual(INSTALL_SH.count('chmod 600 "$ENV_FILE"'), 2)


class SystemdBackendTemplateTest(unittest.TestCase):
    def test_backend_instances_require_env_token_and_force_loopback(self):
        self.assertIn("EnvironmentFile=@ENV_DIR@/%i.env", BACKEND_SERVICE)
        self.assertNotIn("EnvironmentFile=-", BACKEND_SERVICE)
        self.assertIn("Environment=MOBILE_TERMINAL_REQUIRE_INTERNAL_TOKEN=true", BACKEND_SERVICE)
        self.assertIn("UMask=0077", BACKEND_SERVICE)
        self.assertIn("server.py --host 127.0.0.1", BACKEND_SERVICE)
        self.assertIn("if require_internal_token and not internal_token:", SERVER_PY)


class TerminalEnvironmentTest(unittest.TestCase):
    def test_internal_token_is_removed_only_from_terminal_children(self):
        with mock.patch.dict(
            os.environ,
            {
                "MOBILE_TERMINAL_INTERNAL_TOKEN": "internal-secret",
                "MOBILE_TERMINAL_INTERNAL_TOKEN_POWERHOUSE": "profile-internal-secret",
                "MOBILE_TERMINAL_TOKEN": "external-secret",
                "PATH": "/usr/bin",
            },
            clear=True,
        ):
            child_env = terminal_child_environment()
        self.assertNotIn("MOBILE_TERMINAL_INTERNAL_TOKEN", child_env)
        self.assertNotIn("MOBILE_TERMINAL_INTERNAL_TOKEN_POWERHOUSE", child_env)
        self.assertEqual(child_env["MOBILE_TERMINAL_TOKEN"], "external-secret")
        self.assertEqual(child_env["PATH"], "/usr/bin")
        self.assertTrue(terminal_command("/bin/bash -l").startswith("env -u MOBILE_TERMINAL_INTERNAL_TOKEN "))


class TokenAuthenticatorTest(unittest.TestCase):
    def test_authenticate_returns_principal_only_for_matching_realm_token(self):
        with tempfile.TemporaryDirectory() as root:
            payload = RuntimeConfigTest().base_config()
            path = Path(root) / "config.json"
            path.write_text(json.dumps(payload))
            config = load_runtime_config(
                {
                    "MOBILE_TERMINAL_CONFIG": str(path),
                    "MT_INTERNAL": "internal-secret",
                    "MT_TOKEN": "external-secret",
                }
            )
        authenticator = TokenAuthenticator(config.auth_realms)
        valid = AuthenticationRequest("mine", {"token": "external-secret"}, {}, ("127.0.0.1", 1))
        invalid = AuthenticationRequest("mine", {"token": "wrong"}, {}, ("127.0.0.1", 1))
        self.assertEqual(authenticator.authenticate(valid), "ben")
        self.assertIsNone(authenticator.authenticate(invalid))


class BackendInternalHopTest(unittest.TestCase):
    def connection(self, host, token, principal="ben"):
        return SimpleNamespace(
            remote_address=(host, 12345),
            request=SimpleNamespace(
                headers={
                    "X-Mobile-Terminal-Internal-Token": token,
                    "X-Mobile-Terminal-Principal": principal,
                }
            ),
        )

    def test_forwarded_principal_requires_loopback_and_internal_token(self):
        server = object.__new__(AppServer)
        server.internal_token = "internal-secret"
        self.assertEqual(
            server.internal_request_principal(self.connection("127.0.0.1", "internal-secret")),
            "ben",
        )
        self.assertIsNone(server.internal_request_principal(self.connection("127.0.0.1", "wrong")))
        self.assertIsNone(server.internal_request_principal(self.connection("192.0.2.1", "internal-secret")))
    def test_identity_header_requires_same_origin_browser_request(self):
        server = object.__new__(AppServer)
        connection = SimpleNamespace(
            remote_address=("127.0.0.1", 12345),
            request=SimpleNamespace(
                headers={
                    "Host": "terminal.example.ts.net",
                    "Origin": "https://terminal.example.ts.net",
                    "Tailscale-User-Login": "Ben@Example.COM",
                }
            ),
        )
        self.assertEqual(server.proxy_login(connection), "ben@example.com")
        connection.request.headers["Origin"] = "https://attacker.example"
        self.assertIsNone(server.proxy_login(connection))
        connection.request.headers.pop("Origin")
        self.assertIsNone(server.proxy_login(connection))


if __name__ == "__main__":
    unittest.main()
