"""scripts/pc02-commit.sh の、SNMP の community の置き換え（@SNMP_COMMUNITY@ → 環境変数 SNMP_COMMUNITY）を検査する。

ssh は偽物に差し替える（PATH の先頭に置く）。偽物は渡された標準入力を記録し、「変更なし」と答えて
スクリプトを早く終わらせる。実機には何も送らない。
"""

import os
import re
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


# ---- commit-confirm が始まったときの流れ（confirm まで進むか） ----------------------------

# commit-confirm が始まったと答える偽物。confirm の接続（標準入力に confirm の行がある）は記録だけする。
# 鍵の SSH の確認（リモートのコマンドが true）は FAKE_TRUE_RC で成否を決める
FAKE_SSH_CONFIRM = """#!/usr/bin/env bash
for last; do :; done
echo "$*" >> "${FAKE_LOG}/calls"
case "${last}" in
  *pgrep*) exit 1 ;;
  *vbash*)
    body=$(cat)
    if grep -qx confirm <<<"${body}"; then printf '%s\\n' "${body}" > "${FAKE_LOG}/confirm"; exit 0; fi
    printf '%s\\n' "${body}" > "${FAKE_LOG}/stdin"
    printf '%s\\n' "${body}"
    echo "Initialized commit-confirm, 5 minutes"
    # community の行を最後に出す（pc02-commit.sh が tail -5 で見せる範囲に入れ、伏せ字が効いていることを確かめる）
    grep 'snmp community' <<<"${body}" | tail -2
    ;;
  true) exit "${FAKE_TRUE_RC:-0}" ;;
esac
"""


def run_confirm(tmp_path: Path, community: str, true_rc: int = 0) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH_CONFIRM)
    ssh.chmod(0o755)
    sleep = bin_dir / "sleep"  # 鍵の SSH の確認の待ち（5 秒 × 6 回）を飛ばす
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "SNMP_COMMUNITY"}
    env |= {"PATH": f"{bin_dir}:{env['PATH']}", "FAKE_LOG": str(tmp_path), "SNMP_COMMUNITY": community,
            "FAKE_TRUE_RC": str(true_rc)}
    return subprocess.run(["bash", str(SCRIPT), str(SNMP)], env=env, capture_output=True, text=True)


@pytest.mark.parametrize("value", ["commit", "confirm", "commit-confirm", "Initialized", "changes"])
def test_masking_does_not_hide_the_commit_confirm_state(tmp_path, value):
    # 伏せ字は表示用だけ。判定は伏せる前の出力で行う（community が commit-confirm などの語と重なっても confirm に進む）
    out = run_confirm(tmp_path, value)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (tmp_path / "confirm").exists(), "confirm を送っていない（5 分後に PC02 が再起動する）"
    assert f"community '{value}'" not in out.stdout + out.stderr


def test_confirm_is_not_sent_when_key_ssh_never_succeeds(tmp_path):
    # 別の接続で鍵の SSH が 1 回も通らなければ confirm しない（§1 ルール 2）。commit-confirm の時間切れで戻させる
    out = run_confirm(tmp_path, "Ab9-x_y!z*", true_rc=255)
    assert out.returncode != 0
    assert not (tmp_path / "confirm").exists()
    assert "confirm" in out.stderr


def test_key_ssh_check_opens_a_fresh_connection(tmp_path):
    # ~/.ssh/config の接続共有（ControlMaster）で既存の接続を使い回すと、鍵の認証をし直さずに通ってしまう
    out = run_confirm(tmp_path, "Ab9-x_y!z*")
    assert out.returncode == 0, out.stdout + out.stderr
    checks = [c for c in (tmp_path / "calls").read_text().splitlines() if c.endswith(" pc02 true")]
    assert checks
    for c in checks:
        for opt in ("BatchMode=yes", "ConnectTimeout=5", "ControlMaster=no", "ControlPath=none"):
            assert f"-o {opt}" in c, c


# ---- 印の一般化（@SNMP_COMMUNITY@ 以外の印も、投入時に環境変数で置き換える） -------------------

PLACEHOLDER_VARS = ("SNMP_COMMUNITY", "SSH_PUBKEY", "ULA_PREFIX", "WAN_PREFIX64", "NGN_GW_LLA")
FILL = ROOT / "scripts" / "fill-placeholders.sh"
PUBKEY = "AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample"


def run_env(tmp_path: Path, text: str, values: dict[str, str]) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    cmds = tmp_path / "in.cmds"
    cmds.write_text(text)
    env = {k: v for k, v in os.environ.items() if k not in PLACEHOLDER_VARS}
    env |= {"PATH": f"{bin_dir}:{env['PATH']}", "FAKE_LOG": str(tmp_path), **values}
    return subprocess.run(["bash", str(SCRIPT), str(cmds)], env=env, capture_output=True, text=True)


PROD_LINES = """set system login user vyos authentication public-keys admin key '@SSH_PUBKEY@'
set interfaces ethernet eth1 address '@ULA_PREFIX@::1/64'
set service ndp-proxy interface eth2 prefix '@WAN_PREFIX64@::/64' mode 'static'
set protocols static route6 ::/0 next-hop '@NGN_GW_LLA@' interface 'eth2'
"""
GOOD = {"SSH_PUBKEY": PUBKEY, "ULA_PREFIX": "fd12:3456:789a:1", "WAN_PREFIX64": "2001:db8:0:1",
        "NGN_GW_LLA": "fe80::1"}


