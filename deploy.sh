#!/usr/bin/env bash
# Deploy the canonical Mobile Terminal runtime closure to exact fleet targets.
# This script never copies target-owned environments, state, credentials, or auth files.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"
MANIFEST="$ROOT/deployment-manifest.sh"
REMOTE_HELPER="$ROOT/scripts/deploy-remote.sh"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes)

usage() {
  cat <<'EOF'
Usage: ./deploy.sh --dry-run TARGET [TARGET ...]
       ./deploy.sh --apply [--confirm-ph-accepted] [--confirm-ps-accepted] TARGET

Exact targets:
  ps-powerhouse
  lat-ben
  lat-bperritt
  mbp-powerhouse

There are no implicit, fleet, ps, or lat aliases. Applying ps/lat targets requires
live ph/iPhone acceptance recorded explicitly with --confirm-ph-accepted. Applying
lat also requires explicit ps acceptance with --confirm-ps-accepted.
EOF
}

MODE=
CONFIRM_PH=0
CONFIRM_PS=0
WANT=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run|--apply)
      [[ -z "$MODE" ]] || { printf 'Choose exactly one of --dry-run or --apply\n' >&2; exit 2; }
      MODE=${1#--}
      ;;
    --confirm-ph-accepted) CONFIRM_PH=1 ;;
    --confirm-ps-accepted) CONFIRM_PS=1 ;;
    -h|--help) usage; exit 0 ;;
    --*) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    *) WANT+=("$1") ;;
  esac
  shift
done

