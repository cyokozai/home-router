本番接続テンプレートの設計と切り替え手順
=========================================

-	作成: 2026-11-05
-	前提: PC02（HP ProDesk 400 G7 SFF、Intel X710、VyOS `2026.10.16-0024-rolling`）を、自宅のRTX830の代わりに、新しく引くフレッツの回線（光クロス）へ直接つなぐ本番ルータにする。RTX830がつながっている今の回線は、しばらくそのまま残す。
-	この文書の扱い: 設計と手順。設定は `vyos/prod/` のテンプレートから `scripts/render_config.py` で生成する。
-	実行できる時期: §4 は §6 の I1 と I5、§5 は I1〜I4 ができてから。それまでは設計として読む。

---

§0 決まっていること・決めること
-------------------------------

### 決まったこと（2026-11-05）

| 項目 | 内容 |
|---|---|
| 本番の回線 | **光クロス 10G、ひかり電話なし、プロバイダは enひかり（v6プラス）**。WANは型A（ONU直結、DHCPv6-PDで/56）を使う。今の回線（RTX830）とは別の新しい回線で、2026-11-13にNTTが工事する。新しいONUをPC02の `eth2` につなぐ |
| テンプレートの範囲 | 型A（光クロス）・型B（ネクスト＋ひかり電話、HGWの配下でPD /60）・型C（ネクストで電話なし、RAで/64）の3通りを、siteのyamlで選べるようにする。型Cは実回線で確かめるまで選ばない（下の「決まったこと（2026-11-11）」） |
| WAN | X710の `eth2` に、10GBASE-TのSFP+モジュールを挿してONUにつなぐ（検証のときと同じ向き） |
| LAN | X710の `eth1`。**RTX830を置き換え**、家のLANのDHCPv4・DNS・RAをVyOSが配る |

### 決まったこと（2026-11-11）

| 項目 | 内容 |
|---|---|
| 型C（RAで/64、NDプロキシ） | **実回線で確かめるまで使わない**（2026-11-11 Yusuke決定）。本番は型A（光クロス、PDで/56）なので、型Cは予備のテンプレートとして残すだけにする。使うのは、ネクストで電話なしの回線で、`CLAUDE.md` §1 ルール10の3段（`set` で書けるか → commitが通るか → パケットを見て効くか）を実回線で通してから（§2-5） |
| 本番の管理系 | 管理はLANの 192.168.2.1 から行い、`eth0` は救出用の 192.168.255.1/24（ゲートウェイなし）にする（D2のとおり）。`CLAUDE.md` §1 ルール1を、検証環境（VRF MGMT）と本番の2つに分けて書き直した（2026-11-11 Yusuke決定） |
| 10Gの経路での管理と監視 | 本番では、LANの `eth1`（10G）からSSH・node-exporter・SNMPを使ってよい。`CLAUDE.md` §1 ルール6の「10Gの経路でSSHや監視をしない」は、計測（`docs/performance-evaluation.md` の試験）のあいだだけに限る（2026-11-11 Yusuke決定） |

### Yusukeに決めてほしいこと（推奨の値をテンプレートの既定にしてある）

| # | 決めること | 推奨 |
|---|---|---|
| D1 | 家のLANのアドレス | **192.168.2.0/24 のまま、VyOSを 192.168.2.1 にする**。PC01・PC03・thin-k8s（.50）の固定割り当てと、`~/.ssh/config`・Prometheus・SNMPの許可先をそのまま使える。RTX830も .1 なので、PC02の `eth1` を家のLANにつなぐのは、RTX830のLAN側を外してから（§5の段階C） |
| D2 | 管理系（VRF MGMT）の扱い | **本番ではVRF MGMTをやめる**。管理はLANの 192.168.2.1 から行い、入力フィルタでLANからだけ許す。`eth0`（オンボード1G）は**救出用の口**として残し、固定の `192.168.255.1/24` を持たせる（ゲートウェイなし。ノートPCを直結して入る）。切り替えの段階A・Bのあいだは、`eth0` を今のままArubaにつないでおき、作業用のMacに 192.168.255.x/24 の別名を足してこの口から入る（§5） |
| D3 | LANの物理配線 | `eth1`（SFP+）からAruba 1930のSFP+の口へ、10GのDACでつなぐ。Arubaの型番が「24G 4SFP/SFP+」（JL682A）であることを確かめる。1G専用の型ならSFP（1G）になる |
| D4 | WANのモジュール | §2-1 の表のとおり。光クロスは10GBASE-TのSFP+（Intel互換） |
| D5 | ULA（LAN内で固定のIPv6） | RFC 4193のランダムな /48 を1つ決める。DNSとRDNSSを、委任プレフィックスが変わっても変わらないアドレスで配るため（§3-2） |

---

§1 全体の考え方
---------------

### 1-1. 二段で入れる

本番の値（委任プレフィックス、MAP-EのIPv4アドレスとポートセット）は、**フレッツにつないでみるまで分からない**。そのため、設定を二段に分ける。

| 段 | 中身 | 値の出どころ |
|---|---|---|
| **固定の段** | 基本設定、LAN、WAN（回線の種別ごとに3通り）、ファイアウォール、DSCP、監視 | `vyos/prod/` のテンプレートとsiteのyaml。秘密の値は `@...@` の印にして、投入時に環境変数で置き換える |
| **実値の段** | MAP-E（CEのIPv6アドレス、トンネル、NAT44の15ルール） | 接続後に観測したプレフィックスをsiteの `wan.prefix` に書き、`scripts/render_config.py --mape` が `40-mape` に描く（計算は `scripts/mape_calc.py`）。**グローバルアドレスなのでgitには入れない**（`CLAUDE.md` §1 ルール3） |

型CのNDプロキシとLANの/64は固定の段（`20-wan-ra64`）に入れ、RAで受けた/64とNGNのルータのリンクローカルを `@WAN_PREFIX64@`・`@NGN_GW_LLA@` の印で渡す。`mape_calc.py` はNDプロキシの設定を作らない。

