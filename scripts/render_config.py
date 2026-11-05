"""本番の PC02（光クロス 10G・ひかり電話なし・v6プラス）の VyOS の設定を、site の yaml とテンプレートから作る。

テンプレートは vyos/prod/*.cmds.j2。出力は部分ごとの .cmds（set の行だけ。CLAUDE.md §1 ルール 10）:

  00-base.cmds          ホスト名・時刻・NTP・ログイン鍵・SSH・救出用の eth0・オフロードとリング・conntrack
  10-lan.cmds           LAN のアドレス（IPv4 と ULA）・DHCPv4 サーバ・DNS の転送・RA
  20-wan-common.cmds    WAN の説明と RA の受け入れ
  20-wan-<型>.cmds      site の wan.type で選ぶ。pd56（型A、ONU 直結）/ pd60（型B、HGW 配下）/ ra64（型C、RA の /64）
  30-firewall.cmds      ステートフルの forward と flowtable、ルータ宛ての input、NGN 向けの DSCP の書き換え
  40-mape.cmds          --mape のときだけ。site の wan.prefix から scripts/mape_calc.py で MAP-E の行を作る（接続後の段）
  50-monitor.cmds       SNMP（読み取り専用）と node-exporter

リポジトリに書けない値（秘密の値・回線ごとに決まる値）は @名前@ の印のまま出し、投入時に
scripts/pc02-commit.sh が環境変数で置き換える（scripts/placeholders.sh）。site の yaml に値を書けば、その値で描く。
実際の値を書いた site は site/<名前>.local.yaml に置く（.gitignore 済み）。出力も site/<名前>.local/ に出す。

  python3 -I scripts/render_config.py site/example.yaml -o <出力先>          site の型の部分を描く
  python3 -I scripts/render_config.py site/example.yaml -o <出力先> --all-wan-types   3 つの型の WAN の部分を全部描く
  python3 -I scripts/render_config.py site/home.local.yaml -o site/home.local --mape  MAP-E の段も描く
  python3 -I scripts/render_config.py site/example.yaml --check vyos/prod/example --all-wan-types
                                                                          描いた結果と置いてあるものが一致するか

終了コード: 0 = 成功、1 = --check で不一致、2 = site の誤り。
"""

from __future__ import annotations

import argparse
import copy
import ipaddress
import re
import sys
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "vyos" / "prod"
VERSION_FILE = ROOT / "vyos" / "VERSION"

# -I（隔離モード）で動かしても隣の計算器を読めるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mape_calc  # noqa: E402

WAN_TYPES = ("pd56", "pd60", "ra64")
# 型C は RA の /64 を LAN 側に置くので、WAN に置く CE のアドレスは /128
CE_PLEN = {"pd56": 64, "pd60": 64, "ra64": 128}
PERFORMANCE = ("network-throughput", "network-latency")
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")

# 印（scripts/placeholders.sh と同じ名前と形）
ULA_PLACEHOLDER = "@ULA_PREFIX@"
WAN_PREFIX64_PLACEHOLDER = "@WAN_PREFIX64@"
NGN_GW_LLA_PLACEHOLDER = "@NGN_GW_LLA@"
FOUR_HEXTETS = re.compile(r"[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){3}")
ULA_FORM = re.compile(r"fd[0-9a-f]{2}(?::[0-9a-f]{1,4}){3}")
LLA_FORM = re.compile(r"fe80::[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,3}")

REQUIRED = ("host_name", "wan.type")
DEFAULTS: dict = {
    "host_name": "home-router",
    "time_zone": "Asia/Tokyo",
    "ntp_servers": ["ntp.nict.jp", "time.google.com"],
    "interfaces": {"rescue": "eth0", "lan": "eth1", "wan": "eth2"},
    "login": {"user": "vyos", "key_name": "admin", "key_type": "ssh-ed25519"},
    "rescue": {"address": "192.168.255.1/24"},
    "lan": {
        "address": "192.168.2.1/24",
        "ula_prefix": ULA_PLACEHOLDER,
        "dhcp": {
            "subnet_id": 1,
            "range": {"start": "192.168.2.100", "stop": "192.168.2.199"},
            "static_mappings": [],
        },
    },
    "wan": {"type": None, "prefix": None, "prefix64": WAN_PREFIX64_PLACEHOLDER, "ngn_gw_lla": NGN_GW_LLA_PLACEHOLDER},
    "tuning": {"ring_buffer": 4096, "conntrack_table_size": 1048576, "conntrack_hash_size": 262144,
               "performance": "network-throughput"},
    "dns": {"upstream": []},
    "firewall": {"dscp_udp_port": 51820, "router_wireguard": False},
    "monitor": {"snmp": {"enabled": True, "network": "192.168.2.50/32"}, "node_exporter": True},
}

