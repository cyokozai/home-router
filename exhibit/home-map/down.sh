#!/usr/bin/env bash
# ブースの NetBox と shumoku を止めて、コンテナを消す。データ（ボリューム）は残すので、up.sh でそのまま戻る。
#   ./down.sh          止める（データは残る。再起動しても上がらなくなる）
#   ./down.sh --purge  データも消す（家の台帳の複製・生成した値・図）。撤収の最後に使う
set -euo pipefail
source "$(dirname "$0")/lib.sh"

if [[ "${1:-}" == "--purge" ]]; then
  project=${COMPOSE_PROJECT_NAME:-booth-home-map}
  echo "プロジェクト ${project} のコンテナとボリューム（NetBox の DB・図・生成した値）を消す。元に戻せない。"
  read -r -p "消すなら ${project} と入力: " answer
  if [[ "$answer" != "$project" ]]; then
    echo "中止した"
    exit 1
  fi
  compose down --volumes
else
  compose down
fi
