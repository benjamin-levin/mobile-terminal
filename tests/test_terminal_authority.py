import asyncio
import copy
import json
import os
import shutil
import subprocess
import threading
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

from server import (
    AcceptedCommand,
    AppServer,
    CommandProvenance,
    CommandProvenanceState,
    PaneRowTracker,
    PaneSnapshot,
    TmuxBridge,
    UnavailableCommandProvenance,
    _absolute_row,
    _authored_physical_map,
    _command_row_digest,
    _slice_display_cells,
    capture_pane_snapshot,
    exact_provenance_selection,
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
    cursor_x=0,
    cursor_y=0,
    history_limit=100000,
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
        cursor_x=cursor_x,
        cursor_y=cursor_y,
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
        history_limit=history_limit,
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
            2000,
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


class CommandProvenanceSelectionTest(unittest.TestCase):
    def setUp(self):
        self.draft = (
            "python3 -c 'print(\"SOFTWRAP|\"+\"0123456789\"*20+\"|END\"); "
            "print(\"HARD-ONE\"); print(); print(\"  INDENTED\"); "
            "print(\"\\tTABBED\\tVALUE  WITH  SPACES\"); print(\"TRAIL  \")'"
        )
        self.pane = snapshot(
            cols=10,
            authored_lines=["$ python3 ", "        -c", "  redraw  "],
            plain_physical_rows=["$ python3 ", "        -c", "  redraw  "],
            rows=3,
            cursor_x=8,
            cursor_y=2,
        )
        self.active = CommandProvenance(
            session_name="session",
            pane_id="%1",
            cols=10,
            rows=3,
            layout_generation=4,
            start_row=0,
            start_x=2,
            draft=self.draft,
            revision=7,
            source="composer-sync",
            owner_id=1,
            end_row=2,
            end_x=8,
            row_digest=_command_row_digest(self.pane, 0, 2),
        )
        self.accepted = AcceptedCommand(
            session_name="session",
            pane_id="%1",
            cols=10,
            rows=3,
            layout_generation=4,
            start_row=0,
            start_x=2,
            end_row=2,
            end_x=8,
            draft=self.draft,
            revision=8,
            source="composer-sync",
            row_digest=_command_row_digest(self.pane, 0, 2),
        )

    def test_active_whole_command_returns_exact_one_line(self):
        matched, text = exact_provenance_selection(
            self.pane,
            "session",
            4,
            (0, 2),
            (2, 8),
            self.active,
            (),
            selected_text="python3 -c 'fake\nhard\nrows'",
        )
        self.assertTrue(matched)
        self.assertEqual(text, self.draft)
        self.assertNotIn("\n", text)

    def test_accepted_historical_whole_command_returns_exact_draft(self):
        exact = self.draft + "\t  "
        record = replace(self.accepted, draft=exact)
        matched, text = exact_provenance_selection(
            self.pane,
            "session",
            4,
            (0, 2),
            (2, 8),
            None,
            (record,),
        )
        self.assertTrue(matched)
        self.assertEqual(text, exact)

    def test_stale_active_candidate_does_not_mask_valid_accepted_record(self):
        stale_active = replace(self.active, layout_generation=3)
        matched, text = exact_provenance_selection(
            self.pane,
            "session",
            4,
            (0, 2),
            (2, 8),
            stale_active,
            (self.accepted,),
            selected_text=self.draft,
        )
        self.assertTrue(matched)
        self.assertEqual(text, self.draft)

    def test_partial_mixed_and_multiple_command_selections_fail_closed(self):
        selections = (
            ((0, 3), (2, 8)),
            ((0, 2), (2, 9)),
            ((0, 0), (2, 8)),
        )
        for start, end in selections:
            with self.subTest(start=start, end=end):
                with self.assertRaises(RuntimeError):
                    exact_provenance_selection(
                        self.pane,
                        "session",
                        4,
                        start,
                        end,
                        None,
                        (self.accepted,),
                    )
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            exact_provenance_selection(
                self.pane,
                "session",
                4,
                (0, 2),
                (2, 8),
                self.active,
                (self.accepted,),
            )

    def test_identity_revision_source_and_digest_mismatches_fail_closed(self):
        stale_records = (
            replace(self.accepted, session_name="other"),
            replace(self.accepted, pane_id="%2"),
            replace(self.accepted, cols=11),
            replace(self.accepted, rows=4),
            replace(self.accepted, layout_generation=5),
            replace(self.accepted, source="terminal-extract"),
            replace(self.accepted, row_digest="0" * 64),
            replace(self.accepted, row_epoch=1),
        )
        for record in stale_records:
            with self.subTest(record=record):
                with self.assertRaises(RuntimeError):
                    exact_provenance_selection(
                        self.pane,
                        "session",
                        4,
                        (0, 2),
                        (2, 8),
                        None,
                        (record,),
                    )

    def test_non_command_output_remains_on_tmux_authority(self):
        ordinary = snapshot(
            cols=5,
            authored_lines=["ordinarywrap"],
            plain_physical_rows=["ordin", "arywr", "ap   "],
            rows=3,
        )
        matched, text = exact_provenance_selection(
            ordinary,
            "session",
            4,
            (0, 0),
            (2, 2),
            None,
            (),
        )
        self.assertFalse(matched)
        self.assertIsNone(text)
        self.assertEqual(extract_authoritative_selection(ordinary, 0, 0, 2, 2), "ordinarywrap")

    def test_records_are_bounded_per_session_and_filtered_by_pane(self):
        state = CommandProvenanceState()
        for index in range(40):
            state.remember(replace(self.accepted, pane_id=f"%{index % 2 + 1}", revision=index))
        self.assertEqual(len(state.accepted_records), 24)
        self.assertTrue(all(record.pane_id == "%1" for record in state.accepted("%1")))


class SaturatedHistoryIdentityTest(unittest.TestCase):
    def test_capped_history_scroll_advances_monotonic_row_origin(self):
        tracker = PaneRowTracker()
        before = snapshot(
            cols=5,
            authored_lines=["a", "b", "c", "d", "e"],
            plain_physical_rows=["a    ", "b    ", "c    ", "d    ", "e    "],
            rows=3,
            history_limit=2,
        )
        after = snapshot(
            cols=5,
            authored_lines=["b", "c", "d", "e", "f"],
            plain_physical_rows=["b    ", "c    ", "d    ", "e    ", "f    "],
            rows=3,
            history_limit=2,
        )

        first = tracker.observe(before, 10)
        second = tracker.observe(after, 20)

        self.assertEqual(before.history, after.history)
        self.assertEqual(second.epoch, first.epoch)
        self.assertEqual(second.first_row, first.first_row + 1)

    def test_repeated_one_row_eviction_fences_instead_of_reusing_origin(self):
        tracker = PaneRowTracker()
        before = snapshot(
            cols=5,
            authored_lines=["a", "a", "a"],
            plain_physical_rows=["a    ", "a    ", "a    "],
            rows=2,
            history_limit=1,
        )
        after = snapshot(
            cols=5,
            authored_lines=["a", "a", "b"],
            plain_physical_rows=["a    ", "a    ", "b    "],
            rows=2,
            history_limit=1,
        )

        first = tracker.observe(before, 10)
        second = tracker.observe(after, 20)

        self.assertGreater(second.epoch, first.epoch)
        self.assertEqual(second.first_row, 0)

    def test_ambiguous_saturated_scroll_fences_stale_records_not_recent_ones(self):
        tracker = PaneRowTracker()
        ambiguous = snapshot(
            cols=5,
            authored_lines=["same", "same", "same"],
            plain_physical_rows=["same ", "same ", "same "],
            rows=2,
            cursor_x=4,
            cursor_y=1,
            history_limit=1,
        )
        old_identity = tracker.observe(ambiguous, 10)
        current_identity = tracker.observe(ambiguous, 20)
        self.assertGreater(current_identity.epoch, old_identity.epoch)

        current = snapshot(
            cols=5,
            authored_lines=["same", "cmd"],
            plain_physical_rows=["same ", "cmd  "],
            rows=2,
            cursor_x=3,
            cursor_y=1,
        )
        current_identity = tracker.observe(current, 20)
        recent = AcceptedCommand(
            session_name="session",
            pane_id="%1",
            cols=5,
            rows=2,
            layout_generation=4,
            start_row=current_identity.first_row + 1,
            start_x=0,
            end_row=current_identity.first_row + 1,
            end_x=3,
            draft="cmd",
            revision=2,
            source="composer-sync",
            row_digest=_command_row_digest(current, 1, 1),
            row_epoch=current_identity.epoch,
        )
        stale = replace(recent, row_epoch=old_identity.epoch)
        stale_digest = replace(recent, row_digest="0" * 64)
        matched, text = exact_provenance_selection(
            current,
            "session",
            4,
            (recent.start_row, 0),
            (recent.end_row, 3),
            None,
            (stale, stale_digest, recent),
            current_identity,
            "cmd",
        )
        self.assertTrue(matched)
        self.assertEqual(text, "cmd")


class ComposerProvenanceLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_direct_sync_tracks_only_monotonic_end_cursor_revisions(self):
        app = object.__new__(AppServer)
        app.mobile_composer_states = {}
        app.command_provenance_states = {}
        bridge = mock.Mock()
        bridge.pane_id = "%1"
        bridge.write = mock.AsyncMock()

        async def start(_data, draft, revision, _generation):
            active = CommandProvenance(
                session_name="session",
                pane_id="%1",
                cols=20,
                rows=6,
                layout_generation=0,
                start_row=3,
                start_x=2,
                draft=draft,
                revision=revision,
                source="composer-sync",
                owner_id=id(bridge),
            )
            app.command_provenance_state("session").active = active
            return active

        async def continue_command(_data, active, draft, revision, _generation):
            updated = replace(active, draft=draft, revision=revision)
            app.command_provenance_state("session").active = updated
            return updated

        bridge.write_with_command_start = mock.AsyncMock(side_effect=start)
        bridge.write_command_continuation = mock.AsyncMock(side_effect=continue_command)

        with mock.patch("server.pane_in_mode", return_value=False):
            await app.sync_mobile_composer(bridge, "session", "exact", 5, revision=1)
            active = app.command_provenance_state("session").active
            self.assertEqual(active.draft, "exact")
            self.assertEqual(active.revision, 1)

            await app.sync_mobile_composer(bridge, "session", "exact!", 6, revision=1)
            self.assertIsNone(app.command_provenance_state("session").active)

            app.reset_mobile_composer_tracking("session", allow_provenance_start=True)
            await app.sync_mobile_composer(bridge, "session", "cursor", 2, revision=2)
            self.assertIsNone(app.command_provenance_state("session").active)

    async def test_other_bridge_noop_duplicate_preserves_unavailable_taint(self):
        app = object.__new__(AppServer)
        app.mobile_composer_states = {}
        app.command_provenance_states = {"session": CommandProvenanceState()}
        app.scroll_states = {}
        provenance = app.command_provenance_state("session")
        write_lock = asyncio.Lock()
        connection_a = RecordingConnection()
        connection_b = RecordingConnection()
        bridge_a = TmuxBridge(
            connection_a,
            "session",
            "/bin/sh",
            "/",
            provenance_state=provenance,
            write_lock=write_lock,
        )
        bridge_b = TmuxBridge(
            connection_b,
            "session",
            "/bin/sh",
            "/",
            provenance_state=provenance,
            write_lock=write_lock,
        )
        connection_a.bridge = bridge_a
        connection_b.bridge = bridge_b
        bridge_a.pane_id = bridge_b.pane_id = "%1"
        bridge_a.quiet = mock.AsyncMock()
        bridge_a.command = mock.AsyncMock(return_value=[])
        bridge_b.command = mock.AsyncMock(return_value=[])
        exact = "printf '%s' tainted"

        with (
            mock.patch("server.pane_in_mode", return_value=False),
            mock.patch(
                "server.capture_pane_snapshot",
                side_effect=RuntimeError("forced provenance capture failure"),
            ),
        ):
            await app.handle_command(
                connection_a,
                bridge_a,
                {"session": "session"},
                {
                    "type": "composer-sync",
                    "value": exact,
                    "cursor": len(exact),
                    "revision": 1,
                },
            )

        taint = provenance.unavailable
        self.assertIsNotNone(taint)
        self.assertIsNone(provenance.active)
        bridge_a.command.assert_awaited_once_with(
            f"send-keys -t %1 -H {exact.encode().hex(' ')}"
        )

        with mock.patch("server.pane_in_mode", return_value=False):
            await app.handle_command(
                connection_b,
                bridge_b,
                {"session": "session"},
                {
                    "type": "composer-sync",
                    "value": exact,
                    "cursor": len(exact),
                    "revision": 2,
                },
            )

        bridge_b.command.assert_not_awaited()
        self.assertIs(provenance.unavailable, taint)
        self.assertIsNone(provenance.active)
        text, error = await bridge_b.authoritative_selection(
            {
                "requestId": "tainted-noop",
                "profile": "",
                "session": "session",
                "paneId": "%1",
                "epoch": 0,
                "revision": 0,
                "cutoff": 0,
                "layoutGeneration": 0,
                "cols": 20,
                "rows": 6,
                "baseY": 0,
                "bufferType": "normal",
                "selection": {
                    "start": {"x": 0, "y": 0},
                    "end": {"x": 1, "y": 0},
                },
            }
        )
        self.assertIsNone(text)
        self.assertEqual(error, "Terminal changed; select again.")

    async def test_semantic_sync_generic_input_and_layout_change_invalidate_active(self):
        app = object.__new__(AppServer)
        app.mobile_composer_states = {}
        app.command_provenance_states = {}
        app.scroll_states = {}
        app.settle_scroll_history = mock.AsyncMock()
        connection = mock.AsyncMock()
        bridge = mock.Mock()
        bridge.pane_id = "%1"
        bridge.write = mock.AsyncMock()

        async def start(_data, draft, revision, _generation):
            active = self._active_for_layout(0)
            active = replace(active, draft=draft, revision=revision, owner_id=id(bridge))
            app.command_provenance_state("session").active = active
            return active

        bridge.write_with_command_start = mock.AsyncMock(side_effect=start)
        bridge.write_command_continuation = mock.AsyncMock()

        with mock.patch("server.pane_in_mode", return_value=False):
            await app.sync_mobile_composer(bridge, "session", "exact", 5, revision=1)
            await app.handle_command(
                connection,
                bridge,
                {"session": "session"},
                {
                    "type": "composer-semantic-sync",
                    "value": "rendered",
                    "cursor": 8,
                    "revision": 2,
                    "source": "terminal-extract",
                },
            )
            self.assertIsNone(app.command_provenance_state("session").active)
            self.assertEqual(app.mobile_composer_state("session")["source"], "terminal-extract")

            app.reset_mobile_composer_tracking("session", allow_provenance_start=True)
            await app.sync_mobile_composer(bridge, "session", "again", 5, revision=3)
            await app.handle_command(
                connection,
                bridge,
                {"session": "session"},
                {"type": "input", "data": "x", "revision": 4},
            )
            self.assertIsNone(app.command_provenance_state("session").active)

        state = app.command_provenance_state("session")
        state.active = self._active_for_layout(state.layout_generation)
        state.invalidate_layout()
        self.assertIsNone(state.active)

    @staticmethod
    def _active_for_layout(layout_generation):
        return CommandProvenance(
            session_name="session",
            pane_id="%1",
            cols=20,
            rows=6,
            layout_generation=layout_generation,
            start_row=0,
            start_x=0,
            draft="x",
            revision=1,
            source="composer-sync",
            owner_id=1,
        )


class LiveComposerHandlerIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not shutil.which("tmux") or not shutil.which("zsh") or not shutil.which("script"):
            self.skipTest("tmux, zsh, and script are required")
        self.session_name = f"mt-provenance-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-x",
                "36",
                "-y",
                "8",
                "-s",
                self.session_name,
                "env PS1='$ ' zsh -f",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.ordinary_client = subprocess.Popen(
            [
                "script",
                "-qfec",
                f"exec tmux attach-session -t {self.session_name}",
                "/dev/null",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await self._wait_for_ordinary_client()

        self.app = object.__new__(AppServer)
        self.app.mobile_composer_states = {}
        self.app.command_provenance_states = {
            self.session_name: CommandProvenanceState()
        }
        self.app.terminal_write_locks = {self.session_name: asyncio.Lock()}
        self.app.scroll_states = {}
        self.connection = RecordingConnection()
        self.bridge = TmuxBridge(
            self.connection,
            self.session_name,
            "/bin/zsh",
            "/",
            create_if_missing=False,
            initial_size=(36, 8),
            provenance_state=self.app.command_provenance_state(self.session_name),
            write_lock=self.app.terminal_write_lock(self.session_name),
        )
        self.connection.bridge = self.bridge
        await self.bridge.open()
        self.bridge.phase = "forward"
        await self.bridge.quiet(0.08)
        self.state = {"session": self.session_name, "user": ""}

    async def asyncTearDown(self):
        bridge = getattr(self, "bridge", None)
        if bridge is not None:
            await bridge.close()
        ordinary_client = getattr(self, "ordinary_client", None)
        if ordinary_client is not None and ordinary_client.poll() is None:
            ordinary_client.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(ordinary_client.wait), timeout=1)
            except asyncio.TimeoutError:
                ordinary_client.kill()
                await asyncio.to_thread(ordinary_client.wait)
        session_name = getattr(self, "session_name", "")
        if session_name:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    async def _wait_for_ordinary_client(self):
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            result = subprocess.run(
                [
                    "tmux",
                    "list-clients",
                    "-t",
                    self.session_name,
                    "-F",
                    "#{client_control_mode}",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if "0" in result.stdout.splitlines():
                return
            await asyncio.sleep(0.03)
        self.fail("ordinary tmux client did not attach")

    def _assert_ordinary_client_remains_attached(self):
        result = subprocess.run(
            [
                "tmux",
                "list-clients",
                "-t",
                self.session_name,
                "-F",
                "#{client_control_mode}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("0", result.stdout.splitlines())

    async def _send_native_paste_syncs(self, value):
        for revision in (1, 2):
            await self.app.handle_command(
                self.connection,
                self.bridge,
                self.state,
                {
                    "type": "composer-sync",
                    "value": value,
                    "cursor": len(value),
                    "revision": revision,
                },
            )

    async def _selection_payload(self, active=None):
        if active is None:
            start_x = end_x = start_y = end_y = 0
            cols, rows, base_y = 36, 8, 0
        else:
            pane = None
            for attempt in range(10):
                try:
                    pane = capture_pane_snapshot(
                        self.session_name,
                        self.bridge.pane_id,
                    )
                    break
                except RuntimeError:
                    if attempt == 9:
                        raise
                    await asyncio.sleep(0.03)
            identity = self.bridge.provenance_state.row_tracker.observe(
                pane,
                self.bridge.offset,
            )
            start_absolute_row = _absolute_row(pane, identity, active.start_row)
            end_absolute_row = _absolute_row(pane, identity, active.end_row)
            start_x = active.start_x
            end_x = active.end_x
            start_y = pane.seed_history + start_absolute_row - pane.history
            end_y = pane.seed_history + end_absolute_row - pane.history
            cols, rows, base_y = pane.cols, pane.rows, pane.seed_history
        return {
            "type": "selection-request",
            "requestId": uuid.uuid4().hex,
            "profile": "",
            "session": self.session_name,
            "paneId": self.bridge.pane_id,
            "epoch": self.bridge.epoch_state["epoch"],
            "revision": self.bridge.offset,
            "cutoff": self.bridge.cutoff,
            "layoutGeneration": self.bridge.epoch_state["layout"],
            "cols": cols,
            "rows": rows,
            "baseY": base_y,
            "bufferType": "normal",
            "selection": {
                "start": {"x": start_x, "y": start_y},
                "end": {"x": end_x, "y": end_y},
            },
        }

    def _last_selection_result(self):
        payloads = [
            json.loads(message)
            for message in self.connection.messages
            if isinstance(message, str)
        ]
        return next(
            payload
            for payload in reversed(payloads)
            if payload.get("type") == "selection-result"
        )

    async def test_native_paste_uses_exact_source_with_stable_ordinary_client(self):
        exact = "printf '%s' 'ONE-LINE-" + ("0123456789" * 8) + "'"
        await self._send_native_paste_syncs(exact)
        await self.bridge.settle_active_provenance_fence()
        active = self.bridge.provenance_state.active

        self.assertIsNotNone(active)
        self.assertEqual(active.draft, exact)
        self.assertEqual(active.revision, 2)
        self._assert_ordinary_client_remains_attached()
        await self.app.handle_command(
            self.connection,
            self.bridge,
            self.state,
            await self._selection_payload(active),
        )

        result = self._last_selection_result()
        self.assertEqual(result.get("text"), exact)
        self.assertNotIn("error", result)

    async def test_failed_start_write_stays_tainted_across_duplicate_sync(self):
        exact = "printf '%s' 'TAINTED-" + ("abcdefghij" * 6) + "'"
        with mock.patch(
            "server.capture_pane_snapshot",
            side_effect=RuntimeError("forced pre-write capture failure"),
        ):
            await self.app.handle_command(
                self.connection,
                self.bridge,
                self.state,
                {
                    "type": "composer-sync",
                    "value": exact,
                    "cursor": len(exact),
                    "revision": 1,
                },
            )
        await self.app.handle_command(
            self.connection,
            self.bridge,
            self.state,
            {
                "type": "composer-sync",
                "value": exact,
                "cursor": len(exact),
                "revision": 2,
            },
        )

        rendered = subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-t", self.bridge.pane_id],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("TAINTED-", rendered)
        self.assertIsNone(self.bridge.provenance_state.active)
        self.assertEqual(self.bridge.provenance_state.unavailable.draft, exact)
        self.assertEqual(self.bridge.provenance_state.unavailable.revision, 2)
        self._assert_ordinary_client_remains_attached()
        await self.app.handle_command(
            self.connection,
            self.bridge,
            self.state,
            await self._selection_payload(),
        )

        result = self._last_selection_result()
        self.assertEqual(result.get("error"), "Terminal changed; select again.")
        self.assertNotIn("text", result)


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

    async def test_failed_open_terminates_process_cancels_reader_and_drops_owner(self):
        class Process:
            pid = 321
            stdin = None

            def __init__(self):
                self.stdout = mock.Mock()
                self.stdout.fileno.return_value = 9
                self.alive = True
                self.terminated = False
                self.waited = False

            def poll(self):
                return None if self.alive else 0

            def terminate(self):
                self.terminated = True
                self.alive = False

            def wait(self):
                self.waited = True
                return 0

            def kill(self):
                self.alive = False

        process = Process()
        bridge = TmuxBridge(
            RecordingConnection(),
            "session",
            "/bin/sh",
            "/",
            create_if_missing=False,
        )
        bridge.initial_block_seen.set()
        reader_started = asyncio.Event()

        async def read_forever():
            reader_started.set()
            await asyncio.Event().wait()

        bridge.read_loop = read_forever
        with (
            mock.patch("server.subprocess.Popen", return_value=process),
            mock.patch("server.os.set_blocking"),
            mock.patch("server.pane_metadata", side_effect=RuntimeError("open failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "open failed"):
                await bridge.open()

        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)
        self.assertTrue(bridge.closed)
        self.assertNotIn(process.pid, bridge.provenance_state.owner_pids)
        self.assertTrue(bridge.read_task.done())

    async def test_closing_process_remains_owned_until_wait_completes(self):
        class Process:
            pid = 654

            def __init__(self):
                self.alive = True
                self.wait_started = threading.Event()
                self.release_wait = threading.Event()

            def poll(self):
                return None if self.alive else 0

            def terminate(self):
                pass

            def wait(self):
                self.wait_started.set()
                self.release_wait.wait()
                self.alive = False
                return 0

            def kill(self):
                self.release_wait.set()

        process = Process()
        provenance = CommandProvenanceState(owner_pids={process.pid})
        bridge = TmuxBridge(
            RecordingConnection(),
            "session",
            "/bin/sh",
            "/",
            provenance_state=provenance,
        )
        bridge.process = process
        closing = asyncio.create_task(bridge.close())
        try:
            started = await asyncio.wait_for(
                asyncio.to_thread(process.wait_started.wait),
                timeout=1,
            )
            self.assertTrue(started)
            self.assertIn(process.pid, provenance.owner_pids)
        finally:
            process.release_wait.set()
        await closing

        self.assertNotIn(process.pid, provenance.owner_pids)

    async def test_concurrent_invalidation_cannot_reestablish_provenance_after_write(self):
        provenance = CommandProvenanceState()
        bridge = TmuxBridge(
            RecordingConnection(),
            "session",
            "/bin/sh",
            "/",
            provenance_state=provenance,
        )
        bridge.pane_id = "%1"
        active = CommandProvenance(
            session_name="session",
            pane_id="%1",
            cols=20,
            rows=6,
            layout_generation=0,
            start_row=0,
            start_x=2,
            draft="old",
            revision=1,
            source="composer-sync",
            owner_id=id(bridge),
        )
        provenance.active = active
        command_started = asyncio.Event()
        release_command = asyncio.Event()

        async def command(_value):
            command_started.set()
            await release_command.wait()
            return []

        bridge.command = command
        update = asyncio.create_task(
            bridge.write_command_continuation(
                "x",
                active,
                "oldx",
                2,
                provenance.tracking_generation,
            )
        )
        await command_started.wait()
        provenance.invalidate_active()
        release_command.set()

        self.assertIsNone(await update)
        self.assertIsNone(provenance.active)

    async def test_unknown_tmux_client_attachment_invalidates_active_provenance(self):
        provenance = CommandProvenanceState()
        provenance.owner_pids.add(123)
        bridge = TmuxBridge(
            RecordingConnection(),
            "session",
            "/bin/sh",
            "/",
            provenance_state=provenance,
        )
        active = CommandProvenance(
            session_name="session",
            pane_id="%1",
            cols=20,
            rows=6,
            layout_generation=0,
            start_row=0,
            start_x=2,
            draft="exact",
            revision=1,
            source="composer-sync",
            owner_id=id(bridge),
        )
        provenance.active = active

        await bridge.control_line(b"%client-session-changed client-123 $1 session")
        self.assertIs(provenance.active, active)

        await bridge.control_line(b"%client-session-changed client-456 $1 session")
        self.assertIsNone(provenance.active)

    async def test_background_fence_binds_active_to_settled_terminal_revision(self):
        bridge = TmuxBridge(RecordingConnection(), "session", "/bin/sh", "/")
        bridge.pane_id = "%1"
        bridge.offset = 7
        bridge.quiet = mock.AsyncMock()
        pane = snapshot(
            cols=10,
            authored_lines=["$ exact"],
            plain_physical_rows=["$ exact   "],
            rows=1,
            cursor_x=7,
            cursor_y=0,
        )
        active = CommandProvenance(
            session_name="session",
            pane_id="%1",
            cols=10,
            rows=1,
            layout_generation=0,
            start_row=0,
            start_x=2,
            draft="exact",
            revision=1,
            source="composer-sync",
            owner_id=id(bridge),
        )
        bridge.provenance_state.active = active

        with (
            mock.patch("server.asyncio.sleep", new=mock.AsyncMock()),
            mock.patch("server.capture_pane_snapshot", return_value=pane),
        ):
            await bridge._capture_active_provenance_fence(active)

        settled = bridge.provenance_state.active
        self.assertEqual(settled.terminal_revision, 7)
        self.assertEqual((settled.end_row, settled.end_x), (0, 7))

    async def test_accepted_enter_captures_immutable_rows_before_sending_enter(self):
        bridge = TmuxBridge(RecordingConnection(), "session", "/bin/sh", "/")
        bridge.pane_id = "%1"
        bridge.quiet = mock.AsyncMock()
        bridge.command = mock.AsyncMock(return_value=[])
        pane = snapshot(
            cols=10,
            authored_lines=["$ exact command"],
            plain_physical_rows=["$ exact co", "mmand     "],
            rows=2,
            cursor_x=5,
            cursor_y=1,
        )
        active = CommandProvenance(
            session_name="session",
            pane_id="%1",
            cols=10,
            rows=2,
            layout_generation=0,
            start_row=0,
            start_x=2,
            draft="exact command",
            revision=4,
            source="composer-sync",
            owner_id=id(bridge),
            end_row=1,
            end_x=5,
            row_digest=_command_row_digest(pane, 0, 1),
            terminal_revision=0,
        )
        bridge.provenance_state.active = active
        metadata = (
            "%1",
            0,
            10,
            2,
            0,
            7,
            1,
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
            2000,
        )

        with (
            mock.patch("server.pane_metadata", return_value=metadata),
            mock.patch("server.capture_pane_snapshot", return_value=pane),
        ):
            record = await bridge.write_accepted_enter(active, 5, 0)

        self.assertEqual(record.draft, "exact command")
        self.assertEqual((record.start_row, record.start_x), (0, 2))
        self.assertEqual((record.end_row, record.end_x), (1, 5))
        self.assertEqual(record.row_digest, _command_row_digest(pane, 0, 1))
        bridge.command.assert_awaited_once_with("send-keys -t %1 -H 0d")

    async def test_out_of_band_input_before_enter_does_not_create_accepted_override(self):
        bridge = TmuxBridge(RecordingConnection(), "session", "/bin/sh", "/")
        bridge.pane_id = "%1"
        bridge.quiet = mock.AsyncMock()
        bridge.command = mock.AsyncMock(return_value=[])
        original = snapshot(
            cols=10,
            authored_lines=["$ exact command"],
            plain_physical_rows=["$ exact co", "mmand     "],
            rows=2,
            cursor_x=5,
            cursor_y=1,
        )
        pane = snapshot(
            cols=10,
            authored_lines=["$ exactXcommand"],
            plain_physical_rows=["$ exactXco", "mmand     "],
            rows=2,
            cursor_x=5,
            cursor_y=1,
        )
        active = CommandProvenance(
            session_name="session",
            pane_id="%1",
            cols=10,
            rows=2,
            layout_generation=0,
            start_row=0,
            start_x=2,
            draft="exact command",
            revision=4,
            source="composer-sync",
            owner_id=id(bridge),
            end_row=1,
            end_x=5,
            row_digest=_command_row_digest(original, 0, 1),
        )
        bridge.provenance_state.active = active

        with mock.patch("server.capture_pane_snapshot", return_value=pane):
            record = await bridge.write_accepted_enter(active, 5, 0)

        self.assertIsNone(record)
        self.assertEqual(tuple(bridge.provenance_state.accepted_records), ())
        bridge.command.assert_awaited_once_with("send-keys -t %1 -H 0d")

    async def test_stable_ordinary_tmux_client_does_not_disable_accepted_override(self):
        bridge = TmuxBridge(RecordingConnection(), "session", "/bin/sh", "/")
        bridge.pane_id = "%1"
        bridge.quiet = mock.AsyncMock()
        bridge.command = mock.AsyncMock(return_value=[])
        pane = snapshot(
            cols=10,
            authored_lines=["$ exact command"],
            plain_physical_rows=["$ exact co", "mmand     "],
            rows=2,
            cursor_x=5,
            cursor_y=1,
        )
        active = CommandProvenance(
            session_name="session",
            pane_id="%1",
            cols=10,
            rows=2,
            layout_generation=0,
            start_row=0,
            start_x=2,
            draft="exact command",
            revision=4,
            source="composer-sync",
            owner_id=id(bridge),
            end_row=1,
            end_x=5,
            row_digest=_command_row_digest(pane, 0, 1),
            terminal_revision=0,
        )
        bridge.provenance_state.active = active

        with mock.patch("server.capture_pane_snapshot", return_value=pane):
            record = await bridge.write_accepted_enter(active, 5, 0)

        self.assertEqual(record.draft, "exact command")
        self.assertEqual(tuple(bridge.provenance_state.accepted_records), (record,))
        bridge.command.assert_awaited_once_with("send-keys -t %1 -H 0d")

    async def test_failed_or_cancelled_composer_enter_keeps_draft_history_and_taint(self):
        for write_error in (RuntimeError("tmux closed"), asyncio.CancelledError()):
            with self.subTest(write_error=type(write_error).__name__):
                app = object.__new__(AppServer)
                app.mobile_composer_states = {}
                app.command_provenance_states = {}
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
                bridge.session_name = "session"
                bridge.pane_id = "%1"
                bridge.settle_active_provenance_fence = mock.AsyncMock()
                bridge.write = mock.AsyncMock(side_effect=write_error)
                provenance = app.command_provenance_state("session")
                taint = UnavailableCommandProvenance(
                    session_name="session",
                    pane_id="%1",
                    layout_generation=provenance.layout_generation,
                    draft="unfinished",
                    revision=8,
                    owner_id=id(bridge),
                )
                provenance.mark_unavailable(taint)

                with mock.patch("server.pane_in_mode", return_value=False):
                    with self.assertRaises(type(write_error)):
                        await app.commit_mobile_composer(bridge, "session", revision=9)

                self.assertEqual(state, before)
                self.assertIsNone(provenance.active)
                self.assertIs(provenance.unavailable, taint)

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

    async def test_authoritative_selection_uses_exact_command_and_fails_partial_closed(self):
        connection = RecordingConnection()
        provenance = CommandProvenanceState(layout_generation=3)
        bridge = TmuxBridge(
            connection,
            "session",
            "/bin/sh",
            "/",
            provenance_state=provenance,
        )
        connection.bridge = bridge
        bridge.pane_id = "%1"
        bridge.phase = "forward"
        bridge.quiet = mock.AsyncMock()
        pane = snapshot(
            cols=10,
            authored_lines=["$ exact command"],
            plain_physical_rows=["$ exact co", "mmand     "],
            rows=2,
            cursor_x=5,
            cursor_y=1,
        )
        exact = "exact command"
        provenance.active = CommandProvenance(
            session_name="session",
            pane_id="%1",
            cols=10,
            rows=2,
            layout_generation=3,
            start_row=0,
            start_x=2,
            draft=exact,
            revision=4,
            source="composer-sync",
            owner_id=id(bridge),
            end_row=1,
            end_x=5,
            row_digest=_command_row_digest(pane, 0, 1),
            terminal_revision=0,
        )
        payload = {
            "requestId": "exact",
            "profile": "",
            "session": "session",
            "paneId": "%1",
            "epoch": 0,
            "revision": 0,
            "cutoff": 0,
            "layoutGeneration": 0,
            "cols": 10,
            "rows": 2,
            "baseY": 0,
            "bufferType": "normal",
            "selection": {"start": {"x": 2, "y": 0}, "end": {"x": 5, "y": 1}},
        }

        with (
            mock.patch("server.capture_pane_snapshot", return_value=pane),
            mock.patch(
                "server.provider_selection",
                side_effect=AssertionError("provider authority ran before composer authority"),
            ),
        ):
            text, error = await bridge.authoritative_selection(payload)
            self.assertEqual((text, error), (exact, None))

            payload["requestId"] = "partial"
            payload["selection"]["start"]["x"] = 3
            text, error = await bridge.authoritative_selection(payload)

        self.assertIsNone(text)
        self.assertEqual(error, "Terminal changed; select again.")

        payload["requestId"] = "stable-ordinary-client"
        payload["selection"] = {"start": {"x": 2, "y": 0}, "end": {"x": 5, "y": 1}}
        with mock.patch("server.capture_pane_snapshot", return_value=pane):
            text, error = await bridge.authoritative_selection(payload)
        self.assertEqual((text, error), (exact, None))

        changed = snapshot(
            cols=10,
            authored_lines=["$ exactXcommand"],
            plain_physical_rows=["$ exactXco", "mmand     "],
            rows=2,
            cursor_x=5,
            cursor_y=1,
        )
        payload["requestId"] = "out-of-band"
        payload["selection"] = {"start": {"x": 2, "y": 0}, "end": {"x": 5, "y": 1}}
        with mock.patch("server.capture_pane_snapshot", return_value=changed):
            text, error = await bridge.authoritative_selection(payload)
        self.assertIsNone(text)
        self.assertEqual(error, "Terminal changed; select again.")

    async def test_authoritative_selection_never_falls_back_inside_provider_ownership(self):
        connection = RecordingConnection()
        bridge = TmuxBridge(
            connection,
            "session",
            "/bin/sh",
            "/",
            provenance_state=CommandProvenanceState(),
        )
        connection.bridge = bridge
        bridge.pane_id = "%1"
        bridge.phase = "forward"
        bridge.quiet = mock.AsyncMock()
        pane = snapshot(
            cols=12,
            authored_lines=["provider"],
            plain_physical_rows=["provider    "],
            rows=1,
        )
        payload = {
            "requestId": "provider",
            "profile": "",
            "session": "session",
            "paneId": "%1",
            "epoch": 0,
            "revision": 0,
            "cutoff": 0,
            "layoutGeneration": 0,
            "cols": 12,
            "rows": 1,
            "baseY": 0,
            "bufferType": "normal",
            "selection": {"start": {"x": 0, "y": 0}, "end": {"x": 8, "y": 0}},
        }
        with (
            mock.patch("server.capture_pane_snapshot", return_value=pane),
            mock.patch(
                "server.provider_selection",
                return_value=mock.Mock(owned=True, text="exact provider source"),
            ),
            mock.patch(
                "server.extract_authoritative_selection",
                side_effect=AssertionError("provider selection fell through to tmux"),
            ),
        ):
            text, error = await bridge.authoritative_selection(payload)
        self.assertEqual((text, error), ("exact provider source", None))

        payload["requestId"] = "provider-failure"
        with (
            mock.patch("server.capture_pane_snapshot", return_value=pane),
            mock.patch("server.provider_selection", side_effect=RuntimeError("unsupported")),
            mock.patch(
                "server.extract_authoritative_selection",
                side_effect=AssertionError("provider failure fell through to tmux"),
            ),
        ):
            text, error = await bridge.authoritative_selection(payload)
        self.assertIsNone(text)
        self.assertEqual(error, "Terminal changed; select again.")

        payload["requestId"] = "provider-terminal-mutation"
        changed = snapshot(
            cols=12,
            authored_lines=["changed"],
            plain_physical_rows=["changed     "],
            rows=1,
        )
        with (
            mock.patch("server.capture_pane_snapshot", side_effect=(pane, changed)),
            mock.patch(
                "server.provider_selection",
                return_value=mock.Mock(owned=True, text="stale provider source"),
            ),
            mock.patch(
                "server.extract_authoritative_selection",
                side_effect=AssertionError("changed provider selection fell through to tmux"),
            ),
        ):
            text, error = await bridge.authoritative_selection(payload)
        self.assertIsNone(text)
        self.assertEqual(error, "Terminal changed; select again.")

    async def test_authoritative_selection_blocks_owned_writer_until_exact_copy_finishes(self):
        connection = RecordingConnection()
        provenance = CommandProvenanceState(layout_generation=3)
        write_lock = asyncio.Lock()
        bridge = TmuxBridge(
            connection,
            "session",
            "/bin/sh",
            "/",
            provenance_state=provenance,
            write_lock=write_lock,
        )
        writer = TmuxBridge(
            RecordingConnection(),
            "session",
            "/bin/sh",
            "/",
            provenance_state=provenance,
            write_lock=write_lock,
        )
        connection.bridge = bridge
        bridge.pane_id = writer.pane_id = "%1"
        bridge.phase = "forward"
        bridge.quiet = mock.AsyncMock()
        writer.command = mock.AsyncMock(return_value=[])
        pane = snapshot(
            cols=10,
            authored_lines=["$ exact command"],
            plain_physical_rows=["$ exact co", "mmand     "],
            rows=2,
            cursor_x=5,
            cursor_y=1,
        )
        provenance.active = CommandProvenance(
            session_name="session",
            pane_id="%1",
            cols=10,
            rows=2,
            layout_generation=3,
            start_row=0,
            start_x=2,
            draft="exact command",
            revision=4,
            source="composer-sync",
            owner_id=id(bridge),
            end_row=1,
            end_x=5,
            row_digest=_command_row_digest(pane, 0, 1),
            terminal_revision=0,
        )
        payload = {
            "requestId": "owned-writer",
            "profile": "",
            "session": "session",
            "paneId": "%1",
            "epoch": 0,
            "revision": 0,
            "cutoff": 0,
            "layoutGeneration": 0,
            "cols": 10,
            "rows": 2,
            "baseY": 0,
            "bufferType": "normal",
            "selection": {"start": {"x": 2, "y": 0}, "end": {"x": 5, "y": 1}},
        }
        capture_started = asyncio.Event()
        release_capture = asyncio.Event()

        async def to_thread(function, *args, **kwargs):
            if function is capture_pane_snapshot:
                capture_started.set()
                await release_capture.wait()
                return pane
            return function(*args, **kwargs)

        with mock.patch("server.asyncio.to_thread", side_effect=to_thread):
            copying = asyncio.create_task(bridge.authoritative_selection(payload))
            await capture_started.wait()
            writing = asyncio.create_task(writer.write("x"))
            await asyncio.sleep(0)
            writer.command.assert_not_awaited()
            release_capture.set()
            self.assertEqual(await copying, ("exact command", None))
            await writing

        writer.command.assert_awaited_once_with("send-keys -t %1 -H 78")

    async def test_transient_unknown_client_edit_during_copy_fails_closed(self):
        connection = RecordingConnection()
        provenance = CommandProvenanceState(layout_generation=3)
        bridge = TmuxBridge(
            connection,
            "session",
            "/bin/sh",
            "/",
            provenance_state=provenance,
        )
        connection.bridge = bridge
        bridge.pane_id = "%1"
        bridge.phase = "forward"
        bridge.quiet = mock.AsyncMock()
        pane = snapshot(
            cols=10,
            authored_lines=["$ exact command"],
            plain_physical_rows=["$ exact co", "mmand     "],
            rows=2,
            cursor_x=5,
            cursor_y=1,
        )
        provenance.active = CommandProvenance(
            session_name="session",
            pane_id="%1",
            cols=10,
            rows=2,
            layout_generation=3,
            start_row=0,
            start_x=2,
            draft="exact command",
            revision=4,
            source="composer-sync",
            owner_id=id(bridge),
            end_row=1,
            end_x=5,
            row_digest=_command_row_digest(pane, 0, 1),
            terminal_revision=0,
        )
        payload = {
            "requestId": "unknown-edit",
            "profile": "",
            "session": "session",
            "paneId": "%1",
            "epoch": 0,
            "revision": 0,
            "cutoff": 0,
            "layoutGeneration": 0,
            "cols": 10,
            "rows": 2,
            "baseY": 0,
            "bufferType": "normal",
            "selection": {"start": {"x": 2, "y": 0}, "end": {"x": 5, "y": 1}},
        }
        def capture_with_external_revision(*_args):
            bridge.offset += 1
            return pane

        with mock.patch(
            "server.capture_pane_snapshot",
            side_effect=capture_with_external_revision,
        ):
            text, error = await bridge.authoritative_selection(payload)

        self.assertIsNone(text)
        self.assertEqual(error, "Terminal changed; select again.")

    async def test_identical_rendered_rows_after_terminal_revision_change_fail_closed(self):
        connection = RecordingConnection()
        provenance = CommandProvenanceState(layout_generation=3)
        bridge = TmuxBridge(
            connection,
            "session",
            "/bin/sh",
            "/",
            provenance_state=provenance,
        )
        connection.bridge = bridge
        bridge.pane_id = "%1"
        bridge.phase = "forward"
        bridge.offset = 1
        bridge.quiet = mock.AsyncMock()
        pane = snapshot(
            cols=10,
            authored_lines=["$       "],
            plain_physical_rows=["$         "],
            rows=1,
            cursor_x=8,
            cursor_y=0,
            tab_stops=(8,),
        )
        provenance.active = CommandProvenance(
            session_name="session",
            pane_id="%1",
            cols=10,
            rows=1,
            layout_generation=3,
            start_row=0,
            start_x=2,
            draft="\t",
            revision=4,
            source="composer-sync",
            owner_id=id(bridge),
            end_row=0,
            end_x=8,
            row_digest=_command_row_digest(pane, 0, 0),
            terminal_revision=0,
        )
        payload = {
            "requestId": "same-cells-new-revision",
            "profile": "",
            "session": "session",
            "paneId": "%1",
            "epoch": 0,
            "revision": 1,
            "cutoff": 0,
            "layoutGeneration": 0,
            "cols": 10,
            "rows": 1,
            "baseY": 0,
            "bufferType": "normal",
            "selection": {"start": {"x": 2, "y": 0}, "end": {"x": 8, "y": 0}},
        }

        with mock.patch("server.capture_pane_snapshot", return_value=pane):
            text, error = await bridge.authoritative_selection(payload)

        self.assertIsNone(text)
        self.assertEqual(error, "Terminal changed; select again.")

    async def test_selection_geometry_mismatch_returns_exact_stale_error(self):
        connection = RecordingConnection()
        bridge = TmuxBridge(connection, "session", "/bin/sh", "/")
        connection.bridge = bridge
        bridge.pane_id = "%1"
        bridge.phase = "forward"
        bridge.quiet = mock.AsyncMock()
        pane = snapshot(cols=5, authored_lines=["safe"], rows=1)
        payload = {
            "requestId": "geometry",
            "profile": "",
            "session": "session",
            "paneId": "%1",
            "epoch": 0,
            "revision": 0,
            "cutoff": 0,
            "layoutGeneration": 0,
            "cols": 6,
            "rows": 1,
            "baseY": 0,
            "bufferType": "normal",
            "selection": {"start": {"x": 0, "y": 0}, "end": {"x": 1, "y": 0}},
        }

        with mock.patch("server.capture_pane_snapshot", return_value=pane):
            text, error = await bridge.authoritative_selection(payload)

        self.assertIsNone(text)
        self.assertEqual(error, "Terminal changed; select again.")

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


class RenameProvenanceTransferTest(unittest.IsolatedAsyncioTestCase):
    def make_app(self):
        app = object.__new__(AppServer)
        app.multi_tenant = False
        app.mobile_composer_states = {"old": {"draft": "cmd"}}
        app.command_provenance_states = {"old": CommandProvenanceState()}
        app.terminal_write_locks = {"old": asyncio.Lock()}
        app.live_terminal_connections = {}
        app.terminal_sizes = {"old": (10, 2)}
        app.scroll_states = {"old": {"pending": 0, "task": None, "paneId": "%1"}}
        return app

    async def test_rename_during_open_transfers_the_registered_opening_bridge(self):
        app = self.make_app()
        provenance = app.command_provenance_states["old"]
        lock = app.terminal_write_locks["old"]
        state = {"session": "old", "user": ""}
        bridge = TmuxBridge(
            RecordingConnection(),
            "old",
            "/bin/sh",
            "/",
            provenance_state=provenance,
            write_lock=lock,
        )
        open_started = asyncio.Event()
        release_open = asyncio.Event()

        async def suspended_open():
            open_started.set()
            await release_open.wait()

        bridge.open = mock.AsyncMock(side_effect=suspended_open)
        bridge.close = mock.AsyncMock()
        app.send_json = mock.AsyncMock()
        app.send_tabs = mock.AsyncMock()
        app.send_sessions = mock.AsyncMock()
        opening = asyncio.create_task(app.open_live_terminal(bridge, state))
        await open_started.wait()
        self.assertEqual(app.live_terminal_connections["old"][0][0], bridge)

        with (
            mock.patch("server.session_exists", return_value=False),
            mock.patch(
                "server.tmux_capture",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
        ):
            await app.handle_command(
                RecordingConnection(),
                bridge,
                state,
                {"type": "rename-tab", "session": "old", "name": "new"},
            )

        self.assertEqual(bridge.session_name, "new")
        self.assertEqual(state["session"], "new")
        self.assertNotIn("old", app.live_terminal_connections)
        self.assertEqual(app.live_terminal_connections["new"][0][0], bridge)
        release_open.set()
        await opening

    async def test_open_failure_closes_bridge_and_removes_registration(self):
        app = self.make_app()
        state = {"session": "old", "user": ""}
        bridge = TmuxBridge(
            RecordingConnection(),
            "old",
            "/bin/sh",
            "/",
            provenance_state=app.command_provenance_states["old"],
            write_lock=app.terminal_write_locks["old"],
        )
        bridge.open = mock.AsyncMock(side_effect=RuntimeError("open failed"))
        bridge.close = mock.AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "open failed"):
            await app.open_live_terminal(bridge, state)

        bridge.close.assert_awaited_once()
        self.assertNotIn("old", app.live_terminal_connections)

    async def test_cancelled_open_closes_bridge_and_removes_registration(self):
        app = self.make_app()
        state = {"session": "old", "user": ""}
        bridge = TmuxBridge(
            RecordingConnection(),
            "old",
            "/bin/sh",
            "/",
            provenance_state=app.command_provenance_states["old"],
            write_lock=app.terminal_write_locks["old"],
        )
        bridge.open = mock.AsyncMock(side_effect=asyncio.CancelledError())
        bridge.close = mock.AsyncMock()

        with self.assertRaises(asyncio.CancelledError):
            await app.open_live_terminal(bridge, state)

        bridge.close.assert_awaited_once()
        self.assertNotIn("old", app.live_terminal_connections)

    async def test_rename_preserves_copy_state_and_reconnect_identity(self):
        app = self.make_app()
        pane = snapshot(
            cols=10,
            authored_lines=["$ cmd"],
            plain_physical_rows=["$ cmd     ", "          "],
            rows=2,
        )
        provenance = app.command_provenance_states["old"]
        identity = provenance.row_tracker.observe(pane, 1)
        record = AcceptedCommand(
            session_name="old",
            pane_id="%1",
            cols=10,
            rows=2,
            layout_generation=0,
            start_row=identity.first_row,
            start_x=2,
            end_row=identity.first_row,
            end_x=5,
            draft="cmd",
            revision=2,
            source="composer-sync",
            row_digest=_command_row_digest(pane, 0, 0),
            row_epoch=identity.epoch,
        )
        provenance.remember(record)
        lock = app.terminal_write_locks["old"]
        first_state = {"session": "old"}
        second_state = {"session": "old"}
        first = TmuxBridge(
            RecordingConnection(), "old", "/bin/sh", "/", provenance_state=provenance, write_lock=lock
        )
        second = TmuxBridge(
            RecordingConnection(), "old", "/bin/sh", "/", provenance_state=provenance, write_lock=lock
        )
        app.register_live_terminal("old", first, first_state)
        app.register_live_terminal("old", second, second_state)
        second.provenance_state = CommandProvenanceState()
        second.write_lock = asyncio.Lock()

        app.transfer_single_tenant_session_state("old", "new")

        self.assertIs(app.command_provenance_state("new"), provenance)
        self.assertIs(app.terminal_write_lock("new"), lock)
        self.assertEqual((first.session_name, second.session_name), ("new", "new"))
        self.assertEqual((first_state["session"], second_state["session"]), ("new", "new"))
        self.assertIs(first.provenance_state, provenance)
        self.assertIs(second.provenance_state, provenance)
        self.assertIs(first.write_lock, lock)
        self.assertIs(second.write_lock, lock)
        reconnected = TmuxBridge(
            RecordingConnection(),
            "new",
            "/bin/sh",
            "/",
            provenance_state=app.command_provenance_state("new"),
            write_lock=app.terminal_write_lock("new"),
        )
        self.assertIs(reconnected.provenance_state, provenance)
        self.assertIs(reconnected.write_lock, lock)
        renamed = provenance.accepted("%1")[0]
        self.assertEqual(renamed.session_name, "new")
        matched, text = exact_provenance_selection(
            pane,
            "new",
            0,
            (renamed.start_row, 2),
            (renamed.end_row, 5),
            None,
            (renamed,),
            identity,
            "cmd",
        )
        self.assertTrue(matched)
        self.assertEqual(text, "cmd")

    async def test_rename_waits_for_shared_write_and_future_writes_use_transferred_lock(self):
        app = self.make_app()
        provenance = app.command_provenance_states["old"]
        lock = app.terminal_write_locks["old"]
        first_state = {"session": "old", "user": ""}
        second_state = {"session": "old", "user": ""}
        first = TmuxBridge(
            RecordingConnection(), "old", "/bin/sh", "/", provenance_state=provenance, write_lock=lock
        )
        second = TmuxBridge(
            RecordingConnection(), "old", "/bin/sh", "/", provenance_state=provenance, write_lock=lock
        )
        first.pane_id = second.pane_id = "%1"
        app.register_live_terminal("old", first, first_state)
        app.register_live_terminal("old", second, second_state)
        started = asyncio.Event()
        release = asyncio.Event()
        commands = []

        async def first_command(value):
            commands.append(("first", value))
            started.set()
            await release.wait()
            return []

        async def second_command(value):
            commands.append(("second", value))
            return []

        first.command = first_command
        second.command = second_command
        app.send_json = mock.AsyncMock()
        app.send_tabs = mock.AsyncMock()
        app.send_sessions = mock.AsyncMock()
        write = asyncio.create_task(first.write("a"))
        await started.wait()
        renamed = asyncio.create_task(
            app.handle_command(
                RecordingConnection(),
                second,
                second_state,
                {"type": "rename-tab", "session": "old", "name": "new"},
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(renamed.done())
        release.set()
        with (
            mock.patch("server.session_exists", return_value=False),
            mock.patch(
                "server.tmux_capture",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
        ):
            await asyncio.gather(write, renamed)
        await second.write("b")

        self.assertEqual([kind for kind, _ in commands], ["first", "second"])
        self.assertIs(first.write_lock, app.terminal_write_lock("new"))
        self.assertIs(second.write_lock, app.terminal_write_lock("new"))
        self.assertEqual((first.session_name, second.session_name), ("new", "new"))

    async def test_input_rereads_renamed_identity_after_scroll_settle(self):
        app = self.make_app()
        provenance = app.command_provenance_states["old"]
        lock = app.terminal_write_locks["old"]
        input_state = {"session": "old", "user": ""}
        rename_state = {"session": "old", "user": ""}
        input_bridge = TmuxBridge(
            RecordingConnection(), "old", "/bin/sh", "/", provenance_state=provenance, write_lock=lock
        )
        rename_bridge = TmuxBridge(
            RecordingConnection(), "old", "/bin/sh", "/", provenance_state=provenance, write_lock=lock
        )
        input_bridge.pane_id = rename_bridge.pane_id = "%1"
        input_bridge.command = mock.AsyncMock(return_value=[])
        app.register_live_terminal("old", input_bridge, input_state)
        app.register_live_terminal("old", rename_bridge, rename_state)
        settle_started = asyncio.Event()
        release_settle = asyncio.Event()

        async def settle(_session_name):
            settle_started.set()
            await release_settle.wait()

        app.settle_scroll_history = settle
        app.send_json = mock.AsyncMock()
        app.send_tabs = mock.AsyncMock()
        app.send_sessions = mock.AsyncMock()
        pending_input = asyncio.create_task(
            app.handle_command(
                RecordingConnection(),
                input_bridge,
                input_state,
                {"type": "input", "data": "x", "revision": 2},
            )
        )
        await settle_started.wait()
        with (
            mock.patch("server.session_exists", return_value=False),
            mock.patch("server.pane_in_mode", return_value=False),
            mock.patch(
                "server.tmux_capture",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
        ):
            await app.handle_command(
                RecordingConnection(),
                rename_bridge,
                rename_state,
                {"type": "rename-tab", "session": "old", "name": "new"},
            )
            release_settle.set()
            await pending_input

        self.assertNotIn("old", app.mobile_composer_states)
        self.assertNotIn("old", app.command_provenance_states)
        self.assertNotIn("old", app.terminal_write_locks)
        self.assertNotIn("old", app.scroll_states)
        self.assertEqual(input_state["session"], "new")
        self.assertEqual(app.mobile_composer_states["new"]["draft"], "")
        input_bridge.command.assert_awaited_once_with("send-keys -t %1 -H 78")


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

    def test_selection_contract_includes_client_geometry(self):
        selection_start = self.source.index("  function terminalSelectionState()")
        selection_end = self.source.index("  function sameTerminalSelectionState", selection_start)
        selection = self.source[selection_start:selection_end]
        self.assertIn("cols: term.cols", selection)
        self.assertIn("rows: term.rows", selection)

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
