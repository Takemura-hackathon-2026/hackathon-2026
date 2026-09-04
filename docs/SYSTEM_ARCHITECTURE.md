# 現行システム構成・処理ロジック

基準日: 2026-09-04

本書は [`../spec.md`](../spec.md) §4〜§8を図示したもの。仕様と図が矛盾する場合は
`spec.md` を正とする。制御Piと表示Pi 3台の値は実機の稼働サービスから確認済み。

## 1. システム構成

```mermaid
flowchart LR
    depthSensor["STRUCTURE Sensor"]

    subgraph inputNet["入力ネットワーク 192.168.50.0/24"]
        subgraph sensorPi["センサーPi<br/>pi3-sensor / .33"]
            capture["OpenNI2深度取得"]
            detection["背景差分・人物検出<br/>追跡・入力分類"]
            inputTx["sensor_agent.py<br/>入力状態送信"]
        end

        subgraph controlPi["制御Pi<br/>pi3-control / .32"]
            inputRx["入力受信<br/>CRC・sequence検証"]
            game["ゲーム更新<br/>60fps"]
            renderer["MSX16描画<br/>192×384"]
        end
    end

    subgraph displayNet["表示専用LAN 192.168.10.0/24"]
        frameTx["制御Pi表示側 / .2<br/>3分割・UDP送信"]
        networkSwitch["1 GbEスイッチ"]

        subgraph displayPis["表示Pi 3台"]
            pi1["上段: pi1 / .101<br/>target 0 / 192×128"]
            pi4["中段: pi4 / .104<br/>target 1 / 192×128"]
            pi2["下段: pi2 / .102<br/>target 2 / 192×128"]
        end
    end

    subgraph ledWall["RGB LED表示面 192×384"]
        upper["上段 24枚<br/>8列×3段"]
        middle["中段 24枚<br/>8列×3段"]
        lower["下段 24枚<br/>8列×3段"]
    end

    depthSensor -->|"USB深度フレーム"| capture
    capture --> detection --> inputTx
    inputTx -->|"判定済み入力 UDP 5200"| inputRx
    inputRx --> game --> renderer --> frameTx
    game -->|"人物再選択 UDP 5201"| inputTx
    frameTx --> networkSwitch
    networkSwitch -->|"UDP 5000 / target 0"| pi1
    networkSwitch -->|"UDP 5000 / target 1"| pi4
    networkSwitch -->|"UDP 5000 / target 2"| pi2
    pi1 --> upper
    pi4 --> middle
    pi2 --> lower
    pi1 -.->|"PIHEALTH UDP 5101"| frameTx
    pi4 -.->|"PIHEALTH UDP 5101"| frameTx
    pi2 -.->|"PIHEALTH UDP 5101"| frameTx
```

深度画像と人物マスクはセンサーPiから外へ送らない。ネットワークを流れるのは、
人物位置、左右入力、ジャンプ、開始イベント、人物IDなどの判定済み入力だけである。

## 2. LED表示面と送信順

```mermaid
flowchart TB
    frame["論理フレーム<br/>192×384 / MSX16"]
    split["上から128行ずつ3分割"]
    slice0["y=0..127<br/>target 0"]
    slice1["y=128..255<br/>target 1"]
    slice2["y=256..383<br/>target 2"]
    node0["pi1 / 192.168.10.101<br/>chain 8 × parallel 3"]
    node1["pi4 / 192.168.10.104<br/>chain 8 × parallel 3"]
    node2["pi2 / 192.168.10.102<br/>chain 8 × parallel 3"]
    top["LED上段<br/>192×128 / 24枚"]
    center["LED中段<br/>192×128 / 24枚"]
    bottom["LED下段<br/>192×128 / 24枚"]

    frame --> split
    split --> slice0 --> node0 --> top
    split --> slice1 --> node1 --> center
    split --> slice2 --> node2 --> bottom
    top --> center --> bottom
```

宛先はIPアドレス順ではない。送信引数と物理表示順は必ず
`.101 → .104 → .102` の順に保つ。

## 3. 1フレームの処理シーケンス

```mermaid
sequenceDiagram
    autonumber
    participant DS as STRUCTURE Sensor
    participant SA as sensor_agent
    participant SC as SensorController
    participant IR as InputStateReceiver
    participant GM as ゲームループ
    participant FT as フレーム送信
    participant PC as pi_client × 3
    participant LED as HUB75 LED

    DS->>SA: 深度フレーム
    SA->>SC: read(now)
    SC->>SC: 背景差分・人物候補抽出
    SC->>SC: 人物追跡・操作対象ロック
    SC->>SC: body_x・lateral・イベント生成
    SC-->>SA: InputState
    SA->>SA: sequence・flags・CRC32を付与
    SA-->>IR: UDP 5200
    IR->>IR: CRC・形式・sequenceを検証
    IR-->>GM: 最新状態と一度きりのイベント
    GM->>GM: 入力反映・衝突判定・状態遷移
    GM->>GM: 192×384 MSX16フレーム描画
    GM->>FT: frame_id・palette・画素配列
    FT->>FT: 192×128×3へ分割
    FT->>FT: 1200 byte単位へ分割・CRC32付与
    par target 0
        FT-->>PC: .101:5000
    and target 1
        FT-->>PC: .104:5000
    and target 2
        FT-->>PC: .102:5000
    end
    PC->>PC: チャンク再構成・全項目検証
    PC->>PC: MSX16 LUT・パネル校正
    PC->>LED: SwapOnVSync
    PC-->>FT: PIHEALTH UDP 5101
```

