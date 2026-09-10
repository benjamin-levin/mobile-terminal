import os
from pathlib import Path
import subprocess
import sys
import unittest

import server


DEFAULT_HOST = "100.70.108.32"
DEFAULT_PORT = 18443
DEFAULT_SOCKET = "/home/powerhouse/voice/run/voice.sock"


class TtsEndpointConfigurationTest(unittest.TestCase):
    """Speech defaults to authenticated tailnet TCP, with an optional local socket."""

    def test_defaults(self):
        self.assertEqual(server.TTS_MODE, "tcp")
        self.assertEqual(server.TTS_SOCKET, DEFAULT_SOCKET)
        self.assertEqual(server.TTS_HOST, DEFAULT_HOST)
        self.assertEqual(server.TTS_PORT, DEFAULT_PORT)
        self.assertEqual(
            server.TTS_TOKEN_PATH, Path.home() / ".config/voice/auth-token"
        )

    def test_environment_override(self):
        env = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "MOBILE_TERMINAL_TTS_MODE": " SoCkEt ",
            "MOBILE_TERMINAL_TTS_SOCKET": "/tmp/mobile-terminal-test-tts.sock",
            "MOBILE_TERMINAL_TTS_HOST": "127.0.0.1",
            "MOBILE_TERMINAL_TTS_PORT": "19000",
            "MOBILE_TERMINAL_TTS_TOKEN_FILE": "/tmp/mt-test-token",
        }
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import server; print(server.TTS_MODE, server.TTS_SOCKET, "
                "server.TTS_HOST, server.TTS_PORT, server.TTS_TOKEN_PATH)",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(
            result.stdout.strip(),
            "socket /tmp/mobile-terminal-test-tts.sock 127.0.0.1 19000 /tmp/mt-test-token",
        )


if __name__ == "__main__":
    unittest.main()
