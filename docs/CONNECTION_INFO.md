# 接続情報（現行構成の正本）

RGB LED インタラクティブゲームの接続先・認証・通信ポートを平文でまとめたもの。

> **現行実機の正本:** 2026-09-02時点では、制御Pi `pi3-control`、センサーPi
> `pi3-sensor`、表示Pi 3台（`.101`、`.102`、`.104`）で運用する。
> `docs/NETWORK.md` と開発計画書に残る4台構成は設計原案または旧テスト用であり、
> 現行の接続先を判断するときは本書を優先する。

## 現行構成（2026-09-02）

| 区分 | 台数 | 接続先 |
|---|---:|---|
| 表示機 | 3台 | `192.168.10.101`、`192.168.10.102`、`192.168.10.104` |
| 制御機 | 2台 | ゲーム制御 `192.168.50.32`、センサー `192.168.50.33` |
| 管理・開発接続 | 別枠 | Mac → `oyaki`（Ubuntu） |

## 1. Mac から Ubuntu 主機（oyaki）へ接続

| 項目 | 値 |
|---|---|
| SSH エイリアス | `oyaki` |
| ユーザー | `th1` |
| IPv4 接続先 | `192.168.20.1` |
| ポート | TCP `22` |
| 認証 | SSH 公開鍵認証 |
| 秘密鍵 | `~/.ssh/id_ed25519` |
| Mac 側インターフェース | `en9`（AX88179A） |
| Mac 側 IP | `192.168.20.50/24` |
| Ubuntu 側インターフェース | `enp3s0` |
| Ubuntu 側 IP | `192.168.20.1/24` |
| 旧設定の退避 IP | Ubuntu 側 `169.254.10.1/16` |

通常の接続:

```bash
ssh oyaki
```

`~/.ssh/config` の `oyaki` 定義は次の内容。

```sshconfig
Host oyaki
    HostName 192.168.20.1
    User th1
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30
```

### 配備スクリプトが使う接続先

`host/oyaki_camera_calibrate.sh` は、通常のSSH設定を上書きして次を使う。

| 項目 | 値 |
|---|---|
| 接続先エイリアス | `oyaki` |
| 接続先ホスト名 | `fe80::f56f:3e9f:fbb:3a85%%en9` |
| 実際のIPv6スコープ表記 | `fe80::f56f:3e9f:fbb:3a85%en9` |
| SSHユーザー | `th1`（`~/.ssh/config`から継承） |
| HostKeyAlias | `192.168.20.1` |
| 接続タイムアウト | 10秒 |
| リモートリポジトリ | `/home/th1/hackathon-2026` |

`%%` はOpenSSH設定内で文字通りの `%` を渡すための記法。

`docs/NETWORK.md` にはMac側インターフェースが `en7` と記載されているが、現在のMacでは
USB Ethernetが `en9` として認識されている。配備スクリプトの既定値と現在の実測は `en9`。

## 2. 表示用専用LAN（現行3台構成）

経路は「制御Piの表示LAN側 `192.168.10.2` → 1GbEスイッチ → 表示Pi 3台」。
固定IPを使い、DHCP・デフォルトゲートウェイ・DNSは設定しない。

| 機器 | ホスト名 | IPアドレス | `target_id` | 表示順 | フレーム受信 |
|---|---|---:|---:|---:|---|
| 制御Pi（表示LAN側） | `pi3-control` | `192.168.10.2/24` | — | — | 3台へ送信 |
| 表示機1 | `pi1` | `192.168.10.101/24` | `0` | `1` | UDP `5000` |
| 表示機2 | `pi2` | `192.168.10.102/24` | `1` | `2` | UDP `5000` |
| 表示機3 | `pi4` | `192.168.10.104/24` | `3` | `3` | UDP `5000` |
| Ubuntu主機（管理・開発） | `th1` | `192.168.10.1/24` | — | — | 現行の送信元ではない |

表示機の現行IP割当は `.101`、`.102`、`.104` で、`target_id` は `0`、`1`、`3`。
`.103` / `target_id 2` は現行の表示機には含めない。表示順と `target_id` は連番ではないため、
校正・診断時も `.104` には `target_id 3` を指定する。

リポジトリの汎用テストモード・配備ヘルパーには旧4台構成の前提が残る。
現行3台の実機運用では、制御Piへ配備済みの3台用サービス設定を正本とする。

### PiへのSSH

配備スクリプトが使うPi側ユーザーは `takemuralab`。

```bash
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-deploy
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-status
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-start
```

Pi側では、`takemuralab` からパスワードなしで `sudo -n` が実行できる必要がある。

## 3. 通信ポート

