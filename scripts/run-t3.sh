#!/usr/bin/env bash
# 試験 T3 の本番（CLAUDE.md §8）。64B で、送信能力の上限 → 1 フロー / 多数フロー × NDR / PDR を順に流す。
#   scripts/run-t3.sh [出力先（既定: results/T3/<日付>）] [フレーム長（既定 64）]
set -euo pipefail
out="${1:-results/T3/$(date +%Y%m%d)}" frame="${2:-64}"
mkdir -p "${out}"
scp -q "$(dirname "$0")/pktgen-run.sh" pc01:~/pktgen-run.sh
ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
  | sed -E "s/(encrypted-password|key) '[^']*'/\1 '<伏せ字>'/" > "${out}/pc02-config.cmds"

for flows in single many; do
  python3 -I "$(dirname "$0")/t3_search.py" --frame "${frame}" --flows "${flows}" --sender-max \
    --out "${out}/${frame}B-${flows}-sendermax"
  for tol in 0 0.001; do
    python3 -I "$(dirname "$0")/t3_search.py" --frame "${frame}" --flows "${flows}" --tolerance "${tol}" \
      --out "${out}/${frame}B-${flows}-tol${tol}"
  done
done
echo "保存先: ${out}"
