#!/usr/bin/env bash
set -euo pipefail

ACTION=${1:-}
shift || true

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

case "$ACTION" in
  preflight) ;;
  prepare|smoke|activate|cleanup)
    [[ ${1:-} == "--apply" ]] || fail "$ACTION requires explicit --apply confirmation"
    shift
    ;;
  *) fail "unknown remote deployment action" ;;
esac

validate_runtime_tuple() {
  local ssh_user=$1 runtime_user=$2 repo=$3 interpreter=$4 scope=$5 service=$6 port=$7
  [[ "$port" =~ ^[0-9]+$ ]] || fail "invalid health port"
  [[ "$repo" == /* && "$interpreter" == "$repo/.venv/bin/python" ]] || fail "invalid repository interpreter"

  case "$scope|$service|$runtime_user" in
    "user-systemd|mobile-terminal.service|powerhouse"|"user-systemd|mobile-terminal-proxy.service|powerhouse"|"system-systemd|mobile-terminal@ben.service|ben"|"system-systemd|mobile-terminal@bperritt.service|bperritt"|"launchd|com.mobile-terminal.server|powerhouse") ;;
    *) fail "service is not allowlisted for runtime user" ;;
  esac

  case "$scope" in
    user-systemd|launchd)
      [[ "$ssh_user" == "$runtime_user" ]] || fail "direct service target identity mismatch"
      ;;
    system-systemd)
      [[ "$ssh_user" == "ubuntu" ]] || fail "system service SSH identity mismatch"
      ;;
  esac
}

run_target() {
  if [[ "$SERVICE_SCOPE" == "system-systemd" ]]; then
    sudo -n -u "$RUNTIME_USER" -- "$@"
  else
    "$@"
  fi
}

run_in_tree() {
  local tree=$1
  shift
  run_target bash -c '
    tree=$1
    home=$2
    shift 2
    cd -- "$tree"
    exec env -i HOME="$home" PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 "$@"
  ' _ "$tree" "$TARGET_HOME" "$@"
}

service_property() {
  local property=$1
  case "$SERVICE_SCOPE" in
    user-systemd) systemctl --user show --property="$property" --value "$SERVICE_NAME" ;;
    system-systemd) sudo -n systemctl show --property="$property" --value "$SERVICE_NAME" ;;
    *) return 1 ;;
  esac
}

auth_migration_ready() {
  run_target "$INTERPRETER" - "$REPO/mobile-terminal.env" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(1)
values = {}
with path.open(encoding="utf-8") as stream:
    for line in stream:
        separator = line.find("=")
        if line.lstrip().startswith("#") or separator < 0:
            continue
        key = line[:separator].strip()
        if key not in {"MOBILE_TERMINAL_AUTH_MIGRATION", "MOBILE_TERMINAL_NO_TOKEN"}:
            continue
        value = line[separator + 1 :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
no_token = values.get("MOBILE_TERMINAL_NO_TOKEN", "").lower() in {"1", "true", "yes"}
ready = values.get("MOBILE_TERMINAL_AUTH_MIGRATION") == "passkey-bootstrap-v1"
raise SystemExit(0 if no_token or ready else 1)
PY
}

service_active() {
  case "$SERVICE_SCOPE" in
    user-systemd) systemctl --user is-active --quiet "$SERVICE_NAME" ;;
    system-systemd) sudo -n systemctl is-active --quiet "$SERVICE_NAME" ;;
    launchd) launchctl print "gui/$(id -u)/$SERVICE_NAME" >/dev/null ;;
  esac
}

restart_service() {
  case "$SERVICE_SCOPE" in
    user-systemd) systemctl --user restart "$SERVICE_NAME" ;;
    system-systemd) sudo -n systemctl restart "$SERVICE_NAME" ;;
    launchd) launchctl kickstart -k "gui/$(id -u)/$SERVICE_NAME" ;;
  esac
}

health_code() {
  curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$HEALTH_PORT/health"
}

load_staged_manifest() {
  local stage=$1 file
  [[ "$stage" == "$HOME/.mobile-terminal-deploy."* ]] || fail "invalid staging directory"
  [[ -f "$stage/tree/deployment-manifest.sh" ]] || fail "staged deployment manifest is missing"
  # shellcheck disable=SC1090
  source "$stage/tree/deployment-manifest.sh"
  [[ ${#DEPLOY_FILES[@]} -gt 0 ]] || fail "staged runtime manifest is empty"
  for file in "${DEPLOY_FILES[@]}"; do
    [[ "$file" != /* && "$file" != *".."* && -f "$stage/tree/$file" && ! -L "$stage/tree/$file" ]] ||
      fail "invalid or missing staged runtime file: $file"
  done
}

validate_generated_files() {
  local stage=$1 file
  for file in "${DEPLOY_GENERATED_FILES[@]}"; do
    [[ "$file" != /* && "$file" != *".."* && -f "$stage/tree/$file" && ! -L "$stage/tree/$file" ]] ||
      fail "invalid or missing staged generated file: $file"
  done
  DEPLOY_ACTIVATION_FILES=("${DEPLOY_FILES[@]}" "${DEPLOY_GENERATED_FILES[@]}")
}

preflight() {
  local ssh_user=$1 runtime_user=$2 repo=$3 interpreter=$4 scope=$5 service=$6 port=$7 imports=$8
  local actual_user owner prefix load_state workdir exec_start launch_state code

  validate_runtime_tuple "$ssh_user" "$runtime_user" "$repo" "$interpreter" "$scope" "$service" "$port"
  actual_user=$(id -un)
  [[ "$actual_user" == "$ssh_user" ]] || fail "SSH identity mismatch"

  RUNTIME_USER=$runtime_user
  REPO=$repo
  INTERPRETER=$interpreter
  SERVICE_SCOPE=$scope
  SERVICE_NAME=$service
  HEALTH_PORT=$port
  TARGET_HOME=${repo%/mobile-terminal}

  command -v bash >/dev/null || fail "bash is unavailable"
  command -v tar >/dev/null || fail "tar is unavailable"
  command -v curl >/dev/null || fail "curl is unavailable"
  command -v node >/dev/null || fail "node is unavailable"
  command -v npm >/dev/null || fail "npm is unavailable"

  if [[ "$scope" == "system-systemd" ]]; then
    sudo -n -u "$runtime_user" -- true || fail "cannot become runtime user"
    sudo -n systemctl show --property=LoadState --value "$service" >/dev/null || fail "cannot inspect exact service"
    run_target test -d "$repo" || fail "repository path is missing"
    owner=$(sudo -n stat -c '%U' "$repo" 2>/dev/null || sudo -n stat -f '%Su' "$repo")
  else
    [[ -d "$repo" ]] || fail "repository path is missing"
    owner=$(stat -c '%U' "$repo" 2>/dev/null || stat -f '%Su' "$repo")
  fi
  [[ "$owner" == "$runtime_user" ]] || fail "repository owner mismatch"
  run_target test -x "$interpreter" || fail "target interpreter is missing"
  auth_migration_ready ||
    fail "authentication migration is not ready; run install.sh --apply --migrate-token-auth before deployment"

  prefix=$repo/.venv
  run_target "$interpreter" -c 'import pathlib, sys; expected = pathlib.Path(sys.argv[1]).resolve(); actual = pathlib.Path(sys.prefix).resolve(); raise SystemExit(0 if actual == expected else 1)' "$prefix" ||
    fail "interpreter is not the target repository virtualenv"
  run_in_tree "$repo" "$interpreter" -c 'import importlib, sys; [importlib.import_module(name) for name in sys.argv[1].split(",") if name]' "$imports" ||
    fail "runtime dependency import preflight failed"
  run_in_tree "$repo" "$interpreter" -m pip install --dry-run --no-index --disable-pip-version-check -r requirements.txt >/dev/null ||
    fail "runtime requirements are not satisfied by the target virtualenv"

  case "$scope" in
    user-systemd|system-systemd)
      load_state=$(service_property LoadState)
      workdir=$(service_property WorkingDirectory)
      exec_start=$(service_property ExecStart)
      [[ "$load_state" == "loaded" ]] || fail "exact service is not loaded"
      [[ "$workdir" == "$repo" ]] || fail "service working directory mismatch"
      [[ "$exec_start" == *"$interpreter"* && "$exec_start" == *"$repo/server.py"* ]] ||
        fail "service interpreter or entrypoint mismatch"
      ;;
    launchd)
      launch_state=$(launchctl print "gui/$(id -u)/$service") || fail "exact launchd service is not loaded"
      [[ "$launch_state" == *"$repo"* ]] || fail "launchd repository path mismatch"
      ;;
  esac
  service_active || fail "exact service is not active"
  code=$(health_code || true)
  [[ "$code" == "200" ]] || fail "current service health check failed"
  printf 'remote preflight passed: %s\n' "$service"
}

prepare() {
  local stage
  stage=$(mktemp -d "$HOME/.mobile-terminal-deploy.XXXXXX")
  chmod 0755 "$stage"
  printf '%s\n' "$stage"
}

smoke() {
  local stage=$1 runtime_user=$2 repo=$3 interpreter=$4 scope=$5 service=$6 port=$7
  local javascript_file ssh_user

  ssh_user=$(id -un)
  validate_runtime_tuple "$ssh_user" "$runtime_user" "$repo" "$interpreter" "$scope" "$service" "$port"
  RUNTIME_USER=$runtime_user
  REPO=$repo
  INTERPRETER=$interpreter
  SERVICE_SCOPE=$scope
  SERVICE_NAME=$service
  HEALTH_PORT=$port
  TARGET_HOME=${repo%/mobile-terminal}

  [[ -f "$stage/runtime.tgz" ]] || fail "staged archive is missing"
  mkdir -p "$stage/tree"
  tar -xzf "$stage/runtime.tgz" -C "$stage/tree"
  load_staged_manifest "$stage"
  if ! (
    cd "$stage/tree"
    env -i HOME="$HOME" PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin npm ci --ignore-scripts --no-audit --no-fund
  ) >/dev/null 2>&1; then
    fail "locked JavaScript dependency staging failed"
  fi
  validate_generated_files "$stage"
  chmod -R a+rX "$stage/tree"

  run_in_tree "$stage/tree" "$interpreter" -c '
import pathlib
import sys
for name in sys.argv[1:]:
    path = pathlib.Path(name)
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
' "${DEPLOY_PYTHON_FILES[@]}"
  run_in_tree "$stage/tree" "$interpreter" -c 'import server, provider_authority, provider_binding_hook, proxy, proxy_auth, webauthn_auth; from mobile_terminal_config import ConfigError, load_runtime_config; from proxy import ProxyServer'

  for javascript_file in "${DEPLOY_JAVASCRIPT_FILES[@]}" "${DEPLOY_GENERATED_FILES[@]}"; do
    case "$javascript_file" in
      *.js) run_in_tree "$stage/tree" node --check "$javascript_file" >/dev/null ;;
    esac
  done
  printf 'staged smoke check passed\n'
}

activate() {
  local stage=$1 runtime_user=$2 repo=$3 interpreter=$4 scope=$5 service=$6 port=$7
  local backup file parent code rollback_code ssh_user
  local had_files=()

  ssh_user=$(id -un)
  validate_runtime_tuple "$ssh_user" "$runtime_user" "$repo" "$interpreter" "$scope" "$service" "$port"
  RUNTIME_USER=$runtime_user
  REPO=$repo
  INTERPRETER=$interpreter
  SERVICE_SCOPE=$scope
  SERVICE_NAME=$service
  HEALTH_PORT=$port
  TARGET_HOME=${repo%/mobile-terminal}
  STAGE_TO_CLEAN=$stage
  trap 'rm -rf "$STAGE_TO_CLEAN"' EXIT
  load_staged_manifest "$stage"
  validate_generated_files "$stage"

  backup="$repo/.mobile-terminal-deploy-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
  run_target mkdir -p "$backup"

  for file in "${DEPLOY_ACTIVATION_FILES[@]}"; do
    parent=${file%/*}
    [[ "$parent" == "$file" ]] && parent=.
    run_target mkdir -p "$backup/$parent"
    if run_target test -e "$repo/$file"; then
      had_files+=("$file")
      run_target cp -a "$repo/$file" "$backup/$file"
    fi
  done

  restore_files() {
    local restore_file restore_parent present_file was_present
    for restore_file in "${DEPLOY_ACTIVATION_FILES[@]}"; do
      restore_parent=${restore_file%/*}
      [[ "$restore_parent" == "$restore_file" ]] && restore_parent=.
      run_target mkdir -p "$repo/$restore_parent"
      run_target rm -f "$repo/$restore_file.deploy-new"
      was_present=0
      for present_file in "${had_files[@]}"; do
        [[ "$present_file" == "$restore_file" ]] && was_present=1
      done
      if [[ "$was_present" == 1 ]]; then
        run_target rm -f "$repo/$restore_file"
        run_target cp -a "$backup/$restore_file" "$repo/$restore_file"
      else
        run_target rm -f "$repo/$restore_file"
      fi
    done
  }

  rollback_signal() {
    trap - HUP INT TERM
    restore_files || true
    restart_service || true
    printf 'ERROR: activation interrupted; runtime files restored\n' >&2
    exit 1
  }
  trap rollback_signal HUP INT TERM

  for file in "${DEPLOY_ACTIVATION_FILES[@]}"; do
    parent=${file%/*}
    [[ "$parent" == "$file" ]] && parent=.
    if ! run_target mkdir -p "$repo/$parent" ||
       ! run_target cp "$stage/tree/$file" "$repo/$file.deploy-new" ||
       ! run_target chmod 0644 "$repo/$file.deploy-new" ||
       ! run_target mv -f "$repo/$file.deploy-new" "$repo/$file"; then
      restore_files
      trap - HUP INT TERM
      code=$(health_code || true)
      if [[ "$code" != "200" ]] || ! service_active; then
        fail "activation copy failed; files restored but prior service health check failed"
      fi
      fail "activation copy failed; runtime files and service health restored"
    fi
  done

  if restart_service; then
    sleep 2
    code=$(health_code || true)
  else
    code=restart-failed
  fi
  if [[ "$code" == "200" ]] && service_active; then
    trap - HUP INT TERM
    printf 'activated; backup retained at %s\n' "$backup"
    return 0
  fi

  restore_files
  if restart_service; then
    sleep 2
    rollback_code=$(health_code || true)
  else
    rollback_code=restart-failed
  fi
  if [[ "$rollback_code" == "200" ]] && service_active; then
    printf 'ERROR: activation health failed; rollback restored files and service health\n' >&2
  else
    printf 'ERROR: activation health failed; rollback service health also failed\n' >&2
  fi
  trap - HUP INT TERM
  return 1
}

cleanup() {
  local stage=$1
  [[ "$stage" == "$HOME/.mobile-terminal-deploy."* ]] || fail "invalid staging directory"
  rm -rf "$stage"
}

case "$ACTION" in
  preflight) preflight "$@" ;;
  prepare) prepare "$@" ;;
  smoke) smoke "$@" ;;
  activate) activate "$@" ;;
  cleanup) cleanup "$@" ;;
  *) fail "unknown remote deployment action" ;;
esac
