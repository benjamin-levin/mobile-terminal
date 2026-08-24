#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT_DIR/mobile-terminal.env"
SYSTEMD_TEMPLATE="$ROOT_DIR/systemd/mobile-terminal.service"
LAUNCHD_TEMPLATE="$ROOT_DIR/launchd/com.mobile-terminal.server.plist"

PORT="8085"
SESSION="mobile-terminal"
CWD_VALUE="${HOME}"
SHELL_VALUE="${SHELL:-/bin/bash}"
HOST_VALUE=""
TAILSCALE_VALUE=""
NO_TOKEN_VALUE=""
ALLOW_CLIENTS_VALUE=""
SERVICE_MODE="auto"
PROVIDER_HOOKS_MODE="auto"
AUTH_MIGRATION="passkey-bootstrap-v1"
MIGRATE_TOKEN_AUTH=0
APPLY=0

usage() {
  cat <<'EOF'
Usage: ./install.sh --apply [options]

Installation is non-mutating unless --apply is provided.

Options:
  --apply                    Confirm dependency, config, hook, and service changes
  --port <port>              Default port for generated mobile-terminal.env
  --session <name>           Default tmux session name
  --cwd <path>               Default working directory for new tmux sessions
  --shell <path>             Login shell to use inside tmux
  --host <host>              Optional MOBILE_TERMINAL_HOST value
  --tailscale                Set MOBILE_TERMINAL_TAILSCALE=true in the env file
  --no-token                 Set MOBILE_TERMINAL_NO_TOKEN=true in the env file
  --allow-clients <list>     Comma-separated MOBILE_TERMINAL_ALLOW_CLIENTS value
  --service auto|systemd|launchd|none
                             Choose how to install the long-running service
  --provider-hooks auto|off|required
                             Install provider lifecycle hooks (default: auto)
  --migrate-token-auth       Rotate an existing token-mode env to a generated
                             bootstrap token and record readiness (value hidden)
  --help                     Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --session)
      SESSION="$2"
      shift 2
      ;;
    --cwd)
      CWD_VALUE="$2"
      shift 2
      ;;
    --shell)
      SHELL_VALUE="$2"
      shift 2
      ;;
    --host)
      HOST_VALUE="$2"
      shift 2
      ;;
    --tailscale)
      TAILSCALE_VALUE="true"
      shift
      ;;
    --no-token)
      NO_TOKEN_VALUE="true"
      shift
      ;;
    --allow-clients)
      ALLOW_CLIENTS_VALUE="$2"
      shift 2
      ;;
    --service)
      SERVICE_MODE="$2"
      shift 2
      ;;
    --provider-hooks)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --provider-hooks" >&2
        usage >&2
        exit 1
      fi
      PROVIDER_HOOKS_MODE="$2"
      shift 2
      ;;
    --migrate-token-auth)
      MIGRATE_TOKEN_AUTH=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

case "$PROVIDER_HOOKS_MODE" in
  auto|off|required)
    ;;
  *)
    echo "Invalid --provider-hooks mode: $PROVIDER_HOOKS_MODE" >&2
    usage >&2
    exit 1
    ;;
esac

if [[ "$APPLY" != 1 ]]; then
  echo "Refusing installation without --apply; no changes made" >&2
  exit 2
fi

log() {
  printf '[install] %s\n' "$*"
}

need_sudo() {
  [[ "${EUID}" -ne 0 ]]
}

run_privileged() {
  if need_sudo; then
    sudo "$@"
  else
    "$@"
  fi
}

shell_quote() {
  printf '%q' "$1"
}

