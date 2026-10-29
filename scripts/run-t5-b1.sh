#!/usr/bin/env bash
# 試験 T5（ステートフル FW のコスト）と B1（firewall の set dscp のコスト）を、① を基準にまとめて測る。
#   構成: s1    ①（リング 4096。基準）
#         s1fw  ①＋ステートフル FW（vyos/fw-stateful.cmds）
#         s1fwft ①＋ステートフル FW＋flowtable（tuning-3-flowtable.cmds）
#         s1dfw ①＋firewall の set dscp（vyos/dscp-firewall.cmds）
#   測るもの: T3 の 64B（1 フロー・多数フロー × NDR・PDR）。巡回ごとに 4 構成を回し、毎回基準から入れ直す。
#   片方向（PC01 → PC03）。pktgen の UDP は片方向で conntrack 上は established にならないため、
#   flowtable は効かない見込み（効かないこと自体を記録する）。
#
#   scripts/run-t5-b1.sh [出力先（既定: results/T5-B1/<日付>）] [巡回数（既定 3）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/T5-B1/$(date +%Y%m%d)}" rounds="${2:-3}"
v="${here}/../vyos"
declare -A files=(
  [s1]="${v}/tuning-1-offload.cmds"
  [s1fw]="${v}/tuning-1-offload.cmds ${v}/fw-stateful.cmds"
  [s1fwft]="${v}/tuning-1-offload.cmds ${v}/fw-stateful.cmds ${v}/tuning-3-flowtable.cmds"
  [s1dfw]="${v}/tuning-1-offload.cmds ${v}/dscp-firewall.cmds"
)
order=(s1 s1fw s1fwft s1dfw)
mkdir -p "${out}"
scp -q "${here}/pktgen-run.sh" pc01:~/pktgen-run.sh

cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/ (encrypted-password|key|community) ('[^']*'|[^ ']+)/ \1 '<伏せ字>'/g" | sort > "$1"
}
state() {
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -g $i | sed -n "/Current/,+5p"; done; sudo nft list ruleset | grep -E "hook forward|flowtable|dscp set|counter packets" | head -40; sudo conntrack -C 2>/dev/null' > "$1" 2>&1
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
    sleep 5
    for flows in single many; do
      for tol in 0 0.001; do
        python3 -I "${here}/t3_search.py" --frame 64 --flows "${flows}" --tolerance "${tol}" \
          --out "${d}/64B-${flows}-tol${tol}" | grep -E "^結果" | sed "s/^/   ${c} ${flows} tol=${tol}: /" || true
      done
    done
    state "${d}/pc02-state-after.txt"   # nft のカウンタと conntrack の数（flowtable に乗ったかを見る）
  done
done
echo "保存先: ${out}"
