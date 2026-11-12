"""展示 D「家の監視画面」の、ブースで動かす複製の構成（exhibit/home-map/）の検査。

コンテナは起動しない。次を確かめる。

- compose.yaml の image が版固定で、家の thin-k8s と同じ版であること（版を変えるなら両方を直す）
- 自動で上がる（restart）・ポートは環境変数で切り替えられ、既定は覚えやすい値で 127.0.0.1 にだけ開くこと
- 認証情報の値を compose に書いていないこと（*_FILE で、実行時に生成したファイルを指す）
- keygen.py が実行時に値を作り、2 回目は作り直さず、値を表示しないこと
- shumoku_setup.py の初期設定が、2 回流しても重複を作らないこと（偽の API で確かめる）
- restore.sh が dump の形式を見分けること
- 家の構成の中身（実アドレス・MAC・dump）がリポジトリに入っていないこと
"""

import importlib.util
import ipaddress
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from test_no_sensitive_data import find_sensitive

ROOT = Path(__file__).resolve().parent.parent
EXHIBIT = Path(os.environ.get("EXHIBIT_DIR", ROOT / "exhibit" / "home-map"))
COMPOSE = EXHIBIT / "compose.yaml"

# 家の thin-k8s と揃える版。変えない（変えるなら家の側と同時に）
HOME_IMAGES = {
    "netbox": "docker.io/netboxcommunity/netbox:v4.7-5.1.1",
    "postgres": "docker.io/postgres:18-alpine",
    "valkey": "docker.io/valkey/valkey:9.1-alpine",
    "shumoku": "ghcr.io/konoe-akitoshi/shumoku:0.2.0",
}
PROJECT = "booth-home-map"
# ブースで来場者に見せる 2 つ。既定のポートと切り替えの環境変数
PUBLISHED = {
    "netbox": "${BOOTH_BIND:-127.0.0.1}:${BOOTH_NETBOX_PORT:-8080}:8080",
    "shumoku": "${BOOTH_BIND:-127.0.0.1}:${BOOTH_SHUMOKU_PORT:-8081}:8080",
}
# 一度だけ流れて終わるもの（restart しない）
ONE_SHOT = {"keygen"}
# 実行時に作るファイル。名前に token / secret / credential を含めない（フックが止めるため）
KEY_FILES = {"nb_vars.sh", "db_pw", "shumoku_admin_pw"}
FORBIDDEN_NAME = re.compile(r"token|secret|credential|\.env$|\.key$|\.pem$", re.IGNORECASE)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# ---- compose.yaml ----------------------------------------------------------


def test_project_name(compose):
    assert compose["name"] == PROJECT


def test_images_are_pinned_to_home_versions(compose):
    used = {svc["image"] for svc in compose["services"].values()}
    assert used == set(HOME_IMAGES.values()), "家と同じ 4 つの image だけを使う"
    for name, svc in compose["services"].items():
        image = svc["image"]
        tag = image.rsplit(":", 1)[-1]
        assert ":" in image and tag != "latest", f"{name}: 版を固定する"
        assert "build" not in svc, f"{name}: ブースで build しない"
    for key in HOME_IMAGES:
        assert compose["services"][key]["image"] == HOME_IMAGES[key]


def test_restart_policy_brings_services_up_after_reboot(compose):
    for name, svc in compose["services"].items():
        expected = "no" if name in ONE_SHOT else "unless-stopped"
        assert str(svc.get("restart")) == expected, name


def test_only_netbox_and_shumoku_are_published(compose):
    for name, svc in compose["services"].items():
        if name in PUBLISHED:
            assert svc["ports"] == [PUBLISHED[name]], name
        else:
            assert "ports" not in svc, f"{name}: ホストに開かない"


def _env_items(svc) -> list[tuple[str, str]]:
    env = svc.get("environment") or {}
    if isinstance(env, list):
        return [tuple(e.split("=", 1)) if "=" in e else (e, "") for e in env]
    return [(k, "" if v is None else str(v)) for k, v in env.items()]


def test_no_credential_values_in_compose(compose):
    sensitive = re.compile(r"PASSWORD|TOKEN|SECRET|PEPPER|_KEY$")
    for name, svc in compose["services"].items():
        for key, value in _env_items(svc):
            if sensitive.search(key) and not key.endswith("_FILE"):
                assert value == "", f"{name}: {key} に値を書かない"
            if key.endswith("_FILE"):
                assert value.startswith("/booth-keys/"), f"{name}: {key} は実行時に作るファイルを指す"
                assert Path(value).name in KEY_FILES


