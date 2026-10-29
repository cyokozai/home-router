import csv
import json
import re
import statistics as st
import sys
from pathlib import Path

base = Path(sys.argv[1])
rows = [["構成", "N1 新規接続の速度 中央値 [範囲] [Mpps]", "N2 最大の件数", "N2 上限", "N2 メモリ（slab）[MB]", "N2 並行 TCP 最小 [Mbps]", "n"]]
for c, label in (("s1", "①（FW なし）"), ("s1fw", "＋ステートフル FW（表は既定）"), ("s1fwbig", "＋ステートフル FW（表 4,194,304）")):
    cps = [json.loads(p.read_text())["result_pps"] / 1e6 for p in sorted(base.glob(f"{c}/round*/C1-new-flows/result.json"))]
    counts, maxes, slabs, tcpmin = [], [], [], []
    # C2 は送り終えた巡回（C2-fill/result.json がある）だけを数える。途中で止まった巡回の C2-fill.log は使わない
    for p in sorted(r.parent.parent / "C2-fill.log" for r in base.glob(f"{c}/round*/C2-fill/result.json")):
        text = p.read_text()
        counts.append(max(int(x) for x in re.findall(r"count=(\d+)", text)))
        maxes.append(int(re.findall(r"max=(\d+)", text)[-1]))
        slabs.append(max(int(x) for x in re.findall(r"slab_bytes=(\d+)", text)) / 2**20)
        try:
            j = json.loads((p.parent / "C2-tcp.json").read_text())
            tcpmin.append(min(i["sum"]["bits_per_second"] for i in j["intervals"]) / 1e6)
        except (OSError, ValueError, KeyError):
            pass
    cell = f"{st.median(cps):.2f} [{min(cps):.2f}-{max(cps):.2f}]" if cps else "―"
    rows.append([label, cell, f"{st.median(counts):,.0f}" if counts else "―", f"{maxes[0]:,}" if maxes else "―",
                 f"{st.median(slabs):.0f}" if slabs else "―", f"{min(tcpmin):.0f}" if tcpmin else "―", len(cps)])
rows.append(["", "N1 は試行ごとに conntrack を空にして 1 秒送り、ロス 0.1% 未満の最大。送信側の上限（clone_skb 0 で約 2 Mpps）に近い", "", "", "", "", ""])
with (base / "summary.csv").open("w", newline="", encoding="utf-8") as f:
    csv.writer(f).writerows(rows)
for r in rows:
    print(r)
