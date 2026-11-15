#!/bin/sh
# NetBox のコンテナの入口。keys ボリュームの値を環境変数に読み込んでから、渡されたコマンドを実行する。
# 値は表示しない（set -x を付けない）。
set -eu
. /booth-keys/nb_vars.sh
exec "$@"
