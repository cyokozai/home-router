#!/usr/bin/env bash
# 双方向の T3（① の状態の PC02 の上限）。PC01 → PC03 と PC03 → 捨て先（192.168.10.99）を同時に流す。
#   構成: ①（リング 4096）＋ 試験用の捨て先（vyos/test-sink.cmds）
#   scripts/run-bidir.sh [出力先（既定: results/T3-bidir/<日付>）] [巡回数（既定 3）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/T3-bidir/$(date +%Y%m%d)}" rounds="${2:-3}"
v="${here}/../vyos"
mkdir -p "${out}"
for h in pc01 pc03; do scp -q "${here}/pktgen-run.sh" "${h}:~/pktgen-run.sh"; done
to_base() {
  bash "${here}/pc02-commit.sh" --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
# 鍵・パスワードと SNMP の community（vyos/snmp-mgmt.cmds、秘密）を、引用符の有無によらず伏せてから残す
cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/ (encrypted-password|key|community) ('[^']*'|[^ ']+)/ \1 '<伏せ字>'/g" | sort > "$1"
}
trap 'echo "== 基準に戻す"; to_base; cfg "${out}/pc02-config-end.cmds"' EXIT
to_base
bash "${here}/pc02-commit.sh" "${v}/tuning-1-offload.cmds" "${v}/test-sink.cmds" >/dev/null
python3 -I "${here}/t3_search.py" --frame 64 --flows many --bidir --sender-max --out "${out}/64B-many-bidir-sendermax" | grep -E "^  試行|^保存先" || true
for r in $(seq 1 "${rounds}"); do
  for flows in single many; do
    for tol in 0 0.001; do
      python3 -I "${here}/t3_search.py" --frame 64 --flows "${flows}" --bidir --tolerance "${tol}" \
        --out "${out}/round${r}/64B-${flows}-bidir-tol${tol}" | grep -E "^結果" | sed "s/^/   round${r} ${flows} tol=${tol}: /" || true
    done
  done
done
echo "保存先: ${out}"
