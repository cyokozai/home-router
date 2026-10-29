"""E3（バースト）と E4（過負荷からの戻り）の準備の検査。実機に触らない。

- scripts/pktgen-run.sh: /proc/net/pktgen の代わりに偽のディレクトリ（PKTGEN_PROC）へ書かせ、
  bash -x の出力から pktgen への書き込みの並びを取り出して確かめる。modprobe と sleep は何もしない偽物に替える。
  - COUNT も RATE2 も無いときは、変更前の書き込みと 1 行も違わないこと（tests/fixtures/pktgen-run/ に、
    変更前のスクリプトで取った並びを置いてある）
  - COUNT: スレッドごとの count の合計が COUNT に一致し、時間ではなく数で止まること
  - RATE2: 送信を止めずに途中でレートを変えること
- scripts/t3_search.py: 両向きのときに COUNT・RATE2 を送信元 2 台へ半分ずつ渡すこと、引数の組み合わせの検査
- scripts/run-e3-e4.sh: bash -n と shellcheck が通ること
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pktgen-run.sh"
RUNNER = ROOT / "scripts" / "run-e3-e4.sh"
GOLDEN = ROOT / "tests" / "fixtures" / "pktgen-run"

spec = importlib.util.spec_from_file_location("t3_search", ROOT / "scripts" / "t3_search.py")
t3 = importlib.util.module_from_spec(spec)
sys.modules["t3_search"] = t3
spec.loader.exec_module(t3)

BASE = {"IFACE": "sfp0", "SIZE": "60", "DST": "192.168.20.2", "SRC": "192.168.10.10",
        "DMAC": "6c:fe:54:6e:27:a0", "THREADS": "4", "DURATION": "10"}

# 変更前と比べる 3 通り（既定の 1 フロー / レート指定・送信元を散らす・IMIX / IPv6）
GOLDEN_CASES = {
    "single": {},
    "many-rate-imix": {"RATE": "4000000", "FLOWS": "many", "BURST": "1", "IMIX": "60,7 566,4 1514,1",
                       "SPORT_MIN": "2000", "SPORT_MAX": "3000"},
    "ipv6-srcmax": {"DST": "2001:db8:20::2", "SRC": "2001:db8:10::10", "RATE": "1000000"},
    "dstmax-srcmax": {"DST_MAX": "198.51.100.255", "SRC_MAX": "192.168.10.250", "FLOWS": "many"},
}


def trace(tmp_path: Path, env: dict[str, str],
          stubs: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess, list[str]]:
    """pktgen-run.sh を偽の /proc で流し、pktgen への書き込みと待ち時間を、起きた順に返す。

    stubs: 偽物に替えるコマンドと、その中身（シェルの本文）。既定は modprobe と sleep を何もしない偽物にする。
    """
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    for name, body in ({"modprobe": "exit 0", "sleep": "exit 0"} | (stubs or {})).items():
        (stub / name).write_text(f"#!/bin/sh\n{body}\n")
        (stub / name).chmod(0o755)
    full = {"PATH": f"{stub}:{os.environ['PATH']}", "PS4": "+ ", "PKTGEN_PROC": str(proc)} | BASE | env
    r = subprocess.run(["bash", "-x", str(SCRIPT)], env=full, capture_output=True, text=True, timeout=30)
    keep = ("+ pg ", "+ sleep ", "+ timeout ")
    lines = [ln for ln in r.stderr.splitlines()
             if ln.startswith(keep) or ln in ("+ echo start", "+ echo stop")]
    return r, lines


def pg_lines(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.startswith("+ pg ")]


# ---- 既定（COUNT も RATE2 も無い）は変更前と同じ ----------------------------------


@pytest.mark.parametrize("case", sorted(GOLDEN_CASES))
def test_default_writes_are_unchanged(tmp_path, case):
    r, lines = trace(tmp_path, GOLDEN_CASES[case])
    assert r.returncode == 0, r.stderr
    golden = (GOLDEN / f"{case}.txt").read_text().splitlines()
    # 設定の書き込みは順番まで同じ。start は裏で走るので、start・sleep・stop は順を問わず数で比べる
    assert pg_lines(lines) == pg_lines(golden)
    assert sorted(x for x in lines if not x.startswith("+ pg ")) == sorted(x for x in golden if not x.startswith("+ pg "))


def test_default_still_stops_by_time(tmp_path):
    _, lines = trace(tmp_path, {})
    assert "+ sleep 10" in lines and "+ echo stop" in lines
    assert all("count 0" in ln for ln in pg_lines(lines) if " count " in ln)


# ---- COUNT: 数で止める（E3） --------------------------------------------------------


def thread_counts(lines: list[str]) -> list[int]:
    return [int(ln.rsplit(" ", 1)[1].rstrip("'")) for ln in pg_lines(lines) if "'count " in ln]


@pytest.mark.parametrize(("count", "threads", "expected"),
                         [(10, 4, [3, 3, 2, 2]), (8, 4, [2, 2, 2, 2]), (29000, 4, [7250] * 4), (7, 3, [3, 2, 2])])
def test_count_is_split_over_threads(tmp_path, count, threads, expected):
    r, lines = trace(tmp_path, {"COUNT": str(count), "THREADS": str(threads)})
    assert r.returncode == 0, r.stderr
    assert thread_counts(lines) == expected
    assert sum(thread_counts(lines)) == count


def test_count_stops_by_count_not_by_time(tmp_path):
    # start は全スレッドが count を送り終えるまで返らないので、sleep で待って stop を送る競り合いが無い
    r, lines = trace(tmp_path, {"COUNT": "1000"})
    assert r.returncode == 0, r.stderr
    assert not any(ln.startswith("+ sleep ") for ln in lines)
    assert any(ln.startswith("+ timeout ") for ln in lines)  # 止まらないときの安全弁
    assert lines[-1] == "+ echo stop"                       # 安全弁で切れたときも必ず止める


def test_count_timeout_can_be_changed(tmp_path):
    _, lines = trace(tmp_path, {"COUNT": "1000", "COUNT_TIMEOUT": "7"})
    assert any(ln.startswith("+ timeout 7 ") for ln in lines)


def test_count_not_finished_in_time_fails_the_trial(tmp_path):
    # 安全弁（timeout、終了コード 124）で切れたら、止めたうえで失敗を返す。COUNT に届かなかった試行を
    # 有効なバーストとして記録させない
    r, lines = trace(tmp_path, {"COUNT": "1000"}, stubs={"timeout": "exit 124"})
    assert r.returncode != 0
    assert "+ echo stop" in lines


def test_rate_switch_stops_pktgen_even_if_the_switch_fails(tmp_path):
    # 送信を始めたあとで切り替えの途中（ここでは sleep 0.1）が失敗しても、count 0（無制限）の pktgen を止めて抜ける
    r, lines = trace(tmp_path, {"RATE": "10000000", "RATE2": "1400000", "SWITCH_AFTER": "20", "DURATION": "40"},
                     stubs={"sleep": '[ "$1" = 0.1 ] && exit 1; exit 0'})
    assert r.returncode != 0
    assert lines.count("+ echo stop") == 1


@pytest.mark.parametrize("bad", ["3", "0", "abc"])
def test_count_smaller_than_threads_is_refused(tmp_path, bad):
    # count 0 は pktgen では「無制限」。スレッドに 0 が配られると止まらなくなるので、始める前に断る
    r, lines = trace(tmp_path, {"COUNT": bad})
    assert r.returncode != 0
    assert "+ echo start" not in lines


# ---- RATE2: 途中でレートを変える（E4） --------------------------------------------


def test_rate_switch_without_stopping(tmp_path):
    r, lines = trace(tmp_path, {"RATE": "10000000", "RATE2": "1400000", "SWITCH_AFTER": "20", "DURATION": "40"})
    assert r.returncode == 0, r.stderr
    # start は裏で走るので、出力の順は決まらない。並びからは外し、数だけ見る
    seq = [ln for ln in lines if ln != "+ echo start" and (not ln.startswith("+ pg ") or "'ratep " in ln or "'delay " in ln)]
    # 設定のときの ratep（1 スレッド 2,500,000）は start の前
    assert pg_lines(lines).count("+ pg sfp0@0 'ratep 2500000'") == 1
    # 切り替え: 20 秒 → 遅れの取り戻し（delay 1ms）→ 0.1 秒 → 新しいレート → 残りの 20 秒 → stop。stop は最後に 1 回だけ
    tail = seq[seq.index("+ sleep 20"):]
    assert tail == (["+ sleep 20"] + [f"+ pg sfp0@{i} 'delay 1000000'" for i in range(4)] + ["+ sleep 0.1"]
                    + [f"+ pg sfp0@{i} 'ratep 350000'" for i in range(4)] + ["+ sleep 20", "+ echo stop"])
    assert lines.count("+ echo stop") == 1 and lines.count("+ echo start") == 1


def test_rate_switch_from_unlimited(tmp_path):
    # 前半を制限なし（delay 0）にしても切り替えられる
    r, lines = trace(tmp_path, {"RATE": "0", "RATE2": "1400000", "SWITCH_AFTER": "5", "DURATION": "12"})
    assert r.returncode == 0, r.stderr
    assert "+ pg sfp0@3 'ratep 350000'" in lines and "+ sleep 7" in lines


@pytest.mark.parametrize("env", [
    {"RATE2": "1400000"},                                          # SWITCH_AFTER が無い
    {"RATE2": "1400000", "SWITCH_AFTER": "10", "DURATION": "10"},  # 後半が 0 秒
    {"RATE2": "1400000", "SWITCH_AFTER": "1.5", "DURATION": "10"},  # 秒は整数
    {"RATE2": "3", "SWITCH_AFTER": "5", "DURATION": "10"},          # ratep 0 は pktgen が黙って無視する
    {"RATE2": "1400000", "SWITCH_AFTER": "5", "DURATION": "10", "COUNT": "1000"},  # 数で止めるのとは一緒に使えない
])
def test_bad_schedule_is_refused(tmp_path, env):
    r, lines = trace(tmp_path, env)
    assert r.returncode != 0
    assert "+ echo start" not in lines


# ---- t3_search.py: 送信元 2 台への分け方と引数 ---------------------------------------


def test_schedule_env_one_direction():
    assert t3.schedule_env(count=29000, rate2=None, switch_after=None, bidir=False) == {"COUNT": "29000"}
    assert t3.schedule_env(count=None, rate2=2_800_000, switch_after=20, bidir=False) == \
        {"RATE2": "2800000", "SWITCH_AFTER": "20"}
    assert t3.schedule_env(count=None, rate2=None, switch_after=None, bidir=False) == {}


def test_schedule_env_bidir_halves_like_rate():
    # 要求レートと同じく、両向きの合計を PC01 と PC03 に半分ずつ渡す
    assert t3.schedule_env(count=58000, rate2=2_800_000, switch_after=20, bidir=True) == \
        {"COUNT": "29000", "RATE2": "1400000", "SWITCH_AFTER": "20"}


def test_schedule_env_bidir_odd_count_is_refused():
    # 2 台に同じ値を渡すので、両向きの合計は偶数にする（黙って 1 つ減らさない）
    with pytest.raises(ValueError):
        t3.schedule_env(count=7, rate2=None, switch_after=None, bidir=True)


@pytest.mark.parametrize("argv", [
    ["--count", "1000"],                                                   # --fixed-pps が要る
    ["--fixed-pps", "1e6", "--rate2", "5e5"],                              # --switch-after が要る
    ["--fixed-pps", "1e6", "--rate2", "5e5", "--switch-after", "20", "--final-seconds", "20"],  # 後半が無い
    ["--fixed-pps", "1e6", "--count", "1000", "--rate2", "5e5", "--switch-after", "5", "--final-seconds", "10"],
])
def test_bad_cli_is_refused_before_touching_hardware(argv, monkeypatch, tmp_path):
    monkeypatch.setattr(t3, "ssh", lambda *a, **k: pytest.fail("実機に触れた"))
    with pytest.raises(SystemExit):
        t3.main(argv + ["--out", str(tmp_path)])


# ---- run-e3-e4.sh ------------------------------------------------------------------


def test_runner_parses():
    r = subprocess.run(["bash", "-n", str(RUNNER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck が無い（CI と Docker では入っている）")
def test_shellcheck():
    r = subprocess.run(["shellcheck", str(RUNNER), str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
