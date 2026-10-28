#!/usr/bin/env bash
# 追加の試験（run-extra.sh、引数の PID）が終わってから、LAN → WAN の向きの遅延の試験を流す。締め切りを過ぎていたら流さない。
#
#   nohup bash scripts/run-latency-lan-after.sh <待つ PID> ["YYYY-MM-DD HH:MM"] > ログ 2>&1 &
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "${here}/.." || exit 1
wait_pid="${1:?}" deadline="${2:-2026-10-29 07:15}"
end=$(date -j -f "%Y-%m-%d %H:%M" "${deadline}" +%s)
echo "== $(date '+%H:%M') PID ${wait_pid} の終わりを待つ"
while kill -0 "${wait_pid}" 2>/dev/null; do sleep 30; done
if (($(date +%s) >= end)); then echo "== $(date '+%H:%M') 締め切り（${deadline}）を過ぎたので流さない"; exit 0; fi
echo "== $(date '+%H:%M') L（LAN → WAN）開始"
# 1 巡が約 8 分。締め切りまでに収まる巡回数にする（最大 3）
rounds=$(( (end - $(date +%s)) / 480 )); ((rounds > 3)) && rounds=3; ((rounds < 1)) && rounds=1
bash scripts/run-latency-lan.sh "results/L-latency-lan/$(date +%Y%m%d)" "${rounds}"
echo "== $(date '+%H:%M') L（LAN → WAN）終了（${rounds} 巡、終了コード $?）"
