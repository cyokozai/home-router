"""CLAUDE.md（引き継ぎ資料）の数値・アドレス・設定の整合性を検査する。

資料は試験と機器設定の正典なので、計算値の誤りや管理系への誤適用を
文章のレビューではなく機械検査で止める。
HANDOFF_DOC 環境変数で検査対象を差し替えられる（検査自体の確認用）。
"""

import ipaddress
import os
import re
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DOC = Path(os.environ.get("HANDOFF_DOC", ROOT / "CLAUDE.md"))

MGMT_NET = ipaddress.ip_network("172.16.0.0/24")
LINE_RATE_BPS = 10_000_000_000
# プリアンブル7 + SFD1 + IFG12。FCS込みのフレーム長に足すと回線上の占有バイト数になる
ETHERNET_GAP_BYTES = 20
FULL_MTU_FRAME_ON_WIRE = 1500 + 14 + 4 + ETHERNET_GAP_BYTES  # 1538
IPV6_HEADER = 40
IPV4_ICMP_HEADERS = 28
TCP_TIMESTAMP_OPTION = 12


# ---- 資料の読み取り -------------------------------------------------------


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text(encoding="utf-8")


def headings(text: str) -> list[tuple[int, int, str]]:
    """(行番号, レベル, 見出し文) を返す。ATX と setext の両方に対応する。"""
    lines = text.splitlines()
    found = []
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        atx = re.match(r"^(#{1,6})\s+(.*)$", line)
        if atx:
            found.append((i, len(atx.group(1)), atx.group(2).strip()))
            continue
        if i + 1 < len(lines) and line.strip() and not line.startswith(("|", "-", ">")):
            underline = lines[i + 1]
            if re.fullmatch(r"=+", underline):
                found.append((i, 1, line.strip()))
            elif re.fullmatch(r"-+", underline):
                found.append((i, 2, line.strip()))
    return found


def section_number(title: str) -> str | None:
    m = re.match(r"^§?(\d+(?:-\d+)?)[.\s]", title)
    return m.group(1) if m else None


def sections(text: str) -> dict[str, str]:
    """節番号（"6" や "6-4"）→ 節の本文。"""
    lines = text.splitlines()
    heads = headings(text)
    result = {}
    for idx, (start, level, title) in enumerate(heads):
        number = section_number(title)
        if number is None:
            continue
        end = len(lines)
        for later_start, later_level, _ in heads[idx + 1 :]:
            if later_level <= level:
                end = later_start
                break
        result[number] = "\n".join(lines[start:end])
    return result


def code_blocks(text: str) -> list[str]:
    blocks = re.findall(r"^([ \t]*)```[^\n]*\n(.*?)^\1```", text, flags=re.M | re.S)
    return [textwrap.dedent(body) for _, body in blocks]


def vyos_lines(text: str) -> list[str]:
    out = []
    for block in code_blocks(text):
        for line in block.splitlines():
            if re.match(r"^(set|delete) ", line):
                out.append(line.split("#")[0].strip())
    return out


def vyos_value(lines: list[str], prefix: str) -> list[str]:
    values = []
    for line in lines:
        if line.startswith(prefix + " "):
            values.append(line[len(prefix) + 1 :].strip().strip("'"))
    return values


def table_value(text: str, label: str) -> str:
    m = re.search(r"^\|\s*" + re.escape(label) + r"\s*\|\s*([^|]+?)\s*\|", text, flags=re.M)
    assert m, f"表の行 {label!r} が見つからない"
    return m.group(1)


def to_number(cell: str) -> float:
    return float(re.sub(r"[^\d.]", "", cell))


# ---- 節の参照 -------------------------------------------------------------


def test_every_section_reference_points_to_an_existing_section(doc):
    existing = set(sections(doc))
    referenced = set(re.findall(r"§(\d+(?:-\d+)?)", doc))
    assert referenced - existing == set()


def test_pc01_status_points_to_the_section_with_its_triage_steps(doc):
    row = next(line for line in doc.splitlines() if line.startswith("| **PC01**"))
    m = re.search(r"§(\d+(?:-\d+)?)で切り分け中", row)
    assert m, "PC01の状態欄に切り分け先の節が書かれていない"
    assert "PC01でBIOSに入れない" in sections(doc)[m.group(1)]


