#!/bin/sh
# NetBox のコンテナの中で動かす（up.sh・restore.sh・seed-empty.sh が docker compose exec で呼ぶ）。
# 読み取り用の利用者 booth-viewer の API の値を作り直し、shumoku のデータ源に入れて、図を取り込む。
# 値はコンテナの中の一時ファイルにだけ置き、終わったら消す。表示しない。
set -eu
. /booth-keys/nb_vars.sh
PY=/opt/netbox/venv/bin/python
umask 077
f=$(mktemp)
trap 'rm -f "$f"' EXIT INT TERM
BOOTH_ROLE=viewer BOOTH_OUT="$f" "$PY" /opt/netbox/netbox/manage.py shell --no-startup --no-imports \
  --interface python </booth-setup/nb_access.py
"$PY" /booth-setup/shumoku_setup.py configure --viewer-file "$f"
