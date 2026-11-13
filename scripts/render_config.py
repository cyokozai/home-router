"""本番の PC02（光クロス 10G・ひかり電話なし・v6プラス）の VyOS の設定を、site の yaml とテンプレートから作る。

テンプレートは vyos/prod/*.cmds.j2。出力は部分ごとの .cmds（set と delete の行だけ。CLAUDE.md §1 ルール 10。
delete は 20-wan-ra64 の 1 行と 01-migrate-from-lab だけ）:

  00-base.cmds          ホスト名・時刻・NTP・ログイン鍵・SSH・救出用の eth0・オフロードとリング・conntrack
  01-migrate-from-lab.cmds
                        --from-lab のときだけ。検証環境の状態（configs/pc02/baseline.cmds: フェーズ1 と VRF MGMT）
                        から本番へ移る最初の commit で、VRF MGMT・検証用のアドレスと経路・前の NTP サーバを消す
                        delete の行。同じ commit の他の部分が set する所は消さないので、つなぐ順によらない。
                        2 回目からは描かない（無い所の delete は VyOS が拒否する）
  10-lan.cmds           LAN のアドレス（IPv4 と ULA）・DHCPv4 サーバ・DNS の転送・RA
  20-wan-common.cmds    WAN の説明と RA の受け入れ、wan.duid_ll_mac を書いたときは DHCPv6 の DUID（DUID-LL）
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
  python3 -I scripts/render_config.py site/home.local.yaml -o site/home.local --from-lab
                                                                          検証環境から移る最初の commit 用
  python3 -I scripts/render_config.py site/example.yaml --check vyos/prod/example --all-wan-types
                                                                          描いた結果と置いてあるものが一致するか

終了コード: 0 = 成功、1 = --check で不一致、2 = site の誤り。
"""

from __future__ import annotations

import argparse
import copy
import ipaddress
import re
import shlex
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
MAC_FORM = r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}"
# DUID-LL（RFC 8415 11.4）: DUID の種別 3、ハードウェアの種別 1（Ethernet）、続けて MAC。NTT東「IP通信網サービスの
# インタフェース 第三分冊」第46版 4.4.2.1.4 は、光クロスの端末の DUID を DUID-LL か DUID-LLT にする。VyOS の既定は
# DUID-UUID（種別 4）で、2026-11-13 に光クロスへつないだ PC02 は DHCPv6-PD に返事をもらえなかった
DUID_LL_PREFIX = "00:03:00:01"

REQUIRED = ("host_name", "wan.type")
DEFAULTS: dict = {
    "host_name": "home-router",
    "time_zone": "Asia/Tokyo",
    "ntp_servers": ["ntp.nict.jp", "time.google.com"],
    "interfaces": {"rescue": "eth0", "lan": "eth1", "wan": "eth2"},
    "login": {"user": "vyos", "key_name": "admin", "key_type": "ssh-ed25519"},
    "rescue": {"address": "192.168.255.1/24"},
    # rescue: interfaces.rescue を救出用の固定アドレスにする（既定）。vrf: VRF MGMT に入れて DHCP で受ける
    "mgmt": {"mode": "rescue", "vrf_table": 100},
    "lan": {
        "address": "192.168.2.1/24",
        "ula_prefix": ULA_PLACEHOLDER,
        "dhcp": {
            "subnet_id": 1,
            "range": {"start": "192.168.2.100", "stop": "192.168.2.199"},
            "static_mappings": [],
        },
        # VLAN-aware のブリッジで網を分けるとき（validate_bridge）。null なら interfaces.lan に 1 つの網を置く
        "bridge": None,
    },
    # duid_ll_mac: WAN の MAC。書くと DHCPv6 の DUID を DUID-LL にする（光クロスの PD に要る。DUID_LL_PREFIX）。null なら描かない
    "wan": {"type": None, "prefix": None, "prefix64": WAN_PREFIX64_PLACEHOLDER, "ngn_gw_lla": NGN_GW_LLA_PLACEHOLDER,
            "duid_ll_mac": None},
    "tuning": {"ring_buffer": 4096, "conntrack_table_size": 1048576, "conntrack_hash_size": 262144,
               "performance": "network-throughput"},
    "dns": {"upstream": []},
    "firewall": {"dscp_udp_port": 51820, "router_wireguard": False,
                 # 網ごとの monitor_to で通す監視の通信（ICMP に加えて）
                 "monitor_ports": {"tcp": [9100], "udp": [161]},
                 # NGN から LAN の WireGuard へ来る新しい通信を通す先のインターフェース ID（下位 64 ビット）。null なら描かない
                 "wireguard_inbound_iid": None},
    "monitor": {"snmp": {"enabled": True, "network": "192.168.2.50/32"}, "node_exporter": True},
}

