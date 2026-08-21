# 引き継ぎ書 — LEDマトリックス表示システム構築 (2026-08-18)

対象: `hackathon-2026` / 192×384 縦型 RGB LED マトリックス（32×32パネル72枚、6列×12段）

---

## 0. 最重要: 揮発性の注意

**本日作成した実装はすべて oyaki の `/tmp` 配下にあり、再起動で消えます。**
リポジトリには一切コミットしていません。永続化が必要なら §7 を参照。

`pi_client` も手動起動のため、Pi を再起動すると停止します（自動起動は未設定）。

---

## 1. 到達点

| 項目 | 状態 |
|---|---|
| Mac → oyaki → Pi 4台の疎通 | **完了** |
| セグメントA/B の配線・アドレス | **設計 (`docs/NETWORK.md`) と一致** |
| `pi_client` の4台配備・常駐 | **完了**（手動起動） |
| 表示テスト（カラーバー / トリコロール / 縦縞 / ID表示） | **完了** |
| テトリス（自動プレイ + 効果音 + BGM） | **動作中** |
| ブロック崩し（master の `block_breaker.py`） | **表示は動作**、カメラ操作は**未解決** |
| スピーカー（AUX） | **鳴動確認済み** |
| カメラ（See3CAM_130） | **キャプチャ可**、検出は**破綻中** |

---

## 2. ネットワーク構成（実測確定値）

### 接続経路

```
Mac (EMBP.local) --USB-Ethernet(en9)--> oyaki:enp3s0 [セグメントB]
oyaki:enp2s0 --1GbEスイッチ--> Pi ×4 [セグメントA]
```

### oyaki (`th1`, Ubuntu)

| NIC | アドレス | 用途 |
|---|---|---|
| `enp2s0` | `192.168.10.1/24` + `192.168.50.200/24` | セグメントA（フレーム配信） |
| `enp3s0` | `192.168.20.1/24` + `169.254.10.1/16` | セグメントB（開発機直結） |
| `tailscale0` | — | **ログアウト状態**（DNS解決不可のため使用不可） |

> `enp2s0` に旧 `192.168.50.200/24` が残存。実害はないが設計上は `192.168.10.1` のみ。

### Raspberry Pi（本日 IP・ホスト名とも移行済み）

物理配置は**上から** pi2 → pi4 → pi3 → pi1 の順（機体ラベル基準）。
`target_id` は設計規約どおり **第4オクテット − 101**。

| target_id | 画面位置 | ホスト名 | IP | 機体ラベル | 機種 | `--led-slowdown-gpio` |
|---|---|---|---|---|---|---|
| 0 | 最上段 | `pi1` | `192.168.10.101` | rp1 | Pi 4B Rev1.5 | 4 |
| 1 | 上から2番目 | `pi2` | `192.168.10.102` | rasppi02 | Pi 3B Rev1.2 | 2 |
| 2 | 上から3番目 | `pi3` | `192.168.10.103` | rasppi03 | Pi 4B Rev1.4 | 4 |
| 3 | 最下段 | `pi4` | `192.168.10.104` | rasppi01 | Pi 4B Rev1.5 | 4 |

**注意: 設計ホスト名と機体ラベルが逆順**（`rasppi01` が `pi4`）。設計が物理位置基準のため。

移行時の変更内容:
- `dhcpcd` を全機で `disable --now`（NetworkManager と二重管理だった）
- NMプロファイル `led-net` を作成（`ipv4.method manual` / `never-default yes` / `ipv6.method disabled` / `autoconnect yes`）
- 旧プロファイル（`有線接続 1` 等）は `autoconnect no`
- `/etc/hosts` の `127.0.1.1` も更新（`sudo` の名前解決警告対策）
- 旧 `192.168.50.x`（`.12/.13/.22/.31/.56/.77/.86`）は**全て消滅を確認**

**取り外した機体**: `rasppi04`（旧 `192.168.50.32`、Pi 3B）— 低電圧で落ちたため `rasppi02` と交換。

### 認証情報