### 1-2. 守るルール（`CLAUDE.md` から）

-	**ルール10**: 本番で使う機能は、VyOSの `set` だけで書ける形にする。シェル、生のnftables、起動スクリプトは使わない。
-	**ルール2・7**: 投入は `scripts/pc02-commit.sh`（`commit-confirm 5` → 鍵のSSHで入り直せるか確かめて `confirm`）。
-	**ルール3**: グローバルのIPv4・IPv6アドレス、SNMPのcommunityは、印に置き換えてから保存する。

### 1-3. 検証で決まった本番の形

`docs/performance-evaluation.md` で「本番に近い構成」とした組み合わせをそのまま使う。

-	①オフロードとリング4096 ＋ ステートフルFW ＋ flowtable ＋ **firewallの `set dscp`**（B1）
-	DSCPの書き換えはQoSのshaper（HTB）を使わない（PDRが48%に落ちる）。RPSも入れない（51%）。
-	MAP-Eはポートセット15組への振り分け（B2、`vyos/mape-portsets.cmds` の形）。

---

§2 WAN側の設計（回線ごとの3通り）
---------------------------------

### 2-1. 物理層

| 回線 | つなぐ先 | 速度 | `eth2` に挿すもの |
|---|---|---|---|
| **光クロス（本番）** | ONUの10GBASE-Tの口 | 10G | 10GBASE-TのSFP+モジュール（**Intelの互換コードのもの**） |
| ネクスト＋ひかり電話 | HGWのLANの口 | 1G | **1000BASE-TのSFPモジュール**（Intelの互換コード）。10GBASE-T用のSFP+は、1Gでリンクしない製品が多い |
| ネクストで電話なし | ONUの口 | 1G | 同上 |

-	**X710はインテル純正（互換コード）以外のモジュールを拒否する**。`dmesg` に「unsupported SFP module type」と出たら、これが原因。買う前に「Intel互換」と明記されたものを選ぶ。
-	10GBASE-TのSFP+モジュールは、消費電力が2〜3Wあって高温になる。SFF筐体の中で、隣のポート（`eth1` のDAC）との間隔と発熱を確かめる。

### 2-2. 共通の設定（3通りとも）

```
set interfaces ethernet eth2 description 'WAN-NGN'
set interfaces ethernet eth2 ipv6 address autoconf
set interfaces tunnel tun0 parameters ipv6 tclass '0x00'
```

-	`autoconf` は、RAで既定経路を受け取るために要る（転送が有効なLinuxでもRAを受けるよう、VyOSが `accept_ra=2` にする）。
-	`tclass '0x00'` は、トンネルの外側のIPv6のDSCPを0に固定するもの。内側のIPv4のDSCPがNGNに見えなくなる（`mape_calc.py` の出力にも同じ行が入っている）。

### 2-3. 型A: 光クロス（ONU直結、DHCPv6-PDで/56）— 本番

```
set interfaces ethernet eth2 dhcpv6-options pd 0 length '56'
set interfaces ethernet eth2 dhcpv6-options pd 0 interface eth1 sla-id '1'
set interfaces ethernet eth2 dhcpv6-options pd 0 interface eth1 address '1'
```

-	LANには、委任された/56の中の2番目の/64（サブネットID 1）を割り当てる。
-	**サブネットID 0の/64には、MAP-EのCEアドレスを置く**（v6プラスの仕様。`mape_calc.py` がWAN側の `eth2` に置く）。LANにサブネットID 0を使わないのはこのため。
-	光クロスでPDが本当に/56で来るかは、まだ実機で確かめていない。§5の段階Aで確かめる。

### 2-4. 型B: ネクスト＋ひかり電話（HGWの配下、PDで/60）

```
set interfaces ethernet eth2 dhcpv6-options pd 0 length '60'
set interfaces ethernet eth2 dhcpv6-options pd 0 interface eth1 sla-id '1'
set interfaces ethernet eth2 dhcpv6-options pd 0 interface eth1 address '1'
```

-	WANの `eth2` は、HGWのLAN（HGWがRAで流す、/56の先頭の/64＝サブネットID 0）に入る。CEアドレスもこの/64に置くことになるので、`mape_calc.py` の計算（/56のサブネットID 0）と一致する。`mape_calc.py` には、WANで観測した/64をそのまま渡せばよい。
-	**HGW側のv6プラスのIPv4を無効にする（必須）**。ひかり電話のHGWには、JPNEソフトウェアが自動で入り、HGW自身がMAP-Eで動く。VyOSも同じIPv4アドレスとポートセットでMAP-Eを始めると、2台が同じポートを取り合って壊れる。HGWの `http://ntt.setup:8888/t/` →「IPv4設定」で、IPv4通信を無効にする（『徹底解説v6プラス』7.2節）。
-	HGWが/60を再委任するかどうかは、機種によって違う。/60が来なければ/64を試す。

### 2-5. 型C: ネクストで電話なし（RAで/64、NDプロキシ）

**実回線で確かめるまで、この型は使わない（2026-11-11 Yusuke決定）。** 本番は型Aなので、型Cは予備のテンプレートとして残す。使うことになったら、実回線で `CLAUDE.md` §1 ルール10の3段（`set` で書けるか → commitが通るか → パケットを見て効くか）を通してから入れる。§4のR-Cの予行演習が通っても、実回線での確認の代わりにはしない。

この型はPDが使えない。家のLANとWANが1つの/64を分け合うことになる。`@WAN_PREFIX64@` などは接続後に観測した値で置き換える。

```
delete interfaces ethernet eth2 ipv6 address autoconf
set interfaces ethernet eth1 address '@WAN_PREFIX64@::1/64'
set protocols static route6 ::/0 next-hop '@NGN_GW_LLA@' interface 'eth2'
set service ndp-proxy interface eth2 prefix '@WAN_PREFIX64@::/64' mode 'static'
```

