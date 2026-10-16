"""scripts/vyos-bump.py（VyOS rolling の版を上げる）の検査。

ネットワークには出ない。リリース一覧は tests/fixtures/vyos-bump/releases.json
（GitHub の releases API と同じ形）で与え、チェックサムの取得や ISO の取得は
関数を差し替えて代わりに返す。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "vyos-bump.py"
FIXTURE = ROOT / "tests" / "fixtures" / "vyos-bump" / "releases.json"
VERSION_FILE = ROOT / "vyos" / "VERSION"


def _load():
    spec = importlib.util.spec_from_file_location("vyos_bump", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclass が sys.modules から自分のモジュールを引くので、実行前に登録する
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bump = _load()


@pytest.fixture
def releases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _release(name, *assets, draft=False):
    return {
        "tag_name": name,
        "name": name,
        "draft": draft,
        "prerelease": False,
        "assets": [
            {
                "name": a,
                "browser_download_url": f"https://example.invalid/{name}/{a}",
                "digest": "sha256:" + "ab" * 32,
            }
            for a in assets
        ],
    }


def _iso(name):
    return f"vyos-{name}-generic-amd64.iso"


# --- 版の名前 ---------------------------------------------------------------


def test_parse_version_accepts_rolling_names():
    assert bump.parse_version("2026.10.16-0024-rolling") == (2026, 10, 16, 24)


@pytest.mark.parametrize(
    "name",
    ["1.5-stream-2026-Q3", "2026.10.16-0024-epa1", "2026.10.16-rolling", "v2026.10.16-0024-rolling", ""],
)
def test_parse_version_rejects_other_names(name):
    assert bump.parse_version(name) is None


# --- 最新版の選び方 ---------------------------------------------------------


def test_select_latest_picks_newest_rolling_with_amd64_iso(releases):
    # 一覧の順番（公開順）ではなく、名前の日時で選ぶ。
    # 下書き・ISO の無いリリース・arm64 だけのリリース・rolling 以外の名前は無視する
    cand = bump.select_latest(releases)
    assert cand.version == "2026.10.17-0024-rolling"
    assert cand.iso_name == "vyos-2026.10.17-0024-rolling-generic-amd64.iso"
    assert cand.iso_url.endswith("/2026.10.17-0024-rolling/vyos-2026.10.17-0024-rolling-generic-amd64.iso")


def test_select_latest_ignores_release_without_iso():
    rels = [
        _release("2026.10.19-0011-rolling", _iso("2026.10.19-0011-rolling") + ".minisig"),
        _release("2026.10.01-0000-rolling", _iso("2026.10.01-0000-rolling")),
    ]
    assert bump.select_latest(rels).version == "2026.10.01-0000-rolling"


def test_select_latest_ignores_non_rolling_names():
    rels = [
        _release("1.5-stream-2026-Q3", "vyos-1.5-stream-2026-Q3-generic-amd64.iso"),
        _release("2026.08.01-0000-rolling", _iso("2026.08.01-0000-rolling")),
    ]
    assert bump.select_latest(rels).version == "2026.08.01-0000-rolling"


def test_select_latest_ignores_drafts():
    rels = [
        _release("2026.10.20-0000-rolling", _iso("2026.10.20-0000-rolling"), draft=True),
        _release("2026.08.01-0000-rolling", _iso("2026.08.01-0000-rolling")),
    ]
    assert bump.select_latest(rels).version == "2026.08.01-0000-rolling"


def test_select_latest_never_picks_before_2026_07():
    # QoS の set-dscp は 2026-06-30 の rolling で入った（CLAUDE.md §5-2）。それより前は選ばない
    rels = [
        _release("2026.06.30-2359-rolling", _iso("2026.06.30-2359-rolling")),
        _release("2026.06.29-0010-rolling", _iso("2026.06.29-0010-rolling")),
    ]
    assert bump.select_latest(rels) is None


def test_select_latest_accepts_first_july_build():
    rels = [_release("2026.07.01-0000-rolling", _iso("2026.07.01-0000-rolling"))]
    assert bump.select_latest(rels).version == "2026.07.01-0000-rolling"


# --- 上げるかどうか ---------------------------------------------------------


def test_plan_updates_when_newer(releases):
    plan = bump.plan(releases, current="2026.10.16-0024-rolling")
    assert plan.update is True
    assert plan.candidate.version == "2026.10.17-0024-rolling"


def test_plan_does_nothing_for_same_version(releases):
    plan = bump.plan(releases, current="2026.10.17-0024-rolling")
    assert plan.update is False
    assert plan.candidate.version == "2026.10.17-0024-rolling"


def test_plan_never_downgrades(releases):
    # 今の版が一覧より新しい（一覧の取得が古い、など）ときは、古い版へ戻さない
    plan = bump.plan(releases, current="2026.10.30-0000-rolling")
    assert plan.update is False


def test_plan_without_candidate_is_an_error():
    with pytest.raises(bump.BumpError):
        bump.plan([_release("2026.06.01-0000-rolling", _iso("2026.06.01-0000-rolling"))], current="2026.10.16-0024-rolling")


# --- sha256 の求め方 --------------------------------------------------------


def _no_network(*_args, **_kwargs):
    raise AssertionError("ネットワークに出てはいけない")


def test_sha256_prefers_checksum_asset():
    name = "2026.10.17-0024-rolling"
    rel = _release(name, _iso(name), _iso(name) + ".sha256")
    cand = bump.select_latest([rel])
    fetched = []

    def fetch_text(url):
        fetched.append(url)
        return f"{'cd' * 32}  {_iso(name)}\n"

    sha, source = bump.resolve_sha256(cand, fetch_text=fetch_text, download_sha256=_no_network)
    assert (sha, source) == ("cd" * 32, "checksum-asset")
    assert fetched == [f"https://example.invalid/{name}/{_iso(name)}.sha256"]


def test_sha256_reads_matching_line_from_sha256sums():
    name = "2026.10.17-0024-rolling"
    rel = _release(name, _iso(name), "SHA256SUMS")
    cand = bump.select_latest([rel])
    text = f"{'11' * 32}  other.iso\n{'ef' * 32} *{_iso(name)}\n"
    sha, source = bump.resolve_sha256(cand, fetch_text=lambda _u: text, download_sha256=_no_network)
    assert (sha, source) == ("ef" * 32, "checksum-asset")


def test_sha256_uses_asset_digest_when_no_checksum_asset(releases):
    cand = bump.select_latest(releases)
    sha, source = bump.resolve_sha256(cand, fetch_text=_no_network, download_sha256=_no_network)
    assert sha == "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert source == "asset-digest"


def test_sha256_downloads_when_nothing_else():
    name = "2026.10.17-0024-rolling"
    rel = _release(name, _iso(name))
    del rel["assets"][0]["digest"]
    cand = bump.select_latest([rel])
    sha, source = bump.resolve_sha256(cand, fetch_text=_no_network, download_sha256=lambda url: "9f" * 32)
    assert (sha, source) == ("9f" * 32, "download")


def test_sha256_rejects_malformed_value():
    name = "2026.10.17-0024-rolling"
    rel = _release(name, _iso(name), _iso(name) + ".sha256")
    cand = bump.select_latest([rel])
    with pytest.raises(bump.BumpError):
        bump.resolve_sha256(cand, fetch_text=lambda _u: "not-a-hash\n", download_sha256=_no_network)


# --- vyos/VERSION の書き換え ------------------------------------------------


def test_read_version_file_reads_keys():
    values = bump.read_version_file(VERSION_FILE.read_text(encoding="utf-8"))
    assert set(values) >= {"VYOS_VERSION", "VYOS_ISO_SHA256", "VYOS_BUILD_REF", "ISO_TO_OCI_SHA256"}


def test_rewrite_changes_only_two_lines():
    before = VERSION_FILE.read_text(encoding="utf-8")
    after = bump.rewrite_version_file(before, "2026.10.17-0024-rolling", "a1" * 32)
    b_lines, a_lines = before.splitlines(), after.splitlines()
    assert len(a_lines) == len(b_lines)
    changed = [(b, a) for b, a in zip(b_lines, a_lines) if b != a]
    assert [a for _b, a in changed] == [
        "VYOS_VERSION=2026.10.17-0024-rolling",
        "VYOS_ISO_SHA256=" + "a1" * 32,
    ]
    # コメント・他のキー・末尾の改行はそのまま
    assert after.endswith("\n") == before.endswith("\n")
    values_b, values_a = bump.read_version_file(before), bump.read_version_file(after)
    for key in ("VYOS_BUILD_REF", "ISO_TO_OCI_SHA256"):
        assert values_a[key] == values_b[key]


def test_rewrite_same_values_is_identity():
    before = VERSION_FILE.read_text(encoding="utf-8")
    values = bump.read_version_file(before)
    assert bump.rewrite_version_file(before, values["VYOS_VERSION"], values["VYOS_ISO_SHA256"]) == before


def test_rewrite_does_not_touch_comment_mentioning_key():
    text = "# VYOS_VERSION=ここはコメント\nVYOS_VERSION=old\nVYOS_ISO_SHA256=" + "0" * 64 + "\n"
    after = bump.rewrite_version_file(text, "2026.10.17-0024-rolling", "1" * 64)
    assert after.splitlines()[0] == "# VYOS_VERSION=ここはコメント"


@pytest.mark.parametrize(
    "version, sha",
    [("2026.10.17-0024-rolling", "xyz"), ("not-rolling", "a1" * 32), ("2026.10.17-0024-rolling\nX=1", "a1" * 32)],
)
def test_rewrite_rejects_bad_values(version, sha):
    with pytest.raises(bump.BumpError):
        bump.rewrite_version_file(VERSION_FILE.read_text(encoding="utf-8"), version, sha)


def test_rewrite_requires_both_keys():
    with pytest.raises(bump.BumpError):
        bump.rewrite_version_file("VYOS_VERSION=x\n", "2026.10.17-0024-rolling", "a1" * 32)


# --- コマンドライン ---------------------------------------------------------


def test_cli_detect_writes_github_output(tmp_path, monkeypatch):
    version_file = tmp_path / "VERSION"
    version_file.write_text(VERSION_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "out"
    monkeypatch.setattr(bump, "fetch_text", _no_network)
    monkeypatch.setattr(bump, "download_sha256", _no_network)
    rc = bump.main(
        ["detect", "--releases", str(FIXTURE), "--version-file", str(version_file), "--github-output", str(out)]
    )
    assert rc == 0
    lines = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())
    assert lines["current"] == bump.read_version_file(VERSION_FILE.read_text(encoding="utf-8"))["VYOS_VERSION"]
    assert lines["latest"] == "2026.10.17-0024-rolling"
    assert lines["update"] == "true"
    assert lines["sha256"] == "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert lines["sha256_source"] == "asset-digest"
    # detect は vyos/VERSION を書き換えない
    assert version_file.read_text(encoding="utf-8") == VERSION_FILE.read_text(encoding="utf-8")


def test_cli_write_rewrites_file(tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text(VERSION_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    rc = bump.main(
        ["write", "--version-file", str(version_file), "--version", "2026.10.17-0024-rolling", "--sha256", "a1" * 32]
    )
    assert rc == 0
    values = bump.read_version_file(version_file.read_text(encoding="utf-8"))
    assert values["VYOS_VERSION"] == "2026.10.17-0024-rolling"
    assert values["VYOS_ISO_SHA256"] == "a1" * 32


def test_cli_get_prints_value(tmp_path, capsys):
    rc = bump.main(["get", "--version-file", str(VERSION_FILE), "VYOS_VERSION"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == bump.read_version_file(VERSION_FILE.read_text(encoding="utf-8"))["VYOS_VERSION"]
