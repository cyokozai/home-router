#!/usr/bin/env bash
# 構成ごとの 64B 多数フローを、双方向の送信で測り直す。片方向（PC01 だけが送る）では多数フローが PC01 の送信上限
# （約 2.84 Mpps）に張り付き、構成の差が見えなかった（results/T4b・T5-B1・T8）。双方向なら ① の PC02 の上限
# （PDR 約 5.0 Mpps、results/T3-bidir）まで押せる。1 フローは片方向でも送信上限より十分下だったので測り直さない。
#   構成（すべて ① の上に足す。試験用の捨て先 test-sink.cmds も入れる）:
#     s1 ① / s1rps ＋② RPS / s1ft ＋③ flowtable / s1perf ＋④ 性能プロファイル / s1fw ＋ステートフル FW /
#     s1fwft ＋FW＋flowtable / s1dfw ＋firewall の set dscp / s1qos ＋QoS の shaper（NGN-OUT）
#   FW を入れる構成には test-sink-fw.cmds も入れる（WAN から捨て先への流れを FW が落とさないように）。
#   QoS の shaper は eth2 の送信だけに付くので、効くのは PC01 → PC03 の向きだけ。
#
#   scripts/run-bidir-configs.sh [出力先（既定: results/T3-bidir-configs/<日付>）] [巡回数（既定 3）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/T3-bidir-configs/$(date +%Y%m%d)}" rounds="${2:-3}"
v="${here}/../vyos"
base="${v}/tuning-1-offload.cmds ${v}/test-sink.cmds"
declare -A files=(
  [s1]="${base}"
  [s1rps]="${base} ${v}/tuning-2-rps.cmds"
  [s1ft]="${base} ${v}/tuning-3-flowtable.cmds"
  [s1perf]="${base} ${v}/tuning-4-performance.cmds"
  [s1fw]="${base} ${v}/fw-stateful.cmds ${v}/test-sink-fw.cmds"
  [s1fwft]="${base} ${v}/fw-stateful.cmds ${v}/test-sink-fw.cmds ${v}/tuning-3-flowtable.cmds"
  [s1dfw]="${base} ${v}/dscp-firewall.cmds"
  [s1qos]="${base} ${v}/qos-ngn-out.cmds"
)
order=(s1 s1rps s1ft s1perf s1fw s1fwft s1dfw s1qos)
mkdir -p "${out}"
for h in pc01 pc03; do scp -q "${here}/pktgen-run.sh" "${h}:~/pktgen-run.sh"; done

cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/(encrypted-password|key|community) '[^']*'/\1 '<伏せ字>'/" | sort > "$1"
}
to_base() {
  bash "${here}/pc02-commit.sh" --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
trap 'echo "== 基準に戻す"; to_base; cfg "${out}/pc02-config-end.cmds"' EXIT

to_base
cfg "${out}/pc02-config-start.cmds"
for r in $(seq 1 "${rounds}"); do
  for c in "${order[@]}"; do
    d="${out}/${c}/round${r}"
    mkdir -p "${d}"
    echo "== 巡回 ${r}/${rounds}: ${c}"
    to_base
    # shellcheck disable=SC2086
    bash "${here}/pc02-commit.sh" ${files[$c]} >/dev/null
    cfg "${d}/pc02-config.cmds"
    sleep 5
    for tol in 0 0.001; do
      python3 -I "${here}/t3_search.py" --frame 64 --flows many --bidir --tolerance "${tol}" \
        --out "${d}/64B-many-bidir-tol${tol}" | grep -E "^結果" | sed "s/^/   ${c} tol=${tol}: /" || true
    done
  done
done
echo "保存先: ${out}"
