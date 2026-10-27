#!/usr/bin/env bash
# 試験 T4 の確かめ直し。積み上げずに、構成ごとに基準から入れ直して各 3 回ずつ測る。
#   構成: 基準 / ①だけ / ①④ / ①②④（① リング 4096、② RPS、④ 性能プロファイル）
#   測るもの: T3 の 64B（1 フロー・多数フロー × NDR・PDR）
#   順番: 巡回ごとに 4 構成を回す（時間による変化が特定の構成に偏らないように）
# T2 の 512B は、T4 の全段階で送信側（約 91 万 pps）の頭打ちで差が出なかったので測らない。
#
#   scripts/run-t4b.sh [出力先（既定: results/T4b/<日付>）] [巡回数（既定 3）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/T4b/$(date +%Y%m%d)}" rounds="${2:-3}"
v="${here}/../vyos"
declare -A files=(
  [base]=""
  [s1]="${v}/tuning-1-offload.cmds"
  [s14]="${v}/tuning-1-offload.cmds ${v}/tuning-4-performance.cmds"
  [s124]="${v}/tuning-1-offload.cmds ${v}/tuning-2-rps.cmds ${v}/tuning-4-performance.cmds"
)
order=(base s1 s14 s124)
mkdir -p "${out}"
scp -q "${here}/pktgen-run.sh" pc01:~/pktgen-run.sh

cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/(encrypted-password|key) '[^']*'/\1 '<伏せ字>'/" | sort > "$1"
}
state() {
  # 設定には出ない実行時の状態（リング・RPS・sysctl）も残し、基準に戻ったかを比べる
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -g $i | sed -n "/Current/,+5p"; cat /sys/class/net/$i/queues/rx-0/rps_cpus; done;
            sudo sysctl -a 2>/dev/null | grep -E "^net\.core\.|^net\.ipv4\.tcp_|^vm\.|^kernel\.sched" | sort' > "$1" 2>&1
}
to_base() {
  bash "${here}/pc02-commit.sh" --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; echo 0 | sudo tee /sys/class/net/$i/queues/rx-*/rps_cpus >/dev/null; done'
}
trap 'echo "== 基準に戻す"; to_base; cfg "${out}/pc02-config-end.cmds"; state "${out}/pc02-state-end.txt"' EXIT

cfg "${out}/pc02-config-start.cmds"
state "${out}/pc02-state-start.txt"

for r in $(seq 1 "${rounds}"); do
  for c in "${order[@]}"; do
    d="${out}/${c}/round${r}"
    mkdir -p "${d}"
    echo "== 巡回 ${r}/${rounds}: ${c}"
    to_base
    # shellcheck disable=SC2086
    [[ -n "${files[$c]}" ]] && bash "${here}/pc02-commit.sh" ${files[$c]} >/dev/null
    cfg "${d}/pc02-config.cmds"
    state "${d}/pc02-state.txt"
    sleep 5
    for flows in single many; do
      for tol in 0 0.001; do
        python3 -I "${here}/t3_search.py" --frame 64 --flows "${flows}" --tolerance "${tol}" \
          --out "${d}/64B-${flows}-tol${tol}" | grep -E "^結果" | sed "s/^/   ${c} ${flows} tol=${tol}: /" || true
      done
    done
  done
done
echo "保存先: ${out}"
