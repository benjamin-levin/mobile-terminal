# Mobile Terminal

`mobile-terminal` is a browser-based terminal for this machine that attaches to a tmux session and works well from a phone.

## Features

- Real PTY attached to `tmux`, so shell completion, history, prompts, Starship, arrow keys, and readline or zle behavior work normally.
- Mobile-friendly tmux window tabs with create, rename, close, and polling-based sync.
- Shortcut bar for touch devices with editable macros such as `{CTRL+C}`, `{CTRL+X}{TAB}`, arrows, and pasted text.
- Shared-secret login gate so the terminal is not exposed anonymously on your LAN.
- Optional Tailscale-only binding, with optional remote IP allowlisting so only a chosen Tailscale device can connect.
- Follow-output mode that keeps the viewport pinned to the bottom while streaming.
- Separate display controls for overall UI scale and terminal text size.

## Run

```bash
cd /home/powerhouse/mobile-terminal
./run.sh --host 0.0.0.0 --port 8085 --session mobile-terminal
```

The server prints an access token. Open `http://<this-computer-ip>:8085` on your phone, enter the token, and the browser will attach to the `mobile-terminal` tmux session.

## Useful options

```bash
python3 server.py --help
python3 server.py --host 0.0.0.0 --port 8085 --session mobile-terminal --cwd /home/powerhouse --shell /usr/bin/zsh
MOBILE_TERMINAL_TOKEN='choose-a-long-secret' ./run.sh --host 0.0.0.0 --port 8085
```

## Tailscale-only mode

Bind only to this computer's Tailscale IP and keep the token:

```bash
./run.sh --tailscale --port 8085 --session mobile-terminal
```

Bind only to Tailscale and disable the token:

```bash
./run.sh --tailscale --no-token --port 8085 --session mobile-terminal
```

Bind only to Tailscale and allow only your phone's Tailscale IP:

```bash
./run.sh --tailscale --no-token --allow-client 100.x.y.z --port 8085 --session mobile-terminal
```

You can find the phone's Tailscale IP in the Tailscale app or the Tailscale admin console. With `--allow-client`, any other device is rejected before the terminal UI loads.

## tmux scrolling

Mouse scrolling needs tmux mouse support enabled. Add this to `~/.tmux.conf` if it is not already present:

```tmux
set -g mouse on
unbind -n WheelUpPane
unbind -n WheelDownPane
bind -n WheelDownPane if -Ft= '#{pane_in_mode}' 'send-keys -M' 'copy-mode -e'
```

Then reload tmux:

```bash
tmux source-file ~/.tmux.conf
```

That setup enables tmux copy-mode on scroll and matches the scroll direction expected by this browser UI.

## Notes

- The backend creates the tmux session automatically if it does not exist yet.
- The UI stores the access token and shortcut layout in browser local storage.
- Traffic is plain HTTP and WebSocket. That is fine on a trusted LAN, but use a VPN, Tailscale, or HTTPS reverse proxy if you want to access it across untrusted networks.

## systemd user service

The repo now includes:

- [mobile-terminal.service](/home/powerhouse/.config/systemd/user/mobile-terminal.service:1)
- [example.env](/home/powerhouse/mobile-terminal/example.env:1)
- [systemd/mobile-terminal.service](/home/powerhouse/mobile-terminal/systemd/mobile-terminal.service:1)

Default startup mode is Tailscale-only on port `8085` with no browser token. Start by copying `example.env` to `mobile-terminal.env`, then add your real settings there. To further lock it to one phone, add that phone's Tailscale IP to `MOBILE_TERMINAL_ALLOW_CLIENTS` in that local env file, then run:

```bash
systemctl --user daemon-reload
systemctl --user restart mobile-terminal.service
```
