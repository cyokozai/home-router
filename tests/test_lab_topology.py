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


def expand_defaults(text: str) -> str:
    """`name="${N:-既定値}"` と `name="${other%/*}"` の代入を読み、`"${name}"` を値に置き換える。

    引数で値を差し替えられるスクリプト（pc03-br.sh）を、引数なしで流したときの形にして検査するため。
    """
    env: dict[str, str] = {}
    for m in re.finditer(r'^(\w+)="\$\{(?:\d+|\w+):-([^}]*)\}"$', text, flags=re.M):
        env[m.group(1)] = m.group(2)
    for m in re.finditer(r'^(\w+)="\$\{(\w+)%/\*\}"$', text, flags=re.M):
        env[m.group(1)] = env[m.group(2)].rsplit("/", 1)[0]
    for name, value in env.items():
        text = re.sub(r'"\$\{' + name + r'\}([^"]*)"', lambda m, v=value: v + m.group(1), text)
    return text


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


# pc03 は NGN 役（DHCPv6-PD と RA）の道具を足したイメージ。lab/pc03-ngn.Dockerfile から作る
PC03_IMAGE = "home-router/pc03-ngn:lab"
PC03_DOCKERFILE = "pc03-ngn.Dockerfile"


def dockerfile_base(text: str) -> str:
    (base,) = re.findall(r"^FROM\s+(\S+)", text, flags=re.M)
    return base


def test_pc03_image_is_pc01_image_plus_the_ngn_tools(nodes):
    assert nodes["pc03"]["image"] == PC03_IMAGE
    text = script(PC03_DOCKERFILE)
    assert dockerfile_base(text) == nodes["pc01"]["image"]
    # Kea（IA_PD を配る DHCPv6 サーバ）と radvd
    assert re.search(r"apk add [^\n]*\bkea-dhcp6\b", text)
    assert re.search(r"apk add [^\n]*\bradvd\b", text)


def test_ci_builds_the_pc03_image_before_deploying(nodes):
    wf = (ROOT / ".github" / "workflows" / "lab-e2e.yaml").read_text(encoding="utf-8")
    build = wf.index(f"docker build -t {PC03_IMAGE} -f lab/{PC03_DOCKERFILE} lab")
    assert build < wf.index("containerlab deploy")


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


# 実機の管理系は自宅LAN（§3「管理系のアドレス」）。lab は §3「containerlab の管理系」の表に合わせる
LAB_MGMT_HEADING = "### containerlab の管理系"


def lab_mgmt_table(doc: str) -> str:
    s3 = sections(doc)["3"]
    rest = s3[s3.index(LAB_MGMT_HEADING) :]
    end = rest.find("\n### ", 1)
    return rest if end < 0 else rest[:end]


def test_mgmt_subnet_matches_the_design(doc, topo):
    mgmt_row = table_value(lab_mgmt_table(doc), "管理系")
    assert topo["mgmt"]["ipv4-subnet"] == mgmt_row.strip()
    assert ipaddress.ip_network(topo["mgmt"]["ipv4-subnet"]) == ipaddress.ip_network("172.16.0.0/24")


@pytest.mark.parametrize(("node", "label"), [("pc01", "PC01"), ("pc02", "PC02（VyOS）"), ("pc03", "PC03")])
def test_mgmt_addresses_match_the_table(doc, nodes, node, label):
    assert nodes[node]["mgmt-ipv4"] == table_value(lab_mgmt_table(doc), label)


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
    return ip_commands(expand_defaults(script("pc03-br.sh")))


def test_br_script_takes_the_ngn_values_as_arguments():
    # NGN 役（§6-5 の v6プラス）では、BR と CE の値を scripts/mape_calc.py の計算から渡す
    text = script("pc03-br.sh")
    for n in (1, 2, 3):
        assert re.search(r'^\w+="\$\{' + str(n) + r':-[^}]+\}"$', text, flags=re.M), f"引数 {n} を受けていない"
    # 2 回目に別の値で流したときも、トンネルの両端を合わせ直す
    assert re.search(r'^ip -6 tunnel change br0 mode ipip6 local \S+ remote \S+ encaplimit none$', text, flags=re.M)


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


LAB_SCRIPTS = ["pc01.sh", "pc03.sh", "pc03-br.sh", "pc03-ngn.sh"]


@pytest.mark.parametrize("name", LAB_SCRIPTS)
def test_scripts_stop_on_error(name):
    lines = script(name).splitlines()
    assert lines[0].startswith("#!")
    assert any(re.fullmatch(r"set -e\w*", line.strip()) for line in lines)


