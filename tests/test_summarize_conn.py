"""scripts/summarize_conn.py（N1・N2 の集計）を、作った結果のディレクトリで流して確かめる。"""

import csv
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "summarize_conn.py"


def _round(base: Path, c: str, r: int, *, done: bool = True, tcp: bool = True, count: int = 1000) -> Path:
    d = base / c / f"round{r}"
    (d / "C1-new-flows").mkdir(parents=True)
    (d / "C1-new-flows" / "result.json").write_text(json.dumps({"result_pps": 2_000_000}))
    if c == "s1":
        return d
    (d / "C2-fill.log").write_text(f"t=0 count=0 max=262144 slab_bytes=0\nt=5 count={count} max=262144 slab_bytes={2**20}\n")
    if done:
        (d / "C2-fill").mkdir()
        (d / "C2-fill" / "result.json").write_text("{}")
    if tcp:
        (d / "C2-tcp.json").write_text(json.dumps({"intervals": [{"sum": {"bits_per_second": 5e8}}]}))
    return d


def _run(base: Path) -> list[list[str]]:
    subprocess.run([sys.executable, "-I", str(SCRIPT), str(base)], check=True, capture_output=True)
    with (base / "summary.csv").open(encoding="utf-8") as f:
        return list(csv.reader(f))


def test_missing_tcp_json_does_not_stop_summary(tmp_path):
    _round(tmp_path, "s1fw", 1, tcp=False)
    rows = _run(tmp_path)
    s1fw = rows[2]
    assert s1fw[2] == "1,000"
    assert s1fw[5] == "―"


def test_unfinished_c2_round_is_not_counted(tmp_path):
    # 途中で止まった巡回は C2-fill.log だけが残り、C2-fill/result.json が無い。集計に入れない
    _round(tmp_path, "s1fw", 1, count=1000)
    _round(tmp_path, "s1fw", 2, count=999_999, done=False)
    rows = _run(tmp_path)
    assert rows[2][2] == "1,000"
