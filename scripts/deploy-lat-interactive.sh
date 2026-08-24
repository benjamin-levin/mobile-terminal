#!/usr/bin/env bash
# Interactive lat deployment for the post-2026-08-24 lat privilege policy:
# `ubuntu` is unprivileged and ben's sudo is command-restricted, so the remote
# payload is executed BY ROOT (the operator runs it via `su`). The local half
# only builds and uploads the payload over ben's key-based SSH.
#
# The payload deploys the canonical closure to BOTH per-user lat backends
# (mobile-terminal@ben, mobile-terminal@bperritt): per-user auth migration
# (token rotation + marker), per-user repository virtualenv creation with
# pinned dependencies (replacing the banned shared-ubuntu-venv drift via a
# systemd drop-in), staged smoke check, timestamped backup, atomic activation,
# restart of ONLY the per-user units, health verification, and automatic
# rollback. The lat hub (`mobile-terminal.service`) is never touched.
#
# Usage: scripts/deploy-lat-interactive.sh --apply [upload-user]
# then, on lat as root:  bash ~ben/.mt-lat-remote-deploy.sh <STAMP> ~ben
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
# shellcheck source=../deployment-manifest.sh
source "$ROOT/deployment-manifest.sh"

if [[ ${1:-} != "--apply" ]]; then
  echo "Refusing lat payload upload without explicit --apply." >&2
  exit 2
fi
ADMIN_USER=${2:-ben}
LAT_HOST=100.88.210.92

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
if [[ $(id -u) -ne 0 ]]; then
  echo "This payload must run as root (su -, then rerun)." >&2
  exit 2
fi
umask 077
WORK=$(mktemp -d "/tmp/mt-lat-deploy.XXXXXX")
trap 'rm -rf -- "$WORK" "$PAYLOAD_DIR/.mt-lat-closure.tar.gz" "$PAYLOAD_DIR/.mt-lat-remote-deploy.sh"' EXIT
mkdir -p "$WORK/closure"
tar -xzf "$PAYLOAD_DIR/.mt-lat-closure.tar.gz" -C "$WORK/closure"

ensure_unit_dropin() {
  local dropin_dir=/etc/systemd/system/mobile-terminal@.service.d
  local dropin="$dropin_dir/10-repo-interpreter.conf"
  if [[ -f "$dropin" ]]; then
    echo "== unit drop-in already present"
    return 0
  fi
  echo "== installing per-user interpreter drop-in for mobile-terminal@.service"
  mkdir -p "$dropin_dir"
  cat >"$dropin" <<'CONF'
[Service]
ExecStart=
ExecStart=/home/%i/mobile-terminal/.venv/bin/python /home/%i/mobile-terminal/server.py
CONF
  chmod 644 "$dropin"
  systemctl daemon-reload
}

ensure_user_venv() {
  local user=$1
  local repo="/home/$user/mobile-terminal"
  if [[ -x "$repo/.venv/bin/python" ]]; then
    echo "== $user: repository venv present"
  else
    echo "== $user: creating repository venv"
    if ! runuser -u "$user" -- python3 -m venv "$repo/.venv"; then
      echo "== venv module unavailable; installing python3-venv"
      DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv
      runuser -u "$user" -- python3 -m venv "$repo/.venv"
    fi
    runuser -u "$user" -- "$repo/.venv/bin/pip" install -q --upgrade pip
  fi
  runuser -u "$user" -- "$repo/.venv/bin/pip" install -q -r "$WORK/closure/requirements.txt"
}

deploy_user() {
  local user=$1 port=$2
  local repo="/home/$user/mobile-terminal"
  local unit="mobile-terminal@$user.service"
  echo "== $user: preflight"
  test -d "$repo" || { echo "$user repo missing"; return 1; }
  systemctl show --property=LoadState --value "$unit" | grep -qx loaded || { echo "$unit not loaded"; return 1; }
  ensure_user_venv "$user" || return 1

  echo "== $user: auth migration (rotate token, add marker, preserve rest)"
  runuser -u "$user" -- python3 - "$repo/mobile-terminal.env" <<'PY'
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

  echo "== $user: staged smoke check"
  local stage="/home/$user/.mobile-terminal-stage-$STAMP"
  rm -rf "$stage"
  mkdir -p "$stage"
  cp -a "$WORK/closure/." "$stage/"
  chown -R "$user:$user" "$stage"
  ( cd "$stage" && runuser -u "$user" -- "$repo/.venv/bin/python" - <<'PY'
import importlib
import server, mobile_terminal_config, webauthn_auth, provider_authority
for name in ("cryptography", "PIL", "regex", "wcwidth", "webauthn", "websockets"):
    importlib.import_module(name)
print("staged smoke ok")
PY
  ) || { rm -rf "$stage"; echo "$user staged smoke failed"; return 1; }

  echo "== $user: backup + activate"
  local backup="/home/$user/.mobile-terminal-deploy-backups/$STAMP"
  runuser -u "$user" -- mkdir -p "$backup"
  ( cd "$WORK/closure" && find . -type f -print0 ) | while IFS= read -r -d '' rel; do
    rel=${rel#./}
    if [[ -f "$repo/$rel" ]]; then
      runuser -u "$user" -- mkdir -p "$backup/$(dirname "$rel")"
      cp -a "$repo/$rel" "$backup/$rel"
      chown "$user:$user" "$backup/$rel"
    fi
    runuser -u "$user" -- mkdir -p "$repo/$(dirname "$rel")"
    install -o "$user" -g "$user" -m 0644 "$stage/$rel" "$repo/$rel"
  done
  rm -rf "$stage"

  echo "== $user: restart $unit"
  systemctl restart "$unit"
  sleep 3
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port/health" || echo 000)
  if systemctl is-active --quiet "$unit" && [[ "$code" == 200 ]]; then
    echo "== $user: DEPLOYED (health $code); backup at $backup"
    return 0
  fi
  echo "== $user: FAILED (health $code) - rolling back"
  ( cd "$backup" && find . -type f -print0 ) | while IFS= read -r -d '' rel; do
    rel=${rel#./}
    install -o "$user" -g "$user" -m 0644 "$backup/$rel" "$repo/$rel"
  done
  systemctl restart "$unit"
  sleep 3
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port/health" || echo 000)
  echo "== $user: rolled back (health $code)"
  return 1
}

ensure_unit_dropin
overall=0
deploy_user ben 8086 || overall=1
deploy_user bperritt 8085 || overall=1
echo "== lat deployment finished (0=ok): $overall"
exit "$overall"
REMOTE

scp -q "$LOCAL_WORK/closure.tar.gz" "$ADMIN_USER@$LAT_HOST:.mt-lat-closure.tar.gz"
scp -q "$LOCAL_WORK/remote-deploy.sh" "$ADMIN_USER@$LAT_HOST:.mt-lat-remote-deploy.sh"
echo "Payload uploaded. On lat, as root:"
echo "  bash ~$ADMIN_USER/.mt-lat-remote-deploy.sh '$STAMP' ~$ADMIN_USER"
