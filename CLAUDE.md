10G VyOSルータ検証 引き継ぎ資料
===============================

-	最終更新: 2026-10-15
-	オーナー: Yusuke（cyokozai）

> 新しいClaude Codeセッションへの引き継ぎ用。このリポジトリの `CLAUDE.md` として置いてあり、セッション開始時に自動で読み込まれる。

---

§0 このプロジェクトの目的
-------------------------

-	**最終目標**: 自宅サーバを「NGN（フレッツ）網内に特化した10G自作ルータ」で運用できるようにする。自鯖缶（2026-11-15、お台場）での登壇ネタにもする。
-	**当面の目標**: 閉域網の検証環境で、PC02（VyOS）が10Gルータとして理論値にどこまで迫れるかを測る。そのうえで、MAP-Eの模擬とNGN特有の処理（DSCPの書き換えなど）の性能コストを測る。
-	**判断したいこと**: VyOSのまま運用するか、NixOS＋FRRで自作ルータOSにするか（§7）。

---

§1 役割分担（重要）
-------------------

| 担当                       | やること                                                                                                                                      |
|----------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| **Yusuke（人間）**         | ハードウェア全般。BIOS設定、OSのインストール（USB起動）、配線、NICやメモリの抜き差し、物理コンソールでの作業、Aruba スイッチのWeb画面での設定 |
| **Claude Code（desktop）** | SSH経由のソフトウェア設定全般。VyOSの設定、Ubuntuのパッケージ、netplan、sysctl、監視、試験の実行、結果の記録                                  |

### Claude Codeが守るルール

1.	**管理系（172.16.0.0/24、オンボードの1G）の設定は変更しない。** 変更が必要なときは事前にYusukeに確認する。SSHが切れると、物理コンソールでの復旧が必要になる。
2.	**VyOSでは原則 `commit-confirm 5` を使う。** 確認後に `confirm` → `save`。管理系に影響しうる変更は必ずこの手順にする。
3.	**変更の前後で設定を保存する。** VyOSは `show configuration commands`、Ubuntuは対象ファイルを、このリポジトリの `configs/<host>/` に保存してgitにcommitする。
4.	**試験の結果は `results/<試験ID>/` にJSONとログで保存する。** 手入力で丸めた数字だけを残さない。
5.	**ハードウェアの作業が必要になったら止まって依頼する。** BIOS、再インストール、配線、NICの差し替えなどは、Yusukeへの依頼文を出して待つ。
6.	**10Gの経路でSSHや監視をしない。** 計測の邪魔になるため。
7.	**OSインストールの依頼文には、インストーラが出す質問をすべて答えと一緒に書く。** 特にRAID・インストール先の選択・「全データを消す」の確認。インストール先は容量と型番で特定させ、起動用以外のUSBメモリ・外付けディスクは先に抜いてもらう（2026-10-22 実害: 書き漏らしたため、PC02のVyOSインストーラが起動中のVentoy USBをRAID-1の相手に選び、ユーザーのUSBを上書きした）。
8.	**ディスク装置を直接読み書きする操作（dd・diskutil erase・mkfs・パーティション操作・Ventoy2Diskなど）は自分で実行しない。** `bash-guard.sh` が拒否する。ユーザーに渡すときは、対象の装置・消えるもの・使う容量と時間・より軽い代わり（直接読む、公式から取り直す）を先に示し、ユーザーの道具（VentoyはWindowsのRufus）を確かめてからにする。

### このリポジトリのgit運用

-	既定ブランチは `king's`（`dev` は無い）。分岐は `origin/king's` から切る。
-	通常の作業ツリーではgitの書き込みが `bash-guard.sh` で拒否される。ルール3・4のcommitは **worktreeの中で** 行い、PRにしてYusukeがGitHub上でマージする。
-	試験中に取った設定・結果は、まず作業ツリーの `configs/` `results/` に書き出し、区切りのよいところでworktreeからまとめてcommitする。
-	`.claude/worktrees/` は `.gitignore` 済み。

---

§2 機材一覧
-----------