-	WANの `eth2` には/64を持たせない（SLAACを切る）。同じ/64をWANとLANの両方に持つと、経路が衝突するため。CEアドレスだけを `/128` でWANに置く（`mape_calc.py --ce-plen 128`）。
-	既定経路は、RAを送ってくるNGNのルータのリンクローカルアドレス（`@NGN_GW_LLA@`）へ固定で向ける。値は段階Aで `show ipv6 route` か `rdisc6 eth2` から取る。
-	NDプロキシは、NGNからのNeighbor Solicitationに、LAN側の端末の代わりに答える。
-	**この型は設計上の仮説の段階で、まだ試していない**。§4の閉域網での予行演習で先に確かめ、そのうえで実回線でルール10の3段を通す（この節の冒頭）。
-	/64の第4ヘクステットの下位8ビットが0でないと、CEアドレスが自分の/64の外に出る。そのときは `mape_calc.py` が警告を出す。

---

§3 LAN・ファイアウォール・サービスの設計
---------------------------------------

### 3-1. 基本

```
set system host-name 'home-router'
set system option performance 'network-throughput'
set system conntrack table-size '1048576'
set system conntrack hash-size '262144'
set service ntp server 'ntp.nict.jp'
set service ntp server 'time.google.com'
set interfaces ethernet eth0 description 'RESCUE'
set interfaces ethernet eth0 address '192.168.255.1/24'
# ①オフロードとリング（eth1・eth2）は vyos/tuning-1-offload.cmds の行をそのまま使う
```

-	conntrackの表は、MAP-Eで外へ出られるのが240ポート分しかないので、既定の262144件でも足りる可能性が高い。N1・N2の試験の結果を見て、大きさを決める。

### 3-2. LAN

```
set interfaces ethernet eth1 description 'LAN-10G-to-Aruba'
set interfaces ethernet eth1 address '192.168.2.1/24'
set interfaces ethernet eth1 address '@ULA_PREFIX@::1/64'
set service dhcp-server shared-network-name LAN subnet 192.168.2.0/24 subnet-id '1'
set service dhcp-server shared-network-name LAN subnet 192.168.2.0/24 option default-router '192.168.2.1'
set service dhcp-server shared-network-name LAN subnet 192.168.2.0/24 option name-server '192.168.2.1'
set service dhcp-server shared-network-name LAN subnet 192.168.2.0/24 range 0 start '192.168.2.100'
set service dhcp-server shared-network-name LAN subnet 192.168.2.0/24 range 0 stop '192.168.2.199'
# RTX830 の固定割り当てを static-mapping に移す（PC01 .190、PC03 .13、thin-k8s .50 など）
set service dns forwarding listen-address '192.168.2.1'
set service dns forwarding listen-address '@ULA_PREFIX@::1'
set service dns forwarding allow-from '192.168.2.0/24'
set service dns forwarding allow-from '@ULA_PREFIX@::/64'
set service dns forwarding allow-from '::/0'
set service dns forwarding dhcp 'eth2'
set service router-advert interface eth1 prefix '::/64'
set service router-advert interface eth1 name-server '@ULA_PREFIX@::1'
```

-	RDNSS（RAで配るDNSサーバ）とDNSの待ち受けは、**ULAのアドレスで固定**する。委任プレフィックスが変わっても、LANの端末のDNSの設定が壊れないようにするため（D5）。
-	`prefix '::/64'` は、`eth1` に付いているプレフィックス（PDで割り当てたGUAとULA）を自動で広告する。
-	`dns forwarding dhcp 'eth2'` で、DHCPv6でNGNから受けたDNSサーバを上流に使う。IPv4とIPv6が同じVNEから出るので、CDNの最寄り判定がずれにくい（『徹底解説v6プラス』2.4節）。
-	`allow-from '::/0'` で、LANの端末がGUAから問い合わせても答えられるようにする。待ち受けは192.168.2.1とULAだけで、どちらもインターネットからは届かない（GUAはプレフィックスに追従できないので、許可先には書かない）。
-	RTX830の固定割り当て、ポート開放、静的経路は、切り替えの前に書き出して移す（§5の段階0）。

### 3-3. ファイアウォール

転送（forward）は、`vyos/fw-stateful.cmds` の `eth1`（LAN）と `eth2`（WAN）の行に、IPv4の外側を `tun0` にした分を足す。flowtableは `vyos/tuning-3-flowtable.cmds`、DSCPは `vyos/dscp-firewall.cmds` の形。

**ルール番号の順番**

| 番号 | 中身 | 理由 |
|---|---|---|
| 5 | WAN（`eth2`）へ出るIPv6の、送信元UDP 51820 → `set dscp '0'`、accept（`outbound-interface name 'eth2'`） | **offloadより先に置く**。flowtableに載ったフローは、2パケット目以降がforwardのチェーンを通らない。先にoffloadされると、LANの端末が張るWireGuardの鍵の更新がDSCP 0x88のまま出ていく。先にacceptしたフローはoffloadされないので、毎回書き換わる。`outbound-interface` で出る向きを絞らないと、WANから来た送信元ポート51820の新しい通信が、rule 100〜120の判定より前に通ってしまう |
| 10 | established・relatedをoffload（flowtable） | |
| 100〜130 | `fw-stateful.cmds` の内容 | |

**入力（input）のフィルタ（ルータ自身を守る）**

