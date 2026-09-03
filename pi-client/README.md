# Pi 常駐表示クライアント

Raspberry Pi 上で動く表示専用プロセス。主機から届く 192×96 のパレットインデックス配列を
受信し、固定 LUT で RGB へ変換して HUB75 へ出力するだけの処理に限定する。

> **現行実機（2026-09-02）:** 表示Piは3台構成で、`pi1`（`192.168.10.101`、
> `target_id 0`）、`pi2`（`.102`、`target_id 1`）、`pi4`（`.104`、`target_id 3`）を使う。
> `192.168.10.103` / `target_id 2` は現行構成に含めない。接続先・表示順の正本は
> [docs/CONNECTION_INFO.md](../docs/CONNECTION_INFO.md) とする。

```text
UDP受信 → 固定位置へmemcpy → CRC・パレット範囲確認 → 裏表バッファ交換 → HUB75出力
```

計画書 §7.2 に対応する。ゲームロジック、画像生成、圧縮・解凍、動的メモリ確保、
外部プロセス起動は行わない。受信バッファは固定長で持ち、実行中に確保し直さない。

**同期段階 A の実装**。READY 返送（UDP 5100）と M5 の GPIO 同期待機は未実装で、
完成フレームを受け取り次第 `SwapOnVSync` で表示する。

## ビルド

[rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) のソースツリーが必要。
`RGB_LIB_DISTRIBUTION` で場所を指定する（既定は `$HOME/rpi-rgb-led-matrix`）。

```bash
sudo apt install build-essential zlib1g-dev
```

```bash
git clone https://github.com/hzeller/rpi-rgb-led-matrix.git ~/rpi-rgb-led-matrix
```

```bash
make RGB_LIB_DISTRIBUTION=$HOME/rpi-rgb-led-matrix
```

`libz` に依存する（CRC32 の検証に `zlib` の `crc32()` を使う）。

## 実行

GPIO を直接叩くため root 権限が必要。`--target-id` は自機が担当する表示領域を指定する。
現行の割当は `.101`→`0`、`.102`→`1`、`.104`→`3`（`.103`→`2`は未使用）。詳しくは
[docs/NETWORK.md](../docs/NETWORK.md)。

```bash
sudo ./pi_client --target-id 0
```

## 起動時の自動起動

`rpi-rgb-led-matrix`自体は常駐プロセスではなく、Pi上でビルドした`pi_client`がリンクして
HUB75を駆動する。`install.sh`はPi上のライブラリを使ってビルドし、
`pi-client@.service`をインストールして、指定した`target_id`のサービスをboot時に有効化する。

Pi単体で設定する場合（Pi1の例）:

```bash
./install.sh 0
systemctl is-enabled pi-client@0.service
systemctl status pi-client@0.service
```

主機から転送・Pi上ビルド・systemd有効化まで行う配備ヘルパーは、リポジトリ上では旧4台構成の
`pi_specs`（`.101`〜`.104`）を使う。現行3台構成では`.103`を含めず、各Piへ個別に
`target_id 0`、`1`、`3`を配備するか、現行の3台用サービス設定を使うこと。
`pi-deploy`は現状のままでは現行運用へ流用しない。

旧4台構成の配備ヘルパーを検証するときは、oyakiからPiへSSHできるユーザーを指定する。
`pi-deploy`はPi上の`$HOME/rpi-rgb-led-matrix`を既定のライブラリ位置として使い、
`PI_RGB_LIB_DISTRIBUTION`で変更できる。Pi側でパスワードなしsudoが使える必要がある。

```bash
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-deploy
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-status
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-start
```

PiのIPと`target_id`は固定する。現行構成では`192.168.10.101`→`0`、`.102`→`1`、
`.104`→`3`で、`.103`→`2`は未使用。主機の待機画面は別途`standby-start`で起動する。

| オプション | 既定 | 内容 |
|---|---|---|
| `--target-id N` | （必須） | 担当領域の番号 `0`〜`3`。現行は `0`、`1`、`3`。他機宛のチャンクは捨てる |
| `--port N` | `5000` | フレームチャンクの待受ポート |
| `--health-port N` | `5101` | 死活報告の送信先ポート |
| `--rotate180` | 無効 | パネルを上下逆に取り付けた個体向けに点対称へ写す |
| `--verbose` | 無効 | 60 フレームごとに `frame_id` と累計を出力 |
| `--led-brightness N` | `40` | パネル輝度（0〜100%。指定時は既定値を上書き） |

