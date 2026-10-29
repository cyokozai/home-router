#!/usr/bin/env bash
# 2026-10-29 の追加試験（scripts/run-extra.sh）で測りきれなかった E4 と E3 を、送り方を直して流し直す。
#   E4 過負荷からの戻り（RFC 2544 26.5）: 1 回の pktgen で、上限を超える量（20 Mpps を要求。送れるのは両向き約 5.7 Mpps）を
#      20 秒 → 止めずに 50%（両向き 2.8 Mpps）を 20 秒。3 巡。PC02 の 1 秒ごとの転送量を PC03 の Prometheus から取る
#      前回は 2 つの送信のあいだに約 25 秒の切れ目（カウンタの読み取り）があり、戻るまでの時間が測れなかった。
#      いまは pktgen-run.sh の RATE2 で、動いている pktgen の ratep を書き換えて切り替える（切れ目は 0.1 秒未満）
#   E3 バースト（RFC 2544 26.4 の back-to-back の近似）: 送れるだけの速さ（両向き約 5.8 Mpps、PC02 の上限 5.6 を少し超える）で、
#      0.01〜3 秒ぶんの数を送り、落ちた数を数える。リング 512 と 4096（vyos/tuning-1-offload.cmds）で比べる。各 3 回
#      前回は時間で止めたので、0.01 秒では stop が start より先に届いて止まらなかった。いまは pktgen の count で止める
#
#   scripts/run-e3-e4.sh [出力先（既定: results/E-extra/<日付>-e3e4）] [締め切り "YYYY-MM-DD HH:MM"（既定: 無し）] [待つ PID]
#
# 途中で止めて流し直すと、結果がそろっている分（E4 は巡ごと、E3 は数ごと）は飛ばす。
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "${here}/.." || exit 1
out="${1:-results/E-extra/$(date +%Y%m%d)-e3e4}" deadline="${2:-}" wait_pid="${3:-}"
if [[ -n ${wait_pid} ]]; then
  echo "== $(date '+%H:%M') PID ${wait_pid} の終わりを待つ"
  while kill -0 "${wait_pid}" 2>/dev/null; do sleep 30; done
fi
v=vyos
if [[ -n ${deadline} ]]; then end=$(date -j -f "%Y-%m-%d %H:%M" "${deadline}" +%s) || exit 1; else end=$((1 << 62)); fi
# ログの置き場所を先に作る（2026-10-29 実害: 作っていなかったので、リダイレクトで失敗して 1 本も流れなかった）
mkdir -p "${out}/E4" "${out}/E3/ring512" "${out}/E3/ring4096"
for h in pc01 pc03; do scp -q scripts/pktgen-run.sh "${h}:~/pktgen-run.sh" || exit 1; done

