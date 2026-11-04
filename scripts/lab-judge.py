#!/usr/bin/env python3
"""containerlab の E2E 検査（scripts/lab-e2e.sh）で取ったログを判定する。

    python3 scripts/lab-judge.py <検査> [引数...] < ログ

ログ（ping / traceroute / ss -ltnH / tcpdump -nn -v の出力）を標準入力で受け取り、
`OK <要約>` か `NG <理由>` を 1 行出す。終了コードは OK 0 / NG 1 / 使い方の誤り 2。

検査:
  ping-ok                        全部の echo に返事があった
  ping-fails                     返事が 1 つも無かった（対照用）
  ping-frag-needed <MTU>         ルータが Frag needed（mtu = MTU）を返し、返事は無かった
  traceroute-via <HOP> <DEST>    1 ホップ目が HOP で、最後に DEST へ届いた
  route-default <GW|dev:IF>      ip -4 route show default の既定経路が 1 本だけで、GW（か IF）を向く
  listen-only <PORT> <ADDR>      PORT で待ち受けているのは ADDR だけ（1 つ以上ある）
  outer-nh <NH> <SRC6> <DST6>    SRC6 → DST6 の IPv6 パケットの Next Header がすべて NH
  inner-src <SRC6> <ADDR> <PORTS>
                                 SRC6 から出たトンネルの中の IPv4 の送信元がすべて ADDR で、
                                 TCP / UDP の送信元ポートが PORTS に収まる
  src-port-outside <ADDR> <PORTS>
                                 ADDR から出た TCP / UDP の送信元ポートがすべて PORTS の外（対照用）
  mss-max <N> <ADDR>             ADDR から出た SYN の MSS がすべて N 以下
  tclass <HEX> <SPORT>           送信元ポート SPORT の IPv6 UDP の Traffic Class がすべて HEX
  addr-is <ADDR/LEN>             ip -6 addr show に ADDR/LEN がある（DAD に失敗したものは数えない）
  addr-in <PREFIX>               ip -6 addr show に PREFIX の中のグローバルなアドレスがある。
                                 OK の直後に、見つけた最初のアドレスを ADDR/LEN で出す

PORTS は LO-HI か、それをカンマで並べたもの（v6プラスの 15 組のポートセット）。
"""

from __future__ import annotations

import ipaddress
import re
import sys
from dataclasses import dataclass, field


class Usage(Exception):
    pass


# ---- tcpdump -nn -v の読み取り -------------------------------------------------------


@dataclass
class Ip:
    """IP ヘッダ 1 つ分（外側の IPv6 か、IPv4）。ポートが無ければ None。"""

    family: int
    src: str
    dst: str
    sport: int | None = None
    dport: int | None = None
    proto: str = ""
    tclass: int = 0
    nh: int | None = None
    flags: str | None = None  # TCP のフラグ（S, S. など）
    mss: int | None = None


@dataclass
class Packet:
    outer: Ip
    inner: Ip | None = None  # IPv6 の中に入った IPv4（ipip6）
    lines: list[str] = field(default_factory=list)


IP6_HEAD = re.compile(r"\bIP6 \((?P<opts>.*?payload length: \d+)\) (?P<src>\S+) > (?P<dst>\S+):(?: (?P<rest>.*))?$")
IP4_HEAD = re.compile(r"\bIP \((?P<opts>[^\n]*?length \d+)\)")
ADDR_LINE = re.compile(r"^\s+(?P<src>\S+) > (?P<dst>[^\s:]+|\S+?):(?: (?P<rest>.*))?$")


def split_addr_port(text: str) -> tuple[str, int | None]:
    if ":" in text:  # IPv6。ポートは最後の . の後
        if "." in text:
            addr, port = text.rsplit(".", 1)
            return addr, int(port)
        return text, None
    parts = text.split(".")
    if len(parts) == 5:
        return ".".join(parts[:4]), int(parts[4])
    return text, None


def tcp_fields(rest: str) -> tuple[str | None, int | None]:
    flags = re.search(r"Flags \[([^\]]*)\]", rest or "")
    mss = re.search(r"\bmss (\d+)", rest or "")
    return (flags.group(1) if flags else None, int(mss.group(1)) if mss else None)