```
set firewall ipv4 input filter default-action 'drop'
set firewall ipv4 input filter rule 10 action 'accept'
set firewall ipv4 input filter rule 10 state 'established'
set firewall ipv4 input filter rule 10 state 'related'
set firewall ipv4 input filter rule 20 action 'accept'
set firewall ipv4 input filter rule 20 protocol 'icmp'
set firewall ipv4 input filter rule 30 action 'accept'
set firewall ipv4 input filter rule 30 inbound-interface name 'eth1'
set firewall ipv4 input filter rule 40 action 'accept'
set firewall ipv4 input filter rule 40 inbound-interface name 'eth0'
set firewall ipv6 input filter default-action 'drop'
set firewall ipv6 input filter rule 10 action 'accept'
set firewall ipv6 input filter rule 10 state 'established'
set firewall ipv6 input filter rule 10 state 'related'
set firewall ipv6 input filter rule 20 action 'accept'
set firewall ipv6 input filter rule 20 protocol 'ipv6-icmp'
set firewall ipv6 input filter rule 30 action 'accept'
set firewall ipv6 input filter rule 30 protocol 'udp'
set firewall ipv6 input filter rule 30 source address 'fe80::/10'
set firewall ipv6 input filter rule 30 destination port '546'
set firewall ipv6 input filter rule 40 action 'accept'
set firewall ipv6 input filter rule 40 inbound-interface name 'eth1'
set firewall ipv6 input filter rule 50 action 'accept'
set firewall ipv6 input filter rule 50 protocol 'ipencap'
set firewall ipv6 input filter rule 50 source address '2404:9200:225:100::64'
```

-	rule 30はDHCPv6の応答（PD）、rule 50はMAP-EのBRから届くipip6のパケット。**これが無いと、PDもIPv4も止まる**。
-	ICMPv6を通さないと、RA、ND、PMTUDが壊れる。種類を絞るのは、まず全部を通して動くことを確かめてから。
-	拠点間のWireGuardを本番ルータで終端する場合は、UDP 51820を入力で許す行と、output filterのDSCP（`dscp-firewall.cmds` のoutputの行）を足す。

### 3-4. 監視

-	SNMP: `vyos/snmp-mgmt.cmds` から `vrf 'MGMT'` を外したもの。問い合わせ元は thin-k8s（192.168.2.50/32）だけ。入力フィルタはLANからのものを通すので、追加は要らない。
-	node-exporter: listen-addressを `192.168.2.1` に。PC03のPrometheusの対象を `192.168.2.14` から `192.168.2.1` に直す。
-	プレフィックスの変化をDiscordに通知する仕組み（`CLAUDE.md` §6-5）は、そのまま使う。

---

§4 閉域網での予行演習（フレッツにつなぐ前）
-----------------------------------------

**この節は、§6 の I1（テンプレートと生成器）と I5（PC03のNGN役）が揃ってから行う。** 今の `lab/pc03-br.sh` はMAP-EのBRだけで、PDサーバとRAの役は持たない。

今の検証環境（PC01 ⇄ PC02 ⇄ PC03）で、PC03に「NGN役」をさせて、テンプレートを先に通しておく。これで、当日の作業を「値を入れてcommitするだけ」にできる。本番は型Aなので、R-A・R-M・R-F・R-Dを優先する。

| 演習 | PC03（NGN役）でやること | PC02で確かめること |
|---|---|---|
| R-A | DHCPv6のPDサーバで、v6プラスのルールに入る/56を委任する。RAも流す | 型Aの設定で、PDを受けて `eth1` に/64が付き、LANにRAが流れる |
| R-B | PDで/60を委任する。RAで/56の先頭の/64を流す（HGWを模擬） | 型Bの設定で、`mape_calc.py` に渡す値がWANの/64から決まる |
| R-C | PDは止めて、RAで/64だけを流す | 型Cの設定で、NDプロキシが答え、LANの端末がPC03までIPv6で届く |
| R-M | `lab/pc03-br.sh` のBR役を、R-Aで委任したプレフィックスから計算した値で動かす | `render_config.py --mape` で描いた `40-mape` を入れて、IPv4がPC03まで届く。ポートが割当範囲に収まる |
| R-F | WAN側から、TCP 22、UDP 161、9100番に新しい接続を試みる | すべて落ちる。LANからは届く |
| R-D | LAN側の端末からDSCP 0x88でUDP 51820を送り続け、途中で鍵の更新に相当するパケットを混ぜる | flowtableを有効にした状態でも、WANに出るパケットがすべてDSCP 0になっている（§3-3の順番の確認） |

-	PC02は `configs/pc02/baseline.cmds` の状態から始め、§5と同じ順で入れる: 段階Aの前段（`eth0` に 192.168.255.1/24）→ 段階Aの本体（`--from-lab` で描いた `40-mape` 以外の全部を1回のcommit）→ 段階B（`--mape`）。SSHが切れる所と、`pc02-commit.sh` が途中で切れたときの確かめ方も、ここで先に通しておく。予行の後は `scripts/pc02-commit.sh --restore-saved` で基準に戻す。
-	containerlab（`lab/`）とCIにR-A〜R-Cの「NGN役」を足すと、テンプレートが壊れていないかを毎回自動で確かめられる。
-	使う委任プレフィックスはv6プラスのルール（240b:10::/31 など）に入る値にする。gitに残すのは、`tests/test_no_sensitive_data.py` が例として許している値だけにする。

---

§5 当日の切り替え手順
---------------------

段階ごとに確かめ、だめならすぐ戻せるようにする。**RTX830は設定を消さずに、今の回線につないだまま残しておく**。段階A・Bのあいだ、家のLANとインターネットはRTX830のまま動く。戻すときは、LAN側のケーブルを差し戻すだけで済む。

### 段階0: 前日まで

-	[ ] 新しい回線（光クロス）の工事が済み、ONUが届いている。enひかりのv6プラスが開通しているかは、ここでは確かめられない（RTX830の今の回線とは別の契約なので、RTX830がMAP-Eで出ていても根拠にならない）。段階Aで、委任されたプレフィックスがv6プラスの範囲に入るかで確かめる
-	[ ] RTX830の設定を書き出す（DHCPの固定割り当て、ポート開放、静的経路、DNSの設定）。siteのyamlのstatic-mappingに移す
-	[ ] WAN用の10GBASE-TのSFP+モジュールを用意する（§2-1）。`eth2` に挿して `ethtool -m eth2` でモジュールが読めることを確かめる
-	[ ] §4の予行演習が通っている
-	[ ] 印の値を決めておく（Yusukeの作業）。段階A・Bで `pc02-commit.sh` を流すシェルで、次を設定する。値はリポジトリにもこの文書にも書かない。不明な印・未設定・形の合わない値があれば、`pc02-commit.sh` はPC02に何も送らずに止まる（`scripts/placeholders.sh`）

