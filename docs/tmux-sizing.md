# tmux Geometry Authority

## Required policy

Mobile Terminal shares tmux windows with ordinary SSH and mosh clients. The window must follow the most recently interactive client in either direction.

```tmux
set -g window-size latest
```

Do not use `tmux resize-window -x ... -y ...`. In tmux 3.6 it sets a persistent per-window `window-size=manual` override. That prevents later desktop input or resize from reclaiming geometry.

## Mobile Terminal claim sequence

A control-mode client reports its geometry and marks itself latest with these commands, issued through that same control client under the bridge write lock:

```text
set-option -wu -t PANE window-size
refresh-client -C COLS,ROWS
select-window
```

Important details:

- `set-option -wu` clears only the target window's local policy. It must not be applied globally or to unrelated windows.
- `refresh-client -C COLS,ROWS` reports the control client's geometry. It does not by itself mark that client latest.
- Targetless `select-window` marks the control client authoritative without changing its active window or pane.
- Do not use `refresh-client -C @WINDOW:COLSxROWS`; that pins a window to the control client and blocks ordinary clients from taking it back.
- Ordinary attach, input, focus input, and SIGWINCH naturally reclaim authority for desktop clients.

If a desktop-sized window has reflowed before Mobile Terminal reclaims it, the geometry change must occur inside the bridge reseed protocol. If the window is already at the reported Mobile Terminal dimensions, clear a same-size manual override and issue targetless `select-window` without reseeding.

Compare `#{window_width}` and `#{window_height}`. Pane dimensions are smaller in split layouts and are not the window geometry.

## Interaction signals

Mobile Terminal claims authority on:

- browser focus;
- initial pointer/touch contact, not movement;
- genuine terminal keyboard or paste input;
- genuine composer edits;
- a terminal fit that changes rows or columns.

Generated xterm query replies, mouse-tracking movement, and refresh/reset traffic must not claim authority. Normal key/composer signals are throttled; focus and initial contact use a separately bounded forced claim so returning from a desktop client is immediate.

## Testing

Never use the live tmux server. Each integration test must:

1. Create a private directory and socket.
2. Start `tmux -S <private-socket>` with `TMUX` removed from the environment.
3. Attach PTY-backed ordinary clients to that socket.
4. Verify mobile -> desktop -> mobile -> desktop transfer, same-size claims, detach behavior, and split-pane identity.
5. Kill only the private tmux server and remove its directory.

Required regressions include:

- production code contains no `resize-window`;
- same-size manual target returns to inherited `latest`;
- an unrelated manual window remains manual;
- targetless `select-window` preserves active pane, active window, and split layout;
- a geometry difference reseeds once; repeated same-size activity does not reseed.

## Read-only diagnosis

```bash
printf 'global='; tmux show-options -gv window-size
tmux list-windows -a -F '#{session_name}:#{window_index} size=#{window_width}x#{window_height} local=#{window_size}'
```

A blank `local=` inherits the global value. Diagnose first; never fix this by globally unsetting all window options or restarting the tmux server.
