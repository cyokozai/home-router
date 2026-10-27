"""CLAUDE.md §6 の VyOS 設定と vyos/*.cmds の同期を検査する。

資料（CLAUDE.md）のコードブロックは人が読むための正典、vyos/*.cmds は実機と
CI の VyOS コンテナへそのまま投入するファイル。両者が食い違うと「資料どおりに
入れたつもりが別物を測っていた」になるので、資料の set/delete 行が対応する
ファイルにすべて含まれることを検査する（ファイル側は資料で省略された行を
足してあるので、包含関係で見る）。
§6-5（本番向け DHCPv6-PD）は検証環境の対象外なので扱わない。
"""

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = Path(os.environ.get("HANDOFF_DOC", ROOT / "CLAUDE.md"))
VYOS = ROOT / "vyos"
WORKFLOW = ROOT / ".github" / "workflows" / "vyos-config.yaml"

BASELINE = "phase1.cmds"
# §6-2 のブロックは丸数字のコメントで 4 つの差分に分かれている
TUNING_PARTS = {
    "①": "tuning-1-offload.cmds",
    "②": "tuning-2-rps.cmds",
    "③": "tuning-3-flowtable.cmds",
    "④": "tuning-4-performance.cmds",
}
SECTION_FILES = {
    "6-1": BASELINE,
    "6-3": "phase2-mape.cmds",
    "6-4": "qos-ngn-out.cmds",
}
DELTAS = list(TUNING_PARTS.values()) + ["phase2-mape.cmds", "qos-ngn-out.cmds"]

# 資料の行が VyOS rolling では投入できないため、ファイル側で正しい構文に直したもの。
# 資料（CLAUDE.md）が直ったら、ここから外す（下の検査がそれを促す）。
# 書式: {資料の行: ファイルに書く行}。今は無い（§6-4 の set-dscp 'CS0' は資料側を 'default' に直した）。
DOC_CORRECTIONS: dict[str, str] = {}

UNSUPPORTED = VYOS / "container-unsupported.txt"


# ---- 読み取り ---------------------------------------------------------------


def subsection(text: str, number: str) -> str:
    m = re.search(rf"^### {re.escape(number)}\.[^\n]*\n(.*?)(?=^### |^---|\Z)", text, flags=re.M | re.S)
    assert m, f"§{number} が見つからない"
    return m.group(1)


def code_blocks(text: str) -> list[str]:
    return re.findall(r"^```[^\n]*\n(.*?)^```", text, flags=re.M | re.S)


def strip_comment(line: str) -> str:
    """行末コメントを外す。クォートの中の # は残す。"""
    out, quote = [], None
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#":
            break
        out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def commands(text: str) -> list[str]:
    return [c for c in map(strip_comment, text.splitlines()) if re.match(r"^(set|delete) ", c)]


def doc_commands(number: str) -> list[str]:
    blocks = code_blocks(subsection(DOC.read_text(encoding="utf-8"), number))
    assert len(blocks) == 1, f"§{number} の設定ブロックが 1 つでない"
    return commands(blocks[0])


def doc_tuning_parts() -> dict[str, list[str]]:
    (block,) = code_blocks(subsection(DOC.read_text(encoding="utf-8"), "6-2"))
    parts: dict[str, list[str]] = {}
    current = None
    for line in block.splitlines():
        marker = re.match(r"^#\s*([①-⑳])", line)
        if marker:
            current = marker.group(1)
            parts[current] = []
            continue
        parts.setdefault(current, []).extend(commands(line))
    return parts


def file_commands(name: str) -> list[str]:
    return commands((VYOS / name).read_text(encoding="utf-8"))


def expected_in_file() -> list[tuple[str, str]]:
    pairs = []
    for number, name in SECTION_FILES.items():
        pairs += [(name, line) for line in doc_commands(number)]
    for mark, lines in doc_tuning_parts().items():
        pairs += [(TUNING_PARTS[mark], line) for line in lines]
    return [(name, DOC_CORRECTIONS.get(line, line)) for name, line in pairs]


def unsupported_entries() -> list[tuple[str, str, str]]:
    entries = []
    for raw in UNSUPPORTED.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        fields = [f.strip() for f in raw.split(" | ")]
        assert len(fields) == 3, f"「ファイル | コマンド | 理由」の 3 列でない: {raw!r}"
        entries.append(tuple(fields))
    return entries


# ---- 資料とファイルの同期 ---------------------------------------------------------


def test_tuning_block_is_split_into_the_known_parts():
    assert set(doc_tuning_parts()) == set(TUNING_PARTS), "§6-2 の丸数字の区切りが変わった"


@pytest.mark.parametrize("name", [BASELINE] + DELTAS)
def test_cmds_file_exists(name):
    assert (VYOS / name).is_file()