```
export SSH_PUBKEY='<本番で使う鍵の公開鍵の本体（.pub の2つ目の欄）>'
export ULA_PREFIX='<D5で決めたULAの /64 の上位4組>'   # siteの lan.ula_prefix に書いたなら要らない
export SNMP_COMMUNITY='<SNMPのcommunity>'             # siteで snmp を有効にしたとき
```

### 段階Aの前段: 救出用のアドレスを先に足す（今の経路のまま）

段階Aの本体のcommitで、`eth0` のDHCPのアドレス（192.168.2.14）が消え、`eth0` はVRF MGMTから既定のVRFに移る。`scripts/pc02-commit.sh` はconfirmの前に `ssh pc02` で入り直せるかを確かめるので、`~/.ssh/config` の `pc02` が 192.168.2.14 のままだと通らない。そこで、先に救出用のアドレスを今の経路で足し、Macからそのアドレスで入れるようにしておく。

1.	`set interfaces ethernet eth0 address '192.168.255.1/24'` の1行だけのcmdsを作り、`scripts/pc02-commit.sh --save <そのファイル>` で入れる。confirmの確認は今の経路（`ssh pc02` = 192.168.2.14、VRF MGMT）で通る。
	-	`--save` を付ける。付けないと、段階Aの本体がconfirmされずに戻ったとき（`commit-confirm` の時間切れ）や `--restore-saved` で、このアドレスも消え、Macから入れなくなる。
	-	保存した後、`show configuration commands` を `configs/pc02/` に保存する（`CLAUDE.md` §1 ルール3）。
	-	今のSSHはVRF MGMTの中で `listen-address` を持たない（`configs/pc02/baseline.cmds` は `set service ssh vrf 'MGMT'` と `disable-password-authentication` だけ）。MGMTの中のアドレスすべてで待ち受けるので、MGMTにいる `eth0` に足した 192.168.255.1 でも待ち受ける見込み。
2.	Macから 192.168.255.1 に届くようにする。`eth0` はAruba（家のLANと同じL2）につながっているので、MacのそのL2につながるIFに別名を足す。IFの名前は `route -n get 192.168.2.1` の `interface:` で確かめる。Wi-Fiで入る場合は、アクセスポイントがブリッジとして家のLANと同じL2につないでいる必要がある。

```
sudo ifconfig en0 alias 192.168.255.2 255.255.255.0    # en0 は上で確かめたIF。段階Cで外す
ssh -o HostName=192.168.255.1 pc02 true                 # 鍵で入れること
```

3.	入れたら、`~/.ssh/config` の `pc02` の `HostName` を `192.168.255.1` に直す（Yusukeの作業）。以後、段階A・Bの `pc02-commit.sh` はこのアドレスで入り、確かめる。
4.	**段階Aに進む前に、`eth0` が他のIFのアドレスのARPに答えないことを確かめる。** 段階A・Bのあいだ、`eth0` は家のL2（Aruba）にいて、`eth1` が 192.168.2.1 を持つ。Linuxは `arp_ignore=0` だと、どのIFのアドレスのARPにも答える。`eth0` が家のLANの 192.168.2.1 のARPに答えると、RTX830とぶつかって家の通信が止まる。

```
ssh pc02 'sysctl net.ipv4.conf.all.arp_filter net.ipv4.conf.eth0.arp_filter net.ipv4.conf.all.arp_ignore net.ipv4.conf.eth0.arp_ignore'
```

	-	`arp_filter` が `all` か `eth0` のどちらかで1、または `arp_ignore` が `all` と `eth0` の大きい方で1以上なら進む（カーネルは `arp_filter` を `all` と各IFのどちらかが1なら有効とし、`arp_ignore` は大きい方を使う）。`arp_filter=1` なら、192.168.2.0/24 への経路は `eth1` なので、`eth0` は答えない見込み。
	-	どちらでもなければ、段階Aに進まず、Yusukeが決める（案: 段階A・Bのあいだ `eth0` をArubaから抜き、Macを `eth0` に直結する）。
	-	段階Aの後の `arp -n 192.168.2.1` の確かめ（段階Aの5.）は、そのまま行う。

### 段階A: IPv6だけを通す（家のLANはまだRTX830のまま）

1.	PC02の `eth2` からPC03とのケーブルを外し、新しい回線のONUをつなぐ。RTX830のWANは今の回線につないだまま触らない。
2.	`--from-lab` を付けて描く。`--mape` は付けない（MAP-Eは段階Bで入れる）。

```
python3 -I scripts/render_config.py site/home.local.yaml -o site/home.local/stage-a --from-lab
```

3.	描いた全部（`00-base`・`01-migrate-from-lab`・`10-lan`・`20-wan-common`・`20-wan-pd56`・`30-firewall`・`50-monitor`）を、**1回の** `pc02-commit.sh` で入れる: `scripts/pc02-commit.sh site/home.local/stage-a/*.cmds`
	-	`01-migrate-from-lab` は、同じcommitに入る他の部分が `set` する所を消さないように描かれている（`vyos/prod/README.md` の「検証環境から移る」）。描いたときの部分の組と、入れる組を同じにする。一部だけを入れたり、分けて入れたりしない。
	-	段階Aがconfirmされて入った後に入れ直すときは、`--from-lab` を付けずに描く。無い所を `delete` するとVyOSが拒否する。
	-	段階Aがconfirmされずに戻ったとき（下の4.で時間切れになったときや `--restore-saved` の後）は、PC02は基準＋前段の 192.168.255.1 に戻っている。入れ直すときは、もう一度 `--from-lab` を付けて描く。付けないと、移行の段が描かれず、VRF MGMTや検証用の経路が残る。
