#!/usr/bin/env bash
# P3 の IMIX だけを流し直す。1 回目は pktgen の clone_skb と imix_weights が両立せず、IMIX の探索がすべて失敗した。
# run-remaining.sh（引数の PID）が終わるのを待ってから、run-p3-sizes.sh を同じ出力先で流す（512B は結果があるので飛ばす）。
#
#   nohup bash scripts/run-imix-after.sh <待つ PID> > ログ 2>&1 &
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "${here}/.." || exit 1
wait_pid="${1:?}"
echo "== $(date '+%H:%M') PID ${wait_pid} の終わりを待つ"
while kill -0 "${wait_pid}" 2>/dev/null; do sleep 30; done
# 1 回目の IMIX は result.json が無い（探索が始まってすぐ落ちた）ので、残った途中の記録だけ消す
for d in results/P3-sizes/20261028/round*/imix-*; do [[ -f ${d}/result.json ]] || rm -rf "${d}"; done
echo "== $(date '+%H:%M') P3（IMIX）開始"
bash scripts/run-p3-sizes.sh results/P3-sizes/20261028 3
rc=$?   # echo の中の $(date) が $? を上書きするので、先に取っておく
echo "== $(date '+%H:%M') P3（IMIX）終了（終了コード ${rc}）"
exit "${rc}"
