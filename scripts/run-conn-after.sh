#!/usr/bin/env bash
# 前の試験（引数の PID）が終わってから、接続数の試験（N1・N2、run-conn.sh）を流す。締め切りを過ぎていたら流さない。
# 1 回目は pktgen の clone_skb 1000 で新しい流れにならず使えなかったので、直した pktgen-run.sh で流し直す（2026-10-29）。
#
#   nohup bash scripts/run-conn-after.sh <待つ PID> ["YYYY-MM-DD HH:MM"（省くと締め切りなし）] > ログ 2>&1 &
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "${here}/.." || exit 1
wait_pid="${1:?}" deadline="${2:-}"
# 締め切りを省いたときは締め切りなし（以前の既定は 2026-10-29 07:15 の固定で、その日を過ぎると何も流さずに 0 で終わった）
if [[ -n ${deadline} ]]; then end=$(date -j -f "%Y-%m-%d %H:%M" "${deadline}" +%s); else end=$((1 << 62)); fi
echo "== $(date '+%H:%M') PID ${wait_pid} の終わりを待つ"
while kill -0 "${wait_pid}" 2>/dev/null; do sleep 30; done
if (($(date +%s) >= end)); then echo "== $(date '+%H:%M') 締め切り（${deadline}）を過ぎたので流さない"; exit 0; fi
# 1 巡が約 7 分。締め切りまでに収まる巡回数にする（最大 3）
rounds=$(( (end - $(date +%s)) / 420 )); ((rounds > 3)) && rounds=3; ((rounds < 1)) && rounds=1
echo "== $(date '+%H:%M') N（接続数）開始（${rounds} 巡）"
bash scripts/run-conn.sh "results/C-conn/$(date +%Y%m%d)" "${rounds}"
rc=$?   # echo の中の $(date) が $? を上書きするので、先に取っておく
echo "== $(date '+%H:%M') N（接続数）終了（終了コード ${rc}）"
exit "${rc}"
