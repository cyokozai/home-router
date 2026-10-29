#!/usr/bin/env bash
# T5 の続き: ステートフル FW と flowtable のコストを TCP で測る。片方向の UDP（pktgen）は conntrack 上 established に
# ならず flowtable に乗らなかった（results/T5-B1）。TCP は返りの ACK があるので、2 つ目のパケットから flowtable に乗る。
#   構成: s1（①）/ s1fw（①＋FW）/ s1fwft（①＋FW＋flowtable）
#   流し方: PC01 → PC03（192.168.20.2）に iperf3 -P 16 を 30 秒（-O 5）。MSS は既定（1448）と 88（線上 158 バイトの
#          小さいフレーム。1 秒あたりのパケット数を増やし、転送の処理の重さが速さに出るようにする）
#   見るもの: 受信の Gbps、PC02 の CPU（コアの最大と、全コアの合計 = 何コア分使ったか）、flowtable に乗った接続の数
#
#   scripts/run-tcp-ft.sh [出力先（既定: results/T5-tcp/<日付>）] [巡回数（既定 3）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/T5-tcp/$(date +%Y%m%d)}" rounds="${2:-3}"
secs=30 omit=5
v="${here}/../vyos"
declare -A files=(
  [s1]="${v}/tuning-1-offload.cmds"
  [s1fw]="${v}/tuning-1-offload.cmds ${v}/fw-stateful.cmds"
  [s1fwft]="${v}/tuning-1-offload.cmds ${v}/fw-stateful.cmds ${v}/tuning-3-flowtable.cmds"
)
order=(s1 s1fw s1fwft)
mkdir -p "${out}"

cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/ (encrypted-password|key|community) ('[^']*'|[^ ']+)/ \1 '<伏せ字>'/g" | sort > "$1"
}
to_base() {
  bash "${here}/pc02-commit.sh" --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
# PC02 の CPU を PC03 の Prometheus から取る（直近 w 秒の平均）。max はコアの最大、sum は全コアの合計
pc02_cpu() {
  local agg=$1 w=$2
  ssh pc03 "curl -s --get localhost:9090/api/v1/query --data-urlencode 'query=${agg}(1 - rate(node_cpu_seconds_total{host=\"pc02\",mode=\"idle\"}[${w}s]))'" \
    | jq -r '.data.result[0].value[1] // "null"'
}
trap 'echo "== 基準に戻す"; to_base; ssh pc03 "pkill -x iperf3" || true; cfg "${out}/pc02-config-end.cmds"' EXIT

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
    for mss in default 88; do
      opt=""; [[ ${mss} == 88 ]] && opt="-M 88"
      ssh pc03 'pkill -x iperf3; iperf3 -s -D -B 192.168.20.2; sleep 1'
      ssh pc01 "timeout $((secs + 30)) iperf3 -c 192.168.20.2 -t ${secs} -O ${omit} -P 16 ${opt} -J" > "${d}/mss-${mss}.json" 2> "${d}/mss-${mss}.err" &
      ipid=$!
      sleep $((secs / 2))
      # flowtable に乗った接続は conntrack で [OFFLOAD] と表示される
      ssh pc02 'echo "conntrack: $(sudo conntrack -L -p tcp -d 192.168.20.2 2>/dev/null | wc -l) 本、うち OFFLOAD $(sudo conntrack -L -p tcp -d 192.168.20.2 2>/dev/null | grep -c OFFLOAD) 本"' > "${d}/mss-${mss}-conntrack.txt" || true
      wait "${ipid}" || true
      w=$((secs - omit - 2))
      printf '{"cpu_max": %s, "cpu_sum": %s, "window_s": %s}\n' "$(pc02_cpu max ${w})" "$(pc02_cpu sum ${w})" "${w}" > "${d}/mss-${mss}-cpu.json"
      jq -r --arg c "${c}" --arg m "${mss}" '"   \($c) MSS \($m): 受信 \(.end.sum_received.bits_per_second/1e9*1000|round/1000) Gbps / 再送 \(.end.sum_sent.retransmits)"' \
        "${d}/mss-${mss}.json" 2>/dev/null || echo "   ${c} MSS ${mss}: iperf3 が結果を返さなかった"
      echo "     $(cat "${d}/mss-${mss}-conntrack.txt") / CPU $(cat "${d}/mss-${mss}-cpu.json")"
      sleep 3
    done
  done
done
echo "保存先: ${out}"
