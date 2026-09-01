# 主機側テストモード

表示経路を実機で確認するためのテストモード群。すべて主機（Ubuntu）で動かし、
`--send --pi ...`（3個）を付けると 3 台の Pi へ送信する。省略するとプレビューだけになる。

| モード | スクリプト | 内容 | 主な確認対象 |
|---|---|---|---|
| TEST1 | `test_mode.py` | WebP を 192×384 内で DVD ロゴ風に反射移動 | パレット量子化・3分割・伝送経路 |
| TEST2 | `test2_status.py` | 主機 → PI1 → … → PI3 とページを切り替えて状態を文字表示 | 各 Pi の死活・FPS・欠損 |
| TEST3 | `test3_quad.py` | 192×128 の 3 帯すべてへ同じ画像を描く | 1 台ごとの色再現・向き・欠け |
| TEST4 | `test4_super.py` | 旧4台特殊配置のローカル確認（現行構成では送信不可） | 非標準配置の割り当て検証 |

共通の補助として、`palette_check.py`（全色パターン）、`udp_preview.py`（1台PCでの結合試験）、
`selftest.py`（機械的検証）がある。現行の伝送は `test_mode.py` の `UdpFrameSender` を TEST1〜3 で共用する。

- `send()` … 192×384 の全画面を上から 128 行ずつ 3 分割して送る（TEST1〜3）
- `send_slices()` … `target_id` 順に並べた 192×128 のスライスをそのまま送る（旧TEST4のローカル配置にも使用）

---

## TEST1: WebP DVDテストモード

主機上のローカル`test.webp`を読み込み、192×384の論理画面内でDVDプレイヤーのロゴのように反射移動させる。色は FC6（52色）または MSX16（16色）の登録済みインデックスだけを使う。着色は色相を選び直さず、パレット定義順の番号を開始番号から最終番号（FC6:51 / MSX16:15）まで順繰りに使う。

計画書 §4.7 の WBMP テストモードを、素材 WebP 対応へ置き換えたもの。

- AI・カメラ処理なし
- 主機で移動、反射、着色、パレット量子化、全画面描画、3分割まで実施
- 1画素1バイトのパレットインデックス（FC6: `0x00〜0x33` / MSX16: `0x01〜0x0F`）
- Pi 側は受信、バッファ交換、HUB75 出力のみ

### 描画モード

| モード | 内容 | 検証対象 |
|---|---|---|
| `image`（既定） | WebP の各画素を最近傍色でパレット量子化して表示 | パレット量子化経路、色再現 |
| `mask` | 輝度しきい値で1bit化し、パレット定義順の番号を帯ごとに順繰り割当 | 全インデックスの表示確認、従来の WBMP テストモード相当 |

### 実行

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

### 3台のPiへ送信

```bash
python3 test_mode.py --image test.webp --palette fc6 --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.104:5000 \
  --pi 192.168.10.102:5000
```

3台に同じ`frame_id`を付け、上から順に192×128ずつ送る。受け側は `pi-client/`（同期段階A）。
M5の`FRAME_SYNC`と READY バリア（同期段階B）は未実装で、各 Pi は完成フレームを受け取り次第
`SwapOnVSync` で表示する。

### 1台PCでのUDP結合試験

端末1:

```bash
python3 udp_preview.py --port 5000
```

端末2:

```bash
python3 test_mode.py --image test.webp --no-preview --send \
  --pi 127.0.0.1:5000 --pi 127.0.0.1:5000 --pi 127.0.0.1:5000
```

### GIF 書き出し

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

### 主なオプション

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

### 素材

`test.webp` は `takemura-lab.github.io/kentaro.webp`（300×300）を 64×64 へ縮小したロスレスWebP。任意の WebP へ差し替えできる（192×384 を超える場合は最近傍縮小）。

---

## TEST2: 状態表示モード

主機と各 Pi の状態を、192×384 の論理画面へ 1 ページずつ文字で描く。
主機 → PI1 → PI2 → PI3 → 主機 … と `--page-seconds` ごとに切り替える。
文字はパレット登録色だけで描画するため、そのまま LED へ出せる。

各 Pi の情報は `pi_client` が UDP 5101 へ 1 秒ごとに送る死活報告（`PIHEALTH ...`）から得る。

```bash
python3 test2_status.py --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.104:5000 \
  --pi 192.168.10.102:5000
```

ページの内容:

| ページ | 表示項目 |
|---|---|
| HOST | パレット、送信FPS、`frame_id`、論理画面・スライスの大きさ、稼働時間、送信先一覧 |
| PI1〜PI3 | 状態、送信元アドレス、`target_id`、表示枚数、欠損数、FPS、`rotate180`、最終受信からの経過 |

状態は 3 つ。

| 表示 | 意味 |
|---|---|
| `OK` | 3秒以内に報告が届いている |
| `LOST` | 報告が3秒以上途絶えた |
| `NO SIGNAL` | 起動後まだ一度も報告が届いていない |