env_file_value() {
  local value=$1
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "Environment values cannot contain newlines" >&2
    return 1
  fi
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//\$/\\\$}
  value=${value//\`/\\\`}
  printf '"%s"' "$value"
}

write_env_entry() {
  local key=$1 value=$2
  printf '%s=' "$key"
  env_file_value "$value"
  printf '\n'
}

existing_auth_migration_ready() {
  grep -Eiq '^[[:space:]]*MOBILE_TERMINAL_AUTH_MIGRATION[[:space:]]*=[[:space:]]*("passkey-bootstrap-v1"|'"'"'passkey-bootstrap-v1'"'"'|passkey-bootstrap-v1)[[:space:]]*$' "$ENV_FILE"
}

existing_no_token_mode() {
  grep -Eiq '^[[:space:]]*MOBILE_TERMINAL_NO_TOKEN[[:space:]]*=[[:space:]]*("(1|true|yes)"|'"'"'(1|true|yes)'"'"'|(1|true|yes))[[:space:]]*$' "$ENV_FILE"
}

preflight_existing_auth_migration() {
  if [[ ! -f "$ENV_FILE" ]] || existing_no_token_mode || existing_auth_migration_ready; then
    return 0
  fi
  if [[ "$MIGRATE_TOKEN_AUTH" == 1 ]]; then
    return 0
  fi
  echo "Existing token-mode env is not marked ready for bootstrap-only authentication." >&2
  echo "Re-run with --apply --migrate-token-auth to replace it without parsing or printing the old token." >&2
  exit 1
}

detect_linux_package_manager() {
  for candidate in apt-get dnf yum pacman zypper apk; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

ensure_homebrew() {
  if command -v brew >/dev/null 2>&1; then
    return 0
  fi
  log "Installing Homebrew"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
  command -v brew >/dev/null 2>&1
}

ensure_dependencies_linux() {
  local pm
  pm="$(detect_linux_package_manager)" || {
    echo "Unsupported Linux package manager. Install python3, tmux, node, and npm manually." >&2
    exit 1
  }

  case "$pm" in
    apt-get)
      log "Installing dependencies with apt-get"
      run_privileged apt-get update
      run_privileged apt-get install -y python3 tmux nodejs npm
      ;;
    dnf)
      log "Installing dependencies with dnf"
      run_privileged dnf install -y python3 tmux nodejs npm
      ;;
    yum)
      log "Installing dependencies with yum"
      run_privileged yum install -y python3 tmux nodejs npm
      ;;
    pacman)
      log "Installing dependencies with pacman"
      run_privileged pacman -Sy --noconfirm python tmux nodejs npm
      ;;
    zypper)
      log "Installing dependencies with zypper"
      run_privileged zypper --non-interactive install python3 tmux nodejs npm
      ;;
    apk)
      log "Installing dependencies with apk"
      run_privileged apk add python3 tmux nodejs npm bash
      ;;
  esac
}

ensure_dependencies_macos() {
  ensure_homebrew
  log "Installing dependencies with Homebrew"
  brew install python tmux node
}

ensure_node_command() {
  if command -v node >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v nodejs >/dev/null 2>&1; then
    echo "Missing required command after installation: node" >&2
    exit 1
  fi

  local user_bin
  user_bin="${HOME}/.local/bin"
  mkdir -p "$user_bin"
  ln -sf "$(command -v nodejs)" "${user_bin}/node"
  export PATH="${user_bin}:$PATH"
  log "Created ${user_bin}/node shim for nodejs"
}

ensure_runtime_dependencies() {
  local os_name
  local need_install="false"
  if ! command -v python3 >/dev/null 2>&1 || ! command -v tmux >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    need_install="true"
  fi
  if ! command -v node >/dev/null 2>&1 && ! command -v nodejs >/dev/null 2>&1; then
    need_install="true"
  fi

  os_name="$(uname -s)"
  if [[ "$need_install" == "true" ]]; then
    if [[ "$os_name" == "Darwin" ]]; then
      ensure_dependencies_macos
    else
      ensure_dependencies_linux
    fi
  else
    log "Runtime dependencies already installed"
  fi

  ensure_node_command

  for cmd in python3 tmux node npm; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      echo "Missing required command after installation: $cmd" >&2
      exit 1
    fi
  done
}

ensure_node_modules() {
  log "Installing JavaScript dependencies"
  (cd "$ROOT_DIR" && npm ci)
}

# Create a self-contained Python venv and install server.py's runtime deps
# (websockets, cryptography, pillow) from requirements.txt. This makes every
# machine's install self-contained — no per-machine manual pip installs.
ensure_python_env() {
  local venv="${ROOT_DIR}/.venv"
  local reqs="${ROOT_DIR}/requirements.txt"
  if [[ ! -x "${venv}/bin/python" ]]; then
    log "Creating Python venv at ${venv}"
    python3 -m venv "$venv"
  fi
  if [[ -f "$reqs" ]]; then
    log "Installing Python dependencies from requirements.txt"
    "${venv}/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    "${venv}/bin/pip" install --quiet -r "$reqs"
  else
    log "No requirements.txt found; skipping Python dependency install"
  fi
}

