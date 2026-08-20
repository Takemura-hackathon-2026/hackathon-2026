# RGB LED マトリックス縦型インタラクティブゲーム（hackathon-2026）

32×32 RGB LED パネル 72 枚（6列×12段、論理解像度 192×384）を、Ubuntu 主機の集中処理と
Raspberry Pi 4 台の外部同期で駆動する縦型ディスプレー・ゲームのリポジトリ。

現在の内容は表示経路の検証に加え、**主機側のブロック崩し試作**を含む。計画書 §13 の実装開始条件のうち
1〜3（DVD テストモード、FC6 全色パターン、MSX16 全色パターン）とその 4 分割 UDP 送信、
加えて主機側テストモード 4 種（TEST1〜4）と Pi 常駐表示クライアントの同期段階 A
（受信 → CRC 確認 → LUT 変換 → HUB75 出力）までを実装済み。

`host/block_breaker.py` は、背景差分による `LEFT` / `RIGHT` / `JUMP` 判定、192×384の
FC6描画、既存UDP経路への4分割送信を主機だけで行う。ゲームは画像キャラクターを倒すボス戦で、
左右移動でパドルを動かし、ジャンプでボスの口元からボールを発射する。人物映像・人物マスクは
LEDへ送らない。

ジャンプ判定は重心上昇0.05、下端上昇0.04（0〜1正規化）を既定値とする。カメラ設置や
身体の映り方に合わせて、`--jump-rise-y-min` と `--jump-rise-bottom-min` で個別調整できる。

ジャンプだけを確認する場合は、ゲーム更新・左右移動・LED送信を行わない専用CLIを使う。
背景学習後にジャンプイベントを標準出力へ記録し、`--preview`指定時はカメラ画像とマスクを表示する。

```bash
python3 host/jump_detector.py --camera 0 --seconds 30 --preview
python3 host/jump_detector_selftest.py
```

READY バリア（同期段階 B）、M5 の物理同期パルス、実機（LEDパネル・Pi・M5）での確認は未実装。
これらの同期試験が完了するまで、ゲームモードは主機プレビューおよび表示経路の試作として扱う。

## デモ

`test.webp` を 192×384 の論理画面内で反射移動させ、反射のたびに FC6 のパレット番号を
`0x00` から順繰りに進めたもの（20秒／20fps／2倍拡大、実機ではなく主機シミュレーション）。

![DVDテストモード](docs/assets/test_mode_dvd.gif)

```bash
cd host/test_mode && python3 test_mode.py --render-mode mask --color-style solid \
  --rainbow-hz 0 --speed-x 97 --speed-y 131 --gif-seconds 20 --gif demo.gif
```

## ホスト常時起動の待機表示

`host/standby.py` は、現在時刻と主機温度、Pi1〜Pi4の温度を192×384の縦画面へ表示し、
4台のPiへ繰り返し送信する。`--mode palette`ではFC6全52色を左上から8×8のタイルへ
並べた市松状グラデーション、`--mode logo`では`host/test_mode/single-eye-catch_2800x1040.png`
のロゴ領域を表示する。

ローカルで送出フレームを確認する場合:

```bash
.venv/bin/python host/standby.py --frames 1 --preview
```

oyakiへ送って常時起動する場合:

```bash
host/oyaki_camera_calibrate.sh deploy
host/oyaki_camera_calibrate.sh standby-start
host/oyaki_camera_calibrate.sh standby-status
host/oyaki_camera_calibrate.sh standby-stop
```

`--mode logo --image PATH`で別ロゴ素材へ切り替えられる。既存の`start`／`foreground`はカメラ校正用として残している。

## 構成