4.	**このcommitで、SSHが一度切れる見込み。** `eth0` がVRF MGMTから既定のVRFへ移り、SSHも既定のVRFで `192.168.2.1` と `192.168.255.1` を待ち受け直す（`00-base`）。2026-10-25 にVRF MGMTへ移したとき（コミット b83f0ed）と同じ形で、向きが逆になる。そのときは `commit-confirm 5` で入れ、鍵のSSHで入り直せることを確かめてからconfirmした。
	-	`pc02-commit.sh` が途中で切れて、exit 3（「commit-confirm が始まらなかった」）や exit 5（「鍵の SSH が通らない」）を返したり、止まったまま戻らなかったりしても、**すぐに流し直さない**。exit 3でも、commitを流していた接続が切れただけで、PC02ではconfirm待ちになっていることがある。
	-	新しい接続で、confirm待ちかを見る: `ssh pc02 'pgrep -f "[c]ommit-confirm-notify"'`
	-	入れてconfirm待ちなら、`show interfaces` と `show configuration commands | match vrf` で、`eth0` が 192.168.255.1 だけを持ち、VRFが残っていないことを見る。続けて、接続共有を使わずに新しく鍵で入れることを `ssh -o BatchMode=yes -o ControlMaster=no -o ControlPath=none pc02 true` で確かめる（`pc02-commit.sh` と同じ確かめ方。既存の接続を使い回すと、鍵の認証が通らなくても成功して見える）。通ったら、5分以内に手で `confirm` する（`timeout 60 ssh pc02 vbash -s` に `source /opt/vyatta/etc/functions/script-template`・`configure`・`confirm`・`exit` を流す）。
	-	入れなければ、待つ。5分たつとPC02が再起動し、保存済みの設定（基準＋前段の 192.168.255.1）に戻る。戻ったらSSHはVRF MGMTの中でまた 192.168.255.1 を待ち受けるので、同じ `ssh pc02` で入れる。原因を見てから入れ直す。
5.	**段階A・Bのあいだ、`eth1` を家のLANにつながない。** このcommitで `eth1` は 192.168.2.1/24 を持ち、DHCPv4とRAを配る。RTX830も 192.168.2.1 なので、つなぐとアドレスがぶつかる。`eth1` は今のままPC01と直結しておき、段階Bの判定用の端末にする（PC01は管理系の `eno1` も 192.168.2.0/24 にいるので、判定のときは `sfp0` をnetwork namespaceに入れて使う）。
	-	`50-monitor` のSNMPとnode-exporterは `192.168.2.1` で待ち受けるので、PC03のPrometheusとthin-k8sから読めるのは段階Cから。
	-	`30-firewall` は、`eth0` から入るものをすべて受ける（ipv4 inputの rule 40 が `inbound-interface name 'eth0'`、送信元のアドレスは絞らない）。192.168.255.0/24 から `eth0` に届くSSHも通る。IPv6のinputには `eth0` の行が無いので、救出用の口はIPv4で使う。
	-	家のLANの機器が 192.168.2.1 を引いたときに、PC02の `eth0` が答えないこと（RTX830のMACのままであること）を、Macの `arp -n 192.168.2.1` で見る。
6.	確かめること:
	-	`show interfaces ethernet eth2`: RAでアドレスが付いている
	-	`show ipv6 route`: 既定経路がNGNのリンクローカルに向いている（検証用の `route6 ::/0 next-hop 2001:db8:20::2` は移行の段で消えている）
	-	`show log | match dhcp6` でPDを受けている。`eth1` に `<委任プレフィックス>:1::1/64` が付いている
	-	プレフィックスが `240b:10::/31`、`240b:12::/31`、`240b:250::/31`、`240b:252::/31` のどれかに入っている。新しい回線でenひかりのv6プラスが開通していることは、ここで確かめる（入っていなければv6プラスではない。開通の状態とVNEを確かめる）
	-	PC02から `ping 2001:4860:4860::8888`（Google Public DNSのIPv6）が通る
	-	DNSの上流（2026-11-05 Yusuke決定: siteの `dns.upstream` は空のまま、ここで確かめる）: `show dns forwarding nameservers` に上流が載っているか、PC02から `dig @<LANのアドレス> www.flets-east.jp AAAA` が答えるかを見る。型A・BはPDだけを求めるので、DHCPv6でDNSが来ない見込み。答えなければ、`dns.upstream` にNGNかVNEのDNSを書いて描き直すか、WANに `address dhcpv6` を足すかを、その場で決める（描き直すときは `--from-lab` を付けない）
7.	観測した委任プレフィックスを控える。**gitには入れない**。

### 段階B: MAP-EでIPv4を通す

段階Aの移行の段は、検証用の既定経路 `route 0.0.0.0/0` を経路ごと消している（同じcommitにMAP-Eの段が無いので、`MIGRATE_DELETE` の広い方の候補が選ばれる）。段階Bの `40-mape` が `route 0.0.0.0/0 interface 'tun0'` を新しく入れる。

1.	値を確かめる: `python3 -I scripts/mape_calc.py <委任プレフィックス>`。警告が出ていないこと、ルールに一致していること。
2.	siteの `wan.prefix` に委任プレフィックスを書き、`--mape` を付け、`--from-lab` を付けずに、段階Aとは別の出力先へ描く。

```
python3 -I scripts/render_config.py site/home.local.yaml -o site/home.local/stage-b --mape
```

	-	`mape_calc.py --cmds` の出力をそのまま入れない。先頭に `delete protocols static route 0.0.0.0/0` があり、段階Aで経路が消えているので、無い所の `delete` になる。また、tun0をflowtableに入れる行が無い。`render_config.py --mape` の `40-mape` は、`delete` の行を除き、`set firewall flowtable FT10G interface 'tun0'` を足してある。
