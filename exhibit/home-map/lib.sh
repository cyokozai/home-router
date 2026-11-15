# exhibit/home-map の各スクリプトが source する共通の関数。読み込むだけでは何もしない。
# 値（パスワード・API の値）は表示しない。set -x を付けない。

HOME_MAP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
NB_PY=/opt/netbox/venv/bin/python
NB_SHELL="$NB_PY /opt/netbox/netbox/manage.py shell --no-startup --no-imports --interface python"

compose() {
  docker compose -f "$HOME_MAP_DIR/compose.yaml" "$@"
}

netbox_url() { echo "http://127.0.0.1:${BOOTH_NETBOX_PORT:-8080}"; }
shumoku_url() { echo "http://127.0.0.1:${BOOTH_SHUMOKU_PORT:-8081}"; }

# dump の形式を先頭のバイトで見分ける: custom（pg_dump -Fc）・sql.gz・sql
detect_dump_format() {
  local magic
  magic=$(head -c 5 "$1" | od -An -tx1 | tr -d ' \n')
  case "$magic" in
    5047444d50) echo custom ;;  # "PGDMP"
    1f8b*) echo sql.gz ;;
    *) echo sql ;;
  esac
}

# 起動して、NetBox と shumoku が応答するまで待つ
start_all() {
  compose up -d --wait
}

# 読み取り用の値を作り直し、shumoku に NetBox を登録して図を取り込む（setup/connect.sh）
connect_shumoku() {
  compose exec -T netbox sh /booth-setup/connect.sh
}

# NetBox の主な件数を 1 行の JSON で出す（restore の前後の確認用）
netbox_counts() {
  compose exec -T netbox sh /booth-setup/with-vars.sh $NB_SHELL -c "
import json
from dcim.models import Site, Device, Interface, Cable
from ipam.models import Prefix, IPAddress, VLAN
print(json.dumps({m.__name__: m.objects.count() for m in (Site, Device, Interface, Cable, Prefix, IPAddress, VLAN)}))
" 2>/dev/null | tail -n 1
}
