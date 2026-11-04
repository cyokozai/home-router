#!/bin/sh
# PC03 に NGN 役を足す。PC02 の WAN（eth2）を、ONU 直結の光クロス（v6プラス）に見立てて、
# DHCPv6-PD と RA を返す。本番の設定（CLAUDE.md §6-5）を、実回線の前に containerlab で通すためのもの。
# pc03.sh の後に、pc03 コンテナ（lab/pc03-ngn.Dockerfile のイメージ）の中で実行する。2 回実行しても壊れない。
#
#   sh /lab/pc03-ngn.sh <場面> [<PC02 の eth2 のリンクローカル>]
#   sh /lab/pc03-ngn.sh stop
#
# 場面:
#   pd56  ONU 直結（光クロス・ひかり電話なし）。DHCPv6-PD で /56 を 1 つ委任する。RA は M・O フラグだけで、
#         プレフィックスは載せない（WAN の IPv6 アドレスは委任された /56 から自分で付ける）
#   pd60  HGW の配下。/56 の最初の /64 を RA し（SLAAC）、その隣の /60 を DHCPv6-PD で委任する
#   ra64  RA で /64 だけ（ひかり電話なしのネクスト）。PD は無い
#   stop  Kea と radvd を止め、場面ごとに足したアドレスと経路を消す
#
# リンクローカルを渡すと、委任したプレフィックスをそこへ向ける経路を足す（実際の NGN が委任先へ
# 経路を向けるのと同じ。Kea は経路を入れないので、ここで入れる）。
#
# プレフィックスは v6プラスの MAP ルール 240b:10::/31 に入る例（tests/test_mape_calc.py の例と同じ /56）で、
# 実在の回線のものではない。scripts/mape_calc.py がこの /56 から MAP-E の値を計算できる。
set -eu

IF=sfp0
# PC03 の WAN 側の網（pc03.sh）。Kea はこの網の上の要求に答える
WAN_SUBNET=2001:db8:20::/64
PD56=240b:10:aabb:cc00::/56
# HGW の配下（pd60）と ra64 で RA する /64（/56 の最初の /64）と、そこに置く PC03（HGW 役）のアドレス
HGW_LAN=240b:10:aabb:cc00::/64
HGW_ADDR=240b:10:aabb:cc00::1/64
# HGW が配下のルータへ委任する /60（RA する /64 と重ならないもの）
PD60=240b:10:aabb:cc10::/60

work=/tmp/ngn
kea_conf="${work}/kea-dhcp6.json"
radvd_conf="${work}/radvd.conf"
radvd_pid=/run/radvd/radvd.pid

usage() {
  echo "使い方: sh /lab/pc03-ngn.sh <pd56|pd60|ra64> [<PC02 の eth2 のリンクローカル>] / sh /lab/pc03-ngn.sh stop" >&2
  exit 2
}

stop() {
  pkill -x kea-dhcp6 2>/dev/null || true
  if [ -f "${radvd_pid}" ]; then
    kill "$(cat "${radvd_pid}")" 2>/dev/null || true
  fi
  pkill -x radvd 2>/dev/null || true
  sleep 1
  rm -f /run/kea/*.pid "${radvd_pid}"
  ip -6 addr del "${HGW_ADDR}" dev "${IF}" 2>/dev/null || true
  ip -6 route del "${PD56}" 2>/dev/null || true
  ip -6 route del "${PD60}" 2>/dev/null || true
}

# write_kea <委任するプレフィックス>   そのプレフィックスと同じ長さで 1 つだけ委任する
write_kea() {
  pd_prefix="$(echo "$1" | cut -d/ -f1)"
  pd_len="$(echo "$1" | cut -d/ -f2)"
  cat > "${kea_conf}" <<EOF
{
  "Dhcp6": {
    "interfaces-config": { "interfaces": [ "${IF}" ] },
    "lease-database": { "type": "memfile", "persist": false },
    "renew-timer": 900,
    "rebind-timer": 1800,
    "preferred-lifetime": 3600,
    "valid-lifetime": 7200,
    "subnet6": [
      {
        "id": 1,
        "subnet": "${WAN_SUBNET}",
        "interface": "${IF}",
        "pd-pools": [ { "prefix": "${pd_prefix}", "prefix-len": ${pd_len}, "delegated-len": ${pd_len} } ]
      }
    ],
    "loggers": [
      { "name": "kea-dhcp6", "output-options": [ { "output": "stdout" } ], "severity": "INFO" }
    ]
  }
}
EOF
  if ! kea-dhcp6 -t "${kea_conf}" > "${work}/kea-check.log" 2>&1; then
    cat "${work}/kea-check.log" >&2
    exit 1
  fi
}

# write_radvd <M フラグ on|off> [<RA する /64>]
write_radvd() {
  {
    echo "interface ${IF} {"
    echo "  AdvSendAdvert on;"
    echo "  MinRtrAdvInterval 3;"
    echo "  MaxRtrAdvInterval 10;"
    echo "  AdvDefaultLifetime 1800;"
    echo "  AdvManagedFlag $1;"
    echo "  AdvOtherConfigFlag on;"
    if [ -n "${2:-}" ]; then
      echo "  prefix $2 { AdvOnLink on; AdvAutonomous on; };"
    fi
    echo "};"
  } > "${radvd_conf}"
  radvd -c -C "${radvd_conf}"
}

start_kea() {
  setsid kea-dhcp6 -c "${kea_conf}" > "${work}/kea.log" 2>&1 < /dev/null &
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if grep -q DHCP6_STARTED "${work}/kea.log" 2>/dev/null; then return 0; fi
    sleep 1
  done
  echo "pc03-ngn: kea-dhcp6 が起動しない" >&2
  cat "${work}/kea.log" >&2
  exit 1
}

start_radvd() {
  radvd -C "${radvd_conf}" -p "${radvd_pid}" -m logfile -l "${work}/radvd.log"
}

[ -n "${1:-}" ] || usage
scenario="$1" pc02_ll="${2:-}"

stop
[ "${scenario}" = stop ] && exit 0

mkdir -p "${work}" /run/kea /run/radvd /var/lib/kea
sysctl -qw net.ipv6.conf."${IF}".disable_ipv6=0 2>/dev/null || true
# radvd はルータとして RA を出すので、転送を有効にしておく（無効だと radvd が警告する）
sysctl -qw net.ipv6.conf.all.forwarding=1

case "${scenario}" in
  pd56)
    delegated="${PD56}"
    write_kea "${PD56}"
    write_radvd on
    ;;
  pd60)
    delegated="${PD60}"
    ip -6 addr replace "${HGW_ADDR}" dev "${IF}" nodad
    write_kea "${PD60}"
    write_radvd off "${HGW_LAN}"
    ;;
  ra64)
    delegated=""
    ip -6 addr replace "${HGW_ADDR}" dev "${IF}" nodad
    write_radvd off "${HGW_LAN}"
    ;;
  *) usage ;;
esac

[ -n "${delegated}" ] && start_kea
start_radvd

if [ -n "${delegated}" ] && [ -n "${pc02_ll}" ]; then
  ip -6 route replace "${delegated}" via "${pc02_ll}" dev "${IF}"
fi
echo "pc03-ngn: ${scenario} を始めた（委任 ${delegated:-なし}、RA ${IF}）"
