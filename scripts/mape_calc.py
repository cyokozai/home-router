"""v6プラス（MAP-E）の値を、NGN から受けた IPv6 の委任プレフィックスから計算する（CLAUDE.md §6-5）。

VyOS には MAP-E を自動で組む機能が無いので、計算した値を固定値として set で書く（§1 ルール 10）。
プレフィックスが変わると IPv4 だけが止まるので、そのときは人がこれを流して set を入れ直す。

  python3 -I scripts/mape_calc.py <委任プレフィックス>                     値を表示する
  python3 -I scripts/mape_calc.py --cmds --wan-if <WAN の IF> <委任プレフィックス> [--old-prefix <前のプレフィックス>]
                                                                          入れ直す set の行を出す

計算（v6プラスの 4 ルールは EA 25 ビット、PSID の長さ k=8、オフセット a=4）:
  委任プレフィックス（/56、または /64 の上位 56 ビット）を 128 ビットの整数 P とし、Rule IPv6（/31）でルールを選ぶ。
  EA = P の Rule IPv6 の直後の 25 ビット。上位 17 ビットが IPv4 のサフィックス（Rule IPv4 /15 に OR）、下位 8 ビットが PSID。
  ポートは A=1〜15 について start = (A<<12) | (PSID<<4) から 16 個（A=0 は使わない）。
  CE の IPv6 アドレス = エンドユーザのプレフィックス（/56）＋サブネット ID 0 の /64 ＋インターフェース ID。
  インターフェース ID は v6プラスが採用する draft-ietf-softwire-map-03 の形 (IPv4 << 24) | (PSID << 8) が既定。
  RFC 7597 の形 (IPv4 << 16) | PSID も --iid-format rfc7597 で選べる。

出典: ルールの表は fc2 の計算機（ipv4.web.fc2.com/map-e.html）・site-u2023/aios の mape_rules.json・intaa.net で一致。
例は『徹底解説v6プラス』表 4.1 ほか（tests/test_mape_calc.py）。
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from dataclasses import dataclass, field
from pathlib import Path

# -I（隔離モード）で動かしても隣の生成器を読めるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_mape_portsets as gen  # noqa: E402

V6PLUS_BR = "2404:9200:225:100::64"
LAB_LAN = gen.LAB_LAN
TUNNEL_MTU = 1460
ADJUST_MSS = 1420
CE_PREFIX_LEN = 64


class MapeError(ValueError):
    """プレフィックスがルールに合わない、など計算できないとき。"""


@dataclass(frozen=True)
class Rule:
    ipv6: str
    ipv4: str
    ea_len: int
    offset: int  # a
    br: str

    @property
    def net6(self) -> ipaddress.IPv6Network:
        return ipaddress.IPv6Network(self.ipv6)

    @property
    def net4(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.ipv4)

    @property
    def suffix_len(self) -> int:
        return 32 - self.net4.prefixlen

    @property
    def psid_len(self) -> int:
        return self.ea_len - self.suffix_len

    @property
    def eup_len(self) -> int:
        """エンドユーザのプレフィックス長（Rule IPv6 の長さ + EA ビット長）。"""
        return self.net6.prefixlen + self.ea_len


V6PLUS_RULES = [
    Rule("240b:10::/31", "106.72.0.0/15", 25, 4, V6PLUS_BR),
    Rule("240b:12::/31", "14.8.0.0/15", 25, 4, V6PLUS_BR),
    Rule("240b:250::/31", "14.10.0.0/15", 25, 4, V6PLUS_BR),
    Rule("240b:252::/31", "14.12.0.0/15", 25, 4, V6PLUS_BR),
]


@dataclass
class Result:
    prefix: ipaddress.IPv6Network
    rule: Rule
    ipv4: ipaddress.IPv4Address
    psid: int
    ports: list[tuple[int, int]]
    ce: ipaddress.IPv6Address
    br: ipaddress.IPv6Address
    warnings: list[str] = field(default_factory=list)


def find_rule(prefix: ipaddress.IPv6Network, rules: list[Rule]) -> Rule:
    for rule in rules:
        if prefix.network_address in rule.net6:
            return rule
    raise MapeError(f"{prefix} は v6プラスの MAP ルール（"
                    + ", ".join(r.ipv6 for r in rules)
                    + "）のどれにも入らない。v6プラスの回線ではない（BIGLOBE・OCN などは別方式）")


def interface_id(ipv4: ipaddress.IPv4Address, psid: int, iid_format: str) -> int:
    if iid_format == "draft03":
        return (int(ipv4) << 24) | (psid << 8)
    if iid_format == "rfc7597":
        return (int(ipv4) << 16) | psid
    raise MapeError(f"インターフェース ID の形が不明: {iid_format}")


def calc(prefix: str, rule: Rule | None = None, iid_format: str = "draft03") -> Result:
    try:
        net = ipaddress.IPv6Network(prefix, strict=False)
    except ValueError as e:
        raise MapeError(f"IPv6 のプレフィックスとして読めない: {prefix}") from e
    rule = rule or find_rule(net, V6PLUS_RULES)
    if net.network_address not in rule.net6:
        raise MapeError(f"{net} はルール {rule.ipv6} に入らない")
    if net.prefixlen < rule.eup_len:
        raise MapeError(f"{net} は /{rule.eup_len} より短い。EA ビットが決まらない")
    if not 0 <= rule.psid_len <= 16 - rule.offset:
        raise MapeError("ルールの EA ビット長・IPv4 の長さ・オフセットの組み合わせが合わない")

    p = int(net.network_address)
    ea = (p >> (128 - rule.eup_len)) & ((1 << rule.ea_len) - 1)
    suffix = ea >> rule.psid_len
    psid = ea & ((1 << rule.psid_len) - 1)
    ipv4 = ipaddress.IPv4Address(int(rule.net4.network_address) | suffix)
    ports = gen.port_sets(psid, a=rule.offset, k=rule.psid_len)

    eup = (p >> (128 - rule.eup_len)) << (128 - rule.eup_len)  # エンドユーザのプレフィックス。サブネット ID 0
    ce = ipaddress.IPv6Address(eup | interface_id(ipv4, psid, iid_format))

    warnings = []
    if net.prefixlen >= CE_PREFIX_LEN and ce not in ipaddress.IPv6Network((net.network_address, CE_PREFIX_LEN),
                                                                            strict=False):
        warnings.append(f"/64 の {net} は /{rule.eup_len} の境界の後ろが 0 でない。CE アドレス {ce} は"
                        " 自分の /64 の外になる（ひかり電話なしの回線では届かない可能性がある）")
    return Result(net, rule, ipv4, psid, ports, ce, ipaddress.IPv6Address(rule.br), warnings)


def cmds_lines(ipv4: str, psid: int, ce: str, br: str, wan_if: str, lan: str = LAB_LAN,
               ports: list[tuple[int, int]] | None = None) -> list[str]:
    """vyos/mape-portsets.cmds と同じ構成の set の行（値だけ差し替える）。"""
    ipv4 = str(ipaddress.IPv4Address(ipv4))
    ce = str(ipaddress.IPv6Address(ce))
    br = str(ipaddress.IPv6Address(br))
    lan = str(ipaddress.IPv4Network(lan))
    return [
        "delete protocols static route 0.0.0.0/0",
        f"set interfaces ethernet {wan_if} address '{ce}/{CE_PREFIX_LEN}'",
        "set interfaces tunnel tun0 encapsulation 'ipip6'",
        f"set interfaces tunnel tun0 source-address '{ce}'",
        f"set interfaces tunnel tun0 remote '{br}'",
        f"set interfaces tunnel tun0 address '{ipv4}/32'",
        f"set interfaces tunnel tun0 mtu '{TUNNEL_MTU}'",
        "set interfaces tunnel tun0 parameters ipv6 encaplimit 'none'",
        f"set interfaces tunnel tun0 ip adjust-mss '{ADJUST_MSS}'",
        "set protocols static route 0.0.0.0/0 interface 'tun0'",
        "set nat source rule 199 outbound-interface name 'tun0'",
        f"set nat source rule 199 source address '{lan}'",
        f"set nat source rule 199 translation address '{ipv4}'",
        "set interfaces tunnel tun0 parameters ipv6 tclass '0x00'",
        *gen.rule_lines(psid=psid, ipv4=ipv4, lan=lan, ports=ports),
    ]


def result_cmds(r: Result, wan_if: str, lan: str = LAB_LAN, old: Result | None = None) -> list[str]:
    """入れ直す行。old（前のプレフィックスの結果）を渡すと、前の CE と IPv4 のアドレスを先に消す。"""
    lines = []
    if old is not None:
        if old.ce != r.ce:
            lines.append(f"delete interfaces ethernet {wan_if} address '{old.ce}/{CE_PREFIX_LEN}'")
        if old.ipv4 != r.ipv4:
            lines.append(f"delete interfaces tunnel tun0 address '{old.ipv4}/32'")
    return lines + cmds_lines(str(r.ipv4), r.psid, str(r.ce), str(r.br), wan_if, lan, ports=r.ports)


def summary(r: Result) -> str:
    ports = ", ".join(f"{lo}-{hi}" for lo, hi in r.ports)
    return "\n".join([
        f"委任プレフィックス: {r.prefix}",
        f"MAP ルール:         {r.rule.ipv6} → {r.rule.ipv4}（EA {r.rule.ea_len} ビット、"
        f"k={r.rule.psid_len}、a={r.rule.offset}）",
        f"IPv4 アドレス:      {r.ipv4}",
        f"PSID:               {r.psid}（0x{r.psid:02x}）",
        f"ポート（{len(r.ports)} 組）:    {ports}",
        f"CE の IPv6 アドレス: {r.ce}",
        f"BR:                 {r.br}",
    ])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="v6プラス（MAP-E）の値を委任プレフィックスから計算する")
    p.add_argument("prefix", help="NGN から受けた委任プレフィックス（例 240b:10:aabb:cc00::/56、/64 も可）")
    p.add_argument("--iid-format", choices=["draft03", "rfc7597"], default="draft03",
                   help="CE のインターフェース ID の形（既定 draft03。v6プラスはこちら）")
    p.add_argument("--cmds", action="store_true", help="入れ直す VyOS の set の行を出す")
    p.add_argument("--wan-if", help="CE アドレスを置く WAN 側の ethernet の名前（--cmds で必須）")
    p.add_argument("--lan", default=LAB_LAN, help=f"NAT する LAN 側の網（既定 {LAB_LAN}）")
    p.add_argument("--old-prefix", help="前のプレフィックス。前の CE と IPv4 のアドレスを消す delete を先頭に出す")
    args = p.parse_args(argv)
    if args.cmds and not args.wan_if:
        p.error("--cmds には --wan-if が要る")
    try:
        r = calc(args.prefix, iid_format=args.iid_format)
        old = calc(args.old_prefix, iid_format=args.iid_format) if args.old_prefix else None
    except MapeError as e:
        print(f"mape_calc: {e}", file=sys.stderr)
        return 2
    for w in r.warnings:
        print(f"mape_calc: 警告: {w}", file=sys.stderr)
    if args.cmds:
        print("\n".join(result_cmds(r, wan_if=args.wan_if, lan=args.lan, old=old)))
    else:
        print(summary(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
