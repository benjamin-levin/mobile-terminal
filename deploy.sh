#!/usr/bin/env bash
#
# Deploy mobile-terminal to the tailnet fleet.
#
#   ./deploy.sh                 # deploy to every reachable host
#   ./deploy.sh ps lat          # only the named hosts
#   ./deploy.sh --dry-run       # show per-file divergence, copy/restart nothing
#   ./deploy.sh --dry-run mbp   # dry-run a single host
#
# Deploys by file-copy (NOT git): backs up each remote file to <file>.bak-deploy,
# copies the local version, compile-checks server.py, restarts the service, and
# verifies /health. Hosts that don't answer SSH are skipped, not failed.
#
# Run it from the repo root. Branding stays env-driven (MOBILE_TERMINAL_LABEL on
# each host) — this never touches a host's label or env.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

FILES=(server.py static/app.js static/styles.css static/index.html static/sw.js)
SSH_OPTS=(-o ConnectTimeout=8 -o BatchMode=yes -o StrictHostKeyChecking=accept-new)

# name | ssh target | repo path | restart command | health port
HOSTS=(
  "ps|powerspec|/home/powerhouse/mobile-terminal|systemctl --user restart mobile-terminal.service|8085"
  "lat|ubuntu@100.88.210.92|/home/ubuntu/mobile-terminal|sudo systemctl restart mobile-terminal.service|8085"
  "mbp|100.80.7.0|~/mobile-terminal|launchctl kickstart -k gui/\$(id -u)/com.mobile-terminal.server|8085"
)

DRY=0
WANT=()
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) WANT+=("$a") ;;
  esac
done

wanted() {  # $1 = host name -> 0 if it should be deployed
  [ ${#WANT[@]} -eq 0 ] && return 0
  for w in "${WANT[@]}"; do [ "$w" = "$1" ] && return 0; done
  return 1
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

fail=0
for entry in "${HOSTS[@]}"; do
  IFS='|' read -r name tgt repo restart port <<<"$entry"
  wanted "$name" || continue
  echo "==================== $name ($tgt) ===================="

  if ! ssh "${SSH_OPTS[@]}" "$tgt" true 2>/dev/null; then
    echo "  SKIP: unreachable over SSH (offline, or Remote Login disabled on macOS)"
    continue
  fi

  # Per-file divergence preview.
  changed=()
  for f in "${FILES[@]}"; do
    if scp -q "${SSH_OPTS[@]}" "$tgt:$repo/$f" "$TMP/remote" 2>/dev/null; then
      n=$(diff "$TMP/remote" "$f" 2>/dev/null | grep -c '^[<>]')
    else
      n="new"   # file absent on remote
    fi
    [ "$n" != "0" ] && changed+=("$f")
    printf "  %-22s %s\n" "$f" "$([ "$n" = "0" ] && echo "unchanged" || echo "$n differing lines")"
  done

  if [ ${#changed[@]} -eq 0 ]; then
    echo "  nothing to deploy."
    continue
  fi
  if [ "$DRY" = "1" ]; then
    echo "  [dry-run] would copy: ${changed[*]}"
    continue
  fi

  # Backup + copy each changed file.
  for f in "${changed[@]}"; do
    ssh "${SSH_OPTS[@]}" "$tgt" "cp '$repo/$f' '$repo/$f.bak-deploy' 2>/dev/null" || true
    if scp -q "${SSH_OPTS[@]}" "$f" "$tgt:$repo/$f"; then
      echo "  copied $f"
    else
      echo "  ERROR copying $f"; fail=1; continue
    fi
  done

  # Compile-check server.py if it changed, using the host venv when present.
  if printf '%s\n' "${changed[@]}" | grep -qx "server.py"; then
    if ! ssh "${SSH_OPTS[@]}" "$tgt" "cd '$repo' && { .venv/bin/python -m py_compile server.py || python3 -m py_compile server.py; }" 2>/dev/null; then
      echo "  ABORT: server.py failed to compile on $name — NOT restarting (backup left at server.py.bak-deploy)"
      fail=1; continue
    fi
    echo "  server.py compiles"
  fi

  # Restart + verify.
  if ssh "${SSH_OPTS[@]}" "$tgt" "$restart" 2>/dev/null; then
    echo "  restarted"
  else
    echo "  ERROR: restart failed"; fail=1; continue
  fi
  sleep 2
  code=$(ssh "${SSH_OPTS[@]}" "$tgt" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$port/health" 2>/dev/null)
  if [ "$code" = "200" ]; then
    echo "  health=200 OK"
  else
    echo "  WARNING: health check returned '${code:-no-response}' (check the service on $name)"
    fail=1
  fi
done

echo "===================================================="
[ "$fail" = "0" ] && echo "Done." || echo "Done with warnings — see above."
exit $fail