def test_key_volume_is_internal(compose):
    assert "keys" in compose["volumes"]
    for name, svc in compose["services"].items():
        for vol in svc.get("volumes", []):
            if str(vol).startswith("keys:"):
                assert str(vol).split(":")[1] == "/booth-keys", name


# ---- keygen.py -------------------------------------------------------------


@pytest.fixture(scope="module")
def keygen():
    return load_module("booth_keygen", EXHIBIT / "setup" / "keygen.py")


def _read_vars(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"export ([A-Z0-9_]+)='([^']*)'", line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def test_keygen_creates_files_once_and_prints_nothing(keygen, tmp_path, capsys):
    keygen.ensure_keys(tmp_path)
    first = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert set(first) == KEY_FILES
    assert not any(FORBIDDEN_NAME.search(n) for n in first)

    vars_ = _read_vars(tmp_path / "nb_vars.sh")
    assert set(vars_) == {"SECRET_KEY", "API_TOKEN_PEPPER_1", "DB_PASSWORD"}
    assert len(vars_["SECRET_KEY"]) >= 50
    assert len(vars_["API_TOKEN_PEPPER_1"]) >= 50  # NetBox は 50 文字以上を求める
    assert (tmp_path / "db_pw").read_text().strip() == vars_["DB_PASSWORD"]
    assert len((tmp_path / "shumoku_admin_pw").read_text().strip()) >= 16

    keygen.ensure_keys(tmp_path)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == first, "2 回目は作り直さない"

    out = capsys.readouterr()
    for value in [*vars_.values(), (tmp_path / "shumoku_admin_pw").read_text().strip()]:
        assert value not in out.out + out.err, "値を表示しない"


def test_keygen_values_differ_between_runs(keygen, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    keygen.ensure_keys(a)
    keygen.ensure_keys(b)
    assert (a / "nb_vars.sh").read_text() != (b / "nb_vars.sh").read_text()


# ---- shumoku_setup.py（偽の API で） -----------------------------------------


class FakeShumoku:
    """shumoku 0.2.0 の API（/api/openapi.json で確かめた形）を、状態つきで真似る。"""

    def __init__(self):
        self.datasources: list[dict] = []
        self.topologies: list[dict] = []
        self.sources: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"id{self._n}"

    def request(self, method: str, path: str, body=None):
        self.calls.append((method, path))
        parts = path.strip("/").split("/")  # api, kind, id, ...
        if (method, path) == ("GET", "/api/datasources"):
            return list(self.datasources)
        if (method, path) == ("POST", "/api/datasources"):
            ds = {"id": self._id(), **body}
            self.datasources.append(ds)
            return ds
        if method == "PUT" and parts[1] == "datasources":
            ds = next(d for d in self.datasources if d["id"] == parts[2])
            ds.update(body)
            return ds
        if (method, path) == ("GET", "/api/topologies"):
            return list(self.topologies)
        if (method, path) == ("POST", "/api/topologies"):
            t = {"id": self._id(), "name": body["name"]}
            self.topologies.append(t)
            self.sources[t["id"]] = []
            return t
        if parts[1] == "topologies" and parts[3:] == ["sources"]:
            if method == "GET":
                return list(self.sources[parts[2]])
            s = {"id": self._id(), **body}
            self.sources[parts[2]].append(s)
            return s
        if parts[1] == "topologies" and parts[3:] == ["sync-from-source"]:
            return {"ok": True}
        if parts[1] == "topologies" and parts[3:] == ["sync-job"]:
            return {"status": "completed"}
        if parts[1] == "topologies" and parts[3:] == ["share"] and method == "POST":
            t = next(t for t in self.topologies if t["id"] == parts[2])
            t["shareToken"] = "share-" + t["id"]
            return {"shareToken": t["shareToken"]}
        raise AssertionError(f"想定外の呼び出し: {method} {path}")


@pytest.fixture(scope="module")
def shumoku_setup():
    return load_module("booth_shumoku_setup", EXHIBIT / "setup" / "shumoku_setup.py")


def test_configure_is_idempotent(shumoku_setup):
    api = FakeShumoku()
    first = shumoku_setup.configure(api, "http://netbox:8080", "nbt_a.b", poll_interval=0)
    assert len(api.datasources) == 1 and len(api.topologies) == 1
    tid = api.topologies[0]["id"]
    assert len(api.sources[tid]) == 1
    assert api.sources[tid][0]["dataSourceId"] == api.datasources[0]["id"]
    assert api.sources[tid][0]["purpose"] == "topology"
    assert first["share_path"] == f"/share/topologies/share-{tid}"

    second = shumoku_setup.configure(api, "http://netbox:8080", "nbt_c.d", poll_interval=0)
    assert len(api.datasources) == 1 and len(api.topologies) == 1 and len(api.sources[tid]) == 1
    assert second == first
    assert '"nbt_c.d"' in api.datasources[0]["configJson"], "restore で作り直した読み取り用の値に差し替える"
    assert ("POST", f"/api/topologies/{tid}/sync-from-source") in api.calls


# ---- restore.sh の dump の見分け --------------------------------------------


@pytest.mark.parametrize("content, expected", [
    (b"PGDMP\x01\x10\x00", "custom"),
    (b"\x1f\x8b\x08\x00rest", "sql.gz"),
    (b"--\n-- PostgreSQL database dump\n", "sql"),
])
def test_detect_dump_format(tmp_path, content, expected):
    dump = tmp_path / "d"
    dump.write_bytes(content)
    out = subprocess.run(["bash", "-c", f'source "{EXHIBIT}/lib.sh"; detect_dump_format "$1"', "_", str(dump)],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == expected


# ---- シェルスクリプト --------------------------------------------------------


SCRIPTS = ["up.sh", "down.sh", "restore.sh", "seed-empty.sh", "export.sh", "screen.sh", "lib.sh"]


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_are_strict_and_parse(name):
    path = EXHIBIT / name
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\s*set\s+-\w*x", text), "実行の追跡は値を表示しうる"
    if name != "lib.sh":
        assert "set -euo pipefail" in text
        assert os.access(path, os.X_OK), f"{name} に実行権限を付ける"
    subprocess.run(["bash", "-n", str(path)], check=True)


# ---- 家の中身を入れていない ------------------------------------------------


def exhibit_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", "exhibit"],
                             capture_output=True, check=True).stdout.decode()
        files = [ROOT / p for p in out.split("\0") if p]
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass
    # git の使えないコンテナでは、gitignore した置き場所（dumps/・exports/）を除いて辿る
    return [Path(d) / f for d, dirs, fs in os.walk(EXHIBIT)
            if not set(Path(d).relative_to(EXHIBIT).parts) & {"dumps", "exports"} for f in fs]


ALLOWED_V4 = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "0.0.0.0/32",
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",  # 文書用
    "192.168.2.0/24",  # 管理系。CLAUDE.md §3 で公開済み
)]
IPV4 = re.compile(r"(?<![0-9.])(\d{1,3}(?:\.\d{1,3}){3})(?:/\d{1,2})?(?![0-9.])")
MAC = re.compile(r"(?<![0-9A-Fa-f:])[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}(?![0-9A-Fa-f:])")
DUMP_NAME = re.compile(r"\.(sql|sql\.gz|dump|pgdump|backup|tar)$|^pg_dump", re.IGNORECASE)


