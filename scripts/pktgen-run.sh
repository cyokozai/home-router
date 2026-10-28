#!/usr/bin/env bash
# PC01 で pktgen を 1 回流す（scripts/t3_search.py が ssh で ~/pktgen-run.sh として呼ぶ）。root で実行する。
#
#   IFACE=sfp0 SIZE=60 DST=192.168.20.2 SRC=192.168.10.10 DMAC=<PC02 eth1 の MAC> \
#   THREADS=4 DURATION=10 RATE=<全体の pps、0 なら制限なし> BURST=1 FLOWS=single|many bash pktgen-run.sh
#
# 任意の指定:
#   DST・SRC に IPv6 のアドレスを渡すと IPv6 で送る（pktgen の dst6 / src6）
#   DST_MAX   宛先を DST〜DST_MAX の範囲で散らす（IPv4 のみ。MAP-E の試験で NAT の組を宛先ごとに分けるため）
#   SRC_MAX   送信元を SRC〜SRC_MAX の範囲でランダムに散らす（IPv4 のみ。新規接続の試験で毎回新しい流れにするため）
#   SPORT_MIN / SPORT_MAX  many のときの送信元ポートの範囲（既定 1024〜65535）
#   IMIX      "60,7 566,4 1514,1" のように pkt_size,重み を並べると IMIX で送る（SIZE は無視される）
#
# /proc/net/pktgen に直接書く（kernel の samples/pktgen にはレート指定の引数が無いため）。
# スレッド i は送信キュー i に固定し、レートは THREADS 本に等分する（ratep は 1 スレッドあたり）。
# count は 0（無制限）にして、DURATION 秒たったら stop で止める。送った数は呼び出し側が NIC のカウンタで数える。
set -euo pipefail
# 秒数は DURATION で渡す（SECONDS は bash の組み込み変数で、シェルの経過秒数に置き換わるため使わない）
: "${IFACE:?}" "${SIZE:?}" "${DST:?}" "${SRC:?}" "${DMAC:?}" "${THREADS:=4}"
run_seconds="${DURATION:?}"
rate="${RATE:-0}" burst="${BURST:-1}" flows="${FLOWS:-single}"
dst_max="${DST_MAX:-}" src_max="${SRC_MAX:-}" sport_min="${SPORT_MIN:-1024}" sport_max="${SPORT_MAX:-65535}" imix="${IMIX:-}"

modprobe pktgen
pg() { echo "$2" > "/proc/net/pktgen/$1"; }

pg pgctrl reset
for ((i = 0; i < THREADS; i++)); do
  dev="${IFACE}@${i}"
  pg "kpktgend_${i}" "rem_device_all"
  pg "kpktgend_${i}" "add_device ${dev}"
  pg "${dev}" "count 0"
  # IMIX は clone_skb（同じ skb の使い回し）と一緒に使えない（2026-10-29 実害: imix_weights の書き込みで失敗した）
  if [[ -n ${imix} ]]; then pg "${dev}" "clone_skb 0"; else pg "${dev}" "clone_skb 1000"; fi
  pg "${dev}" "pkt_size ${SIZE}"
  if [[ -n ${imix} ]]; then
    pg "${dev}" "imix_weights ${imix}"
  fi
  pg "${dev}" "burst ${burst}"
  if [[ ${DST} == *:* ]]; then
    pg "${dev}" "dst6 ${DST}"
    pg "${dev}" "src6 ${SRC}"
  else
    pg "${dev}" "dst_min ${DST}"
    pg "${dev}" "dst_max ${dst_max:-${DST}}"
    pg "${dev}" "src_min ${SRC}"
    pg "${dev}" "src_max ${src_max:-${SRC}}"
    [[ -n ${src_max} ]] && pg "${dev}" "flag IPSRC_RND"
  fi
  pg "${dev}" "dst_mac ${DMAC}"
  pg "${dev}" "queue_map_min ${i}"
  pg "${dev}" "queue_map_max ${i}"
  pg "${dev}" "udp_dst_min 9"
  pg "${dev}" "udp_dst_max 9"
  if [[ ${flows} == many ]]; then
    # 送信元ポートを散らして、PC02 の RSS で複数の受信キューに分かれるようにする
    pg "${dev}" "udp_src_min ${sport_min}"
    pg "${dev}" "udp_src_max ${sport_max}"
    pg "${dev}" "flag UDPSRC_RND"
  else
    # 1 フロー: 全スレッドで同じ 5 タプルにする（PC02 では 1 つの受信キュー、つまり 1 コアに乗る）
    pg "${dev}" "udp_src_min 9000"
    pg "${dev}" "udp_src_max 9000"
  fi
  if ((rate > 0)); then
    pg "${dev}" "ratep $((rate / THREADS))"
  else
    pg "${dev}" "delay 0"
  fi
done

echo start > /proc/net/pktgen/pgctrl &
starter=$!
sleep "${run_seconds}"
echo stop > /proc/net/pktgen/pgctrl
wait "${starter}" || true

for ((i = 0; i < THREADS; i++)); do
  echo "== ${IFACE}@${i}"
  grep -A2 "^Result" "/proc/net/pktgen/${IFACE}@${i}" || true
done
