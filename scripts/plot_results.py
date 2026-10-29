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


def plot_t5b1(base: Path) -> None:
    import json

    configs = [("s1", "①（基準）", "#2ca02c"), ("s1fw", "＋ステートフル FW", "#ff7f0e"),
               ("s1fwft", "＋FW＋flowtable", "#9467bd"), ("s1dfw", "＋firewall の set dscp", "#1f77b4")]
    conds = [("single", "0", "1 フロー\nNDR"), ("single", "0.001", "1 フロー\nPDR"),
             ("many", "0", "多数フロー\nNDR"), ("many", "0.001", "多数フロー\nPDR")]
    fig, ax = plt.subplots(figsize=(11, 4.5))
    width = 0.2
    for k, (c, label, color) in enumerate(configs):
        stats, pts = [], []
        for f, t, _ in conds:
            vals = [json.loads(p.read_text())["result_pps"] / 1e6 for p in sorted(base.glob(f"{c}/round*/64B-{f}-tol{t}/result.json"))]
            stats.append(median_range(vals))
            pts.append(vals)
        xs = [i + (k - 1.5) * width for i in range(len(conds))]
        ax.bar(xs, [s[0] for s in stats], width, color=color, label=label,
               yerr=[[s[1] for s in stats], [s[2] for s in stats]], capsize=2)
        for x, vals, s in zip(xs, pts, stats):
            ax.scatter([x] * len(vals), vals, s=7, color="black", zorder=3)
            ax.text(x, s[0] + s[2] + 0.03, f"{s[0]:.2f}", ha="center", va="bottom", fontsize=7)
    ax.axhline(2.84, color=COLORS["PC01 送信"], ls=":", lw=1.5, label="PC01 の送信上限 約 2.84")
    ax.set_xticks(range(len(conds)), [c[2] for c in conds])
    ax.set_ylabel("64B のフレームレート [Mpps]")
    ax.set_ylim(0, 3.2)
    ax.legend(fontsize=8, loc="upper left", ncol=2)
    ax.set_title("T5・B1: ステートフル FW と firewall の DSCP 書き換えのコスト（64B）")
    save(fig, "T5-B1-64B", "すべて ① リング 4096 の上に足した構成。各 n=3、棒は中央値、ひげは最小〜最大、点は各回。2026-10-27")


def plot_bidir(base: Path, oneway: Path) -> None:
    import json

    conds = [("single", "0", "1 フロー\nNDR"), ("single", "0.001", "1 フロー\nPDR"),
             ("many", "0", "多数フロー\nNDR"), ("many", "0.001", "多数フロー\nPDR")]
    series = [("片方向（PC01 → PC03）", "#2ca02c", lambda f, t: oneway.glob(f"s1/round*/64B-{f}-tol{t}/result.json")),
              ("双方向（両向きの合計）", "#1f77b4", lambda f, t: base.glob(f"round*/64B-{f}-bidir-tol{t}/result.json"))]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    width = 0.35
    for k, (label, color, files) in enumerate(series):
        stats, pts = [], []
        for f, t, _ in conds:
            vals = [json.loads(p.read_text())["result_pps"] / 1e6 for p in sorted(files(f, t))]
            stats.append(median_range(vals))
            pts.append(vals)
        xs = [i + (k - 0.5) * width for i in range(len(conds))]
        ax.bar(xs, [s[0] for s in stats], width, color=color, label=label,
               yerr=[[s[1] for s in stats], [s[2] for s in stats]], capsize=2)
        for x, vals, s in zip(xs, pts, stats):
            ax.scatter([x] * len(vals), vals, s=7, color="black", zorder=3)
            ax.text(x, s[0] + s[2] + 0.05, f"{s[0]:.2f}", ha="center", va="bottom", fontsize=8)
    ax.axhline(2.84, color=COLORS["PC01 送信"], ls=":", lw=1.5, label="PC01 1 台の送信上限 約 2.84")
    smax = json.loads((base / "64B-many-bidir-sendermax" / "result.json").read_text())
    # 送信上限の試行は 60 秒。pc01_tx・pc02_tx は両向きの合計（t3_search.bidir_counters）
    sent, fwd = smax["sender_max_pps"] / 1e6, smax["counters"]["pc02_tx"] / 60 / 1e6
    ax.scatter([3.35], [fwd], marker="x", color="#d62728", zorder=4)
    ax.annotate(f"制限なしで {sent:.2f} 送ると\n転送は {fwd:.2f} に落ちる（×）", xy=(3.35, fwd), xytext=(2.45, 5.7),
                fontsize=8, arrowprops={"arrowstyle": "->", "color": "#d62728"}, color="#d62728")
    ax.set_xticks(range(len(conds)), [c[2] for c in conds])
    ax.set_ylabel("64B のフレームレート [Mpps]")
    ax.set_ylim(0, 6.5)
    ax.legend(fontsize=8, loc="upper left")
    ax.set_title("双方向の送信: PC02（① リング 4096）の 64B の上限")
    save(fig, "T3-bidir-64B", "双方向は PC01 → PC03 と PC03 → 捨て先を同時に送った合計。片方向は T5-B1 の ①。各 n=3、棒は中央値、ひげは最小〜最大。2026-10-27")


