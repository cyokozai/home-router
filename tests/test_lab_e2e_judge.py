"""scripts/lab-judge.py（containerlab の E2E 検査で、取ったログを判定する部分）の検査。

判定器は標準入力に ping / traceroute / ss / tcpdump -nn -v の出力を受け取り、
`OK ...` か `NG ...` を 1 行出して終了コード 0 / 1 で返す。
ここでは「通るべきものが通る」に加えて「壊れたものを NG にする」ことを確かめる。
サンプルは netshoot:v0.14（iputils 20250605、busybox traceroute、tcpdump）の実出力から取った。
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JUDGE = ROOT / "scripts" / "lab-judge.py"


def judge(*args: str, stdin: str) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, str(JUDGE), *args], input=stdin, capture_output=True, text=True, check=False
    )
    return p.returncode, p.stdout + p.stderr


def ok(*args, stdin):
    rc, out = judge(*args, stdin=stdin)
    assert rc == 0, out
    assert out.startswith("OK"), out
    return out


def ng(*args, stdin):
    rc, out = judge(*args, stdin=stdin)
    assert rc == 1, out
    assert out.startswith("NG"), out
    return out


# ---- ping ------------------------------------------------------------------

PING_OK = """\
PING 198.51.100.1 (198.51.100.1) 56(84) bytes of data.
64 bytes from 198.51.100.1: icmp_seq=1 ttl=64 time=0.849 ms
64 bytes from 198.51.100.1: icmp_seq=2 ttl=64 time=0.186 ms

--- 198.51.100.1 ping statistics ---
2 packets transmitted, 2 received, 0% packet loss, time 1017ms
rtt min/avg/max/mdev = 0.186/0.517/0.849/0.331 ms
"""

PING_PARTIAL = """\
--- 198.51.100.1 ping statistics ---
3 packets transmitted, 2 received, 33.3333% packet loss, time 2003ms
"""

PING_LOCAL_TOO_BIG = """\
PING 198.51.100.1 (198.51.100.1) 1472(1500) bytes of data.
ping: sendmsg: Message too large
ping: sendmsg: Message too large

--- 198.51.100.1 ping statistics ---
2 packets transmitted, 0 received, +2 errors, 100% packet loss, time 1015ms
"""

# ルータ（PC02）が DF 付きの大きいパケットを落として ICMP を返した場合。2 発目以降はカーネルが覚えた PMTU で手元で止まる
PING_FRAG_NEEDED = """\
PING 198.51.100.1 (198.51.100.1) 1472(1500) bytes of data.
From 192.168.10.1 icmp_seq=1 Frag needed and DF set (mtu = 1460)
ping: local error: message too long, mtu=1460

--- 198.51.100.1 ping statistics ---
2 packets transmitted, 0 received, +2 errors, 100% packet loss, time 1001ms
"""

PING_LOST = """\
--- 198.51.100.1 ping statistics ---
2 packets transmitted, 0 received, 100% packet loss, time 1001ms
"""


def test_ping_ok_passes_when_every_echo_is_answered():
    ok("ping-ok", stdin=PING_OK)


@pytest.mark.parametrize("text", [PING_PARTIAL, PING_LOCAL_TOO_BIG, PING_FRAG_NEEDED, PING_LOST, ""])
def test_ping_ok_fails_on_loss_errors_or_no_summary(text):
    ng("ping-ok", stdin=text)


def test_frag_needed_passes_when_the_router_reports_the_tunnel_mtu():
    out = ok("ping-frag-needed", "1460", stdin=PING_FRAG_NEEDED)
    assert "192.168.10.1" in out


def test_frag_needed_fails_when_the_ping_got_through():
    ng("ping-frag-needed", "1460", stdin=PING_OK)


def test_frag_needed_fails_on_a_different_mtu():
    ng("ping-frag-needed", "1452", stdin=PING_FRAG_NEEDED)


def test_frag_needed_fails_when_the_sender_stopped_it_locally():
    # 手元の IF の MTU で止まっただけ（ルータは何も返していない）は Frag needed ではない
    ng("ping-frag-needed", "1460", stdin=PING_LOCAL_TOO_BIG)


def test_ping_fails_passes_only_when_nothing_came_back():
    ok("ping-fails", stdin=PING_LOCAL_TOO_BIG)
    ng("ping-fails", stdin=PING_OK)
    ng("ping-fails", stdin=PING_PARTIAL)


# ---- traceroute ------------------------------------------------------------

TRACE_VIA_PC02 = """\
traceroute to 192.168.20.2 (192.168.20.2), 30 hops max, 46 byte packets
 1  192.168.10.1  0.031 ms
 2  192.168.20.2  0.022 ms
