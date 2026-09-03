# 主機側テストモード

表示経路を実機で確認するためのテストモード群。すべて主機（Ubuntu）で動かし、
旧4台構成の`--send --pi ...`（4個）を付けると 4 台の Pi へ送信する。省略するとプレビューだけになる。

> **現行実機:** 表示Piは `pi1`（`.101` / `target_id 0`）、`pi2`（`.102` / `target_id 1`）、
> `pi4`（`.104` / `target_id 3`）の3台。`.103` / `target_id 2` は未使用である。
> このディレクトリの汎用送信・テストモードは4分割を前提とするため、以下の4台用例は
> 旧構成またはローカル結合試験として扱う。現行の接続先は
> [docs/CONNECTION_INFO.md](../../docs/CONNECTION_INFO.md)を参照する。

| モード | スクリプト | 内容 | 主な確認対象 |
|---|---|---|---|
| TEST1 | `test_mode.py` | WebP を 192×384 内で DVD ロゴ風に反射移動 | 旧4分割・伝送経路 |
| TEST2 | `test2_status.py` | 旧4台の主機 → PI1 → … → PI4 とページを切り替えて状態を文字表示 | 各 Pi の死活・FPS・欠損 |
| TEST3 | `test3_quad.py` | 旧4台の192×96の4帯すべてへ同じ画像を描く | 1台ごとの色再現・向き・欠け |
| TEST4 | `test4_super.py` | 旧4台の斜めN字配置の仮想画面 576×192 で反射移動 | 旧構成の物理配置の割り当て |

共通の補助として、`palette_check.py`（全色パターン）、`udp_preview.py`（1台PCでの結合試験）、
`selftest.py`（機械的検証）がある。伝送は `test_mode.py` の `UdpFrameSender` を全モードで共用する。

## PC側（Mac / Windows）72枚RGB・輝度キャリブレーションUI

現行実機の表示Pi 3台（各24枚、合計72枚）をPCから個別に選択し、パネルごとのR/G/B倍率を
色相環または0.00〜2.00の数値入力欄で、RGB共通の輝度倍率を「輝度」欄で調整する。UIはPython標準の
Tkinterだけを使うため、追加パッケージは不要。Windowsでも同じPythonスクリプトを実行できる。

macOS / Linux:

```bash
python3 host/test_mode/panel_calibration_ui.py
```

Windows PowerShell:

```powershell
py -3 host\test_mode\panel_calibration_ui.py
```

またはWindows Explorerで`host\test_mode\panel_calibration_ui.bat`をダブルクリックする。

Windows側の前提:

- Python 3.10以降（公式インストーラーのTkinterを含む構成）
- Windowsの「OpenSSH Client」。PowerShellで`ssh -V`が通ること
- 表示Piと同じネットワーク、および`takemuralab`でパスワードなしSSH接続できる鍵

初回はPowerShellで次を実行し、SSH鍵と接続を確認する。UIは`BatchMode=yes`で接続するため、
パスワード入力待ちにはならない。

```powershell
ssh takemuralab@192.168.10.101
ssh -o BatchMode=yes takemuralab@192.168.10.101 systemctl is-active pi-client@0.service
```

`ssh.exe`がPATHにない場合は、PowerShellでフルパスを指定できる。

```powershell
$env:PANEL_CALIBRATION_SSH = 'C:\Windows\System32\OpenSSH\ssh.exe'
py -3 host\test_mode\panel_calibration_ui.py
```

起動時に`.101`（target 0）、`.102`（target 2）、`.104`（target 1）から現在の補正値を読み込む。
「校正モード開始」で制御Piのゲーム送信を停止し、「単色表示開始」で選択した1枚だけへ
赤・緑・青・白などの単色パターンを継続送信する。下側のパネルも画面上の`row=2/3`から
個別に選べる。「全部単色表示開始」では同じ色を全3台・全72枚へ送信できる。

画面上部の色相環は、現在選択中のパネルのRGB補正値を決める。中心はR/G/Bすべて`1.00倍`、
色相が補正する色味、中心からの距離が補正量となり、外周で各ゲインが`0.00〜2.00倍`の範囲に
なる。選択パネルを変えると、そのパネルの数値入力値に合う位置へ色相環も更新される。
輝度は色相環とは独立したRGB共通倍率で、各カードの「輝度」欄へ`0.00〜2.00`を入力する。
単色表示のテスト色は、上部のプリセット（赤・緑・青・白・グレーなど）から選択できる。

数値入力の変更は「選択Piへ適用」または「全3台へ適用」を押すまでPiへ書き込まない。
適用時は`/etc/hackathon-2026/panel_calibration.conf`を更新し、対象の`pi-client`を再起動する。UIはRGB3列に加えて
6列目の`brightness`へ輝度を保存し、旧5列設定を読み込んだ場合は輝度1.00で表示する。
終了時は校正モード開始前に稼働していた`pi3-control.service`を復帰する。

UIの配線・設定変換だけを確認する自己テスト:

```bash
python3 -B host/test_mode/panel_calibration_ui_selftest.py
```

- `send()` … 旧4台向けに192×384の全画面を上から96行ずつ4分割して送る（TEST1〜3）
- `send_slices()` … 旧4台向けに`target_id`順に並べた192×96のスライスをそのまま送る（TEST4）

