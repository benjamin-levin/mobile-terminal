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

    def test_proxy_shell_and_message_router_load_passkey_helper(self):
        self.assertIn('<script defer src="/static/passkey.js"></script>', PROXY_PY)
        self.assertIn('const CACHE = "mobile-terminal-v14";', PROXY_PY)
        self.assertIn('const CACHE = "mobile-terminal-proxy-v9";', PROXY_PY)
        self.assertIn("await window.MobileTerminalPasskeys.handleMessage(", APP_JS)
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

    def test_plain_server_loads_passkeys_and_closes_socket_for_passkey_retry(self):
        self.assertIn("Boolean(serverConfig.passkeyAuth)", APP_JS)
        self.assertIn("const passkeyAvailable = loginSupportsPasskey();", APP_JS)
        self.assertIn("setPasskeyRetryUi(true);", APP_JS)
        self.assertIn('authenticationSocket?.close(4000, "passkey retry");', APP_JS)
        self.assertNotIn("passkeyRetryAllowsToken", APP_JS)
        self.assertIn('const CACHE = "mobile-terminal-v14";', SW_JS)

    def test_passkey_ceremony_is_cancelled_and_bound_to_its_socket(self):
        self.assertIn("cancelPasskeyCeremony();", APP_JS)
        self.assertIn("authenticationSocket !== socket", APP_JS)
        self.assertIn("ceremonyController?.signal", APP_JS)
        self.assertIn("async function handleMessage(payload, send, signal)", PASSKEY_JS)
        self.assertGreaterEqual(PASSKEY_JS.count("...(signal ? { signal } : {})"), 2)

    def test_early_websocket_keeps_profile_state_proxy_only(self):
        self.assertIn('localStorage.getItem("mobile-terminal.active-session")', INDEX_HTML)
        self.assertNotIn('localStorage.getItem("mobile-terminal.active-profile")', INDEX_HTML)
        self.assertNotIn('query.push("profile="', INDEX_HTML)
        self.assertIn('localStorage.getItem("mobile-terminal.active-profile")', PROXY_PY)
        self.assertIn('"profile=" + encodeURIComponent(profile)', PROXY_PY)
        self.assertIn('"mobile-terminal.profile." + profile + ".active-session"', PROXY_PY)


if __name__ == "__main__":
    unittest.main()
