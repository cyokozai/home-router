#!/usr/bin/env bash
# shumoku の図を、静的なファイルに書き出す（ポスターの元と、図だけを外に見せるとき用）。
#   ./export.sh                 exports/home-map.svg と exports/home-map.html を作る
#   ./export.sh svg png ...     形式を選ぶ（svg・html・png）
#   EXPORT_DIR=/path ./export.sh  置き場所を変える
# 書き出したファイルには家の構成が入る。exports/ は gitignore 済み。リポジトリに入れない。
set -euo pipefail
source "$(dirname "$0")/lib.sh"

out=${EXPORT_DIR:-$HOME_MAP_DIR/exports}
mkdir -p "$out"
formats=("$@")
[[ ${#formats[@]} -eq 0 ]] && formats=(svg html)

for fmt in "${formats[@]}"; do
  case "$fmt" in svg | html | png) ;; *) echo "形式は svg・html・png: $fmt" >&2; exit 2 ;; esac
  dest="$out/home-map.$fmt"
  compose exec -T netbox "$NB_PY" /booth-setup/shumoku_setup.py export "$fmt" >"$dest.part"
  mv "$dest.part" "$dest"
  echo "書き出した: $dest（$(du -h "$dest" | cut -f1)）"
done
