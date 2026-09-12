import asyncio
import base64
from contextlib import asynccontextmanager
import io
import json
import logging
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock
import wave

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

import server
from tests.test_server_passkey_integration import FakeBridge, StubPasskeys


class SpeechBridge(FakeBridge):
    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connection = args[0]
        self.send_lock = asyncio.Lock()
        self.resize = mock.AsyncMock()
        self.instances.append(self)


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
            mock.patch("server.device_pubkey", return_value="test-enrolled-key"),
            mock.patch("server.verify_device_signature", return_value=True),
            mock.patch("server.TmuxBridge", SpeechBridge),
            mock.patch("server.pane_scrolls_locally", return_value=False),
            mock.patch("server.pane_metadata", return_value=("%1",)),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        SpeechBridge.instances = []
        self.app = server.AppServer(
            host="127.0.0.1", port=0, session_name="test", shell="/bin/sh",
            cwd=self.temp.name, token="test-bootstrap", require_token=True,
            allowed_clients=[], tailscale_mode=False, passkeys=StubPasskeys(),
        )
        self.app.resolve_user_session = mock.Mock(side_effect=lambda user, name: (name or "test", False))
        self.app.open_tabs_for = mock.Mock(return_value=[])
        self.app.tabs_for_user = mock.Mock(return_value=[])
        for name in ("send_tabs", "send_sessions", "send_settings", "send_composer_state", "record_session"):
            setattr(self.app, name, mock.Mock() if name == "record_session" else mock.AsyncMock())
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
        port_patch = mock.patch("server.TTS_PORT", self.voice_server.sockets[0].getsockname()[1])
        port_patch.start()
        self.addCleanup(port_patch.stop)
        self.http_server = await serve(
            self.app.websocket_handler, "127.0.0.1", 0,
            process_request=self.app.process_request,
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

    async def receive(self, ws, kind):
        async with asyncio.timeout(2):
            while True:
                raw = await ws.recv()
                self.assertIsInstance(raw, str, "speech must never use terminal binary frames")
                payload = json.loads(raw)
                if payload["type"] == kind:
                    return payload

    @asynccontextmanager
    async def authenticated(self):
        async with connect(f"ws://127.0.0.1:{self.port}/_ws") as ws:
            await self.receive(ws, "auth-challenge")
            await ws.send(json.dumps({
                "type": "auth", "realm": "standalone", "profile": "", "deviceId": "test-device",
                "requirePasskey": False, "signature": "test-signature",
            }))
            await self.receive(ws, "ready")
            yield ws

    async def speech(self, payload):
        async with self.authenticated() as ws:
            await ws.send(json.dumps({**payload, "type": "tts-request", "requestId": "test-request"}))
            result = await self.receive(ws, "tts-result")
            self.assertEqual(result["requestId"], "test-request")
            return result

    def assert_unavailable(self, result):
        self.assertEqual(result, {
            "type": "tts-result", "requestId": "test-request", "error": "Speech service unavailable.",
        })

    async def test_authenticated_wav_and_inclusive_text_boundary(self):
        for text in ("hello\n  world", "x" * 2000, "\U0001f600" * 2000):
            result = await self.speech({"text": text})
            self.assertEqual(result["contentType"], "audio/wav")
            self.assertEqual(base64.b64decode(result["audio"], validate=True), self.audio)
            lines, payload = self.requests[-1]
            self.assertEqual(lines[0], "POST /v1/synthesize HTTP/1.1")
            self.assertIn(f"Host: {server.TTS_HOST}:{server.TTS_PORT}", lines)
            # Only the service token goes upstream, never the browser's device auth.
            self.assertEqual([line for line in lines if line.startswith("Authorization:")],
                             ["Authorization: Bearer test-voice-token"])
            self.assertNotIn("test-signature", "\r\n".join(lines))
            self.assertEqual(payload, {"text": text})

    async def test_socket_mode_authenticated_wav_request(self):
        socket_path = str(Path(self.temp.name) / "voice.sock")
        async with await asyncio.start_unix_server(self.voice, path=socket_path):
            with mock.patch("server.TTS_MODE", "socket"), mock.patch("server.TTS_SOCKET", socket_path):
                result = await self.speech({"text": "hello\n  world"})
        self.assertEqual(base64.b64decode(result["audio"]), self.audio)
        lines, payload = self.requests[-1]
        self.assertEqual(lines[0], "POST /v1/synthesize HTTP/1.1")
        self.assertIn("Host: localhost", lines)
        self.assertIn("Content-Type: application/json", lines)
        self.assertIn("Connection: close", lines)
        self.assertEqual([line for line in lines if line.startswith("Authorization:")],
                         ["Authorization: Bearer test-voice-token"])
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
                with mock.patch("server.TTS_MODE", mode), mock.patch("server.TTS_SOCKET", socket_path):
                    for index, (status_line, framing, audio) in enumerate(cases):
                        with self.subTest(mode=mode, case=index):
                            self.voice_response = status_line + b"\r\nContent-Type: audio/wav\r\n" + framing + b"\r\n" + audio
                            self.assert_unavailable(await self.speech({"text": "hello"}))
                    self.voice_response = None
                    self.voice_type = "text/plain"
                    self.assert_unavailable(await self.speech({"text": "hello"}))
                    self.voice_type = "audio/wav"

    async def test_socket_mode_deadline_and_missing_socket_are_generic_errors(self):
        socket_path = str(Path(self.temp.name) / "voice.sock")
        with mock.patch("server.TTS_MODE", "socket"), mock.patch("server.TTS_SOCKET", socket_path):
            self.assert_unavailable(await self.speech({"text": "hello"}))
            async with await asyncio.start_unix_server(self.voice, path=socket_path):
                self.voice_delay = 0.1
                with mock.patch("server.TTS_TIMEOUT_SECONDS", 0.02):
                    self.assert_unavailable(await self.speech({"text": "hello"}))
                await asyncio.sleep(0.11)

    async def test_empty_wrong_type_and_oversized_text_rejected_before_proxy(self):
        for payload in ({}, {"text": ""}, {"text": " \n\t"}, {"text": None}, {"text": True},
                        {"text": 1}, {"text": []}, {"text": {}}, {"text": "x" * 2001}):
            self.assertEqual(await self.speech(payload), {
                "type": "tts-result", "requestId": "test-request", "error": "Invalid text.",
            })
        self.assertEqual(self.requests, [])

    async def test_invalid_ids_and_malformed_commands_do_not_synthesize(self):
        async with self.authenticated() as ws:
            for request_id in (None, "", 1, [], {}, "x" * 81):
                await ws.send(json.dumps({"type": "tts-request", "requestId": request_id, "text": "hello"}))
            for raw in ("{", "[]", "null", '"hello"', b"binary"):
                await ws.send(raw)
            await ws.send(json.dumps({"type": "request-stats"}))
            await self.receive(ws, "stats")
        self.assertEqual(self.requests, [])

    async def test_missing_refused_unhealthy_and_non_wav_are_generic_errors(self):
        for status, kind, body in ((503, "application/json", b'{"detail":"private upstream error"}'),
                                   (500, "text/plain", b"private upstream error"),
                                   (200, "text/plain", self.audio),
                                   (200, "audio/wav", b"not a WAV" * 8)):
            self.voice_status, self.voice_type, self.voice_body = status, kind, body
            self.assert_unavailable(await self.speech({"text": "hello"}))
        with socket.socket(socket.AF_INET) as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]
        with mock.patch("server.TTS_PORT", closed_port):
            self.assert_unavailable(await self.speech({"text": "hello"}))
        with mock.patch("server.TTS_TOKEN_PATH", Path(self.temp.name) / "missing-token"):
            self.assert_unavailable(await self.speech({"text": "hello"}))

    async def test_upstream_deadline_returns_correlated_error(self):
        self.assertEqual(server.TTS_TIMEOUT_SECONDS, 10)
        self.voice_delay = 0.1
        with mock.patch("server.TTS_TIMEOUT_SECONDS", 0.02):
            self.assert_unavailable(await self.speech({"text": "hello"}))
        await asyncio.sleep(0.11)

    async def test_websocket_cannot_synthesize_before_authentication(self):
        async with connect(f"ws://127.0.0.1:{self.port}/_ws") as ws:
            await self.receive(ws, "auth-challenge")
            await ws.send(json.dumps({"type": "tts-request", "requestId": "unauthenticated", "text": "hello"}))
            await self.receive(ws, "auth-error")
        self.assertEqual(self.requests, [])

    async def test_network_gate_rejects_before_synthesis(self):
        with mock.patch.object(self.app, "client_is_allowed", return_value=False):
            with self.assertRaises(InvalidStatus) as error:
                async with connect(f"ws://127.0.0.1:{self.port}/_ws"):
                    self.fail("network gate allowed a connection")
        self.assertEqual(error.exception.response.status_code, 403)
        self.assertEqual(self.requests, [])

    async def test_concurrent_replies_are_correlated_bounded_and_do_not_block_resize(self):
        started = {text: asyncio.Event() for text in ("first", "second")}
        release = {text: asyncio.Event() for text in started}

        async def synthesize(text):
            started[text].set()
            await release[text].wait()
            return self.audio + text.encode()

        with mock.patch("server.synthesize_terminal_speech", side_effect=synthesize) as synthesis:
            async with self.authenticated() as ws:
                for text in started:
                    await ws.send(json.dumps({"type": "tts-request", "requestId": text, "text": text}))
                    await asyncio.wait_for(started[text].wait(), 1)
                await ws.send(json.dumps({"type": "tts-request", "requestId": "first", "text": "duplicate"}))
                await ws.send(json.dumps({"type": "tts-request", "requestId": "third", "text": "overflow"}))
                self.assertEqual(await self.receive(ws, "tts-result"), {
                    "type": "tts-result", "requestId": "third", "error": "Speech service busy. Try again.",
                })
                await ws.send(json.dumps({"type": "resize", "cols": 90, "rows": 30}))
                await ws.send(json.dumps({"type": "request-stats"}))
                await self.receive(ws, "stats")
                SpeechBridge.instances[-1].resize.assert_awaited_once_with(90, 30)
                for text in ("second", "first"):
                    release[text].set()
                    result = await self.receive(ws, "tts-result")
                    self.assertEqual(result["requestId"], text)
                    self.assertEqual(base64.b64decode(result["audio"]), self.audio + text.encode())
            self.assertEqual(synthesis.await_count, 2)

    async def test_speech_reply_cannot_split_terminal_metadata_and_binary(self):
        synthesized = asyncio.Event()

        async def synthesize(text):
            synthesized.set()
            return self.audio

        with mock.patch("server.synthesize_terminal_speech", side_effect=synthesize):
            async with self.authenticated() as ws:
                bridge = SpeechBridge.instances[-1]
                async with bridge.send_lock:
                    await bridge.connection.send(json.dumps({"type": "terminal-output", "start": 0, "end": 5}))
                    await self.receive(ws, "terminal-output")
                    await ws.send(json.dumps({"type": "tts-request", "requestId": "paired", "text": "hello"}))
                    await asyncio.wait_for(synthesized.wait(), 1)
                    await bridge.connection.send(b"shell")
                    self.assertEqual(await ws.recv(), b"shell")
                self.assertEqual((await self.receive(ws, "tts-result"))["requestId"], "paired")

    async def test_tab_switch_uses_new_bridge_lock_and_disconnect_cancels_synthesis(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def synthesize(text):
            started.set()
            try:
                await release.wait()
                return self.audio
            finally:
                cancelled.set()

        with mock.patch("server.synthesize_terminal_speech", side_effect=synthesize):
            async with self.authenticated() as ws:
                await ws.send(json.dumps({"type": "tts-request", "requestId": "switch", "text": "hello"}))
                await asyncio.wait_for(started.wait(), 1)
                await ws.send(json.dumps({"type": "switch-session", "session": "other"}))
                self.assertEqual((await self.receive(ws, "ready"))["session"], "other")
                bridge = SpeechBridge.instances[-1]
                async with bridge.send_lock:
                    await bridge.connection.send(json.dumps({"type": "terminal-output", "start": 0, "end": 5}))
                    await self.receive(ws, "terminal-output")
                    release.set()
                    await asyncio.wait_for(cancelled.wait(), 1)
                    await bridge.connection.send(b"shell")
                    self.assertEqual(await ws.recv(), b"shell")
                self.assertEqual((await self.receive(ws, "tts-result"))["requestId"], "switch")
                started.clear()
                cancelled.clear()
                release.clear()
                await ws.send(json.dumps({"type": "tts-request", "requestId": "disconnect", "text": "hello"}))
                await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(cancelled.wait(), 1)
        self.http_server.close()
        await self.http_server.wait_closed()
        self.assertEqual(self.app.active_sessions, 0)

    async def test_obsolete_http_speech_adapter_is_removed(self):
        self.assertFalse(hasattr(server, "TerminalHTTPConnection"))
        self.assertFalse(hasattr(self.app, "handle_tts"))
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(b"GET /tts HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 1)
        finally:
            writer.close()
            await writer.wait_closed()
        self.assertTrue(response.startswith(b"HTTP/1.1 404"))
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