PARTS_BEFORE_WAN = ("00-base", "10-lan", "20-wan-common")
PARTS_AFTER_WAN = ("30-firewall", "50-monitor")


class SiteError(ValueError):
    """site の yaml の誤り。メッセージの先頭に、誤ったキーを「a.b」の形で書く。"""


# ---- 読み込みと検査 --------------------------------------------------------------------


def load_site(path: str | Path) -> dict:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise SiteError(f"site を読めない: {path}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise SiteError(f"site の yaml として読めない: {path}: {e}") from e
    if not isinstance(raw, dict):
        raise SiteError(f"site の最上位は「キー: 値」の並びにする: {path}")
    return raw


def merge(defaults: dict, given: dict, where: str = "") -> dict:
    """既定値に site の値を重ねる。既定値に無いキーは誤記として止める。"""
    out = copy.deepcopy(defaults)
    for key, value in given.items():
        dotted = f"{where}{key}"
        if key not in defaults:
            raise SiteError(f"{dotted}: 知らないキー（書き間違いでないか）")
        if isinstance(defaults[key], dict):
            if not isinstance(value, dict):
                raise SiteError(f"{dotted}: 「キー: 値」の並びにする")
            out[key] = merge(defaults[key], value, f"{dotted}.")
        else:
            out[key] = value
    return out


def lookup(d: dict, dotted: str):
    for key in dotted.split("."):
        if not isinstance(d, dict) or key not in d:
            return None
        d = d[key]
    return d


def text(value, key: str, pattern: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise SiteError(f"{key}: 形が合わない（{pattern}）: {value!r}")
    return value


def integer(value, key: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SiteError(f"{key}: {low}〜{high} の整数にする: {value!r}")
    return value


def boolean(value, key: str) -> bool:
    if not isinstance(value, bool):
        raise SiteError(f"{key}: true か false にする: {value!r}")
    return value


def ipv4_host(value, key: str, net: ipaddress.IPv4Network | None = None) -> ipaddress.IPv4Address:
    try:
        ip = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, ValueError) as e:
        raise SiteError(f"{key}: IPv4 アドレスとして読めない: {value!r}") from e
    if net is not None and (ip not in net or ip in (net.network_address, net.broadcast_address)):
        raise SiteError(f"{key}: {net} のホストのアドレスにする: {ip}")
    return ip


def ipv4_interface(value, key: str) -> ipaddress.IPv4Interface:
    try:
        iface = ipaddress.IPv4Interface(value)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError) as e:
        raise SiteError(f"{key}: 「アドレス/長さ」の IPv4 として読めない: {value!r}") from e
    net = iface.network
    if net.prefixlen > 30 or iface.ip in (net.network_address, net.broadcast_address):
        raise SiteError(f"{key}: 網のアドレスやブロードキャストではなく、ホストのアドレスを書く: {value}")
    if not net.is_private:
        raise SiteError(f"{key}: プライベートなアドレスにする: {value}")
    return iface


def placeholder_or(value, key: str, placeholder: str, form: re.Pattern, check=None) -> str:
    if value == placeholder:
        return value
    if not isinstance(value, str) or not form.fullmatch(value):
        raise SiteError(f"{key}: {placeholder} のままにするか、{form.pattern} の形で書く: {value!r}")
    if check is not None:
        check(value)
    return value


def validate(raw: dict) -> dict:
    """site の値を確かめ、テンプレートに渡す値にする。"""
    for dotted in REQUIRED:
        if lookup(raw, dotted) is None:
            raise SiteError(f"{dotted}: 必ず書く")
    s = merge(DEFAULTS, raw)

    host_name = text(s["host_name"], "host_name", r"[A-Za-z0-9][A-Za-z0-9-]{0,62}")
    time_zone = text(s["time_zone"], "time_zone", r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)*")
    ntp = s["ntp_servers"]
    if not isinstance(ntp, list) or not ntp:
        raise SiteError("ntp_servers: 1 つ以上の一覧にする")
    ntp = [text(v, "ntp_servers", r"[A-Za-z0-9][A-Za-z0-9.:-]*") for v in ntp]

    ifs = {k: text(v, f"interfaces.{k}", r"eth\d+") for k, v in s["interfaces"].items()}
    if len(set(ifs.values())) != len(ifs):
        raise SiteError(f"interfaces: 救出用・LAN・WAN は別の IF にする: {ifs}")

    login = {
        "user": text(s["login"]["user"], "login.user", r"[a-z_][a-z0-9_-]{0,31}"),
        "key_name": text(s["login"]["key_name"], "login.key_name", r"[A-Za-z0-9_.-]{1,64}"),
        "key_type": s["login"]["key_type"],
    }
    if login["key_type"] not in KEY_TYPES:
        raise SiteError(f"login.key_type: {', '.join(KEY_TYPES)} のどれか: {login['key_type']!r}")

    rescue = ipv4_interface(s["rescue"]["address"], "rescue.address")
    lan_if = ipv4_interface(s["lan"]["address"], "lan.address")
    lan_net = lan_if.network
    if rescue.network.overlaps(lan_net):
        raise SiteError(f"rescue.address: LAN（{lan_net}）と重ならない網にする: {rescue}")

    def ula_check(v: str) -> None:
        if ipaddress.IPv6Network(f"{v}::/64").network_address not in ipaddress.IPv6Network("fd00::/8"):
            raise SiteError(f"lan.ula_prefix: fd00::/8 の中にする: {v}")

    ula = placeholder_or(s["lan"]["ula_prefix"], "lan.ula_prefix", ULA_PLACEHOLDER, ULA_FORM, ula_check)

    dhcp = s["lan"]["dhcp"]
    subnet_id = integer(dhcp["subnet_id"], "lan.dhcp.subnet_id", 1, 2**32 - 1)
    rng = dhcp["range"]
    start = ipv4_host(rng.get("start"), "lan.dhcp.range.start", lan_net)
    stop = ipv4_host(rng.get("stop"), "lan.dhcp.range.stop", lan_net)
    if start > stop or start <= lan_if.ip <= stop:
        raise SiteError(f"lan.dhcp.range: start ≦ stop にし、ルータのアドレス {lan_if.ip} を含めない: {start}〜{stop}")
    mappings = []
    mapping_list = dhcp["static_mappings"]
    if not isinstance(mapping_list, list):
        raise SiteError("lan.dhcp.static_mappings: 一覧にする")
    for n, m in enumerate(mapping_list):
        key = f"lan.dhcp.static_mappings[{n}]"
        if not isinstance(m, dict) or set(m) != {"name", "mac", "ip"}:
            raise SiteError(f"{key}: name・mac・ip の 3 つを書く: {m!r}")
        ip = ipv4_host(m["ip"], f"{key}.ip", lan_net)
        if ip == lan_if.ip:
            raise SiteError(f"{key}.ip: ルータのアドレスと重なる: {ip}")
        mappings.append({
            "name": text(m["name"], f"{key}.name", r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}"),
            "mac": text(m["mac"], f"{key}.mac", r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}"),
            "ip": str(ip),
        })
    for field in ("name", "mac", "ip"):
        values = [m[field] for m in mappings]
        if len(values) != len(set(values)):
            raise SiteError(f"lan.dhcp.static_mappings: {field} が重なっている")

    wan_type = s["wan"]["type"]
    if wan_type not in WAN_TYPES:
        raise SiteError(f"wan.type: {' / '.join(WAN_TYPES)} のどれか: {wan_type!r}")
    prefix = s["wan"]["prefix"]
    if prefix is not None:
        try:
            mape_calc.calc(str(prefix))
        except mape_calc.MapeError as e:
            raise SiteError(f"wan.prefix: {e}") from e
        prefix = str(prefix)
    prefix64 = placeholder_or(s["wan"]["prefix64"], "wan.prefix64", WAN_PREFIX64_PLACEHOLDER, FOUR_HEXTETS)
    gw = placeholder_or(s["wan"]["ngn_gw_lla"], "wan.ngn_gw_lla", NGN_GW_LLA_PLACEHOLDER, LLA_FORM)

    t = s["tuning"]
    tuning = {
        "ring_buffer": integer(t["ring_buffer"], "tuning.ring_buffer", 64, 16384),
        "conntrack_table_size": integer(t["conntrack_table_size"], "tuning.conntrack_table_size", 1, 2**32 - 1),
        "conntrack_hash_size": integer(t["conntrack_hash_size"], "tuning.conntrack_hash_size", 1, 2**32 - 1),
        "performance": t["performance"],
    }
    if tuning["performance"] is not None and tuning["performance"] not in PERFORMANCE:
        raise SiteError(f"tuning.performance: {' / '.join(PERFORMANCE)} か null: {tuning['performance']!r}")

    upstream = s["dns"]["upstream"]
    if not isinstance(upstream, list):
        raise SiteError(f"dns.upstream: IP アドレスの一覧にする: {upstream!r}")
    dns = {"upstream": []}
    for n, v in enumerate(upstream):
        try:
            dns["upstream"].append(str(ipaddress.ip_address(v)))
        except (TypeError, ValueError) as e:
            raise SiteError(f"dns.upstream[{n}]: IP アドレスとして読めない: {v!r}") from e

    fw = {
        "dscp_udp_port": integer(s["firewall"]["dscp_udp_port"], "firewall.dscp_udp_port", 1, 65535),
        "router_wireguard": boolean(s["firewall"]["router_wireguard"], "firewall.router_wireguard"),
    }

    snmp = s["monitor"]["snmp"]
    snmp_enabled = boolean(snmp["enabled"], "monitor.snmp.enabled")
    try:
        snmp_net = str(ipaddress.ip_network(snmp["network"]))
    except (TypeError, ValueError) as e:
        raise SiteError(f"monitor.snmp.network: 網として読めない: {snmp['network']!r}") from e
    monitor = {"snmp": {"enabled": snmp_enabled, "network": snmp_net},
               "node_exporter": boolean(s["monitor"]["node_exporter"], "monitor.node_exporter")}

    return {
        "host_name": host_name,
        "time_zone": time_zone,
        "ntp_servers": ntp,
        "i": ifs,
        "login": login,
        "rescue": {"address": str(rescue), "ip": str(rescue.ip)},
        "lan": {"address": str(lan_if), "ip": str(lan_if.ip), "network": str(lan_net), "ula_prefix": ula,
                "dhcp": {"subnet_id": subnet_id, "start": str(start), "stop": str(stop),
                         "static_mappings": mappings}},
        "wan": {"type": wan_type, "prefix": prefix, "prefix64": prefix64, "ngn_gw_lla": gw},
        "tuning": tuning,
        "dns": dns,
        "firewall": fw,
        "monitor": monitor,
        "br": mape_calc.V6PLUS_BR,
    }


# ---- 描画 --------------------------------------------------------------------------------


def vyos_version() -> str:
    m = re.search(r"^VYOS_VERSION=(\S+)$", VERSION_FILE.read_text(encoding="utf-8"), re.M)
    if not m:
        raise RuntimeError(f"{VERSION_FILE} に VYOS_VERSION が無い")
    return m.group(1)


def header(name: str, source: str) -> str:
    return (f"# 生成: scripts/render_config.py（site: {source}、VyOS {vyos_version()}）。"
            f"手で直さず、テンプレート（vyos/prod/{name}.j2）か site を直して描き直す\n")


def environment() -> jinja2.Environment:
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)), undefined=jinja2.StrictUndefined,
                              trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True, autoescape=False)