def ip4_from(opts: str, addr_line: str | None) -> Ip | None:
    if addr_line is None:
        return None
    m = ADDR_LINE.match(addr_line)
    if not m:
        return None
    src, sport = split_addr_port(m.group("src"))
    dst, dport = split_addr_port(m.group("dst"))
    proto = re.search(r"proto (\w+)", opts)
    flags, mss = tcp_fields(m.group("rest") or "")
    return Ip(4, src, dst, sport, dport, proto.group(1) if proto else "", flags=flags, mss=mss)


def records(text: str) -> list[list[str]]:
    """先頭が空白でない行から、次の同様の行の手前までを 1 パケットにまとめる。"""
    out: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0].isspace():
            if out:
                out[-1].append(line)
        else:
            out.append([line])
    return out


def packets(text: str) -> list[Packet]:
    found = []
    for rec in records(text):
        head = rec[0]
        cont = rec[1] if len(rec) > 1 else None
        m6 = IP6_HEAD.search(head)
        if m6:
            opts = m6.group("opts")
            tclass = re.search(r"class (0x[0-9a-f]+)", opts)
            nh = re.search(r"next-header .*?\((\d+)\)", opts)
            src, sport = split_addr_port(m6.group("src"))
            dst, dport = split_addr_port(m6.group("dst"))
            rest = m6.group("rest") or ""
            outer = Ip(
                6,
                src,
                dst,
                sport,
                dport,
                "UDP" if nh and nh.group(1) == "17" else "",
                tclass=int(tclass.group(1), 16) if tclass else 0,
                nh=int(nh.group(1)) if nh else None,
            )
            inner = None
            m4 = IP4_HEAD.search(rest)
            if m4:
                inner = ip4_from(m4.group("opts"), cont)
            found.append(Packet(outer, inner, rec))
            continue
        m4 = IP4_HEAD.search(head)
        if m4:
            ip = ip4_from(m4.group("opts"), cont)
            if ip:
                found.append(Packet(ip, None, rec))
    return found


def ipv4_packets(text: str) -> list[Ip]:
    """IPv4 のヘッダ（トンネルの中のものも含む）をすべて返す。"""
    out = []
    for p in packets(text):
        if p.inner:
            out.append(p.inner)
        elif p.outer.family == 4:
            out.append(p.outer)
    return out


def port_ranges(text: str) -> list[tuple[int, int]]:
    out = []
    for part in text.split(","):
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if not m:
            raise Usage(f"ポートの範囲は LO-HI か、それをカンマで並べて書く: {text}")
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def in_ranges(port: int, ranges: list[tuple[int, int]]) -> bool:
    return any(lo <= port <= hi for lo, hi in ranges)


def ranges_text(ranges: list[tuple[int, int]]) -> str:
    if len(ranges) <= 2:
        return ", ".join(f"{lo}-{hi}" for lo, hi in ranges)
    return f"{ranges[0][0]}-{ranges[0][1]} ほか {len(ranges)} 組"


# ---- 検査 -------------------------------------------------------------------------


def ping_stats(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def check_ping_ok(text: str) -> tuple[bool, str]:
    stats = ping_stats(text)
    if stats is None:
        return False, "ping の集計行が無い"
    sent, got = stats
    return sent > 0 and got == sent, f"{sent} 送信 / {got} 受信"


def check_ping_fails(text: str) -> tuple[bool, str]:
    stats = ping_stats(text)
    if stats is None:
        return False, "ping の集計行が無い"
    sent, got = stats
    return sent > 0 and got == 0, f"{sent} 送信 / {got} 受信"


def check_ping_frag_needed(text: str, mtu: str) -> tuple[bool, str]:
    stats = ping_stats(text)
    m = re.search(r"From (\S+) icmp_seq=\d+ Frag needed and DF set \(mtu = (\d+)\)", text)
    if not m:
        return False, "ルータからの Frag needed が無い（" + (f"{stats[0]} 送信 / {stats[1]} 受信" if stats else "集計なし") + "）"
    if m.group(2) != mtu:
        return False, f"{m.group(1)} が返した mtu が {m.group(2)}（期待 {mtu}）"
    if stats and stats[1] != 0:
        return False, f"Frag needed のほかに返事が {stats[1]} 個あった"
    return True, f"{m.group(1)} が Frag needed（mtu = {m.group(2)}）を返した"


def check_traceroute_via(text: str, hop: str, dest: str) -> tuple[bool, str]:
    hops = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\S+)", line)
        if m:
            hops[int(m.group(1))] = m.group(2)
    if not hops:
        return False, "traceroute のホップが無い"
    path = " → ".join(hops[k] for k in sorted(hops))
    last = hops[max(hops)]
    if hops.get(1) != hop:
        return False, f"1 ホップ目が {hops.get(1)}（期待 {hop}）: {path}"
    if last != dest:
        return False, f"{dest} に届いていない: {path}"
    return True, path


