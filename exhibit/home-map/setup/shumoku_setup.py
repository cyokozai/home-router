"""ブースの shumoku（0.2.0）に、ブースの NetBox をデータ源として登録し、図を作る・書き出す。

NetBox のコンテナの中で動かす（標準ライブラリだけを使う。新しい image を足さないため）。
shumoku の管理者パスワードは /booth-keys/shumoku_admin_pw から読み、表示しない。

  configure --viewer-file <path>  データ源・図（topology）・取り込み・共有のリンクを作り、NetBox から取り込む。
                                  2 回流しても重複を作らない。読み取り用の値は毎回差し替える（restore で作り直すため）
  view-path                       来場者に見せる共有の画面のパス（/share/topologies/…）を出す。screen.sh が受け取る
  export <svg|html|png>           図を標準出力へ書き出す
  status                          件数（ノード・リンク）だけを出す

API の形は shumoku 0.2.0 の /openapi.json で確かめた。
"""

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SHUMOKU_URL = os.environ.get("SHUMOKU_URL", "http://shumoku:8080")
NETBOX_INTERNAL_URL = os.environ.get("NETBOX_INTERNAL_URL", "http://netbox:8080")
ADMIN_PW_FILE = Path(os.environ.get("SHUMOKU_ADMIN_PW_FILE", "/booth-keys/shumoku_admin_pw"))
SOURCE_NAME = "booth-netbox"
TOPOLOGY_NAME = os.environ.get("BOOTH_TOPOLOGY_NAME", "home")
SYNC_TIMEOUT = 180


class Api:
    """shumoku の API。ログインのクッキーを持つ。"""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def raw(self, method: str, path: str, body=None) -> tuple[int, bytes]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=60) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def request(self, method: str, path: str, body=None):
        status, data = self.raw(method, path, body)
        if status >= 400:
            # 応答の本文には設定の値が入りうるので、先頭だけを出す
            raise RuntimeError(f"shumoku {method} {path}: HTTP {status}: {data[:200]!r}")
        return json.loads(data) if data else None

    def wait_healthy(self, timeout: int = 120) -> None:
        end = time.time() + timeout
        while True:
            try:
                status, _ = self.raw("GET", "/api/health")
                if status == 200:
                    return
            except OSError:
                pass
            if time.time() > end:
                raise RuntimeError("shumoku が応答しない")
            time.sleep(2)

    def login(self, password: str) -> None:
        self.request("POST", "/api/auth/login", {"password": password})


def _items(value) -> list[dict]:
    if isinstance(value, list):
        return value
    for key in ("items", "data", "results"):
        if isinstance(value, dict) and isinstance(value.get(key), list):
            return value[key]
    return []


def _by_name(items: list[dict], name: str) -> dict | None:
    return next((i for i in items if i.get("name") == name), None)


def wait_sync(api, topology_id: str, timeout: int = SYNC_TIMEOUT, poll_interval: float = 2,
              since_ms: float = 0) -> dict:
    """取り込み（非同期）が終わるまで待つ。0.2.0 の応答は {"job": {"state": "running" | "done" | …, "steps": […]}}。"""
    end = time.time() + timeout
    while True:
        res = api.request("GET", f"/api/topologies/{topology_id}/sync-job") or {}
        job = res.get("job") or {}
        state = str(job.get("state") or "").lower()
        if state in ("failed", "error", "cancelled", "canceled"):
            failed = [s.get("label") for s in job.get("steps", []) if s.get("status") not in ("done", None)]
            raise RuntimeError(f"NetBox からの取り込みに失敗（{state}、{failed}）")
        # 前回の取り込みの done を拾わない（コンテナ同士は同じ時計。ずれの余裕を 5 秒みる）
        if state == "done" and job.get("startedAt", since_ms) >= since_ms - 5000:
            return job
        if time.time() > end:
            raise RuntimeError("NetBox からの取り込みが終わらない")
        time.sleep(poll_interval)