def mape_ports(path: Path) -> list[int]:
    """conntrack の行から、返りの宛先ポート（= 変換後の送信元ポート）を並べる。"""
    import re

    ports = []
    for line in path.read_text().splitlines():
        found = re.findall(r"dport=(\d+)", line)
        if len(found) == 2:
            ports.append(int(found[1]))
    return ports


def plot_mape(base: Path) -> None:
    import json

    configs = [("p2", "ポートセット 1 組（§6-3 の元の形）", "#ff7f0e"),
               ("p2b", "15 組に振り分け（B2）", "#1f77b4")]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.5), gridspec_kw={"width_ratios": [1, 1.4]})
    width = 0.35
    for k, (c, label, color) in enumerate(configs):
        for i, par in enumerate((4, 32)):
            vals = []
            for p in sorted(base.glob(f"{c}/round*/T6-P{par}.json")):
                try:
                    vals.append(json.loads(p.read_text())["end"]["sum_received"]["bits_per_second"] / 1e9)
                except (ValueError, KeyError):
                    vals.append(0.0)
            x = i + (k - 0.5) * width
            m, lo, hi = median_range(vals)
            ax1.bar(x, m, width, color=color, label=label if i == 0 else None, yerr=[[lo], [hi]], capsize=2)
            ax1.scatter([x] * len(vals), vals, s=7, color="black", zorder=3)
            ax1.text(x, max(m + hi, 0.2) + 0.1, f"{m:.2f}" if m else "失敗\n（全回）", ha="center", va="bottom", fontsize=8)
    # MSS 1420（adjust-mss）からタイムスタンプ 12 を引いた 1408 バイトが、内側 IPv4 20＋外側 IPv6 40 を足して 1518 のフレームに載る
    limit = wire_pps(1518) * 1408 * 8 / 1e9
    ax1.axhline(limit, color="#555555", ls="--", lw=1, label=f"ipip6 越しの理論値 {limit:.2f}")
    ax1.set_xticks([0, 1], ["-P 4", "-P 32"])
    ax1.set_ylabel("TCP の受信 [Gbps]")
    ax1.set_ylim(0, 12.5)
    ax1.legend(fontsize=8, loc="upper center")
    ax1.set_title("T6: MAP-E 越しの TCP（60 秒）")

    for k, (c, label, color) in enumerate(configs):
        counts = [0] * 15
        for port in mape_ports(base / c / "round1" / "B2-conntrack.txt"):
            a = (port - 0xFE0) // 4096
            if 1 <= a <= 15:
                counts[a - 1] += 1
        xs = [a + (k - 0.5) * width for a in range(1, 16)]
        ax2.bar(xs, counts, width, color=color, label=f"{label}: conntrack 計 {sum(counts)} 本")
    ax2.axhline(16, color="#d62728", ls=":", lw=1.2, label="1 組の上限 16 ポート")
    ax2.set_xticks(range(1, 16))
    ax2.set_xlabel("ポートセットの番号 A（変換後のポート A×4096+4064 〜 +15）")
    ax2.set_ylabel("-P 32 中の接続数")
    ax2.legend(fontsize=8, loc="upper right")
    ax2.set_title("B2: 変換後のポートがどの組に入ったか（巡回 1）")
    save(fig, "T6-B2-mape", "PC02 は ①＋MAP-E。BR 役は PC03。-P 32 は データ 32 本＋制御 1 本 = 33 接続。2026-10-27")


