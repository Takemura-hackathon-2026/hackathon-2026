# Pi 常駐表示クライアント

Raspberry Pi 上で動く表示専用プロセス。主機から届く 192×96 のパレットインデックス配列を
受信し、固定 LUT で RGB へ変換して HUB75 へ出力するだけの処理に限定する。

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

GPIO を直接叩くため root 権限が必要。`--target-id` は自機の IP 末尾 − 101
（`192.168.10.101` なら `0`）。詳しくは [docs/NETWORK.md](../docs/NETWORK.md)。

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

主機から4台へ転送・Pi上ビルド・systemd有効化まで行う場合は、oyakiからPiへSSHできるユーザーを
指定する。`pi-deploy`はPi上の`$HOME/rpi-rgb-led-matrix`を既定のライブラリ位置として使い、
`PI_RGB_LIB_DISTRIBUTION`で変更できる。Pi側でパスワードなしsudoが使える必要がある。

```bash
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-deploy
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-status
PI_SSH_USER=takemuralab host/oyaki_camera_calibrate.sh pi-start
```

PiのIPと`target_id`は`192.168.10.101`→`0`、`.102`→`1`、`.103`→`2`、`.104`→`3`で固定する。
主機の待機画面は別途`standby-start`で起動する。

| オプション | 既定 | 内容 |
|---|---|---|
| `--target-id N` | （必須） | 担当領域の番号 `0`〜`3`。他機宛のチャンクは捨てる |
| `--port N` | `5000` | フレームチャンクの待受ポート |
| `--health-port N` | `5101` | 死活報告の送信先ポート |
| `--rotate180` | 無効 | パネルを上下逆に取り付けた個体向けに点対称へ写す |
| `--verbose` | 無効 | 60 フレームごとに `frame_id` と累計を出力 |
| `--led-brightness N` | `40` | パネル輝度（0〜100%。指定時は既定値を上書き） |

上記に加えて rpi-rgb-led-matrix の `--led-*` 系オプションをそのまま受ける。
既定のパネル輝度は40%で、全体を低めに抑えている。既定のパネル構成は 32×32・`chain_length=6`・`parallel=3`（= 192×96）、
`hardware_mapping=regular`。

## 8枚直列の単体確認

3台構成へ組み替える前に、HUB75のP0出力だけへ32×32パネルを8枚直列でつなぎ、
番号付きテストを行える。`chain8_test`はP0だけを使い、各パネルへ1〜8の番号と
異なる背景色を低輝度で表示する。通常の`pi_client`サービスは停止してから実行する。

```bash
make chain8_test
sudo ./chain8_test --led-chain=8 --led-parallel=1 \
  --led-pwm-bits=7 --led-slowdown-gpio=4 --led-brightness=10
```

配線は電源断状態で行い、パネル1枚、2枚、4枚、8枚の順に増やして確認する。番号が
途中から欠ける・順番が違う・乱れる場合は、直前に追加したパネルとそのHUB75ケーブルを外して確認する。

## パネルごとの色補正

パネルの個体差は、HUB75の出力レーンとチェーン位置ごとのRGB倍率で補正できる。まず
`panel_calibration.example.conf`をPi上へコピーして編集する。形式は
`lane chain R G B`で、`lane`はP0/P1/P2を`0/1/2`、`chain`はPiから数えて`0`始まりで指定する。
倍率は`0`〜`2`で、色が強いチャンネルは1未満、弱いチャンネルは1超へ少しずつ調整する。

```bash
cp panel_calibration.example.conf panel_calibration.conf
sudo ./pi_client --target-id 0 --panel-calibration "$PWD/panel_calibration.conf"
```

調整中は、制御Piから対象の表示Piだけへ同じ単色を送る。白→グレー→赤→緑→青の順に
中心付近の明るさ・色味を見比べる。ディスプレーPiの`target_id`は通常どおり保持する。

```bash
python3 host/test_mode/panel_calibration.py --pi 192.168.10.101:5000 \
  --target-id 0 --color white
```

補正値を変えたらPiの表示クライアントを再起動して再確認する。スマートフォン撮影で判断する場合は、
自動露出・自動ホワイトバランスをロックする。自動補正なしの色彩計があれば、その測定値を基準にする。

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
