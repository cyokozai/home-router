home-router
===========

NGN（フレッツ）網内に特化した10G自作ルータ（VyOS）の検証リポジトリ。

計画・機材・設定・試験の正典は [CLAUDE.md](CLAUDE.md) にある。

構成
----

| パス                | 中身                                                                                                                                   |
|---------------------|----------------------------------------------------------------------------------------------------------------------------------------|
| `CLAUDE.md`         | 引き継ぎ資料（目的、機材、ネットワーク設計、試験計画）                                                                                 |
| `vyos/`             | CLAUDE.md §6 のVyOS設定をそのまま投入できる形にした `*.cmds`、CIで使うVyOSの版（`VERSION`）、コンテナで検証できない行の一覧            |
| `scripts/`          | VyOS rollingのISOからコンテナイメージを作るスクリプトと、VyOSコンテナへ設定を投入・検査するスクリプト（`vyos-apply.sh` など）          |
| `lab/`              | §3 の検証環境を containerlab で再現するトポロジーと、端末（pc01 / pc03）用の設定スクリプト。実機の管理系につながったマシンでは起動しない |
| `configs/<host>/`   | 各機の設定の退避先（VyOSは `show configuration commands`）                                                                             |
| `results/<試験ID>/` | 試験結果（JSONとログ）                                                                                                                 |
| `tests/`            | `CLAUDE.md` と `vyos/`・`lab/` の整合性検査（pytest）                                                                                  |
| `.github/`          | CIのワークフローと、VyOSイメージを用意する composite action、Issue / PR のテンプレート                                                  |

テスト
------

`tests/` は次の3つを機械検査する。ローカルではコンテナで実行する。

-	`test_handoff_doc.py`: `CLAUDE.md` の計算値（ワイヤーレート、MTU/MSS、MAP-Eのポートセット）、アドレス設計、管理系の保護ルール、VyOS rollingで通る書き方、資料が参照するリポジトリ内のパスの実在
-	`test_vyos_cmds.py`: `CLAUDE.md` §6 のコードブロックと `vyos/*.cmds` の一致、`vyos/container-unsupported.txt` と `vyos/VERSION` の形
-	`test_lab_topology.py`: `lab/` のトポロジーと端末用スクリプトが `CLAUDE.md` §3・§5-3・§6 と一致しているか（コンテナは起動しない）

```shell
docker run --rm -v "$PWD":/w -w /w python:3.13-slim \
  sh -c "pip install -q -r requirements-dev.txt && pytest -q"
```

VyOS rolling で実際に commit できるかは、CI（下記）がVyOSコンテナで検査する。

CI
--

ワークフローは `.github/workflows/` にある。各ファイルの冒頭のコメントに、何を検査するかを書いている。
