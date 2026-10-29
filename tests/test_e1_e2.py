"""E1（RFC 2544 の残りのフレーム長）と E2（ロス率の曲線を 3 巡）の準備の検査。

実機には触らない。scripts/run-e1-e2.sh の文法と中身（理論値の表、E2 の点が 1 巡目と同じこと）と、
scripts/summarize_e1_e2.py が小さな結果の JSON から summary.csv を作れることを確かめる。
"""

import csv
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "run-e1-e2.sh"
EXTRA = ROOT / "scripts" / "run-extra.sh"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass が型の解決にモジュールを引くため、実行前に登録する
    spec.loader.exec_module(mod)
    return mod


t3 = load("t3_search")
summ = load("summarize_e1_e2")

E1_SIZES = (128, 256, 1024, 1280, 1518)


# ---- 理論値（ワイヤーレート） ------------------------------------------------


@pytest.mark.parametrize(("frame", "pps"), [(128, 8_445_946), (256, 4_528_986), (1024, 1_197_318),
                                            (1280, 961_538), (1518, 812_744)])
def test_wire_rate_pps_for_e1_sizes(frame, pps):
    # 10e9 / ((フレーム長 + 20) × 8)。片向き
    assert round(t3.wire_rate_pps(frame)) == pps


@pytest.mark.parametrize("frame", E1_SIZES)
def test_pktgen_size_fits_mtu(frame):
    # pktgen の pkt_size は FCS を除いた長さ。1518B でも 1514（IP で 1500）に収まる
    assert t3.pktgen_size(frame) == frame - 4
    assert t3.pktgen_size(frame) <= 1514


def test_runner_header_lists_wire_rate_per_size():
    """ヘッダーの表（片向き・両向き）が式と一致する。"""
    text = RUNNER.read_text()
    rows = {int(m[1]): (int(m[2].replace(",", "")), int(m[3].replace(",", "")))
            for m in re.finditer(r"^#\s+(\d+)B\s+([\d,]+)\s+([\d,]+)\s*$", text, flags=re.M)}
    assert sorted(rows) == list(E1_SIZES)
    for frame, (one, both) in rows.items():
        assert one == round(t3.wire_rate_pps(frame))
        assert both == round(2 * t3.wire_rate_pps(frame))


def test_runner_e1_sizes():
    m = re.search(r"^e1_sizes=\(([^)]*)\)", RUNNER.read_text(), flags=re.M)
    assert m, "e1_sizes=(...) が無い"
    assert tuple(int(x) for x in m[1].split()) == E1_SIZES


# ---- E2 の点が 1 巡目（run-extra.sh）と同じ ----------------------------------


def extra_e2_section() -> str:
    text = EXTRA.read_text()
    return text[text.index("# ---- E2"):]


def test_e2_pct_list_matches_first_round():
    old = re.search(r"for pct in ([\d ]+); do", extra_e2_section())[1].split()
    new = re.search(r"^e2_pcts=\(([^)]*)\)", RUNNER.read_text(), flags=re.M)[1].split()
    assert new == old


def test_e2_64b_rate_list_matches_first_round():
    old = re.search(r"for rate in ([\d ]+); do", extra_e2_section())[1].split()
    new = re.search(r"^e2_64b_rates=\(([^)]*)\)", RUNNER.read_text(), flags=re.M)[1].split()
    assert new == old


def test_e2_wire512_matches_first_round_and_formula():
    old = re.search(r"wire512=(\d+)", extra_e2_section())[1]
    new = re.search(r"^wire512=(\d+)", RUNNER.read_text(), flags=re.M)[1]
    assert new == old
    assert int(new) == int(2 * t3.wire_rate_pps(512))


# ---- 実行する手順の約束 ------------------------------------------------------


def test_runner_restores_base_on_exit_and_makes_log_dirs_first():
    text = RUNNER.read_text()
    assert re.search(r"^trap '.*to_base.*' EXIT", text, flags=re.M)
    # ログの置き場所を、リダイレクトより前に作る（2026-10-29 実害: 作っておらず 1 本も流れなかった）
    # 各点は fresh（その中で mkdir -p）を通ってから、ログをその中へリダイレクトする
    first_redirect = text.index('> "${d}/')
    assert text.index('mkdir -p "$1"') < first_redirect
    assert text.count('> "${d}/') == text.count('fresh "${d}"')


