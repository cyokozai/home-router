#!/usr/bin/env bash
# P4: 長時間の連続運転。本番に近い構成（①＋ステートフル FW＋flowtable＋firewall の set dscp）で、64B 多数フローを
# 双方向に一定のレートで 1 時間流し、1 分ごとのロス、PC02 の CPU と温度を記録する。崩れたり、熱で遅くなったりしないかを見る。
#   レートは引数で渡す（既定は 4.0 Mpps。PDR の 9 割程度を目安にする）。
#   温度は PC02 の hwmon（CPU の coretemp など）。X710 は温度を出さないので、NIC の温度は測れない。
#
#   scripts/run-p4-soak.sh [出力先（既定: results/P4-soak/<日付>）] [レート pps（既定 4000000）] [分（既定 60）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/P4-soak/$(date +%Y%m%d)}" rate="${2:-4000000}" minutes="${3:-60}"
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
# PC02 の温度と CPU を 1 分ごとに記録する（管理系の SSH と PC03 の Prometheus だけを使う）
watch_pc02() {
  while true; do
    t=$(ssh pc02 'for f in /sys/class/hwmon/hwmon*/temp*_input; do d=$(dirname $f); echo "$(cat $d/name):$(basename $f _input)=$(cat $f)"; done | tr "\n" " "' 2>/dev/null || echo "取得失敗")
    cpu=$(ssh pc03 "curl -s --get localhost:9090/api/v1/query --data-urlencode 'query=sum(1 - rate(node_cpu_seconds_total{host=\"pc02\",mode=\"idle\"}[60s]))'" | jq -r '.data.result[0].value[1] // "null"' 2>/dev/null || echo null)
    echo "$(date -Iseconds) cpu_sum=${cpu} ${t}" >> "${out}/pc02-watch.log"
    sleep 60
  done
}
trap 'echo "== 基準に戻す"; kill "${watcher:-0}" 2>/dev/null || true; to_base; cfg "${out}/pc02-config-end.cmds"' EXIT

to_base
bash "${here}/pc02-commit.sh" "${v}/tuning-1-offload.cmds" "${v}/fw-stateful.cmds" "${v}/tuning-3-flowtable.cmds" \
  "${v}/dscp-firewall.cmds" "${v}/test-sink.cmds" "${v}/test-sink-fw.cmds" >/dev/null
cfg "${out}/pc02-config.cmds"
sleep 5
watch_pc02 &
watcher=$!
python3 -I "${here}/t3_search.py" --frame 64 --flows many --bidir --fixed-pps "${rate}" --final-seconds 60 --repeat "${minutes}" \
  --out "${out}/64B-many-bidir-fixed" | grep -E "^結果|試行" | tail -3 || true
echo "保存先: ${out}"
