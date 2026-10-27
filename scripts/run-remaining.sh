#!/usr/bin/env bash
# 残りの性能試験を続けて流す（2026-10-28 のユーザーの指示: 終わり次第次を投入してよい）。
#   いま走っている試験（引数の PID）が終わるのを待ってから、P1 → P2 → P3 → P4 の順に流す。
#   1 本が失敗しても次へ進む（失敗はログに残す）。P4 のレートは、構成比較の「①＋FW＋flowtable」の PDR の
#   中央値の 9 割にする（取れなければ 4.0 Mpps）。
#
#   nohup bash scripts/run-remaining.sh <待つ PID> > ログ 2>&1 &
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "${here}/.." || exit 1
wait_pid="${1:-}"
day=$(date +%Y%m%d)

if [[ -n ${wait_pid} ]]; then
  echo "== $(date '+%H:%M') PID ${wait_pid} の終わりを待つ"
  while kill -0 "${wait_pid}" 2>/dev/null; do sleep 30; done
fi

step() {
  local name=$1; shift
  echo "== $(date '+%H:%M') ${name} 開始"
  if "$@"; then echo "== $(date '+%H:%M') ${name} 終了"; else echo "== $(date '+%H:%M') ${name} 失敗（終了コード $?）。次へ進む"; fi
}

step P1 bash scripts/run-p1-ipv6.sh "results/P1-ipv6/${day}" 3
step P2 bash scripts/run-p2-mape-pps.sh "results/P2-mape-pps/${day}" 3
step P3 bash scripts/run-p3-sizes.sh "results/P3-sizes/${day}" 3

rate=$(python3 -I - <<'EOF'
import json, statistics as st, glob
vals = [json.load(open(p))["result_pps"] for p in glob.glob("results/T3-bidir-configs/*/s1fwft/round*/64B-many-bidir-tol0.001/result.json")]
print(int(st.median(vals) * 0.9) if vals else 4000000)
EOF
)
echo "== P4 のレート: ${rate} pps"
step P4 bash scripts/run-p4-soak.sh "results/P4-soak/${day}" "${rate}" 60
echo "== $(date '+%H:%M') すべて終了"