[[ -n "$MODE" ]] || { printf 'Refusing to choose a deployment mode implicitly; use --dry-run or --apply\n' >&2; usage >&2; exit 2; }
[[ ${#WANT[@]} -gt 0 ]] || { printf 'Refusing implicit fleet deployment: name an exact target\n' >&2; usage >&2; exit 2; }
if [[ "$MODE" == apply && ${#WANT[@]} -ne 1 ]]; then
  printf 'Refusing multi-target apply: deploy one exact target, then obtain live acceptance before continuing\n' >&2
  exit 2
fi
[[ -f "$MANIFEST" ]] || { printf 'Deployment manifest is missing: %s\n' "$MANIFEST" >&2; exit 1; }
# shellcheck disable=SC1090
source "$MANIFEST"

selected() {
  local candidate=$1 wanted
  for wanted in "${WANT[@]}"; do
    [[ "$wanted" == "$candidate" ]] && return 0
  done
  return 1
}

target_exists() {
  local candidate=$1 entry name rest
  for entry in "${DEPLOY_TARGETS[@]}"; do
    IFS='|' read -r name rest <<<"$entry"
    [[ "$name" == "$candidate" ]] && return 0
  done
  return 1
}

for target in "${WANT[@]}"; do
  target_exists "$target" || { printf 'Unknown exact target: %s\n' "$target" >&2; usage >&2; exit 2; }
done
for ((i = 0; i < ${#WANT[@]}; i++)); do
  for ((j = i + 1; j < ${#WANT[@]}; j++)); do
    [[ "${WANT[$i]}" != "${WANT[$j]}" ]] || { printf 'Duplicate target: %s\n' "${WANT[$i]}" >&2; exit 2; }
  done
done

if [[ "$MODE" == apply ]]; then
  for entry in "${DEPLOY_TARGETS[@]}"; do
    IFS='|' read -r name gate _ <<<"$entry"
    if selected "$name" && [[ "$gate" == ps || "$gate" == lat ]] && [[ "$CONFIRM_PH" != 1 ]]; then
      printf 'Refusing %s deployment without --confirm-ph-accepted\n' "$name" >&2
      exit 1
    fi
    if selected "$name" && [[ "$gate" == lat ]] && [[ "$CONFIRM_PS" != 1 ]]; then
      printf 'Refusing %s deployment without --confirm-ps-accepted\n' "$name" >&2
      exit 1
    fi
  done
  if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
    printf 'Refusing deployment from a dirty tracked tree\n' >&2
    exit 1
  fi
fi

validate_manifest_path() {
  local path=$1
  [[ "$path" != /* && "$path" != *".."* && "$path" != *$'\n'* ]] || {
    printf 'Invalid deployment manifest path: %s\n' "$path" >&2
    return 1
  }
  [[ -f "$ROOT/$path" && ! -L "$ROOT/$path" ]] || {
    printf 'Missing or invalid deployment manifest file: %s\n' "$path" >&2
    return 1
  }
}

local_preflight() {
  local file imports python
  local env_args=()

  printf '%s\n' 'Running local deployment preflight'
  bash -n "$ROOT/deploy.sh" "$MANIFEST" "$REMOTE_HELPER"
  [[ -x "$ROOT/.venv/bin/python" ]] || { printf 'Repository interpreter is missing: %s\n' "$ROOT/.venv/bin/python" >&2; return 1; }
  python="$ROOT/.venv/bin/python"

  for file in "${DEPLOY_FILES[@]}"; do
    validate_manifest_path "$file"
    git -C "$ROOT" ls-files --error-unmatch -- "$file" >/dev/null || {
      printf 'Refusing untracked deployment input: %s\n' "$file" >&2
      return 1
    }
  done
  git -C "$ROOT" ls-files --error-unmatch -- deployment-manifest.sh scripts/deploy-remote.sh >/dev/null || {
    printf 'Deployment control files must be tracked\n' >&2
    return 1
  }
  for file in "${DEPLOY_PYTHON_FILES[@]}"; do
    validate_manifest_path "$file"
  done
  for file in "${DEPLOY_JAVASCRIPT_FILES[@]}"; do
    validate_manifest_path "$file"
  done

  while IFS= read -r name; do
    [[ -n "$name" ]] && env_args+=(-u "$name")
  done < <(compgen -A variable MOBILE_TERMINAL_ || true)
  env "${env_args[@]}" PYTHONDONTWRITEBYTECODE=1 "$python" -c '
import pathlib
import sys
for name in sys.argv[1:]:
    path = pathlib.Path(name)
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
' "${DEPLOY_PYTHON_FILES[@]}"
  imports=$(IFS=,; printf '%s' "${DEPLOY_DEPENDENCY_IMPORTS[*]}")
  env "${env_args[@]}" PYTHONDONTWRITEBYTECODE=1 "$python" -c 'import importlib, sys; [importlib.import_module(name) for name in sys.argv[1].split(",") if name]' "$imports"
  env "${env_args[@]}" PYTHONDONTWRITEBYTECODE=1 "$python" -c 'import server, provider_authority, provider_binding_hook, proxy, proxy_auth, webauthn_auth; from mobile_terminal_config import ConfigError, load_runtime_config; from proxy import ProxyServer'
  env "${env_args[@]}" "$python" -m pip install --dry-run --no-index --disable-pip-version-check -r "$ROOT/requirements.txt" >/dev/null

  if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    printf 'node and npm are required for JavaScript dependency preflight\n' >&2
    return 1
  fi
  npm ls --all --omit=dev >/dev/null 2>&1 || {
    printf 'Installed JavaScript dependency preflight failed\n' >&2
    return 1
  }
  for file in "${DEPLOY_GENERATED_FILES[@]}"; do
    validate_manifest_path "$file"
  done
  for file in "${DEPLOY_JAVASCRIPT_FILES[@]}" "${DEPLOY_GENERATED_FILES[@]}"; do
    case "$file" in
      *.js) node --check "$ROOT/$file" >/dev/null ;;
    esac
  done
  printf '%s\n' 'Local deployment preflight passed'
}

remote_helper() {
  local host=$1
  shift
  ssh "${SSH_OPTS[@]}" "$host" bash -s -- "$@" <"$REMOTE_HELPER"
}

remote_preflight() {
  local name=$1 ssh_target=$2 ssh_user=$3 runtime_user=$4 repo=$5 interpreter=$6 scope=$7 service=$8 port=$9
  local imports
  imports=$(IFS=,; printf '%s' "${DEPLOY_DEPENDENCY_IMPORTS[*]}")
  printf 'Running remote preflight: %s\n' "$name"
  remote_helper "$ssh_target" preflight "$ssh_user" "$runtime_user" "$repo" "$interpreter" "$scope" "$service" "$port" "$imports"
}

apply_target() {
  local name=$1 ssh_target=$2 ssh_user=$3 runtime_user=$4 repo=$5 interpreter=$6 scope=$7 service=$8 port=$9 archive=${10}
  local stage

  printf 'Staging exact target: %s\n' "$name"
  stage=$(remote_helper "$ssh_target" prepare --apply)
  if [[ "$stage" != */.mobile-terminal-deploy.* ]]; then
    printf 'Remote returned an invalid staging directory for %s\n' "$name" >&2
    return 1
  fi
  if ! scp -q "${SSH_OPTS[@]}" "$archive" "$ssh_target:$stage/runtime.tgz"; then
    remote_helper "$ssh_target" cleanup --apply "$stage" >/dev/null 2>&1 || true
    printf 'Staging copy failed for %s\n' "$name" >&2
    return 1
  fi
  if ! remote_helper "$ssh_target" smoke --apply "$stage" "$runtime_user" "$repo" "$interpreter" "$scope" "$service" "$port"; then
    remote_helper "$ssh_target" cleanup --apply "$stage" >/dev/null 2>&1 || true
    printf 'Staged smoke check failed for %s; active tree was not changed\n' "$name" >&2
    return 1
  fi
  if ! remote_helper "$ssh_target" activate --apply "$stage" "$runtime_user" "$repo" "$interpreter" "$scope" "$service" "$port"; then
    printf 'Transactional activation failed for %s\n' "$name" >&2
    return 1
  fi
  printf 'Deployment passed service health: %s\n' "$name"
}

local_preflight

# Every selected remote target must pass before any archive is copied to any host.
for entry in "${DEPLOY_TARGETS[@]}"; do
  IFS='|' read -r name gate ssh_target ssh_user runtime_user repo interpreter scope service port <<<"$entry"
  selected "$name" || continue
  remote_preflight "$name" "$ssh_target" "$ssh_user" "$runtime_user" "$repo" "$interpreter" "$scope" "$service" "$port" || {
    printf 'Remote preflight failed; no target was staged or activated\n' >&2
    exit 1
  }
done

if [[ "$MODE" == dry-run ]]; then
  printf 'Dry run passed all safe local and remote preflight checks. No files copied and no services restarted.\n'
  exit 0
fi

TMP=$(mktemp -d "${TMPDIR:-/var/tmp}/mobile-terminal-deploy.XXXXXX")
trap 'rm -rf -- "$TMP"' EXIT HUP INT TERM
ARCHIVE="$TMP/runtime.tgz"
tar -czf "$ARCHIVE" -C "$ROOT" -- deployment-manifest.sh "${DEPLOY_FILES[@]}"

# Manifest order is rollout order, regardless of argument order. Any failure stops progression.
for entry in "${DEPLOY_TARGETS[@]}"; do
  IFS='|' read -r name gate ssh_target ssh_user runtime_user repo interpreter scope service port <<<"$entry"
  selected "$name" || continue
  apply_target "$name" "$ssh_target" "$ssh_user" "$runtime_user" "$repo" "$interpreter" "$scope" "$service" "$port" "$ARCHIVE" || exit 1
done
