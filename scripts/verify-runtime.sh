#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/mobile-terminal.env"
PYTHON="$ROOT_DIR/.venv/bin/python"
SERVICE="mobile-terminal.service"
FAILED=0

if [[ ! -x "$PYTHON" ]]; then
  printf 'Required repository interpreter is missing: %s\n' "$PYTHON" >&2
  exit 1
fi

read_env_key() {
  "$PYTHON" - "$ENV_FILE" "$1" "$2" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
wanted = sys.argv[2]
default = sys.argv[3]
if not path.is_file():
    print(default)
    raise SystemExit(0)
with path.open(encoding="utf-8") as stream:
    for line in stream:
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == wanted:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                quote = value[0]
                value = value[1:-1]
                if quote == '"':
                    value = re.sub(r'\\([\\"`$])', r'\1', value)
            print(value or default)
            break
    else:
        print(default)
PY
}

STATE="$(systemctl --user is-active "$SERVICE" 2>/dev/null || true)"
MODE="$(read_env_key MOBILE_TERMINAL_PROVIDER_AUTHORITY off)"
PORT="$(read_env_key MOBILE_TERMINAL_PORT 8085)"
REVISION="$(git -C "$ROOT_DIR" rev-parse --short=7 HEAD 2>/dev/null || printf unknown)"
if git -C "$ROOT_DIR" diff --quiet && git -C "$ROOT_DIR" diff --cached --quiet; then
  TRACKED_STATE=clean
else
  TRACKED_STATE=dirty
fi

if TMUX_POLICY="$(tmux show-options -gv window-size 2>/dev/null)"; then
  MANUAL_WINDOWS="$(tmux list-windows -a -F '#{window_size}' 2>/dev/null | grep -cx manual || true)"
else
  TMUX_POLICY=unavailable
  MANUAL_WINDOWS=unavailable
fi

HEALTH=unavailable
if [[ "$STATE" == "active" && "$PORT" =~ ^[0-9]+$ ]]; then
  if ! HEALTH="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${PORT}/health" 2>/dev/null)"; then
    HEALTH=000
  fi
fi

ERROR_COUNT="$(journalctl --user -u "$SERVICE" --since '10 minutes ago' --lines=200 --no-pager -o cat 2>/dev/null \
  | { grep -E 'Traceback|ERROR|Exception|provider.*(fail|error)|resize.*(fail|error)' || true; } \
  | wc -l)"
ERROR_COUNT="${ERROR_COUNT//[[:space:]]/}"

read -r CLAUDE_HOOKS CODEX_HOOKS < <("$PYTHON" - <<'PY'
import json
from pathlib import Path

source = "mobile-terminal-provider-authority"
def count(path):
    try:
        document = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return 0
    hooks = document.get("hooks", {}) if isinstance(document, dict) else {}
    return sum(
        1
        for entries in hooks.values()
        if isinstance(entries, list)
        for entry in entries
        if isinstance(entry, dict) and entry.get("_mobile_terminal_source") == source
    )

home = Path.home()
print(count(home / ".claude/settings.json"), count(home / ".codex/hooks/hooks.json"))
PY
)

printf 'revision=%s\ntracked_tree=%s\nservice=%s\nhealth=%s\nprovider_mode=%s\n' \
  "$REVISION" "$TRACKED_STATE" "$STATE" "$HEALTH" "$MODE"
printf 'tmux_window_size=%s\nmanual_windows=%s\nclaude_hook_events=%s\ncodex_hook_events=%s\nrecent_error_count=%s\n' \
  "$TMUX_POLICY" "$MANUAL_WINDOWS" "$CLAUDE_HOOKS" "$CODEX_HOOKS" "$ERROR_COUNT"

[[ "$STATE" == "active" ]] || FAILED=1
[[ "$HEALTH" == "200" ]] || FAILED=1
[[ "$TMUX_POLICY" == "latest" ]] || FAILED=1
[[ "$MODE" == "off" || "$MODE" == "shadow" || "$MODE" == "enforce" ]] || FAILED=1
[[ "$ERROR_COUNT" == "0" ]] || FAILED=1

exit "$FAILED"
