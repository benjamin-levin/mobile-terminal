#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/mobile-terminal.env"
PYTHON="$ROOT_DIR/.venv/bin/python"
SERVICE="mobile-terminal.service"
MODE="${1:-status}"
APPLY=0
CONFIRM_ENFORCE=0

usage() {
  printf '%s\n' \
    'Usage: scripts/provider-mode.sh status' \
    '       scripts/provider-mode.sh off|shadow|prefer [--apply]' \
    '       scripts/provider-mode.sh enforce [--apply --confirm-enforce]' \
    '' \
    'Mode changes are previews unless --apply is present. The script updates' \
    'only MOBILE_TERMINAL_PROVIDER_AUTHORITY and restarts only the local user' \
    'mobile-terminal.service. Enforcement is accepted only from prefer mode' \
    'and requires both explicit flags.'
}

shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --confirm-enforce) CONFIRM_ENFORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ ! -x "$PYTHON" ]]; then
  printf 'Required repository interpreter is missing: %s\n' "$PYTHON" >&2
  exit 1
fi

read_mode() {
  "$PYTHON" - "$ENV_FILE" <<'PY'
import re
import sys
from pathlib import Path


def decode(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        quote = value[0]
        value = value[1:-1]
        if quote == '"':
            value = re.sub(r'\\([\\"`$])', r'\1', value)
    return value


path = Path(sys.argv[1])
if not path.is_file():
    print("missing")
    raise SystemExit(0)
with path.open(encoding="utf-8") as stream:
    for line in stream:
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "MOBILE_TERMINAL_PROVIDER_AUTHORITY":
            print(decode(value) or "unset")
            break
    else:
        print("off")
PY
}

read_port() {
  "$PYTHON" - "$ENV_FILE" <<'PY'
import re
import sys
from pathlib import Path


def decode(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        quote = value[0]
        value = value[1:-1]
        if quote == '"':
            value = re.sub(r'\\([\\"`$])', r'\1', value)
    return value


path = Path(sys.argv[1])
if not path.is_file():
    print("8085")
    raise SystemExit(0)
with path.open(encoding="utf-8") as stream:
    for line in stream:
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "MOBILE_TERMINAL_PORT":
            print(decode(value) or "8085")
            break
    else:
        print("8085")
PY
}

service_state() {
  systemctl --user is-active "$SERVICE" 2>/dev/null || true
}

wait_for_service_health() {
  local attempt code
  for attempt in {1..10}; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:${HEALTH_PORT}/health" 2>/dev/null || true)"
    if [[ "$(service_state)" == "active" && "$code" == "200" ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

CURRENT="$(read_mode)"
if [[ "$MODE" == "status" ]]; then
  if [[ "$APPLY" == 1 || "$CONFIRM_ENFORCE" == 1 ]]; then
    echo "Status does not accept mutation confirmation flags" >&2
    exit 2
  fi
  STATE="$(service_state)"
  printf 'provider_mode=%s\nservice=%s\n' "$CURRENT" "$STATE"
  [[ "$STATE" == "active" ]]
  exit
fi

case "$MODE" in
  off|shadow|prefer|enforce) ;;
  -h|--help) usage; exit 0 ;;
  *) echo "Invalid provider mode: $MODE" >&2; usage >&2; exit 2 ;;
esac

if [[ "$CURRENT" == "missing" ]]; then
  echo "Refusing provider-mode change: mobile-terminal.env is missing" >&2
  exit 1
fi
if [[ "$APPLY" == 1 ]]; then
  chmod 600 "$ENV_FILE"
fi
if [[ "$MODE" != "enforce" && "$CONFIRM_ENFORCE" == 1 ]]; then
  echo "--confirm-enforce is valid only with enforce" >&2
  exit 2
fi

if [[ "$MODE" == "enforce" && "$CURRENT" != "prefer" && "$CURRENT" != "enforce" ]]; then
  echo "Refusing transition to enforce: enable and verify prefer first" >&2
  exit 1
fi
if [[ "$MODE" == "enforce" && "$APPLY" == 1 && "$CONFIRM_ENFORCE" != "1" ]]; then
  echo "Refusing enforcement without --confirm-enforce after live prefer acceptance" >&2
  exit 1
fi

if [[ "$CURRENT" == "$MODE" ]]; then
  STATE="$(service_state)"
  printf 'provider_mode=%s\nservice=%s\nchanged=no\n' "$CURRENT" "$STATE"
  [[ "$STATE" == "active" ]]
  exit
fi

if [[ "$APPLY" != 1 ]]; then
  STATE="$(service_state)"
  printf 'provider_mode=%s\nrequested_mode=%s\nservice=%s\ndry_run=yes\nchanged=no\n' \
    "$CURRENT" "$MODE" "$STATE"
  exit 0
fi

HEALTH_PORT="$(read_port)"
[[ "$HEALTH_PORT" =~ ^[0-9]+$ ]] || {
  echo "Refusing provider-mode change: configured health port is invalid" >&2
  exit 1
}
command -v curl >/dev/null 2>&1 || {
  echo "Refusing provider-mode change: curl is unavailable for health verification" >&2
  exit 1
}

ROLLBACK="$(mktemp --tmpdir="$(dirname "$ENV_FILE")" .mobile-terminal.env.rollback.XXXXXX)"
chmod 600 "$ROLLBACK"
cp "$ENV_FILE" "$ROLLBACK"
chmod 600 "$ROLLBACK"
cleanup() { rm -f "$ROLLBACK"; }
trap cleanup EXIT

"$PYTHON" - "$ENV_FILE" "$MODE" <<'PY'
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
mode = sys.argv[2]
original = path.read_text().splitlines()
key = "MOBILE_TERMINAL_PROVIDER_AUTHORITY"
replacement = f"{key}={mode}"
updated = []
seen = False
for line in original:
    if not line.lstrip().startswith("#") and "=" in line and line.split("=", 1)[0].strip() == key:
        if not seen:
            updated.append(replacement)
            seen = True
    else:
        updated.append(line)
if not seen:
    updated.append(replacement)

fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(updated) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
except BaseException:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY

if ! systemctl --user restart "$SERVICE" || ! wait_for_service_health; then
  echo "Provider-mode restart or health verification failed; restoring the previous env file" >&2
  cp "$ROLLBACK" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  if ! systemctl --user restart "$SERVICE" || ! wait_for_service_health; then
    echo "Provider-mode rollback restored the env file, but prior service health was not recovered" >&2
  fi
  exit 1
fi

VERIFIED="$(read_mode)"
if [[ "$VERIFIED" != "$MODE" ]]; then
  echo "Provider-mode verification failed; restoring the previous env file" >&2
  cp "$ROLLBACK" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  if ! systemctl --user restart "$SERVICE" || ! wait_for_service_health; then
    echo "Provider-mode rollback restored the env file, but prior service health was not recovered" >&2
  fi
  exit 1
fi

printf 'provider_mode=%s\nservice=active\nhealth=200\nchanged=yes\n' "$VERIFIED"