def _run_apply(tmp_path: Path, to_base_rc: int, commit_rc: int) -> int:
    """run-e1-e2.sh の apply() だけを取り出し、to_base と pc02-commit.sh を差し替えて戻り値を見る。"""
    line = re.search(r"^apply\(\) \{.*\}$", RUNNER.read_text(), flags=re.M)[0]
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "pc02-commit.sh").write_text(f"exit {commit_rc}\n")
    script = f"sleep() {{ :; }}\nto_base() {{ return {to_base_rc}; }}\n{line}\napply x\n"
    return subprocess.run(["bash", "-c", script], cwd=tmp_path, check=False).returncode


@pytest.mark.parametrize(("to_base_rc", "commit_rc", "ok"), [(0, 0, True), (1, 0, False), (0, 3, False)])
def test_runner_apply_propagates_failure(tmp_path, to_base_rc, commit_rc, ok):
    # 設定の投入に失敗したら apply は失敗を返す（sleep の成功で隠さない）
    assert (_run_apply(tmp_path, to_base_rc, commit_rc) == 0) is ok


def test_runner_stops_when_apply_fails():
    # 設定の投入に失敗したまま、別の設定で測り続けない
    assert re.search(r"^apply .*\|\| \{.*exit 1; \}$", RUNNER.read_text(), flags=re.M)


def test_runner_skips_existing_results():
    assert "result.json ]]" in RUNNER.read_text()


def test_runner_syntax():
    subprocess.run(["bash", "-n", str(RUNNER)], check=True)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck が無い（Docker の中で確かめる）")
def test_runner_shellcheck():
    subprocess.run(["shellcheck", "-x", str(RUNNER)], check=True)


# ---- 集計 --------------------------------------------------------------------


def search_json(frame: int, tol: float, pps: float, sender_limited: bool = False) -> dict:
    """results/P3-sizes/20261028/round1/512-many-bidir-tol0.001/result.json と同じ形（試行は省く）。"""
    wire = 2 * t3.wire_rate_pps(frame)
    return {"test": "T3", "started": "2026-10-30T01:00:00+09:00", "frame": frame, "flows": "many", "threads": 4,
            "burst": 1, "bidir": True, "family": 4,
            "flow": {"src": "192.168.10.10", "dst": "192.168.20.2", "sink": "192.168.10.99",
                     "reply_src": "192.168.20.2", "extra": {}},
            "wire_rate_pps": wire, "tolerance": tol, "precision": 0.01, "trial_seconds": 10, "final_seconds": 60,
            "result_pps": pps, "searched_upper_pps": wire, "result_ratio_of_wire": pps / wire,
            "sender_limited": sender_limited,
            "final_counters": {"pc01_tx": 1000, "pc02_rx": 1000, "pc02_tx": 1000, "pc03_rx": 900}, "trials": []}


def fixed_json(frame: int, rate: float, sent: float, loss: float) -> dict:
    """results/E-extra/20261029/E2/512B-50pct/result.json と同じ形。"""
    return {"test": "T3", "started": "2026-10-30T01:00:00+09:00", "frame": frame, "flows": "many", "threads": 4,
            "burst": 1, "bidir": True, "family": 4,
            "flow": {"src": "192.168.10.10", "dst": "192.168.20.2", "sink": "192.168.10.99",
                     "reply_src": "192.168.20.2", "extra": {}},
            "pc02_before": None, "wire_rate_pps": 2 * t3.wire_rate_pps(frame), "fixed_pps": rate,
            "runs": [{"at": "2026-10-30T01:00:30+09:00",
                      "counters": {"pc01_tx": 100, "pc02_rx": 100, "pc02_tx": 100, "pc03_rx": 90},
                      "sent_pps": sent, "dut_loss_ratio": loss}],
            "total_loss_ratio": loss}


def put(path: Path, d: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "result.json").write_text(json.dumps(d, ensure_ascii=False))


def read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


def test_fmt_median_range():
    assert summ.median_range([3.69e6, 3.97e6, 3.89e6]) == "3.89 [3.69-3.97]"
    assert summ.median_range([5.6e6]) == "5.60 [5.60-5.60]"
    assert summ.median_range([]) == "―"


