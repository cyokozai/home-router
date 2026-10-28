"""vyos/mape-portsets.cmds の NAT ルール 101〜115 を作る（B2。CLAUDE.md §6-3 の既知の課題）。

v6プラスの例（a=4, k=8, PSID=0xfe。徹底解説v6プラス 表4.1）のポートセット 15 組を、LAN 側の送信元ポートで
15 本のルールに振り分ける。送信元ポートを 0〜65535 の連続した 15 区間に分けると、1 台の端末から同じ宛先へ
張る接続はほぼ連番のポート（Linux は 2 つおき）を使うので、全部が同じ区間 = 同じ 16 ポートに入り、17 本目で
枯渇する（2026-10-27 実測）。そこで送信元ポートを BLOCK 個ずつの塊に切り、塊の番号を 15 で割った余りで
ルールを決める（塊 b はルール 101 + b mod 15）。連番の接続は塊ごとに次の組へ移るので、15 組すべてに散る。

ポート 0 は含めない。VyOS は範囲の下端が 0 だと送信元ポートの条件を nftables に書かない（'0-4368' が
条件なしになり、ルール 101 が全部を拾った。2026-10-27 実機で確認）。

  python3 -I scripts/gen_mape_portsets.py > 生成した行
"""

A_RANGE = range(1, 16)  # A=0 の組は使わない（ウェルノウンポートを含むため）
PSID_OFFSET = 0xFE0  # PSID=0xfe を k=8 ビット、m=4 ビットの位置に置いた値
BLOCK = 16
FIRST_RULE = 101


def translation(a: int) -> tuple[int, int]:
    low = a * 4096 + PSID_OFFSET
    return low, low + 15


def source_ranges(index: int, block: int = BLOCK, count: int = len(A_RANGE)) -> list[tuple[int, int]]:
    """index 番目（0 起点）のルールが受け持つ送信元ポートの範囲の並び。"""
    out = []
    for b in range(index, 65536 // block, count):
        low, high = b * block, b * block + block - 1
        out.append((max(low, 1), high))
    return out


def rule_lines() -> list[str]:
    lines = []
    for i, a in enumerate(A_RANGE):
        n = FIRST_RULE + i
        ports = ",".join(f"{lo}-{hi}" for lo, hi in source_ranges(i))
        t_lo, t_hi = translation(a)
        lines += [
            f"set nat source rule {n} outbound-interface name 'tun0'",
            f"set nat source rule {n} source address '192.168.10.0/24'",
            f"set nat source rule {n} source port '{ports}'",
            f"set nat source rule {n} protocol 'tcp_udp'",
            f"set nat source rule {n} translation address '192.0.2.18'",
            f"set nat source rule {n} translation port '{t_lo}-{t_hi}'",
        ]
    return lines


if __name__ == "__main__":
    print("\n".join(rule_lines()))
