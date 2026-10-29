#!/usr/bin/env bash
# E1・E2 の性能試験。締め切りの時刻を過ぎたら、新しい試行を始めない。PC02 は ①（tuning-1-offload ＋ test-sink）。
#   E1 RFC 2544 の残りのフレーム長（128・256・1024・1280・1518B）の転送レートの上限。pktgen で双方向、多数フロー、
#      NDR（ロス 0）と PDR（ロス率 0.1%）を 3 巡。1518B も、ほかの長さと同じ道具で比べるため pktgen で測る（T2 は iperf3）。
#      巡回を外側に回すので、途中で止めても 1 巡目は全部の長さがそろう
#   E2 ロス率の曲線を 3 巡（scripts/run-extra.sh の E2 は 1 巡だけだった）。点は 1 巡目と同じ:
#      512B は理論値の 10〜100%、64B は 1.0〜5.5 Mpps と 20 Mpps（送れるだけの速さ）、を 30 秒ずつ
#
#   理論値（10e9 / ((フレーム長 + 20) × 8)、片向き と 両向きの合計、pps）:
#   128B      8,445,946    16,891,892
#   256B      4,528,986     9,057,971
#   1024B     1,197,318     2,394,636
#   1280B       961,538     1,923,077
#   1518B       812,744     1,625,488
#
#   かかる時間の目安（P3 の 512B と E-extra の E2 の実績から。探索の短い試行は 1 回約 45 秒、最終の 60 秒は約 95 秒）:
#     E1 1 巡: 128B・256B は送信側の頭打ちと探索で 1 本 10〜18 分 × 4 本、1024B 以上は理論値で通る見込みで
#              1 本約 2.5 分 × 6 本 → 1 巡 40〜65 分、3 巡で 2〜3.5 時間
#     E2 1 巡: 18 点 × 約 55 秒 → 約 17 分、3 巡で約 50 分
#
#   ONLY=E1|E2 ROUNDS=3 scripts/run-e1-e2.sh [日付の名前（既定: 今日）] [締め切り "YYYY-MM-DD HH:MM"] [待つ PID]
#   出力: results/E1-sizes/<日付>/round<r>/<長さ>-many-bidir-tol<0|0.001>/ と results/E2-curve/<日付>/round<r>/<点>/
#   結果（result.json）がある点は飛ばすので、止まったら同じ日付の名前で流し直せば続きから始まる。
#   集計: python3 -I scripts/summarize_e1_e2.py results/E1-sizes/<日付> results/E2-curve/<日付>
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "${here}/.." || exit 1
tag="${1:-$(date +%Y%m%d)}" deadline="${2:-}" wait_pid="${3:-}"
only="${ONLY:-E1 E2}" rounds="${ROUNDS:-3}"
e1="results/E1-sizes/${tag}" e2="results/E2-curve/${tag}"
v=vyos

e1_sizes=(128 256 1024 1280 1518)
e1_tols=(0 0.001)
e2_pcts=(10 20 30 40 50 60 70 80 90 100)
e2_64b_rates=(1000000 2000000 3000000 4000000 4500000 5000000 5500000 20000000)
wire512=4699248   # 512B の理論値（両向きの合計）

if [[ -n ${wait_pid} ]]; then
  echo "== $(date '+%H:%M') PID ${wait_pid} の終わりを待つ"
  while kill -0 "${wait_pid}" 2>/dev/null; do sleep 30; done
fi
if [[ -n ${deadline} ]]; then end=$(date -j -f "%Y-%m-%d %H:%M" "${deadline}" +%s); else end=$((1 << 62)); fi
want() { [[ " ${only} " == *" $1 "* ]]; }
in_time() { (($(date +%s) < end)) || { echo "== $(date '+%H:%M') 締め切り（${deadline}）を過ぎたので $1 を飛ばす"; return 1; }; }
cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/(encrypted-password|key) '[^']*'/\1 '<伏せ字>'/" | sort > "$1"
}
to_base() {
  bash scripts/pc02-commit.sh --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
apply() { to_base; bash scripts/pc02-commit.sh "$@" >/dev/null; sleep 5; }
t3() { python3 -I scripts/t3_search.py "$@"; }
# 結果がある点は飛ばす。途中で落ちた点は、前回の試行の記録を消してから流し直す（番号が重なって混ざらないように）
fresh() {
  [[ -f $1/result.json ]] && { echo "   $2: 済み（飛ばす）"; return 1; }
  mkdir -p "$1" && rm -f "$1"/trial*.json
}

# ログの置き場所を先に作る（2026-10-29 実害: run-extra.sh で作っておらず、リダイレクトで失敗して 1 本も流れなかった）
want E1 && mkdir -p "${e1}"
want E2 && mkdir -p "${e2}"
last="${e1}"; want E1 || last="${e2}"
trap 'echo "== 基準に戻す"; to_base; cfg "${last}/pc02-config-end.cmds"' EXIT

for h in pc01 pc03; do scp -q scripts/pktgen-run.sh "${h}:~/pktgen-run.sh"; done
apply "${v}/tuning-1-offload.cmds" "${v}/test-sink.cmds"

# ---- E1 RFC 2544 の残りのフレーム長 ----
if want E1; then
  echo "== $(date '+%H:%M') E1 開始（${e1}）"
  cfg "${e1}/pc02-config.cmds"
  for r in $(seq 1 "${rounds}"); do
    for size in "${e1_sizes[@]}"; do
      for tol in "${e1_tols[@]}"; do
        name="round${r} ${size}B tol=${tol}"
        d="${e1}/round${r}/${size}-many-bidir-tol${tol}"
        fresh "${d}" "E1 ${name}" || continue
        in_time "E1 ${name}" || break 3
        t3 --frame "${size}" --flows many --bidir --tolerance "${tol}" --out "${d}" > "${d}/search.log" 2>&1
        echo "   E1 ${name}: $(grep '^結果' "${d}/search.log")"
      done
    done
  done
  echo "== $(date '+%H:%M') E1 終了"
fi

# ---- E2 ロス率の曲線（3 巡） ----
if want E2; then
  last="${e2}"
  echo "== $(date '+%H:%M') E2 開始（${e2}）"
  cfg "${e2}/pc02-config.cmds"
  for r in $(seq 1 "${rounds}"); do
    for pct in "${e2_pcts[@]}"; do
      d="${e2}/round${r}/512B-${pct}pct"
      fresh "${d}" "E2 round${r} 512B ${pct}%" || continue
      in_time "E2 round${r} 512B ${pct}%" || break 2
      t3 --frame 512 --flows many --bidir --fixed-pps "$((wire512 * pct / 100))" --final-seconds 30 \
        --out "${d}" > "${d}/run.log" 2>&1
      echo "   E2 round${r} 512B ${pct}%: $(grep '^結果' "${d}/run.log")"
    done
    for rate in "${e2_64b_rates[@]}"; do
      d="${e2}/round${r}/64B-${rate}"
      fresh "${d}" "E2 round${r} 64B ${rate}" || continue
      in_time "E2 round${r} 64B ${rate}" || break 2
      t3 --frame 64 --flows many --bidir --fixed-pps "${rate}" --final-seconds 30 --out "${d}" > "${d}/run.log" 2>&1
      echo "   E2 round${r} 64B ${rate}: $(grep '^結果' "${d}/run.log")"
    done
  done
  echo "== $(date '+%H:%M') E2 終了"
fi
echo "== $(date '+%H:%M') すべて終了（集計: python3 -I scripts/summarize_e1_e2.py ${e1} ${e2}）"
