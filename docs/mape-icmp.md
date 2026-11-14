MAP-E で IPv4 の ping だけが通らなかった件
=========================================

-	記録: 2026-11-15
-	対象: 本番の PC02（VyOS 2026.10.16-0024-rolling、光クロス・enひかり・v6プラス）
-	扱い: **試験の機能**。生の nftables で補っており、`CLAUDE.md` §1 ルール 10（本番の機能は VyOS の `set` だけで）の例外。2026-11-15 に Yusuke が決めた

---

起きたこと
----------

家の LAN を PC02 に切り替えたあと、IPv4 の Web は開くのに、`ping 8.8.8.8` だけが 100% 落ちた。GitHub のような IPv4 だけのサイトにも届き、外の DNS（`8.8.8.8` へ UDP）も答える。通らないのは ICMP だけだった。

最初はMAP-E のトンネルか NAT のどこかが壊れていると考えた。ところが TCP と UDP は同じトンネルを通って外に出ている。トンネルが壊れているなら、Web も開かないはずである。

| 試したこと（Mac から、Wi-Fi で PC02 の配下） | 結果 |
|---|---|
| `curl -4 http://1.1.1.1/` | 301、0.02 秒 |
| `curl -4 https://github.com/` | 200、0.15 秒 |
| `dig @8.8.8.8 example.com` | NOERROR |
| `ping 8.8.8.8` | 100% loss |

原因
----

PC02 の tun0（MAP-E のトンネル）で見ると、ping は MAP-E の IPv4 アドレスで出ていた。送信元のアドレスは正しく変換されている。

```
IP <MAP-E の IPv4> > 8.8.8.8: ICMP echo request, id 16899, seq 1, length 64
IP <MAP-E の IPv4> > 8.8.8.8: ICMP echo request, id 16899, seq 2, length 64
```

返事は 1 つも戻らなかった。手がかりは `id 16899` のほうにある。

v6プラスの MAP-E では、1 つの IPv4 アドレスを複数の契約者で分け合い、契約者ごとに使える送信元ポートが決まっている（PC02 の場合は 16 個ずつ 15 組、計 240 個。最初の組は 6496〜6511）。ICMP にはポートが無いので、echo の識別子（ID）がポートの代わりに使われる。ID が自分の範囲の外にあると、v6プラスの出口（BR）は、その ping をどの契約者に返せばよいか決められず、捨てる。16899 は範囲の外だった。

TCP と UDP が通るのは、NAT の規則 101〜115 が送信元ポートを範囲の中へ振り直しているからである（`vyos/mape-portsets.cmds`、`scripts/mape_calc.py`）。ICMP が当たるのは規則 199 で、こちらはアドレスだけを変える。Linux の NAT は、変換先にポートの範囲を指定されなければ、ICMP の ID を元のまま残す。端末が選んだ ID がそのまま BR に届いていた。

VyOS の set で直せるか
----------------------

直せそうに見えたのは、ICMP にも変換先のポートの範囲を付ける形だった。

```
set nat source rule 198 outbound-interface name 'tun0'
set nat source rule 198 protocol 'icmp'
set nat source rule 198 source group network-group 'MAPE-LAN'
set nat source rule 198 translation address '<MAP-E の IPv4>'
set nat source rule 198 translation port '6496-6511'
```

commit は通らなかった。

```
Source NAT configuration error in rule 198: ports can only be specified
when protocol is either tcp, udp or tcp_udp!
```

VyOS の本体（vyos-1x）を読むと、この制限ははっきり書かれている。

-	`src/conf_mode/nat.py` の `verify_rule`（82〜90 行目）は、変換先や送信元・宛先にポートを書いた規則で、プロトコルが `tcp`・`udp`・`tcp_udp` 以外なら拒む
-	`python/vyos/nat.py`（114〜120 行目）にある変換の選択肢は、`translation options address-mapping persistent` と `port-mapping random / fully-random` だけで、番号を決まった範囲に収める指定は無い

一般的な直し方として「プロトコルを `tcp_udp` にする」か「ポートの指定を外す」がよく挙がる。どちらもエラーは消える。前者は規則が ICMP に当たらなくなり、後者は規則 199 と同じ「アドレスだけを変える」規則になる。どちらにしても ID は範囲の外のまま出ていく。エラーが消えることと、ping が通ることは別の話だった。

nftables の側には、この制限が無い。`snat to <アドレス>:<範囲>` は ICMP にも書けて、ID を範囲の中へ振り直す。VyOS が拒んでいるのは設定の検査の段階で、カーネルの NAT は対応している。

補い方（試験の機能）
--------------------

VyOS が作る NAT の表（`vyos_nat`）には手を入れず、別の表 `mape_icmp` を足した。VyOS は commit のたびに `vyos_nat` を作り直すが、自分で作っていない表は消さない。優先度は 99 とし、VyOS の `vyos_nat`（srcnat、100）より先に評価させる。同じフックの NAT は先に決めたものが使われるので、ICMP だけがこちらで変換される。

```
table ip mape_icmp {
	chain postrouting {
		type nat hook postrouting priority 99; policy accept;
		oifname "tun0" ip protocol icmp ip saddr { 192.168.2.0/24, 192.168.20.0/24, 192.168.30.0/24, 192.168.40.0/24, 192.168.50.0/29 } counter snat to <MAP-E の IPv4>:6496-6511
	}
}
```

-	PC02 では `/config/scripts/mape-icmp.nft` に置き、`/config/scripts/vyos-postconfig-bootup.script` の最後の行（`/usr/sbin/nft -f /config/scripts/mape-icmp.nft`）で起動のたびに読み込む
-	`nft -c -f`（検査だけ）を通してから入れた
-	入れたあと、PC02 から（送信元 192.168.2.1）も Mac からも `ping 8.8.8.8` が 0% loss になった（Mac から約 11 ms）。規則の `counter` も増えた

### 気をつけること

-	**VyOS の設定に現れない。** `show configuration commands` にも `configs/pc02/` の書き出しにも出てこないので、この文書が唯一の記録になる。PC02 を作り直すときは、この表を入れ直す
-	**委任プレフィックスが変わったら直す。** MAP-E の IPv4 と範囲はプレフィックスから決まる（`scripts/mape_calc.py`）。`CLAUDE.md` §6-5 の手順でプレフィックスを入れ直したら、このファイルも書き直す
-	**ID は 16 個しかない。** 最初の 1 組（6496〜6511）だけを使っている。同時に 16 を超える ping の流れがあると足りなくなる。家庭の使い方では足りる見込みで、足りなければ範囲を広げる
-	**ルータ自身の ping は対象外。** 家の網（`saddr` の 5 つ）から出る ICMP だけを変換する。PC02 自身が MAP-E の IPv4 で出す ICMP は、これまでどおり範囲の外の ID で出る
-	**再起動で読み込まれるかは、まだ確かめていない。** 起動スクリプトに書いたところまで。次に再起動したとき `sudo nft list table ip mape_icmp` で確かめる

VyOS への提案
-------------

`set` だけで書けるのが本来の形である。vyos.dev に、ICMP（とプロトコル `all`）にも変換先のポートの範囲を許すよう提案する。カーネルの NAT は ICMP の ID の範囲の指定に対応しているので、変えるのは主に `verify_rule` の検査と、nftables の文を組み立てる部分だと見ている（未確認）。起票したら、ここに課題の番号を書く。

-	起票: （未）