| 用途 | 方向 | プロトコル | 宛先/待受 | 状態 |
|---|---|---|---|---|
| LEDフレームチャンク | 制御Pi `192.168.10.2` → 表示機 `.101/.102/.104` | UDP | 各表示機 `:5000` | 実装済み |
| Pi死活・FPS・欠損報告 | 表示機 `.101/.102/.104` → 制御Pi `192.168.10.2` | UDP | 制御Pi `:5101` | 実装済み、1秒ごと |
| READYバリア | 表示機 `.101/.102/.104` → 制御Pi `192.168.10.2` | UDP | 制御Pi `:5100` | 未実装 |
| SSH | Mac/主機 → 接続先 | TCP | `:22` | SSH鍵認証 |

フレームチャンクの既定サイズは `1200` バイト。

## 4. 現行の制御機2台

サービス定義・READMEに記載された構成。

| ノード | ホスト名 | セグメントB IP | 表示LAN IP | 役割 |
|---|---|---:|---:|---|
| 制御Pi | `pi3-control` | `192.168.50.32` | `192.168.10.2` | ゲーム描画、入力受信、3台の表示Piへ送信 |
| センサーPi | `pi3-sensor` | `192.168.50.33` | — | STRUCTURE Sensor判定、入力送信 |

| 通信 | 方向 | プロトコル | ポート |
|---|---|---|---:|
| 判定済み入力 | センサーPi `.33` → 制御Pi `.32` | UDP | `5200` |
| 人物再選択通知 | 制御Pi `.32` → センサーPi `.33` | UDP | `5201` |

起動パラメータ:

```bash
# センサーPi（192.168.50.33）
python3 host/sensor_agent.py \
  --destination 192.168.50.32:5200 \
  --control-bind 0.0.0.0:5201 \
  --start-mode still

# 制御Pi（192.168.50.32、表示LAN側は192.168.10.2）
python3 host/block_breaker.py \
  --input-bind 0.0.0.0:5200 \
  --sensor-control 192.168.50.33:5201 \
  --start-mode still --send --no-preview --palette msx16 \
  --pi 192.168.10.101:5000 \
  --pi 192.168.10.102:5000 \
  --pi 192.168.10.104:5000
```

深度画像そのものはLANへ送らず、判定済み入力だけをUDP送信する。

上の接続先は現行3台構成に合わせたもの。実機の `pi3-control` サービスも
`.101/.102/.104` の3台へ送信する。一方、リポジトリ内の汎用送信処理・テストモード・
配備用systemdテンプレートには4台構成の前提が残るため、旧4台用コマンドを現行運用へ
そのまま流用しない。

## 5. Macから主機へのインターネット共有

一時的な開発用設定。MacのWi-Fi `en0` を上流として、`192.168.20.0/24` から外へNATする。

```bash
sudo sysctl -w net.inet.ip.forwarding=1
sudo pfctl -f docs/nat.conf -E
```

解除:

```bash
sudo pfctl -d
sudo sysctl -w net.inet.ip.forwarding=0
```

主機側で共有を使うときだけ、デフォルトルートとDNSを設定する。

```bash
sudo nmcli con mod mac-direct \
  ipv4.never-default no \
  ipv4.gateway 192.168.20.50 \
  ipv4.dns "1.1.1.1 8.8.8.8"
sudo nmcli con up mac-direct
```

## 6. 2026-09-02時点の接続実測

このMarkdown作成時にMac上から確認した結果。

| 確認対象 | 結果 |
|---|---|
| Mac `en9` | UP、`192.168.20.50` と `192.168.10.50` を保持 |
| `192.168.20.1` ping | 応答なし |
| `ssh -o BatchMode=yes -o ConnectTimeout=5 oyaki` | TCP `22` がタイムアウト |
| 配備スクリプト既定のIPv6接続先 | TCP `22` がタイムアウト |
| `192.168.10.101` / `pi1` | ping・SSH応答あり |
| `192.168.10.102` / `pi2` | ping・SSH応答あり |
| `192.168.10.103` | ARP応答なし。現行表示機ではない |
| `192.168.10.104` / `pi4` | ping・SSH応答あり |
| `192.168.10.2` / `pi3-control` | SSH応答あり、`pi3-control.service` active |
| `192.168.50.32/.33` | 現行サービス設定上、制御Pi／センサーPiとして使用 |

したがって、現行の表示機3台（`.101/.102/.104`）と制御PiのSSH・サービス構成を確認済み。
`.103` は再起動後もARP応答がなく、現行構成の対象外である。

## 7. 参照元

- `docs/NETWORK.md`
- `docs/nat.conf`
- `host/oyaki_camera_calibrate.sh`
- `host/pi3-sensor.service`
- `host/pi3-control.service`
- `pi-client/pi-client@.service`
- `README.md`
- `host/test_mode/README.md`
- `pi-client/README.md`
- Mac側 `~/.ssh/config`

パスワード、APIキー、トークンはリポジトリ内に記載されていない。SSHは鍵認証を使用する。
