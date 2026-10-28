"""L1・L2（遅延）の ping の記録を集計する。各点 1 万回の往復時間の分位と損失率を、3 巡の中央値で出す。

    python3 -I scripts/summarize_latency.py results/L-latency/20261029 [results/L-latency-lan/20261029 ...]

summary.csv を各ディレクトリに書く。往復時間には PC01・PC03 の処理の時間も入る（専用の試験機ではない）。
"""

import csv
import re
import statistics as st
import sys
from pathlib import Path

LOADS = {"0": "0", "1000000": "1.0 Mpps", "2000000": "2.0 Mpps", "3000000": "3.0 Mpps", "20000000": "上限超え"}


def quantile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def parse(path: Path) -> dict | None:
    text = path.read_text(errors="replace")
    rtts = [float(m) for m in re.findall(r"time=([0-9.]+) ms", text)]
    sent = re.search(r"(\d+) packets transmitted, (\d+) received", text)
    if not sent:
        return None
    tx, rx = int(sent.group(1)), int(sent.group(2))
    out = {"loss": 1 - rx / tx if tx else None}
    if rtts:
        out |= {"p50": quantile(rtts, 0.5), "p99": quantile(rtts, 0.99), "p999": quantile(rtts, 0.999), "max": max(rtts)}
    return out


def main(dirs: list[str]) -> None:
    for base in map(Path, dirs):
        rows = [["構成", "背景の負荷（両向き）", "中央値 [ms]", "99% [ms]", "99.9% [ms]", "最大 [ms]", "損失率", "n"]]
        for cfg in sorted(p.name for p in base.iterdir() if p.is_dir()):
            for load, label in LOADS.items():
                runs = [r for p in sorted(base.glob(f"{cfg}/round*/ping-{load}.txt")) if (r := parse(p))]
                if not runs:
                    continue
                med = {k: st.median([r[k] for r in runs if k in r]) if any(k in r for r in runs) else None
                       for k in ("p50", "p99", "p999", "max", "loss")}
                fmt = lambda v, f: "―" if v is None else f.format(v)  # noqa: E731
                rows.append([cfg, label, fmt(med["p50"], "{:.3f}"), fmt(med["p99"], "{:.3f}"), fmt(med["p999"], "{:.3f}"),
                             fmt(med["max"], "{:.1f}"), fmt(med["loss"], "{:.2%}"), len(runs)])
        with (base / "summary.csv").open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        for r in rows:
            print(r)


if __name__ == "__main__":
    main(sys.argv[1:])
