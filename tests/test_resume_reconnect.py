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
            'const STORAGE_PASSKEY_LAST_INTERACTION_AT_KEY = "mobile-terminal.passkey-last-interaction-at";',
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

    def test_authentication_draft_survives_same_scope_push_and_resets_on_scope_change(self):
        functions = []
        for name in (
            "normalizeAuthenticationSettings",
            "authenticationScope",
            "syncAuthenticationControls",
            "updateAuthenticationDraft",
            "applyAuthenticationScope",
        ):
            match = re.search(rf"  function {name}\(.*?\n  \}}", self.source, re.DOTALL)
            self.assertIsNotNone(match)
            functions.append(match.group(0))
        script = "\n".join(
            (
                'const DEFAULT_AUTHENTICATION_MODE = "every-open";',
                "const DEFAULT_AUTHENTICATION_IDLE_MINUTES = 15;",
                'const AUTHENTICATION_MODES = new Set(["off", "idle", "every-open"]);',
                "const serverConfig = {multiTenant: false}; let currentUser = '';",
                "const STORAGE_USER_KEY = 'user'; const localStorage = {getItem: () => ''};",
                "let loginRealm = 'alpha'; let overlayHidden = true;",
                "const authenticationOverlay = {classList: {contains: () => overlayHidden}};",
                "const authenticationModeInput = {value: ''};",
                "const authenticationIdleInput = {value: ''};",
                "const authenticationIdleControl = {classList: {toggle: () => {}}};",
                "let authenticationSettings; let draftAuthenticationSettings;",
                "let draftAuthenticationRealm = ''; let draftAuthenticationScope = '';",
                "const authoritative = {alpha: {mode: 'every-open', idleMinutes: 15}, beta: {mode: 'every-open', idleMinutes: 9}};",
                "const loadAuthenticationSettings = (realm) => authoritative[realm];",
                *functions,
                "applyAuthenticationScope('alpha');",
                "overlayHidden = false;",
                "updateAuthenticationDraft({mode: 'idle', idleMinutes: 42}, 'alpha');",
                "authoritative.alpha = {mode: 'off', idleMinutes: 3};",
                "applyAuthenticationScope('alpha');",
                "const sameScope = {authoritative: authenticationSettings, draft: draftAuthenticationSettings, realm: draftAuthenticationRealm};",
                "loginRealm = 'beta'; applyAuthenticationScope('beta');",
                "process.stdout.write(JSON.stringify({sameScope, changed: {draft: draftAuthenticationSettings, realm: draftAuthenticationRealm, scope: draftAuthenticationScope}}));",
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
            '{"sameScope":{"authoritative":{"mode":"off","idleMinutes":3},'
            '"draft":{"mode":"idle","idleMinutes":42},"realm":"alpha"},'
            '"changed":{"draft":{"mode":"every-open","idleMinutes":9},'
            '"realm":"beta","scope":"realm:beta"}}',
        )

    def test_authentication_save_normalizes_verified_realm_and_rejects_cross_realm(self):
        functions = []
        for name in (
            "normalizeAuthenticationSettings",
            "authenticationScope",
            "authenticationStorageKey",
            "syncAuthenticationControls",
            "updateAuthenticationDraft",
            "saveAuthentication",
        ):
            match = re.search(rf"  function {name}\(.*?\n  \}}", self.source, re.DOTALL)
            self.assertIsNotNone(match)
            functions.append(match.group(0))
        script = "\n".join(
            (
                'const DEFAULT_AUTHENTICATION_MODE = "every-open";',
                "const DEFAULT_AUTHENTICATION_IDLE_MINUTES = 15;",
                'const AUTHENTICATION_MODES = new Set(["off", "idle", "every-open"]);',
                "const STORAGE_PASSKEY_AUTH_MODE_KEY = 'auth-mode';",
                "const STORAGE_PASSKEY_IDLE_MINUTES_KEY = 'idle-minutes';",
                "const STORAGE_USER_KEY = 'user'; const serverConfig = {multiTenant: false}; let currentUser = '';",
                "let loginRealm = 'alpha'; let draftAuthenticationRealm = 'alpha'; let draftAuthenticationScope = 'realm:alpha';",
                "let draftAuthenticationSettings = {mode: 'idle', idleMinutes: 15}; let authenticationSettings = {mode: 'idle', idleMinutes: 15};",
                "let passkeyRequiredScope = ''; let passkeyRetryPending = false;",
                "const authenticationModeInput = {value: 'idle'}; const authenticationIdleInput = {value: '9999'};",
                "const authenticationIdleControl = {classList: {toggle: () => {}}};",
                "const authenticationOverlay = {classList: {add: () => {}}};",
                "const values = {}; const localStorage = {getItem: () => '', setItem: (key, value) => { values[key] = value; }};",
                "const seeds = []; const recordUserInteraction = (realm) => { seeds.push(realm); return true; };",
                "let applyCount = 0; const applyAuthenticationScope = () => { applyCount += 1; authenticationSettings = draftAuthenticationSettings; };",
                "let hostSaves = 0; const saveHostSettings = () => { hostSaves += 1; };",
                "const setPasskeyLocked = () => {}; const reconnectSocket = () => {};",
                *functions,
                "saveAuthentication();",
                "const verified = {values: {...values}, seeds: [...seeds], draft: draftAuthenticationSettings, hostSaves};",
                "loginRealm = 'beta'; draftAuthenticationRealm = 'alpha'; draftAuthenticationScope = 'realm:alpha';",
                "authenticationModeInput.value = 'off'; authenticationIdleInput.value = '1';",
                "saveAuthentication();",
                "process.stdout.write(JSON.stringify({verified, afterRejected: {values, seeds, hostSaves, applyCount}}));",
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
            '{"verified":{"values":{"auth-mode.realm%3Aalpha":"idle",'
            '"idle-minutes.realm%3Aalpha":"1440"},"seeds":["alpha"],'
            '"draft":{"mode":"idle","idleMinutes":1440},"hostSaves":1},'
            '"afterRejected":{"values":{"auth-mode.realm%3Aalpha":"idle",'
            '"idle-minutes.realm%3Aalpha":"1440"},"seeds":["alpha"],'
            '"hostSaves":1,"applyCount":2}}',
        )
        self.assertIn("saveHostSettings(realm);", self.source)
        self.assertIn(
            "function activeAuthenticationSettings(realm = activeProfile()?.authRealm || loginRealm)",
            self.source,
        )


    def test_config_failure_retries_without_fabricating_token_auth_and_restarts_normally(self):
        load = re.search(r"  async function loadServerConfig\(\).*?\n  \}", self.source, re.DOTALL)
        retry = re.search(r"  function showAuthConfigRetrying\(\).*?\n  \}", self.source, re.DOTALL)
        poll = re.search(r"  function scheduleAuthConfigPolling\(\).*?\n  \}", self.source, re.DOTALL)
        startup = re.search(
            r"  async function startAuthenticationClient\(\).*?\n  \}",
            self.source,
            re.DOTALL,
        )
        for match in (load, retry, poll, startup):
            self.assertIsNotNone(match)
        self.assertIn("serverConfigAvailable = false;\n      return false;", load.group(0))
        self.assertNotIn("requireToken: true", load.group(0))
        self.assertIn('loginMessage.textContent =', retry.group(0))
        self.assertIn('tokenInput.classList.add("hidden");', retry.group(0))
        self.assertIn("await startAuthenticationClient();", poll.group(0))
        self.assertNotIn("requireToken", poll.group(0))
        self.assertLess(
            startup.group(0).index("await loadServerConfig()"),
            startup.group(0).index("showAuthConfigRetrying();"),
        )
        self.assertLess(
            startup.group(0).index("const authenticationReady = await prepareAuthenticationClient();"),
            startup.group(0).index("connect();"),
        )
        self.assertIn("startAuthenticationClient();", self.source)

    def test_device_enrollment_ack_success_failure_and_legacy_timeout(self):
        functions = []
        for name in ("finishDeviceKeyEnrollmentCompatibility", "finishDeviceKeyEnrollment"):
            match = re.search(rf"  function {name}\(.*?\n  \}}", self.source, re.DOTALL)
            self.assertIsNotNone(match)
            functions.append(match.group(0))
        script = "\n".join(
            (
                "let pendingDeviceEnrollment = null; let hasDeviceKey = false;",
                "const authenticationScope = () => 'realm:mine';",
                "const window = {clearTimeout: () => {}};",
                *functions,
                "pendingDeviceEnrollment = {enrollmentId: 'ok', scope: 'realm:mine', previousHasDeviceKey: false, timer: 1};",
                "const success = finishDeviceKeyEnrollment({type: 'register-key-ok', enrollmentId: 'ok'});",
                "const afterSuccess = hasDeviceKey; hasDeviceKey = false;",
                "pendingDeviceEnrollment = {enrollmentId: 'bad', scope: 'realm:mine', previousHasDeviceKey: false, timer: 2};",
                "const failure = finishDeviceKeyEnrollment({type: 'register-key-error', enrollmentId: 'bad'});",
                "const afterFailure = hasDeviceKey;",
                "pendingDeviceEnrollment = {enrollmentId: 'legacy', scope: 'realm:mine', previousHasDeviceKey: false, timer: 3};",
                "const timeout = finishDeviceKeyEnrollmentCompatibility('legacy', 'realm:mine');",
                "process.stdout.write(JSON.stringify({success, afterSuccess, failure, afterFailure, timeout, afterTimeout: hasDeviceKey, pendingDeviceEnrollment}));",
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
            '{"success":true,"afterSuccess":true,"failure":true,"afterFailure":false,'
            '"timeout":true,"afterTimeout":true,"pendingDeviceEnrollment":null}',
        )
        enroll = re.search(r"  async function enrollDeviceKey\(.*?\n  \}", self.source, re.DOTALL)
        self.assertIsNotNone(enroll)
        self.assertIn("}, 3000)", enroll.group(0))
        self.assertNotIn("hasDeviceKey = true;", enroll.group(0))

    def test_passkey_policy_matrix_fails_closed_at_idle_threshold(self):
        parse = re.search(
            r"  function parseAuthenticationTimestamp\(.*?\n  \}",
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
                "const AUTHENTICATION_TIMESTAMP_MAX_FUTURE_MS = 24 * 60 * 60 * 1000;",
                'const off = { mode: "off", idleMinutes: 15 };',
                'const idle = { mode: "idle", idleMinutes: 15 };',
                'const every = { mode: "every-open", idleMinutes: 15 };',
                "const now = 2000000;",
                "const threshold = 15 * 60 * 1000;",
                "const results = {",
                "  off: passkeyRequiredAfterBackground(off, Number.NaN, Number.NaN, true, now),",
                "  everyInitial: passkeyRequiredAfterBackground(every, 0, 0, true, now),",
                "  everyNoMarker: passkeyRequiredAfterBackground(every, 0, now, false, now),",
                "  everyMarker: passkeyRequiredAfterBackground(every, now - 1, now, false, now),",
                "  idleMissing: passkeyRequiredAfterBackground(idle, now, parseAuthenticationTimestamp(null), false, now),",
                '  idleMalformed: passkeyRequiredAfterBackground(idle, now, parseAuthenticationTimestamp("junk"), false, now),',
                "  idleUnder: passkeyRequiredAfterBackground(idle, now, now - threshold + 1, false, now),",
                "  idleExact: passkeyRequiredAfterBackground(idle, now, now - threshold, false, now),",
                "  idleOver: passkeyRequiredAfterBackground(idle, now, now - threshold - 1, false, now),",
                "  idleFutureCorrection: passkeyRequiredAfterBackground(idle, now, now + 2 * 60 * 1000, false, now),",
                "  idleFutureDay: passkeyRequiredAfterBackground(idle, now, now + 24 * 60 * 60 * 1000, false, now),",
                "  idleGrossFuture: passkeyRequiredAfterBackground(idle, now, now + 24 * 60 * 60 * 1000 + 1, false, now),",
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
            '"idleUnder":false,"idleExact":true,"idleOver":true,'
            '"idleFutureCorrection":false,"idleFutureDay":false,"idleGrossFuture":true}',
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
        self.assertIn("const backgroundedAt = Date.now();", record.group(0))
        self.assertLess(
            record.group(0).index("setPasskeyLocked(true);"),
            record.group(0).index("writeBackgroundedAt(realm, backgroundedAt);"),
        )
        pagehide = self.source[self.source.index('window.addEventListener("pagehide", (event) => {') :]
        pagehide = pagehide[: pagehide.index("  });")]
        self.assertIn("clearActiveShortcutRepeatTimers();", pagehide)
        self.assertIn("cancelPasskeyCeremony();", pagehide)
        self.assertIn("recordBackgrounded(event);", pagehide)


    def test_real_interaction_is_realm_scoped_and_persisted_without_a_timer(self):
        match = re.search(
            r"  function recordUserInteraction\(.*?\n  \}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        script = "\n".join(
            (
                "let loginRealm = 'alpha'; let initialResumeDecisionMade = true;",
                "let passkeyRequiredScope = ''; let terminalReadyWhileHidden = false;",
                "const authenticationScope = (realm = loginRealm) => `realm:${realm}`;",
                "const authenticationStorageKey = (_base, realm) => `interaction:${realm}`;",
                "const STORAGE_PASSKEY_LAST_INTERACTION_AT_KEY = 'last-interaction';",
                "const writes = []; const localStorage = {setItem: (key, value) => writes.push([key, value])};",
                match.group(0),
                "const results = [",
                "  recordUserInteraction('beta', 700),",
                "  recordUserInteraction('alpha', Number.NaN),",
                "  recordUserInteraction('alpha', 701),",
                "];",
                "process.stdout.write(JSON.stringify({results, writes}));",
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
            '{"results":[false,false,true],"writes":[["interaction:alpha","701"]]}',
        )
        self.assertNotIn("recordVisibleIdleCheckpoint", self.source)
        self.assertNotIn("setInterval(recordVisibleIdleCheckpoint", self.source)
        self.assertIn(
            'document.addEventListener("keydown", () => recordUserInteraction(), { capture: true });',
            self.source,
        )
        self.assertIn(
            'document.addEventListener("paste", () => recordUserInteraction(), { capture: true });',
            self.source,
        )


    def test_system_resume_ready_and_focus_do_not_extend_idle_grace(self):
        decision = re.search(
            r"  async function runResumeDecision\(\).*?\n  \}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(decision)
        self.assertNotIn("recordUserInteraction", decision.group(0))
        self.assertNotIn("writeBackgroundedAt", decision.group(0))

        ready_start = self.source.index('if (payload.type === "ready")')
        ready_end = self.source.index('if (payload.type === "tabs")', ready_start)
        ready = self.source[ready_start:ready_end]
        self.assertIn("if (completedPasskeyInteraction)", ready)
        self.assertIn("recordUserInteraction(loginRealm, Date.now(), true);", ready)
        self.assertLess(ready.index("removeAuthenticationStorage();"), ready.index("recordUserInteraction("))
        self.assertLess(
            ready.index("resetAuthenticationLifecycle({ locked: readyIsHidden });"),
            ready.index("initialResumeDecisionMade = true;"),
        )
        self.assertLess(
            ready.index("initialResumeDecisionMade = true;"),
            ready.index("recordUserInteraction("),
        )
        self.assertNotIn("writeBackgroundedAt", ready)
        self.assertIn('reportForcedActivity(false);', self.source)

    def test_authoritative_realm_resets_and_reruns_resume_decision(self):
        scope = re.search(r"  function authenticationScope\(.*?\n  \}", self.source, re.DOTALL)
        apply_realm = re.search(
            r"  async function applyAuthoritativeAuthenticationScope\(.*?\n  \}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(scope)
        self.assertIsNotNone(apply_realm)
        script = "\n".join(
            (
                "let loginRealm = 'provisional'; const serverConfig = {multiTenant: false}; let currentUser = '';",
                "const STORAGE_USER_KEY = 'user'; const localStorage = {getItem: () => ''};",
                "let resumeHandlingReady = true; let resumeDecisionPromise = null; let passkeyRequiredScope = 'realm:provisional'; let passkeyRetryPending = true;",
                "let initialResumeDecisionMade = true; let handledResumeMarker = 'realm:provisional:123'; let backgroundRecordedScope = 'realm:provisional';",
                "const applied = []; const applyAuthenticationScope = (realm) => applied.push(realm);",
                "const setPasskeyLocked = () => {};",
                "let resumes = 0; const resumeApplication = async () => { resumes += 1; };",
                scope.group(0),
                apply_realm.group(0),
                "applyAuthoritativeAuthenticationScope('authoritative').then(() => process.stdout.write(JSON.stringify({loginRealm, applied, resumes, passkeyRequiredScope, passkeyRetryPending, initialResumeDecisionMade, handledResumeMarker, backgroundRecordedScope})));",
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
            '{"loginRealm":"authoritative","applied":["authoritative"],"resumes":1,'
            '"passkeyRequiredScope":"","passkeyRetryPending":false,'
            '"initialResumeDecisionMade":false,"handledResumeMarker":"",'
            '"backgroundRecordedScope":""}',
        )
        self.assertGreaterEqual(
            self.source.count("await applyAuthoritativeAuthenticationScope("),
            2,
        )
        send = re.search(r"  async function sendAuthResponse\(.*?\n  \}", self.source, re.DOTALL)
        self.assertIsNotNone(send)
        self.assertLess(
            send.group(0).index("await applyAuthoritativeAuthenticationScope(realm);"),
            send.group(0).index("authSocket.send(JSON.stringify(msg));"),
        )

    def test_authoritative_scope_queues_behind_provisional_resume_before_auth_response(self):
        functions = []
        for pattern in (
            r"  function authenticationScope\(.*?\n  \}",
            r"  async function applyAuthoritativeAuthenticationScope\(.*?\n  \}",
            r"  async function sendAuthResponse\(.*?\n  \}",
            r"  function resumeApplication\(\).*?\n  \}",
        ):
            match = re.search(pattern, self.source, re.DOTALL)
            self.assertIsNotNone(match)
            functions.append(match.group(0))
        script = "\n".join(
            (
                "let loginRealm = 'provisional'; let currentUser = 'owner';",
                "const serverConfig = {multiTenant: false, profileMode: true, rpId: 'terminal.test'};",
                "const STORAGE_USER_KEY = 'user'; const localStorage = {getItem: () => ''};",
                "let resumeHandlingReady = true; let resumeDecisionPromise = null;",
                "let passkeyRequiredScope = ''; let passkeyRetryPending = true;",
                "let initialResumeDecisionMade = true; let handledResumeMarker = 'realm:provisional:123';",
                "let backgroundRecordedScope = 'realm:provisional';",
                "const document = {visibilityState: 'visible'}; const WebSocket = {OPEN: 1};",
                "const location = {hostname: 'terminal.test'}; const messages = []; const events = [];",
                "let locked = false; const lockStates = []; const setPasskeyLocked = (value) => { locked = value; lockStates.push(value); events.push(`lock:${value}`); };",
                "const socket = {readyState: WebSocket.OPEN, send: (message) => { messages.push(JSON.parse(message)); events.push('auth-response'); }};",
                "const applyAuthenticationScope = () => {}; const tokenStorageKey = () => 'token';",
                "const deviceProtocolRealm = (realm) => realm; const getDeviceId = () => 'device';",
                "const loadDeviceKey = async () => null;",
                "let releaseProvisional; let activeDecisions = 0; let maxActiveDecisions = 0;",
                "const runResumeDecision = async () => {",
                "  const realm = loginRealm; activeDecisions += 1; maxActiveDecisions = Math.max(maxActiveDecisions, activeDecisions);",
                "  events.push(`start:${realm}`);",
                "  if (realm === 'provisional') await new Promise((resolve) => { releaseProvisional = resolve; });",
                "  if (realm === 'authoritative') passkeyRequiredScope = `realm:${realm}`;",
                "  events.push(`end:${realm}`); activeDecisions -= 1;",
                "};",
                *functions,
                "(async () => {",
                "  const provisionalResume = resumeApplication();",
                "  const authResponse = sendAuthResponse('nonce', 'authoritative', 'profile', 'terminal.test', socket);",
                "  await Promise.resolve(); const messagesBeforeRelease = messages.length; const lockedBeforeRelease = locked; events.push('release:provisional'); releaseProvisional();",
                "  await Promise.all([provisionalResume, authResponse]);",
                "  process.stdout.write(JSON.stringify({events, lockStates, lockedBeforeRelease, messagesBeforeRelease, requirePasskey: messages[0]?.requirePasskey, maxActiveDecisions}));",
                "})().catch((error) => { console.error(error); process.exitCode = 1; });",
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
            '{"events":["start:provisional","lock:true","release:provisional","end:provisional","start:authoritative",'
            '"end:authoritative","auth-response"],"lockStates":[true],"lockedBeforeRelease":true,'
            '"messagesBeforeRelease":0,"requirePasskey":true,"maxActiveDecisions":1}',
        )

    def test_pagehide_ready_hidden_and_visible_resume_keeps_terminal_obscured(self):
        functions = []
        for name in (
            "applyTerminalReadyVisibility",
            "revealTerminalAfterVisibleResume",
            "writeBackgroundedAt",
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
                "const authenticationScope = (realm = loginRealm) => `realm:${realm}`;",
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
                "let draftAuthenticationSettings = {mode: 'off'};",
                "let draftAuthenticationRealm = 'old'; let draftAuthenticationScope = 'realm:old';",
                "let passkeyRequiredScope = 'realm:old';",
                "let passkeyRetryPending = true;",
                "let passkeyInteractionPending = true;",
                "let passkeyCeremonyController = {abort: () => {}};",
                "const cancelPasskeyCeremony = () => { passkeyCeremonyController = null; };",
                "const cancelPendingDeviceEnrollment = () => {};",
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
                "process.stdout.write(JSON.stringify({authenticationByRealm, hostAuthenticationDefault, authenticationSettings, draftAuthenticationSettings, draftAuthenticationRealm, draftAuthenticationScope, passkeyRequiredScope, passkeyRetryPending, passkeyInteractionPending, resumeDecisionPromise, initialResumeDecisionMade, handledResumeMarker, backgroundRecordedScope, hasDeviceKey, lockState, applied}));",
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
            '"authenticationSettings":{"mode":"every-open","idleMinutes":15},'
            '"draftAuthenticationSettings":{"mode":"every-open","idleMinutes":15},'
            '"draftAuthenticationRealm":"","draftAuthenticationScope":"","passkeyRequiredScope":"",'
            '"passkeyRetryPending":false,"passkeyInteractionPending":false,"resumeDecisionPromise":null,'
            '"initialResumeDecisionMade":false,'
            '"handledResumeMarker":"","backgroundRecordedScope":"","hasDeviceKey":false,"lockState":true,"applied":1}',
        )
        self.assertGreaterEqual(self.source.count("resetAuthenticationLifecycle();"), 2)
        self.assertIn("resetAuthenticationLifecycle({ locked: readyIsHidden });", self.source)

    def test_user_switch_clears_all_scoped_passkey_keys_without_skipping(self):
        match = re.search(
            r"  function removeAuthenticationStorage\(\).*?\n  \}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        script = "\n".join(
            (
                "const STORAGE_PASSKEY_AUTH_MODE_KEY = 'auth-mode';",
                "const STORAGE_PASSKEY_IDLE_MINUTES_KEY = 'idle-minutes';",
                "const STORAGE_PASSKEY_BACKGROUNDED_AT_KEY = 'backgrounded-at';",
                "const STORAGE_PASSKEY_LAST_INTERACTION_AT_KEY = 'last-interaction-at';",
                "const values = new Map([",
                "  ['auth-mode', 'off'], ['auth-mode.realm%3Aone', 'idle'],",
                "  ['idle-minutes.realm%3Aone', '15'], ['idle-minutes.realm%3Atwo', '30'],",
                "  ['backgrounded-at.user%3Aone', '123'], ['last-interaction-at.user%3Aone', '124'],",
                "  ['unrelated', 'keep'],",
                "]);",
                "const localStorage = {",
                "  get length() { return values.size; },",
                "  key(index) { return Array.from(values.keys())[index] ?? null; },",
                "  removeItem(key) { values.delete(key); },",
                "};",
                match.group(0),
                "removeAuthenticationStorage();",
                "process.stdout.write(JSON.stringify(Object.fromEntries(values)));",
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, '{"unrelated":"keep"}')
        self.assertIn("removeAuthenticationStorage();", self.source)

    def test_auth_close_clears_token_only_for_rejected_token_reason(self):
        close_start = self.source.index('socket.addEventListener("close", (event) => {')
        close_end = self.source.index("\n    });", close_start)
        close_handler = self.source[close_start:close_end]
        auth_failure = close_handler[close_handler.index("if (event.code === 4001)") :]
        self.assertIn('const tokenRejected = event.reason === "token rejected";', auth_failure)
        self.assertIn("const passkeyRetryAvailable = !tokenRejected && loginSupportsPasskey();", auth_failure)
        self.assertIn("if (tokenRejected) {\n          localStorage.removeItem(tokenStorageKey());", auth_failure)
        self.assertNotIn("localStorage.removeItem(tokenStorageKey());\n        scheduleAuthConfigPolling", auth_failure)

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
