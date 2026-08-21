# WebP DVDテストモード

主機上のローカル`test.webp`を読み込み、192×384の論理画面内でDVDプレイヤーのロゴのように反射移動させる。色は FC6（52色）または MSX16（16色）の登録済みインデックスだけを使う。着色は色相を選び直さず、パレット定義順の番号を開始番号から最終番号（FC6:51 / MSX16:15）まで順繰りに使う。

計画書 §4.7 の WBMP テストモードを、素材 WebP 対応へ置き換えたもの。

- AI・カメラ処理なし
- 主機で移動、反射、着色、パレット量子化、全画面描画、4分割まで実施
- 1画素1バイトのパレットインデックス（FC6: `0x00〜0x33` / MSX16: `0x01〜0x0F`）
- Pi 側は受信、バッファ交換、HUB75 出力のみ

## 描画モード

| モード | 内容 | 検証対象 |
|---|---|---|
| `image`（既定） | WebP の各画素を最近傍色でパレット量子化して表示 | パレット量子化経路、色再現 |
| `mask` | 輝度しきい値で1bit化し、パレット定義順の番号を帯ごとに順繰り割当 | 全インデックスの表示確認、従来の WBMP テストモード相当 |

## 実行

```bash
python3 test_mode.py --image test.webp --palette fc6
```

```bash
python3 test_mode.py --image test.webp --palette msx16 --render-mode mask
```

操作キー:

| キー | 動作 |
|---|---|
| `F` | FC6へ切替 |
| `M` | MSX16へ切替 |
| `P` | パレット切替 |
| `I` | 描画モード切替（image / mask） |
| `Space` | 一時停止 |
| `R` | 位置・色位相をリセット |
| `Q` / `Esc` | 終了 |

## 4台のPiへ送信

```bash
python3 test_mode.py --image test.webp --palette fc6 --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.102:5000 \
  --pi 192.168.10.103:5000 --pi 192.168.10.104:5000
```

4台に同じ`frame_id`を付け、上から順に192×96ずつ送る。M5の`FRAME_SYNC`および Pi 側の READY バリアは表示クライアント側で処理する（本リポジトリ未実装）。

## 1台PCでのUDP結合試験

端末1:

```bash
python3 udp_preview.py --port 5000
```

端末2:

```bash
python3 test_mode.py --image test.webp --no-preview --send \
  --pi 127.0.0.1:5000 --pi 127.0.0.1:5000 \
  --pi 127.0.0.1:5000 --pi 127.0.0.1:5000
```

## GIF 書き出し

DVD プレイヤーのデモと同じく、反射のたびに次の色番号へ進む動きを GIF で保存する。

```bash
python3 test_mode.py --render-mode mask --color-style solid --rainbow-hz 0 \
  --speed-x 97 --speed-y 131 --gif-seconds 20 --gif ../../docs/assets/test_mode_dvd.gif
```

- `--rainbow-hz 0` で時間経過による色送りを止め、**反射のときだけ**色が変わる
- `--color-style solid` で図形全体が 1 色になる（`cycle` にすると帯ごとにパレット順で着色したまま動く）
- `--invert` を付けると白地ではなく人物シルエット側が動く
- 実時間ではなく固定の時間刻みで進めるため、同じ引数なら毎回同じ GIF になる
- GIF のパレットとインデックスは送出値と同一（FC6 なら 52 色がそのまま GIF パレットに入る）

| オプション | 既定 | 内容 |
|---|---|---|
| `--gif PATH` | なし | 指定すると GIF を保存して終了（プレビュー・送信はしない） |
| `--gif-seconds` | 8.0 | 長さ [秒] |
| `--gif-fps` | 20.0 | フレームレート |
| `--gif-scale` | 2 | 最近傍拡大率 |

## 全色テストパターン

計画書 §13 の実装開始条件 2・3（FC6 全52色 / MSX16 全16色）。

```bash
python3 palette_check.py --palette fc6
```

```bash
python3 palette_check.py --palette msx16 --no-preview --save msx16_pattern.png
```

`--send --pi ...` を付けると同じパターンを4台へ送信できる。Pi 境界（Y=96/192/288）に1pxの目印を入れてある。

## 機械的検証

```bash
python3 selftest.py
```

`0 errors` で終了することを完了条件とする。検証内容は、送出インデックスのパレット範囲、量子化距離計算の妥当性、4スライス分割の再結合一致、UDPヘッダー＋CRC32の往復、反射移動の画面内保持。

## 主なオプション

```text
--render-mode image|mask 描画モード（既定 image）
--color-style cycle      mask モードで帯ごとにパレット順で着色（既定）
--color-style solid      mask モードで全体を1色にし、時間・反射で次の番号へ
--color-start 0x10       巡回の開始番号（既定 FC6=0x00 / MSX16=0x01、終端は各パレットの最終番号）
--mask-threshold 128     mask モードの輝度しきい値
--invert                 mask モードの前景/背景を反転
--background 0x30        背景インデックス（FC6 既定 0x30、MSX16 既定 0x1）
--speed-x 83             X方向速度 [pixel/s]
--speed-y 109            Y方向速度 [pixel/s]
--rainbow-hz 0.8         巡回速度
--stripe-width 3         色帯の幅
--fps 60                 生成フレームレート
--no-fit                 画面より大きい画像を縮小せずエラーにする
--frames 600             指定フレーム数で終了
```

## 素材

`test.webp` は `takemura-lab.github.io/kentaro.webp`（300×300）を 64×64 へ縮小したロスレスWebP。任意の WebP へ差し替えできる（192×384 を超える場合は最近傍縮小）。
