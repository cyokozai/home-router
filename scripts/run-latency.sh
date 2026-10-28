#!/usr/bin/env bash
# L1・L2: 遅延と、負荷をかけたときの遅延（RFC 2544 26.2 の考え方を ping で近似する）。
#   背景の負荷: pktgen の 64B 多数フローを双方向に一定のレートで流す（0・1・2・3 Mpps と、上限を超える量）
#   遅延: その最中に PC03 → PC01（192.168.10.10）へ ICMP を 2 ms おきに 1 万回送り、往復時間の分布を取る
#   構成: base（リング 512。① なし）/ s1（①、リング 4096）/ prod（①＋FW＋flowtable＋firewall の set dscp）
#     リングを 512 → 4096 にしたことで、混んだときの待ち（バッファの膨らみ）が増えていないかを見る
#   pktgen は 3 スレッドにして、PC01 の 1 コアを ping の応答に残す
#   弱点: 往復時間には PC01・PC03 の処理の時間も入る（専用の試験機ではない）。値は負荷ごと・構成ごとの差で読む
#
#   scripts/run-latency.sh [出力先（既定: results/L-latency/<日付>）] [巡回数（既定 3）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/L-latency/$(date +%Y%m%d)}" rounds="${2:-3}"
v="${here}/../vyos"
declare -A files=(
  [base]="${v}/test-sink.cmds"
  [s1]="${v}/tuning-1-offload.cmds ${v}/test-sink.cmds"
  [prod]="${v}/tuning-1-offload.cmds ${v}/fw-stateful.cmds ${v}/tuning-3-flowtable.cmds ${v}/dscp-firewall.cmds ${v}/test-sink.cmds ${v}/test-sink-fw.cmds"
)
order=(base s1 prod)
loads=(0 1000000 2000000 3000000 20000000)   # 20 Mpps は上限を超える量（送信側の上限まで送る）
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
trap 'echo "== 基準に戻す"; to_base; cfg "${out}/pc02-config-end.cmds"' EXIT

for r in $(seq 1 "${rounds}"); do
  for c in "${order[@]}"; do
    d="${out}/${c}/round${r}"
    mkdir -p "${d}"
    echo "== 巡回 ${r}/${rounds}: ${c}"
    to_base
    # shellcheck disable=SC2086
    bash "${here}/pc02-commit.sh" ${files[$c]} >/dev/null
    cfg "${d}/pc02-config.cmds"
    ssh pc02 'for i in eth1 eth2; do sudo ethtool -g $i | sed -n "/Current/,+1p" | tail -1; done' > "${d}/ring.txt"
    sleep 5
    for load in "${loads[@]}"; do
      [[ -f ${d}/ping-${load}.txt ]] && { echo "   ${c} ${load}: 済み（飛ばす）"; continue; }
      gen=""
      if ((load > 0)); then
        python3 -I "${here}/t3_search.py" --frame 64 --flows many --bidir --threads 3 --fixed-pps "${load}" \
          --final-seconds 35 --repeat 1 --out "${d}/load-${load}" > "${d}/load-${load}.log" 2>&1 &
        gen=$!
        sleep 8   # 背景の負荷が流れ始めるのを待つ（カウンタの読み取りに数秒かかる）
      fi
      ssh pc03 'sudo ping -i 0.002 -c 10000 -W 1 192.168.10.10' > "${d}/ping-${load}.txt" 2>&1 || true
      [[ -n ${gen} ]] && { wait "${gen}" || true; }
      echo "   ${c} ${load}: $(tail -1 "${d}/ping-${load}.txt") / $(grep -m1 -o 'packet loss.*\|[0-9.]*% packet loss' "${d}/ping-${load}.txt" | head -1)"
      sleep 3
    done
  done
done
echo "保存先: ${out}"
