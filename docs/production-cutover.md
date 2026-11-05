本番接続テンプレートの設計と切り替え手順
=========================================

-	作成: 2026-11-05
-	前提: PC02（HP ProDesk 400 G7 SFF、Intel X710、VyOS `2026.10.16-0024-rolling`）を、自宅のRTX830の代わりにフレッツへ直接つなぐ本番ルータにする。
-	この文書の扱い: 設計と手順。設定は `vyos/prod/` のテンプレートから `scripts/render_config.py` で生成する。
-	実行できる時期: §4 は §6 の I1 と I5、§5 は I1〜I4 ができてから。それまでは設計として読む。

---

§0 決まっていること・決めること
-------------------------------

### 決まったこと（2026-11-05）

| 項目 | 内容 |
|---|---|
| 本番の回線 | **光クロス 10G、ひかり電話なし、プロバイダは enひかり（v6プラス）**。WANは型A（ONU直結、DHCPv6-PDで/56）を使う |
| テンプレートの範囲 | 型A（光クロス）・型B（ネクスト＋ひかり電話、HGWの配下でPD /60）・型C（ネクストで電話なし、RAで/64）の3通りを、siteのyamlで選べるようにする |
| WAN | X710の `eth2` に、10GBASE-TのSFP+モジュールを挿してONUにつなぐ（検証のときと同じ向き） |
| LAN | X710の `eth1`。**RTX830を置き換え**、家のLANのDHCPv4・DNS・RAをVyOSが配る |

### Yusukeに決めてほしいこと（推奨の値をテンプレートの既定にしてある）

| # | 決めること | 推奨 |
|---|---|---|
| D1 | 家のLANのアドレス | **192.168.2.0/24 のまま、VyOSを 192.168.2.1 にする**。PC01・PC03・thin-k8s（.50）の固定割り当てと、`~/.ssh/config`・Prometheus・SNMPの許可先をそのまま使える。RTX830が .1 なら、RTX830を外してから入れる |
| D2 | 管理系（VRF MGMT）の扱い | **本番ではVRF MGMTをやめる**。管理はLANの 192.168.2.1 から行い、入力フィルタでLANからだけ許す。`eth0`（オンボード1G）は**救出用の口**として残し、固定の `192.168.255.1/24` を持たせる（ゲートウェイなし。ノートPCを直結して入る） |
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
| **実値の段** | MAP-E（CEのIPv6アドレス、トンネル、NAT44の15ルール） | 接続後に観測したプレフィックスから `scripts/mape_calc.py --cmds` が作る。**グローバルアドレスなのでgitには入れない**（`CLAUDE.md` §1 ルール3） |

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
-	**この型は設計上の仮説の段階で、まだ試していない**。§4の閉域網での予行演習で、必ず先に確かめる。
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
| 5 | IPv6のUDP 51820 → `set dscp '0'`、accept | **offloadより先に置く**。flowtableに載ったフローは、2パケット目以降がforwardのチェーンを通らない。先にoffloadされると、LANの端末が張るWireGuardの鍵の更新がDSCP 0x88のまま出ていく。先にacceptしたフローはoffloadされないので、毎回書き換わる |
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
| R-M | `lab/pc03-br.sh` のBR役を、R-Aで委任したプレフィックスから計算した値で動かす | `mape_calc.py --cmds` の出力をそのまま入れて、IPv4がPC03まで届く。ポートが割当範囲に収まる |
| R-F | WAN側から、TCP 22、UDP 161、9100番に新しい接続を試みる | すべて落ちる。LANからは届く |
| R-D | LAN側の端末からDSCP 0x88でUDP 51820を送り続け、途中で鍵の更新に相当するパケットを混ぜる | flowtableを有効にした状態でも、WANに出るパケットがすべてDSCP 0になっている（§3-3の順番の確認） |

-	containerlab（`lab/`）とCIにR-A〜R-Cの「NGN役」を足すと、テンプレートが壊れていないかを毎回自動で確かめられる。
-	使う委任プレフィックスはv6プラスのルール（240b:10::/31 など）に入る値にする。gitに残すのは、`tests/test_no_sensitive_data.py` が例として許している値だけにする。

---

§5 当日の切り替え手順
---------------------

段階ごとに確かめ、だめならすぐ戻せるようにする。**RTX830は設定を消さずに残しておく**。戻すときは、ケーブルを差し戻すだけで済む。

### 段階0: 前日まで

-	[ ] v6プラスの契約が有効であることを確かめる（RTX830で今IPv4がMAP-Eで出ているなら有効）
-	[ ] RTX830の設定を書き出す（DHCPの固定割り当て、ポート開放、静的経路、DNSの設定）。siteのyamlのstatic-mappingに移す
-	[ ] WAN用の10GBASE-TのSFP+モジュールを用意する（§2-1）。`eth2` に挿して `ethtool -m eth2` でモジュールが読めることを確かめる
-	[ ] 救出用の口（`eth0`、192.168.255.1）に、ノートPCを直結して入れることを確かめる
-	[ ] §4の予行演習が通っている

### 段階A: IPv6だけを通す（家のLANはまだRTX830のまま）

1.	RTX830のWANケーブルを抜き、PC02の `eth2` にONUをつなぐ。
	-	RTX830がPDを受けていた場合、NGNのDHCPv6サーバが前のリースを覚えていて、すぐには委任しないことがある。ONUの電源を入れ直すか、数分待つ。