3.	`scripts/pc02-commit.sh site/home.local/stage-b/*.cmds` で入れる。`40-mape` 以外の部分は段階Aで入れたものと同じなので、変わるのは `40-mape` の分だけになる。
4.	確かめること:
	-	`show interfaces tunnel tun0` が up
	-	PC02から `ping 8.8.8.8` が通る
	-	LANの端末（段階CでつなぐまではPC01の `sfp0` を、`eth1` に直結のままnetwork namespaceに入れて使う）から `curl -4 https://ifconfig.io` を実行し、表示されたIPv4が計算したIPv4と一致する
	-	`sudo tcpdump -ni eth2 -c 20 'ip6 proto 4'` で、外側のNext Headerが4、宛先がBR（2404:9200:225:100::64）、内側の送信元ポートが割当範囲に収まっている
	-	JPNE切り分けサイト（kiriwake.jpne.co.jp）で、v6プラスの接続が判定される
	-	通らなければ、IIDをRFC 7597の形にして試す（§7）。`render_config.py` にはIIDの形を選ぶ引数が無いので、`mape_calc.py` で作る。CEアドレスが変わるので、draft-03の形で入れたCEアドレスを同じcommitで消す（消さないと `eth2` に前のCEアドレスが残る）。

```
python3 -I scripts/mape_calc.py <委任プレフィックス>     # 表示されたCE（draft-03の形）を控える
printf "delete interfaces ethernet eth2 address '%s/64'\n" '<控えたCE>' > /tmp/mape-old-ce.cmds
python3 -I scripts/mape_calc.py --iid-format rfc7597 --cmds --wan-if eth2 --lan 192.168.2.0/24 <委任プレフィックス> > /tmp/mape-rfc7597.cmds
scripts/pc02-commit.sh /tmp/mape-old-ce.cmds /tmp/mape-rfc7597.cmds
```

	-	`mape_calc.py --cmds` の先頭の `delete protocols static route 0.0.0.0/0` は、段階Bの3.で経路が入っているので、ここでは通る。tun0のflowtableの行は `40-mape` で入っている。どちらのファイルにもグローバルアドレスが入るので、リポジトリには入れない。
	-	RFC 7597の形で通ったら、そのことを記録する。以後、プレフィックスが変わったとき（`CLAUDE.md` §6-5の手順）も `mape_calc.py` に `--iid-format rfc7597` を付ける。`--old-prefix` は前のCEアドレスを同じ形で計算するので、形を揃えないと前のCEアドレスを消せない。`render_config.py --mape` はdraft-03の形で描くので、この場合は使わない。
5.	通ったら、`save` の前に、今の保存済みの設定（基準＋前段の 192.168.255.1）を別名で残す: `ssh pc02 'cp /config/config.boot /config/config.boot.lab'`。`save` すると `--restore-saved` は本番の設定を読み直すので、検証環境へ戻るときにこのファイルを使う（戻し方）。
6.	`save` し、`show configuration commands` を、グローバルアドレスとcommunityを印に戻してから `configs/pc02/` に保存する。

### 段階C: 家のLANを切り替える（ケーブルの差し替えだけ）

設定は段階A・Bで入っている。この段では設定を入れない。

1.	RTX830のLAN側をArubaから外す。
2.	PC02の `eth1` をPC01から外し、ArubaのSFP+の口につなぐ（PC01の `sfp0` をnetwork namespaceに入れていたら、`sudo ip netns del <名前>` で戻し、`sudo netplan apply` する）。
3.	`~/.ssh/config` の `pc02` の `HostName` を `192.168.2.1` に直す（Yusukeの作業）。Macの別名を外す: `sudo ifconfig en0 -alias 192.168.255.2`
4.	`eth0` をArubaにつないだままにするか、抜いて救出用の口（D2、ノートPCを直結）に戻すかを決める。つないだままでも、家のLANの端末からは 192.168.255.1 に届かない（別名を足した機器だけが届く）。
5.	確かめること:
	-	端末がDHCPで 192.168.2.x を受け取る（固定割り当ての機器は同じアドレス）
	-	端末がRAでGUAとULAを受け取る。`nslookup` がULAのDNS（`@ULA_PREFIX@::1`）で引ける
	-	PC03のPrometheusでPC02（192.168.2.1:9100）がup。thin-k8sからSNMPでプレフィックスが読める
	-	作業用のMacから、`ssh pc02`（192.168.2.1）で入れる

### 段階D: NGN特有の確認

| 確認 | やり方 | 期待する結果 |
|---|---|---|
| DSCP（ルータ自身） | siteで `firewall.router_wireguard` を有効にしたときだけ（無効ならoutputの書き換えは入らない）。PC02から送信元ポートを51820に固定して `nc -u -p 51820 -T 0x88 <相手のIPv6> 52528`。PC02の `sudo tcpdump -v -ni eth2 'udp src port 51820'` と、相手側の `tcpdump -v` の両方で見る（ひかり電話なしの回線ではNGNが0x00に書き換えて届けることがあるので、相手側だけでは区別できない） | `eth2` から class 0x00 で出ていく |
| DSCP（LANの端末） | LANの端末から、送信元ポートを51820に固定して同じことを行う（`nc -u -p 51820 -T 0x88 ...`）。flowtableが有効な状態で、数分おきに繰り返す。PC02の `eth2` の `tcpdump` と相手側の両方で見る | 毎回 `eth2` から class 0x00 で出ていく |
| MTU | LANの端末から `ping -4 -M do -s 1432 8.8.8.8` は通り、`-s 1433` は「Frag needed」が返る | トンネルのMTUが1460 |
| MSS | `tcpdump -ni eth1 'tcp[tcpflags] & tcp-syn != 0'` で、SYN-ACKのMSSを見る | 1420以下 |
| ポートセット | LANの端末から外へ、同時に多数の接続を張る（`iperf3 -P 32` を外部のサーバへ）。PC02の `show nat source translations` で、変換後の送信元ポートを見る | 17本目以降も失敗しない。変換後のポートが最初の組（16ポート）を超えて、別の組にも入っている。32本では送信元ポートが数個の塊にしか入らないので、15組すべてが使われるとは限らない。全部の組を見るなら、接続の数を増やす（`-P 128`）か、送信元ポートを塊ごとに変えて接続し、使われた組の数を数える |
| 速度 | フレッツの速度測定サイトと、外部のiperf3サーバ | 回線の上限近く。CPUの使用率も記録する |
| 再起動 | PC02を再起動して、PDとMAP-Eが自動で戻るまでの時間を測る | R1と同じ程度。同じプレフィックスで戻れば、そのままIPv4も通る。変わっていても失敗ではない。変わったことを記録し、`CLAUDE.md` §6-5の「プレフィックスが変わったとき」の手順でMAP-Eの値を入れ直す |