| ID       | 機種                                                         | CPU / メモリ                                                                              | 役割                                                                             | 状態（2026-10-15時点）                                                                                   |
|----------|--------------------------------------------------------------|-------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------|
| **PC01** | HP ProDesk 600 G4/G5系 SFF（BIOS P07 Ver.02.49、2023-12-08） | 不明（第8〜9世代 Core i系の想定）                                                         | 送信側の端末（Ubuntu 24.04 Server、内蔵ディスク）                                     | **F10を押すと「セットアップメニューに移行しています...」で止まる**（§9で切り分け中）。OSは未インストール |
| **PC02** | HP ProDesk 400 G7 SFF（BIOS S08 Ver.02.23.00、2025-07-04）   | Core i5-10500（6コア12スレッド、3.1〜4.5GHz）/ DDR4 8GB（1枚か2枚かは未確認） / SSD 240GB | **ルータ本体（VyOS）**。このまま本番ルータにする予定で、入れ替えはしない         | OSは未インストール                                                                                       |
| **PC03** | MSI MAG B550 TOMAHAWK（BIOS E7C91AMS.AE0、2023-06-28）       | Ryzen 7 5700G（8コア16スレッド）/ DDR4 64GB（3200） / NVMe SSD（M2_2）                    | 受信側の端末、MAP-EのBR役、監視（Prometheus/Grafana）、NTPサーバ（Ubuntu 24.04 Server、内蔵NVMe） | OSは未インストール                                                                                       |
| **SW**   | Aruba Instant On 1930（24ポート＋SFP 4口）                   | ―                                                                                         | 管理系の1Gスイッチ                                                               | 未設定。**CLIもシリアルコンソールもなく、Web画面のみ**                                                   |

### NIC

-	**PC02**: インテルの2ポートSFP+カード1枚を、**x16スロット**に装着済み。型番（X520-DA2 / 82599ESか、X710-DA2 / XL710か）は**未確認**。
-	PC01、PC03のSFP+ NICの型番も未確認。
-	OSを入れたら最初に `lspci -nn | grep -i ether`、`ethtool -i`、`lspci -vv | grep LnkSta:` で確認し、この表を更新する。

### 機材ごとの注意点

-	**PC01**: Ubuntu 24.04 Serverを内蔵ディスクに直接入れる（2026-10-21決定。一時検討した外付けSSDからの起動は使わない）。内蔵ディスクの型番と容量は未確認。
-	**PC02（400 G7 SFF）**: 拡張スロットはx16が1本とx1が1本。NICは必ずx16側に挿す（x1だと約7.9Gbpsが上限）。メモリが1枚挿しなら、4GB×2枚または8GB×2枚にしてデュアルチャネルにすると、64Bパケットの試験で差が出る可能性がある（`sudo dmidecode -t memory` で確認）。
-	**PC03（5700G）**: APUのため、PCIeは**Gen3止まり**。NICはCPU直結の一番上のx16スロットに挿す。オンボードLANは2.5GbE（Realtek）と1GbE（Intel）の2口のはず。管理系にはIntel側を使う。BIOSの時計がずれていたので、chronyで時刻を合わせる。
-	**SW（Aruba 1930）**: 型番が「24G 4SFP/SFP+」（JL682A）なら、右端の4口は10GのSFP+。今回の10G経路はPC同士の直結なので使わない。

---

§3 ネットワーク設計
-------------------

```
            10G LAN（SFP+）               10G WAN（SFP+）
  PC01 ─────────────────── PC02 VyOS ─────────────────── PC03
  sfp0                     eth1 │ eth2                    sfp0
  192.168.10.10/24   192.168.10.1/24  192.168.20.1/30     192.168.20.2/30
  2001:db8:10::10/64  2001:db8:10::1   2001:db8:20::1     2001:db8:20::2
    │                        │                              │
    └──────── 管理系 1G（Aruba 1930、172.16.0.0/24）──────────┘
```

| 区間                      | IPv4                                              | IPv6                                 | PC02側のIF             |
|---------------------------|---------------------------------------------------|--------------------------------------|------------------------|
| PC01 ⇄ PC02（LAN）        | 192.168.10.0/24（PC02 .1 / PC01 .10）             | 2001:db8:10::/64（::1 / ::10）       | `eth1`（SFP+ #1）      |
| PC02 ⇄ PC03（WAN、NGN役） | 192.168.20.0/30（PC02 .1 / PC03 .2）              | 2001:db8:20::/64（::1 / ::2）        | `eth2`（SFP+ #2）      |
| 管理系                    | 172.16.0.0/24                                     | なし                                 | `eth0`（オンボード1G） |
| MAP-E模擬                 | CE 192.0.2.18/32、PC03の外側アドレス 198.51.100.1 | CE 2001:db8:20::c、BR 2001:db8:20::b | `tun0`                 |

### 管理系のアドレス

| 機器                                   | IP           | Arubaのポート |
|----------------------------------------|--------------|---------------|
| Aruba 1930                             | 172.16.0.2   | ―             |
| PC01                                   | 172.16.0.11  | 1             |
| PC02（VyOS）                           | 172.16.0.12  | 2             |
| PC03                                   | 172.16.0.13  | 3             |
| 作業用ノートPC / Claude Codeを動かすPC | 172.16.0.100 | 24            |

-	VLANは全ポート初期値（VLAN 1、タグなし）のまま。
-	インターフェース名（eth0〜2、sfp0）は仮置き。インストール後にMACアドレスで対応づけて更新する。

