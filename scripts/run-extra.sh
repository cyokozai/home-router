#!/usr/bin/env bash
# 追加の性能試験（E2〜E5）。締め切りの時刻を過ぎたら、新しい試験を始めない（2026-10-29 のユーザーの都合: 8 時に家を出る）。
#   E4 過負荷からの戻り: 上限を超える量を 20 秒 → 50% を 20 秒。PC02 の 1 秒ごとの転送量を PC03 の Prometheus から取る
#      弱点: 2 つの送信のあいだに数秒の切れ目がある（カウンタの読み取り）。切れ目の後の最初の秒から落ちずに転送できるかで見る
#   E3 バースト（RUN_E3=1 のときだけ流す）: 送れるだけの速さ（両向き約 5.8 Mpps、PC02 の上限 5.6 を少し超える）で 0.01〜3 秒送り、落ちた数を数える。
#      リング 512 と 4096 で、上限を超えた分をどれだけ吸収できるかを比べる（RFC 2544 の back-to-back の近似。
#      送信側が 64B のワイヤーレートを出せないため）
#   E5 MAP-E の新規接続の上限: 送信元・ポート・宛先を散らした新しい流れを、決まったレートで 40 秒送る。
#      NAT の組は 240 ポート × 256 宛先 = 61,440、返事の無い UDP は 30 秒残るので、約 2,000 本/秒が理論上の上限
#   E2 ロス率の曲線（1 巡）: 512B は理論値の 10〜100%、64B は 1.0〜5.5 Mpps と送れるだけ、を 30 秒ずつ
#
#   [RUN_E3=1] scripts/run-extra.sh [出力先（既定: results/E-extra/<日付>）] [締め切り "YYYY-MM-DD HH:MM"（省くと締め切りなし）] [待つ PID]
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "${here}/.." || exit 1
out="${1:-results/E-extra/$(date +%Y%m%d)}" deadline="${2:-}" wait_pid="${3:-}"
if [[ -n ${wait_pid} ]]; then
  echo "== $(date '+%H:%M') PID ${wait_pid} の終わりを待つ"
  while kill -0 "${wait_pid}" 2>/dev/null; do sleep 30; done
fi
v=vyos
# 締め切りを省いたときは締め切りなし（以前の既定は 2026-10-29 07:15 の固定で、その日を過ぎると何も流さずに 0 で終わった）
if [[ -n ${deadline} ]]; then end=$(date -j -f "%Y-%m-%d %H:%M" "${deadline}" +%s); else end=$((1 << 62)); fi
mkdir -p "${out}"
for h in pc01 pc03; do scp -q scripts/pktgen-run.sh "${h}:~/pktgen-run.sh"; done