"""

TRACE_DIRECT = """\
traceroute to 198.51.100.1 (198.51.100.1), 30 hops max, 46 byte packets
 1  198.51.100.1  0.005 ms
"""

TRACE_LOST = """\
traceroute to 192.168.20.2 (192.168.20.2), 30 hops max, 46 byte packets
 1  192.168.10.1  0.031 ms
 2  *
 3  *
"""


def test_traceroute_via_passes_when_the_first_hop_is_pc02_and_the_last_is_the_target():
    ok("traceroute-via", "192.168.10.1", "192.168.20.2", stdin=TRACE_VIA_PC02)


def test_traceroute_via_fails_when_pc02_is_not_on_the_path():
    ng("traceroute-via", "192.168.10.1", "198.51.100.1", stdin=TRACE_DIRECT)


def test_traceroute_via_fails_when_the_target_is_not_reached():
    ng("traceroute-via", "192.168.10.1", "192.168.20.2", stdin=TRACE_LOST)


# ---- ip -4 route show default（既定経路） ---------------------------------------

# フェーズ1 の static（FRR が入れる）
ROUTE_STATIC = "default nhid 20 via 192.168.20.2 dev eth2 proto static metric 20\n"
# Docker が管理系に入れる既定経路。カーネルの経路なので FRR の static より優先され、static が入らない
ROUTE_DOCKER = "default via 172.16.0.1 dev eth0 \n"
ROUTE_TUN0 = "default nhid 31 dev tun0 proto static scope link metric 20\n"


def test_route_default_passes_on_the_expected_next_hop():
    ok("route-default", "192.168.20.2", stdin=ROUTE_STATIC)


def test_route_default_accepts_an_interface_route():
    ok("route-default", "dev:tun0", stdin=ROUTE_TUN0)


def test_route_default_fails_on_the_docker_mgmt_gateway():
    out = ng("route-default", "192.168.20.2", stdin=ROUTE_DOCKER)
    assert "172.16.0.1" in out
    ng("route-default", "dev:tun0", stdin=ROUTE_DOCKER)


def test_route_default_fails_when_there_are_two_defaults_or_none():
    ng("route-default", "192.168.20.2", stdin=ROUTE_DOCKER + ROUTE_STATIC)
    ng("route-default", "192.168.20.2", stdin="")


# ---- ss（待ち受け） ----------------------------------------------------------

SS_MGMT_ONLY = """\
LISTEN 0      128      172.16.0.12:22         0.0.0.0:*
LISTEN 0      4096     172.16.0.12:9100       0.0.0.0:*
LISTEN 0      4096       127.0.0.1:8080       0.0.0.0:*
"""

# containerlab の既定の config.boot（ssh listen-address 0.0.0.0）にフェーズ1を足しただけの状態
SS_ALL_ADDRESSES = """\
LISTEN 0      128          0.0.0.0:22         0.0.0.0:*
LISTEN 0      128      172.16.0.12:22         0.0.0.0:*
LISTEN 0      128             [::]:22            [::]:*
LISTEN 0      4096     172.16.0.12:9100       0.0.0.0:*
"""

SS_WILDCARD_V6 = """\
LISTEN 0      4096               *:9100             *:*
"""


def test_listen_only_passes_when_the_port_is_bound_to_the_mgmt_address_only():
    ok("listen-only", "22", "172.16.0.12", stdin=SS_MGMT_ONLY)
    ok("listen-only", "9100", "172.16.0.12", stdin=SS_MGMT_ONLY)


def test_listen_only_fails_on_wildcard_listeners():
    out = ng("listen-only", "22", "172.16.0.12", stdin=SS_ALL_ADDRESSES)
    assert "0.0.0.0" in out
    ng("listen-only", "9100", "172.16.0.12", stdin=SS_WILDCARD_V6)


def test_listen_only_fails_when_nothing_listens():
    ng("listen-only", "9100", "172.16.0.12", stdin="LISTEN 0 128 172.16.0.12:22 0.0.0.0:*\n")


def test_listen_only_does_not_confuse_port_prefixes():
    # :2222 は :22 ではない
    ng("listen-only", "22", "172.16.0.12", stdin="LISTEN 0 128 0.0.0.0:2222 0.0.0.0:*\n")


# ---- tcpdump -nn -v ----------------------------------------------------------

# PC03 の sfp0 で見た MAP-E の外側（ipip6）。Linux の ip6 トンネルの既定（encaplimit 4）では
# 外側の Next Header が 60（Destination Options）になり、その中に IPv4 が入る
DUMP_ENCAPLIMIT = """\
19:34:07.272387 IP6 (flowlabel 0xfc4e9, hlim 64, next-header unknown (60) payload length: 92) 2001:db8:20::c > 2001:db8:20::b: DSTOPT (opt_type 0x04: len=1)(padn) IP (tos 0x0, ttl 64, id 9111, offset 0, flags [DF], proto ICMP (1), length 84)
    192.0.2.18 > 198.51.100.1: ICMP echo request, id 25785, seq 1, length 64
