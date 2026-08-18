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

| オプション | 既定 | 内容 |
|---|---|---|
| `--target-id N` | （必須） | 担当領域の番号 `0`〜`3`。他機宛のチャンクは捨てる |
| `--port N` | `5000` | フレームチャンクの待受ポート |
| `--health-port N` | `5101` | 死活報告の送信先ポート |
| `--rotate180` | 無効 | パネルを上下逆に取り付けた個体向けに点対称へ写す |
| `--verbose` | 無効 | 60 フレームごとに `frame_id` と累計を出力 |

上記に加えて rpi-rgb-led-matrix の `--led-*` 系オプションをそのまま受ける。
既定のパネル構成は 32×32・`chain_length=6`・`parallel=3`（= 192×96）、
`hardware_mapping=regular`。

## 死活報告

1 秒ごとに 1 行の ASCII テキストを UDP で送る。宛先は受信パケットの送信元から学習するため、
主機のアドレスを設定する必要はない。主機側では TEST2（`host/test_mode/test2_status.py`）が
これを受けて表示する。

```text
PIHEALTH target=0 displayed=1234 dropped=2 fps=59.8 up=41 rot=0
```

| 項目 | 内容 |
|---|---|
| `target` | `--target-id` |
| `displayed` | 表示した累計フレーム数 |
| `dropped` | 捨てたフレーム数（未完成、CRC/範囲不正、組み立て失敗） |
| `fps` | 直前の報告からの実表示レート |
| `up` | 起動からの経過秒 |
| `rot` | `--rotate180` が有効なら `1` |

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
