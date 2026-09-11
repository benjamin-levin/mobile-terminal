# Provider-backed Copy Authority

Claude and Codex add gutters and application-level wrapping before text reaches tmux. tmux cells cannot reliably distinguish provider layout from authored indentation or source line breaks. Provider authority therefore combines three sources:

- the provider transcript supplies authored bytes and semantic hard breaks;
- a fenced tmux snapshot supplies pane identity, geometry, ownership, and binding context;
- the authenticated browser supplies bounded selected rows and wrap flags. Normal-buffer selections verify these against tracked tmux rows and a second snapshot; alternate-screen panes use the browser rows because a repainting alternate screen has no retained tmux row identity.

Terminal or client-rendered whitespace verifies transcript-derived candidates. It never invents, trims, dedents, or normalizes provider-exact source content.

## Precedence and shared consumers

Authority order is:

1. Trusted Mobile Terminal composer command provenance.
2. Provider transcript authority.
3. Ordinary tmux output outside provider-owned regions.

Copy and To-tab intentionally call the same authoritative selection request. Direct PTY paste flattening is a separate safety boundary. In `enforce`, a provider-owned failure must never fall through to raw tmux text; the public failure is exactly:

```text
Terminal changed; select again.
```

## Modes

`MOBILE_TERMINAL_PROVIDER_AUTHORITY` accepts:

- `off`: provider authority disabled;
- `shadow`: evaluate mappings and bounded reason counters without making provider failures block ordinary operation;
- `prefer`: return provider source for a unique supported mapping, otherwise fall back to exact tmux cell extraction and visibly identify the result as raw terminal text;
- `enforce`: return provider source for unique supported mappings and fail closed for provider-owned failures.

Use the checked-in mode tool:

```bash
scripts/provider-mode.sh status
scripts/provider-mode.sh shadow --apply
scripts/provider-mode.sh prefer --apply
scripts/provider-mode.sh enforce --apply --confirm-enforce
scripts/provider-mode.sh off --apply
```

Without `--apply`, mode changes are previews. Enforcement is deliberately gated from `prefer` and requires the additional `--confirm-enforce` acknowledgement. Moving directly from `off` to `prefer` is allowed, but `shadow` remains the diagnostic rollout gate before live use. A successful test suite is not a substitute for a real ph/iPhone selection.

In `prefer`, every provider-side rejection falls back to rendered cell extraction and carries only the sanitized `terminal-raw` indicator. Normal-buffer fallback uses the exact tmux cells. Alternate-screen fallback uses the validated client rows on which the selection was made, with physical row boundaries preserved, because the live pane may already have repainted. Copy and To-tab use the indicator for a non-blocking warning that line breaks and spaces may be terminal-rendered. A canonical transcript match carries `provider-exact`. Ordinary unowned selections carry no provider indicator, and no response exposes provider reason codes, transcript identifiers, or content beyond the selected result.

## Rollout gate