## 4. センサー入力判定ロジック

```mermaid
flowchart TD
    receive(["深度フレーム取得"])
    transform["設置方向の反転<br/>ROI・縮小処理"]
    learning{"背景学習中?"}
    collect["無人フレームを蓄積"]
    background["画素別中央値背景と<br/>ノイズP95を生成"]
    foreground["背景より手前の変化だけを抽出"]
    filter["OPEN・CLOSE・時間方向合意"]
    candidates["面積・形状・継続性で<br/>人物候補を選別"]
    tracking["時系列で人物を追跡"]
    locked{"ロック対象が有効?"}
    keep["同じ人物を維持"]
    select["最も手前の安定人物を選択"]
    classify["横位置・移動・ジャンプを分類"]
    still{"横位置を3秒維持?"}
    state["InputState生成"]
    idle["無入力状態"]

    receive --> transform --> learning
    learning -->|"はい"| collect --> idle
    learning -->|"完了"| background --> foreground
    learning -->|"いいえ"| foreground
    foreground --> filter --> candidates --> tracking --> locked
    locked -->|"はい"| keep --> classify
    locked -->|"いいえ"| select --> classify
    classify --> still
    still -->|"はい"| state
    still -->|"いいえ"| state
    state -->|"UDP 5200"| receive
```

入力が0.50秒以上途絶えた場合、制御Piは無入力へ戻す。古いsequence、不正CRC、
未定義flagsは破棄し、イベントフラグは新しいsequenceごとに一度だけ消費する。

## 5. ゲーム状態遷移

```mermaid
stateDiagram-v2
    [*] --> BackgroundLearning: 起動
    BackgroundLearning --> PlayerWaiting: 背景学習完了
    PlayerWaiting --> StartCountdown: 人物が3秒静止
    StartCountdown --> Serving: カウントダウン完了
    Serving --> Playing: ボール発射

    Playing --> Playing: 壁・上端・パドルで反射
    Playing --> BossHit: ボスまたはバリアへ命中
    BossHit --> Playing: HPまたは段階が残る
    BossHit --> StageTransition: ステージクリア

    Playing --> LifeLost: ボール落下・ビーム被弾
    LifeLost --> PlayerWaiting: 残機あり・人物ロック解除
    LifeLost --> GameOver: 残機ゼロ
    GameOver --> PlayerWaiting: 表示後に全リセット

    StageTransition --> PlayerWaiting: 次ステージ・人物再選択
    StageTransition --> Victory: 全ボス撃破
    Victory --> PlayerWaiting: 表示後に全リセット
```

## 6. 表示Piの受信・表示ロジック

```mermaid
flowchart TD
    packet(["UDP 5000受信"])
    header{"magic・target・palette・<br/>chunk情報は有効?"}
    crc{"payload長とCRC32は有効?"}
    current{"現在のframe_id?"}
    reset["未完成の旧フレームを破棄し<br/>Assemblerを初期化"]
    duplicate{"重複チャンク?"}
    store["固定バッファへ格納"]
    complete{"全チャンク受信?"}
    frameSize{"192×128を満たす?"}
    palette{"全画素がMSX16範囲内?"}
    order{"表示済みより新しい?"}
    restart{"大幅な巻き戻り?"}
    lut["MSX16 LUTでRGB変換"]
    calibration["向きとパネル校正を適用"]
    swap["SwapOnVSync"]
    discard(["破棄"])

    packet --> header
    header -->|"いいえ"| discard
    header -->|"はい"| crc
    crc -->|"いいえ"| discard
    crc -->|"はい"| current
    current -->|"新規"| reset --> duplicate
    current -->|"同一"| duplicate
    duplicate -->|"はい"| discard
    duplicate -->|"いいえ"| store --> complete
    complete -->|"いいえ"| packet
    complete -->|"はい"| frameSize
    frameSize -->|"いいえ"| discard
    frameSize -->|"はい"| palette
    palette -->|"いいえ"| discard
    palette -->|"はい"| order
    order -->|"はい"| lut
    order -->|"いいえ"| restart
    restart -->|"小さな巻き戻り"| discard
    restart -->|"送信元再起動"| lut
    lut --> calibration --> swap --> packet
```

## 7. 現行の同期境界

```mermaid
flowchart LR
    control["制御Pi<br/>フレーム確定"]
    pi1["pi1<br/>完成フレーム待ち"]
    pi4["pi4<br/>完成フレーム待ち"]
    pi2["pi2<br/>完成フレーム待ち"]
    v1["各PiのVSync"]
    v2["各PiのVSync"]
    v3["各PiのVSync"]
    ready["READYバリア<br/>UDP 5100"]
    gpio["GPIO物理同期"]

    control -->|"UDPチャンク"| pi1 --> v1
    control -->|"UDPチャンク"| pi4 --> v2
    control -->|"UDPチャンク"| pi2 --> v3
    ready -.->|"未実装"| control
    gpio -.->|"未実装"| v1
    gpio -.->|"未実装"| v2
    gpio -.->|"未実装"| v3
```

現状は、各表示Piが完成フレームを個別に検証し、それぞれのVSyncで表示する同期段階Aである。
READYバリアとGPIO同期は現行の表示経路に含まれない。
