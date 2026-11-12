"""scripts/render_config.py の、VLAN で網を分ける形（lan.bridge）と VRF MGMT を残す形（mgmt.mode: vrf）の検査。

2026-11-12 に Yusuke が決めたこと:
- LAN 側（eth1）は VLAN-aware のブリッジにして、家族用・自分用・監視用・来客用・Nekko Cloud の 5 つの網に分ける（VXLAN はまだ入れない）
- 当日は VRF MGMT を残す（eth0 は VRF MGMT で DHCP、SSH・NTP・node-exporter は VRF MGMT の中）。既定は今のまま rescue
- DSCP は firewall の set dscp '0'。対象のポートは 51820 と 51821（単数の書き方も受ける）
- 5 つの網すべてを MAP-E で出す

例（site/example-vlan.yaml）を描いた結果が vyos/prod/example-vlan/ と一致すること（CI で再生成と一致を確かめる）、
VyOS の set だけであること、site の誤りを描く前に止めること、を見る。commit できるかは CI のコンテナで見る。
"""

import copy
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PROD = ROOT / "vyos" / "prod"
VLAN_DIR = PROD / "example-vlan"
VLAN_SITE = ROOT / "site" / "example-vlan.yaml"
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
sens = load("test_no_sensitive_data_for_vlan", ROOT / "tests" / "test_no_sensitive_data.py")

WAN_TYPES = ["pd56", "pd60", "ra64"]
COMMON = ["00-base.cmds", "10-lan.cmds", "20-wan-common.cmds", "30-firewall.cmds", "50-monitor.cmds"]
MAPE_PREFIX = "240b:10:aabb:cc00::/56"
NETS = {  # 名前: (VLAN, 網, sla_id)
    "FAMILY": (10, "192.168.2.0/24", 1),
    "SELF": (20, "192.168.20.0/24", 2),
    "MONITOR": (30, "192.168.30.0/24", 3),
    "GUEST": (40, "192.168.40.0/24", 4),
    "NEKKO": (50, "192.168.50.0/29", 5),
}
DELETE = object()


def raw_site(path: Path = VLAN_SITE) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def site_with(base: Path = VLAN_SITE, **changes) -> dict:
    raw = copy.deepcopy(raw_site(base))
    for dotted, value in changes.items():
        node = raw
        *parents, last = dotted.split(".")
        for key in parents:
            node = node[int(key)] if isinstance(node, list) else node.setdefault(key, {})
        if value is DELETE:
            node.pop(last, None)
        elif isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value
    return raw


def parts(site: dict | None = None, **kw) -> dict[str, str]:
    return rc.render(rc.validate(site if site is not None else site_with()), source="example-vlan.yaml", **kw)