@pytest.mark.parametrize("name", LAB_SCRIPTS)
def test_scripts_are_idempotent(name):
    # add は 2 回目で「File exists」になる。replace を使い、トンネルの作成は存在確認で守る。
    # 消すとき（del）は、無いときに落ちないよう || true を付ける
    text = script(name)
    for tokens in ip_commands(text):
        _, rest = family_and_rest(tokens)
        if rest[:1] in (["addr"], ["address"], ["route"]):
            assert rest[1] in ("replace", "del"), " ".join(tokens)
    for line in text.splitlines():
        if re.search(r"\bip (-6 )?(addr|route) del\b", line):
            assert "|| true" in line, line
    if "tunnel add" in text:
        assert re.search(r"ip link show (dev )?br0[^\n]*\|\|\s*ip -6 tunnel add", text)


@pytest.mark.parametrize("name", LAB_SCRIPTS)
def test_scripts_never_touch_the_mgmt_interface(name):
    # §1 ルール1。containerlab の linux ノードでは eth0 が管理系
    for tokens in ip_commands(script(name)):
        assert "eth0" not in tokens and "mgmt0" not in tokens, " ".join(tokens)


# ---- PC03 の NGN 役（lab/pc03-ngn.sh。光クロスの WAN 側を模す） -----------------------

NGN = "pc03-ngn.sh"
NGN_SCENARIOS = ("pd56", "pd60", "ra64")
# v6プラスの MAP ルール 240b:10::/31 の例（tests/test_mape_calc.py の 240b:10:aabb:ccdd::/64 を含む /56）
NGN_EXAMPLE_IN_MAPE_TEST = "240b:10:aabb:ccdd::/64"


