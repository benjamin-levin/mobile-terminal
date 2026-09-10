import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

from server import AppServer


class AuthenticationOriginRegressionTest(unittest.TestCase):
    def test_localhost_http_origin_is_accepted_only_for_same_host_loopback(self):
        app = AppServer.__new__(AppServer)
        app.host = "127.0.0.1"
        cases = (
            ("localhost:8085", "http://localhost:8085", "127.0.0.1", "http://localhost:8085"),
            ("localhost:8085", "http://localhost:8085", "::1", "http://localhost:8085"),
            ("localhost:8085", "https://localhost:8085", "127.0.0.1", "https://localhost:8085"),
            ("localhost:8085", "http://localhost:8086", "127.0.0.1", "https://localhost:8085"),
            ("localhost:8085", "http://localhost:8085", "192.0.2.1", "https://localhost:8085"),
            ("terminal.example", "http://terminal.example", "127.0.0.1", "https://terminal.example"),
            ("localhost.evil.example", "http://localhost.evil.example", "127.0.0.1", "https://localhost.evil.example"),
        )
        with mock.patch.dict("os.environ", {}, clear=True):
            for host, origin, address, expected in cases:
                with self.subTest(host=host, origin=origin, address=address):
                    connection = SimpleNamespace(
                        remote_address=(address, 12345),
                        request=SimpleNamespace(headers={"Host": host, "Origin": origin}),
                    )
                    self.assertEqual(app.expected_origin(connection), expected)

    def test_explicit_origin_configuration_takes_precedence(self):
        app = AppServer.__new__(AppServer)
        with mock.patch.dict("os.environ", {"MOBILE_TERMINAL_ORIGIN": "https://configured.example"}):
            self.assertEqual(app.expected_origin(None), "https://configured.example")

    def test_client_identifies_insecure_and_ip_origins_before_registration(self):
        source = (Path(__file__).parents[1] / "static/app.js").read_text()
        function = re.search(
            r"^  function authenticationOriginError\(.*?^  \}$",
            source,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(function)
        script = function.group(0) + "\n" + """
const cases = [
  [{hostname: 'terminal.example'}, false],
  [{hostname: '127.0.0.1'}, true],
  [{hostname: '[::1]'}, true],
  [{hostname: 'localhost'}, true],
  [{hostname: 'terminal.example'}, true],
];
process.stdout.write(JSON.stringify(cases.map(([location, secure]) => authenticationOriginError(location, secure))));
"""
        result = subprocess.run(["node", "-e", script], text=True, capture_output=True, check=True)
        messages = json.loads(result.stdout)
        self.assertIn("HTTPS", messages[0])
        self.assertIn("IP address", messages[1])
        self.assertIn("IP address", messages[2])
        self.assertEqual(messages[3:], ["", ""])