def commands(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def all_commands(p: dict[str, str]) -> list[str]:
    return [line for name in sorted(p) for line in commands(p[name])]


# ---- 例と構成 ---------------------------------------------------------------------------


def test_example_vlan_dir_matches_the_renderer():
    # vyos/prod/example-vlan/ は手で直さない。テンプレートや site/example-vlan.yaml を変えたら描き直す
    rendered = parts(all_wan_types=True)
    on_disk = {p.name: p.read_text(encoding="utf-8") for p in VLAN_DIR.glob("*.cmds")}
    assert on_disk == rendered


def test_example_vlan_renders_every_part():
    assert set(parts(all_wan_types=True)) == set(COMMON) | {f"20-wan-{t}.cmds" for t in WAN_TYPES}


@pytest.mark.parametrize("wan_type", WAN_TYPES)
def test_only_set_lines_and_no_duplicates(wan_type):
    p = parts(site_with(**{"wan.type": wan_type}))
    lines = all_commands(p)
    for name, text in p.items():
        for line in commands(text):
            allowed = ("set ",) + (("delete ",) if name == "20-wan-ra64.cmds" else ())
            assert line.startswith(allowed), f"{name}: {line}"
            assert "#" not in line, f"{name}: {line}"
    assert len(lines) == len(set(lines))


def test_example_vlan_passes_the_sensitive_data_check():
    for name, text in parts(all_wan_types=True).items():
        assert sens.find_sensitive(text) == [], name


def test_no_rps_no_qos_shaper():
    for line in all_commands(parts(all_wan_types=True)):
        assert "offload rps" not in line
        assert not line.startswith("set qos")


# ---- ブリッジと網 -------------------------------------------------------------------------


def test_eth1_is_a_vlan_aware_bridge_member_with_native_vlan_10():
    lan = commands(parts()["10-lan.cmds"])
    assert "set interfaces bridge br0 enable-vlan" in lan
    assert "set interfaces bridge br0 member interface eth1 native-vlan '10'" in lan
    allowed = sorted(line for line in lan if " allowed-vlan " in line)
    assert allowed == sorted(f"set interfaces bridge br0 member interface eth1 allowed-vlan '{v}'"
                             for v in (20, 30, 40, 50))
    # ブリッジのメンバーにはアドレスを付けない（VyOS が拒否する）
    assert not [x for x in all_commands(parts(all_wan_types=True)) if x.startswith("set interfaces ethernet eth1 address")]


def test_each_network_has_a_vif_dhcp_dns_and_ra():
    lan = commands(parts()["10-lan.cmds"])
    for name, (vlan, net, _) in NETS.items():
        gw = net.split("/")[0][:-1] + "1"
        plen = net.split("/")[1]
        assert f"set interfaces bridge br0 vif {vlan} address '{gw}/{plen}'" in lan
        assert f"set interfaces bridge br0 vif {vlan} description '{name}'" in lan
        dhcp = f"set service dhcp-server shared-network-name {name} subnet {net}"
        assert f"{dhcp} option default-router '{gw}'" in lan
        assert f"{dhcp} option name-server '{gw}'" in lan
        assert f"{dhcp} subnet-id '{vlan}'" in lan
        assert f"set service dns forwarding listen-address '{gw}'" in lan
        assert f"set service dns forwarding allow-from '{net}'" in lan
        assert f"set service router-advert interface br0.{vlan} prefix '::/64' deprecate-prefix" in lan
        assert f"set service router-advert interface br0.{vlan} name-server '@ULA_PREFIX@::1'" in lan
    # ULA は家族用の網にだけ
    assert [x for x in lan if "::1/64" in x] == ["set interfaces bridge br0 vif 10 address '@ULA_PREFIX@::1/64'"]


def test_family_dhcp_range_leaves_out_the_core_router_and_pc02_mgmt():
    site = rc.validate(site_with())
    fam = site["lan"]["bridge"]["primary"]
    assert fam["name"] == "FAMILY"
    start, stop = (int(fam["dhcp"][k].rsplit(".", 1)[1]) for k in ("start", "stop"))
    assert (start, stop) == (100, 199)
    assert not start <= 211 <= stop and not start <= 14 <= stop
    assert {"name": "pc02-mgmt", "mac": "02:00:00:00:00:14", "ip": "192.168.2.14"} in fam["dhcp"]["static_mappings"]


@pytest.mark.parametrize("wan_type", ["pd56", "pd60"])
def test_prefix_delegation_gives_each_vif_its_sla_id(wan_type):
    pd = commands(parts(site_with(**{"wan.type": wan_type}))[f"20-wan-{wan_type}.cmds"])
    for vlan, _, sla in NETS.values():
        assert f"set interfaces ethernet eth2 dhcpv6-options pd 0 interface br0.{vlan} sla-id '{sla}'" in pd
        assert f"set interfaces ethernet eth2 dhcpv6-options pd 0 interface br0.{vlan} address '1'" in pd
    assert not [x for x in pd if " interface eth1 " in x]
    assert not [x for x in pd if x.endswith("sla-id '0'")]


def test_type_c_puts_the_single_64_on_the_family_vif():
    ra = commands(parts(site_with(**{"wan.type": "ra64"}))["20-wan-ra64.cmds"])
    assert "set interfaces bridge br0 vif 10 address '@WAN_PREFIX64@::1/64'" in ra
    assert not [x for x in ra if "ethernet eth1" in x]


# ---- 管理系（VRF MGMT） --------------------------------------------------------------------


MGMT_VRF_LINES = [
    "set vrf name MGMT table '100'",
    "set interfaces ethernet eth0 address 'dhcp'",
    "set interfaces ethernet eth0 vrf 'MGMT'",
    "set service ssh vrf 'MGMT'",
    "set service ssh disable-password-authentication",
    "set service ntp vrf 'MGMT'",
]


def test_vrf_mode_keeps_mgmt_like_mgmt_home():
    p = parts()
    base = commands(p["00-base.cmds"])
    for line in MGMT_VRF_LINES:
        assert line in base, line
    # vyos/mgmt-home.cmds の set の行（NTP サーバと説明を除く）を使っている
    home = [x for x in commands((ROOT / "vyos" / "mgmt-home.cmds").read_text(encoding="utf-8"))
            if x.startswith("set ") and " ntp server " not in x and "node-exporter" not in x]
    assert set(home) <= set(base)
    assert not [x for x in base if "listen-address" in x or "192.168.255." in x]
    mon = commands(p["50-monitor.cmds"])
    assert "set service snmp vrf 'MGMT'" in mon
    assert "set service monitoring prometheus node-exporter vrf 'MGMT'" in mon
    assert not [x for x in mon if "listen-address" in x]
    fw = commands(p["30-firewall.cmds"])
    assert "set firewall ipv4 input filter rule 40 inbound-interface name 'eth0'" in fw
    assert "set firewall ipv4 input filter rule 45 inbound-interface name 'MGMT'" in fw


def test_rescue_mode_is_the_default_and_draws_no_vrf():
    p = parts(site_with(**{"mgmt": DELETE}))
    text = all_commands(p)
    assert not [x for x in text if " vrf " in x or "MGMT" in x]
    assert "set interfaces ethernet eth0 address '192.168.255.1/24'" in text
    assert "set service ssh listen-address '192.168.2.1'" in text
    assert "set service monitoring prometheus node-exporter listen-address '192.168.2.1'" in text


def test_vrf_mode_also_works_with_a_single_lan():
    raw = yaml.safe_load(EXAMPLE_SITE.read_text(encoding="utf-8"))
    raw["mgmt"] = {"mode": "vrf"}
    text = all_commands(parts(raw))
    for line in MGMT_VRF_LINES:
        assert line in text, line
    assert "set interfaces ethernet eth1 address '192.168.2.1/24'" in text


def test_migrate_keeps_what_vrf_mode_sets():
    p = parts()
    migrate = rc.render_migrate(p, source="example-vlan.yaml")
    deletes = [x for x in commands(migrate) if x.startswith("delete ")]
    assert not [x for x in deletes if "vrf" in x or "eth0 address 'dhcp'" in x]
    # 検証用のアドレスと経路、前の NTP サーバは消す
    for line in ("delete interfaces ethernet eth1 address '192.168.10.1/24'",
                 "delete interfaces ethernet eth2 address '192.168.20.1/30'",
                 "delete protocols static route 0.0.0.0/0",
                 "delete service ntp server '192.168.2.13'"):
        assert line in deletes, line


# ---- firewall -----------------------------------------------------------------------------


def fw_lines() -> list[str]:
    return commands(parts()["30-firewall.cmds"])


def rule(lines: list[str], family: str, n: int) -> list[str]:
    head = f"set firewall {family} forward filter rule {n} "
    return [x[len(head):] for x in lines if x.startswith(head)]


def test_interface_groups_cover_the_vifs():
    fw = fw_lines()
    assert sorted(x for x in fw if "interface-group LAN interface" in x) == sorted(
        f"set firewall group interface-group LAN interface 'br0.{v}'" for v, _, _ in NETS.values())
    assert [x for x in fw if "interface-group LAN-ALL " in x] == [
        "set firewall group interface-group LAN-ALL interface 'br0.20'"]
    assert sorted(x for x in fw if "interface-group MON-MONITOR " in x) == [
        "set firewall group interface-group MON-MONITOR interface 'br0.10'",
        "set firewall group interface-group MON-MONITOR interface 'br0.50'"]
    # inbound-interface name に LAN の物理の口（vif に一致しない）を使わない
    assert not [x for x in fw if "inbound-interface name 'eth1'" in x]


@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
def test_forward_policy_between_networks(family):
    fw = fw_lines()
    assert rule(fw, family, 120) == ["action 'accept'", "inbound-interface group 'LAN-ALL'"]
    icmp = "icmp" if family == "ipv4" else "ipv6-icmp"
    assert rule(fw, family, 140) == ["action 'accept'", "inbound-interface name 'br0.30'",
                                     "outbound-interface group 'MON-MONITOR'", f"protocol '{icmp}'"]
    assert rule(fw, family, 141)[-2:] == ["protocol 'udp'", "destination port '161'"]
    assert rule(fw, family, 142)[-2:] == ["protocol 'tcp'", "destination port '9100'"]
    assert rule(fw, family, 290) == ["action 'drop'", "inbound-interface group 'LAN'", "outbound-interface group 'LAN'"]
    assert rule(fw, family, 300) == ["action 'accept'", "inbound-interface group 'LAN'"]
    # 判定の順: 戻り → 自分用 → 監視 → 網のあいだを捨てる → 残りはインターネットへ
    order = [int(x.split()[6]) for x in fw if x.startswith(f"set firewall {family} forward filter rule ")]
    firsts = list(dict.fromkeys(order))
    assert firsts.index(100) < firsts.index(120) < firsts.index(140) < firsts.index(290) < firsts.index(300)


def test_dscp_rules_cover_both_wireguard_ports_and_keep_the_direction():
    fw = fw_lines()
    r5, r6 = rule(fw, "ipv6", 5), rule(fw, "ipv6", 6)
    assert "outbound-interface name 'eth2'" in r5 and "source port '51820,51821'" in r5 and "set dscp '0'" in r5
    assert "inbound-interface name 'eth2'" in r6 and "destination port '51820,51821'" in r6
    assert "state 'established'" in r6 and "state 'related'" in r6


def test_single_dscp_port_still_works():
    fw = commands(parts(site_with(**{"firewall.dscp_udp_port": 51820}))["30-firewall.cmds"])
    assert "source port '51820'" in rule(fw, "ipv6", 5)


def test_wireguard_inbound_to_the_core_router_matches_only_the_interface_id():
    fw = fw_lines()
    assert rule(fw, "ipv6", 115) == [
        "action 'accept'", "inbound-interface name 'eth2'", "protocol 'udp'", "destination port '51820,51821'",
        "destination address '::200:ff:fe00:211'", "destination address-mask '::ffff:ffff:ffff:ffff'"]
    assert not rule(fw, "ipv4", 115)
    assert not rule(commands(parts(site_with(**{"firewall.wireguard_inbound_iid": None}))["30-firewall.cmds"]),
                    "ipv6", 115)


def test_flowtable_lists_the_vifs():
    fw = fw_lines()
    for vlan, _, _ in NETS.values():
        assert f"set firewall flowtable FT10G interface 'br0.{vlan}'" in fw


def test_input_accepts_from_every_lan_vif():
    fw = fw_lines()
    assert "set firewall ipv4 input filter rule 30 inbound-interface group 'LAN'" in fw
    assert "set firewall ipv6 input filter rule 40 inbound-interface group 'LAN'" in fw


# ---- MAP-E ----------------------------------------------------------------------------------


def test_mape_translates_every_network():
    site = rc.validate(site_with(**{"wan.prefix": MAPE_PREFIX}))
    got = commands(rc.render_mape(site, source="example-vlan.yaml"))
    nets = [net for _, net, _ in NETS.values()]
    expected = [x for x in mc.result_cmds(mc.calc(MAPE_PREFIX), wan_if="eth2", lan=nets) if not x.startswith("delete ")]
    assert got[:len(expected)] == expected
    for net in nets:
        assert f"set firewall group network-group {mc.LAN_GROUP} network '{net}'" in got
    assert not [x for x in got if " source address " in x]


# ---- site の誤り ----------------------------------------------------------------------------

N = "lan.bridge.networks"


@pytest.mark.parametrize(("changes", "words"), [
    ({"mgmt.mode": "oob"}, "mgmt.mode"),
    ({f"{N}.1.sla_id": 0}, "sla_id"),
    ({f"{N}.1.vlan": 10}, "vlan が重なっている"),
    ({f"{N}.4.address": "192.168.2.241/29", f"{N}.4.dhcp.range.start": "192.168.2.242",
      f"{N}.4.dhcp.range.stop": "192.168.2.246"}, "重なる"),
    ({f"{N}.1.native": True}, "native"),
    ({f"{N}.2.monitor_to": ["NOPE"]}, "monitor_to"),
    ({f"{N}.2.monitor_to": ["MONITOR"]}, "monitor_to"),
    ({f"{N}.1.access": "lan"}, "access"),
    ({f"{N}.1.name": "self"}, "name"),
    ({f"{N}.1.typo": 1}, "typo"),
    ({f"{N}.1.dhcp": DELETE}, "dhcp"),
    ({f"{N}.0.dhcp.range.start": "192.168.2.1"}, "range"),
    ({f"{N}.4.dhcp.range.stop": "192.168.50.9"}, "range.stop"),
    ({f"{N}.1.dhcp.subnet_id": 10}, "subnet_id"),
    ({"lan.bridge.networks": []}, "networks"),
    ({"lan.bridge.name": "eth5"}, "lan.bridge.name"),
    ({"lan.address": "192.168.2.1/24"}, "lan.address"),
    ({"firewall.dscp_udp_port": []}, "dscp_udp_port"),
    ({"firewall.dscp_udp_port": [51820, 51820]}, "dscp_udp_port"),
    ({"firewall.dscp_udp_port": [70000]}, "dscp_udp_port"),
    ({"firewall.wireguard_inbound_iid": "2001:db8::1"}, "wireguard_inbound_iid"),
    ({"firewall.wireguard_inbound_iid": "not-an-address"}, "wireguard_inbound_iid"),
    ({"firewall.monitor_ports.tcp": 9100}, "monitor_ports"),
    ({"wan.type": "pd60", f"{N}.1.sla_id": 16}, "1〜15"),
])
def test_bad_site_values_are_rejected(changes, words):
    with pytest.raises(rc.SiteError) as e:
        rc.validate(site_with(**changes))
    assert words in str(e.value)


def test_rescue_mode_rejects_a_rescue_net_that_overlaps_a_vif():
    with pytest.raises(rc.SiteError, match="rescue.address"):
        rc.validate(site_with(**{"mgmt.mode": "rescue", "rescue.address": "192.168.30.254/24"}))


# ---- コマンドラインと CI ---------------------------------------------------------------------


def test_cli_check_compares_with_the_example_vlan_dir():
    r = subprocess.run([sys.executable, "-I", str(RENDER), str(VLAN_SITE), "--check", str(VLAN_DIR), "--all-wan-types"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_container_unsupported_entries_for_the_vlan_example_point_to_real_lines():
    lines = [line for name in COMMON for line in commands((VLAN_DIR / name).read_text(encoding="utf-8"))]
    entries = [e for e in (raw.split(" | ") for raw in (ROOT / "vyos" / "container-unsupported.txt")
                           .read_text(encoding="utf-8").splitlines() if raw.strip() and not raw.startswith("#"))
               if e[0].strip() in ("prod-vlan-example.cmds", "vlan-from-lab-mape.cmds")]
    assert len(entries) == 10
    for _, line, reason in entries:
        assert line.strip() in lines, line
        assert reason.strip()


def workflow_runs() -> str:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    return "\n".join(step.get("run", "") for job in jobs.values() for step in job.get("steps", []))


def test_workflow_checks_and_commits_the_vlan_example():
    runs = workflow_runs()
    assert re.search(r"render_config\.py site/example-vlan\.yaml --check vyos/prod/example-vlan --all-wan-types", runs)
    # 型によらない部分をつないだ基準と、型 A・B・C、複数の網の MAP-E の段を、VyOS のコンテナで commit する
    assert "prod-vlan-example.cmds" in runs
    for name in ("vlan-20-wan-pd56.cmds", "vlan-20-wan-pd60.cmds", "vlan-20-wan-ra64.cmds", "vlan-40-mape.cmds"):
        assert name in runs, name
