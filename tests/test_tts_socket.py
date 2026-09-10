import os
from pathlib import Path
import subprocess
import sys
import unittest

import server


DEFAULT_SOCKET = "/home/powerhouse/tts-server/run/tts.sock"


class TtsSocketConfigurationTest(unittest.TestCase):
    def test_default_socket(self):
        self.assertEqual(server.TTS_SOCKET, DEFAULT_SOCKET)

    def test_environment_override(self):
        override = "/tmp/mobile-terminal-test-tts.sock"
        env = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "MOBILE_TERMINAL_TTS_SOCKET": override,
        }
        result = subprocess.run(
            [sys.executable, "-c", "import server; print(server.TTS_SOCKET)"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.stdout.strip(), override)


if __name__ == "__main__":
    unittest.main()
