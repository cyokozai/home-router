#!/usr/bin/env bash
# 実機の PC02 に cmds ファイルの set / delete を入れる（作業用 Mac から ssh で）。CLAUDE.md §1 ルール 2 の手順どおり:
#   commit-confirm 5（次の行に y）→ 別の接続で鍵の SSH を確かめる → confirm。save はしない（--save を付けたときだけ）。
# save しないので、再起動すれば /config/config.boot（baseline）に戻る。
#
#   scripts/pc02-commit.sh [--save] <cmds ファイル>...
#   scripts/pc02-commit.sh --restore-saved      保存済みの設定（/config/config.boot）を読み直して戻す
#
# 段階を戻すときに「入れた set 行を delete に変える」形は使わない。基準にもとから同じ行がある
# （例: offload gro/gso/sg/tso はインストール時の既定で入っている）と、基準の行まで消してしまう
# （2026-10-25 に実際に起きた）。試験中は save しないので、保存済みの設定がそのまま基準になる。
set -euo pipefail
save=0 restore=0
while [[ $# -gt 0 && $1 == --* ]]; do
  case $1 in --save) save=1 ;; --restore-saved) restore=1 ;; *) echo "不明な引数: $1" >&2; exit 1 ;; esac
  shift
done
if ((restore)); then
  lines="load /config/config.boot"
else
  [[ $# -gt 0 ]] || { echo "cmds ファイルを指定する" >&2; exit 1; }
  lines=$(cat "$@" | grep -E '^(set|delete) ')
fi

script=$(printf 'source /opt/vyatta/etc/functions/script-template\nconfigure\n%s\ncompare\ncommit-confirm 5\ny\nexit\n' "${lines}")
out=$(printf '%s\n' "${script}" | ssh pc02 'timeout 120 vbash -s' 2>&1) || true
printf '%s\n' "${out}" | tail -5
if ! grep -q "Initialized commit-confirm" <<<"${out}"; then
  echo "pc02-commit: commit-confirm が始まらなかった（設定は入っていない）" >&2
  exit 3
fi

# 管理系の SSH が生きていることを、別の接続で確かめてから confirm する
for _ in 1 2 3 4 5 6; do
  ssh -o BatchMode=yes -o ConnectTimeout=5 pc02 true 2>/dev/null && break
  sleep 5
done
confirm=$'source /opt/vyatta/etc/functions/script-template\nconfigure\nconfirm\n'
((save)) && confirm+=$'save\n'
confirm+=$'exit\n'
printf '%s' "${confirm}" | ssh pc02 'timeout 60 vbash -s' 2>&1 | tail -2
echo "pc02-commit: 確定した（save=${save}）"
