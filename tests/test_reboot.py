"""R1（PC02 の再起動にかかる時間、RFC 2544 26.6）の検査。

- scripts/summarize_reboot.py: PC03 の 1 秒ごとの受信数から、転送が止まっていた時間を求める部分を、
  作った記録（平常 → 0 → 一部 → 平常、行の欠け）で確かめる。
- scripts/run-reboot.sh: CONFIRM_REBOOT=yes が無いと動かないこと、再起動の前の安全確認で止まること。
  ssh・scp は偽物に置き換え、ネットワークには出ない。
"""

import csv
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "run-reboot.sh"
spec = importlib.util.spec_from_file_location("summarize_reboot", ROOT / "scripts" / "summarize_reboot.py")
sr = importlib.util.module_from_spec(spec)
sys.modules["summarize_reboot"] = sr
spec.loader.exec_module(sr)

RATE = 100_000


def samples(rates: list[int], t0: float = 1000.0, skip: set[int] = frozenset()) -> str:
    """1 秒ごとの受信数の記録（「時刻 カウンタ」の行）を作る。rates[i] は i 秒目から i+1 秒目までの受信数。
    skip に入れた番号の行は書かない（記録の欠け）。"""
    lines, c = [], 5_000_000
    for i in range(len(rates) + 1):
        if i not in skip:
            lines.append(f"{t0 + i:.3f} {c}")
        if i < len(rates):
            c += rates[i]
    return "\n".join(lines) + "\n"


