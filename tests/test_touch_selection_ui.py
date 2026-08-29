from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text()
STYLES_CSS = (ROOT / "static" / "styles.css").read_text()


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


def css_rule(selector):
    match = re.search(rf"^{re.escape(selector)} \{{.*?^\}}$", STYLES_CSS, re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError(f"CSS rule {selector} not found")
    return match.group(0)


class TouchSelectionUITest(unittest.TestCase):
    def run_node_source(self, script):
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def run_node(self, functions, assertions):
        script = "\n".join(
            [
                'const assert = require("node:assert/strict");',
                *(extract_function(name) for name in functions),
                assertions,
            ]
        )
        self.run_node_source(script)

    def test_activity_reports_throttle_keyboard_and_record_only_user_interaction(self):
        script = "\n".join(
            [
                'const assert = require("node:assert/strict");',
                "const ACTIVITY_REPORT_INTERVAL_MS = 1000;",
                "const FORCED_ACTIVITY_DEDUPE_MS = 250;",
                "let now = 100;",
                "global.performance = { now: () => now };",
                "const WebSocket = { OPEN: 1 };",
                "const sent = [];",
                "let socket = { readyState: WebSocket.OPEN, send: (value) => sent.push(JSON.parse(value)) };",
                "let lastActivityReportAt = -Infinity;",
                "let lastForcedActivityReportAt = -Infinity;",
                "let recordedInteractions = 0;",
                "function recordUserInteraction() { recordedInteractions += 1; }",
                extract_function("reportActivity"),
                extract_function("reportForcedActivity"),
                "assert.equal(reportActivity(), true);",
                'assert.deepEqual(sent, [{ type: "activity" }]);',
                "now = 200; assert.equal(reportForcedActivity(), true);",
                'assert.deepEqual(sent[1], { type: "activity", force: true });',
                "assert.equal(recordedInteractions, 1);",
                "now = 300; assert.equal(reportForcedActivity(), false);",
                "assert.equal(recordedInteractions, 2);",
                "now = 450; assert.equal(reportForcedActivity(false), true);",
                "assert.equal(recordedInteractions, 2);",
                "now = 1099; assert.equal(reportActivity(), false);",
                "now = 1450; assert.equal(reportActivity(), true);",
                "socket.readyState = 0; now = 2500; assert.equal(reportForcedActivity(), false);",
                "assert.equal(recordedInteractions, 3);",
                "assert.equal(sent.length, 4);",
            ]
        )
        self.run_node_source(script)

    def test_terminal_resize_delivery_waits_replays_and_deduplicates(self):
        script = "\n".join(
            [
                'const assert = require("node:assert/strict");',
                "const WebSocket = { OPEN: 1 };",
                "const sent = [];",
                "let socket = { readyState: 0 };",
                "let terminalAuthoritative = false;",
                "let desiredTerminalCols = 0;",
                "let desiredTerminalRows = 0;",
                "let deliveredTerminalCols = 0;",
                "let deliveredTerminalRows = 0;",
                "let terminalResizeDirty = false;",
                "let terminalResizePending = false;",
                "function recordViewportForensics() {}",
                "function sendMessage(payload) {",
                "  if (!socket || socket.readyState !== WebSocket.OPEN) return false;",
                "  sent.push(payload);",
                "  return true;",
                "}",
                extract_function("setDesiredTerminalSize"),
                extract_function("resetDeliveredTerminalSize"),
                extract_function("flushTerminalResize"),
                "assert.equal(setDesiredTerminalSize(80, 24), true);",
                "assert.equal(flushTerminalResize(), false);",
                "assert.deepEqual([terminalResizeDirty, terminalResizePending], [true, true]);",
                "assert.deepEqual([deliveredTerminalCols, deliveredTerminalRows], [0, 0]);",
                "socket.readyState = WebSocket.OPEN;",
                "assert.equal(flushTerminalResize(), false);",
                "assert.equal(sent.length, 0);",
                "terminalAuthoritative = true;",
                "assert.equal(flushTerminalResize(), true);",
                'assert.deepEqual(sent, [{ type: "resize", cols: 80, rows: 24 }]);',
                "assert.deepEqual([deliveredTerminalCols, deliveredTerminalRows], [80, 24]);",
                "assert.equal(flushTerminalResize(), false);",
                "assert.equal(sent.length, 1);",
                "resetDeliveredTerminalSize();",
                "assert.equal(flushTerminalResize(), true);",
                "assert.equal(sent.length, 2);",
                "socket.readyState = 0;",
                "assert.equal(setDesiredTerminalSize(100, 30), true);",
                "assert.equal(flushTerminalResize(), false);",
                "assert.deepEqual([deliveredTerminalCols, deliveredTerminalRows], [80, 24]);",
                "socket.readyState = WebSocket.OPEN;",
                "assert.equal(flushTerminalResize(), true);",
                'assert.deepEqual(sent[2], { type: "resize", cols: 100, rows: 30 });',
                "assert.equal(flushTerminalResize(), false);",
                "assert.equal(sent.length, 3);",
            ]
        )
        self.run_node_source(script)

    def test_activity_sources_exclude_pointer_moves_and_generated_terminal_replies(self):
        pointer_section = app_section(
            'document.addEventListener("pointerdown", reportForcedActivity',
            "  let lastTouchEndAt = 0;",
        )
        self.assertIn('document.addEventListener("pointerdown", reportForcedActivity', pointer_section)
        self.assertIn('document.addEventListener("touchstart", reportForcedActivity', pointer_section)
        self.assertIn("capture: true, passive: true", pointer_section)
        self.assertNotIn("pointermove", pointer_section)
        self.assertNotIn("touchmove", pointer_section)

        terminal_input = app_section("  term.onKey(() => reportActivity());", "  term.onScroll(")
        self.assertIn("term.onKey(() => reportActivity());", terminal_input)
        on_data = terminal_input[terminal_input.index("term.onData") :]
        self.assertNotIn("reportActivity", on_data)

        send_message = extract_function("sendMessage")
        self.assertIn('"composer-sync"', send_message)
        self.assertIn('"composer-enter"', send_message)
        self.assertIn('"composer-force-clear"', send_message)
        self.assertNotIn('"composer-refresh"', send_message)
        self.assertNotIn('"composer-reset"', send_message)
        self.assertNotIn('"composer-semantic-sync"', send_message)

        focus_handler = app_section(
            '  window.addEventListener("focus", () => {',
            '  document.addEventListener("visibilitychange",',
        )
        self.assertIn("reportForcedActivity(false);", focus_handler)

    def test_overlay_bounds_intersect_terminal_screen_viewport_and_safe_area(self):
        self.run_node(
            ["computeSelectionOverlayBounds"],
            r'''
const bounds = computeSelectionOverlayBounds(
  { left: 0, top: 0, right: 400, bottom: 800 },
  { left: 10, top: 20, right: 390, bottom: 780 },
  { left: 30, top: 40, right: 330, bottom: 640 },
  { top: 7, right: 11, bottom: 13, left: 5 },
);
assert.deepEqual(bounds, {
  left: 35, top: 47, right: 319, bottom: 627, width: 284, height: 580,
});
const empty = computeSelectionOverlayBounds(
  { left: 0, top: 0, right: 10, bottom: 10 },
  { left: 20, top: 20, right: 30, bottom: 30 },
  { left: 0, top: 0, right: 100, bottom: 100 },
  { top: 0, right: 0, bottom: 0, left: 0 },
);
assert.deepEqual(empty, { left: 20, top: 20, right: 20, bottom: 20, width: 0, height: 0 });
''',
        )

    def test_toolbar_prefers_above_flips_below_and_clamps_all_edges(self):
        self.run_node(
            ["clampSelectionValue", "computeSelectionToolbarPlacement"],
            r'''
const bounds = { left: 10, top: 20, right: 310, bottom: 500, width: 300, height: 480 };
assert.deepEqual(
  computeSelectionToolbarPlacement(
    { left: 100, top: 200, right: 220, bottom: 230 },
    { width: 120, height: 44 }, bounds, 8,
  ),
  { left: 100, top: 148, side: "above", maxWidth: 300, overflowX: false },
);
assert.deepEqual(
  computeSelectionToolbarPlacement(
    { left: 0, top: 22, right: 30, bottom: 50 },
    { width: 120, height: 44 }, bounds, 8,
  ),
  { left: 10, top: 58, side: "below", maxWidth: 300, overflowX: false },
);
const squeezed = computeSelectionToolbarPlacement(
  { left: 100, top: 40, right: 120, bottom: 70 },
  { width: 80, height: 50 },
  { left: 10, top: 20, right: 210, bottom: 100, width: 200, height: 80 },
  8,
);
assert.deepEqual(squeezed, {
  left: 70, top: 50, side: "below", maxWidth: 200, overflowX: false,
});
''',
        )

    def test_narrow_toolbar_stays_one_safe_left_anchored_group(self):
        self.run_node(
            ["clampSelectionValue", "computeSelectionToolbarPlacement"],
            r'''
const placement = computeSelectionToolbarPlacement(
  { left: 30, top: 90, right: 50, bottom: 110 },
  { width: 180, height: 44 },
  { left: 12, top: 10, right: 92, bottom: 220, width: 80, height: 210 },
  8,
);
assert.deepEqual(placement, {
  left: 12, top: 38, side: "above", maxWidth: 80, overflowX: true,
});
''',
        )

    def test_handle_stems_keep_edge_boundaries_while_targets_and_knobs_clamp(self):
        self.run_node(
            ["clampSelectionValue", "computeSelectionHandlePlacement"],
            r'''
const bounds = { left: 10, top: 20, right: 210, bottom: 220, width: 200, height: 200 };
const first = computeSelectionHandlePlacement(10, 20, 38, bounds, "start", 44, 16);
assert.deepEqual(first, {
  left: 10, top: 20, width: 44, height: 44,
  stemX: 10, stemTop: 20, stemHeight: 18,
  knobLeft: 10, knobTop: 20, knobWidth: 16, knobHeight: 16,
});
const last = computeSelectionHandlePlacement(210, 202, 220, bounds, "end", 44, 16);
assert.deepEqual(last, {
  left: 166, top: 176, width: 44, height: 44,
  stemX: 210, stemTop: 202, stemHeight: 18,
  knobLeft: 194, knobTop: 204, knobWidth: 16, knobHeight: 16,
});
''',
        )

    def test_drag_point_clamping_covers_every_screen_edge(self):
        self.run_node(
            ["clampSelectionValue", "clampSelectionPoint"],
            r'''
const bounds = { left: 10, top: 20, right: 210, bottom: 220 };
assert.deepEqual(clampSelectionPoint({ x: -5, y: 400 }, bounds), { x: 10, y: 220 });
assert.deepEqual(clampSelectionPoint({ x: 500, y: -4 }, bounds), { x: 210, y: 20 });
assert.deepEqual(clampSelectionPoint({ x: 80, y: 90 }, bounds), { x: 80, y: 90 });
''',
        )

    def test_magnifier_motion_uses_euclidean_ewma_thresholds_and_hysteresis(self):
        self.run_node(
            [
                "createSelectionDragMotionState",
                "sampleSelectionDragMotion",
                "decideSelectionMagnifierVisibility",
            ],
            r'''
const initial = createSelectionDragMotionState({ x: 0, y: 0 }, 0);
let motion = sampleSelectionDragMotion(initial, { x: 3, y: 4 }, 100);
assert.equal(motion.instantaneousSpeed, 0.05);
assert.equal(motion.speed, 0.017499999999999998);
assert.equal(decideSelectionMagnifierVisibility(initial, 1000, false), false);
assert.equal(decideSelectionMagnifierVisibility(motion, 199, false), false);
assert.equal(decideSelectionMagnifierVisibility(motion, 200, false), true);

const medium = {
  ...motion, sampledAt: 200, instantaneousSpeed: 0.3, speed: 0.3,
  lowSpeedSince: null, lastMeaningfulAt: 200,
};
assert.equal(decideSelectionMagnifierVisibility(medium, 200, true), true);
assert.equal(decideSelectionMagnifierVisibility(medium, 200, false), false);
const slowBoundary = {
  ...motion, sampledAt: 200, instantaneousSpeed: 0.18, speed: 0.18,
  lowSpeedSince: 100, lastMeaningfulAt: 200,
};
assert.equal(decideSelectionMagnifierVisibility(slowBoundary, 200, false), true);
const hideBoundary = {
  ...motion, sampledAt: 200, instantaneousSpeed: 0.45, speed: 0.2,
};
assert.equal(decideSelectionMagnifierVisibility(hideBoundary, 200, true), false);

const fast = sampleSelectionDragMotion(motion, { x: 103, y: 4 }, 110);
assert.equal(fast.instantaneousSpeed, 10);
assert.equal(fast.speed, motion.speed * 0.65 + 10 * 0.35);
assert.equal(decideSelectionMagnifierVisibility(fast, 110, true), false);
''',
        )

    def test_magnifier_requires_cumulative_meaningful_movement_and_ignores_jitter(self):
        self.run_node(
            ["createSelectionDragMotionState", "sampleSelectionDragMotion"],
            r'''
const initial = createSelectionDragMotionState({ x: 20, y: 30 }, 10);
const jitter = sampleSelectionDragMotion(initial, { x: 20.3, y: 30.4 }, 40);
assert.equal(jitter.cumulativeMovement, 0);
assert.equal(jitter.hasMeaningfulMovement, false);
assert.deepEqual(jitter.point, initial.point);

let jitterDrift = createSelectionDragMotionState({ x: 0, y: 0 }, 0);
for (let step = 1; step <= 10; step += 1) {
  jitterDrift = sampleSelectionDragMotion(jitterDrift, { x: step * 0.5, y: 0 }, step * 10);
}
assert.equal(jitterDrift.cumulativeMovement, 0);
assert.equal(jitterDrift.hasMeaningfulMovement, false);

let cumulative = createSelectionDragMotionState({ x: 0, y: 0 }, 0);
cumulative = sampleSelectionDragMotion(cumulative, { x: 2, y: 0 }, 20);
assert.equal(cumulative.cumulativeMovement, 2);
assert.equal(cumulative.hasMeaningfulMovement, false);
cumulative = sampleSelectionDragMotion(cumulative, { x: 4, y: 0 }, 40);
assert.equal(cumulative.cumulativeMovement, 4);
assert.equal(cumulative.hasMeaningfulMovement, true);
assert.equal(cumulative.lastMeaningfulAt, 40);
assert.deepEqual(cumulative.point, { x: 4, y: 0 });
cumulative = sampleSelectionDragMotion(cumulative, { x: 5, y: 0 }, 50);
assert.equal(cumulative.instantaneousSpeed, 0.1);
assert.equal(cumulative.lastMeaningfulAt, 50);
assert.deepEqual(cumulative.point, { x: 5, y: 0 });

const boundary = sampleSelectionDragMotion(initial, { x: 24, y: 30 }, 50);
assert.equal(boundary.hasMeaningfulMovement, true);
assert.equal(boundary.cumulativeMovement, 4);
''',
        )

    def test_magnifier_slow_dwell_and_pause_deadlines_are_deterministic(self):
        self.run_node(
            [
                "createSelectionDragMotionState",
                "sampleSelectionDragMotion",
                "decideSelectionMagnifierVisibility",
                "selectionMagnifierDwellDeadline",
            ],
            r'''
const still = createSelectionDragMotionState({ x: 0, y: 0 }, 25);
assert.equal(selectionMagnifierDwellDeadline(still, 25), null);
assert.equal(decideSelectionMagnifierVisibility(still, 1000, false), false);
const jitter = sampleSelectionDragMotion(still, { x: 0.3, y: 0.4 }, 1000);
assert.equal(jitter.cumulativeMovement, 0);
assert.equal(selectionMagnifierDwellDeadline(jitter, 1000), null);
assert.equal(decideSelectionMagnifierVisibility(jitter, 2000, false), false);
const almost = sampleSelectionDragMotion(still, { x: 3.9, y: 0 }, 100);
assert.equal(almost.hasMeaningfulMovement, false);
assert.equal(selectionMagnifierDwellDeadline(almost, 1000), null);
assert.equal(decideSelectionMagnifierVisibility(almost, 2000, false), false);

const slow = sampleSelectionDragMotion(still, { x: 4, y: 0 }, 125);
assert.equal(selectionMagnifierDwellDeadline(slow, 125), 225);
assert.equal(decideSelectionMagnifierVisibility(slow, 224, false), false);
assert.equal(decideSelectionMagnifierVisibility(slow, 225, false), true);

const moved = sampleSelectionDragMotion(still, { x: 20, y: 0 }, 35);
assert.equal(moved.lowSpeedSince, null);
assert.equal(selectionMagnifierDwellDeadline(moved, 35), 135);
assert.equal(decideSelectionMagnifierVisibility(moved, 134, false), false);
assert.equal(decideSelectionMagnifierVisibility(moved, 135, false), true);
''',
        )

    def test_magnifier_generation_invalidates_stale_dwell_callbacks(self):
        functions = [
            "createSelectionDragMotionState",
            "sampleSelectionDragMotion",
            "decideSelectionMagnifierVisibility",
            "selectionMagnifierDwellDeadline",
            "scheduleSelectionDragFeedbackDwell",
            "applySelectionDragFeedbackDecision",
            "beginSelectionDragFeedback",
            "updateSelectionDragFeedback",
            "endSelectionDragFeedback",
        ]
        script = "\n".join(
            [
                'const assert = require("node:assert/strict");',
                "let now = 0;",
                "global.performance = { now: () => now };",
                "const timers = [];",
                "const window = {",
                "  setTimeout(callback, delay) { const timer = { callback, delay, cleared: false }; timers.push(timer); return timer; },",
                "  clearTimeout(timer) { timer.cleared = true; },",
                "  cancelAnimationFrame() {},",
                "};",
                "let selectionDragFeedback = null;",
                "let selectionDragFeedbackGeneration = 0;",
                "function ensureSelectionHandles() {}",
                "function hideSelectionMagnifier() {}",
                "function scheduleSelectionDragFeedbackFrame() {}",
                *(extract_function(name) for name in functions),
                "beginSelectionDragFeedback({ x: 10, y: 20 });",
                "assert.equal(timers.length, 0);",
                "now = 5; updateSelectionDragFeedback({ x: 10.3, y: 20.4 });",
                "assert.equal(timers.length, 0);",
                "now = 10; updateSelectionDragFeedback({ x: 12.3, y: 20.4 });",
                "assert.equal(selectionDragFeedback.motion.cumulativeMovement, 2);",
                "assert.equal(selectionDragFeedback.motion.hasMeaningfulMovement, false);",
                "assert.equal(timers.length, 0);",
                "now = 20; updateSelectionDragFeedback({ x: 14.3, y: 20.4 });",
                "assert.equal(selectionDragFeedback.motion.cumulativeMovement, 4);",
                "assert.equal(selectionDragFeedback.visible, false);",
                "assert.equal(timers.length, 1);",
                "assert.equal(timers[0].delay, 100);",
                "const stale = timers.at(-1);",
                "const staleGeneration = selectionDragFeedback.generation;",
                "endSelectionDragFeedback();",
                "assert.equal(selectionDragFeedback, null);",
                "now = 100; stale.callback();",
                "assert.equal(selectionDragFeedback, null);",
                "beginSelectionDragFeedback({ x: 30, y: 40 });",
                "assert.notEqual(selectionDragFeedback.generation, staleGeneration);",
            ]
        )
        self.run_node_source(script)

    def test_magnifier_placement_flips_and_clamps_all_edges_and_transform(self):
        self.run_node(
            [
                "clampSelectionValue",
                "computeSelectionMagnifierPlacement",
                "computeSelectionMagnifierTransform",
            ],
            r'''
const bounds = { left: 10, top: 20, right: 310, bottom: 500, width: 300, height: 480 };
const size = { width: 112, height: 72 };
assert.deepEqual(
  computeSelectionMagnifierPlacement({ x: 10, y: 25 }, size, bounds, 72),
  { left: 10, top: 61, width: 112, height: 72, side: "below" },
);
assert.deepEqual(
  computeSelectionMagnifierPlacement({ x: 310, y: 495 }, size, bounds, 72),
  { left: 198, top: 387, width: 112, height: 72, side: "above" },
);
assert.deepEqual(
  computeSelectionMagnifierPlacement({ x: 160, y: 250 }, size, bounds, 72),
  { left: 104, top: 142, width: 112, height: 72, side: "above" },
);
assert.deepEqual(
  computeSelectionMagnifierTransform(
    { x: 60, y: 70 },
    { left: 10, top: 20, right: 210, bottom: 220 },
    size,
    1.8,
  ),
  { translateX: -34, translateY: -54, scale: 1.8 },
);
''',
        )

    def test_both_drag_routes_clamp_before_mousemove_without_touch_semantic_changes(self):
        handle_drag = app_section(
            "  function attachHandleDrag(handle)",
            "  // Select the word under a point",
        )
        self.assertGreaterEqual(handle_drag.count("clampTerminalSelectionDragPoint("), 3)
        self.assertIn('dispatchTerminalMouse("mousemove", point.x, point.y, 1);', handle_drag)
        self.assertIn("beginSelectionDragFeedback(point);", handle_drag)
        self.assertIn("updateSelectionDragFeedback(point);", handle_drag)
        self.assertIn("endSelectionDragFeedback();", handle_drag)
        self.assertIn('handle.addEventListener(\n      "touchmove"', handle_drag)
        self.assertGreaterEqual(handle_drag.count("event.preventDefault();"), 2)
        self.assertGreaterEqual(handle_drag.count("{ passive: false }"), 4)

        terminal_drag = app_section(
            '    terminalRoot.addEventListener(\n      "touchmove"',
            "    const finishTouchScroll = (event) => {",
        )
        self.assertIn(
            "const point = clampTerminalSelectionDragPoint(touch.clientX, touch.clientY);",
            terminal_drag,
        )
        self.assertIn('dispatchTerminalMouse("mousemove", point.x, point.y, 1);', terminal_drag)
        self.assertIn("updateSelectionDragFeedback(point);", terminal_drag)
        self.assertIn("event.preventDefault();", terminal_drag)
        self.assertIn("{ passive: false }", terminal_drag)

    def test_magnifier_lifecycle_covers_drag_routes_and_cleanup_paths(self):
        activate = extract_function("activateTerminalSelection")
        finish = extract_function("finishTerminalSelectionPress")
        cancel = extract_function("cancelTerminalSelectionPress")
        clear = extract_function("clearTerminalSelectionUI")
        self.assertIn("beginSelectionDragFeedback(", activate)
        self.assertIn("endSelectionDragFeedback();", finish)
        self.assertIn("endSelectionDragFeedback();", cancel)
        self.assertIn("endSelectionDragFeedback();", clear)

        handles = app_section(
            "  function ensureSelectionHandles()",
            "  // Reposition the handles",
        )
        self.assertIn("await copyTerminalSelectionAndDismiss();", handles)
        self.assertIn("await pasteSelectionToRecentTabAndDismiss();", handles)
        self.assertIn('handle.addEventListener("touchend", finishHandleDrag', APP_JS)
        self.assertIn('handle.addEventListener("touchcancel", finishHandleDrag', APP_JS)

        to_tab = app_section(
            "  async function pasteSelectionToRecentTab()",
            "  // --- Touch text selection",
        )
        switch_profile = extract_function("switchProfile")
        switch_session = extract_function("switchSession")
        reset_interaction = extract_function("resetTerminalInteractionState")
        ready = app_section('    if (payload.type === "ready") {', '    if (payload.type === "tabs") {')
        selection_change = app_section("    term.onSelectionChange(() => {", "    wheelTarget.addEventListener(")
        self.assertIn("dismissTerminalSelection();", to_tab)
        self.assertIn(
            "clearTerminalSelectionUI();\n    terminalAuthoritative = false;\n"
            "    resetComposerTracking(true);",
            switch_profile,
        )
        self.assertIn("resetTerminalInteractionState();", switch_session)
        self.assertIn("resetTerminalInteractionState();", ready)
        self.assertIn("dismissTerminalSelection();", reset_interaction)
        self.assertIn("clearTerminalSelectionUI();", selection_change)

    def test_ready_uses_authoritative_scroll_ownership_and_keeps_updates_working(self):
        ready = app_section('    if (payload.type === "ready") {', '    if (payload.type === "tabs") {')
        self.assertIn(
            'Object.prototype.hasOwnProperty.call(payload, "paneLocalScroll")',
            ready,
        )
        self.assertIn("? payload.paneLocalScroll === true\n        : true;", ready)
        pane_scroll = app_section(
            '    if (payload.type === "pane-scroll") {',
            '    if (payload.type === "notice") {',
        )
        self.assertIn("activePaneLocalScroll = payload.local === true;", pane_scroll)

    def test_session_switch_teardown_cancels_all_scroll_pipeline_state(self):
        script = "\n".join(
            [
                'const assert = require("node:assert/strict");',
                "const events = [];",
                "const window = {",
                "  cancelAnimationFrame(id) { events.push(`frame:${id}`); },",
                "  clearTimeout(id) { events.push(`timeout:${id}`); },",
                "};",
                "let selectionTapCopy = { x: 1, y: 2 };",
                "let pendingScrollFrameId = 41;",
                "let pendingScrollLines = 9;",
                "let scrollLineRemainder = 0.75;",
                "let scrollRepaintTimer = 42;",
                "function dismissTerminalSelection() { events.push('selection'); }",
                "function cancelTouchInertia() { events.push('inertia'); }",
                extract_function("resetTerminalInteractionState"),
                "resetTerminalInteractionState();",
                "assert.equal(selectionTapCopy, null);",
                "assert.equal(pendingScrollFrameId, null);",
                "assert.equal(pendingScrollLines, 0);",
                "assert.equal(scrollLineRemainder, 0);",
                "assert.equal(scrollRepaintTimer, null);",
                "assert.deepEqual(events, ['selection', 'frame:41', 'timeout:42', 'inertia']);",
                "resetTerminalInteractionState();",
                "assert.deepEqual(events, [",
                "  'selection', 'frame:41', 'timeout:42', 'inertia', 'selection', 'inertia',",
                "]);",
            ]
        )
        self.run_node_source(script)

    def test_dismiss_selection_fully_tears_down_every_transient_state(self):
        script = "\n".join(
            [
                'const assert = require("node:assert/strict");',
                "const events = [];",
                "const window = {",
                "  clearTimeout(id) { events.push(`timeout:${id}`); },",
                "  cancelAnimationFrame(id) { events.push(`frame:${id}`); },",
                "};",
                "const term = { clearSelection() { events.push('clear-selection'); } };",
                "let termSel = { active: true, timer: 11 };",
                "let selectionTapCopy = { x: 1, y: 2 };",
                "let touchScrollState = { lastY: 3 };",
                "let terminalSelectionSyncFrameId = 44;",
                "let terminalSelectionSyncForced = true;",
                "let selectionDragFeedbackGeneration = 0;",
                "let selectionDragFeedback = { timerId: 22, frameId: 33 };",
                "let termSelHandles = { layer: { style: { display: 'block' } } };",
                "function hideSelectionMagnifier(clearSnapshot) {",
                "  events.push(`magnifier:${clearSnapshot}`);",
                "}",
                extract_function("endSelectionDragFeedback"),
                extract_function("clearTerminalSelectionUI"),
                extract_function("dismissTerminalSelection"),
                "dismissTerminalSelection();",
                "assert.equal(termSel, null);",
                "assert.equal(selectionTapCopy, null);",
                "assert.equal(touchScrollState, null);",
                "assert.equal(selectionDragFeedback, null);",
                "assert.equal(terminalSelectionSyncFrameId, null);",
                "assert.equal(terminalSelectionSyncForced, false);",
                "assert.equal(termSelHandles.layer.style.display, 'none');",
                "assert.deepEqual(events, [",
                "  'timeout:11', 'frame:44', 'clear-selection', 'timeout:22', 'frame:33', 'magnifier:true',",
                "]);",
                "dismissTerminalSelection();",
                "assert.equal(termSel, null);",
                "assert.equal(selectionTapCopy, null);",
                "assert.equal(touchScrollState, null);",
            ]
        )
        self.run_node_source(script)

    def test_fresh_single_touch_resets_stale_selection_without_affecting_second_finger(self):
        self.run_node(
            ["resetStaleTerminalSelectionAtTouchStart"],
            r'''
let termSel = { active: true };
let dismissals = 0;
function dismissTerminalSelection() { dismissals += 1; termSel = null; }
resetStaleTerminalSelectionAtTouchStart({ touches: [{ identifier: 1 }] });
assert.equal(dismissals, 1);
assert.equal(termSel, null);

termSel = { active: true };
resetStaleTerminalSelectionAtTouchStart({
  touches: [{ identifier: 1 }, { identifier: 2 }],
});
assert.equal(dismissals, 1);
assert.deepEqual(termSel, { active: true });
''',
        )
        touchstart = app_section(
            '    terminalRoot.addEventListener(\n      "touchstart"',
            '    terminalRoot.addEventListener(\n      "touchmove"',
        )
        self.assertLess(
            touchstart.index("resetStaleTerminalSelectionAtTouchStart(event);"),
            touchstart.index("if (event.touches.length >= 2)"),
        )

    def test_long_press_and_double_tap_flows_remain_intact(self):
        press = app_section(
            "  function beginTerminalSelectionPress(touch)",
            "  function persistActiveSession(sessionName)",
        )
        for marker in (
            "cancelTerminalSelectionPress();",
            "window.setTimeout(activateTerminalSelection, TERM_LONGPRESS_MS)",
            "termSel.active = true;",
            "touchScrollState = null; // this contact is a selection, not a scroll",
            'dispatchTerminalMouse("mousedown", termSel.startX, termSel.startY, 2);',
            "beginSelectionDragFeedback({ x: termSel.startX, y: termSel.startY });",
        ):
            self.assertIn(marker, press)

        touchstart = app_section(
            '    terminalRoot.addEventListener(\n      "touchstart"',
            '    terminalRoot.addEventListener(\n      "touchmove"',
        )
        self.assertIn("now - lastTermTapAt < TERM_DOUBLETAP_MS", touchstart)
        self.assertIn("TERM_DOUBLETAP_DIST", touchstart)
        self.assertIn("termSel = { doubleTap: true };", touchstart)
        self.assertIn("composerInput.blur();", touchstart)
        self.assertIn("selectWordAt(touch.clientX, touch.clientY);", touchstart)
        self.assertIn("beginTerminalSelectionPress(touch);", touchstart)

    def test_touchcancel_uses_the_selection_finalizer(self):
        handlers = app_section(
            "    const finishTouchScroll = (event) => {",
            "  function installTabStripScrollHandlers()",
        )
        self.assertIn('const cancelled = event.type === "touchcancel";', handlers)
        self.assertIn("finishTerminalSelectionPress();", handlers)
        self.assertIn(
            'terminalRoot.addEventListener("touchend", finishTouchScroll',
            handlers,
        )
        self.assertIn(
            'terminalRoot.addEventListener("touchcancel", finishTouchScroll',
            handlers,
        )

    def test_magnifier_clones_only_renderer_rows_and_raf_coalesces_refresh(self):
        snapshot = extract_function("refreshSelectionMagnifierSnapshot")
        frame = extract_function("scheduleSelectionDragFeedbackFrame")
        dirty = extract_function("markSelectionDragFeedbackDirty")
        self.assertIn('terminalElement.querySelector(".xterm-rows")', snapshot)
        self.assertIn("const clone = rows.cloneNode(true);", snapshot)
        self.assertNotIn("terminalElement.cloneNode", snapshot)
        self.assertIn('clone.querySelectorAll(".xterm-cursor").forEach((element) => {', snapshot)
        cursor_cleanup_match = re.search(
            r'clone\.querySelectorAll\("\.xterm-cursor"\)\.forEach\(\(element\) => \{'
            r"(?P<body>.*?)^    \}\);",
            snapshot,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(cursor_cleanup_match)
        cursor_cleanup = cursor_cleanup_match.group(0)
        self.assertIn('className.startsWith("xterm-cursor")', cursor_cleanup)
        self.assertIn("element.classList.remove(className);", cursor_cleanup)
        self.assertIn('attribute.name.includes("cursor")', cursor_cleanup)
        self.assertIn("element.removeAttribute(attribute.name);", cursor_cleanup)
        self.assertNotIn('attribute.name === "style"', cursor_cleanup)
        self.assertNotIn(".remove()", cursor_cleanup)
        self.assertNotIn("textContent", cursor_cleanup)

        self.run_node_source(
            "\n".join(
                [
                    'const assert = require("node:assert/strict");',
                    "const classes = new Set([",
                    '  "xterm-cursor", "xterm-cursor-block", "xterm-fg-42",',
                    "]);",
                    "classes.remove = classes.delete.bind(classes);",
                    "const removedAttributes = [];",
                    "const element = {",
                    "  classList: classes,",
                    "  attributes: [",
                    '    { name: "style" }, { name: "data-cursor-style" }, { name: "data-cell" },',
                    "  ],",
                    '  style: { color: "rgb(1, 2, 3)", letterSpacing: "0.5px" },',
                    '  textContent: "X",',
                    "  removeAttribute(name) {",
                    "    removedAttributes.push(name);",
                    '    if (name === "style") this.style = {};',
                    "  },",
                    '  remove() { throw new Error("cursor cell removed"); },',
                    "};",
                    cursor_cleanup_match.group("body"),
                    'assert.deepEqual([...classes], ["xterm-fg-42"]);',
                    'assert.deepEqual(removedAttributes, ["data-cursor-style"]);',
                    'assert.deepEqual(element.style, { color: "rgb(1, 2, 3)", letterSpacing: "0.5px" });',
                    'assert.equal(element.textContent, "X");',
                ]
            )
        )

        removed_nodes = re.search(
            r"clone\.querySelectorAll\(\n(?P<selector>.*?)\n    \)\.forEach"
            r"\(\(element\) => element\.remove\(\)\);",
            snapshot,
            re.DOTALL,
        )
        self.assertIsNotNone(removed_nodes)
        removed_selector = removed_nodes.group("selector")
        self.assertNotIn(".xterm-cursor", removed_selector)
        for forbidden in (
            "textarea",
            ".xterm-helper-textarea",
            ".xterm-selection-layer",
            ".term-select-layer",
            "[contenteditable]",
        ):
            self.assertIn(forbidden, removed_selector)
        self.assertIn('rows.closest(".xterm")', snapshot)
        self.assertIn("owner?.className", snapshot)
        self.assertIn("selectionDragFeedback.frameId !== null", frame)
        self.assertEqual(frame.count("window.requestAnimationFrame"), 1)
        self.assertIn("snapshotDirty = true;", dirty)
        self.assertIn("markSelectionDragFeedbackDirty();", extract_function("scheduleTerminalSelectionUISync"))

    def test_selection_sync_is_raf_coalesced_and_wired_to_layout_paths(self):
        scheduler = extract_function("scheduleTerminalSelectionUISync")
        self.assertIn("window.requestAnimationFrame", scheduler)
        self.assertIn("terminalSelectionSyncFrameId !== null", scheduler)
        self.assertEqual(scheduler.count("updateTerminalSelectionUI();"), 1)
        for marker in (
            "term.onRender(() => {",
            "term.onSelectionChange(() => {",
            "term.onScroll(() => {",
            "const layoutObserver = new ResizeObserver(() => {",
            'window.visualViewport?.addEventListener("resize", scheduleViewportSettlePasses);',
            'window.visualViewport?.addEventListener("scroll", scheduleViewportSettlePasses);',
            'window.addEventListener("resize", scheduleViewportSettlePasses);',
            'window.addEventListener("orientationchange", scheduleViewportSettlePasses);',
        ):
            self.assertIn(marker, APP_JS)
        self.assertGreaterEqual(APP_JS.count("scheduleTerminalSelectionUISync("), 12)
        fit = app_section("  function fitTerminal(", "  function cancelTouchInertia()")
        self.assertIn("scheduleTerminalSelectionUISync();", fit)
        viewport = extract_function("updateViewportMetrics")
        self.assertIn("scheduleTerminalSelectionUISync();", viewport)

    def test_viewport_geometry_tracks_focused_keyboard_across_ios_modes(self):
        self.run_node(
            ["normalizeViewportGeometry"],
            r"""
const KEYBOARD_THRESHOLD = 80;
const tallLayout = { width: 390, height: 436, offsetLeft: 0, offsetTop: 0 };
assert.deepEqual(
  normalizeViewportGeometry(tallLayout, 390, 759, true),
  {
    viewportWidth: 390,
    viewportHeight: 436,
    offsetLeft: 0,
    offsetTop: 0,
    keyboardInset: 323,
    keyboardOpen: true,
    layoutHeight: 759,
    hasVisualViewport: true,
  },
);
const shrunkenLayout = { width: 390, height: 436, offsetLeft: 0, offsetTop: 323 };
assert.deepEqual(
  normalizeViewportGeometry(shrunkenLayout, 390, 436, true),
  {
    viewportWidth: 390,
    viewportHeight: 436,
    offsetLeft: 0,
    offsetTop: 323,
    keyboardInset: 0,
    keyboardOpen: true,
    layoutHeight: 436,
    hasVisualViewport: true,
  },
);
assert.deepEqual(
  normalizeViewportGeometry(shrunkenLayout, 390, 759, false),
  {
    viewportWidth: 390,
    viewportHeight: 759,
    offsetLeft: 0,
    offsetTop: 0,
    keyboardInset: 0,
    keyboardOpen: false,
    layoutHeight: 759,
    hasVisualViewport: true,
  },
);
const focusedWithoutKeyboard = { width: 400, height: 730, offsetLeft: 3, offsetTop: 10 };
assert.deepEqual(
  normalizeViewportGeometry(focusedWithoutKeyboard, 430, 800, true),
  {
    viewportWidth: 400,
    viewportHeight: 730,
    offsetLeft: 3,
    offsetTop: 10,
    keyboardInset: 0,
    keyboardOpen: false,
    layoutHeight: 800,
    hasVisualViewport: true,
  },
);
""",
        )
        current = extract_function("currentViewportGeometry")
        self.assertIn("focusedElementAcceptsKeyboard()", current)
        applied = extract_function("applyViewportGeometry")
        self.assertIn('geometry.keyboardOpen ? "true" : "false"', applied)
        keyboard_shell = css_rule('body[data-keyboard-open="true"] .app-shell')
        self.assertIn("height: calc(var(--app-height) + var(--kb-accessory-gap));", keyboard_shell)
        keyboard_shortcuts = css_rule('body[data-keyboard-open="true"] .shortcut-bar')
        self.assertIn("padding-bottom: 0;", keyboard_shortcuts)
        blur = app_section(
            '  composerInput.addEventListener("blur", () => {',
            '  composerInput.addEventListener("input", () => {',
        )
        self.assertLess(
            blur.index("assertKeyboardClosedGeometry();"),
            blur.index("scheduleViewportSettlePasses({ immediate: false });"),
        )

    def test_viewport_changes_share_one_generation_settle_scheduler(self):
        scheduler = extract_function("scheduleViewportSettlePasses")
        self.assertIn("const generation = ++viewportSettleGeneration;", scheduler)
        self.assertIn("generation !== viewportSettleGeneration", scheduler)
        self.assertIn("window.clearTimeout(viewportSettleTimer);", scheduler)
        self.assertIn("VIEWPORT_SETTLE_DELAYS[passIndex]", scheduler)
        self.assertIn("const VIEWPORT_SETTLE_DELAYS = [80, 220, 420, 700, 1000, 1400];", APP_JS)
        for wiring in (
            'window.visualViewport?.addEventListener("scroll", scheduleViewportSettlePasses);',
            'document.addEventListener("focusin", scheduleViewportSettlePasses);',
            'document.addEventListener("focusout", () => {',
        ):
            self.assertIn(wiring, APP_JS)
        composer = app_section(
            '  composerInput.addEventListener("focus", () => {',
            '  composerInput.addEventListener("input", () => {',
        )
        self.assertEqual(composer.count("scheduleViewportSettlePasses"), 2)
        self.assertIn("scheduleViewportSettlePasses({ immediate: false });", composer)

    def test_toolbar_css_and_pointer_keyboard_guards_remain_intact(self):
        for name in ("top", "right", "bottom", "left"):
            self.assertIn(
                f"--safe-area-inset-{name}: env(safe-area-inset-{name}, 0px);",
                STYLES_CSS,
            )
        layer = css_rule(".term-select-layer")
        handle = css_rule(".term-select-handle")
        knob = css_rule(".term-select-knob")
        toolbar = css_rule(".term-select-chips")
        chip = css_rule(".term-select-chip")
        magnifier = css_rule(".term-select-magnifier")
        magnifier_viewport = css_rule(".term-select-magnifier-viewport")
        marker = css_rule(".term-select-magnifier::after")
        self.assertIn("pointer-events: none;", layer)
        self.assertIn("min-width: 44px;", handle)
        self.assertIn("min-height: 44px;", handle)
        self.assertIn("pointer-events: auto;", handle)
        self.assertIn("16px", knob)
        self.assertIn("display: flex;", toolbar)
        self.assertIn("flex-wrap: nowrap;", toolbar)
        self.assertIn("overflow-x: auto;", toolbar)
        self.assertIn("max-width: 100%;", toolbar)
        self.assertNotIn("transform:", toolbar)
        self.assertIn("flex: 0 0 auto;", chip)
        self.assertIn("white-space: nowrap;", chip)
        self.assertIn("width: 112px;", magnifier)
        self.assertIn("height: 72px;", magnifier)
        self.assertIn("border-radius:", magnifier)
        self.assertIn("background: #08131a;", magnifier)
        self.assertIn("box-shadow:", magnifier)
        self.assertIn("pointer-events: none;", magnifier)
        self.assertIn("overflow: hidden;", magnifier_viewport)
        self.assertIn("pointer-events: none;", magnifier_viewport)
        self.assertIn("left: 50%;", marker)
        self.assertIn("top: 50%;", marker)
        self.assertIn("pointer-events: none;", marker)
        self.assertIn("@media (prefers-reduced-motion: reduce)", STYLES_CSS)
        reduced_motion = re.search(
            r"@media \(prefers-reduced-motion: reduce\) \{.*?transition: none;.*?\n\}",
            STYLES_CSS,
            re.DOTALL,
        )
        self.assertIsNotNone(reduced_motion)

        handles = app_section(
            "  function ensureSelectionHandles()",
            "  // Reposition the handles",
        )
        self.assertIn("chips.append(copy, paste);", handles)
        self.assertIn('magnifier.setAttribute("aria-hidden", "true");', handles)
        self.assertIn("layer.append(start, end, chips, magnifier);", handles)
        self.assertIn('btn.addEventListener(\n        "touchend"', handles)
        self.assertIn(
            'btn.addEventListener("touchcancel", () => {\n'
            "        touchStartPoint = null;\n        dismissTerminalSelection();",
            handles,
        )
        self.assertIn("touchStartPoint.moved = true;", handles)
        self.assertIn("{ passive: false }", handles)
        guard = extract_function("guardTerminalHelperTextarea")
        self.assertIn("helper.readOnly = true;", guard)
        self.assertIn('helper.setAttribute("inputmode", "none");', guard)


if __name__ == "__main__":
    unittest.main()