def test_exhibit_has_no_home_contents():
    files = exhibit_files()
    assert files, "exhibit/ のファイルが見つからない"
    problems = []
    for path in files:
        rel = path.relative_to(ROOT)
        if DUMP_NAME.search(path.name):
            problems.append(f"{rel}: dump らしいファイル")
        if FORBIDDEN_NAME.search(path.name):
            problems.append(f"{rel}: 認証情報らしい名前")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for m in IPV4.finditer(text):
            try:
                ip = ipaddress.ip_address(m.group(1))
            except ValueError:
                continue
            if not any(ip in n for n in ALLOWED_V4):
                problems.append(f"{rel}: 実アドレスらしい IPv4")
        if MAC.search(text):
            problems.append(f"{rel}: MAC アドレス")
        problems += [f"{rel}: {p}" for p in find_sensitive(text)]
    assert not problems, "\n".join(problems)


def _ignored(rel: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", rel], capture_output=True)
        if r.returncode in (0, 1):
            return r.returncode == 0
    except OSError:
        pass
    # git の使えないコンテナでは exhibit/home-map/.gitignore を読む
    patterns = [ln.strip() for ln in (EXHIBIT / ".gitignore").read_text().splitlines()
                if ln.strip() and not ln.startswith("#")]
    sub = Path(rel).relative_to(EXHIBIT.relative_to(ROOT)).as_posix()
    return any(not p.startswith("!") and sub.startswith(p.rstrip("*").lstrip("/")) for p in patterns)


@pytest.mark.parametrize("rel", [
    "exhibit/home-map/dumps/netbox-2026-11-13.dump",
    "exhibit/home-map/dumps/netbox.sql.gz",
    "exhibit/home-map/exports/home-map.svg",
    "exhibit/home-map/exports/home-map.html",
])
def test_dumps_and_exports_are_gitignored(rel):
    assert _ignored(rel), f"{rel} を gitignore する"
