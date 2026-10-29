#!/usr/bin/env bash
# R1 再起動にかかる時間（RFC 2544 26.6 Reset）。PC02 を再起動して、転送が止まっていた時間を PC03 の受信数から測る。
# **PC02 を再起動する。** CONFIRM_REBOOT=yes を付けたときだけ動く（1 回ごとにユーザーの承認を得てから付ける）。
#
#   CONFIRM_REBOOT=yes scripts/run-reboot.sh [出力先（既定: results/R1-reboot/<日付>）] [締め切り "YYYY-MM-DD HH:MM"]
#
# 任意の指定（環境変数）: ROUNDS（既定 3）、RATE（全体の pps、既定 100000）、DURATION（1 巡の送信秒数、既定 420）、
#   PRE（送信を始めてから再起動までの秒数、既定 30）、COOLDOWN（巡のあいだの休み、既定 120）、SSH_TIMEOUT（既定 600）
#
# 手順:
#   1. 安全確認: confirm 待ちの commit-confirm が無いこと、保存済みの設定に管理系の行（VRF MGMT・eth0 の DHCP・
#      SSH の VRF・鍵）があること。どちらかが欠けたら何もせずに止める
#   2. 保存済みの基準（/config/config.boot）を /config/config.boot.reboot-baseline に写して取っておく
#   3. tuning-1-offload と test-sink を入れて save する（再起動しても残るように）。保存後にもう一度 1 の確認をする
#   4. 各巡: PC03 で sfp0 の rx_packets を 1 秒ごとに記録 → PC01 から 64B を RATE pps で送る → PRE 秒後に
#      `reboot now` → SSH が落ちて戻るのを待つ → 送信が終わったら記録を回収する
#   5. 終わったら（中断でも）、取っておいた基準を config.boot に戻して読み直す（他の実行器の to_base と同じ状態）
#
# 再起動の指示は op モードの `reboot now`（vyos-1x の op-mode-definitions/reboot.xml.in で
# `powerctrl.py --yes --reboot` に当たる。--yes なので確認を聞かない）。
# 停止時間は PC03 の受信数だけで測る（PC02 の exporter は再起動中に止まるため使わない）。
# 遠くのコマンドに手元の変数（${backup} など）を埋め込むのは意図どおりなので、SC2029 は全体で止める
# shellcheck disable=SC2029
set -uo pipefail
if [[ ${CONFIRM_REBOOT:-} != yes ]]; then
  echo "run-reboot: PC02 を再起動する試験。承認を得てから CONFIRM_REBOOT=yes を付けて実行する" >&2
  exit 2
fi
here="$(cd "$(dirname "$0")" && pwd)"
cd "${here}/.." || exit 1
out="${1:-results/R1-reboot/$(date +%Y%m%d)}"
deadline="${2:-$(date -v+3H '+%Y-%m-%d %H:%M' 2>/dev/null || date -d '+3 hours' '+%Y-%m-%d %H:%M')}"
rounds="${ROUNDS:-3}" rate="${RATE:-100000}" duration="${DURATION:-420}" pre="${PRE:-30}"
cooldown="${COOLDOWN:-120}" ssh_timeout="${SSH_TIMEOUT:-600}"
v=vyos
backup=/config/config.boot.reboot-baseline
pc02_eth1_mac=6c:fe:54:6e:27:a0
sshq=(ssh -o BatchMode=yes -o ConnectTimeout=5)

now() { python3 -I -c 'import time; print(f"{time.time():.3f}")'; }
end=$(python3 -I -c 'import datetime, sys; print(int(datetime.datetime.strptime(sys.argv[1], "%Y-%m-%d %H:%M").timestamp()))' "${deadline}") \
  || { echo "run-reboot: 締め切りの形が違う（YYYY-MM-DD HH:MM）: ${deadline}" >&2; exit 1; }
in_time() { (($(date +%s) < end)) || { echo "== $(date '+%H:%M') 締め切り（${deadline}）を過ぎたので $1 を飛ばす"; return 1; }; }

