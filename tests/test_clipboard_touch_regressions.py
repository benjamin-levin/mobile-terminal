"""Clipboard contracts and Unicode edits; real DOM paths also run in tests/browser."""
import asyncio
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests.tmux_harness import TmuxHarness
from tests.test_touch_selection_ui import speech_player_harness

from server import (
    AppServer,
    CommandProvenanceState,
    LEFT_ARROW,
    build_composer_sync_sequence,
    codepoint_cursor_to_utf16,
    utf16_cursor_to_codepoint,
)


APP_JS = (Path(__file__).parents[1] / "static/app.js").read_text()


def function(name):
    match = re.search(rf"^  (?:async )?function {name}\(.*?^  \}}$", APP_JS, re.M | re.S)
    if not match:
        raise AssertionError(f"Missing {name}")
    return match[0]


class ComposerUnicodeTest(unittest.TestCase):
    def test_utf16_cursor_round_trip_and_surrogate_boundary(self):
        text = "😀ab"
        self.assertEqual(utf16_cursor_to_codepoint(text, 4), 3)
        self.assertEqual(utf16_cursor_to_codepoint(text, 2), 1)
        self.assertEqual(utf16_cursor_to_codepoint(text, 1), 0)
        for cursor in range(len(text) + 1):
            self.assertEqual(utf16_cursor_to_codepoint(text, codepoint_cursor_to_utf16(text, cursor)), cursor)
        sequence, cursor = build_composer_sync_sequence(text, 3, text, 1)
        self.assertEqual((sequence, cursor), (LEFT_ARROW * 2, 1))

    def test_grapheme_movement_deletion_and_combining_edit(self):
        text = "界e\u0301👩‍💻ab"
        sequence, cursor = build_composer_sync_sequence(text, len(text), text, 3)
        self.assertEqual((sequence, cursor), (LEFT_ARROW * 3, 3))
        self.assertEqual(build_composer_sync_sequence("e\u0301", 2, "", 0), ("\x7f", 0))
        self.assertEqual(build_composer_sync_sequence("e\u0301", 2, "e\u0300", 2), ("\x7fe\u0300", 2))
        self.assertEqual(build_composer_sync_sequence("e\u0301a", 0, "e\u0301a", 1), ("", 0))

    def test_multiline_preserves_bytes_and_explicit_submission_controls_brackets(self):
        text = "😀  a\n  e\u0301"
        self.assertEqual(build_composer_sync_sequence("", 0, text, len(text)),
                         ("\x1b[200~" + text + "\x1b[201~", len(text)))
        self.assertEqual(build_composer_sync_sequence("", 0, text, len(text), bracketed_paste=False),
                         (text, len(text)))

    def test_wire_cursor_is_utf16_in_both_directions(self):
        async def run():
            app = object.__new__(AppServer)
            app.mobile_composer_states = {}
            app.send_json = mock.AsyncMock()
            state = app.mobile_composer_state("test")
            state.update(draft="😀ab", cursor=3)
            await app.send_composer_state(None, "test")
            self.assertEqual(app.send_json.call_args.args[1]["cursor"], 4)

            app.settle_scroll_history = mock.AsyncMock()
            app.sync_mobile_composer = mock.AsyncMock()
            await app.handle_command(None, None, {"session": "test"}, {
                "type": "composer-sync", "value": "😀ab", "cursor": 2, "revision": 1,
            })
            self.assertEqual(app.sync_mobile_composer.call_args.args[3], 1)
        asyncio.run(run())

    def test_server_rejects_unnegotiated_sync_and_submit_retries_are_deduplicated(self):
        async def run():
            app = object.__new__(AppServer)
            app.mobile_composer_states = {}
            app.command_provenance_states = {"test": CommandProvenanceState()}
            app.send_json = mock.AsyncMock()
            app.settle_scroll_history = mock.AsyncMock()
            bridge = mock.Mock(pane_id="%1")
            bridge.write = mock.AsyncMock()
            app.commit_mobile_composer = mock.AsyncMock()
            payload = {"type": "composer-sync", "session": "test", "value": "one\ntwo",
                       "cursor": 7, "revision": 1, "allowUnbracketedMultiline": True}
            with mock.patch("server.pane_metadata", return_value=(None,)), mock.patch("server.pane_in_mode", return_value=False):
                await app.handle_command(None, bridge, {"session": "test"}, payload)
                bridge.write.assert_not_awaited()
                self.assertEqual(app.send_json.call_args.args[1]["type"], "composer-rejected")
                self.assertEqual(app.mobile_composer_state("test")["draft"], "")

                payload.update(type="composer-enter", requestId="submit-1")
                await app.handle_command(None, bridge, {"session": "test"}, payload)
                bridge.write.assert_awaited_once_with("one\ntwo")
                app.commit_mobile_composer.assert_awaited_once()
                await app.handle_command(None, bridge, {"session": "test"}, payload)
                bridge.write.assert_awaited_once()
                app.commit_mobile_composer.assert_awaited_once()
                acknowledgments = [call.args[1] for call in app.send_json.call_args_list
                                   if call.args[1]["type"] == "composer-submitted"]
                self.assertEqual(len(acknowledgments), 2)
            app.reset_mobile_composer_tracking("test")
            bridge.write.reset_mock()
            payload["requestId"] = "submit-2"
            with mock.patch("server.pane_metadata", return_value=(True,)), mock.patch("server.pane_in_mode", return_value=False):
                await app.handle_command(None, bridge, {"session": "test"}, payload)
                bridge.write.assert_awaited_once_with("\x1b[200~one\ntwo\x1b[201~")
        asyncio.run(run())

    def test_all_composer_actions_reject_a_switched_session(self):
        async def run():
            app = object.__new__(AppServer)
            app.send_json = mock.AsyncMock()
            app.settle_scroll_history = mock.AsyncMock()
            for action in ("composer-enter", "composer-sync", "composer-reset", "composer-force-clear", "composer-history", "composer-semantic-sync"):
                await app.handle_command(None, None, {"session": "new"}, {
                    "type": action, "session": "old", "value": "retained", "cursor": 8,
                })
                response = app.send_json.call_args.args[1]
                self.assertEqual(response["type"], "composer-rejected")
                self.assertEqual(response["session"], "old")
            app.settle_scroll_history.assert_not_awaited()
        asyncio.run(run())

    def test_concurrent_reconnect_retry_cannot_submit_twice(self):
        async def run():
            app = object.__new__(AppServer)
            app.mobile_composer_states = {}
            app.send_json = mock.AsyncMock()
            app.settle_scroll_history = mock.AsyncMock()
            started, release = asyncio.Event(), asyncio.Event()
            async def sync(*args, **kwargs):
                started.set()
                await release.wait()
            app.sync_mobile_composer = mock.AsyncMock(side_effect=sync)
            app.commit_mobile_composer = mock.AsyncMock()
            payload = {"type": "composer-enter", "session": "test", "requestId": "same-request",
                       "value": "one\ntwo", "cursor": 0, "revision": 1}
            first = asyncio.create_task(app.handle_command(None, None, {"session": "test"}, payload))
            await started.wait()
            second = asyncio.create_task(app.handle_command(None, None, {"session": "test"}, payload))
            await asyncio.sleep(0)
            app.sync_mobile_composer.assert_awaited_once()
            release.set()
            await asyncio.gather(first, second)
            app.sync_mobile_composer.assert_awaited_once()
            app.commit_mobile_composer.assert_awaited_once()
            self.assertEqual(app.sync_mobile_composer.call_args.args[3], len(payload["value"]))
        asyncio.run(run())