2.	`00-base`、`20-wan-common`、`20-wan-pd56`、`30-firewall`（MAP-Eの行を除く）を `pc02-commit.sh` で入れる。
3.	確かめること:
	-	`show interfaces ethernet eth2`: RAでアドレスが付いている
	-	`show ipv6 route`: 既定経路がNGNのリンクローカルに向いている
	-	`show log | match dhcp6` でPDを受けている。`eth1` に `<委任プレフィックス>:1::1/64` が付いている
	-	プレフィックスが `240b:10::/31`、`240b:12::/31`、`240b:250::/31`、`240b:252::/31` のどれかに入っている（入っていなければv6プラスではない。VNEを確かめる）
	-	PC02から `ping 2001:4860:4860::8888`（Google Public DNSのIPv6）が通る
4.	観測した委任プレフィックスを控える。**gitには入れない**。

### 段階B: MAP-EでIPv4を通す

1.	値を確かめる: `python3 -I scripts/mape_calc.py <委任プレフィックス>`。警告が出ていないこと、ルールに一致していること。
2.	set行を作る: `python3 -I scripts/mape_calc.py --cmds --wan-if eth2 --lan 192.168.2.0/24 <委任プレフィックス> > /tmp/mape-prod.cmds`
3.	`scripts/pc02-commit.sh /tmp/mape-prod.cmds` で入れる。
4.	確かめること:
	-	`show interfaces tunnel tun0` が up
	-	PC02から `ping 8.8.8.8` が通る
	-	LANの端末（段階CでつなぐまではPC01を `eth1` に直結）から `curl -4 https://ifconfig.io` を実行し、表示されたIPv4が計算したIPv4と一致する
	-	`sudo tcpdump -ni eth2 -c 20 'ip6 proto 4'` で、外側のNext Headerが4、宛先がBR（2404:9200:225:100::64）、内側の送信元ポートが割当範囲に収まっている
	-	JPNE切り分けサイト（kiriwake.jpne.co.jp）で、v6プラスの接続が判定される
	-	通らなければ `--iid-format rfc7597` で作り直して試す（§7）
5.	通ったら `save` し、`show configuration commands` を、グローバルアドレスとcommunityを印に戻してから `configs/pc02/` に保存する。

### 段階C: 家のLANを切り替える

1.	RTX830のLAN側をArubaから外し、PC02の `eth1` をArubaのSFP+の口につなぐ。
2.	`10-lan`、`50-monitor` を入れる。
3.	確かめること:
	-	端末がDHCPで 192.168.2.x を受け取る（固定割り当ての機器は同じアドレス）
	-	端末がRAでGUAとULAを受け取る。`nslookup` がULAのDNS（`@ULA_PREFIX@::1`）で引ける
	-	PC03のPrometheusでPC02（192.168.2.1:9100）がup。thin-k8sからSNMPでプレフィックスが読める
	-	作業用のMacから、`~/.ssh/config` の `pc02` を 192.168.2.1 に直してSSHで入れる

### 段階D: NGN特有の確認

| 確認 | やり方 | 期待する結果 |
|---|---|---|
| DSCP（ルータ自身） | 外部のVPSなどに向けて、PC02から `nc -u -T 0x88 <相手のIPv6> 52528`。相手側で `tcpdump -v` | class 0x00 で届く |
| DSCP（LANの端末） | LANの端末から同じことを、UDP 51820で行う。flowtableが有効な状態で、数分おきに繰り返す | 毎回 class 0x00 で届く |
| MTU | LANの端末から `ping -4 -M do -s 1432 8.8.8.8` は通り、`-s 1433` は「Frag needed」が返る | トンネルのMTUが1460 |
| MSS | `tcpdump -ni eth1 'tcp[tcpflags] & tcp-syn != 0'` で、SYN-ACKのMSSを見る | 1420以下 |
| ポートセット | LANの端末から外へ、同時に多数の接続を張る（`iperf3 -P 32` を外部のサーバへ） | 17本目以降も失敗しない（15組に振り分けられている） |
| 速度 | フレッツの速度測定サイトと、外部のiperf3サーバ | 回線の上限近く。CPUの使用率も記録する |
| 再起動 | PC02を再起動して、PDとMAP-Eが自動で戻るまでの時間を測る | R1と同じ程度。プレフィックスが変わっていないこと |

### 戻し方

-	段階のどこで失敗しても、**WANとLANのケーブルをRTX830に差し戻せば元に戻る**。
-	PC02の設定は `scripts/pc02-commit.sh --restore-saved` で、保存済みの状態に戻す。
-	救出用の口（`eth0`）は、どの段階でも入れる。

---

§6 実装するもの
---------------

| # | 中身 |
|---|---|
| I1 | `vyos/prod/` のテンプレート（`00-base`、`10-lan`、`20-wan-common`、`20-wan-pd56`・`pd60`・`ra64`、`30-firewall`、`50-monitor`）と、siteのyamlから設定を作る `scripts/render_config.py`。印は `scripts/pc02-commit.sh` が環境変数で置き換える |
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
-	RTX830からPC02に替えたときに、NGNのPDのリースがすぐ移るか。
-	型B・型Cは本番では使わないが、テンプレートとしては残す。型Cの設計（WANに/64を持たせず、NDプロキシで答える）が、NGNで実際に動くかは分からない。