install_provider_hooks() {
  case "$PROVIDER_HOOKS_MODE" in
    off)
      log "Skipping provider lifecycle hooks (--provider-hooks off)"
      ;;
    required)
      log "Installing provider lifecycle hooks"
      "${ROOT_DIR}/.venv/bin/python" "${ROOT_DIR}/install_provider_hooks.py" --apply
      ;;
    auto)
      log "Installing provider lifecycle hooks"
      if "${ROOT_DIR}/.venv/bin/python" "${ROOT_DIR}/install_provider_hooks.py" --apply; then
        log "Provider lifecycle hooks installed"
      else
        log "Provider lifecycle hook installation failed; continuing (--provider-hooks auto)"
      fi
      ;;
  esac
}

write_env_file() {
  local action generated_token=""
  if [[ -f "$ENV_FILE" ]]; then
    chmod 600 "$ENV_FILE"
    if existing_no_token_mode; then
      if existing_auth_migration_ready; then
        log "Keeping existing $ENV_FILE (mode 0600)"
        return 0
      fi
      action="mark"
    elif [[ "$MIGRATE_TOKEN_AUTH" == 1 ]]; then
      action="rotate"
    else
      log "Keeping existing $ENV_FILE (mode 0600)"
      return 0
    fi

    action="$("${ROOT_DIR}/.venv/bin/python" - "$ENV_FILE" "$action" "$AUTH_MIGRATION" <<'PY'
import os
import secrets
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
action = sys.argv[2]
auth_migration = sys.argv[3]
auth_key = "MOBILE_TERMINAL_AUTH_MIGRATION"
token_key = "MOBILE_TERMINAL_TOKEN"


def encode(value):
    escaped = "".join(f"\\{character}" if character in '\\\"$`' else character for character in value)
    return f'"{escaped}"'


updated = []
with path.open(encoding="utf-8") as stream:
    for line in stream:
        stripped = line.rstrip("\n")
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key == auth_key or (action == "rotate" and key == token_key):
            continue
        updated.append(stripped)
if action == "rotate":
    updated.append(f"{token_key}={encode(secrets.token_urlsafe(32))}")
updated.append(f"{auth_key}={encode(auth_migration)}")

fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
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
print(action)
PY
)"
    if [[ "$action" == "rotate" ]]; then
      log "Rotated the bootstrap token and recorded auth readiness (value hidden)"
    else
      log "Recorded no-token auth readiness in the existing env file"
    fi
    return 0
  fi

  log "Creating $ENV_FILE"
  {
    echo "# Generated by install.sh"
    write_env_entry MOBILE_TERMINAL_PORT "$PORT"
    write_env_entry MOBILE_TERMINAL_SESSION "$SESSION"
    write_env_entry MOBILE_TERMINAL_CWD "$CWD_VALUE"
    write_env_entry MOBILE_TERMINAL_SHELL "$SHELL_VALUE"
    if [[ -n "$HOST_VALUE" ]]; then
      write_env_entry MOBILE_TERMINAL_HOST "$HOST_VALUE"
    fi
    if [[ -n "$TAILSCALE_VALUE" ]]; then
      write_env_entry MOBILE_TERMINAL_TAILSCALE "$TAILSCALE_VALUE"
    fi
    if [[ -n "$NO_TOKEN_VALUE" ]]; then
      write_env_entry MOBILE_TERMINAL_NO_TOKEN "$NO_TOKEN_VALUE"
    else
      generated_token="$("${ROOT_DIR}/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
      write_env_entry MOBILE_TERMINAL_TOKEN "$generated_token"
    fi
    if [[ -n "$ALLOW_CLIENTS_VALUE" ]]; then
      write_env_entry MOBILE_TERMINAL_ALLOW_CLIENTS "$ALLOW_CLIENTS_VALUE"
    fi
    write_env_entry MOBILE_TERMINAL_AUTH_MIGRATION "$AUTH_MIGRATION"
  } >"$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