- oyaki: ユーザー `th1`、鍵認証。**`sudo` はパスワード必須**（未取得）
- Pi 4台: ユーザー `takemuralab` / パスワード `eyetracking`、`sudo` 可

---

## 3. 接続方法（重要）

**`ssh oyaki` は現状使えません。** Mac 側に `192.168.20.50` が未設定のためです。

```bash
sudo networksetup -setmanual "AX88179A 2" 192.168.20.50 255.255.255.0 192.168.20.1
```

> `docs/NETWORK.md` は Mac 側を `en7` と記載しているが、実機は **`en9`**（ハードウェアポート名 `AX88179A 2`）。ドキュメント修正候補。

設定するまでの暫定手段（IPv6リンクローカル経由）:

```bash
ssh -o HostKeyAlias=192.168.20.1 -o 'HostName=fe80::f56f:3e9f:fbb:3a85%%en9' oyaki 'hostname'
```

- `fe80::f56f:3e9f:fbb:3a85` は oyaki の **`enp3s0`** のリンクローカルアドレス
- `HostKeyAlias` を付けないと `Host key verification failed` になる
- リンクが切れたら `ping6 -c4 ff02::1%en9` で再探索する

---

## 4. 現在動くもの

### テトリス（自作、自動プレイ）

```bash
bash /tmp/led_game.sh start    # 起動
bash /tmp/led_game.sh status   # 状態
bash /tmp/led_game.sh stop     # 停止
```

- 10列×20行、セル19px。FC6パレットのみ使用
- El-Tetris 系の評価関数（高さ・ライン・穴・凸凹）で自動プレイ
- 実測 22.8fps、4台とも `dropped=0`
- 効果音9種 + オリジナルBGM（Aマイナー、140BPM、8小節ループ）

### ブロック崩し（master の `host/block_breaker.py`）

```bash
bash /tmp/bb.sh start   # 起動（露出設定を焼いてから起動する）
bash /tmp/bb.sh status
bash /tmp/bb.sh stop
```

- **表示は正常**。4台とも 32fps、`dropped` 0〜1
- `block_breaker_selftest.py` → `0 errors`
- **カメラ操作は機能していない**（§6参照）

### 表示テスト各種

```bash
/home/th1/hackathon-2026/.venv/bin/python /tmp/stripes.py v 120   # 縦縞 R/G/B
/home/th1/hackathon-2026/.venv/bin/python /tmp/stripes.py h 120   # 横縞 R/G/B
/home/th1/hackathon-2026/.venv/bin/python /tmp/id_display.py 120  # 各パネルにID表示
```

---

## 5. 音声

| 項目 | 値 |
|---|---|
| 出力先 | **`plughw:0,0`**（card 0 = ALC671 アナログ / AUX） |
| スピーカー | Bose SoundLink Revolve（**AUX接続**。USB接続では鳴らなかった） |
| ミキサー | Master / Headphone / PCM / Line Out を 100%・unmute、Auto-Mute Mode = Disabled |

### 遅延対策（重要な知見）

当初 **約1.5秒**の遅延があった。原因は `aplay` のバッファではなく **Python と `aplay` 間の OS パイプ**（既定64KB = 22050Hz で約1490ms）。

| 対策 | 内容 |
|---|---|
| パイプ容量 | `fcntl(F_SETPIPE_SZ=1031, 4096)` で 4096B に縮小（Linux下限=1ページ） |
| サンプルレート | 22050 → **44100Hz**（同バイト数でも時間換算が半分になる） |
| aplay バッファ | `--buffer-time=40000 --period-time=10000` |
| **結果** | **約1.5秒 → 約86ms** |

`th1` を `audio` / `video` グループに追加済み（デバイスアクセスに必須だった）。

---

## 6. 未解決の問題

### 6.1 カメラ検出の破綻（最優先）

`block_breaker.py` の JUMP・左右移動とも機能していない。キャリブレーション実測値:

| 項目 | 実測 | しきい値 | 判定 |
|---|---|---|---|
| `rise_y_p90`（重心の上昇） | **−0.0079** | 0.075 | 未達 |
| `rise_bottom_max`（下端の上昇） | **−0.239** | 0.06 | 未達 |
| 左右の x 差（span） | **0.006** | — | 左右移動も検出不能 |
| 前景の外接矩形 | `(0,0)-(240,180)` = **画面全体** | — | 背景差分が破綻 |

