import asyncio
import io
import json
import logging
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest import mock
import wave

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

import server
from tests.test_server_passkey_integration import FakeBridge, StubConnection, StubPasskeys


class TerminalSpeechTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token_path = Path(self.temp.name) / "auth-token"
        self.token_path.write_text("test-voice-token", encoding="utf-8")
        self.patches = [
            mock.patch("server.TTS_MODE", "tcp"),
            mock.patch("server.TTS_HOST", "127.0.0.1"),
            mock.patch("server.TTS_TOKEN_PATH", self.token_path),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.app = server.AppServer(
            host="127.0.0.1", port=0, session_name="test", shell="/bin/sh",
            cwd=self.temp.name, token="test-bootstrap", require_token=True,
            allowed_clients=[], tailscale_mode=False, passkeys=StubPasskeys(),
        )
        self.owner = StubConnection()
        self.requests = []
        self.voice_status = 200
        self.voice_type = "audio/wav"
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b"\0\0" * 24)
        self.audio = output.getvalue()
        self.voice_body = self.audio
        self.voice_delay = 0
        self.voice_response = None
        self.voice_server = await asyncio.start_server(self.voice, "127.0.0.1", 0)
        # Patch the port only once the ephemeral port is known.
        port_patch = mock.patch(
            "server.TTS_PORT", self.voice_server.sockets[0].getsockname()[1]
        )
        port_patch.start()
        self.addCleanup(port_patch.stop)
        self.http_server = await serve(
            self.app.websocket_handler, "127.0.0.1", 0,
            process_request=self.app.process_request,
            create_connection=server.TerminalHTTPConnection,
            open_timeout=21,
            logger=logging.getLogger("test.tts.websocket"),
        )
        self.port = self.http_server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.http_server.close()
        await self.http_server.wait_closed()
        self.voice_server.close()
        await self.voice_server.wait_closed()

    async def voice(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("ascii").split("\r\n")
            length = int(next(line.split(":", 1)[1] for line in lines if line.startswith("Content-Length:")))
            body = json.loads(await reader.readexactly(length))
            self.requests.append((lines, body))
            if self.voice_delay:
                await asyncio.sleep(self.voice_delay)
            writer.write(self.voice_response if self.voice_response is not None else (
                f"HTTP/1.1 {self.voice_status} Result\r\nContent-Type: {self.voice_type}\r\n"
                f"Content-Length: {len(self.voice_body)}\r\n\r\n".encode("ascii")
                + self.voice_body
            ))
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def capability(self):
        self.owner.sent.clear()
        await self.app.handle_command(
            self.owner, None, {"session": "test"}, {"type": "tts-auth", "requestId": "test-request"},
        )
        return self.owner.sent[-1]["capability"]

    async def raw_http(self, data, *, split=False):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            if split:
                for chunk in (data[:2], data[2:23], data[23:]):
                    writer.write(chunk)
                    await writer.drain()
                    await asyncio.sleep(0)
            else:
                writer.write(data)
                await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=12)
        finally:
            writer.close()
            await writer.wait_closed()
        head, _, body = response.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        return status, head, body

    async def post(self, payload, *, capability=None, raw=None, extra=b"", split=False):
        body = json.dumps(payload).encode() if raw is None else raw
        auth = f"Authorization: Bearer {capability}\r\n".encode() if capability else b""
        return await self.raw_http(
            b"POST /tts HTTP/1.1\r\nHost: terminal.example.ts.net\r\n"
            b"Origin: https://terminal.example.ts.net\r\nContent-Type: application/json\r\n"
            + auth + extra + f"Content-Length: {len(body)}\r\n\r\n".encode() + body,
            split=split,
        )

    async def test_unauthenticated_wrong_expired_and_replayed_capabilities_are_rejected(self):
        for capability in (None, "wrong", "test-bootstrap"):
            response = await self.post({"text": "hello"}, capability=capability)
            self.assertEqual(response[0], 401)
        expired = await self.capability()
        self.app.tts_capabilities[self.owner] = (expired, time.monotonic() - 1)
        self.assertEqual((await self.post({"text": "hello"}, capability=expired))[0], 401)
        self.assertEqual(self.requests, [])
        capability = await self.capability()
        self.assertEqual((await self.post({"text": "hello"}, capability=capability))[0], 200)
        self.assertEqual((await self.post({"text": "hello"}, capability=capability))[0], 401)
        self.assertEqual(len(self.requests), 1)

    async def test_authenticated_wav_and_inclusive_text_boundary(self):
        for text in ("hello\n  world", "x" * 2000, "\U0001f600" * 2000):
            capability = await self.capability()
            status, headers, body = await self.post({"text": text}, capability=capability, split=True)
            self.assertEqual(status, 200)
            self.assertIn(b"Content-Type: audio/wav", headers)
            self.assertIn(b"Cache-Control: no-store", headers)
            self.assertEqual(body, self.audio)
            lines, payload = self.requests[-1]
            self.assertEqual(lines[0], "POST /v1/synthesize HTTP/1.1")
            self.assertIn(f"Host: {server.TTS_HOST}:{server.TTS_PORT}", lines)
            # The shared voice endpoint is authenticated, so the call carries exactly
            # one Authorization header and it is the SERVICE token. The browser
            # client's own capability must never be forwarded upstream.
            authorizations = [line for line in lines if line.startswith("Authorization:")]
            self.assertEqual(authorizations, ["Authorization: Bearer test-voice-token"])
            self.assertNotIn(capability, "\r\n".join(lines))
            self.assertEqual(payload, {"text": text})

    async def test_socket_mode_authenticated_wav_request(self):
        socket_path = str(Path(self.temp.name) / "voice.sock")
        async with await asyncio.start_unix_server(self.voice, path=socket_path):
            with (
                mock.patch("server.TTS_MODE", "socket"),
                mock.patch("server.TTS_SOCKET", socket_path),
            ):
                self.assertEqual(
                    await server.synthesize_terminal_speech("hello\n  world"), self.audio,
                )
                capability = await self.capability()
                status, headers, body = await self.post(
                    {"text": "hello\n  world"}, capability=capability,
                )
        self.assertEqual(status, 200)
        self.assertIn(b"Content-Type: audio/wav", headers)
        self.assertEqual(body, self.audio)
        lines, payload = self.requests[-1]
        self.assertEqual(lines[0], "POST /v1/synthesize HTTP/1.1")
        self.assertIn("Host: localhost", lines)
        self.assertIn("Content-Type: application/json", lines)
        self.assertIn("Connection: close", lines)
        self.assertEqual(
            [line for line in lines if line.startswith("Authorization:")],
            ["Authorization: Bearer test-voice-token"],
        )
        self.assertNotIn(capability, "\r\n".join(lines))
        self.assertEqual(payload, {"text": "hello\n  world"})

    async def test_both_transports_reject_invalid_upstream_responses(self):
        socket_path = str(Path(self.temp.name) / "voice.sock")
        length = f"Content-Length: {len(self.audio)}\r\n".encode("ascii")
        cases = (
            (b"HTTP/1.1 503 Error", length, self.audio),
            (b"HTTP/1.0 200 OK", length, self.audio),
            (b"HTTP/1.1 200 OK", b"", self.audio),
            (b"HTTP/1.1 200 OK", length * 2, self.audio),
            (b"HTTP/1.1 200 OK", b"Content-Length: wat\r\n", self.audio),
            (b"HTTP/1.1 200 OK", b"Content-Length: 43\r\n", self.audio),
            (b"HTTP/1.1 200 OK", b"Content-Length: 33554433\r\n", self.audio),
            (b"HTTP/1.1 200 OK", length + b"Transfer-Encoding: chunked\r\n", self.audio),
            (b"HTTP/1.1 200 OK", length, b"not a WAV" * 16),
            (b"HTTP/1.1 200 OK", length, self.audio[:44]),
        )
        async with await asyncio.start_unix_server(self.voice, path=socket_path):
            for mode in ("tcp", "socket"):
                with (
                    mock.patch("server.TTS_MODE", mode),
                    mock.patch("server.TTS_SOCKET", socket_path),
                ):
                    for index, (status_line, framing, audio) in enumerate(cases):
                        with self.subTest(mode=mode, case=index):
                            self.voice_response = (
                                status_line + b"\r\nContent-Type: audio/wav\r\n"
                                + framing + b"\r\n" + audio
                            )
                            status, _, body = await self.post(
                                {"text": "hello"}, capability=await self.capability(),
                            )
                            self.assertEqual(status, 503)
                            self.assertEqual(json.loads(body), {"error": "Speech service unavailable."})
                    self.voice_response = None
                    self.voice_type = "text/plain"
                    status, _, body = await self.post(
                        {"text": "hello"}, capability=await self.capability(),
                    )
                    self.assertEqual(status, 503)
                    self.assertEqual(json.loads(body), {"error": "Speech service unavailable."})

    async def test_socket_mode_deadline_and_missing_socket_are_generic_503(self):
        socket_path = str(Path(self.temp.name) / "voice.sock")
        with (
            mock.patch("server.TTS_MODE", "socket"),
            mock.patch("server.TTS_SOCKET", socket_path),
        ):
            status, _, body = await self.post({"text": "hello"}, capability=await self.capability())
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body), {"error": "Speech service unavailable."})
            self.assertNotIn(socket_path.encode(), body)
            async with await asyncio.start_unix_server(self.voice, path=socket_path):
                self.voice_delay = 0.1
                with mock.patch("server.TTS_TIMEOUT_SECONDS", 0.02):
                    status, _, body = await self.post({"text": "hello"}, capability=await self.capability())
                self.assertEqual(status, 503)
                self.assertEqual(json.loads(body), {"error": "Speech service unavailable."})
                await asyncio.sleep(0.11)

    async def test_empty_type_malformed_and_oversized_text_rejected_before_proxy(self):
        for payload in ({}, {"text": ""}, {"text": " \n\t"}, {"text": None},
                        {"text": True}, {"text": 1}, {"text": []}, {"text": {}},
                        {"text": "x" * 2001}, [], None, "hello"):
            with self.subTest(payload_type=type(payload).__name__):
                status, _, body = await self.post(payload, capability=await self.capability())
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body), {"error": "Invalid text."})
        for raw in (b"{", b"", b"\xff"):
            self.assertEqual((await self.post(None, raw=raw, capability=await self.capability()))[0], 400)
        self.assertEqual(self.requests, [])

    async def test_missing_refused_unhealthy_and_non_wav_are_generic_503(self):
        for status, kind, body in ((503, "application/json", b'{"detail":"private upstream error"}'),
                                   (500, "text/plain", b"private upstream error"),
                                   (200, "text/plain", self.audio),
                                   (200, "audio/wav", b"not a WAV" * 8)):
            self.voice_status, self.voice_type, self.voice_body = status, kind, body
            response = await self.post({"text": "hello"}, capability=await self.capability())
            self.assertEqual(response[0], 503)
            self.assertEqual(json.loads(response[2]), {"error": "Speech service unavailable."})
        # An unreachable endpoint must also be a generic 503 that never names the host
        # or port. Binding then closing yields a port nothing listens on.
        with socket.socket(socket.AF_INET) as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]
        with mock.patch("server.TTS_PORT", closed_port):
            status, _, body = await self.post({"text": "hello"}, capability=await self.capability())
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "Speech service unavailable."})
        self.assertNotIn(str(closed_port).encode(), body)
        self.assertNotIn(b"127.0.0.1", body)

    async def test_upstream_deadline_returns_json_503(self):
        self.assertEqual(server.TTS_TIMEOUT_SECONDS, 10)
        self.voice_delay = 0.1
        with mock.patch("server.TTS_TIMEOUT_SECONDS", 0.02):
            status, _, body = await self.post({"text": "hello"}, capability=await self.capability())
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "Speech service unavailable."})
        await asyncio.sleep(0.11)

    async def test_full_unix_timeout_outlives_original_websocket_handshake_budget(self):
        self.voice_delay = 10.1
        status, _, body = await self.post({"text": "hello"}, capability=await self.capability())
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "Speech service unavailable."})
        await asyncio.sleep(0.15)

    async def test_websocket_cannot_issue_tts_capability_before_authentication(self):
        async with connect(f"ws://127.0.0.1:{self.port}/_ws") as ws:
            self.assertEqual(json.loads(await ws.recv())["type"], "auth-challenge")
            await ws.send(json.dumps({"type": "tts-auth", "requestId": "unauthenticated"}))
            self.assertEqual(json.loads(await ws.recv())["type"], "auth-error")
        self.assertEqual(self.app.tts_capabilities, {})
        self.assertEqual(self.requests, [])

    async def test_adapter_rejects_ambiguous_framing_and_preserves_get(self):
        for extra in (b"Transfer-Encoding: chunked\r\n", b"Content-Length: 0\r\n",
                      b"Upgrade: websocket\r\n", b"Expect: 100-continue\r\n"):
            self.assertEqual((await self.post({"text": "hello"}, extra=extra))[0], 400)
        for length, status in ((b"-1", 400), (b"wat", 400), (b"32769", 413)):
            response = await self.raw_http(b"POST /tts HTTP/1.1\r\nHost: test\r\nContent-Length: " + length + b"\r\n\r\n")
            self.assertEqual(response[0], status)
        self.assertEqual((await self.raw_http(b"POST /tts HTTP/1.1\r\nContent-Length: 0\r\n\r\nextra"))[0], 400)
        self.assertEqual((await self.raw_http(b"POST /other HTTP/1.1\r\nContent-Length: 0\r\n\r\n"))[0], 405)
        status, _, body = await self.raw_http(b"GET /health HTTP/1.1\r\nHost: test\r\n\r\n", split=True)
        self.assertEqual((status, body), (200, b"ok\n"))
        self.assertEqual((await self.raw_http(b"GET /tts HTTP/1.1\r\nHost: test\r\n\r\n"))[0], 405)
        self.assertEqual(self.requests, [])

    async def test_cross_origin_and_network_gate_reject_before_proxy(self):
        capability = await self.capability()
        response = await self.raw_http(
            b"POST /tts HTTP/1.1\r\nHost: terminal.example.ts.net\r\nOrigin: https://evil.example\r\n"
            + f"Authorization: Bearer {capability}\r\nContent-Length: 16\r\n\r\n".encode()
            + b'{"text":"hello"}',
        )
        self.assertEqual(response[0], 401)
        with mock.patch.object(self.app, "client_is_allowed", return_value=False):
            self.assertEqual((await self.post({"text": "hello"}, capability=await self.capability()))[0], 403)
        self.assertEqual(self.requests, [])

    async def test_real_websocket_auth_issues_capability_and_disconnect_revokes_it(self):
        self.app.resolve_user_session = mock.Mock(return_value=("test", False))
        self.app.open_tabs_for = mock.Mock(return_value=[])
        self.app.tabs_for_user = mock.Mock(return_value=[])
        for name in ("send_tabs", "send_sessions", "send_settings", "send_composer_state", "record_session"):
            setattr(self.app, name, mock.Mock() if name == "record_session" else mock.AsyncMock())
        with (
            mock.patch("server.device_pubkey", return_value="test-enrolled-key"),
            mock.patch("server.verify_device_signature", return_value=True),
            mock.patch("server.TmuxBridge", FakeBridge),
            mock.patch("server.pane_scrolls_locally", return_value=False),
            mock.patch("server.pane_metadata", return_value=("%1",)),
        ):
            async with connect(f"ws://127.0.0.1:{self.port}/_ws") as ws:
                challenge = json.loads(await ws.recv())
                self.assertEqual(challenge["type"], "auth-challenge")
                self.assertEqual(self.app.tts_capabilities, {})
                await ws.send(json.dumps({
                    "type": "auth", "realm": "standalone", "profile": "", "deviceId": "test-device",
                    "requirePasskey": False, "signature": "test-signature",
                }))
                while json.loads(await ws.recv())["type"] != "ready":
                    pass
                await ws.send(json.dumps({"type": "tts-auth", "requestId": "live-request"}))
                while True:
                    payload = json.loads(await ws.recv())
                    if payload["type"] == "tts-auth":
                        break
                capability = payload["capability"]
                self.assertEqual((await self.post({"text": "hello"}, capability=capability))[0], 200)
                await ws.send(json.dumps({"type": "tts-auth", "requestId": "unused"}))
                while True:
                    payload = json.loads(await ws.recv())
                    if payload["type"] == "tts-auth":
                        break
                capability = payload["capability"]
            await asyncio.sleep(0.02)
        self.assertEqual((await self.post({"text": "hello"}, capability=capability))[0], 401)
        self.assertEqual(self.app.tts_capabilities, {})


if __name__ == "__main__":
    unittest.main()
