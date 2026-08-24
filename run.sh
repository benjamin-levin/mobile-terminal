#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="$PWD/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  printf 'Required repository interpreter is missing: %s\n' "$PYTHON" >&2
  exit 1
fi

if [[ -f mobile-terminal.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source mobile-terminal.env
  set +a
fi

exec "$PYTHON" server.py "$@"