```text
.
├── docs/
│   ├── RGB_LEDインタラクティブゲーム開発計画書.md   # 計画書（v1.2 + 52色化の注記）
│   ├── NETWORK.md                                  # セグメント・IP・ポート設計、設定コマンド
│   ├── nat.conf                                    # Mac→親機のインターネット共有用 pf ルール
│   └── assets/                                     # 構成図・同期図・パレット見本・デモGIF
├── host/                       # Ubuntu 主機側
│   ├── palettes.py             # FC6(52色) / MSX16(16色) 定義
│   ├── make_palette_sheet.py   # docs/assets のパレット見本画像を再生成
│   ├── palettes.json           # 機械可読定義
│   ├── fc6.pal / msx16.pal     # 生 RGB パレット（156 byte / 48 byte）
│   ├── block_breaker.py         # カメラ操作ブロック崩し・FC6描画・UDP送信
│   ├── block_breaker_selftest.py # カメラ不要のゲーム・入力分類器検証
│   ├── standby.py                # 時刻・主機/Pi温度を表示する192x384縦画面のホスト常時起動
│   └── test_mode/
│       ├── test_mode.py        # TEST1: WebP を DVD ロゴ風に反射移動・量子化・4分割 UDP 送信
│       ├── single-eye-catch_2800x1040.png # 常時起動時の既定ロゴ素材
│       ├── test2_status.py     # TEST2: 主機と各 Pi の状態を文字で交互表示（死活受信付き）
│       ├── test3_quad.py       # TEST3: 4帯それぞれへ同じ画像を出す個体確認モード
│       ├── test4_super.py      # TEST4(SUPERTESTMODE): 斜めN字配置の仮想画面 576×192
│       ├── palette_check.py    # 全色テストパターン
│       ├── udp_preview.py      # 1台PCで4領域を再結合する開発用プレビュー
│       ├── selftest.py         # 機械的検証（0 errors が完了条件）
│       ├── test.webp           # 動作確認用素材（64×64）
│       └── color_bar.webp      # TEST3 の既定素材（カラーバー）
├── pi-client/                  # Raspberry Pi 常駐表示クライアント（C++ / rpi-rgb-led-matrix）
│   ├── pi_client.cc            # UDP受信 → CRC確認 → LUT変換 → HUB75 出力、UDP 5101 へ死活報告
│   ├── Makefile                # RGB_LIB_DISTRIBUTION で rpi-rgb-led-matrix を指す
│   └── README.md               # ビルド手順・オプション・死活報告の形式
├── requirements.txt
├── LICENSE                     # AGPL-3.0
└── README.md
```

## パレット（重要・計画書からの変更点）

FC6 は **2026-08-06 の指定により 52 色**（インデックス `0x00`〜`0x33`）へ変更した。
重複色と予約黒を持たないため、計画書 §4.4 の 64 色表とはインデックス互換性がない。
**正本は `host/palettes.py` / `host/palettes.json`** であり、計画書 §4.4 の表は旧版として扱う。

- 伝送形式（1画素1バイト、上位ビット0、ビットパックなし）は変更なし
- 固定インデックス: 黒 `0x30`、グレー `0x31`、ライトグレー `0x32`、白 `0x33`
- `mask` 描画の着色は、開始番号から最終番号（FC6:51 / MSX16:15）までを順繰りに巡回する（`--color-start` で開始番号を変更可）
- MSX16 は 16 色のまま。インデックス `0` は透明で、送出前に背景色へ解決する

見本画像 [docs/assets/fc6_palette.png](docs/assets/fc6_palette.png) は `palettes.py` から再生成できる。

```bash
.venv/bin/python host/make_palette_sheet.py --palette fc6
```

## セットアップ

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Ubuntu 主機ではディストリビューションのパッケージでもよい。

```bash
sudo apt install python3-numpy python3-opencv
```

## テストモード

主機側に 4 種類のテストモードがある。いずれも `--send --pi ...`（4 個）で 4 台の Pi へ送信でき、
省略するとローカルプレビューのみになる。

| モード | スクリプト | 内容 | 主な確認対象 |
|---|---|---|---|
| TEST1 | `test_mode.py` | WebP を 192×384 内で DVD ロゴ風に反射移動 | パレット量子化・4分割・伝送経路 |
| TEST2 | `test2_status.py` | 主機 → PI1 → … → PI4 とページを切り替えて状態を文字表示 | 各 Pi の死活・FPS・欠損（UDP 5101 の報告） |
| TEST3 | `test3_quad.py` | 192×96 の 4 帯すべてへ同じ画像を描く | 1 台ごとの色再現・向き・欠け |
| TEST4 | `test4_super.py` | 斜めN字配置の仮想画面 576×192 で反射移動 | 縦一列でない物理配置の割り当て |

TEST1（DVD テストモード）:

```bash
cd host/test_mode && python3 test_mode.py --image test.webp --palette fc6
```

TEST2（状態表示。各 Pi の死活報告を UDP 5101 で受ける）:

```bash
cd host/test_mode && python3 test2_status.py --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.102:5000 \
  --pi 192.168.10.103:5000 --pi 192.168.10.104:5000
```

TEST3（4 帯へ同じ画像。既定素材は `color_bar.webp`）:

```bash
cd host/test_mode && python3 test3_quad.py --palette fc6
```

TEST4（SUPERTESTMODE。配置は `test4_super.py` の `LAYOUT` で定義）:

```bash
cd host/test_mode && python3 test4_super.py --image test.webp
```

全色テストパターン:

```bash
cd host/test_mode && python3 palette_check.py --palette fc6
```

1台PCでの UDP 結合試験（端末1で受信、端末2で送信）:

```bash
cd host/test_mode && python3 udp_preview.py --port 5000
```

```bash
cd host/test_mode && python3 test_mode.py --no-preview --send \
  --pi 127.0.0.1:5000 --pi 127.0.0.1:5000 --pi 127.0.0.1:5000 --pi 127.0.0.1:5000
```