def check_route_default(text: str, want: str) -> tuple[bool, str]:
    found = []
    for line in text.splitlines():
        cols = line.split()
        if not cols or cols[0] != "default":
            continue
        if "via" in cols:
            found.append(cols[cols.index("via") + 1])
        elif "dev" in cols:
            found.append("dev:" + cols[cols.index("dev") + 1])
    if not found:
        return False, "既定経路が無い"
    if len(found) > 1:
        return False, f"既定経路が {len(found)} 本ある: {', '.join(found)}"
    return found[0] == want, f"既定経路は {found[0]}" + ("" if found[0] == want else f"（期待 {want}）")


def check_listen_only(text: str, port: str, addr: str) -> tuple[bool, str]:
    locals_ = []
    for line in text.splitlines():
        cols = line.split()
        if len(cols) < 5 or cols[0] != "LISTEN":
            continue
        local = cols[3]
        a, _, p = local.rpartition(":")
        if p == port:
            locals_.append(a.strip("[]").split("%")[0])
    if not locals_:
        return False, f":{port} で待ち受けているものが無い"
    other = sorted({a for a in locals_ if a != addr})
    if other:
        return False, f":{port} を {', '.join(other)} でも待ち受けている（期待 {addr} のみ）"
    return True, f":{port} は {addr} でだけ待ち受けている"


def check_outer_nh(text: str, nh: str, src: str, dst: str) -> tuple[bool, str]:
    seen = [p.outer.nh for p in packets(text) if p.outer.family == 6 and p.outer.src == src and p.outer.dst == dst]
    if not seen:
        return False, f"{src} → {dst} の IPv6 パケットが無い"
    counts = {n: seen.count(n) for n in sorted(set(seen), key=lambda x: -1 if x is None else x)}
    summary = ", ".join(f"next-header {n}: {c}" for n, c in counts.items())
    return all(n == int(nh) for n in seen), f"{len(seen)} パケット（{summary}）"


def check_inner_src(text: str, outer_src: str, addr: str, ports: str) -> tuple[bool, str]:
    ranges = port_ranges(ports)
    inner = [p.inner for p in packets(text) if p.inner and p.outer.src == outer_src]
    with_port = [i for i in inner if i.sport is not None]
    if not with_port:
        return False, f"{outer_src} から出たトンネルの中に TCP / UDP が無い"
    bad_src = sorted({i.src for i in inner if i.src != addr})
    if bad_src:
        return False, f"送信元が {', '.join(bad_src)}（期待 {addr}）"
    bad_port = sorted({i.sport for i in with_port if not in_ranges(i.sport, ranges)})
    if bad_port:
        return False, f"送信元ポート {', '.join(map(str, bad_port))} が {ranges_text(ranges)} の外"
    used = sorted({i.sport for i in with_port})
    return True, f"{len(inner)} パケット、送信元 {addr}、ポート {', '.join(map(str, used))}"


def check_src_port_outside(text: str, addr: str, ports: str) -> tuple[bool, str]:
    ranges = port_ranges(ports)
    mine = [i for i in ipv4_packets(text) if i.src == addr and i.sport is not None]
    if not mine:
        return False, f"{addr} から出た TCP / UDP が無い"
    inside = sorted({i.sport for i in mine if in_ranges(i.sport, ranges)})
    if inside:
        return False, f"送信元ポート {', '.join(map(str, inside))} が {ranges_text(ranges)} に入っている"
    return True, (f"送信元ポート {', '.join(map(str, sorted({i.sport for i in mine})))} はすべて"
                  f" {ranges_text(ranges)} の外")


