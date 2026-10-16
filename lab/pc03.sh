#!/bin/sh
# PC03（受信側の端末）の 10G 側を設定する。CLAUDE.md §3 の表と §5-3 の注記のとおり。
# containerlab の pc03 コンテナの中で実行する（exec か docker exec）。2 回実行しても壊れない。
# フェーズ2 の BR 役は pc03-br.sh に分けてある。
set -eu

sysctl -qw net.ipv6.conf.sfp0.disable_ipv6=0 2>/dev/null || true

ip link set sfp0 up mtu 1500
ip addr replace 192.168.20.2/30 dev sfp0
ip -6 addr replace 2001:db8:20::2/64 dev sfp0 nodad

# PC01 の LAN への戻り経路。PC02 の eth2（§6-1）へ
ip route replace 192.168.10.0/24 via 192.168.20.1
ip -6 route replace 2001:db8:10::/64 via 2001:db8:20::1
