import asyncio
import base64
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from mobile_terminal_config import AuthRealmConfig, ProfileConfig, ProxyConfig
from proxy import INTERNAL_TOKEN_HEADER, PRINCIPAL_HEADER, PROFILE_HEADER, ProxyServer
from webauthn_auth import (
    PasskeyAuth,
    PasskeyVerificationError,
    PendingDeviceEnrollment,
    device_authentication_transcript,
    device_enrollment_transcript,
)


def device_key_pair_and_signature(transcript):
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode("ascii")
    der_signature = private_key.sign(transcript, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_signature)
    signature = base64.b64encode(
        r.to_bytes(32, "big") + s.to_bytes(32, "big")
    ).decode("ascii")
    return private_key, public_key, signature


def enrollment_payload(ticket):
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode("ascii")
    transcript = device_enrollment_transcript(
        ticket.rp_id,
        ticket.realm,
        ticket.profile,
        ticket.enrollment_id,
        ticket.nonce,
        ticket.device_id,
        public_key,
    )
    der_signature = private_key.sign(transcript, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_signature)
    return {
        **ticket.message(),
        "type": "register-key",
        "deviceId": ticket.device_id,
        "publicKey": public_key,
        "signature": base64.b64encode(
            r.to_bytes(32, "big") + s.to_bytes(32, "big")
        ).decode("ascii"),
    }


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


class RelayConnection(StubConnection):
    def __init__(self):
        super().__init__()
        self.incoming = asyncio.Queue()

    async def recv(self):
        return await self.incoming.get()


class StubPasskeys:
    def __init__(self, credentials=()):
        self.credentials = list(credentials)
        self.calls = []
        self.authentication_error = None

    def list_credentials(self, realm):
        self.calls.append(("list", realm))
        return list(self.credentials)

    def begin_authentication(self, realm, *, principal, binding):
        self.calls.append(("begin-auth", realm, principal, binding))
        return {"type": "webauthn-auth-options", "challengeId": "auth-id", "options": {}}

    def finish_authentication(self, realm, challenge_id, assertion, *, binding):
        self.calls.append(("finish-auth", realm, challenge_id, binding))
        if self.authentication_error:
            raise self.authentication_error
        return {"principal": "ben", "credentialId": "credential"}

    def begin_registration(self, realm, *, principal, user_name=None, user_display_name=None, label="device", binding=""):
        self.calls.append(("begin-register", realm, principal, binding))
        return {"type": "webauthn-register-options", "challengeId": "register-id", "options": {}}

    def finish_registration(self, realm, challenge_id, attestation, *, binding):
        self.calls.append(("finish-register", realm, challenge_id, binding))
        self.credentials.append({"principal": "ben", "credentialId": "credential"})
        return {"principal": "ben", "credentialId": "credential"}

    def revoke_credential(self, realm, credential_id, *, principal=None):
        self.calls.append(("revoke", realm, credential_id, principal))
        if principal != "ben":
            raise PasskeyVerificationError("credential owner mismatch")
        self.credentials = [
            credential for credential in self.credentials
            if credential.get("credentialId") != credential_id
        ]
        return True

    def revoke_all_credentials(self, realm, *, principal=None):
        self.calls.append(("revoke-all", realm, principal))
        if principal != "ben":
            raise PasskeyVerificationError("credential owner mismatch")
        count = len(self.credentials)
        self.credentials = []
        return count


class ProxyRelayTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mt-proxy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.test_root = Path(self.temporary.name).resolve()
        self.state_dir = self.test_root / "state"
        self.static_root = self.test_root / "static"
        self.node_modules_root = self.test_root / "node_modules"
        self.static_root.mkdir()
        self.node_modules_root.mkdir()

    async def receive_json(self, connection):
        message = await connection.recv()
        self.assertIsInstance(message, str)
        return json.loads(message)

    def passkey_proxy(self, passkeys, authenticate=None, state_dir=None):
        state_dir = self.state_dir if state_dir is None else Path(state_dir).resolve()
        profile = ProfileConfig(
            id="powerhouse",
            label="Powerhouse",
            auth_realm="mine",
            backend="ws://127.0.0.1:8090",
        )
        config = ProxyConfig(
            path=self.test_root / "config.json",
            host="127.0.0.1",
            port=8085,
            state_dir=state_dir,
            label="ps",
            auth_realms={
                "mine": AuthRealmConfig(
                    id="mine",
                    token="external-secret",
                    principal="ben",
                    device_key_auth=True,
                )
            },
            profiles=(profile,),
            active_profile="powerhouse",
            internal_token="internal-secret",
            rp_id="example.ts.net",
            expected_origin="https://terminal.example.ts.net",
        )
        proxy = ProxyServer(
            config,
            static_root=self.static_root,
            node_modules_root=self.node_modules_root,
            render_icon=lambda _label, _size: b"",
            authenticate=authenticate,
            passkeys=passkeys,
        )
        return proxy, profile

    def test_proxy_constructs_passkey_manager_from_explicit_config(self):
        with tempfile.TemporaryDirectory() as root:
            state_dir = Path(root) / "state"
            config = ProxyConfig(
                path=Path(root) / "config.json",
                host="127.0.0.1",
                port=8085,
                state_dir=state_dir,
                label="ps",
                auth_realms={
                    "mine": AuthRealmConfig(
                        id="mine",
                        token="external-secret",
                        principal="ben",
                        device_key_auth=True,
                    )
                },
                profiles=(
                    ProfileConfig(
                        id="powerhouse",
                        label="Powerhouse",
                        auth_realm="mine",
                        backend="ws://127.0.0.1:8090",
                    ),
                ),
                active_profile="powerhouse",
                internal_token="internal-secret",
                rp_id="example.ts.net",
                expected_origin="https://terminal.example.ts.net",
            )
            proxy = ProxyServer(
                config,
                static_root=Path(root),
                node_modules_root=Path(root),
                render_icon=lambda _label, _size: b"",
            )

        self.assertIsInstance(proxy.passkeys, PasskeyAuth)
        self.assertEqual(proxy.passkeys.state_dir, state_dir / "realms")
        self.assertEqual(proxy.passkeys.store_filename, "passkeys.json")
        self.assertEqual(proxy.passkeys.rp_id, "example.ts.net")
        self.assertEqual(proxy.passkeys.expected_origin, "https://terminal.example.ts.net")

    def test_default_state_root_is_absolute_and_ignores_repo_sentinel(self):
        repository = self.test_root / "repository"
        sentinel = repository / "state" / "sentinel"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("untouched")
        _private_key, public_key, _signature = device_key_pair_and_signature(b"test")

        with contextlib.chdir(repository):
            proxy, _profile = self.passkey_proxy(StubPasskeys())
            self.assertTrue(proxy.config.state_dir.is_absolute())
            self.assertTrue(
                proxy.device_keys.register(
                    "mine",
                    "device-id",
                    public_key,
                    "ben",
                    "test",
                )
            )

        self.assertEqual(sentinel.read_text(), "untouched")
        self.assertEqual(list(sentinel.parent.iterdir()), [sentinel])
        self.assertTrue((self.state_dir / "realms" / "mine" / "device-keys.json").is_file())

    async def test_passkey_authentication_precedes_token_bootstrap(self):
        passkeys = StubPasskeys([{"credentialId": "credential", "principal": "ben"}])

        def reject_token(_request):
            self.fail("token authenticator must not run after a passkey assertion")

        proxy, profile = self.passkey_proxy(passkeys, reject_token)
        connection = StubConnection(
            [
                {
                    "type": "auth",
                    "realm": "mine",
                    "profile": "powerhouse",
                    "requirePasskey": True,
                },
                {"type": "webauthn-auth", "challengeId": "auth-id", "assertion": {}},
            ]
        )

        principal = await proxy.authenticate_realm(connection, profile, binding="connection-id")

        self.assertEqual(principal, "ben")
        self.assertEqual(
            [message["type"] for message in connection.sent],
            ["auth-challenge", "webauthn-auth-options"],
        )
        self.assertEqual(connection.sent[1]["realm"], "mine")
        self.assertIn(("finish-auth", "mine", "auth-id", "connection-id"), passkeys.calls)

    async def test_token_is_bootstrap_only_until_passkey_registration_finishes(self):
        passkeys = StubPasskeys()
        proxy, profile = self.passkey_proxy(
            passkeys,
            lambda request: "ben" if request.credentials.get("token") == "external-secret" else None,
        )
        connection = StubConnection(
            [
                {
                    "type": "auth",
                    "realm": "mine",
                    "profile": "powerhouse",
                    "token": "external-secret",
                },
                {
                    "type": "webauthn-register",
                    "challengeId": "register-id",
                    "attestation": {},
                },
            ]
        )

        principal = await proxy.authenticate_realm(connection, profile, binding="connection-id")

        self.assertEqual(principal, "ben")
        self.assertEqual(
            [message["type"] for message in connection.sent],
            ["auth-challenge", "webauthn-register-options"],
        )
        self.assertIn(("finish-register", "mine", "register-id", "connection-id"), passkeys.calls)

    async def test_enrolled_device_key_authenticates_silently_per_realm(self):
        transcript = device_authentication_transcript(
            "example.ts.net", "mine", "powerhouse", "fixed-nonce"
        )
        _private_key, public_key, signature = device_key_pair_and_signature(transcript)

        with tempfile.TemporaryDirectory() as root:
            proxy, profile = self.passkey_proxy(
                StubPasskeys([{"credentialId": "credential", "principal": "ben"}]),
                lambda _request: self.fail("token must not authorize an enrolled device"),
                Path(root),
            )
            self.assertTrue(
                proxy.device_keys.register("mine", "device-id", public_key, "ben", "test")
            )
            connection = StubConnection(
                [
                    {
                        "type": "auth",
                        "realm": "mine",
                        "profile": "powerhouse",
                        "deviceId": "device-id",
                        "signature": signature,
                        "requirePasskey": False,
                    }
                ]
            )
            with mock.patch("proxy.secrets.token_urlsafe", return_value="fixed-nonce"):
                principal = await proxy.authenticate_realm(
                    connection,
                    profile,
                    binding="connection-id",
                )

            self.assertEqual(principal, "ben")
            self.assertEqual([message["type"] for message in connection.sent], ["auth-challenge"])
            self.assertTrue((Path(root) / "realms" / "mine" / "device-keys.json").is_file())

    async def test_invalid_device_key_requires_passkey_and_schedules_enrollment(self):
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = base64.b64encode(
            private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode("ascii")
        passkeys = StubPasskeys([{"credentialId": "credential", "principal": "ben"}])
        with tempfile.TemporaryDirectory() as root:
            proxy, profile = self.passkey_proxy(
                passkeys,
                lambda _request: self.fail("token must not replace an invalid device key"),
                Path(root),
            )
            proxy.device_keys.register("mine", "device-id", public_key, "ben", "test")
            connection = StubConnection(
                [
                    {
                        "type": "auth",
                        "realm": "mine",
                        "profile": "powerhouse",
                        "deviceId": "device-id",
                        "signature": "invalid",
                        "requirePasskey": False,
                    },
                    {"type": "webauthn-auth", "challengeId": "auth-id", "assertion": {}},
                ]
            )
            enrollment = {}

            principal = await proxy.authenticate_realm(
                connection,
                profile,
                binding="connection-id",
                enrollment=enrollment,
            )

        self.assertEqual(principal, "ben")
        self.assertEqual(
            [message["type"] for message in connection.sent],
            ["auth-challenge", "webauthn-auth-options"],
        )
        self.assertEqual(
            enrollment,
            {
                "realm": "mine",
                "profile": "powerhouse",
                "deviceId": "device-id",
                "principal": "ben",
            },
        )

    async def test_missing_device_key_requires_passkey_and_schedules_enrollment(self):
        passkeys = StubPasskeys([{"credentialId": "credential", "principal": "ben"}])
        with tempfile.TemporaryDirectory() as root:
            proxy, profile = self.passkey_proxy(passkeys, state_dir=Path(root))
            connection = StubConnection(
                [
                    {
                        "type": "auth",
                        "realm": "mine",
                        "profile": "powerhouse",
                        "deviceId": "device-id",
                        "requirePasskey": False,
                    },
                    {"type": "webauthn-auth", "challengeId": "auth-id", "assertion": {}},
                ]
            )
            enrollment = {}

            principal = await proxy.authenticate_realm(
                connection,
                profile,
                binding="connection-id",
                enrollment=enrollment,
            )

        self.assertEqual(principal, "ben")
        self.assertEqual(enrollment["deviceId"], "device-id")
        self.assertIn(("finish-auth", "mine", "auth-id", "connection-id"), passkeys.calls)

    async def test_require_passkey_shape_matrix_allows_silent_key_only_for_literal_false(self):
        transcript = device_authentication_transcript(
            "example.ts.net", "mine", "powerhouse", "fixed-nonce"
        )
        _private_key, public_key, signature = device_key_pair_and_signature(transcript)
        missing = object()
        cases = (
            ("missing", missing, True),
            ("null", None, True),
            ("string", "false", True),
            ("true", True, True),
            ("false", False, False),
        )

        for name, value, needs_passkey in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                passkeys = StubPasskeys(
                    [{"credentialId": "credential", "principal": "ben"}]
                )
                proxy, profile = self.passkey_proxy(passkeys, state_dir=Path(root))
                proxy.device_keys.register("mine", "device-id", public_key, "ben", "test")
                auth = {
                    "type": "auth",
                    "realm": "mine",
                    "profile": "powerhouse",
                    "deviceId": "device-id",
                    "signature": signature,
                }
                if value is not missing:
                    auth["requirePasskey"] = value
                messages = [auth]
                if needs_passkey:
                    messages.append(
                        {"type": "webauthn-auth", "challengeId": "auth-id", "assertion": {}}
                    )
                connection = StubConnection(messages)
                with mock.patch("proxy.secrets.token_urlsafe", return_value="fixed-nonce"):
                    principal = await proxy.authenticate_realm(
                        connection,
                        profile,
                        binding="connection-id",
                    )

                self.assertEqual(principal, "ben")
                self.assertEqual(
                    any(call[0] == "finish-auth" for call in passkeys.calls),
                    needs_passkey,
                )

    async def test_forget_key_requires_matching_principal(self):
        _private_key, public_key, _signature = device_key_pair_and_signature(b"test")

        with tempfile.TemporaryDirectory() as root:
            proxy, profile = self.passkey_proxy(StubPasskeys(), state_dir=Path(root))
            self.assertTrue(
                proxy.device_keys.register(
                    "mine",
                    "device-id",
                    public_key,
                    "ben",
                    "test",
                )
            )
            store_path = Path(root) / "realms" / "mine" / "device-keys.json"
            original = store_path.read_text()
            payload = {
                "type": "forget-key",
                "realm": "mine",
                "profile": "powerhouse",
                "deviceId": "device-id",
            }

            rejected = (
                ("other-user", payload),
                ("", payload),
                ("ben", {**payload, "realm": "other"}),
                ("ben", {**payload, "profile": "other"}),
                ("ben", {**payload, "deviceId": "missing-device"}),
            )
            for principal, rejected_payload in rejected:
                with self.subTest(principal=principal, payload=rejected_payload):
                    connection = StubConnection()
                    self.assertTrue(
                        await proxy._handle_device_key_message(
                            connection,
                            profile,
                            principal,
                            {},
                            rejected_payload,
                        )
                    )
                    self.assertEqual(connection.sent, [])
                    self.assertEqual(store_path.read_text(), original)

            pre_auth = StubConnection([payload])
            self.assertIsNone(await proxy.authenticate_realm(pre_auth, profile))
            self.assertEqual(
                [message["type"] for message in pre_auth.sent],
                ["auth-challenge"],
            )
            self.assertEqual(store_path.read_text(), original)

            connection = StubConnection()
            self.assertTrue(
                await proxy._handle_device_key_message(
                    connection, profile, "ben", {}, payload
                )
            )
            self.assertEqual(connection.sent, [])
            self.assertEqual(json.loads(store_path.read_text())["devices"], {})

    async def test_enrollment_ticket_survives_profile_switch_and_rejects_replay(self):
        with tempfile.TemporaryDirectory() as root:
            proxy, profile = self.passkey_proxy(StubPasskeys(), state_dir=Path(root))
            other_profile = ProfileConfig(
                id="other",
                label="Other",
                auth_realm="mine",
                backend="ws://127.0.0.1:8091",
            )
            ticket = PendingDeviceEnrollment.issue(
                rp_id="example.ts.net",
                realm="mine",
                profile=profile.id,
                device_id="device-id",
                principal="ben",
                now=100.0,
            )
            payload = enrollment_payload(ticket)
            pending = {ticket.enrollment_id: ticket}
            connection = StubConnection()
            with (
                mock.patch("proxy.time.monotonic", return_value=101.0),
                mock.patch.object(proxy.device_keys, "register") as register,
            ):
                self.assertTrue(
                    await proxy._handle_device_key_message(
                        connection, other_profile, "ben", pending, payload
                    )
                )
                self.assertTrue(
                    await proxy._handle_device_key_message(
                        connection, other_profile, "ben", pending, payload
                    )
                )

            register.assert_called_once_with(
                "mine",
                "device-id",
                payload["publicKey"],
                "ben",
                "test browser",
            )
            self.assertEqual(pending, {})

    async def test_enrollment_rejects_bad_mismatch_expired_and_preserves_unrelated_ticket(self):
        cases = ("bad-signature", "mismatch", "expired")
        for name in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                proxy, profile = self.passkey_proxy(StubPasskeys(), state_dir=Path(root))
                ticket = PendingDeviceEnrollment.issue(
                    rp_id="example.ts.net",
                    realm="mine",
                    profile=profile.id,
                    device_id="device-id",
                    principal="ben",
                    now=100.0,
                )
                payload = enrollment_payload(ticket)
                now = 101.0
                if name == "bad-signature":
                    payload["signature"] = base64.b64encode(b"\0" * 64).decode("ascii")
                elif name == "mismatch":
                    payload["realm"] = "other"
                else:
                    now = ticket.expires_at
                pending = {ticket.enrollment_id: ticket}
                with (
                    mock.patch("proxy.time.monotonic", return_value=now),
                    mock.patch.object(proxy.device_keys, "register") as register,
                ):
                    self.assertTrue(
                        await proxy._handle_device_key_message(
                            StubConnection(), profile, "ben", pending, payload
                        )
                    )
                register.assert_not_called()
                self.assertEqual(pending, {})

        proxy, profile = self.passkey_proxy(StubPasskeys())
        ticket = PendingDeviceEnrollment.issue(
            rp_id="example.ts.net",
            realm="mine",
            profile=profile.id,
            device_id="device-id",
            principal="ben",
        )
        pending = {ticket.enrollment_id: ticket}
        payload = {**enrollment_payload(ticket), "enrollmentId": "unrelated-id"}
        with mock.patch.object(proxy.device_keys, "register") as register:
            self.assertTrue(
                await proxy._handle_device_key_message(
                    StubConnection(), profile, "ben", pending, payload
                )
            )
        register.assert_not_called()
        self.assertIn(ticket.enrollment_id, pending)

    async def test_enrollment_seed_waits_for_its_bound_profile_ready(self):
        async def backend_handler(connection):
            await connection.send(
                json.dumps(
                    {
                        "type": "ready",
                        "session": "shell",
                        "openTabs": ["shell"],
                        "multiTenant": False,
                    }
                )
            )

        async with serve(backend_handler, "127.0.0.1", 0) as backend_server:
            backend_port = backend_server.sockets[0].getsockname()[1]
            proxy, _profile = self.passkey_proxy(StubPasskeys())
            other_profile = ProfileConfig(
                id="other",
                label="Other",
                auth_realm="mine",
                backend=f"ws://127.0.0.1:{backend_port}",
            )
            seed = {
                "realm": "mine",
                "profile": "powerhouse",
                "deviceId": "device-id",
                "principal": "ben",
            }
            pending = {}
            connection = RelayConnection()

            relay = asyncio.create_task(
                proxy._relay_profile(
                    connection,
                    other_profile,
                    "ben",
                    "",
                    seed,
                    pending,
                )
            )
            for _ in range(100):
                if any(message.get("type") == "ready" for message in connection.sent):
                    break
                await asyncio.sleep(0.01)
            await connection.incoming.put(
                json.dumps({"type": "switch-profile", "profile": "powerhouse"})
            )
            await asyncio.wait_for(relay, timeout=2)

        self.assertNotIn("enroll-key", [message.get("type") for message in connection.sent])
        self.assertEqual(pending, {})
        self.assertEqual(seed["profile"], "powerhouse")

    async def test_passkey_failures_are_generic_and_do_not_expose_verification_details(self):
        passkeys = StubPasskeys([{"credentialId": "credential", "principal": "ben"}])
        passkeys.authentication_error = PasskeyVerificationError("sensitive credential detail")
        proxy, _profile = self.passkey_proxy(passkeys)
        connection = StubConnection(
            [
                {
                    "type": "auth",
                    "realm": "mine",
                    "profile": "powerhouse",
                    "requirePasskey": True,
                },
                {"type": "webauthn-auth", "challengeId": "auth-id", "assertion": {}},
            ]
        )

        await proxy.websocket_handler(connection)

        self.assertEqual(connection.sent[-1]["type"], "auth-error")
        self.assertEqual(connection.sent[-1]["message"], "Passkey verification failed.")
        self.assertNotIn("sensitive", json.dumps(connection.sent))
        self.assertEqual(connection.closed, (4001, "auth failed"))

    async def test_unauthorized_passkey_revocations_are_indistinguishable(self):
        messages = (
            {"type": "revoke-credential", "credentialId": "credential"},
            {"type": "revoke-credential", "credentialId": "missing"},
            {"type": "revoke-all-credentials"},
        )
        expected = [{"type": "notice", "message": "Passkey operation failed."}]

        for principal in ("", "other-user"):
            for payload in messages:
                with self.subTest(principal=principal, payload=payload):
                    passkeys = StubPasskeys(
                        [{"credentialId": "credential", "principal": "ben"}]
                    )
                    proxy, profile = self.passkey_proxy(passkeys)
                    connection = StubConnection()

                    self.assertTrue(
                        await proxy._handle_credential_message(
                            connection,
                            profile,
                            principal,
                            payload,
                        )
                    )
                    self.assertEqual(
                        passkeys.credentials,
                        [{"credentialId": "credential", "principal": "ben"}],
                    )
                    self.assertEqual(connection.sent, expected)

            passkeys = StubPasskeys(
                [{"credentialId": "credential", "principal": "ben"}]
            )
            proxy, profile = self.passkey_proxy(passkeys)
            connection = StubConnection()
            self.assertFalse(
                await proxy._cascade_passkeys_for_rotation(
                    connection,
                    profile,
                    principal,
                    {"type": "rotate-token", "cascadePasskeys": True},
                )
            )
            self.assertEqual(
                passkeys.credentials,
                [{"credentialId": "credential", "principal": "ben"}],
            )
            self.assertEqual(connection.sent, expected)

        passkeys = StubPasskeys(
            [{"credentialId": "credential", "principal": "ben"}]
        )
        proxy, profile = self.passkey_proxy(passkeys)
        pre_auth = StubConnection(
            [{"type": "revoke-credential", "credentialId": "credential"}]
        )
        self.assertIsNone(await proxy.authenticate_realm(pre_auth, profile))
        self.assertEqual(
            passkeys.credentials,
            [{"credentialId": "credential", "principal": "ben"}],
        )
        self.assertEqual(
            [message["type"] for message in pre_auth.sent],
            ["auth-challenge"],
        )

    async def test_passkey_credentials_can_be_listed_and_revoked_per_realm(self):
        passkeys = StubPasskeys([{"credentialId": "credential", "principal": "ben"}])
        proxy, profile = self.passkey_proxy(passkeys)
        connection = StubConnection()

        self.assertTrue(
            await proxy._handle_credential_message(
                connection,
                profile,
                "ben",
                {"type": "request-devices"},
            )
        )
        self.assertEqual(connection.sent[-1]["devices"][0]["credentialId"], "credential")
        self.assertTrue(
            await proxy._handle_credential_message(
                connection,
                profile,
                "ben",
                {"type": "revoke-credential", "credentialId": "credential"},
            )
        )
        self.assertEqual(connection.sent[-1], {"type": "devices", "devices": []})
        self.assertIn(("revoke", "mine", "credential", "ben"), passkeys.calls)
        passkeys.credentials = [{"credentialId": "other", "principal": "ben"}]
        self.assertTrue(
            await proxy._handle_credential_message(
                connection,
                profile,
                "ben",
                {"type": "revoke-all-credentials"},
            )
        )
        self.assertIn(("revoke-all", "mine", "ben"), passkeys.calls)
        self.assertEqual(connection.sent[-1], {"type": "devices", "devices": []})

        passkeys.credentials = [{"credentialId": "rotated", "principal": "ben"}]
        self.assertTrue(
            await proxy._cascade_passkeys_for_rotation(
                connection,
                profile,
                "ben",
                {"type": "rotate-token", "cascadePasskeys": True},
            )
        )
        self.assertEqual(passkeys.credentials, [])

    async def test_rotate_token_request_cascades_before_reaching_backend(self):
        received_rotation = asyncio.Event()

        async def backend_handler(connection):
            async for message in connection:
                payload = json.loads(message)
                if payload.get("type") == "rotate-token":
                    received_rotation.set()

        async with serve(backend_handler, "127.0.0.1", 0) as backend_server:
            backend_port = backend_server.sockets[0].getsockname()[1]
            passkeys = StubPasskeys([{"credentialId": "credential", "principal": "ben"}])
            profile = ProfileConfig(
                id="powerhouse",
                label="Powerhouse",
                auth_realm="mine",
                backend=f"ws://127.0.0.1:{backend_port}",
            )
            config = ProxyConfig(
                path=self.test_root / "rotation-config.json",
                host="127.0.0.1",
                port=8085,
                state_dir=self.test_root / "rotation-state",
                label="ps",
                auth_realms={
                    "mine": AuthRealmConfig(
                        id="mine",
                        token="external-secret",
                        principal="ben",
                        device_key_auth=True,
                    )
                },
                profiles=(profile,),
                active_profile="powerhouse",
                internal_token="internal-secret",
                rp_id="example.ts.net",
                expected_origin="https://terminal.example.ts.net",
            )
            proxy = ProxyServer(
                config,
                static_root=Path("static"),
                node_modules_root=Path("node_modules"),
                render_icon=lambda _label, _size: b"",
                passkeys=passkeys,
            )
            client = RelayConnection()
            relay = asyncio.create_task(proxy._relay_profile(client, profile, "ben", ""))
            await client.incoming.put(
                json.dumps({"type": "rotate-token", "cascadePasskeys": True})
            )
            await asyncio.wait_for(received_rotation.wait(), timeout=2)
            self.assertEqual(passkeys.credentials, [])
            self.assertIn(("revoke-all", "mine", "ben"), passkeys.calls)
            relay.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await relay

    def test_trusted_identity_requires_same_origin_and_non_funnel_request(self):
        profile = ProfileConfig(
            id="powerhouse",
            label="Powerhouse",
            auth_realm="mine",
            backend="ws://127.0.0.1:8090",
        )
        config = ProxyConfig(
            path=self.test_root / "config.json",
            host="127.0.0.1",
            port=8085,
            state_dir=self.test_root / "identity-state",
            label="ps",
            auth_realms={
                "mine": AuthRealmConfig(id="mine", trust_identity=True, principal="ben")
            },
            profiles=(profile,),
            active_profile="powerhouse",
            internal_token="internal-secret",
        )
        proxy = ProxyServer(
            config,
            static_root=self.static_root,
            node_modules_root=self.node_modules_root,
            render_icon=lambda _label, _size: b"",
        )
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
        self.assertEqual(proxy._identity_principal(connection, profile), "ben@example.com")
        connection.request.headers["Origin"] = "https://attacker.example"
        self.assertIsNone(proxy._identity_principal(connection, profile))
        connection.request.headers["Origin"] = "https://terminal.example.ts.net"
        connection.request.headers["Tailscale-Funnel-Request"] = "1"
        self.assertIsNone(proxy._identity_principal(connection, profile))

    async def test_profile_relay_internal_headers_and_down_stub(self):
        backend_requests = []
        backend_messages = []

        async def backend_handler(connection):
            backend_requests.append(connection.request)
            await connection.send(
                json.dumps(
                    {
                        "type": "ready",
                        "session": "shell",
                        "openTabs": ["shell"],
                        "multiTenant": False,
                    }
                )
            )
            async for message in connection:
                if not isinstance(message, str):
                    continue
                payload = json.loads(message)
                backend_messages.append(payload)
                if payload.get("type") == "request-sessions":
                    await connection.send(
                        json.dumps(
                            {
                                "type": "sessions",
                                "sessions": [{"name": "shell", "label": "shell"}],
                                "activeSession": "shell",
                            }
                        )
                    )

        async with serve(backend_handler, "127.0.0.1", 0) as backend_server:
            backend_port = backend_server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as root:
                config = ProxyConfig(
                    path=Path(root) / "config.json",
                    host="127.0.0.1",
                    port=0,
                    state_dir=Path(root) / "state",
                    label="ps",
                    auth_realms={
                        "mine": AuthRealmConfig(
                            id="mine",
                            token="external-secret",
                            principal="ben",
                        )
                    },
                    profiles=(
                        ProfileConfig(
                            id="powerhouse",
                            label="Powerhouse",
                            auth_realm="mine",
                            backend=f"ws://127.0.0.1:{backend_port}",
                            internal_token="powerhouse-internal-secret",
                        ),
                        ProfileConfig(
                            id="behuman",
                            label="Behuman",
                            auth_realm="mine",
                            backend=None,
                            status="down",
                            status_message="Behuman is not running yet",
                        ),
                    ),
                    active_profile="powerhouse",
                    internal_token="internal-secret",
                )
                proxy = ProxyServer(
                    config,
                    static_root=Path(root),
                    node_modules_root=Path(root),
                    render_icon=lambda _label, _size: b"",
                )
                proxy.save_settings(
                    {
                        "uiScale": 0.9,
                        "authenticationByRealm": {
                            "mine": {"mode": "idle", "idleMinutes": 0},
                        },
                    }
                )
                self.assertEqual(
                    proxy.load_settings(),
                    {
                        "uiScale": 0.9,
                        "authenticationByRealm": {
                            "mine": {"mode": "idle", "idleMinutes": 1},
                        },
                    },
                )
                async with serve(
                    proxy.websocket_handler,
                    "127.0.0.1",
                    0,
                    process_request=proxy.process_request,
                ) as proxy_server:
                    proxy_port = proxy_server.sockets[0].getsockname()[1]
                    async with connect(f"ws://127.0.0.1:{proxy_port}/_ws?profile=powerhouse") as client:
                        challenge = await self.receive_json(client)
                        self.assertEqual((challenge["type"], challenge["realm"]), ("auth-challenge", "mine"))
                        await client.send(json.dumps({"type": "auth", "token": "external-secret"}))

                        profiles = await self.receive_json(client)
                        status = await self.receive_json(client)
                        ready = await self.receive_json(client)
                        self.assertEqual(profiles["activeProfile"], "powerhouse")
                        self.assertTrue(status["available"])
                        self.assertEqual(ready["activeProfile"], "powerhouse")
                        self.assertEqual(ready["principal"], "ben")
                        self.assertEqual(ready["openTabs"], ["shell"])
                        self.assertEqual(
                            backend_requests[0].headers[INTERNAL_TOKEN_HEADER],
                            "powerhouse-internal-secret",
                        )
                        self.assertEqual(backend_requests[0].headers[PRINCIPAL_HEADER], "ben")
                        self.assertEqual(backend_requests[0].headers[PROFILE_HEADER], "powerhouse")

                        await client.send(
                            json.dumps(
                                {
                                    "type": "selection-request",
                                    "requestId": "wrong-profile",
                                    "profile": "behuman",
                                }
                            )
                        )
                        rejected = await self.receive_json(client)
                        self.assertEqual(
                            rejected,
                            {
                                "type": "selection-result",
                                "requestId": "wrong-profile",
                                "error": "Terminal changed; select again.",
                            },
                        )
                        await asyncio.sleep(0.02)
                        self.assertNotIn("selection-request", [item.get("type") for item in backend_messages])

                        await client.send(json.dumps({"type": "switch-profile", "profile": "behuman"}))
                        profiles = await self.receive_json(client)
                        status = await self.receive_json(client)
                        ready = await self.receive_json(client)
                        self.assertEqual(profiles["activeProfile"], "behuman")
                        self.assertFalse(status["available"])
                        self.assertIn("not running", status["message"])
                        self.assertFalse(ready["profileAvailable"])

                        await client.send(json.dumps({"type": "switch-profile", "profile": "powerhouse"}))
                        await self.receive_json(client)
                        status = await self.receive_json(client)
                        ready = await self.receive_json(client)
                        self.assertTrue(status["available"])
                        self.assertEqual(ready["activeProfile"], "powerhouse")
                        self.assertGreaterEqual(len(backend_requests), 2)

    async def test_first_run_backend_defaults_remain_unpersisted_until_client_save(self):
        async def backend_handler(connection):
            await connection.send(
                json.dumps(
                    {
                        "type": "settings",
                        "settings": {"uiScale": 0.85},
                        "persisted": False,
                    }
                )
            )
            await connection.send(
                json.dumps(
                    {
                        "type": "ready",
                        "session": "shell",
                        "openTabs": ["shell"],
                        "multiTenant": False,
                    }
                )
            )
            async for _message in connection:
                pass

        async with serve(backend_handler, "127.0.0.1", 0) as backend_server:
            backend_port = backend_server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as root:
                state_dir = Path(root) / "state"
                config = ProxyConfig(
                    path=Path(root) / "config.json",
                    host="127.0.0.1",
                    port=0,
                    state_dir=state_dir,
                    label="ps",
                    auth_realms={
                        "mine": AuthRealmConfig(id="mine", token="external-secret", principal="ben")
                    },
                    profiles=(
                        ProfileConfig(
                            id="powerhouse",
                            label="Powerhouse",
                            auth_realm="mine",
                            backend=f"ws://127.0.0.1:{backend_port}",
                        ),
                    ),
                    active_profile="powerhouse",
                    internal_token="internal-secret",
                )
                proxy = ProxyServer(
                    config,
                    static_root=Path(root),
                    node_modules_root=Path(root),
                    render_icon=lambda _label, _size: b"",
                )
                async with serve(
                    proxy.websocket_handler,
                    "127.0.0.1",
                    0,
                    process_request=proxy.process_request,
                ) as proxy_server:
                    proxy_port = proxy_server.sockets[0].getsockname()[1]
                    async with connect(f"ws://127.0.0.1:{proxy_port}/_ws") as client:
                        await self.receive_json(client)
                        await client.send(json.dumps({"type": "auth", "token": "external-secret"}))
                        await self.receive_json(client)
                        await self.receive_json(client)
                        settings = await self.receive_json(client)
                        ready = await self.receive_json(client)

                        self.assertEqual(settings["type"], "settings")
                        self.assertFalse(settings["persisted"])
                        self.assertEqual(settings["settings"], {"uiScale": 0.85})
                        self.assertEqual(ready["type"], "ready")
                        self.assertFalse((state_dir / "settings.json").exists())

                        saved_settings = {"uiScale": 0.9, "terminalFontSize": 11}
                        await client.send(
                            json.dumps({"type": "save-settings", "settings": saved_settings})
                        )
                        saved = await self.receive_json(client)
                        self.assertTrue(saved["persisted"])
                        self.assertEqual(saved["settings"], saved_settings)
                        self.assertEqual(proxy.load_settings(), saved_settings)

                        await client.send(json.dumps({"type": "request-settings"}))
                        subsequent = await self.receive_json(client)
                        self.assertTrue(subsequent["persisted"])
                        self.assertEqual(subsequent["settings"], saved_settings)

    async def test_distinct_realm_auth_failure_restores_previous_profile_and_retry_succeeds(self):
        async def backend_handler(connection):
            await connection.send(
                json.dumps(
                    {
                        "type": "ready",
                        "session": "shell",
                        "openTabs": ["shell"],
                        "multiTenant": False,
                    }
                )
            )
            async for _message in connection:
                pass

        async def receive_relay(client, profile_id):
            profiles = await self.receive_json(client)
            status = await self.receive_json(client)
            ready = await self.receive_json(client)
            self.assertEqual(profiles["activeProfile"], profile_id)
            self.assertEqual(status["profile"], profile_id)
            self.assertEqual(ready["activeProfile"], profile_id)

        async with serve(backend_handler, "127.0.0.1", 0) as alpha_backend:
            async with serve(backend_handler, "127.0.0.1", 0) as beta_backend:
                alpha_port = alpha_backend.sockets[0].getsockname()[1]
                beta_port = beta_backend.sockets[0].getsockname()[1]
                with tempfile.TemporaryDirectory() as root:
                    config = ProxyConfig(
                        path=Path(root) / "config.json",
                        host="127.0.0.1",
                        port=0,
                        state_dir=Path(root) / "state",
                        label="ps",
                        auth_realms={
                            "alpha-realm": AuthRealmConfig(
                                id="alpha-realm", token="alpha-secret", principal="alpha-user"
                            ),
                            "beta-realm": AuthRealmConfig(
                                id="beta-realm", token="beta-secret", principal="beta-user"
                            ),
                        },
                        profiles=(
                            ProfileConfig(
                                id="alpha-other",
                                label="Alpha Other",
                                auth_realm="alpha-realm",
                                backend=f"ws://127.0.0.1:{alpha_port}",
                            ),
                            ProfileConfig(
                                id="alpha",
                                label="Alpha",
                                auth_realm="alpha-realm",
                                backend=f"ws://127.0.0.1:{alpha_port}",
                            ),
                            ProfileConfig(
                                id="beta",
                                label="Beta",
                                auth_realm="beta-realm",
                                backend=f"ws://127.0.0.1:{beta_port}",
                            ),
                        ),
                        active_profile="alpha",
                        internal_token="internal-secret",
                    )
                    proxy = ProxyServer(
                        config,
                        static_root=Path(root),
                        node_modules_root=Path(root),
                        render_icon=lambda _label, _size: b"",
                    )
                    async with serve(
                        proxy.websocket_handler,
                        "127.0.0.1",
                        0,
                        process_request=proxy.process_request,
                    ) as proxy_server:
                        proxy_port = proxy_server.sockets[0].getsockname()[1]
                        async with connect(
                            f"ws://127.0.0.1:{proxy_port}/_ws?profile=alpha"
                        ) as client:
                            challenge = await self.receive_json(client)
                            self.assertEqual(challenge["realm"], "alpha-realm")
                            await client.send(json.dumps({"type": "auth", "token": "alpha-secret"}))
                            await receive_relay(client, "alpha")

                            await client.send(json.dumps({"type": "switch-profile", "profile": "beta"}))
                            challenge = await self.receive_json(client)
                            self.assertEqual(challenge["realm"], "beta-realm")
                            await client.send(json.dumps({"type": "auth", "token": "wrong"}))
                            auth_error = await self.receive_json(client)
                            self.assertEqual(auth_error["type"], "auth-error")
                            self.assertEqual(auth_error["profile"], "beta")
                            await receive_relay(client, "alpha")

                            await client.send(json.dumps({"type": "switch-profile", "profile": "beta"}))
                            challenge = await self.receive_json(client)
                            self.assertEqual(challenge["realm"], "beta-realm")
                            await client.send(json.dumps({"type": "auth", "token": "beta-secret"}))
                            await receive_relay(client, "beta")

                            await client.send(json.dumps({"type": "switch-profile", "profile": "alpha"}))
                            await receive_relay(client, "alpha")
                            await client.send(json.dumps({"type": "switch-profile", "profile": "beta"}))
                            await receive_relay(client, "beta")


if __name__ == "__main__":
    unittest.main()
