#!/usr/bin/env bash
# PC01 で、リンクが落ちても送り続ける（R1 再起動の試験用。scripts/run-reboot.sh が ssh で呼ぶ）。root で実行する。
#
#   KEEP_SECONDS=420 IFACE=sfp0 ...（pktgen-run.sh と同じ変数）bash pktgen-keepalive.sh
#
# pktgen は送信先のリンクが落ちると、そこで送信を止めて終わる（2026-10-30 実害: PC02 の再起動で 34 秒目に止まり、
# 再起動の後の転送の再開を測れなかった）。ここでは sfp0 の carrier を 0.2 秒おきに見て、0 から 1 に戻ったら、
# 残りの秒数で pktgen-run.sh を流し直す。リンクが上がっているあいだは 1 本の pktgen が続けて送るので、
# 受信側の 1 秒ごとの数に切れ目が入らない。
#
# 検査のために替えられる場所（既定は実機のもの）:
#   PKTGEN_PROC   pktgen の /proc の場所（既定 /proc/net/pktgen。pktgen-run.sh と同じ変数）
#   CARRIER_FILE  リンクの状態を読むファイル（既定 /sys/class/net/${IFACE}/carrier）
#   PKTGEN_RUN    流す pktgen-run.sh（既定 このスクリプトと同じ場所のもの）
set -uo pipefail
: "${KEEP_SECONDS:?}" "${IFACE:?}"
here="$(cd "$(dirname "$0")" && pwd)"
proc="${PKTGEN_PROC:-/proc/net/pktgen}"
carrier_file="${CARRIER_FILE:-/sys/class/net/${IFACE}/carrier}"
pktgen_run="${PKTGEN_RUN:-${here}/pktgen-run.sh}"
end=$(($(date +%s) + KEEP_SECONDS))
carrier() { cat "${carrier_file}" 2>/dev/null || echo 0; }
pg_pid=""
start_pg() {
  local rem=$((end - $(date +%s)))
  ((rem > 0)) || return 0
  echo "== 送信を始める $(date +%s.%N)（残り ${rem} 秒）"
  DURATION=${rem} bash "${pktgen_run}" &
  pg_pid=$!
}
stop_pg() {
  echo stop > "${proc}/pgctrl" 2>/dev/null || true
  if [[ -n ${pg_pid} ]]; then kill "${pg_pid}" 2>/dev/null; wait "${pg_pid}" 2>/dev/null; fi
  pg_pid=""
}
trap 'stop_pg' EXIT

start_pg
prev=$(carrier)
while (($(date +%s) < end)); do
  c=$(carrier)
  if [[ ${prev} == 1 && ${c} == 0 ]]; then
    echo "== リンクが落ちた $(date +%s.%N)"
  elif [[ ${prev} == 0 && ${c} == 1 ]]; then
    echo "== リンクが戻った $(date +%s.%N)"
    stop_pg
    start_pg
  fi
  prev=${c}
  sleep 0.2
done
stop_pg
trap - EXIT