sed_escape() {
  printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'
}

install_systemd_service() {
  local user_dir service_path python_path workdir env_path
  user_dir="${HOME}/.config/systemd/user"
  service_path="${user_dir}/mobile-terminal.service"
  python_path="${ROOT_DIR}/.venv/bin/python"
  [[ -x "$python_path" ]] || {
    echo "Required repository interpreter is missing: $python_path" >&2
    exit 1
  }
  workdir="$ROOT_DIR"
  env_path="$ENV_FILE"

  mkdir -p "$user_dir"
  sed \
    -e "s|@WORKDIR@|$(sed_escape "$workdir")|g" \
    -e "s|@ENV_FILE@|$(sed_escape "$env_path")|g" \
    -e "s|@PYTHON@|$(sed_escape "$python_path")|g" \
    "$SYSTEMD_TEMPLATE" >"$service_path"

  log "Installed systemd user service at $service_path"
  systemctl --user daemon-reload
  systemctl --user enable --now mobile-terminal.service
}

install_launchd_service() {
  local launch_agents_dir plist_path wrapper_path log_dir python_path uid_value
  local root_quoted env_quoted python_quoted server_quoted
  launch_agents_dir="${HOME}/Library/LaunchAgents"
  plist_path="${launch_agents_dir}/com.mobile-terminal.server.plist"
  wrapper_path="${ROOT_DIR}/mobile-terminal-launchd.sh"
  log_dir="${HOME}/Library/Logs/mobile-terminal"
  python_path="${ROOT_DIR}/.venv/bin/python"
  [[ -x "$python_path" ]] || {
    echo "Required repository interpreter is missing: $python_path" >&2
    exit 1
  }
  uid_value="$(id -u)"
  root_quoted="$(shell_quote "$ROOT_DIR")"
  env_quoted="$(shell_quote "$ENV_FILE")"
  python_quoted="$(shell_quote "$python_path")"
  server_quoted="$(shell_quote "$ROOT_DIR/server.py")"

  mkdir -p "$launch_agents_dir" "$log_dir"
  cat >"$wrapper_path" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd $root_quoted
if [[ -f $env_quoted ]]; then
  set -a
  source $env_quoted
  set +a
fi
exec $python_quoted $server_quoted
EOF
  chmod +x "$wrapper_path"

  sed \
    -e "s|@LABEL@|com.mobile-terminal.server|g" \
    -e "s|@WORKDIR@|$(sed_escape "$ROOT_DIR")|g" \
    -e "s|@WRAPPER@|$(sed_escape "$wrapper_path")|g" \
    -e "s|@STDOUT_LOG@|$(sed_escape "$log_dir/stdout.log")|g" \
    -e "s|@STDERR_LOG@|$(sed_escape "$log_dir/stderr.log")|g" \
    "$LAUNCHD_TEMPLATE" >"$plist_path"

  log "Installed launchd agent at $plist_path"
  launchctl bootout "gui/${uid_value}" "$plist_path" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/${uid_value}" "$plist_path"
  launchctl kickstart -k "gui/${uid_value}/com.mobile-terminal.server"
}

install_service() {
  local os_name
  os_name="$(uname -s)"

  case "$SERVICE_MODE" in
    none)
      log "Skipping service installation"
      return 0
      ;;
    systemd)
      install_systemd_service
      return 0
      ;;
    launchd)
      install_launchd_service
      return 0
      ;;
    auto)
      ;;
    *)
      echo "Invalid --service mode: $SERVICE_MODE" >&2
      exit 1
      ;;
  esac

  if [[ "$os_name" == "Darwin" ]]; then
    install_launchd_service
    return 0
  fi

  if command -v systemctl >/dev/null 2>&1; then
    install_systemd_service
    return 0
  fi

  log "No supported service manager detected. Run ./run.sh manually."
}

main() {
  preflight_existing_auth_migration
  ensure_runtime_dependencies
  ensure_node_modules
  ensure_python_env
  install_provider_hooks
  write_env_file
  install_service
  log "Install complete"
  log "Configuration: $ENV_FILE"
  if [[ "$SERVICE_MODE" != "none" ]]; then
    log "Service should now be running."
  fi
}

main "$@"