MGMT_MODES = ("rescue", "vrf")
ACCESS = ("all", "internet")
MGMT_VRF = "MGMT"
NET_NAME = r"[A-Z][A-Z0-9_-]{0,15}"
BRIDGE_NAME = r"br\d{1,3}"
IID_MASK = "::ffff:ffff:ffff:ffff"

PARTS_BEFORE_WAN = ("00-base", "10-lan", "20-wan-common")
PARTS_AFTER_WAN = ("30-firewall", "50-monitor")

# ---- 検証環境から移る段（--from-lab） ------------------------------------------------------------
# 実機の PC02 は configs/pc02/baseline.cmds（フェーズ1 と VRF MGMT）で動いている。本番の部分は set だけなので、
# そのまま重ねると SSH が VRF MGMT に残り、検証用の既定経路やアドレスも残る。最初の commit だけ、これを消す段を足す。
# baseline.cmds の行は、すべて次の 3 つのどれかに分けてある（tests/test_render_config.py が確かめる）。
MIGRATE_PART = "01-migrate-from-lab.cmds"
LAB_BASELINE = "configs/pc02/baseline.cmds"
# 消す。候補は広い順に並べ、同じ commit の他の部分が候補の所かその下を set していれば、次の狭い候補に移る
# （順番によらず同じ結果にするため）。どの候補も当たれば消さない
MIGRATE_DELETE = (
    ("interfaces ethernet eth0 address 'dhcp'",),
    ("interfaces ethernet eth0 vrf 'MGMT'",),
    ("interfaces ethernet eth1 address '192.168.10.1/24'",),
    ("interfaces ethernet eth1 address '2001:db8:10::1/64'",),
    ("interfaces ethernet eth2 address '192.168.20.1/30'",),
    ("interfaces ethernet eth2 address '2001:db8:20::1/64'",),
    # 経路ごと消すと、MAP-E の段（route 0.0.0.0/0 interface tun0）や型C（route6 ::/0 next-hop <NGN>）が消えうる
    ("protocols static route 0.0.0.0/0", "protocols static route 0.0.0.0/0 next-hop '192.168.20.2'"),
    ("protocols static route6 ::/0", "protocols static route6 ::/0 next-hop '2001:db8:20::2'"),
    # node-exporter を使わない site で vrf だけを消すと、全部の IF で待ち受ける node-exporter が残る
    ("service monitoring prometheus node-exporter", "service monitoring prometheus node-exporter vrf 'MGMT'"),
    ("service ntp server '192.168.2.13'",),
    ("service ntp vrf 'MGMT'",),
    ("service ssh vrf 'MGMT'",),
    ("vrf name MGMT",),
)
# 1 つの値だけを持つ葉。他の部分が同じ葉を set すれば上書きされるので消さない。誰も set しなければ消す
MIGRATE_LEAVES = (
    "interfaces ethernet eth0 description 'MGMT-1G'",
    "interfaces ethernet eth1 description 'LAN-10G-to-PC01'",
    "interfaces ethernet eth2 description 'WAN-10G-to-PC03'",
    "system host-name 'pc02-vyos'",
)
# 残す（行の頭）。本番でもそのまま使うもの、インストール時の既定、ログインと秘密の値（触らない）
MIGRATE_KEEP = (
    "interfaces ethernet eth0 hw-id", "interfaces ethernet eth0 offload",
    "interfaces ethernet eth1 hw-id", "interfaces ethernet eth1 offload",
    "interfaces ethernet eth2 hw-id", "interfaces ethernet eth2 offload",
    "interfaces loopback lo",
    "service ntp allow-client",
    # VyOS の既定の NTP サーバ。インストールし直した本番機にも入るので、ここでも消さない
    "service ntp server time1.vyos.net", "service ntp server time2.vyos.net", "service ntp server time3.vyos.net",
    "service ssh disable-password-authentication",
    "system config-management", "system console", "system option reboot-on-upgrade-failure", "system syslog",
    # 今の鍵（claude-lab）も残す。消すかどうかは、本番の鍵で入れることを確かめてから別に決める
    "system login",
)


def config_words(line: str) -> list[str]:
    """設定の行を語の並びにする（引用符を外す。'x' と x を同じに扱う）。"""
    return shlex.split(line)