# ---- ワイヤーレートの目標値（§8） ---------------------------------------------


def wire_rate_pps(frame_bytes: float) -> float:
    return LINE_RATE_BPS / ((frame_bytes + ETHERNET_GAP_BYTES) * 8)


@pytest.mark.parametrize("frame", [64, 512, 1518])
def test_fixed_size_wire_rate_pps(doc, frame):
    assert to_number(table_value(doc, f"{frame}B")) == round(wire_rate_pps(frame))


def test_imix_wire_rate_pps(doc):
    # Simple IMIX 7:4:1 = 64B / 570B / 1518B
    mix = {64: 7, 570: 4, 1518: 1}
    average = sum(size * n for size, n in mix.items()) / sum(mix.values())
    documented = to_number(table_value(doc, "IMIX（7:4:1）"))
    assert documented == pytest.approx(wire_rate_pps(average), rel=1e-4)


@pytest.mark.parametrize(
    ("label", "mss"),
    [("TCP（MSS 1448）", 1448), ("TCP（ipip6越し、MSS 1408）", 1408)],
)
def test_tcp_goodput(doc, label, mss):
    # どちらも外側のフレームは1500B MTU一杯なので、回線上は1セグメント1538B
    expected_gbps = LINE_RATE_BPS * mss / FULL_MTU_FRAME_ON_WIRE / 1e9
    documented = to_number(re.search(r"約([\d.]+)Gbps", doc[doc.index(label) :]).group(1))
    assert documented == round(expected_gbps, 2)


# ---- MAP-E模擬（§6-3, §5-3, §8 T7） -----------------------------------------


@pytest.fixture(scope="module")
def tunnel(doc):
    lines = vyos_lines(sections(doc)["6-3"])
    return {
        "mtu": int(vyos_value(lines, "set interfaces tunnel tun0 mtu")[0]),
        "mss": int(vyos_value(lines, "set interfaces tunnel tun0 ip adjust-mss")[0]),
    }


def test_tunnel_mtu_leaves_room_for_outer_ipv6_header(tunnel):
    assert tunnel["mtu"] == 1500 - IPV6_HEADER


def test_adjust_mss_matches_tunnel_mtu(tunnel):
    assert tunnel["mss"] == tunnel["mtu"] - 40  # IPv4 20 + TCP 20


def test_br_side_tunnel_mtu_matches_ce_side(doc, tunnel):
    m = re.search(r"ip link set br0 up mtu (\d+)", sections(doc)["5-3"])
    assert int(m.group(1)) == tunnel["mtu"]


def test_pmtud_ping_sizes(doc, tunnel):
    t7 = next(line for line in doc.splitlines() if line.startswith("| T7"))
    sizes = [int(s) for s in re.findall(r"-s (\d+)", t7)]
    assert sizes == [tunnel["mtu"] - IPV4_ICMP_HEADERS, 1500 - IPV4_ICMP_HEADERS]


def test_tcp_over_tunnel_mss_accounts_for_timestamps(doc, tunnel):
    assert "MSS 1408" in doc
    assert 1408 == tunnel["mss"] - TCP_TIMESTAMP_OPTION


def map_port_sets(a: int, k: int, psid: int) -> list[tuple[int, int]]:
    """RFC 7597 の GMA。A=0 は well-known ポートなので除外する。"""
    m = 16 - a - k
    return [
        ((index << (16 - a)) | (psid << m), (index << (16 - a)) | (psid << m) | ((1 << m) - 1))
        for index in range(1, 1 << a)
    ]


@pytest.fixture(scope="module")
def port_sets(doc):
    m = re.search(r"a=(\d+), k=(\d+), PSID=0x([0-9a-fA-F]+)", doc)
    assert m, "MAP-Eのパラメータ（a, k, PSID）が見つからない"
    return map_port_sets(int(m.group(1)), int(m.group(2)), int(m.group(3), 16))


def test_port_set_count_and_total(doc, port_sets):
    per_set = port_sets[0][1] - port_sets[0][0] + 1
    assert re.search(rf"{per_set}ポート×{len(port_sets)}セット", doc)
    assert f"計{per_set * len(port_sets)}ポート" in doc


