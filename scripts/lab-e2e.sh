#!/usr/bin/env bash
# containerlab で起動した検証環境（lab/home-router.clab.yml）で、PC02（VyOS）を通した疎通・MAP-E・DSCP の
# 書き換えを確かめる。トポロジーを deploy した後に、ホストで実行する（docker exec で各ノードを操作する）。
#
#   scripts/lab-e2e.sh
#
# 流れ（CLAUDE.md §8 の運用どおり、基準＝フェーズ1 に戻してから差分を入れる）
#   0. 準備: veth の IPv6 を有効にし、端末の設定（lab/pc01.sh・pc03.sh）を流し直す
#   1. フェーズ1: lab/pc02-clab-defaults.cmds と vyos/phase1.cmds を入れ、基準として保存する
#   2. QoS: 入れる前に DSCP 0x88 がそのまま届くこと（対照）を見てから vyos/qos-ngn-out.cmds を入れる
#   3. フェーズ2: 基準に戻して vyos/phase2-mape.cmds を入れ、PC03 に BR 役（lab/pc03-br.sh）を足す（§8 T7）
# 判定は scripts/lab-judge.py。項目ごとに OK / NG を出し、最後に一覧を出す。NG が 1 つでもあれば 1 で終わる。
#
# 環境変数: LAB_PREFIX（既定 clab-home-router）、GITHUB_STEP_SUMMARY（あれば一覧を書き足す）
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/.." && pwd)"
prefix="${LAB_PREFIX:-clab-home-router}"
pc01="${prefix}-pc01" pc02="${prefix}-pc02" pc03="${prefix}-pc03"
baseline=/config/lab-e2e-baseline.config

# MAP-E（§6-3）の値
ce6=2001:db8:20::c br6=2001:db8:20::b ce4=192.0.2.18 ports=8160-8175 inet=198.51.100.1

summary=()
failures=0

# ---- 記録 -----------------------------------------------------------------------

# record <ID> <OK|NG> <項目> <詳細>
record() {
  local id="$1" verdict="$2" item="$3" detail="$4"
  summary+=("${verdict}	${id}	${item}	${detail}")
  if [[ "${verdict}" == OK ]]; then
    echo "[OK] ${id} ${item}: ${detail}"
  else
    failures=$((failures + 1))
    echo "[NG] ${id} ${item}: ${detail}"
    echo "::error title=lab-e2e ${id}::${item}: ${detail}"
  fi
}

# check <ID> <項目> <ログ> <lab-judge.py の引数...>   判定器が OK を返せば OK
# expect_ng <ID> <項目> <ログ> <引数...>              対照。判定器が NG を返せば OK（壊れた状態を NG にできることの確認）
judge_log() {
  local log="$1"; shift
  printf '%s\n' "${log}" | python3 "${here}/lab-judge.py" "$@" || true
}
check() {
  local id="$1" item="$2" log="$3"; shift 3
  local out
  out="$(judge_log "${log}" "$@")"
  if [[ "${out}" == OK* ]]; then
    record "${id}" OK "${item}" "${out#OK }"
  else
    record "${id}" NG "${item}" "${out#NG }"
    show_log "${id}" "${log}"
  fi
}
expect_ng() {
  local id="$1" item="$2" log="$3"; shift 3
  local out
  out="$(judge_log "${log}" "$@")"
  if [[ "${out}" == NG* ]]; then
    record "${id}" OK "${item}" "期待どおり NG: ${out#NG }"
  else
    record "${id}" NG "${item}" "NG になるはずが: ${out}"
    show_log "${id}" "${log}"
  fi
}
# expect_rc <ID> <項目> <期待する終了コード 0|非0> <コマンド...>
expect_rc() {
  local id="$1" item="$2" want="$3"; shift 3
  local rc=0 out
  out="$("$@" 2>&1)" || rc=$?
  if [[ "${want}" == 0 && "${rc}" -eq 0 ]] || [[ "${want}" != 0 && "${rc}" -ne 0 ]]; then
    record "${id}" OK "${item}" "終了コード ${rc}"
  else
    record "${id}" NG "${item}" "終了コード ${rc}（期待 ${want}）"
    show_log "${id}" "${out}"
  fi
}
show_log() {
  echo "----- ${1} のログ -----"
  printf '%s\n' "${2}"
  echo "-----"
}
section() {
  echo
  echo "== $*"
}
print_summary() {
  section "結果の一覧"
  printf '%s\n' "${summary[@]}" | column -t -s '	'
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
      echo "### lab-e2e"
      echo
      echo "| 結果 | ID | 項目 | 詳細 |"
      echo "|---|---|---|---|"
      printf '%s\n' "${summary[@]}" | sed -e 's/|/\\|/g' -e 's/	/ | /g' -e 's/^/| /' -e 's/$/ |/'
    } >> "${GITHUB_STEP_SUMMARY}"
  fi
  echo
  if [[ "${failures}" -gt 0 ]]; then
    echo "lab-e2e: NG が ${failures} 件" >&2
  else
    echo "lab-e2e: ${#summary[@]} 項目すべて OK"
  fi
}
# 投入に失敗したら、その先は検査しない
abort() {
  record "$1" NG "$2" "$3"
  print_summary
  exit 1
}

