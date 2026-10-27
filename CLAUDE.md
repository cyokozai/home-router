10G VyOSルータ検証 引き継ぎ資料
===============================

-	最終更新: 2026-10-17
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

1.	**管理系（自宅LAN 192.168.2.0/24、オンボードの1G。PC02はVRF MGMT）の設定は変更しない。** 変更が必要なときは事前にYusukeに確認する。SSHが切れると、物理コンソールでの復旧が必要になる。
2.	**VyOSでは原則 `commit-confirm 5` を使う。** 確認後に `confirm` → `save`。管理系に影響しうる変更は必ずこの手順にする。スクリプト（`vbash -s` と `script-template`）から使うときは、`commit-confirm 5` の**次の行に `y`** を書き、全体を `timeout 60` で包む。`commit-confirm` は「Proceed ? [Y/n]」と聞き、答えが無いと同じ質問を出し続けて止まらない（2026-10-25 実害: 出力が 5GB を超えて強制終了し、PC02 に処理が残った）。`confirm` は別の接続から鍵で入り直せることを確かめてから行う。
3.	**変更の前後で設定を保存する。** VyOSは `show configuration commands`、Ubuntuは対象ファイルを、このリポジトリの `configs/<host>/` に保存してgitにcommitする。
4.	**試験の結果は `results/<試験ID>/` にJSONとログで保存する。** 手入力で丸めた数字だけを残さない。
5.	**ハードウェアの作業が必要になったら止まって依頼する。** BIOS、再インストール、配線、NICの差し替えなどは、Yusukeへの依頼文を出して待つ。
6.	**10Gの経路でSSHや監視をしない。** 計測の邪魔になるため。
7.	**VyOSへスクリプトから設定を投入するときは `scripts/vyos-apply.sh` を使う。** VyOS rollingの `set` や `commit` は、失敗しても終了コード 0 を返す（2026-10のrollingで確認）。このスクリプトは、設定セッションの状態（各行が反映されたか、commit後に未反映の差分が残っていないか）で成否を判定し、拒否は 2、commitの失敗は 3 で返す。実機のPC02へは `scripts/pc02-commit.sh`（`commit-confirm 5` → 鍵のSSHを確かめて `confirm`、戻すときは保存済みの設定を `load`）を使う。
8.	**OSインストールの依頼文には、インストーラが出す質問をすべて答えと一緒に書く。** 特にRAID・インストール先の選択・「全データを消す」の確認。インストール先は容量と型番で特定させ、起動用以外のUSBメモリ・外付けディスクは先に抜いてもらう（2026-10-22 実害: 書き漏らしたため、PC02のVyOSインストーラが起動中のVentoy USBをRAID-1の相手に選び、ユーザーのUSBを上書きした）。
9.	**ディスク装置を直接読み書きする操作（dd・diskutil erase・mkfs・パーティション操作・Ventoy2Diskなど）は自分で実行しない。** `bash-guard.sh` が拒否する。ユーザーに渡すときは、対象の装置・消えるもの・使う容量と時間・より軽い代わり（直接読む、公式から取り直す）を先に示し、ユーザーの道具（VentoyはWindowsのRufus）を確かめてからにする。
10.	**本番で使う機能は、VyOSの `set` だけで設定でき、実機のPC02で動くことを確かめる（必須条件、2026-10-27）。** 機能ごとに「setで書けるか → commitが通るか → パケットを見て効くか」の3段で確かめる。setだけでできない機能は、シェル・生のnftables・起動スクリプトで補わず、根拠付きで報告してYusukeの判断を仰ぐ。

### このリポジトリのgit運用

-	既定ブランチは `king's`（`dev` は無い）。分岐は `origin/king's` から切る。
-	通常の作業ツリーではgitの書き込みが `bash-guard.sh` で拒否される。ルール3・4のcommitは **worktreeの中で** 行い、PRにしてYusukeがGitHub上でマージする。
-	試験中に取った設定・結果は、まず作業ツリーの `configs/` `results/` に書き出し、区切りのよいところでworktreeからまとめてcommitする。
-	`.claude/worktrees/` は `.gitignore` 済み。