def test_every_placeholder_is_replaced(tmp_path):
    out = run_env(tmp_path, PROD_LINES, GOOD)
    assert out.returncode == 0, out.stderr
    sent = (tmp_path / "stdin").read_text()
    assert not re.search(r"@[A-Z][A-Z0-9_]*@", sent)
    assert f"key '{PUBKEY}'" in sent
    assert "address 'fd12:3456:789a:1::1/64'" in sent
    assert "prefix '2001:db8:0:1::/64'" in sent
    assert "next-hop 'fe80::1'" in sent


@pytest.mark.parametrize("missing", sorted(GOOD))
def test_missing_variable_stops_before_ssh(tmp_path, missing):
    out = run_env(tmp_path, PROD_LINES, {k: v for k, v in GOOD.items() if k != missing})
    assert out.returncode != 0
    assert missing in out.stderr
    assert not (tmp_path / "calls").exists()


@pytest.mark.parametrize(("name", "bad"), [
    ("SSH_PUBKEY", "ssh-ed25519 AAAA"), ("SSH_PUBKEY", "x'y"),
    ("ULA_PREFIX", "2001:db8:0:1"), ("ULA_PREFIX", "fd12:3456::"), ("ULA_PREFIX", "fd12:3456:789a:1::/64"),
    ("WAN_PREFIX64", "2001:db8::/64"), ("WAN_PREFIX64", "2001:db8:0:1:2"),
    ("NGN_GW_LLA", "2001:db8::1"), ("NGN_GW_LLA", "fe80::1'"),
])
def test_bad_value_stops_before_ssh(tmp_path, name, bad):
    out = run_env(tmp_path, PROD_LINES, GOOD | {name: bad})
    assert out.returncode != 0
    assert name in out.stderr
    assert not (tmp_path / "calls").exists()


def test_unknown_placeholder_stops_before_ssh(tmp_path):
    out = run_env(tmp_path, "set system host-name '@NO_SUCH_THING@'\n", {})
    assert out.returncode != 0
    assert "@NO_SUCH_THING@" in out.stderr
    assert not (tmp_path / "calls").exists()


def run_fill(tmp_path: Path, text: str, values: dict[str, str]) -> subprocess.CompletedProcess:
    cmds = tmp_path / "in.cmds"
    cmds.write_text(text)
    env = {k: v for k, v in os.environ.items() if k not in PLACEHOLDER_VARS} | values
    return subprocess.run(["bash", str(FILL), str(cmds)], env=env, capture_output=True, text=True)


def test_fill_placeholders_prints_the_filled_lines(tmp_path):
    out = run_fill(tmp_path, PROD_LINES, GOOD)
    assert out.returncode == 0, out.stderr
    assert "@SSH_PUBKEY@" not in out.stdout and "fd12:3456:789a:1::1/64" in out.stdout


@pytest.mark.parametrize("token", ["@snmp_community@", "@Ula_Prefix@", "@_X@", "@NGN-GW@", "@9X@"])
def test_malformed_placeholder_stops(tmp_path, token):
    # 決まった形（@大文字_数字@）に合わない印も、置き換えずに残さない
    out = run_env(tmp_path, f"set service snmp community '{token}' authorization 'ro'\n",
                  {"SNMP_COMMUNITY": "Ab9-x_y!z*"})
    assert out.returncode != 0
    assert token in out.stderr
    assert not (tmp_path / "calls").exists()
    assert run_fill(tmp_path, f"set system host-name '{token}'\n", {}).returncode != 0


@pytest.mark.parametrize("community", ["a@B@c", "@x@", "x@SSH_PUBKEY@y"])
def test_at_signs_in_the_community_value_are_not_placeholders(tmp_path, community):
    # community には @ が使える。置き換えた値の中の @ を印と読まない
    # 同じ投入に別の印があっても、community の値の中を置き換えない
    text = ("set service snmp community '@SNMP_COMMUNITY@' authorization 'ro'\n"
            "set system login user vyos authentication public-keys admin key '@SSH_PUBKEY@'\n")
    out = run_env(tmp_path, text, {"SNMP_COMMUNITY": community, "SSH_PUBKEY": PUBKEY})
    assert out.returncode == 0, out.stderr
    sent = (tmp_path / "stdin").read_text()
    assert f"community '{community}' authorization" in sent
    assert f"key '{PUBKEY}'" in sent


def test_fill_placeholders_stops_when_an_input_file_is_unreadable(tmp_path):
    # 一部のファイルを読めないまま残りだけを出すと、CI が欠けた設定を検査してしまう
    good = tmp_path / "good.cmds"
    good.write_text("set system host-name 'x'\n")
    env = {k: v for k, v in os.environ.items() if k not in PLACEHOLDER_VARS}
    out = subprocess.run(["bash", str(FILL), str(good), str(tmp_path / "missing.cmds")],
                         env=env, capture_output=True, text=True)
    assert out.returncode != 0 and out.stdout == ""
    assert "missing.cmds" in out.stderr


def test_fill_placeholders_allows_files_without_commands(tmp_path):
    # コメントだけのファイル（set / delete の行が無い）は誤りではない
    only_comment = tmp_path / "c.cmds"
    only_comment.write_text("# コメントだけ\n")
    env = {k: v for k, v in os.environ.items() if k not in PLACEHOLDER_VARS}
    out = subprocess.run(["bash", str(FILL), str(only_comment)], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_fill_placeholders_stops_on_unknown_or_missing(tmp_path):
    assert run_fill(tmp_path, "set system host-name '@NOPE@'\n", {}).returncode != 0
    out = run_fill(tmp_path, PROD_LINES, {})
    assert out.returncode != 0 and out.stdout == ""
