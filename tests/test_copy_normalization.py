from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text()


def extract_function(name):
    match = re.search(
        rf"^  (?:async )?function {re.escape(name)}\([^)]*\) \{{.*?^  \}}$",
        APP_JS,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"function {name} not found")
    return match.group(0)


def app_section(start_marker, end_marker):
    start = APP_JS.index(start_marker)
    return APP_JS[start : APP_JS.index(end_marker, start)]


class CopyNormalizationTest(unittest.TestCase):
    def run_node(self, functions, assertions):
        script = "\n".join(
            [
                'const assert = require("node:assert/strict");',
                *(extract_function(name) for name in functions),
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

    def test_terminal_copy_normalization_preserves_selection_fidelity(self):
        self.run_node(
            ["normalizeTerminalCopyText"],
            r'''
assert.equal(normalizeTerminalCopyText("soft-wrap-equivalent text is already joined"),
  "soft-wrap-equivalent text is already joined");
assert.equal(normalizeTerminalCopyText("first hard line\n\n  indented line\n\tTabbed\tvalue  with  spaces\n"),
  "first hard line\n\n  indented line\n\tTabbed\tvalue  with  spaces\n");
assert.equal(normalizeTerminalCopyText(" \t edge whitespace  \t "), " \t edge whitespace  \t ");
assert.equal(normalizeTerminalCopyText("crlf\r\nlone-cr\rfinal\r\n"),
  "crlf\nlone-cr\nfinal\n");
''',
        )

    def test_rendered_xterm_cells_are_bounded_alternate_visual_authority_only(self):
        self.assertNotIn("staleVisualContinuationText", APP_JS)
        self.assertNotIn("extractTerminalSelectionText", APP_JS)
        self.assertNotIn("terminalSelectionText", APP_JS)
        self.assertNotIn("term.getSelection()", APP_JS)

        request = extract_function("requestAuthoritativeSelection")
        for field in (
            'profile: activeProfileId || ""',
            "session: activeSessionName",
            "paneId: terminalPaneId",
            "epoch: terminalEpoch",
            "revision: terminalRevision",
            "cutoff: terminalCutoff",
            "layoutGeneration: terminalLayoutGeneration",
            "baseY: buffer.baseY",
            "bufferType: buffer.type",
            "...(clientRows ? { clientRows } : {})",
        ):
            self.assertIn(field, APP_JS)
        self.assertIn('buffer.type !== "alternate"', APP_JS)
        self.assertIn("line.translateToString(false, 0, term.cols)", APP_JS)
        self.assertIn('type: "selection-request"', request)
        self.assertIn("selection: {", APP_JS)

    def test_selection_check_uses_retained_pending_state_without_live_selection_reads(self):
        self.run_node(
            ["pendingSelectionRequestIsCurrent", "handleServerMessage"],
            r'''
let terminalAuthoritative = true;
let terminalEpoch = 42;
const pendingSelectionRequests = new Map();
const sent = [];
function sendMessage(message) { sent.push(message); return true; }
let liveSelectionCalls = 0;
const term = { getSelectionPosition: null };

(async () => {
  const liveSelectionBehaviors = [
    () => ({ start: { x: 9, y: 9 }, end: { x: 10, y: 10 } }),
    () => null,
    () => { throw new Error("live selection must not be read"); },
  ];

  for (const [index, behavior] of liveSelectionBehaviors.entries()) {
    term.getSelectionPosition = () => {
      liveSelectionCalls += 1;
      return behavior();
    };
    const requestId = `retained-${index}`;
    const state = Object.freeze({
      selection: Object.freeze({
        start: Object.freeze({ x: 1, y: 2 }),
        end: Object.freeze({ x: 3, y: 4 }),
      }),
      epoch: 42,
    });
    pendingSelectionRequests.set(requestId, Object.freeze({ state }));
    await handleServerMessage({ type: "selection-check", requestId });
    assert.deepEqual(sent.pop(), {
      type: "selection-check-ack",
      requestId,
      epoch: 42,
      unchanged: true,
    });
  }
  assert.equal(liveSelectionCalls, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
''',
        )

    def test_selection_check_fails_closed_without_current_authoritative_pending_state(self):
        self.run_node(
            ["pendingSelectionRequestIsCurrent", "handleServerMessage"],
            r'''
let terminalAuthoritative = true;
let terminalEpoch = 42;
const pendingSelectionRequests = new Map();
const sent = [];
function sendMessage(message) { sent.push(message); return true; }
const term = {
  getSelectionPosition() { throw new Error("live selection must not be read"); },
};

(async () => {
  await handleServerMessage({ type: "selection-check", requestId: "missing" });
  assert.equal(sent.pop().unchanged, false);

  pendingSelectionRequests.set("not-authoritative", { state: { epoch: 42 } });
  terminalAuthoritative = false;
  await handleServerMessage({ type: "selection-check", requestId: "not-authoritative" });
  assert.equal(sent.pop().unchanged, false);

  pendingSelectionRequests.set("wrong-epoch", { state: { epoch: 41 } });
  terminalAuthoritative = true;
  await handleServerMessage({ type: "selection-check", requestId: "wrong-epoch" });
  assert.equal(sent.pop().unchanged, false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
''',
        )

    def test_selection_check_handler_has_no_live_selection_comparison(self):
        selection_check = app_section(
            '    if (payload.type === "selection-check") {',
            '    if (payload.type === "selection-result") {',
        )
        self.assertIn("unchanged: pendingSelectionRequestIsCurrent(pending),", selection_check)
        for forbidden in (
            "terminalSelectionState",
            "getSelectionPosition",
            "JSON.stringify",
            "sameTerminalSelectionState",
        ):
            self.assertNotIn(forbidden, selection_check)
        self.assertNotIn("sameTerminalSelectionState", APP_JS)

    def test_selection_result_preserves_only_sanitized_authority(self):
        self.run_node(
            ["pendingSelectionRequestIsCurrent", "handleServerMessage"],
            r'''
const pendingSelectionRequests = new Map();
global.window = { clearTimeout() {} };

async function resolveResult(payload) {
  let resolved;
  pendingSelectionRequests.set("request", {
    timer: 1,
    resolve(value) { resolved = value; },
  });
  await handleServerMessage({
    type: "selection-result",
    requestId: "request",
    text: "selected",
    ...payload,
  });
  return resolved;
}

(async () => {
  assert.deepEqual(
    await resolveResult({ authority: "terminal-raw" }),
    { text: "selected", authority: "terminal-raw" },
  );
  assert.deepEqual(
    await resolveResult({ authority: "provider-exact" }),
    { text: "selected", authority: "provider-exact" },
  );
  assert.deepEqual(
    await resolveResult({ authority: "private-provider-reason" }),
    { text: "selected", authority: undefined },
  );
})().catch((error) => { console.error(error); process.exitCode = 1; });
''',
        )

    def test_copy_to_tab_and_native_copy_share_authoritative_request(self):
        copy = app_section(
            "  async function copyTerminalSelection()",
            "  // The most recent tab other than the current one.",
        )
        self.assertIn("selectionPromise = Promise.resolve(requestAuthoritativeSelection());", copy)
        self.assertIn("beginAuthoritativeClipboardWrite(selectionPromise)", copy)
        self.assertIn("result = await selectionPromise;", copy)
        self.assertIn("const text = normalizeTerminalCopyText(result.text);", copy)
        self.assertIn("if (result.error)", copy)
        self.assertNotIn("if (!result.text)", copy)

        pending = app_section(
            "  async function pasteSelectionToRecentTab()",
            "  // --- Touch text selection",
        )
        self.assertIn("const result = await requestAuthoritativeSelection();", pending)
        self.assertIn("text: normalizeTerminalCopyText(result.text),", pending)
        self.assertIn('authority: result.authority === "terminal-raw"', pending)
        self.assertIn("if (result.error)", pending)
        self.assertNotIn("if (!result.text)", pending)

        native_copy = app_section(
            "  function isTerminalCopyTarget(target)",
            "  function handleImagePaste(file)",
        )
        self.assertIn("copyTerminalSelectionAndDismiss().catch", native_copy)
        self.assertIn("event.stopPropagation();", native_copy)
        self.assertIn('document.addEventListener("copy", handleNativeTerminalCopy, true);', native_copy)
        self.assertNotIn("clipboardData.setData", native_copy)
        self.assertNotIn("getSelection", native_copy)

    def test_copy_teardown_runs_for_error_exception_and_clipboard_failure(self):
        self.run_node(
            [
                "normalizeTerminalCopyText",
                "beginAuthoritativeClipboardWrite",
                "copyTextWithFallback",
                "copyClipboardTextWithFallback",
                "copyTerminalSelection",
                "copyTerminalSelectionAndDismiss",
            ],
            r'''
(async () => {
  Object.defineProperty(global, "ClipboardItem", { value: undefined, configurable: true });
  Object.defineProperty(global, "navigator", {
    value: { clipboard: { async writeText() {} } },
    configurable: true,
  });
  global.document = { createElement() { throw new Error("legacy clipboard blocked"); } };
  const toasts = [];
  let dismissals = 0;
  global.showToast = (message) => { toasts.push(message); };
  global.dismissTerminalSelection = () => { dismissals += 1; };

  global.requestAuthoritativeSelection = () => Promise.resolve({ error: "Exact selection failed." });
  await copyTerminalSelectionAndDismiss();
  assert.equal(dismissals, 1);
  assert.deepEqual(toasts, ["Exact selection failed."]);

  global.requestAuthoritativeSelection = () => Promise.resolve({ text: null });
  await assert.rejects(copyTerminalSelectionAndDismiss(), TypeError);
  assert.equal(dismissals, 2);

  navigator.clipboard.writeText = async () => { throw new Error("clipboard denied"); };
  global.requestAuthoritativeSelection = () => Promise.resolve({ text: "selected" });
  await copyTerminalSelectionAndDismiss();
  assert.equal(dismissals, 3);
  assert.equal(toasts.at(-1), "Clipboard copy is blocked by this browser.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
''',
        )

    def test_to_tab_teardown_runs_for_error_and_thrown_exception(self):
        self.run_node(
            [
                "normalizeTerminalCopyText",
                "pasteSelectionToRecentTab",
                "pasteSelectionToRecentTabAndDismiss",
            ],
            r'''
(async () => {
  const toasts = [];
  let dismissals = 0;
  let pendingPasteAfterSwitch = null;
  global.showToast = (message) => { toasts.push(message); };
  global.dismissTerminalSelection = () => { dismissals += 1; };
  global.recentOtherSession = () => "other";
  global.switchSession = () => {};

  global.requestAuthoritativeSelection = () => Promise.resolve({ error: "Selection unavailable." });
  await pasteSelectionToRecentTabAndDismiss();
  assert.equal(dismissals, 1);
  assert.deepEqual(toasts, ["Selection unavailable."]);

  global.requestAuthoritativeSelection = () => { throw new Error("selection request crashed"); };
  await assert.rejects(pasteSelectionToRecentTabAndDismiss(), /selection request crashed/);
  assert.equal(dismissals, 2);
})().catch((error) => { console.error(error); process.exitCode = 1; });
''',
        )

    def test_promised_clipboard_write_starts_before_authority_and_uses_exact_text(self):
        self.run_node(
            ["normalizeTerminalCopyText", "beginAuthoritativeClipboardWrite"],
            r'''
(async () => {
  const writes = [];
  class MockClipboardItem {
    constructor(data) { this.data = data; }
  }
  Object.defineProperty(global, "ClipboardItem", { value: MockClipboardItem, configurable: true });
  Object.defineProperty(global, "navigator", {
    value: { clipboard: { write(items) { writes.push(items); return Promise.resolve(); } } },
    configurable: true,
  });
  let resolveSelection;
  const selectionPromise = new Promise((resolve) => { resolveSelection = resolve; });
  const writePromise = beginAuthoritativeClipboardWrite(selectionPromise);
  assert.equal(writes.length, 1, "clipboard.write must start synchronously");
  assert.ok(writePromise instanceof Promise);
  resolveSelection({ text: "  exact authoritative text\n\t" });
  const blob = await writes[0][0].data["text/plain"];
  assert.equal(blob.type, "text/plain");
  assert.equal(await blob.text(), "  exact authoritative text\n\t");
  await writePromise;
})().catch((error) => { console.error(error); process.exitCode = 1; });
''',
        )

    def test_empty_authoritative_text_succeeds_through_promised_clipboard(self):
        self.run_node(
            [
                "normalizeTerminalCopyText",
                "beginAuthoritativeClipboardWrite",
                "copyTextWithFallback",
                "copyClipboardTextWithFallback",
                "copyTerminalSelection",
            ],
            r'''
(async () => {
  let capturedBlob = null;
  const toasts = [];
  class MockClipboardItem {
    constructor(data) { this.data = data; }
  }
  Object.defineProperty(global, "ClipboardItem", { value: MockClipboardItem, configurable: true });
  Object.defineProperty(global, "navigator", {
    value: { clipboard: {
      async write(items) { capturedBlob = await items[0].data["text/plain"]; },
      async writeText() { throw new Error("must not fall back"); },
    } },
    configurable: true,
  });
  global.requestAuthoritativeSelection = () => Promise.resolve({ text: "" });
  global.showToast = (message) => { toasts.push(message); };
  await copyTerminalSelection();
  assert.ok(capturedBlob instanceof Blob);
  assert.equal(await capturedBlob.text(), "");
  assert.deepEqual(toasts, ["Copied terminal selection."]);
})().catch((error) => { console.error(error); process.exitCode = 1; });
''',
        )

    def test_raw_terminal_copy_has_distinct_non_blocking_warning(self):
        self.run_node(
            [
                "normalizeTerminalCopyText",
                "beginAuthoritativeClipboardWrite",
                "copyTextWithFallback",
                "copyClipboardTextWithFallback",
                "copyTerminalSelection",
            ],
            r'''
(async () => {
  Object.defineProperty(global, "ClipboardItem", { value: undefined, configurable: true });
  Object.defineProperty(global, "navigator", {
    value: { clipboard: { async writeText() {} } },
    configurable: true,
  });
  const toasts = [];
  global.showToast = (message) => { toasts.push(message); };

  global.requestAuthoritativeSelection = () => Promise.resolve({
    text: "raw terminal text",
    authority: "terminal-raw",
  });
  await copyTerminalSelection();
  assert.deepEqual(toasts, [
    "Copied raw terminal text — line breaks and spaces may be included.",
  ]);

  toasts.length = 0;
  global.requestAuthoritativeSelection = () => Promise.resolve({
    text: "provider text",
    authority: "provider-exact",
  });
  await copyTerminalSelection();
  assert.deepEqual(toasts, ["Copied terminal selection."]);

  toasts.length = 0;
  global.requestAuthoritativeSelection = () => Promise.resolve({ text: "ordinary" });
  await copyTerminalSelection();
  assert.deepEqual(toasts, ["Copied terminal selection."]);
})().catch((error) => { console.error(error); process.exitCode = 1; });
''',
        )

    def test_authority_error_wins_over_clipboard_failure_and_fallback_remains(self):
        self.run_node(
            [
                "normalizeTerminalCopyText",
                "beginAuthoritativeClipboardWrite",
                "copyTextWithFallback",
                "copyClipboardTextWithFallback",
                "copyTerminalSelection",
            ],
            r'''
(async () => {
  class MockClipboardItem {
    constructor(data) { this.data = data; }
  }
  Object.defineProperty(global, "ClipboardItem", { value: MockClipboardItem, configurable: true });
  const toasts = [];
  let writeTextCalls = 0;
  Object.defineProperty(global, "navigator", {
    value: { clipboard: {
      write(items) { return items[0].data["text/plain"]; },
      async writeText() { writeTextCalls += 1; },
    } },
    configurable: true,
  });
  global.showToast = (message) => { toasts.push(message); };
  global.requestAuthoritativeSelection = () => Promise.resolve({ error: "Exact server selection error." });
  await copyTerminalSelection();
  assert.deepEqual(toasts, ["Exact server selection error."]);
  assert.equal(writeTextCalls, 0);

  toasts.length = 0;
  navigator.clipboard.write = () => Promise.reject(new Error("permission denied"));
  global.requestAuthoritativeSelection = () => Promise.resolve({ text: "fallback text" });
  await copyTerminalSelection();
  assert.equal(writeTextCalls, 1);
  assert.deepEqual(toasts, ["Copied terminal selection."]);

  toasts.length = 0;
  Object.defineProperty(global, "ClipboardItem", { value: undefined, configurable: true });
  navigator.clipboard.writeText = async () => { throw new Error("permission denied"); };
  let execCalls = 0;
  const textarea = {
    style: {},
    setAttribute() {},
    select() {},
    remove() {},
  };
  global.document = {
    createElement() { return textarea; },
    body: { appendChild() {} },
    execCommand(command) { execCalls += 1; assert.equal(command, "copy"); return true; },
  };
  global.requestAuthoritativeSelection = () => Promise.resolve({ text: "legacy fallback" });
  await copyTerminalSelection();
  assert.equal(execCalls, 1);
  assert.equal(textarea.value, "legacy fallback");
  assert.deepEqual(toasts, ["Copied terminal selection."]);
})().catch((error) => { console.error(error); process.exitCode = 1; });
''',
        )

    def test_native_capture_blocks_xterm_helper_but_preserves_other_copying(self):
        self.run_node(
            ["isTerminalCopyTarget", "handleNativeTerminalCopy"],
            r'''
const helper = { editable: true };
const ordinaryInput = { editable: true };
const terminalElement = {
  contains(target) { return target === helper; },
};
let selected = true;
let copyCalls = 0;
function terminalHasSelection() { return selected; }
function copyTerminalSelectionAndDismiss() { copyCalls += 1; return Promise.resolve(); }
function showToast() {}
function copyEvent(target) {
  return {
    target,
    prevented: 0,
    stopped: 0,
    preventDefault() { this.prevented += 1; },
    stopPropagation() { this.stopped += 1; },
  };
}
const terminalCopy = copyEvent(helper);
handleNativeTerminalCopy(terminalCopy);
assert.equal(terminalCopy.prevented, 1);
assert.equal(terminalCopy.stopped, 1);
assert.equal(copyCalls, 1);

const ordinaryCopy = copyEvent(ordinaryInput);
handleNativeTerminalCopy(ordinaryCopy);
assert.equal(ordinaryCopy.prevented, 0);
assert.equal(ordinaryCopy.stopped, 0);
assert.equal(copyCalls, 1);

selected = false;
const noSelectionCopy = copyEvent(helper);
handleNativeTerminalCopy(noSelectionCopy);
assert.equal(noSelectionCopy.prevented, 0);
assert.equal(noSelectionCopy.stopped, 0);
assert.equal(copyCalls, 1);
''',
        )

    def test_direct_pty_normalization_preserves_legacy_one_line_policy(self):
        self.run_node(
            ["normalizeTerminalCopyText", "normalizeDirectPtyPasteText"],
            r'''
assert.equal(normalizeDirectPtyPasteText("  alpha  beta \t\r\n \t gamma\r\r delta  \t "),
  "alpha beta gamma delta");
assert.equal(normalizeDirectPtyPasteText("one\n  two\t three\n\nfour"),
  "one two\t three four");
assert.equal(normalizeDirectPtyPasteText("\t keep\tinside  spaces \t"),
  "keep\tinside spaces");
assert.equal(normalizeDirectPtyPasteText("soft-wrap-equivalent text"),
  "soft-wrap-equivalent text");
for (const value of ["a\nb", "a\r\nb", "a\rb", "\r\n\r", "  a  \n  b  "]) {
  assert.doesNotMatch(normalizeDirectPtyPasteText(value), /[\r\n]/);
}
''',
        )

    def test_direct_pty_helper_emits_only_normalized_input(self):
        self.run_node(
            [
                "normalizeTerminalCopyText",
                "normalizeDirectPtyPasteText",
                "sendDirectPtyPaste",
            ],
            r'''
const sent = [];
function sendMessage(message) { sent.push(message); }
sendDirectPtyPaste(" \r\n foo  bar\r baz \n ");
assert.deepEqual(sent, [{ type: "input", data: "foo bar baz" }]);
''',
        )
        helper = extract_function("sendDirectPtyPaste")
        self.assertEqual(helper.count("sendMessage("), 1)
        self.assertNotIn("resetSpeechInputState", helper)
        self.assertNotIn("resetComposerTracking", helper)

    def test_composer_and_direct_pty_paste_boundaries_are_preserved(self):
        deferred = app_section(
            '      // A "To tab" send switched us here.',
            "      return;\n    }\n    if (payload.type === \"tabs\")",
        )
        self.assertIn("handlePendingPasteReady();", deferred)
        direct_delivery = extract_function("handlePendingPasteReady")
        self.assertIn("resetSpeechInputState();\n    if (!sendDirectPtyPaste(normalizedText))", direct_delivery)
        composer_delivery = extract_function("deliverPendingPasteToComposer")
        self.assertIn('type: "composer-sync",', composer_delivery)
        self.assertIn("revision: nextComposerRevision(),", composer_delivery)

        clipboard_api = app_section(
            "  async function pasteFromClipboard(",
            "  function isTerminalCopyTarget(target)",
        )
        self.assertIn("insertComposerText(text, true);", clipboard_api)
        self.assertIn("resetComposerTracking(true);\n            sendDirectPtyPaste(text);", clipboard_api)
        self.assertIn("resetSpeechInputState();\n        sendDirectPtyPaste(text);", clipboard_api)
        self.assertNotIn('sendMessage({ type: "input", data: text });', clipboard_api)

        native_paste = app_section(
            '  document.addEventListener("paste", (event) => {',
            '  loginForm.addEventListener("submit", (event) => {',
        )
        self.assertIn("if (event.target === composerInput)", native_paste)
        self.assertIn("insertComposerText(text);", native_paste)
        self.assertIn("resetSpeechInputState();\n    sendDirectPtyPaste(text);", native_paste)
        self.assertEqual(APP_JS.count("sendDirectPtyPaste("), 5)

        ordinary_input = app_section(
            "  term.onData((data) => {",
            "  term.onScroll(() => {",
        )
        self.assertIn('sendMessage({ type: "input", data });', ordinary_input)
        self.assertNotIn("sendDirectPtyPaste", ordinary_input)


if __name__ == "__main__":
    unittest.main()
