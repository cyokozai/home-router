#!/usr/bin/env bash
# vyos/*.cmds が VyOS rolling で commit できるかを、使い捨ての VyOS コンテナで検査する（§8 の運用どおり）。
#
#   scripts/vyos-config-check.sh <フェーズ1の cmds> <差分の cmds>...
#
# 1. コンテナを eth0（管理系の役）・eth1（LAN 役）・eth2（WAN 役）の 3 本の veth 付きで起動する
# 2. 起動直後の設定に フェーズ1 を入れて commit し、それを基準（baseline）として保存する
# 3. 差分を 1 本ずつ「基準に戻してから」入れて commit する（差分同士は独立に検査する）
# 4. QoS の差分（qos-*.cmds）では、commit 後に eth2 の tc filter に pedit が入ったことも確かめる
#    SNMP の差分（snmp-*.cmds）では snmpd.conf の community を、検証環境から本番へ移る差分（prod-from-lab*.cmds）では
#    VRF MGMT・検証用のアドレスと経路が消え、SSH が既定の VRF で 192.168.2.1 を待ち受けることを確かめる
# コンテナで検証できない行は vyos/container-unsupported.txt で扱う（scripts/vyos-check-cmds.sh）。
#
# 環境変数: VYOS_IMAGE（既定 home-router/vyos:rolling）、VYOS_CONTAINER（既定 vyos-config-check）、
#           VYOS_KEEP=1 で終了後もコンテナを残す（調査用）
set -euo pipefail

