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

    def test_both_drag_routes_clamp_before_mousemove_without_touch_semantic_changes(self):
        handle_drag = app_section(
            "  function attachHandleDrag(handle)",
            "  // Select the word under a point",
        )
        self.assertGreaterEqual(handle_drag.count("clampTerminalSelectionDragPoint("), 3)
        self.assertIn('dispatchTerminalMouse("mousemove", point.x, point.y, 1);', handle_drag)
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
        self.assertIn("event.preventDefault();", terminal_drag)
        self.assertIn("{ passive: false }", terminal_drag)

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
            'window.visualViewport?.addEventListener("resize", () => {',
            'window.visualViewport?.addEventListener("scroll", updateViewportMetrics);',
            'window.addEventListener("resize", () => {',
            'window.addEventListener("orientationchange", () => {',
        ):
            self.assertIn(marker, APP_JS)
        self.assertGreaterEqual(APP_JS.count("scheduleTerminalSelectionUISync("), 12)
        fit = app_section("  function fitTerminal(", "  function cancelTouchInertia()")
        self.assertIn("scheduleTerminalSelectionUISync();", fit)
        viewport = extract_function("updateViewportMetrics")
        self.assertIn("scheduleTerminalSelectionUISync();", viewport)

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

        handles = app_section(
            "  function ensureSelectionHandles()",
            "  // Reposition the handles",
        )
        self.assertIn("chips.append(copy, paste);", handles)
        self.assertIn('btn.addEventListener(\n        "touchend"', handles)
        self.assertIn("touchStartPoint.moved = true;", handles)
        self.assertIn("{ passive: false }", handles)
        guard = extract_function("guardTerminalHelperTextarea")
        self.assertIn("helper.readOnly = true;", guard)
        self.assertIn('helper.setAttribute("inputmode", "none");', guard)


if __name__ == "__main__":
    unittest.main()
