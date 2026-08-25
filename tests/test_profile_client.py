from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text()
INDEX_HTML = (ROOT / "static" / "index.html").read_text()
STYLES_CSS = (ROOT / "static" / "styles.css").read_text()
PROXY_PY = (ROOT / "proxy.py").read_text()
SW_JS = (ROOT / "static" / "sw.js").read_text()
PASSKEY_JS = (ROOT / "static" / "passkey.js").read_text()


def section(source, start_marker, end_marker):
    start = source.index(start_marker)
    return source[start : source.index(end_marker, start)]


class ProfileClientWiringTest(unittest.TestCase):
    def test_profile_switch_protocol_and_ui_are_wired(self):
        for marker in (
            'type: "switch-profile"',
            'type: "request-profiles"',
            'payload.type === "profiles"',
            'payload.type === "profile-status"',
            'profileButton.addEventListener("click", toggleProfileMenu)',
        ):
            self.assertIn(marker, APP_JS)
        self.assertIn('id="profileBanner"', INDEX_HTML)
        self.assertIn('id="profileMenu"', INDEX_HTML)

    def test_active_session_open_tabs_and_editor_state_use_profile_keys(self):
        self.assertIn('profileStateKey("active-session"', APP_JS)
        self.assertIn('profileStateKey("open-tabs"', APP_JS)
        self.assertIn('profileStateKey("editor-tabs"', APP_JS)
        self.assertIn('const STORAGE_PROFILE_PREFIX = "mobile-terminal.profile."', APP_JS)
        self.assertIn('sessionSnapshotKey(sessionName)', APP_JS)

    def test_file_bookmarks_and_requests_are_profile_scoped(self):
        self.assertIn("fileBookmarksByProfile", APP_JS)
        self.assertIn("profileId: activeProfileId", APP_JS)
        self.assertIn("context.profileId !== activeProfileId", APP_JS)
        self.assertIn("cancelPendingFileRequests();", APP_JS)
        self.assertIn("legacyEditorTabs", APP_JS)

    def test_failed_cross_realm_switch_uses_pending_profile_login_policy(self):
        match = re.search(r"  function loginRequiresToken\(\) \{.*?\n  \}", APP_JS, re.DOTALL)
        self.assertIsNotNone(match)
        script = "\n".join(
            (
                'const profiles = [{id: "alpha", requireToken: false}, {id: "beta", requireToken: true}];',
                'let activeProfileId = "alpha";',
                'let pendingProfileId = "beta";',
                'const serverConfig = {requireToken: false};',
                match.group(0),
                'process.stdout.write(String(loginRequiresToken()));',
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, "true")
        self.assertGreaterEqual(APP_JS.count("loginRequiresToken()"), 3)

    def test_profile_switch_is_transactional_and_reconnects_with_the_target(self):
        switch_profile = section(
            APP_JS,
            "  function switchProfile(profileId)",
            "  function renderSessionMenu()",
        )
        self.assertNotIn("localStorage", switch_profile)
        self.assertNotIn("term.reset()", switch_profile)
        script = "\n".join(
            (
                'const assert = require("node:assert/strict");',
                'const WebSocket = { OPEN: 1 };',
                'const profiles = [',
                '  { id: "alpha", authRealm: "alpha-realm" },',
                '  { id: "beta", authRealm: "beta-realm" },',
                '];',
                'let activeProfileId = "alpha";',
                'let pendingProfileId = "";',
                'let loginRealm = "alpha-realm";',
                'let selectedSessionName = "alpha-session";',
                'let terminalAuthoritative = true;',
                'let socket = { readyState: WebSocket.OPEN };',
                'let sendSucceeds = true;',
                'let events = [];',
                'function snapshotActiveSession() { events.push("snapshot"); }',
                'function loadActiveSession(profileId) {',
                '  events.push(`load-session:${profileId}`);',
                '  return `${profileId}-session`;',
                '}',
                'function closeProfileMenu() { events.push("close-profile"); }',
                'function closeSessionMenu() { events.push("close-session"); }',
                'function closeTabMenu() { events.push("close-tab"); }',
                'function sendMessage(payload) {',
                '  events.push(["send", payload]);',
                '  return sendSucceeds;',
                '}',
                'function reconnectSocket() { events.push("reconnect"); }',
                'function loadActiveProfileState(previousProfileId) {',
                '  events.push(`load-state:${previousProfileId}`);',
                '}',
                'function clearTerminalSelectionUI() { events.push("clear-selection"); }',
                'function resetComposerTracking() { events.push("reset-composer"); }',
                'function applyActiveProfile() { events.push("apply-profile"); }',
                'function renderProfileMenu() { events.push("render-profile-menu"); }',
                'function syncOpenTabsToSessions() { events.push("sync-tabs"); }',
                switch_profile,
                'switchProfile("beta");',
                'assert.equal(activeProfileId, "beta");',
                'assert.equal(pendingProfileId, "beta");',
                'assert.equal(loginRealm, "beta-realm");',
                'const sentIndex = events.findIndex((event) => Array.isArray(event));',
                'assert.ok(sentIndex >= 0);',
                'assert.deepEqual(events[sentIndex][1], {',
                '  type: "switch-profile", profile: "beta", session: "beta-session",',
                '});',
                'assert.ok(sentIndex < events.indexOf("load-state:alpha"));',
                'assert.ok(sentIndex < events.indexOf("clear-selection"));',
                'events = [];',
                'activeProfileId = "alpha";',
                'pendingProfileId = "";',
                'loginRealm = "alpha-realm";',
                'terminalAuthoritative = true;',
                'socket = { readyState: WebSocket.OPEN };',
                'sendSucceeds = false;',
                'switchProfile("beta");',
                'assert.equal(activeProfileId, "alpha");',
                'assert.equal(pendingProfileId, "beta");',
                'assert.equal(loginRealm, "beta-realm");',
                'assert.ok(events.some((event) => Array.isArray(event)));',
                'assert.ok(events.includes("reconnect"));',
                'assert.ok(!events.includes("load-state:alpha"));',
                'assert.ok(!events.includes("clear-selection"));',
                'events = [];',
                'activeProfileId = "alpha";',
                'pendingProfileId = "";',
                'loginRealm = "alpha-realm";',
                'socket = { readyState: 3 };',
                'sendSucceeds = true;',
                'switchProfile("beta");',
                'assert.equal(activeProfileId, "alpha");',
                'assert.equal(pendingProfileId, "beta");',
                'assert.ok(!events.some((event) => Array.isArray(event)));',
                'assert.ok(events.includes("reconnect"));',
                'assert.ok(!events.includes("load-state:alpha"));',
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_profile_reconnect_url_and_ready_confirmation_use_pending_target(self):
        ws_url = section(APP_JS, "  function wsUrl()", "  function stopAuthConfigPolling()")
        update_inventory = section(
            APP_JS,
            "  function updateProfileInventory(",
            "  function migrateLegacyProfileState(",
        )
        ready = section(
            APP_JS,
            '    if (payload.type === "ready")',
            '    if (payload.type === "tabs")',
        )
        self.assertIn("profileSwitchConfirmed", ready)
        self.assertIn("profileSwitchConfirmed,", ready)
        script = "\n".join(
            (
                'const assert = require("node:assert/strict");',
                'const window = { location: { href: "https://terminal.test/app", protocol: "https:" } };',
                'let activeProfileId = "alpha";',
                'let pendingProfileId = "beta";',
                'let selectedSessionName = "alpha-session";',
                'function loadActiveSession(profileId) { return `${profileId}-session`; }',
                ws_url,
                'let url = new URL(wsUrl());',
                'assert.equal(url.searchParams.get("profile"), "beta");',
                'assert.equal(url.searchParams.get("session"), "beta-session");',
                'pendingProfileId = "";',
                'url = new URL(wsUrl());',
                'assert.equal(url.searchParams.get("profile"), "alpha");',
                'assert.equal(url.searchParams.get("session"), "alpha-session");',
                'let profiles = [',
                '  { id: "alpha", authRealm: "alpha-realm" },',
                '  { id: "beta", authRealm: "beta-realm" },',
                '];',
                'pendingProfileId = "beta";',
                'let loginRealm = "alpha-realm";',
                'const writes = [];',
                'const loaded = [];',
                'const scopes = [];',
                'const deviceRealms = [];',
                'const STORAGE_ACTIVE_PROFILE_KEY = "active-profile";',
                'const localStorage = { setItem: (key, value) => writes.push([key, value]) };',
                'let profileMenuOpen = false;',
                'function activeProfile() {',
                '  return profiles.find((profile) => profile.id === activeProfileId) || null;',
                '}',
                'function applyAuthenticationScope(realm) { scopes.push(realm); }',
                'function refreshDeviceKeyFlag(realm) { deviceRealms.push(realm); }',
                'function loadActiveProfileState(profileId) { loaded.push(profileId); }',
                'function applyActiveProfile() {}',
                'function renderProfileMenu() {}',
                'function positionProfileMenu() {}',
                update_inventory,
                'updateProfileInventory(profiles, "beta");',
                'assert.equal(activeProfileId, "alpha");',
                'assert.deepEqual(writes, []);',
                'assert.deepEqual(loaded, []);',
                'updateProfileInventory(profiles, "beta", true);',
                'assert.equal(activeProfileId, "beta");',
                'assert.equal(loginRealm, "beta-realm");',
                'assert.deepEqual(writes, [["active-profile", "beta"]]);',
                'assert.deepEqual(loaded, ["alpha"]);',
                'assert.deepEqual(scopes, ["beta-realm"]);',
                'assert.deepEqual(deviceRealms, ["beta-realm"]);',
                'writes.length = 0;',
                'loaded.length = 0;',
                'updateProfileInventory(profiles, "beta", true);',
                'assert.deepEqual(writes, [["active-profile", "beta"]]);',
                'assert.deepEqual(loaded, []);',
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_proxy_shell_and_message_router_load_passkey_helper(self):
        self.assertIn('<script defer src="/static/passkey.js"></script>', PROXY_PY)
        self.assertIn('const CACHE = "mobile-terminal-v20";', PROXY_PY)
        self.assertIn('const CACHE = "mobile-terminal-proxy-v15";', PROXY_PY)
        self.assertIn("startPasskeyCeremony(payload, messageSocket, generation);", APP_JS)
        self.assertIn("sendAuthenticationMessage,", APP_JS)
        self.assertIn("ensurePasskeyHelper", APP_JS)
        self.assertIn("showProxySignIn(payload", APP_JS)
        self.assertLess(
            PROXY_PY.index('<script defer src="/static/passkey.js"></script>'),
            PROXY_PY.index("'    <script defer src=\"/static/app.js\"></script>'"),
        )

    def test_authentication_sheet_is_isolated_from_display_mobile_grid(self):
        start = INDEX_HTML.index('id="authenticationOverlay"')
        end = INDEX_HTML.index('id="usageOverlay"', start)
        authentication_markup = INDEX_HTML[start:end]
        self.assertIn('class="sheet authentication-sheet"', authentication_markup)
        self.assertEqual(authentication_markup.count('class="authentication-control"'), 2)
        self.assertIn('class="authentication-control"', authentication_markup)
        self.assertNotIn("display-sheet", authentication_markup)
        self.assertNotIn("display-control", authentication_markup)

        mobile = STYLES_CSS[STYLES_CSS.index("@media (max-width: 720px)") :]
        for marker in (
            ".display-sheet,\n  .authentication-sheet {",
            "height: calc(var(--app-height) - var(--sheet-top-offset));",
            ".authentication-sheet {\n    display: block;",
            ".authentication-sheet .authentication-control {\n    width: 100%;\n    height: auto;",
            ".authentication-sheet .text-input {\n    height: auto;",
            ".authentication-sheet .helper-text {\n    width: 100%;\n    text-align: left;",
        ):
            self.assertIn(marker, mobile)
        self.assertIn(
            ".display-sheet {\n    display: grid;\n    grid-template-columns: repeat(2, minmax(0, 1fr));",
            mobile,
        )
        self.assertNotIn(".authentication-sheet .display-control", mobile)

    def test_passkey_credential_management_is_wired(self):
        self.assertIn('type: "revoke-credential"', APP_JS)
        self.assertIn('type: "revoke-all-credentials"', APP_JS)
        self.assertIn('payload.type === "devices"', APP_JS)

    def test_plain_server_loads_passkeys_and_offers_fresh_retry(self):
        self.assertIn("Boolean(serverConfig.passkeyAuth)", APP_JS)
        self.assertIn("const passkeyAvailable = loginSupportsPasskey();", APP_JS)
        self.assertIn('id="passkeyLoginButton"', INDEX_HTML)
        self.assertIn('>Use passkey</button>', INDEX_HTML)
        self.assertIn('tokenFieldLabel.textContent = passkeyEnrolled ? "Or enter access token"', APP_JS)
        self.assertIn('passkeyLoginButton.addEventListener("click", () => {', APP_JS)
        retry_start = APP_JS.index('passkeyLoginButton.addEventListener("click"')
        retry_end = APP_JS.index('loginForm.addEventListener("submit"', retry_start)
        retry = APP_JS[retry_start:retry_end]
        self.assertIn("reconnectSocket();", retry)
        self.assertIn('authenticationSocket.close(4000, "passkey retry");', APP_JS)
        self.assertIn('const CACHE = "mobile-terminal-v20";', SW_JS)

    def test_passkey_ceremony_is_cancelled_and_bound_to_its_socket(self):
        self.assertIn("cancelPasskeyCeremony();", APP_JS)
        self.assertIn("authenticationSocket !== socket", APP_JS)
        self.assertIn("ceremonyController?.signal", APP_JS)
        self.assertIn("async function handleMessage(payload, send, signal)", PASSKEY_JS)
        self.assertIn("const CEREMONY_TIMEOUT_MS = 90000;", PASSKEY_JS)
        self.assertGreaterEqual(PASSKEY_JS.count("...(ceremonySignal ? { signal: ceremonySignal } : {})"), 2)

    def test_early_websocket_keeps_profile_state_proxy_only(self):
        self.assertIn('localStorage.getItem("mobile-terminal.active-session")', INDEX_HTML)
        self.assertNotIn('localStorage.getItem("mobile-terminal.active-profile")', INDEX_HTML)
        self.assertNotIn('query.push("profile="', INDEX_HTML)
        self.assertIn('localStorage.getItem("mobile-terminal.active-profile")', PROXY_PY)
        self.assertIn('"profile=" + encodeURIComponent(profile)', PROXY_PY)
        self.assertIn('"mobile-terminal.profile." + profile + ".active-session"', PROXY_PY)


if __name__ == "__main__":
    unittest.main()