def shell_constants(text: str) -> dict[str, str]:
    """`NAME=値` の行（大文字の名前、行頭）を読む。"""
    return {m.group(1): m.group(2).strip("'\"")
            for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=(\S+)", text, flags=re.M)}


@pytest.fixture(scope="module")
def ngn():
    return shell_constants(script(NGN))


@pytest.fixture(scope="module")
def ngn_net(ngn):
    return {k: ipaddress.ip_network(v) for k, v in ngn.items() if k in ("PD56", "PD60", "HGW_LAN", "WAN_SUBNET")}


def load_mape_calc():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("mape_calc", ROOT / "scripts" / "mape_calc.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("mape_calc", mod)
    spec.loader.exec_module(mod)
    return mod


def test_ngn_prefix_is_the_v6plus_example_used_by_the_mape_tests(ngn_net):
    pd56 = ngn_net["PD56"]
    assert pd56.prefixlen == 56
    assert ipaddress.ip_network(NGN_EXAMPLE_IN_MAPE_TEST).subnet_of(pd56)
    assert NGN_EXAMPLE_IN_MAPE_TEST in (ROOT / "tests" / "test_mape_calc.py").read_text(encoding="utf-8")
    mc = load_mape_calc()
    assert mc.find_rule(pd56, mc.V6PLUS_RULES).ipv6 == "240b:10::/31"


def test_hgw_emulation_ras_the_first_64_and_delegates_a_60_beside_it(ngn, ngn_net):
    pd56, pd60, lan = ngn_net["PD56"], ngn_net["PD60"], ngn_net["HGW_LAN"]
    assert lan == next(pd56.subnets(new_prefix=64))
    assert pd60.prefixlen == 60 and pd60.subnet_of(pd56)
    assert not pd60.overlaps(lan)
    assert ipaddress.ip_interface(ngn["HGW_ADDR"]).network == lan


def test_dhcpv6_server_serves_pc03_wan_link(ngn, ngn_net, pc03):
    assert ngn["IF"] == HOST_IF
    assert ngn_net["WAN_SUBNET"] in {a.network for _, a in addresses(pc03) if a.version == 6}


@pytest.mark.parametrize("scenario", NGN_SCENARIOS)
def test_ngn_script_knows_every_scenario(scenario):
    assert re.search(rf"^\s*{scenario}\)", script(NGN), flags=re.M)


def test_ngn_script_validates_the_kea_config_before_starting():
    text = script(NGN)
    assert "kea-dhcp6 -t" in text
    # Kea 3 は保存先を決まった場所に限る。リースはファイルに残さない（流し直しのたびに新しく配る）
    assert re.search(r'"persist":\s*false', text)


def test_pc03_mounts_the_ngn_script(nodes):
    assert f"{NGN}:/lab/{NGN}:ro" in nodes["pc03"]["binds"]


# ---- PC02 側（lab/pc02-ngn-*.cmds。DHCPv6-PD のクライアントと LAN の RA だけ） -------------

WAN_IF, LAN_IF = "eth2", "eth1"


def lab_cmds(name: str) -> list[str]:
    lines = []
    for raw in script(name).splitlines():
        line = raw.split("#")[0].strip()
        if line:
            lines.append(line)
    return lines


@pytest.mark.parametrize("scenario", NGN_SCENARIOS)
def test_pc02_ngn_cmds_are_set_lines_without_the_mgmt_interface(scenario):
    lines = lab_cmds(f"pc02-ngn-{scenario}.cmds")
    assert lines
    for line in lines:
        assert re.match(r"^(set|delete) ", line), line
        assert "eth0" not in line, line  # §1 ルール1（lab でも eth0 は管理系）


def test_wan_and_lan_ifs_match_the_topology(topo):
    for link in topo["topology"]["links"]:
        ends = dict(e.split(":") for e in link["endpoints"])
        assert ends["pc02"] == (WAN_IF if "pc03" in ends else LAN_IF)


@pytest.mark.parametrize(("scenario", "length"), [("pd56", 56), ("pd60", 60)])
def test_pc02_requests_the_delegated_length_and_puts_subnet_1_on_the_lan(scenario, length):
    lines = lab_cmds(f"pc02-ngn-{scenario}.cmds")
    pd = f"set interfaces ethernet {WAN_IF} dhcpv6-options pd 0"
    assert vyos_value(lines, f"{pd} length") == [str(length)]
    assert vyos_value(lines, f"{pd} interface {LAN_IF} sla-id") == ["1"]
    assert vyos_value(lines, f"{pd} interface {LAN_IF} address") == ["1"]
    assert vyos_value(lines, f"set service router-advert interface {LAN_IF} prefix") == ["::/64"]
    assert f"set interfaces ethernet {WAN_IF} ipv6 address autoconf" in lines


def test_pc02_ra64_only_accepts_the_ra():
    lines = lab_cmds("pc02-ngn-ra64.cmds")
    assert f"set interfaces ethernet {WAN_IF} ipv6 address autoconf" in lines
    assert not any("dhcpv6-options pd" in line for line in lines)


@pytest.mark.parametrize(("scenario", "key"), [("pd56", "PD56"), ("pd60", "PD60")])
def test_pc02_requested_length_matches_what_pc03_delegates(ngn_net, scenario, key):
    lines = lab_cmds(f"pc02-ngn-{scenario}.cmds")
    (length,) = vyos_value(lines, f"set interfaces ethernet {WAN_IF} dhcpv6-options pd 0 length")
    assert int(length) == ngn_net[key].prefixlen


# ---- scripts/lab-e2e.sh の期待値が pc03-ngn.sh と食い違わないこと ---------------------


@pytest.fixture(scope="module")
def e2e():
    return (ROOT / "scripts" / "lab-e2e.sh").read_text(encoding="utf-8")


def e2e_value(e2e: str, name: str) -> str:
    m = re.search(rf"\b{name}=(\S+)", e2e)
    assert m, f"scripts/lab-e2e.sh に {name} が無い"
    return m.group(1)


def sla_address(net: ipaddress.IPv6Network, sla_id: int, host: int) -> ipaddress.IPv6Interface:
    sub = list(net.subnets(new_prefix=64))[sla_id]
    return ipaddress.IPv6Interface(f"{sub.network_address + host}/64")


@pytest.mark.parametrize(("e2e_name", "key"),
                         [("ngn_pd56", "PD56"), ("ngn_pd60", "PD60"), ("ngn_hgw_lan", "HGW_LAN")])
def test_e2e_prefixes_match_the_ngn_script(e2e, ngn_net, e2e_name, key):
    assert ipaddress.ip_network(e2e_value(e2e, e2e_name)) == ngn_net[key]


@pytest.mark.parametrize(("e2e_name", "key"), [("ngn_lan56", "PD56"), ("ngn_lan60", "PD60")])
def test_e2e_expects_sla_id_1_address_1_on_eth1(e2e, ngn_net, e2e_name, key):
    # pc02-ngn-*.cmds の sla-id '1' と address '1'
    assert ipaddress.IPv6Interface(e2e_value(e2e, e2e_name)) == sla_address(ngn_net[key], 1, 1)


@pytest.mark.parametrize("scenario", NGN_SCENARIOS)
def test_e2e_runs_every_ngn_scenario(e2e, scenario):
    assert re.search(rf"^ngn_round {scenario}\b", e2e, flags=re.M)


def test_e2e_takes_the_mape_values_from_mape_calc(e2e):
    # MAP-E の値は scripts/mape_calc.py に計算させる（手で書き写さない）
    assert re.search(r"mape_calc\.py\"? --cmds --wan-if eth2\b", e2e)
