"""containerlab の検証環境（lab/）が CLAUDE.md の設計と一致しているかを静的に検査する。

コンテナは起動しない。トポロジーの YAML と端末用のシェルスクリプトを読み、
§3（ネットワーク設計）、§5-3（netplan と BR 役）、§6-1・§6-3（VyOS 側のアドレスとトンネル）
と突き合わせる。VyOS 側の値は vyos/ ではなく CLAUDE.md のコードブロックから読む。

HANDOFF_DOC で資料を、LAB_DIR で lab/ を差し替えられる（検査自体の確認用）。
"""

import ipaddress
import os
import re
import shlex
from pathlib import Path

import pytest
import yaml

from test_handoff_doc import code_blocks, interfaces, sections, table_value, vyos_lines, vyos_value

ROOT = Path(__file__).resolve().parent.parent
DOC = Path(os.environ.get("HANDOFF_DOC", ROOT / "CLAUDE.md"))
LAB = Path(os.environ.get("LAB_DIR", ROOT / "lab"))
TOPOLOGY = LAB / "home-router.clab.yml"

# 並行タスク（VyOS イメージの作成）との契約。名前を変えるなら両方を直す
VYOS_IMAGE = "home-router/vyos:rolling"
VYOS_KIND = "vyosnetworks_vyos"
# linux ノードの 10G 側 IF 名。実機の netplan（set-name: sfp0）に合わせる
HOST_IF = "sfp0"