def test_snat_port_range_is_the_first_port_set(doc, port_sets):
    lines = vyos_lines(sections(doc)["6-3"])
    first = "{}-{}".format(*port_sets[0])
    assert vyos_value(lines, "set nat source rule 101 translation port") == [first]


def test_br_drop_rule_uses_the_same_port_set(doc, port_sets):
    first = "{}-{}".format(*port_sets[0])
    assert f"宛先ポートが{first}以外ならdrop" in sections(doc)["5-3"]


# ---- アドレス設計（§3, §5-3, §6-1） ------------------------------------------


@pytest.fixture(scope="module")
def phase1(doc):
    return vyos_lines(sections(doc)["6-1"])


@pytest.fixture(scope="module")
def netplan(doc):
    block = next(b for b in code_blocks(sections(doc)["5-3"]) if b.startswith("network:"))
    return yaml.safe_load(block)


def interfaces(lines, name):
    return [ipaddress.ip_interface(v) for v in vyos_value(lines, f"set interfaces ethernet {name} address")]


def test_netplan_example_is_valid_yaml_with_standard_mtu(netplan):
    assert netplan["network"]["version"] == 2
    assert netplan["network"]["ethernets"]["sfp0"]["mtu"] == 1500


def test_pc01_shares_lan_subnets_with_pc02_eth1(phase1, netplan):
    pc01 = [ipaddress.ip_interface(a) for a in netplan["network"]["ethernets"]["sfp0"]["addresses"]]
    eth1 = interfaces(phase1, "eth1")
    assert {i.network for i in pc01} == {i.network for i in eth1}


def test_pc01_routes_go_via_pc02_eth1(phase1, netplan):
    gateways = {ipaddress.ip_address(r["via"]) for r in netplan["network"]["ethernets"]["sfp0"]["routes"]}
    assert gateways == {i.ip for i in interfaces(phase1, "eth1")}


def test_default_routes_point_to_pc03_inside_the_wan_subnets(phase1):
    eth2 = interfaces(phase1, "eth2")
    hops = vyos_value(phase1, "set protocols static route 0.0.0.0/0 next-hop")
    hops += vyos_value(phase1, "set protocols static route6 ::/0 next-hop")
    for hop in map(ipaddress.ip_address, hops):
        assert any(hop in i.network and hop != i.ip for i in eth2), hop


def test_mgmt_address_matches_the_mgmt_table(doc, phase1):
    (eth0,) = interfaces(phase1, "eth0")
    assert eth0.network == MGMT_NET
    assert str(eth0.ip) == table_value(doc, "PC02（VyOS）")


def test_10g_subnets_do_not_overlap_mgmt(phase1):
    for name in ("eth1", "eth2"):
        for i in interfaces(phase1, name):
            if i.version == 4:
                assert not i.network.overlaps(MGMT_NET)


# ---- 管理系の保護（§1 ルール1・6） ----------------------------------------------


def test_ssh_and_exporter_listen_only_on_mgmt(phase1):
    # ルール6: 10Gの経路でSSHや監視をしない
    listen = vyos_value(phase1, "set service ssh listen-address")
    listen += vyos_value(phase1, "set service monitoring prometheus node-exporter listen-address")
    assert listen and all(ipaddress.ip_address(a) in MGMT_NET for a in listen)


def test_prometheus_scrapes_over_mgmt(doc):
    m = re.search(r"対象は `([\d./]+):9100`", sections(doc)["5-3"])
    base, *rest = m.group(1).split("/")
    prefix = base.rsplit(".", 1)[0]
    hosts = [base] + [f"{prefix}.{h}" for h in rest]
    assert all(ipaddress.ip_address(h) in MGMT_NET for h in hosts)


@pytest.mark.parametrize("number", ["6-2", "6-3", "6-4"])
def test_test_environment_changes_never_touch_mgmt_eth0(doc, number):
    # ルール1: 管理系は§6-1の初期設定以外で触らない。§6-5は本番向けで別のIF割当
    touching = [line for line in vyos_lines(sections(doc)[number]) if re.search(r"\beth0\b", line)]
    assert touching == []


