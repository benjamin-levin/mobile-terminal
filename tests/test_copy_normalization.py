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

    def test_buffer_selection_preserves_wraps_hard_lines_and_whitespace(self):
        self.run_node(
            [
                "staleVisualContinuationText",
                "extractTerminalSelectionText",
            ],
            r'''
function makeLine(text, { length = text.length, isWrapped = false } = {}) {
  const cells = Array.from(text);
  while (cells.length < length) cells.push(null);
  return {
    isWrapped,
    length,
    translateToString(trimRight = false, start = 0, end = length) {
      const selected = cells.slice(start, Math.min(end, length));
      if (trimRight) {
        while (selected.length && selected[selected.length - 1] === null) selected.pop();
      }
      return selected.map((cell) => cell === null ? " " : cell).join("");
    },
  };
}
function makeBuffer(lines, type = "normal") {
  return { type, getLine(row) { return lines[row]; } };
}
function extract(lines, cols, start, end, type = "normal") {
  return extractTerminalSelectionText(makeBuffer(lines, type), cols, { start, end });
}

assert.equal(extract([
  makeLine("soft "),
  makeLine("wrap", { length: 5, isWrapped: true }),
], 5, { x: 0, y: 0 }, { x: 4, y: 1 }), "soft wrap");

assert.equal(extract([
  makeLine("12345"),
  makeLine("abcde"),
], 5, { x: 0, y: 0 }, { x: 5, y: 1 }), "12345\nabcde");

assert.equal(extract([
  makeLine("first  ", { length: 12 }),
  makeLine("", { length: 12 }),
  makeLine("  indented", { length: 12 }),
  makeLine("last\t value", { length: 12 }),
], 12, { x: 0, y: 0 }, { x: 11, y: 3 }), "first  \n\n  indented\nlast\t value");

assert.equal(extract([
  makeLine(" \t edge  ", { length: 10 }),
], 10, { x: 0, y: 0 }, { x: 9, y: 0 }), " \t edge  ");
''',
        )

    def test_stale_alternate_rows_are_clipped_and_visual_continuations_are_narrowly_joined(self):
        self.run_node(
            [
                "staleVisualContinuationText",
                "extractTerminalSelectionText",
            ],
            r'''
function makeLine(text, { length = text.length, isWrapped = false } = {}) {
  const cells = Array.from(text);
  while (cells.length < length) cells.push(null);
  return {
    isWrapped,
    length,
    translateToString(trimRight = false, start = 0, end = length) {
      const selected = cells.slice(start, Math.min(end, length));
      if (trimRight) {
        while (selected.length && selected[selected.length - 1] === null) selected.pop();
      }
      return selected.map((cell) => cell === null ? " " : cell).join("");
    },
  };
}
function extract(lines, cols, start, end, type = "alternate") {
  const buffer = { type, getLine(row) { return lines[row]; } };
  return extractTerminalSelectionText(buffer, cols, { start, end });
}

assert.equal(extract([
  makeLine("helloSECRET"),
], 5, { x: 0, y: 0 }, { x: 5, y: 0 }), "hello");
assert.equal(extract([
  makeLine("helloSECRET"),
], 5, { x: 1, y: 0 }, { x: 4, y: 0 }), "ell");

const visualRows = [
  makeLine("Tabbed line", { length: 11 }),
  makeLine("  line", { length: 11 }),
];
assert.equal(extract(visualRows, 6, { x: 0, y: 0 }, { x: 6, y: 1 }), "Tabbed line");
assert.equal(extract(visualRows, 6, { x: 2, y: 0 }, { x: 4, y: 1 }), "bbed li");
assert.equal(extract(visualRows, 6, { x: 0, y: 0 }, { x: 2, y: 1 }), "Tabbed ");

const twoSpaceRows = [
  makeLine("Tabbed  line", { length: 12 }),
  makeLine("  line", { length: 12 }),
];
assert.equal(extract(twoSpaceRows, 6, { x: 0, y: 0 }, { x: 6, y: 1 }), "Tabbed  line");
assert.equal(extract(twoSpaceRows, 6, { x: 0, y: 0 }, { x: 3, y: 1 }), "Tabbed  l");

assert.equal(extract([
  makeLine("123456"),
  makeLine("abcdef"),
], 6, { x: 0, y: 0 }, { x: 6, y: 1 }), "123456\nabcdef");

assert.equal(extract([
  makeLine("alpha OTHER", { length: 11 }),
  makeLine("  beta", { length: 11 }),
], 6, { x: 0, y: 0 }, { x: 6, y: 1 }), "alpha \n  beta");
assert.equal(extract(visualRows, 6, { x: 0, y: 0 }, { x: 6, y: 1 }, "normal"),
  "Tabbed\n  line");
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
        self.assertIn("term.getSelectionPosition()", selection)
        self.assertIn("extractTerminalSelectionText(term.buffer.active, term.cols, selection)", selection)
        self.assertIn("extracted === null ? term.getSelection() : extracted", selection)

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
