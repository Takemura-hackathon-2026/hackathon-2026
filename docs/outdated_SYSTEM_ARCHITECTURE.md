# [OUTDATED] システム構成・処理ロジック

> 2026-09-04の実機再確認前に作成した旧構成図。現行構成はリポジトリ直下の `README.md` と `spec.md` を参照する。

現行実機（2026-09-02時点）と、リポジトリ内の実装から読み取れる処理を Mermaid で示す。
接続先の正本は [`CONNECTION_INFO.md`](CONNECTION_INFO.md) とし、図中の「現行」は同文書に従う。

## 1. 現行システム構成

```mermaid
flowchart LR
    sensor["STRUCTURE Sensor"]

    subgraph sensorNode["センサーPi: pi3-sensor"]
        capture["OpenNI2 深度取得<br/>structure_depth_capture.cpp"]
        detect["背景差分・人物検出・追跡<br/>SensorController"]
        agent["入力状態送信<br/>sensor_agent.py"]
        runtime["実行時設定・テレメトリ<br/>SensorRuntimeServer"]
    end

    subgraph controlNode["制御Pi: pi3-control"]
        input["入力受信・CRC・順序制御<br/>InputStateReceiver"]
        game["ゲーム状態更新<br/>ClassicThenBoss / BlockBreaker"]
        render["192 x 384<br/>パレットインデックス描画"]
        frameTx["フレーム分割・UDP送信<br/>UdpFrameSender系"]
        healthRx["死活・FPS・欠損受信"]
    end

    switch["1 GbE スイッチ<br/>表示専用LAN 192.168.10.0/24"]

    subgraph displayNodes["表示Pi 3台（現行）"]
        pi1["pi1  .101<br/>target_id 0<br/>pi_client"]
        pi2["pi2  .102<br/>target_id 1<br/>pi_client"]
        pi4["pi4  .104<br/>target_id 3<br/>pi_client"]
    end

    led["RGB LEDパネル群<br/>HUB75"]

    subgraph development["管理・開発"]
        mac["開発Mac<br/>192.168.20.50"]
        oyaki["Ubuntu主機 oyaki<br/>192.168.20.1"]
    end

    sensor -->|"USB / 深度フレーム"| capture
    capture --> detect --> agent
    runtime -.->|"設定反映"| detect
    agent -->|"判定済み入力 UDP 5200<br/>深度画像は送らない"| input
    input --> game --> render --> frameTx
    game -->|"人物ロック解除 UDP 5201"| agent
    frameTx --> switch
    switch -->|"フレームチャンク UDP 5000"| pi1
    switch -->|"フレームチャンク UDP 5000"| pi2
    switch -->|"フレームチャンク UDP 5000"| pi4
    pi1 --> led
    pi2 --> led
    pi4 --> led
    pi1 -.->|"PIHEALTH UDP 5101"| healthRx
    pi2 -.->|"PIHEALTH UDP 5101"| healthRx
    pi4 -.->|"PIHEALTH UDP 5101"| healthRx
    mac <-->|"SSH / 配備<br/>セグメントB"| oyaki
    oyaki -.->|"管理・配備"| controlNode
    oyaki -.->|"管理・配備"| sensorNode
```

## 2. 1フレームの処理シーケンス

```mermaid
sequenceDiagram
    autonumber
    participant S as STRUCTURE Sensor
    participant SA as sensor_agent
    participant C as SensorController
    participant TX as InputStateSender
    participant RX as InputStateReceiver
    participant G as ゲームループ
    participant F as フレーム送信
    participant P as 表示Pi pi_client
    participant L as HUB75 LED
    participant H as 死活監視

    S->>SA: 深度フレーム
    SA->>C: read(now)
    C->>C: 反転・ROI・縮小
    C->>C: 背景差分・ノイズ閾値・形態処理
    C->>C: 人物候補抽出・追跡・操作対象ロック
    C-->>SA: InputState
    SA->>TX: sequence付きで符号化
    TX->>TX: magic / version / flags / CRC32
    TX-->>RX: UDP 5200
    RX->>RX: CRC・形式・sequence検証
    RX-->>G: 最新の連続状態 + 一度きりのイベント
    G->>G: 入力反映・衝突判定・状態遷移
    G->>G: 192 x 384インデックス画像を描画
    G->>F: frame_id・palette_mode・画像
    F->>F: 表示領域へ分割し1200 byte単位へチャンク化
    F->>F: 各チャンクにCRC32を付与
    F-->>P: UDP 5000
    P->>P: 宛先・CRC・チャンク完全性・パレット範囲を検証
    P->>P: LUTでRGB変換・パネル補正
    P->>L: SwapOnVSync
    P-->>H: PIHEALTH UDP 5101（1秒ごと）
```

## 3. センサー入力判定ロジック