# ---- 端末の sysctl（§5-3） --------------------------------------------------


def test_tcp_buffer_maximums_fit_in_core_limits(doc):
    block = next(b for b in code_blocks(sections(doc)["5-3"]) if "net.core.rmem_max" in b)
    conf = dict(line.split(" = ") for line in block.strip().splitlines())
    assert int(conf["net.ipv4.tcp_rmem"].split()[2]) <= int(conf["net.core.rmem_max"])
    assert int(conf["net.ipv4.tcp_wmem"].split()[2]) <= int(conf["net.core.wmem_max"])


# ---- リポジトリの置き場（§1 ルール3・4） ---------------------------------------


@pytest.mark.parametrize("path", ["configs/pc01", "configs/pc02", "configs/pc03", "results"])
def test_storage_directories_exist(path):
    assert (ROOT / path).is_dir()


# ---- VyOS rolling で通る書き方（§6-4） ------------------------------------------


def test_set_dscp_uses_a_value_vyos_accepts(doc):
    # set-dscp は 0-63 か名前の一覧しか受け付けず、一覧に CS0 は無い。DSCP 0 は 'default'
    lines = vyos_lines(sections(doc)["6-4"])
    lines += [line.lstrip("# ") for b in code_blocks(sections(doc)["6-4"]) for line in b.splitlines()]
    assert not [line for line in lines if "set-dscp 'CS0'" in line]
    assert "set qos policy shaper NGN-OUT class 10 set-dscp 'default'" in lines


def test_tc_filter_check_does_not_restrict_to_egress(doc):
    # filter は root qdisc（HTB）に付くので、egress を指定すると何も表示されない
    checks = [line for line in doc.splitlines() if "tc filter show dev" in line]
    assert checks
    assert [line for line in checks if re.search(r"tc filter show dev \S+ egress", line)] == []


# ---- リポジトリ内のパスの参照（CLAUDE.md・README.md） ----------------------------


README = ROOT / "README.md"
REPO_PATH = re.compile(r"^(?:vyos|lab|scripts|tests|\.github)(?:/|$)")


def referenced_repo_paths(text: str) -> list[str]:
    """インラインコードのうち、リポジトリ内のパスを指すもの（先頭のトークン）。"""
    found = []
    for span in re.findall(r"`([^`\n]+)`", text):
        token = span.split()[0]
        if REPO_PATH.match(token):
            found.append(token)
    return found


def repo_path_exists(path: str) -> bool:
    if any(ch in path for ch in "*?["):
        return any(ROOT.glob(path))
    return (ROOT / path).exists()


def all_referenced_paths() -> list[tuple[str, str]]:
    pairs = []
    for source in (DOC, README):
        pairs += [(source.name, p) for p in referenced_repo_paths(source.read_text(encoding="utf-8"))]
    return sorted(set(pairs))


@pytest.mark.parametrize(("source", "path"), all_referenced_paths())
def test_referenced_repo_path_exists(source, path):
    assert repo_path_exists(path), f"{source} が参照する {path} が無い"


@pytest.mark.parametrize(
    "path",
    [
        "vyos/*.cmds",
        "vyos/VERSION",
        "vyos/container-unsupported.txt",
        "tests/test_vyos_cmds.py",
        ".github/workflows/vyos-config.yaml",
        "scripts/vyos-apply.sh",
        "lab/home-router.clab.yml",
        "tests/test_lab_topology.py",
    ],
)
def test_handoff_doc_explains_the_repo_files(doc, path):
    assert path in referenced_repo_paths(doc)


@pytest.mark.parametrize("path", ["vyos/", "scripts/", "lab/", ".github/"])
def test_readme_layout_table_lists_the_directory(path):
    rows = [line for line in README.read_text(encoding="utf-8").splitlines() if line.startswith("|")]
    assert any(f"`{path}" in row for row in rows), f"README の構成の表に {path} が無い"


def test_vyos_cmds_are_applied_with_the_checking_script(doc):
    # rolling の set / commit は失敗しても終了コード 0 を返す。スクリプトからは vyos-apply.sh で投入する
    assert "終了コード 0" in doc
    assert "scripts/vyos-apply.sh" in doc
