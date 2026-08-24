# Mobile Terminal

`mobile-terminal` is a browser-based terminal for this machine that attaches to a tmux session and works well from a phone.

## Features

- Real PTY attached to `tmux`, so shell completion, history, prompts, Starship, arrow keys, and readline or zle behavior work normally.
- Mobile-friendly tmux window tabs with create, rename, close, and polling-based sync.
- Shortcut bar for touch devices with editable macros such as `{CTRL+C}`, `{CTRL+X}{TAB}`, arrows, and pasted text.
- Shared-secret login gate so the terminal is not exposed anonymously on your LAN.
- Device-bound silent auth: a browser enrolls a non-extractable key once, then reconnects with no prompt (see [Authentication](#authentication)).
- Optional Tailscale-only binding, with optional remote IP allowlisting so only a chosen Tailscale device can connect, or public access via Tailscale Funnel.
- Optional multi-tenancy: several users on one machine, each with their own token, tabs, and devices.
- Follow-output mode that keeps the viewport pinned to the bottom while streaming.
- Separate display controls for overall UI scale and terminal text size.

## Operations and deployment

Read [`docs/RUNBOOK.md`](docs/RUNBOOK.md) before changing a running installation. Subsystem references:

- [`docs/deployment.md`](docs/deployment.md) — fleet topology, exact service boundaries, rollout gates, and rollback.
- [`docs/tmux-sizing.md`](docs/tmux-sizing.md) — latest-interactive-client geometry and private-socket testing.
- [`docs/provider-authority.md`](docs/provider-authority.md) — transcript-backed Copy/To-tab authority and shadow/enforce rollout.

Use `scripts/verify-runtime.sh` for redacted, read-only local verification and `scripts/provider-mode.sh` for provider-mode previews and explicitly applied changes. `deploy.sh` requires `--dry-run` or `--apply` plus exact per-user targets and explicit ph/ps acceptance gates; see the deployment guide.

## Run

```bash
cd /path/to/mobile-terminal
./run.sh --host 0.0.0.0 --port 8085 --session mobile-terminal
```

The server reports whether access-token authentication is configured but never prints the token value. Configure `MOBILE_TERMINAL_TOKEN` in the owner-only `mobile-terminal.env` file before starting; `run.sh` loads that file and refuses to use any interpreter except this checkout's `.venv/bin/python`. Then open `http://<this-computer-ip>:8085` on your phone. Use `--no-token` only with the documented Tailscale, allowlist, or loopback safeguards.

## Useful options

```bash
./run.sh --help
./run.sh --host 0.0.0.0 --port 8085 --session mobile-terminal --cwd "$HOME" --shell "$SHELL"
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

## Authentication

Each WebSocket connection is authenticated by the first of these that succeeds:

1. **Device-bound key (silent).** The browser generates a non-extractable ECDSA
   P-256 key pair (WebCrypto), keeps the private key in IndexedDB, and registers
   only the public key with the server (`state/device-keys.json`). To connect it
   signs a one-time server nonce; the server verifies against the stored public
   key. Unlike a token this credential cannot be exported by JavaScript or
   replayed (each nonce is single-use), so an enrolled device reconnects with no
   prompt from anywhere.
2. **Shared token (bootstrap / fallback).** The per-user (multi-tenant) or global
   (`MOBILE_TERMINAL_TOKEN`) secret. Entering it once enrolls this device's key;
   thereafter the device is silent.
3. **Tailscale identity (opt-in, off by default).** Token-less auto-login from the
   `Tailscale-User-Login` header injected by `tailscale serve`. Gated behind
   `MOBILE_TERMINAL_TRUST_IDENTITY` — see the caveat below.

### Enrolling a device

- **Token-free (tailnet identity):** with `MOBILE_TERMINAL_TRUST_IDENTITY=1` on a
  machine reachable on the tailnet *with identity*, opening the terminal enrolls
  the device's key automatically — no token. Safe **only** where identity is
  genuine (see caveat).
- **One-time token:** otherwise, enter the token once. The device enrolls its key
  and every later connection is silent.

Enrollment persists server-side indefinitely (until sign-out, token rotation, or
deleting the entry). On the phone the private key lives in browser storage; on
**iOS Safari, add the site to the Home Screen** so the key is exempt from
Safari's ~7-day eviction of unused-site storage and survives permanently.
Signing out sends `forget-key`, which revokes the server-side public key.

### Public (off-tailnet) access — Tailscale Funnel

`tailscale serve` is tailnet-only. To reach the terminal from the public internet,
enable Funnel on the served port (Funnel must be enabled for the tailnet first, via
the admin console `nodeAttrs`/consent flow):

```bash
sudo tailscale funnel --bg --https=<public-port> http://127.0.0.1:<local-port>
```

The device key (or token) is then the sole gate, so keep it enabled.

### ⚠️ Identity trust vs Funnel — NAT box vs public VPS

`MOBILE_TERMINAL_TRUST_IDENTITY` is **off by default** because Tailscale Funnel was
observed injecting the node owner's `Tailscale-User-Login` into public requests on
some setups, which would auto-authenticate anyone as the owner. Identity login now
also requires a same-origin browser WebSocket (`Origin` must match `Host`), which
blocks cross-site browser use but cannot authenticate the local reverse proxy itself.
Behavior differs by how the machine reaches the internet:

- **NAT'd machine (no public IP):** the `*.ts.net` name resolves to the *tailnet*
  IP for tailnet members, so tailnet requests carry genuine identity while Funnel
  serves the public path separately. Token-free auto-enroll works on the same URL,
  provided all local processes and users are also inside the trust boundary.
- **Public-IP VPS:** enabling Funnel makes the `*.ts.net` name resolve to the
  machine's *public* IP for everyone (even on the tailnet), so every request
  arrives via the public path with **no identity** — and non-Funnel ports become
  unreachable by that name. There is no identity-bearing address, so token-free
  auto-enroll is not possible; use the one-time token. Keep identity trust **off**.

A local process can forge loopback, `Host`, `Origin`, and `Tailscale-*` headers over
the current TCP reverse-proxy hop. Keep identity trust off if local processes or users
are outside the trust boundary. See [`INTEGRATION-proxy-auth.md`](INTEGRATION-proxy-auth.md)
for the profile-proxy authenticator contract and the exact limitation.

Optional: `MOBILE_TERMINAL_RP_ID` / `MOBILE_TERMINAL_ORIGIN` override the WebAuthn
RP id / origin (otherwise derived from the request `Host`).

### Multi-tenancy

Copy `mobile-terminal-users.example.json` to `mobile-terminal-users.json` to host
several users on one machine, each with their own token, tabs, devices, and per-user
settings. See that file's comments for the format. It is an organizational boundary,
not a security one — every tab is a real shell as the one OS user.

### Dependencies

Device-key verification uses `cryptography`; the optional WebAuthn enrollment path
uses `webauthn` + `cbor2`. Install them into the virtualenv the service runs from
(`.venv/bin/pip install cryptography cbor2 webauthn`) and point the service's
`ExecStart` at that interpreter. `state/`, `*.env`, and `mobile-terminal-users.json`
hold secrets/keys and are gitignored. Keep secret-bearing files owner-only:

```bash
chmod 600 mobile-terminal.env mobile-terminal-users.json /path/to/proxy-config.json
```

`install.sh` enforces mode `0600` on `mobile-terminal.env`; token persistence uses
owner-only replacement files, and the service templates use `UMask=0077`. Access and internal
hop tokens are removed from terminal and tmux child environments.

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
- Session switches do not preload tmux history anymore. The browser reconnects to the new session first, then streams live output, which avoids large-history freezes on heavy sessions.
- Mobile composer sync is trigger-based. It updates on explicit recall and edit actions instead of scanning terminal output continuously, which keeps full-screen apps from stalling the UI.
- The UI stores the access token and shortcut layout in browser local storage.
- Traffic is plain HTTP and WebSocket. That is fine on a trusted LAN, but use a VPN, Tailscale, or HTTPS reverse proxy if you want to access it across untrusted networks.

## Install

The repo includes a cross-platform installer:

```bash
./install.sh --apply
```

The installer is non-mutating unless `--apply` is present. It:

- Installs `python3`, `tmux`, `node`, and `npm` if they are missing.
- Runs `npm ci` to populate `node_modules`.
- Creates `mobile-terminal.env` if needed, writes Bash/systemd-compatible quoted values, and records an auth-readiness marker. Existing token-mode installs must be migrated explicitly with `./install.sh --apply --migrate-token-auth`; this replaces the old token line with a generated bootstrap token without parsing or printing the old value. Existing `--no-token` envs are marked ready without adding a token. The file is always mode `0600`.
- Installs and starts a user service:
  - Linux: `systemd --user`
  - macOS: `launchd`

Useful variants:

```bash
./install.sh --apply --tailscale --no-token
./install.sh --apply --port 8085 --session mobile-terminal --cwd "$HOME" --shell "$SHELL"
./install.sh --apply --service none
```

On Linux, the generated service is written to `~/.config/systemd/user/mobile-terminal.service`.
On macOS, the generated agent is written to `~/Library/LaunchAgents/com.mobile-terminal.server.plist`.