in_time() { (($(date +%s) < end)) || { echo "== $(date '+%H:%M') 締め切り（${deadline}）を過ぎたので $1 を飛ばす"; return 1; }; }
cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/(encrypted-password|key) '[^']*'/\1 '<伏せ字>'/" | sort > "$1"
}
to_base() {
  bash scripts/pc02-commit.sh --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
# 設定の投入に失敗したら、違う構成のまま測らないよう止める（EXIT の trap が基準に戻す）
apply() {
  to_base
  bash scripts/pc02-commit.sh "$@" >/dev/null || { echo "== $(date '+%H:%M') 設定の投入に失敗した: $*"; exit 1; }
  sleep 5
}
t3() { python3 -I scripts/t3_search.py "$@"; }
br_up() {
  scp -q lab/pc03-br.sh pc03:/tmp/pc03-br.sh
  ssh pc03 'sudo ip link del br0 2>/dev/null; sudo ip addr del 198.51.100.1/32 dev lo 2>/dev/null; sudo ip -6 addr del 2001:db8:20::b/64 dev sfp0 2>/dev/null; sudo sh /tmp/pc03-br.sh >/dev/null'
}
br_down() {
  ssh pc03 'sudo ip link del br0 2>/dev/null; sudo ip addr del 198.51.100.1/32 dev lo 2>/dev/null; sudo ip -6 addr del 2001:db8:20::b/64 dev sfp0 2>/dev/null; true'
}
# PC02 の 1 秒ごとの送信量（eth1＋eth2）を、開始〜終了の時刻（UNIX 秒）で PC03 の Prometheus から取る
prom_tx() {
  ssh pc03 "curl -s --get localhost:9090/api/v1/query_range --data-urlencode 'query=sum(irate(node_network_transmit_packets_total{host=\"pc02\",device=~\"eth1|eth2\"}[3s]))' --data-urlencode start=$1 --data-urlencode end=$2 --data-urlencode step=1"
}
trap 'echo "== 基準に戻す"; to_base; br_down; cfg "${out}/pc02-config-end.cmds"' EXIT

# ---- E4 過負荷からの戻り ----
# ログの置き場所を先に作る（2026-10-29 実害: 作っていなかったので E3・E5・E2 はリダイレクトで失敗し、1 本も流れなかった）
mkdir -p "${out}/E3/ring512" "${out}/E3/ring4096" "${out}/E5" "${out}/E2"
# 完了の印は result.json（t3_search.py は試行の前にディレクトリを作り、result.json は最後に書く）
if [[ -f ${out}/E4/round3/half/result.json ]]; then echo "== E4 は結果があるので飛ばす"; elif in_time E4; then
  echo "== $(date '+%H:%M') E4 開始"
  apply "${v}/tuning-1-offload.cmds" "${v}/test-sink.cmds"
  cfg "${out}/E4-pc02-config.cmds"
  for r in 1 2 3; do
    d="${out}/E4/round${r}"; mkdir -p "${d}"
    t0=$(date +%s)
    t3 --frame 64 --flows many --bidir --fixed-pps 20000000 --final-seconds 20 --out "${d}/overload" > "${d}/overload.log" 2>&1
    t3 --frame 64 --flows many --bidir --fixed-pps 2800000 --final-seconds 20 --out "${d}/half" > "${d}/half.log" 2>&1
    t1=$(date +%s)
    sleep 3
    prom_tx "${t0}" "${t1}" > "${d}/pc02-tx-per-second.json"
    echo "   E4 round${r}: $(grep -h '^結果' "${d}/overload.log" "${d}/half.log" | tr '\n' ' ')"
  done
  echo "== $(date '+%H:%M') E4 終了"
fi

# ---- E3 バースト ----
# 0.01 秒のような短い送信では、pktgen の stop が start より先に届き、送信が止まらなくなる（2026-10-29 実害）。
# 回数（count）で止める形に直すまでは、既定では流さない。RUN_E3=1 のときだけ流す
if [[ ${RUN_E3:-0} != 1 ]]; then echo "== E3 を飛ばす（流すときは RUN_E3=1）"; elif in_time E3; then
  echo "== $(date '+%H:%M') E3 開始"
  for c in ring512 ring4096; do
    if [[ ${c} == ring512 ]]; then apply "${v}/test-sink.cmds"; else apply "${v}/tuning-1-offload.cmds" "${v}/test-sink.cmds"; fi
    cfg "${out}/E3-${c}-pc02-config.cmds"
    for dur in 0.01 0.03 0.1 0.3 1 3; do
      in_time "E3 ${c} ${dur}s" || break
      t3 --frame 64 --flows many --bidir --fixed-pps 20000000 --final-seconds "${dur}" --repeat 3 \
        --out "${out}/E3/${c}/burst-${dur}s" > "${out}/E3/${c}-${dur}.log" 2>&1
      echo "   E3 ${c} ${dur}s: $(grep '^結果' "${out}/E3/${c}-${dur}.log")"
    done
  done
  echo "== $(date '+%H:%M') E3 終了"
fi

# ---- E5 MAP-E の新規接続の上限 ----
if in_time E5; then
  echo "== $(date '+%H:%M') E5 開始"
  br_up
  apply "${v}/tuning-1-offload.cmds" "${v}/mape-portsets.cmds"
  cfg "${out}/E5-pc02-config.cmds"
  for rate in 500 1000 2000 5000 20000; do
    in_time "E5 ${rate}" || break
    d="${out}/E5/rate-${rate}"
    t3 --frame 64 --flows many --dst 198.51.100.1 --dst-max 198.51.100.255 --src-max 192.168.10.250 \
      --sport-min 1024 --sport-max 65535 --fixed-pps "${rate}" --final-seconds 40 \
      --pc02-before 'sudo conntrack -F >/dev/null 2>&1; true' --out "${d}" > "${d}.log" 2>&1
    ssh pc02 'echo "count=$(cat /proc/sys/net/netfilter/nf_conntrack_count) $(sudo conntrack -S | tr " " "\n" | awk -F= "/^(drop|insert_failed)=/{a[\$1]+=\$2} END{printf \"drop=%d insert_failed=%d\", a[\"drop\"], a[\"insert_failed\"]}")"' > "${d}-ct.txt" 2>&1 || true
    echo "   E5 ${rate}/s: $(grep '^結果' "${d}.log") / $(cat "${d}-ct.txt")"
  done
  br_down
  echo "== $(date '+%H:%M') E5 終了"
fi

# ---- E2 ロス率の曲線（1 巡） ----
if in_time E2; then
  echo "== $(date '+%H:%M') E2 開始"
  apply "${v}/tuning-1-offload.cmds" "${v}/test-sink.cmds"
  cfg "${out}/E2-pc02-config.cmds"
  wire512=4699248   # 512B の理論値（両向きの合計）
  for pct in 10 20 30 40 50 60 70 80 90 100; do
    in_time "E2 512B ${pct}%" || break
    rate=$((wire512 * pct / 100))
    t3 --frame 512 --flows many --bidir --fixed-pps "${rate}" --final-seconds 30 --out "${out}/E2/512B-${pct}pct" > "${out}/E2/512B-${pct}pct.log" 2>&1
    echo "   E2 512B ${pct}%: $(grep '^結果' "${out}/E2/512B-${pct}pct.log")"
  done
  for rate in 1000000 2000000 3000000 4000000 4500000 5000000 5500000 20000000; do
    in_time "E2 64B ${rate}" || break
    t3 --frame 64 --flows many --bidir --fixed-pps "${rate}" --final-seconds 30 --out "${out}/E2/64B-${rate}" > "${out}/E2/64B-${rate}.log" 2>&1
    echo "   E2 64B ${rate}: $(grep '^結果' "${out}/E2/64B-${rate}.log")"
  done
  echo "== $(date '+%H:%M') E2 終了"
fi
echo "== $(date '+%H:%M') すべて終了（保存先: ${out}）"