---

§2 機材一覧
-----------

実機で確かめた値（2026-10-25、各機の `configs/<host>/inventory.txt`）。

| ID       | 機種                                       | CPU / メモリ / ディスク                                                         | OS                                          | 役割                                                            |
|----------|--------------------------------------------|---------------------------------------------------------------------------------|---------------------------------------------|-----------------------------------------------------------------|
| **PC01** | HP ProDesk 600 G3 SFF                      | Core i5-7500（4コア4スレッド）/ DDR4-2400 8GB **1枚** / SATA SSD 238.5GB        | Ubuntu 24.04.2 Server（カーネル 6.8.0-147） | 送信側の端末                                                    |
| **PC02** | HP ProDesk 400 G7 SFF                      | Core i5-10500（6コア12スレッド）/ DDR4-2400 8GB **1枚** / ADATA SU650 223.6GB  | VyOS 2026.10.16-0024-rolling（6.18.55）     | **ルータ本体**。このまま本番ルータにする予定で、入れ替えはしない |
| **PC03** | MSI MAG B550 TOMAHAWK（MS-7C91）           | Ryzen 7 5700G（8コア16スレッド）/ DDR4 64GB / NVMe 953.9GB                     | Ubuntu 24.04.5 Server（カーネル 6.8.0-147） | 受信側の端末、MAP-EのBR役、監視（Prometheus）、NTPサーバ        |
| **SW**   | Aruba Instant On 1930（24ポート＋SFP 4口） | ―                                                                               | ―                                           | 自宅のRTX830につないだ1Gスイッチ（管理系）                      |

### NIC

| 機器 | 10G NIC | ドライバ / FW | PCIe | 10G で使うポート（MAC） | 管理系 |
|---|---|---|---|---|---|
| PC01 | Intel X710 2ポート | i40e / 9.00 | 8GT/s x8 | `enp1s0f0np0` → `sfp0`（`6c:fe:54:6d:f2:60`） | `eno1`（I219-LM） |
| PC02 | Intel X710 2ポート | i40e 2.30.18 / 9.00 | 8GT/s x8 | `eth1`（PC01側、`6c:fe:54:6e:27:a0`）、`eth2`（PC03側、`…:a1`） | `eth0`（I219-LM、`b0:22:7a:1e:20:6b`） |
| PC03 | Broadcom BCM57840 4ポート | bnx2x / 7.6.55 | 8GT/s x8 | `enp16s0f3` → `sfp0`（`0c:c4:7a:eb:56:2e`）。他の3ポートは未配線 | `enp34s0`（Realtek RTL8111、1G） |

-	10Gはすべて10000Mb/sでリンクしている。リングバッファの上限は、X710が8160、BCM57840が**4078**（4096は拒否される）。
-	キュー数（Combined）は、PC01が4、PC02が12、PC03が4。

### 機材ごとの注意点

-	**PC01（600 G3）**: 4コア4スレッドで、メモリは1枚挿し（シングルチャネル）。64Bの試験（T3）では、ルータより先に送信側が頭打ちになる可能性がある。T3の前に送信能力の上限を必ず測る。
-	**PC02（400 G7 SFF）**: 拡張スロットはx16が1本とx1が1本。NICはx16側に挿してある（x1だと約7.9Gbpsが上限）。メモリが1枚挿しなので、8GB×2枚にしてデュアルチャネルにすると、64Bパケットの試験で差が出る可能性がある。VyOSのインストール時にRAID-1を勧められたら `n`（§5-2）。
-	**PC03（5700G）**: APUのため、PCIeは**Gen3止まり**（NICは8GT/s x8で足りている）。オンボードLANは2口とも**Realtek**（1Gの `enp34s0` を管理系に使う。2.5Gの `enp42s0` は未使用）。**IOMMUを有効にしていると、BCM57840で `AMD-Vi: IOTLB_INV_TIMEOUT` が出続けて起動が進まない**ので、BIOSで無効にした（MSIでは OC → CPU Features → IOMMU）。BIOSがCPPCの情報を渡さないため、cpufreq（`amd_pstate`）が無く、CPUの周波数はBIOS任せ。
-	**SW（Aruba 1930）**: 自宅のRTX830にぶら下げて、管理系のスイッチとして使う。今回の10G経路はPC同士の直結なので、右端のSFP+は使わない。

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
    └──── 管理系 1G（Aruba 1930 → RTX830、192.168.2.0/24）────┘
