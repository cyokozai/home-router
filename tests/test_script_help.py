"""引数が足りないときに先頭のコメントを使い方として出すスクリプト（sed -n 'A,Bp' "$0"）の検査。

範囲がコメントの外にはみ出すと、使い方の末尾にシェルのコード行（set -euo pipefail など）がそのまま出る。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HELP = re.compile(r"""sed -n '(\d+),(\d+)p' "\$0\"""")
SCRIPTS = sorted(p for p in (ROOT / "scripts").glob("*.sh") if HELP.search(p.read_text(encoding="utf-8")))


def test_some_scripts_use_the_help_pattern():
    assert SCRIPTS


# PR #25 より前からある同じ誤り（範囲の終わりが set -euo pipefail の行）。この PR の範囲の外なので直さずに残し、
# 直したら strict の xfail が落ちて気づけるようにする
KNOWN = {"vyos-apply.sh", "vyos-check-cmds.sh"}


@pytest.mark.parametrize("script", [
    pytest.param(p, marks=pytest.mark.xfail(strict=True, reason="既存の誤り（PR #25 の範囲外）"))
    if p.name in KNOWN else p for p in SCRIPTS], ids=lambda p: p.name)
def test_help_range_covers_only_the_header_comment(script):
    lines = script.read_text(encoding="utf-8").splitlines()
    for m in HELP.finditer(script.read_text(encoding="utf-8")):
        start, end = int(m.group(1)), int(m.group(2))
        shown = lines[start - 1:end]
        assert all(line.startswith("#") for line in shown), f"{script.name}: {start},{end}p にコメント以外の行がある"
        # 先頭のコメントを最後まで出す（途中で切らない）
        assert end >= len(lines) or not lines[end].startswith("#"), f"{script.name}: {end} 行目の後にもコメントが続く"
