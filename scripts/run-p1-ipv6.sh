#!/usr/bin/env bash
# P1: IPv6 の転送レートの上限。IPv6＋UDP は 64B に収まらないので、RFC 5180 にならって 78B で測り、
# 比べるために IPv4 も同じ 78B で測る。双方向の送信、多数フロー、NDR と PDR。PC02 は ①。
#   IPv6 の捨て先: VyOS の静的 ARP は IPv4 だけなので、PC02 には何も足さない。PC01 に 2001:db8:10::99 を付けて
#   NDP に答えさせ、宛てた UDP は X710 の ntuple のフィルタで NIC が捨てる（PC01 の CPU を受信に使わない）。
#
#   scripts/run-p1-ipv6.sh [出力先（既定: results/P1-ipv6/<日付>）] [巡回数（既定 3）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/P1-ipv6/$(date +%Y%m%d)}" rounds="${2:-3}"
v="${here}/../vyos"
mkdir -p "${out}"
for h in pc01 pc03; do scp -q "${here}/pktgen-run.sh" "${h}:~/pktgen-run.sh"; done

cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/(encrypted-password|key) '[^']*'/\1 '<伏せ字>'/" | sort > "$1"
}
to_base() {
  bash "${here}/pc02-commit.sh" --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
sink6_up() {
  ssh pc01 'sudo ip -6 addr replace 2001:db8:10::99/128 dev sfp0 nodad
            sudo ethtool -N sfp0 flow-type udp6 dst-ip 2001:db8:10::99 action -1
            sudo ethtool -n sfp0'
}
sink6_down() {
  ssh pc01 'for id in $(sudo ethtool -n sfp0 | sed -n "s/^Filter: \([0-9]*\)$/\1/p"); do sudo ethtool -N sfp0 delete "${id}"; done
            sudo ip -6 addr del 2001:db8:10::99/128 dev sfp0 2>/dev/null; true'
}
trap 'echo "== 基準に戻す"; to_base; sink6_down; cfg "${out}/pc02-config-end.cmds"' EXIT

to_base
sink6_up | tee "${out}/pc01-sink6.txt"
bash "${here}/pc02-commit.sh" "${v}/tuning-1-offload.cmds" "${v}/test-sink.cmds" >/dev/null
cfg "${out}/pc02-config.cmds"
sleep 5
# PC02 が捨て先の NDP を解決できるか（1 個だけ送って neighbor の表を見る）
ssh pc03 'ping -6 -c 2 -W 1 2001:db8:10::99 >/dev/null 2>&1; true'
ssh pc02 'ip -6 neigh show 2001:db8:10::99' | tee "${out}/pc02-neigh.txt"

for r in $(seq 1 "${rounds}"); do
  for fam in 4 6; do
    for tol in 0 0.001; do
      d="${out}/round${r}/78B-many-v${fam}-bidir-tol${tol}"
      python3 -I "${here}/t3_search.py" --frame 78 --family "${fam}" --flows many --bidir --tolerance "${tol}" \
        --out "${d}" | grep -E "^結果" | sed "s/^/   round${r} IPv${fam} tol=${tol}: /" || true
    done
  done
done
echo "保存先: ${out}"