def configure(api, netbox_url: str, viewer_value: str, *, topology_name: str = TOPOLOGY_NAME,
              poll_interval: float = 2) -> dict:
    """データ源・図・取り込み・共有を揃え、取り込みを流す。何度流しても同じ状態になる。"""
    config_json = json.dumps({"url": netbox_url, "token": viewer_value})

    ds = _by_name(_items(api.request("GET", "/api/datasources")), SOURCE_NAME)
    if ds is None:
        ds = api.request("POST", "/api/datasources",
                         {"name": SOURCE_NAME, "type": "netbox", "configJson": config_json})
    else:
        api.request("PUT", f"/api/datasources/{ds['id']}",
                    {"name": SOURCE_NAME, "type": "netbox", "configJson": config_json})

    topo = _by_name(_items(api.request("GET", "/api/topologies")), topology_name)
    if topo is None:
        topo = api.request("POST", "/api/topologies", {"name": topology_name})
    tid = topo["id"]

    attached = _items(api.request("GET", f"/api/topologies/{tid}/sources"))
    if not any(s.get("dataSourceId") == ds["id"] for s in attached):
        api.request("POST", f"/api/topologies/{tid}/sources",
                    {"dataSourceId": ds["id"], "purpose": "topology", "syncMode": "manual"})

    since_ms = time.time() * 1000
    api.request("POST", f"/api/topologies/{tid}/sync-from-source")
    wait_sync(api, tid, poll_interval=poll_interval, since_ms=since_ms)

    share = topo.get("shareToken")
    if not share:
        share = api.request("POST", f"/api/topologies/{tid}/share")["shareToken"]
    return {"topology_id": tid, "share_path": f"/share/topologies/{share}"}


def _topology(api, topology_name: str = TOPOLOGY_NAME) -> dict:
    topo = _by_name(_items(api.request("GET", "/api/topologies")), topology_name)
    if topo is None:
        raise RuntimeError(f"図 {topology_name!r} がまだ無い。先に up.sh か restore.sh を流す")
    return topo


def _connect() -> Api:
    api = Api(SHUMOKU_URL)
    api.wait_healthy()
    api.login(ADMIN_PW_FILE.read_text(encoding="utf-8").strip())
    return api


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("configure")
    p.add_argument("--viewer-file", required=True, type=Path)
    sub.add_parser("view-path")
    sub.add_parser("status")
    p = sub.add_parser("export")
    p.add_argument("format", choices=["svg", "html", "png"])
    args = parser.parse_args(argv)

    api = _connect()
    if args.cmd == "configure":
        viewer = args.viewer_file.read_text(encoding="utf-8").strip()
        if not viewer:
            raise RuntimeError("NetBox の読み取り用の値が空")
        result = configure(api, NETBOX_INTERNAL_URL, viewer)
        render = api.request("GET", f"/api/topologies/{result['topology_id']}/render")
        print(f"shumoku: 図 {TOPOLOGY_NAME!r} を NetBox から取り込んだ"
              f"（ノード {render.get('nodeCount')}、リンク {render.get('edgeCount')}）", file=sys.stderr)
    elif args.cmd == "view-path":
        topo = _topology(api)
        if not topo.get("shareToken"):
            raise RuntimeError("共有のリンクがまだ無い。先に up.sh か restore.sh を流す")
        print(f"/share/topologies/{topo['shareToken']}")
    elif args.cmd == "status":
        render = api.request("GET", f"/api/topologies/{_topology(api)['id']}/render")
        print(json.dumps({"nodeCount": render.get("nodeCount"), "edgeCount": render.get("edgeCount")}))
    elif args.cmd == "export":
        status, data = api.raw("GET", f"/api/topologies/{_topology(api)['id']}/export?format={args.format}")
        if status != 200:
            raise RuntimeError(f"書き出しに失敗: HTTP {status}")
        sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"shumoku_setup: {e}", file=sys.stderr)
        sys.exit(1)