class ClipboardClientTest(unittest.TestCase):
    def run_node(self, names, script):
        result = subprocess.run(["node", "-e", "\n".join([
            'const assert = require("node:assert/strict");',
            *(function(name) for name in names), script,
        ])], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_speech_autoplay_rejection_is_ready_and_manual_play_is_synchronous(self):
        self.run_node([], speech_player_harness() + r'''
(async () => {
  playResponse = () => Promise.reject(Error("NotAllowedError"));
  await tap();
  assert.equal(terminalSpeechPlayer.status.textContent, "Ready — tap ▶");
  assert.equal(terminalSpeechPlayer.play.disabled, false);
  assert.equal(events.filter(event => event.type === "play" && event.src.startsWith("blob:")).length, 1);
  assert.equal(events.at(-1).inGesture, false);
  assert.equal(dismissals, 1);
  const readyUrl = terminalSpeechUrl;
  playResponse = () => Promise.resolve();
  const manual = clickPlay();
  assert.equal(events.at(-1).type, "play", "manual play must not wait for a microtask");
  assert.equal(events.at(-1).inGesture, true);
  await manual;
  assert.equal(terminalSpeechUrl, readyUrl);
  assert.equal(terminalSpeechPlayer.status.textContent, "Playing");
  clickPlay();
  assert.equal(terminalSpeechAudio.paused, true);
  assert.equal(terminalSpeechPlayer.status.textContent, "Paused");
  playResponse = () => { throw Error("manual play blocked"); };
  await clickPlay();
  assert.equal(terminalSpeechPlayer.status.textContent, "Speech failed: playback blocked.");
  assert.equal(terminalSpeechPlayer.play.disabled, true);
  assert.equal(terminalSpeechAudio.src, "");
  assert.equal(terminalSpeechUrl, null);
  assert.ok(revoked.includes(readyUrl));

  for (const response of [() => Promise.reject(Error("unlock denied")), () => { throw Error("unlock denied"); }]) {
    unlockResponse = response;
    playResponse = () => Promise.resolve();
    const before = events.filter(event => event.type === "play" && event.src.startsWith("blob:")).length;
    await tap();
    assert.equal(terminalSpeechPlayer.status.textContent, "Playing");
    assert.equal(events.filter(event => event.type === "play" && event.src.startsWith("blob:")).length, before + 1,
      "silent unlock failure must not veto a real WAV the browser allows");
    playResponse = () => { throw Error("autoplay denied"); };
    await tap();
    assert.equal(terminalSpeechPlayer.status.textContent, "Ready — tap ▶");
    playResponse = () => Promise.resolve();
    await clickPlay();
    assert.equal(events.at(-1).inGesture, true);
    assert.equal(terminalSpeechPlayer.status.textContent, "Playing");
  }

  let releaseUnlock;
  unlockResponse = () => new Promise(resolve => { releaseUnlock = resolve; });
  await tap(); // A hung unlock cannot strand a synthesized WAV or require another tap.
  assert.equal(terminalSpeechPlayer.status.textContent, "Playing");
  assert.equal(terminalSpeechUnlocked, true);
  const beforeLateUnlock = events.length;
  releaseUnlock();
  await flush();
  assert.equal(events.length, beforeLateUnlock);
  assert.equal(terminalSpeechAudio.paused, false);
  assert.equal(terminalSpeechPlayer.status.textContent, "Playing");
  dismiss();
})().catch(error => { console.error(error); process.exitCode = 1; });
''')

    def test_speech_progress_metadata_seek_and_drag_release(self):
        self.run_node([], speech_player_harness() + r'''
(async () => {
  await tap();
  const { seek, time, status } = terminalSpeechPlayer;
  const audio = terminalSpeechAudio;
  assert.equal(seek.disabled, true);
  assert.equal(time.textContent, "0:00 / --:--");
  for (const duration of [NaN, Infinity, 0]) {
    audio.duration = duration;
    audio.onloadedmetadata();
    assert.equal(seek.disabled, true);
  }
  audio.duration = 125;
  audio.onloadedmetadata();
  assert.equal(seek.disabled, false);
  assert.equal(seek.max, "125");
  audio.currentTime = 32.5;
  audio.ontimeupdate();
  assert.equal(seek.value, "32.5");
  assert.equal(time.textContent, "0:32 / 2:05");
  assert.equal(status.textContent, "Playing");
  seek.value = "64";
  seek.listeners.input();
  assert.equal(audio.currentTime, 64);
  assert.equal(seek.attributes["aria-valuetext"], "1:04 of 2:05");
  for (const [start, end] of [["pointerdown", "pointerup"], ["pointerdown", "pointercancel"],
      ["pointerdown", "lostpointercapture"], ["touchstart", "touchend"], ["touchstart", "touchcancel"], ["pointerdown", "blur"]]) {
    seek.listeners[start]({ pointerId: start === "pointerdown" ? 4 : undefined });
    if (start === "pointerdown") assert.equal(seek.pointerCapture, 4);
    seek.value = "70";
    seek.listeners.input();
    assert.equal(audio.currentTime, 70);
    audio.currentTime = 71;
    audio.ontimeupdate();
    assert.equal(seek.value, "70", `${start} holds thumb position`);
    seek.listeners[end]();
    assert.equal(terminalSpeechSeeking, false, end);
    assert.equal(seek.value, "71", `${end} resumes progress`);
  }
  clickPlay();
  seek.value = "90";
  seek.listeners.input();
  assert.equal(audio.currentTime, 90);
  assert.equal(status.textContent, "Paused");
  await clickPlay();
  assert.equal(audio.currentTime, 90);
  assert.equal(status.textContent, "Playing");
  audio.duration = 130;
  audio.ondurationchange();
  assert.equal(seek.max, "130");
  dismiss();
})().catch(error => { console.error(error); process.exitCode = 1; });
''')

    def test_speech_ended_dismisses_and_new_speech_ignores_old_events(self):
        self.run_node([], speech_player_harness() + r'''
(async () => {
  assert.equal(audioCount, 0);
  assert.equal(document.body.children.length, 0);
  await tap();
  const audio = terminalSpeechAudio;
  audio.duration = 80;
  audio.currentTime = 80;
  const oldEvents = [audio.onended, audio.onerror, audio.onplay, audio.onpause, audio.ontimeupdate];
  const firstUrl = terminalSpeechUrl;
  const generation = terminalSpeechGeneration;
  terminalSpeechSeeking = true;
  audio.ended = true;
  audio.onended();
  assert.equal(terminalSpeechPlayer.player.hidden, true);
  assert.equal(terminalSpeechState, "");
  assert.equal(terminalSpeechGeneration, generation + 1);
  assert.equal(terminalSpeechSeeking, false);
  assert.equal(terminalSpeechUrl, null);
  assert.equal(terminalSpeechBlob, null);
  assert.equal(audio.src, "");
  assert.equal(audio.paused, true);
  assert.equal(audio.onended, null);
  assert.equal(audio.onerror, null);
  assert.equal(terminalSpeechUnlocked, false);
  assert.equal(events.at(-1).type, "load");
  assert.ok(revoked.includes(firstUrl));
  const plays = events.filter(event => event.type === "play").length;
  oldEvents.forEach(callback => callback());
  await flush();
  assert.equal(terminalSpeechPlayer.player.hidden, true);
  assert.equal(events.filter(event => event.type === "play").length, plays);
  assert.equal(requests.length, 1);
  const speaking = tap();
  assert.equal(audio.currentTime, 0);
  await speaking;
  assert.notEqual(terminalSpeechUrl, firstUrl);
  const nextUrl = terminalSpeechUrl;
  oldEvents.forEach(callback => callback());
  // Queued events can also dispatch the newly installed same-element handler.
  assert.equal(audio.ended, false);
  assert.equal(audio.error, null);
  audio.onended();
  audio.onerror();
  assert.equal(terminalSpeechPlayer.status.textContent, "Playing");
  assert.equal(terminalSpeechPlayer.player.hidden, false);
  assert.equal(terminalSpeechUrl, nextUrl);
  assert.equal(requests.length, 2, "Speak requests a new WAV after dismissal");
  const nextEvents = [audio.onended, audio.onerror, audio.onplay, audio.onpause, audio.ontimeupdate];
  dismiss();
  nextEvents.forEach(callback => callback());
  assert.equal(terminalSpeechPlayer.player.hidden, true);
  assert.equal(terminalSpeechUrl, null);
  assert.equal(terminalSpeechBlob, null);
  assert.equal(audio.paused, true);
  assert.equal(audio.src, "");
  assert.ok(revoked.includes(nextUrl));
  assert.equal(new Set(revoked).size, urlCounter);
  assert.equal(revoked.length, urlCounter);
})().catch(error => { console.error(error); process.exitCode = 1; });
''')

    def test_speech_pause_keeps_player_and_audio_for_resume(self):
        self.run_node([], speech_player_harness() + r'''
(async () => {
  await tap();
  const audio = terminalSpeechAudio;
  const url = terminalSpeechUrl, blob = terminalSpeechBlob;
  const generation = terminalSpeechGeneration;
  audio.currentTime = 32;
  audio.pause();
  audio.onpause();
  assert.equal(terminalSpeechPlayer.status.textContent, "Paused");
  assert.equal(terminalSpeechPlayer.player.hidden, false);
  assert.equal(terminalSpeechPlayer.play.disabled, false);
  assert.equal(terminalSpeechGeneration, generation);
  assert.equal(terminalSpeechUrl, url);
  assert.equal(terminalSpeechBlob, blob);
  assert.equal(audio.src, url);
  assert.equal(revoked.includes(url), false);
  await clickPlay();
  assert.equal(terminalSpeechPlayer.status.textContent, "Playing");
  assert.equal(audio.currentTime, 32);
  assert.equal(terminalSpeechUrl, url);
  assert.equal(requests.length, 1);
  dismiss();
})().catch(error => { console.error(error); process.exitCode = 1; });
''')

    def test_speech_rapid_pause_resume_ignores_stale_promises_and_queued_events(self):
        self.run_node([], speech_player_harness() + r'''
(async () => {
  let rejectPlay;
  playResponse = () => new Promise((_resolve, reject) => { rejectPlay = reject; });
  const speaking = tap();
  await flush();
  const audio = terminalSpeechAudio;
  clickPlay();
  assert.equal(terminalSpeechPlayer.status.textContent, "Paused");
  audio.onplay(); // Queued play event arrives after Pause.
  assert.equal(terminalSpeechPlayer.status.textContent, "Paused");
  rejectPlay(Object.assign(Error("paused"), { name: "AbortError" }));
  await speaking;
  assert.equal(terminalSpeechPlayer.status.textContent, "Paused");
  playResponse = () => Promise.resolve();
  const resumed = clickPlay();
  audio.onpause(); // Queued pause arrives after Play.
  assert.equal(terminalSpeechPlayer.status.textContent, "Playing");
  await resumed;
  assert.equal(terminalSpeechPlayer.status.textContent, "Playing");

  clickPlay();
  playResponse = () => new Promise((_resolve, reject) => { rejectPlay = reject; });
  const manual = clickPlay();
  clickPlay();
  rejectPlay(Object.assign(Error("paused"), { name: "AbortError" }));
  await manual;
  assert.equal(terminalSpeechPlayer.status.textContent, "Paused");
  playResponse = () => Promise.reject(Error("manual denied"));
  await clickPlay();
  assert.equal(terminalSpeechPlayer.status.textContent, "Speech failed: playback blocked.");
  dismiss();
})().catch(error => { console.error(error); process.exitCode = 1; });
''')

    def test_word_hit_testing_uses_cells_for_wide_and_combining_characters(self):
        self.run_node(["selectWordAt"], r'''
let cells = [], selected;
const term = { cols: 20, rows: 1, buffer: { active: { viewportY: 0,
  getLine() { return { getCell(column) { return cells[column]; } }; },
}}, select(start, row, length) { selected = { start, row, length }; } };
function cell(chars, width = 1) { return { getChars() { return chars; }, getWidth() { return width; } }; }
function terminalScreenEl() { return { getBoundingClientRect() { return { left: 0, top: 0 }; } }; }
function terminalCellSize() { return { width: 10, height: 20 }; }
function terminalHasSelection() { return true; }
function hapticPulse() {}
function scheduleTerminalSelectionUISync() {}
let suppressNextTerminalClick = false;
for (const prefix of ["界", "😀"]) {
  cells = [cell(prefix, 2), cell("", 0), cell(" "), ...Array.from("hello world", c => cell(c))];
  assert.equal(selectWordAt(45, 5), true);
  assert.deepEqual(selected, { start: 3, row: 0, length: 5 });
  assert.equal(selectWordAt(15, 5), true, "wide continuation selects whole glyph");
  assert.deepEqual(selected, { start: 0, row: 0, length: 2 });
}
cells = [cell("e\u0301"), cell(" "), ...Array.from("hello", c => cell(c))];
assert.equal(selectWordAt(35, 5), true);
assert.deepEqual(selected, { start: 2, row: 0, length: 5 });
assert.equal(selectWordAt(15, 5), false);
''')

    def test_timeout_and_denial_can_retry_copy_without_reselection(self):
        self.run_node(["normalizeTerminalCopyText", "copyTerminalSelection", "copyTerminalSelectionAndDismiss"], r'''
let selectionResult = { error: "Selection timed out." }, clipboardAllowed = false, dismissals = 0;
const toasts = [], writes = [];
function requestSelectionWithFallback() { return Promise.resolve(selectionResult); }
function beginAuthoritativeClipboardWrite() { return null; }
async function copyClipboardTextWithFallback(text) { writes.push(text); return clipboardAllowed; }
function showToast(message) { toasts.push(message); }
function dismissTerminalSelection() { dismissals += 1; }
(async () => {
  await copyTerminalSelectionAndDismiss();
  assert.equal(dismissals, 0);
  selectionResult = { text: "same selection" };
  await copyTerminalSelectionAndDismiss();
  assert.equal(dismissals, 0);
  clipboardAllowed = true;
  await copyTerminalSelectionAndDismiss();
  assert.equal(dismissals, 1);
  assert.deepEqual(writes, ["same selection", "same selection"]);
})().catch(error => { console.error(error); process.exitCode = 1; });
''')

    def test_submit_ack_preserves_later_edits_and_the_other_tabs_draft(self):
        self.run_node(["handleServerMessage", "composerDraftKey"], r'''
const window = {};
const currentUser = "user", activeProfileId = "profile";
let activeSessionName = "a";
const stagedComposerDrafts = new Map();
const updates = [];
function setComposerValue(value) { updates.push(value); }
(async () => {
  const key = composerDraftKey();
  stagedComposerDrafts.set(key, { value: "edited after Enter", submission: { requestId: "one", value: "sent" } });
  await handleServerMessage({ type: "composer-submitted", session: "a", requestId: "one" });
  assert.equal(stagedComposerDrafts.get(key).value, "edited after Enter");
  assert.equal(stagedComposerDrafts.get(key).submission, undefined);
  assert.deepEqual(updates, []);
  stagedComposerDrafts.set(key, { value: "sent", submission: { requestId: "two", value: "sent" } });
  await handleServerMessage({ type: "composer-rejected", session: "a", requestId: "one" });
  assert.equal(stagedComposerDrafts.get(key).submission.requestId, "two", "old rejection cannot cancel newer submission");
  activeSessionName = "b";
  stagedComposerDrafts.set(composerDraftKey(), { value: "other tab" });
  await handleServerMessage({ type: "composer-submitted", session: "a", requestId: "two" });
  assert.equal(stagedComposerDrafts.has(key), false);
  assert.equal(stagedComposerDrafts.get(composerDraftKey()).value, "other tab");
  assert.deepEqual(updates, []);
})().catch(error => { console.error(error); process.exitCode = 1; });
''')

    def test_no_mode_multiline_is_owned_by_tab_until_explicit_send(self):
        self.run_node([
            "composerDraftKey", "syncComposerState", "nextComposerRevision", "setComposerValue",
            "commitComposerLine", "clearComposer", "resetComposerTracking",
        ], r'''
let mobileComposerMode = true, suppressComposerSync = false, composerRevision = 0;
let currentUser = "user", activeProfileId = "profile", activeSessionName = "a";
let selectedSessionName = "a";
const stagedComposerDrafts = new Map();
const term = { modes: { bracketedPasteMode: false } };
const sent = [], toasts = [];
let connected = true, statusHidden = true;
const composerDraftStatus = { classList: {
  remove() { statusHidden = false; }, toggle(_name, hidden) { statusHidden = hidden; },
}};
const composerInput = { value: "😀  a\n  b", selectionEnd: 9, style: {},
  setSelectionRange(start, end) { this.selectionEnd = end; },
};
let reviews = 0;
const window = { requestAnimationFrame(callback) { callback(); }, confirm() { reviews += 1; return true; } };
const crypto = { randomUUID() { return "test-submit-id"; } };
function sendMessage(message) { if (!connected) return false; sent.push(message); return true; }
function showToast(message) { toasts.push(message); }
function autoSizeComposer() {}
function resetSpeechInputState() {}
function openComposer() {}

assert.equal(syncComposerState(), true);
assert.deepEqual(sent, []);
assert.equal(statusHidden, false);
assert.equal(toasts.length, 1);
composerInput.value += "!";
composerInput.selectionEnd = composerInput.value.length;
syncComposerState();
assert.equal(toasts.length, 1, "edits do not repeat prompts");
const draft = composerInput.value;
setComposerValue("old server value", 1);
assert.equal(composerInput.value, draft, "reconnect state cannot overwrite local draft");
activeSessionName = "b";
setComposerValue("😀default cursor");
assert.equal(composerInput.selectionEnd, "😀default cursor".length);
setComposerValue("other", 5);
assert.equal(composerInput.value, "other");
assert.equal(statusHidden, true);
activeSessionName = "a";
setComposerValue("", 0);
assert.equal(composerInput.value, draft);
resetComposerTracking(true);
assert.equal(stagedComposerDrafts.size, 1, "tab switch reset must retain draft");
assert.equal(composerInput.value, draft);
connected = false;
commitComposerLine();
assert.equal(composerInput.value, draft, "offline Enter retains work");
assert.equal(stagedComposerDrafts.size, 1);
connected = true;
commitComposerLine();
assert.deepEqual(sent.map(message => message.type), ["composer-enter"]);
assert.equal(sent[0].value, draft);
assert.equal(sent[0].cursor, draft.length);
assert.equal(sent[0].allowUnbracketedMultiline, true);
assert.equal(stagedComposerDrafts.size, 1, "enqueue is not a server acknowledgment");
assert.equal(composerInput.value, draft);
assert.equal(statusHidden, false);
assert.equal(reviews, 1, "retry reuses the reviewed submit");
assert.equal(sent[0].requestId, "test-submit-id");
commitComposerLine();
assert.equal(sent[1].requestId, sent[0].requestId);
''')

    def test_renaming_a_tab_keeps_its_unsent_draft(self):
        self.run_node(["handleServerMessage", "composerDraftKey"], r'''
const window = {}, currentUser = "user", activeProfileId = "profile";
let activeSessionName = "old", selectedSessionName = "old", activeTabKey = "old";
let currentSessions = [{ name: "old" }];
const stagedComposerDrafts = new Map();
const draft = { value: "one\ntwo", cursor: 7, submission: { requestId: "pending" } };
stagedComposerDrafts.set(composerDraftKey(), draft);
function replaceOpenTabName() {}
function terminalTabKey(name) { return name; }
function persistActiveSession() {}
function closeTabMenu() {}
function syncOpenTabsToSessions() {}
(async () => {
  await handleServerMessage({ type: "session-renamed", oldSession: "old", session: "new" });
  assert.equal(activeSessionName, "new");
  assert.equal(stagedComposerDrafts.get(composerDraftKey()), draft);
  assert.equal(stagedComposerDrafts.has(composerDraftKey("old")), false);
})().catch(error => { console.error(error); process.exitCode = 1; });
''')

    def test_direct_paste_requires_review_without_bracket_mode(self):
        self.run_node(["normalizeTerminalCopyText", "normalizeDirectPtyPasteText", "sendDirectPtyPaste"], r'''
const term = { modes: { bracketedPasteMode: false } };
const sent = [];
let accepted = false, reviews = 0;
const window = { confirm(text) { reviews += 1; assert.ok(text.includes("a  b\n  c")); return accepted; } };
function sendMessage(message) { sent.push(message); return true; }
assert.equal(sendDirectPtyPaste("a  b\n  c"), false);
assert.deepEqual(sent, []);
accepted = true;
assert.equal(sendDirectPtyPaste("a  b\n  c"), true);
assert.equal(sent[0].data, "a  b\n  c");
term.modes.bracketedPasteMode = true;
sendDirectPtyPaste("a  b\n  c");
assert.equal(sent[1].data, "\x1b[200~a  b\n  c\x1b[201~");
assert.equal(reviews, 2);
term.modes.bracketedPasteMode = false;
sendDirectPtyPaste("  printf 'a  b'  ");
assert.equal(sent[2].data, "  printf 'a  b'  ");
''')

    def test_native_paste_capture_precedes_xterm_and_excludes_editor_images(self):
        start = APP_JS.index('  document.addEventListener("paste", (event) => {')
        handler = APP_JS[start:APP_JS.index('  passkeyLoginButton.addEventListener', start)]
        self.run_node(["imageFileFromClipboard"], r'''
let handlePaste, capture, uploads = 0;
const composerInput = {}, terminalTextarea = {}, fileEditor = {};
const document = { addEventListener(_name, handler, options) { handlePaste = handler; capture = options; } };
const sent = [];
let mobileComposerMode = false;
function isTerminalCopyTarget(target) { return target === terminalTextarea; }
function handleImagePaste() { uploads += 1; }
function resetSpeechInputState() {}
function focusTerminal() {}
function sendDirectPtyPaste(text) { sent.push(text); }
function insertComposerText(text) { sent.push(text); }
''' + handler + r'''
assert.equal(capture, true);
function event(target, text = "", image = false) {
  return { target, prevented: false, stopped: false,
    clipboardData: { getData() { return text; }, items: image ? [{ kind: "file", type: "image/png", getAsFile() { return {}; } }] : [] },
    preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; },
  };
}
const terminalPaste = event(terminalTextarea, "a  b\n  c");
handlePaste(terminalPaste);
assert.equal(terminalPaste.prevented, true);
assert.equal(terminalPaste.stopped, true);
assert.deepEqual(sent, ["a  b\n  c"]);
const editorPaste = event(fileEditor, "", true);
handlePaste(editorPaste);
assert.equal(editorPaste.prevented, false);
assert.equal(uploads, 0);
handlePaste(event(composerInput, "", true));
assert.equal(uploads, 1);
''')


class ComposerTerminalSemanticsTest(unittest.IsolatedAsyncioTestCase):
    async def test_unbracketed_newline_submits_a_line_before_final_enter(self):
        harness = TmuxHarness(self, prefix="composer-newline-")
        first = Path(harness.temporary.name) / "first"
        second = Path(harness.temporary.name) / "second"
        harness.start_session("input", "/bin/bash", "--noprofile", "--norc", "-c",
                              'IFS= read -r first; printf "%s" "$first" > "$1"; '
                              'IFS= read -r second; printf "%s" "$second" > "$2"; sleep 1',
                              "test-input", str(first), str(second))
        harness.run("send-keys", "-t", "input", "-l", "one  two\n  three")
        await harness.wait_for(lambda: first.read_text() if first.exists() else None,
                               "one  two", description="first line submitted by LF")
        self.assertFalse(second.exists(), "trailing text remains pending until Enter")
        harness.run("send-keys", "-t", "input", "Enter")
        await harness.wait_for(lambda: second.read_text() if second.exists() else None,
                               "  three", description="second line submitted by Enter")


if __name__ == "__main__":
    unittest.main()
