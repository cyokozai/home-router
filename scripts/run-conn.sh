#!/usr/bin/env bash
# C1・C2: ステートフル FW の新規接続の速度と、同時接続数の上限（RFC 9411 の考え方を UDP で近似する）。
#   流れ: 送信元 192.168.10.10〜250（241 個）× 送信元ポート 1024〜65535 をランダムに選ぶ 64B の UDP。
#         ほぼすべてのパケットが新しい流れになる（PC02 の conntrack に新しい項目が作られる）。片方向 PC01 → PC03
#   C1 新規接続の速度: 試行ごとに conntrack を空にしてから 1 秒送り、ロス 0.1% 未満で送れる新しい流れのパケットの量を探す。
#         result_pps はパケットの数（送信元・ポートの組は重なりうる）。作られた接続の数は C1-ct-after.txt の件数で見る
#   C2 同時接続数: 1 秒に 20 万本ずつ 40 秒送り（返事の無い UDP は 30 秒残るので最大 600 万件を作ろうとする）、
#         5 秒ごとに conntrack の件数・メモリ・捨てた数を記録する。並行して TCP（iperf3、500 Mbps）を流し、
#         表が埋まっても確立済みの接続が影響を受けないかを見る
#   構成: s1（①、FW なし。conntrack を使わない）/ s1fw（①＋FW、表は既定の 262144）/
#         s1fwbig（①＋FW＋conntrack-large.cmds、表は 4194304）
#
#   scripts/run-conn.sh [出力先（既定: results/C-conn/<日付>）] [巡回数（既定 3）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/C-conn/$(date +%Y%m%d)}" rounds="${2:-3}"
v="${here}/../vyos"
declare -A files=(
  [s1]="${v}/tuning-1-offload.cmds"
  [s1fw]="${v}/tuning-1-offload.cmds ${v}/fw-stateful.cmds"
  [s1fwbig]="${v}/tuning-1-offload.cmds ${v}/fw-stateful.cmds ${v}/conntrack-large.cmds"
)
order=(s1 s1fw s1fwbig)
flow=(--src-max 192.168.10.250 --sport-min 1024 --sport-max 65535)
flush='sudo conntrack -F >/dev/null 2>&1; true'
mkdir -p "${out}"
scp -q "${here}/pktgen-run.sh" pc01:~/pktgen-run.sh

cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/(encrypted-password|key) '[^']*'/\1 '<伏せ字>'/" | sort > "$1"
}
to_base() {
  bash "${here}/pc02-commit.sh" --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
# conntrack の件数・上限・メモリ・捨てた数を 1 行で
ct_state() {
  ssh pc02 'n=$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || echo 0); m=$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo 0)
            s=$(sudo conntrack -S 2>/dev/null | tr " " "\n" | awk -F= "/^(drop|early_drop|insert_failed)=/{a[\$1]+=\$2} END{printf \"drop=%d early_drop=%d insert_failed=%d\", a[\"drop\"], a[\"early_drop\"], a[\"insert_failed\"]}")
            slab=$(sudo awk "/^nf_conntrack /{print \$2*\$4}" /proc/slabinfo 2>/dev/null)
            avail=$(awk "/MemAvailable/{print \$2}" /proc/meminfo)
            echo "count=${n} max=${m} slab_bytes=${slab:-0} mem_available_kb=${avail} ${s}"'
}
trap 'echo "== 基準に戻す"; to_base; ssh pc03 "pkill -x iperf3" || true; cfg "${out}/pc02-config-end.cmds"' EXIT

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
    ct_state > "${d}/ct-start.txt"

    # C1: 新規接続の速度
    if [[ ! -f ${d}/C1-new-flows/result.json ]]; then
      python3 -I "${here}/t3_search.py" --frame 64 --flows many "${flow[@]}" --tolerance 0.001 \
        --trial-seconds 1 --final-seconds 1 --pc02-before "${flush}" \
        --out "${d}/C1-new-flows" | grep -E "^結果" | sed "s/^/   ${c} C1: /" || true
      ct_state > "${d}/C1-ct-after.txt"
    fi

    # C2: 同時接続数（s1 は conntrack を使わないので測らない）
    # 完了の印は C2-fill/result.json（送り終えた後に書かれる）。途中で止まった巡回の記録は消してから流し直す
    if [[ ${c} != s1 && ! -f ${d}/C2-fill/result.json ]]; then
      rm -f "${d}/C2-fill.log"
      ssh pc02 "${flush}"
      ssh pc03 'pkill -x iperf3; iperf3 -s -D -B 192.168.20.2; sleep 1'
      ssh pc01 'iperf3 -c 192.168.20.2 -b 500M -t 60 -J' > "${d}/C2-tcp.json" 2>&1 &
      tcp=$!
      sleep 5
      python3 -I "${here}/t3_search.py" --frame 64 --flows many "${flow[@]}" --fixed-pps 200000 \
        --final-seconds 40 --repeat 1 --out "${d}/C2-fill" > "${d}/C2-fill.out" 2>&1 &
      gen=$!
      for t in $(seq 0 5 50); do
        echo "t=${t} $(ct_state)" >> "${d}/C2-fill.log"
        sleep 5
      done
      wait "${gen}" || true
      wait "${tcp}" || true
      echo "   ${c} C2: $(tail -2 "${d}/C2-fill.log" | head -1)"
      echo "   ${c} C2 TCP: $(jq -r '[.intervals[].sum.bits_per_second] | "最小 \(min/1e6|floor) / 中央 \(sort|.[length/2|floor]/1e6|floor) Mbps"' "${d}/C2-tcp.json" 2>/dev/null || echo 取得失敗)"
    fi
  done
done
echo "保存先: ${out}"
