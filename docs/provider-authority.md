# Provider-backed Copy Authority

Claude and Codex add gutters and application-level wrapping before text reaches tmux. tmux cells cannot reliably distinguish provider layout from authored indentation or source line breaks. Provider authority therefore combines two sources:

- the provider transcript supplies authored bytes and semantic hard breaks;
- a fenced tmux snapshot supplies visible placement and selection coordinates.

Terminal whitespace verifies transcript-derived candidates. It never invents, trims, dedents, or normalizes source content.

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

In `prefer`, every provider-side rejection falls back to the same exact tmux extraction used for ordinary output. A provider binding or ownership signal makes the response carry only the sanitized `terminal-raw` indicator; Copy and To-tab use it for a non-blocking warning that line breaks and spaces may be terminal-rendered. A canonical transcript match carries `provider-exact`. Ordinary unowned selections carry no provider indicator, and no response exposes provider reason codes, transcript identifiers, or content beyond the selected result.

## Rollout gate

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

## Binding and transcript safety

- Treat hook files and pane options as discovery caches, not authority.
- Revalidate process start time, pane, provider session ID, transcript path/inode, lifecycle generation, and provider version on every selection.
- Open transcripts beneath approved roots with symlink-safe descriptor traversal.
- Fence descriptor identity, metadata, size, and complete JSONL boundary before parsing and revalidate before returning text.
- Never infer identity from cwd, pane title, newest file, or transcript recency.
- Never print transcript text, selected text, credentials, or complete paths in diagnostics.

## Diagnostics

Diagnostics are bounded aggregate reason counters only. They may report counts and stable reason codes such as binding failure, unsupported renderer, ambiguous placement, or snapshot mismatch. They must not contain source text, selected text, terminal rows, tokens, credentials, or provider authentication data.

Do not use `/proc/<pid>/environ` as the deployment verifier. It can be inaccessible under procfs hardening and can expose secrets if printed. Read only the configured authority mode from the owner-only env file and use provider binding validation for runtime authority.

## Required regression coverage

- hard breaks, blank lines, indentation, tabs, repeated/trailing spaces, and gutter-like authored prefixes;
- soft wraps at width boundaries with no copied newline;
- unsupported material before/after a supported island;
- selections crossing unsupported material;
- historical 45/90-column rows inside a current 180-column snapshot;
- duplicate placement ambiguity and unsupported alias poisoning;
- binding replay, process reuse, transcript append/truncate/rotation, and final fence revalidation;
- command provenance remains first and ordinary tmux output remains available outside provider-owned ranges;
- Copy and To-tab use one authoritative result.