def _pps(pattern: str) -> list[float]:
    import json

    return [json.loads(p.read_text())["result_pps"] for p in sorted(ROOT.glob(pattern))]


def plot_frame_sizes() -> None:
    """RFC 2544 の報告の形: フレーム長ごとに、ロス 0.1% 未満で通せた量を理論値に対する割合で示す（PC02 は ①）。"""
    rows = [  # (ラベル, PDR の値の並び [pps], 理論値 [pps], 注記)
        ("64B\nIPv4", _pps("results/T3-bidir-configs/20261028/s1/round*/64B-many-bidir-tol0.001/result.json"), 2 * wire_pps(64), "送信側の上限に近い"),
        ("78B\nIPv4", _pps("results/P1-ipv6/20261028/round*/78B-many-v4-bidir-tol0.001/result.json"), 2 * wire_pps(78), "送信側の上限に近い"),
        ("78B\nIPv6", _pps("results/P1-ipv6/20261028/round*/78B-many-v6-bidir-tol0.001/result.json"), 2 * wire_pps(78), "送信側の上限に近い"),
        ("512B\nIPv4", _pps("results/P3-sizes/20261028/round*/512-many-bidir-tol0.001/result.json"), 2 * wire_pps(512), ""),
    ]
    imix = _pps("results/P3-sizes/20261028/round*/imix-many-bidir-tol0.001/result.json")
    if imix:
        mean = (7 * 64 + 4 * 570 + 1518) / 12
        rows.append(("IMIX\n7:4:1", imix, 2 * LINE_RATE / ((mean + 20) * 8), "送信側の上限"))
    fig, ax = plt.subplots(figsize=(10, 4.4))
    for i, (label, vals, wire, note) in enumerate(rows):
        pct = [v / wire * 100 for v in vals]
        m, lo, hi = median_range(pct)
        ax.bar(i, m, 0.6, color="#1f77b4", yerr=[[lo], [hi]], capsize=3)
        ax.scatter([i] * len(pct), pct, s=8, color="black", zorder=3)
        ax.text(i, m + hi + 2, f"{m:.1f}%\n{st.median(vals) / 1e6:.2f} Mpps", ha="center", va="bottom", fontsize=8)
        if note:
            ax.text(i, 3, note, ha="center", va="bottom", fontsize=7, color="white")
    # 1518B は T2（iperf3 の UDP、片方向）。送信が理論値の 97.8% で頭打ち、PC02 のロスは 0〜5 個
    i = len(rows)
    ax.bar(i, 97.8, 0.6, color="#9ecae1", hatch="//", edgecolor="#1f77b4")
    ax.text(i, 99.8, "97.8% 以上\n（送信側の頭打ち）", ha="center", va="bottom", fontsize=8)
    ax.set_xticks(range(i + 1), [r[0] for r in rows] + ["1518B\nIPv4（T2）"])
    ax.set_ylabel("PDR / 理論値 [%]")
    ax.set_ylim(0, 118)
    ax.axhline(100, color="#555555", ls="--", lw=1)
    ax.set_title("フレーム長ごとの転送能力（PDR、ロス 0.1% 未満。PC02 は ①）")
    save(fig, "summary-frame-size", "64B〜512B・IMIX は pktgen の双方向（理論値は両向きの合計）、各 n=3、棒は中央値、ひげは最小〜最大。1518B は T2（片方向）。2026-10-29")


