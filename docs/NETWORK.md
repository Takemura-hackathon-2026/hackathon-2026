# ネットワーク設計（現行3台表示構成）

計画書 §2・§5 に対応する実配線・アドレス設計。現行実機の接続先・台数は
[docs/CONNECTION_INFO.md](CONNECTION_INFO.md)を正本とする。

現行は制御Pi `pi3-control`、センサーPi `pi3-sensor`、表示Pi 3台（`.101`、`.102`、`.104`）で構成する。
本書に残る4台の送信・テスト例は旧構成の検証用であり、現行運用の接続先には`.103`を含めない。

## セグメント

| 記号 | 用途 | 経路 | ネットワーク | 備考 |
|---|---|---|---|---|
| A | フレーム配信 | 制御Pi `192.168.10.2` — 1GbEスイッチ — 表示Pi ×3 | `192.168.10.0/24` | 専用有線。ここに他機器を接続しない |
| B | 開発機直結 | 親機 `enp3s0` — Mac `en9`（USB-Ethernet） | `192.168.20.0/24` | SSH・転送・インターネット共有 |

セグメントAは60fpsのフレーム配信専用とし、ブロードキャストを出す機器やWi-Fiブリッジを混ぜない。

## アドレス割当

### セグメントA（フレーム配信）

| 機器 | ホスト名 | IP | `target_id` | 現行の役割 |
|---|---|---|---:|---|
| 制御Pi（表示LAN側） | `pi3-control` | `192.168.10.2` | — | 3台へ送信 |
| 表示機1 | `pi1` | `192.168.10.101` | `0` | 表示順1、UDP受信 |
| 表示機2 | `pi2` | `192.168.10.102` | `1` | 表示順2、UDP受信 |
| 表示機3 | `pi4` | `192.168.10.104` | `3` | 表示順3、UDP受信 |
| 旧表示機3 | `pi3` | `192.168.10.103` | `2` | 現行構成では未接続 |
| Ubuntu主機（管理・開発） | `th1` | `192.168.10.1` | — | 現行の送信元ではない |
| 保守用PC等 | — | `192.168.10.200`–`254` | — | 常設しない |

規約:

- **`target_id` = 第4オクテット − 101**。現行の表示Piは`0`、`1`、`3`で、`2`は未使用。
  `pi_client` 側は `--target-id` で明示し、自機宛以外のチャンクは捨てる。
- 表示順は`1`、`2`、`3`だが、IP・`target_id`の連番とは一致しない。表示順3は`pi4`（`.104`、`target_id 3`）。
- 上表の「担当行」は縦一列（192×384）に積んだ通常配置の場合。TEST4（SUPERTESTMODE）のように
  縦一列でない物理配置では、`target_id` と IP の対応はそのままに、割り当てる領域だけを
  主機側（`test4_super.py` の `LAYOUT`）で差し替える。
- DHCPは使わない。起動順で番号が入れ替わると、上下の領域が入れ替わったまま気づけないため。
- このセグメントにデフォルトゲートウェイを設定しない（`ipv4.never-default yes`）。
- DNSも設定しない。相互参照はIP直書き。

### セグメントB（開発機直結）

| 機器 | IP | 備考 |
|---|---|---|
| 親機 `enp3s0` | `192.168.20.1/24` | `169.254.10.1/16` も併記（旧設定の退避用） |
| Mac `en9` | `192.168.20.50/24` | インターネット共有時は親機のゲートウェイになる |

## ポート

| 用途 | 方向 | プロトコル | ポート | 状態 |
|---|---|---|---|---|
| フレームチャンク | 現行: 制御Pi → 表示Pi 3台／旧汎用経路: 主機 → 各Pi | UDP | `5000`（各Pi側で待受） | 実装済み |
| READY 応答 | 各Pi → 制御Pi（現行） | UDP | `5100`（制御Pi側で待受） | **未実装**（同期段階B） |
| 死活・診断（FPS、欠損数） | 各Pi → 制御Pi（現行） | UDP | `5101`（制御Pi側で待受） | 実装済み。`pi_client` が1秒ごとに送出、TEST2 が受信 |
| SSH | 開発機 → 各機 | TCP | `22` | — |

UDPペイロードは1200バイト固定（`--chunk-size`）。MTU 1500のままIPフラグメントを避ける。

`pi_client` は主機のアドレスを受信パケットの送信元から学習するため、死活報告の宛先設定は不要。
報告は1行のASCIIテキストで、内容は次のとおり。

```text
PIHEALTH target=0 displayed=1234 dropped=2 fps=59.8 up=41 rot=0 temp_c=42.1
```

## 設定コマンド

いずれもNetworkManager（`nmcli`）を使う。netplanの `90-NM-*.yaml` はNMの生成物なので手編集しない。

### 旧主機 `enp2s0`（旧セグメントAの直接送信例）

現行は制御Pi `pi3-control` が表示LANの送信元であり、以下はUbuntu主機から直接送信していた
旧構成の設定例として残す。実機の現行サービスを変更する手順ではない。旧来の
`netplan-enp2s0` プロファイルと競合する場合は、先に自動接続を止める。

