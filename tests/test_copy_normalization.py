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

    def test_copy_and_pending_text_use_fidelity_normalization(self):
        selection = extract_function("terminalSelectionText")
        self.assertIn("return term.getSelection();", selection)
        self.assertNotIn("normalize", selection)

        copy = app_section(
            "  async function copyTerminalSelection()",
            "  // The most recent tab other than the current one.",
        )
        self.assertIn("const text = normalizeTerminalCopyText(raw);", copy)
        self.assertEqual(copy.count('showToast("Copied terminal selection.");'), 2)
        self.assertNotIn("flattened", copy)
        self.assertNotIn("line breaks removed", APP_JS)

        native_copy = app_section(
            '  document.addEventListener("copy", (event) => {',
            "  function handleImagePaste(file)",
        )
        self.assertIn(
            'event.clipboardData.setData("text/plain", normalizeTerminalCopyText(text));',
            native_copy,
        )

        pending = app_section(
            "  async function pasteSelectionToRecentTab()",
            "  // --- Touch text selection",
        )
        self.assertIn(
            "pendingPasteAfterSwitch = { session: target, text: normalizeTerminalCopyText(raw) };",
            pending,
        )

    def test_composer_and_direct_pty_paste_boundaries_are_preserved(self):
        deferred = app_section(
            '      // A "To tab" send switched us here;',
            "      return;\n    }\n    if (payload.type === \"tabs\")",
        )
        self.assertIn("insertComposerText(text, true);", deferred)
        self.assertIn("resetSpeechInputState();\n          sendDirectPtyPaste(text);", deferred)

        clipboard_api = app_section(
            "  async function pasteFromClipboard(",
            '  document.addEventListener("copy", (event) => {',
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