def test_summarize_e1(tmp_path):
    for r, (ndr, pdr) in enumerate([(4.0e6, 5.0e6), (3.8e6, 5.2e6), (3.9e6, 5.1e6)], start=1):
        put(tmp_path / f"round{r}" / "128-many-bidir-tol0", search_json(128, 0, ndr, sender_limited=True))
        put(tmp_path / f"round{r}" / "128-many-bidir-tol0.001", search_json(128, 0.001, pdr, sender_limited=True))
        w = 2 * t3.wire_rate_pps(1024)
        put(tmp_path / f"round{r}" / "1024-many-bidir-tol0", search_json(1024, 0, w))
        put(tmp_path / f"round{r}" / "1024-many-bidir-tol0.001", search_json(1024, 0.001, w))
    summ.summarize_e1(tmp_path)
    rows = read_csv(tmp_path / "summary.csv")
    assert rows[0] == ["ID", "フレーム長", "理論値（両向き） [Mpps]", "NDR 中央値 [範囲] [Mpps]",
                       "PDR 中央値 [範囲] [Mpps]", "理論値との比（PDR）", "n", "送信側の頭打ち"]
    assert rows[1] == ["E1", "128B", "16.89", "3.90 [3.80-4.00]", "5.10 [5.00-5.20]", "30%", "3", "あり"]
    assert rows[2] == ["E1", "1024B", "2.39", "2.39 [2.39-2.39]", "2.39 [2.39-2.39]", "100%", "3", ""]
    assert rows[-1][0] == "" and "双方向" in rows[-1][-1]


def test_summarize_e2(tmp_path):
    for r, loss in enumerate([0.0, 1e-4, 2e-4], start=1):
        put(tmp_path / f"round{r}" / "512B-50pct", fixed_json(512, 2349624, 2.34e6 + r * 1e3, loss))
        put(tmp_path / f"round{r}" / "512B-100pct", fixed_json(512, 4699248, 4.69e6, loss))
        put(tmp_path / f"round{r}" / "64B-20000000", fixed_json(64, 20_000_000, 5.8e6, 0.03 * r))
        put(tmp_path / f"round{r}" / "64B-4500000", fixed_json(64, 4_500_000, 4.49e6, 0.0))
    summ.summarize_e2(tmp_path)
    rows = read_csv(tmp_path / "summary.csv")
    assert rows[0] == ["ID", "フレーム長", "要求（両向き） [Mpps]", "理論値に対する要求",
                       "送信 中央値 [範囲] [Mpps]", "ロス率 中央値 [範囲]", "n"]
    # 512B は割合の小さい順、続いて 64B はレートの小さい順
    assert [r[1:4] for r in rows[1:5]] == [["512B", "2.35", "50%"], ["512B", "4.70", "100%"],
                                          ["64B", "4.50", "15%"], ["64B", "20.00", "67%"]]
    assert rows[1][4] == "2.34 [2.34-2.34]"
    assert rows[1][5] == "1.00e-04 [0.00e+00-2.00e-04]"
    assert rows[4][5] == "6.00e-02 [3.00e-02-9.00e-02]"
    assert rows[1][6] == "3"


def test_summarize_ignores_missing_rounds(tmp_path):
    put(tmp_path / "round1" / "256-many-bidir-tol0", search_json(256, 0, 4.0e6))
    summ.summarize_e1(tmp_path)
    rows = read_csv(tmp_path / "summary.csv")
    assert rows[1][1] == "256B" and rows[1][4] == "―" and rows[1][6] == "1"


def test_summarize_main_dispatches_by_layout(tmp_path):
    e1, e2 = tmp_path / "E1-sizes" / "d", tmp_path / "E2-curve" / "d"
    put(e1 / "round1" / "1518-many-bidir-tol0", search_json(1518, 0, 1.6e6))
    put(e2 / "round1" / "64B-1000000", fixed_json(64, 1_000_000, 1e6, 0.0))
    summ.main([str(e1), str(e2)])
    assert read_csv(e1 / "summary.csv")[1][1] == "1518B"
    assert read_csv(e2 / "summary.csv")[1][1] == "64B"