19:34:07.272579 IP6 (flowlabel 0xfc4e9, hlim 64, next-header unknown (60) payload length: 92) 2001:db8:20::b > 2001:db8:20::c: DSTOPT (opt_type 0x04: len=1)(padn) IP (tos 0x0, ttl 64, id 39531, offset 0, flags [none], proto ICMP (1), length 84)
    198.51.100.1 > 192.0.2.18: ICMP echo reply, id 25785, seq 1, length 64
19:34:08.351501 IP6 (flowlabel 0xe7283, hlim 64, next-header unknown (60) payload length: 68) 2001:db8:20::c > 2001:db8:20::b: DSTOPT (opt_type 0x04: len=1)(padn) IP (tos 0x0, ttl 64, id 53277, offset 0, flags [DF], proto TCP (6), length 60)
    192.0.2.18.45126 > 198.51.100.1.5201: Flags [S], cksum 0xec75 (incorrect -> 0xe764), seq 371555772, win 65320, options [mss 1420,sackOK,TS val 2089197648 ecr 0,nop,wscale 10], length 0
"""

# encaplimit none のトンネル（外側の Next Header が 4）。PC02 が NAT と MSS の調整をした後
DUMP_IPIP = """\
10:00:00.000001 IP6 (hlim 255, next-header ICMPv6 (58) payload length: 32) 2001:db8:20::c > ff02::1:ff00:b: [icmp6 sum ok] ICMP6, neighbor solicitation, length 32, who has 2001:db8:20::b
	  source link-address option (1), length 8 (1): 1e:dc:f5:dd:21:a8
10:00:00.000002 IP6 (flowlabel 0xe7283, hlim 64, next-header IPIP (4) payload length: 60) 2001:db8:20::c > 2001:db8:20::b: IP (tos 0x0, ttl 63, id 53277, offset 0, flags [DF], proto TCP (6), length 60)
    192.0.2.18.8160 > 198.51.100.1.5201: Flags [S], cksum 0xec75 (incorrect -> 0xe764), seq 371555772, win 64240, options [mss 1420,sackOK,TS val 2089197648 ecr 0,nop,wscale 7], length 0