def plot_p1_p2() -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.4))
    width = 0.38
    for k, (fam, color) in enumerate(((4, "#1f77b4"), (6, "#ff7f0e"))):
        for i, tol in enumerate(("0", "0.001")):
            vals = [v / 1e6 for v in _pps(f"results/P1-ipv6/20261028/round*/78B-many-v{fam}-bidir-tol{tol}/result.json")]
            m, lo, hi = median_range(vals)
            x = i + (k - 0.5) * width
            ax1.bar(x, m, width, color=color, label=f"IPv{fam}" if i == 0 else None, yerr=[[lo], [hi]], capsize=2)
            ax1.scatter([x] * len(vals), vals, s=7, color="black", zorder=3)
            ax1.text(x, m + hi + 0.06, f"{m:.2f}", ha="center", va="bottom", fontsize=8)
    ax1.set_xticks([0, 1], ["NDR（ロス 0%）", "PDR（ロス 0.1% 未満）"])
    ax1.set_ylabel("78B のフレームレート [Mpps]（両向きの合計）")
    ax1.set_ylim(0, 6.8)
    ax1.legend(fontsize=8, loc="upper left")
    ax1.set_title("P1: IPv4 と IPv6（78B、多数フロー、双方向）")

    conds = [("s1", "many", "素のルーティング\n多数フロー", "#2ca02c"), ("p2b", "many", "MAP-E\n多数フロー", "#9467bd"),
             ("p2b", "single", "MAP-E\n1 フロー", "#c5b0d5")]
    for i, (c, f, label, color) in enumerate(conds):
        vals = [v / 1e6 for v in _pps(f"results/P2-mape-pps/20261028/{c}/round*/64B-{f}-tol0.001/result.json")]
        m, lo, hi = median_range(vals)
        ax2.bar(i, m, 0.6, color=color, yerr=[[lo], [hi]], capsize=2)
        ax2.scatter([i] * len(vals), vals, s=7, color="black", zorder=3)
        ax2.text(i, m + hi + 0.05, f"{m:.2f}", ha="center", va="bottom", fontsize=8)
    ax2.axhline(2.70, color=COLORS["PC01 送信"], ls=":", lw=1.5, label="PC01 の送信上限（片方向）約 2.70")
    ax2.set_xticks(range(len(conds)), [c[2] for c in conds])
    ax2.set_ylabel("64B の PDR [Mpps]（片方向）")
    ax2.set_ylim(0, 3.2)
    ax2.legend(fontsize=8, loc="upper right")
    ax2.set_title("P2: MAP-E（ipip6＋NAT 15 組）の転送能力")
    save(fig, "P1-P2", "P1 は PC02 ①、捨て先あり。P2 は宛先 256 × 送信元ポート 240 の流れ。各 n=3、棒は中央値、ひげは最小〜最大。2026-10-29")


