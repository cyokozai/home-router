#!/usr/bin/env bash
# P3: 512B と IMIX（7:4:1）の転送レートの上限（CLAUDE.md §8 の目標表）。512B は T2 で送信側の iperf3 が先に
# 頭打ちになり、PC02 の上限が測れていない。pktgen で双方向に送る。多数フロー、NDR と PDR。PC02 は ①。
#   IMIX はカーネルの pktgen の imix_weights で送る（pkt_size 60,566,1514 を 7:4:1）。
#
#   scripts/run-p3-sizes.sh [出力先（既定: results/P3-sizes/<日付>）] [巡回数（既定 3）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/P3-sizes/$(date +%Y%m%d)}" rounds="${2:-3}"
v="${here}/../vyos"
mkdir -p "${out}"
for h in pc01 pc03; do scp -q "${here}/pktgen-run.sh" "${h}:~/pktgen-run.sh"; done

cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/(encrypted-password|key) '[^']*'/\1 '<伏せ字>'/" | sort > "$1"
}
to_base() {
  bash "${here}/pc02-commit.sh" --restore-saved >/dev/null
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
trap 'echo "== 基準に戻す"; to_base; cfg "${out}/pc02-config-end.cmds"' EXIT

to_base
bash "${here}/pc02-commit.sh" "${v}/tuning-1-offload.cmds" "${v}/test-sink.cmds" >/dev/null
cfg "${out}/pc02-config.cmds"
sleep 5
for r in $(seq 1 "${rounds}"); do
  for size in 512 imix; do
    for tol in 0 0.001; do
      if [[ ${size} == imix ]]; then opt=(--imix); else opt=(--frame "${size}"); fi
      python3 -I "${here}/t3_search.py" "${opt[@]}" --flows many --bidir --tolerance "${tol}" \
        --out "${out}/round${r}/${size}-many-bidir-tol${tol}" | grep -E "^結果" | sed "s/^/   round${r} ${size} tol=${tol}: /" || true
    done
  done
done
echo "保存先: ${out}"
