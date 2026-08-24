#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    printf 'Required test interpreter is missing: %s\n' "$PYTHON" >&2
    exit 1
fi

umask 077
TEST_ROOT=$(mktemp -d /var/tmp/mt.XXXXXX)
cleanup() {
    rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT HUP INT TERM
mkdir -p "$TEST_ROOT/home" "$TEST_ROOT/tmp" "$TEST_ROOT/tmux" "$TEST_ROOT/work"

CLEAN_ENV=(
    env -i
    "HOME=$TEST_ROOT/home"
    "PATH=$PATH"
    "TMPDIR=$TEST_ROOT/tmp"
    "TMUX_TMPDIR=$TEST_ROOT/tmux"
    "PYTHONDONTWRITEBYTECODE=1"
    "USER=${USER:-mobile-terminal-test}"
    "LOGNAME=${LOGNAME:-${USER:-mobile-terminal-test}}"
    "SHELL=${SHELL:-/bin/sh}"
    "TERM=${TERM:-xterm-256color}"
)
if [[ -n ${LANG:-} ]]; then
    CLEAN_ENV+=("LANG=$LANG")
fi
if [[ -n ${LC_ALL:-} ]]; then
    CLEAN_ENV+=("LC_ALL=$LC_ALL")
fi

run_syntax() {
    "${CLEAN_ENV[@]}" "$PYTHON" -m py_compile \
        server.py mobile_terminal_config.py proxy.py proxy_auth.py webauthn_auth.py \
        provider_authority.py provider_binding_hook.py install_provider_hooks.py \
        tests/__init__.py tests/tmux_harness.py tests/test_*.py
    for source in static/*.js; do
        "${CLEAN_ENV[@]}" node --check "$source"
    done
    for source in collect-access.sh deploy.sh install.sh ps-proxy-up.sh run.sh scripts/*.sh; do
        "${CLEAN_ENV[@]}" bash -n "$source"
    done
}

run_unittests() {
    (
        cd "$TEST_ROOT/work"
        "${CLEAN_ENV[@]}" "PYTHONPATH=$ROOT" "$PYTHON" -m unittest -v "$@"
    )
}

run_discovery() {
    (
        cd "$TEST_ROOT/work"
        "${CLEAN_ENV[@]}" "PYTHONPATH=$ROOT" "$PYTHON" -m unittest discover -s "$ROOT/tests" -t "$ROOT" -v
    )
}

if [[ ${1:-} == "--syntax" ]]; then
    shift
    if (( $# )); then
        printf '%s\n' '--syntax does not accept unittest arguments' >&2
        exit 2
    fi
    run_syntax
elif [[ ${1:-} == "--all" ]]; then
    shift
    run_syntax
    if (( $# )); then
        run_unittests "$@"
    else
        run_discovery
    fi
elif (( $# )); then
    run_unittests "$@"
else
    run_discovery
fi