---

## TEST1: WebP DVDテストモード

主機上のローカル`test.webp`を読み込み、192×384の論理画面内でDVDプレイヤーのロゴのように反射移動させる。色は FC6（52色）または MSX16（16色）の登録済みインデックスだけを使う。着色は色相を選び直さず、パレット定義順の番号を開始番号から最終番号（FC6:51 / MSX16:15）まで順繰りに使う。

計画書 §4.7 の WBMP テストモードを、素材 WebP 対応へ置き換えたもの。

- AI・カメラ処理なし
- 主機で移動、反射、着色、パレット量子化、全画面描画、旧4分割まで実施
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

### 旧4台構成のPiへ送信

```bash
python3 test_mode.py --image test.webp --palette fc6 --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.102:5000 \
  --pi 192.168.10.103:5000 --pi 192.168.10.104:5000
```

旧4台に同じ`frame_id`を付け、上から順に192×96ずつ送る。受け側は `pi-client/`（同期段階A）。
現行3台の実機にはこの4台用例をそのまま送信しない。
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
  --pi 127.0.0.1:5000 --pi 127.0.0.1:5000 \
  --pi 127.0.0.1:5000 --pi 127.0.0.1:5000
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
旧4台構成では主機 → PI1 → PI2 → PI3 → PI4 → 主機 … と `--page-seconds` ごとに切り替える。
文字はパレット登録色だけで描画するため、そのまま LED へ出せる。

各 Pi の情報は `pi_client` が UDP 5101 へ 1 秒ごとに送る死活報告（`PIHEALTH ...`）から得る。

```bash
python3 test2_status.py --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.102:5000 \
  --pi 192.168.10.103:5000 --pi 192.168.10.104:5000
```

ページの内容:

| ページ | 表示項目 |
|---|---|
| HOST | パレット、送信FPS、`frame_id`、論理画面・スライスの大きさ、稼働時間、送信先一覧 |
| PI1〜PI4 | 状態、送信元アドレス、`target_id`、表示枚数、欠損数、FPS、`rotate180`、最終受信からの経過（旧4台構成） |

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
--boundary            Pi境界（Y=96/192/288）に区切り線を引く
--preview-scale 2     プレビュー拡大率
--no-preview          プレビュー窓を開かない
```

## TEST3: 4分割モード（旧4台構成）

192×384 を 192×96 の 4 帯に分け、そのすべてへ同じ画像を 1 枚ずつ描く。
1 台ごとの色再現・向き・欠けを個別に見るためのモード。既定素材は `color_bar.webp`。

```bash
python3 test3_quad.py --palette fc6
```

```bash
python3 test3_quad.py --image test.webp --stretch --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.102:5000 \
  --pi 192.168.10.103:5000 --pi 192.168.10.104:5000
```

- 既定で各帯の四隅へ 3px の緑の目印を置く（欠けと向きの判定用）。`--no-marker` で消す
- 既定で各帯の左下へ `PI1`〜`PI4` を描く。`--no-label` で消す
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

4 つの 192×96 ブロックが縦一列ではなく、隣り合うブロックの角 64dot だけで繋がる
ジグザグ配置の実機構成に対応する。仮想画面は 576×192 で、その中で画像を反射移動させる。
パネルが無い領域へ図形が入ると、その間だけ見えなくなる。

実機の接続:

```text
Pi1 の右上 64dot ↔ Pi3 の左下 64dot
Pi3 の右下 64dot ↔ Pi2 の左上 64dot
Pi2 の右上 64dot ↔ Pi4 の左下 64dot
```

ここから各ブロックの原点は次のようになる（y は下向き）。

| ブロック | `target_id` | 原点 (x, y) |
|---|---:|---|
| PI1 | 0 | (0, 96) |
| PI3 | 2 | (128, 0) |
| PI2 | 1 | (256, 96) |
| PI4 | 3 | (384, 0) |

```bash
python3 test4_super.py --image test.webp --show-links
```

**配置は `test4_super.py` の `LAYOUT` で定義している。実機の並びが違う場合はここだけ書き換える。**
仮想画面の大きさ（`VIRTUAL_WIDTH` / `VIRTUAL_HEIGHT`）は `LAYOUT` から自動で決まる。

送信には `UdpFrameSender.send_slices()` を使い、`target_id` 順に切り出した 192×96 を
そのまま送る。伝送形式は TEST1〜3 と同一で、Pi 側の処理も変わらない。

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

`--send --pi ...` を付けると同じパターンを旧4台へ送信できる。Pi 境界（Y=96/192/288）に1pxの目印を入れてある。
現行3台の表示確認には、制御Pi側の現行サービス設定を使う。

## 機械的検証

```bash
python3 selftest.py
```

`0 errors` で終了することを完了条件とする。検証内容は、送出インデックスのパレット範囲、量子化距離計算の妥当性、4スライス分割の再結合一致、UDPヘッダー＋CRC32の往復、反射移動の画面内保持。

対象は TEST1（`test_mode.py`）と共用部分のみで、TEST2〜4 と `pi-client` は検証していない。
