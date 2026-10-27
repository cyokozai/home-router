"""試験 T3: pktgen で送り、NIC のカウンタで数え、二分探索で「落とさない最大レート」を求める。

RFC 2544 の Throughput を近似する（26.1 節）。
  - 最大レートから始め、ロスがあれば下げ、無ければ上げる二分探索
  - 探索の途中は短い試験（既定 10 秒）、最終の値は 60 秒の試験で決める（24 節）
  - 60 秒で落ちたら 1% ずつ下げて、通るまで確かめ直す
ロスは「PC01 が送ったのに PC02 が送り出さなかった数」（PC01 sfp0 の tx_packets − PC02 eth2 の tx_packets）。
PC03 の取りこぼしは装置のロスに数えず、別に記録する。

作業用 Mac から実行する（ssh の pc01 / pc02 / pc03 を使い、管理系だけで操作する。§1 ルール 6）。

    python3 scripts/t3_search.py --frame 64 --flows single [--tolerance 0] [--out results/T3/...]
    python3 scripts/t3_search.py --frame 64 --sender-max      # 送信能力の上限だけを測る

標準ライブラリだけで動く（Mac に何も入れない）。
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

LINE_RATE_BPS = 10_000_000_000
GAP_BYTES = 20  # プリアンブル 7 + SFD 1 + IFG 12

# 経路（CLAUDE.md §3）。PC01 は PC02 の eth1 宛てに送り、PC02 が PC03 へ転送する
PC01_IF = "sfp0"
PC02_IN, PC02_OUT = "eth1", "eth2"
PC03_IF = "sfp0"
SRC_IP, DST_IP = "192.168.10.10", "192.168.20.2"
PC02_ETH1_MAC = "6c:fe:54:6e:27:a0"


def wire_rate_pps(frame: int) -> float:
    return LINE_RATE_BPS / ((frame + GAP_BYTES) * 8)


def pktgen_size(frame: int) -> int:
    """pktgen の pkt_size は FCS（4 バイト）を含まない。"""
    return frame - 4


@dataclass
class Counters:
    pc01_tx: int
    pc02_rx: int
    pc02_tx: int
    pc03_rx: int

    @property
    def dut_lost(self) -> int:
        return max(0, self.pc01_tx - self.pc02_tx)

    @property
    def receiver_lost(self) -> int:
        return max(0, self.pc02_tx - self.pc03_rx)

    @property
    def dut_loss_ratio(self) -> float:
        if self.pc01_tx <= 0:
            raise ValueError("PC01 が 1 個も送っていない")
        return self.dut_lost / self.pc01_tx


def bidir_counters(d: dict[str, int]) -> Counters:
    """双方向の差分（snapshot_bidir の後 − 前）を、1 つの装置としての Counters にまとめる。

    PC01 → PC03 は PC02 の eth1 で受けて eth2 から、PC03 → PC01 は eth2 で受けて eth1 から出る。
    """
    return Counters(
        pc01_tx=d["pc01_tx"] + d["pc03_tx"],
        pc02_rx=d["pc02_eth1_rx"] + d["pc02_eth2_rx"],
        pc02_tx=d["pc02_eth1_tx"] + d["pc02_eth2_tx"],
        pc03_rx=d["pc01_rx"] + d["pc03_rx"],
    )


@dataclass
class Result:
    rate_pps: float
    final: Counters
    sender_limited: bool
    upper_pps: float = 0.0  # 実際に探した範囲の上端（送信側の頭打ちで下げたあとの値）
    trials: list[dict] = field(default_factory=list)


Trial = Callable[[float, int], Counters]


def search(trial: Trial, upper_pps: float, loss_tolerance: float, precision: float,
           trial_seconds: int, final_seconds: int, sender_shortfall: float = 0.02) -> Result:
    """落とさない（ロス率 <= loss_tolerance）最大レートを二分探索で求める。

    sender_shortfall: 要求したレートより実際の送信が この割合以上少なければ、送信側の頭打ちとみなす。
    """
    log: list[dict] = []
    sender_limited = False

    def run(rate: float, seconds: int) -> tuple[bool, Counters]:
        nonlocal sender_limited
        c = trial(rate, seconds)
        sent_pps = c.pc01_tx / seconds
        limited = sent_pps < rate * (1 - sender_shortfall)
        sender_limited = sender_limited or limited
        ok = c.dut_loss_ratio <= loss_tolerance
        log.append({"rate_pps": rate, "seconds": seconds, "sent_pps": sent_pps, "dut_lost": c.dut_lost,
                    "dut_loss_ratio": c.dut_loss_ratio, "receiver_lost": c.receiver_lost,
                    "sender_limited": limited, "passed": ok})
        return ok, c

    # 1. 上限で試す。送信側が頭打ちなら、実際に送れたレートを上限にして試し直す
    ok, first = run(upper_pps, trial_seconds)
    if log[-1]["sender_limited"]:
        upper_pps = first.pc01_tx / trial_seconds
        ok, _ = run(upper_pps, trial_seconds)
    if ok:
        best = upper_pps
    else:
        lo, hi = 0.0, upper_pps
        # 打ち切りの幅は、上限ではなく探索中の範囲の上端に対する割合で測る（答えが小さくても 1% の精度を保つ）
        while (hi - lo) / hi > precision:
            mid = (lo + hi) / 2
            ok, _ = run(mid, trial_seconds)
            if ok:
                lo = mid
            else:
                hi = mid
        best = lo

    # 2. 最終の値は長い試験で確かめる。落ちたら 1% ずつ下げる
    rate = best
    while True:
        ok, final = run(rate, final_seconds)
        if ok or rate <= 0:
            return Result(rate_pps=rate, final=final, sender_limited=sender_limited, upper_pps=upper_pps, trials=log)
        rate = rate * 0.99


# ---- 実機での 1 回の試行 --------------------------------------------------------


def ssh(host: str, command: str, timeout: int = 300) -> str:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", host, command], check=True, capture_output=True,
                          text=True, timeout=timeout).stdout


def ethtool_counter(host: str, iface: str, key: str) -> int:
    sudo = "sudo " if host == "pc02" else ""
    out = ssh(host, f"{sudo}ethtool -S {iface}")
    m = re.search(rf"^\s*{re.escape(key)}:\s*(\d+)\s*$", out, flags=re.M)
    if not m:
        raise KeyError(f"{host} {iface} に {key} が無い")
    return int(m.group(1))


def snapshot() -> dict[str, int]:
    return {
        "pc01_tx": ethtool_counter("pc01", PC01_IF, "tx_packets"),
        "pc02_rx": ethtool_counter("pc02", PC02_IN, "rx_packets"),
        "pc02_tx": ethtool_counter("pc02", PC02_OUT, "tx_packets"),
        "pc03_rx": ethtool_counter("pc03", PC03_IF, "rx_ucast_packets"),  # bnx2x は rx_packets が無い
    }


PC02_ETH2_MAC = "6c:fe:54:6e:27:a1"
# PC03 → PC01 向けの流れの宛先。PC02 が静的 ARP（vyos/test-sink.cmds）で存在しない MAC に送り出すので、
# PC02 の負荷は本物と同じまま、PC01 の CPU を受信に使わせない（双方向にしても PC01 が送り続けられるように）
SINK_IP = "192.168.10.99"


def snapshot_bidir() -> dict[str, int]:
    return {
        "pc01_tx": ethtool_counter("pc01", PC01_IF, "tx_packets"),
        "pc01_rx": ethtool_counter("pc01", PC01_IF, "rx_packets"),
        "pc02_eth1_rx": ethtool_counter("pc02", PC02_IN, "rx_packets"),
        "pc02_eth1_tx": ethtool_counter("pc02", PC02_IN, "tx_packets"),
        "pc02_eth2_rx": ethtool_counter("pc02", PC02_OUT, "rx_packets"),
        "pc02_eth2_tx": ethtool_counter("pc02", PC02_OUT, "tx_packets"),
        "pc03_tx": ethtool_counter("pc03", PC03_IF, "tx_ucast_packets"),
        "pc03_rx": ethtool_counter("pc03", PC03_IF, "rx_ucast_packets"),
    }


def make_trial_bidir(frame: int, flows: str, threads: int, burst: int, logdir: Path) -> Trial:
    """PC01 → PC03 と PC03 → PC01 を同時に流す。要求レートは両向きの合計で、半分ずつに分ける。"""
    import concurrent.futures as cf

    counter = {"n": 0}

    def trial(rate_pps: float, seconds: int) -> Counters:
        counter["n"] += 1
        before = snapshot_bidir()
        common = {"SIZE": pktgen_size(frame), "THREADS": threads, "DURATION": seconds,
                  "RATE": int(rate_pps / 2), "BURST": burst, "FLOWS": flows}
        runs = {
            "pc01": common | {"IFACE": PC01_IF, "DST": DST_IP, "SRC": SRC_IP, "DMAC": PC02_ETH1_MAC},
            "pc03": common | {"IFACE": PC03_IF, "DST": SINK_IP, "SRC": DST_IP, "DMAC": PC02_ETH2_MAC},
        }
        with cf.ThreadPoolExecutor(2) as ex:
            futs = {h: ex.submit(ssh, h, "sudo " + " ".join(f"{k}={shlex.quote(str(v))}" for k, v in a.items())
                                 + " bash ~/pktgen-run.sh", seconds + 120) for h, a in runs.items()}
            outs = {h: f.result() for h, f in futs.items()}
        time.sleep(1)
        after = snapshot_bidir()
        diff = {k: after[k] - before[k] for k in before}
        c = bidir_counters(diff)
        (logdir / f"trial{counter['n']:03d}.json").write_text(json.dumps(
            {"rate_pps": rate_pps, "seconds": seconds, "counters": asdict(c), "detail": diff, "pktgen": outs},
            ensure_ascii=False, indent=1))
        print(f"  試行 {counter['n']:3d}: 要求 {rate_pps/1e6:7.3f} Mpps（両向きの合計）× {seconds:2d}s → 送信 {c.pc01_tx/seconds/1e6:7.3f} Mpps,"
              f" PC02 のロス {c.dut_lost} ({c.dut_loss_ratio:.2e})", flush=True)
        return c

    return trial


def make_trial(frame: int, flows: str, threads: int, burst: int, logdir: Path) -> Trial:
    counter = {"n": 0}

    def trial(rate_pps: float, seconds: int) -> Counters:
        counter["n"] += 1
        before = snapshot()
        args = {"IFACE": PC01_IF, "SIZE": pktgen_size(frame), "DST": DST_IP, "SRC": SRC_IP,
                "DMAC": PC02_ETH1_MAC, "THREADS": threads, "DURATION": seconds,
                "RATE": int(rate_pps), "BURST": burst, "FLOWS": flows}
        env = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in args.items())
        out = ssh("pc01", f"sudo {env} bash ~/pktgen-run.sh", timeout=seconds + 120)
        time.sleep(1)  # 送り終えたフレームが PC03 まで届くのを待つ
        after = snapshot()
        c = Counters(**{k: after[k] - before[k] for k in before})
        (logdir / f"trial{counter['n']:03d}.json").write_text(json.dumps(
            {"rate_pps": rate_pps, "seconds": seconds, "counters": asdict(c), "pktgen": out}, ensure_ascii=False, indent=1))
        print(f"  試行 {counter['n']:3d}: 要求 {rate_pps/1e6:7.3f} Mpps × {seconds:2d}s → 送信 {c.pc01_tx/seconds/1e6:7.3f} Mpps,"
              f" PC02 のロス {c.dut_lost} ({c.dut_loss_ratio:.2e}), PC03 の取りこぼし {c.receiver_lost}", flush=True)
        return c

    return trial


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--frame", type=int, default=64, help="線上のフレーム長（FCS 込み）")
    p.add_argument("--flows", choices=["single", "many"], default="single",
                   help="single: 1 フロー（RSS が効かない） / many: 送信元ポートを散らす")
    p.add_argument("--threads", type=int, default=4, help="PC01 の pktgen のスレッド数")
    p.add_argument("--burst", type=int, default=1, help="pktgen の burst（レート指定時は 1 を推奨）")
    p.add_argument("--tolerance", type=float, default=0.0, help="許すロス率（NDR は 0、PDR は 0.001）")
    p.add_argument("--precision", type=float, default=0.01, help="探索を打ち切る幅（探索範囲の上端に対する割合）")
    p.add_argument("--trial-seconds", type=int, default=10)
    p.add_argument("--final-seconds", type=int, default=60)
    p.add_argument("--sender-max", action="store_true", help="レートを制限せずに送り、送信能力の上限だけを測る")
    p.add_argument("--bidir", action="store_true",
                   help="PC01 → PC03 と PC03 → PC01 を同時に流す（レートは両向きの合計。上限は理論値の 2 倍）")
    p.add_argument("--out", type=Path)
    a = p.parse_args(argv)

    name = f"{a.frame}B-{a.flows}-t{a.threads}" + ("-bidir" if a.bidir else "") \
        + ("-sendermax" if a.sender_max else f"-tol{a.tolerance:g}")
    out = a.out or Path("results/T3") / datetime.now().strftime("%Y%m%d") / name
    out.mkdir(parents=True, exist_ok=True)
    trial = (make_trial_bidir if a.bidir else make_trial)(a.frame, a.flows, a.threads, a.burst, out)
    upper = wire_rate_pps(a.frame) * (2 if a.bidir else 1)
    meta = {"test": "T3", "started": datetime.now().astimezone().isoformat(), "frame": a.frame, "flows": a.flows,
            "threads": a.threads, "burst": a.burst, "bidir": a.bidir, "wire_rate_pps": upper}
    print(f"T3 {name}: 理論値 {upper/1e6:.3f} Mpps", flush=True)

    if a.sender_max:
        c = trial(0, a.final_seconds)  # RATE=0 はレート制限なし
        meta |= {"sender_max_pps": c.pc01_tx / a.final_seconds, "counters": asdict(c)}
    else:
        r = search(trial, upper, a.tolerance, a.precision, a.trial_seconds, a.final_seconds)
        meta |= {"tolerance": a.tolerance, "precision": a.precision, "trial_seconds": a.trial_seconds,
                 "final_seconds": a.final_seconds, "result_pps": r.rate_pps, "searched_upper_pps": r.upper_pps,
                 "result_ratio_of_wire": r.rate_pps / upper, "sender_limited": r.sender_limited,
                 "final_counters": asdict(r.final), "trials": r.trials}
        print(f"結果: {r.rate_pps/1e6:.3f} Mpps（理論値の {r.rate_pps/upper:.1%}）"
              + ("。送信側の頭打ちあり" if r.sender_limited else ""), flush=True)
    (out / "result.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    print(f"保存先: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
