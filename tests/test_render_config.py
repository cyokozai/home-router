"""scripts/render_config.py（本番の PC02 の設定を site の yaml と vyos/prod/*.cmds.j2 から作る）の検査。

本番の構成: 光クロス 10G・ひかり電話なし・v6プラス。WAN は型A（pd56、ONU 直結）が既定で、型B（pd60、HGW 配下）・
型C（ra64、RA の /64 と ND proxy）も選べる。出力は部分ごとの .cmds で、MAP-E の実値は接続後に別の段（40-mape）で作る。

- 例（site/example.yaml）を描いた結果が vyos/prod/example/ と一致すること（CI で再生成と一致を確かめる）
- 検証済みの形（vyos/*.cmds）を使っていること、RPS・QoS の shaper・VRF を使わないこと
- 秘密の値やグローバルなアドレスが出力に入らないこと（印 @…@ のまま出し、投入時に置き換える）
- site の値の誤りを、描く前に止めること
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
VYOS = ROOT / "vyos"
PROD = VYOS / "prod"
EXAMPLE_DIR = PROD / "example"
EXAMPLE_SITE = ROOT / "site" / "example.yaml"
RENDER = SCRIPTS / "render_config.py"
WORKFLOW = ROOT / ".github" / "workflows" / "vyos-config.yaml"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rc = load("render_config", RENDER)
mc = load("mape_calc", SCRIPTS / "mape_calc.py")
sens = load("test_no_sensitive_data_for_render", ROOT / "tests" / "test_no_sensitive_data.py")

WAN_TYPES = ["pd56", "pd60", "ra64"]
COMMON = ["00-base.cmds", "10-lan.cmds", "20-wan-common.cmds", "30-firewall.cmds", "50-monitor.cmds"]
KNOWN_PLACEHOLDERS = {"ULA_PREFIX", "SNMP_COMMUNITY", "SSH_PUBKEY", "WAN_PREFIX64", "NGN_GW_LLA"}
PLACEHOLDER = re.compile(r"@([A-Z][A-Z0-9_]*)@")
# CI のコンテナで検査する、型によらない部分をつないだファイルの名前（vyos/container-unsupported.txt の 1 列目）
PROD_EXAMPLE = "prod-example.cmds"
# 例で使う MAP-E の委任プレフィックス（tests/test_mape_calc.py の既知の例と同じ、作った値）
MAPE_PREFIX = "240b:10:aabb:cc00::/56"
DELETE = object()  # site_with でキーを消す印


def example() -> dict:
    return rc.load_site(EXAMPLE_SITE)


def site_with(**changes) -> dict:
    """例の site を元に、「a.b」形式のキーで値を差し替えたもの（検証前の生の dict）。"""
    import copy
    import yaml

    raw = yaml.safe_load(EXAMPLE_SITE.read_text(encoding="utf-8"))
    raw = copy.deepcopy(raw)
    for dotted, value in changes.items():
        node = raw
        *parents, last = dotted.split(".")
        for key in parents:
            node = node.setdefault(key, {})
        if value is DELETE:
            node.pop(last, None)
        else:
            node[last] = value
    return raw


def parts(site: dict | None = None, **kw) -> dict[str, str]:
    return rc.render(rc.validate(site if site is not None else site_with()), source="example.yaml", **kw)


def commands(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def all_commands(p: dict[str, str]) -> list[str]:
    return [line for name in sorted(p) for line in commands(p[name])]


def file_commands(path: Path) -> list[str]:
    return commands(path.read_text(encoding="utf-8"))


# ---- 構成 -----------------------------------------------------------------------------


def test_example_site_renders_the_parts_for_type_a():
    assert list(parts()) == ["00-base.cmds", "10-lan.cmds", "20-wan-common.cmds", "20-wan-pd56.cmds",
                             "30-firewall.cmds", "50-monitor.cmds"]


@pytest.mark.parametrize("wan_type", WAN_TYPES)
def test_wan_type_selects_one_wan_part(wan_type):
    names = list(parts(site_with(**{"wan.type": wan_type})))
    assert [n for n in names if n.startswith("20-wan-") and n != "20-wan-common.cmds"] == [f"20-wan-{wan_type}.cmds"]


def test_all_wan_types_renders_every_wan_part():
    names = set(parts(all_wan_types=True))
    assert names == set(COMMON) | {f"20-wan-{t}.cmds" for t in WAN_TYPES}


def test_example_dir_matches_the_renderer():
    # vyos/prod/example/ は手で直さない。テンプレートや site/example.yaml を変えたら描き直す
    rendered = parts(all_wan_types=True)
    on_disk = {p.name: p.read_text(encoding="utf-8") for p in EXAMPLE_DIR.glob("*.cmds")}
    assert on_disk == rendered


def test_every_template_is_used():
    used = {name + ".j2" for name in parts(all_wan_types=True)}
    assert {p.name for p in PROD.glob("*.cmds.j2")} == used


def test_header_names_the_vyos_version_and_the_source():
    version = re.search(r"^VYOS_VERSION=(\S+)$", (VYOS / "VERSION").read_text(encoding="utf-8"), re.M).group(1)
    for name, text in parts(all_wan_types=True).items():
        head = text.splitlines()[0]
        assert head.startswith("#") and version in head and "example.yaml" in head, name


def test_rendering_is_deterministic():
    assert parts(all_wan_types=True) == parts(all_wan_types=True)


@pytest.mark.parametrize("wan_type", WAN_TYPES)
def test_only_set_lines_and_no_duplicates(wan_type):
    p = parts(site_with(**{"wan.type": wan_type}))
    lines = all_commands(p)
    for name, text in p.items():
        for line in commands(text):
            # 型C だけは、共通の部分が入れた autoconf を消す（RA を受けない）
            allowed = ("set ",) + (("delete ",) if name == "20-wan-ra64.cmds" else ())
            assert line.startswith(allowed), f"{name}: {line}"
            assert "#" not in line, f"{name}: 値に # がある（行末コメントと読まれる）: {line}"
    assert len(lines) == len(set(lines))


def test_no_rps_no_qos_shaper_no_vrf():
    # 実機の試験で、RPS と QoS の shaper は 64B の転送性能を半分にした。本番では VRF MGMT を使わない
    for line in all_commands(parts(all_wan_types=True)):
        assert "offload rps" not in line
        assert not line.startswith("set qos")
        assert " vrf " not in line and "MGMT" not in line


def test_no_password_lines():
    for line in all_commands(parts(all_wan_types=True)):
        assert "plaintext-password" not in line and "encrypted-password" not in line


# ---- 中身（既定値） ----------------------------------------------------------------------

BASE = [
    "set system host-name 'home-router'",
    "set system time-zone 'Asia/Tokyo'",
    "set system option performance 'network-throughput'",
    "set system conntrack table-size '1048576'",
    "set system conntrack hash-size '262144'",
    "set service ntp server 'ntp.nict.jp'",
    "set service ntp server 'time.google.com'",
    "set interfaces ethernet eth0 description 'RESCUE'",
    "set interfaces ethernet eth0 address '192.168.255.1/24'",
    "set system login user vyos authentication public-keys admin type 'ssh-ed25519'",
    "set system login user vyos authentication public-keys admin key '@SSH_PUBKEY@'",
    "set service ssh listen-address '192.168.2.1'",
    "set service ssh listen-address '192.168.255.1'",
    "set service ssh disable-password-authentication",
]
LAN_NET = "set service dhcp-server shared-network-name LAN subnet 192.168.2.0/24"
LAN = [
    "set interfaces ethernet eth1 description 'LAN'",
    "set interfaces ethernet eth1 address '192.168.2.1/24'",
    "set interfaces ethernet eth1 address '@ULA_PREFIX@::1/64'",
    f"{LAN_NET} subnet-id '1'",
    f"{LAN_NET} option default-router '192.168.2.1'",
    f"{LAN_NET} option name-server '192.168.2.1'",
    f"{LAN_NET} range 0 start '192.168.2.100'",
    f"{LAN_NET} range 0 stop '192.168.2.199'",
    "set service dns forwarding listen-address '192.168.2.1'",
    "set service dns forwarding listen-address '@ULA_PREFIX@::1'",
    "set service dns forwarding allow-from '192.168.2.0/24'",
    "set service dns forwarding allow-from '@ULA_PREFIX@::/64'",
    "set service dns forwarding allow-from '::/0'",
    "set service dns forwarding dhcp 'eth2'",
    "set service router-advert interface eth1 prefix '::/64'",
    "set service router-advert interface eth1 name-server '@ULA_PREFIX@::1'",
]
WAN_COMMON = [
    "set interfaces ethernet eth2 description 'WAN-NGN'",
    "set interfaces ethernet eth2 ipv6 address autoconf",
]
WAN = {
    "pd56": ["set interfaces ethernet eth2 dhcpv6-options pd 0 length '56'",
             "set interfaces ethernet eth2 dhcpv6-options pd 0 interface eth1 sla-id '1'",
             "set interfaces ethernet eth2 dhcpv6-options pd 0 interface eth1 address '1'"],
    "pd60": ["set interfaces ethernet eth2 dhcpv6-options pd 0 length '60'",
             "set interfaces ethernet eth2 dhcpv6-options pd 0 interface eth1 sla-id '1'",
             "set interfaces ethernet eth2 dhcpv6-options pd 0 interface eth1 address '1'"],
    "ra64": ["delete interfaces ethernet eth2 ipv6 address autoconf",
             "set interfaces ethernet eth1 address '@WAN_PREFIX64@::1/64'",
             "set protocols static route6 ::/0 next-hop '@NGN_GW_LLA@' interface 'eth2'",
             "set service ndp-proxy interface eth2 prefix '@WAN_PREFIX64@::/64' mode 'static'"],
}
FIREWALL = [
    "set firewall ipv6 forward filter rule 5 action 'accept'",
    "set firewall ipv6 forward filter rule 5 protocol 'udp'",
    "set firewall ipv6 forward filter rule 5 source port '51820'",
    "set firewall ipv6 forward filter rule 5 set dscp '0'",
    "set firewall ipv4 input filter default-action 'drop'",
    "set firewall ipv4 input filter rule 10 action 'accept'",
    "set firewall ipv4 input filter rule 10 state 'established'",
    "set firewall ipv4 input filter rule 10 state 'related'",
    "set firewall ipv4 input filter rule 20 action 'accept'",
    "set firewall ipv4 input filter rule 20 protocol 'icmp'",
    "set firewall ipv4 input filter rule 30 action 'accept'",
    "set firewall ipv4 input filter rule 30 inbound-interface name 'eth1'",
    "set firewall ipv4 input filter rule 40 action 'accept'",
    "set firewall ipv4 input filter rule 40 inbound-interface name 'eth0'",
    "set firewall ipv6 input filter default-action 'drop'",
    "set firewall ipv6 input filter rule 10 action 'accept'",
    "set firewall ipv6 input filter rule 10 state 'established'",
    "set firewall ipv6 input filter rule 10 state 'related'",
    "set firewall ipv6 input filter rule 20 action 'accept'",
    "set firewall ipv6 input filter rule 20 protocol 'ipv6-icmp'",
    "set firewall ipv6 input filter rule 30 action 'accept'",
    "set firewall ipv6 input filter rule 30 protocol 'udp'",
    "set firewall ipv6 input filter rule 30 source address 'fe80::/10'",
    "set firewall ipv6 input filter rule 30 destination port '546'",
    "set firewall ipv6 input filter rule 40 action 'accept'",
    "set firewall ipv6 input filter rule 40 inbound-interface name 'eth1'",
    "set firewall ipv6 input filter rule 50 action 'accept'",
    "set firewall ipv6 input filter rule 50 protocol 'ipencap'",
    "set firewall ipv6 input filter rule 50 source address '2404:9200:225:100::64'",
]
MONITOR = [
    "set service snmp community '@SNMP_COMMUNITY@' authorization 'ro'",
    "set service snmp community '@SNMP_COMMUNITY@' network '192.168.2.50/32'",
    "set service monitoring prometheus node-exporter listen-address '192.168.2.1'",
]


@pytest.mark.parametrize(("name", "expected"), [
    ("00-base.cmds", BASE), ("10-lan.cmds", LAN), ("20-wan-common.cmds", WAN_COMMON),
    ("30-firewall.cmds", FIREWALL), ("50-monitor.cmds", MONITOR),
] + [(f"20-wan-{t}.cmds", lines) for t, lines in WAN.items()])
def test_part_has_the_expected_lines(name, expected):
    got = commands(parts(all_wan_types=True)[name])
    missing = [line for line in expected if line not in got]
    assert not missing, f"{name} に無い行:\n" + "\n".join(missing)


def test_lan_never_uses_subnet_id_0_of_the_delegated_prefix():
    # サブネット ID 0 は MAP-E の CE 用
    for t in ("pd56", "pd60"):
        assert "sla-id '0'" not in parts(all_wan_types=True)[f"20-wan-{t}.cmds"]


def test_dhcp_static_mappings_come_from_the_site():
    lan = commands(parts()["10-lan.cmds"])
    assert f"{LAN_NET} static-mapping monitor ip-address '192.168.2.50'" in lan
    assert f"{LAN_NET} static-mapping monitor mac '02:00:00:00:00:50'" in lan


def test_dscp_rewrite_comes_before_offload_and_only_toward_the_wan():
    fw = commands(parts()["30-firewall.cmds"])
    # 先に offload されると、以後のパケットは forward の規則を通らず DSCP が書き換わらない
    assert fw.index("set firewall ipv6 forward filter rule 5 set dscp '0'") < \
        fw.index("set firewall ipv6 forward filter rule 10 action 'offload'")
    # WAN から来た送信元ポート 51820 の新しい通信を、ステートフルの判定より前に通さない
    assert "set firewall ipv6 forward filter rule 5 outbound-interface name 'eth2'" in fw


def test_router_wireguard_output_dscp_is_optional():
    off = commands(parts()["30-firewall.cmds"])
    assert not [line for line in off if " output " in line]
    on = commands(parts(site_with(**{"firewall.router_wireguard": True}))["30-firewall.cmds"])
    for line in file_commands(VYOS / "dscp-firewall.cmds"):
        if " output " in line:
            assert line in on


# ---- 検証済みの形を使っていること ------------------------------------------------------


@pytest.mark.parametrize(("src", "part", "skip"), [
    ("tuning-1-offload.cmds", "00-base.cmds", ()),
    ("fw-stateful.cmds", "30-firewall.cmds", ()),
    ("tuning-3-flowtable.cmds", "30-firewall.cmds", ()),
    ("dscp-firewall.cmds", "30-firewall.cmds", (" output ",)),
    ("snmp-mgmt.cmds", "50-monitor.cmds", (" vrf ",)),
])
def test_verified_lines_are_reused(src, part, skip):
    got = commands(parts()[part])
    for line in file_commands(VYOS / src):
        if any(s in line for s in skip):
            continue
        assert line in got, f"vyos/{src} の行が {part} に無い: {line}"


def test_lab_dscp_rule_is_renumbered_to_5():
    # flowtable の offload（rule 10）と同じ番号だと、組み合わせたときに 1 本の規則に混ざる
    lines = file_commands(VYOS / "dscp-firewall.cmds")
    assert lines and all(" rule 5 " in line for line in lines)


# ---- 秘密の値とアドレス ----------------------------------------------------------------


def test_example_output_passes_the_sensitive_data_check():
    for name, text in parts(all_wan_types=True).items():
        assert sens.find_sensitive(text) == [], name


def test_placeholders_are_all_known():
    found = set(PLACEHOLDER.findall("\n".join(parts(all_wan_types=True).values())))
    assert found == KNOWN_PLACEHOLDERS


def test_site_values_replace_placeholders():
    p = parts(site_with(**{"lan.ula_prefix": "fd12:3456:789a:1", "wan.type": "ra64",
                           "wan.prefix64": "2001:db8:0:1", "wan.ngn_gw_lla": "fe80::1"}))
    text = "\n".join(all_commands(p))
    assert "@ULA_PREFIX@" not in text and "@WAN_PREFIX64@" not in text and "@NGN_GW_LLA@" not in text
    assert "set interfaces ethernet eth1 address 'fd12:3456:789a:1::1/64'" in text
    assert "set protocols static route6 ::/0 next-hop 'fe80::1' interface 'eth2'" in text


def test_interface_names_and_lan_address_come_from_the_site():
    p = parts(site_with(**{"interfaces.lan": "eth5", "interfaces.wan": "eth6", "interfaces.rescue": "eth7",
                           "lan.address": "10.20.30.1/24", "lan.dhcp.range.start": "10.20.30.100",
                           "lan.dhcp.range.stop": "10.20.30.199", "lan.dhcp.static_mappings": [],
                           "monitor.snmp.network": "10.20.30.50/32"}))
    text = "\n".join(all_commands(p))
    for old in ("eth0", "eth1", "eth2", "192.168.2."):
        assert old not in text, old
    assert "set interfaces ethernet eth5 address '10.20.30.1/24'" in text


def test_snmp_and_node_exporter_are_optional():
    p = parts(site_with(**{"monitor.snmp.enabled": False, "monitor.node_exporter": False}))
    assert "50-monitor.cmds" not in p


# ---- site の誤り ------------------------------------------------------------------------


@pytest.mark.parametrize(("changes", "words"), [
    ({"wan.type": DELETE}, "wan.type"),
    ({"wan.type": "pppoe"}, "wan.type"),
    ({"host_name": "a'b"}, "host_name"),
    ({"unknown_key": 1}, "unknown_key"),
    ({"lan.typo": 1}, "lan.typo"),
    ({"lan.address": "192.168.2.0/24"}, "lan.address"),
    ({"lan.address": "not-an-address"}, "lan.address"),
    ({"lan.dhcp.range.start": "192.168.3.100"}, "lan.dhcp.range"),
    ({"lan.dhcp.range.start": "192.168.2.200"}, "lan.dhcp.range"),
    ({"lan.dhcp.static_mappings": [{"name": "x", "mac": "zz", "ip": "192.168.2.50"}]}, "mac"),
    ({"lan.dhcp.static_mappings": [{"name": "x", "mac": "02:00:00:00:00:01", "ip": "10.0.0.1"}]}, "ip"),
    ({"lan.ula_prefix": "2001:db8:0:1"}, "lan.ula_prefix"),
    ({"lan.ula_prefix": "fd12:3456::"}, "lan.ula_prefix"),
    ({"wan.prefix64": "fd12::/64", "wan.type": "ra64"}, "wan.prefix64"),
    ({"wan.ngn_gw_lla": "2001:db8::1", "wan.type": "ra64"}, "wan.ngn_gw_lla"),
    ({"interfaces.lan": "eth2"}, "interfaces"),
    ({"interfaces.wan": "wan0"}, "interfaces.wan"),
    ({"monitor.snmp.network": "bad"}, "monitor.snmp.network"),
    ({"ntp_servers": []}, "ntp_servers"),
    ({"wan.prefix": "2001:db8::/56"}, "wan.prefix"),
])
def test_bad_site_values_are_rejected(changes, words):
    with pytest.raises(rc.SiteError) as e:
        rc.validate(site_with(**changes))
    assert words in str(e.value)


# ---- MAP-E の段（接続後に、prefix が分かってから） ------------------------------------------


@pytest.mark.parametrize(("wan_type", "plen"), [("pd56", 64), ("pd60", 64), ("ra64", 128)])
def test_mape_part_reuses_mape_calc(wan_type, plen):
    site = rc.validate(site_with(**{"wan.type": wan_type, "wan.prefix": MAPE_PREFIX}))
    got = commands(rc.render_mape(site, source="example.yaml"))
    r = mc.calc(MAPE_PREFIX)
    expected = [line for line in mc.result_cmds(r, wan_if="eth2", lan="192.168.2.0/24", ce_plen=plen)
                if not line.startswith("delete ")]
    assert got[:len(expected)] == expected
    assert f"set interfaces ethernet eth2 address '{r.ce}/{plen}'" in got
    # IPv4 の WAN 側（tun0）も flowtable に入れる
    assert got[len(expected):] == ["set firewall flowtable FT10G interface 'tun0'"]


def test_mape_part_needs_the_prefix():
    with pytest.raises(rc.SiteError, match="wan.prefix"):
        rc.render_mape(rc.validate(site_with()), source="example.yaml")


# ---- コマンドライン ----------------------------------------------------------------------


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(RENDER), *args], capture_output=True, text=True)


def test_cli_writes_the_parts(tmp_path):
    out = run(str(EXAMPLE_SITE), "-o", str(tmp_path), "--all-wan-types")
    assert out.returncode == 0, out.stderr
    assert {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir()} == parts(all_wan_types=True)


def test_cli_check_compares_with_the_example_dir(tmp_path):
    assert run(str(EXAMPLE_SITE), "--check", str(EXAMPLE_DIR), "--all-wan-types").returncode == 0
    for p in EXAMPLE_DIR.glob("*.cmds"):
        (tmp_path / p.name).write_text(p.read_text(encoding="utf-8") + "set system host-name 'x'\n",
                                       encoding="utf-8")
    out = run(str(EXAMPLE_SITE), "--check", str(tmp_path), "--all-wan-types")
    assert out.returncode == 1 and "00-base.cmds" in out.stderr


def test_cli_rejects_bad_site_without_writing(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("host_name: x\nwan: {type: pppoe}\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    out = run(str(bad), "-o", str(out_dir))
    assert out.returncode == 2
    assert "wan.type" in out.stderr
    assert not out_dir.exists()


def test_cli_mape_writes_40_mape(tmp_path):
    site = tmp_path / "home.local.yaml"
    site.write_text(EXAMPLE_SITE.read_text(encoding="utf-8").replace("type: pd56", "type: pd56\n  prefix: "
                                                                      + MAPE_PREFIX), encoding="utf-8")
    out = run(str(site), "-o", str(tmp_path / "out"), "--mape")
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "out" / "40-mape.cmds").exists()


# ---- 置き場所と CI ------------------------------------------------------------------------


def test_real_site_files_are_gitignored():
    patterns = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert "site/*.local.yaml" in patterns
    assert "site/*.local/" in patterns


def test_container_unsupported_entries_for_prod_point_to_real_lines():
    lines = [line for name in COMMON for line in file_commands(EXAMPLE_DIR / name)]
    entries = [e for e in (raw.split(" | ") for raw in (VYOS / "container-unsupported.txt")
                           .read_text(encoding="utf-8").splitlines() if raw.strip() and not raw.startswith("#"))
               if e[0].strip() == PROD_EXAMPLE]
    assert entries
    for _, line, reason in entries:
        assert line.strip() in lines, line
        assert reason.strip()


def test_workflow_renders_and_checks_every_wan_type():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/render_config.py" in text
    assert "scripts/fill-placeholders.sh" in text
    assert PROD_EXAMPLE in text
    for t in WAN_TYPES:
        assert f"20-wan-{t}.cmds" in text
    assert "40-mape.cmds" in text