### SSHの設定例（Claude Codeを動かすPCの `~/.ssh/config`）

```
Host pc01
  HostName 172.16.0.11
  User yusuke
Host pc02 vyos
  HostName 172.16.0.12
  User vyos
Host pc03
  HostName 172.16.0.13
  User yusuke
Host pc0*
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 30
```

鍵の登録とユーザー名はYusukeが決めて追記する（2026-10-15時点で `~/.ssh/config` に `pc0*` の項目はまだ無い）。VyOSでは、鍵を `set system login user vyos authentication public-keys ...` で登録する。

---

§4 Aruba 1930の初期設定（Yusukeの作業）
---------------------------------------

1.	中古や使い回しの機器なら、リセット穴を**5秒以上**押して初期化する。
2.	ノートPCを `192.168.1.2/24` に固定し、DHCPのない状態で `http://192.168.1.1` を開く。
3.	最初の画面で**ローカル管理（CONNECT）**を選ぶ。ユーザー名は `admin`、パスワードは空欄。初回に変更を求められる。
4.	管理用IPを `172.16.0.2/24` に固定する（ゲートウェイとDNSは空欄）。適用後は、ノートPCを `172.16.0.100/24` に変えて入り直す。
5.	SNTPのサーバは、PC03（172.16.0.13）が立ち上がってから設定する。

---

§5 ホストのセットアップ手順
---------------------------

### 5-1. BIOS（Yusukeの作業）

| 項目                                                                                                        | 設定                            |
|-------------------------------------------------------------------------------------------------------------|---------------------------------|
| C-state / 省電力（HPは「Runtime Power Management」「Idle Power Savings」、MSIは「Global C-state Control」） | 無効                            |
| 電源喪失後の動作                                                                                            | 電源オン                        |
| Secure Boot                                                                                                 | 無効                            |
| 起動モード                                                                                                  | UEFIのみ                        |
| NICのオプションROM / PXE                                                                                    | 無効（UEFIのみ、またはPXE無効） |
| SMT / Hyper-Threading                                                                                       | 試験ごとにON/OFFを記録する      |

### 5-2. PC02: VyOS

-	**イメージ**: **rollingの2026-08-06以降のビルド**を使う（このリポジトリでは `vyos/VERSION` の `2026.10.16-0024-rolling` に固定）。理由は、QoSの `set-dscp` がPR #5305（T8996、コミット `a726455aa` は2026-07-28作成、rollingへのマージは2026-08-06）で初めて実装されたため（§6-4）。旧安定系列のブランチ（sagitta / circinus）は public-unmaintained で、この修正は入っていない。VyOS Stream（最新は2026.03、circinusベース）にも入っていない（2026-10-21確認）。circinus系でも `set-dscp` は設定として書けてcommitも通るが、書き換えの処理（pedit）が無いので効かない。
-	**インストーラの質問と答え**（`install image`）: 続行 `y` / イメージ名 既定のまま / vyosのパスワード 強いものを入力 / コンソール 既定（K） / **RAID-1 は `n`**（内蔵はSSD 223.6GBの1台だけ。2台見えたらもう1台は起動中のUSB） / インストール先 **`/dev/sda`（223.6GB）** / 全データ削除の確認 `y` / 空き領域をすべて使う `y` / 起動時の設定ファイル 既定（1）。始める前に起動用以外のUSBメモリを抜く。
-	インストールはYusukeが行う。USBから起動 → `install image` → 再起動 → 物理コンソールで管理系の `eth0` に 172.16.0.12/24 を設定し、SSHを有効にする。ここまで終われば、以降はClaude CodeがSSHで作業する。

確認するコマンド:

```
sudo lspci -nn | grep -i ether
sudo lspci -vv -s <BDF> | grep -E 'LnkCap|LnkSta'   # X520は5GT/s x8、X710は8GT/s x8
sudo ethtool -i eth1 ; sudo ethtool -l eth1 ; nproc
show configuration commands > /config/baseline.cmds
```

### 5-3. PC01 / PC03: Ubuntu 24.04 LTS Server

**インストール（Yusukeの作業）**

-	PC01は内蔵ディスク、PC03は内蔵NVMe（M2_2）の全体を使う。「Install OpenSSH server」にチェックする。ユーザー名 `yusuke`、ホスト名 `pc01` / `pc03`。
-	どちらも、**インターネットにつながる自宅LANにオンボード1Gを挿した状態**でインストールし、次のパッケージまで入れてから管理系へ移す。管理系（172.16.0.0/24）にはゲートウェイが無く、移したあとは `apt` が使えない。足りないものが後から出たら、その機体だけ一時的に自宅LANへ戻す。

