#!/usr/bin/env bash
# 来場者に見せる画面（shumoku の共有の画面。ログイン不要で図だけが出る）をブラウザの全画面で開く。
# NetBox と shumoku が上がるまで待つので、PC03 の自動ログインから呼んでよい（README の「画面を自動で開く」）。
#   ./screen.sh            ブラウザを開く（BOOTH_BROWSER で変えられる。既定は firefox --kiosk）
#   ./screen.sh --print    開く URL を表示するだけ（共有のリンクが入るので、人に見せる場では使わない）
set -euo pipefail
source "$(dirname "$0")/lib.sh"

path=""
for _ in $(seq 1 90); do
  if path=$(compose exec -T netbox "$NB_PY" /booth-setup/shumoku_setup.py view-path 2>/dev/null) && [[ -n "$path" ]]; then
    break
  fi
  sleep 10
done
if [[ -z "$path" ]]; then
  echo "共有の画面のパスが取れない。./up.sh か ./restore.sh を先に流す" >&2
  exit 1
fi
url="$(shumoku_url)$path"

if [[ "${1:-}" == "--print" ]]; then
  echo "$url"
  exit 0
fi
read -r -a browser <<<"${BOOTH_BROWSER:-firefox --kiosk}"
exec "${browser[@]}" "$url"
