#!/usr/bin/env bash
set -euo pipefail

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

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Options:
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
  --help                     Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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

write_env_file() {
  if [[ -f "$ENV_FILE" ]]; then
    log "Keeping existing $ENV_FILE"
    return 0
  fi

  log "Creating $ENV_FILE"
  {
    echo "# Generated by install.sh"
    echo "MOBILE_TERMINAL_PORT=$PORT"
    echo "MOBILE_TERMINAL_SESSION=$SESSION"
    echo "MOBILE_TERMINAL_CWD=$CWD_VALUE"
    echo "MOBILE_TERMINAL_SHELL=$SHELL_VALUE"
    if [[ -n "$HOST_VALUE" ]]; then
      echo "MOBILE_TERMINAL_HOST=$HOST_VALUE"
    fi
    if [[ -n "$TAILSCALE_VALUE" ]]; then
      echo "MOBILE_TERMINAL_TAILSCALE=$TAILSCALE_VALUE"
    fi
    if [[ -n "$NO_TOKEN_VALUE" ]]; then
      echo "MOBILE_TERMINAL_NO_TOKEN=$NO_TOKEN_VALUE"
    fi
    if [[ -n "$ALLOW_CLIENTS_VALUE" ]]; then
      echo "MOBILE_TERMINAL_ALLOW_CLIENTS=$ALLOW_CLIENTS_VALUE"
    fi
  } >"$ENV_FILE"
}

sed_escape() {
  printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'
}

install_systemd_service() {
  local user_dir service_path python_path workdir env_path
  user_dir="${HOME}/.config/systemd/user"
  service_path="${user_dir}/mobile-terminal.service"
  python_path="$(command -v python3)"
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
  python_path="$(command -v python3)"
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
  ensure_runtime_dependencies
  ensure_node_modules
  write_env_file
  install_service
  log "Install complete"
  log "Configuration: $ENV_FILE"
  if [[ "$SERVICE_MODE" != "none" ]]; then
    log "Service should now be running."
  fi
}

main "$@"
