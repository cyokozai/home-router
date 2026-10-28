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


# IPv6（--family 6）の経路。IPv6＋UDP は 64B のフレームに収まらないので、RFC 5180 にならって 78B 以上で測る
SRC_IP6, DST_IP6 = "2001:db8:10::10", "2001:db8:20::2"
MIN_FRAME_IP6 = 78

# IMIX（7:4:1、線上 64 / 570 / 1518 バイト）。CLAUDE.md §8 の目標表と同じ
IMIX_FRAMES = ((64, 7), (570, 4), (1518, 1))


def wire_rate_pps(frame: int) -> float:
    return LINE_RATE_BPS / ((frame + GAP_BYTES) * 8)


def imix_wire_rate_pps(frames: tuple[tuple[int, int], ...] = IMIX_FRAMES) -> float:
    """重み付きの平均フレーム長での理論値。"""
    total = sum(w for _, w in frames)
    mean = sum(f * w for f, w in frames) / total
    return LINE_RATE_BPS / ((mean + GAP_BYTES) * 8)


def imix_weights(frames: tuple[tuple[int, int], ...] = IMIX_FRAMES) -> str:
    """pktgen の imix_weights の書式（pkt_size は FCS を含まない）。"""
    return " ".join(f"{pktgen_size(f)},{w}" for f, w in frames)


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


def ssh(host: str, command: str, timeout: int = 300, retries: int = 5, wait: float = 10.0) -> str:
    """終了コード 255（ssh そのものの接続の失敗）だけ、待って繰り返す。コマンドの失敗はそのまま上げる。

    2026-10-28 実害: 作業用 Mac から 3 台への ssh が一斉に切れ（255、Interrupted system call）、探索が途中で落ち、
    続く P2〜P4 も始まらなかった。
    """
    for attempt in range(retries + 1):
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 255 or attempt == retries:
            break
        print(f"  ssh {host} が接続に失敗（255）。{wait:.0f} 秒待って繰り返す（{attempt + 1}/{retries}）", flush=True)
        time.sleep(wait)
    if r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, r.args, output=r.stdout, stderr=r.stderr)
    return r.stdout


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
# IPv6 の捨て先。VyOS の静的 ARP（protocols static arp）は IPv4 しか受け付けないので、PC02 には何も足さず、
# PC01 にこのアドレスを付けて NDP に答えさせ、宛てた UDP は X710 の ntuple のフィルタで NIC が捨てる（PC01 の CPU を使わない）
SINK_IP6 = "2001:db8:10::99"


@dataclass
class Flow:
    """1 回の試行で pktgen に渡す、経路と中身の指定。"""
    src: str = SRC_IP
    dst: str = DST_IP
    sink: str = SINK_IP
    reply_src: str = DST_IP  # 双方向のとき、PC03 が送るパケットの送信元
    extra: dict[str, str] = field(default_factory=dict)  # DST_MAX・SPORT_MIN・SPORT_MAX・IMIX など

    @classmethod
    def ipv6(cls, extra: dict[str, str] | None = None) -> "Flow":
        return cls(src=SRC_IP6, dst=DST_IP6, sink=SINK_IP6, reply_src=DST_IP6, extra=extra or {})


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


def pc02_cpu_per_core(seconds: float) -> list[float] | None:
    """直近 seconds 秒の PC02 のコアごとの使用率（0〜1）。取れなければ None（試験は止めない）。"""
    w = max(2, int(seconds) - 2)
    q = f'1 - rate(node_cpu_seconds_total{{host="pc02",mode="idle"}}[{w}s])'
    try:
        out = ssh("pc03", f"curl -s --get localhost:9090/api/v1/query --data-urlencode {shlex.quote('query=' + q)}",
                  retries=1)
        res = json.loads(out)["data"]["result"]
        return [round(float(r["value"][1]), 3) for r in sorted(res, key=lambda r: int(r["metric"]["cpu"]))]
    except Exception:  # noqa: BLE001
        return None


