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
# copies the local version, smoke-checks the Python runtime, restarts the service,
# and verifies /health. Hosts that don't answer SSH are skipped, not failed.
#
# Run it from the repo root. Branding stays env-driven (MOBILE_TERMINAL_LABEL on
# each host) — this never touches a host's label or env.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

FILES=(server.py mobile_terminal_config.py proxy.py proxy_auth.py webauthn_auth.py static/app.js static/passkey.js static/styles.css static/index.html static/sw.js)
SSH_OPTS=(-o ConnectTimeout=8 -o BatchMode=yes -o StrictHostKeyChecking=accept-new)

# name | ssh target | repo path | restart command | health port
HOSTS=(
  "ps|powerspec|/home/powerhouse/mobile-terminal|systemctl --user restart mobile-terminal.service|8085"
  "mbp|100.80.7.0|~/mobile-terminal|launchctl kickstart -k gui/\$(id -u)/com.mobile-terminal.server|8085"
)

LAT_SSH=ubuntu@100.88.210.92
LAT_USERS=("ben|8086" "bperritt|8085")

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

deploy_lat_user() {
  local user=$1
  local port=$2
  local repo="/home/$user/mobile-terminal"
  local f n staged dest parent py_files
  local changed=()

  echo "==================== lat/$user ($LAT_SSH) ===================="

  if ! ssh "${SSH_OPTS[@]}" "$LAT_SSH" true 2>/dev/null; then
    echo "  SKIP: unreachable over SSH"
    return
  fi

  # Per-file divergence preview, reading through sudo for user-owned trees.
  for f in "${FILES[@]}"; do
    if ssh "${SSH_OPTS[@]}" "$LAT_SSH" "sudo -n cat '$repo/$f'" >"$TMP/remote" 2>/dev/null; then
      n=$(diff "$TMP/remote" "$f" 2>/dev/null | grep -c '^[<>]')
    else
      n="new"   # file absent or unreadable on remote
    fi
    [ "$n" != "0" ] && changed+=("$f")
    printf "  %-22s %s\n" "$f" "$([ "$n" = "0" ] && echo "unchanged" || echo "$n differing lines")"
  done

  if [ ${#changed[@]} -eq 0 ]; then
    echo "  nothing to deploy."
    return
  fi
  if [ "$DRY" = "1" ]; then
    echo "  [dry-run] would copy: ${changed[*]}"
    return
  fi

  if ! ssh "${SSH_OPTS[@]}" "$LAT_SSH" "mkdir -p '/home/ubuntu/mobile-terminal/incoming'" 2>/dev/null; then
    echo "  ERROR: could not create staging directory"
    fail=1
    return
  fi

  # Stage as the SSH user, then install into each user's tree with ownership preserved.
  local copy_failed=0
  for f in "${changed[@]}"; do
    staged="/home/ubuntu/mobile-terminal/incoming/${user}-${f//\//_}"
    dest="$repo/$f"
    parent=${dest%/*}
    if scp -q "${SSH_OPTS[@]}" "$f" "$LAT_SSH:$staged" &&
       ssh "${SSH_OPTS[@]}" "$LAT_SSH" "sudo -u '$user' mkdir -p '$parent' && if sudo test -e '$dest'; then sudo cp -a '$dest' '$dest.bak-deploy'; fi && sudo install -o '$user' -g '$user' -m 0644 '$staged' '$dest'"; then
      echo "  copied $f"
    else
      echo "  ERROR copying $f"; fail=1; copy_failed=1
    fi
  done
  if [ "$copy_failed" = "1" ]; then
    echo "  ABORT: not all runtime files were copied on lat/$user — NOT restarting"
    return
  fi

  # Compile copied Python files and import-check the per-user runtime.
  py_files=
  for f in "${changed[@]}"; do
    case "$f" in
      *.py) py_files+=" '$f'" ;;
    esac
  done
  if [ -n "$py_files" ]; then
    if ! ssh "${SSH_OPTS[@]}" "$LAT_SSH" "sudo -u '$user' env -C '$repo' /home/ubuntu/mobile-terminal/.venv/bin/python -m py_compile$py_files && sudo -u '$user' env -C '$repo' /home/ubuntu/mobile-terminal/.venv/bin/python -c 'import server; from mobile_terminal_config import ConfigError, load_runtime_config; import webauthn_auth'" 2>/dev/null; then
      echo "  ABORT: Python runtime smoke check failed on lat/$user — NOT restarting (backups left at *.bak-deploy)"
      fail=1
      return
    fi
    echo "  Python runtime smoke check passed"
  fi

  if ssh "${SSH_OPTS[@]}" "$LAT_SSH" "sudo systemctl restart mobile-terminal@$user.service" 2>/dev/null; then
    echo "  restarted"
  else
    echo "  ERROR: restart failed"; fail=1
    return
  fi
  sleep 2
  local code
  code=$(ssh "${SSH_OPTS[@]}" "$LAT_SSH" "curl -s -o /dev/null -w '%{http_code}' http://localhost:$port/health" 2>/dev/null)
  if [ "$code" = "200" ]; then
    echo "  health=200 OK"
  else
    echo "  WARNING: health check returned '${code:-no-response}' (check the service on lat/$user)"
    fail=1
  fi
}

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
  copy_failed=0
  for f in "${changed[@]}"; do
    ssh "${SSH_OPTS[@]}" "$tgt" "cp '$repo/$f' '$repo/$f.bak-deploy' 2>/dev/null" || true
    if scp -q "${SSH_OPTS[@]}" "$f" "$tgt:$repo/$f"; then
      echo "  copied $f"
    else
      echo "  ERROR copying $f"; fail=1; copy_failed=1
    fi
  done
  if [ "$copy_failed" = "1" ]; then
    echo "  ABORT: not all runtime files were copied on $name — NOT restarting"
    continue
  fi

  # Compile and import-check Python runtime files using the interpreter the service uses.
  if printf '%s\n' "${changed[@]}" | grep -Eq '^(server|mobile_terminal_config|proxy|proxy_auth|webauthn_auth)\.py$'; then
    if ! ssh "${SSH_OPTS[@]}" "$tgt" "cd '$repo' && if [ -x .venv/bin/python ]; then py=.venv/bin/python; else py=python3; fi && \"\$py\" -m py_compile server.py mobile_terminal_config.py proxy.py proxy_auth.py webauthn_auth.py && \"\$py\" -c 'import server; from mobile_terminal_config import ConfigError, load_runtime_config; from proxy import ProxyServer; import proxy_auth; import webauthn_auth'" 2>/dev/null; then
      echo "  ABORT: Python runtime smoke check failed on $name — NOT restarting (backups left at *.bak-deploy)"
      fail=1; continue
    fi
    echo "  Python runtime smoke check passed"
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

if wanted lat; then
  for entry in "${LAT_USERS[@]}"; do
    IFS='|' read -r user port <<<"$entry"
    deploy_lat_user "$user" "$port"
  done
fi

echo "===================================================="
[ "$fail" = "0" ] && echo "Done." || echo "Done with warnings — see above."
exit $fail