MIGRATE_DELETE_WORDS = [[config_words(c) for c in cands] for cands in MIGRATE_DELETE]
MIGRATE_LEAVES_WORDS = [config_words(c) for c in MIGRATE_LEAVES]
MIGRATE_KEEP_WORDS = [config_words(c) for c in MIGRATE_KEEP]


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


def validate_dhcp(dhcp, key: str, iface: ipaddress.IPv4Interface, default_subnet_id: int | None = None) -> dict:
    """DHCPv4 サーバの範囲と固定の割り当て。iface はルータのアドレス（網と、範囲から外すアドレス）。"""
    if not isinstance(dhcp, dict):
        raise SiteError(f"{key}: 「キー: 値」の並びにする")
    unknown = set(dhcp) - {"subnet_id", "range", "static_mappings"}
    if unknown:
        raise SiteError(f"{key}.{sorted(unknown)[0]}: 知らないキー（書き間違いでないか）")
    net = iface.network
    subnet_id = dhcp.get("subnet_id", default_subnet_id)
    subnet_id = integer(subnet_id, f"{key}.subnet_id", 1, 2**32 - 1)
    rng = dhcp.get("range")
    if not isinstance(rng, dict):
        raise SiteError(f"{key}.range: start と stop を書く")
    start = ipv4_host(rng.get("start"), f"{key}.range.start", net)
    stop = ipv4_host(rng.get("stop"), f"{key}.range.stop", net)
    if start > stop or start <= iface.ip <= stop:
        raise SiteError(f"{key}.range: start ≦ stop にし、ルータのアドレス {iface.ip} を含めない: {start}〜{stop}")
    mappings = []
    mapping_list = dhcp.get("static_mappings", [])
    if not isinstance(mapping_list, list):
        raise SiteError(f"{key}.static_mappings: 一覧にする")
    for n, m in enumerate(mapping_list):
        mkey = f"{key}.static_mappings[{n}]"
        if not isinstance(m, dict) or set(m) != {"name", "mac", "ip"}:
            raise SiteError(f"{mkey}: name・mac・ip の 3 つを書く: {m!r}")
        ip = ipv4_host(m["ip"], f"{mkey}.ip", net)
        if ip == iface.ip:
            raise SiteError(f"{mkey}.ip: ルータのアドレスと重なる: {ip}")
        mappings.append({
            "name": text(m["name"], f"{mkey}.name", r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}"),
            "mac": text(m["mac"], f"{mkey}.mac", MAC_FORM),
            "ip": str(ip),
        })
    for field in ("name", "mac", "ip"):
        values = [m[field] for m in mappings]
        if len(values) != len(set(values)):
            raise SiteError(f"{key}.static_mappings: {field} が重なっている")
    return {"subnet_id": subnet_id, "start": str(start), "stop": str(stop), "static_mappings": mappings}


NET_KEYS = {"name", "vlan", "native", "address", "sla_id", "access", "monitor_to", "dhcp"}
NET_REQUIRED = ("name", "vlan", "address", "sla_id", "access", "dhcp")
MAX_MONITOR_NETS = 4  # forward の規則 140〜179 に収める


