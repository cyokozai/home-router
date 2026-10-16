#!/usr/bin/env bash
# cmds ファイル 1 本を「基準の設定」に足して commit できるかを、VyOS コンテナで検査する。
#
#   scripts/vyos-check-cmds.sh <コンテナ名> <コンテナ内の基準設定> <cmds ファイル> <コンテナで検証不能な行の一覧>
#
# 1. 一覧（vyos/container-unsupported.txt）に載った行を除いた残りを、基準に足して commit する。通らなければ失敗。
#    失敗したときは 1 行ずつ足し直して、どの行が落ちるかを「一覧の書式」で出す（一覧の更新に使える）。
# 2. 一覧に載った行を 1 本ずつ足して、commit まで通って「しまわない」ことを確かめる。通ったら失敗。
# 3. 最後に 1 の状態（基準 + 検証できる行）に戻して終わる。
#
# 終了コード: 0 = 一覧と実際が一致、1 = 不一致または投入できない
set -euo pipefail

[[ $# -eq 4 ]] || { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 1; }
container="$1" base="$2" cmds="$3" unsupported="$4"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
apply="${here}/vyos-apply.sh"
name="$(basename "${cmds}")"

work="$(mktemp -d "${TMPDIR:-/tmp}/vyos-check.XXXXXX")"
trap 'rm -rf "${work}"' EXIT

strip() { sed -e 's/#.*$//' -e 's/[[:space:]]*$//' -e 's/^[[:space:]]*//' "$1" | grep -v '^$' || true; }

strip "${cmds}" > "${work}/all"
# 一覧のうち、このファイルの行だけ（「ファイル | コマンド | 理由」）
awk -F' [|] ' -v f="${name}" '!/^[[:space:]]*#/ && NF == 3 && $1 == f { print $2 }' "${unsupported}" \
  > "${work}/listed"
grep -vxF -f "${work}/listed" "${work}/all" > "${work}/checked" || true

missing="$(grep -vxF -f "${work}/all" "${work}/listed" || true)"
if [[ -n "${missing}" ]]; then
  echo "NG ${name}: 一覧に載っているがファイルに無い行:" >&2
  printf '  %s\n' "${missing}" >&2
  exit 1
fi

echo "== ${name}: 基準 + 検証できる $(wc -l < "${work}/checked" | tr -d ' ') 行を commit する（一覧で除外 $(wc -l < "${work}/listed" | tr -d ' ') 行）"
if ! "${apply}" --load "${base}" "${container}" "${work}/checked"; then
  echo "NG ${name}: 一覧に無い行で commit が通らない。1 行ずつ足して、落ちる行を探す" >&2
  : > "${work}/accepted"
  while IFS= read -r line; do
    cp "${work}/accepted" "${work}/trial"
    echo "${line}" >> "${work}/trial"
    if "${apply}" --load "${base}" "${container}" "${work}/trial" > "${work}/log" 2>&1; then
      echo "${line}" >> "${work}/accepted"
    else
      echo "  落ちる行（一覧の書式）: ${name} | ${line} | <理由>" >&2
      sed 's/^/      /' "${work}/log" >&2
    fi
  done < "${work}/checked"
  exit 1
fi

status=0
if [[ -s "${work}/listed" ]]; then
  while IFS= read -r line; do
    cp "${work}/checked" "${work}/trial"
    echo "${line}" >> "${work}/trial"
    echo "-- ${name}: 一覧の行が落ちることを確かめる: ${line}"
    if "${apply}" --load "${base}" "${container}" "${work}/trial"; then
      echo "NG ${name}: 一覧に載っているのに commit が通った（一覧から外す）: ${line}" >&2
      status=1
    else
      echo "OK ${name}: 想定どおりコンテナでは通らない: ${line}"
    fi
  done < "${work}/listed"
  # 基準 + 検証できる行の状態に戻す（後続の確認がこの状態を前提にする）
  "${apply}" --load "${base}" "${container}" "${work}/checked" || status=1
fi

if [[ "${status}" -eq 0 ]]; then
  echo "OK ${name}"
fi
exit "${status}"
