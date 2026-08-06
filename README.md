# RGB LED マトリックス縦型インタラクティブゲーム（hackathon-2026）

32×32 RGB LED パネル 72 枚（6列×12段、論理解像度 192×384）を、Ubuntu 主機の集中処理と
Raspberry Pi 4 台の外部同期で駆動する縦型ディスプレー・ゲームのリポジトリ。

現在の内容は **最小単位（表示経路の検証まで）** に限定している。計画書 §13 の実装開始条件のうち
1〜3（DVD テストモード、FC6 全色パターン、MSX16 全色パターン）と、その 4 分割 UDP 送信までを実装済み。
ゲーム本体・カメラ入力・Pi クライアント・M5 同期はまだ実装していない。

## デモ

`test.webp` を 192×384 の論理画面内で反射移動させ、反射のたびに FC6 のパレット番号を
`0x00` から順繰りに進めたもの（20秒／20fps／2倍拡大、実機ではなく主機シミュレーション）。

![DVDテストモード](docs/assets/test_mode_dvd.gif)

```bash
cd host/test_mode && python3 test_mode.py --render-mode mask --color-style solid \
  --rainbow-hz 0 --speed-x 97 --speed-y 131 --gif-seconds 20 --gif demo.gif
```

## 構成

```text
.
├── docs/
│   ├── RGB_LEDインタラクティブゲーム開発計画書.md   # 計画書（v1.2 + 52色化の注記）
│   └── assets/                                     # 構成図・同期図・パレット見本・デモGIF
├── host/                       # Ubuntu 主機側
│   ├── palettes.py             # FC6(52色) / MSX16(16色) 定義
│   ├── make_palette_sheet.py   # docs/assets のパレット見本画像を再生成
│   ├── palettes.json           # 機械可読定義
│   ├── fc6.pal / msx16.pal     # 生 RGB パレット（156 byte / 48 byte）
│   └── test_mode/
│       ├── test_mode.py        # WebP を DVD ロゴ風に反射移動・量子化・4分割 UDP 送信
│       ├── palette_check.py    # 全色テストパターン
│       ├── udp_preview.py      # 1台PCで4領域を再結合する開発用プレビュー
│       ├── selftest.py         # 機械的検証（0 errors が完了条件）
│       └── test.webp           # 動作確認用素材（64×64）
├── requirements.txt
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

## 実行

ローカルプレビュー（DVD テストモード）:

```bash
cd host/test_mode && python3 test_mode.py --image test.webp --palette fc6
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

## 検証

```bash
cd host/test_mode && python3 selftest.py
```

`0 errors` を完了条件とする。実機（LED パネル・Pi・M5）での確認は未実施。

## 次のステップ（計画書 §9.1 の優先順位）

1. Pi 常駐表示クライアント（`pi-client/`: UDP 受信 → CRC 確認 → READY → GPIO 同期待機 → HUB75 出力）
2. 4 台の論理フレーム同期（READY バリア、`frame_id` 一致）
3. M5StickC Plus の物理同期パルスと走査位相の評価
4. ゲーム本体
5. USB カメラ入力（背景差分による LEFT/RIGHT/JUMP 判定）
