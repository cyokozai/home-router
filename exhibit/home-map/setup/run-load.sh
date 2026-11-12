#!/bin/sh
# seed-empty.sh が docker compose run で NetBox の image の中から呼ぶ。
# 書き込みのできる値を作り、NETBOX_URL・NETBOX_API_TOKEN として home-network の load.py に渡して実行し、
# 終わったら（失敗しても）その値を NetBox から消す。値は表示しない。
#   使い方: run-load.sh <load.py のファイル名> [load.py への引数...]（load.py のあるディレクトリが /load）
set -eu
. /booth-keys/nb_vars.sh
PY=/opt/netbox/venv/bin/python
MANAGE="/opt/netbox/netbox/manage.py shell --no-startup --no-imports --interface python"
script=$1
shift
umask 077
f=$(mktemp)
cleanup() {
  BOOTH_ROLE=revoke-writer "$PY" $MANAGE </booth-setup/nb_access.py || true
  rm -f "$f"
}
trap cleanup EXIT INT TERM
BOOTH_ROLE=writer BOOTH_OUT="$f" "$PY" $MANAGE </booth-setup/nb_access.py
cd /load
NETBOX_URL=http://netbox:8080 NETBOX_API_TOKEN=$(cat "$f") "$PY" "/load/$script" "$@"