```

| 区間                      | IPv4                                              | IPv6                                 | PC02側のIF             |
|---------------------------|---------------------------------------------------|--------------------------------------|------------------------|
| PC01 ⇄ PC02（LAN）        | 192.168.10.0/24（PC02 .1 / PC01 .10）             | 2001:db8:10::/64（::1 / ::10）       | `eth1`（SFP+ #1）      |
| PC02 ⇄ PC03（WAN、NGN役） | 192.168.20.0/30（PC02 .1 / PC03 .2）              | 2001:db8:20::/64（::1 / ::2）        | `eth2`（SFP+ #2）      |
| 管理系                    | 192.168.2.0/24（自宅LAN、RTX830のDHCP）           | 自宅LANのまま                        | `eth0`（オンボード1G、VRF MGMT） |
| MAP-E模擬                 | CE 192.0.2.18/32、PC03の外側アドレス 198.51.100.1 | CE 2001:db8:20::c、BR 2001:db8:20::b | `tun0`                 |

### 管理系のアドレス

管理系は自宅LAN（RTX830配下、192.168.2.0/24）で、アドレスはRTX830のDHCPが配る（2026-10-24 決定。当初の閉じた 172.16.0.0/24 はやめた）。インターネットに出られるので、パッケージはいつでも入れられる。作業用のMacは自宅のWi-Fiから届く。

| 機器                                   | IP（DHCP）     | 管理系のIF |
|----------------------------------------|----------------|------------|
| PC01                                   | 192.168.2.190  | `eno1`     |
| PC02（VyOS）                           | 192.168.2.14   | `eth0`（VRF MGMT） |
| PC03                                   | 192.168.2.13   | `enp34s0`  |
| 作業用Mac（Claude Code）               | 192.168.2.3（Wi-Fi） | ―    |

-	**アドレスが変わらないよう、RTX830のDHCPで各機のMACに固定割り当てを入れる**（未設定。§9）。変わった場合は `~/.ssh/config`・PC03のPrometheus・chronyの参照先を直す。
-	**PC02は管理系をVRF MGMTに分けている**（§6-6）。DHCPの既定経路はMGMTの経路表にだけ入り、試験用の既定経路（main、192.168.20.2向け）と混ざらない。PC01・PC03は試験用の経路を個別に書いているので、管理系に既定経路があっても問題ない。
-	containerlab の検証環境（`lab/`）とCIは、引き続き 172.16.0.0/24 を管理系として使う（`vyos/phase1.cmds` の管理系の行）。

### containerlab の管理系（`lab/`・CI）

| 機器         | IP            |
|--------------|---------------|
| 管理系       | 172.16.0.0/24 |
| PC01         | 172.16.0.11   |
| PC02（VyOS） | 172.16.0.12   |
| PC03         | 172.16.0.13   |

### SSHの設定（作業用Macの `~/.ssh/config`）

```
Host pc01
    HostName 192.168.2.190
    User cyokozai
Host pc03
    HostName 192.168.2.13
    User cyokozai
Host pc02 vyos
    HostName 192.168.2.14
    User vyos
Host pc01 pc02 pc03 vyos
    IdentityFile ~/.ssh/home-router-lab_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 30
