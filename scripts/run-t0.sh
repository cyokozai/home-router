#!/usr/bin/env bash
# 試験 T0（端末の実力。CLAUDE.md §8）を作業用 Mac から流す。
#   PC01 → PC02（192.168.10.1）と PC03 → PC02（192.168.20.1）を、VyOS で終端して測る。
#   1 条件 60 秒 × 3 回、最初の 5 秒は捨てる（-O 5）。並列数は既定 4。
#
#   scripts/run-t0.sh [出力先（既定: results/T0/<日時>）]
#
# 操作はすべて管理系の SSH（~/.ssh/config の pc01 / pc02 / pc03）で行い、10G 側では SSH しない（§1 ルール 6）。
# 結果は iperf3 -J の JSON と、試験前後の ethtool -S をそのまま残す（§1 ルール 4）。
set -euo pipefail

out="${1:-results/T0/$(date +%Y%m%d-%H%M%S)}"
runs="${RUNS:-3}" secs="${SECS:-60}" omit="${OMIT:-5}" par="${PARALLEL:-4}"
mkdir -p "${out}"

# 条件の記録（各機の版・CPU・NIC・PC02 の設定）
{
  echo "{"
  echo "  \"test\": \"T0\", \"started\": \"$(date -Iseconds)\","
  echo "  \"runs\": ${runs}, \"seconds\": ${secs}, \"omit\": ${omit}, \"parallel\": ${par},"
  echo "  \"pc01_iperf3\": \"$(ssh pc01 'iperf3 --version | head -1')\","
  echo "  \"pc02_iperf3\": \"$(ssh pc02 'iperf3 --version | head -1')\","
  echo "  \"pc03_iperf3\": \"$(ssh pc03 'iperf3 --version | head -1')\","
  echo "  \"pc02_vyos\": \"$(ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show version' | awk -F': *' '/^Version/{print $2}')\""
  echo "}"
} > "${out}/conditions.json"
ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
  | sed -E "s/ (encrypted-password|key|community) ('[^']*'|[^ ']+)/ \1 '<伏せ字>'/g" > "${out}/pc02-config.cmds"

# client ホスト名、client の 10G IF、PC02 の受け口のアドレス、PC02 の IF
pairs=("pc01 sfp0 192.168.10.1 eth1" "pc03 sfp0 192.168.20.1 eth2")

for pair in "${pairs[@]}"; do
  read -r client cif addr sif <<<"${pair}"
  dir="${out}/${client}-to-pc02"
  mkdir -p "${dir}"
  for n in $(seq 1 "${runs}"); do
    echo "== ${client} → pc02（${addr}）run ${n}/${runs}"
    ssh "${client}" "ethtool -S ${cif}" > "${dir}/ethtool-before-${client}-run${n}.txt"
    ssh pc02 "sudo ethtool -S ${sif}" > "${dir}/ethtool-before-pc02-run${n}.txt"
    # 1 回で終わる iperf3 サーバを PC02 の 10G 側のアドレスだけで待ち受ける
    ssh pc02 "iperf3 -s -1 -D -B ${addr}"
    sleep 1
    ssh "${client}" "iperf3 -c ${addr} -t ${secs} -O ${omit} -P ${par} -J" > "${dir}/run${n}.json"
    ssh "${client}" "ethtool -S ${cif}" > "${dir}/ethtool-after-${client}-run${n}.txt"
    ssh pc02 "sudo ethtool -S ${sif}" > "${dir}/ethtool-after-pc02-run${n}.txt"
    jq -r '"   受信 \(.end.sum_received.bits_per_second/1e9*1000|round/1000) Gbps / 再送 \(.end.sum_sent.retransmits)"' "${dir}/run${n}.json"
    sleep 2
  done
done
echo "保存先: ${out}"
