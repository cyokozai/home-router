"""R1（PC02 の再起動にかかる時間、RFC 2544 26.6）の記録を集計する。

    python3 -I scripts/summarize_reboot.py results/R1-reboot/20261030

各巡のディレクトリ（round1, round2, ...）にある次の 2 つを読み、summary.csv を書く。
  pc03-rx.txt  PC03 で 1 秒ごとに取った「時刻（UNIX 秒） sfp0 の rx_packets」の行
  events.txt   実行器が書いた「名前=値」の行（traffic_start・reboot・ssh_back・traffic_end は UNIX 秒）

求める値（いずれも秒）:
  転送停止   平常の受信だった最後の秒の終わりから、平常の受信に戻った最初の秒の始まりまで
  受信の再開 同じ起点から、一部でも受信が戻った最初の秒の始まりまで
  完全復帰   同じ起点から、それ以降は送信の終わりまで平常のままになった秒の始まりまで
  SSH 復帰   再起動の指示から、管理系（VRF MGMT）の SSH が通るまで
平常の受信は、送信を始めて 3 秒後から再起動の指示までの 1 秒ごとの受信数の中央値。その 95% 以上を平常とみなす。
時刻は 2 か所の時計（PC03 と作業用 Mac）を使うので、SSH 復帰には NTP のずれ（数十 ms 程度）が入る。
"""

import csv
import statistics as st
import sys
from pathlib import Path

NORMAL_RATIO = 0.95   # 平常とみなす受信の割合
WARMUP_S = 3.0        # 送信の始めの不安定な秒を捨てる
END_MARGIN_S = 1.0    # 送信の終わり際の秒は使わない（止める時刻の誤差）
RX_FLOOR_RATIO = 0.01  # これを超えたら「受信が戻り始めた」とみなす（迷いパケットを数えない）


def parse_samples(text: str) -> list[tuple[float, int]]:
    """「時刻 カウンタ」の行を読む。壊れた行は飛ばす。"""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            out.append((float(parts[0]), int(parts[1])))
        except ValueError:
            continue
    return out


def parse_events(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        try:
            out[k.strip()] = float(v)
        except ValueError:
            out[k.strip()] = v.strip()
    return out


def intervals(samples: list[tuple[float, int]]) -> list[tuple[float, float, float]]:
    """隣り合う行の差から（始まり, 終わり, pps）を作る。行が欠けても時刻の差で割る。カウンタが戻った区間は捨てる。"""
    out = []
    for (t0, c0), (t1, c1) in zip(samples, samples[1:]):
        if t1 <= t0 or c1 < c0:
            continue
        out.append((t0, t1, (c1 - c0) / (t1 - t0)))
    return out


def _num(ev: dict, key: str) -> float | None:
    v = ev.get(key)
    return v if isinstance(v, float) else None


def analyze(samples: list[tuple[float, int]], ev: dict) -> dict:
    reboot = _num(ev, "reboot")
    start = _num(ev, "traffic_start")
    end = _num(ev, "traffic_end")
    ivs = [iv for iv in intervals(samples) if end is None or iv[1] <= end - END_MARGIN_S]
    before = [r for t0, t1, r in ivs if (start is None or t0 >= start + WARMUP_S) and (reboot is None or t1 <= reboot)]
    normal = st.median(before) if before else None
    ssh = _num(ev, "ssh_back")
    res = {"normal_pps": normal, "last_normal": None, "first_rx": None, "outage_s": None,
           "rx_resume_s": None, "full_recovery_s": None,
           "ssh_back_s": ssh - reboot if ssh is not None and reboot is not None else None}
    if not normal:
        return res
    ok = lambda r: r >= NORMAL_RATIO * normal  # noqa: E731
    after = [iv for iv in ivs if reboot is None or iv[1] > reboot]
    first_bad = next((i for i, iv in enumerate(after) if not ok(iv[2])), None)
    if first_bad is None:
        res |= {"outage_s": 0, "rx_resume_s": 0, "full_recovery_s": 0}
        return res
    last_normal = after[first_bad][0]
    res["last_normal"] = last_normal
    rest = after[first_bad:]
    rx = next((iv for iv in rest if iv[2] > RX_FLOOR_RATIO * normal), None)
    back = next((iv for iv in rest if ok(iv[2])), None)
    # 完全復帰: 最後の「平常でない秒」の次の秒。最後の秒まで平常でないなら、戻っていない
    last_bad = max(i for i, iv in enumerate(rest) if not ok(iv[2]))
    full = rest[last_bad + 1] if last_bad + 1 < len(rest) else None
    if rx:
        res["first_rx"] = rx[0]
        res["rx_resume_s"] = rx[0] - last_normal
    if back:
        res["outage_s"] = back[0] - last_normal
    if full:
        res["full_recovery_s"] = full[0] - last_normal
    return res


def _fmt(v) -> str:
    return "―" if v is None else f"{v:.1f}"


def _agg(vals: list) -> str:
    xs = [v for v in vals if v is not None]
    if not xs:
        return "―"
    return f"{st.median(xs):.1f} [{min(xs):.1f}-{max(xs):.1f}]"


COLS = [("outage_s", "転送停止 [s]"), ("rx_resume_s", "受信の再開 [s]"), ("full_recovery_s", "完全復帰 [s]"),
        ("ssh_back_s", "SSH 復帰 [s]"), ("normal_pps", "平常の受信 [pps]")]


def main(dirs: list[str]) -> None:
    for base in map(Path, dirs):
        rows = [["巡"] + [label for _, label in COLS] + ["再起動後の設定"]]
        results = []
        for d in sorted(p for p in base.glob("round*") if p.is_dir()):
            rx, evf = d / "pc03-rx.txt", d / "events.txt"
            if not rx.exists() or not evf.exists():
                continue
            ev = parse_events(evf.read_text(errors="replace"))
            r = analyze(parse_samples(rx.read_text(errors="replace")), ev)
            results.append(r)
            rows.append([d.name] + [_fmt(r[k]) for k, _ in COLS] + [str(ev.get("config_survived", "―"))])
        if results:
            rows.append(["中央値 [最小-最大]"] + [_agg([r[k] for r in results]) for k, _ in COLS] + [""])
        with (base / "summary.csv").open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        for row in rows:
            print(row)


if __name__ == "__main__":
    main(sys.argv[1:])
