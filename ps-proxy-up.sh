#!/usr/bin/env bash
# ps-proxy-up.sh — run the profiles proxy WITH passkeys on ps (this box).
#
# Runs the "powerhouse" backend on loopback:8090 and the proxy on :8085. The
# "behuman" backend is a separately provisioned system service on loopback:8091,
# running as the behuman OS user. The public funnel already points 443 ->
# 127.0.0.1:8085, so no funnel change is needed.
# Foreground process: Ctrl-C stops the powerhouse backend and proxy; the isolated
# behuman backend stays running. Revert to normal any time by restarting the systemd
# service (see the message this script prints if port 8085 is busy).
#
# This script starts processes but touches NO systemd units. You stop the standalone
# service yourself first; you restart it yourself to revert.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
CANONICAL_CONFIG="$ROOT/docs/ps-proxy.example.json"
RUNTIME_CONFIG="$ROOT/state/proxy/ps-proxy.runtime.json"
PUBLIC_HOSTNAME=powerspec.tailbfd3d7.ts.net
BH_PROXY_ENV="${MOBILE_TERMINAL_BH_PROXY_ENV:-$HOME/.config/mobile-terminal/behuman-proxy.env}"
BACKEND_PORT=8090
BH_PORT=8091
PROXY_PORT=8085

[ -x "$PY" ] || { echo "ERROR: venv python not found at $PY"; exit 1; }
[ -f "$CANONICAL_CONFIG" ] || { echo "ERROR: config not found: $CANONICAL_CONFIG"; exit 1; }

# The checked-in example still names the old hostname. Generate the runtime copy
# atomically, preserving relative-path routing as resolved from the canonical file.
mkdir -p "$ROOT/state/proxy"
"$PY" - "$CANONICAL_CONFIG" "$RUNTIME_CONFIG" "$PUBLIC_HOSTNAME" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2]).resolve()
hostname = sys.argv[3]
labels = hostname.split(".")
if (
    len(hostname) > 253
    or hostname != hostname.lower()
    or any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    )
):
    raise SystemExit(f"invalid public hostname: {hostname}")

config = json.loads(source.read_text())
if not isinstance(config, dict):
    raise SystemExit("proxy config root must be an object")
state_dir = config.get("stateDir", "state/proxy")
if isinstance(state_dir, str):
    resolved_state_dir = Path(state_dir).expanduser()
    if not resolved_state_dir.is_absolute():
        resolved_state_dir = source.parent / resolved_state_dir
    config["stateDir"] = str(resolved_state_dir.resolve())
config["rpId"] = hostname
config["origin"] = f"https://{hostname}"

temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
try:
    temporary.write_text(json.dumps(config, indent=2) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, destination)
finally:
    temporary.unlink(missing_ok=True)
PY
CONFIG="$RUNTIME_CONFIG"

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

# 3. Internal hop tokens (proxy <-> backend). The co-launched powerhouse backend
#    gets an ephemeral token. The independently provisioned behuman backend uses
#    a persistent token shared through an owner-only environment file.
HOP_POWERHOUSE="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(24))')"
[ -f "$BH_PROXY_ENV" ] || {
  echo "ERROR: behuman proxy environment not found: $BH_PROXY_ENV"
  echo "Run the bh account deployment first (see BH-DEPLOY-README.md)."
  exit 1
}
[ "$(stat -c '%u' "$BH_PROXY_ENV")" = "$(id -u)" ] || {
  echo "ERROR: $BH_PROXY_ENV must be owned by $(id -un)."
  exit 1
}
[ "$(stat -c '%a' "$BH_PROXY_ENV")" = "600" ] || {
  echo "ERROR: $BH_PROXY_ENV must have mode 600."
  exit 1
}
set -a
# shellcheck disable=SC1090
. "$BH_PROXY_ENV"
set +a
: "${MOBILE_TERMINAL_INTERNAL_TOKEN_BEHUMAN:?missing from $BH_PROXY_ENV}"
HOP_BEHUMAN="$MOBILE_TERMINAL_INTERNAL_TOKEN_BEHUMAN"
case "$HOP_BEHUMAN" in
  *[!A-Za-z0-9_-]*|'') echo "ERROR: invalid behuman internal token in $BH_PROXY_ENV"; exit 1 ;;
esac
export MOBILE_TERMINAL_INTERNAL_TOKEN="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(24))')"
export MOBILE_TERMINAL_INTERNAL_TOKEN_POWERHOUSE="$HOP_POWERHOUSE"
export MOBILE_TERMINAL_INTERNAL_TOKEN_BEHUMAN="$HOP_BEHUMAN"

# 4. Launch the powerhouse backend: loopback-only, validate the proxy's hop token,
#    no user token (the proxy is the only auth surface). MOBILE_TERMINAL_CONFIG is
#    unset here so server.py runs as a plain backend, not the proxy.
BACKEND_PID=""
PROXY_PID=""
CLEANED_UP=false
cleanup() {
  if [ "$CLEANED_UP" = true ]; then return; fi
  CLEANED_UP=true
  echo
  echo "stopping proxy processes..."
  if [ -n "$BACKEND_PID" ]; then kill "$BACKEND_PID" 2>/dev/null || true; fi
  if [ -n "$PROXY_PID" ]; then kill "$PROXY_PID" 2>/dev/null || true; fi
  if [ -n "$BACKEND_PID" ]; then wait "$BACKEND_PID" 2>/dev/null || true; fi
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

# Wait for the co-launched backend and the separately managed behuman backend to
# listen before starting the proxy.
for _ in $(seq 1 20); do
  ss -tlnp 2>/dev/null | grep -q "127.0.0.1:${BACKEND_PORT} " && break
  sleep 0.25
done
if ! ss -tlnp 2>/dev/null | grep -q "127.0.0.1:${BACKEND_PORT} "; then
  echo "ERROR: powerhouse backend did not come up on ${BACKEND_PORT} — see its output above."
  exit 1
fi
# The behuman backend is a separate system service; if it is down the proxy must
# still start so the gen profile stays up (the bh profile just shows unavailable).
for _ in $(seq 1 20); do
  ss -tlnp 2>/dev/null | grep -q "127.0.0.1:${BH_PORT} " && break
  sleep 0.25
done
if ss -tlnp 2>/dev/null | grep -q "127.0.0.1:${BH_PORT} "; then
  echo "backend (behuman system service) -> 127.0.0.1:${BH_PORT}"
else
  echo "WARNING: behuman backend not listening on ${BH_PORT}; the bh profile will show"
  echo "         'unavailable' until: sudo systemctl start mobile-terminal@behuman.service"
  echo "         Starting the proxy anyway so the gen profile stays up."
fi

# 5. Launch the proxy and supervise its co-launched powerhouse backend.
echo "proxy -> 127.0.0.1:${PROXY_PORT}   (funnel: https://${PUBLIC_HOSTNAME})"
echo
echo "On your phone: open the PWA, enter the shared token ONCE, then the OS passkey"
echo "sheet appears -> enroll. After that it authenticates by passkey. Ctrl-C to stop."
echo
env MOBILE_TERMINAL_CONFIG="$CONFIG" "$PY" server.py &
PROXY_PID=$!

wait -n "$BACKEND_PID" "$PROXY_PID" || true
cleanup
exit 1
