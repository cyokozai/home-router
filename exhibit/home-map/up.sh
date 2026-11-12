#!/usr/bin/env bash
# ブースの NetBox と shumoku を起動し、shumoku に NetBox を登録して図を取り込む。何度流してもよい。
# 起動のあとは restart: unless-stopped なので、PC03 を再起動しても自動で上がる（down.sh で止めるまで）。
set -euo pipefail
source "$(dirname "$0")/lib.sh"

echo "== 起動（初回は NetBox の移行に数分かかる）"
start_all
echo "== shumoku に NetBox を登録して取り込む"
connect_shumoku
echo "== NetBox の件数: $(netbox_counts)"
cat <<EOF
== できた
  NetBox : $(netbox_url)
  shumoku: $(shumoku_url)（管理画面。来場者向けの全画面は ./screen.sh で開く）
EOF