@pytest.mark.parametrize(("name", "line"), expected_in_file())
def test_doc_command_is_in_the_cmds_file(name, line):
    assert line in file_commands(name), f"CLAUDE.md の行が vyos/{name} に無い: {line}"


@pytest.mark.parametrize("doc_line", list(DOC_CORRECTIONS))
def test_doc_correction_is_still_needed(doc_line):
    everything = [line for n in SECTION_FILES for line in doc_commands(n)]
    everything += [line for lines in doc_tuning_parts().values() for line in lines]
    assert doc_line in everything, "CLAUDE.md が直ったので DOC_CORRECTIONS から外す"


# ---- ファイルの形 -------------------------------------------------------------------


@pytest.mark.parametrize("name", [BASELINE] + DELTAS)
def test_cmds_file_has_only_commands_and_comments(name):
    for raw in (VYOS / name).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        assert not line or line.startswith("#") or re.match(r"^(set|delete) ", line), raw


@pytest.mark.parametrize("name", [BASELINE] + DELTAS)
def test_cmds_values_never_contain_hash(name):
    # scripts/vyos-apply.sh は # 以降をコメントとして単純に切り落とす
    for raw in (VYOS / name).read_text(encoding="utf-8").splitlines():
        if not raw.lstrip().startswith("#"):
            assert raw.split("#")[0].strip() == strip_comment(raw), raw


@pytest.mark.parametrize("name", [BASELINE] + DELTAS)
def test_cmds_file_has_no_duplicate_commands(name):
    lines = file_commands(name)
    assert len(lines) == len(set(lines))


def test_no_unexpected_cmds_files():
    assert {p.name for p in VYOS.glob("*.cmds")} == {BASELINE, *DELTAS}


@pytest.mark.parametrize("name", DELTAS)
def test_deltas_never_touch_mgmt_eth0(name):
    # §1 ルール1: 管理系は §6-1 の初期設定以外で触らない
    assert [line for line in file_commands(name) if re.search(r"\beth0\b", line)] == []


def test_ipv6_flowtable_mirrors_ipv4():
    # 資料の「（ipv6も同様）」を省略せずに書いてあること
    lines = file_commands("tuning-3-flowtable.cmds")
    v4 = [line for line in lines if line.startswith("set firewall ipv4 ")]
    assert v4
    for line in v4:
        assert line.replace("firewall ipv4 ", "firewall ipv6 ", 1) in lines


def test_eth2_offload_mirrors_eth1():
    # 資料の「（eth2も同様）」を省略せずに書いてあること
    lines = file_commands("tuning-1-offload.cmds")
    eth1 = [line for line in lines if " eth1 " in line]
    assert eth1
    for line in eth1:
        assert line.replace(" eth1 ", " eth2 ") in lines


# ---- コンテナで検証できない行の一覧 -------------------------------------------------


def test_unsupported_entries_point_to_real_lines():
    for name, line, reason in unsupported_entries():
        assert name in [BASELINE] + DELTAS, name
        assert line in file_commands(name), f"vyos/{name} に無い行: {line}"
        assert reason, f"理由が空: {line}"


def test_unsupported_entries_are_unique():
    keys = [(name, line) for name, line, _ in unsupported_entries()]
    assert len(keys) == len(set(keys))


# ---- イメージの版と CI ---------------------------------------------------------------


@pytest.fixture(scope="module")
def version() -> dict[str, str]:
    pairs = {}
    for raw in (VYOS / "VERSION").read_text(encoding="utf-8").splitlines():
        if raw.strip() and not raw.startswith("#"):
            key, value = raw.split("=", 1)
            pairs[key] = value
    return pairs


def test_vyos_version_is_a_rolling_build_with_set_dscp(version):
    # §5-2: QoS の set-dscp は 2026-06-30 の rolling で入った。7 月以降のビルドを使う
    m = re.fullmatch(r"(\d{4})\.(\d{2})\.(\d{2})-\d{4}-rolling", version["VYOS_VERSION"])
    assert m
    assert (int(m.group(1)), int(m.group(2))) >= (2026, 7)


def test_iso_and_iso_to_oci_are_pinned(version):
    assert re.fullmatch(r"[0-9a-f]{64}", version["VYOS_ISO_SHA256"])
    assert re.fullmatch(r"[0-9a-f]{40}", version["VYOS_BUILD_REF"])
    assert re.fullmatch(r"[0-9a-f]{64}", version["ISO_TO_OCI_SHA256"])


@pytest.mark.parametrize("name", DELTAS)
def test_workflow_checks_every_delta(name):
    assert f"vyos/{name}" in WORKFLOW.read_text(encoding="utf-8")