def plot_latency() -> None:
    """L1・L2: 背景の負荷ごとの、PC02 自身のロス率（リング 512 / 4096）と、ping の往復時間（LAN → WAN）。"""
    import json
    import re

    loads = [1000000, 2000000, 3000000]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.4))
    for c, label, color in (("base", "リング 512", "#d62728"), ("s1", "リング 4096（①）", "#1f77b4")):
        meds = []
        for load in loads:
            vals = []
            for p in sorted(ROOT.glob(f"results/L-latency/20261029/{c}/round*/load-{load}/result.json")):
                vals.append(max(json.loads(p.read_text())["runs"][0]["dut_loss_ratio"], 1e-7))
            meds.append(st.median(vals))
            ax1.scatter([load / 1e6] * len(vals), vals, s=8, color=color, zorder=3)
        ax1.plot([x / 1e6 for x in loads], meds, color=color, marker="o", label=label)
    ax1.set_yscale("log")
    ax1.axhline(1e-3, color="#555555", ls="--", lw=1, label="判定の基準 0.1%")
    ax1.set_xlabel("背景の負荷 [Mpps]（64B 多数フロー、両向きの合計）")
    ax1.set_ylabel("PC02 のロス率")
    ax1.legend(fontsize=8, loc="upper left")
    ax1.set_title("L2: 負荷ごとの PC02 のロス率")

    def rtts(path: Path) -> list[float]:
        return [float(m) for m in re.findall(r"time=([0-9.]+) ms", path.read_text(errors="replace"))]

    xs = [0] + loads
    for c, label, color in (("s1", "①", "#1f77b4"), ("prod", "本番に近い構成", "#ff7f0e")):
        p50, p99 = [], []
        for load in xs:
            runs = [rtts(p) for p in sorted(ROOT.glob(f"results/L-latency-lan/20261029/{c}/round*/ping-{load}.txt"))]
            runs = [r for r in runs if r]
            p50.append(st.median([sorted(r)[len(r) // 2] for r in runs]))
            p99.append(st.median([sorted(r)[int(len(r) * 0.99)] for r in runs]))
        ax2.plot([x / 1e6 for x in xs], p50, color=color, marker="o", label=f"{label} 中央値")
        ax2.plot([x / 1e6 for x in xs], p99, color=color, marker="s", ls="--", label=f"{label} 99% 値")
    ax2.set_xlabel("背景の負荷 [Mpps]")
    ax2.set_ylabel("ping の往復時間 [ms]（PC01 → PC03）")
    ax2.legend(fontsize=8, loc="upper left")
    ax2.set_title("L1・L2: ping の往復時間（端末の処理を含む）")
    save(fig, "L-latency", "背景の負荷は pktgen 3 スレッド。ping は 2 ms おきに 1 万回、各 n=3 の中央値。2 Mpps 以上の ping の損失 17〜25% は端末側。2026-10-29")


def plot_conn() -> None:
    """N2: conntrack を新しい流れで埋めたときの件数の推移（表の既定と拡大）。"""
    import re

    fig, ax = plt.subplots(figsize=(10, 4.2))
    for c, label, color in (("s1fw", "既定（262,144）", "#ff7f0e"), ("s1fwbig", "拡大（4,194,304）", "#1f77b4")):
        for r in (1, 2, 3):
            path = ROOT / f"results/C-conn/20261029/{c}/round{r}/C2-fill.log"
            if not path.exists():
                continue
            pts = [(int(m.group(1)), int(m.group(2))) for m in re.finditer(r"t=(\d+) count=(\d+)", path.read_text())]
            ax.plot([t for t, _ in pts], [n / 1e6 for _, n in pts], color=color, marker="o", ms=3,
                    label=label if r == 1 else None, alpha=0.8)
    ax.axvspan(5, 45, color="#eeeeee", zorder=0, label="新しい流れを送った時間（20 万本/秒）")
    ax.set_xlabel("経過時間 [秒]")
    ax.set_ylabel("conntrack の件数 [百万]")
    ax.legend(fontsize=8, loc="upper left")
    ax.set_title("N2: conntrack を埋めたときの件数（ステートフル FW、各 3 回）")
    save(fig, "N-conntrack", "返事の無い UDP は 30 秒残る。並行して流した TCP 500 Mbps はどの回も 492〜500 Mbps のまま。2026-10-29")


def plot_e2_e5(base: Path) -> None:
    """E2 ロス率の曲線（決まった負荷ごと）と、E5 MAP-E の新規接続のレートごとのロス率。"""
    import json
    import re

    def fixed(path: Path) -> tuple[float, float] | None:
        try:
            r = json.loads(path.read_text())["runs"][0]
        except (OSError, ValueError, KeyError, IndexError):
            return None
        return r["sent_pps"], max(r["dut_loss_ratio"], 1e-7)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.4))
    for frame, color, pat in ((512, "#1f77b4", "512B-*pct"), (64, "#d62728", "64B-*")):
        pts = sorted(p for d in sorted(base.glob(f"E2/{pat}")) if d.is_dir() and (p := fixed(d / "result.json")))
        if pts:
            ax1.plot([s / 1e6 for s, _ in pts], [l for _, l in pts], color=color, marker="o", label=f"{frame}B")
    ax1.set_yscale("log")
    ax1.axhline(1e-3, color="#555555", ls="--", lw=1, label="判定の基準 0.1%")
    ax1.axvline(2 * wire_pps(512) / 1e6, color="#1f77b4", ls=":", lw=1, label="512B の理論値")
    ax1.set_xlabel("実際に送った量 [Mpps]（両向きの合計）")
    ax1.set_ylabel("PC02 のロス率")
    ax1.legend(fontsize=8, loc="upper left")
    ax1.set_title("E2: 負荷ごとのロス率（PC02 は ①、1 巡）")

    rates, loss, count = [], [], []
    for d in sorted((p for p in base.glob("E5/rate-*") if p.is_dir()), key=lambda p: int(p.name.split("-")[1])):
        f = fixed(d / "result.json")
        ct = (base / "E5" / f"{d.name}-ct.txt")
        if f and ct.exists():
            m = re.search(r"count=(\d+)", ct.read_text())
            rates.append(int(d.name.split("-")[1]))
            loss.append(f[1] * 100)
            count.append(int(m.group(1)) / 1e3 if m else 0)
    ax2.plot(rates, loss, color="#9467bd", marker="o", label="ロス率 [%]（左の目盛り）")
    ax2.axvline(61440 / 30, color="#555555", ls="--", lw=1, label="理論上の上限 61,440 / 30 秒 = 約 2,048 本/秒")
    ax2.set_xscale("log")
    ax2.set_xlabel("新しい流れ [本/秒]")
    ax2.set_ylabel("ロス率 [%]")
    ax2b = ax2.twinx()
    ax2b.plot(rates, count, color="#2ca02c", marker="s", ls=":", label="conntrack の件数 [千]（右の目盛り）")
    ax2b.axhline(61.44, color="#2ca02c", lw=0.8, alpha=0.6)
    ax2b.set_ylabel("conntrack の件数 [千]")
    ax2b.set_ylim(0, 70)
    h1, l1 = ax2.get_legend_handles_labels()
    h2, l2 = ax2b.get_legend_handles_labels()
    ax2.legend(h1 + h2, l1 + l2, fontsize=8, loc="upper left")
    ax2.set_title("E5: MAP-E の新規接続のレートごとのロス率")
    save(fig, "E2-E5", "E2 は決まった負荷を 30 秒、ロス 0 の点は 1e-7 に置いた。E5 は送信元・ポート・宛先 256 個を散らした新しい流れを 40 秒、NAT の組は 240 ポート × 256 宛先。2026-10-29")


def plot_bidir_configs(base: Path) -> None:
    import json

    configs = [("s1", "①（基準）"), ("s1perf", "＋④ 性能"), ("s1dfw", "＋set dscp（FW）"), ("s1fw", "＋ステートフル FW"),
               ("s1ft", "＋③ flowtable"), ("s1fwft", "＋FW＋flowtable"), ("s1rps", "＋② RPS"), ("s1qos", "＋QoS shaper")]
    fig, ax = plt.subplots(figsize=(12, 4.8))
    width = 0.38
    for k, (tol, label, color) in enumerate((("0", "NDR（ロス 0%）", "#1f77b4"), ("0.001", "PDR（ロス 0.1% 未満）", "#ff7f0e"))):
        for i, (c, _) in enumerate(configs):
            vals = [json.loads(p.read_text())["result_pps"] / 1e6 for p in sorted(base.glob(f"{c}/round*/64B-many-bidir-tol{tol}/result.json"))]
            m, lo, hi = median_range(vals)
            x = i + (k - 0.5) * width
            ax.bar(x, m, width, color=color, label=label if i == 0 else None, yerr=[[lo], [hi]], capsize=2)
            ax.scatter([x] * len(vals), vals, s=7, color="black", zorder=3)
            ax.text(x, m + hi + 0.05, f"{m:.2f}", ha="center", va="bottom", fontsize=7.5)
    ax.axhline(5.82, color=COLORS["PC01 送信"], ls=":", lw=1.5, label="送信側の上限（両向き）約 5.82")
    ax.set_xticks(range(len(configs)), [c[1] for c in configs], fontsize=9)
    ax.set_ylabel("64B のフレームレート [Mpps]（両向きの合計）")
    ax.set_ylim(0, 6.6)
    ax.legend(fontsize=8, loc="upper right")
    ax.set_title("構成ごとの 64B 多数フロー（双方向の送信、すべて ① の上に足した構成）")
    save(fig, "T3-bidir-configs-64B", "PC01 → PC03 と PC03 → 捨て先を同時に送った合計。各 n=3、棒は中央値、ひげは最小〜最大、点は各回。2026-10-28")


def plot_tcp_ft(base: Path) -> None:
    import json

    configs = [("s1", "①（FW なし）", "#2ca02c"), ("s1fw", "＋ステートフル FW", "#ff7f0e"),
               ("s1fwft", "＋FW＋flowtable", "#9467bd")]
    conds = [("default", "MSS 1448（通常）"), ("88", "MSS 88（小さいフレーム）")]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.5))
    width = 0.26
    for k, (c, label, color) in enumerate(configs):
        for i, (mss, _) in enumerate(conds):
            gbps, cpu = [], []
            for d in sorted(base.glob(f"{c}/round*")):
                gbps.append(json.loads((d / f"mss-{mss}.json").read_text())["end"]["sum_received"]["bits_per_second"] / 1e9)
                cpu.append(json.loads((d / f"mss-{mss}-cpu.json").read_text())["cpu_sum"])
            x = i + (k - 1) * width
            for ax, vals, fmt in ((ax1, gbps, "{:.2f}"), (ax2, cpu, "{:.2f}")):  # noqa: B007
                m, lo, hi = median_range(vals)
                ax.bar(x, m, width, color=color, label=label if i == 0 else None, yerr=[[lo], [hi]], capsize=2)
                ax.scatter([x] * len(vals), vals, s=7, color="black", zorder=3)
                ax.text(x, m + hi + (0.15 if ax is ax1 else 0.03), fmt.format(m), ha="center", va="bottom", fontsize=8)
    for ax in (ax1, ax2):
        ax.set_xticks(range(len(conds)), [c[1] for c in conds])
    ax1.set_ylabel("TCP の受信 [Gbps]")
    ax1.set_ylim(0, 11.5)
    ax1.legend(fontsize=8, loc="upper right")
    ax1.set_title("TCP の速さ（-P 16、30 秒）")
    ax2.set_ylabel("PC02 の CPU（全コアの合計）[コア]")
    ax2.set_ylim(0, 1.9)
    ax2.set_title("そのときの PC02 の CPU")
    save(fig, "T5-tcp-flowtable", "PC01 → PC03。CPU は PC03 の Prometheus から各回 23 秒の平均。各 n=3、棒は中央値、ひげは最小〜最大。2026-10-28")


if __name__ == "__main__":
    tcpft = ROOT / "results" / "T5-tcp" / "20261028"
    if tcpft.exists():
        plot_tcp_ft(tcpft)
    mape = ROOT / "results" / "T6-T7" / "20261027"
    if mape.exists():
        plot_mape(mape)
    bidir = ROOT / "results" / "T3-bidir" / "20261027"
    if bidir.exists():
        plot_bidir(bidir, ROOT / "results" / "T5-B1" / "20261027")
    t5 = ROOT / "results" / "T5-B1" / "20261027"
    if t5.exists():
        plot_t5b1(t5)
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
