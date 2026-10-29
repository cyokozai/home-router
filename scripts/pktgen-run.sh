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
#   COUNT     送る数（全スレッドの合計）。指定すると DURATION ではなく数で止める（E3 のバースト。下の注記）
#   COUNT_TIMEOUT  COUNT のときの安全弁の秒数（既定 60）。この秒数で送り終わらなければ止める
#   RATE2 / SWITCH_AFTER  SWITCH_AFTER 秒たったら、止めずに全体のレートを RATE2 に変える（E4 の過負荷からの戻り。下の注記）。
#             DURATION は全体の秒数。SWITCH_AFTER と DURATION は整数で、DURATION > SWITCH_AFTER
#   PKTGEN_PROC  pktgen の /proc の場所（既定 /proc/net/pktgen。検査で偽のディレクトリに替えるため）
#
# /proc/net/pktgen に直接書く（kernel の samples/pktgen にはレート指定の引数が無いため）。
# スレッド i は送信キュー i に固定し、レートは THREADS 本に等分する（ratep は 1 スレッドあたり）。
# count は 0（無制限）にして、DURATION 秒たったら stop で止める。送った数は呼び出し側が NIC のカウンタで数える。
#
# COUNT（数で止める）: 0.01 秒のような短い送信を時間で止めると、stop が start より先に届いて止まらなくなる
#   （2026-10-29 実害）。count を各スレッドに配ると、start の書き込みは全スレッドが送り終えるまで返らない
#   （net/core/pktgen.c の pktgen_run_all_threads → pktgen_wait_all_threads_run）ので、sleep と stop の競り合いが無い。
#   止まらないときのために timeout で包む。書き込み中の start が signal を受けると、kernel が全スレッドを止める。
# RATE2（途中でレートを変える）: 動いている device の ratep を書き換えると、次のパケットから間隔が変わる
#   （pktgen_if_write は running を見ずに delay を書き換え、spin() が毎回 next_tx = 前の next_tx + delay とする）。
#   ただし前半で送信側が要求に追いつけないと next_tx が現在より遅れたままになり、そのまま ratep を下げると、
#   遅れを取り戻すまで全速で送り続ける（20 秒の過負荷のあとなら十数秒）。そこで先に delay を 1ms にして、
#   遅れを 1 パケットあたり 1ms ずつ取り戻させ（20 秒の遅れでも 2 万パケット、数十 ms）、0.1 秒おいてから ratep を書く。
#   取り戻したあと ratep を書くまでの残り（0.1 秒より短い）は 1 スレッド 1,000 pps に落ちる。切れ目は 0.1 秒未満で、
#   PC02 の 1 秒ごとの転送量（Prometheus）では切り替えの 1 秒が 1 割ほど少なく見えるだけ。
set -euo pipefail
# 秒数は DURATION で渡す（SECONDS は bash の組み込み変数で、シェルの経過秒数に置き換わるため使わない）
: "${IFACE:?}" "${SIZE:?}" "${DST:?}" "${SRC:?}" "${DMAC:?}" "${THREADS:=4}"
run_seconds="${DURATION:?}"
rate="${RATE:-0}" burst="${BURST:-1}" flows="${FLOWS:-single}"
dst_max="${DST_MAX:-}" src_max="${SRC_MAX:-}" sport_min="${SPORT_MIN:-1024}" sport_max="${SPORT_MAX:-65535}" imix="${IMIX:-}"
count="${COUNT:-}" count_timeout="${COUNT_TIMEOUT:-60}" rate2="${RATE2:-}" switch_after="${SWITCH_AFTER:-}"
proc="${PKTGEN_PROC:-/proc/net/pktgen}"

# 始める前に断る（count 0 は pktgen では「無制限」、ratep 0 は黙って無視される）
die() { echo "pktgen-run.sh: $*" >&2; exit 2; }
if [[ -n ${count} ]]; then
  if ! [[ ${count} =~ ^[0-9]+$ ]] || ((count < THREADS)); then die "COUNT（${count}）は THREADS（${THREADS}）以上の整数にする"; fi
  [[ -z ${rate2} ]] || die "COUNT と RATE2 は一緒に使えない"
fi
if [[ -n ${rate2} ]]; then
  if ! [[ ${rate2} =~ ^[0-9]+$ ]] || ((rate2 < THREADS)); then die "RATE2（${rate2}）は THREADS 以上の整数にする"; fi
  [[ ${switch_after} =~ ^[0-9]+$ && ${run_seconds} =~ ^[0-9]+$ ]] || die "RATE2 のとき SWITCH_AFTER と DURATION は整数の秒"
  ((run_seconds > switch_after)) || die "DURATION（${run_seconds}）は SWITCH_AFTER（${switch_after}）より長くする"
fi

modprobe pktgen
pg() { echo "$2" > "${proc}/$1"; }

pg pgctrl reset
for ((i = 0; i < THREADS; i++)); do
  dev="${IFACE}@${i}"
  pg "kpktgend_${i}" "rem_device_all"
  pg "kpktgend_${i}" "add_device ${dev}"
  if [[ -n ${count} ]]; then
    # 合計が COUNT になるよう、割り切れない分は前のスレッドから 1 つずつ足す
    pg "${dev}" "count $((count / THREADS + (i < count % THREADS ? 1 : 0)))"
  else
    pg "${dev}" "count 0"
  fi
  # clone_skb 1000 は同じ skb を 1000 回送るので、送信元・ポートのランダム化も 1000 パケットに 1 回しか変わらない。
  # 新規接続の試験（SRC_MAX）では毎回新しい流れにするため 0 にする（2026-10-29 実害: 20 万本/秒のつもりが conntrack は 6303 件）。
  # IMIX も clone_skb と一緒に使えない（imix_weights の書き込みで失敗した）
  if [[ -n ${imix} || -n ${src_max} ]]; then pg "${dev}" "clone_skb 0"; else pg "${dev}" "clone_skb 1000"; fi
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

if [[ -n ${count} ]]; then
  # 送り終えるまで start が返らない。安全弁で切れたら（終了コード 124）そのまま止めに進む
  # shellcheck disable=SC2016  # $1 は bash -c の中で展開する
  timeout "${count_timeout}" bash -c 'echo start > "$1"' _ "${proc}/pgctrl" || echo "== ${count_timeout} 秒で送り終わらなかったので止める" >&2
  echo stop > "${proc}/pgctrl"
elif [[ -n ${rate2} ]]; then
  echo start > "${proc}/pgctrl" &
  starter=$!
  sleep "${switch_after}"
  for ((i = 0; i < THREADS; i++)); do pg "${IFACE}@${i}" "delay 1000000"; done
  sleep 0.1
  for ((i = 0; i < THREADS; i++)); do pg "${IFACE}@${i}" "ratep $((rate2 / THREADS))"; done
  # 切り替えた時刻（UNIX 秒）。Prometheus の 1 秒ごとの値と突き合わせるため
  echo "== 切り替え $(date +%s.%N)"
  sleep "$((run_seconds - switch_after))"
  echo stop > "${proc}/pgctrl"
  wait "${starter}" || true
else
  echo start > "${proc}/pgctrl" &
  starter=$!
  sleep "${run_seconds}"
  echo stop > "${proc}/pgctrl"
  wait "${starter}" || true
fi

for ((i = 0; i < THREADS; i++)); do
  echo "== ${IFACE}@${i}"
  grep -A2 "^Result" "${proc}/${IFACE}@${i}" || true
done
