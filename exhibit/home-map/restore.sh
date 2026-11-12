#!/usr/bin/env bash
# 家の NetBox から取った pg_dump を、ブースの NetBox に戻す。DB を空にしてから戻し、NetBox を起動し、
# 読み取り用の値を作り直して shumoku に図を取り込む。
#   ./restore.sh dumps/netbox-YYYYMMDD.dump   （pg_dump -Fc。plain の .sql と .sql.gz も受け付ける）
# dump は dumps/ に置く（gitignore 済み）。家の値（利用者・API の値）は戻るが、ブースの pepper では通らない。
set -euo pipefail
source "$(dirname "$0")/lib.sh"

dump=${1:-}
if [[ -z "$dump" || ! -f "$dump" ]]; then
  echo "使い方: $0 <dump のファイル>" >&2
  exit 2
fi
format=$(detect_dump_format "$dump")
echo "== dump: $dump（形式: $format、$(du -h "$dump" | cut -f1)）"

echo "== DB と Valkey を起動"
compose up -d --wait postgres valkey
echo "== NetBox を止める"
compose stop netbox netbox-worker >/dev/null 2>&1 || true

echo "== DB を空にする"
compose exec -T postgres dropdb -U netbox --if-exists --force netbox
compose exec -T postgres createdb -U netbox -O netbox netbox

echo "== dump を戻す"
case "$format" in
  custom)
    compose exec -T postgres pg_restore -U netbox -d netbox --no-owner --no-acl --exit-on-error <"$dump"
    ;;
  sql.gz)
    gzip -dc "$dump" | compose exec -T postgres psql -q -U netbox -d netbox -v ON_ERROR_STOP=1 >/dev/null
    ;;
  sql)
    compose exec -T postgres psql -q -U netbox -d netbox -v ON_ERROR_STOP=1 <"$dump" >/dev/null
    ;;
esac

echo "== NetBox のキャッシュを消す"
compose exec -T valkey valkey-cli FLUSHALL >/dev/null

echo "== NetBox と shumoku を起動（dump の版が古ければ、ここで移行が走る）"
start_all
echo "== shumoku に NetBox を登録して取り込む"
connect_shumoku
echo "== 戻した NetBox の件数: $(netbox_counts)"
echo "== できた。来場者向けの画面は ./screen.sh で開く"
