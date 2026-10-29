"""E1（RFC 2544 の残りのフレーム長）と E2（ロス率の曲線、3 巡）の結果を集計し、各ディレクトリに summary.csv を書く。

    python3 -I scripts/summarize_e1_e2.py results/E1-sizes/<日付> results/E2-curve/<日付>

置き場所の形で見分ける（scripts/run-e1-e2.sh が作る形）。
  E1: <dir>/round<r>/<フレーム長>-many-bidir-tol<0|0.001>/result.json（探索の結果。result_pps）
  E2: <dir>/round<r>/<512B-<割合>pct | 64B-<pps>>/result.json（決まったレートの結果。runs）
値は巡回をまたいだ中央値と [最小-最大]（results/T3-bidir-configs/20261028/summary.csv と同じ書き方）。
巡回が欠けていても、ある分だけで集計する（n の列に数を書く）。標準ライブラリだけで動く。
"""

import csv
import json
import re
import statistics as st
import sys
from pathlib import Path

E1_DIR = re.compile(r"^(\d+)-many-bidir-tol(0|0\.001)$")
E2_DIR = re.compile(r"^(512B-(\d+)pct|64B-(\d+))$")


def median_range(xs: list[float], scale: float = 1e6, fmt: str = "{:.2f}") -> str:
    """中央値 [最小-最大]。既定は pps を Mpps にして小数 2 桁。"""
    if not xs:
        return "―"
    v = [x / scale for x in xs]
    return f"{fmt.format(st.median(v))} [{fmt.format(min(v))}-{fmt.format(max(v))}]"


def results(base: Path, pattern: re.Pattern) -> dict[str, list[dict]]:
    """名前（round の下のディレクトリ名）ごとに、各巡回の result.json を集める。"""
    out: dict[str, list[dict]] = {}
    for p in sorted(base.glob("round*/*/result.json")):
        if pattern.match(p.parent.name):
            out.setdefault(p.parent.name, []).append(json.loads(p.read_text(encoding="utf-8")))
    return out


def write(base: Path, rows: list[list]) -> None:
    with (base / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    for r in rows:
        print(",".join(map(str, r)))


def summarize_e1(base: Path) -> None:
    found = results(base, E1_DIR)
    frames = sorted({int(E1_DIR.match(n)[1]) for n in found})
    rows: list[list] = [["ID", "フレーム長", "理論値（両向き） [Mpps]", "NDR 中央値 [範囲] [Mpps]",
                         "PDR 中央値 [範囲] [Mpps]", "理論値との比（PDR）", "n", "送信側の頭打ち"]]
    for frame in frames:
        ndr = found.get(f"{frame}-many-bidir-tol0", [])
        pdr = found.get(f"{frame}-many-bidir-tol0.001", [])
        wire = (ndr + pdr)[0]["wire_rate_pps"]
        ratio = f"{st.median(r['result_pps'] for r in pdr) / wire:.0%}" if pdr else "―"
        limited = "あり" if any(r.get("sender_limited") for r in ndr + pdr) else ""
        rows.append(["E1", f"{frame}B", f"{wire / 1e6:.2f}", median_range([r["result_pps"] for r in ndr]),
                     median_range([r["result_pps"] for r in pdr]), ratio, max(len(ndr), len(pdr)), limited])
    rows.append([""] * 7 + ["多数フロー、双方向（PC01 → PC03 と PC03 → 捨て先）の合計。NDR はロス 0、PDR はロス率 0.1% まで。"
                            "PC02 は ①（tuning-1-offload ＋ test-sink）。送信側の頭打ちは要求に送信が届かなかった試行があったもの"])
    write(base, rows)


def e2_key(name: str) -> tuple[int, int]:
    m = E2_DIR.match(name)
    return (0, int(m[2])) if m[2] else (1, int(m[3]))


def summarize_e2(base: Path) -> None:
    found = results(base, E2_DIR)
    rows: list[list] = [["ID", "フレーム長", "要求（両向き） [Mpps]", "理論値に対する要求",
                         "送信 中央値 [範囲] [Mpps]", "ロス率 中央値 [範囲]", "n"]]
    for name in sorted(found, key=e2_key):
        rs = found[name]
        rate, wire = rs[0]["fixed_pps"], rs[0]["wire_rate_pps"]
        sent = [st.median(x["sent_pps"] for x in r["runs"]) for r in rs]
        loss = [r["total_loss_ratio"] for r in rs if r.get("total_loss_ratio") is not None]
        rows.append(["E2", f"{rs[0]['frame']}B", f"{rate / 1e6:.2f}", f"{rate / wire:.0%}", median_range(sent),
                     median_range(loss, scale=1, fmt="{:.2e}"), len(rs)])
    rows.append([""] * 6 + ["多数フロー、双方向の合計。決まったレートで 30 秒ずつ。ロス率は PC02 が送り出さなかった割合。"
                            "64B の 20 Mpps は送れるだけの速さ（送信側の上限は両向きで約 5.8 Mpps）"])
    write(base, rows)


def main(dirs: list[str]) -> None:
    for base in map(Path, dirs):
        names = {p.parent.name for p in base.glob("round*/*/result.json")}
        if any(E1_DIR.match(n) for n in names):
            summarize_e1(base)
        elif any(E2_DIR.match(n) for n in names):
            summarize_e2(base)
        else:
            print(f"{base}: E1・E2 の結果が無い", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
