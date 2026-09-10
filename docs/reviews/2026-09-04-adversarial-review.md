# Adversarial review and UI walkthrough — September 4, 2026

Reviewed revision `213f6ff`. This is a diagnosis and reproduction report; production implementation and service configuration were not changed.

The subsequent repair work is tracked in [repair validation](2026-09-04-repair-validation.md). Findings below describe the reviewed revision, not the repaired checkout. The source probes at the end now assert corrected behavior.

**The Codex/Claude difference is real, and application defects amplify it.** Both live Codex panes used the normal buffer with mouse tracking off. All seven observed Claude panes used the alternate screen with SGR mouse tracking. The app routes these through different scrolling and copy protocols. Its normal-buffer copy bookkeeping rejects output that arrived after its earlier snapshot, which directly disadvantages Codex. Codex transcript parsing also rejects ordinary metadata records. These are independent of clipboard permissions.

## Evidence and scope

- `scripts/test.sh --all`: **460 tests passed**, including syntax checks, in about 20 seconds. Tests ran through the repository's isolated runner.
- Read-only live verification: service active, health 200, provider mode `prefer`, global tmux `window-size=latest`, **zero manual windows**, five installed Claude hook events and four Codex hook events. Process names and terminal flags confirmed the provider comparison without attaching to live panes.
- Installed commands reported Codex `0.153.3` and Claude `2.1.261`. These are installed-command versions, not an assertion that every running process uses the latest installed binary.
- Browser walkthrough used headless Chrome, real xterm and WebSockets, a disposable copy of the backend, virtual WebAuthn, private tmux, and synthetic text. Exercised touch mode, desktop mode, clipboard buttons, native paste event handling, selection, tabs, offline recovery, display settings, shortcuts, gestures, usage, authentication settings, local file editing, and viewport changes. Screenshots were visually inspected.
- Browser viewports included 390×844, 1280×800, 844×390, 390×240, 390×160, 430×844, and 320×568 at maximum settings. A short viewport tests constrained geometry; it does **not** reproduce Safari's actual keyboard animation.
- Existing copy forensic files were examined only for aggregate reason codes. Four recorded fallbacks included three `claude-binding-unavailable` and one `placement-ambiguous`. No live selected text, credentials, or transcript content is included here.
- No real iPhone/Safari session, paid provider generation, remote profile outage, or SSH editor was exercised. Provider TUI conclusions combine live mode metadata, actual source/parser checks, and synthetic terminal protocol probes. Native clipboard event injection tests handler behavior; it does not prove every OS clipboard UI behaves identically.

Evidence: [touch/geometry results](2026-09-04/adversarial-results.json), [fresh selection results](2026-09-04/selection-results.json), [desktop results](2026-09-04/desktop-results.json), [UI results](2026-09-04/ui-results.json), [terminal protocol results](2026-09-04/protocol-results.json). The first desktop fresh-selection probe lacked enough coordinate evidence; its returned text is not used as proof of incorrect copying. The dedicated fresh-session selection probe establishes the finding below.

## Prioritized findings

### 1. P1 — Normal-buffer copy rejects fresh output and becomes stuck on new history

**Confirmed in a fresh real terminal.** Print `FRESH OUTPUT`, select that completed line, and Copy: the first request returns `Terminal changed; select again.` The same selection immediately succeeds on the second request. Print another 100 lines and select `NEW-ROW-100`: repeated requests fail. An explicit history reseed restores copy.

