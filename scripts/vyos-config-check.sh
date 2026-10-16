#!/usr/bin/env bash
# vyos/*.cmds が VyOS rolling で commit できるかを、使い捨ての VyOS コンテナで検査する（§8 の運用どおり）。
#
#   scripts/vyos-config-check.sh <フェーズ1の cmds> <差分の cmds>...
#
# 1. コンテナを eth0（管理系の役）・eth1（LAN 役）・eth2（WAN 役）の 3 本の veth 付きで起動する
# 2. 起動直後の設定に フェーズ1 を入れて commit し、それを基準（baseline）として保存する
# 3. 差分を 1 本ずつ「基準に戻してから」入れて commit する（差分同士は独立に検査する）
# 4. QoS の差分（qos-*.cmds）では、commit 後に eth2 の tc filter に pedit が入ったことも確かめる
# コンテナで検証できない行は vyos/container-unsupported.txt で扱う（scripts/vyos-check-cmds.sh）。
#
# 環境変数: VYOS_IMAGE（既定 home-router/vyos:rolling）、VYOS_CONTAINER（既定 vyos-config-check）、
#           VYOS_KEEP=1 で終了後もコンテナを残す（調査用）
set -euo pipefail

[[ $# -ge 1 ]] || { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 1; }
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
done

echo
if [[ ${#failed[@]} -gt 0 ]]; then
  echo "NG: ${failed[*]}" >&2
  exit 1
fi
echo "OK: フェーズ1 と差分 $# 本がすべて期待どおり"