```
sudo apt update && sudo apt -y full-upgrade
sudo apt -y install openssh-server iperf3 ethtool tcpdump tshark nload sysstat \
  linux-tools-common linux-tools-$(uname -r) linux-modules-extra-$(uname -r) \
  chrony prometheus-node-exporter jq git
sudo modprobe pktgen && lsmod | grep pktgen
git clone --depth 1 --filter=blob:none --sparse https://github.com/torvalds/linux.git ~/linux
git -C ~/linux sparse-checkout set samples/pktgen    # T3で使うpktgenのサンプル
```

-	`full-upgrade` でカーネルが上がったら、再起動してから `linux-tools-$(uname -r)` と `linux-modules-extra-$(uname -r)` を入れ直す（版がずれるとcpupowerとpktgenが使えない）。
-	PC03だけ、`prometheus` と、公式のAPTリポジトリ（`https://apt.grafana.com stable main`）を追加してから `grafana` も入れる。

Ubuntu 24.04のiperf3は3.16で、マルチスレッドに対応している。

**管理系への移動（Yusukeの作業。§1 ルール1）**

-	DHCPの `/etc/netplan/50-cloud-init.yaml` を消し、`/etc/netplan/50-mgmt.yaml` に `mgmt0`（オンボード1GのMACで照合。PC03はIntel側。`addresses: [172.16.0.11/24]` / `[172.16.0.13/24]`、`dhcp4: false`、ゲートウェイは書かない）を書いて `sudo netplan apply`。
-	ケーブルを自宅LANからArubaの1番（PC01）・3番（PC03）へ挿し替え、Macから `ssh pc01` / `ssh pc03` が通ったらClaude Codeに交代する。

**netplan（10G側。Claude Codeの作業）**: PC01の例。`/etc/netplan/60-10g.yaml`（chmod 600）に置く。PC03は `192.168.20.2/30`、`2001:db8:20::2/64` にし、`192.168.10.0/24 via 192.168.20.1` などの戻り経路を入れる。

```yaml
network:
  version: 2
  ethernets:
    sfp0:
      match: {macaddress: "<SFP+のMAC>"}
      set-name: sfp0
      mtu: 1500
      addresses: [192.168.10.10/24, "2001:db8:10::10/64"]
      routes:
        - {to: 192.168.20.0/30, via: 192.168.10.1}
        - {to: 192.0.2.0/24, via: 192.168.10.1}
        - {to: 198.51.100.0/24, via: 192.168.10.1}
        - {to: "2001:db8:20::/64", via: "2001:db8:10::1"}
```

**sysctl**（`/etc/sysctl.d/90-10g.conf`）:

```
net.core.rmem_max = 268435456
net.core.wmem_max = 268435456
net.ipv4.tcp_rmem = 4096 131072 268435456
net.ipv4.tcp_wmem = 4096 131072 268435456
net.core.netdev_max_backlog = 250000
```

あわせて `sudo cpupower frequency-set -g performance`、`sudo ethtool -G sfp0 rx 4096 tx 4096`、`sudo ethtool -A sfp0 rx off tx off`（フロー制御を切る。pps試験で送信側が絞られないように）を実行する。どれも再起動で戻るので、起動時に流すsystemdのユニットにまとめる。

**PC03だけ: BR役（フェーズ2で使う）**

```
sudo ip -6 addr add 2001:db8:20::b/64 dev sfp0
sudo ip -6 tunnel add br0 mode ipip6 local 2001:db8:20::b remote 2001:db8:20::c
sudo ip link set br0 up mtu 1460
sudo ip addr add 192.0.2.1/32 dev br0
sudo ip route add 192.0.2.18/32 dev br0
sudo ip addr add 198.51.100.1/32 dev lo    # インターネット役。iperf3 -s -B 198.51.100.1
```

実際のBRのように、割当範囲外のポートを捨てる動作も再現したい場合は、nftablesで「宛先ポートが8160-8175以外ならdrop」というルールを足す。

**PC03だけ: 監視とNTP**

-	Prometheus: `scrape_interval: 1s`。対象は `172.16.0.11/12/13:9100`。
-	Grafanaは、公式のAPTリポジトリを追加してから入れる。
-	chrony: `allow 172.16.0.0/24`、`local stratum 10` でNTPサーバにする。PC01、PC02、Arubaはこれを参照する。
-	見るべきメトリクス: PC02の `rate(node_network_receive_packets_total{device="eth1"}[5s])`、`rate(node_network_receive_drop_total[5s])`、softirqのコアごとの偏り、`ethtool -S` の `rx_missed`、`rx_no_dma_resources`、`fdir_miss`。

### 5-4. PC01 / PC03: 試験中に動く常駐を止める

