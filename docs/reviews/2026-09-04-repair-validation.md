# Adversarial review repairs — September 4, 2026

This records the implementation following the [original review](2026-09-04-adversarial-review.md). Changes are in the checkout; the live application and user hook configurations have not been deployed or restarted.

## Repairs

| Review findings | Result |
| --- | --- |
| 1: fresh-output copy | Normal selections carry the actual selected cells and wrap flags. The server verifies them against current rows and retains the later-change fences. Fresh output and new scrollback copy on the first attempt. |
| 2–3: Codex authority and renderer drift | Context metadata no longer fails message parsing. Codex user hooks install in `~/.codex/hooks.json`, with a tagged-entry migration from the incorrect nested path. Current renderer support is tested against actual CLI captures, described below. |
| 4: paste corruption | Clipboard-button and native-event paths preserve spacing and line breaks. Mobile paste consistently uses the composer. |
| 5: paste mode | Seeds reset and restore known paste mode. On older tmux, observations are tied to process identity and discarded after the last observer disconnects. Unknown multiline input requires explicit review. |
| 6: repaint resize freeze | Resizes have a 350 ms total quiet budget. Busy panes retain their browser buffer and replay every held byte in order, including output held by a superseded selection. Exact copy waits for stabilization. |
| 7: geometry disagreement | Browser and backend share the 20×6 minimum. Delivered geometry reflects the backend acknowledgment, and a constrained viewport explains how to make space. |
| 8: URL corruption | Removed the heuristic that joined separate authored lines merely because they resembled a URL. |
| 9–10: touch and Unicode | Removed global rapid-tap cancellation. Word selection uses terminal cells; composer offsets explicitly convert UTF-16 and editing uses grapheme boundaries. |
| 11–12: failed operations and editor paste | Failed Copy/To-tab retains recoverable selection or text. Image paste only enters the terminal workflow from its own input targets. |
| 13: scroll intent | Null targets remain absent. Ordinary seeds retain reading position or following output, and replay preserves soft-wrap boundaries. |
| 14: duplicate query replies | tmux owns terminal query responses; xterm parser handlers prevent duplicate replies while keeping genuine input, including reply-shaped pasted text. |
| 15: authentication bootstrap | HTTP localhost works for loopback development. Unsupported HTTP/IP origins show an actionable message, and setup documentation explains the HTTPS hostname requirement. |

Further submission review found that staged drafts could cross tab boundaries, clear before acknowledgment, or send twice during overlapping reconnect requests. Staged submission is session-bound, acknowledged, and deduplicated; concurrent submission handlers serialize the transaction. Drafts remain available on rejected or uncertain delivery.

## Current CLI evidence

The recorder in `tests/tools/record_resumed_provider_fixtures.py` resumes synthetic saved conversations using disposable homes and private tmux sockets. It submits no prompt and configures an unused loopback model endpoint. The committed fixtures contain only synthetic assistant output.

There are twelve captures: Codex `0.153.3` and Claude `2.1.261`, each at 60 and 100 columns, covering prose, authored hard breaks, headings, emphasis, lists, emoji/CJK, inline code, and fenced code. Current Codex differs from the older renderer in visible heading markers, list markers and spacing, and inline-code color. Version-specific profiles account for the recorded differences and retain strict text/style verification.

The hook lifecycle test launches a synthetic provider process in private tmux, invokes installed hook commands, and verifies binding creation, live process identity, and closure. It does not establish that an existing real Codex session has approved or executed those hooks. Codex requires trust of the exact installed hook definition; the installer does not grant that trust.

An additional actual Codex test discovered the corrected hook file, saved four trusted definitions through its normal review UI, and confirmed the hooks feature was enabled. A subsequent synthetic resume did not invoke a diagnostic wrapper placed before the helper's imports or parsing. Thus the remaining uncertainty is event dispatch, not a demonstrated helper rejection. Real fresh-session binding delivery remains unverified; current Codex provider-exact copying still depends on a valid binding.

## Reproduction and limits

Run `scripts/test.sh --all` for isolated unit/integration tests and syntax checks. Run `npm run test:browser` for real Chromium, backend, WebSocket, passkey, and tmux regressions. Install Chromium with `npx playwright install chromium` if needed. Neither suite requires provider credentials or operates on live tmux sessions.

- tmux 3.6 cannot reveal paste mode after an observation gap. The app treats that state as unknown until the application announces it again. It does not silently treat pasted newlines as Enter keys.
- A busy initial connection still uses the existing degraded snapshot fallback; exact copying remains unavailable until a stable snapshot is established.
- Renderer fixtures cover completed assistant messages in the recorded layouts. They do not establish universal compatibility with streaming states, all themes, future versions, or current user-echo layouts.
- Locally staged drafts survive tab switches, renames, and reconnects in the open page; they are not persisted across page reload. Submission retry deduplication covers the running backend's last 64 successful requests per session. It serializes staged retries; ordinary input from another connected terminal can still interleave.
- Chromium touch emulation exercises the DOM and protocol paths. Actual iPhone/Safari keyboard animation and system clipboard UI still require device validation.

## Final validation

- `scripts/test.sh --all`: **500 tests passed** in 23.120 seconds, plus Python, JavaScript, and shell syntax checks. The reviewed baseline had 460 tests.
- `npm run test:browser`: **14 tests passed** in 40.797 seconds, with no uncaught browser errors. Fixtures used real xterm, WebSockets, a disposable backend, private tmux, Chromium touch/desktop modes, and virtual WebAuthn.
- Browser coverage includes first-attempt fresh/history copy, failed-copy retention, soft wraps before/after reseeding, CJK word selection, focus-independent paste, rapid settings taps, minimum geometry and constrained-area notice, reading/follow intent, resizing during continuous repaint, file Save and image-paste isolation, one terminal query response, unknown-mode review after reconnect, exact bracketed paste bytes, and staged multiline paste through tab switching, cancellation, and acknowledged submission.
- Busy browser resize is required to reopen within two seconds while repaint continues; the private-tmux integration test requires less than 1.5 seconds. Byte-fence tests check no held output is dropped or duplicated.
- All twelve installed-renderer captures replay successfully, with negative style-mismatch and partial-selection coverage.
- Updated source probes pass for Codex metadata, current renderer profiles, authored URL breaks, and UTF-16 cursor conversion. `git diff --check` passes.

Three legacy full-suite expectations were updated for the new seed replay branch, null scroll target, and corrected hook location. An unbounded fake-pane test wait was also bounded after exposing a fixture setup failure; no failing check was disabled.
