# 本番の PC02 の設定（vyos/prod/）

光クロス 10G・ひかり電話なし・enひかり（v6プラス）につなぐ PC02 の VyOS の設定を、site の yaml とこのディレクトリのテンプレートから描く。出力は部分ごとの `.cmds` で、VyOS の `set` と `delete` の行だけからなる（CLAUDE.md §1 ルール 10）。`delete` を出すのは、型C（`20-wan-ra64`）が共通の部分の `autoconf` を消す 1 行と、`--from-lab` の `01-migrate-from-lab` だけ。

## 描く

`scripts/render_config.py` は Jinja2 と PyYAML を使う（`requirements-dev.txt`）。作業用の Mac には入れず、コンテナの中で動かす。

1 回だけ描くなら、コマンドごとに `docker run` する（描いたらコンテナは終わって消える）。

```
docker run --rm -v "$PWD":/w -w /w python:3.12-slim sh -c \
  'pip install -q -r requirements-dev.txt && python3 -I scripts/render_config.py site/home.local.yaml -o site/home.local'
```

続けていくつも描くなら、対話のコンテナを起動して、その中で `pip install -q -r requirements-dev.txt` を 1 回打ってから、下の例を打つ（`exit` でコンテナは消える）。

```
docker run --rm -it -v "$PWD":/w -w /w python:3.12-slim sh
```

```
python3 -I scripts/render_config.py site/example.yaml -o <出力先>
python3 -I scripts/render_config.py site/home.local.yaml -o site/home.local --mape   # 接続後、MAP-E の段も
python3 -I scripts/render_config.py site/example.yaml --check vyos/prod/example --all-wan-types
```

- 実際の値は `site/<名前>.local.yaml` に書く。描いた結果は `site/<名前>.local/` に出す（どちらも `.gitignore` 済み）。
- `vyos/prod/example/` は `site/example.yaml` を `--all-wan-types` で描いた結果。手で直さない。CI が再生成して一致を確かめる。

## 部分

| ファイル | 中身 |
|---|---|
| `00-base` | ホスト名・時刻・NTP・鍵のログイン・SSH・救出用の `eth0`・オフロードとリング・性能プロファイル・conntrack |
| `01-migrate-from-lab` | `--from-lab` のときだけ。検証環境の状態（`configs/pc02/baseline.cmds`）から移る最初の commit で、VRF MGMT・検証用のアドレスと経路・前の NTP サーバを消す `delete` の行 |
| `10-lan` | LAN の IPv4 と ULA、DHCPv4 サーバ、DNS の転送、RA（`deprecate-prefix` 付き） |
| `20-wan-common` | WAN の説明と RA の受け入れ |
| `20-wan-pd56` / `pd60` / `ra64` | `wan.type` で 1 つ選ぶ。型A（ONU 直結、/56）・型B（HGW 配下、/60）・型C（RA の /64 と ND proxy） |
| `30-firewall` | forward（DSCP の書き換え・flowtable・ステートフル）と、ルータ宛ての input |
| `40-mape` | `--mape` のときだけ。`wan.prefix` から `scripts/mape_calc.py` で計算した MAP-E と、tun0 の flowtable |
| `50-monitor` | SNMP（読み取り専用）と node-exporter |

投入する順は番号の順。型によらない部分と、選んだ型の WAN の部分を同じ commit で入れる。今の PC02（検証環境の状態）に最初に入れるときだけ、`--from-lab` で描いた `01-migrate-from-lab` も同じ commit に入れる（下の「検証環境から移る」）。

- `-o` の出力先に、今回描かなかった `*.cmds`（前に描いた別の型や古い `40-mape`）が残っていると警告が出る。消してから投入する。
- 型C（`ra64`）を描くと、実機で未確認の仮説である旨の警告が出る。

## 検証環境から移る（`--from-lab`）

今の PC02 は `configs/pc02/baseline.cmds`（フェーズ1 と VRF MGMT）で動いている。部分は `set` だけなので、そのまま重ねると、SSH が VRF MGMT に残ったまま `00-base` の `listen-address` が足され、検証用の既定経路（`route6 ::/0 next-hop 2001:db8:20::2` など）も RA の既定経路より優先されて残る。

```
python3 -I scripts/render_config.py site/home.local.yaml -o site/home.local --from-lab
```

