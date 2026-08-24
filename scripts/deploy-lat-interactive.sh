#!/usr/bin/env bash
# Interactive lat deployment for the post-2026-08-24 lat sudo policy: the
# `ubuntu` account is unprivileged, so deployment runs through ONE `ssh -t`
# session as a sudo-capable admin user (default: ben) with a single password
# prompt. Deploys the canonical closure to BOTH per-user lat backends
# (mobile-terminal@ben, mobile-terminal@bperritt) with per-user auth
# migration, dependency install, staged smoke check, timestamped backup,
# atomic activation, restart of ONLY the per-user unit, health verification,
# and automatic rollback. The lat hub (`mobile-terminal.service`) is never
# touched.
#
# Usage: scripts/deploy-lat-interactive.sh --apply [admin-user]
# Requires prior live ph and ps acceptance; this script is lat-only.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
# shellcheck source=../deployment-manifest.sh
source "$ROOT/deployment-manifest.sh"

if [[ ${1:-} != "--apply" ]]; then
  echo "Refusing lat deployment without explicit --apply (single admin-password prompt follows)." >&2
  exit 2
fi
ADMIN_USER=${2:-ben}
LAT_HOST=100.88.210.92
LAT_USERS=(ben bperritt)
declare -A LAT_PORTS=([ben]=8086 [bperritt]=8085)

command -v tar >/dev/null && command -v base64 >/dev/null

# Local preflight: closure exists and compiles with the local interpreter.
for f in "${DEPLOY_FILES[@]}" "${DEPLOY_GENERATED_FILES[@]}"; do
  [[ -f "$ROOT/$f" ]] || { echo "missing closure file: $f" >&2; exit 1; }
done
"$ROOT/.venv/bin/python" -m py_compile "${DEPLOY_PYTHON_FILES[@]}"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOCAL_WORK=$(mktemp -d)
trap 'rm -rf -- "$LOCAL_WORK"' EXIT
tar -czf "$LOCAL_WORK/closure.tar.gz" -C "$ROOT" "${DEPLOY_FILES[@]}" "${DEPLOY_GENERATED_FILES[@]}"

cat >"$LOCAL_WORK/remote-deploy.sh" <<'REMOTE'
set -euo pipefail
STAMP="$1"
PAYLOAD_DIR="${2:-$HOME}"
umask 077
WORK=$(mktemp -d "/tmp/mt-lat-deploy.XXXXXX")
trap 'rm -rf -- "$WORK" "$PAYLOAD_DIR/.mt-lat-closure.tar.gz" "$PAYLOAD_DIR/.mt-lat-remote-deploy.sh"' EXIT
mkdir -p "$WORK/closure"
tar -xzf "$PAYLOAD_DIR/.mt-lat-closure.tar.gz" -C "$WORK/closure"

deploy_user() {
  local user=$1 port=$2
  local repo="/home/$user/mobile-terminal"
  local unit="mobile-terminal@$user.service"
  echo "== $user: preflight"
  sudo test -d "$repo" || { echo "$user repo missing"; return 1; }
  sudo test -x "$repo/.venv/bin/python" || { echo "$user venv missing"; return 1; }
  sudo systemctl show --property=LoadState --value "$unit" | grep -qx loaded || { echo "$unit not loaded"; return 1; }

  echo "== $user: auth migration (rotate token, add marker, preserve rest)"
  sudo -u "$user" python3 - "$repo/mobile-terminal.env" <<'PY'
import secrets, sys
from pathlib import Path
path = Path(sys.argv[1])
lines = path.read_text().splitlines() if path.is_file() else []
output, rotated, marker = [], False, False
for line in lines:
    key = line.split('=', 1)[0].strip() if '=' in line else ''
    if key == 'MOBILE_TERMINAL_TOKEN':
        output.append('MOBILE_TERMINAL_TOKEN=' + secrets.token_urlsafe(32)); rotated = True
    elif key == 'MOBILE_TERMINAL_AUTH_MIGRATION':
        output.append('MOBILE_TERMINAL_AUTH_MIGRATION=passkey-bootstrap-v1'); marker = True
    else:
        output.append(line)
if not rotated:
    output.append('MOBILE_TERMINAL_TOKEN=' + secrets.token_urlsafe(32))
if not marker:
    output.append('MOBILE_TERMINAL_AUTH_MIGRATION=passkey-bootstrap-v1')
path.write_text('\n'.join(output) + '\n')
path.chmod(0o600)
print('migrated rotated=%s marker_added=%s' % (rotated, not marker))
PY

  echo "== $user: staging + dependencies"
  local stage="/home/$user/.mobile-terminal-stage-$STAMP"
  sudo rm -rf "$stage"
  sudo mkdir -p "$stage"
  sudo cp -a "$WORK/closure/." "$stage/"
  sudo chown -R "$user:$user" "$stage"
  sudo -u "$user" "$repo/.venv/bin/pip" install -q -r "$stage/requirements.txt"
  sudo -u "$user" env -C "$stage" "$repo/.venv/bin/python" - <<'PY'
import importlib
import server, mobile_terminal_config, webauthn_auth, provider_authority
for name in ("cryptography", "PIL", "regex", "wcwidth", "webauthn", "websockets"):
    importlib.import_module(name)
print("staged smoke ok")
PY

  echo "== $user: backup + activate"
  local backup="/home/$user/.mobile-terminal-deploy-backups/$STAMP"
  sudo -u "$user" mkdir -p "$backup"
  ( cd "$WORK/closure" && find . -type f -print0 ) | while IFS= read -r -d '' rel; do
    rel=${rel#./}
    if sudo test -f "$repo/$rel"; then
      sudo -u "$user" mkdir -p "$backup/$(dirname "$rel")"
      sudo cp -a "$repo/$rel" "$backup/$rel"
    fi
    sudo -u "$user" mkdir -p "$repo/$(dirname "$rel")"
    sudo install -o "$user" -g "$user" -m 0644 "$stage/$rel" "$repo/$rel"
  done
  sudo rm -rf "$stage"

  echo "== $user: restart $unit"
  sudo systemctl restart "$unit"
  sleep 3
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port/health" || echo 000)
  if sudo systemctl is-active --quiet "$unit" && [[ "$code" == 200 ]]; then
    echo "== $user: DEPLOYED (health $code); backup at $backup"
    return 0
  fi
  echo "== $user: FAILED (health $code) - rolling back"
  ( cd "$backup" && find . -type f -print0 ) | while IFS= read -r -d '' rel; do
    rel=${rel#./}
    sudo install -o "$user" -g "$user" -m 0644 "$backup/$rel" "$repo/$rel"
  done
  sudo systemctl restart "$unit"
  sleep 3
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port/health" || echo 000)
  echo "== $user: rolled back (health $code)"
  return 1
}

sudo -v
overall=0
deploy_user ben 8086 || overall=1
deploy_user bperritt 8085 || overall=1
echo "== lat deployment finished (0=ok): $overall"
exit "$overall"
REMOTE

scp -q "$LOCAL_WORK/closure.tar.gz" "$ADMIN_USER@$LAT_HOST:.mt-lat-closure.tar.gz"
scp -q "$LOCAL_WORK/remote-deploy.sh" "$ADMIN_USER@$LAT_HOST:.mt-lat-remote-deploy.sh"
echo "Connected to $ADMIN_USER@$LAT_HOST - you will be prompted once for the sudo password."
ssh -t "$ADMIN_USER@$LAT_HOST" "bash \$HOME/.mt-lat-remote-deploy.sh '$STAMP' \$HOME"
