#!/usr/bin/env bash
# 試験 T4（チューニングの効果。CLAUDE.md §8）。①→②→③→④ を一つずつ積み上げ、各段階で
# T2 の 512B（iperf3 UDP）と T3（64B、1 フロー / 多数フロー × NDR / PDR）を測り直す。
# 終わったら積んだ設定を後ろから外して基準（フェーズ1）に戻す。基準の値は T2・T3 の結果を使う。
#
#   scripts/run-t4.sh [出力先（既定: results/T4/<日付>）]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-results/T4/$(date +%Y%m%d)}"
stages=(tuning-1-offload tuning-2-rps tuning-3-flowtable tuning-4-performance)
mkdir -p "${out}"
scp -q "${here}/pktgen-run.sh" pc01:~/pktgen-run.sh

cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/ (encrypted-password|key|community) ('[^']*'|[^ ']+)/ \1 '<伏せ字>'/g" > "$1"
}
cfg "${out}/pc02-config-baseline.cmds"

applied=()
cleanup() {
  # 途中で止まっても基準に戻す。試験中は save しないので、保存済みの設定がそのまま基準になる
  if ((${#applied[@]})); then
    echo "== 基準に戻す（${applied[*]}）"
    bash "${here}/pc02-commit.sh" --restore-saved || echo "!! 基準に戻せなかった。PC02 を確認する" >&2
    # VyOS は ring-buffer を設定から消しても NIC の値を戻さない。i40e の既定の 512 に戻す
    ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512; done' || true
    cfg "${out}/pc02-config-after-revert.cmds"
    diff <(sort "${out}/pc02-config-baseline.cmds") <(sort "${out}/pc02-config-after-revert.cmds") \
      && echo "== 基準と同じに戻った" || echo "!! 基準と違う。上の差分を確認する" >&2
  fi
}
trap cleanup EXIT

for i in "${!stages[@]}"; do
  s="${stages[$i]}"
  d="${out}/stage$((i + 1))-${s}"
  mkdir -p "${d}"
  echo "== 段階 $((i + 1)): ${s} を入れる"
  bash "${here}/pc02-commit.sh" "${here}/../vyos/${s}.cmds"
  applied+=("${s}")
  cfg "${d}/pc02-config.cmds"
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -g $i; sudo ethtool -k $i | grep -E "generic-receive|scatter|tcp-segment|generic-segment"; cat /sys/class/net/$i/queues/rx-0/rps_cpus; done; sudo nft list flowtables 2>/dev/null' \
    > "${d}/pc02-state.txt" 2>&1 || true
  sleep 5

  echo "-- T2 512B"
  SIZES=466 bash "${here}/run-t2.sh" "${d}/T2" | grep -E "^== |保存先" || true
  for flows in single many; do
    for tol in 0 0.001; do
      echo "-- T3 64B ${flows} tol=${tol}"
      python3 -I "${here}/t3_search.py" --frame 64 --flows "${flows}" --tolerance "${tol}" \
        --out "${d}/T3/64B-${flows}-tol${tol}" | grep -E "^結果|^保存先" || true
    done
  done
done
echo "保存先: ${out}"
