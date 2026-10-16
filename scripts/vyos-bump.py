#!/usr/bin/env python3
"""vyos/VERSION の VyOS rolling の版を、vyos-nightly-build の最新リリースへ上げる。

    scripts/vyos-bump.py detect --releases releases.json [--version-file vyos/VERSION] [--github-output PATH]
    scripts/vyos-bump.py write  --version <リリース名> --sha256 <ISO の sha256> [--version-file vyos/VERSION]
    scripts/vyos-bump.py get    [--version-file vyos/VERSION] <キー>

detect: リリース一覧（GitHub の releases API の JSON。取得は呼び出し側が gh api で行う）から、
        generic amd64 の ISO がある最新の rolling を選び、今の VYOS_VERSION より新しいかを判定する。
        結果は key=value で標準出力と --github-output に書く（vyos/VERSION は書き換えない）。
          current / latest / update（true|false）/ sha256 / sha256_source / iso_url
write:  vyos/VERSION の VYOS_VERSION と VYOS_ISO_SHA256 の 2 行だけを書き換える（コメントと他のキーはそのまま）。
get:    vyos/VERSION のキーの値を出す。

選ぶ条件:
  - 名前が YYYY.MM.DD-HHMM-rolling で、下書きではない
  - vyos-<名前>-generic-amd64.iso の asset がある（scripts/build-vyos-image.sh が取得するもの）
  - 2026-07-01 以降（QoS の set-dscp が入った rolling。CLAUDE.md §5-2）
  - 一覧の順番ではなく、名前の日時で新しいものを選ぶ。今の版より古い版へは戻さない

ISO の sha256 の求め方（上から順に、最初に得られたもの）:
  1. checksum-asset: リリースに <ISO>.sha256 / SHA256SUMS などのチェックサムの asset があればそれを読む
  2. asset-digest:   GitHub が asset ごとに返す digest（sha256:...）。GitHub がアップロード時に計算した値
  3. download:       ISO を取得して計算する
どれで求めた値でも、scripts/build-vyos-image.sh が ISO の取得時に sha256sum -c で照合する。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VERSION_FILE = ROOT / "vyos" / "VERSION"
ARCH = "amd64"
# QoS の set-dscp（2026-06-30 の rolling、T8996）が入った最初の月
MIN_VERSION = (2026, 7, 1, 0)

VERSION_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})-(\d{4})-rolling$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CHECKSUM_SUFFIXES = (".sha256", ".sha256sum", ".sha256.txt")
CHECKSUM_FILES = ("sha256sums", "sha256sums.txt", "checksums.txt")


class BumpError(Exception):
    """版を決められない、または vyos/VERSION を書き換えられない。"""


@dataclass(frozen=True)
class Candidate:
    version: str
    iso_name: str
    iso_url: str
    iso_asset: dict
    release: dict


@dataclass(frozen=True)
class Plan:
    update: bool
    candidate: Candidate


def parse_version(name: str) -> tuple[int, int, int, int] | None:
    """YYYY.MM.DD-HHMM-rolling を (年, 月, 日, 時分) にする。それ以外の名前は None。"""
    m = VERSION_RE.fullmatch(name or "")
    if not m:
        return None
    year, month, day, hhmm = (int(g) for g in m.groups())
    return (year, month, day, hhmm)


def iso_name_for(version: str) -> str:
    return f"vyos-{version}-generic-{ARCH}.iso"


def _candidate(release: dict) -> Candidate | None:
    if release.get("draft"):
        return None
    version = release.get("tag_name") or release.get("name") or ""
    key = parse_version(version)
    if key is None or key < MIN_VERSION:
        return None
    iso_name = iso_name_for(version)
    for asset in release.get("assets") or []:
        if asset.get("name") == iso_name:
            return Candidate(version, iso_name, asset.get("browser_download_url", ""), asset, release)
    return None


def select_latest(releases: list[dict]) -> Candidate | None:
    """条件を満たすリリースのうち、名前の日時がもっとも新しいもの。無ければ None。"""
    candidates = [c for c in (_candidate(r) for r in releases) if c is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda c: parse_version(c.version))


def plan(releases: list[dict], current: str) -> Plan:
    """最新の候補と、今の版から上げるべきか。候補が 1 本も無ければ BumpError。"""
    cand = select_latest(releases)
    if cand is None:
        raise BumpError("generic amd64 の ISO がある 2026-07 以降の rolling が一覧に無い")
    cur = parse_version(current)
    update = cur is None or parse_version(cand.version) > cur
    return Plan(update=update, candidate=cand)


def _checked_sha(value: str, where: str) -> str:
    value = (value or "").strip().lower()
    if not SHA256_RE.fullmatch(value):
        raise BumpError(f"sha256 の形になっていない（{where}）: {value[:80]!r}")
    return value


def _sha_from_checksum_text(text: str, iso_name: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == iso_name:
            return parts[0]
    if len(lines) == 1 and len(lines[0].split()) == 1:
        return lines[0]
    raise BumpError(f"チェックサムの asset に {iso_name} の行が無い")


def _checksum_asset(cand: Candidate) -> dict | None:
    names = {cand.iso_name + s for s in CHECKSUM_SUFFIXES}
    for asset in cand.release.get("assets") or []:
        name = asset.get("name", "")
        if name in names or name.lower() in CHECKSUM_FILES:
            return asset
    return None


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 (https の固定 URL)
        return resp.read().decode("utf-8", "replace")


def download_sha256(url: str) -> str:
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=600) as resp:  # noqa: S310
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_sha256(cand: Candidate, fetch_text=None, download_sha256=None) -> tuple[str, str]:
    """ISO の sha256 と、その求め方（checksum-asset / asset-digest / download）。"""
    fetch_text = fetch_text or globals()["fetch_text"]
    download_sha256 = download_sha256 or globals()["download_sha256"]

    asset = _checksum_asset(cand)
    if asset is not None:
        text = fetch_text(asset["browser_download_url"])
        return _checked_sha(_sha_from_checksum_text(text, cand.iso_name), asset["name"]), "checksum-asset"

    digest = cand.iso_asset.get("digest") or ""
    if digest.startswith("sha256:"):
        return _checked_sha(digest.split(":", 1)[1], "asset の digest"), "asset-digest"

    return _checked_sha(download_sha256(cand.iso_url), "ISO を取得して計算"), "download"


def read_version_file(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def rewrite_version_file(text: str, version: str, sha256: str) -> str:
    """VYOS_VERSION と VYOS_ISO_SHA256 の行だけを書き換える。"""
    if parse_version(version) is None:
        raise BumpError(f"rolling のリリース名ではない: {version!r}")
    sha256 = _checked_sha(sha256, "--sha256")
    replace = {"VYOS_VERSION": version, "VYOS_ISO_SHA256": sha256}
    seen = set()
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        key = body.split("=", 1)[0] if "=" in body and not body.startswith("#") else None
        if key in replace:
            out.append(f"{key}={replace[key]}" + line[len(body):])
            seen.add(key)
        else:
            out.append(line)
    missing = sorted(set(replace) - seen)
    if missing:
        raise BumpError(f"vyos/VERSION に {', '.join(missing)} の行が無い")
    return "".join(out)


def _write_outputs(values: dict[str, str], github_output: str | None) -> None:
    lines = "".join(f"{k}={v}\n" for k, v in values.items())
    sys.stdout.write(lines)
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="最新の rolling を選び、上げるべきかを判定する")
    d.add_argument("--releases", required=True, help="GitHub の releases API の JSON（配列）")
    d.add_argument("--version-file", default=str(DEFAULT_VERSION_FILE))
    d.add_argument("--github-output", default=None)

    w = sub.add_parser("write", help="VYOS_VERSION と VYOS_ISO_SHA256 を書き換える")
    w.add_argument("--version-file", default=str(DEFAULT_VERSION_FILE))
    w.add_argument("--version", required=True)
    w.add_argument("--sha256", required=True)

    g = sub.add_parser("get", help="vyos/VERSION のキーの値を出す")
    g.add_argument("--version-file", default=str(DEFAULT_VERSION_FILE))
    g.add_argument("key")

    args = p.parse_args(argv)
    try:
        path = Path(args.version_file)
        text = path.read_text(encoding="utf-8")
        if args.cmd == "get":
            values = read_version_file(text)
            if args.key not in values:
                raise BumpError(f"{path} に {args.key} が無い")
            print(values[args.key])
        elif args.cmd == "write":
            path.write_text(rewrite_version_file(text, args.version, args.sha256), encoding="utf-8")
        else:
            releases = json.loads(Path(args.releases).read_text(encoding="utf-8"))
            current = read_version_file(text).get("VYOS_VERSION", "")
            result = plan(releases, current)
            sha, source = resolve_sha256(result.candidate)
            _write_outputs(
                {
                    "current": current,
                    "latest": result.candidate.version,
                    "update": "true" if result.update else "false",
                    "sha256": sha,
                    "sha256_source": source,
                    "iso_url": result.candidate.iso_url,
                },
                args.github_output,
            )
    except BumpError as e:
        print(f"vyos-bump: エラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
