#!/usr/bin/env bash
# IMIX の流し直し（run-imix-after.sh、引数の PID）が終わってから、遅延（L1・L2）と接続数（C1・C2）の試験を続けて流す。
#
#   nohup bash scripts/run-after-imix.sh <待つ PID> > ログ 2>&1 &
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "${here}/.." || exit 1
wait_pid="${1:?}"
day=$(date +%Y%m%d)
echo "== $(date '+%H:%M') PID ${wait_pid} の終わりを待つ"
while kill -0 "${wait_pid}" 2>/dev/null; do sleep 30; done
for s in "L run-latency.sh results/L-latency/${day}" "C run-conn.sh results/C-conn/${day}"; do
  read -r name script dir <<<"${s}"
  echo "== $(date '+%H:%M') ${name} 開始"
  bash "scripts/${script}" "${dir}" 3
  echo "== $(date '+%H:%M') ${name} 終了（終了コード $?）"
done
echo "== $(date '+%H:%M') すべて終了"