def validate_bridge(b, trunk: str) -> dict:
    """lan.bridge: VLAN-aware のブリッジ（trunk をメンバーにする）と、vif ごとの網。"""
    if not isinstance(b, dict):
        raise SiteError("lan.bridge: 「キー: 値」の並びにする")
    unknown = set(b) - {"name", "networks"}
    if unknown:
        raise SiteError(f"lan.bridge.{sorted(unknown)[0]}: 知らないキー（書き間違いでないか）")
    name = text(b.get("name", "br0"), "lan.bridge.name", BRIDGE_NAME)
    raw_nets = b.get("networks")
    if not isinstance(raw_nets, list) or not raw_nets:
        raise SiteError("lan.bridge.networks: 網を 1 つ以上並べる")
    nets = []
    for i, n in enumerate(raw_nets):
        key = f"lan.bridge.networks[{i}]"
        if not isinstance(n, dict):
            raise SiteError(f"{key}: 「キー: 値」の並びにする")
        unknown = set(n) - NET_KEYS
        if unknown:
            raise SiteError(f"{key}.{sorted(unknown)[0]}: 知らないキー（書き間違いでないか）")
        missing = [k for k in NET_REQUIRED if k not in n]
        if missing:
            raise SiteError(f"{key}.{missing[0]}: 必ず書く")
        # VLAN 1 は Linux のブリッジの既定の PVID で、VyOS も native-vlan に使わせない
        vlan = integer(n["vlan"], f"{key}.vlan", 2, 4094)
        iface = ipv4_interface(n["address"], f"{key}.address")
        access = n["access"]
        if access not in ACCESS:
            raise SiteError(f"{key}.access: {' / '.join(ACCESS)} のどれか: {access!r}")
        monitor_to = n.get("monitor_to", [])
        if not isinstance(monitor_to, list):
            raise SiteError(f"{key}.monitor_to: 網の名前の一覧にする: {monitor_to!r}")
        net_name = text(n["name"], f"{key}.name", NET_NAME)
        nets.append({
            "name": net_name,
            "vlan": vlan,
            "native": boolean(n.get("native", False), f"{key}.native"),
            "ifname": f"{name}.{vlan}",
            "path": f"bridge {name} vif {vlan}",
            "address": str(iface),
            "ip": str(iface.ip),
            "network": str(iface.network),
            # 0 は MAP-E の CE が使う。/56 の中のサブネット ID は 0〜255
            "sla_id": integer(n["sla_id"], f"{key}.sla_id", 1, 255),
            "access": access,
            "monitor_to": [text(t, f"{key}.monitor_to", NET_NAME) for t in monitor_to],
            "monitor_group": f"MON-{net_name}",
            "dhcp": validate_dhcp(n["dhcp"], f"{key}.dhcp", iface, default_subnet_id=vlan),
        })
    for field in ("name", "vlan", "sla_id"):
        values = [n[field] for n in nets]
        if len(values) != len(set(values)):
            raise SiteError(f"lan.bridge.networks: {field} が重なっている")
    subnet_ids = [n["dhcp"]["subnet_id"] for n in nets]
    if len(subnet_ids) != len(set(subnet_ids)):
        raise SiteError("lan.bridge.networks: dhcp.subnet_id が重なっている（既定は vlan）")
    for i, a in enumerate(nets):
        for c in nets[i + 1:]:
            if ipaddress.IPv4Network(a["network"]).overlaps(ipaddress.IPv4Network(c["network"])):
                raise SiteError(f"lan.bridge.networks: {a['name']}（{a['network']}）と {c['name']}（{c['network']}）が重なる")
    natives = [n for n in nets if n["native"]]
    if len(natives) > 1:
        raise SiteError(f"lan.bridge.networks: native は 1 つの網だけ: {', '.join(n['name'] for n in natives)}")
    names = {n["name"] for n in nets}
    for n in nets:
        for t in n["monitor_to"]:
            if t not in names or t == n["name"]:
                raise SiteError(f"lan.bridge.networks: {n['name']} の monitor_to に、他の網の名前を書く: {t}")
    monitor_nets = [n for n in nets if n["monitor_to"]]
    if len(monitor_nets) > MAX_MONITOR_NETS:
        raise SiteError(f"lan.bridge.networks: monitor_to を書ける網は {MAX_MONITOR_NETS} つまで")
    by_name = {n["name"]: n for n in nets}
    for i, n in enumerate(monitor_nets):
        n["monitor_rule"] = 140 + 10 * i
        n["monitor_ifnames"] = [by_name[t]["ifname"] for t in n["monitor_to"]]
    primary = natives[0] if natives else nets[0]
    return {
        "name": name,
        "trunk": trunk,
        "native_vlan": natives[0]["vlan"] if natives else None,
        "tagged": sorted(n["vlan"] for n in nets if not n["native"]),
        "nets": nets,
        "primary": primary,
        "all_access": [n["ifname"] for n in nets if n["access"] == "all"],
        "monitor_nets": monitor_nets,
    }


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

    mgmt_mode = s["mgmt"]["mode"]
    if mgmt_mode not in MGMT_MODES:
        raise SiteError(f"mgmt.mode: {' / '.join(MGMT_MODES)} のどれか: {mgmt_mode!r}")
    mgmt = {"mode": mgmt_mode, "vrf": MGMT_VRF,
            "vrf_table": integer(s["mgmt"]["vrf_table"], "mgmt.vrf_table", 100, 65535)}

    rescue = ipv4_interface(s["rescue"]["address"], "rescue.address")

    def ula_check(v: str) -> None:
        if ipaddress.IPv6Network(f"{v}::/64").network_address not in ipaddress.IPv6Network("fd00::/8"):
            raise SiteError(f"lan.ula_prefix: fd00::/8 の中にする: {v}")

    ula = placeholder_or(s["lan"]["ula_prefix"], "lan.ula_prefix", ULA_PLACEHOLDER, ULA_FORM, ula_check)

    bridge = None
    if s["lan"]["bridge"] is not None:
        given = [k for k in ("address", "dhcp") if k in (raw.get("lan") or {})]
        if given:
            raise SiteError(f"lan.{given[0]}: lan.bridge を書いたときは、網ごとの値を lan.bridge.networks に書く")
        bridge = validate_bridge(s["lan"]["bridge"], ifs["lan"])
        primary = bridge["primary"]
        lan_if = ipaddress.IPv4Interface(primary["address"])
        lan_dhcp = primary["dhcp"]
    else:
        lan_if = ipv4_interface(s["lan"]["address"], "lan.address")
        lan_dhcp = validate_dhcp(s["lan"]["dhcp"], "lan.dhcp", lan_if)
    lan_net = lan_if.network
    nets = bridge["nets"] if bridge else [{"network": str(lan_net)}]
    if mgmt_mode == "rescue":
        for n in nets:
            if rescue.network.overlaps(ipaddress.IPv4Network(n["network"])):
                raise SiteError(f"rescue.address: LAN（{n['network']}）と重ならない網にする: {rescue}")

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
    if bridge and wan_type == "pd60":
        big = [n["name"] for n in bridge["nets"] if n["sla_id"] > 15]
        if big:
            raise SiteError(f"lan.bridge.networks: 型B（/60）のサブネット ID は 1〜15: {', '.join(big)}")
    prefix64 = placeholder_or(s["wan"]["prefix64"], "wan.prefix64", WAN_PREFIX64_PLACEHOLDER, FOUR_HEXTETS)
    gw = placeholder_or(s["wan"]["ngn_gw_lla"], "wan.ngn_gw_lla", NGN_GW_LLA_PLACEHOLDER, LLA_FORM)
    duid_mac = s["wan"]["duid_ll_mac"]
    if duid_mac is not None and not isinstance(duid_mac, str):
        # yaml（1.1）は引用符の無い 12:34:56:00:00:01 のような MAC を 60 進の整数として読む
        raise SiteError(f"wan.duid_ll_mac: MAC を引用符で囲んで書く（例 \"02:00:00:00:00:01\"）: {duid_mac!r}")
    duid = None if duid_mac is None else f"{DUID_LL_PREFIX}:{text(duid_mac, 'wan.duid_ll_mac', MAC_FORM)}"

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

    ports = s["firewall"]["dscp_udp_port"]
    ports = ports if isinstance(ports, list) else [ports]
    if not ports:
        raise SiteError("firewall.dscp_udp_port: ポートを 1 つ以上書く")
    ports = [integer(p, "firewall.dscp_udp_port", 1, 65535) for p in ports]
    if len(ports) != len(set(ports)):
        raise SiteError(f"firewall.dscp_udp_port: ポートが重なっている: {ports}")
    mon_ports = s["firewall"]["monitor_ports"]
    monitor_ports = {}
    for proto in ("tcp", "udp"):
        v = mon_ports[proto]
        if not isinstance(v, list) or not v:
            raise SiteError(f"firewall.monitor_ports.{proto}: ポートを 1 つ以上並べる: {v!r}")
        monitor_ports[proto] = ",".join(str(integer(p, f"firewall.monitor_ports.{proto}", 1, 65535)) for p in v)
    iid = s["firewall"]["wireguard_inbound_iid"]
    if iid is not None:
        try:
            iid_addr = ipaddress.IPv6Address(iid)
        except (TypeError, ValueError) as e:
            raise SiteError(f"firewall.wireguard_inbound_iid: 「::」で始まる下位 64 ビットの形で書く: {iid!r}") from e
        if int(iid_addr) >> 64 or not int(iid_addr):
            raise SiteError(f"firewall.wireguard_inbound_iid: 上位 64 ビットは 0、下位 64 ビットは 0 以外にする: {iid}")
        iid = str(iid_addr)
    fw = {
        "dscp_udp_port": ",".join(map(str, ports)),
        "router_wireguard": boolean(s["firewall"]["router_wireguard"], "firewall.router_wireguard"),
        "monitor_ports": monitor_ports,
        "wireguard_inbound_iid": iid,
        "iid_mask": IID_MASK,
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
        "mgmt": mgmt,
        "lan": {"address": str(lan_if), "ip": str(lan_if.ip), "network": str(lan_net), "ula_prefix": ula,
                "dhcp": lan_dhcp, "bridge": bridge},
        "wan": {"type": wan_type, "prefix": prefix, "prefix64": prefix64, "ngn_gw_lla": gw, "duid": duid},
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
            allowed = {"20-wan-ra64.cmds": ("set ", "delete "), MIGRATE_PART: ("delete ",)}.get(name, ("set ",))
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
    bridge = ctx["lan"]["bridge"]
    # VLAN で網を分けたときは、すべての網を MAP-E で出す（網の集まり mape_calc.LAN_GROUP にまとめる）
    lan = [n["network"] for n in bridge["nets"]] if bridge else ctx["lan"]["network"]
    lines = [line for line in mape_calc.result_cmds(r, wan_if=ctx["i"]["wan"], lan=lan,
                                                    ce_plen=CE_PLEN[ctx["wan"]["type"]])
             if not line.startswith("delete ")]
    body = ("# MAP-E（v6プラス）。scripts/mape_calc.py で wan.prefix から計算した値。プレフィックスが変わったら描き直す\n"
            + "\n".join(lines) + "\n"
            + "# IPv4 の WAN 側（tun0）も flowtable に入れる（tun0 はこの段で作るので、30-firewall には書けない）\n"
            + "set firewall flowtable FT10G interface 'tun0'\n")
    return header("40-mape.cmds", source).replace("（vyos/prod/40-mape.cmds.j2）", "（scripts/mape_calc.py）") \
        + tidy("40-mape.cmds", body)


def render_migrate(others: dict[str, str], source: str) -> str:
    """01-migrate-from-lab.cmds。others（同じ commit で入れる部分）が set する所は消さない。"""
    sets = [config_words(line)[1:] for text_ in others.values() for line in text_.splitlines()
            if line.startswith("set ")]

    def under(path: list[str]) -> bool:
        return any(s[:len(path)] == path for s in sets)

    deletes, kept = [], []
    for raw, cands in zip(MIGRATE_DELETE, MIGRATE_DELETE_WORDS):
        line = next((line for line, c in zip(raw, cands) if not under(c)), None)
        if line is None:
            kept.append(f"# 消さない（他の部分が set する）: {raw[-1]}")
        else:
            deletes.append(f"delete {line}")
    for raw, leaf in zip(MIGRATE_LEAVES, MIGRATE_LEAVES_WORDS):
        if not any(s[:-1] == leaf[:-1] for s in sets):
            deletes.append(f"delete {raw}")
    body = ("# 検証環境の状態（configs/pc02/baseline.cmds: フェーズ1 と VRF MGMT）から本番へ移る最初の commit だけで、\n"
            "# 本番の部分と同じ commit で入れる。2 回目からは描かない（--from-lab を付けない）: 無い所を delete すると\n"
            "# VyOS が拒否し、scripts/pc02-commit.sh が失敗する。\n"
            "# 他の部分が set する所とその上は消さない（部分をつなぐ順によらず同じ結果になる）。\n"
            "# 1 つの値の葉（説明・ホスト名）は、他の部分が set して上書きするなら消さない\n"
            + "\n".join(deletes + kept) + "\n")
    return (f"# 生成: scripts/render_config.py（site: {source}、VyOS {vyos_version()}）。"
            f"手で直さず、scripts/render_config.py の MIGRATE_* か site を直して描き直す。"
            f"消す元は {LAB_BASELINE}\n") + tidy(MIGRATE_PART, body)


# ---- コマンドライン ------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="本番の PC02 の VyOS の設定を site の yaml から描く（vyos/prod/*.cmds.j2）")
    p.add_argument("site", help="site の yaml（例 site/example.yaml。実際の値は site/<名前>.local.yaml）")
    out = p.add_mutually_exclusive_group(required=True)
    out.add_argument("-o", "--output", help="部分ごとの .cmds を書き出すディレクトリ")
    out.add_argument("--check", help="描いた結果が、このディレクトリの .cmds と一致するかだけを見る")
    p.add_argument("--all-wan-types", action="store_true", help="WAN の部分を 3 つの型すべて描く（例の置き場所用）")
    p.add_argument("--mape", action="store_true", help="wan.prefix から MAP-E の段（40-mape.cmds）も描く")
    p.add_argument("--from-lab", action="store_true",
                   help=f"検証環境の状態（{LAB_BASELINE}）から移る最初の commit 用に、{MIGRATE_PART} も描く")
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
        if args.from_lab:
            # 同じ commit で入れる部分（MAP-E の段を含む）がそろってから描く
            files[MIGRATE_PART] = render_migrate(files, source=source)
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