- `01-migrate-from-lab.cmds` は、`baseline.cmds` にある次のものを消す: `eth0` の DHCP と `vrf`、`eth1`・`eth2` の検証用のアドレス、検証用の既定経路（IPv4・IPv6）、SSH・NTP・node-exporter の `vrf`、NTP サーバ 192.168.2.13、`vrf name MGMT`。
- **管理系（VRF MGMT・`eth0` の DHCP・SSH の待ち受け）を変える投入なので、`scripts/pc02-commit.sh` に `01-migrate-from-lab.cmds` を渡す前に Yusuke の承認を得る**（CLAUDE.md §1 ルール 1）。
- この commit の後、PC02 は管理系の DHCP のアドレス（`~/.ssh/config` の `pc02` が指す 192.168.2.14）を失う。`scripts/pc02-commit.sh` は同じ `pc02` に入り直せたときだけ confirm するので、入り直す経路（新しい LAN の 192.168.2.1 か、救出用の 192.168.255.1）と接続先を先に用意しないと、5 分後に元の設定へ戻る。入り直す経路は、切り替えの手順書 `docs/production-cutover.md` の「段階Aの前段」で用意する（`eth0` に救出用の 192.168.255.1 を先に足し、`pc02` の接続先をそこへ向ける）。
- **最初の commit だけ**で使う。2 回目からは `--from-lab` を付けずに描く。無い所を `delete` すると VyOS が拒否し、`scripts/pc02-commit.sh` が失敗する。
- つなぐ順によらない。同じ commit で入れる部分が `set` する所とその上は消さない（例: 予行の site が LAN に 192.168.10.1/24 を使うなら、その行は消さずにコメントに残す。MAP-E の段や型C が既定経路を `set` するなら、経路ごとではなく検証用の `next-hop` だけを消す）。説明とホスト名は、部分が上書きするので消さない。
- 残すもの: `hw-id`・`offload`・`loopback`・NTP の `allow-client` と VyOS の既定の NTP サーバ・`disable-password-authentication`・ログイン（今の鍵 `claude-lab` を含む）・コンソール・syslog など。
- 分け方は `scripts/render_config.py` の `MIGRATE_DELETE`・`MIGRATE_LEAVES`・`MIGRATE_KEEP`。`baseline.cmds` の行がどれにも入っていない、または消す所が `baseline.cmds` に無いと、`tests/test_render_config.py` が落ちる。

## DNS の上流

- 型A・B の WAN は PD だけを求めるので、VyOS の DHCPv6 クライアントは DNS サーバを求めない（vyos-1x `data/templates/dhcp-client/ipv6.j2` で、`request domain-name-servers` は `address dhcpv6` のときだけ）。`service dns forwarding dhcp '<WAN の IF>'`（site の `interfaces.wan`。例では `eth2`）には何も届かない見込み。
- 上流を決めるときは、site の `dns.upstream` に IP アドレスを並べる。`service dns forwarding name-server` が描かれる。空なら `name-server` の行を描かない（今の既定）。そのときも `dns forwarding dhcp` の行は残るので、WAN の DHCP で DNS が届けばそれを使い、届かなければルートから自分で引く。
- 上流なしで名前を引けるか、光クロスで DHCPv6 の DNS がどう来るかは、実回線で確かめる。

## 委任プレフィックスが変わったとき

- RA に `deprecate-prefix` を付けている。radvd が止まるときに、配っていたプレフィックスの寿命を 0 にして知らせる。
- `decrement-lifetime` は付けていない。`::/64` は同じ口の ULA も含むので、ULA まで寿命が減って失効するため。
- プレフィックスが変わったときに、前の /64 が LAN の端末で本当に廃止されるかは、実機で確かめていない。

## VLAN で網を分ける形と VRF MGMT（`site/example-vlan.yaml`）

2026-11-12 の決定（LAN を 5 つの網に分け、当日は VRF MGMT を残す）に合わせた形。描いた結果は `vyos/prod/example-vlan/`（`--all-wan-types`）。既定値は今の形のままなので、`site/example.yaml` と `vyos/prod/example/` は変わらない。

