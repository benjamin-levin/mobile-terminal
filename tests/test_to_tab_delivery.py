from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text()


def extract_function(name):
    match = re.search(
        rf"^  function {re.escape(name)}\([^)]*\) \{{.*?^  \}}$",
        APP_JS,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"function {name} not found")
    return match.group(0)


def app_section(start_marker, end_marker):
    start = APP_JS.index(start_marker)
    return APP_JS[start : APP_JS.index(end_marker, start)]


class ToTabDeliveryTest(unittest.TestCase):
    def run_node(self, assertions):
        script = "\n".join(
            [
                'const assert = require("node:assert/strict");',
                extract_function("normalizeTerminalCopyText"),
                extract_function("normalizeDirectPtyPasteText"),
                extract_function("sendDirectPtyPaste"),
                extract_function("handlePendingPasteReady"),
                extract_function("nextComposerRevision"),
                extract_function("deliverPendingPasteToComposer"),
                r'''
function isBtopSession(name) { return String(name || "").startsWith("btop-"); }
function makeComposer(value = "", cursor = value.length, disabled = false) {
  return {
    value,
    disabled,
    selectionStart: cursor,
    selectionEnd: cursor,
    setRangeText(text, start, end) {
      this.value = this.value.slice(0, start) + text + this.value.slice(end);
      this.selectionStart = start + text.length;
      this.selectionEnd = this.selectionStart;
    },
  };
}
function openComposer() {}
function autoSizeComposer() {}
function resetSpeechInputState() { speechResets += 1; }
function showToast(message) { toasts.push(message); }
function sendMessage(message) { sent.push(message); return queueMessages; }
''',
                assertions,
            ]
        )
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_matching_mobile_ready_waits_for_blank_rev4_state_then_syncs_newer_once(self):
        self.run_node(
            r'''
let activeSessionName = "destination";
let mobileComposerMode = true;
let composerInput = makeComposer("source draft", 12);
let composerRevision = 0;
let pendingPasteAfterSwitch = { session: "destination", text: "selected", ready: false };
let queueMessages = true;
let sent = [];
let toasts = [];
let speechResets = 0;

assert.equal(handlePendingPasteReady(), false);
assert.equal(pendingPasteAfterSwitch.ready, true);
assert.deepEqual(sent, []);
assert.deepEqual(toasts, []);

composerInput = makeComposer("", 0);
assert.equal(deliverPendingPasteToComposer(4), true);
assert.equal(composerInput.value, "selected");
assert.deepEqual(sent, [{
  type: "composer-sync", value: "selected", cursor: 8, revision: 5,
}]);
assert.equal(pendingPasteAfterSwitch, null);
assert.deepEqual(toasts, ["Pasted into this tab."]);

assert.equal(deliverPendingPasteToComposer(5), false);
assert.equal(toasts.length, 1);
'''
        )

    def test_matching_mobile_state_inserts_at_existing_draft_cursor(self):
        self.run_node(
            r'''
let activeSessionName = "destination";
let mobileComposerMode = true;
let composerInput = makeComposer("hello world", 6);
let composerRevision = 2;
let pendingPasteAfterSwitch = { session: "destination", text: "new ", ready: true };
let queueMessages = true;
let sent = [];
let toasts = [];
let speechResets = 0;

assert.equal(deliverPendingPasteToComposer(4), true);
assert.equal(composerInput.value, "hello new world");
assert.equal(composerInput.selectionEnd, 10);
assert.equal(sent[0].cursor, 10);
assert.equal(sent[0].revision, 5);
assert.deepEqual(toasts, ["Pasted into this tab."]);
'''
        )

    def test_matching_direct_pty_ready_uses_single_line_boundary(self):
        self.run_node(
            r'''
let activeSessionName = "destination";
let mobileComposerMode = false;
let composerInput = makeComposer();
let composerRevision = 0;
let pendingPasteAfterSwitch = {
  session: "destination", text: " alpha \n  beta\r\n gamma ", ready: false,
};
let queueMessages = true;
let sent = [];
let toasts = [];
let speechResets = 0;

assert.equal(handlePendingPasteReady(), true);
assert.deepEqual(sent, [{ type: "input", data: "alpha beta gamma" }]);
assert.equal(speechResets, 1);
assert.equal(pendingPasteAfterSwitch, null);
assert.deepEqual(toasts, ["Pasted into this tab."]);
'''
        )

    def test_mismatched_ready_preserves_pending_without_send_or_toast(self):
        self.run_node(
            r'''
let activeSessionName = "wrong";
let mobileComposerMode = false;
let composerInput = makeComposer();
let composerRevision = 0;
let pendingPasteAfterSwitch = { session: "destination", text: "selected", ready: false };
const originalPending = pendingPasteAfterSwitch;
let queueMessages = true;
let sent = [];
let toasts = [];
let speechResets = 0;

assert.equal(handlePendingPasteReady(), false);
assert.equal(pendingPasteAfterSwitch, originalPending);
assert.equal(pendingPasteAfterSwitch.ready, false);
assert.deepEqual(sent, []);
assert.deepEqual(toasts, []);
assert.equal(speechResets, 0);
'''
        )

    def test_btop_and_disabled_composer_do_not_mutate_hidden_draft_or_toast_success(self):
        self.run_node(
            r'''
let activeSessionName = "btop-local";
let mobileComposerMode = true;
let composerInput = makeComposer("hidden draft", 6);
let composerRevision = 0;
let pendingPasteAfterSwitch = { session: "btop-local", text: "selected", ready: false };
let queueMessages = true;
let sent = [];
let toasts = [];
let speechResets = 0;

assert.equal(handlePendingPasteReady(), false);
assert.equal(composerInput.value, "hidden draft");
assert.equal(pendingPasteAfterSwitch, null);
assert.deepEqual(sent, []);
assert.deepEqual(toasts, ["This tab doesn't accept pasted text."]);
assert.doesNotMatch(toasts[0], /^Pasted/);

activeSessionName = "disabled";
composerInput = makeComposer("still hidden", 5, true);
pendingPasteAfterSwitch = { session: "disabled", text: "selected", ready: false };
toasts = [];
assert.equal(handlePendingPasteReady(), false);
assert.equal(composerInput.value, "still hidden");
assert.equal(pendingPasteAfterSwitch, null);
assert.deepEqual(sent, []);
assert.deepEqual(toasts, ["This tab doesn't accept pasted text."]);
'''
        )

    def test_normalized_empty_direct_paste_is_not_sent_or_toasted(self):
        self.run_node(
            r'''
let activeSessionName = "destination";
let mobileComposerMode = false;
let composerInput = makeComposer();
let composerRevision = 0;
let pendingPasteAfterSwitch = { session: "destination", text: " \t\r\n \t ", ready: false };
let queueMessages = true;
let sent = [];
let toasts = [];
let speechResets = 0;

assert.equal(handlePendingPasteReady(), false);
assert.equal(pendingPasteAfterSwitch, null);
assert.deepEqual(sent, []);
assert.deepEqual(toasts, []);
assert.equal(speechResets, 0);
'''
        )

    def test_composer_state_is_applied_before_pending_delivery_and_skips_disabled_tabs(self):
        composer_state = app_section(
            '    if (payload.type === "composer-state") {',
            '    if (payload.type === "sessions") {',
        )
        self.assertIn(
            "if (mobileComposerMode && !isBtopSession(activeSessionName) && !composerInput.disabled)",
            composer_state,
        )
        self.assertLess(
            composer_state.index('setComposerValue(payload.value || "", payload.cursor);'),
            composer_state.index("deliverPendingPasteToComposer(revision);"),
        )


if __name__ == "__main__":
    unittest.main()
