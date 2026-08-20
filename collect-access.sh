#!/usr/bin/env bash
# collect-access.sh — print the public URL + bootstrap access token for every
# mobile-terminal ENTRY in the fleet, all at once. Run this on `powerhouse`.
#
#   ./collect-access.sh
#
# The fleet is NOT one-backend-per-host. lat runs a separate per-user backend
# (own OS user, own tmux, own token, own funnel port) for ben and bperritt, so
# it contributes TWO entries. Each entry below names the loopback port its
# funnel URL proxies to, the env file holding that backend's token, and whether
# reading that env needs sudo (cross-user on lat).
#
# For each entry the script reads the LIVE funnel URL (the funnel line that
# proxies to 127.0.0.1:<port> on that host) and MOBILE_TERMINAL_TOKEN from that
# backend's env file. Remote hosts are reached over SSH; cross-user env files on
# lat are read with passwordless sudo.
#
# The token is the one-time bootstrap credential: enter it once per new device,
# then the device reconnects silently (device key / passkey). Treat the output
# as secret — anyone with a token + the matching URL can enroll a device.
set -uo pipefail

# label | ssh target ("" = local) | backend loopback port | env file path | sudo? (yes/no)
ENTRIES=(
  "ph||8085|/home/powerhouse/mobile-terminal/mobile-terminal.env|no"
  "ps|powerspec|8085|/home/powerhouse/mobile-terminal/mobile-terminal.env|no"
  "lat/ben|ubuntu@100.88.210.92|8086|/home/ben/mobile-terminal/mobile-terminal.env|yes"
  "lat/bradley|ubuntu@100.88.210.92|8085|/home/bperritt/mobile-terminal/mobile-terminal.env|yes"
)

# Remote/local snippet: emit "URL<TAB>TOKEN" for one entry.
# $1 = backend port, $2 = env file path, $3 = sudo flag (yes/no).
probe_snippet='
  port="$1"; f="$2"; usesudo="$3"
  read_env() { if [ "$usesudo" = "yes" ]; then sudo -n cat "$f" 2>/dev/null; else cat "$f" 2>/dev/null; fi; }
  tok=$(read_env | grep -m1 "^MOBILE_TERMINAL_TOKEN=" | cut -d= -f2- | sed "s/^[\"'"'"']//;s/[\"'"'"']$//")
  url=""
  if command -v tailscale >/dev/null 2>&1; then
    # Track the last-seen https URL; when the following proxy line names our
    # backend port, that URL is the public entry for this backend.
    url=$(tailscale funnel status 2>/dev/null | awk -v p="127.0.0.1:${port}" "
      /^https:\/\// {u=\$1}
      \$0 ~ p {print u; exit}
    ")
  fi
  printf "%s\t%s\n" "${url:-UNKNOWN-URL}" "${tok:-NO-TOKEN}"
'

printf '\n%-12s %-45s %s\n' "ENTRY" "URL" "TOKEN"
printf '%s\n' "----------------------------------------------------------------------------------------------------"

for row in "${ENTRIES[@]}"; do
  IFS='|' read -r key target port env sudoflag <<<"$row"
  if [ -z "$target" ]; then
    out=$(bash -c "$probe_snippet" _ "$port" "$env" "$sudoflag" 2>/dev/null)
  else
    out=$(timeout 25 ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
            "$target" "bash -s -- '$port' '$env' '$sudoflag'" <<<"$probe_snippet" 2>/dev/null)
  fi

  if [ -z "$out" ]; then
    printf '%-12s %-45s %s\n' "$key" "(unreachable)" "UNREACHABLE"
    continue
  fi
  url=${out%%$'\t'*}
  tok=${out#*$'\t'}
  printf '%-12s %-45s %s\n' "$key" "$url" "$tok"
done
printf '\n'
