# 本番の PC02 の設定（vyos/prod/）

光クロス 10G・ひかり電話なし・enひかり（v6プラス）につなぐ PC02 の VyOS の設定を、site の yaml とこのディレクトリのテンプレートから描く。出力は `set` の行だけの部分ごとの `.cmds`（CLAUDE.md §1 ルール 10）。

## 描く

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
| `10-lan` | LAN の IPv4 と ULA、DHCPv4 サーバ、DNS の転送、RA（`deprecate-prefix` 付き） |
| `20-wan-common` | WAN の説明と RA の受け入れ |
| `20-wan-pd56` / `pd60` / `ra64` | `wan.type` で 1 つ選ぶ。型A（ONU 直結、/56）・型B（HGW 配下、/60）・型C（RA の /64 と ND proxy） |
| `30-firewall` | forward（DSCP の書き換え・flowtable・ステートフル）と、ルータ宛ての input |
| `40-mape` | `--mape` のときだけ。`wan.prefix` から `scripts/mape_calc.py` で計算した MAP-E と、tun0 の flowtable |
| `50-monitor` | SNMP（読み取り専用）と node-exporter |

投入する順は番号の順。型によらない部分と、選んだ型の WAN の部分を同じ commit で入れる。

- `-o` の出力先に、今回描かなかった `*.cmds`（前に描いた別の型や古い `40-mape`）が残っていると警告が出る。消してから投入する。
- 型C（`ra64`）を描くと、実機で未確認の仮説である旨の警告が出る。

## DNS の上流

- 型A・B の WAN は PD だけを求めるので、VyOS の DHCPv6 クライアントは DNS サーバを求めない（vyos-1x `data/templates/dhcp-client/ipv6.j2` で、`request domain-name-servers` は `address dhcpv6` のときだけ）。`dns forwarding dhcp 'eth2'` には何も届かない見込み。
- 上流を決めるときは、site の `dns.upstream` に IP アドレスを並べる。`service dns forwarding name-server` が描かれる。空なら上流なし（今の既定）。
- 上流なしで名前を引けるか、光クロスで DHCPv6 の DNS がどう来るかは、実回線で確かめる。

## 委任プレフィックスが変わったとき

- RA に `deprecate-prefix` を付けている。radvd が止まるときに、配っていたプレフィックスの寿命を 0 にして知らせる。
- `decrement-lifetime` は付けていない。`::/64` は同じ口の ULA も含むので、ULA まで寿命が減って失効するため。
- プレフィックスが変わったときに、前の /64 が LAN の端末で本当に廃止されるかは、実機で確かめていない。

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
```

## 検査

- `tests/test_render_config.py`: 例と描いた結果の一致、検証済みの `vyos/*.cmds` の行を使っていること、RPS・QoS の shaper・VRF を使わないこと、出力が `tests/test_no_sensitive_data.py` の検査を通ること、site の誤りで止まること。
- `.github/workflows/vyos-config.yaml`: 印に文書用の値を入れ、型によらない部分（`prod-example.cmds`）を基準にして、型 A・B・C と MAP-E の段を 1 本ずつ VyOS のコンテナで commit する。
