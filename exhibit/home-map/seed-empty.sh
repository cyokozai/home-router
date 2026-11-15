#!/usr/bin/env bash
# 予備の道: 家の NetBox の dump が無いとき、ブースの NetBox を空で起動し、home-network の load.py で台帳を作る。
#   ./seed-empty.sh                               空で起動し、管理者 booth-admin を作るところまで
#   ./seed-empty.sh <load.py のパス> [引数...]    続けて load.py を流し、shumoku に図を取り込む
# load.py は NetBox の image の Python（requests・PyYAML あり、pynetbox なし）で動かす。load.py のある
# ディレクトリを読み取りだけで /load に見せ、そこを作業ディレクトリにする。
# load.py には NETBOX_URL と NETBOX_API_TOKEN（書き込みのできる値）を渡す。値は実行の間だけ作り、
# 終わったら NetBox から消す。表示しない。
set -euo pipefail
source "$(dirname "$0")/lib.sh"

echo "== 起動（初回は NetBox の移行に数分かかる）"
start_all
echo "== NetBox の件数（流す前）: $(netbox_counts)"

if [[ $# -eq 0 ]]; then
  # 管理者だけを作っておく（値は作らない。load.py を流すときに作って消す）
  compose exec -T netbox sh /booth-setup/with-vars.sh $NB_SHELL -c "
from users.models import User
u, created = User.objects.get_or_create(username='booth-admin')
if created:
    u.set_unusable_password()
u.is_superuser = u.is_staff = u.is_active = True
u.save()
print('booth-admin を用意した' if created else 'booth-admin は作成済み')
"
  connect_shumoku
  cat <<EOF
== 空で起動した。続けて台帳を作るには:
  $0 <home-network の load.py のパス> [load.py への引数...]
EOF
  exit 0
fi

load=$1
shift
if [[ ! -f "$load" ]]; then
  echo "load.py が見つからない: $load" >&2
  exit 2
fi
load_dir=$(cd "$(dirname "$load")" && pwd)
load_name=$(basename "$load")

echo "== load.py を流す: $load_dir/$load_name"
compose run --rm --no-deps -T -v "$load_dir:/load:ro" --entrypoint /bin/sh netbox \
  /booth-setup/run-load.sh "$load_name" "$@"
echo "== NetBox の件数（流した後）: $(netbox_counts)"
echo "== shumoku に NetBox を登録して取り込む"
connect_shumoku
echo "== できた。来場者向けの画面は ./screen.sh で開く"
