#!/bin/sh
# PC03 に MAP-E の BR 役を足す（フェーズ2 用）。CLAUDE.md §5-3「PC03だけ: BR役」と同じ内容で、
# 対向は §6-3 の CE 側 tun0（source 2001:db8:20::c、remote 2001:db8:20::b、MTU 1460）。
# pc03.sh の後に、pc03 コンテナの中で実行する。2 回実行しても壊れない。
# ipip6 にはホストのカーネルの ip6_tunnel モジュールが要る。
#
# §5-3 との違い: encaplimit none を付ける。Linux の ip6 トンネルは既定で Tunnel Encapsulation Limit
# （Destination Options ヘッダ、8 バイト）を付けるため、外側の Next Header が 4 ではなく 60 になり、
# トンネルの実効 MTU も 1452 に下がる。実際の BR（RFC 7597 の MAP-E）は素の IPv4-in-IPv6 を返すので、それに揃える。
set -eu

ip -6 addr replace 2001:db8:20::b/64 dev sfp0 nodad
ip link show br0 >/dev/null 2>&1 || ip -6 tunnel add br0 mode ipip6 local 2001:db8:20::b remote 2001:db8:20::c encaplimit none
ip link set br0 up mtu 1460
ip addr replace 192.0.2.1/32 dev br0
ip route replace 192.0.2.18/32 dev br0
# インターネット役。iperf3 -s -B 198.51.100.1
ip addr replace 198.51.100.1/32 dev lo
