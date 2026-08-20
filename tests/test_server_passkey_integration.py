import json
import unittest
from types import SimpleNamespace
from unittest import mock

from server import AppServer


class StubConnection:
    def __init__(self, messages=(), headers=None):
        self.messages = [json.dumps(message) for message in messages]
        self.sent = []
        self.closed = None
        self.remote_address = ("127.0.0.1", 12345)
        self.request = SimpleNamespace(
            path="/_ws",
            headers=headers
            or {
                "Host": "terminal.example.ts.net",
                "Origin": "https://terminal.example.ts.net",
                "User-Agent": "test browser",
            },
        )

    async def recv(self):
        return self.messages.pop(0)

    async def send(self, message):
        self.sent.append(json.loads(message) if isinstance(message, str) else message)

    async def close(self, code, reason):
        self.closed = (code, reason)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class StubPasskeys:
    def __init__(self, credentials=()):
        self.credentials = list(credentials)
        self.calls = []

    def list_credentials(self, realm):
        self.calls.append(("list", realm))
        return list(self.credentials)

    def begin_authentication(self, realm, *, principal, binding):
        self.calls.append(("begin-auth", realm, principal, binding))
        return {"type": "webauthn-auth-options", "challengeId": "auth-id", "options": {}}

    def finish_authentication(self, realm, challenge_id, assertion, *, binding):
        self.calls.append(("finish-auth", realm, challenge_id, binding))
        return {"principal": "standalone", "credentialId": "credential"}

    def begin_registration(self, realm, *, principal, user_name=None, user_display_name=None, label="device", binding=""):
        self.calls.append(("begin-register", realm, principal, binding))
        return {"type": "webauthn-register-options", "challengeId": "register-id", "options": {}}

    def finish_registration(self, realm, challenge_id, attestation, *, binding):
        self.calls.append(("finish-register", realm, challenge_id, binding))
        return {"principal": "standalone", "credentialId": "credential"}


class FakeBridge:
    def __init__(self, *args, **kwargs):
        self.process = None

    def open(self):
        pass

    async def read(self):
        return b""

    def close(self):
        pass

    def resize(self, cols, rows):
        pass


class StandalonePasskeyIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def app(self, passkeys):
        return AppServer(
            host="127.0.0.1",
            port=8085,
            session_name="mobile-terminal",
            shell="/bin/bash",
            cwd="/home/test",
            token="external-secret",
            require_token=True,
            allowed_clients=[],
            tailscale_mode=False,
            passkeys=passkeys,
        )

    async def test_config_advertises_plain_server_passkey_support(self):
        app = self.app(StubPasskeys())
        connection = StubConnection()
        request = SimpleNamespace(path="/config", headers=connection.request.headers)

        response = await app.process_request(connection, request)
        payload = json.loads(response.body)

        self.assertTrue(payload["deviceKeyAuth"])
        self.assertTrue(payload["passkeyAuth"])
        self.assertEqual(payload["rpId"], "terminal.example.ts.net")

    async def test_registered_passkey_authenticates_before_token_fallback(self):
        passkeys = StubPasskeys([{"credentialId": "credential", "principal": "standalone"}])
        app = self.app(passkeys)
        connection = StubConnection(
            [{"type": "webauthn-auth", "challengeId": "auth-id", "assertion": {}}]
        )

        record, fallback = await app.authenticate_passkey(
            connection,
            "",
            binding="connection-id",
        )

        self.assertEqual(record["credentialId"], "credential")
        self.assertIsNone(fallback)
        self.assertEqual(connection.sent[0]["type"], "webauthn-auth-options")
        self.assertIn(("finish-auth", "standalone", "auth-id", "connection-id"), passkeys.calls)

    async def test_passkey_prompt_can_fall_back_to_existing_token_frame(self):
        passkeys = StubPasskeys([{"credentialId": "credential", "principal": "standalone"}])
        app = self.app(passkeys)
        connection = StubConnection([{"type": "auth", "token": "external-secret"}])

        record, fallback = await app.authenticate_passkey(
            connection,
            "",
            binding="connection-id",
        )

        self.assertIsNone(record)
        self.assertEqual(fallback["token"], "external-secret")
        self.assertNotIn("finish-auth", [call[0] for call in passkeys.calls])

    async def test_public_token_bootstrap_registers_passkey_before_silent_key(self):
        passkeys = StubPasskeys()
        app = self.app(passkeys)
        connection = StubConnection(
            [{"type": "webauthn-register", "challengeId": "register-id", "attestation": {}}]
        )

        record = await app.register_passkey(
            connection,
            "",
            binding="connection-id",
        )

        self.assertEqual(record["credentialId"], "credential")
        self.assertEqual([message["type"] for message in connection.sent], ["webauthn-register-options"])
        self.assertIn(("finish-register", "standalone", "register-id", "connection-id"), passkeys.calls)

        state = {
            "user": "",
            "deviceId": "device-id",
            "authMethod": "passkey-bootstrap",
            "needsEnroll": True,
            "enrollPublic": True,
        }
        await app.maybe_enroll_device(connection, state)

        self.assertEqual(connection.sent[-1], {"type": "enroll-key"})

    async def test_public_websocket_does_not_reach_ready_before_passkey_registration(self):
        passkeys = StubPasskeys()
        app = self.app(passkeys)
        connection = StubConnection(
            [
                {"type": "auth", "token": "external-secret", "deviceId": "device-id"},
                {"type": "webauthn-register", "challengeId": "register-id", "attestation": {}},
            ],
            headers={
                "Host": "terminal.example.ts.net",
                "Origin": "https://terminal.example.ts.net",
                "User-Agent": "test browser",
                "Tailscale-Funnel-Request": "true",
            },
        )
        app.resolve_user_session = mock.Mock(return_value=("mobile-terminal", False))
        app.open_tabs_for = mock.Mock(return_value=[])
        app.send_tabs = mock.AsyncMock(return_value=[])
        app.send_sessions = mock.AsyncMock(return_value=[])
        app.send_settings = mock.AsyncMock(return_value={})
        app.send_composer_state = mock.AsyncMock()
        app.record_session = mock.Mock()

        with (
            mock.patch("server.device_pubkey", return_value=None),
            mock.patch("server.TmuxBridge", FakeBridge),
            mock.patch("server.pane_scrolls_locally", return_value=False),
            mock.patch("server.capture_history", return_value=""),
        ):
            await app.websocket_handler(connection)

        message_types = [message["type"] for message in connection.sent]
        self.assertLess(
            message_types.index("webauthn-register-options"),
            message_types.index("ready"),
        )
        self.assertEqual(message_types[-1], "enroll-key")
        self.assertIsNone(connection.closed)

    def test_environment_overrides_rp_id_and_origin(self):
        app = self.app(StubPasskeys())
        app.passkeys = None
        connection = StubConnection()
        with mock.patch.dict(
            "os.environ",
            {
                "MOBILE_TERMINAL_RP_ID": "example.ts.net",
                "MOBILE_TERMINAL_ORIGIN": "https://terminal.example.ts.net:8443",
            },
        ):
            manager = app.passkey_auth(connection)

        self.assertEqual(manager.rp_id, "example.ts.net")
        self.assertEqual(manager.expected_origin, "https://terminal.example.ts.net:8443")


if __name__ == "__main__":
    unittest.main()
