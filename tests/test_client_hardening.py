import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text()
SW_JS = (ROOT / "static" / "sw.js").read_text()
PASSKEY_JS = (ROOT / "static" / "passkey.js").read_text()
PROXY_PY = (ROOT / "proxy.py").read_text()


def section(source, start_marker, end_marker):
    start = source.index(start_marker)
    return source[start : source.index(end_marker, start)]


class ClientHardeningWiringTest(unittest.TestCase):
    def test_service_worker_tracks_revalidation_and_uses_network_first_navigation(self):
        self.assertIn('const CACHE = "mobile-terminal-v20";', SW_JS)
        self.assertIn("const NAVIGATION_TIMEOUT_MS = 2500;", SW_JS)
        self.assertIn('if (req.mode === "navigate")', SW_JS)
        self.assertIn("Promise.race([", SW_JS)
        self.assertIn("controller?.abort();", SW_JS)
        self.assertIn(
            "return cached;",
            section(SW_JS, "async function navigationResponse", 'self.addEventListener("fetch"'),
        )
        static_fetch = section(SW_JS, "  const cachePromise = caches.open(CACHE);", "});")
        self.assertIn("event.waitUntil(revalidation.catch(() => {}));", static_fetch)
        self.assertLess(
            static_fetch.index("event.waitUntil(revalidation.catch(() => {}));"),
            static_fetch.index("event.respondWith("),
        )

    def test_proxy_service_worker_rewrite_matches_the_current_cache_name(self):
        self.assertIn("'const CACHE = \"mobile-terminal-v20\";'", PROXY_PY)
        self.assertIn("'const CACHE = \"mobile-terminal-proxy-v15\";'", PROXY_PY)
        self.assertNotIn("mobile-terminal-v19", SW_JS)
        self.assertNotIn("mobile-terminal-proxy-v14", PROXY_PY)

    def test_controller_change_reload_is_early_or_deferred_until_inactive(self):
        controller_change = section(
            APP_JS,
            'navigator.serviceWorker.addEventListener("controllerchange"',
            '    window.addEventListener("load"',
        )
        self.assertIn("serviceWorkerReloaded", controller_change)
        self.assertIn("serviceWorkerReloadDeferred", controller_change)
        self.assertIn('document.visibilityState === "hidden"', controller_change)
        self.assertIn("SERVICE_WORKER_EARLY_RELOAD_MS", controller_change)
        self.assertIn('window.addEventListener("pagehide", reloadWhenInactive);', controller_change)
        self.assertIn('document.addEventListener("visibilitychange", reloadWhenInactive);', controller_change)

    def test_shortcut_repeat_timers_are_registered_and_cleared_at_teardown_points(self):
        shortcut = section(APP_JS, "  function clearActiveShortcutRepeatTimers()", "  function expandShortcutSequence")
        self.assertIn("const activeShortcutRepeatTimers = new Set();", APP_JS)
        self.assertGreaterEqual(shortcut.count("activeShortcutRepeatTimers.add("), 2)
        self.assertIn("clearActiveShortcutRepeatTimers();\n    shortcutBar.innerHTML", shortcut)
        self.assertIn("button.setPointerCapture(event.pointerId);", shortcut)
        self.assertIn('"pointerup", "pointercancel", "pointerleave", "lostpointercapture"', shortcut)
        visibility = section(
            APP_JS,
            '  document.addEventListener("visibilitychange", () => {',
            '  document.addEventListener("keydown"',
        )
        self.assertGreaterEqual(visibility.count("clearActiveShortcutRepeatTimers();"), 2)
        self.assertGreaterEqual(APP_JS.count("clearActiveShortcutRepeatTimers();"), 5)

    def test_reconnects_are_tracked_and_terminal_awaits_recheck_generation(self):
        self.assertNotIn("setTimeout(connect", APP_JS)
        schedule = section(APP_JS, "  function scheduleConnect(delay)", "  function reconnectSocket()")
        self.assertIn("const scheduledGeneration = connectionGeneration;", schedule)
        self.assertIn("connectionGenerationIsCurrent(scheduledGeneration)", schedule)
        connect = section(APP_JS, "  function connect()", "  function setPasskeyRetryUi")
        self.assertLess(connect.index("const previousSocket = socket;"), connect.index("new WebSocket(wsUrl())"))
        self.assertIn("previousSocket.close();", connect)
        self.assertIn("connectionGeneration += 1;", connect)
        self.assertIn("scheduleConnect(80);", connect)
        self.assertIn("scheduleConnect(1500);", connect)
        close_handler = section(APP_JS, '    socket.addEventListener("close"', "  function setPasskeyRetryUi")
        self.assertIn("connectionGeneration += 1;", close_handler)
        seed = section(APP_JS, "  async function applyTerminalSeed", "  async function handleTerminalBinary")
        self.assertEqual(seed.count("await writeTerminal("), 4)
        self.assertGreaterEqual(seed.count("connectionGenerationIsCurrent(generation)"), 5)
        binary = section(APP_JS, "  async function handleTerminalBinary", "  async function drainSocketMessageQueue")
        self.assertIn("chunk = await chunk.arrayBuffer();", binary)
        self.assertIn("await writeTerminal", binary)
        self.assertIn("terminalRevision = Number(lastMetadata.end);", binary)
        self.assertGreaterEqual(binary.count("connectionGenerationIsCurrent(generation)"), 4)
        self.assertLess(
            binary.index("chunk = await chunk.arrayBuffer();"),
            binary.index(
                "if (!connectionGenerationIsCurrent(generation))",
                binary.index("chunk = await chunk.arrayBuffer();"),
            ),
        )
        self.assertLess(
            binary.index("await writeTerminal"),
            binary.index(
                "if (!connectionGenerationIsCurrent(generation))",
                binary.index("await writeTerminal"),
            ),
        )
        seed_end = section(APP_JS, '    if (payload.type === "seed-end")', '    if (payload.type === "post-flush")')
        self.assertIn("messageSocket !== socket", seed_end)
        self.assertIn("messageSocket.send(JSON.stringify", seed_end)

    def test_terminal_backlog_coalesces_only_adjacent_contiguous_live_output(self):
        helpers = section(
            APP_JS,
            "  const TERMINAL_BACKLOG_MAX_BYTES",
            "  function yieldTerminalRender()",
        )
        script = "\n".join(
            (
                'const assert = require("node:assert/strict");',
                "const performance = { now: () => 2000 };",
                "let connectionGeneration = 7;",
                "let socket = null;",
                "let terminalAuthoritative = true;",
                "let historyReseedPending = false;",
                "let terminalSeedHistory = 2000;",
                "const WebSocket = { OPEN: 1 };",
                helpers,
                "const metadata = (paneId, epoch, start, end, kind = 'live') => ({",
                "  paneId, epoch, start, end, kind,",
                "});",
                "const output = (meta, queuedAt = 1000) => ({",
                "  type: 'terminal-output', metadata: meta, data: new ArrayBuffer(meta.end - meta.start),",
                "  byteLength: meta.end - meta.start, queuedAt,",
                "});",
                "const first = metadata('%1', 4, 10, 12);",
                "assert.equal(terminalOutputsCanCoalesce(first, metadata('%1', 4, 12, 15)), true);",
                "assert.equal(terminalOutputsCanCoalesce(first, metadata('%1', 4, 13, 15)), false);",
                "assert.equal(terminalOutputsCanCoalesce(first, metadata('%2', 4, 12, 15)), false);",
                "assert.equal(terminalOutputsCanCoalesce(first, metadata('%1', 5, 12, 15)), false);",
                "assert.equal(terminalOutputsCanCoalesce(first, metadata('%1', 4, 12, 15, 'postseed')), false);",
                "const queue = createSocketMessageQueue();",
                "queue.items = [",
                "  output(first),",
                "  output(metadata('%1', 4, 12, 15)),",
                "  output(metadata('%1', 4, 16, 17)),",
                "];",
                "queue.terminalBytes = 6;",
                "queue.terminalOldestAt = 1000;",
                "const batch = takeQueuedTerminalOutputBatch(queue);",
                "assert.equal(batch.length, 2);",
                "assert.equal(queue.items.length, 1);",
                "assert.equal(queue.terminalBytes, 1);",
                "assert.equal(queue.terminalOldestAt, 1000);",
                "const separated = createSocketMessageQueue();",
                "separated.items = [",
                "  output(first),",
                "  { type: 'message', payload: { type: 'tabs' } },",
                "  output(metadata('%1', 4, 12, 15)),",
                "];",
                "separated.terminalBytes = 5;",
                "separated.terminalOldestAt = 1000;",
                "assert.equal(takeQueuedTerminalOutputBatch(separated).length, 1);",
                "assert.equal(separated.items[0].type, 'message');",
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_terminal_backlog_cap_drops_output_and_requests_existing_reseed(self):
        helpers = section(
            APP_JS,
            "  const TERMINAL_BACKLOG_MAX_BYTES",
            "  function yieldTerminalRender()",
        )
        script = "\n".join(
            (
                'const assert = require("node:assert/strict");',
                "const performance = { now: () => 2000 };",
                "let connectionGeneration = 7;",
                "let terminalAuthoritative = true;",
                "let historyReseedPending = false;",
                "let terminalSeedHistory = 2500;",
                "const sent = [];",
                "const WebSocket = { OPEN: 1 };",
                "const socket = { readyState: 1, send: (message) => sent.push(JSON.parse(message)) };",
                "const console = { debug: () => {} };",
                helpers,
                "const queue = createSocketMessageQueue();",
                "queue.items = [",
                "  { type: 'message', payload: { type: 'tabs' } },",
                "  { type: 'terminal-output', byteLength: TERMINAL_BACKLOG_MAX_BYTES + 1, queuedAt: 1000 },",
                "];",
                "queue.terminalBytes = TERMINAL_BACKLOG_MAX_BYTES + 1;",
                "queue.terminalOldestAt = 1000;",
                "assert.equal(terminalBacklogExceeded(queue), true);",
                "const agedQueue = createSocketMessageQueue();",
                "agedQueue.terminalBytes = 1;",
                "agedQueue.terminalOldestAt = 0;",
                "assert.equal(terminalBacklogExceeded(agedQueue), true);",
                "assert.equal(requestTerminalBacklogReseed(queue, 7, socket), true);",
                "assert.deepEqual(queue.items, [{ type: 'message', payload: { type: 'tabs' } }]);",
                "assert.equal(queue.terminalBytes, 0);",
                "assert.equal(queue.reseedPending, true);",
                "assert.equal(terminalAuthoritative, false);",
                "assert.equal(historyReseedPending, true);",
                "assert.deepEqual(sent, [{ type: 'history-reseed', historyLines: 2500, scrollTarget: 0 }]);",
                "assert.equal(requestTerminalBacklogReseed(queue, 7, socket), false);",
                "assert.equal(sent.length, 1);",
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        wiring = section(
            APP_JS,
            "    const onSocketMessage = (event) => {",
            '    socket.addEventListener("message"',
        )
        self.assertIn("if (terminalBacklogExceeded(messageQueue))", wiring)
        self.assertIn("requestTerminalBacklogReseed(", wiring)
        drain = section(
            APP_JS,
            "  async function drainSocketMessageQueue",
            "  function scheduleLayoutRefresh",
        )
        self.assertIn("await yieldTerminalRender();", drain)
        self.assertIn("TERMINAL_RENDER_SLICE_MS", drain)
        self.assertIn("if (!connectionGenerationIsCurrent(generation)) return;", drain)

    def test_coalesced_terminal_write_tracks_last_revision_and_generation(self):
        generation_guard = section(
            APP_JS,
            "  function connectionGenerationIsCurrent(generation)",
            "  function createSocketMessageQueue()",
        )
        binary = section(
            APP_JS,
            "  async function handleTerminalBinary",
            "  async function drainSocketMessageQueue",
        )
        script = "\n".join(
            (
                'const assert = require("node:assert/strict");',
                "let connectionGeneration = 9;",
                "let terminalRevision = 10;",
                "let terminalPaneId = '%1';",
                "let terminalEpoch = 4;",
                "let terminalAuthoritative = true;",
                "let followOutput = false;",
                "let bottomPinUntil = 0;",
                "const semanticPromptState = { seenMarker: false };",
                "const performance = { now: () => 100 };",
                "const term = { scrollToBottom: () => { throw new Error('unexpected scroll'); } };",
                "const decoder = { decode: (bytes) => bytes.byteLength };",
                "const writes = [];",
                "const writeTerminal = async (data) => { writes.push(data); };",
                "const terminalHorizontallyOverflows = () => false;",
                "const scheduleSemanticComposerSync = () => {};",
                "const scheduleLayoutRefresh = () => {};",
                generation_guard,
                binary,
                "const item = (start, end) => ({",
                "  metadata: { paneId: '%1', epoch: 4, start, end, kind: 'live' },",
                "  data: new Uint8Array(end - start).buffer,",
                "});",
                "(async () => {",
                "  assert.equal(await handleTerminalBinary([item(10, 12), item(12, 15)], 9), true);",
                "  assert.equal(terminalRevision, 15);",
                "  assert.deepEqual(writes, [5]);",
                "  assert.equal(await handleTerminalBinary([item(15, 16)], 8), false);",
                "  assert.equal(terminalRevision, 15);",
                "  assert.deepEqual(writes, [5]);",
                "})().catch((error) => { console.error(error); process.exit(1); });",
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_passkey_ceremony_runs_off_chain_and_is_timeout_abortable(self):
        router = section(
            APP_JS,
            "  async function handleServerMessage(payload, generation, messageSocket, queueState)",
            '    if (String(payload.type || "").startsWith("fs-"))',
        )
        webauthn_branch = router[router.index("window.MobileTerminalPasskeys") :]
        self.assertIn("startPasskeyCeremony(payload, messageSocket, generation);", webauthn_branch)
        self.assertNotIn("await window.MobileTerminalPasskeys.handleMessage", webauthn_branch)
        ceremony = section(
            APP_JS,
            "  function startPasskeyCeremony(",
            "  async function handleServerMessage(payload, generation, messageSocket, queueState)",
        )
        self.assertIn("passkeyCeremonyPromise = trackedCeremony;", ceremony)
        self.assertIn("await window.MobileTerminalPasskeys.handleMessage", ceremony)
        self.assertIn("cancelPasskeyCeremony();", ceremony)
        self.assertIn("const CEREMONY_TIMEOUT_MS = 90000;", PASSKEY_JS)
        self.assertIn("root.setTimeout(() => controller.abort(), CEREMONY_TIMEOUT_MS)", PASSKEY_JS)
        close_handler = section(APP_JS, '    socket.addEventListener("close"', "  function setPasskeyRetryUi")
        self.assertIn("cancelPasskeyCeremony();", close_handler)
        background = section(
            APP_JS,
            '  document.addEventListener("visibilitychange", () => {',
            '  document.addEventListener("keydown"',
        )
        self.assertGreaterEqual(background.count("cancelPasskeyCeremony();"), 2)

    def test_indexeddb_open_and_transaction_time_out_to_no_device_key(self):
        idb_source = section(APP_JS, "  function idbOpen()", "  async function ensureDeviceKey")
        self.assertIn('new Error("IndexedDB open timed out")', idb_source)
        self.assertIn('new Error("IndexedDB transaction timed out")', idb_source)
        self.assertIn("req.result.close();", idb_source)
        self.assertIn("db.close();", idb_source)
        resume = section(APP_JS, "  function resumeApplication()", "  function scheduleConnect")
        self.assertIn("resumeDecisionPromise === trackedDecision", resume)
        script = "\n".join(
            (
                'const assert = require("node:assert/strict");',
                "const window = globalThis;",
                "const IDB_TIMEOUT_MS = 5;",
                'const DEVICE_KEY_DB = "test";',
                'const DEVICE_KEY_STORE = "keys";',
                "let indexedDB;",
                'let loginRealm = "";',
                "function deviceKeySupported() { return true; }",
                'function deviceKeyId() { return "key"; }',
                idb_source,
                "(async () => {",
                "  indexedDB = { open: () => ({}) };",
                "  assert.equal(await loadDeviceKey(), null);",
                "  let closed = 0;",
                "  const db = {",
                "    objectStoreNames: { contains: () => true },",
                "    close: () => { closed += 1; },",
                "    transaction: () => {",
                "      const tx = {",
                "        objectStore: () => ({ get: () => ({}) }),",
                "        abort: () => {},",
                "      };",
                "      return tx;",
                "    },",
                "  };",
                "  indexedDB = { open: () => {",
                "    const request = { result: db };",
                "    setTimeout(() => request.onsuccess(), 0);",
                "    return request;",
                "  } };",
                "  assert.equal(await loadDeviceKey(), null);",
                "  assert.equal(closed, 1);",
                "})().catch((error) => { console.error(error); process.exit(1); });",
            )
        )
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
