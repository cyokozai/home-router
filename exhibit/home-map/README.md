展示 D「家の監視画面」: ブースの PC03 で動かす複製
=================================================

自鯖缶（2026-11-15）の展示 D で、家の台帳（NetBox）と構成図（shumoku）を、ブースの PC03 だけで見せるための構成。会場の回線にも家にも頼らない。

-	11-13 の夜に家の NetBox から取った `pg_dump` を、ブースの PC03 で同じ版の NetBox に戻す（`restore.sh`）。
-	家の NetBox が間に合わないときは、空の NetBox に home-network の `load.py` を流して作る（`seed-empty.sh`）。
-	shumoku は、ブースの NetBox をデータ源にして図を作る。来場者には、ログインなしで図だけが出る共有の画面を全画面で見せる（`screen.sh`）。

このリポジトリは公開している。**家の構成の中身（dump・書き出した図・アドレスや機器の一覧）は入れない。** dump は `dumps/`、書き出した図は `exports/` に置く（どちらも gitignore 済み）。

---

構成
----

| サービス      | image（家の thin-k8s と同じ版。変えない）      | ポート（PC03 の 127.0.0.1） |
|---------------|------------------------------------------------|-----------------------------|
| netbox        | `docker.io/netboxcommunity/netbox:v4.7-5.1.1`  | **8080**                    |
| netbox-worker | 同上                                           | ―                           |
| postgres      | `docker.io/postgres:18-alpine`                 | ―                           |
| valkey        | `docker.io/valkey/valkey:9.1-alpine`           | ―                           |
| shumoku       | `ghcr.io/konoe-akitoshi/shumoku:0.2.0`         | **8081**                    |
| keygen        | netbox と同じ image（初回に一度だけ動いて終わる） | ―                         |

-	compose のプロジェクト名は `booth-home-map`。ボリュームは `booth-home-map_keys`・`_postgres-data`・`_valkey-data`・`_netbox-media`・`_shumoku-data`。
-	ポートは環境変数で切り替えられる: `BOOTH_NETBOX_PORT`（既定 8080）・`BOOTH_SHUMOKU_PORT`（既定 8081）・`BOOTH_BIND`（既定 127.0.0.1）。
-	`restart: unless-stopped` なので、PC03 の電源を入れると Docker が自動で上げる（`down.sh` で止めるまで）。
-	版を変えるときは、家の thin-k8s と `tests/test_exhibit_home_map.py` の `HOME_IMAGES` を同時に直す。

### 認証情報

-	NetBox の `SECRET_KEY`・API の pepper・DB のパスワード・shumoku の管理者パスワードは、初回の起動で keygen が作り、`keys` ボリュームの中にだけ置く。リポジトリにもホストのファイルにも書かない。スクリプトは値を表示しない。
-	shumoku が NetBox を読むための値は、読み取りだけの利用者 `booth-viewer` に対して、`up.sh`・`restore.sh` のたびに作り直す（家の dump に入っている値は、ブースの pepper では通らない）。
-	`seed-empty.sh` が `load.py` に渡す書き込みのできる値は、実行の間だけ作り、終わったら消す。
-	NetBox の画面にログインするとき: dump を戻した後は、家の NetBox の利用者とパスワードでそのまま入れる。空で作ったときは、次でパスワードを決める（入力は画面に出ない）。

```
docker compose -f compose.yaml exec netbox sh /booth-setup/with-vars.sh \
  /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py changepassword booth-admin
```

### スクリプト

| スクリプト                    | すること                                                                                         |
|-------------------------------|--------------------------------------------------------------------------------------------------|
| `./up.sh`                     | 起動し、shumoku に NetBox を登録して図を取り込む。何度流してもよい                               |
| `./restore.sh <dump>`         | DB を空にして dump を戻し、起動して図を取り込む。`pg_dump -Fc`・`.sql`・`.sql.gz` を受け付ける     |
| `./seed-empty.sh [load.py …]` | 予備の道。空で起動し、`load.py` を渡せば流して図を取り込む                                       |
| `./screen.sh`                 | 来場者向けの共有の画面をブラウザの全画面で開く（上がるまで待つ）。`--print` で URL だけを出す      |
| `./export.sh [svg html png]`  | 図を `exports/home-map.*` に書き出す（既定は svg と html）                                       |
| `./down.sh`                   | 止める（データは残る）。`--purge` でデータも消す                                                 |

---

11-13 の夜（家で）
------------------

前提: PC03 にこのリポジトリと Docker がある。家の LAN（インターネットに出られる）につながっている。

### 1. 家の NetBox から dump を取る

thin-k8s の PostgreSQL の Pod から取る。名前空間と Pod の名前は、家の構成に合わせて読み替える。

```
kubectl -n <netbox の名前空間> exec <postgres の Pod> -- \
  pg_dump -U netbox -d netbox -Fc --no-owner --no-acl > netbox-20261113.dump
```

-	`-Fc`（custom 形式）で取る。plain の SQL でもよいが、そのときも `--no-owner --no-acl` を付ける（家の DB の利用者名がブースと違うと、所有者の指定で止まる）。
-	DB の名前・利用者が `netbox` でなければ、`-U` と `-d` を読み替える。

