#!/usr/bin/env bash
# 送信側（PC01・PC03）が 64B をどこまで送れるかを、pktgen のスレッド数ごとに測る（作業用 Mac から実行）。
# 送り先は PC02（PC01 からは eth1、PC03 からは eth2）。PC02 が落としても、送信側の数は NIC の送信カウンタで数える。
#   scripts/sender-scaling.sh [出力先] [秒数（既定 5）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/sender/$(date +%Y%m%d)}" secs="${2:-5}"
mkdir -p "${out}"
echo "host,threads,burst,tx_pps,cpu_busy_max" > "${out}/sender.csv"

# host IF 宛先 IP 宛先 MAC 送信元 IP 送信カウンタ名 最大スレッド
specs=(
  "pc01 sfp0 192.168.20.2 6c:fe:54:6e:27:a0 192.168.10.10 tx_packets 4"
  "pc03 sfp0 192.168.10.10 6c:fe:54:6e:27:a1 192.168.20.2 tx_ucast_packets 16"
)
for spec in "${specs[@]}"; do
  read -r host iface dst dmac src key maxt <<<"${spec}"
  scp -q "${here}/pktgen-run.sh" "${host}:~/pktgen-run.sh"
  for t in 1 2 4 8 16; do
    ((t <= maxt)) || continue
    # PC03 の BCM57840 は送信キューが 4 本（ethtool -l）。pktgen はスレッド i を送信キュー i に固定するので、
    # キューより多いスレッドは使えない。PC03 は 4 本まで
    if [[ ${host} == pc03 ]] && ((t > 4)); then continue; fi
    for burst in 1 32; do
      b=$(ssh "${host}" "ethtool -S ${iface}" | awk -v k="${key}" -F: '{gsub(/ /,"",$1);gsub(/ /,"",$2)} $1==k{print $2}')
      ssh "${host}" "sudo IFACE=${iface} SIZE=60 DST=${dst} SRC=${src} DMAC=${dmac} THREADS=${t} DURATION=${secs} RATE=0 BURST=${burst} FLOWS=many bash ~/pktgen-run.sh" > "${out}/${host}-t${t}-b${burst}.log" &
      pid=$!
      sleep $((secs / 2))
      cpu=$(ssh "${host}" "mpstat -P ALL 1 1 | awk '/^Average/ && \$2 ~ /^[0-9]+$/ {b=100-\$NF; if (b>m) m=b} END{print m}'")
      wait "${pid}"
      a=$(ssh "${host}" "ethtool -S ${iface}" | awk -v k="${key}" -F: '{gsub(/ /,"",$1);gsub(/ /,"",$2)} $1==k{print $2}')
      pps=$(( (a - b) / secs ))
      echo "${host},${t},${burst},${pps},${cpu}" | tee -a "${out}/sender.csv"
    done
  done
done