**JUMP条件は AND**（`rise_y >= .075 and rise_bottom >= .06`, `block_breaker.py:110`）なので、`rise_bottom` が満たされない限り成立しない。

前景が全画面に広がり、左右に寄っても重心 x がほぼ動かない（常に 0.62〜0.76 の右寄りに張り付く）。**背景モデルが機能していない**。

**次の一手（未実施）**: カメラの生画像と前景マスクを PNG で吸い出して目視確認する。推測で直す前に何が写っているかを確定させること。仮説としては、カメラがLEDウォール自体を捉えており、常時変化する表示が前景として検出されている可能性がある（未検証）。

### 6.2 カメラの露出設定（原因判明・回避策あり）

**`CAP_PROP_EXPOSURE` を明示しないと 30fps → 4fps に落ちる。**

| 条件 | fps | 平均輝度 |
|---|---|---|
| 何も設定しない | **4.0** | 243（白飛び） |
| `AUTO_EXPOSURE=1` のみ | **4.0** | 207 |
| `AUTO_EXPOSURE=3`（自動へ戻す） | **4.0** | 212（**戻らない**） |
| `AUTO_EXPOSURE=1 + EXPOSURE=312 + GAIN=2` | **30.0** | 99 ← 最適 |

- UVC の設定は**デバイス側に残る**ため、一度手動露出にすると次回以降も居座る
- OpenCV から自動露出へ戻せない（読み戻すと 1 のまま）
- `block_breaker.py` は露出を設定しないので、**起動前に `/tmp/camset.py 312 2` を実行する必要がある**（`/tmp/bb.sh` には組み込み済み）
- `v4l2-ctl`（v4l-utils）未インストール。対応モード一覧が引けていない
- 640×480 固定（1280×720 を要求しても 640×480 が返る）。理由未調査

> `agy` ワーカーに See3CAM_130 の仕様調査を投げたが、**結果は未回収**（job_id `c305819036e7`）。

### 6.3 `pi_client` の2つの制約（master のコード）

**(a) `frame_id` 巻き戻しの無言破棄** — `pi-client/pi_client.cc:338`

```c
if (has_displayed && frame_id <= last_displayed) {
  if (last_displayed - frame_id < kResyncThreshold) continue;  // dropped++ しない
```

主機側スクリプトを再実行すると `frame_id` が0に戻る。巻き戻し量が再同期しきい値（600）未満だと**全フレームが無言で捨てられ、`dropped` にも計上されない**。「送っているのに映らない、統計上は正常」という切り分け困難な状態を作る。

回避: 送信側で `frame_id` を永続化して単調増加させる（`/tmp/tricolor_frameid` で実施済み）。または送信前に `pi_client` を再起動。

**(b) 死活報告先が初回学習のみ** — `pi-client/pi_client.cc:241`

```c
if (!host_known) { ...; host_known = true; }
```

主機のアドレスが変わると報告が届かなくなる。IP移行直後に `reporters=0` になった。**再起動が必要**。

### 6.4 電源（物理対処が必要）

| 機体 | `vcgencmd get_throttled` | 意味 |
|---|---|---|
| `pi2` (`.102`, rasppi02) | **`0x50005`** | **現在進行形で低電圧＋スロットリング** |
| `pi4` (`.104`, rasppi01) | `0x50000` | 過去に低電圧・スロットリング発生 |
| `pi1` (`.101`, rp1) | `0x0` | 正常 |
| `pi3` (`.103`, rasppi03) | n/a | — |

上から2番目のスロットは**別個体（旧 rasppi04）でも同じ症状で落ちた**実績がある。個体ではなく**そのスロットの給電系**（アダプタ・ケーブル・タップ）が原因と考えられる。要物理対処。

### 6.5 その他