def tidy(name: str, body: str) -> str:
    """空行をまとめ、行の形を確かめる（テンプレートの誤りで set 以外が混ざらないように）。"""
    out = []
    for line in body.splitlines():
        line = line.rstrip()
        if not line and (not out or not out[-1]):
            continue
        if line and not line.startswith("#"):
            allowed = ("set ", "delete ") if name == "20-wan-ra64.cmds" else ("set ",)
            if not line.startswith(allowed) or line.count("'") % 2 or "#" in line:
                raise RuntimeError(f"{name}: テンプレートが set の行でないものを出した: {line}")
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + "\n"


def render_part(env: jinja2.Environment, stem: str, ctx: dict, source: str) -> str:
    name = f"{stem}.cmds"
    return header(name, source) + tidy(name, env.get_template(f"{name}.j2").render(**ctx))


def render(ctx: dict, source: str, all_wan_types: bool = False) -> dict[str, str]:
    """部分ごとの .cmds（名前 → 中身）。順番は投入する順。"""
    env = environment()
    wan_types = WAN_TYPES if all_wan_types else (ctx["wan"]["type"],)
    stems = [*PARTS_BEFORE_WAN, *(f"20-wan-{t}" for t in wan_types), *PARTS_AFTER_WAN]
    out = {}
    for stem in stems:
        text_ = render_part(env, stem, ctx, source)
        if any(line and not line.startswith("#") for line in text_.splitlines()):
            out[f"{stem}.cmds"] = text_
    return out


