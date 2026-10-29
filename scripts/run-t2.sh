#!/usr/bin/env bash
# 試験 T2（ルーティング・UDP。CLAUDE.md §8）を作業用 Mac から流す。
#   PC01（192.168.10.10）→ PC02 → PC03（192.168.20.2）。UDP の 1518B（-l 1472）と 512B（-l 466）。
#   10G のワイヤーレート分を -P 本に分けて送り（1 本あたり -b を割り振る）、PC02 が落とさず転送できるかを見る。
#   1 条件 60 秒 × 3 回、最初の 5 秒は捨てる（-O 5）。
#
#   scripts/run-t2.sh [出力先（既定: results/T2/<日時>）]
#   環境変数: RUNS / SECS / OMIT / PARALLEL（既定 4）/ SIZES（"1472 466"）
#
# pps は各機の ethtool -S の差分（PC01 sfp0 の送信、PC02 eth1 の受信・eth2 の送信、PC03 sfp0 の受信）を、
# 測定にかかった時間（-O を含む）で割って出す。ロス率は iperf3 の受信側の値と、カウンタの差の両方を残す。
set -euo pipefail

out="${1:-results/T2/$(date +%Y%m%d-%H%M%S)}"
runs="${RUNS:-3}" secs="${SECS:-60}" omit="${OMIT:-5}" par="${PARALLEL:-4}" sizes="${SIZES:-1472 466}"
mkdir -p "${out}"

ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
  | sed -E "s/(encrypted-password|key|community) '[^']*'/\1 '<伏せ字>'/" > "${out}/pc02-config.cmds"
cat > "${out}/conditions.json" <<EOF
{"test": "T2", "started": "$(date -Iseconds)", "runs": ${runs}, "seconds": ${secs}, "omit": ${omit},
 "parallel": ${par}, "udp_payloads": "${sizes}", "path": "pc01 sfp0 -> pc02 eth1/eth2 -> pc03 sfp0",
 "pc02_phase": "phase1 (baseline)"}
EOF

pc02_cpu_max() {
  ssh pc03 "curl -s --get localhost:9090/api/v1/query --data-urlencode 'query=max(1 - rate(node_cpu_seconds_total{host=\"pc02\",mode=\"idle\"}[$1s]))'" \
    | jq -r '.data.result[0].value[1] // "null"'
}
counter() { awk -v k="$2" -F: '{gsub(/ /,"",$1); gsub(/ /,"",$2)} $1==k {print $2}' "$1"; }

echo "id,payload,frame,run,offered_gbps,recv_gbps,iperf_lost_percent,pc01_tx_pps,pc02_rx_pps,pc02_tx_pps,pc03_rx_pps,counter_loss_percent,pc02_core_max" > "${out}/runs.csv"
for len in ${sizes}; do
  frame=$((len + 8 + 20 + 14 + 4))                       # UDP + IPv4 + Ethernet + FCS
  linerate_pps=$((10000000000 / ((frame + 20) * 8)))     # プリアンブル・SFD・IFG の 20 バイトを足す
  offered_bps=$((linerate_pps * len * 8))                # iperf3 の -b はペイロードのビット数
  per=$((offered_bps / par))
  cond="udp-${frame}B"
  mkdir -p "${out}/${cond}"
  for n in $(seq 1 "${runs}"); do
    ssh pc01 "ethtool -S sfp0" > "${out}/${cond}/ethtool-before-pc01-run${n}.txt"
    ssh pc02 "sudo ethtool -S eth1" > "${out}/${cond}/ethtool-before-pc02-eth1-run${n}.txt"
    ssh pc02 "sudo ethtool -S eth2" > "${out}/${cond}/ethtool-before-pc02-eth2-run${n}.txt"
    ssh pc03 "ethtool -S sfp0" > "${out}/${cond}/ethtool-before-pc03-run${n}.txt"
    ssh pc03 "iperf3 -s -1 -D -B 192.168.20.2"
    sleep 1
    t0=$(date +%s.%N)
    ssh pc01 "iperf3 -c 192.168.20.2 -u -l ${len} -b ${per} -P ${par} -t ${secs} -O ${omit} -J" > "${out}/${cond}/run${n}.json"
    t1=$(date +%s.%N)
    cpu=$(pc02_cpu_max $((secs - omit)))
    ssh pc01 "ethtool -S sfp0" > "${out}/${cond}/ethtool-after-pc01-run${n}.txt"
    ssh pc02 "sudo ethtool -S eth1" > "${out}/${cond}/ethtool-after-pc02-eth1-run${n}.txt"
    ssh pc02 "sudo ethtool -S eth2" > "${out}/${cond}/ethtool-after-pc02-eth2-run${n}.txt"
    ssh pc03 "ethtool -S sfp0" > "${out}/${cond}/ethtool-after-pc03-run${n}.txt"
    d="${out}/${cond}"
    dur=$(echo "${t1} - ${t0}" | bc)
    a=$(( $(counter "${d}/ethtool-after-pc01-run${n}.txt" tx_packets) - $(counter "${d}/ethtool-before-pc01-run${n}.txt" tx_packets) ))
    b=$(( $(counter "${d}/ethtool-after-pc02-eth1-run${n}.txt" rx_packets) - $(counter "${d}/ethtool-before-pc02-eth1-run${n}.txt" rx_packets) ))
    c=$(( $(counter "${d}/ethtool-after-pc02-eth2-run${n}.txt" tx_packets) - $(counter "${d}/ethtool-before-pc02-eth2-run${n}.txt" tx_packets) ))
    # PC03 の BCM57840（bnx2x）は rx_packets が無く、rx_ucast_packets で数える
    e=$(( $(counter "${d}/ethtool-after-pc03-run${n}.txt" rx_ucast_packets) - $(counter "${d}/ethtool-before-pc03-run${n}.txt" rx_ucast_packets) ))
    line=$(jq -r --arg o "$(echo "scale=3; ${offered_bps}/1000000000" | bc)" \
      '[$o, (.end.sum.bits_per_second/1e9|tostring), (.end.sum.lost_percent|tostring)] | join(",")' "${d}/run${n}.json")
    pps=$(printf '%.0f,%.0f,%.0f,%.0f' "$(echo "${a}/${dur}" | bc -l)" "$(echo "${b}/${dur}" | bc -l)" \
          "$(echo "${c}/${dur}" | bc -l)" "$(echo "${e}/${dur}" | bc -l)")
    closs=$(echo "scale=6; (${a} - ${e}) * 100 / ${a}" | bc)
    echo "T2,${len},${frame},${n},${line},${pps},${closs},${cpu}" >> "${out}/runs.csv"
    echo "== ${cond} run ${n}/${runs}: ${line} pps(pc01tx,pc02rx,pc02tx,pc03rx)=${pps} counter_loss=${closs}% cpu=${cpu}"
    sleep 2
  done
done
echo "保存先: ${out}"
