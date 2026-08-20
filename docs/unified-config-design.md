# Unified mobile-terminal: one codebase, two deployment shapes

Status: design, approved-in-principle 2026-08-19. Author decisions locked (see §1).
Implementation details are documented in `profiles-proxy-integration.md`; the authenticator
contract is documented in `../INTEGRATION-proxy-auth.md`. Those documents are authoritative
where this design sketch differs from the shipped configuration.
Supersedes the ad-hoc split between ph (single) / lat (separate-users) / ps (wanted: profiles).

---

## 1. Locked decisions

- **One codebase.** Separate backend **processes** always — even ps. No single-process
  multi-tenant. (multi_tenant partitions UI only; it is *not* a security boundary.)
- **Two shapes, chosen by config:**
  - `separate-users` (lat today): N systemd `@user` processes, OS-level isolation, each
    its own funnel + creds. **No code change.**
  - `profiles` (ps = "Option B"): the *same* separate-user backend processes **plus a
    proxy front** that shows a dropdown in ONE UI and routes the WebSocket to the selected
    user's backend. Proxy is enabled by a **config switch**.
- **One auth surface where profiles are all yours** (ps: behuman + powerhouse → one auth
  unlocks both). **Per-profile auth where profiles are different people** (lat: ben /
  bperritt keep separate creds). Same proxy code; the difference is config (auth realms).
- **Serve on sovl-me-daddy tailnet only.** benjamin-levin stays reachability-only.
- **OS isolation is real and preserved**: each backend runs as its own OS user, own tmux
  server, own state dir. The proxy never runs shells; it only relays WebSockets.

The proxy is the entire new feature. Everything else is config plumbing + backward-compat.

---

## 2. Architecture

```
                 sovl-me-daddy tailnet  (tailscale serve/funnel)
                              │
                              ▼
   ┌──────────────────────────────────────────────┐
   │  PROXY  (new component, one process)          │   ← the config switch turns this on
   │  • serves the PWA + the ONE auth surface      │
   │  • enumerates profiles from config            │
   │  • after auth, relays WS → selected backend   │
   └───────────────┬───────────────┬───────────────┘
                   │ ws loopback    │ ws loopback
                   ▼                ▼
   ┌───────────────────┐  ┌───────────────────┐
   │ backend @powerhouse│  │ backend @behuman  │   ← unchanged server.py, one per OS user
   │ 127.0.0.1:8090     │  │ 127.0.0.1:8091    │      own tmux, own state, loopback-only
   │ OS user: powerhouse│  │ OS user: behuman  │
   └───────────────────┘  └───────────────────┘
```

- **Backends** are today's `server.py`, each bound to loopback, reachable **only** from the
  proxy. They do the terminal work (tmux → xterm). OS isolation lives here.
- **Proxy** terminates the tailnet/funnel TLS, holds auth, shows the dropdown, and relays.
- **No proxy?** (ph, and each lat backend when run standalone) → the backend serves itself
  directly, exactly as today. The config switch is off; zero behavior change.

### Why a proxy instead of one process with many profiles
Every tmux call in `server.py` uses the default socket (no `-S`/`-L`); the TmuxBridge
attaches with no socket qualifier. Threading a per-profile socket selector through every
tmux helper + the bridge is invasive and fragile, and it would still run all profiles under
one OS UID (no real isolation). The proxy gets **real OS isolation for free** (separate
users) and keeps `server.py` almost untouched.

---

## 3. Config schema

New env var `MOBILE_TERMINAL_CONFIG` → path to a JSON file. **Absent = today's behavior**
(single implicit profile synthesized from the existing env vars → full backward compat; ph
and standalone lat backends set nothing new).

Two roles, two shapes of the same file:

### 3a. Backend config (per process) — mostly today's env, no new file needed
A backend keeps reading its existing env (`PORT`, `SESSION`, `LABEL`, `CWD`, `SHELL`,
`TOKEN`/`NO_TOKEN`, `TRUST_IDENTITY`, …). When fronted by a proxy it additionally:
- binds loopback only (`MOBILE_TERMINAL_HOST=127.0.0.1`),
- trusts the proxy as its client (loopback allow-list), and
- delegates auth to the proxy (`NO_TOKEN=true` on the loopback hop is acceptable *because*
  it is unreachable except via the proxy; or a shared internal token — see §5).

### 3b. Proxy config — the profiles-mode file (only on ps-like hosts)

```json
{
  "mode": "proxy",
  "listen": "127.0.0.1:8085",
  "stateDir": "state/proxy",
  "authRealms": {
    "mine":    { "token": "<bootstrap-token>", "deviceKeyAuth": true, "trustIdentity": false }
  },
  "profiles": [
    { "id": "powerhouse", "label": "Powerhouse",
      "backend": "ws://127.0.0.1:8090", "osUser": "powerhouse", "authRealm": "mine" },
    { "id": "behuman",    "label": "Behuman",
      "backend": "ws://127.0.0.1:8091", "osUser": "behuman",    "authRealm": "mine" }
  ]
}
```

- **Both ps profiles share `authRealm: "mine"`** → one auth unlocks both (locked decision).
- **lat**, if/when it gets the proxy, uses **distinct realms** so each profile authenticates
  separately (isolation between different people preserved):

```json
  "authRealms": {
    "ben":      { "deviceKeyAuth": true, "token": "<ben-token>" },
    "bperritt": { "deviceKeyAuth": true, "token": "<bperritt-token>" }
  },
  "profiles": [
    { "id": "ben",      "label": "Ben",      "backend": "ws://127.0.0.1:8086", "authRealm": "ben" },
    { "id": "bperritt", "label": "B Perritt","backend": "ws://127.0.0.1:8085", "authRealm": "bperritt" }
  ]
```

