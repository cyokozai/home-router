"""results/ の試験結果から図を描く（RFC 2544 の報告形式に寄せる）。

    docker build -t home-router/plot -f tools/plot.Dockerfile tools
    docker run --rm -v "$PWD":/w -w /w home-router/plot python scripts/plot_results.py

図は results/figures/ に PNG と SVG で出す。値は runs.csv から読み、手で書き写さない。
"""

import csv
import statistics as st
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

plt.rcParams.update({
    "font.family": "Noto Sans CJK JP",
    "font.size": 11,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "figure.dpi": 150,
})

# 色は構成・系列ごとに固定し、すべての図で揃える
COLORS = {"IPv4": "#1f77b4", "IPv6": "#ff7f0e", "理論値": "#555555",
          "PC01 送信": "#7f7f7f", "PC02 転送": "#2ca02c", "PC03 受信": "#d62728"}
LINE_RATE = 10e9


def wire_pps(frame: int) -> float:
    return LINE_RATE / ((frame + 20) * 8)


def tcp_goodput(mss: int, family: int) -> float:
    """1 フレームに TCP の中身が mss バイト載るときの上限（Gbps、タイムスタンプ 12 バイト込み）。"""
    frame = mss + 12 + 20 + (20 if family == 4 else 40) + 14 + 4
    return wire_pps(frame) * mss * 8 / 1e9


def save(fig, name: str, note: str) -> None:
    fig.text(0.01, 0.005, note, fontsize=8, color="#555555", ha="left", va="bottom")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    for ext in ("png", "svg"):
        fig.savefig(OUT / f"{name}.{ext}")
    plt.close(fig)
    print("wrote", OUT / f"{name}.png")


def median_range(values):
    m = st.median(values)
    return m, m - min(values), max(values) - m


def plot_t1(path: Path) -> None:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    g = defaultdict(list)
    for r in rows:
        g[(int(r["family"]), int(r["parallel"]), r["direction"])].append(float(r["recv_gbps"]))
    dirs = [("up", "上り"), ("down", "下り"), ("bidir", "双方向（合計）")]
    pars = [1, 4, 16]
    fig, axes = plt.subplots(1, 3, figsize=(11, 4), sharey=True)
    width = 0.38
    for ax, (d, label) in zip(axes, dirs):
        for i, fam in enumerate((4, 6)):
            xs = [p + (i - 0.5) * width for p in range(len(pars))]
            stats = [median_range(g[(fam, p, d)]) for p in pars]
            ax.bar(xs, [s[0] for s in stats], width, color=COLORS[f"IPv{fam}"], label=f"IPv{fam}",
                   yerr=[[s[1] for s in stats], [s[2] for s in stats]], capsize=3)
            for x, p in zip(xs, pars):  # 3 回の値も点で重ねる
                ax.scatter([x] * len(g[(fam, p, d)]), g[(fam, p, d)], s=8, color="black", zorder=3)
            limit = tcp_goodput(1448 if fam == 4 else 1428, fam) * (2 if d == "bidir" else 1)
            ax.axhline(limit, color=COLORS[f"IPv{fam}"], ls="--", lw=1)
        ax.set_title(label)
        ax.set_xticks(range(len(pars)), [f"-P {p}" for p in pars])
        ax.set_xlabel("並列数")
    axes[0].set_ylabel("受信スループット [Gbps]")
    axes[0].set_ylim(0, 20)
    axes[0].legend(loc="lower left")
    fig.suptitle("T1: PC02（VyOS フェーズ1）を通す TCP — 破線は 10GbE の TCP 上限")
    save(fig, "T1-tcp-routing", "棒は 3 回の中央値、ひげは最小〜最大、点は各回（n=3、60 秒、-O 5）。2026-10-25")


def plot_t2(path: Path) -> None:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    if not rows:
        return
    g = defaultdict(list)
    for r in rows:
        g[int(r["frame"])].append(r)
    frames = sorted(g)
    fig, ax = plt.subplots(figsize=(7, 4.5))
    xs = list(range(len(frames)))
    ax.plot(xs, [wire_pps(f) for f in frames], "--", color=COLORS["理論値"], marker="_", ms=20, label="理論値（10GbE ワイヤーレート）")
    series = [("pc01_tx_pps", "PC01 送信", -0.15), ("pc02_tx_pps", "PC02 転送", 0.0), ("pc03_rx_pps", "PC03 受信", 0.15)]
    for key, label, dx in series:
        stats = [median_range([float(r[key]) for r in g[f]]) for f in frames]
        ax.errorbar([x + dx for x in xs], [s[0] for s in stats], yerr=[[s[1] for s in stats], [s[2] for s in stats]],
                    fmt="o", color=COLORS[label], capsize=3, label=label)
    ax.set_yscale("log")
    ax.set_xticks(xs, [f"{f}B" for f in frames])
    ax.set_xlabel("フレームサイズ")
    ax.set_ylabel("フレームレート [pps]（対数）")
    ax.legend(fontsize=9)
    ax.set_title("T2: UDP のフレームレート — 測る場所ごと（RFC 2544 の報告形式）")
    save(fig, "T2-udp-frame-rate", "iperf3 -u -P 4、ワイヤーレート分を送信。点は 3 回の中央値、ひげは最小〜最大。pps は ethtool -S の差分。2026-10-25")


if __name__ == "__main__":
    t1 = ROOT / "results" / "T1" / "20261025" / "runs.csv"
    t2 = ROOT / "results" / "T2" / "20261025" / "runs.csv"
    if t1.exists():
        plot_t1(t1)
    if t2.exists():
        plot_t2(t2)
