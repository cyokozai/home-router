"""scripts/mape_calc.py（v6プラスの MAP-E の値を委任プレフィックスから計算する道具）の検査。

既知の例は、コミュニティの計算機・実例と『徹底解説v6プラス』表 4.1、RFC 7597 の付録の形の例。
出す set の行は vyos/mape-portsets.cmds と同じ構成であること（値だけが計算したものに替わる）。
"""

import importlib.util
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mc = load("mape_calc")
gen = load("gen_mape_portsets")

BR = "2404:9200:225:100::64"


def ports_for(psid: int, a: int = 4, k: int = 8) -> list[tuple[int, int]]:
    m = 16 - a - k
    return [((i << (16 - a)) | (psid << m), ((i << (16 - a)) | (psid << m)) + (1 << m) - 1)
            for i in range(1, 1 << a)]


# ---- 既知の例 -----------------------------------------------------------------------

KNOWN = [
    # 入力, IPv4, PSID, CE（draft-03 の形）, 最初のポート範囲, 最後のポート範囲
    ("240b:12:3456:7800::/64", "14.8.52.86", 120, "240b:12:3456:7800:e:834:5600:7800",
     (6016, 6031), (63360, 63375)),
    ("240b:10:aabb:ccdd::/64", "106.72.170.187", 204, "240b:10:aabb:cc00:6a:48aa:bb00:cc00",
     (7360, 7375), (64704, 64719)),
    ("240b:11:fe:fe00::/56", "106.73.0.254", 254, "240b:11:fe:fe00:6a:4900:fe00:fe00",
     (8160, 8175), (65504, 65519)),
]


@pytest.mark.parametrize(("prefix", "ipv4", "psid", "ce", "first", "last"), KNOWN)
def test_v6plus_known_examples(prefix, ipv4, psid, ce, first, last):
    r = mc.calc(prefix)
    assert r.ipv4 == ipaddress.IPv4Address(ipv4)
    assert r.psid == psid
    assert r.ce == ipaddress.IPv6Address(ce)
    assert r.br == ipaddress.IPv6Address(BR)
    assert len(r.ports) == 15
    assert r.ports[0] == first and r.ports[-1] == last
    assert r.ports == ports_for(psid)


def test_v6plus_example_with_hextet_bits_set_warns_on_64():
    # ひかり電話なしの /64 で第 4 hextet の下位 8 ビットが 0 でないと、CE は自分の /64 の外になる
    r = mc.calc("240b:10:aabb:ccdd::/64")
    assert any("/64" in w for w in r.warnings)
    assert mc.calc("240b:12:3456:7800::/64").warnings == []
    assert mc.calc("240b:11:fe:fe00::/56").warnings == []


def test_rfc_style_generic_rule():
    # RFC 7597 の付録の形の例（EA 16 ビット、Rule IPv6 2001:db8::/40、Rule IPv4 192.0.2.0/24）
    rule = mc.Rule(ipv6="2001:db8::/40", ipv4="192.0.2.0/24", ea_len=16, offset=6, br="2001:db8:ffff::1")
    r = mc.calc("2001:db8:12:3400::/56", rule=rule, iid_format="rfc7597")
    assert r.ipv4 == ipaddress.IPv4Address("192.0.2.18")
    assert r.psid == 52
    assert r.ce == ipaddress.IPv6Address("2001:db8:12:3400:0:c000:212:34")


def test_rfc7597_iid_format_for_v6plus():
    r = mc.calc("240b:12:3456:7800::/64", iid_format="rfc7597")
    assert r.ce == ipaddress.IPv6Address("240b:12:3456:7800:0:e08:3456:78")


@pytest.mark.parametrize("prefix", ["2404:7a80::/56", "2400:4050:1234:5600::/56", "2001:db8::/56"])
def test_prefix_outside_v6plus_rules_is_an_error(prefix):
    with pytest.raises(mc.MapeError):
        mc.calc(prefix)


def test_prefix_shorter_than_the_end_user_prefix_is_an_error():
    with pytest.raises(mc.MapeError):
        mc.calc("240b:10::/48")


# ---- 出す set の行 --------------------------------------------------------------------


def commands(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if re.match(r"^(set|delete) ", line.strip())]


def lab_lines() -> list[str]:
    return mc.cmds_lines(ipv4="192.0.2.18", psid=0xFE, ce="2001:db8:20::c", br="2001:db8:20::b",
                         wan_if="eth2", lan="192.168.10.0/24")


def test_cmds_with_lab_values_equal_the_lab_file():
    # 検証用の値を渡すと、今の vyos/mape-portsets.cmds とまったく同じ行（順番も）になる
    file_lines = commands((ROOT / "vyos" / "mape-portsets.cmds").read_text(encoding="utf-8"))
    assert lab_lines() == file_lines


