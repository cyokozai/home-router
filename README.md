home-router
===========

NGN（フレッツ）網内に特化した10G自作ルータ（VyOS）の検証リポジトリ。

計画・機材・設定・試験の正典は [CLAUDE.md](CLAUDE.md) にある。

構成
----

| パス                | 中身                                                       |
|---------------------|------------------------------------------------------------|
| `CLAUDE.md`         | 引き継ぎ資料（目的、機材、ネットワーク設計、試験計画）     |
| `configs/<host>/`   | 各機の設定の退避先（VyOSは `show configuration commands`） |
| `results/<試験ID>/` | 試験結果（JSONとログ）                                     |
| `tests/`            | `CLAUDE.md` の数値・アドレス・設定の整合性検査             |

テスト
------

`CLAUDE.md` の計算値（ワイヤーレート、MTU/MSS、MAP-Eのポートセット）、アドレス設計、管理系の保護ルールを機械検査する。ローカルではコンテナで実行する。

```shell
docker run --rm -v "$PWD":/w -w /w python:3.13-slim \
  sh -c "pip install -q -r requirements-dev.txt && pytest -q"
```

CI
--

| ワークフロー                          | 内容                                                |
|---------------------------------------|-----------------------------------------------------|
| `.github/workflows/test.yaml`         | PRと `king's` へのpushで `pytest` を実行する        |
| `.github/workflows/markdown-fmt.yaml` | PRで `markdownfmt` を実行し、差分があれば自動commit |