自動更新は試験中にCPUとディスクを使う。管理系からはインターネットに出られないので動いても失敗するだけだが、試験の邪魔になるので止めておく（Claude Codeの作業）。

```
sudo systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer
sudo snap refresh --hold
```

---

§6 VyOSの設定
-------------

構文は1.5系 / rollingを前提にしている。ノード名は `?` の補完で必ず確認すること。

### 6-1. フェーズ1: 素のルーティング（FWもNATもなし）

```
set system host-name 'pc02-vyos'
set interfaces ethernet eth0 address '172.16.0.12/24'
set interfaces ethernet eth0 description 'MGMT-1G'
set service ssh listen-address '172.16.0.12'
set interfaces ethernet eth1 description 'LAN-10G-to-PC01'
set interfaces ethernet eth1 address '192.168.10.1/24'
set interfaces ethernet eth1 address '2001:db8:10::1/64'
set interfaces ethernet eth2 description 'WAN-10G-to-PC03'
set interfaces ethernet eth2 address '192.168.20.1/30'
set interfaces ethernet eth2 address '2001:db8:20::1/64'
set protocols static route 0.0.0.0/0 next-hop '192.168.20.2'
set protocols static route6 ::/0 next-hop '2001:db8:20::2'
set service ntp server '172.16.0.13'
set service monitoring prometheus node-exporter listen-address '172.16.0.12'
```

### 6-2. チューニング（一つずつ入れて、その都度測る）

```
# ① オフロードとリングバッファ（eth2も同様）
set interfaces ethernet eth1 offload gro
set interfaces ethernet eth1 offload gso
set interfaces ethernet eth1 offload sg
set interfaces ethernet eth1 offload tso
set interfaces ethernet eth1 ring-buffer rx '4096'
set interfaces ethernet eth1 ring-buffer tx '4096'
# ② RPS
set interfaces ethernet eth1 offload rps
set interfaces ethernet eth2 offload rps
# ③ flowtable
set firewall flowtable FT10G interface 'eth1'
set firewall flowtable FT10G interface 'eth2'
set firewall flowtable FT10G offload 'software'
set firewall ipv4 forward filter rule 10 action 'offload'
set firewall ipv4 forward filter rule 10 offload-target 'FT10G'
set firewall ipv4 forward filter rule 10 state 'established'
set firewall ipv4 forward filter rule 10 state 'related'
# （ipv6も同様）
# ④ CPUの性能プロファイル
set system option performance 'network-throughput'
```

先行事例（VyOSフォーラムのUDP pps事例、82599ESとE5-2680 v4の構成）では、次のとおりだった。

-	単一フローでは約50万ppsで頭打ちになった。
-	リングバッファやIRQの調整は、効果が小さかった。
-	RPSとflowtableの併用で約400万pps、XDPで約1000万ppsまで伸びた。

flowtableには次の制約がある。

-	フローの最初のパケットは通常の経路で処理される。
-	フラグメントしたパケットには効かない。
-	`offload hardware` は対応するNICでしか使えない。

### 6-3. フェーズ2: MAP-E模擬（ipip6とNAT44）

```
delete protocols static route 0.0.0.0/0
set interfaces ethernet eth2 address '2001:db8:20::c/64'
set interfaces tunnel tun0 encapsulation 'ipip6'
set interfaces tunnel tun0 source-address '2001:db8:20::c'
set interfaces tunnel tun0 remote '2001:db8:20::b'
set interfaces tunnel tun0 address '192.0.2.18/32'
set interfaces tunnel tun0 mtu '1460'
set interfaces tunnel tun0 ip adjust-mss '1420'
set protocols static route 0.0.0.0/0 interface 'tun0'
set nat source rule 101 outbound-interface name 'tun0'
set nat source rule 101 source address '192.168.10.0/24'
set nat source rule 101 protocol 'tcp_udp'
set nat source rule 101 translation address '192.0.2.18'
set nat source rule 101 translation port '8160-8175'
set nat source rule 199 outbound-interface name 'tun0'
set nat source rule 199 source address '192.168.10.0/24'
set nat source rule 199 translation address '192.0.2.18'
```

-	ポートの範囲は『徹底解説v6プラス』の表4.1にある例（a=4, k=8, PSID=0xfe）。使えるのは16ポート×15セットで、計240ポート。
-	**既知の課題**: VyOSのSNATは1ルールに1つのポート範囲しか書けない。単純にルールを並べると、最初の16ポートしか使われない。15セットすべてを使う方法として、(a) 送信元ごとにルールを分ける、(b) 生のnftablesでパケットにマークを付けてSNATを振り分ける（OpenWrtでの事例あり）、の2案を比べる。

### 6-4. NGN向けのDSCP書き換え（QoSの set-dscp）

