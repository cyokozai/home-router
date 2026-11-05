# shellcheck shell=bash
# cmds の印（@名前@）を、同じ名前の環境変数の値で置き換える。scripts/pc02-commit.sh と scripts/fill-placeholders.sh が読む。
#
# 印はリポジトリに書けない値（秘密の値や、回線ごとに決まる値）の置き場所。値の形をここで確かめ、
# 不明な印・未設定・形の合わない値が 1 つでもあれば、何も置き換えずに 1 を返す（投入する側は何も送らずに止まる）。
#
#   印                 値の形                                   秘密（表示で伏せる）
#   @SNMP_COMMUNITY@   VyOS の community の名前（# は除く）     はい
#   @SSH_PUBKEY@       公開鍵の本体（base64）                    いいえ
#   @ULA_PREFIX@       fdXX:XXXX:XXXX:XXXX（ULA の /64 の上位 4 組）いいえ
#   @WAN_PREFIX64@     XXXX:XXXX:XXXX:XXXX（RA で受けた /64 の上位 4 組）いいえ
#   @NGN_GW_LLA@       fe80:: で始まる NGN 側のルータのリンクローカル  いいえ
#
# 使い方: fill_placeholders "<文字列>" → 成功なら FILLED に置き換えた文字列、FILLED_SECRETS に秘密の値の並び

placeholder_regex() {
  case $1 in
    SNMP_COMMUNITY) echo '^[A-Za-z0-9_!@*-]{1,100}$' ;;
    SSH_PUBKEY) echo '^[A-Za-z0-9+/]{16,}={0,2}$' ;;
    ULA_PREFIX) echo '^fd[0-9a-f]{2}(:[0-9a-f]{1,4}){3}$' ;;
    WAN_PREFIX64) echo '^[0-9a-f]{1,4}(:[0-9a-f]{1,4}){3}$' ;;
    NGN_GW_LLA) echo '^fe80::[0-9a-f]{1,4}(:[0-9a-f]{1,4}){0,3}$' ;;
    *) return 1 ;;
  esac
}

placeholder_is_secret() {
  [[ $1 == SNMP_COMMUNITY ]]
}

fill_placeholders() {
  local text=$1 names name re value
  # shellcheck disable=SC2034  # 読む側が使う
  FILLED=""
  # shellcheck disable=SC2034  # 読む側が使う
  FILLED_SECRETS=()
  names=$(grep -oE '@[A-Z][A-Z0-9_]*@' <<<"${text}" | tr -d @ | sort -u) || true
  # 先に全部を確かめる（途中まで置き換えた状態を残さない）
  for name in ${names}; do
    if ! re=$(placeholder_regex "${name}"); then
      echo "印 @${name}@ は置き換え方が決まっていない（不明な印）" >&2
      return 1
    fi
    if [[ -z ${!name+x} ]]; then
      echo "印 @${name}@ を置き換える環境変数 ${name} が未設定" >&2
      return 1
    fi
    value=${!name}
    if ! [[ ${value} =~ ${re} ]]; then
      echo "環境変数 ${name} の値の形が合わない（${re}）" >&2
      return 1
    fi
  done
  for name in ${names}; do
    value=${!name}
    if placeholder_is_secret "${name}"; then
      FILLED_SECRETS+=("${value}")
    fi
    text=${text//"@${name}@"/"${value}"}
  done
  # shellcheck disable=SC2034  # 読む側（pc02-commit.sh・fill-placeholders.sh）が使う
  FILLED=${text}
}
