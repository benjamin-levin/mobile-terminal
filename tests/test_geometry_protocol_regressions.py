"""Behavioral regressions for snapshot input modes, geometry and output fencing."""
import asyncio
import json
from pathlib import Path
import re
import subprocess
import time
import unittest
from unittest import mock

import provider_authority
import server
from tests.test_terminal_authority import QueuedConnection, RecordingConnection, snapshot
from tests.tmux_harness import TmuxHarness

ROOT = Path(__file__).parents[1]
SOURCE = (ROOT / "static/app.js").read_text()


def function(name):
    match = re.search(rf"^  (?:async )?function {name}\(.*?^  \}}$", SOURCE, re.M | re.S)
    if not match:
        raise AssertionError(name)
    return match[0]


class ClientGeometryProtocolTest(unittest.TestCase):
    def node(self, names, body):
        script = "\n".join([
            'const assert = require("node:assert/strict");',
            f'const {{ Terminal }} = require({json.dumps(str(ROOT / "node_modules/@xterm/xterm"))});',
            'const term = new Terminal({cols: 40, rows: 6, allowProposedApi: true}); term._core.textarea = {value: ""};',
            *(function(name) for name in names),
            '(async () => {', body,
            '})().catch(error => { console.error(error); process.exitCode = 1; });',
        ])
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_server_width_table_matches_installed_xterm_unicode_provider(self):
        script = '\n'.join([
            f'const {{ Terminal }} = require({json.dumps(str(ROOT / "node_modules/@xterm/xterm"))});',
            'const term = new Terminal({allowProposedApi: true});',
            'require("node:assert/strict").equal(term.unicode.activeVersion, "6");',
            'const widths = Buffer.alloc(0x110000);',
            'for (let value = 0; value < widths.length; value++) {',
            '  widths[value] = term._core.unicodeService.wcwidth(value);',
            '}',
            'process.stdout.write(widths); term.dispose();',
        ])
        result = subprocess.run(["node", "-e", script], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = bytes(provider_authority.xterm_cell_width(chr(value)) for value in range(0x110000))
        self.assertEqual(result.stdout, expected)

    def test_real_xterm_cells_match_server_tokens_for_sequences(self):
        values = ["✅❌✨", "😀界é", "♥︎♥️", "🇺🇸", "👩‍💻", "1️⃣", "👍🏽",
                  "AःB", "A\U0001d167B", "\U00020000", "A​B"]
        script = '\n'.join([
            f'const {{ Terminal }} = require({json.dumps(str(ROOT / "node_modules/@xterm/xterm"))});',
            'const term = new Terminal({cols: 40, rows: 2, allowProposedApi: true});',
            '(async () => { const results = [];',
            f'for (const text of {json.dumps(values)}) {{',
            '  await new Promise(resolve => term.write("\\x1b[2J\\x1b[H" + text, resolve));',
            '  const line = term.buffer.active.getLine(0); const cells = [];',
            '  for (let x = 0; x < term.buffer.active.cursorX; x++) {',
            '    const cell = line.getCell(x);',
            '    if (cell.getWidth()) cells.push([cell.getChars(), x, x + cell.getWidth()]);',
            '  }',
            '  results.push([cells, term.buffer.active.cursorX]);',
            '}',
            'console.log(JSON.stringify(results)); term.dispose();',
            '})().catch(error => { console.error(error); process.exitCode = 1; });',
        ])
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for text, (cells, width) in zip(values, json.loads(result.stdout)):
            with self.subTest(text=text):
                tokens, actual_width = server._client_row_display_tokens(text)
                self.assertEqual(([list(token) for token in tokens], actual_width), (cells, width))

    def test_real_xterm_queries_have_one_owner_and_reply_shaped_paste_survives(self):
        self.node(["installTmuxQueryOwnership"], r'''
          const received = [];
          term.onData(data => received.push(data));
          const write = data => new Promise(resolve => term.write(data, resolve));
          await write('\x1b[6n');
          assert.deepEqual(received, ['\x1b[1;1R']);
          received.length = 0;
          installTmuxQueryOwnership();
          await write('\x1b[6n\x1b[?6n\x1b[c\x1b[>c\x1b[?2004$p\x1b]10;?\x07\x1bP$qm\x1b\\');
          assert.deepEqual(received, []);
          term.paste('\x1b[1;1R');
          assert.deepEqual(received, ['\x1b[1;1R']);
          term.dispose();
        ''')

    def test_real_xterm_seed_restores_bracketed_mode_and_clears_pane_leakage(self):
        self.node(["terminalReplayBaselineSequence", "terminalModeSequence", "terminalCursorStyle"], r'''
          const write = data => new Promise(resolve => term.write(data, resolve));
          const meta = {rows: 6, cursorX: 0, cursorY: 0, scrollLower: 5, bracketedPaste: true};
          await write(terminalModeSequence(meta));
          assert.equal(term.modes.bracketedPasteMode, true);
          const received = [];
          term.onData(data => received.push(data));
          term.paste('one\ntwo');
          assert.deepEqual(received, ['\x1b[200~one\rtwo\x1b[201~']);
          await write(terminalModeSequence({...meta, bracketedPaste: false}));
          assert.equal(term.modes.bracketedPasteMode, false);
          await write(terminalModeSequence({...meta, bracketedPaste: null}));
          assert.equal(term.modes.bracketedPasteMode, false);
          term.dispose();
        ''')

    def test_seed_preserves_real_xterm_softwrap_and_hard_breaks(self):
        self.node(["applyTerminalSeed", "terminalReplayBaselineSequence", "terminalModeSequence", "terminalCursorStyle", "terminalTabStopsSequence"], r'''
          globalThis.connectionGeneration = 1;
          globalThis.connectionGenerationIsCurrent = value => value === 1;
          globalThis.writeTerminal = data => new Promise(resolve => term.write(data, resolve));
          globalThis.acknowledgeTerminalSize = () => {};
          for (const name of ['decoder','terminalMinCols','terminalMinRows','terminalEpoch','terminalPaneId',
            'terminalCutoff','terminalRevision','terminalLayoutGeneration','terminalSeedHistory','terminalHistory','pendingSeedScrollTarget']) globalThis[name] = null;
          const meta = {cols:40, rows:6, seedHistory:0, history:0, cursorX:0, cursorY:2, scrollLower:5};
          const payload = {meta, physicalRows:['x'.repeat(40),'continuation','hard break','','',''],
            wrappedRows:[false,true,false,false,false,false], paneId:'%1', epoch:1, cutoff:0, layoutGeneration:1};
          await applyTerminalSeed(payload);
          assert.equal(term.buffer.active.getLine(1).isWrapped, true);
          assert.equal(term.buffer.active.getLine(2).isWrapped, false);
          term.resize(60,6);
          assert.equal(term.buffer.active.getLine(0).translateToString(true), 'x'.repeat(40)+'continuation');
          assert.equal(term.buffer.active.getLine(1).translateToString(true), 'hard break');
          await applyTerminalSeed({...payload,
            physicalRows:['\x1b[31m'+'x'.repeat(39), '\x1b[31m界tail', '\x1b[0mhard break','','','']});
          assert.equal(term.buffer.active.getLine(1).isWrapped, true);
          assert.equal(term.buffer.active.getLine(0).translateToString(true), 'x'.repeat(39));
          assert.equal(term.buffer.active.getLine(1).translateToString(true), '界tail');
          assert.equal(term.buffer.active.getLine(1).getCell(0).getFgColor(), 1);
          assert.equal(term.buffer.active.getLine(2).isWrapped, false);
          term.dispose();
        ''')

    def test_geometry_requests_wait_for_actual_ack_and_coalesce(self):
        self.node(["terminalResizeDiffersFromDelivered", "setDesiredTerminalSize", "flushTerminalResize", "acknowledgeTerminalSize"], r'''
          globalThis.terminalMinCols = 20; globalThis.terminalMinRows = 6;
          globalThis.desiredTerminalCols = 0; globalThis.desiredTerminalRows = 0;
          globalThis.deliveredTerminalCols = 0; globalThis.deliveredTerminalRows = 0;
          globalThis.terminalResizeDirty = false; globalThis.terminalResizePending = false;
          globalThis.terminalSeedInFlight = false; globalThis.terminalAuthoritative = false;
          globalThis.sentTerminalSize = null; globalThis.TERMINAL_RESIZE_WATCHDOG_MS = 2000;
          globalThis.recordViewportForensics = () => {};
          globalThis.recordTerminalResizeWithheld = () => {};
          globalThis.scheduleTerminalResizeWatchdog = () => {};
          globalThis.clearTerminalResizeWatchdog = () => {};
          globalThis.terminalSocketIsOpen = () => true;
          const sent = []; globalThis.sendMessage = data => { sent.push(data); return true; };
          setDesiredTerminalSize(61, 1);
          assert.deepEqual([desiredTerminalCols, desiredTerminalRows], [61, 6]);
          assert.equal(flushTerminalResize(), true);
          assert.deepEqual([deliveredTerminalCols, deliveredTerminalRows], [0, 0]);
          setDesiredTerminalSize(70, 8); setDesiredTerminalSize(72, 9);
          assert.equal(flushTerminalResize(), false);
          acknowledgeTerminalSize(61, 6);
          assert.equal(flushTerminalResize(), true);
          assert.deepEqual(sent.map(x => [x.cols, x.rows]), [[61,6], [72,9]]);
          acknowledgeTerminalSize(72, 9);
          assert.equal(terminalResizePending, false);
          assert.equal(flushTerminalResize(), false);
          term.dispose();
        ''')

    def test_seed_null_target_differs_from_explicit_zero(self):
        self.node(["applyTerminalSeed", "terminalReplayBaselineSequence", "terminalModeSequence", "terminalCursorStyle", "terminalTabStopsSequence"], r'''
          globalThis.connectionGeneration = 1;
          globalThis.connectionGenerationIsCurrent = value => value === 1;
          globalThis.writeTerminal = data => new Promise(resolve => term.write(data, resolve));
          globalThis.acknowledgeTerminalSize = () => {};
          for (const name of ['decoder','terminalMinCols','terminalMinRows','terminalEpoch','terminalPaneId',
            'terminalCutoff','terminalRevision','terminalLayoutGeneration','terminalSeedHistory','terminalHistory','pendingSeedScrollTarget']) globalThis[name] = null;
          const meta = {cols:40, rows:6, seedHistory:0, history:0, cursorX:0, cursorY:0, scrollLower:5};
          const payload = {meta, physicalRows:['a','','','','',''], paneId:'%1', epoch:1, cutoff:0, layoutGeneration:1};
          for (const value of [null, undefined]) {
            await applyTerminalSeed({...payload, scrollTarget:value});
            assert.equal(pendingSeedScrollTarget, null);
          }
          await applyTerminalSeed({...payload, scrollTarget:0});
          assert.equal(pendingSeedScrollTarget, 0);
          term.dispose();
        ''')


class ServerGeometryProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_mode_cache_requires_continuous_observation_and_matching_process_identity(self):
        with mock.patch.dict(server._PANE_BRACKETED_PASTE, {}, clear=True), mock.patch("server.pane_mode_identity", return_value="10:20:/dev/pts/1"):
            first = server.TmuxBridge(RecordingConnection(), "session", "/bin/sh", "/")
            first.pane_id = "%1"
            first._observe_pane_modes("%1")
            await first.pane_bytes("%1", b"\x1b[?20")
            await first.pane_bytes("%1", b"04h")
            second = server.TmuxBridge(RecordingConnection(), "session", "/bin/sh", "/")
            second.pane_id = "%1"
            second._observe_pane_modes("%1")
            fields = ["%1", "0", "40", "6", "0", "0", "0", "1", "1", "default",
                      "0", "0", "0", "0", "1", "0", "0", "0", "0", "0", "5", "", "2000", "", "10:20:/dev/pts/1"]
            def metadata():
                with mock.patch("server.tmux_capture", return_value=subprocess.CompletedProcess([], 0, '\t'.join(fields)+'\n', '')):
                    return server.pane_metadata("session")[-1]
            self.assertIs(metadata(), True)
            await first.pane_bytes("%1", b"\x1b]title;\x1b[?2004l")
            await first.pane_bytes("%1", b"ignored\x07")
            self.assertIs(metadata(), True, "OSC text must not change input modes")
            await first.close()
            self.assertIs(metadata(), True, "another observer still sees all mode changes")
            fields[-1] = "11:21:/dev/pts/1"
            self.assertIsNone(metadata(), "reused pane IDs cannot inherit an old process's mode")
            fields[-1] = "10:20:/dev/pts/1"
            await second.close()
            self.assertIsNone(metadata(), "offline changes make cached state unknowable")
            self.assertEqual(server._PANE_BRACKETED_PASTE, {})
            self.assertIsNone(snapshot(cols=40, rows=6, authored_lines=['']*6).metadata()['bracketedPaste'])

    async def test_busy_resize_has_bounded_latency_and_no_dropped_or_duplicate_held_bytes(self):
        connection = RecordingConnection()
        bridge = server.TmuxBridge(connection, "session", "/bin/sh", "/")
        connection.bridge = bridge
        bridge.pane_id = "%1"
        bridge.phase = "forward"
        await bridge.pane_bytes("%1", b"before")
        bridge.phase = "hold"
        bridge.selection_hold_request_id = "copy"
        await bridge.pane_bytes("%1", b"pending-selection-output")
        emitted = []
        async def repaint():
            for index in range(30):
                data = f"frame-{index}\r".encode()
                emitted.append(data)
                await bridge.pane_bytes("%1", data)
                await asyncio.sleep(.02)
        task = asyncio.create_task(repaint())
        with (
            mock.patch.object(bridge, "set_size", new=mock.AsyncMock()),
            mock.patch("server.pane_metadata", return_value=("%1", 0, 70, 8)),
            mock.patch("server.capture_pane_snapshot", side_effect=AssertionError("busy resize must retain buffer")),
            mock.patch.object(bridge, "_schedule_stabilize_reseed"),
        ):
            started = time.monotonic()
            await bridge.resize(70, 8)
            self.assertLess(time.monotonic()-started, .8)
            self.assertFalse(task.done(), "resize waited for output to stop")
        await task
        self.assertEqual(b''.join(x for x in connection.messages if isinstance(x, bytes)), b'beforepending-selection-output'+b''.join(emitted))
        payloads = [json.loads(x) for x in connection.messages if isinstance(x, str)]
        resume = next(x for x in payloads if x['type'] == 'seed-resume')
        self.assertEqual(resume['cutoff'], len(b'before'))
        self.assertTrue(bridge.seed_degraded)
        self.assertEqual(bridge.phase, "forward")
        records = [x for x in payloads if x['type'] == 'terminal-output']
        self.assertTrue(all(a['end'] == b['start'] for a,b in zip(records, records[1:])))

    async def test_flush_ack_fence_orders_waiting_output_and_cancellation_releases_waiters(self):
        connection = QueuedConnection()
        bridge = server.TmuxBridge(connection, "session", "/bin/sh", "/")
        bridge.pane_id = "%1"
        flushing = asyncio.create_task(bridge._flush_seed_output(1, 0))
        barrier = await asyncio.wait_for(connection.payloads.get(), timeout=1)
        self.assertEqual(barrier['type'], 'post-flush')
        output = asyncio.create_task(bridge.pane_bytes("%1", b"after"))
        await asyncio.sleep(0)
        self.assertFalse(output.done())
        bridge.acknowledge({'type':'post-flush-ack', 'epoch':1, 'cycle':1})
        await asyncio.wait_for(asyncio.gather(flushing, output), timeout=1)
        types = [json.loads(x)['type'] for x in connection.messages if isinstance(x, str)]
        self.assertEqual(types, ['post-flush', 'seed-open', 'terminal-output'])
        self.assertEqual(bridge.flush_acks, {})
        connection.payloads = asyncio.Queue()
        bridge.phase = "hold"
        flushing = asyncio.create_task(bridge._flush_seed_output(2, bridge.offset))
        await asyncio.wait_for(connection.payloads.get(), timeout=1)
        flushing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await flushing
        self.assertEqual(bridge.flush_acks, {})
        self.assertFalse(bridge.send_lock.locked())
        await asyncio.wait_for(bridge.close(), timeout=1)
        self.assertTrue(bridge.closed)

    async def test_private_tmux_repainting_resize_finishes_while_app_remains_active(self):
        harness = TmuxHarness(self, prefix="mt-resize-")
        harness.start_session("busy", "-x", "40", "-y", "8", "/bin/sh")
        harness.run("set-option", "-t", "busy", "status", "off")
        connection = RecordingConnection()
        bridge = harness.register_async_close(server.TmuxBridge(connection, "busy", "/bin/sh", "/", create_if_missing=False, initial_size=(40,8)))
        connection.bridge = bridge
        await bridge.open()
        bridge.phase = "forward"
        await bridge.write("while :; do printf '\\rspinner'; sleep 0.04; done\r")
        await asyncio.sleep(.1)
        with mock.patch.object(bridge, "_schedule_stabilize_reseed"):
            started = time.monotonic()
            await bridge.resize(50, 6)
            self.assertLess(time.monotonic()-started, 1.5)
        self.assertEqual(server.window_dimensions("busy"), (50,6))
        payloads = [json.loads(x) for x in connection.messages if isinstance(x, str)]
        self.assertIn('seed-resume', [x['type'] for x in payloads])
        self.assertEqual(payloads[-1]['type'], 'seed-open')
        await bridge.write(b'\x03')
