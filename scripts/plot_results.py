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


def plot_t3(base: Path) -> None:
    import json

    def load(name):
        p = base / name / "result.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    groups = [("single", "1 フロー"), ("many", "多数フロー")]
    bars = [("NDR（ロス 0%）", "tol0", "#2ca02c"), ("PDR（ロス 0.1% 未満）", "tol0.001", "#98df8a"),
            ("送信上限での転送", "sendermax", "#1f77b4")]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    width = 0.26
    sent = []
    for gi, (key, glabel) in enumerate(groups):
        for bi, (blabel, suffix, color) in enumerate(bars):
            j = load(f"64B-{key}-{suffix}")
            if not j:
                continue
            if suffix == "sendermax":
                val = j["counters"]["pc02_tx"] / 60 / 1e6
                sent.append(j["sender_max_pps"] / 1e6)
            else:
                val = j["result_pps"] / 1e6
            x = gi + (bi - 1) * width
            ax.bar(x, val, width, color=color, label=blabel if gi == 0 else None)
            ax.text(x, val, f"{val:.2f}", ha="center", va="bottom", fontsize=9)
    if sent:
        ax.axhline(st.median(sent), color=COLORS["PC01 送信"], ls=":", lw=1.5, label=f"PC01 の送信上限（約 {st.median(sent):.2f} Mpps）")
    ax.axhline(wire_pps(64) / 1e6, color=COLORS["理論値"], ls="--", lw=1, label="10GbE の理論値 14.88 Mpps")
    ax.set_ylim(0, 16)
    ax.set_xticks(range(len(groups)), [g[1] for g in groups])
    ax.set_ylabel("フレームレート [Mpps]")
    ax.legend(fontsize=9, loc="upper left")
    ax.set_title("T3: 64B の転送レート（PC02 = VyOS フェーズ1、チューニングなし）")
    save(fig, "T3-64B-ndr-pdr", "pktgen 4 スレッド。ロスは PC01 の送信数 − PC02 の送信数（NIC のカウンタ）。NDR・PDR は二分探索（幅 1%）、最終 60 秒。2026-10-25")


def plot_t4(t3_base: Path, t4_base: Path) -> None:
    import json

    stages = [("基準", t3_base, "64B-{f}-tol{t}"), ("①", t4_base / "stage1-tuning-1-offload" / "T3", "64B-{f}-tol{t}"),
              ("①②", t4_base / "stage2-tuning-2-rps" / "T3", "64B-{f}-tol{t}"),
              ("①②③", t4_base / "stage3-tuning-3-flowtable" / "T3", "64B-{f}-tol{t}"),
              ("①〜④", t4_base / "stage4-tuning-4-performance" / "T3", "64B-{f}-tol{t}")]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.3), sharey=True)
    width = 0.38
    for ax, (f, title) in zip(axes, [("single", "1 フロー"), ("many", "多数フロー")]):
        for k, (t, label, color) in enumerate([("0", "NDR（ロス 0%）", "#2ca02c"), ("0.001", "PDR（ロス 0.1% 未満）", "#98df8a")]):
            vals = []
            for _, base, pat in stages:
                p = base / pat.format(f=f, t=t) / "result.json"
                vals.append(json.loads(p.read_text())["result_pps"] / 1e6 if p.exists() else float("nan"))
            xs = [i + (k - 0.5) * width for i in range(len(stages))]
            ax.bar(xs, vals, width, color=color, label=label)
            for x, v in zip(xs, vals):
                ax.text(x, v, f"{v:.2f}", ha="center", va="bottom", fontsize=8)
        ax.axhline(2.84, color=COLORS["PC01 送信"], ls=":", lw=1.5, label="PC01 の送信上限 約 2.84")
        ax.set_xticks(range(len(stages)), [s[0] for s in stages])
        ax.set_title(title)
        ax.set_xlabel("チューニングの段階（積み上げ）")
    axes[0].set_ylabel("64B のフレームレート [Mpps]")
    axes[0].set_ylim(0, 3.2)
    axes[0].legend(fontsize=8, loc="upper left")
    fig.suptitle("T4: チューニングの段階ごとの 64B 転送レート（各条件 1 回の探索）")
    save(fig, "T4-tuning-64B", "①リング 4096 ②RPS ③flowtable ④性能プロファイル。各条件は二分探索 1 回（最終 60 秒）で n=1。ばらつきの幅は未測定。2026-10-25")


