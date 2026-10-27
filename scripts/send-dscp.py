"""DSCP を付けた IPv6 の UDP を数個送る（T8 の書き換えの動作確認。PC01 で実行する）。

    python3 send-dscp.py <宛先 IPv6> <送信元ポート> [DSCP（既定 0x22 = AF41。TOS バイトでは 0x88）] [個数]
"""

import socket
import sys

dst, sport = sys.argv[1], int(sys.argv[2])
dscp = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0x22
count = int(sys.argv[4]) if len(sys.argv) > 4 else 5

s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_TCLASS, dscp << 2)  # トラフィッククラス = DSCP << 2 | ECN
s.bind(("::", sport))
for i in range(count):
    s.sendto(f"t8-dscp-check {i}".encode(), (dst, 52528))
print(f"sent {count} packets sport={sport} tclass=0x{dscp << 2:02x}")
