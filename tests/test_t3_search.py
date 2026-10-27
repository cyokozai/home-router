"""scripts/t3_search.py の計算と探索の検査（RFC 2544 の Throughput を二分探索で近似する）。

実機に触らない部分（ワイヤーレート、カウンタからのロス、探索の手順）だけを、
偽の「装置」（容量 C を超えた分を落とす）を相手に確かめる。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("t3_search", ROOT / "scripts" / "t3_search.py")
t3 = importlib.util.module_from_spec(spec)
sys.modules["t3_search"] = t3  # dataclass が型の解決にモジュールを引くため、実行前に登録する
spec.loader.exec_module(t3)


# ---- ワイヤーレートと pktgen の pkt_size --------------------------------------


@pytest.mark.parametrize(("frame", "pps"), [(64, 14_880_952), (512, 2_349_624), (1518, 812_744)])
def test_wire_rate_pps(frame, pps):
    assert round(t3.wire_rate_pps(frame)) == pps


def test_pktgen_size_excludes_fcs():
    # pktgen の pkt_size は FCS（4 バイト）を含まない。線上で 64B にするには 60
    assert t3.pktgen_size(64) == 60
    assert t3.pktgen_size(1518) == 1514


# ---- カウンタからのロス ------------------------------------------------------


def test_dut_loss_counts_only_what_pc02_failed_to_forward():
    c = t3.Counters(pc01_tx=1_000_000, pc02_rx=999_990, pc02_tx=999_900, pc03_rx=990_000)
    assert c.dut_lost == 100                      # PC01 が送ったのに PC02 が送り出さなかった数
    assert c.dut_loss_ratio == pytest.approx(1e-4)
    assert c.receiver_lost == 9_900               # PC03 側の取りこぼしは別に数える


def test_dut_loss_is_never_negative():
    # 管理系やLLDPのフレームで PC02 の送信が少し多く数えられても、ロスは 0 とする
    c = t3.Counters(pc01_tx=1000, pc02_rx=1000, pc02_tx=1003, pc03_rx=1003)
    assert c.dut_lost == 0


def test_zero_sent_is_an_error():
    with pytest.raises(ValueError):
        t3.Counters(pc01_tx=0, pc02_rx=0, pc02_tx=0, pc03_rx=0).dut_loss_ratio


# ---- 二分探索 ----------------------------------------------------------------


def fake_device(capacity_pps, sender_max_pps=None):
    """capacity を超えた分を PC02 が落とす装置。送信側は sender_max を超えて送れない。"""
    calls = []

    def trial(rate_pps, seconds):
        sent_rate = min(rate_pps, sender_max_pps) if sender_max_pps else rate_pps
        sent = int(sent_rate * seconds)
        forwarded = int(min(sent_rate, capacity_pps) * seconds)
        calls.append((rate_pps, seconds))
        return t3.Counters(pc01_tx=sent, pc02_rx=sent, pc02_tx=forwarded, pc03_rx=forwarded)

    return trial, calls


def test_search_finds_capacity_within_precision():
    trial, _ = fake_device(capacity_pps=3_000_000)
    result = t3.search(trial, upper_pps=14_880_952, loss_tolerance=0.0, precision=0.01, trial_seconds=10, final_seconds=60)
    assert 3_000_000 * 0.99 <= result.rate_pps <= 3_000_000
    assert result.final.dut_lost == 0


def test_search_returns_upper_when_no_loss_at_upper():
    trial, calls = fake_device(capacity_pps=20_000_000)
    result = t3.search(trial, upper_pps=812_744, loss_tolerance=0.0, precision=0.01, trial_seconds=10, final_seconds=60)
    assert result.rate_pps == 812_744
    assert calls[0] == (812_744, 10)  # まず上限で試す（RFC 2544: 最大レートから始める）


def test_final_trial_uses_full_length():
    # RFC 2544: 探索の途中は短くてよいが、最終の値は 60 秒の試験で決める
    trial, calls = fake_device(capacity_pps=3_000_000)
    t3.search(trial, upper_pps=14_880_952, loss_tolerance=0.0, precision=0.01, trial_seconds=10, final_seconds=60)
    assert calls[-1][1] == 60
    assert all(s == 10 for _, s in calls[:-1])


def test_tolerance_allows_small_loss():
    # PDR（ロス 0.1% 未満を許す）は NDR（0%）以上になる
    trial, _ = fake_device(capacity_pps=3_000_000)
    ndr = t3.search(trial, upper_pps=14_880_952, loss_tolerance=0.0, precision=0.005, trial_seconds=10, final_seconds=60)
    trial, _ = fake_device(capacity_pps=3_000_000)
    pdr = t3.search(trial, upper_pps=14_880_952, loss_tolerance=0.001, precision=0.005, trial_seconds=10, final_seconds=60)
    assert pdr.rate_pps >= ndr.rate_pps
    assert pdr.rate_pps <= 3_000_000 / (1 - 0.001)


def test_final_failure_steps_down_until_pass():
    # 短い試験では通ったのに 60 秒で落ちる装置（長く流すと容量が下がる）
    calls = []

    def trial(rate_pps, seconds):
        capacity = 3_000_000 if seconds <= 10 else 2_900_000
        sent = int(rate_pps * seconds)
        forwarded = int(min(rate_pps, capacity) * seconds)
        calls.append((rate_pps, seconds))
        return t3.Counters(pc01_tx=sent, pc02_rx=sent, pc02_tx=forwarded, pc03_rx=forwarded)

    result = t3.search(trial, upper_pps=14_880_952, loss_tolerance=0.0, precision=0.01, trial_seconds=10, final_seconds=60)
    assert result.rate_pps <= 2_900_000
    assert result.final.dut_lost == 0
    assert [s for _, s in calls].count(60) >= 2


def test_search_reports_sender_limit():
    # 送信側が要求したレートを出せなかった試行は、送信側の頭打ちとして記録する
    trial, _ = fake_device(capacity_pps=20_000_000, sender_max_pps=4_000_000)
    result = t3.search(trial, upper_pps=14_880_952, loss_tolerance=0.0, precision=0.01, trial_seconds=10, final_seconds=60)
    assert result.sender_limited
    assert result.final.pc01_tx / 60 == pytest.approx(4_000_000, rel=0.01)


def test_upper_is_lowered_to_what_the_sender_can_send():
    # 上限で送信側が頭打ちなら、実際に送れたレートを上限として探す（届かないレートを空振りしない）
    trial, calls = fake_device(capacity_pps=1_000_000, sender_max_pps=3_000_000)
    result = t3.search(trial, upper_pps=14_880_952, loss_tolerance=0.0, precision=0.01, trial_seconds=10, final_seconds=60)
    assert calls[1][0] <= 3_000_000
    assert 1_000_000 * 0.99 <= result.rate_pps <= 1_000_000
    assert result.upper_pps == pytest.approx(3_000_000, rel=0.01)


def test_every_trial_is_logged():
    trial, calls = fake_device(capacity_pps=3_000_000)
    result = t3.search(trial, upper_pps=14_880_952, loss_tolerance=0.0, precision=0.01, trial_seconds=10, final_seconds=60)
    assert len(result.trials) == len(calls)
    assert all({"rate_pps", "seconds", "dut_loss_ratio", "passed"} <= set(t) for t in result.trials)