def shape(line: str) -> str:
    return re.sub(r"'[^']*'", "'…'", line)


@pytest.mark.parametrize("prefix", [k[0] for k in KNOWN])
def test_cmds_for_known_prefixes_keep_the_lab_shape(prefix):
    r = mc.calc(prefix)
    lines = mc.result_cmds(r, wan_if="eth2", lan="192.168.10.0/24")
    assert [shape(x) for x in lines] == [shape(x) for x in lab_lines()]
    assert f"set interfaces ethernet eth2 address '{r.ce}/64'" in lines
    assert f"set interfaces tunnel tun0 source-address '{r.ce}'" in lines
    assert f"set interfaces tunnel tun0 remote '{BR}'" in lines
    assert f"set interfaces tunnel tun0 address '{r.ipv4}/32'" in lines
    assert "set interfaces tunnel tun0 parameters ipv6 encaplimit 'none'" in lines
    assert "set interfaces tunnel tun0 parameters ipv6 tclass '0x00'" in lines
    assert "set interfaces tunnel tun0 mtu '1460'" in lines
    assert "set interfaces tunnel tun0 ip adjust-mss '1420'" in lines
    assert "set protocols static route 0.0.0.0/0 interface 'tun0'" in lines
    assert f"set nat source rule 199 translation address '{r.ipv4}'" in lines

    trans, src = {}, {}
    for line in lines:
        m = re.match(r"set nat source rule (\d+) translation port '(\d+)-(\d+)'", line)
        if m:
            trans[int(m.group(1))] = (int(m.group(2)), int(m.group(3)))
        m = re.match(r"set nat source rule (\d+) source port '([\d,-]+)'", line)
        if m:
            src[int(m.group(1))] = [tuple(map(int, x.split("-"))) for x in m.group(2).split(",")]
        m = re.match(r"set nat source rule (\d+) translation address '([\d.]+)'", line)
        if m:
            assert m.group(2) == str(r.ipv4)
    assert sorted(trans.values()) == r.ports
    assert set(trans) == set(src) == set(range(101, 116))
    ranges = sorted(x for rs in src.values() for x in rs)
    assert ranges[0][0] == 1 and ranges[-1][1] == 65535
    assert all(p[1] + 1 == n[0] for p, n in zip(ranges, ranges[1:]))


def test_cmds_old_prefix_deletes_the_old_addresses():
    new = mc.calc("240b:12:3456:7800::/64")
    old = mc.calc("240b:11:fe:fe00::/56")
    lines = mc.result_cmds(new, wan_if="eth9", lan="192.168.10.0/24", old=old)
    assert lines[:2] == [f"delete interfaces ethernet eth9 address '{old.ce}/64'",
                         f"delete interfaces tunnel tun0 address '{old.ipv4}/32'"]
    assert f"set interfaces ethernet eth9 address '{new.ce}/64'" in lines


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(SCRIPTS / "mape_calc.py"), *args],
                          capture_output=True, text=True)


def test_cli_summary_and_cmds():
    out = run("240b:12:3456:7800::/64")
    assert out.returncode == 0, out.stderr
    for text in ("14.8.52.86", "120", "6016-6031", "240b:12:3456:7800:e:834:5600:7800", BR):
        assert text in out.stdout
    out = run("--cmds", "--wan-if", "eth2", "240b:12:3456:7800::/64")
    assert out.returncode == 0, out.stderr
    assert all(re.match(r"^(set|delete) ", x) for x in out.stdout.splitlines())


def test_cli_cmds_requires_wan_if():
    out = run("--cmds", "240b:12:3456:7800::/64")
    assert out.returncode != 0


def test_cli_error_and_warning():
    out = run("2404:7a80::/56")
    assert out.returncode != 0 and "v6プラス" in out.stderr
    out = run("240b:10:aabb:ccdd::/64")
    assert out.returncode == 0 and "/64" in out.stderr


# ---- gen_mape_portsets.py の引数 ------------------------------------------------------


def test_generator_defaults_are_the_lab_values():
    assert gen.rule_lines() == gen.rule_lines(psid=0xFE, ipv4="192.0.2.18")


def test_generator_takes_psid_and_ipv4():
    lines = gen.rule_lines(psid=120, ipv4="14.8.52.86")
    assert "set nat source rule 101 translation port '6016-6031'" in lines
    assert "set nat source rule 115 translation port '63360-63375'" in lines
    assert all("'192.0.2.18'" not in x for x in lines)
    out = subprocess.run([sys.executable, "-I", str(SCRIPTS / "gen_mape_portsets.py"),
                          "--psid", "120", "--ipv4", "14.8.52.86"],
                         check=True, capture_output=True, text=True).stdout.splitlines()
    assert out == lines
