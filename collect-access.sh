#!/usr/bin/env bash
# collect-access.sh — report fleet entry URLs without inspecting authentication data.
#
# This script intentionally never reads, prints, or transports token values or
# authentication files. Use a target user's interactive setup flow to enroll a
# device; do not collect fleet secrets into one terminal transcript.
set -uo pipefail

# label | ssh target ("" = local) | backend loopback port
ENTRIES=(
  "ph||8085"
  "ps|powerspec|8085"
  "lat/ben|ubuntu@100.88.210.92|8086"
  "lat/bradley|ubuntu@100.88.210.92|8085"
)

# Remote/local snippet: emit only the public URL.
probe_snippet='
  port="$1"
  url=""
  if command -v tailscale >/dev/null 2>&1; then
    url=$(tailscale funnel status 2>/dev/null | awk -v p="127.0.0.1:${port}" "
      /^https:\/\// {u=\$1}
      \$0 ~ p {print u; exit}
    ")
  fi
  printf "%s\n" "${url:-UNKNOWN-URL}"
'

printf '\n%-12s %s\n' "ENTRY" "URL"
printf '%s\n' "------------------------------------------------------------"

for row in "${ENTRIES[@]}"; do
  IFS='|' read -r key target port <<<"$row"
  if [[ -z "$target" ]]; then
    out=$(bash -c "$probe_snippet" _ "$port" 2>/dev/null)
  else
    out=$(timeout 25 ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o BatchMode=yes \
            "$target" "bash -s -- '$port'" <<<"$probe_snippet" 2>/dev/null)
  fi

  if [[ -z "$out" ]]; then
    printf '%-12s %s\n' "$key" "(unreachable)"
    continue
  fi
  printf '%-12s %s\n' "$key" "$out"
done
printf '\n'