Same proxy binary. ps = one realm shared; lat = one realm per profile. That is the whole
"one auth vs per-profile auth" knob.

---

## 4. Code changes (concrete seams)

1. **Config loader** (new, `server.py` startup): if `MOBILE_TERMINAL_CONFIG` set and
   `mode == "proxy"` → run as proxy. Else synthesize one implicit profile from env and run
   as a plain backend (today's path). Absence changes nothing.
2. **Proxy component** (new, the bulk of the work):
   - serves `static/` (PWA) + `/config` (advertises `profiles[]`, `activeProfile`, auth caps),
   - the ONE auth handshake (token → device-key → optional identity; §5),
   - a profile registry from config,
   - a WS relay: on `switch-profile`, dial the target backend's loopback WS and pump frames
     both ways. Auth is checked at the proxy; the backend hop is loopback-trusted.
3. **Backend `server.py`**: minimal change. Accept an optional proxy-forwarded principal
   header (so the backend can label sessions per the authed user) and bind loopback when
   fronted. No tmux/socket changes.
4. **Client `static/app.js`**:
   - add a **profile dropdown** (reuse the session-menu render seam, ~`renderSessionMenu`),
   - send `switch-profile` / `request-profiles` (mirror `switch-session`/`request-sessions`),
   - **namespace per-profile client state**: open-tabs snapshot + active-session are keyed
     by `profileId` (today they are global — this is the one client-state correctness fix).
5. **Protocol additions** (next to `open-tabs` dispatch): `request-profiles` → `profiles`;
   `switch-profile`; `ready` payload gains `profiles` + `activeProfile`.

Everything above is inert when `MOBILE_TERMINAL_CONFIG` is absent.

---

## 5. Auth: passkeys primary, token demoted (folds in the two known bugs)

Auth lives at the **proxy** (single place to enroll/verify for profiles mode; per-realm for
separate people). Precedence per connection, unchanged in spirit:

**tailnet identity** (loopback + no Funnel marker + `trustIdentity`) → **device-key ECDSA
P-256 signature** (silent, WebCrypto, non-extractable, IndexedDB) → **shared token**.

Two bugs this design fixes as a side effect:

- **WebAuthn is dead code today.** `build_webauthn_options()` / `handle_webauthn_register()`
  exist but nothing ever emits `webauthn-register-options`; enrollment always falls to the
  silent `enroll-key` path. **Fix:** make passkeys the *primary, hardware-backed* credential
  at the proxy — the proxy emits `webauthn-register-options` on new-device bootstrap, verifies
  the attestation, then registers. Per-device, individually revocable. Demote the shared token
  to **bootstrap-only**.
- **Token rotation doesn't revoke device keys.** Rotating the token clears the legacy
  `devices` map but not `state/device-keys.json`, so old keys keep authenticating. **Fix:**
  per-credential server-side revocation (delete the SPKI record); rotation optionally
  cascades. With passkeys primary, each device is a distinct revocable credential — the
  revocation story becomes correct by construction.

Per-realm credential stores (`stateDir/<realm>/device-keys.json`) make lat's per-profile
auth work: enrolling a passkey in realm `ben` never unlocks realm `bperritt`.

---

## 6. The three hosts under this design

| Host | Shape | Proxy? | Backends | Auth | Serving |
|------|-------|--------|----------|------|---------|
| **ph** | single | no | 1 (self-served) | token + device-key | sovl-me-daddy |
| **lat** | separate-users | opt-in later | @ben :8086, @bperritt :8085 (+@ubuntu :8087) | per-profile realms | sovl-me-daddy |
| **ps** | profiles (Option B) | **yes** | @powerhouse :8090, @behuman :8091 | one shared realm | sovl-me-daddy + funnel |

- **ph** unchanged.
- **lat** unchanged for now; proxy is opt-in (per-profile realms) when we want the unified UI.
  Its people stay isolated because their realms are distinct.
- **ps** is where the proxy ships first.

---

## 7. Rollout

1. Build the config loader + proxy behind the switch. **No behavior change when config absent.**
   Verify ph + lat backends still run untouched.
2. **ps**: stand up 2 backends as OS users — `mobile-terminal@powerhouse` (127.0.0.1:8090),
   `mobile-terminal@behuman` (127.0.0.1:8091). Note: behuman currently runs **ttyd**, not MT —
   standardize it onto an MT backend to be a profile.
3. ps proxy on 127.0.0.1:8085 (already funnel-fronted). Dropdown = {Powerhouse, Behuman},
   shared realm → one auth. Test both profiles reach their own shells as their own UID.
4. **lat** (later, opt-in): add the proxy with per-profile realms for the consistent UI.
5. Wire passkeys as primary at the proxy; demote token to bootstrap; add per-credential revoke.

---

## 8. Open decisions (need a call before build)

1. **Settings scope**: per-profile or global? (open-tabs + active-session MUST be per-profile;
   zoom is already per-device by prior decision. Theme/font — global is simpler.)
2. **behuman off ttyd**: confirm we standardize behuman onto an MT backend (required for it to
   be a dropdown profile).
3. **Proxy OS user**: the proxy can reach every backend loopback, so its own user is a broker
   with reach to all profiles. Acceptable for ps (all yours). For lat, keep per-realm auth so a
   proxy compromise still needs each realm's credential to *use* a backend.
4. **Internal hop hardening**: loopback-only + firewall, or a shared internal token on the
   proxy→backend hop, or a proxy-signed principal header. Pick one (loopback-only is the
   simplest and sufficient given backends bind 127.0.0.1).