# 平常 30 秒 → 0 が 60 秒 → 一部（半分）が 3 秒 → 平常 40 秒。再起動の指示は 25 秒目
BASIC = [RATE] * 30 + [0] * 60 + [RATE // 2] * 3 + [RATE] * 40
EVENTS = {"traffic_start": 1000.0, "reboot": 1025.0, "ssh_back": 1080.0, "traffic_end": 1000.0 + len(BASIC)}


# ---- 記録の読み取り ----------------------------------------------------------


def test_parse_skips_broken_lines():
    text = "1000.0 10\nこわれた行\n1001.0\n1002.0 abc\n1003.0 40\n"
    assert sr.parse_samples(text) == [(1000.0, 10), (1003.0, 40)]


def test_parse_events():
    assert sr.parse_events("reboot=1025.5\nssh_back=1080\n# メモ\nconfig_survived=yes\n") == {
        "reboot": 1025.5, "ssh_back": 1080.0, "config_survived": "yes"}


# ---- 停止の検出 ----------------------------------------------------------------


def test_outage_normal_zero_partial_normal():
    r = sr.analyze(sr.parse_samples(samples(BASIC)), EVENTS)
    assert r["normal_pps"] == pytest.approx(RATE)
    assert r["last_normal"] == pytest.approx(1030.0)       # 平常だった最後の秒の終わり
    assert r["first_rx"] == pytest.approx(1090.0)          # 一部でも受信が戻り始めた秒の始まり
    assert r["outage_s"] == pytest.approx(63.0)            # 平常に戻った秒（1093）まで
    assert r["full_recovery_s"] == pytest.approx(63.0)     # その後に落ち込みが無いので同じ
    assert r["ssh_back_s"] == pytest.approx(55.0)          # 再起動の指示から


def test_second_dip_moves_full_recovery():
    # 平常に戻った後、設定の読み込みで IF が上げ直されると、もう一度落ちる。完全復帰はその後
    rates = [RATE] * 30 + [0] * 60 + [RATE] * 5 + [0] * 2 + [RATE] * 30
    ev = EVENTS | {"traffic_end": 1000.0 + len(rates)}
    r = sr.analyze(sr.parse_samples(samples(rates)), ev)
    assert r["outage_s"] == pytest.approx(60.0)
    assert r["full_recovery_s"] == pytest.approx(67.0)


def test_missing_lines_are_bridged_by_timestamps():
    # 停止中と、平常の途中の行が欠けても、時刻の差でレートを出す
    text = samples(BASIC, skip={10, 40, 41, 42, 100})
    r = sr.analyze(sr.parse_samples(text), EVENTS)
    assert r["normal_pps"] == pytest.approx(RATE)
    assert r["outage_s"] == pytest.approx(63.0)


def test_traffic_end_is_not_an_outage():
    # 送信が終わった後の 0 は停止に数えない
    rates = BASIC + [0] * 10
    r = sr.analyze(sr.parse_samples(samples(rates)), EVENTS)
    assert r["full_recovery_s"] == pytest.approx(63.0)


def test_never_recovered():
    rates = [RATE] * 30 + [0] * 100
    r = sr.analyze(sr.parse_samples(samples(rates)), EVENTS | {"traffic_end": 1130.0, "ssh_back": None})
    assert r["outage_s"] is None and r["full_recovery_s"] is None and r["first_rx"] is None
    assert r["ssh_back_s"] is None


def test_no_outage():
    r = sr.analyze(sr.parse_samples(samples([RATE] * 100)), EVENTS | {"traffic_end": 1100.0})
    assert r["outage_s"] == 0 and r["full_recovery_s"] == 0


def test_summary_csv(tmp_path):
    for i, extra in enumerate([0, 2, 5], start=1):
        d = tmp_path / f"round{i}"
        d.mkdir()
        rates = [RATE] * 30 + [0] * (60 + extra) + [RATE] * 40
        (d / "pc03-rx.txt").write_text(samples(rates))
        ev = EVENTS | {"ssh_back": 1080.0 + extra, "traffic_end": 1000.0 + len(rates)}
        (d / "events.txt").write_text("".join(f"{k}={v}\n" for k, v in ev.items()))
    sr.main([str(tmp_path)])
    rows = list(csv.reader((tmp_path / "summary.csv").open(encoding="utf-8")))
    assert rows[0][0] == "巡"
    assert [r[0] for r in rows[1:]] == ["round1", "round2", "round3", "中央値 [最小-最大]"]
    assert rows[-1][1] == "62.0 [60.0-65.0]"   # 転送が止まっていた時間


# ---- 実行器 --------------------------------------------------------------------


def fake_bin(tmp_path: Path, ssh_script: str) -> tuple[Path, Path]:
    """偽の ssh・scp を置いたディレクトリ。呼ばれた引数を calls.log に書く。"""
    b = tmp_path / "bin"
    b.mkdir()
    log = tmp_path / "calls.log"
    (b / "ssh").write_text(f'#!/bin/sh\necho "ssh $*" >> "{log}"\ncat >/dev/null\n{ssh_script}\n')
    (b / "scp").write_text(f'#!/bin/sh\necho "scp $*" >> "{log}"\nexit 0\n')
    for f in ("ssh", "scp"):
        (b / f).chmod(0o755)
    return b, log


def run_runner(tmp_path: Path, env_extra: dict, ssh_script: str = "exit 1"):
    b, log = fake_bin(tmp_path, ssh_script)
    env = {"PATH": f"{b}:/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path)} | env_extra
    p = subprocess.run(["bash", str(RUNNER), str(tmp_path / "out")], env=env, capture_output=True,
                       text=True, timeout=60, stdin=subprocess.DEVNULL)
    calls = log.read_text() if log.exists() else ""
    return p, calls


@pytest.mark.parametrize("value", [None, "no", "YES", "1"])
def test_runner_refuses_without_confirm(tmp_path, value):
    env = {} if value is None else {"CONFIRM_REBOOT": value}
    p, calls = run_runner(tmp_path, env)
    assert p.returncode != 0
    assert "CONFIRM_REBOOT=yes" in p.stderr
    assert calls == ""                       # ssh も scp も呼ばない
    assert not (tmp_path / "out").exists()   # 何も作らない


def test_runner_stops_on_pending_commit_confirm(tmp_path):
    # pgrep が commit-confirm を見つけた（終了コード 0）ら、再起動しない
    script = 'case "$*" in *ommit-confirm-notify*) exit 0 ;; esac\nexit 1'
    p, calls = run_runner(tmp_path, {"CONFIRM_REBOOT": "yes"}, script)
    assert p.returncode != 0
    assert "commit-confirm" in p.stderr
    assert "reboot" not in calls


def test_runner_stops_when_saved_config_lacks_mgmt(tmp_path):
    # 保存済みの設定に VRF MGMT の SSH が無ければ、再起動の後に入れなくなるので止める
    script = ('case "$*" in *ommit-confirm-notify*) exit 1 ;; '
              "*config.boot*) echo \"set interfaces ethernet eth0 address 'dhcp'\"; exit 0 ;; esac\nexit 1")
    p, calls = run_runner(tmp_path, {"CONFIRM_REBOOT": "yes"}, script)
    assert p.returncode != 0
    assert "管理系" in p.stderr
    assert "reboot" not in calls


@pytest.mark.parametrize("script", ["run-reboot.sh", "run-e3-e4.sh"])
def test_saved_config_hides_snmp_community(script):
    # results/ に残す show configuration commands に、SNMP の community の実値を書かない（鍵・パスワードと同じく伏せる）
    import re
    text = (ROOT / "scripts" / script).read_text()
    expr = re.search(r'show configuration commands\' \\\n\s*\| sed -E "([^"]+)"', text).group(1)
    sample = ("set service snmp community 'S3cret-x' authorization 'ro'\n"
              "set service snmp community 'S3cret-x' network '192.168.2.50/32'\n"
              "set system login user vyos authentication public-keys claude-lab key 'AAAAC3Nz'\n")
    out = subprocess.run(["sed", "-E", expr], input=sample, capture_output=True, text=True, check=True).stdout
    assert "S3cret-x" not in out and "AAAAC3Nz" not in out
    assert "network '192.168.2.50/32'" in out and "authorization 'ro'" in out


def test_runner_syntax():
    subprocess.run(["bash", "-n", str(RUNNER)], check=True)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck が無い")
def test_runner_shellcheck():
    subprocess.run(["shellcheck", str(RUNNER)], check=True)


def test_runner_is_executable_by_bash_only_from_repo():
    assert RUNNER.read_text().startswith("#!/usr/bin/env bash\n")
    assert os.access(RUNNER, os.R_OK)
