#!/bin/sh
# PC01（送信側の端末）の 10G 側を設定する。CLAUDE.md §5-3 の netplan と同じ内容。
# containerlab の pc01 コンテナの中で実行する（exec か docker exec）。2 回実行しても壊れない。
set -eu

# コンテナでは IPv6 が無効になっていることがある。書けない環境（非特権）では無視する
sysctl -qw net.ipv6.conf.sfp0.disable_ipv6=0 2>/dev/null || true

ip link set sfp0 up mtu 1500
ip addr replace 192.168.10.10/24 dev sfp0
ip -6 addr replace 2001:db8:10::10/64 dev sfp0 nodad

# 経路はすべて PC02 の eth1（§6-1）へ
ip route replace 192.168.20.0/30 via 192.168.10.1
ip route replace 192.0.2.0/24 via 192.168.10.1
ip route replace 198.51.100.0/24 via 192.168.10.1
ip -6 route replace 2001:db8:20::/64 via 2001:db8:10::1
