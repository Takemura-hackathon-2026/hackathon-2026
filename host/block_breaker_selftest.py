#!/usr/bin/env python3
"""合成深度フレームを含むブロック崩し・入力分類器検証。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from block_breaker import (  # noqa: E402
    BodyMeasurement,
    BlockBreaker,
    ForegroundGate,
    GameInput,
    InputClassifier,
    PassbyStartDetector,
    SensorController,
    keyboard_action,
)
from palettes import FC6_LIMIT, FC6_WHITE  # noqa: E402


class FakeDepthCapture:
    """SensorControllerへ決定的な深度フレーム列を注入するテスト取得器。"""

    def __init__(self, frames: list[np.ndarray]) -> None:
        self.frames = [np.asarray(frame, dtype=np.uint16) for frame in frames]
        self.index = 0

    def read(self) -> np.ndarray:
        frame = self.frames[min(self.index, len(self.frames) - 1)]
        self.index += 1
        return frame.copy()

    def close(self) -> None:
        pass


def synthetic_depth(kind: str = "background", phase: int = 0) -> np.ndarray:
    """640x480の背景・床・ドア・端ちらつき・人物を作る。"""
    yy, xx = np.indices((480, 640), dtype=np.int32)
    noise = ((xx * 3 + yy * 5 + phase * 7) % 7) - 3
    frame = np.full((480, 640), 5000, dtype=np.int32) + noise
    if kind == "floor":
        frame[405:, :] = 3500
    elif kind == "door":
        frame[90:390, 90:550] = 3500
    elif kind == "side":
        frame[:, :105] = 3500
    elif kind == "small-noise":
        frame[220:235, 300:315] = 3500
    elif kind == "person":
        frame[80:430, 220:420] = 3500
    elif kind != "background":
        raise ValueError(f"unknown synthetic depth kind: {kind}")
    return np.clip(frame, 1, 65535).astype(np.uint16)


def run_sensor_sequence(kind: str, event_frames: int = 4) -> tuple[list[object], SensorController]:
    """背景40枚の後に指定変化を流し、SensorControllerの状態を返す。"""
    frames = [synthetic_depth("background", phase) for phase in range(40)]
    frames.extend(synthetic_depth(kind, 40 + phase) for phase in range(event_frames))
    capture = FakeDepthCapture(frames)
    sensor = SensorController(
        640,
        480,
        background_seconds=2.0,
        min_area=420,
        roi=None,
        jump_rise_y_min=0.05,
        jump_rise_bottom_min=0.04,
        depth_min_change_mm=0.0,
        capture=capture,
    )
    import time

    started = time.monotonic()
    states: list[object] = []
    for index in range(40):
        states.append(sensor.read(started + index * 0.05))
    for index in range(event_frames):
        states.append(sensor.read(started + 2.10 + index * 0.05))
    return states, sensor


def main() -> int:
    errors: list[str] = []
    gate = ForegroundGate(min_area=420)
    shape = (180, 240)
    person_mask = np.zeros(shape, dtype=np.uint8)
    person_mask[28:165, 92:145] = 255
    person_gain = np.full(shape, 900.0, dtype=np.float32)
    for index in range(2):
        body, _, _ = gate.detect(person_mask, person_gain, 400.0)
        if body is not None:
            errors.append("人物候補を3フレーム未満で確定する")
    body, _, _ = gate.detect(person_mask, person_gain, 400.0)
    if body is None:
        errors.append("縦長の人物候補を確定しない")

    arm_gate = ForegroundGate(min_area=420)
    arm_mask = np.zeros(shape, dtype=np.uint8)
    arm_mask[35:165, 100:140] = 255
    arm_mask[55:105, 70:170] = 255
    arm_body = None
    for _ in range(3):
        arm_body, _, _ = arm_gate.detect(arm_mask, person_gain, 400.0)
    if arm_body is None or arm_body.width < 0.30 or arm_body.upper_width < 0.30:
        errors.append("腕輪姿勢の上半身幅を計測しない")

    def rejected(mask_slice: tuple[slice, slice], label: str) -> None:
        test_gate = ForegroundGate(min_area=420)
        mask = np.zeros(shape, dtype=np.uint8)
        mask[mask_slice] = 255
        candidate, _, _ = test_gate.detect(mask, person_gain, 400.0)
        if candidate is not None:
            errors.append(f"{label}を人物として確定する")

    rejected((slice(160, 168), slice(0, 240)), "床の横長反射")
    rejected((slice(20, 165), slice(0, 210)), "広いドア変化")
    rejected((slice(80, 95), slice(110, 125)), "小さなノイズ")
    rejected((slice(30, 150), slice(0, 46)), "センサー左端のちらつき")

    # SensorController全体を通す統合判定テスト。表示用の深度コンテキストではなく、
    # body_present/accepted_maskがゲーム入力と同じ確定結果になることを確認する。
    for label, kind in (
        ("背景ノイズ", "background"),
        ("床反射", "floor"),
        ("後方ドア", "door"),
        ("左右端ちらつき", "side"),
        ("小ノイズ", "small-noise"),
    ):
        sensor = None
        try:
            states, sensor = run_sensor_sequence(kind)
            event_states = states[40:]
            if any(state.body_present for state in event_states):
                errors.append(f"統合判定で{label}を人物として確定する")
            if sensor.accepted_mask is not None and np.any(sensor.accepted_mask):
                errors.append(f"統合判定で{label}の確定マスクを残す")
        except (RuntimeError, ValueError, OSError) as exc:
            errors.append(f"統合判定テスト({label})が実行できない: {exc}")
        finally:
            if sensor is not None:
                sensor.close()

    sensor = None
    try:
        states, sensor = run_sensor_sequence("person")
        event_states = states[40:]
        body_flags = [state.body_present for state in event_states]
        if body_flags[:2] != [False, False] or not body_flags[2]:
            errors.append(f"人物を3フレーム継続後に確定しない: {body_flags}")
        if sensor.accepted_mask is None or not np.any(sensor.accepted_mask):
            errors.append("人物確定時のaccepted_maskを生成しない")
    except (RuntimeError, ValueError, OSError) as exc:
        errors.append(f"統合判定テスト(人物)が実行できない: {exc}")
    finally:
        if sensor is not None:
            sensor.close()

    classifier = InputClassifier(samples=3)
    stance = BodyMeasurement(.5, .5, .9, .2, width=.22, height=.52, upper_width=.22)
    for step in range(3):
        classifier.update(stance, step * .05)
    if not classifier.calibrated:
        errors.append("姿勢校正を完了しない")
    for index in range(4):
        left = classifier.update(
            BodyMeasurement(.20, .5, .9, .2, width=.22, height=.52, upper_width=.22),
            .20 + index * .05,
        )
    if left.lateral != -1:
        errors.append("LEFTを確定しない")
    # 小さめのジャンプ（上昇0.06、下端上昇0.05）も確定できること。
    jump = classifier.update(BodyMeasurement(.5, .44, .85, .2, width=.22, height=.52, upper_width=.22), 1.2)
    if not jump.jump:
        errors.append("JUMPを確定しない")
    if jump.launch:
        errors.append("ジャンプだけでゲームを開始する")
    arm_body = BodyMeasurement(.5, .46, .9, .30, width=.42, height=.58, upper_width=.42)
    arm_events = []
    for index in range(4):
        arm_events.append(classifier.update(arm_body, 1.4 + index * .05).launch)
    if arm_events != [False, False, False, True]:
        errors.append(f"腕で輪を作った姿勢の開始イベントが不正: {arm_events}")
    held = classifier.update(
        arm_body,
        1.45,
    )
    if held.launch:
        errors.append("腕輪スタートを同じ姿勢で連続発火する")
    for index in range(6):
        classifier.update(stance, 1.6 + index * .05)
    rearmed = False
    for index in range(4):
        rearmed = classifier.update(arm_body, 2.0 + index * .05).launch
    if not rearmed:
        errors.append("腕輪スタートを再アーム後に再検知しない")
    classifier.reset()
    if classifier.jump_rise_y_min != .05 or classifier.jump_rise_bottom_min != .04:
        errors.append("JUMP閾値をreset後も維持しない")
    if classifier.start_width_gain != .06 or classifier.start_upper_width_min != .30:
        errors.append("腕輪スタート閾値をreset後も維持しない")
    for step in range(3):
        classifier.update(stance, 2.0 + step * .05)
    one_frame_noise = classifier.update(
        BodyMeasurement(.70, .5, .9, .2, width=.22, height=.52, upper_width=.22),
        2.2,
    )
    if one_frame_noise.lateral != 0:
        errors.append("1フレームの重心揺れを左右移動として確定する")

    passby = PassbyStartDetector(confirm_frames=4, rearm_frames=3)
    passby_events = [passby.update(False) for _ in range(3)]
    passby_events.extend(passby.update(True) for _ in range(4))
    if passby_events != [False, False, False, False, False, False, True]:
        errors.append(f"通過検知を4フレームで確定しない: {passby_events}")
    if passby.update(True):
        errors.append("通過中に開始イベントを連続発火する")
    for _ in range(3):
        passby.update(False)
    rearm_events = [passby.update(True) for _ in range(4)]
    if rearm_events != [False, False, False, True]:
        errors.append("再通過後に開始イベントを再検知しない")
    for key, expected in ((ord("a"), "left"), (83, "right"), (ord(" "), "launch"), (ord("r"), "reset")):
        if keyboard_action(key) != expected:
            errors.append(f"キーボード操作の変換が不正: {key} -> {keyboard_action(key)}")
    game = BlockBreaker()
    if game.boss_width <= 80 or game.boss_height <= 80:
        errors.append("ボス画像を拡大しない")
    if game.boss_y - 26 < game.ball_radius * 2:
        errors.append("拡大後のボス上部にボールの隙間を残さない")
    start_x = game.paddle_x
    game.step(.1, GameInput(lateral=1), .1)
    if game.paddle_x <= start_x:
        errors.append("RIGHTでパドルが動かない")
    game.step(0, GameInput(paddle_center_x=40.0), .15)
    if abs(game.paddle_x - (40.0 - game.paddle_width / 2)) > 1e-6:
        errors.append("人物中心Xへパドル位置を直接同期しない")
    game.step(0, GameInput(launch=True), .2)
    if game.serving or game.ball.vy <= 0:
        errors.append("ボスの口からプレイヤー方向へボールを発射しない")
    initial = BlockBreaker().render("READY")
    if int(initial[12, 164]) == FC6_WHITE:
        errors.append("スタート時に残機の白丸を表示する")
    playing = game.render("READY")
    if int(playing[12, 164]) != FC6_WHITE:
        errors.append("プレイ中に残機の白丸を表示しない")
    game._lose_ball(.3)
    after_loss = game.render("READY")
    if int(after_loss[12, 164]) != FC6_WHITE or int(after_loss[12, 189]) != FC6_WHITE:
        errors.append("ミス後に残機と減った位置の点滅輪郭を表示しない")
    for step in range(40):
        game.step(.04, GameInput(), .4 + step * .04)
    if not game.life_loss_feedback_active:
        errors.append("開始前に残機減少の点滅を終了する")
    game.step(0, GameInput(launch=True), 2.1)
    if game.life_loss_feedback_active:
        errors.append("開始後も残機減少の点滅を表示する")
    game.boss_defeated = True
    cleared = game.render("READY")
    if int(cleared[12, 164]) == FC6_WHITE:
        errors.append("クリア時に残機の白丸を表示する")
    game.boss_defeated = False

    def check_boss_face(label: str, point: np.ndarray, offset: tuple[float, float], velocity: tuple[float, float], expected: str) -> None:
        game.reset(full=True)
        game.serving = False
        game.boss_collision_armed = True
        game.ball.x = game.boss_x + float(point[1]) + offset[0]
        game.ball.y = game.boss_y + float(point[0]) + offset[1]
        game.ball.vx, game.ball.vy = velocity
        hp_before = game.boss_hp
        if not game._hit_boss() or game.boss_hp != hp_before - game.boss_damage:
            errors.append(f"{label}: ボスHPを減らさない")
            return
        reflected = game.ball.vx < 0 if expected == "left" else game.ball.vx > 0 if expected == "right" else game.ball.vy < 0 if expected == "up" else game.ball.vy > 0
        if not reflected:
            errors.append(f"{label}: 衝突面に応じて反射しない")
        effect_before = game.damage_effect_remaining
        if effect_before <= 0.0:
            errors.append(f"{label}: ダメージエフェクトを開始しない")
        game.step(.04, GameInput(), .3)
        if not (0.0 < game.damage_effect_remaining < effect_before):
            errors.append(f"{label}: ダメージエフェクトが時間で減衰しない")

    edge_points = game.boss_edge_points
    check_boss_face("左側面", edge_points[np.argmin(edge_points[:, 1])], (-game.ball_radius + .25, 0), (220, 0), "left")
    check_boss_face("上面", edge_points[np.argmin(edge_points[:, 0])], (0, -game.ball_radius + .25), (0, 220), "up")
    check_boss_face("下面", edge_points[np.argmax(edge_points[:, 0])], (0, game.ball_radius - .25), (0, -220), "down")

    game.reset(full=True)
    game.boss_hp = 60
    point = game.boss_edge_points[np.argmax(game.boss_edge_points[:, 0])]
    game.serving = False
    game.boss_collision_armed = True
    game.ball.x = game.boss_x + float(point[1])
    game.ball.y = game.boss_y + float(point[0]) + game.ball_radius - .25
    game.ball.vx, game.ball.vy = 0, -220
    if not game._hit_boss() or game.boss_hp != 50:
        errors.append("HP半分到達時のボスダメージを処理しない")
    if game.boss_move_active or game.boss_transition_remaining <= 0.0:
        errors.append("HP半分到達時に3回点滅の待機へ入らない")
    game.serving = True
    x_before_move = game.boss_x
    for step in range(20):
        game.step(.04, GameInput(), .5 + step * .04)
    if not game.boss_move_active or game.boss_x == x_before_move:
        errors.append("3回点滅後にボスが左右移動を開始しない")

    game.reset(full=True)
    game.boss_hp = game.boss_damage
    point = game.boss_edge_points[np.argmax(game.boss_edge_points[:, 0])]
    game.serving = False
    game.boss_collision_armed = True
    game.ball.x = game.boss_x + float(point[1])
    game.ball.y = game.boss_y + float(point[0]) + game.ball_radius - .25
    game.ball.vx, game.ball.vy = 0, -220
    game._hit_boss()
    if not game.boss_defeated or game.clear_remaining <= 0.0:
        errors.append("クリア時の自動リセット待機に入らない")
    for step in range(50):
        game.step(.04, GameInput(), 1.5 + step * .04)
    if game.boss_defeated or game.boss_hp != game.boss_max_hp or game.lives != 3 or not game.serving:
        errors.append("クリア後に初期画面へ自動復帰しない")

    for step in range(120):
        frame = game.render("READY")
        if frame.shape != (384, 192) or int(frame.max()) >= FC6_LIMIT:
            errors.append("FC6の192x384フレームを維持しない")
            break
        game.step(1 / 60, GameInput(-1 if step % 60 < 30 else 1), .4 + step / 60)
    for error in errors:
        print(f"ERROR: {error}")
    print(f"{len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