def mape_result(ctx: dict) -> mape_calc.Result:
    if ctx["wan"]["prefix"] is None:
        raise SiteError("wan.prefix: MAP-E の段（--mape）には、接続後に受けた委任プレフィックスを書く")
    return mape_calc.calc(ctx["wan"]["prefix"])


def render_mape(ctx: dict, source: str) -> str:
    """40-mape.cmds。scripts/mape_calc.py の行（前の値を消す delete は除く）と、tun0 の flowtable。"""
    r = mape_result(ctx)
    lines = [line for line in mape_calc.result_cmds(r, wan_if=ctx["i"]["wan"], lan=ctx["lan"]["network"],
                                                    ce_plen=CE_PLEN[ctx["wan"]["type"]])
             if not line.startswith("delete ")]
    body = ("# MAP-E（v6プラス）。scripts/mape_calc.py で wan.prefix から計算した値。プレフィックスが変わったら描き直す\n"
            + "\n".join(lines) + "\n"
            + "# IPv4 の WAN 側（tun0）も flowtable に入れる（tun0 はこの段で作るので、30-firewall には書けない）\n"
            + "set firewall flowtable FT10G interface 'tun0'\n")
    return header("40-mape.cmds", source).replace("（vyos/prod/40-mape.cmds.j2）", "（scripts/mape_calc.py）") \
        + tidy("40-mape.cmds", body)


