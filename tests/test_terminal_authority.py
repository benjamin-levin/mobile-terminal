import asyncio
import copy
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from server import (
    AppServer,
    PaneSnapshot,
    TmuxBridge,
    _authored_physical_map,
    _slice_display_cells,
    capture_pane_snapshot,
    extract_authoritative_selection,
)


def snapshot(
    *,
    cols,
    authored_lines,
    plain_physical_rows=None,
    physical_rows=None,
    tab_stops=(),
    rows=None,
):
    plain_physical_rows = list(
        authored_lines if plain_physical_rows is None else plain_physical_rows
    )
    physical_rows = list(plain_physical_rows if physical_rows is None else physical_rows)
    physical_count = len(physical_rows)
    pane_rows = rows or physical_count
    return PaneSnapshot(
        pane_id="%1",
        history=max(0, physical_count - pane_rows),
        seed_history=max(0, physical_count - pane_rows),
        cols=cols,
        rows=pane_rows,
        alternate=False,
        cursor_x=0,
        cursor_y=0,
        cursor_flag=True,
        cursor_blinking=True,
        cursor_shape="default",
        insert=False,
        keypad_cursor=False,
        keypad=False,
        origin=False,
        wrap=True,
        mouse_standard=False,
        mouse_button=False,
        mouse_any=False,
        mouse_sgr=False,
        scroll_upper=0,
        scroll_lower=pane_rows - 1,
        tab_stops=tab_stops,
        physical_rows=physical_rows,
        plain_physical_rows=plain_physical_rows,
        authored_lines=authored_lines,
    )


class RecordingConnection:
    def __init__(self):
        self.bridge = None
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        if not isinstance(message, str) or self.bridge is None:
            return
        payload = json.loads(message)
        if payload["type"] == "seed-start":
            self.bridge.acknowledge({"type": "seed-start-ack", "epoch": payload["epoch"]})
        elif payload["type"] == "seed-data":
            await self.bridge.pane_bytes("%1", b"post-cutoff")
        elif payload["type"] == "seed-end":
            self.bridge.acknowledge({"type": "seed-ack", "epoch": payload["epoch"]})
        elif payload["type"] == "selection-check":
            self.bridge.acknowledge(
                {
                    "type": "selection-check-ack",
                    "requestId": payload["requestId"],
                    "unchanged": True,
                }
            )
        elif payload["type"] == "post-flush":
            self.bridge.acknowledge(
                {
                    "type": "post-flush-ack",
                    "epoch": payload["epoch"],
                    "cycle": payload["cycle"],
                }
            )


class QueuedConnection:
    def __init__(self):
        self.messages = []
        self.payloads = asyncio.Queue()

    async def send(self, message):
        self.messages.append(message)
        if isinstance(message, str):
            await self.payloads.put(json.loads(message))