10:00:00.000003 IP6 (flowlabel 0x4031a, hlim 64, next-header IPIP (4) payload length: 60) 2001:db8:20::b > 2001:db8:20::c: IP (tos 0x0, ttl 64, id 0, offset 0, flags [DF], proto TCP (6), length 60)
    198.51.100.1.5201 > 192.0.2.18.8160: Flags [S.], cksum 0xec75 (incorrect -> 0xa3cf), seq 856776962, ack 371555773, win 64768, options [mss 1460,sackOK,TS val 725190239 ecr 2089197648,nop,wscale 10], length 0
10:00:00.000004 IP6 (flowlabel 0x1, hlim 64, next-header IPIP (4) payload length: 60) 2001:db8:20::c > 2001:db8:20::b: IP (tos 0x0, ttl 63, id 1, offset 0, flags [DF], proto TCP (6), length 60)
    192.0.2.18.8175 > 198.51.100.1.5201: Flags [S], cksum 0x0 (incorrect -> 0x1), seq 1, win 64240, options [mss 1420,sackOK,TS val 1 ecr 0,nop,wscale 7], length 0
10:00:00.000005 IP6 (flowlabel 0x2, hlim 64, next-header IPIP (4) payload length: 84) 2001:db8:20::c > 2001:db8:20::b: IP (tos 0x0, ttl 63, id 2, offset 0, flags [DF], proto ICMP (1), length 84)
    192.0.2.18 > 198.51.100.1: ICMP echo request, id 7, seq 1, length 64
"""

# NAT の範囲外の送信元ポート（ルール 199 に落ちた、など）
DUMP_IPIP_BAD_PORT = DUMP_IPIP.replace("192.0.2.18.8175 >", "192.0.2.18.40000 >")
# NAT されていない（送信元が LAN のまま）
DUMP_IPIP_NO_NAT = DUMP_IPIP.replace("192.0.2.18.8160 > 198.51.100.1.5201: Flags [S]", "192.168.10.10.8160 > 198.51.100.1.5201: Flags [S]")

# PC01 の sfp0（PC02 の手前）で見た同じ通信。MSS は端末の既定のまま、送信元ポートも NAT 前
DUMP_PC01_SIDE = """\
10:00:00.000001 IP (tos 0x0, ttl 64, id 53277, offset 0, flags [DF], proto TCP (6), length 60)
    192.168.10.10.45126 > 198.51.100.1.5201: Flags [S], cksum 0xec75 (incorrect -> 0xe764), seq 371555772, win 64240, options [mss 1460,sackOK,TS val 2089197648 ecr 0,nop,wscale 7], length 0
10:00:00.000002 IP (tos 0x0, ttl 62, id 0, offset 0, flags [DF], proto TCP (6), length 60)
    198.51.100.1.5201 > 192.168.10.10.45126: Flags [S.], cksum 0xec75 (incorrect -> 0xa3cf), seq 856776962, ack 371555773, win 64768, options [mss 1420,sackOK,TS val 725190239 ecr 2089197648,nop,wscale 10], length 0
"""

DUMP_TCLASS = """\
19:34:09.432185 IP6 (hlim 255, next-header ICMPv6 (58) payload length: 32) 2001:db8:10::10 > ff02::1:ff00:2: [icmp6 sum ok] ICMP6, neighbor solicitation, length 32, who has 2001:db8:20::2
	  source link-address option (1), length 8 (1): 1e:dc:f5:dd:21:a8
