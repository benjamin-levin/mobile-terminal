#!/usr/bin/env bash
# ps-proxy-up.sh — prototype the profiles proxy WITH passkeys on ps (this box).
#
# Runs the "powerhouse" backend on loopback:8090, a "behuman" backend on
# loopback:8091, and the proxy on :8085. Prototype caveat: both backends run as
# the current OS user, so this does not test the production OS-user isolation.
# The public funnel already points 443 -> 127.0.0.1:8085, so no funnel change is needed.
# Foreground process: Ctrl-C stops all three. Revert to normal any time by restarting the
# systemd service (see the message this script prints if port 8085 is busy).
#
# This script starts processes but touches NO systemd units. You stop the standalone
# service yourself first; you restart it yourself to revert.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
CONFIG="$ROOT/docs/ps-proxy.example.json"
BACKEND_PORT=8090
BH_PORT=8091
PROXY_PORT=8085

[ -x "$PY" ]     || { echo "ERROR: venv python not found at $PY"; exit 1; }
[ -f "$CONFIG" ] || { echo "ERROR: config not found: $CONFIG"; exit 1; }

# 1. Port 8085 must be free. The standalone systemd service normally owns it.
if ss -tlnp 2>/dev/null | grep -q "127.0.0.1:${PROXY_PORT} "; then
  echo "Port ${PROXY_PORT} is busy — that's your standalone service. Stop it first:"
  echo "    systemctl --user stop mobile-terminal.service"
  echo "then re-run this script."
  echo "Revert to normal later with:  systemctl --user start mobile-terminal.service"
  exit 1
fi

# 2. Shared bootstrap token (used once per browser to authorize passkey enrollment).
#    Pulled from your existing env file; never printed.
if [ -f "$ROOT/mobile-terminal.env" ]; then set -a; . "$ROOT/mobile-terminal.env"; set +a; fi
: "${MOBILE_TERMINAL_TOKEN:?MOBILE_TERMINAL_TOKEN not set — put it in mobile-terminal.env}"

# 3. Ephemeral internal hop tokens (proxy <-> backend). Prototype-only, regenerated per run.
HOP_POWERHOUSE="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(24))')"
HOP_BEHUMAN="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(24))')"
export MOBILE_TERMINAL_INTERNAL_TOKEN="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(24))')"
export MOBILE_TERMINAL_INTERNAL_TOKEN_POWERHOUSE="$HOP_POWERHOUSE"
export MOBILE_TERMINAL_INTERNAL_TOKEN_BEHUMAN="$HOP_BEHUMAN"

mkdir -p "$ROOT/state/proxy"

# 4. Launch both backends: loopback-only, validate the proxy's hop token,
#    no user token (the proxy is the only auth surface). MOBILE_TERMINAL_CONFIG is
#    unset here so server.py runs as a plain backend, not the proxy.
BACKEND_PID=""
BH_PID=""
PROXY_PID=""
CLEANED_UP=false
cleanup() {
  if [ "$CLEANED_UP" = true ]; then return; fi
  CLEANED_UP=true
  echo
  echo "stopping backends..."
  if [ -n "$BACKEND_PID" ]; then kill "$BACKEND_PID" 2>/dev/null || true; fi
  if [ -n "$BH_PID" ]; then kill "$BH_PID" 2>/dev/null || true; fi
  if [ -n "$PROXY_PID" ]; then kill "$PROXY_PID" 2>/dev/null || true; fi
  if [ -n "$BACKEND_PID" ]; then wait "$BACKEND_PID" 2>/dev/null || true; fi
  if [ -n "$BH_PID" ]; then wait "$BH_PID" 2>/dev/null || true; fi
  if [ -n "$PROXY_PID" ]; then wait "$PROXY_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

env -u MOBILE_TERMINAL_CONFIG \
    MOBILE_TERMINAL_HOST=127.0.0.1 \
    MOBILE_TERMINAL_PORT="$BACKEND_PORT" \
    MOBILE_TERMINAL_SESSION=mt-powerhouse \
    MOBILE_TERMINAL_NO_TOKEN=true \
    MOBILE_TERMINAL_INTERNAL_TOKEN="$HOP_POWERHOUSE" \
    MOBILE_TERMINAL_REQUIRE_INTERNAL_TOKEN=true \
    "$PY" server.py &
BACKEND_PID=$!
echo "backend (powerhouse) pid ${BACKEND_PID} -> 127.0.0.1:${BACKEND_PORT}"

env -u MOBILE_TERMINAL_CONFIG \
    MOBILE_TERMINAL_HOST=127.0.0.1 \
    MOBILE_TERMINAL_PORT="$BH_PORT" \
    MOBILE_TERMINAL_SESSION=mt-behuman \
    MOBILE_TERMINAL_NO_TOKEN=true \
    MOBILE_TERMINAL_INTERNAL_TOKEN="$HOP_BEHUMAN" \
    MOBILE_TERMINAL_REQUIRE_INTERNAL_TOKEN=true \
    "$PY" server.py &
BH_PID=$!
echo "backend (behuman) pid ${BH_PID} -> 127.0.0.1:${BH_PORT}"

# Wait for both backends to listen before starting the proxy.
for _ in $(seq 1 20); do
  ss -tlnp 2>/dev/null | grep -q "127.0.0.1:${BACKEND_PORT} " && break
  sleep 0.25
done
if ! ss -tlnp 2>/dev/null | grep -q "127.0.0.1:${BACKEND_PORT} "; then
  echo "ERROR: powerhouse backend did not come up on ${BACKEND_PORT} — see its output above."
  exit 1
fi
for _ in $(seq 1 20); do
  ss -tlnp 2>/dev/null | grep -q "127.0.0.1:${BH_PORT} " && break
  sleep 0.25
done
if ! ss -tlnp 2>/dev/null | grep -q "127.0.0.1:${BH_PORT} "; then
  echo "ERROR: behuman backend did not come up on ${BH_PORT} — see its output above."
  exit 1
fi

# 5. Launch the proxy and supervise all three processes.
echo "proxy -> 127.0.0.1:${PROXY_PORT}   (funnel: https://powerhouse.tailbfd3d7.ts.net)"
echo
echo "On your phone: open the PWA, enter the shared token ONCE, then the OS passkey"
echo "sheet appears -> enroll. After that it authenticates by passkey. Ctrl-C to stop."
echo
env MOBILE_TERMINAL_CONFIG="$CONFIG" "$PY" server.py &
PROXY_PID=$!

wait -n "$BACKEND_PID" "$BH_PID" "$PROXY_PID" || true
cleanup
exit 1
