# 実機運用メモ（git 未収録情報のまとめ）

このファイルは、**リポジトリのコード・README・docs に反映されていない実機作業の知見**を
1か所へ集めたもの。出典は 2026-08-20 の実機作業記録（セッションメモ）で、当時の実測値である。

> 検証状況: 2026-08-21 時点で親機 `oyaki` へ ssh を試みたが到達せず（ハードウェア未接続）、
> 本ファイルの記載は**再実測できていない**。実機を再接続したら各項目を再確認すること。
> また「未コミット」と書いた実装は oyaki 上のみに存在し、本リポジトリには入っていない
> （2026-08-21 時点で `git status` はクリーン、`host/distance_probe.py` 等は不在）。

---

## 1. 接続経路・機材構成

### 親機 oyaki への入り方
- ssh config 上は `oyaki` = `th1@192.168.20.1` だが、**Mac 側 en9(USB-Ethernet) に
  `192.168.20.50` が設定されていないため到達しない。**
- **`ssh th1@169.254.10.1` で入る**（親機 `enp3s0` に併記してある link-local 退避アドレス）。
  Mac 側の link-local アドレスは再割当で変わるが、親機側は固定なので影響しない。
- `docs/NETWORK.md` は `169.254.10.1` を「旧設定の退避用」と書いているが、
  **実運用ではこちらが主経路**になっている。

### Pi 4 台
- `192.168.10.101`〜`.104`、ユーザー **`takemuralab`**、親機から鍵認証。
  `target_id` = 第4オクテット − 101（NETWORK.md の規約どおり）。
- **4 台とも Raspberry Pi 4 Model B / Ubuntu Server 22.04。**
  `.102` は 2026-08-20 に Pi3 から Pi4 へ交換した。
- 起動オプションは **4 台共通**:

```bash
--led-pwm-bits=7 --led-slowdown-gpio=4 --led-pwm-lsb-nanoseconds=100
```

  既定の 11bit のままだとリフレッシュが 78Hz まで落ちてチラつく。

### docs/NETWORK.md 「現状」表との差分
NETWORK.md の現状表は 2026-08-06 時点で「セグメントA 未設定 / 実機疎通未確認」となっているが、
2026-08-20 に **4 台起動・フレーム配信・死活報告まで実機で動作確認済み**。表は未更新。

---

## 2. Pi 運用の落とし穴（すべて実際に踏んだもの）

- **`--rotate180` は付けない。** 旧 `restart_client.sh` は target2/3 に付けていたが、
  実機確認すると 3・4 段目が上下逆になった。4 台とも付けないのが正しい。
- **プロセス停止は `sudo pkill -x pi_client`。**
  `pkill -f '[p]i_client'` 形式は ssh 越しのクォート展開に失敗し、旧プロセスが残って
  二重描画になる事故があった。`pgrep -f <文字列>` も ssh のコマンドライン自身にマッチするので使わない。
- **`snd_bcm2835` が有効だと rpi-rgb-led-matrix が起動を拒否する**（HW パルスと競合）。
  既存機は `/etc/modprobe.d/raspi-blacklist.conf` の `blacklist snd_bcm2835` で無効化済み。
  新規セットアップ時はこのファイル作成 + `modprobe -r snd_bcm2835` が必要。
- **Pi3 では UDP 受信バッファが溢れる**（Pi4 交換で解消）。`pi_client` は `SO_RCVBUF` に 4MB を
  要求するが、カーネル既定 `net.core.rmem_max=212992` にクランプされる。Pi4 は既定値のまま
  `RcvbufErrors=0`。将来 Pi3 を使うなら `sysctl -w net.core.rmem_max=16777216`。
- **セグメントA はゲートウェイ・DNS を持たないので apt が通らない。**
  新機体へパッケージを入れる時は他機の `/var/cache/apt/archives` から集め、不足分を Mac から
  **`ports.ubuntu.com`**（arm64 は `archive.ubuntu.com` ではない）の pool へ直接 curl して
  親機経由で配り、`dpkg -i *.deb` + `dpkg --configure -a` で入れる（2026-08-20 に実施）。
  バージョンは他機の `dpkg-query -W` に合わせる。
- **Pi は RTC を持たず NTP にも届かないので時刻が大きくずれる**（新機体は約 2 年ずれていた）。
  `make` が clock skew 警告を出す。親機の `date -u` を各機へ `sudo date -u -s` で流し込んで揃える。
- **新規 Pi の cloud-init**: Raspberry Pi Imager のカスタマイズには users 定義が入らないので
  `user-data` に自分で書く。`packages:` はネット不通で失敗しブートが遅延するのでコメントアウトする。

