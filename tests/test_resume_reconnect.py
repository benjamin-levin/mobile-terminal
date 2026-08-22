from pathlib import Path
import re
import subprocess
import unittest


APP_JS = Path(__file__).parents[1] / "static" / "app.js"


class ResumeReconnectWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_JS.read_text()

    def test_resume_signals_refresh_the_connection(self):
        expected = (
            'window.addEventListener("focus", () => {',
            'document.addEventListener("visibilitychange", () => {',
            'window.addEventListener("online", () => resumeApplication());',
            'window.addEventListener("pageshow", () => {',
        )
        for marker in expected:
            self.assertTrue(marker in self.source, f"missing resume signal: {marker}")
        self.assertGreaterEqual(self.source.count("resumeApplication();"), 5)

    def test_passkey_resume_storage_is_separate_and_authentication_realm_scoped(self):
        for marker in (
            'const STORAGE_PASSKEY_AUTH_MODE_KEY = "mobile-terminal.passkey-auth-mode";',
            'const STORAGE_PASSKEY_IDLE_MINUTES_KEY = "mobile-terminal.passkey-idle-minutes";',
            'const STORAGE_PASSKEY_BACKGROUNDED_AT_KEY = "mobile-terminal.passkey-backgrounded-at";',
            'return scope === "standalone" ? base : `${base}.${encodeURIComponent(scope)}`;',
            'return `realm:${realm}`;',
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("STORAGE_AUTHENTICATION_KEY", self.source)
        self.assertNotIn("JSON.stringify(authenticationSettings)", self.source)

    def test_profiles_in_one_authentication_realm_share_policy_storage(self):
        scope = re.search(r"  function authenticationScope\(.*?\n  \}", self.source, re.DOTALL)
        storage = re.search(
            r"  function authenticationStorageKey\(.*?\n  \}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(scope)
        self.assertIsNotNone(storage)
        script = "\n".join(
            (
                "let loginRealm = ''; const serverConfig = {multiTenant: false}; let currentUser = '';",
                "const STORAGE_USER_KEY = 'user'; const localStorage = {getItem: () => ''};",
                scope.group(0),
                storage.group(0),
                "process.stdout.write(JSON.stringify([",
                " authenticationStorageKey('mode', 'shared'),",
                " authenticationStorageKey('mode', 'shared'),",
                " authenticationStorageKey('mode', 'other'),",
                "]));",
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout,
            '["mode.realm%3Ashared","mode.realm%3Ashared","mode.realm%3Aother"]',
        )

    def test_passkey_policy_matrix_fails_closed_at_idle_threshold(self):
        parse = re.search(
            r"  function parseBackgroundedAt\(.*?\n  \}",
            self.source,
            re.DOTALL,
        )
        decision = re.search(
            r"  function passkeyRequiredAfterBackground\(.*?\n  \}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(parse)
        self.assertIsNotNone(decision)
        script = "\n".join(
            (
                parse.group(0),
                decision.group(0),
                'const off = { mode: "off", idleMinutes: 15 };',
                'const idle = { mode: "idle", idleMinutes: 15 };',
                'const every = { mode: "every-open", idleMinutes: 15 };',
                "const now = 2000000;",
                "const threshold = 15 * 60 * 1000;",
                "const results = {",
                "  off: passkeyRequiredAfterBackground(off, Number.NaN, true, now),",
                "  everyInitial: passkeyRequiredAfterBackground(every, 0, true, now),",
                "  everyNoMarker: passkeyRequiredAfterBackground(every, 0, false, now),",
                "  everyMarker: passkeyRequiredAfterBackground(every, now - 1, false, now),",
                "  idleMissing: passkeyRequiredAfterBackground(idle, parseBackgroundedAt(null), false, now),",
                '  idleMalformed: passkeyRequiredAfterBackground(idle, parseBackgroundedAt("junk"), false, now),',
                "  idleUnder: passkeyRequiredAfterBackground(idle, now - threshold + 1, false, now),",
                "  idleExact: passkeyRequiredAfterBackground(idle, now - threshold, false, now),",
                "  idleOver: passkeyRequiredAfterBackground(idle, now - threshold - 1, false, now),",
                "  idleFuture: passkeyRequiredAfterBackground(idle, now + 1, false, now),",
                "};",
                "process.stdout.write(JSON.stringify(results));",
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout,
            '{"off":false,"everyInitial":true,"everyNoMarker":false,'
            '"everyMarker":true,"idleMissing":true,"idleMalformed":true,'
            '"idleUnder":false,"idleExact":true,"idleOver":true,"idleFuture":true}',
        )

    def test_resume_marker_preserves_background_timestamp_and_pagehide_records_it(self):
        match = re.search(r"  async function runResumeDecision\(\).*?\n  \}", self.source, re.DOTALL)
        self.assertIsNotNone(match)
        decision = match.group(0)
        self.assertIn('const resumeMarker = `${scope}:${rawBackgroundMarker ?? "initial"}`;', decision)
        self.assertIn("handledResumeMarker === resumeMarker", decision)
        self.assertNotIn("localStorage.removeItem", decision)
        record = re.search(r"  function recordBackgrounded\(event\).*?\n  \}", self.source, re.DOTALL)
        self.assertIsNotNone(record)
        self.assertIn('event?.type !== "pagehide"', record.group(0))
        self.assertNotIn("resumeHandlingReady", record.group(0))
        self.assertIn("setPasskeyLocked(true);", record.group(0))
        self.assertIn('window.addEventListener("pagehide", recordBackgrounded);', self.source)

    def test_pagehide_ready_hidden_and_visible_resume_keeps_terminal_obscured(self):
        functions = []
        for name in (
            "applyTerminalReadyVisibility",
            "revealTerminalAfterVisibleResume",
            "recordBackgrounded",
        ):
            match = re.search(
                rf"  function {name}\(.*?\n  \}}",
                self.source,
                re.DOTALL,
            )
            self.assertIsNotNone(match)
            functions.append(match.group(0))
        script = "\n".join(
            (
                "let terminalReadyWhileHidden = false;",
                "let resumeHandlingReady = true; let backgroundRecordedScope = ''; let loginRealm = 'mine';",
                "const document = {visibilityState: 'hidden'};",
                "const lockStates = []; const setPasskeyLocked = (locked) => lockStates.push(locked);",
                "const authenticationScope = (realm) => `realm:${realm}`;",
                "const loadAuthenticationSettings = () => ({mode: 'idle', idleMinutes: 15});",
                "const authenticationStorageKey = () => 'backgrounded';",
                "const STORAGE_PASSKEY_BACKGROUNDED_AT_KEY = 'backgrounded';",
                "const stored = {}; const localStorage = {setItem: (key, value) => { stored[key] = value; }};",
                "Date.now = () => 12345;",
                *functions,
                "recordBackgrounded({type: 'pagehide'});",
                "applyTerminalReadyVisibility(true);",
                "document.visibilityState = 'visible';",
                "const revealed = revealTerminalAfterVisibleResume();",
                "process.stdout.write(JSON.stringify({lockStates, stored, revealed, terminalReadyWhileHidden}));",
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout,
            '{"lockStates":[true,true,false],"stored":{"backgrounded":"12345"},'
            '"revealed":true,"terminalReadyWhileHidden":false}',
        )
        self.assertIn("applyTerminalReadyVisibility(readyIsHidden);", self.source)
        self.assertIn("revealTerminalAfterVisibleResume();", self.source)
        self.assertIn("if (!keepEditorActive && !readyIsHidden)", self.source)

    def test_resume_locks_synchronously_before_indexeddb_access(self):
        match = re.search(r"  async function runResumeDecision\(\).*?\n  \}", self.source, re.DOTALL)
        self.assertIsNotNone(match)
        decision = match.group(0)
        self.assertLess(
            decision.index("setPasskeyLocked(true);"),
            decision.index("await refreshDeviceKeyFlag(realm);"),
        )

    def test_owner_switch_reset_clears_authentication_lifecycle_state(self):
        match = re.search(
            r"  function resetAuthenticationLifecycle\(.*?\n  \}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        script = "\n".join(
            (
                "let authenticationByRealm = {old: {mode: 'off'}};",
                "let hostAuthenticationDefault = {mode: 'off'};",
                "let authenticationSettings = {mode: 'off'};",
                "let passkeyRequiredScope = 'realm:old';",
                "let passkeyRetryPending = true;",
                "let passkeyCeremonyController = {abort: () => {}};",
                "const cancelPasskeyCeremony = () => { passkeyCeremonyController = null; };",
                "let resumeDecisionPromise = Promise.resolve();",
                "let initialResumeDecisionMade = true;",
                "let handledResumeMarker = 'old:123';",
                "let backgroundRecordedScope = 'realm:old';",
                "let hasDeviceKey = true;",
                "const normalizeAuthenticationSettings = () => ({mode: 'every-open', idleMinutes: 15});",
                "const setPasskeyRetryUi = () => {};",
                "let lockState = false; const setPasskeyLocked = (locked) => { lockState = locked; };",
                "let applied = 0; const applyAuthenticationScope = () => { applied += 1; };",
                match.group(0),
                "resetAuthenticationLifecycle();",
                "process.stdout.write(JSON.stringify({authenticationByRealm, hostAuthenticationDefault, authenticationSettings, passkeyRequiredScope, passkeyRetryPending, resumeDecisionPromise, initialResumeDecisionMade, handledResumeMarker, backgroundRecordedScope, hasDeviceKey, lockState, applied}));",
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout,
            '{"authenticationByRealm":{},"hostAuthenticationDefault":{"mode":"every-open","idleMinutes":15},'
            '"authenticationSettings":{"mode":"every-open","idleMinutes":15},"passkeyRequiredScope":"",'
            '"passkeyRetryPending":false,"resumeDecisionPromise":null,"initialResumeDecisionMade":false,'
            '"handledResumeMarker":"","backgroundRecordedScope":"","hasDeviceKey":false,"lockState":true,"applied":1}',
        )
        self.assertGreaterEqual(self.source.count("resetAuthenticationLifecycle();"), 2)
        self.assertIn("resetAuthenticationLifecycle({ locked: readyIsHidden });", self.source)

    def test_user_switch_clears_all_unscoped_passkey_keys(self):
        start = self.source.index("if (localStorage.getItem(STORAGE_SETTINGS_OWNER_KEY) !== currentUser)")
        end = self.source.index("openTabNames = [];", start)
        clear_block = self.source[start:end]
        for key in (
            "STORAGE_PASSKEY_AUTH_MODE_KEY",
            "STORAGE_PASSKEY_IDLE_MINUTES_KEY",
            "STORAGE_PASSKEY_BACKGROUNDED_AT_KEY",
        ):
            self.assertEqual(clear_block.count(key), 1)

    def test_closed_socket_reconnects_instead_of_dropping_refresh(self):
        marker = "if (!socket || socket.readyState === WebSocket.CLOSED || socket.readyState === WebSocket.CLOSING)"
        self.assertTrue(marker in self.source, "closed sockets are not reconnected on resume")
        self.assertTrue("reconnectSocket();" in self.source, "reconnect helper is not called")

    def test_ghost_open_socket_has_a_response_timeout(self):
        expected = (
            "const observedMessageAt = lastServerMessageAt;",
            "lastServerMessageAt <= observedMessageAt",
            "}, 4000);",
        )
        for marker in expected:
            self.assertTrue(marker in self.source, f"missing liveness probe marker: {marker}")

    def test_replaced_socket_events_cannot_start_an_extra_reconnect(self):
        self.assertGreaterEqual(
            self.source.count("if (socket !== thisSocket)"),
            2,
            "stale message and close handlers are not both guarded",
        )


if __name__ == "__main__":
    unittest.main()