At [server.py](../../server.py#L4235), selection builds `entry_stable_rows` from the row tracker's **previously observed snapshot** before observing the current snapshot. Changes that occurred before the user made the selection are treated as changes during selection. The first attempt updates the tracker, then fails the signature comparison at line 4344. For coordinates beyond the old tracker, the bounds check fails **before** updating it, so retrying cannot repair the state.

This is the strongest explanation for Codex's worse copy behavior: normal-buffer output grows into scrollback. Claude's alternate-screen path uses the bounded `clientRows` supplied with the selection and returns before this normal-buffer code.

**Repair:** Anchor normal selections to what the browser actually selected at that output revision, with row identity tracked through subsequent output. Preserve the check for changes during the operation; comparing against an arbitrary older snapshot is not that check. Add full-path cases for freshly written rows and history growth beyond the previous snapshot. Reseeding is a diagnostic workaround, not a satisfactory copy prerequisite.

### 2. P1 — Codex transcript parsing rejects valid `turn_context` metadata

[TranscriptIndex._parse_codex](../../provider_authority.py#L872) accepts outer record type `turn_context`, then requires `payload.type` to be a string. Ordinary `turn_context` records have fields such as `turn_id`, `cwd`, and `model`, without a `type`. The parser raises `schema-drift`, aborting and rolling back the index update.

A bounded schema-only sample of two current Codex transcripts contained 12 `turn_context` records; all 12 lacked `payload.type`. A synthetic record with that shape reproduces the failure. No transcript text was needed. Changing only the renderer version table will leave this failure intact.

**Repair:** Validate each outer record type independently. Ignore or parse context metadata without applying message-event schema requirements. Test realistic complete JSONL streams, including metadata between assistant messages.

### 3. P1 — Provider unwrapping is disabled by version drift and missing bindings

[Renderer tables](../../provider_authority.py#L316) accept only Claude `2.1.241` and Codex `0.147.0`, by exact string lookup. The installed versions both produce `unsupported-provider-version` in direct probes. In `prefer`, a rejected provider match falls back to terminal-rendered bytes; application-inserted newlines and gutters therefore remain.

Binding discovery adds another observed obstacle: local cache metadata contained nine Claude entries, including three marked active at supported version `2.1.241`, but **no Codex binding entries**, despite two live Codex panes. Cache activity is not proof of live binding validity. The precise reason Codex hooks have not produced bindings remains unresolved; installed hook counts alone do not demonstrate functioning authority. Older supported Claude bindings help explain why some Claude sessions can work better than newly installed versions.

**Repair:** Verify actual lifecycle event delivery, binding creation, and live process validation before claiming provider copy is healthy. Update version support only against recorded renderer fixtures. Expose a persistent, sanitized indication that exact provider copy is unavailable. Do not merely add new versions to the allowlist without validating their layout.

### 4. P1 — Paste changes bytes according to composer focus

**Confirmed using the actual touch clipboard button.** With clipboard `a  b\n  c`:

| User action | Sent data |
| --- | --- |
| Paste button, composer unfocused | Direct input `a b c` |
| Same button, composer focused | Composer text `a  b\n  c` |
| Desktop native paste with bracketed paste enabled | Bracketed text, preserving spaces and converting newlines to CR |

[normalizeDirectPtyPasteText](../../static/app.js#L1795) collapses repeated spaces, trims edges, and flattens newlines. [pasteFromClipboard](../../static/app.js#L10227) chooses that path based on focus. The loss is deliberate in the implementation but inconsistent with the user's expectation of paste; it corrupts code, quoted arguments, indentation, and formatted prompts. A single-line string such as `printf 'a  b'` is also changed.

The document-level native paste handler does not unify these paths: xterm's own clipboard listener stops propagation and sends through `term.onData` first.

**Repair:** Define one byte-preserving paste contract, with safe multiline delivery through the composer or negotiated bracketed paste. Make any flattening an explicit action. Test keyboard open/closed, desktop native paste, and To-tab against the same bytes.

### 5. P1 — Reconnect loses bracketed-paste mode and can turn pasted lines into Enter keys

**Confirmed with a raw-input fixture.** The running application enabled `CSI ?2004h`. Before reload, xterm reported bracketed paste enabled. After reload and complete synchronization, it reported disabled although the application had not disabled it. A native paste of `one\ntwo` reached the fixture as `one\rtwo`, with no bracket markers.

[PaneSnapshot.metadata](../../server.py#L1083) and [terminalModeSequence](../../static/app.js#L4890) omit bracketed-paste state. A newly constructed xterm starts with the mode off, and a snapshot cannot restore it. Conversely, the replay baseline does not explicitly clear that mode when switching from a pane that enabled it to one that did not, allowing state leakage in a reused terminal.

**Repair:** Include and restore the application's bracketed-paste state in the snapshot contract and reset it at pane boundaries. Audit other input modes for the same omission. Test reconnecting to a running raw-input application that does not reannounce modes on resize. This can cause accidental submissions in an interactive program, independently of the focus-dependent flattening bug.

### 6. P1 — Resizing a continuously repainting TUI stalls for approximately nine seconds

**Confirmed at 25 repaints/second.** Changing width from 390 to 430 while a fixture updated a spinner produced a `seed-start` to `seed-open` gap of **9,047 ms**. During reseeding, copy is disabled and output is held; input handling also queues behind the resize operation.

[TmuxBridge.reseed](../../server.py#L3916) tries three times to obtain quiet output. Each [quiet](../../server.py#L3321) call can wait three seconds. A TUI that redraws more frequently than the 140 ms quiet threshold predictably exhausts that budget before the degraded snapshot path runs. This occurs without a network problem or bad tmux `window-size` policy.

**Repair:** Bound total resize latency, coalesce geometry changes, and design a fenced snapshot/replay protocol for continuously active applications. Separate geometry/input responsiveness from the requirement to establish exact copy authority. Verify no lost/duplicated output and that queued user input remains ordered.

### 7. P1 — Client and server disagree on minimum rows

**Confirmed:** At 390×160, xterm and the client's desired/delivered state settled at **61×1**, while tmux remained **61×6**. The client nevertheless reported `terminalAuthoritative=true`. Its viewport was five rows above its buffer bottom.

[fitTerminal](../../static/app.js#L5416) accepts any positive proposed row count. The server [resize handler](../../server.py#L6091) clamps to a minimum of six. The client marks its requested dimensions delivered before learning what the server applied, and subsequent layout passes can shrink the replayed six-row terminal back to one. Strict selection geometry checks then reject requests.

**Repair:** Use one shared geometry constraint and distinguish requested, acknowledged, and rendered sizes. Handle an area too small for the minimum explicitly. Test landscape plus keyboard, maximum font/UI scale, and controls that reduce usable height. [Screenshot](2026-09-04/viewport-390x160.png).

### 8. P2 — URL “unwrapping” silently joins authored lines

[unwrap_selected_url](../../server.py#L1723) turns `https://example.com/docs\nNEXT` into `https://example.com/docsNEXT`. It checks whether subsequent lines contain whitespace, not whether those breaks were visual wraps. This modifies a URL followed by a separate one-word line, even after exact tmux extraction correctly identified a hard break.

**Repair:** Remove this heuristic from authoritative/raw copying, or require actual wrap/source provenance. Never infer a URL continuation from the absence of spaces. The existing positive URL tests do not cover this counterexample.

### 9. P2 — Global double-tap suppression drops legitimate button actions

**Confirmed:** Tap Settings, then Display approximately 150 ms later. Settings remains open; Display does not open. Waiting before the second tap works.

The document [touchend listener](../../static/app.js#L10892) calls `preventDefault()` for every second touch within 300 ms, regardless of target. That suppresses the synthesized click used by ordinary buttons. Selection chips have explicit touch handlers, but many settings/menu buttons rely on click.

**Repair:** Scope any gesture suppression to the relevant terminal gesture and target; do not cancel unrelated controls. Test quick navigation between distinct buttons, not only double taps on one element.

### 10. P2 — Unicode uses incompatible coordinate systems

Two independent confirmed defects:

- [selectWordAt](../../static/app.js#L3033) interprets terminal cell columns as JavaScript string indices. With `界 hello world`, tapping `hello` selected ` hell`. Wide characters, combining marks, and surrogate pairs invalidate that mapping.
- [syncComposerState](../../static/app.js#L3509) sends UTF-16 textarea offsets. [build_composer_sync_sequence](../../server.py#L2217) treats them as Python character counts. In `😀ab`, moving the browser cursor from offset 4 to offset 2 should move left twice; the server generates one Left key. Combining sequences introduce further assumptions about how the CLI moves its cursor.

**Repair:** Use xterm cell APIs for selection hit testing, explicitly convert the wire cursor unit, and define grapheme-aware editing behavior. Include emoji before the cursor, CJK before selected words, combining accents, and multiline edits.

### 11. P2 — Failed Copy destroys the selection needed to retry

[copyTerminalSelectionAndDismiss](../../static/app.js#L1851) dismisses in `finally`, including authority failures, clipboard denial, and disconnects. **Confirmed offline:** copying displayed the stale-selection error and removed the selection. To-tab has the same unconditional dismissal pattern.

**Repair:** Return an explicit operation result and dismiss on success. Retain a usable selection or selected-text snapshot when retry is meaningful, while clearly marking genuinely stale coordinates. Test denial and timeout, not only successful clipboard writes.

### 12. P2 — Image paste in the file editor uploads into the terminal composer

**Confirmed:** Dispatching an image paste on the focused file editor emitted `upload-image` and displayed `Screenshot added — type a message and send.`

The document [paste listener](../../static/app.js#L10317) handles image items before checking the target or whether it is editable. It intercepts images in unrelated fields and routes them to the terminal workflow.

**Repair:** Resolve the intended paste target first. Restrict terminal image upload to terminal/composer contexts, with explicit handling in the file editor. Local text editing and Save worked in the walkthrough. [Editor screenshot](2026-09-04/mobile-file-editor.png).

### 13. P2 — Ordinary reseeds discard scroll intent and disable follow-output

The server sends `scrollTarget: null` for ordinary reseeds. [applyTerminalSeed](../../static/app.js#L5193) converts it through `Number(null)`, obtaining `0`. The [seed-open handler](../../static/app.js#L8567) then treats it as an explicit scroll request and sets `followOutput=false`.

Every settled normal-buffer seed in the initial/rotation walkthrough showed follow-output disabled. A resize also rebuilds the buffer and moves to this implicit target, losing a user's prior reading position. This is separate from the valid intentional scroll target used when loading older history.

**Repair:** Distinguish absent/null targets from explicit numeric zero; preserve the viewport anchor and follow state across ordinary reseeds. Test both reading older output and following a live stream through keyboard and orientation changes.

### 14. P2 — Desktop sends duplicate terminal query replies

A real fixture emitted one cursor-position query. It received **two identical cursor reports** with the desktop client attached: tmux answered and xterm's reply was forwarded through [term.onData](../../static/app.js#L10935). In touch mode the same fixture received one reply from tmux, despite the client dropping xterm-generated data.

This rules out “mobile drops all cursor replies” as the demonstrated cause of Codex's mobile problems: tmux already answers that query. It also shows why the browser and tmux cannot both act as independent terminal protocol responders. The current keystroke classifier avoids claiming geometry for replies but still forwards them.

**Repair:** Define which layer owns each query/response and test raw byte delivery through tmux. Do not fix mobile input by blindly forwarding every xterm-generated reply. Extra replies may confuse applications; this probe establishes duplication, not a specific Codex failure caused by it.

### 15. P2 — Documented plain-HTTP startup cannot complete the current authentication flow

The README advertises direct HTTP access, including by IP. Fresh direct browsers now require passkey registration even with `--no-token`. Browser WebAuthn cannot register against an IP relying-party ID; an HTTP LAN origin also lacks the required secure context. Even valid `http://localhost` initially failed in the walkthrough because [expected_origin](../../server.py#L4802) unconditionally defaults to `https://Host`.

The test server required `MOBILE_TERMINAL_ORIGIN=http://localhost:18086` plus a virtual passkey to complete the genuine login flow. This is a bootstrap/documentation mismatch, not the cause of glitches in already authenticated HTTPS sessions.

**Repair:** Align documented supported origins with enforced authentication, present an actionable unsupported-origin message, and handle localhost origin configuration explicitly. Do not weaken authentication to hide the incompatibility.

## Why Codex feels worse

| Behavior | Observed Codex panes | Observed Claude panes | Consequence here |
| --- | --- | --- | --- |
| Screen/history | Normal buffer; retained scrollback | Alternate screen; repainted viewport | Codex hits stale normal-row copy bookkeeping; Claude uses client-anchored selected rows. |
| Scrolling | Local xterm scrolling | SGR wheel reports to the application | Their history, output-following, and scroll-position lifecycles differ. |
| Authored-text recovery | No local Codex binding cache observed; valid metadata rejected by parser; installed version unsupported | Some cached entries use the supported version, though other entries and live forensic fallbacks show failures too | Claude has more opportunities for exact recovery; Codex has multiple independent blockers. |
| Resize | Full snapshot/replay over retained history | Full snapshot/replay of application viewport | The nine-second quiet requirement affects active output in either mode; normal history adds selection identity and reading-position problems. |

The app itself documents these routing assumptions in [pane_scrolls_locally](../../server.py#L1817), [queueScrollHistory](../../static/app.js#L5545), and [provider authority](../provider-authority.md). The reported asymmetry is therefore consistent with reproducible application behavior, rather than evidence that Codex is simply an inferior TUI.

`--no-alt-screen` is an official Codex option, but **it is not a demonstrated fix here: the observed Codex panes are already in the normal buffer**. Conversely, switching Codex to alternate mode is not a complete remedy: [_selection_is_owned](../../provider_authority.py#L3245) rejects Codex alternate-screen ownership. Any mode experiment needs coordinated scrolling and copy support. Official references: [CLI option](https://developers.openai.com/codex/cli/reference), [TUI configuration](https://developers.openai.com/codex/config-reference).

## UI assessment and repair order

The basic shell, tab creation/switching, settings sheets, shortcut and gesture editors, usage screen, authentication settings, local file tree, text editing, and Save were usable in the isolated walkthrough. No uncaught browser errors occurred. The account action was hidden in this standalone test configuration; it was not exercised. [Display screenshot](2026-09-04/mobile-display.png), [maximum-scale screenshot](2026-09-04/max-scale-320.png).

The most damaging experience is that different failures share the same stale-selection toast, then discard the selection. Users cannot distinguish fresh-output bookkeeping, unavailable transcript matching, geometry disagreement, or clipboard denial. Fixing the underlying states and preserving recoverable work matters more than visual polish.

Recommended implementation order:

1. Repair normal-buffer selection anchoring and Codex metadata parsing; verify real hook bindings and supported renderer fixtures.
2. Preserve paste bytes and restore bracketed-paste mode through reconnect and tab changes.
3. Unify geometry acknowledgments and minimums, bound resize latency during repaint, and preserve scroll intent.
4. Remove global tap cancellation; fix Unicode hit testing/cursor units and failure dismissal.
5. Scope image paste, fix bootstrap guidance, and add persistent sanitized copy-authority status.

Each step needs browser-plus-backend regression coverage. The passing suite contains valuable backend and extracted-function tests, but did not catch the fresh-output lifecycle, two rapid taps, a one-row fit against a six-row server minimum, or mode loss across a real reconnect.

To rerun the small read-only source reproductions:

```sh
.venv/bin/python docs/reviews/2026-09-04/source-probes.py
```

The browser evidence uses synthetic content only. No production service restart, tmux reconfiguration, provider rollout change, or deployment was performed.