上記に加えて rpi-rgb-led-matrix の `--led-*` 系オプションをそのまま受ける。
既定のパネル輝度は40%で、全体を低めに抑えている。既定のパネル構成は 32×32・`chain_length=6`・`parallel=3`（= 192×96）、
`hardware_mapping=regular`。

## 32×32パネル単位の色校正

`--panel-calibration PATH` を指定すると、HUB75キャンバス上の32×32パネルごとに
RGBゲイン（0〜2倍）とRGB共通の輝度倍率（0〜2倍）を適用する。補正対象は論理画面の座標ではなく、
P0/P1/P2の出力レーンとチェーン位置（0始まり）で指定する。未記載のパネルは1.00倍のままなので、
現在の構成に合わせて `3レーン×8枚` の設定ファイルを使用する。5列の旧形式も読み込め、その場合の輝度は1.00倍になる。

`install.sh` は `/etc/hackathon-2026/panel_calibration.conf` を初回だけ作成し、既存の
校正値を上書きしない。systemdサービスはこのファイルを自動的に読み込む。

```text
# lane chain red_gain green_gain blue_gain brightness
0 0 0.95 1.00 1.08 0.80
2 7 1.00 0.92 1.00 1.00
```

`red_gain` / `green_gain` / `blue_gain` は色味の補正、`brightness` はそのパネルのRGB共通の明るさ補正。
最終的には各チャンネルへ `RGBゲイン×brightness` を適用する。

校正値を編集した後は対象Piの表示クライアントを再起動する。

```bash
sudoedit /etc/hackathon-2026/panel_calibration.conf
sudo systemctl restart pi-client@0.service
```

表示確認用に、主機から1台の表示Piへ単色フレームを送れる。`--panel ROW,COL` は
入力スライス上の32×32領域を1枚だけ点灯し、`all` はスライス全体を点灯する。
現行の192×128スライスは4行×6列で、旧192×96スライスを使う場合は
`--slice-height 96` を追加する。制御Piからのゲーム送信と同時に使わず、送信元を停止してから実行する。

```bash
python3 host/test_mode/panel_calibration.py \
  --pi 192.168.10.101:5000 --target-id 0 \
  --panel 0,0 --color white --slice-height 128 --seconds 10
```

校正ツールの自己テストは次で実行する。

```bash
python3 host/test_mode/panel_calibration_selftest.py
make panel_calibration_selftest
./panel_calibration_selftest
```

## 死活報告

1 秒ごとに 1 行の ASCII テキストを UDP で送る。宛先は受信パケットの送信元から学習するため、
主機のアドレスを設定する必要はない。主機側では TEST2（`host/test_mode/test2_status.py`）が
これを受けて表示する。

```text
PIHEALTH target=0 displayed=1234 dropped=2 fps=59.8 up=41 rot=0 temp_c=42.1
```

| 項目 | 内容 |
|---|---|
| `target` | `--target-id` |
| `displayed` | 表示した累計フレーム数 |
| `dropped` | 捨てたフレーム数（未完成、CRC/範囲不正、組み立て失敗） |
| `fps` | 直前の報告からの実表示レート |
| `up` | 起動からの経過秒 |
| `rot` | `--rotate180` が有効なら `1` |
| `temp_c` | PiのCPU温度（摂氏）。取得できない場合は `NA` |

## 受信の扱い

`test_mode.py` と同じ 20 バイトのヘッダー（`!IIBBHHHI`、magic `RLED` = `0x524C4544`）で、
1 スライス 18432 バイトを `--chunk-size`（既定 1200）ずつに分けて受ける。

- magic 不一致、自機宛以外、`chunk_id`/`chunk_count` 不正、長さ不一致、CRC32 不一致は破棄する
- 書き込み位置は「最終チャンク以外の大きさ」を刻み幅として求める。最終チャンクだけは端数に
  なるため自身の大きさから位置を逆算せず、刻み幅が未確定のうちに届いた場合は保留する
- 新しい `frame_id` が来たら、未完成の古いフレームは捨てて先へ進む
- 完成後にパレット範囲外（FC6 は 52 以上、MSX16 は 16 以上）の画素があれば表示しない
- `frame_id` が戻るフレームは捨てる。ただし 600 以上巻き戻った場合は主機の再起動とみなし、
  新しい `frame_id` 系列へ追従する

## 状態

ビルド・実機（LED パネル・Pi）での動作確認は未実施。