詳細は [host/test_mode/README.md](host/test_mode/README.md)。

## カメラ操作ブロック崩し

通常ゲーム表示はFC6（52色）で固定する。主機が192×384の完成済みパレットインデックス配列を
描き、既存の`UdpFrameSender`が上から192×96ずつ4台へ送る。Pi側のコードとUDP形式は変更しない。

現在のゲームは`host/assets/takemuraface_fc6.png`をボスとして表示するボス戦形式。

- ボスHPは100、ボールがボスへ当たるごとに10減る
- ボス画像は96×96へ拡大し、上端との間にボールが通れる隙間を残す
- 画面左上に現在HPを示すバー、右上に残機（初期値3）を表示する
- ボールはボスの口元からプレイヤー方向へ発射する
- 残機は右上に白丸で表示する
- ミス後の再発射待ちでは残りの白丸を表示し、減った位置の空円の輪郭を次の発射まで点滅させる
- ボスへダメージを与えると、ボス画像を短時間に2回点滅させる
- HPが50以下になった瞬間は3回点滅し、その完了後にボスが左右へ往復移動する
- HPが0になると`BOSS DOWN`を約1.8秒表示し、その後自動的に初期画面へ戻る
- ボールを3回落とすと`GAME OVER`になり、約1.8秒後に最初からやり直す

起動時は、まず誰も写さない状態で背景を約2秒学習する。次に参加者がカメラ正面で静止すると、
立ち位置を校正する。校正後の操作は次の通り。

| 身体操作 | ゲーム操作 |
|---|---|
| 左へ移動 | パドルを左へ移動 |
| 右へ移動 | パドルを右へ移動 |
| ジャンプ | サーブ中のボールを発射 |

キーボードだけで遊ぶ場合は`--keyboard`で起動する。ゲームのプレビュー画面を選択して、
`A`/`D`・`H`/`L`・左右矢印でパドルを動かす。`Space`・`W`・`K`・上矢印でボールを発射し、
`R`でリセット、`Q`または`Esc`で終了する。
Linux/X11環境では左右キーの押下・解放を毎フレーム取得するため、押した瞬間から滑らかに移動し、
キーを離したフレームで停止する。X11を使えない環境ではOpenCVのキーリピートを使うフォールバックとなる。

```bash
python3 host/block_breaker.py --keyboard
```

USBカメラとPi 4台を使う場合:

```bash
python3 host/block_breaker.py --camera 0 --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.102:5000 \
  --pi 192.168.10.103:5000 --pi 192.168.10.104:5000
```

カメラ画面と前景マスクは`--debug-camera`で主機だけに表示する。LEDへの送出内容はゲーム画面だけである。
背景幕や床以外を検出対象から外すには、`--roi x,y,width,height`でカメラ画像内のROIを指定できる。

```bash
python3 host/block_breaker_selftest.py
```

## カメラキャリブレーション

`host/camera_calibrate.py` は、固定照明・床反射を背景として学習し、CENTER/STANCE、LEFT、RIGHT、JUMP、VALIDATEの順に姿勢・移動・ジャンプを約60〜90秒で計測する。回転、ROI、固定照明領域、各ステージ時間はCLIで指定できる。回転はROI・背景差分・計測より先に適用され、将来のoyaki標準例は`ccw`とする。LEDへ送るのは既存FC6の192×384インデックス画像だけで、カメラ映像やマスクは送らない。

ローカルでカメラ・ネットワークなしの表示デモを実行する。各ステージ画面と背景/候補の代表PNGを指定先へ保存する。

```bash
python3 host/camera_calibrate.py --demo --demo-output /tmp/camera-calibrate-demo
```

固定照明の指定は処理後240×320画像の座標で行う。例えば`52,32,142,60`は`--fixed-light 52,32,142,60`と指定する。背景から抽出する安定高輝度マスクも併用するが、瞬間的な明るさの変化や白い服を一律には除外しない。

oyaki上で実カメラと4台のPiへ接続するコマンド例:

```bash
python3 host/camera_calibrate.py --camera 0 --rotation ccw \
  --fixed-light 52,32,142,60 --send \
  --pi 192.168.10.101:5000 --pi 192.168.10.102:5000 \
  --pi 192.168.10.103:5000 --pi 192.168.10.104:5000 \
  --output camera_calibration.json
```

露出CLIの既定値は`1/312/2`で、カメラが返した実際の解像度・FPS・回転・露出readbackをJSONへ記録する。成功時は`camera_calibration.json`へatomic writeし、品質ゲートに失敗した結果は`camera_calibration.invalid.json`へatomic writeするため、既存のvalid JSONを上書きしない。JSONには`version`、`date`、`valid`、`camera`、`ROI`、`fixed_light`、`background`、`baseline`、`motion_stats`、`thresholds`、`quality`、`sample_counts`を含み、NaN/Infinityは許可しない。