def make_trial_bidir(frame: int, flows: str, threads: int, burst: int, logdir: Path,
                     flow: Flow | None = None) -> Trial:
    """PC01 → PC03 と PC03 → PC01 を同時に流す。要求レートは両向きの合計で、半分ずつに分ける。"""
    import concurrent.futures as cf

    flow = flow or Flow()
    counter = {"n": 0}

    def trial(rate_pps: float, seconds: int) -> Counters:
        counter["n"] += 1
        before = snapshot_bidir()
        common = {"SIZE": pktgen_size(frame), "THREADS": threads, "DURATION": seconds,
                  "RATE": int(rate_pps / 2), "BURST": burst, "FLOWS": flows} | flow.extra
        runs = {
            "pc01": common | {"IFACE": PC01_IF, "DST": flow.dst, "SRC": flow.src, "DMAC": PC02_ETH1_MAC},
            "pc03": common | {"IFACE": PC03_IF, "DST": flow.sink, "SRC": flow.reply_src, "DMAC": PC02_ETH2_MAC},
        }
        with cf.ThreadPoolExecutor(2) as ex:
            futs = {h: ex.submit(ssh, h, "sudo " + " ".join(f"{k}={shlex.quote(str(v))}" for k, v in a.items())
                                 + " bash ~/pktgen-run.sh", seconds + 120) for h, a in runs.items()}
            outs = {h: f.result() for h, f in futs.items()}
        time.sleep(1)
        after = snapshot_bidir()
        diff = {k: after[k] - before[k] for k in before}
        c = bidir_counters(diff)
        cpu = pc02_cpu_per_core(seconds) if seconds >= 30 else None  # コアごとの偏りを見るため（長い試行だけ）
        (logdir / f"trial{counter['n']:03d}.json").write_text(json.dumps(
            {"rate_pps": rate_pps, "seconds": seconds, "counters": asdict(c), "detail": diff, "pc02_cpu": cpu, "pktgen": outs},
            ensure_ascii=False, indent=1))
        print(f"  試行 {counter['n']:3d}: 要求 {rate_pps/1e6:7.3f} Mpps（両向きの合計）× {seconds:g}s → 送信 {c.pc01_tx/seconds/1e6:7.3f} Mpps,"
              f" PC02 のロス {c.dut_lost} ({c.dut_loss_ratio:.2e})", flush=True)
        return c

    return trial