Codex user lifecycle hooks belong in `~/.codex/hooks.json`, beside `config.toml`.
The installer removes only its tagged entries from the former
`~/.codex/hooks/hooks.json` location, retaining a backup and foreign entries.
That nested location is a plugin convention and was not a discovered user hook
file. Codex also requires review and trust of the exact hook definition before
execution; installing or counting hooks does not establish event delivery. The
installer does not grant trust. Review the installed commands in Codex and then
verify a new session's binding, live PID/start identity, and ownership boundaries.
See the [official hook discovery and trust documentation](https://learn.chatgpt.com/docs/hooks).

The isolated hook lifecycle regression invokes installed commands from a synthetic
provider process in private tmux and verifies binding creation and closure. It
does not prove actual Codex event delivery or renderer compatibility. Claude
`2.1.261` assistant output is now supported by six recorded synthetic transcript
resumes at 60 and 100 columns, covering prose, authored breaks, headings,
emphasis, lists, Unicode, and code. The captures use the installed CLI with no
submitted prompt or model request. They do not validate streaming states or
user echoes for this version. Codex `0.153.3` is supported by six equivalent
captures. Its profile verifies retained heading markers, dash lists with spacing
after wrapped items, cyan inline code, and fenced code. Presentation markers and
inserted spacing are omitted from copied text; source hard breaks remain. These
fixtures validate completed rendering; they do not establish streaming states,
actual Codex hook trust, delivery, or live pane ownership.

An isolated `0.153.3` probe confirmed discovery of the corrected user-hook file
and persisted trust through Codex's normal review UI. Relaunching a synthetic
legacy saved session did not produce an observed lifecycle callback, even with
the effective `hooks` feature enabled, four trusted states, no disabled hook
states, and an executable interpreter. An immediate wrapper marker confirmed
that the command was not invoked in that probe. That probe
does not establish lifecycle behavior for a fresh real provider turn. Verify
actual event delivery and binding validation separately after installation.

1. Deploy code and hooks to ph with mode `shadow`.
2. Verify bindings, transcript fences, candidate counts, reason counters, service health, and actual Claude/Codex output at the current terminal width.
3. Confirm on iPhone that source hard breaks remain, visual wraps disappear, and provider gutters are omitted.
4. Enable `prefer` on ph and verify both exact provider selections and the visible raw-terminal fallback warning.
5. Enable `enforce` on ph only if fail-closed operation is required and prefer has passed live acceptance.
6. Repeat Copy and To-tab tests.
7. Only after explicit acceptance deploy the managed `ps-powerhouse` target, then `lat-ben` and `lat-bperritt` in order. Behuman is not a managed deployment target.

If valid Copy and To-tab requests both fail in enforce, return to `prefer` immediately so users receive warned exact-terminal fallback while diagnosis continues. They share a backend path, so debugging the buttons independently wastes time.

## Fail-closed boundaries

Renderer support is provider/version gated. The provider matcher rejects unsupported or ambiguous content, including renderer/theme-dependent constructs, transformed messages, clipping, mixed ownership, repeated placements, stale bindings, transcript mutation, and uncertain grapheme geometry. `enforce` exposes that rejection as the stale public error; `prefer` converts it to the warned raw-terminal fallback without weakening the matcher.

Supported selections inside a larger record must be compiled as source-offset-preserving supported islands. A selection wholly inside exactly one supported island may succeed; a selection touching or crossing unsupported material must fail. Unsupported material must still poison aliases that would otherwise make a supported placement ambiguous.

Rows retained by tmux from an earlier narrower window can be shorter than the current snapshot width. Placement normalization may right-pad absent terminal cells to the current display width, but must:

- measure terminal display-cell width rather than Python string length;
- reject overflow or unsafe wide-cell boundaries;
- never call `strip()` or `rstrip()`;
- preserve authored trailing spaces through source provenance.

## Alternate-screen client anchor and trust

Alternate-screen rows are repaintable positions rather than retained history identities. An alternate-screen `selection-request` therefore includes `clientRows` for exactly the selected viewport rows, in ascending contiguous `y` order. Each row contains the full rendered text with exact spacing and bounded style runs. The server requires the rows to cover `selection.start.y` through `selection.end.y`, validates all coordinates against the advertised viewport, accepts only the small provider-verification style vocabulary, and requires both the complete UTF-8 `selection-request` wire encoding and the compact decoded `clientRows` encoding to fit within 64 KiB. Invalid row data produces only the standard stale-selection error and a sanitized `client-rows-invalid` counter.

The authenticated browser is trusted to report the rendered cells on which its user acted; it is not provider source authority. A `provider-exact` response still comes only from a uniquely matched, fenced provider transcript after live pane ownership and binding revalidation. Client text and styles may choose a candidate or cause ambiguity/rejection, but they cannot supply the returned provider-exact bytes. In `off`, `shadow` fallback, or `prefer` fallback, those rendered cells are the only faithful raw view after the alternate screen has repainted, so they are the explicit `terminal-raw` trust boundary.

Alternate client-row validation and raw slicing use the installed xterm UnicodeV6 cell model, not Python `wcwidth`: for example, U+2705, U+274C, and U+2728 occupy one browser cell each but two cells under the provider's native width model and the tested private tmux. Provider matching first renders the logical transcript with native provider wrap decisions, then projects those source-derived cells, style spans, and source-boundary anchors into browser columns, replacing only renderer-owned right padding. It never reflows native provider wraps using browser widths or relabels a tmux snapshot. Normal-buffer validation and raw tmux geometry remain unchanged. Exhaustive installed-xterm width parity and real-xterm row roundtrips guard this contract.

Projection remains conservative: a source grapheme split across multiple UnicodeV6 cells (such as a ZWJ sequence or newer combining marks), a row that expands beyond the viewport, unsupported renderer output, style mismatches, ambiguous placements, or stale binding/transcript fences cannot produce provider-exact text. These retain the existing warned raw fallback in `prefer` and fail closed in `enforce`; width correction does not bypass them.

The server may hold later PTY output while resolving a selection. It sends `selection-result` before releasing that output, so a repaint cannot tear down the browser selection before Copy or To-tab receives its answer. Alternate selection no longer waits for output quiet or requires byte-revision equality; epoch, pane, layout, viewport geometry, coordinates, bounded client rows, provider binding, and transcript fences remain strict.

## Binding and transcript safety

- Treat hook files and pane options as discovery caches, not authority.
- Revalidate process start time, pane, provider session ID, transcript path/inode, lifecycle generation, and provider version on every selection.
- Open transcripts beneath approved roots with symlink-safe descriptor traversal.
- Fence descriptor identity, metadata, size, and complete JSONL boundary before parsing and revalidate before returning text.
- Never infer identity from cwd, pane title, newest file, or transcript recency.
- Never print transcript text, selected text, credentials, or complete paths in diagnostics.

## Diagnostics

Diagnostics are bounded aggregate reason counters only. They may report counts and stable reason codes such as binding failure, unsupported renderer, ambiguous placement, snapshot mismatch, or invalid client-row geometry. They must not contain source text, selected text, client row text/styles, terminal rows, tokens, credentials, or provider authentication data.

For temporary, explicit incident collection, setting `MOBILE_TERMINAL_COPY_FORENSICS=1` at service startup enables full-text copy records under `state/forensics/copy-YYYYMMDD.jsonl` and bounded client viewport/resize records under `state/forensics/viewport-YYYYMMDD.jsonl`. These owner-only files include selected and returned copy text and transcript-path binding metadata, so treat them as sensitive debugging data and disable the setting after collection. Each stream rotates near 50 MiB and retains the latest five files. Journal output remains sanitized and never includes copied text, terminal rows, transcript paths, tokens, or authentication material.

Do not use `/proc/<pid>/environ` as the deployment verifier. It can be inaccessible under procfs hardening and can expose secrets if printed. Read only the configured authority mode from the owner-only env file and use provider binding validation for runtime authority.

## Required regression coverage

- hard breaks, blank lines, indentation, tabs, repeated/trailing spaces, and gutter-like authored prefixes;
- soft wraps at width boundaries with no copied newline;
- unsupported material before/after a supported island;
- selections crossing unsupported material;
- historical 45/90-column rows inside a current 180-column snapshot;
- duplicate placement ambiguity and unsupported alias poisoning;
- binding replay, process reuse, transcript append/truncate/rotation, and final fence revalidation;
- client-anchored alternate-screen rows, bounded coordinate/style validation, repaint during matching, and result-before-held-output ordering;
- one-row normal selections ignore wrap relationships outside the selected interval, unused tab stops, and styling while interior selected wrap changes remain stale;
- scroll bursts coalesce/cancel before tmux and are drained in bounded responsive batches;
- command provenance remains first and ordinary tmux output remains available outside provider-owned ranges;
- Copy and To-tab use one authoritative result.