-	**背景**: NGNは、DSCP 0x88（AF41）のIPv6パケットをひかり電話用に扱う。そのため、ひかり電話ありのネクスト回線では**破棄**され、ひかり電話なしの回線では**0x00に書き換えられて届く**（nekko-labの実測）。
-	WireGuardのハンドシェイクは、Linuxの現行カーネルでも `HANDSHAKE_DSCP = 0x88` のまま。
-	古いOpenSSHのscpなどでも、DSCPの値が原因で止まった事例がある。
-	**以前の対処**: [nekko-lab/vyos](https://github.com/nekko-lab/vyos) で、VyOS 1.3.6（カーネル5.4.268と wireguard-linux-compat）にWireGuardのパッチを当て、ISOを再ビルドしていた（2024年3月）。Nekko Cloudのコアルータで、NGN網内を経由して拠点間のVyOS同士を直接WireGuardでつなぐために必要だった。
-	**現在**: rollingでは、QoSのshaperの `set-dscp` が実装された（tc pedit。IPv6は `ip6 traffic_class`、ECNは保持）。**パッチなしの設定だけで置き換えられる見込み**。送信キューで書き換えるので、ルータ自身が送るパケットにも効くはず。

設定例:

```
set qos policy shaper NGN-OUT bandwidth '10gbit'
set qos policy shaper NGN-OUT class 10 bandwidth '100%'
set qos policy shaper NGN-OUT class 10 match WG-HS ipv6 protocol 'udp'
set qos policy shaper NGN-OUT class 10 match WG-HS ipv6 source port '51820'
set qos policy shaper NGN-OUT class 10 set-dscp 'CS0'
set qos policy shaper NGN-OUT default bandwidth '100%'
# NGNへ出る通信をすべて0にするなら次の行を追加
# set qos policy shaper NGN-OUT default set-dscp 'CS0'
set qos interface eth2 egress 'NGN-OUT'    # 検証環境ではWAN役のeth2。本番では本番のWAN側IF
```

-	**注意**: 検証環境の `eth0` は管理系（§1 ルール1）。shaperを `eth0` に付けない。本番（§6-5）ではWAN側の名前が変わるので、そのときのWAN IFに読み替える。

-	確認: `sudo tc filter show dev <WAN> egress` に `pedit ... traffic_class` が付いているかを見る。`tcpdump -v` で class 0x00 になっていることを確認する。

-	NGNでの実地確認: `nc -u -v -T 0x88 <相手のIPv6> 52528` で送り、相手側で `tcpdump -X` を見る。

-	**懸念**: shaperは帯域制御の仕組み（HTB）を全パケットに通すので、10Gでは律速になるかもしれない。試験T8で確認する。

-	代わりの方法: 起動時のスクリプト（`/config/scripts/vyos-postconfig-bootup.script`）で、独自のnftablesテーブルを入れる（`ip6 dscp af41 ip6 dscp set cs0`）。

### 6-5. 本番向け: DHCPv6-PD

```
set interfaces ethernet eth0 ipv6 address autoconf
set interfaces ethernet eth0 dhcpv6-options pd 0 length '56'      # HGWの配下なら60
set interfaces ethernet eth0 dhcpv6-options pd 0 interface eth1 sla-id '1'
set interfaces ethernet eth0 dhcpv6-options pd 0 interface eth1 address '1'
set service router-advert interface eth1 prefix ::/64
```

-	上の `eth0` は本番でのWAN側IFの例。検証環境の `eth0`（管理系）にはこの設定を入れない。
-	WAN側の入力フィルタで、UDP 546番とICMPv6を許可すること。
-	ひかり電話ありでは、DHCPv6-PDで/56が来る。ひかり電話なしのネクストはRAで/64のみで、NDプロキシが必要になる。光クロスはPDで/56と言われているが、実機で要確認。
-	プレフィックスは半固定なので変わりうる。MAP-EのCE値やIPIPトンネルを作り直す仕組みは自作が必要。

---

§7 VyOSかNixOS＋FRRか（未決定）
-------------------------------

-	nekko-labのパッチはDSCPの書き換えだけだったので、rollingのQoSで置き換えられそう。**「パッチを当てるために再ビルドする」理由はなくなりつつある**。
-	決め手は「**VyOSの設定の外にはみ出す生のnftablesがどれだけ増えるか**」。DSCPの書き換え、MAP-Eのポートセット、プレフィックス変化への追従が候補。はみ出す量が多ければ、NixOSで全部を宣言的に書く方が見通しがよい。
-	NixOSの注意点:
	-	`boot.kernelPatches` は1行で書ける。ただしカーネルのビルドはキャッシュが効かず、更新のたびに手元でビルドが走る。
	-	MAP-Eの既製モジュールは見つからなかった。トンネルはsystemd-networkd、ポートセットはnftablesで自作することになる。
	-	不正な設定でもそのまま適用されてしまう。
-	参考事例:
	-	NGN向けで実績が多いのはOpenWrt（MAP-Eのパッケージあり、x86の10G事例あり）
	-	NixOSの事例: alles-wg/nixos-router、ghostbuster91/nixos-router、FOSDEM 2026「NixOS on routers」
-	**進め方**: フェーズ2をVyOSでやり、設定の外にはみ出した量を数える → 同じ内容をNixOSで書いて比べる。この比較は、そのまま登壇の一節になる。

---

§8 試験計画
-----------

### 目標値（10Gのワイヤーレート）

| フレーム                   | pps         | ツール              | 見込み                                     |
|----------------------------|-------------|---------------------|--------------------------------------------|
| 64B                        | 14,880,952  | pktgen              | カーネル転送では届かない。限界値を記録する |
| 512B                       | 2,349,624   | iperf3 UDP / pktgen | チューニング次第                           |
| IMIX（7:4:1）              | 約3,343,700 | pktgen              | チューニング次第                           |
| 1518B                      | 812,744     | iperf3 UDP          | 届く見込み                                 |
| TCP（MSS 1448）            | 約9.41Gbps  | iperf3              | 届く見込み                                 |
| TCP（ipip6越し、MSS 1408） | 約9.15Gbps  | iperf3              | フェーズ2の目標                            |

### 共通の条件

-	1条件につき60秒を3回測り、中央値を採る。最初の5秒は捨てる（`-O 5`）。
-	試験ごとにPC02を `baseline.cmds` の状態に戻し、差分の設定だけを入れる。
-	試験の前後で `ethtool -S` を保存し、差分を記録する。
-	判定の基準は、ロス率0.1%未満。
-	試験中はGrafanaを開かない。10Gの経路でtcpdumpを動かさない（T7のみ例外）。
-	T8はフェーズ1のまま比べられるので、T6・T7（フェーズ2）より先に行う。

### 試験一覧

| ID             | 内容                                                                                                                                                                                                 | PC02の設定   |
|----------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|--------------|
| T0             | 端末の実力（PC01→PC02、PC03→PC02、VyOSで終端）                                                                                                                                                       | フェーズ1    |
| T1             | ルーティング（TCP）。PC01→PC03、並列数1/4/16、上り・下り・双方向、IPv4とIPv6                                                                                                                         | フェーズ1    |
| T2             | ルーティング（UDP）。1518B（`-l 1472`）と512B（`-l 466`）                                                                                                                                            | フェーズ1    |
| T3             | 64Bのpps限界。カーネルの `samples/pktgen` を使い、単一フロー（sample03）と多数フロー（sample05）を測る。**先にPC01の送信能力の上限を測る**                                                           | フェーズ1    |
| T4             | チューニング①→②→③の効果。T2の512BとT3を再計測                                                                                                                                                        | 一つずつ追加 |
| T5             | ファイアウォールのコスト。ステートフルフィルタの有無、flowtableの有無                                                                                                                                | FWを追加     |
| T6             | MAP-E模擬。`iperf3 -c 198.51.100.1` を `-P 4` と `-P 32`（ポート枯渇の再現）で実行                                                                                                                   | フェーズ2    |
| T7             | プロトコルの動作確認。外側のNext Headerが4であること、送信元ポートが割当範囲に収まること、MSSが1420以下であること、PMTUDが働くこと（`ping -M do -s 1432` は通り、`-s 1472` は「Frag needed」が返る） | フェーズ2    |
| **T8（追加）** | **QoSの set-dscp のコスト。NGN-OUTのshaperの有無で、T1とT3を再計測**                                                                                                                                 | §6-4         |

記録表の列は「ID / 条件 / Gbps / pps / ロス率 / PC02の最大コア使用率 / メモ」。あわせて、各機のスペック、NICの型番とファームウェア、VyOSのビルド名、BIOSの条件を記録する。

---

§9 現在の状況と未解決事項
-------------------------

### ハードウェア（Yusukeの担当）

-	[ ] **PC01でBIOSに入れない問題**。切り分けの順番は次のとおり。
	1.	2〜3分待つ
	2.	キーボード以外のUSBを外し、キーボードは背面のUSB 2.0に挿す
	3.	**10GのNICを抜いて試す（本命）** → 入れたら、オプションROMをUEFIのみにする、PXEを無効にする、レガシーブートを無効にする、の順に設定してから挿し戻す。それでもだめなら、別のPCで `BootUtil -fd` を実行してNICの起動ROMを無効にする
	4.	画面の出力ポートやモニターを変える
	5.	ボタン電池を外してBIOSの設定を初期化する
-	[ ] PC02のメモリが1枚挿しかどうかを確認し、必要ならデュアルチャネルにする
-	[ ] 3台のNICの型番を確認する（X520 / X710）。SFP+モジュールかDACかも確認する（インテルNICの相性問題に注意。X520なら `ixgbe.allow_unsupported_sfp=1` で回避できる）
-	[ ] Aruba 1930の初期設定（§4）
-	[ ] 3台のBIOS設定（§5-1）とOSのインストール（§5-2・§5-3。VyOSのISOはMacの `~/Downloads/home-router-iso/` に取得済みで、sha256は `vyos/VERSION` と一致）
-	[ ] PC01の内蔵ディスクの型番と容量を確認する
-	[ ] 長時間の試験では、SFF筐体の中のNICの発熱を確認する

### ソフトウェア（Claude Codeの担当。OSのインストールが終わってから）

-	[ ] 管理系の疎通とSSH鍵の登録を確認する
-	[ ] NICの情報（型番、ドライバ、PCIeのリンク）を取って §2 を更新する
-	[ ] netplan、sysctl、監視、NTPを設定する（§5）
-	[ ] VyOSのフェーズ1を投入し、`baseline.cmds` を保存する
-	[ ] 疎通確認のチェックリスト（10Gで両方リンクしている、PCIeのリンク、v4/v6のping、`ping -M do -s 1472`、Prometheusの対象が全部UP、chrony）
-	[ ] T0〜T8を実施する
-	[ ] MAP-Eのポートセット全数利用（§6-3）、VyOSとNixOSの比較（§7）

### 未決定・要確認

-	本番回線の種別（ネクスト / クロス）と、ひかり電話の有無。DHCPv6-PDとNDプロキシのどちらを使うか、DSCPの扱いが変わる。
-	v6プラスのCEアドレスのIIDの形式。非公開のドラフト版の形式なので、実機で観測するか、コミュニティの計算方法と照合する。
-	QoSの set-dscp が、NGNの実回線で効くかどうか。
-	光クロスでのDHCPv6-PDの挙動。

---

§10 参考資料
------------

### このプロジェクトで作ったもの（claude.ai上）

-	10G VyOSルータ検証キット（ドキュメント。VyOSの設定、環境構築手順、実験手順書の3タブと構成図）: https://claude.ai/code/artifact/8ab86049-14f0-443e-abe8-d872b2fa9925
-	VyOS v6プラス設定ノート（設定項目の一覧とポートセット計算機）: https://claude.ai/artifact/37T6WdL7EpdraBW4HGthm2
-	10G VyOSルータ検証 構築・試験手順（構築の段階とT0〜T8をチェックリストで並べたもの）: https://claude.ai/artifact/DCGKyZLGHGLsLrFa6J3xqw
-	VyOS rolling を使う理由（Streamの調査結果を含む、2026-10-21）: https://claude.ai/artifact/1ttpnf7h2TMQkv1cMZ2cR3

### 外部の資料

-	『徹底解説v6プラス』JPIX公開PDF: https://www.jpix.ad.jp/files/v6plus-ebook.pdf
	-	4章: MAP、PSID、EAビット
	-	6.2節: ひかり電話の有無で変わる構成
	-	6.3節: 固定IP（IPIP）
	-	8章: トラブルシューティング
-	VyOS Performance Test Report: https://performance.vyos.dev/
-	VyOSのQoSで set-dscp を実装した変更: https://vyos.dev/T8996 / https://github.com/vyos/vyos-1x/pull/5305 （コミット a726455aa は2026-07-28作成、rollingへのマージは2026-08-06）
-	nekko-lab/vyos（旧WireGuardのDSCPパッチ）: https://github.com/nekko-lab/vyos
-	UDP pkt/s performance issue（VyOSフォーラム）: https://forum.vyos.io/t/udp-pkt-s-performance-issue/10222
-	Anyone using vyos for a 10G Router（VyOSフォーラム）: https://forum.vyos.io/t/anyone-using-vyos-for-a-10g-router/4113
-	OpenWrtでMAP-Eのポートセットを全部使う: https://zenn.dev/wistnki/articles/784d33e3c8a17c
-	x86_64マシンを10GルータにしてMAP-Eする: https://scrapbox.io/chun37/x86_64マシンを10GルータにしてMAP-Eする
-	NGNとDSCP（SSHの事例）: https://tsutsui.hatenablog.com/entry/ar1652866
-	Aruba Instant On 1930 管理ガイド: https://download.axilogi.com/HPE/Manual/InstantOn_1930_MCG.pdf
-	VyOSのEthernet設定（dhcpv6-options）: https://docs.vyos.io/en/latest/configuration/interfaces/ethernet.html