def make_trial(frame: int, flows: str, threads: int, burst: int, logdir: Path, flow: Flow | None = None,
               pc02_before: str | None = None) -> Trial:
    """pc02_before: 試行ごとに、送る前に PC02 で流すコマンド（新規接続の試験で conntrack を空にする）。"""
    flow = flow or Flow()
    counter = {"n": 0}

    def trial(rate_pps: float, seconds: int) -> Counters:
        counter["n"] += 1
        if pc02_before:
            ssh("pc02", pc02_before)
        before = snapshot()
        args = {"IFACE": PC01_IF, "SIZE": pktgen_size(frame), "DST": flow.dst, "SRC": flow.src,
                "DMAC": PC02_ETH1_MAC, "THREADS": threads, "DURATION": seconds,
                "RATE": int(rate_pps), "BURST": burst, "FLOWS": flows} | flow.extra
        env = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in args.items())
        out = ssh("pc01", f"sudo {env} bash ~/pktgen-run.sh", timeout=seconds + 120)
        time.sleep(1)  # 送り終えたフレームが PC03 まで届くのを待つ
        after = snapshot()
        c = Counters(**{k: after[k] - before[k] for k in before})
        cpu = pc02_cpu_per_core(seconds) if seconds >= 30 else None
        (logdir / f"trial{counter['n']:03d}.json").write_text(json.dumps(
            {"rate_pps": rate_pps, "seconds": seconds, "counters": asdict(c), "pc02_cpu": cpu, "pktgen": out},
            ensure_ascii=False, indent=1))
        print(f"  試行 {counter['n']:3d}: 要求 {rate_pps/1e6:7.3f} Mpps × {seconds:g}s → 送信 {c.pc01_tx/seconds/1e6:7.3f} Mpps,"
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
    p.add_argument("--trial-seconds", type=float, default=10)
    p.add_argument("--final-seconds", type=float, default=60)
    p.add_argument("--sender-max", action="store_true", help="レートを制限せずに送り、送信能力の上限だけを測る")
    p.add_argument("--bidir", action="store_true",
                   help="PC01 → PC03 と PC03 → PC01 を同時に流す（レートは両向きの合計。上限は理論値の 2 倍）")
    p.add_argument("--family", type=int, choices=[4, 6], default=4, help="6 なら IPv6 で送る（--frame は 78 以上）")
    p.add_argument("--imix", action="store_true", help="IMIX（7:4:1）で送る。--frame は無視する")
    p.add_argument("--dst", help="宛先を変える（IPv4。MAP-E の試験では 198.51.100.1）。片方向のみ")
    p.add_argument("--dst-max", help="宛先を --dst〜この値の範囲で散らす（IPv4）")
    p.add_argument("--sport-min", type=int, help="many のときの送信元ポートの下限")
    p.add_argument("--sport-max", type=int, help="many のときの送信元ポートの上限")
    p.add_argument("--src-max", help="送信元を既定の送信元〜この値の範囲でランダムに散らす（IPv4、片方向）")
    p.add_argument("--pc02-before", help="試行ごとに送る前に PC02 で流すコマンド（片方向のみ。例: sudo conntrack -F）")
    p.add_argument("--fixed-pps", type=float, help="探索せず、このレートで --final-seconds 秒を --repeat 回流す（長時間の試験）")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--out", type=Path)
    a = p.parse_args(argv)
    if a.family == 6 and a.frame < MIN_FRAME_IP6:
        p.error(f"IPv6 は --frame {MIN_FRAME_IP6} 以上にする")
    if a.dst and a.bidir:
        p.error("--dst は片方向でだけ使える（双方向の返りの捨て先は IPv4 / IPv6 の既定のものに固定）")
    if (a.src_max or a.pc02_before) and a.bidir:
        p.error("--src-max と --pc02-before は片方向でだけ使える")

    extra = {}
    if a.dst_max:
        extra["DST_MAX"] = a.dst_max
    if a.src_max:
        extra["SRC_MAX"] = a.src_max
    if a.sport_min is not None:
        extra["SPORT_MIN"] = str(a.sport_min)
    if a.sport_max is not None:
        extra["SPORT_MAX"] = str(a.sport_max)
    if a.imix:
        extra["IMIX"] = imix_weights()
    flow = Flow.ipv6(extra) if a.family == 6 else Flow(extra=extra)
    if a.dst:
        flow.dst = a.dst

    size = "imix" if a.imix else f"{a.frame}B"
    name = f"{size}-{a.flows}-t{a.threads}" + ("-v6" if a.family == 6 else "") + ("-bidir" if a.bidir else "") \
        + ("-sendermax" if a.sender_max else f"-tol{a.tolerance:g}")
    out = a.out or Path("results/T3") / datetime.now().strftime("%Y%m%d") / name
    out.mkdir(parents=True, exist_ok=True)
    if a.bidir:
        trial = make_trial_bidir(a.frame, a.flows, a.threads, a.burst, out, flow)
    else:
        trial = make_trial(a.frame, a.flows, a.threads, a.burst, out, flow, pc02_before=a.pc02_before)
    upper = (imix_wire_rate_pps() if a.imix else wire_rate_pps(a.frame)) * (2 if a.bidir else 1)
    meta = {"test": "T3", "started": datetime.now().astimezone().isoformat(), "frame": "imix" if a.imix else a.frame,
            "flows": a.flows, "threads": a.threads, "burst": a.burst, "bidir": a.bidir, "family": a.family,
            "flow": asdict(flow), "pc02_before": a.pc02_before, "wire_rate_pps": upper}
    print(f"T3 {name}: 理論値 {upper/1e6:.3f} Mpps", flush=True)

    if a.sender_max:
        c = trial(0, a.final_seconds)  # RATE=0 はレート制限なし
        meta |= {"sender_max_pps": c.pc01_tx / a.final_seconds, "counters": asdict(c)}
    elif a.fixed_pps:
        runs = []
        for _ in range(a.repeat):
            c = trial(a.fixed_pps, a.final_seconds)
            runs.append({"at": datetime.now().astimezone().isoformat(), "counters": asdict(c),
                         "sent_pps": c.pc01_tx / a.final_seconds, "dut_loss_ratio": c.dut_loss_ratio})
            # 途中で止まっても、それまでの分が残るように毎回書く
            (out / "result.json").write_text(json.dumps(meta | {"fixed_pps": a.fixed_pps, "runs": runs},
                                                         ensure_ascii=False, indent=1))
        lost = sum(max(0, r["counters"]["pc01_tx"] - r["counters"]["pc02_tx"]) for r in runs)
        sent = sum(r["counters"]["pc01_tx"] for r in runs)
        meta |= {"fixed_pps": a.fixed_pps, "runs": runs, "total_loss_ratio": lost / sent if sent else None}
        print(f"結果: {a.fixed_pps/1e6:.3f} Mpps × {a.final_seconds}s × {a.repeat} 回、ロス率の合計 {lost / sent:.2e}", flush=True)
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