```mermaid
flowchart TD
    start(["深度フレーム受信"])
    transform["上下・左右反転<br/>ROI適用・240px幅へ縮小"]
    background{"背景学習中?"}
    learn["複数フレームを蓄積"]
    model["中央値背景と<br/>画素別ノイズP95を生成"]
    diff["背景 - 現在深度<br/>手前方向の変化だけを採用"]
    threshold["閾値 = max<br/>60mm, 設定値, 画素別ノイズ x 3"]
    morphology["OPEN・CLOSE<br/>時間方向の多数決"]
    gate["面積・形状・継続性で<br/>人物候補を選別"]
    track["候補を人物トラックへ対応付け"]
    active{"ロック中の人物が<br/>有効?"}
    keep["同じ人物を維持"]
    select["最も手前の安定人物を選択"]
    classify["位置・移動・ジャンプを分類"]
    mode{"開始モード"}
    still["同じ横位置で3秒静止"]
    passby["人物の通過を連続確認"]
    arm["腕回し判定のlaunchを使用"]
    output["InputState生成<br/>body_x / lateral / jump / start_trigger<br/>player_id / people_detected"]
    empty["空のInputState"]

    start --> transform --> background
    background -->|"はい"| learn --> empty
    background -->|"終了直後"| model --> diff
    background -->|"いいえ"| diff
    diff --> threshold --> morphology --> gate --> track --> active
    active -->|"はい"| keep --> classify
    active -->|"いいえ"| select --> classify
    classify --> mode
    mode -->|"still"| still --> output
    mode -->|"passby"| passby --> output
    mode -->|"arm-circle"| arm --> output
```

## 4. ゲーム進行ロジック

```mermaid
stateDiagram-v2
    [*] --> SensorBackground: 起動
    SensorBackground --> WaitingPlayer: 背景学習完了
    WaitingPlayer --> Countdown: start_trigger
    Countdown --> Serving: カウントダウン完了
    Serving --> Playing: launch

    Playing --> Playing: 壁・パドル・ボスで反射
    Playing --> BossEffect: ボスへ命中
    BossEffect --> Playing: HP残存
    BossEffect --> StageClear: HPゼロ

    Playing --> LifeLost: ボール落下またはビーム被弾
    LifeLost --> WaitingPlayer: 残機あり・人物ロック解除
    LifeLost --> GameOver: 残機ゼロ
    GameOver --> WaitingPlayer: 約1.8秒後に全リセット

    StageClear --> WaitingPlayer: 次ステージ要求・人物再選択
    StageClear --> WaitingPlayer: 最終ボス後の全リセット
```

ゲームループ内では `dt` を最大40msへ制限し、ボール移動量に応じて最大8回のサブステップへ分割する。
各サブステップで左右壁、上端、パドル、ボス／バリア、画面下への落下を順に判定する。

## 5. 表示Piのフレーム受信ロジック

```mermaid
flowchart TD
    receive(["UDPパケット受信"])
    header{"ヘッダー長・magic・<br/>target_id・palette_modeは有効?"}
    crc{"chunk_id / count / size<br/>CRC32は有効?"}
    newFrame{"新しいframe_id?"}
    reset["未完成の旧フレームを破棄<br/>Assemblerを初期化"]
    duplicate{"受信済みチャンク?"}
    store["固定バッファへ格納<br/>末尾先着時は一時保留"]
    complete{"全チャンク受信?"}
    size{"192 x 96 byteを満たす?"}
    palette{"全画素がパレット範囲内?"}
    order{"古いframe_id?"}
    resync{"大幅な巻き戻り?"}
    convert["FC6 / MSX16 LUTでRGB変換<br/>180度回転・パネル補正"]
    display["SwapOnVSyncで表示"]
    discard(["破棄して次を待つ"])

    receive --> header
    header -->|"いいえ"| discard
    header -->|"はい"| crc
    crc -->|"いいえ"| discard
    crc -->|"はい"| newFrame
    newFrame -->|"はい"| reset --> duplicate
    newFrame -->|"いいえ"| duplicate
    duplicate -->|"はい"| discard
    duplicate -->|"いいえ"| store --> complete
    complete -->|"いいえ"| receive
    complete -->|"はい"| size
    size -->|"いいえ"| discard
    size -->|"はい"| palette
    palette -->|"いいえ"| discard
    palette -->|"はい"| order
    order -->|"いいえ"| convert
    order -->|"はい"| resync
    resync -->|"小さな巻き戻り"| discard
    resync -->|"大幅な巻き戻り"| convert
    convert --> display --> receive
```

## 6. 実装状態と境界

- 現行実機は表示Pi 3台（`.101`、`.102`、`.104`）。`.103` / `target_id 2` は使わない。
- リポジトリ内の汎用 `UdpFrameSender` は、192×384を192×96の4領域へ分ける旧4台構成を前提とする。
  現行3台への送信は、制御Piへ配備済みの3台用サービス設定を使う。
- `READY` 応答（UDP 5100）とGPIO物理同期は未実装。現状は各表示Piが完成フレームを個別に
  `SwapOnVSync` する同期段階Aである。
- センサーPiから制御Piへ送るのは判定済みの入力状態だけで、深度画像・人物マスクは送らない。
- パレット定義の正本は `host/palettes.py` と `host/palettes.json`。

## 7. 主な実装対応

| 図の責務 | 実装 |
|---|---|
| 深度フレーム取得 | `host/structure_depth_capture.cpp`, `host/frame_source.py` |
| 背景差分・人物追跡・入力分類 | `host/block_breaker.py` の `SensorController` |
| センサー側常駐処理 | `host/sensor_agent.py` |
| 入力UDP・再選択制御 | `host/input_transport.py` |
| ゲーム更新・描画 | `host/block_breaker.py` の `ClassicThenBoss`, `BlockBreaker` |
| フレーム分割・UDP送信 | `host/test_mode/test_mode.py` の `UdpFrameSender` |
| 表示Piでの再構成・表示 | `pi-client/pi_client.cc` |
| 現行の接続先 | `docs/CONNECTION_INFO.md` |
