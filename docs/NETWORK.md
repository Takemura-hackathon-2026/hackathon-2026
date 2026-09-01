# ネットワーク設計

計画書 §2・§5 に対応する実配線・アドレス設計。2026-08-06 時点の実機構成に基づく。

## セグメント

| 記号 | 用途 | 経路 | ネットワーク | 備考 |
|---|---|---|---|---|
| A | フレーム配信 | 親機 `enp2s0` — 1GbEスイッチ — Pi ×3 | `192.168.10.0/24` | 専用有線。ここに他機器を接続しない |
| B | 開発機直結 | 親機 `enp3s0` — Mac `en7`（USB-Ethernet） | `192.168.20.0/24` | SSH・転送・インターネット共有 |

セグメントAは60fpsのフレーム配信専用とし、ブロードキャストを出す機器やWi-Fiブリッジを混ぜない。

## アドレス割当

### セグメントA（フレーム配信）

| 機器 | ホスト名 | IP | `target_id` | 担当行（論理画面 192×384） |
|---|---|---|---:|---|
| 親機（Ubuntu） | `th1` | `192.168.10.1` | — | 全体を描画・3分割 |
| Raspberry Pi 1 | `pi1` | `192.168.10.101` | `0` | Y = 0–127（最上段） |
| Raspberry Pi 4 | `pi4` | `192.168.10.104` | `1` | Y = 128–255 |
| Raspberry Pi 2 | `pi2` | `192.168.10.102` | `2` | Y = 256–383（最下段） |
| 保守用PC等 | — | `192.168.10.200`–`254` | — | 常設しない |

`192.168.10.103`（旧`pi3`）は焼損のため、現在の構成から除外している。

規約:

- **`target_id` は物理的な段の上から順に採番**する。現在は`.101`→`0`、`.104`→`1`、`.102`→`2`で、IPの数値差から機械的には算出しない。
  `pi_client` 側は `--target-id` で明示し、自機宛以外のチャンクは捨てる。
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
| Mac `en7` | `192.168.20.50/24` | インターネット共有時は親機のゲートウェイになる |

## ポート

| 用途 | 方向 | プロトコル | ポート | 状態 |
|---|---|---|---|---|
| フレームチャンク | 主機 → 各Pi | UDP | `5000`（各Pi側で待受） | 実装済み |
| READY 応答 | 各Pi → 主機 | UDP | `5100`（主機側で待受） | **未実装**（同期段階B） |
| 死活・診断（FPS、欠損数） | 各Pi → 主機 | UDP | `5101`（主機側で待受） | 実装済み。`pi_client` が1秒ごとに送出、TEST2 が受信 |
| SSH | 開発機 → 各機 | TCP | `22` | — |

UDPペイロードは1200バイト固定（`--chunk-size`）。MTU 1500のままIPフラグメントを避ける。

`pi_client` は主機のアドレスを受信パケットの送信元から学習するため、死活報告の宛先設定は不要。
報告は1行のASCIIテキストで、内容は次のとおり。

```text
PIHEALTH target=0 displayed=1234 dropped=2 fps=59.8 up=41 rot=0 temp_c=42.1
```

## 設定コマンド

いずれもNetworkManager（`nmcli`）を使う。netplanの `90-NM-*.yaml` はNMの生成物なので手編集しない。

### 親機 `enp2s0`（セグメントA）

既存の `netplan-enp2s0` プロファイルと競合するため、先に自動接続を止める。

```bash
sudo nmcli con mod netplan-enp2s0 connection.autoconnect no
sudo nmcli con add type ethernet ifname enp2s0 con-name led-net \
  ipv4.method manual ipv4.addresses 192.168.10.1/24 \
  ipv4.never-default yes ipv6.method link-local connection.autoconnect yes
sudo nmcli con up led-net
```

### Raspberry Pi 各機（Raspberry Pi OS Bookworm 以降、NetworkManager）

`pi1` の例。各機のIP・`target_id`は上表に合わせて設定する。

```bash
sudo nmcli con add type ethernet ifname eth0 con-name led-net \
  ipv4.method manual ipv4.addresses 192.168.10.101/24 \
  ipv4.never-default yes ipv6.method disabled connection.autoconnect yes
sudo nmcli con up led-net
sudo hostnamectl set-hostname pi1
```

### 親機 `enp3s0`（セグメントB、設定済み）

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

## 確認手順

```bash
ping -c 3 192.168.10.101
```

```bash
ssh oyaki "for ip in 192.168.10.101 192.168.10.104 192.168.10.102; do ping -c1 -W1 \$ip >/dev/null && echo \$ip OK || echo \$ip NG; done"
```

各Pi側で表示クライアントを手動インストールする場合（`target_id` は上表の値）:

```bash
./install.sh 0
```

通常は主機から3台へ転送・Pi上ビルド・systemd自動起動を一括で設定する。`PI_SSH_USER`はPi側の
実ユーザー名に置き換える。Pi側でパスワードなしsudoと、`$HOME/rpi-rgb-led-matrix`の配置が必要。

```bash
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-deploy
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-status
```

個別に再起動する場合:

```bash
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-start
```

3台へ実際にフレームを送る:

```bash
cd host/test_mode && python3 test_mode.py --image test.webp --palette fc6 --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.104:5000 \
  --pi 192.168.10.102:5000
```

各Piの死活・FPS・欠損数を主機側で見る（TEST2。UDP 5101 の報告を受けて文字で表示する）:

```bash
cd host/test_mode && python3 test2_status.py --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.104:5000 \
  --pi 192.168.10.102:5000
```

`NO SIGNAL` は一度も報告が届いていない状態、`LOST` は3秒以上途絶えた状態を指す。

## 現状

2026-08-06 時点。

| 項目 | 状態 |
|---|---|
| セグメントB（Mac直結） | 疎通確認済み。`ssh oyaki` で鍵認証 |
| セグメントA（Pi hub） | **未設定**。親機 `enp2s0` はリンク検出済み・IP未割当、Pi側も未設定 |
| インターネット共有 | 未適用 |
| フレーム配信（UDP 5000） | 主機側・Pi側とも実装済み。実機での疎通は未確認 |
| 死活報告（UDP 5101） | 実装済み。実機での疎通は未確認 |
| READY バリア（UDP 5100） | 未実装 |