### 戻し方

-	段階A・Bで失敗しても、家のLANとインターネットはRTX830（今の回線）のまま動いている。段階Cの後に失敗したら、**先にPC02の `eth1` をArubaから外し、それからRTX830のLAN側をArubaに差し戻せば元に戻る**（逆の順にすると、しばらく2台が 192.168.2.1 とDHCPを出す）。RTX830のWANは今の回線につないだままなので、差し戻すのはLAN側だけ。
-	段階Cの後にPC02の設定を戻すときは、192.168.2.1 がRTX830を指すようになるので、先に救出用の口から入れるようにする。`eth0` をArubaにつないだままなら、Macに別名を足し直し（`sudo ifconfig en0 alias 192.168.255.2 255.255.255.0`）、`~/.ssh/config` の `pc02` を 192.168.255.1 に戻す。`eth0` を抜いていたら、ノートPCを `eth0` に直結し、`pc02` をそのノートPCから 192.168.255.1 で入る形にしてから行う。
-	PC02の設定は `scripts/pc02-commit.sh --restore-saved` で、保存済みの状態に戻す。段階Bの `save` の前なら、保存済みの状態は基準（`configs/pc02/baseline.cmds`）に前段の 192.168.255.1 を足したもの。
	-	段階Aの後に戻すと、`eth0` がVRF MGMTへ戻るので、段階Aと同じくSSHが一度切れる見込み。段階Aの4.と同じ確かめ方をする。戻った後もSSHはVRF MGMTの中で 192.168.255.1 を待ち受けるので、`~/.ssh/config` はそのままで入れる。
	-	段階Bの `save` の後は、`--restore-saved` は本番の設定を読み直す。検証環境へ戻るには、段階Bの5.で残した `/config/config.boot.lab` を `commit-confirm 5` で `load` する（`pc02-commit.sh` は `/config/config.boot` しか読まないので、`CLAUDE.md` §1 ルール2の形で手で行う）。確かめてから `confirm` し、`save` する。`eth0` がVRF MGMTへ戻るので、段階Aの4.と同じくSSHが一度切れる見込み。
	-	検証環境の状態まで戻すなら、`eth2` をPC03に、`eth1` をPC01につなぎ直し、`~/.ssh/config` の `pc02` を 192.168.2.14 に戻してMacの別名を外す。
-	救出用の口（`eth0`、192.168.255.1）は、前段の後はどの段階でも入れる（段階A・BはArubaとMacの別名から、段階Cの後は、Arubaにつないだままなら別名を足したMacから、抜いたならノートPCの直結で）。

---

§6 実装するもの
---------------

| # | 中身 |
|---|---|
| I1 | `vyos/prod/` のテンプレート（`00-base`、`10-lan`、`20-wan-common`、`20-wan-pd56`・`pd60`・`ra64`、`30-firewall`、`50-monitor`）と、siteのyamlから設定を作る `scripts/render_config.py`（`--from-lab` で検証環境の状態から移る `01-migrate-from-lab`、`--mape` でMAP-Eの `40-mape` も描く）。`render_config.py` は、印（`@SSH_PUBKEY@`・`@ULA_PREFIX@`・`@SNMP_COMMUNITY@`・`@WAN_PREFIX64@`・`@NGN_GW_LLA@`）のうちsiteに値を書いていないもの（`lan.ula_prefix` などがsiteで印のまま）を置き換えずに出し、`scripts/pc02-commit.sh` が投入時に、`scripts/placeholders.sh` の一覧と値の形で、同じ名前の環境変数から置き換える。一覧に無い印・未設定・形の合わない値があれば、PC02に何も送らずに止まる |
| I2 | `vyos/dscp-firewall.cmds` のルール番号を5に振り直し、flowtableのrule 10との衝突をなくす（§3-3） |
| I3 | `scripts/mape_calc.py` に、型C用の `--ce-plen 128` を足す。型B用に、WANの/64をそのまま渡す使い方を `--help` に書く |
| I4 | CI（`vyos-config.yaml`）に、型A・B・Cごとに生成した設定がcommitできるかの検査を足す。印には文書用のアドレスを入れて検査する |
| I5 | `lab/` に、PC03（NGN役）のPDサーバとRAを足し、R-A〜R-Cを再現する。`scripts/lab-e2e.sh`（CIは `.github/workflows/lab-e2e.yaml`）で疎通を確かめる |
| I6 | 実機の閉域網で、§4の予行演習を通す。結果は `results/prod-rehearsal/` に |

---

§7 まだ分からないこと
---------------------

-	光クロスで、PDが本当に/56で来るか。RAでWANに何が来るか。
-	v6プラスのCEのIIDの形（`mape_calc.py` の既定はdraft-03の形）が、実回線のBRで通るか。段階Bで初めて分かる。通らなければ `--iid-format rfc7597` を試す。
-	新しい回線で、ONUにつないでからPDが委任されるまでの時間。
-	型B・型Cは本番では使わないが、テンプレートとしては残す。型Cの設計（WANに/64を持たせず、NDプロキシで答える）が、NGNで実際に動くかは分からない。そのため型Cは、実回線でルール10の3段を通すまで使わない（2026-11-11 Yusuke決定、§2-5）。
