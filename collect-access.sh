#!/usr/bin/env bash
# collect-access.sh — report fleet entry URLs and (only on explicit request)
# the bootstrap access tokens needed to enroll a new device.
#
# Default mode never reads or prints token values; it reports the public URL
# and whether a token is configured.
#
# --with-secrets prints the actual MOBILE_TERMINAL_TOKEN for each reachable
# target so a new device can be enrolled. Run that mode ONLY in a private
# terminal you control. Never run it through an AI session's `!` prompt or
# any tool that transcribes output — tokens must not enter chat transcripts,
# logs, or clipboards you do not control.
set -uo pipefail

WITH_SECRETS=0
if [[ ${1:-} == "--with-secrets" ]]; then
  WITH_SECRETS=1
elif [[ -n ${1:-} ]]; then
  echo "Usage: $0 [--with-secrets]" >&2
  exit 2
fi

# label | ssh target ("" = local) | env file path | loopback port for funnel probe
# ps: the proxy's external auth realm reads MOBILE_TERMINAL_TOKEN from the
# powerhouse env (docs/ps-proxy.example.json authRealms.mine.tokenEnv).
# lat/bradley: no direct ssh key exists; see the printed root instruction.
ENTRIES=(
  "ph||/home/powerhouse/mobile-terminal/mobile-terminal.env|8085"
  "ps|powerspec|/home/powerhouse/mobile-terminal/mobile-terminal.env|8085"
  "lat/ben|ben@100.88.210.92|/home/ben/mobile-terminal/mobile-terminal.env|8086"
  "lat/bradley|ben@100.88.210.92||8085"
)

probe_snippet='
  port="$1"; env_file="$2"; with_secrets="$3"
  url=""
  if command -v tailscale >/dev/null 2>&1; then
    url=$(tailscale funnel status 2>/dev/null | awk -v p="127.0.0.1:${port}" "
      /^https:\/\// {u=\$1}
      \$0 ~ p {print u; exit}
    ")
  fi
  token_state="no-env-path"
  token_value=""
  if [[ -n "$env_file" && -r "$env_file" ]]; then
    line=$(grep -E "^MOBILE_TERMINAL_TOKEN=" "$env_file" | tail -1)
    if [[ -n "$line" ]]; then
      token_state="configured"
      token_value="${line#MOBILE_TERMINAL_TOKEN=}"
      token_value="${token_value%\"}"; token_value="${token_value#\"}"
    else
      token_state="absent"
    fi
  elif [[ -n "$env_file" ]]; then
    token_state="unreadable"
  fi
  printf "%s\n" "${url:-UNKNOWN-URL}"
  if [[ "$with_secrets" == 1 && "$token_state" == configured ]]; then
    printf "token %s\n" "$token_value"
  else
    printf "token-%s\n" "$token_state"
  fi
'

printf '\n%-12s %-45s %s\n' "ENTRY" "URL" "ACCESS"
printf '%s\n' "--------------------------------------------------------------------------------"

for row in "${ENTRIES[@]}"; do
  IFS='|' read -r key target env_file port <<<"$row"
  if [[ -z "$target" ]]; then
    out=$(bash -c "$probe_snippet" _ "$port" "$env_file" "$WITH_SECRETS" 2>/dev/null)
  else
    out=$(timeout 25 ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o BatchMode=yes \
            "$target" "bash -s -- '$port' '$env_file' '$WITH_SECRETS'" <<<"$probe_snippet" 2>/dev/null)
  fi

  if [[ -z "$out" ]]; then
    printf '%-12s %s\n' "$key" "(unreachable)"
    continue
  fi
  url=$(printf '%s\n' "$out" | sed -n 1p)
  access=$(printf '%s\n' "$out" | sed -n 2p)
  printf '%-12s %-45s %s\n' "$key" "$url" "$access"
done

printf '\n'
printf 'lat/bradley token (no direct ssh): on lat as root run\n'
printf "  grep '^MOBILE_TERMINAL_TOKEN=' /home/bperritt/mobile-terminal/mobile-terminal.env\n"
if [[ "$WITH_SECRETS" == 0 ]]; then
  printf 'Token values were not read. Re-run with --with-secrets in a PRIVATE terminal to print them.\n'
else
  printf 'Tokens printed above are live credentials; clear your scrollback after use.\n'
fi