- `pi2`（`.102`）の `start_app.service` を **disable 済み**。旧常駐 `Server_UDP` + `led-image-viewer` が `pi_client` と HUB75 GPIO を奪い合い、**チラつきの原因**だった。戻す場合は `sudo systemctl enable start_app.service`
- `pi_client` の自動起動（systemd unit 化）は**未実施**

---

## 7. ファイル配置と永続化

### oyaki `/tmp`（揮発性 — 要移設）

| ファイル | 内容 |
|---|---|
| `tetris.py` | テトリス本体（自作） |
| `sfx.py` | 効果音・BGMミキサー（自作） |
| `led_game.sh` | テトリス起動ラッパー（PIDファイル管理） |
| `bb.sh` | ブロック崩し起動ラッパー（露出設定込み） |
| `camset.py` | カメラ露出・ゲイン設定 |
| `calibrate.py` | キャリブレーション（自作、LED誘導＋効果音） |
| `motion_input.py` / `camview.py` / `camrun.sh` | 自作の検出器と可視化（`block_breaker` とは別系統） |
| `stripes.py` / `id_display.py` | 表示テスト |
| `hl_recv.py` | `pi_client` 相当のプロトコル検証受信機 |
| `calib.json` | キャリブレーション結果 |
| `tricolor_frameid` | `frame_id` の永続カウンタ |

### oyaki `~/hackathon-2026`

- master の `host/` `pi-client/` `requirements.txt` を展開済み（**git 管理外のコピー**）
- 旧 `host/` は `host.bak.<時刻>` に退避
- `.venv` あり（`numpy 2.5.1` / `opencv 5.0.0`）

### Pi 各機 `~/hackathon-2026/pi-client/`

- `pi_client.cc` + `Makefile` + ビルド済みバイナリ `pi_client`
- ソースは master / develop と同一（sha256 `544c8266b5ece8bb`）
- `~/rpi-rgb-led-matrix` は元から存在

### リポジトリ

**本日一切変更していない。** master は `887ca03`、develop は `cea3b1a` を取得済み（ローカルブランチは未更新）。

---

## 8. 次にやるべきこと（優先順）

1. **カメラの生画像と前景マスクを目視確認** — §6.1。推測で直さない。PNGで吸い出して何が写っているか確定させる
2. **`pi2` の給電を対処** — §6.4。落ちると表示が欠ける
3. **`/tmp` の実装をリポジトリへ移設** — §7。再起動で消える。`host/games/` あたりに置いて `develop` ベースのブランチを切る
4. **`pi_client` の systemd unit 化** — Pi 再起動で表示が復帰するように
5. **Mac に `192.168.20.50` を設定** — `ssh oyaki` が普通に通るようになる
6. **`docs/NETWORK.md` の更新** — 「現状」表が 2026-08-06 時点で「セグメントA未設定」のまま。実機は設定完了。Mac の NIC 名も `en7` → `en9`
7. **`pi_client` の2つの制約への対処** — §6.3。少なくとも `dropped` に計上する、報告先を毎回更新する
8. **`agy` の調査結果を回収** — job_id `c305819036e7`

---

## 9. 検証済みの事実（推測でないもの）

- 主機の送信経路: `selftest.py` **0 errors**、ヘッドレスループバックで4ターゲット各120フレーム、`bad_magic` / `bad_crc` / `bad_len` / パレット違反 / `frame_id` 逆行すべて **0**
- `block_breaker_selftest.py` **0 errors**
- 実表示: テトリス 22.8fps、ブロック崩し 32fps、いずれも4台 `reporters=4`
- 音声: 1秒WAVの再生に0.93秒を要した（実時間消費）＋ AUX で実際に聞こえることを確認済み
- カメラ: 640×480 / 30.0fps / UYVY でキャプチャ可能

## 10. 未検証・未確認

- カメラが実際に何を写しているか（画像を見ていない）
- See3CAM_130 の対応解像度・フレームレート一覧（`v4l2-ctl` 未導入）
- 1280×720 が通らない理由
- `pi3`（`.103`）の `vcgencmd` が n/a な理由
- キャリブレーション結果 `/tmp/calib.json` の値は**すべて異常値**であり、そのまま使ってはいけない