in_time() { (($(date +%s) < end)) || { echo "== $(date '+%H:%M') 締め切り（${deadline}）を過ぎたので $1 を飛ばす"; return 1; }; }
# 鍵・パスワードと SNMP の community（vyos/snmp-mgmt.cmds、秘密）を伏せてから残す
cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/ (encrypted-password|key|community) ('[^']*'|[^ ']+)/ \1 '<伏せ字>'/g" | sort > "$1"
}
to_base() {
  bash scripts/pc02-commit.sh --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
apply() { to_base; bash scripts/pc02-commit.sh "$@" >/dev/null; sleep 5; }
t3() { python3 -I scripts/t3_search.py "$@"; }
# 開始〜終了の時刻（UNIX 秒）の 1 秒ごとの値を、PC03 の Prometheus から取る
prom() {
  # shellcheck disable=SC2029  # 式は手元で printf %q で包み、PC03 のシェルでほどく
  ssh pc03 "curl -s --get localhost:9090/api/v1/query_range --data-urlencode $(printf %q "query=$1") --data-urlencode start=$2 --data-urlencode end=$3 --data-urlencode step=1"
}
# PC02 の送信（eth1＋eth2）。前回の E4 と同じ式
prom_tx() { prom 'sum(irate(node_network_transmit_packets_total{host="pc02",device=~"eth1|eth2"}[3s]))' "$1" "$2"; }
# 送信側（PC01・PC03 の sfp0）。切り替えで送信が途切れていないか、遅れの取り戻しで全速が続いていないかを見る
prom_sender() { prom 'sum by (host) (irate(node_network_transmit_packets_total{host=~"pc01|pc03",device="sfp0"}[3s]))' "$1" "$2"; }
# 結果がそろっているか（E3 は 3 回、E4 は 1 回）
done_runs() { [[ -s $1/result.json ]] && (($(grep -c '"at":' "$1/result.json") >= $2)); }
trap 'echo "== 基準に戻す"; to_base; cfg "${out}/pc02-config-end.cmds"' EXIT

# ---- E4 過負荷からの戻り ----
if in_time E4; then
  echo "== $(date '+%H:%M') E4 開始"
  applied=
  for r in 1 2 3; do
    d="${out}/E4/round${r}"
    if done_runs "${d}/run" 1 && [[ -s ${d}/pc02-tx-per-second.json ]]; then echo "   E4 round${r} は結果があるので飛ばす"; continue; fi
    in_time "E4 round${r}" || break
    if [[ -z ${applied} ]]; then
      apply "${v}/tuning-1-offload.cmds" "${v}/test-sink.cmds"
      cfg "${out}/E4-pc02-config.cmds"
      applied=1
    fi
    mkdir -p "${d}"
    t0=$(date +%s)
    t3 --frame 64 --flows many --bidir --fixed-pps 20000000 --rate2 2800000 --switch-after 20 --final-seconds 40 \
      --out "${d}/run" > "${d}/run.log" 2>&1
    t1=$(date +%s)
    sleep 3  # Prometheus が最後の秒を取り込むのを待つ
    prom_tx "${t0}" "${t1}" > "${d}/pc02-tx-per-second.json"
    prom_sender "${t0}" "${t1}" > "${d}/senders-tx-per-second.json"
    # 切り替えの時刻は試行の JSON（pktgen の出力）に「== 切り替え <UNIX 秒>」として残る
    echo "   E4 round${r}: $(grep -h '^結果' "${d}/run.log")"
  done
  echo "== $(date '+%H:%M') E4 終了"
fi

# ---- E3 バースト ----
# 0.01〜3 秒ぶんの数（両向きの合計 5.8 Mpps × 秒）。送れるだけの速さで送るため、要求レートは前回と同じ 20 Mpps にする
bursts=("0.01 58000" "0.03 174000" "0.1 580000" "0.3 1740000" "1 5800000" "3 17400000")
if in_time E3; then
  echo "== $(date '+%H:%M') E3 開始"
  for c in ring512 ring4096; do
    applied=
    for b in "${bursts[@]}"; do
      read -r dur count <<< "${b}"
      d="${out}/E3/${c}/burst-${dur}s"
      if done_runs "${d}" 3; then echo "   E3 ${c} ${dur}s は結果があるので飛ばす"; continue; fi
      in_time "E3 ${c} ${dur}s" || break 2
      if [[ -z ${applied} ]]; then
        if [[ ${c} == ring512 ]]; then apply "${v}/test-sink.cmds"; else apply "${v}/tuning-1-offload.cmds" "${v}/test-sink.cmds"; fi
        cfg "${out}/E3-${c}-pc02-config.cmds"
        applied=1
      fi
      t3 --frame 64 --flows many --bidir --fixed-pps 20000000 --count "${count}" --final-seconds "${dur}" --repeat 3 \
        --out "${d}" > "${out}/E3/${c}-${dur}.log" 2>&1
      echo "   E3 ${c} ${dur}s（${count} 個）: $(grep '^結果' "${out}/E3/${c}-${dur}.log")"
    done
  done
  echo "== $(date '+%H:%M') E3 終了"
fi
echo "== $(date '+%H:%M') すべて終了（保存先: ${out}）"
