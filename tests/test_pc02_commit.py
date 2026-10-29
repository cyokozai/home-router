"""scripts/pc02-commit.sh の、SNMP の community の置き換え（@SNMP_COMMUNITY@ → 環境変数 SNMP_COMMUNITY）を検査する。

ssh は偽物に差し替える（PATH の先頭に置く）。偽物は渡された標準入力を記録し、「変更なし」と答えて
スクリプトを早く終わらせる。実機には何も送らない。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pc02-commit.sh"
SNMP = ROOT / "vyos" / "snmp-mgmt.cmds"

FAKE_SSH = """#!/usr/bin/env bash
# 引数の最後がリモートのコマンド。pgrep には「残っていない」（1）で返す
for last; do :; done
echo "$*" >> "${FAKE_LOG}/calls"
case "${last}" in
  *pgrep*) exit 1 ;;
  *vbash*) cat > "${FAKE_LOG}/stdin"; cat "${FAKE_LOG}/stdin"; echo "No configuration changes to commit" ;;
esac
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash が無い")


def run(tmp_path: Path, cmds: Path, community: str | None) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "SNMP_COMMUNITY"}
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_LOG"] = str(tmp_path)
    if community is not None:
        env["SNMP_COMMUNITY"] = community
    return subprocess.run(["bash", str(SCRIPT), str(cmds)], env=env, capture_output=True, text=True)


def test_placeholder_without_env_stops_before_ssh(tmp_path):
    out = run(tmp_path, SNMP, None)
    assert out.returncode != 0
    assert "SNMP_COMMUNITY" in out.stderr
    assert not (tmp_path / "calls").exists()


@pytest.mark.parametrize("bad", ["", "has space", "quote'x", "hash#x", "x" * 101])
def test_invalid_community_stops_before_ssh(tmp_path, bad):
    out = run(tmp_path, SNMP, bad)
    assert out.returncode != 0
    assert not (tmp_path / "calls").exists()


def test_placeholder_is_replaced_and_not_printed(tmp_path):
    value = "Ab9-x_y!z*"
    out = run(tmp_path, SNMP, value)
    assert out.returncode == 0, out.stderr
    sent = (tmp_path / "stdin").read_text()
    assert "@SNMP_COMMUNITY@" not in sent
    assert f"set service snmp community '{value}' authorization 'ro'" in sent
    assert value not in out.stdout + out.stderr


def test_files_without_placeholder_do_not_need_env(tmp_path):
    out = run(tmp_path, ROOT / "vyos" / "tuning-2-rps.cmds", None)
    assert out.returncode == 0, out.stderr
    assert "set interfaces ethernet eth1 offload rps" in (tmp_path / "stdin").read_text()