# ---- 安全確認 ----
# 前の commit-confirm が残っていると、5 分後に PC02 が勝手に再起動して測定が狂う（scripts/pc02-commit.sh と同じ確認）
no_pending_confirm() {
  if ssh pc02 'pgrep -f "[c]ommit-confirm-notify"' >/dev/null 2>&1; then
    echo "run-reboot: confirm 待ちの commit-confirm が残っている。中身を確かめて confirm するか待ってから実行する" >&2
    return 1
  fi
}
# 保存済みの設定（再起動で読まれる /config/config.boot）を set の形にする
saved_cmds() {
  ssh pc02 'sudo cat /config/config.boot | python3 -c "import sys; from vyos.configtree import ConfigTree; print(ConfigTree(config_string=sys.stdin.read()).to_commands())"' \
    2>/dev/null | tr -d "'"
}
# 再起動の後に管理系の SSH で入り直せる行が保存されているか。$1 が test なら、試験の設定が保存されているかも見る
check_saved() {
  local cmds missing=() l
  cmds=$(saved_cmds)
  local need=(
    "set vrf name MGMT table 100"
    "set interfaces ethernet eth0 address dhcp"
    "set interfaces ethernet eth0 vrf MGMT"
    "set service ssh vrf MGMT"
  )
  [[ ${1:-} == test ]] && need+=(
    "set interfaces ethernet eth1 ring-buffer rx 4096"
    "set protocols static arp interface eth1 address 192.168.10.99 mac 02:00:00:00:00:99"
  )
  for l in "${need[@]}"; do grep -qxF "${l}" <<<"${cmds}" || missing+=("${l}"); done
  grep -q '^set system login user vyos authentication public-keys claude-lab key ' <<<"${cmds}" \
    || missing+=("set system login user vyos authentication public-keys claude-lab key ...")
  if ((${#missing[@]})); then
    echo "run-reboot: 保存済みの設定（/config/config.boot）に次の行が無い。再起動すると管理系の SSH で入れない" \
      "か、試験の設定で起動しないので止める:" >&2
    printf '  %s\n' "${missing[@]}" >&2
    return 1
  fi
}

no_pending_confirm || exit 4
check_saved || exit 4

# ---- ここから PC02 を触る ----
mkdir -p "${out}"
scp -q scripts/pktgen-run.sh scripts/pktgen-keepalive.sh pc01:~/
# 鍵・パスワードと SNMP の community（vyos/snmp-mgmt.cmds、秘密）を伏せてから残す
cfg() {
  ssh pc02 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' \
    | sed -E "s/ (encrypted-password|key|community) ('[^']*'|[^ ']+)/ \1 '<伏せ字>'/g" | sort > "$1"
}
wait_ssh_up() {
  local limit=$(($(date +%s) + $1))
  while (($(date +%s) < limit)); do
    "${sshq[@]}" pc02 true 2>/dev/null && return 0
    sleep 2
  done
  return 1
}
console_msg() {
  echo "run-reboot: PC02 の管理系の SSH が ${ssh_timeout} 秒たっても戻らない。物理コンソールで PC02 の起動の様子を確かめる" \
    "（設定は ${backup} に基準を取ってある。戻ったら CONFIRM_REBOOT は付けずに、基準へ戻す手順を人の手で行う）" >&2
}
pc02_lost=0
# 基準へ戻す: 取っておいた config.boot を書き戻して読み直す（pc02-commit.sh --restore-saved）。
# 取っておいたものは消さない（試験の設定を save する前にも使うため）
restore_base() {
  if "${sshq[@]}" pc02 "test -e ${backup}"; then
    ssh pc02 "sudo cp -p ${backup} /config/config.boot" || return 1
  fi
  bash scripts/pc02-commit.sh --restore-saved >/dev/null || return 1
  ssh pc02 'for i in eth1 eth2; do sudo ethtool -G $i rx 512 tx 512 2>/dev/null || true; done'
}
# 終わりの後始末: 基準へ戻し、書き戻した config.boot と取っておいたものが同じなら、取っておいたものを消す
to_base() {
  restore_base || return 1
  ssh pc02 "sudo cmp -s ${backup} /config/config.boot && sudo rm -f ${backup}; true"
}
# 見張り役（pktgen-keepalive.sh）を先に止める（後に残すと、リンクが戻ったときに送信を流し直す）
stop_traffic() {
  ssh pc01 'sudo pkill -f "[p]ktgen-keepalive.sh"; sudo sh -c "echo stop > /proc/net/pktgen/pgctrl" 2>/dev/null; sudo pkill -f "[p]ktgen-run.sh"; true' 2>/dev/null
  ssh pc03 'test -e /tmp/reboot-sampler.pid && kill "$(cat /tmp/reboot-sampler.pid)" 2>/dev/null; rm -f /tmp/reboot-sampler.pid; true' 2>/dev/null
}
cleanup() {
  echo "== $(date '+%H:%M') 後始末（送信と記録を止めて、PC02 を基準に戻す）"
  stop_traffic
  if ((pc02_lost)); then console_msg; return; fi
  if ! wait_ssh_up "${ssh_timeout}"; then console_msg; return; fi
  no_pending_confirm || return
  to_base || echo "run-reboot: 基準に戻せなかった。${backup} を確かめる" >&2
  cfg "${out}/pc02-config-end.cmds"
}
trap cleanup EXIT

# 基準を取っておく。前回が中断して取っておいたものが残っているなら、それが基準なので上書きしない
if "${sshq[@]}" pc02 "test -e ${backup}"; then
  echo "== 前回の ${backup} が残っているので、それを基準として使う"
else
  ssh pc02 "sudo cp -p /config/config.boot ${backup}" || { echo "run-reboot: 基準を取っておけなかった" >&2; exit 1; }
fi
cfg "${out}/pc02-config-before.cmds"
restore_base || { echo "run-reboot: 基準に戻せなかった" >&2; exit 1; }
bash scripts/pc02-commit.sh --save "${v}/tuning-1-offload.cmds" "${v}/test-sink.cmds" \
  || { echo "run-reboot: 試験の設定を保存できなかった" >&2; exit 1; }
check_saved test || exit 4
cfg "${out}/pc02-config-test.cmds"

# PC03 の上で展開させるので、単一引用符のままにする
# shellcheck disable=SC2016
sampler='nohup sh -c '\''while :; do echo "$(date +%s.%N) $(cat /sys/class/net/sfp0/statistics/rx_packets)"; sleep 1; done'\'' < /dev/null > /tmp/reboot-rx.txt 2>&1 & echo $! > /tmp/reboot-sampler.pid'
ev() { echo "$1=$2" >> "${d}/events.txt"; }
for ((r = 1; r <= rounds; r++)); do
  d="${out}/round${r}"
  if [[ -s ${d}/pc03-rx.txt ]] && grep -q '^traffic_end=' "${d}/events.txt" 2>/dev/null; then
    echo "== round${r} は結果があるので飛ばす"; continue
  fi
  in_time "round${r}" || break
  mkdir -p "${d}"
  : > "${d}/events.txt"
  echo "== $(date '+%H:%M') round${r} 開始"
  ssh pc03 "${sampler}"
  sleep 2
  ev traffic_start "$(now)"
  # pktgen はリンクが落ちると止まるので、リンクが戻ったら流し直す見張り役（pktgen-keepalive.sh）を通す
  ssh pc01 "sudo IFACE=sfp0 SIZE=60 DST=192.168.20.2 SRC=192.168.10.10 DMAC=${pc02_eth1_mac} THREADS=4 \
    KEEP_SECONDS=${duration} RATE=${rate} FLOWS=many bash ~/pktgen-keepalive.sh" < /dev/null > "${d}/pktgen.log" 2>&1 &
  pg=$!
  sleep "${pre}"
  # 再起動の直前にもう一度確かめる
  if ! no_pending_confirm || ! check_saved test; then
    echo "run-reboot: 再起動の前の確認で止めた" >&2; exit 4
  fi
  ev reboot "$(now)"
  ssh -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 pc02 \
    '/opt/vyatta/bin/vyatta-op-cmd-wrapper reboot now' > "${d}/reboot.log" 2>&1
  # SSH が落ちるのを待つ（落ちなければ再起動していない）
  down=0 limit=$(($(date +%s) + 120))
  while (($(date +%s) < limit)); do
    if ! "${sshq[@]}" pc02 true 2>/dev/null; then down=1; break; fi
    sleep 1
  done
  if ((!down)); then echo "run-reboot: 120 秒たっても PC02 の SSH が落ちない。再起動していない" >&2; exit 1; fi
  ev ssh_down "$(now)"
  if ! wait_ssh_up "${ssh_timeout}"; then pc02_lost=1; ev ssh_back none; exit 1; fi
  ev ssh_back "$(now)"
  # 起動時の設定の読み込み（vyos-router）が終わるまで待つ
  limit=$(($(date +%s) + 300))
  until ssh pc02 'systemctl is-active -q vyos-router.service' 2>/dev/null || (($(date +%s) >= limit)); do sleep 2; done
  ev router_ready "$(now)"
  wait "${pg}"
  ev traffic_end "$(python3 -I -c 'import sys; print(float(sys.argv[1]) + float(sys.argv[2]))' \
    "$(grep '^traffic_start=' "${d}/events.txt" | cut -d= -f2)" "${duration}")"
  sleep 3
  ssh pc03 'kill "$(cat /tmp/reboot-sampler.pid)"; rm -f /tmp/reboot-sampler.pid'
  scp -q pc03:/tmp/reboot-rx.txt "${d}/pc03-rx.txt"
  # 再起動の後も試験の設定（保存したもの）で動いているか
  cfg "${d}/pc02-config-after.cmds"
  if grep -qxF "set interfaces ethernet eth1 ring-buffer rx '4096'" "${d}/pc02-config-after.cmds"; then
    ev config_survived yes
  else
    ev config_survived no
  fi
  echo "   round${r}: $(python3 -I scripts/summarize_reboot.py "${out}" | tail -1)"
  ((r < rounds)) && { echo "== ${cooldown} 秒休む"; sleep "${cooldown}"; }
done
python3 -I scripts/summarize_reboot.py "${out}"
echo "== $(date '+%H:%M') すべて終了（保存先: ${out}）"