# ---- 操作 -----------------------------------------------------------------------

x() { docker exec "$@"; }
enable_ipv6() {
  # shellcheck disable=SC2016
  x "$1" sh -c 'for f in /proc/sys/net/ipv6/conf/*/disable_ipv6; do echo 0 > "$f"; done'
}
# run_log <ノード> <コマンド...>   出力（標準エラーも）を返す。終了コードは見ない
run_log() { x "$@" 2>&1 || true; }
# 最初の数発は近隣探索（ND / ARP）で落ちることがあるので、通るまで待つ
warm_up() {
  local node="$1" target="$2"
  for _ in $(seq 1 15); do
    if x "${node}" ping -c 1 -W 1 "${target}" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "warm_up: ${node} から ${target} に 15 秒届かなかった（検査は続ける）"
}
# cap_start <ノード> <IF> <名前> <フィルタ>   /tmp/<名前>.pcap へ取り始める
cap_start() {
  local node="$1" ifname="$2" name="$3"; shift 3
  x "${node}" rm -f "/tmp/${name}.pcap"
  x -d "${node}" tcpdump -i "${ifname}" -nn -U -w "/tmp/${name}.pcap" "$@"
  # 取り始めるまでの間に流したパケットを取りこぼさないよう待つ
  sleep 1
  x "${node}" pgrep -f "/tmp/${name}.pcap" >/dev/null || echo "cap_start: ${node} の tcpdump（${name}）が動いていない"
}
# cap_stop <ノード> <名前>   止めて tcpdump -nn -v の文字列で返す
cap_stop() {
  local node="$1" name="$2"
  sleep 1
  x "${node}" pkill -INT -f "/tmp/${name}.pcap" || true
  sleep 1
  x "${node}" tcpdump -r "/tmp/${name}.pcap" -nn -v 2>/dev/null || true
}
# send_udp6 <Traffic Class> <送信元ポート> [回数]   PC01 から PC03 へ IPv6 UDP を送る（§6-4 の nc -T 0x88 と同じ）
send_udp6() {
  x -i "${pc01}" python3 - "$1" "$2" "${3:-3}" <<'PY'
import socket
import sys
import time

tclass, sport, count = int(sys.argv[1], 16), int(sys.argv[2]), int(sys.argv[3])
s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_TCLASS, tclass)
s.bind(("2001:db8:10::10", sport))
for _ in range(count):
    s.sendto(b"lab-e2e", ("2001:db8:20::2", 52528))
    time.sleep(0.1)
PY
}
apply() { "${here}/vyos-apply.sh" "$@"; }

# ---- 0. 準備 --------------------------------------------------------------------

section "0. 準備"
"${here}/vyos-wait-ready.sh" "${pc02}"
for node in "${pc01}" "${pc02}" "${pc03}"; do
  enable_ipv6 "${node}"
done
x "${pc01}" sh /lab/pc01.sh
x "${pc03}" sh /lab/pc03.sh
x "${pc02}" ip -br link

# ---- 1. フェーズ1 ---------------------------------------------------------------

section "1. フェーズ1（${root}/vyos/phase1.cmds）"
listen="$(run_log "${pc02}" ss -ltnH)"
echo "${listen}"
expect_ng P1-0 "対照: containerlab の既定のままでは SSH が管理系以外でも待ち受ける" "${listen}" listen-only 22 172.16.0.12

if ! cat "${root}/lab/pc02-clab-defaults.cmds" "${root}/vyos/phase1.cmds" | apply --save "${baseline}" "${pc02}" -; then
  abort P1-apply "フェーズ1 の投入" "vyos-apply.sh が失敗した"
fi
x "${pc02}" ip -br addr

warm_up "${pc01}" 192.168.20.2
warm_up "${pc01}" 2001:db8:20::2
check P1-1 "pc01 → pc03 IPv4 ping" "$(run_log "${pc01}" ping -c 3 -W 2 192.168.20.2)" ping-ok
check P1-2 "pc01 → pc03 IPv6 ping" "$(run_log "${pc01}" ping -6 -c 3 -W 2 2001:db8:20::2)" ping-ok
check P1-3 "traceroute が pc02（192.168.10.1）を経由する" \
  "$(run_log "${pc01}" traceroute -n -w 1 -q 1 192.168.20.2)" traceroute-via 192.168.10.1 192.168.20.2
check P1-4 "ping -M do -s 1472 が通る（MTU 1500）" "$(run_log "${pc01}" ping -M "do" -s 1472 -c 2 -W 2 192.168.20.2)" ping-ok
check P1-4c "対照: ping -M do -s 1473 は通らない" "$(run_log "${pc01}" ping -M "do" -s 1473 -c 2 -W 2 192.168.20.2)" ping-fails