終了コードは、`0`=valid校正成功、`1`=RETRY/FAILまたは中止、`2`=引数・カメラ等の実行エラー。`/tmp`のデモ出力は揮発性なので、必要な診断PNGは別の保存先へコピーする。自己テストは`python3 host/camera_calibrate_selftest.py`でカメラ・ネットワークなしに実行できる。

Macからは`host/oyaki_camera_calibrate.sh`でSSH操作できる。SSH鍵とHANDOFF記載のIPv6ゾーン指定を使い、秘密はスクリプトへ埋め込まない。`start`はPiクライアントを起動せず、既に4台が待受している前提で校正だけをバックグラウンド実行する。

```bash
host/oyaki_camera_calibrate.sh check
host/oyaki_camera_calibrate.sh deploy
host/oyaki_camera_calibrate.sh display-test 5
host/oyaki_camera_calibrate.sh start
host/oyaki_camera_calibrate.sh status
host/oyaki_camera_calibrate.sh logs 80
host/oyaki_camera_calibrate.sh result
host/oyaki_camera_calibrate.sh stop
host/oyaki_camera_calibrate.sh fetch /tmp/camera-calibration-result
```

今回の実機確認では、4台の`pi_client`を手動起動した後、FC6テスト送信でtarget 0〜3の`displayed`増加と`dropped=0`を確認した。本版キャリブレーションは`rotation=ccw`、640×480/30fps、露出readback 1/312/2で`valid=true`を生成した。背景のみスモークは安定状態で303フレーム中候補0だったが、照明変動時に誤候補が出た試行もあるため、設置後の再測定を推奨する。

## Pi 常駐表示クライアント

`pi-client/` は Raspberry Pi 上で動く C++ の表示専用プロセス。UDP 5000 で 192×96 の
パレットインデックス配列を受け、CRC32 とパレット範囲を確認してから固定 LUT で RGB へ
変換し、HUB75 へ出力する。1 秒ごとに UDP 5101 へ死活報告（`PIHEALTH ...`）を返し、
TEST2 がそれを受けて表示する。

ビルドには [rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) が必要で、
このライブラリは各Pi上の`pi_client`へリンクして使う。起動時に自動起動するsystemd設定と、
主機から4台へ転送・ビルド・有効化する手順は [pi-client/README.md](pi-client/README.md) にまとめてある。

```bash
cd pi-client && make RGB_LIB_DISTRIBUTION=$HOME/rpi-rgb-led-matrix
```

```bash
sudo ./pi_client --target-id 0
```

詳細・オプションは [pi-client/README.md](pi-client/README.md)。

## 検証

```bash
cd host/test_mode && python3 selftest.py
```

`0 errors` を完了条件とする。selftest が見るのは主機側の送出経路（パレット範囲、量子化距離、
4 分割の再結合一致、UDP ヘッダー＋CRC32 の往復、反射移動の画面内保持）で、TEST2〜4 と
`pi-client` は対象外。実機（LED パネル・Pi・M5）での確認も未実施。

## ネットワーク

セグメント構成・Piのアドレス割当・ポート・設定コマンドは [docs/NETWORK.md](docs/NETWORK.md) にまとめてある。

| セグメント | 用途 | ネットワーク |
|---|---|---|
| A | フレーム配信（親機 `enp2s0` — スイッチ — Pi×4） | `192.168.10.0/24`、Piは `.101`〜`.104`（`target_id` = 第4オクテット − 101） |
| B | 開発機直結（親機 `enp3s0` — Mac `en7`） | `192.168.20.0/24` |

## 次のステップ（計画書 §9.1 の優先順位）

1. 実機での表示確認（`pi-client` のビルドと 4 台での TEST1〜4）
2. 4 台の論理フレーム同期（READY 返送・UDP 5100 のバリア、`frame_id` 一致）
3. M5StickC Plus の物理同期パルスと GPIO 同期待機、走査位相の評価
4. 実機でのブロック崩し・カメラ入力の校正（左右・ジャンプの精度と遅延）
5. 会場照明下での誤検出対策とゲーム難易度調整

## ライセンス

GNU Affero General Public License v3.0 以降（AGPL-3.0-or-later）。全文は [LICENSE](LICENSE) を参照。

Copyright (C) 2026 eightman

このプログラムはフリーソフトウェアです。フリーソフトウェアファウンデーションが公開する
GNU Affero General Public License（バージョン3、またはそれ以降のバージョン）の
条件に従って、再配布および改変ができます。

このプログラムは有用であることを願って配布されますが、**無保証**です。商品性や特定目的
への適合性の保証も含め、いかなる保証もありません。詳細は GNU Affero General Public
License を参照してください。
