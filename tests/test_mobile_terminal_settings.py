import json
import re
import subprocess
import unittest
from pathlib import Path

from mobile_terminal_config import (
    default_authentication_settings,
    normalize_authentication_fields,
    normalize_authentication_realms,
    normalize_authentication_settings,
)
from server import default_settings, normalize_settings


class AuthenticationSettingsTest(unittest.TestCase):
    def test_authentication_defaults_use_every_open_and_fifteen_minutes(self):
        expected = {"mode": "every-open", "idleMinutes": 15}

        self.assertEqual(default_authentication_settings(), expected)
        self.assertEqual(default_settings()["authentication"], expected)

    def test_mode_and_idle_minutes_are_normalized_and_clamped(self):
        self.assertEqual(
            normalize_authentication_settings({"mode": "idle", "idleMinutes": 0}),
            {"mode": "idle", "idleMinutes": 1},
        )
        self.assertEqual(
            normalize_authentication_settings({"mode": "off", "idleMinutes": 9999}),
            {"mode": "off", "idleMinutes": 1440},
        )
        self.assertEqual(
            normalize_authentication_settings({"mode": "invalid", "idleMinutes": "invalid"}),
            {"mode": "every-open", "idleMinutes": 15},
        )

    def test_numeric_normalization_matches_client_for_edge_cases(self):
        cases = (
            ("15", 15),
            (" +0015 ", 15),
            ("15junk", 15),
            (15.5, 15),
            (15.0, 15),
            (True, 15),
            (False, 15),
            (float("inf"), 15),
            (float("nan"), 15),
            ("999999999999999999999999", 15),
            ("-999999999999999999999999", 15),
            (9_007_199_254_740_992, 15),
            (10**20, 15),
            (0, 1),
            (2000, 1440),
        )
        python_values = [
            normalize_authentication_settings({"idleMinutes": value})["idleMinutes"]
            for value, _expected in cases
        ]
        self.assertEqual(python_values, [expected for _value, expected in cases])

        source = (Path(__file__).parents[1] / "static" / "app.js").read_text()
        match = re.search(
            r"  function normalizeAuthenticationSettings\(raw\).*?\n  \}",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        script = "\n".join(
            (
                'const AUTHENTICATION_MODES = new Set(["off", "idle", "every-open"]);',
                'const DEFAULT_AUTHENTICATION_MODE = "every-open";',
                "const DEFAULT_AUTHENTICATION_IDLE_MINUTES = 15;",
                match.group(0),
                'const values = ["15", " +0015 ", "15junk", 15.5, 15.0, true, false, Infinity, NaN, "999999999999999999999999", "-999999999999999999999999", 9007199254740992, 1e20, 0, 2000];',
                "process.stdout.write(JSON.stringify(values.map((idleMinutes) => normalizeAuthenticationSettings({idleMinutes}).idleMinutes)));",
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout), python_values)

        self.assertEqual(
            normalize_authentication_realms(
                {
                    "mine": {"mode": "idle", "idleMinutes": "30"},
                    "work": {"mode": "off", "idleMinutes": -4},
                    "": {"mode": "off"},
                }
            ),
            {
                "mine": {"mode": "idle", "idleMinutes": 30},
                "work": {"mode": "off", "idleMinutes": 1},
            },
        )

    def test_server_normalizes_authentication_without_losing_defaults(self):
        settings = normalize_settings(
            {
                "authentication": {"mode": "idle", "idleMinutes": 2000},
                "authenticationByRealm": {
                    "mine": {"mode": "off", "idleMinutes": 5},
                },
            }
        )

        self.assertEqual(settings["authentication"], {"mode": "idle", "idleMinutes": 1440})
        self.assertEqual(
            settings["authenticationByRealm"]["mine"],
            {"mode": "off", "idleMinutes": 5},
        )

    def test_proxy_normalization_preserves_unrelated_settings(self):
        raw = {
            "uiScale": 0.9,
            "authenticationByRealm": {
                "mine": {"mode": "idle", "idleMinutes": 0},
            },
        }

        self.assertEqual(
            normalize_authentication_fields(raw),
            {
                "uiScale": 0.9,
                "authenticationByRealm": {
                    "mine": {"mode": "idle", "idleMinutes": 1},
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
