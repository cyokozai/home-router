#!/usr/bin/env bash
# 動いている VyOS コンテナに cmds ファイルの set/delete を投入して commit する。
#
#   scripts/vyos-apply.sh [--load <コンテナ内の設定ファイル>] [--save <コンテナ内の保存先>] <コンテナ名> <cmds ファイル | ->
#
#   --load  投入の前に、その設定ファイルの状態へ戻す（§8 の「baseline に戻して差分だけ入れる」）
#   --save  commit が通ったあと、稼働中の設定をそのパスへ保存する
#   cmds ファイルの # 以降と空行は無視する。- なら標準入力から読む。
#
# 終了コード
#   0  commit が通った（変更が無ければ commit せずに 0）
#   2  set / delete の段階で拒否された（commit はしていない。稼働中の設定は変わらない）
#   3  commit が失敗した
#   4  --load または --save が失敗した
#   1  使い方の誤り・コンテナに入れないなど
#
# 中では vbash + script-template を vyattacfg グループで動かす
# （https://docs.vyos.io/en/rolling/automation/command-scripting.html）。
set -euo pipefail

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 1; }

load_file="" save_file=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --load) load_file="${2:?--load にパスが要る}"; shift 2 ;;
    --save) save_file="${2:?--save にパスが要る}"; shift 2 ;;
    -h|--help) usage ;;
    --) shift; break ;;
    -*) echo "vyos-apply: 不明なオプション: $1" >&2; usage ;;
    *) break ;;
  esac
done
[[ $# -eq 2 ]] || usage
container="$1" cmds="$2"

if [[ "${cmds}" == "-" ]]; then
  source_text="$(cat)"
else
  [[ -f "${cmds}" ]] || { echo "vyos-apply: ファイルが無い: ${cmds}" >&2; exit 1; }
  source_text="$(cat "${cmds}")"
fi

# 投入する行（コメントと空行を除く）
lines=()
while IFS= read -r raw; do
  line="${raw%%#*}"
  # クォートの中に # を含む行は無い前提（cmds ファイルの検査で担保）
  line="$(echo "${line}" | sed -e 's/[[:space:]]*$//' -e 's/^[[:space:]]*//')"
  [[ -z "${line}" ]] && continue
  if [[ ! "${line}" =~ ^(set|delete)\  ]]; then
    echo "vyos-apply: set / delete 以外の行は投入しない: ${line}" >&2
    exit 1
  fi
  lines+=("${line}")
done <<< "${source_text}"

quote() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

# 生成する vbash の中で展開させる $ は、こちらでは展開しない
# shellcheck disable=SC2016
script="$(
  echo '#!/bin/vbash'
  echo 'source /opt/vyatta/etc/functions/script-template'
  echo 'configure'
  echo 'rc=0'
  if [[ -n "${load_file}" ]]; then
    echo "load $(quote "${load_file}") || rc=4"
  fi
  # set / delete / commit は失敗しても 0 で返る（2026-10 の rolling で確認）。
  # 終了コードには頼らず、結果を設定セッションに問い合わせて判定する
  for i in "${!lines[@]}"; do
    line="${lines[$i]}" path="${lines[$i]#* }"
    if [[ "${line}" == set\ * ]]; then check="\$API exists ${path}"; else check="! \$API exists ${path}"; fi
    echo "if [ \$rc -eq 0 ]; then ${line}; if ! { ${check}; }; then rc=2; echo \"VYOS_APPLY_REJECTED $((i + 1))\"; fi; fi"
  done
  echo 'if [ $rc -eq 0 ]; then'
  echo '  if $API sessionChanged; then'
  echo '    commit'
  # commit が通ればセッションは稼働中の設定と一致する。一致しないままなら失敗
  echo '    if $API sessionChanged; then rc=3; fi'
  echo '  else echo "VYOS_APPLY_NO_CHANGES"; fi'
  echo 'fi'
  if [[ -n "${save_file}" ]]; then
    echo "if [ \$rc -eq 0 ]; then save $(quote "${save_file}") || rc=4; fi"
  fi
  # exit は script-template で設定セッションの後始末（未 commit の変更は捨てる）に置き換わっている
  echo 'exit'
  echo 'builtin exit $rc'
)"

remote="/tmp/vyos-apply.$$.$RANDOM.sh"
printf '%s\n' "${script}" | docker exec -i "${container}" sh -c "cat > ${remote}" \
  || { echo "vyos-apply: コンテナ ${container} に書き込めない" >&2; exit 1; }

set +e
output="$(docker exec "${container}" sg vyattacfg -c "/bin/vbash ${remote}" 2>&1)"
rc=$?
set -e
docker exec "${container}" rm -f "${remote}" >/dev/null 2>&1 || true

printf '%s\n' "${output}" | grep -v '^VYOS_APPLY_' || true
while read -r _ n; do
  echo "vyos-apply: 拒否された行: ${lines[$((n - 1))]}" >&2
done < <(printf '%s\n' "${output}" | grep '^VYOS_APPLY_REJECTED ' || true)

# 念のため出力も見る（commit の一部だけが失敗してもセッションが一致してしまう場合に備える）
if [[ "${rc}" -eq 0 ]] && grep -qE '^(Commit failed|Set failed|Delete failed)' <<< "${output}"; then
  rc=3
fi

case "${rc}" in
  0) if grep -q '^VYOS_APPLY_NO_CHANGES' <<< "${output}"; then
       echo "vyos-apply: 変更なし（${#lines[@]} 行）" >&2
     else
       echo "vyos-apply: commit 成功（${#lines[@]} 行）" >&2
     fi ;;
  2) echo "vyos-apply: set / delete が拒否された" >&2 ;;
  3) echo "vyos-apply: commit に失敗した" >&2 ;;
  4) echo "vyos-apply: load / save に失敗した" >&2 ;;
  *) echo "vyos-apply: vbash が想定外の終了コード ${rc} で終わった" >&2; rc=1 ;;
esac
exit "${rc}"