```

-	鍵は検証環境専用の `~/.ssh/home-router-lab_ed25519`（2026-10-24 作成）。PC01・PC03には `ssh-copy-id`、PC02にはVyOSの設定（`system login user vyos authentication public-keys claude-lab`）で登録してある。
-	PC01・PC03の `cyokozai` は、`/etc/sudoers.d/90-lab` でパスワードなしの sudo を許している（Claude CodeがSSHで設定するため）。
-	PC02はSSHのパスワードログインを止めてある（`service ssh disable-password-authentication`）。物理コンソールはパスワードで入れる。

### 検証環境の仮想再現（`lab/`）

`lab/home-router.clab.yml` が、この節の構成（PC01 ⇄ PC02 ⇄ PC03の10G経路と管理系）をcontainerlabで再現する。実機が揃う前に、設定と手順を確かめるためのもの。性能は測れない。

| ノード | イメージ                                       | 10G側の設定                                                                                       |
|--------|------------------------------------------------|---------------------------------------------------------------------------------------------------|
| pc01   | netshoot（`nicolaka/netshoot`、タグ固定）      | `lab/pc01.sh`（§5-3のnetplanと同じアドレスと経路）                                                |
| pc02   | VyOSコンテナ（`home-router/vyos:rolling`）     | `vyos/*.cmds` を `scripts/vyos-apply.sh` で投入する（§6）。トポロジーにはアドレスを書かない       |
| pc03   | netshoot（pc01と同じ）                         | `lab/pc03.sh`（§5-3の注記のアドレスと戻り経路）。フェーズ2のBR役は `lab/pc03-br.sh` を後から流す |

-	`tests/test_lab_topology.py` が、トポロジーと端末用のスクリプトが §3・§5-3・§6 と一致しているかを静的に検査する（コンテナは起動しない）。
-	**labの管理系は実機と同じ 172.16.0.0/24（各ノードの管理アドレスも上の表と同じ）。実機の管理系につながったマシンでは起動しない。** 起動はCIで行う。
-	VyOSのイメージ（`scripts/build-vyos-image.sh` が `vyos/VERSION` の版のISOから作る）はamd64のみ。arm64のMacではpc02が起動しない。
-	BR役のipip6には、ホストのカーネルの ip6_tunnel モジュールが要る。
-	labを起動して疎通を確かめるCIは、別のPRで追加中。

---

§4 Aruba 1930の初期設定（Yusukeの作業）
---------------------------------------

1.	中古や使い回しの機器なら、リセット穴を**5秒以上**押して初期化する。
2.	ノートPCを `192.168.1.2/24` に固定し、DHCPのない状態で `http://192.168.1.1` を開く。
3.	最初の画面で**ローカル管理（CONNECT）**を選ぶ。ユーザー名は `admin`、パスワードは空欄。初回に変更を求められる。
4.	管理用IPはRTX830のDHCPに任せる（2026-10-24 に管理系を自宅LANへ変えたため。固定するならRTX830側で割り当てる）。
5.	SNTPのサーバは、PC03（192.168.2.13）を指定する。

いまはスイッチとして使うだけなので、Web画面での設定は必須ではない（2026-10-25 時点で未設定のまま動いている）。

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
-	インストールはYusukeが行う。USBから起動 → `install image` → `reboot` と打って**画面が暗くなってから**USBを抜く（先に抜くと、終了処理の最後で止まることがある。その場合は電源ボタンの長押しで切ってよい）。
-	**CIで使う版**: CI（§6の冒頭）でcommitを検査するVyOSコンテナの版は、`vyos/VERSION` で固定している（vyos-nightly-buildのリリース名とISOのsha256など）。実機のPC02も同じ版。nightlyのリリースは直近30本程度しか残らないので、ISOは手元に保存しておく。版を上げるのは手で行い、外から自動で上げる仕組み（自動PRなど）は作らない（2026-10-19 決定、PR #6 は close）。
-	再起動したら、物理コンソールで次を入れる（`interfaces` と `eth0` のあいだの `ethernet` を落とさない）。

```
configure
set interfaces ethernet eth0 address dhcp
set service ssh
commit ; save
run show interfaces
```

-	鍵は、作業用MacからVyOSのパスワードで1回だけ入って、設定として登録する（コンソールで鍵を手打ちしない）。

```
ssh pc02 vbash -s <<'EOF'
source /opt/vyatta/etc/functions/script-template
configure
set system login user vyos authentication public-keys claude-lab type 'ssh-ed25519'
set system login user vyos authentication public-keys claude-lab key '<~/.ssh/home-router-lab_ed25519.pub の本体>'
commit
save
exit
EOF
```

-	ここから先はClaude CodeがSSHで行う: パスワードログインの停止 → 管理系のVRF分け（§6-6）→ フェーズ1（§6-1）→ `show configuration commands` を `/config/baseline.cmds` と `configs/pc02/baseline.cmds` に保存。

### 5-3. PC01 / PC03: Ubuntu 24.04 LTS Server

**インストール（Yusukeの作業）**

-	PC01は内蔵ディスク、PC03は内蔵NVMe（M2_2）の全体を使う。「Install OpenSSH server」にチェックする。ユーザー名 `cyokozai`、ホスト名 `pc01` / `pc03`。ネットワークの画面では、オンボード1GはDHCPのまま、10GのポートはDisabledのままでよい。
-	PC03は、インストーラが `AMD-Vi: IOTLB_INV_TIMEOUT` で進まなくなったら、BIOSでIOMMUを無効にする（§2）。
-	インストール後、作業用Macから `ssh-copy-id -i ~/.ssh/home-router-lab_ed25519.pub pc01`（pc03も）で鍵を入れ、次でパスワードなしのsudoを許す（ここまでYusukeの作業）。

```
ssh -t pc01 "echo 'cyokozai ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/90-lab && sudo chmod 440 /etc/sudoers.d/90-lab"
```

**パッケージ（Claude Codeの作業）**: 管理系からインターネットに出られるので、そのまま入れる。

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

**管理系**: インストーラが作ったDHCPの `/etc/netplan/50-cloud-init.yaml` のまま触らない（§1 ルール1）。

**netplan（10G側。Claude Codeの作業）**: PC01の例。`/etc/netplan/60-10g.yaml`（chmod 600）に置く。PC03は `192.168.20.2/30`、`2001:db8:20::2/64` にし、`192.168.10.0/24 via 192.168.20.1` などの戻り経路を入れる。実際に入れたファイルは `configs/pc01/etc/`・`configs/pc03/etc/` にある。

```yaml
network:
  version: 2
  ethernets:
    sfp0:
      match: {macaddress: "6c:fe:54:6d:f2:60"}
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

あわせて `sudo cpupower frequency-set -g performance`、`sudo ethtool -G sfp0 rx 4096 tx 4096`（PC03のBCM57840は上限の4078）、`sudo ethtool -A sfp0 autoneg off rx off tx off`（フロー制御を切る。pps試験で送信側が絞られないように）を実行する。どれも再起動で戻るので、起動時に流すsystemdのユニット `10g-tune.service` にまとめてある（PC03はcpufreqが無いので、cpupowerの失敗は無視する）。node-exporterは、DHCPのアドレスが付く前に起動するとbindに失敗するので、`network-online.target` を待ってから起動し、落ちたら再試行する（`configs/common/`）。

**PC03だけ: BR役（フェーズ2で使う）**

```
sudo ip -6 addr add 2001:db8:20::b/64 dev sfp0
sudo ip -6 tunnel add br0 mode ipip6 local 2001:db8:20::b remote 2001:db8:20::c encaplimit none
sudo ip link set br0 up mtu 1460
sudo ip addr add 192.0.2.1/32 dev br0
sudo ip route add 192.0.2.18/32 dev br0
sudo ip addr add 198.51.100.1/32 dev lo    # インターネット役。iperf3 -s -B 198.51.100.1
```

実際のBRのように、割当範囲外のポートを捨てる動作も再現したい場合は、nftablesで「宛先ポートが8160-8175以外ならdrop」というルールを足す。

**PC03だけ: 監視とNTP**

-	Prometheus: `scrape_interval: 1s`。対象は `192.168.2.190/14/13:9100`。node-exporterは各機の管理系のアドレスだけで待ち受ける（PC02はVRF MGMTの中）。
-	Grafanaは、公式のAPTリポジトリを追加してから入れる（未導入）。
-	chrony: 上流はインターネットの既定のプール。`allow 192.168.2.0/24`、`local stratum 10` でNTPサーバにする。PC01、PC02、Arubaはこれを参照する。
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

**設定の置き場所と検査**

この節のコマンドは、そのまま投入できる形で `vyos/*.cmds` にファイル化してある（§6-5は本番向けなので対象外）。

| ファイル                         | 対応する節               |
|----------------------------------|--------------------------|
| `vyos/phase1.cmds`               | §6-1（基準。baseline）   |
| `vyos/tuning-1-offload.cmds`     | §6-2の①                  |
| `vyos/tuning-2-rps.cmds`         | §6-2の②                  |
| `vyos/tuning-3-flowtable.cmds`   | §6-2の③                  |
| `vyos/tuning-4-performance.cmds` | §6-2の④                  |
| `vyos/phase2-mape.cmds`          | §6-3                     |
| `vyos/qos-ngn-out.cmds`          | §6-4                     |

-	`tests/test_vyos_cmds.py` が、この節のコードブロックの `set` / `delete` 行がすべて対応するファイルに入っていることを検査する（ファイル側は「eth2も同様」「ipv6も同様」で省いた行も書き下してある）。
-	`.github/workflows/vyos-config.yaml` が、`vyos/VERSION`（§5-2）で固定したVyOS rollingのコンテナで、§6-1を基準にして各差分を1本ずつ入れ、commitできるかを検査する。QoSの差分では、eth2のtc filterにpeditが入ったことまで見る。性能と§6-5は検査しない。
-	コンテナのvethでは確かめられない行（§6-2①のring-bufferの4行と、④の `system option performance`）は、`vyos/container-unsupported.txt` に理由つきで載せ、CIではその行を除いて検査する。実機（PC02）では全行を投入する。
-	**運用ルール: この節のコマンドを書き換えたら、`vyos/*.cmds` も同じ内容に直す。** 食い違うとpytestが落ちる。
-	スクリプトから投入するときは `scripts/vyos-apply.sh` を使う（§1 ルール7）。

### 6-1. フェーズ1: 素のルーティング（FWもNATもなし）

管理系の4行（`eth0`・`ssh listen-address`・`ntp server`・`node-exporter listen-address`）は、containerlab と CI の管理系（172.16.0.0/24）に合わせた値。**実機のPC02では、このあと同じcommitで §6-6 を入れて管理系の行を置き換える**（2026-10-25 に実機へ投入済み。結果は `configs/pc02/baseline.cmds`）。

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
set interfaces tunnel tun0 parameters ipv6 encaplimit 'none'
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

-	`encaplimit 'none'` は必須。VyOSの既定は encaplimit 4 で、外側にDestination Optionsヘッダ（8バイト）が付く。外側のNext Headerが4ではなく60になり、トンネルの実効MTUも1452に下がる。実際のBRは素のIPIP（Next Header 4）を使う。
-	ポートの範囲は『徹底解説v6プラス』の表4.1にある例（a=4, k=8, PSID=0xfe）。使えるのは16ポート×15セットで、計240ポート。
-	**既知の課題**: VyOSのSNATは1ルールに1つのポート範囲しか書けない。単純にルールを並べると、最初の16ポートしか使われない。15セットすべてを使う方法として、(a) 送信元ごとにルールを分ける、(b) 生のnftablesでパケットにマークを付けてSNATを振り分ける（OpenWrtでの事例あり）、の2案を比べる。
-	**setだけでの答え（B2、2026-10-27 実機で確認）**: `vyos/mape-portsets.cmds`。LAN側の送信元ポートを16個ずつの塊に切り、塊の番号を15で割った余りで15本のルール（101〜115）に振り分ける（`source port` は `'1-15,240-255,…'` のような範囲の並びを受け付ける）。ルール101〜115は `scripts/gen_mape_portsets.py` が作る。
	-	送信元ポートを連続した15区間に分ける形は使えない。1台から同じ宛先へ張る接続はほぼ連番のポート（Linuxは2つおき）を使うので、全部が同じ区間＝同じ16ポートに入り、17本目で枯渇する。
	-	範囲の下端を0にしない。`source port '0-4368'` はcommitが通るのに、nftablesには送信元ポートの条件が書かれず、そのルールが全部を拾う。

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
set qos policy shaper NGN-OUT class 10 set-dscp 'default'
set qos policy shaper NGN-OUT default bandwidth '100%'
# NGNへ出る通信をすべて0にするなら次の行を追加
# set qos policy shaper NGN-OUT default set-dscp 'default'
set qos interface eth2 egress 'NGN-OUT'    # 検証環境ではWAN役のeth2。本番では本番のWAN側IF
```

-	**値の書き方**: DSCP 0 は `'default'` と書く。VyOS rollingの `set-dscp` は 0〜63 の数値か決まった名前の一覧しか受け付けず、一覧に CS0 は無いので、`set` の段階で拒否される（vyos-1x の `interface-definitions/include/qos/set-dscp.xml.i`）。

-	**注意**: 検証環境の `eth0` は管理系（§1 ルール1）。shaperを `eth0` に付けない。本番（§6-5）ではWAN側の名前が変わるので、そのときのWAN IFに読み替える。

-	確認: `sudo tc filter show dev <WAN>` に `pedit ... traffic_class` が付いているかを見る。filterはroot qdisc（HTB）側に付くので、`egress` を付けると何も表示されない。`tcpdump -v` で class 0x00 になっていることを確認する。

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

### 6-6. 実機の管理系: 自宅LANとVRF MGMT

実機のPC02では、§6-1 の管理系の行（containerlab用の 172.16.0.0/24）を、§6-1 と**同じcommitで**次に置き換える（`vyos/mgmt-home.cmds`）。別々にcommitすると、途中でSSHが 172.16.0.12 だけを待ち受けて切れる。

```
delete interfaces ethernet eth0 address '172.16.0.12/24'
delete service ssh listen-address '172.16.0.12'
delete service ntp server '172.16.0.13'
delete service monitoring prometheus node-exporter listen-address '172.16.0.12'
set vrf name MGMT table '100'
set interfaces ethernet eth0 address 'dhcp'
set interfaces ethernet eth0 vrf 'MGMT'
set service ssh vrf 'MGMT'
set service ssh disable-password-authentication
set service ntp server '192.168.2.13'
set service ntp vrf 'MGMT'
set service monitoring prometheus node-exporter vrf 'MGMT'
```

-	DHCPの既定経路はMGMTの経路表（table 100）にだけ入り、試験用の既定経路（mainの経路表、192.168.20.2向け）と混ざらない。
-	SSH・NTP・node-exporterはMGMTの中だけで待ち受けるので、10G側（192.168.10.1 など）からは届かない（2026-10-25 にPC01から22番・9100番とも届かないことを確認）。
-	`eth0` をVRFへ移すcommitで、SSHは一度切れて数秒で戻る。必ず `commit-confirm 5` で入れる（§1 ルール2）。

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

-	[x] 3台のOSのインストール（2026-10-24）。PC01のBIOSに入れない問題は解消した（原因の記録は未）
-	[x] 3台のNICの型番の確認（§2。PC01・PC02はX710、PC03はBCM57840）
-	[ ] RTX830のDHCPで、3台の管理系のMACに固定のアドレスを割り当てる（§3）
-	[ ] PC01・PC02のメモリが1枚挿し。必要ならデュアルチャネルにする（64Bの試験の前に判断）
-	[ ] 長時間の試験では、SFF筐体の中のNICの発熱を確認する

### ソフトウェア（Claude Codeの担当）

-	[x] 管理系の疎通とSSH鍵の登録（2026-10-25。3台とも `~/.ssh/home-router-lab_ed25519`）
-	[x] NICの情報（型番、ドライバ、PCIeのリンク）を取って §2 を更新（`configs/<host>/inventory.txt`）
-	[x] netplan、sysctl、監視、NTPの設定（§5。`configs/pc01/`・`configs/pc03/`）
-	[x] PC02の管理系をVRF MGMTに分け（§6-6）、フェーズ1を投入して `baseline.cmds` を保存（`configs/pc02/`）
-	[x] 疎通確認（2026-10-25）: 10Gは両区間とも10000Mb/s、PCIeは3台とも8GT/s x8、PC01→PC03のv4/v6 ping、`ping -M do -s 1472` は通り1473は止まる、Prometheusの対象3台ともup、NTPは3台ともPC03に同期、10G側からPC02の22・9100番に届かない
-	[ ] Grafanaの導入
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