19:34:09.432223 IP6 (class 0x88, flowlabel 0xc158f, hlim 64, next-header UDP (17) payload length: 15) 2001:db8:10::10.51820 > 2001:db8:20::2.52528: [bad udp cksum 0x5bd4 -> 0x73bd!] UDP, length 7
19:34:09.432332 IP6 (flowlabel 0x00bbe, hlim 64, next-header UDP (17) payload length: 15) 2001:db8:10::10.51821 > 2001:db8:20::2.52528: [bad udp cksum 0x5bd4 -> 0x73bc!] UDP, length 7
"""


def test_outer_nh_passes_when_every_tunnel_packet_is_ipip():
    out = ok("outer-nh", "4", "2001:db8:20::c", "2001:db8:20::b", stdin=DUMP_IPIP)
    assert "3" in out  # CE → BR の 3 パケット


def test_outer_nh_fails_on_the_encapsulation_limit_option():
    out = ng("outer-nh", "4", "2001:db8:20::c", "2001:db8:20::b", stdin=DUMP_ENCAPLIMIT)
    assert "60" in out


def test_outer_nh_fails_when_no_tunnel_packet_was_seen():
    ng("outer-nh", "4", "2001:db8:20::c", "2001:db8:20::b", stdin=DUMP_TCLASS)


def test_inner_src_passes_when_nat_used_the_ce_address_and_the_port_set():
    ok("inner-src", "2001:db8:20::c", "192.0.2.18", "8160-8175", stdin=DUMP_IPIP)


def test_inner_src_also_reads_packets_behind_the_destination_options_header():
    # ポートは範囲外（45126）なので NG。ただし「パケットが無い」ではなく、ポートで落ちること
    out = ng("inner-src", "2001:db8:20::c", "192.0.2.18", "8160-8175", stdin=DUMP_ENCAPLIMIT)
    assert "45126" in out


def test_inner_src_fails_on_a_port_outside_the_set():
    out = ng("inner-src", "2001:db8:20::c", "192.0.2.18", "8160-8175", stdin=DUMP_IPIP_BAD_PORT)
    assert "40000" in out


def test_inner_src_fails_when_the_source_was_not_translated():
    out = ng("inner-src", "2001:db8:20::c", "192.0.2.18", "8160-8175", stdin=DUMP_IPIP_NO_NAT)
    assert "192.168.10.10" in out


def test_inner_src_fails_when_no_tcp_or_udp_was_seen():
    ng("inner-src", "2001:db8:20::c", "192.0.2.18", "8160-8175", stdin=DUMP_TCLASS)


def test_src_port_outside_passes_on_the_lan_side_before_nat():
    # 対照: PC02 の手前では送信元ポートは NAT の範囲外（＝範囲に収まるのは PC02 の NAT のおかげ）
    ok("src-port-outside", "192.168.10.10", "8160-8175", stdin=DUMP_PC01_SIDE)
    ng("src-port-outside", "192.168.10.10", "8160-8175", stdin=DUMP_PC01_SIDE.replace(".45126", ".8161"))
    ng("src-port-outside", "192.168.10.10", "8160-8175", stdin=DUMP_TCLASS)


def test_mss_max_passes_when_every_outgoing_syn_is_clamped():
    ok("mss-max", "1420", "192.0.2.18", stdin=DUMP_IPIP)


def test_mss_max_only_looks_at_syns_from_the_given_source():
    # BR からの SYN-ACK は 1460 でも、CE からの SYN だけを見る
    ok("mss-max", "1420", "192.0.2.18", stdin=DUMP_IPIP)


def test_mss_max_fails_on_an_unclamped_syn():
    out = ng("mss-max", "1420", "192.168.10.10", stdin=DUMP_PC01_SIDE)
    assert "1460" in out


def test_mss_max_fails_when_no_syn_was_seen():
    ng("mss-max", "1420", "192.0.2.18", stdin=DUMP_TCLASS)


def test_tclass_reads_the_traffic_class_of_the_udp_packets_from_the_port():
    ok("tclass", "0x88", "51820", stdin=DUMP_TCLASS)
    ok("tclass", "0x00", "51821", stdin=DUMP_TCLASS)  # tcpdump は 0 のとき class を出さない


def test_tclass_fails_when_the_class_differs():
    out = ng("tclass", "0x00", "51820", stdin=DUMP_TCLASS)
    assert "0x88" in out
    ng("tclass", "0x88", "51821", stdin=DUMP_TCLASS)


def test_tclass_fails_when_no_packet_from_the_port_was_seen():
    ng("tclass", "0x00", "51999", stdin=DUMP_TCLASS)


# ---- 使い方 ------------------------------------------------------------------


def test_unknown_check_is_a_usage_error():
    rc, _ = judge("no-such-check", stdin="")
    assert rc == 2
