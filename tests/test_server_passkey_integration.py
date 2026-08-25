import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from server import AppServer
from webauthn_auth import device_enrollment_transcript


class StubConnection:
    def __init__(self, messages=(), headers=None, remote_address=("127.0.0.1", 12345)):
        self.messages = [json.dumps(message) for message in messages]
        self.sent = []
        self.closed = None
        self.remote_address = remote_address
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


class ReadySwitchConnection(StubConnection):
    def __init__(self, messages=(), target="alternate"):
        super().__init__(messages)
        self.target = target
        self.initial_pane_scroll = asyncio.Event()
        self.switched_pane_scroll = asyncio.Event()
        self.switch_sent = False

    async def send(self, message):
        await super().send(message)
        pane_scroll_count = sum(item.get("type") == "pane-scroll" for item in self.sent)
        if pane_scroll_count >= 1:
            self.initial_pane_scroll.set()
        if pane_scroll_count >= 2:
            self.switched_pane_scroll.set()

    async def __anext__(self):
        if not self.switch_sent:
            await self.initial_pane_scroll.wait()
            self.switch_sent = True
            return json.dumps({"type": "switch-session", "session": self.target})
        await self.switched_pane_scroll.wait()
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

    def begin_registration(
        self,
        realm,
        *,
        principal,
        user_name=None,
        user_display_name=None,
        label="device",
        binding="",
    ):
        self.calls.append(("begin-register", realm, principal, binding))
        return {"type": "webauthn-register-options", "challengeId": "register-id", "options": {}}

    def finish_registration(self, realm, challenge_id, attestation, *, binding):
        self.calls.append(("finish-register", realm, challenge_id, binding))
        return {"principal": "standalone", "credentialId": "credential"}


class FakeBridge:
    def __init__(self, *args, **kwargs):
        self.process = None
        self.session_name = args[1]
        self.pane_id = "%1"
        self.bytes_out = 0
        self.pane_change = asyncio.Event()

    async def open(self):
        pass

    async def close(self):
        pass

    async def reseed(self, *args, **kwargs):
        pass

    async def resize(self, cols, rows):
        pass

    def acknowledge(self, payload):
        return False


class StandalonePasskeyIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def app(self, passkeys, *, allowed_clients=(), require_token=True):
        return AppServer(
            host="127.0.0.1",
            port=8085,
            session_name="mobile-terminal",
            shell="/bin/bash",
            cwd="/home/test",
            token="external-secret" if require_token else None,
            require_token=require_token,
            allowed_clients=list(allowed_clients),
            tailscale_mode=False,
            passkeys=passkeys,
        )

    def prepare_handler(self, app):
        app.resolve_user_session = mock.Mock(return_value=("mobile-terminal", False))
        app.open_tabs_for = mock.Mock(return_value=[])
        app.send_tabs = mock.AsyncMock(return_value=[])
        app.send_sessions = mock.AsyncMock(return_value=[])
        app.send_settings = mock.AsyncMock(return_value={})
        app.send_composer_state = mock.AsyncMock()
        app.record_session = mock.Mock()

    async def run_handler(self, app, connection, *, public_key=None, signature_valid=False):
        self.prepare_handler(app)
        with (
            mock.patch("server.device_pubkey", return_value=public_key),
            mock.patch("server.verify_device_signature", return_value=signature_valid) as verify,
            mock.patch("server.TmuxBridge", FakeBridge),
            mock.patch("server.pane_scrolls_locally", return_value=False),
        ):
            await app.websocket_handler(connection)
        return verify

    @staticmethod
    def auth_frame(**updates):
        frame = {
            "type": "auth",
            "realm": "standalone",
            "profile": "",
            "deviceId": "device-id",
            "requirePasskey": False,
        }
        frame.update(updates)
        return frame

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

    async def test_existing_passkey_token_frame_is_returned_only_for_caller_rejection(self):
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

    async def test_ready_carries_scroll_ownership_on_connect_and_session_switch(self):
        app = self.app(StubPasskeys())
        connection = ReadySwitchConnection([self.auth_frame()])
        self.prepare_handler(app)
        app.resolve_user_session = mock.Mock(
            side_effect=lambda _user, requested: (requested or "mobile-terminal", False)
        )

        with (
            mock.patch("server.device_pubkey", return_value="enrolled-key"),
            mock.patch("server.verify_device_signature", return_value=True),
            mock.patch("server.TmuxBridge", FakeBridge),
            mock.patch("server.pane_scrolls_locally", return_value=False),
            mock.patch("server.pane_metadata", return_value=("%1",)),
        ):
            await asyncio.wait_for(app.websocket_handler(connection), timeout=3)

        ready = [message for message in connection.sent if message["type"] == "ready"]
        self.assertEqual(
            [(message["session"], message["paneLocalScroll"]) for message in ready],
            [("mobile-terminal", False), ("alternate", False)],
        )
        pane_scroll = [message for message in connection.sent if message["type"] == "pane-scroll"]
        self.assertEqual([message["local"] for message in pane_scroll], [False, False])

    async def test_public_private_trusted_and_identity_bootstrap_require_registration(self):
        cases = (
            (
                "public",
                self.app(StubPasskeys()),
                StubConnection(
                    [],
                    headers={
                        "Host": "terminal.example.ts.net",
                        "Origin": "https://terminal.example.ts.net",
                        "User-Agent": "test browser",
                        "Tailscale-Funnel-Request": "true",
                    },
                ),
                {"token": "external-secret"},
                {},
            ),
            (
                "private",
                self.app(StubPasskeys()),
                StubConnection(),
                {"token": "external-secret"},
                {},
            ),
            (
                "trusted",
                self.app(StubPasskeys(), allowed_clients=("100.64.0.5",)),
                StubConnection(remote_address=("100.64.0.5", 12345)),
                {},
                {},
            ),
            (
                "identity",
                self.app(StubPasskeys()),
                StubConnection(
                    headers={
                        "Host": "terminal.example.ts.net",
                        "Origin": "https://terminal.example.ts.net",
                        "User-Agent": "test browser",
                        "Tailscale-User-Login": "ben@example.com",
                    }
                ),
                {},
                {"MOBILE_TERMINAL_TRUST_IDENTITY": "true"},
            ),
        )
        for name, app, connection, auth_updates, environment in cases:
            with self.subTest(name=name), mock.patch.dict("os.environ", environment, clear=False):
                connection.messages.extend(
                    json.dumps(message)
                    for message in (
                        self.auth_frame(**auth_updates),
                        {
                            "type": "webauthn-register",
                            "challengeId": "register-id",
                            "attestation": {},
                        },
                    )
                )
                await self.run_handler(app, connection)

                message_types = [message["type"] for message in connection.sent]
                self.assertLess(
                    message_types.index("webauthn-register-options"),
                    message_types.index("ready"),
                )
                self.assertIn("enroll-key", message_types)
                self.assertIsNone(connection.closed)

    async def test_public_private_trusted_and_identity_silent_key_matrix(self):
        access_modes = ("public", "private", "trusted", "identity")
        key_cases = (
            ("valid", "enrolled-key", True, False),
            ("invalid", "enrolled-key", False, True),
            ("missing", None, False, True),
        )
        for access_mode in access_modes:
            for key_name, public_key, signature_valid, needs_passkey in key_cases:
                with self.subTest(access_mode=access_mode, key=key_name):
                    allowed_clients = ("100.64.0.5",) if access_mode == "trusted" else ()
                    app = self.app(
                        StubPasskeys(
                            [{"credentialId": "credential", "principal": "standalone"}]
                        ),
                        allowed_clients=allowed_clients,
                    )
                    headers = None
                    remote_address = (
                        ("100.64.0.5", 12345)
                        if access_mode == "trusted"
                        else ("127.0.0.1", 12345)
                    )
                    environment = {}
                    if access_mode == "public":
                        headers = {
                            "Host": "terminal.example.ts.net",
                            "Origin": "https://terminal.example.ts.net",
                            "User-Agent": "test browser",
                            "Tailscale-Funnel-Request": "true",
                        }
                    elif access_mode == "identity":
                        headers = {
                            "Host": "terminal.example.ts.net",
                            "Origin": "https://terminal.example.ts.net",
                            "User-Agent": "test browser",
                            "Tailscale-User-Login": "ben@example.com",
                        }
                        environment = {"MOBILE_TERMINAL_TRUST_IDENTITY": "true"}
                    messages = [self.auth_frame(signature="device-signature")]
                    if needs_passkey:
                        messages.append(
                            {"type": "webauthn-auth", "challengeId": "auth-id", "assertion": {}}
                        )
                    connection = StubConnection(
                        messages,
                        headers=headers,
                        remote_address=remote_address,
                    )

                    with mock.patch.dict("os.environ", environment, clear=False):
                        await self.run_handler(
                            app,
                            connection,
                            public_key=public_key,
                            signature_valid=signature_valid,
                        )

                    message_types = [message["type"] for message in connection.sent]
                    self.assertIsNone(connection.closed)
                    self.assertIn("ready", message_types)
                    self.assertEqual("webauthn-auth-options" in message_types, needs_passkey)
                    self.assertEqual(
                        any(call[0] == "finish-auth" for call in app.passkeys.calls),
                        needs_passkey,
                    )

    async def test_require_passkey_shape_matrix_allows_silent_key_only_for_literal_false(self):
        missing = object()
        cases = (
            ("missing", missing, True),
            ("null", None, True),
            ("string", "false", True),
            ("true", True, True),
            ("false", False, False),
        )
        for name, value, needs_passkey in cases:
            with self.subTest(name=name):
                passkeys = StubPasskeys(
                    [{"credentialId": "credential", "principal": "standalone"}]
                )
                auth = self.auth_frame(signature="valid-device-signature")
                if value is missing:
                    del auth["requirePasskey"]
                else:
                    auth["requirePasskey"] = value
                messages = [auth]
                if needs_passkey:
                    messages.append(
                        {"type": "webauthn-auth", "challengeId": "auth-id", "assertion": {}}
                    )
                connection = StubConnection(messages)

                verify = await self.run_handler(
                    self.app(passkeys),
                    connection,
                    public_key="enrolled-key",
                    signature_valid=True,
                )

                self.assertIsNone(connection.closed)
                self.assertEqual(verify.call_count, 0 if needs_passkey else 1)
                self.assertEqual(
                    "webauthn-auth-options" in [message["type"] for message in connection.sent],
                    needs_passkey,
                )

    async def test_invalid_token_uses_rejected_token_close_reason(self):
        connection = StubConnection([self.auth_frame(token="wrong-token")])

        await self.run_handler(self.app(StubPasskeys()), connection)

        self.assertEqual(connection.closed, (4001, "token rejected"))
        self.assertEqual(connection.sent[-1]["message"], "Invalid access token.")

    async def test_existing_passkey_cannot_fall_back_to_token(self):
        passkeys = StubPasskeys([{"credentialId": "credential", "principal": "standalone"}])
        connection = StubConnection(
            [
                self.auth_frame(requirePasskey=True),
                self.auth_frame(token="external-secret", requirePasskey=True),
            ]
        )

        await self.run_handler(
            self.app(passkeys),
            connection,
            public_key="enrolled-key",
            signature_valid=True,
        )

        self.assertEqual(connection.closed, (4001, "auth failed"))
        self.assertEqual(connection.sent[-1]["message"], "Passkey authentication is required.")
        self.assertNotIn("ready", [message["type"] for message in connection.sent])

    async def test_realm_or_profile_mismatch_fails_before_key_or_passkey_verification(self):
        for field, value in (("realm", "other"), ("profile", "powerhouse")):
            with self.subTest(field=field):
                passkeys = StubPasskeys(
                    [{"credentialId": "credential", "principal": "standalone"}]
                )
                connection = StubConnection([self.auth_frame(**{field: value})])
                verify = await self.run_handler(
                    self.app(passkeys),
                    connection,
                    public_key="enrolled-key",
                    signature_valid=True,
                )

                self.assertEqual(connection.closed, (4001, "auth failed"))
                self.assertEqual(verify.call_count, 0)
                self.assertEqual(passkeys.calls, [])

    async def test_enrollment_proof_is_consumed_and_cannot_replay(self):
        app = self.app(StubPasskeys())
        connection = StubConnection()
        state = {
            "user": "",
            "deviceId": "device-id",
            "needsEnroll": True,
            "enrollment": None,
        }
        await app.maybe_enroll_device(connection, state)
        enrollment = state["enrollment"]
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = base64.b64encode(
            private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode("ascii")
        transcript = device_enrollment_transcript(
            enrollment.rp_id,
            enrollment.realm,
            enrollment.profile,
            enrollment.enrollment_id,
            enrollment.nonce,
            enrollment.device_id,
            public_key,
        )
        der_signature = private_key.sign(transcript, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_signature)
        payload = {
            **enrollment.message(),
            "type": "register-key",
            "deviceId": "device-id",
            "publicKey": public_key,
            "signature": base64.b64encode(
                r.to_bytes(32, "big") + s.to_bytes(32, "big")
            ).decode("ascii"),
        }

        with mock.patch("server.register_device_key") as register:
            await app.handle_register_key(connection, state, payload)
            await app.handle_register_key(connection, state, payload)

        register.assert_called_once_with("", "device-id", public_key, "device")
        self.assertEqual(
            [message["type"] for message in connection.sent[-2:]],
            ["register-key-ok", "register-key-error"],
        )
        self.assertEqual(connection.sent[-2]["enrollmentId"], enrollment.enrollment_id)
        self.assertIsNone(state["enrollment"])

    async def test_enrollment_failure_is_acknowledged_and_retried_after_reauthentication(self):
        app = self.app(StubPasskeys())
        connection = StubConnection()
        state = {
            "user": "",
            "deviceId": "device-id",
            "needsEnroll": True,
            "enrollment": None,
        }
        await app.maybe_enroll_device(connection, state)
        enrollment = state["enrollment"]
        payload = {
            **enrollment.message(),
            "type": "register-key",
            "deviceId": "device-id",
            "publicKey": "invalid",
            "signature": "invalid",
        }

        with mock.patch("server.register_device_key") as register:
            await app.handle_register_key(connection, state, payload)

        register.assert_not_called()
        self.assertEqual(
            connection.sent[-1],
            {
                "type": "register-key-error",
                "enrollmentId": enrollment.enrollment_id,
            },
        )
        self.assertTrue(state["needsEnroll"])
        self.assertIsNone(state["enrollment"])

    async def test_killing_active_session_switches_on_authenticated_socket(self):
        app = self.app(StubPasskeys())
        connection = StubConnection()
        bridge = SimpleNamespace()
        switch_session = mock.AsyncMock()
        sessions = [
            {"name": "mobile-terminal", "attached": 1, "windows": 1},
            {"name": "fallback", "attached": 0, "windows": 1},
        ]

        with (
            mock.patch("server.list_sessions", return_value=sessions),
            mock.patch("server.tmux_capture") as tmux,
        ):
            await app.handle_command(
                connection,
                bridge,
                {"session": "mobile-terminal", "user": ""},
                {"type": "kill-session", "session": "mobile-terminal"},
                switch_session=switch_session,
            )

        self.assertEqual(connection.sent[0]["type"], "session-closing")
        self.assertEqual(connection.sent[0]["nextSession"], "fallback")
        switch_session.assert_awaited_once_with("fallback")
        self.assertIsNone(connection.closed)
        tmux.assert_called_once_with(
            "kill-session",
            "-t",
            "mobile-terminal",
            check=False,
        )

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