listen="$(run_log "${pc02}" ss -ltnH)"
echo "${listen}"
check P1-5 "pc02 の SSH は 172.16.0.12 でだけ待ち受ける" "${listen}" listen-only 22 172.16.0.12
check P1-6 "pc02 の node-exporter は 172.16.0.12 でだけ待ち受ける" "${listen}" listen-only 9100 172.16.0.12
expect_rc P1-7 "pc01 → 172.16.0.12:22（管理系）に届く" 0 x "${pc01}" nc -z -w 3 172.16.0.12 22
expect_rc P1-8 "pc01 → 192.168.10.1:22（10G 側）には届かない" 1 x "${pc01}" nc -z -w 3 192.168.10.1 22
expect_rc P1-9 "pc01 → 172.16.0.12:9100（管理系）に届く" 0 x "${pc01}" nc -z -w 3 172.16.0.12 9100
expect_rc P1-10 "pc01 → 192.168.10.1:9100（10G 側）には届かない" 1 x "${pc01}" nc -z -w 3 192.168.10.1 9100

# ---- 2. QoS（§6-4、NGN-OUT の set-dscp） ------------------------------------------

section "2. QoS（${root}/vyos/qos-ngn-out.cmds）"
qos_round() {
  cap_start "${pc01}" sfp0 "$1-lan" ip6 and udp
  cap_start "${pc03}" sfp0 "$1-wan" ip6 and udp
  send_udp6 0x88 51820
  send_udp6 0x88 51821
  lan="$(cap_stop "${pc01}" "$1-lan")"
  wan="$(cap_stop "${pc03}" "$1-wan")"
  echo "${wan}"
}
qos_round qos0
check Q-0 "前提: pc01 は Traffic Class 0x88 で送っている" "${lan}" tclass 0x88 51820
check Q-0c "対照: QoS を入れる前は 0x88 のまま pc03 に届く" "${wan}" tclass 0x88 51820

if ! apply "${pc02}" "${root}/vyos/qos-ngn-out.cmds"; then
  abort Q-apply "QoS の投入" "vyos-apply.sh が失敗した"
fi
run_log "${pc02}" tc filter show dev eth2

qos_round qos1
check Q-1 "前提: pc01 は Traffic Class 0x88 で送っている" "${lan}" tclass 0x88 51820
check Q-2 "送信元ポート 51820 の UDP は pc03 で class 0x00 になる" "${wan}" tclass 0x00 51820
check Q-3 "対照: 送信元ポート 51821 は書き換えない（0x88 のまま）" "${wan}" tclass 0x88 51821

# ---- 3. フェーズ2（§6-3 MAP-E、§8 T7） --------------------------------------------

section "3. フェーズ2（${root}/vyos/phase2-mape.cmds と lab/pc03-br.sh）"
if ! apply --load "${baseline}" "${pc02}" "${root}/vyos/phase2-mape.cmds"; then
  abort P2-apply "フェーズ2 の投入" "vyos-apply.sh が失敗した"
fi
x "${pc03}" sh /lab/pc03-br.sh
x "${pc02}" ip -d link show tun0 || true
x "${pc03}" ip -d link show br0 || true
x -d "${pc03}" iperf3 -s -B "${inet}"

cap_start "${pc01}" sfp0 p2-lan tcp
cap_start "${pc03}" sfp0 p2-wan ip6
warm_up "${pc01}" "${inet}"
ping_out="$(run_log "${pc01}" ping -c 3 -W 2 "${inet}")"
iperf_out="$(run_log "${pc01}" iperf3 -c "${inet}" -t 2 -P 4)"
pmtu_ok="$(run_log "${pc01}" ping -M "do" -s 1432 -c 2 -W 2 "${inet}")"
pmtu_ng="$(run_log "${pc01}" ping -M "do" -s 1472 -c 2 -W 2 "${inet}")"
lan="$(cap_stop "${pc01}" p2-lan)"
wan="$(cap_stop "${pc03}" p2-wan)"
echo "${iperf_out}" | tail -n 4

check P2-1 "pc01 → ${inet} ping" "${ping_out}" ping-ok
check P2-2 "pc03 の sfp0 で外側 IPv6 の Next Header が 4（IPIP）" "${wan}" outer-nh 4 "${ce6}" "${br6}"
check P2-3 "内側の送信元が ${ce4}、送信元ポートが ${ports}" "${wan}" inner-src "${ce6}" "${ce4}" "${ports}"
check P2-3c "対照: pc02 の手前（pc01 の sfp0）では送信元ポートが ${ports} の外" "${lan}" src-port-outside 192.168.10.10 "${ports}"
check P2-4 "トンネルに出た SYN の MSS が 1420 以下" "${wan}" mss-max 1420 "${ce4}"
expect_ng P2-4c "対照: pc02 の手前の SYN は MSS 1420 を超える" "${lan}" mss-max 1420 192.168.10.10
check P2-5 "ping -M do -s 1432 が通る" "${pmtu_ok}" ping-ok
check P2-6 "ping -M do -s 1472 は Frag needed（mtu = 1460）が返る" "${pmtu_ng}" ping-frag-needed 1460

print_summary
[[ "${failures}" -eq 0 ]]