class AuthoritativeSelectionTest(unittest.TestCase):
    def test_exact_tmux_views_define_soft_rows_and_hard_boundaries(self):
        pane = snapshot(
            cols=5,
            authored_lines=["softwrap", "hard", "", "  end  "],
            plain_physical_rows=["softw", "rap  ", "hard ", "     ", "  end", "     "],
            rows=4,
        )
        self.assertEqual(
            _authored_physical_map(pane),
            [(0, "softw"), (0, "rap"), (1, "hard"), (2, ""), (3, "  end"), (3, "  ")],
        )
        self.assertEqual(extract_authoritative_selection(pane, 0, -2, 3, -1), "softwrap")
        self.assertEqual(extract_authoritative_selection(pane, 0, -2, 4, 0), "softwrap\nhard")
        self.assertEqual(extract_authoritative_selection(pane, 0, 0, 5, 1), "hard\n")
        self.assertEqual(extract_authoritative_selection(pane, 0, 1, 2, 3), "\n  end  ")

    def test_alignment_preserves_blank_trailing_space_and_wide_wrap_padding(self):
        pane = snapshot(
            cols=5,
            authored_lines=["", "trail  ", "abcd界X"],
            plain_physical_rows=["     ", "trail", "     ", "abcd ", "界X  "],
        )
        self.assertEqual(
            _authored_physical_map(pane),
            [(0, ""), (1, "trail"), (1, "  "), (2, "abcd"), (2, "界X")],
        )
        self.assertEqual(extract_authoritative_selection(pane, 0, 0, 5, 2), "\ntrail  ")
        self.assertEqual(extract_authoritative_selection(pane, 4, 3, 1, 4), "界")

    def test_tab_defaults_custom_stops_and_exhausted_stops(self):
        self.assertEqual(_slice_display_cells("\tZ", 0, 8, None, 10), "\t")
        self.assertEqual(_slice_display_cells("\tZ", 2, 5, None, 10), "   ")
        self.assertEqual(_slice_display_cells("a\tb", 1, 4, (4, 8), 10), "\t")
        self.assertEqual(_slice_display_cells("a\tb", 2, 4, (4, 8), 10), "  ")
        self.assertEqual(_slice_display_cells("a\tb", 1, 9, (), 10), "\t")
        self.assertEqual(_slice_display_cells("abcde\tZ", 5, 9, (4,), 10), "\t")
        self.assertEqual(_slice_display_cells("abcde\tZ", 6, 9, (4,), 10), "   ")
        self.assertEqual(_slice_display_cells("abcde\tZ", 9, 10, (4,), 10), "Z")

    def test_safe_graphemes_are_atomic(self):
        self.assertEqual(_slice_display_cells("éx", 0, 1, (), 5), "é")
        self.assertEqual(_slice_display_cells("界X", 0, 1, (), 5), "界")
        self.assertEqual(_slice_display_cells("界X", 1, 2, (), 5), "界")
        self.assertEqual(_slice_display_cells("♥︎X", 0, 1, (), 5), "♥︎")
        self.assertEqual(_slice_display_cells("🇺🇸X", 0, 1, (), 5), "🇺🇸")
        self.assertEqual(_slice_display_cells("🇺🇸X", 1, 2, (), 5), "🇺🇸")

    def test_unverified_graphemes_fail_closed_without_splitting(self):
        unsafe = (
            "😀",
            "♥️",
            "👍🏽",
            "1️⃣",
            "👩‍💻",
            "👨‍👩‍👧‍👦",
            "́",
            "\x07",
            "·",
            "🇺",
            "🇺🇸🇨",
        )
        for value in unsafe:
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "renderer-proven"):
                    _slice_display_cells(value, 0, 20, (), 20)

    def test_unsafe_grapheme_on_an_unselected_row_does_not_disable_safe_copy(self):
        pane = snapshot(
            cols=5,
            authored_lines=["safe", "😀"],
            plain_physical_rows=["safe ", "😀   "],
        )
        self.assertEqual(extract_authoritative_selection(pane, 0, 0, 4, 0), "safe")
        with self.assertRaisesRegex(RuntimeError, "renderer-proven"):
            extract_authoritative_selection(pane, 0, 1, 1, 1)

    def test_mismatched_tmux_views_and_unconsumed_rows_fail_closed(self):
        mismatched_rows = snapshot(
            cols=5,
            authored_lines=["safe"],
            plain_physical_rows=["safe "],
            physical_rows=["safe ", "extra"],
        )
        with self.assertRaisesRegex(RuntimeError, "inconsistent geometry"):
            extract_authoritative_selection(mismatched_rows, 0, 0, 1, 0)

        for pane in (
            snapshot(cols=5, authored_lines=["abc"], plain_physical_rows=["abd  "]),
            snapshot(cols=5, authored_lines=["abc"], plain_physical_rows=["abc  ", "     "]),
            snapshot(cols=5, authored_lines=["abcdef"], plain_physical_rows=["abc  "]),
        ):
            with self.subTest(pane=pane):
                with self.assertRaisesRegex(RuntimeError, "does not map"):
                    _authored_physical_map(pane)

    def test_capture_uses_styled_plain_and_authored_views_and_rejects_row_mismatch(self):
        metadata = (
            "%1",
            0,
            5,
            2,
            0,
            0,
            0,
            1,
            1,
            "default",
            0,
            0,
            0,
            0,
            1,
            0,
            0,
            0,
            0,
            0,
            1,
            (),
        )
        captures = [
            subprocess.CompletedProcess([], 0, "styled-one\nstyled-two\n", ""),
            subprocess.CompletedProcess([], 0, "plain-one\nplain-two\n", ""),
            subprocess.CompletedProcess([], 0, "plain-one\nplain-two\n", ""),
        ]
        with (
            mock.patch("server.pane_metadata", side_effect=[metadata, metadata]),
            mock.patch("server.tmux_capture", side_effect=captures) as tmux_capture,
        ):
            pane = capture_pane_snapshot("session")
        self.assertEqual(pane.physical_rows, ["styled-one", "styled-two"])
        self.assertEqual(pane.plain_physical_rows, ["plain-one", "plain-two"])
        self.assertEqual(tmux_capture.call_args_list[0].args[0:4], ("capture-pane", "-p", "-e", "-N"))
        self.assertEqual(tmux_capture.call_args_list[1].args[0:3], ("capture-pane", "-p", "-N"))
        self.assertEqual(tmux_capture.call_args_list[2].args[0:3], ("capture-pane", "-p", "-J"))

        captures[1] = subprocess.CompletedProcess([], 0, "plain-one\n", "")
        with (
            mock.patch("server.pane_metadata", side_effect=[metadata, metadata]),
            mock.patch("server.tmux_capture", side_effect=captures),
        ):
            with self.assertRaisesRegex(RuntimeError, "inconsistent geometry"):
                capture_pane_snapshot("session")

    def test_unmappable_coordinates_fail_closed(self):
        pane = snapshot(cols=5, authored_lines=["one"], rows=1)
        with self.assertRaises(ValueError):
            extract_authoritative_selection(pane, 0, -1, 1, 0)


class ControlTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_octal_output_is_decoded_and_only_active_pane_is_forwarded(self):
        connection = RecordingConnection()
        bridge = TmuxBridge(connection, "session", "/bin/sh", "/")
        bridge.pane_id = "%1"
        bridge.phase = "forward"
        bridge.epoch_state["epoch"] = 4

        await bridge.control_line(b"%output %2 ignored")
        await bridge.control_line(b"%output %1 A\\040B\\134C\\012")

        self.assertEqual(bridge.offset, 6)
        metadata = json.loads(connection.messages[0])
        self.assertEqual(
            metadata,
            {
                "type": "terminal-output",
                "paneId": "%1",
                "epoch": 4,
                "start": 0,
                "end": 6,
                "kind": "live",
            },
        )
        self.assertEqual(connection.messages[1], b"A B\\C\n")

    async def test_input_uses_exact_ordered_hex_commands_without_subprocesses(self):
        bridge = TmuxBridge(RecordingConnection(), "session", "/bin/sh", "/")
        bridge.pane_id = "%7"
        bridge.command = mock.AsyncMock(return_value=[])
        data = "\x00\r\n\x1b€\udc80\udcff"

        with (
            mock.patch("server.send_pane_bytes") as send_pane_bytes,
            mock.patch("server.subprocess.run") as subprocess_run,
        ):
            await bridge.write(data)

        bridge.command.assert_awaited_once_with(
            "send-keys -t %7 -H 00 0d 0a 1b e2 82 ac 80 ff"
        )
        send_pane_bytes.assert_not_called()
        subprocess_run.assert_not_called()

    async def test_large_concurrent_inputs_remain_whole_and_ordered_across_chunks(self):
        bridge = TmuxBridge(RecordingConnection(), "session", "/bin/sh", "/")
        bridge.pane_id = "%3"
        commands = []
        first_command = asyncio.Event()
        release_first = asyncio.Event()

        async def command(value):
            commands.append(value)
            if len(commands) == 1:
                first_command.set()
                await release_first.wait()
            await asyncio.sleep(0)
            return []

        bridge.command = command
        with mock.patch("server.TMUX_INPUT_CHUNK_BYTES", 2):
            first = asyncio.create_task(bridge.write(b"abcdef"))
            await first_command.wait()
            second = asyncio.create_task(bridge.write(b"XYZ"))
            await asyncio.sleep(0)
            release_first.set()
            await asyncio.gather(first, second)

        self.assertEqual(
            commands,
            [
                "send-keys -t %3 -H 61 62",
                "send-keys -t %3 -H 63 64",
                "send-keys -t %3 -H 65 66",
                "send-keys -t %3 -H 58 59",
                "send-keys -t %3 -H 5a",
            ],
        )

    async def test_input_stops_on_command_error_and_rejects_after_close(self):
        bridge = TmuxBridge(RecordingConnection(), "session", "/bin/sh", "/")
        bridge.pane_id = "%4"
        bridge.command = mock.AsyncMock(side_effect=[[], RuntimeError("tmux rejected input")])

        with mock.patch("server.TMUX_INPUT_CHUNK_BYTES", 2):
            with self.assertRaisesRegex(RuntimeError, "tmux rejected input"):
                await bridge.write(b"abcdef")

        self.assertEqual(bridge.command.await_count, 2)
        await bridge.close()
        with self.assertRaisesRegex(RuntimeError, "tmux control client is closed"):
            await bridge.write(b"later")
        self.assertEqual(bridge.command.await_count, 2)

    async def test_close_waits_for_reseed_to_finish_and_seed_data_omits_authored_lines(self):
        connection = QueuedConnection()
        bridge = TmuxBridge(connection, "session", "/bin/sh", "/")
        bridge.pane_id = "%1"
        pane = snapshot(cols=10, authored_lines=["one", "two"], rows=2)

        with mock.patch("server.capture_pane_snapshot", return_value=pane):
            reseed_task = asyncio.create_task(bridge.reseed("history"))
            seed_start = await connection.payloads.get()
            self.assertEqual(seed_start["type"], "seed-start")
            close_task = asyncio.create_task(bridge.close())
            await asyncio.sleep(0)
            self.assertFalse(close_task.done())

            bridge.acknowledge({"type": "seed-start-ack", "epoch": seed_start["epoch"]})
            seed_data = await connection.payloads.get()
            self.assertEqual(seed_data["type"], "seed-data")
            self.assertNotIn("authoredLines", seed_data)
            seed_end = await connection.payloads.get()
            bridge.acknowledge({"type": "seed-ack", "epoch": seed_end["epoch"]})
            post_flush = await connection.payloads.get()
            bridge.acknowledge(
                {
                    "type": "post-flush-ack",
                    "epoch": post_flush["epoch"],
                    "cycle": post_flush["cycle"],
                }
            )
            seed_open = await connection.payloads.get()
            self.assertEqual(seed_open["type"], "seed-open")
            await reseed_task
            await close_task

        self.assertTrue(bridge.closed)

    async def test_reseed_after_close_emits_no_frames(self):
        connection = RecordingConnection()
        bridge = TmuxBridge(connection, "session", "/bin/sh", "/")
        bridge.pane_id = "%1"

        await bridge.close()
        await bridge.reseed("pane-change", next_pane_id="%2")

        self.assertEqual(connection.messages, [])
        self.assertEqual(bridge.pane_id, "%1")

    async def test_failed_composer_enter_keeps_draft_and_history_state(self):
        app = object.__new__(AppServer)
        app.mobile_composer_states = {}
        state = app.mobile_composer_state("session")
        state.update(
            {
                "draft": "unfinished",
                "cursor": 4,
                "history": ["older"],
                "revision": 8,
                "tracked": True,
                "source": "composer-sync",
            }
        )
        before = copy.deepcopy(state)
        bridge = mock.Mock()
        bridge.write = mock.AsyncMock(side_effect=RuntimeError("tmux closed"))

        with mock.patch("server.pane_in_mode", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "tmux closed"):
                await app.commit_mobile_composer(bridge, "session", revision=9)

        self.assertEqual(state, before)

    async def test_failed_composer_history_write_keeps_pending_draft_state(self):
        app = object.__new__(AppServer)
        app.mobile_composer_states = {}
        app.refresh_mobile_composer_from_terminal = mock.AsyncMock()
        state = app.mobile_composer_state("session")
        state.update(
            {
                "draft": "unfinished",
                "pendingDraft": "previous",
                "history": ["older"],
                "historyIndex": None,
                "tracked": True,
            }
        )
        before = copy.deepcopy(state)
        bridge = mock.Mock()
        bridge.write = mock.AsyncMock(side_effect=RuntimeError("tmux closed"))

        with self.assertRaisesRegex(RuntimeError, "tmux closed"):
            await app.navigate_mobile_composer_history(bridge, "session", "up")

        self.assertEqual(state, before)
        app.refresh_mobile_composer_from_terminal.assert_not_awaited()

    async def test_malformed_input_payloads_are_ignored(self):
        app = object.__new__(AppServer)
        app.settle_scroll_history = mock.AsyncMock()
        app.reset_mobile_composer_tracking = mock.Mock()
        bridge = mock.Mock()
        bridge.write = mock.AsyncMock()

        with mock.patch("server.pane_in_mode", return_value=False):
            for data in ({"unexpected": "object"}, "\ud800"):
                await app.handle_command(
                    mock.AsyncMock(),
                    bridge,
                    {"session": "session"},
                    {"type": "input", "data": data},
                )

        bridge.write.assert_not_awaited()
        app.settle_scroll_history.assert_not_awaited()
        app.reset_mobile_composer_tracking.assert_not_called()

    async def test_reseed_ack_barriers_flush_post_cutoff_output_before_open(self):
        connection = RecordingConnection()
        bridge = TmuxBridge(connection, "session", "/bin/sh", "/")
        connection.bridge = bridge
        bridge.pane_id = "%1"
        pane = snapshot(cols=10, authored_lines=["one", "two"], rows=2)

        with mock.patch("server.capture_pane_snapshot", return_value=pane):
            await bridge.reseed("history", history_lines=5000, scroll_target=-10)

        text_messages = [json.loads(value) for value in connection.messages if isinstance(value, str)]
        types = [value["type"] for value in text_messages]
        self.assertEqual(
            types,
            [
                "seed-start",
                "seed-data",
                "seed-end",
                "terminal-output",
                "post-flush",
                "seed-open",
            ],
        )
        self.assertLess(types.index("terminal-output"), types.index("post-flush"))
        self.assertLess(types.index("post-flush"), types.index("seed-open"))
        self.assertEqual(connection.messages[-3], b"post-cutoff")
        self.assertEqual(bridge.phase, "forward")
        self.assertEqual(bridge.offset, len(b"post-cutoff"))

    async def test_unverified_geometry_returns_the_exact_stale_selection_error(self):
        connection = RecordingConnection()
        bridge = TmuxBridge(connection, "session", "/bin/sh", "/")
        connection.bridge = bridge
        bridge.pane_id = "%1"
        bridge.phase = "forward"
        bridge.quiet = mock.AsyncMock()
        pane = snapshot(
            cols=5,
            authored_lines=["😀"],
            plain_physical_rows=["😀   "],
        )
        payload = {
            "requestId": "request",
            "profile": "",
            "session": "session",
            "paneId": "%1",
            "epoch": 0,
            "revision": 0,
            "cutoff": 0,
            "layoutGeneration": 0,
            "baseY": 0,
            "bufferType": "normal",
            "selection": {"start": {"x": 0, "y": 0}, "end": {"x": 1, "y": 0}},
        }

        with mock.patch("server.capture_pane_snapshot", return_value=pane):
            text, error = await bridge.authoritative_selection(payload)

        self.assertIsNone(text)
        self.assertEqual(error, "Terminal changed; select again.")

    async def test_stale_selection_contract_has_one_exact_error(self):
        bridge = TmuxBridge(RecordingConnection(), "session", "/bin/sh", "/", profile_id="profile")
        bridge.pane_id = "%1"
        text, error = await bridge.authoritative_selection(
            {
                "requestId": "request",
                "profile": "other",
                "session": "session",
                "paneId": "%1",
                "epoch": 0,
                "revision": 0,
                "cutoff": 0,
                "layoutGeneration": 0,
                "baseY": 0,
                "bufferType": "normal",
                "selection": {"start": {"x": 0, "y": 0}, "end": {"x": 1, "y": 0}},
            }
        )
        self.assertIsNone(text)
        self.assertEqual(error, "Terminal changed; select again.")


class ClientProtocolSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parents[1] / "static" / "app.js").read_text()

    def test_socket_frames_and_seed_acks_share_one_ordered_chain(self):
        self.assertIn("socketMessageChain = socketMessageChain", self.source)
        self.assertIn("await handleTerminalBinary(event.data);", self.source)
        self.assertIn('type: "seed-start-ack"', self.source)
        self.assertIn('type: "seed-ack"', self.source)
        self.assertIn('type: "post-flush-ack"', self.source)
        self.assertIn("await writeTerminal(terminalModeSequence(meta));", self.source)
        self.assertLess(
            self.source.index("await writeTerminal(terminalModeSequence(meta));"),
            self.source.index('sendMessage({ type: "seed-ack", epoch: payload.epoch });'),
        )

    def test_seed_establishes_neutral_geometry_and_tabs_before_replaying_rows(self):
        baseline_start = self.source.index("  function terminalReplayBaselineSequence()")
        baseline_end = self.source.index("  function terminalTabStopsSequence(meta)", baseline_start)
        baseline = self.source[baseline_start:baseline_end]
        for sequence in (
            "\\x1b[0m",
            "\\x0f",
            "\\x1b(B",
            "\\x1b[?1l",
            "\\x1b[?6l",
            "\\x1b[?7l",
            "\\x1b[4l",
            "\\x1b[?25l",
            "\\x1b[?1000l",
            "\\x1b[?1002l",
            "\\x1b[?1003l",
            "\\x1b[?1006l",
            "\\x1b>",
            "\\x1b[r",
            "\\x1b[H",
        ):
            self.assertIn(sequence, baseline)

        seed_start = self.source.index("  async function applyTerminalSeed(payload)")
        seed_end = self.source.index("  async function handleTerminalBinary(data)", seed_start)
        seed = self.source[seed_start:seed_end]
        reset = "await writeTerminal(reset);"
        tabs = "await writeTerminal(terminalTabStopsSequence(meta));"
        rows = 'await writeTerminal(payload.physicalRows.join("\\r\\n"));'
        modes = "await writeTerminal(terminalModeSequence(meta));"
        self.assertIn("terminalReplayBaselineSequence()", seed)
        self.assertLess(seed.index(reset), seed.index(tabs))
        self.assertLess(seed.index(tabs), seed.index(rows))
        self.assertLess(seed.index(rows), seed.index(modes))
        self.assertLess(
            self.source.index(modes),
            self.source.index('sendMessage({ type: "seed-ack", epoch: payload.epoch });'),
        )

    def test_normal_history_grows_by_reseed_without_copy_mode_fallback(self):
        history = self.source[
            self.source.index("  function queueScrollHistory(lines)") :
            self.source.index("  function scrollTerminalByPixels", self.source.index("  function queueScrollHistory(lines)"))
        ]
        self.assertIn('type: "history-reseed"', history)
        self.assertNotIn("activePaneLocalScroll = false", history)
        self.assertNotIn("copy-mode", history)

    def test_empty_authoritative_text_is_a_successful_copy_result(self):
        copy_start = self.source.index("  async function copyTerminalSelection()")
        copy_end = self.source.index("  // The most recent tab other than the current one.", copy_start)
        copy = self.source[copy_start:copy_end]
        self.assertIn("selectionPromise = Promise.resolve(requestAuthoritativeSelection());", copy)
        self.assertIn("beginAuthoritativeClipboardWrite(selectionPromise)", copy)
        self.assertIn("result = await selectionPromise;", copy)
        self.assertIn("if (result.error)", copy)
        self.assertIn("normalizeTerminalCopyText(result.text)", copy)
        self.assertNotIn("if (!result.text)", copy)


if __name__ == "__main__":
    unittest.main()
