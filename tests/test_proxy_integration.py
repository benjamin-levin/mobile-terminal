import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from mobile_terminal_config import AuthRealmConfig, ProfileConfig, ProxyConfig
from proxy import INTERNAL_TOKEN_HEADER, PRINCIPAL_HEADER, ProxyServer
from webauthn_auth import PasskeyAuth, PasskeyVerificationError


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

    def revoke_credential(self, realm, credential_id):
        self.calls.append(("revoke", realm, credential_id))
        self.credentials = [
            credential for credential in self.credentials
            if credential.get("credentialId") != credential_id
        ]
        return True

    def revoke_all_credentials(self, realm):
        self.calls.append(("revoke-all", realm))
        count = len(self.credentials)
        self.credentials = []
        return count


class ProxyRelayTest(unittest.IsolatedAsyncioTestCase):
    async def receive_json(self, connection):
        message = await connection.recv()
        self.assertIsInstance(message, str)
        return json.loads(message)

    def passkey_proxy(self, passkeys, authenticate=None):
        profile = ProfileConfig(
            id="powerhouse",
            label="Powerhouse",
            auth_realm="mine",
            backend="ws://127.0.0.1:8090",
        )
        config = ProxyConfig(
            path=Path("config.json"),
            host="127.0.0.1",
            port=8085,
            state_dir=Path("state"),
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
        self.assertEqual(proxy.passkeys.state_dir, state_dir)
        self.assertEqual(proxy.passkeys.rp_id, "example.ts.net")
        self.assertEqual(proxy.passkeys.expected_origin, "https://terminal.example.ts.net")

    async def test_passkey_authentication_precedes_token_bootstrap(self):
        passkeys = StubPasskeys([{"credentialId": "credential", "principal": "ben"}])

        def reject_token(_request):
            self.fail("token authenticator must not run after a passkey assertion")

        proxy, profile = self.passkey_proxy(passkeys, reject_token)
        connection = StubConnection(
            [{"type": "webauthn-auth", "challengeId": "auth-id", "assertion": {}}]
        )

        principal = await proxy.authenticate_realm(connection, profile, binding="connection-id")

        self.assertEqual(principal, "ben")
        self.assertEqual(connection.sent[0]["type"], "webauthn-auth-options")
        self.assertEqual(connection.sent[0]["realm"], "mine")
        self.assertIn(("finish-auth", "mine", "auth-id", "connection-id"), passkeys.calls)

    async def test_token_is_bootstrap_only_until_passkey_registration_finishes(self):
        passkeys = StubPasskeys()
        proxy, profile = self.passkey_proxy(
            passkeys,
            lambda request: "ben" if request.credentials.get("token") == "external-secret" else None,
        )
        connection = StubConnection(
            [
                {"type": "auth", "token": "external-secret"},
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

    async def test_passkey_failures_are_generic_and_do_not_expose_verification_details(self):
        passkeys = StubPasskeys([{"credentialId": "credential", "principal": "ben"}])
        passkeys.authentication_error = PasskeyVerificationError("sensitive credential detail")
        proxy, _profile = self.passkey_proxy(passkeys)
        connection = StubConnection(
            [{"type": "webauthn-auth", "challengeId": "auth-id", "assertion": {}}]
        )

        await proxy.websocket_handler(connection)

        self.assertEqual(connection.sent[-1]["type"], "auth-error")
        self.assertEqual(connection.sent[-1]["message"], "Passkey verification failed.")
        self.assertNotIn("sensitive", json.dumps(connection.sent))
        self.assertEqual(connection.closed, (4001, "auth failed"))

    async def test_passkey_credentials_can_be_listed_and_revoked_per_realm(self):
        passkeys = StubPasskeys([{"credentialId": "credential", "principal": "ben"}])
        proxy, profile = self.passkey_proxy(passkeys)
        connection = StubConnection()

        self.assertTrue(
            await proxy._handle_credential_message(
                connection,
                profile,
                {"type": "request-devices"},
            )
        )
        self.assertEqual(connection.sent[-1]["devices"][0]["credentialId"], "credential")
        self.assertTrue(
            await proxy._handle_credential_message(
                connection,
                profile,
                {"type": "revoke-credential", "credentialId": "credential"},
            )
        )
        self.assertEqual(connection.sent[-1], {"type": "devices", "devices": []})
        passkeys.credentials = [{"credentialId": "other", "principal": "ben"}]
        self.assertTrue(
            await proxy._handle_credential_message(
                connection,
                profile,
                {"type": "revoke-all-credentials"},
            )
        )
        self.assertIn(("revoke-all", "mine"), passkeys.calls)
        self.assertEqual(connection.sent[-1], {"type": "devices", "devices": []})

        passkeys.credentials = [{"credentialId": "rotated", "principal": "ben"}]
        self.assertTrue(
            await proxy._cascade_passkeys_for_rotation(
                connection,
                profile,
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
                path=Path("config.json"),
                host="127.0.0.1",
                port=8085,
                state_dir=Path("state"),
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
            self.assertIn(("revoke-all", "mine"), passkeys.calls)
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
            path=Path("config.json"),
            host="127.0.0.1",
            port=8085,
            state_dir=Path("state"),
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
            static_root=Path("static"),
            node_modules_root=Path("node_modules"),
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
                proxy.save_settings({"uiScale": 0.9})
                self.assertEqual(proxy.load_settings(), {"uiScale": 0.9})
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