### 親機の `~/restart_client.sh`（リポジトリ未収録）
- 2026-08-20 に現行セグメント向けへ更新済み。旧版は `restart_client.sh.bak-50seg` として退避。
- 4 台へ rsync → make → 起動 → 確認までを 1 コマンドで実行する。
- make/g++ が無い機体へは、ビルドできた機体のバイナリを配るフォールバックを持つ。
- 実行して 4 台起動を実測確認済み。
- **このスクリプトは git に入っていない。** 回収するならまずこれ。

---

## 3. カメラ入力（`host/block_breaker.py`）— 未コミットの設計変更

以下は 2026-08-20 に oyaki 上で実装・実測した内容で、**本リポジトリの
`host/block_breaker.py` には入っていない**（現行コードは旧来の相対移動方式のまま）。

### 実測で判明した落とし穴
- **キャリブレの閾値と baseline は必ずセットで使う。** 閾値は「その時の baseline からの相対量」。
  閾値だけ移植して baseline を自前取得のままにすると `rise_bottom` が常に負になり一切反応しない。
- **収集はゲートを通す。** `camera_calibrate.py` の `eligible()` は「その動作が実際に起きている
  フレームだけ」を記録する。全フレームを記録するとジャンプの滞空は 2 割程度なので p25 が 0 に潰れる。
- **カメラは人を正面から撮るので左右が鏡像になる。** プレイヤーが自分の左へ動くと画像上は x 増加。
  `--mirror` で吸収する。左右の閾値は非対称なので単純な符号反転では足りず、
  **どちらの閾値を使うかも入れ替える**必要がある。
- **`bottom` が 1.0 に張り付いたら足元が画角外。** 跳んでも下端が動かず判定不能。距離帯を変える。

### 入力方式の変更（2026-08-20）
- パドルは **絶対位置マッピング**。カメラ内の横位置をそのままパドル位置にする。
  旧来の lateral 相対移動（`paddle_speed` 175px/s + 候補待ち 0.12 秒）は端まで 0.86 秒かかりラグが酷い。
  ※本リポジトリの現行コードはこの旧方式（`host/block_breaker.py` の `paddle_speed = 175.0`）。
- 揺れの除去は平滑化ではなく **不感帯**（`--position-deadzone`、既定 3px）で行う。
  誤差から不感帯ぶんを引いてから高ゲイン（既定 0.85）で追従するので、追従の速さと揺れ耐性が両立する。
  平滑化を強める方式は必ずラグとして体感される。
- `--play-range LOW,HIGH` でカメラ内の何割の移動で端まで届くかを調整（`0.3,0.7` なら画角の 40%）。
- **人は複数追跡し、ジャンプした人へ操作権が移る**（Tracker / Person）。
  基準は人ごとに直近 1.2 秒の中央値から作るので、誰がどこに立っても体格が違っても成立する。
  これにより固定 baseline は不要になった。
- `--jump-debug` で毎秒の実測（`rise_y` / `rise_bottom` / `offset` と各閾値）が stderr に出る。
  反応しない時はまずこれを見る。

### 追加ツール（リポジトリ未収録）
- **`host/distance_probe.py`**: 近影 / 中距離 / 遠影の 3 距離帯を LED 表示つきで実測し
  `camera_calibration_multi.json` へ保存する。
  `block_breaker.py --calibration-zone NEAR|MID|FAR` で使う。
- **親機は画面が無いので、操作は `/dev/input/event12`（SEM USB Keyboard）を直接読む。**
  `th1` は input グループ所属済み。tty もログインも不要で、ssh から起動しても物理キーボードで操作できる。
  自動検出は `find_keyboard()`。カメラ（See3CAM_130）も kbd ハンドラを持つので、
  名前と LED でスコアリングして選別している。

### README との差分
README は「ジャンプ判定は重心上昇 0.05、下端上昇 0.04 を既定値」「左右移動でパドルを動かす」
と書いており、上記の絶対位置マッピング・複数人追跡・鏡像補正・距離帯校正は反映されていない。

---

## 4. 回収すべきもの（TODO）

1. oyaki 上の `host/block_breaker.py` 改修版（絶対位置 / 不感帯 / `--mirror` / Tracker / `--jump-debug`）
2. oyaki 上の `host/distance_probe.py` と `camera_calibration_multi.json` のスキーマ
3. 親機 `~/restart_client.sh`（4 台一括再起動、バイナリ配布フォールバック付き）
4. `docs/NETWORK.md` の「現状」表と接続経路（`169.254.10.1`、ユーザー `takemuralab`、Pi4×4）の更新
5. `pi-client` 起動オプションの既定化（`--led-pwm-bits=7` 他）と `--rotate180` を使わない旨の明記
6. README のカメラ入力節を新方式へ更新