### 2. PC03 へ運ぶ

```
scp netbox-20261113.dump pc03:~/home-router/exhibit/home-map/dumps/
```

dump には家の構成がすべて入っている。`dumps/` の外（共有のフォルダ・USB メモリなど）に置きっぱなしにしない。

### 3. PC03 で amd64 の image を取る

作業用の Mac にある image は arm64 なので、PC03 では使えない。PC03 で取り直す（家の回線で）。

```
cd ~/home-router/exhibit/home-map
docker compose -f compose.yaml pull
docker image inspect --format '{{.RepoTags}} {{.Architecture}}' \
  docker.io/netboxcommunity/netbox:v4.7-5.1.1 docker.io/postgres:18-alpine \
  docker.io/valkey/valkey:9.1-alpine ghcr.io/konoe-akitoshi/shumoku:0.2.0
```

4 つとも `amd64` と出ればよい。

### 4. 戻す

```
./restore.sh dumps/netbox-20261113.dump
```

最後に「戻した NetBox の件数」と「shumoku: 図 'home' を NetBox から取り込んだ（ノード N、リンク M）」が出る。件数を、家の NetBox の件数と見比べる。

### 5. 確かめる

```
./screen.sh                # 全画面で図が出る（閉じるのは Alt+F4 など）
./export.sh                # ポスター用に exports/ へ書き出す
```

-	NetBox の画面は `http://127.0.0.1:8080`、shumoku の管理画面は `http://127.0.0.1:8081`。
-	PNG の書き出しは、文字（機器の名前）が描かれないことがある（作業用の Mac で試したとき、ラベルが空だった）。ポスターには SVG か HTML を使う。

### 家の NetBox が間に合わないとき

```
./seed-empty.sh ~/home-network/<load.py のパス> [load.py への引数…]
```

-	`load.py` は NetBox の image の Python で動かす（`requests` と `PyYAML` はある。`pynetbox` は無い）。`load.py` のあるディレクトリを読み取りだけで `/load` に見せ、そこを作業ディレクトリにする。
-	接続先は環境変数 `NETBOX_URL`（`http://netbox:8080`）と `NETBOX_API_TOKEN` で渡す。

---

11-15 の立ち上げ（会場で）
--------------------------

1.	PC03 の電源を入れる。
2.	何もしなくても、Docker がコンテナを上げ、自動ログインから `screen.sh` が全画面で図を開く（下の「画面を自動で開く」を入れておいた場合）。NetBox の起動に 1〜2 分かかる。`screen.sh` はその間待つ。
3.	図が出なければ、PC03 で `./up.sh` を流してから `./screen.sh`。

会場の回線にはつながない。PC03 の中だけで動く。

---

撤収
----

```
./down.sh --purge          # コンテナとデータを消す（プロジェクト名 booth-home-map を入力して確定）
rm -f dumps/*.dump dumps/*.sql* exports/home-map.*
```

-	image は消さない（次に使うなら残す）。消すときは名前を指定して `docker rmi <image>`。
-	`docker system prune` のような範囲で消すコマンドは使わない（他のプロジェクトのものまで消える）。

---

画面を自動で開く（案。未実装・未確認）
--------------------------------------

PC03 は Ubuntu 24.04 Server で、画面の環境が無い。いちばん軽い案は、Wayland の全画面専用の `cage` でブラウザだけを動かす形。入れるのは家の回線があるうちに行う。

1.	入れる: `sudo apt install cage` と `sudo snap install firefox`（Chromium でもよい）。`cyokozai` を `docker` グループに入れる（`screen.sh` が `docker compose` を使うため）。
2.	tty1 で自動ログインする: `sudo systemctl edit getty@tty1` で次を書く。

```
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin cyokozai --noclear %I $TERM
```

3.	ログインしたら全画面で開く: `~/.bash_profile` に次を足す。

```
if [ "$(tty)" = /dev/tty1 ]; then
  BOOTH_BROWSER="firefox --kiosk" exec cage -- ~/home-router/exhibit/home-map/screen.sh
fi
```

-	代わりの案: `ubuntu-desktop-minimal` を入れ、GNOME の自動ログインと `~/.config/autostart/` の `.desktop` で `screen.sh` を起動する。重いが、画面の調整（解像度・回転）が GUI でできる。
-	画面が消えないように、省電力（DPMS）を切っておく。cage の下では、ブラウザが全画面で動いている限り画面は消えない見込み（未確認）。

---

図だけを外に見せる（任意。案）
------------------------------

グローバル IPv4 で図だけを見せたいときは、NetBox と shumoku を外に開かず、`export.sh` で書き出した静的なファイルだけを配る。

```
./export.sh svg html
docker run -d --name booth-map-static -p 0.0.0.0:8088:8000 -v "$PWD/exports:/srv:ro" \
  --entrypoint /opt/netbox/venv/bin/python docker.io/netboxcommunity/netbox:v4.7-5.1.1 \
  -m http.server -d /srv 8000
```

-	図には家の構成（機器の名前・配線）が入る。外に出す前に、書き出した図の中身を見て決める。
-	止めるときは `docker rm -f booth-map-static`。