# ---- 読み取り ---------------------------------------------------------------


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def topo() -> dict:
    return yaml.safe_load(TOPOLOGY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def nodes(topo) -> dict:
    return topo["topology"]["nodes"]


@pytest.fixture(scope="module")
def phase1(doc):
    return vyos_lines(sections(doc)["6-1"])


@pytest.fixture(scope="module")
def phase2(doc):
    return vyos_lines(sections(doc)["6-3"])


@pytest.fixture(scope="module")
def netplan_sfp0(doc):
    block = next(b for b in code_blocks(sections(doc)["5-3"]) if b.startswith("network:"))
    return yaml.safe_load(block)["network"]["ethernets"][HOST_IF]


def ip_commands(text: str) -> list[list[str]]:
    """シェルスクリプト（または資料のコードブロック）から `ip ...` の呼び出しを取り出す。

    行をシェルの演算子（`||` `&&` `;` `|`）で区切り、リダイレクトと sudo と行末コメントを除いて、
    `ip` 以降のトークン列を返す。
    """
    found = []
    for raw in text.splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        segment: list[str] = []
        for token in [*lexer, ";"]:
            if token in ("||", "&&", ";", "|"):
                if segment and segment[0] == "sudo":
                    segment = segment[1:]
                if segment and segment[0] == "ip":
                    found.append(segment[1:])
                segment = []
            else:
                segment.append(token)
    # リダイレクト（`>/dev/null` `2>&1`）を落とす
    return [strip_redirects(c) for c in found]


def strip_redirects(tokens: list[str]) -> list[str]:
    out = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token in (">", ">>", "<", ">&", "&>"):
            skip = True
            continue
        if re.fullmatch(r"\d", token) or re.fullmatch(r"\d?>&?\d*", token):
            continue
        out.append(token)
    return out


def family_and_rest(tokens: list[str]) -> tuple[str | None, list[str]]:
    if tokens and tokens[0] in ("-4", "-6"):
        return tokens[0], tokens[1:]
    return None, tokens


def addresses(cmds) -> set[tuple[str, ipaddress.IPv4Interface | ipaddress.IPv6Interface]]:
    """(dev, アドレス) の集合。add / replace のどちらで書いてもよい。"""
    out = set()
    for tokens in cmds:
        _, rest = family_and_rest(tokens)
        if rest[:1] in (["addr"], ["address"], ["a"]) and rest[1] in ("add", "replace"):
            addr = ipaddress.ip_interface(rest[2])
            dev = rest[rest.index("dev") + 1]
            out.add((dev, addr))
    return out


def routes(cmds) -> set[tuple[str, str]]:
    """(宛先, next-hop か dev:名前) の集合。"""
    out = set()
    for tokens in cmds:
        _, rest = family_and_rest(tokens)
        if rest[:1] in (["route"], ["r"]) and rest[1] in ("add", "replace"):
            dst = str(ipaddress.ip_network(rest[2]))
            if "via" in rest:
                hop = str(ipaddress.ip_address(rest[rest.index("via") + 1]))
            else:
                hop = "dev:" + rest[rest.index("dev") + 1]
            out.add((dst, hop))
    return out


def script(name: str) -> str:
    return (LAB / name).read_text(encoding="utf-8")


def eth(lines, name):
    return interfaces(lines, name)


def same_family(addr, candidates):
    (match,) = [c for c in candidates if c.version == addr.version]
    return match


# ---- ノード ---------------------------------------------------------------


def test_topology_has_exactly_the_three_pcs(nodes):
    assert set(nodes) == {"pc01", "pc02", "pc03"}


def test_router_is_the_vyos_image_built_by_the_image_task(nodes):
    assert nodes["pc02"]["kind"] == VYOS_KIND
    assert nodes["pc02"]["image"] == VYOS_IMAGE


@pytest.mark.parametrize("name", ["pc01", "pc03"])
def test_hosts_are_linux_with_a_pinned_image(nodes, name):
    node = nodes[name]
    assert node["kind"] == "linux"
    repo, _, tag = node["image"].rpartition(":")
    assert repo and tag and tag != "latest", "端末のイメージはタグを固定する"


def test_both_hosts_use_the_same_image(nodes):
    assert nodes["pc01"]["image"] == nodes["pc03"]["image"]


def test_vyos_settings_are_not_inlined_into_the_topology(nodes):
    # VyOS の設定は vyos/ の set コマンドを後から投入する。トポロジーに直書きしない
    assert "startup-config" not in nodes["pc02"] or not str(nodes["pc02"]["startup-config"]).lstrip().startswith("set ")


# ---- リンク（§3） -----------------------------------------------------------


def expected_links(doc) -> set[frozenset[str]]:
    """§3 の表の「PC02側のIF」と、図の両端の IF 名から期待するリンクを組み立てる。"""
    s3 = sections(doc)["3"]

    def pc02_if(row_label):
        row = s3[s3.index(row_label) :].splitlines()[0]
        return re.search(r"`(\w+)`", row.split("|")[-2]).group(1)  # 最後の列「PC02側のIF」

    lan_if = pc02_if("PC01 ⇄ PC02（LAN）")
    wan_if = pc02_if("PC02 ⇄ PC03（WAN")
    return {
        frozenset({f"pc01:{HOST_IF}", f"pc02:{lan_if}"}),
        frozenset({f"pc02:{wan_if}", f"pc03:{HOST_IF}"}),
    }


def test_diagram_names_the_same_interfaces_as_the_topology(doc):
    # 図の IF 名の行: PC01側 / PC02 LAN側 │ PC02 WAN側 / PC03側
    diagram = code_blocks(sections(doc)["3"])[0]
    row = next(line for line in diagram.splitlines() if "│" in line and "eth" in line)
    assert row.split() == [HOST_IF, "eth1", "│", "eth2", HOST_IF]


def test_links_match_the_network_design(doc, topo):
    links = {frozenset(link["endpoints"]) for link in topo["topology"]["links"]}
    assert links == expected_links(doc)


def test_pc02_side_interfaces_carry_the_phase1_subnets(doc, topo, phase1):
    # LAN 側のリンクにつながる PC02 の IF が §6-1 で 192.168.10.0/24 を持つ IF であること
    for link in topo["topology"]["links"]:
        ends = dict(e.split(":") for e in link["endpoints"])
        pc02_if = ends["pc02"]
        nets = {i.network for i in eth(phase1, pc02_if)}
        if "pc01" in ends:
            assert ipaddress.ip_network("192.168.10.0/24") in nets
        else:
            assert ipaddress.ip_network("192.168.20.0/30") in nets


def test_mgmt_interface_is_not_used_for_data_links(topo):
    # VyOS の eth0 は containerlab の管理系になる（実機と同じ）
    endpoints = {e for link in topo["topology"]["links"] for e in link["endpoints"]}
    assert "pc02:eth0" not in endpoints


# ---- 管理系（§3「管理系のアドレス」） ------------------------------------------


def test_mgmt_subnet_matches_the_design(doc, topo):
    mgmt_row = table_value(sections(doc)["3"], "管理系")
    assert topo["mgmt"]["ipv4-subnet"] == mgmt_row.strip()
    assert ipaddress.ip_network(topo["mgmt"]["ipv4-subnet"]) == ipaddress.ip_network("172.16.0.0/24")


@pytest.mark.parametrize(("node", "label"), [("pc01", "PC01"), ("pc02", "PC02（VyOS）"), ("pc03", "PC03")])
def test_mgmt_addresses_match_the_table(doc, nodes, node, label):
    s3 = sections(doc)["3"]
    table = s3[s3.index("### 管理系のアドレス") :]
    assert nodes[node]["mgmt-ipv4"] == table_value(table, label)


def test_mgmt_addresses_are_inside_the_mgmt_subnet(topo, nodes):
    net = ipaddress.ip_network(topo["mgmt"]["ipv4-subnet"])
    for node in nodes.values():
        assert ipaddress.ip_address(node["mgmt-ipv4"]) in net


# ---- PC01（§5-3 の netplan） -------------------------------------------------


@pytest.fixture(scope="module")
def pc01():
    return ip_commands(script("pc01.sh"))


def test_pc01_addresses_match_netplan(pc01, netplan_sfp0):
    expected = {(HOST_IF, ipaddress.ip_interface(a)) for a in netplan_sfp0["addresses"]}
    assert addresses(pc01) == expected


def test_pc01_routes_match_netplan(pc01, netplan_sfp0):
    expected = {
        (str(ipaddress.ip_network(r["to"])), str(ipaddress.ip_address(r["via"]))) for r in netplan_sfp0["routes"]
    }
    assert routes(pc01) == expected
    assert len(expected) == 4


def test_pc01_next_hops_are_pc02_eth1(pc01, phase1):
    eth1 = {str(i.ip) for i in eth(phase1, "eth1")}
    assert {hop for _, hop in routes(pc01)} == eth1


def test_pc01_mtu_matches_netplan(pc01, netplan_sfp0):
    assert ["link", "set", HOST_IF, "up", "mtu", str(netplan_sfp0["mtu"])] in pc01


# ---- PC03（§3 の表、§5-3 の注記） ----------------------------------------------


@pytest.fixture(scope="module")
def pc03():
    return ip_commands(script("pc03.sh"))


def pc03_addresses_from_doc(doc):
    note = re.search(r"PC03は `([^`]+)`、`([^`]+)` にし", sections(doc)["5-3"])
    assert note, "§5-3 の PC03 のアドレスの注記が見つからない"
    return {ipaddress.ip_interface(note.group(1)), ipaddress.ip_interface(note.group(2))}


def test_pc03_addresses_match_the_doc(doc, pc03):
    assert addresses(pc03) == {(HOST_IF, a) for a in pc03_addresses_from_doc(doc)}


def test_pc03_addresses_share_subnets_with_pc02_eth2(doc, phase1):
    assert {a.network for a in pc03_addresses_from_doc(doc)} == {i.network for i in eth(phase1, "eth2")}


def test_pc03_addresses_are_the_phase1_next_hops_of_pc02(doc, phase1):
    hops = vyos_value(phase1, "set protocols static route 0.0.0.0/0 next-hop")
    hops += vyos_value(phase1, "set protocols static route6 ::/0 next-hop")
    assert {ipaddress.ip_address(h) for h in hops} == {a.ip for a in pc03_addresses_from_doc(doc)}


def test_pc03_return_routes_reach_the_lan_via_pc02_eth2(doc, pc03, phase1):
    # §5-3「192.168.10.0/24 via 192.168.20.1 などの戻り経路」。LAN の全サブネットを eth2 経由で戻す
    assert "192.168.10.0/24 via 192.168.20.1" in sections(doc)["5-3"]
    eth2 = eth(phase1, "eth2")
    expected = {(str(lan.network), str(same_family(lan, eth2).ip)) for lan in eth(phase1, "eth1")}
    assert routes(pc03) == expected


def test_pc03_mtu_is_standard(pc03, netplan_sfp0):
    assert ["link", "set", HOST_IF, "up", "mtu", str(netplan_sfp0["mtu"])] in pc03


# ---- PC03 の BR 役（§5-3 と §6-3） -------------------------------------------


@pytest.fixture(scope="module")
def br():
    return ip_commands(script("pc03-br.sh"))


@pytest.fixture(scope="module")
def br_doc(doc):
    s = sections(doc)["5-3"]
    block = next(b for b in code_blocks(s) if "ip -6 tunnel add br0" in b)
    return ip_commands(block)


def tunnel_params(cmds) -> dict:
    for tokens in cmds:
        _, rest = family_and_rest(tokens)
        if rest[:2] == ["tunnel", "add"]:
            return {
                "name": rest[2],
                "mode": rest[rest.index("mode") + 1],
                "local": ipaddress.ip_address(rest[rest.index("local") + 1]),
                "remote": ipaddress.ip_address(rest[rest.index("remote") + 1]),
            }
    raise AssertionError("トンネルを作るコマンドが無い")


def link_mtu(cmds, dev) -> int:
    for tokens in cmds:
        if tokens[:3] == ["link", "set", dev] and "mtu" in tokens:
            return int(tokens[tokens.index("mtu") + 1])
    raise AssertionError(f"{dev} の MTU を設定していない")


def test_br_script_matches_the_doc(br, br_doc):
    assert addresses(br) == addresses(br_doc)
    assert routes(br) == routes(br_doc)
    assert tunnel_params(br) == tunnel_params(br_doc)
    assert link_mtu(br, "br0") == link_mtu(br_doc, "br0")


def test_br_tunnel_is_the_mirror_of_ce_tun0(br, phase2):
    tun = tunnel_params(br)
    assert tun["mode"] == vyos_value(phase2, "set interfaces tunnel tun0 encapsulation")[0] == "ipip6"
    assert str(tun["local"]) == vyos_value(phase2, "set interfaces tunnel tun0 remote")[0]
    assert str(tun["remote"]) == vyos_value(phase2, "set interfaces tunnel tun0 source-address")[0]


def test_br_tunnel_mtu_matches_ce_tun0(br, phase2):
    assert link_mtu(br, "br0") == int(vyos_value(phase2, "set interfaces tunnel tun0 mtu")[0])


def test_br_local_address_is_on_pc03_sfp0_in_the_wan_subnet(br, phase1):
    local = tunnel_params(br)["local"]
    assert any(dev == HOST_IF and a.ip == local for dev, a in addresses(br))
    assert any(local in i.network for i in eth(phase1, "eth2"))


def test_br_routes_the_ce_address_into_the_tunnel(br, phase2):
    (ce,) = vyos_value(phase2, "set interfaces tunnel tun0 address")
    assert (str(ipaddress.ip_network(ce)), "dev:br0") in routes(br)


def test_br_owns_the_internet_side_address(doc, br):
    m = re.search(r"PC03の外側アドレス ([\d.]+)", sections(doc)["3"])
    assert ("lo", ipaddress.ip_interface(m.group(1) + "/32")) in addresses(br)


def test_phase1_scripts_do_not_build_the_tunnel(pc03):
    # BR 役はフェーズ2 用に pc03-br.sh へ分ける
    assert not any(family_and_rest(t)[1][:1] == ["tunnel"] for t in pc03)


# ---- スクリプトの書き方 --------------------------------------------------------


@pytest.mark.parametrize("name", ["pc01.sh", "pc03.sh", "pc03-br.sh"])
def test_scripts_stop_on_error(name):
    lines = script(name).splitlines()
    assert lines[0].startswith("#!")
    assert any(re.fullmatch(r"set -e\w*", line.strip()) for line in lines)


@pytest.mark.parametrize("name", ["pc01.sh", "pc03.sh", "pc03-br.sh"])
def test_scripts_are_idempotent(name):
    # add は 2 回目で「File exists」になる。replace を使い、トンネルの作成は存在確認で守る
    text = script(name)
    for tokens in ip_commands(text):
        _, rest = family_and_rest(tokens)
        if rest[:1] in (["addr"], ["address"], ["route"]):
            assert rest[1] == "replace", " ".join(tokens)
    if "tunnel add" in text:
        assert re.search(r"ip link show (dev )?br0[^\n]*\|\|\s*ip -6 tunnel add", text)


@pytest.mark.parametrize("name", ["pc01.sh", "pc03.sh", "pc03-br.sh"])
def test_scripts_never_touch_the_mgmt_interface(name):
    # §1 ルール1。containerlab の linux ノードでは eth0 が管理系
    for tokens in ip_commands(script(name)):
        assert "eth0" not in tokens and "mgmt0" not in tokens, " ".join(tokens)