# ---- コマンドライン ------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="本番の PC02 の VyOS の設定を site の yaml から描く（vyos/prod/*.cmds.j2）")
    p.add_argument("site", help="site の yaml（例 site/example.yaml。実際の値は site/<名前>.local.yaml）")
    out = p.add_mutually_exclusive_group(required=True)
    out.add_argument("-o", "--output", help="部分ごとの .cmds を書き出すディレクトリ")
    out.add_argument("--check", help="描いた結果が、このディレクトリの .cmds と一致するかだけを見る")
    p.add_argument("--all-wan-types", action="store_true", help="WAN の部分を 3 つの型すべて描く（例の置き場所用）")
    p.add_argument("--mape", action="store_true", help="wan.prefix から MAP-E の段（40-mape.cmds）も描く")
    args = p.parse_args(argv)

    source = Path(args.site).name
    try:
        ctx = validate(load_site(args.site))
        files = render(ctx, source=source, all_wan_types=args.all_wan_types)
        if args.mape:
            r = mape_result(ctx)
            for w in r.warnings:
                print(f"render_config: 警告: {w}", file=sys.stderr)
            files["40-mape.cmds"] = render_mape(ctx, source=source)
    except SiteError as e:
        print(f"render_config: {args.site}: {e}", file=sys.stderr)
        return 2
    files = dict(sorted(files.items()))
    if ctx["wan"]["type"] == "ra64":
        print("render_config: 警告: wan.type が ra64（型C）。RA の /64 を LAN に置き ND proxy で答える形は仮説で、"
              "実機で未確認（VyOS のコンテナで commit が通ることだけを確かめてある）", file=sys.stderr)

    if args.check:
        d = Path(args.check)
        on_disk = {f.name: f.read_text(encoding="utf-8") for f in d.glob("*.cmds")}
        differ = sorted(n for n in set(files) | set(on_disk) if files.get(n) != on_disk.get(n))
        if differ:
            print(f"render_config: {d} が描いた結果と一致しない: {', '.join(differ)}"
                  "（scripts/render_config.py で描き直す）", file=sys.stderr)
            return 1
        print(f"render_config: {d} は描いた結果と一致する（{len(files)} 本）")
        return 0

    d = Path(args.output)
    d.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")
        print(d / name)
    # 前に描いた別の型の 20-wan-*.cmds などが残っていると、*.cmds をまとめて投入したときに混ざる
    stale = sorted(f.name for f in d.glob("*.cmds") if f.name not in files)
    if stale:
        print(f"render_config: 警告: {d} に今回描いていない .cmds が残っている: {', '.join(stale)}"
              "（投入する前に消すか、別のディレクトリに描く）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
