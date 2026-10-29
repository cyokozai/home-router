"""vyos/mape-portsets.cmds の NAT ルール 101〜115 を作る（B2。CLAUDE.md §6-3 の既知の課題）。

v6プラスの例（a=4, k=8, PSID=0xfe。徹底解説v6プラス 表4.1）のポートセット 15 組を、LAN 側の送信元ポートで
15 本のルールに振り分ける。送信元ポートを 0〜65535 の連続した 15 区間に分けると、1 台の端末から同じ宛先へ
張る接続はほぼ連番のポート（Linux は 2 つおき）を使うので、全部が同じ区間 = 同じ 16 ポートに入り、17 本目で
枯渇する（2026-10-27 実測）。そこで送信元ポートを BLOCK 個ずつの塊に切り、塊の番号を 15 で割った余りで
ルールを決める（塊 b はルール 101 + b mod 15）。連番の接続は塊ごとに次の組へ移るので、15 組すべてに散る。

ポート 0 は含めない。VyOS は範囲の下端が 0 だと送信元ポートの条件を nftables に書かない（'0-4368' が
条件なしになり、ルール 101 が全部を拾った。2026-10-27 実機で確認）。

  python3 -I scripts/gen_mape_portsets.py [--psid N] [--ipv4 A.B.C.D] [--lan 網] > 生成した行

引数を省くと検証用の値（PSID=0xfe、変換先 192.0.2.18、LAN 192.168.10.0/24）になり、vyos/mape-portsets.cmds の
ルール 101〜115 と一致する（tests/test_vyos_cmds.py）。本番の値は scripts/mape_calc.py が計算してこれを呼ぶ。
"""

import argparse
import ipaddress

A_BITS = 4  # v6プラスのオフセット a
PSID_BITS = 8  # v6プラスの PSID の長さ k
A_RANGE = range(1, 1 << A_BITS)  # A=0 の組は使わない（ウェルノウンポートを含むため）
LAB_PSID = 0xFE
LAB_IPV4 = "192.0.2.18"
LAB_LAN = "192.168.10.0/24"
BLOCK = 16
FIRST_RULE = 101


def port_sets(psid: int, a: int = A_BITS, k: int = PSID_BITS) -> list[tuple[int, int]]:
    """PSID のポートセット（A=1〜2^a-1 の組）。各組は start = (A << (16-a)) | (PSID << m) から 2^m 個。"""
    m = 16 - a - k
    if m < 0 or not 0 <= psid < (1 << k):
        raise ValueError(f"PSID {psid} が k={k} ビットに収まらない、または a+k が 16 を超える")
    out = []
    for i in range(1, 1 << a):
        low = (i << (16 - a)) | (psid << m)
        out.append((low, low + (1 << m) - 1))
    return out


def translation(a: int, psid: int = LAB_PSID) -> tuple[int, int]:
    return port_sets(psid)[a - 1]


def source_ranges(index: int, block: int = BLOCK, count: int = len(A_RANGE)) -> list[tuple[int, int]]:
    """index 番目（0 起点）のルールが受け持つ送信元ポートの範囲の並び。"""
    out = []
    for b in range(index, 65536 // block, count):
        low, high = b * block, b * block + block - 1
        out.append((max(low, 1), high))
    return out


def rule_lines(psid: int = LAB_PSID, ipv4: str = LAB_IPV4, lan: str = LAB_LAN,
               ports: list[tuple[int, int]] | None = None) -> list[str]:
    ports = port_sets(psid) if ports is None else ports
    if not 1 <= len(ports) <= 98:  # ルール 199 と重ならないこと
        raise ValueError(f"ポートセットの組の数が扱えない: {len(ports)}")
    ipv4 = str(ipaddress.IPv4Address(ipv4))
    lan = str(ipaddress.IPv4Network(lan))
    lines = []
    for i, (t_lo, t_hi) in enumerate(ports):
        n = FIRST_RULE + i
        src = ",".join(f"{lo}-{hi}" for lo, hi in source_ranges(i, count=len(ports)))
        lines += [
            f"set nat source rule {n} outbound-interface name 'tun0'",
            f"set nat source rule {n} source address '{lan}'",
            f"set nat source rule {n} source port '{src}'",
            f"set nat source rule {n} protocol 'tcp_udp'",
            f"set nat source rule {n} translation address '{ipv4}'",
            f"set nat source rule {n} translation port '{t_lo}-{t_hi}'",
        ]
    return lines


def main() -> None:
    p = argparse.ArgumentParser(description="MAP-E のポートセットを NAT のルール 101〜 に振り分ける set の行を出す")
    p.add_argument("--psid", type=lambda x: int(x, 0), default=LAB_PSID, help="PSID（既定 0xfe。0x 付きも可）")
    p.add_argument("--ipv4", default=LAB_IPV4, help=f"変換先の IPv4 アドレス（既定 {LAB_IPV4}）")
    p.add_argument("--lan", default=LAB_LAN, help=f"NAT する LAN 側の網（既定 {LAB_LAN}）")
    args = p.parse_args()
    print("\n".join(rule_lines(psid=args.psid, ipv4=args.ipv4, lan=args.lan)))


if __name__ == "__main__":
    main()