[[ $# -ge 1 ]] || { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 1; }
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/.." && pwd)"
image="${VYOS_IMAGE:-home-router/vyos:rolling}"
c="${VYOS_CONTAINER:-vyos-config-check}"
unsupported="${root}/vyos/container-unsupported.txt"
initial=/config/vyos-check-initial.config
baseline=/config/vyos-check-baseline.config
phase1="$1"; shift

cleanup() {
  if [[ "${VYOS_KEEP:-0}" != 1 ]]; then
    docker rm -f "${c}" >/dev/null 2>&1 || true
    docker network rm "${c}-lan" "${c}-wan" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
cleanup

echo "== コンテナを起動する: ${image}"
docker network create --internal "${c}-lan" >/dev/null
docker network create --internal "${c}-wan" >/dev/null
docker create --name "${c}" --privileged -v /lib/modules:/lib/modules "${image}" >/dev/null
docker network connect "${c}-lan" "${c}"
docker network connect "${c}-wan" "${c}"
docker start "${c}" >/dev/null
"${here}/vyos-wait-ready.sh" "${c}"
# Docker は IPv6 の無いネットワークにつないだ veth で IPv6 を無効にする（disable_ipv6=1）。
# このままだと VyOS がリンクローカルや 2001:db8:: を付けられず commit が落ちるので、有効に戻す
# shellcheck disable=SC2016
docker exec "${c}" sh -c 'for f in /proc/sys/net/ipv6/conf/*/disable_ipv6; do echo 0 > "$f"; done'
docker exec "${c}" ip -br link

echo "== 起動直後の設定を保存する"
"${here}/vyos-apply.sh" --save "${initial}" "${c}" - < /dev/null

# 検査器の自己検査: 失敗を失敗として返せるか（set / commit は失敗しても 0 で返ることがあるため）
expect_rc() {
  local want="$1" text="$2" got=0
  printf '%s\n' "${text}" | "${here}/vyos-apply.sh" --load "${initial}" "${c}" - || got=$?
  if [[ "${got}" -ne "${want}" ]]; then
    echo "NG 自己検査: 「${text}」の終了コードが ${got}（期待 ${want}）" >&2
    exit 1
  fi
  echo "OK 自己検査: 「${text}」は ${want} で返る"
}
echo "== vyos-apply.sh の自己検査"
expect_rc 2 "set system option no-such-node 'x'"
# 値の制約で落ちる set も拒否として返すこと。CLAUDE.md §6-4 の set-dscp 'CS0' はこれに当たる
# （vyos/qos-ngn-out.cmds では 'default' に直してある。tests/test_vyos_cmds.py の DOC_CORRECTIONS）
expect_rc 2 "set qos policy shaper SELF-TEST class 10 set-dscp 'CS0'"
expect_rc 3 "set firewall ipv4 forward filter rule 999 action 'offload'"
expect_rc 0 "set system host-name 'vyos-self-test'"
"${here}/vyos-apply.sh" --load "${initial}" "${c}" - < /dev/null

failed=()
echo "== フェーズ1: ${phase1}"
if ! "${here}/vyos-check-cmds.sh" "${c}" "${initial}" "${phase1}" "${unsupported}"; then
  echo "NG: フェーズ1が commit できないので、差分は検査しない" >&2
  exit 1
fi
"${here}/vyos-apply.sh" --save "${baseline}" "${c}" - < /dev/null

for delta in "$@"; do
  echo
  echo "== 差分: ${delta}（基準に戻してから入れる）"
  if ! "${here}/vyos-check-cmds.sh" "${c}" "${baseline}" "${delta}" "${unsupported}"; then
    failed+=("${delta}")
    continue
  fi
  if [[ "$(basename "${delta}")" == qos-*.cmds ]]; then
    # shaper の filter は root qdisc（HTB）に付く。egress（clsact）を指定すると何も出ないので見ない
    echo "-- tc filter（eth2 の root qdisc）"
    filters="$(docker exec "${c}" tc filter show dev eth2 2>&1 || true)"
    echo "${filters}"
    # コメント行の set-dscp は数えない
    if grep -qE '^[[:space:]]*set .* set-dscp ' "${delta}" && ! grep -q 'pedit' <<< "${filters}"; then
      echo "NG ${delta}: set-dscp を入れたのに eth2 の tc filter に pedit が無い" >&2
      failed+=("${delta}")
    else
      echo "OK ${delta}: eth2 の tc filter に pedit がある"
    fi
  fi
  if [[ "$(basename "${delta}")" == snmp-*.cmds ]]; then
    # community の network の既定は 0.0.0.0/0 ::/0。/32 を明示して既定が消えたこと（誰からでも読めないこと）を確かめる
    echo "-- snmpd.conf の community の行"
    conf="$(docker exec "${c}" sh -c 'grep -E "^r[ow]community6? " /etc/snmp/snmpd.conf' 2>&1 || true)"
    echo "${conf}"
    # 全行が「読み取り専用で 192.168.2.50/32 だけ」であること。別の network の行が 1 行でも増えたら落とす
    # （1 行あるかだけを見ると、192.168.2.0/24 などを足しても通ってしまう）
    total="$(grep -c . <<< "${conf}" || true)"
    allowed="$(grep -cE '^rocommunity [^ ]+ 192\.168\.2\.50/32( |$)' <<< "${conf}" || true)"
    if [[ ${total} -ge 1 && ${allowed} -eq ${total} ]]; then
      echo "OK ${delta}: community は 192.168.2.50/32 だけに許されている"
    else
      echo "NG ${delta}: snmpd.conf の community が 192.168.2.50/32 だけになっていない" >&2
      failed+=("${delta}")
    fi
  fi
  if [[ "$(basename "${delta}")" == prod-from-lab*.cmds ]]; then
    # 検証環境の状態（実機の PC02 と同じ形の基準）から、移行の段と本番の部分を 1 回で commit したあと、
    # VRF MGMT・検証用のアドレスと経路・前の NTP サーバが残っておらず、SSH が既定の VRF で本番のアドレスを待ち受けること
    absent=(
      "vrf name MGMT"
      "interfaces ethernet eth0 vrf"
      "service ssh vrf"
      "service ntp vrf"
      "service monitoring prometheus node-exporter vrf"
      "interfaces ethernet eth0 address dhcp"
      "interfaces ethernet eth1 address 192.168.10.1/24"
      "interfaces ethernet eth1 address 2001:db8:10::1/64"
      "interfaces ethernet eth2 address 192.168.20.1/30"
      "interfaces ethernet eth2 address 2001:db8:20::1/64"
      "protocols static route 0.0.0.0/0 next-hop 192.168.20.2"
      "protocols static route6 ::/0 next-hop 2001:db8:20::2"
      "service ntp server 192.168.2.13"
    )
    # 問い合わせが壊れて何でも「無い」と答える場合に備えて、あるはずの所も問う
    present=(
      "service ssh listen-address 192.168.2.1"
      "interfaces ethernet eth0 address 192.168.255.1/24"
    )
    if [[ "$(basename "${delta}")" == *mape*.cmds ]]; then
      present+=("protocols static route 0.0.0.0/0 interface tun0")
    else
      absent+=("protocols static route 0.0.0.0/0" "protocols static route6 ::/0")
    fi
    # shellcheck disable=SC2016
    {
      echo 'source /opt/vyatta/etc/functions/script-template'
      echo 'configure'
      for p in "${absent[@]}"; do echo "if \$API exists ${p}; then echo 'LEFT ${p}'; fi"; done
      for p in "${present[@]}"; do echo "if ! \$API exists ${p}; then echo 'MISSING ${p}'; fi"; done
      echo 'echo QUERY_DONE'
      echo 'exit'
    } | docker exec -i "${c}" sh -c 'cat > /tmp/vyos-from-lab-check.sh'
    answer="$(docker exec "${c}" sg vyattacfg -c '/bin/vbash /tmp/vyos-from-lab-check.sh' 2>&1 || true)"
    vrfs="$(docker exec "${c}" ip -br link show type vrf 2>&1 || true)"
    ssh22="$(docker exec "${c}" ss -Hltn 'sport = :22' 2>&1 || true)"
    echo "-- 設定の問い合わせ"; echo "${answer}"
    echo "-- VRF のデバイス"; echo "${vrfs:-（無し）}"
    echo "-- 22 番で待ち受けている所"; echo "${ssh22}"
    ok=1
    if ! grep -q '^QUERY_DONE' <<< "${answer}" || grep -qE '^(LEFT|MISSING) ' <<< "${answer}"; then ok=0; fi
    if [[ -n "${vrfs}" ]]; then ok=0; fi
    if ! grep -qE '(^|[[:space:]])192\.168\.2\.1:22([[:space:]]|$)' <<< "${ssh22}"; then ok=0; fi
    if ((ok)); then
      echo "OK ${delta}: 検証環境の名残が無く、SSH は既定の VRF で 192.168.2.1 を待ち受けている"
    else
      echo "NG ${delta}: 検証環境の名残がある、または SSH が 192.168.2.1 で待ち受けていない" >&2
      failed+=("${delta}")
    fi
  fi
done

echo
if [[ ${#failed[@]} -gt 0 ]]; then
  echo "NG: ${failed[*]}" >&2
  exit 1
fi
echo "OK: フェーズ1 と差分 $# 本がすべて期待どおり"
