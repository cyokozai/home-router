#!/usr/bin/env bash
# cmds ファイルの set / delete の行の印（@名前@）を環境変数で置き換えて、標準出力に出す（scripts/placeholders.sh）。
# CI が、印に文書用の値を入れて VyOS のコンテナで commit できるかを確かめるときに使う。
# 実機の PC02 へ入れるときは scripts/pc02-commit.sh が同じ置き換えをする（こちらは使わない）。
#
#   ULA_PREFIX=fd00:db8:0:1 ... scripts/fill-placeholders.sh <cmds ファイル>... > 置き換えた cmds
#
# 不明な印・未設定・形の合わない値があれば、何も出さずに 1 で終わる。
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/placeholders.sh
source "${here}/placeholders.sh"

[[ $# -gt 0 ]] || { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 1; }
lines=$(cat "$@" | grep -E '^(set|delete) ' || true)
if ! fill_placeholders "${lines}"; then
  echo "fill-placeholders: 置き換えられない印がある（何も出していない）" >&2
  exit 1
fi
printf '%s\n' "${FILLED}"