def plot_t4b(base: Path) -> None:
    import json

    configs = [("base", "基準"), ("s1", "①だけ"), ("s14", "①④"), ("s124", "①②④")]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.3), sharey=True)
    width = 0.38
    for ax, (f, title) in zip(axes, [("single", "1 フロー"), ("many", "多数フロー")]):
        for k, (t, label, color) in enumerate([("0", "NDR（ロス 0%）", "#2ca02c"), ("0.001", "PDR（ロス 0.1% 未満）", "#98df8a")]):
            stats, points = [], []
            for c, _ in configs:
                vals = [json.loads(p.read_text())["result_pps"] / 1e6
                        for p in sorted(base.glob(f"{c}/round*/64B-{f}-tol{t}/result.json"))]
                stats.append(median_range(vals))
                points.append(vals)
            xs = [i + (k - 0.5) * width for i in range(len(configs))]
            ax.bar(xs, [s[0] for s in stats], width, color=color, label=label,
                   yerr=[[s[1] for s in stats], [s[2] for s in stats]], capsize=3)
            for x, vals, s in zip(xs, points, stats):
                ax.scatter([x] * len(vals), vals, s=9, color="black", zorder=3)
                ax.text(x, s[0] + s[2] + 0.03, f"{s[0]:.2f}", ha="center", va="bottom", fontsize=8)
        ax.axhline(2.84, color=COLORS["PC01 送信"], ls=":", lw=1.5, label="PC01 の送信上限 約 2.84")
        ax.set_xticks(range(len(configs)), [c[1] for c in configs])
        ax.set_title(title)
        ax.set_xlabel("構成（毎回、基準から入れ直す）")
    axes[0].set_ylabel("64B のフレームレート [Mpps]")
    axes[0].set_ylim(0, 3.2)
    axes[0].legend(fontsize=8, loc="upper left")
    fig.suptitle("T4（確かめ直し）: 構成ごとの 64B 転送レート — 3 巡の中央値と最小〜最大")
    save(fig, "T4b-configs-64B", "①リング 4096 ②RPS ④性能プロファイル。巡回ごとに 4 構成を回し、各 n=3。点は各回。二分探索（幅 1%）、最終 60 秒。2026-10-26")


def plot_t8(base: Path) -> None:
    import json

    configs = [("s1", "①（基準）"), ("s1q", "①＋QoS（HTB）")]
    conds = [("single", "0", "1 フロー\nNDR"), ("single", "0.001", "1 フロー\nPDR"),
             ("many", "0", "多数フロー\nNDR"), ("many", "0.001", "多数フロー\nPDR")]
    fig, ax = plt.subplots(figsize=(9, 4.3))
    width = 0.38
    for k, ((c, label), color) in enumerate(zip(configs, ["#2ca02c", "#d62728"])):
        stats, pts = [], []
        for f, t, _ in conds:
            vals = [json.loads(p.read_text())["result_pps"] / 1e6 for p in sorted(base.glob(f"{c}/round*/64B-{f}-tol{t}/result.json"))]
            stats.append(median_range(vals))
            pts.append(vals)
        xs = [i + (k - 0.5) * width for i in range(len(conds))]
        ax.bar(xs, [s[0] for s in stats], width, color=color, label=label,
               yerr=[[s[1] for s in stats], [s[2] for s in stats]], capsize=3)
        for x, vals, s in zip(xs, pts, stats):
            ax.scatter([x] * len(vals), vals, s=9, color="black", zorder=3)
            ax.text(x, s[0] + s[2] + 0.03, f"{s[0]:.2f}", ha="center", va="bottom", fontsize=8)
    ax.axhline(2.84, color=COLORS["PC01 送信"], ls=":", lw=1.5, label="PC01 の送信上限 約 2.84")
    ax.set_xticks(range(len(conds)), [c[2] for c in conds])
    ax.set_ylabel("64B のフレームレート [Mpps]")
    ax.set_ylim(0, 3.2)
    ax.legend(fontsize=8, loc="upper left")
    ax.set_title("T8: QoS（shaper の set-dscp）を通したときの 64B 転送レート")
    save(fig, "T8-qos-64B", "QoS は eth2 の送信に HTB（qos-ngn-out.cmds）。試験の流れは書き換えの対象外で default のクラスを通る。各 n=3、中央値と最小〜最大。TCP は両構成とも上限（9.415 / 9.285 Gbps）。2026-10-27")


if __name__ == "__main__":
    t8 = ROOT / "results" / "T8" / "20261027"
    if t8.exists():
        plot_t8(t8)
    t4b = ROOT / "results" / "T4b" / "20261026"
    if t4b.exists():
        plot_t4b(t4b)
    t4 = ROOT / "results" / "T4" / "20261025"
    if t4.exists():
        plot_t4(ROOT / "results" / "T3" / "20261025", t4)
    t3 = ROOT / "results" / "T3" / "20261025"
    if t3.exists():
        plot_t3(t3)
    t1 = ROOT / "results" / "T1" / "20261025" / "runs.csv"
    t2 = ROOT / "results" / "T2" / "20261025" / "runs.csv"
    if t1.exists():
        plot_t1(t1)
    if t2.exists():
        plot_t2(t2)