```bash
sudo nmcli con mod netplan-enp2s0 connection.autoconnect no
sudo nmcli con add type ethernet ifname enp2s0 con-name led-net \
  ipv4.method manual ipv4.addresses 192.168.10.1/24 \
  ipv4.never-default yes ipv6.method link-local connection.autoconnect yes
sudo nmcli con up led-net
```

### Raspberry Pi 各機（Raspberry Pi OS Bookworm 以降、NetworkManager）

`pi1`（`.101`、`target_id 0`）の例。現行の他機は`pi2`（`.102`、`target_id 1`）と
`pi4`（`.104`、`target_id 3`）に合わせる。`.103`は現行構成では設定しない。

```bash
sudo nmcli con add type ethernet ifname eth0 con-name led-net \
  ipv4.method manual ipv4.addresses 192.168.10.101/24 \
  ipv4.never-default yes ipv6.method disabled connection.autoconnect yes
sudo nmcli con up led-net
sudo hostnamectl set-hostname pi1
```

### 親機 `enp3s0`（セグメントB、管理・開発用）

```bash
sudo nmcli con add type ethernet ifname enp3s0 con-name mac-direct \
  ipv4.method manual ipv4.addresses "192.168.20.1/24,169.254.10.1/16" \
  ipv4.never-default yes ipv6.method link-local connection.autoconnect yes
```

## インターネット共有（Mac → 親機）

親機はデフォルトルートもDNSも持たない。`apt` や `pip` を使うときだけ、MacのWi-Fi（`en0`）経由でNATする。

### Mac側（一時設定、再起動で消える）

```bash
sudo sysctl -w net.inet.ip.forwarding=1
```

```bash
sudo pfctl -f docs/nat.conf -E
```

解除:

```bash
sudo pfctl -d && sudo sysctl -w net.inet.ip.forwarding=0
```

### 親機側

共有を使うときだけデフォルトルートを向ける。

```bash
sudo nmcli con mod mac-direct ipv4.never-default no ipv4.gateway 192.168.20.50 ipv4.dns "1.1.1.1 8.8.8.8"
sudo nmcli con up mac-direct
```

使い終わったら戻す（フレーム配信中に外向き通信が混ざるのを避ける）。

```bash
sudo nmcli con mod mac-direct ipv4.never-default yes ipv4.gateway "" ipv4.dns ""
sudo nmcli con up mac-direct
```

## 確認手順（現行3台）

```bash
for ip in 192.168.10.101 192.168.10.102 192.168.10.104; do
  ping -c 1 "$ip"
done
```

```bash
ssh oyaki "for i in 1 2 4; do ping -c1 -W1 192.168.10.10\$i >/dev/null && echo pi\$i OK || echo pi\$i NG; done"
```

各表示Pi側で表示クライアントを手動インストールする場合:

```bash
./install.sh 0  # pi1 / .101
./install.sh 1  # pi2 / .102
./install.sh 3  # pi4 / .104
```

### 旧配備ヘルパーの注意

`host/oyaki_camera_calibrate.sh` の`pi-deploy`、`pi-status`、`pi-start`は旧4台構成の
`pi_specs`を使うため、現行3台へそのまま実行しない。Pi側で個別にビルド・有効化するか、
3台対応の配備処理を使う。Pi側でパスワードなしsudoと、`$HOME/rpi-rgb-led-matrix`の配置が必要。

```bash
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-deploy
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-status
```

個別に再起動する場合:

```bash
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-start
```

## 旧4台構成の検証（現行運用では使用しない）

テストモードの`send()`は4分割と4宛先を要求するため、以下は旧構成またはローカル結合試験用に残す。

4台へ実際にフレームを送る:

```bash
cd host/test_mode && python3 test_mode.py --image test.webp --palette fc6 --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.102:5000 \
  --pi 192.168.10.103:5000 --pi 192.168.10.104:5000
```

各Piの死活・FPS・欠損数を主機側で見る（TEST2。UDP 5101 の報告を受けて文字で表示する）:

```bash
cd host/test_mode && python3 test2_status.py --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.102:5000 \
  --pi 192.168.10.103:5000 --pi 192.168.10.104:5000
```

`NO SIGNAL` は一度も報告が届いていない状態、`LOST` は3秒以上途絶えた状態を指す。

## 現状（2026-09-02）

| 項目 | 状態 |
|---|---|
| Mac `en9` → 表示LAN | `192.168.10.50/24`を保持し、`.101/.102/.104`へSSH疎通確認済み |
| 表示Pi | `pi1`（`.101`）、`pi2`（`.102`）、`pi4`（`.104`）の3台 |
| `.103` / `pi3` | ARP応答なし。現行表示機ではない |
| 制御Pi | `pi3-control`（`.2`）の`pi3-control.service` activeを確認 |
| センサーPi | `pi3-sensor`（セグメントB `.33`） |
| フレーム配信（UDP 5000） | 現行制御Piサービスは3台へ送信。汎用テスト経路は4台前提 |
| 死活報告（UDP 5101） | 現行表示Piから制御Piへ送信する構成 |
| READY バリア（UDP 5100） | 未実装 |

現行の接続先・台数を変更する場合は、先に[docs/CONNECTION_INFO.md](CONNECTION_INFO.md)を更新する。
