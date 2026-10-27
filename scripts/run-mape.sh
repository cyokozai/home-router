#!/usr/bin/env bash
# 試験 T6・T7・B2（MAP-E 模擬。CLAUDE.md §6-3・§8）を作業用 Mac から流す。
#   1. PC03 を BR 役にする（lab/pc03-br.sh を実機の PC03 で実行。終わったら消す）
#   2. 構成ごとに PC02 を基準から入れ直す:
#        p2  ①＋phase2-mape.cmds（ポートセットは 1 組だけ。§6-3 の元の形）
#        p2b ①＋mape-portsets.cmds（ポートセット 15 組・外側の tclass 0。B2 の本番の形）
#   3. T6: iperf3 -c 198.51.100.1 を -P 4 と -P 32 で 60 秒（-O 5）
#   4. T7: PC03 の 10G 側でパケットを取り、外側の Next Header・内側の送信元ポート・MSS を数える。PMTUD を ping で確かめる
#   5. B2: -P 32 の最中に PC02 の conntrack から、変換後の送信元ポートがどの組に入ったかを数える
#   scripts/run-mape.sh [出力先（既定: results/T6-T7/<日付>）] [巡回数（既定 3）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/T6-T7/$(date +%Y%m%d)}" rounds="${2:-3}"
v="${here}/../vyos"
declare -A files=(
  [p2]="${v}/tuning-1-offload.cmds ${v}/phase2-mape.cmds"
  [p2b]="${v}/tuning-1-offload.cmds ${v}/mape-portsets.cmds"
)
mkdir -p "${out}"
to_base() {
  bash "${here}/pc02-commit.sh" --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
br_up() {
  scp -q "${here}/../lab/pc03-br.sh" pc03:/tmp/pc03-br.sh
  ssh pc03 'sudo sh /tmp/pc03-br.sh && ip -br addr show br0 && pgrep -f "iperf3 -s -B 198.51.100.1" >/dev/null || iperf3 -s -D -B 198.51.100.1'
}
br_down() {
  ssh pc03 'pkill -f "iperf3 -s -D -B 198.51.100.1" 2>/dev/null; sudo ip link del br0 2>/dev/null; sudo ip addr del 198.51.100.1/32 dev lo 2>/dev/null; sudo ip -6 addr del 2001:db8:20::b/64 dev sfp0 2>/dev/null; true'
}
trap 'echo "== 基準に戻す"; to_base; br_down' EXIT

br_up | tee "${out}/pc03-br.txt"
for r in $(seq 1 "${rounds}"); do
  for c in p2 p2b; do
    d="${out}/${c}/round${r}"
    mkdir -p "${d}"
    echo "== 巡回 ${r}/${rounds}: ${c}"
    to_base
    # shellcheck disable=SC2086
    bash "${here}/pc02-commit.sh" ${files[$c]} >/dev/null
    ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
      | sed -E "s/(encrypted-password|key) '[^']*'/\1 '<伏せ字>'/" | sort > "${d}/pc02-config.cmds"
    sleep 5
    ssh pc01 'ping -c3 -W1 198.51.100.1' > "${d}/ping.txt" 2>&1 || true

    # T7（1 巡目だけ）: 10G 側でパケットを取る（この試験だけ 10G で tcpdump を許す。§8）
    if [[ ${r} == 1 ]]; then
      ssh pc03 'sudo timeout 25 tcpdump -ni sfp0 -c 400 -v "ip6 proto 4" > /tmp/t7-cap.txt 2>&1 &'
      sleep 2
      ssh pc01 'iperf3 -c 198.51.100.1 -t 5 -P 4' > /dev/null 2>&1 || true
      ssh pc01 'ping -M do -s 1432 -c 3 -W1 198.51.100.1; ping -M do -s 1472 -c 2 -W1 198.51.100.1' > "${d}/t7-pmtud.txt" 2>&1 || true
      sleep 20
      ssh pc03 'cat /tmp/t7-cap.txt' > "${d}/t7-capture.txt"
    fi

    for par in 4 32; do
      ssh pc01 "iperf3 -c 198.51.100.1 -t 60 -O 5 -P ${par} -J" > "${d}/T6-P${par}.json" 2> "${d}/T6-P${par}.err" &
      ipid=$!
      if [[ ${par} == 32 ]]; then
        sleep 20
        # B2: 変換後の送信元ポート（返りの宛先ポート）を数える
        ssh pc02 'sudo conntrack -L -p tcp -d 198.51.100.1 2>/dev/null' > "${d}/B2-conntrack.txt" || true
      fi
      wait "${ipid}" || true
      jq -r --arg c "${c}" --arg p "${par}" '"   \($c) -P \($p): 受信 \(.end.sum_received.bits_per_second/1e9*1000|round/1000) Gbps / 再送 \(.end.sum_sent.retransmits) / 流れ \(.start.test_start.num_streams)"' \
        "${d}/T6-P${par}.json" 2>/dev/null || echo "   ${c} -P ${par}: iperf3 が結果を返さなかった（$(head -c 200 "${d}/T6-P${par}.err")）"
      sleep 3
    done
  done
done
echo "保存先: ${out}"
