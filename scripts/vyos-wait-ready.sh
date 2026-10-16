#!/usr/bin/env bash
# VyOS コンテナの起動（設定の読み込みまで）が終わるのを待つ。
#
#   scripts/vyos-wait-ready.sh <コンテナ名> [待つ秒数（既定 300）]
#
# systemd の起動完了と、vyos-router（起動時の設定読み込み）の完了を待つ。
set -euo pipefail

[[ $# -ge 1 ]] || { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 1; }
c="$1" timeout="${2:-300}"

deadline=$((SECONDS + timeout))
while (( SECONDS < deadline )); do
  state="$(docker exec "${c}" systemctl is-system-running 2>/dev/null || true)"
  router="$(docker exec "${c}" systemctl is-active vyos-router.service 2>/dev/null || true)"
  if [[ "${state}" =~ ^(running|degraded)$ && "${router}" == active ]] \
     && docker exec "${c}" /bin/cli-shell-api existsActive system >/dev/null 2>&1; then
    echo "vyos-wait-ready: ${c} の起動が終わった（system: ${state}）"
    if [[ "${state}" == degraded ]]; then
      docker exec "${c}" systemctl --failed --no-legend || true
    fi
    exit 0
  fi
  if [[ "$(docker inspect -f '{{.State.Running}}' "${c}" 2>/dev/null)" != true ]]; then
    echo "vyos-wait-ready: ${c} が止まった" >&2
    docker logs --tail 50 "${c}" >&2 || true
    exit 1
  fi
  sleep 3
done
echo "vyos-wait-ready: ${timeout} 秒待っても ${c} の起動が終わらない（system: ${state:-?}, vyos-router: ${router:-?}）" >&2
docker exec "${c}" systemctl --failed --no-legend >&2 || true
exit 1
