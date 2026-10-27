#!/usr/bin/env bash
# 試験 T1（ルーティング・TCP。CLAUDE.md §8）を作業用 Mac から流す。
#   PC01（192.168.10.10）→ PC02 → PC03（192.168.20.2）。並列数 1/4/16 × 上り・下り・双方向 × IPv4/IPv6。
#   1 条件 60 秒 × 3 回、最初の 5 秒は捨てる（-O 5）。
#
#   scripts/run-t1.sh [出力先（既定: results/T1/<日時>）]
#   環境変数: RUNS / SECS / OMIT / PARS（"1 4 16"）/ DIRS（"up down bidir"）/ FAMS（"4 6"）
#
# 操作は管理系の SSH だけで行う（§1 ルール 6）。iperf3 -J と、PC02 の ethtool -S の前後をそのまま残す。
# PC02 のコアごとの使用率は、PC03 の Prometheus から各回の終わりに取る（直近 SECS-5 秒の平均の、コア中の最大）。
set -euo pipefail

out="${1:-results/T1/$(date +%Y%m%d-%H%M%S)}"
runs="${RUNS:-3}" secs="${SECS:-60}" omit="${OMIT:-5}"
pars="${PARS:-1 4 16}" dirs="${DIRS:-up down bidir}" fams="${FAMS:-4 6}"
mkdir -p "${out}"

ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
  | sed -E "s/(encrypted-password|key) '[^']*'/\1 '<伏せ字>'/" > "${out}/pc02-config.cmds"
cat > "${out}/conditions.json" <<EOF
{"test": "T1", "started": "$(date -Iseconds)", "runs": ${runs}, "seconds": ${secs}, "omit": ${omit},
 "parallel": "${pars}", "directions": "${dirs}", "families": "${fams}",
 "path": "pc01 sfp0 -> pc02 eth1/eth2 -> pc03 sfp0", "pc02_phase": "phase1 (baseline)"}
EOF

# PC02 の CPU（各コアの使用率の最大、直近 w 秒の平均）を PC03 の Prometheus から取る
pc02_cpu_max() {
  local w=$1
  ssh pc03 "curl -s --get localhost:9090/api/v1/query --data-urlencode 'query=max(1 - rate(node_cpu_seconds_total{host=\"pc02\",mode=\"idle\"}[${w}s]))'" \
    | jq -r '.data.result[0].value[1] // "null"'
}

echo "id,family,parallel,direction,run,recv_gbps,sent_gbps,retransmits,pc02_core_max" > "${out}/runs.csv"
for fam in ${fams}; do
  if [[ ${fam} == 6 ]]; then dst=2001:db8:20::2; else dst=192.168.20.2; fi
  for par in ${pars}; do
    for dir in ${dirs}; do
      case ${dir} in up) opt="" ;; down) opt="-R" ;; bidir) opt="--bidir" ;; esac
      cond="v${fam}-P${par}-${dir}"
      mkdir -p "${out}/${cond}"
      for n in $(seq 1 "${runs}"); do
        ssh pc02 "sudo ethtool -S eth1" > "${out}/${cond}/ethtool-before-pc02-eth1-run${n}.txt"
        ssh pc02 "sudo ethtool -S eth2" > "${out}/${cond}/ethtool-before-pc02-eth2-run${n}.txt"
        ssh pc03 "iperf3 -s -1 -D -B ${dst}"
        sleep 1
        ssh pc01 "iperf3 -${fam} -c ${dst} -t ${secs} -O ${omit} -P ${par} ${opt} -J" > "${out}/${cond}/run${n}.json"
        cpu=$(pc02_cpu_max $((secs - omit)))
        ssh pc02 "sudo ethtool -S eth1" > "${out}/${cond}/ethtool-after-pc02-eth1-run${n}.txt"
        ssh pc02 "sudo ethtool -S eth2" > "${out}/${cond}/ethtool-after-pc02-eth2-run${n}.txt"
        line=$(jq -r --arg c "${cpu}" '[(.end.sum_received.bits_per_second/1e9), (.end.sum_sent.bits_per_second/1e9),
               (.end.sum_sent.retransmits // 0)] | map(tostring) | join(",")' "${out}/${cond}/run${n}.json")
        # 双方向では逆向きの合計も足す
        if [[ ${dir} == bidir ]]; then
          line=$(jq -r '[((.end.sum_received.bits_per_second + .end.sum_received_bidir_reverse.bits_per_second)/1e9),
                 ((.end.sum_sent.bits_per_second + .end.sum_sent_bidir_reverse.bits_per_second)/1e9),
                 ((.end.sum_sent.retransmits // 0) + (.end.sum_sent_bidir_reverse.retransmits // 0))] | map(tostring) | join(",")' \
                 "${out}/${cond}/run${n}.json")
        fi
        echo "T1,${fam},${par},${dir},${n},${line},${cpu}" >> "${out}/runs.csv"
        echo "== ${cond} run ${n}/${runs}: ${line} cpu=${cpu}"
        sleep 2
      done
    done
  done
done
echo "保存先: ${out}"
