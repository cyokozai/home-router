"""公開リポジトリに、実際のグローバル IP・機材のシリアル・秘密の値を残さない検査。

configs/ と results/ の追跡中のテキストファイルをすべて読み、次のものが入っていたら落ちる。

- (a) グローバルな IPv6 アドレス（2000::/3 のうち ipaddress の is_global が真のもの）。
  文書用の 2001:db8::/32、リンクローカル fe80::/10、::1 などは is_global が偽なので対象外。
  v6プラスの MAP ルールの範囲と BR のように、公開されている値だけを ALLOWED に書いて通す。
- (b) `S/N:` `Serial Number:` `UUID:` `product_uuid` の後ろの、伏せ字でない値。
- (c) `encrypted-password` `plaintext-password` `community` と `key '…'` の、伏せ字でない値。
  伏せ字は `<伏せ字>`、SNMP の community は `@SNMP_COMMUNITY@`（scripts/pc02-commit.sh が置き換える印）。

PC02 の設定を書き出すときに伏せているかは tests/test_reboot.py が見る。ここは、書き出した後のファイルを見る。
"""

import ipaddress
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("configs", "results")

MASKS = {"<伏せ字>", "'<伏せ字>'", "@SNMP_COMMUNITY@", "'@SNMP_COMMUNITY@'", "''"}

# 公開されている値（CLAUDE.md §6-5 の v6プラスの MAP ルールと BR）。範囲は書かれた形のまま一致したときだけ通す
# （240b:12::/31 の中には実際の家のプレフィックスも入るので、範囲の中のアドレスを通してはいけない）。
ALLOWED = {
    "240b:10::/31", "240b:12::/31", "240b:250::/31", "240b:252::/31",
    "2404:9200:225:100::64",
}

GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")
# 前後に 16 進や「:」が続くものは拾わない（RSS の鍵のような 「xx:xx:…」 の長い並びを、途中で切って拾わないため）
IPV6 = re.compile(r"(?<![0-9A-Za-z:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?:/\d{1,3})?(?![0-9A-Za-z:])")
SERIAL = re.compile(r"(S/N|Serial Number|UUID|product_uuid)\s*[:=]\s*(\S+)", re.IGNORECASE)
SECRET = re.compile(r"(encrypted-password|plaintext-password|community)\s+('[^']*'|\S+)|\bkey\s+('[^']*')")


def find_sensitive(text: str) -> list[str]:
    """見つけたものを「何行目・種類」で返す。値そのものは返さない（落ちたときの表示に値を出さないため）。"""
    # 行ごとに分けず、ファイル全体に正規表現を当てて、見つけたときだけ行番号を数える（results/ は数千ファイルあるため）
    found = []

    def at(m: re.Match) -> str:
        return f"{text.count(chr(10), 0, m.start()) + 1}行目"

    for m in IPV6.finditer(text):
        token = m.group(0)
        if token.lower() in ALLOWED:
            continue
        try:
            ip = ipaddress.ip_interface(token).ip
        except ValueError:
            continue
        if ip.version == 6 and ip in GLOBAL_UNICAST and ip.is_global:
            found.append(f"{at(m)}: グローバルな IPv6 アドレス")
    for m in SERIAL.finditer(text):
        if m.group(2) not in MASKS:
            found.append(f"{at(m)}: {m.group(1)} の値")
    for m in SECRET.finditer(text):
        kind, value = (m.group(1), m.group(2)) if m.group(1) else ("key", m.group(3))
        if value not in MASKS:
            found.append(f"{at(m)}: {kind} の値")
    return found


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", *SCANNED],
                             capture_output=True, check=True).stdout.decode()
        return [ROOT / p for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        # git の無いコンテナ（作業ツリーだけを差し込んだもの）では、ディレクトリをそのまま辿る
        return [Path(d) / f for top in SCANNED for d, _, fs in os.walk(ROOT / top) for f in fs]


def read_text(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def test_tracked_files_have_no_sensitive_data():
    files = tracked_files()
    assert files, "configs/ と results/ のファイルが見つからない"
    problems = []
    for path in files:
        text = read_text(path)
        if text is None:
            continue
        problems += [f"{path.relative_to(ROOT)}: {p}" for p in find_sensitive(text)]
    assert not problems, "伏せていない値がある:\n" + "\n".join(problems)


@pytest.mark.parametrize("line", [
    "eno1  UP  192.168.2.190/24 <global-ipv6>/64 <link-local>/64",
    "inet6 2001:db8:10::10/64 scope global",
    "inet6 fe80::1/64 scope link",
    "inet6 ::1/128 scope host",
    "Hardware S/N: <伏せ字>",
    "Hardware UUID: <伏せ字>",
    "set system login user vyos authentication encrypted-password '<伏せ字>'",
    "set system login user vyos authentication plaintext-password ''",
    "set system login user vyos authentication public-keys claude-lab key '<伏せ字>'",
    "set service snmp community '@SNMP_COMMUNITY@' authorization 'ro'",
    "set service snmp community @SNMP_COMMUNITY@ network '192.168.2.50/32'",
    "Src IP addr: :: mask: ffff:ffff::ffff:ffff:ffff:ffff",
    "net.core.netdev_rss_key = " + ":".join(["8e"] * 52),
    "filter parent 1: protocol all pref 1 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0",
    "echo \"::endgroup::\"",
    "BR 2404:9200:225:100::64、ルール 240b:12::/31",
])
def test_masked_and_harmless_lines_pass(line):
    assert find_sensitive(line) == []


@pytest.mark.parametrize("line, kind", [
    ("eno1  UP  192.168.2.190/24 2400:4050:1234:5600::1/64", "IPv6"),
    ("inet6 240b:12:3456:7800::1/64 scope global", "IPv6"),
    ("Hardware S/N: ABC1234567", "S/N"),
    ("Hardware UUID: 00000000-1111-2222-3333-444444444444", "UUID"),
    ("/sys/class/dmi/id/product_uuid=00000000-1111-2222-3333-444444444444", "product_uuid"),
    ("set system login user vyos authentication encrypted-password '$6$abc'", "encrypted-password"),
    ("set service snmp community S3cret-x authorization 'ro'", "community"),
    ("set interfaces wireguard wg0 private-key 'abcdef'", "key"),
])
def test_unmasked_values_are_found(line, kind):
    found = find_sensitive(line)
    assert len(found) == 1 and kind in found[0]
    # 落ちたときの表示に、値そのものを出さない（行番号と種類だけ）
    assert re.fullmatch(r"1行目: (グローバルな IPv6 アドレス|\S+ の値)", found[0])
