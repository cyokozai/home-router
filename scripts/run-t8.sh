#!/usr/bin/env bash
# 試験 T8（QoS の set-dscp のコスト。CLAUDE.md §6-4・§8）。
#   構成: ①（リング 4096。T4 の確かめ直しで効くと分かった基準）/ ①＋QoS（vyos/qos-ngn-out.cmds、eth2 の送信に HTB）
#   測るもの: T3 の 64B（1 フロー・多数フロー × NDR・PDR）と、T1 の TCP（IPv4・IPv6、-P 4、上り、60 秒）
#   巡回ごとに 2 構成を回し、毎回基準から入れ直す。試験の流れ（pktgen・iperf3）は書き換えの対象（UDP 51820）
#   ではないので default のクラスを通る。測るのは「shaper（HTB）を通すこと」のコスト。
#
#   scripts/run-t8.sh [出力先（既定: results/T8/<日付>）] [巡回数（既定 3）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/T8/$(date +%Y%m%d)}" rounds="${2:-3}"
v="${here}/../vyos"
declare -A files=(
  [s1]="${v}/tuning-1-offload.cmds"
  [s1q]="${v}/tuning-1-offload.cmds ${v}/qos-ngn-out.cmds"
)
order=(s1 s1q)
mkdir -p "${out}"
scp -q "${here}/pktgen-run.sh" pc01:~/pktgen-run.sh

cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/(encrypted-password|key|community) '[^']*'/\1 '<伏せ字>'/" | sort > "$1"
}
state() {
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -g $i | sed -n "/Current/,+5p"; cat /sys/class/net/$i/queues/rx-0/rps_cpus; sudo tc qdisc show dev $i; done' > "$1" 2>&1
}
to_base() {
  bash "${here}/pc02-commit.sh" --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
trap 'echo "== 基準に戻す"; to_base; cfg "${out}/pc02-config-end.cmds"; state "${out}/pc02-state-end.txt"' EXIT

to_base
cfg "${out}/pc02-config-start.cmds"
state "${out}/pc02-state-start.txt"

for r in $(seq 1 "${rounds}"); do
  for c in "${order[@]}"; do
    d="${out}/${c}/round${r}"
    mkdir -p "${d}"
    echo "== 巡回 ${r}/${rounds}: ${c}"
    to_base
    # shellcheck disable=SC2086
    bash "${here}/pc02-commit.sh" ${files[$c]} >/dev/null
    cfg "${d}/pc02-config.cmds"
    state "${d}/pc02-state.txt"
    sleep 5
    for flows in single many; do
      for tol in 0 0.001; do
        python3 -I "${here}/t3_search.py" --frame 64 --flows "${flows}" --tolerance "${tol}" \
          --out "${d}/64B-${flows}-tol${tol}" | grep -E "^結果" | sed "s/^/   ${c} ${flows} tol=${tol}: /" || true
      done
    done
    RUNS=1 PARS=4 DIRS=up FAMS="4 6" bash "${here}/run-t1.sh" "${d}/T1" | grep -E "^== " | sed "s/^/   ${c} TCP /" || true
  done
done
echo "保存先: ${out}"