def check_mss_max(text: str, limit: str, addr: str) -> tuple[bool, str]:
    syns = [i for i in ipv4_packets(text) if i.src == addr and i.flags is not None and "S" in i.flags]
    if not syns:
        return False, f"{addr} から出た SYN が無い"
    mss = sorted({i.mss for i in syns if i.mss is not None})
    if not mss:
        return False, "SYN に MSS が無い"
    over = [m for m in mss if m > int(limit)]
    if over:
        return False, f"MSS {', '.join(map(str, over))} が {limit} を超える（SYN {len(syns)} 個）"
    return True, f"SYN {len(syns)} 個の MSS は {', '.join(map(str, mss))}"


def check_tclass(text: str, want: str, sport: str) -> tuple[bool, str]:
    target = int(want, 16)
    seen = [p.outer.tclass for p in packets(text) if p.outer.family == 6 and p.outer.sport == int(sport) and p.outer.proto == "UDP"]
    if not seen:
        return False, f"送信元ポート {sport} の IPv6 UDP が無い"
    classes = ", ".join(f"0x{c:02x}" for c in sorted(set(seen)))
    return all(c == target for c in seen), f"{len(seen)} パケット、class {classes}"


# ---- ip -6 addr show -------------------------------------------------------------

INET6 = re.compile(r"^\s*inet6 (?P<addr>\S+) scope (?P<scope>\S+)(?P<flags>.*)$")


def inet6_addrs(text: str) -> list[tuple[ipaddress.IPv6Interface, str]]:
    """(アドレス, scope) の並び。DAD に失敗したものは除く。"""
    out = []
    for line in text.splitlines():
        m = INET6.match(line)
        if not m or "dadfailed" in m.group("flags").split():
            continue
        try:
            out.append((ipaddress.IPv6Interface(m.group("addr")), m.group("scope")))
        except ValueError:
            continue
    return out


def check_addr_is(text: str, want: str) -> tuple[bool, str]:
    try:
        target = ipaddress.IPv6Interface(want)
    except ValueError as e:
        raise Usage(f"IPv6 のアドレス/長さとして読めない: {want}") from e
    have = [a for a, _ in inet6_addrs(text)]
    if target in have:
        return True, f"{target} がある"
    listed = ", ".join(str(a) for a in have) or "なし"
    return False, f"{target} が無い（あるのは {listed}）"


def check_addr_in(text: str, prefix: str) -> tuple[bool, str]:
    try:
        net = ipaddress.IPv6Network(prefix)
    except ValueError as e:
        raise Usage(f"IPv6 のプレフィックスとして読めない: {prefix}") from e
    found = [a for a, scope in inet6_addrs(text) if scope == "global" and a.ip in net]
    if found:
        return True, f"{found[0]} は {net} の中"
    listed = ", ".join(str(a) for a, scope in inet6_addrs(text) if scope == "global") or "なし"
    return False, f"{net} の中のグローバルなアドレスが無い（あるのは {listed}）"


CHECKS = {
    "ping-ok": (check_ping_ok, 0),
    "ping-fails": (check_ping_fails, 0),
    "ping-frag-needed": (check_ping_frag_needed, 1),
    "traceroute-via": (check_traceroute_via, 2),
    "route-default": (check_route_default, 1),
    "listen-only": (check_listen_only, 2),
    "outer-nh": (check_outer_nh, 3),
    "inner-src": (check_inner_src, 3),
    "src-port-outside": (check_src_port_outside, 2),
    "mss-max": (check_mss_max, 2),
    "tclass": (check_tclass, 2),
    "addr-is": (check_addr_is, 1),
    "addr-in": (check_addr_in, 1),
}


def main(argv: list[str]) -> int:
    try:
        if not argv or argv[0] not in CHECKS:
            raise Usage(f"不明な検査: {argv[0] if argv else '(なし)'}")
        fn, nargs = CHECKS[argv[0]]
        if len(argv) - 1 != nargs:
            raise Usage(f"{argv[0]} の引数は {nargs} 個")
        good, detail = fn(sys.stdin.read(), *argv[1:])
    except Usage as e:
        print(f"lab-judge: {e}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2
    print(f"{'OK' if good else 'NG'} {detail}")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