FPS が 30 未満、または欠損数が 0 でない場合は警告色（橙・赤）で表示する。

主なオプション:

```text
--palette fc6|msx16   パレット（既定 fc6）
--page-seconds 3.0    1ページの表示秒数
--fps 30.0            送信フレームレート
--seconds 0           実行秒数。0は無制限
--health-port 5101    死活報告の待受ポート
--boundary            Pi境界（Y=128/256）に区切り線を引く
--preview-scale 2     プレビュー拡大率
--no-preview          プレビュー窓を開かない
```

## TEST3: 3分割モード

192×384 を 192×128 の 3 帯に分け、そのすべてへ同じ画像を 1 枚ずつ描く。
1 台ごとの色再現・向き・欠けを個別に見るためのモード。既定素材は `color_bar.webp`。

```bash
python3 test3_quad.py --palette fc6
```

```bash
python3 test3_quad.py --image test.webp --stretch --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.104:5000 \
  --pi 192.168.10.102:5000
```

- 既定で各帯の四隅へ 3px の緑の目印を置く（欠けと向きの判定用）。`--no-marker` で消す
- 既定で各帯の左下へ `PI1`〜`PI3` を描く。`--no-label` で消す
- 静止画のため毎フレーム同じ内容を送る。Pi 側は `frame_id` だけが進む

主なオプション:

```text
--image PATH          素材（既定 color_bar.webp）
--palette fc6|msx16   パレット（既定 fc6）
--background 0x30     背景インデックス
--stretch             縦横比を無視して帯いっぱいに広げる
--no-label            PI番号を描かない
--no-marker           四隅の目印を描かない
--fps 24.0            送信フレームレート
--seconds 0           実行秒数。0は無制限
--save out.png        プレビューをPNG保存して終了
```

## TEST4: SUPERTESTMODE（斜めN字配置）

4 つの 192×128 ブロックが縦一列ではなく、隣り合うブロックの角 64dot だけで繋がる
旧ジグザグ配置をローカルで確認する。仮想画面は 576×256 で、その中で画像を反射移動させる。
パネルが無い領域へ図形が入ると、その間だけ見えなくなる。

この4台配置は現行の3台構成には含まれないため、`--send`は使用できない。

実機の接続:

```text
Pi1 の右上 64dot ↔ Pi3 の左下 64dot
Pi3 の右下 64dot ↔ Pi2 の左上 64dot
Pi2 の右上 64dot ↔ Pi4 の左下 64dot
```

ここから各ブロックの原点は次のようになる（y は下向き）。

| ブロック | `target_id` | 原点 (x, y) |
|---|---:|---|
| PI1 | 0 | (0, 128) |
| PI3 | 2 | (128, 0) |
| PI2 | 1 | (256, 128) |
| PI4 | 3 | (384, 0) |

```bash
python3 test4_super.py --image test.webp --show-links
```

**配置は `test4_super.py` の `LAYOUT` で定義している。実機の並びが違う場合はここだけ書き換える。**
仮想画面の大きさ（`VIRTUAL_WIDTH` / `VIRTUAL_HEIGHT`）は `LAYOUT` から自動で決まる。

現行3台構成では送信せず、`--save`またはローカルプレビューで旧配置を確認する。

主なオプション:

```text
--image PATH             素材（既定 test.webp）
--palette fc6|msx16      パレット（既定 fc6）
--render-mode mask|image 描画モード（既定 mask）
--color-start 0x10       巡回の開始番号
--mask-threshold 128     mask モードの輝度しきい値
--invert                 mask モードの前景/背景を反転
--background 0x30        背景インデックス
--speed-x 97             X方向速度 [pixel/s]
--speed-y 61             Y方向速度 [pixel/s]
--fps 24.0               送信フレームレート
--seconds 0              実行秒数。0は無制限
--no-label               PI番号を描かない
--show-links             接続64dotを緑で塗る
--save out.png           仮想画面をPNG保存して終了
```

---

## 全色テストパターン

計画書 §13 の実装開始条件 2・3（FC6 全52色 / MSX16 全16色）。

```bash
python3 palette_check.py --palette fc6
```

```bash
python3 palette_check.py --palette msx16 --no-preview --save msx16_pattern.png
```

`--send --pi ...` を付けると同じパターンを3台へ送信できる。Pi 境界（Y=128/256）に1pxの目印を入れてある。

## 機械的検証

```bash
python3 selftest.py
```

`0 errors` で終了することを完了条件とする。検証内容は、送出インデックスのパレット範囲、量子化距離計算の妥当性、3スライス分割の再結合一致、UDPヘッダー＋CRC32の往復、反射移動の画面内保持。

対象は TEST1（`test_mode.py`）と共用部分のみで、TEST2〜4 と `pi-client` は検証していない。
