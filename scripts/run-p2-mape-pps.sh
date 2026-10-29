#!/usr/bin/env bash
# P2: MAP-E（ipip6＋NAT のポートセット 15 組）の転送レートの上限。T6 は 1500B の TCP だけで、トンネルと NAT の
# 1 パケットあたりのコストが見えていない。64B の UDP を PC01 → 198.51.100.0/24（PC03 の外側）へ片方向で流す。
#   構成: s1   ①（素のルーティング。同じ宛先・同じ送信元ポートで比べる）
#         p2b  ①＋mape-portsets.cmds（本番の形）
#   流れの作り方（many）: 送信元ポートを 4096〜4335 の 240 個（16 個の塊 15 個 = 15 本の NAT のルールに 1 塊ずつ）、
#   宛先を 198.51.100.0〜255 の 256 個に散らす。1 本のルールに入る流れは 16 ポート × 256 宛先で、変換先の
#   16 ポート × 256 宛先とちょうど同じ数なので、NAT のポートは足りる（足りずに落ちたのか、遅くて落ちたのかが混ざらない）。
#   1 フロー（single）は p2b だけで測る。
#
#   scripts/run-p2-mape-pps.sh [出力先（既定: results/P2-mape-pps/<日付>）] [巡回数（既定 3）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/P2-mape-pps/$(date +%Y%m%d)}" rounds="${2:-3}"
v="${here}/../vyos"
declare -A files=(
  [s1]="${v}/tuning-1-offload.cmds"
  [p2b]="${v}/tuning-1-offload.cmds ${v}/mape-portsets.cmds"
)
mkdir -p "${out}"
scp -q "${here}/pktgen-run.sh" pc01:~/pktgen-run.sh

cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/ (encrypted-password|key|community) ('[^']*'|[^ ']+)/ \1 '<伏せ字>'/g" | sort > "$1"
}
to_base() {
  bash "${here}/pc02-commit.sh" --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
br_up() {
  scp -q "${here}/../lab/pc03-br.sh" pc03:/tmp/pc03-br.sh
  ssh pc03 'sudo sh /tmp/pc03-br.sh && ip -br addr show br0'
}
br_down() {
  ssh pc03 'sudo ip link del br0 2>/dev/null; sudo ip addr del 198.51.100.1/32 dev lo 2>/dev/null; sudo ip -6 addr del 2001:db8:20::b/64 dev sfp0 2>/dev/null; true'
}
trap 'echo "== 基準に戻す"; to_base; br_down; cfg "${out}/pc02-config-end.cmds"' EXIT

to_base
br_down
br_up | tee "${out}/pc03-br.txt"
flow=(--dst 198.51.100.1 --dst-max 198.51.100.255 --sport-min 4096 --sport-max 4335)
for r in $(seq 1 "${rounds}"); do
  for c in s1 p2b; do
    d="${out}/${c}/round${r}"
    mkdir -p "${d}"
    echo "== 巡回 ${r}/${rounds}: ${c}"
    to_base
    # shellcheck disable=SC2086
    bash "${here}/pc02-commit.sh" ${files[$c]} >/dev/null
    cfg "${d}/pc02-config.cmds"
    sleep 5
    ssh pc01 'ping -c 2 -W 1 198.51.100.1' > "${d}/ping.txt" 2>&1 || true
    conds=("many 0" "many 0.001")
    [[ ${c} == p2b ]] && conds+=("single 0" "single 0.001")
    for cond in "${conds[@]}"; do
      read -r flows tol <<<"${cond}"
      [[ -f ${d}/64B-${flows}-tol${tol}/result.json ]] && { echo "   ${c} ${flows} tol=${tol}: 済み（飛ばす）"; continue; }
      python3 -I "${here}/t3_search.py" --frame 64 --flows "${flows}" --tolerance "${tol}" "${flow[@]}" \
        --out "${d}/64B-${flows}-tol${tol}" | grep -E "^結果" | sed "s/^/   ${c} ${flows} tol=${tol}: /" || true
    done
    # NAT の組がどう使われたか（変換後のポートの数）
    ssh pc02 'sudo conntrack -C; sudo conntrack -L -p udp -d 198.51.100.0/24 2>/dev/null | grep -o "dport=[0-9]* \[\|dport=[0-9]*$" | sort | uniq -c | sort -rn | head -5' \
      > "${d}/conntrack.txt" 2>&1 || true
  done
done
echo "保存先: ${out}"