- `lan.bridge` を書くと、`interfaces.lan`（eth1）をメンバーにした VLAN-aware のブリッジ（`br0`）を作り、`networks` の網ごとに vif（`br0.<VLAN>`）・DHCPv4 サーバ・DNS の転送・RA・PD の sla-id を描く。`native: true` の網は eth1 でタグなし（`native-vlan`）、他の網は `allowed-vlan` を 1 つずつ書く。`lan.bridge` を書いたら `lan.address` と `lan.dhcp` は書かない
- ULA は先頭の網（native の網）にだけ付け、RA の DNS は全部の網に ULA の `::1` を配る。型C（ra64）では /64 を先頭の網にだけ置く
- `access: all` の網は他の網にもインターネットにも出られる。`access: internet` の網はインターネットだけ。`monitor_to` に書いた網へは、ICMP と `firewall.monitor_ports`（既定 TCP 9100・UDP 161）だけを通す。VLAN 1 は使えない（2〜4094）。sla-id 0 は MAP-E の CE が使うので使えない
- forward は interface-group（`LAN`・`LAN-ALL`・`MON-<網>`）で書く。`inbound-interface name 'eth1'` はブリッジの vif に一致しないため。網と網のあいだは rule 290 で捨て、通過の ICMPv6 は rule 310〜315（その後）で通す
- `mgmt.mode: vrf` では、`interfaces.rescue`（eth0）を VRF MGMT に入れて DHCP で受け、SSH・NTP・node-exporter・SNMP を VRF MGMT の中だけにする（`vyos/mgmt-home.cmds` の形）。eth0 は家族用の網にタグなしでつなぎ、家族用の網の DHCP の固定割り当てで 192.168.2.14 を配る。既定の `rescue` は今のまま（救出用の 192.168.255.1/24）
- `firewall.dscp_udp_port` は並びで書ける（例 `[51820, 51821]`）。rule 5・6 の向きの絞りは変えていない
- `firewall.wireguard_inbound_iid` に下位 64 ビット（実値は `.local.yaml`）を書くと、NGN から LAN の WireGuard へ来る新しい通信を、`destination address` と `destination address-mask '::ffff:ffff:ffff:ffff'` で通す（rule 115）。プレフィックスが変わっても書き直さなくてよい。固定した版の VyOS のコンテナで commit が通ることは確かめたが、パケットを流しての確認はしていない
- MAP-E の段（`--mape`）は、全部の網を `firewall group network-group MAPE-LAN` にまとめて NAT する（`scripts/mape_calc.py --lan` を繰り返したときと同じ）。網の数を変えて入れ直すときは、前の `source address` や消した網のグループの行が残るので、手で消す
- flowtable には eth1・eth2 と各 vif を並べる。commit が通ることだけを確かめた。offload が効くかと性能は未測定
- CI（`.github/workflows/vyos-config.yaml`）は、`prod-vlan-example.cmds` を基準に、型 A・B・C と MAP-E の段を 1 本ずつ commit する。実機と同じ形の基準から `--from-lab --mape` の部分を 1 回で入れる検査（`vlan-from-lab-mape.cmds`）も行う

## 印

リポジトリに書けない値は `@名前@` の印のまま出す。`scripts/pc02-commit.sh` が投入時に、同じ名前の環境変数で置き換える（`scripts/placeholders.sh`）。不明な印・未設定・形の合わない値があれば、PC02 に何も送らずに止まる。

| 印 | 値 |
|---|---|
| `@SSH_PUBKEY@` | ログインに使う公開鍵の本体（base64） |
| `@ULA_PREFIX@` | ULA の /64 の上位 4 組（例 `fd12:3456:789a:1`）。site の `lan.ula_prefix` に書けばその値で描く |
| `@SNMP_COMMUNITY@` | SNMP の community（秘密。表示では伏せる） |
| `@WAN_PREFIX64@` | 型C だけ。RA で受けた /64 の上位 4 組 |
| `@NGN_GW_LLA@` | 型C だけ。NGN 側のルータのリンクローカル |

```
SSH_PUBKEY=... ULA_PREFIX=... SNMP_COMMUNITY=... scripts/pc02-commit.sh site/home.local/*.cmds
# 型C（ra64）で wan.prefix64・wan.ngn_gw_lla を印のままにしたときは、この 2 つも要る
SSH_PUBKEY=... ULA_PREFIX=... SNMP_COMMUNITY=... WAN_PREFIX64=... NGN_GW_LLA=... scripts/pc02-commit.sh site/home.local/*.cmds
```

site に値を書いた印は描いた時点で置き換わっているので、その環境変数は要らない（渡しても使われない）。

## 検査

- `tests/test_render_config.py`: 例と描いた結果の一致、検証済みの `vyos/*.cmds` の行を使っていること、RPS・QoS の shaper・VRF を使わないこと、出力が `tests/test_no_sensitive_data.py` の検査を通ること、site の誤りで止まること。
- `.github/workflows/vyos-config.yaml`: 印に文書用の値を入れ、型によらない部分（`prod-example.cmds`）を基準にして、型 A・B・C と MAP-E の段を 1 本ずつ VyOS のコンテナで commit する。
- 同じワークフローで、`vyos/phase1.cmds` と `vyos/mgmt-home.cmds` をつないだ基準（実機の PC02 と同じ形）に、`01-migrate-from-lab` と型A の部分（MAP-E の段の有無の 2 通り）を 1 回で commit し、VRF MGMT・SSH の `vrf`・検証用のアドレスと経路が残っていないこと、SSH が既定の VRF で 192.168.2.1 を待ち受けることを確かめる（`scripts/vyos-config-check.sh`）。コンテナの `eth0` は DHCP でアドレスを受けないので、DHCP で受けたアドレスが消えることまでは見ていない。
