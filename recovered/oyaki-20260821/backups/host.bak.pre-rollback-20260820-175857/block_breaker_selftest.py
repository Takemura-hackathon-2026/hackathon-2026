#!/usr/bin/env python3
"""カメラ非依存のブロック崩し・入力分類器検証。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from block_breaker import (  # noqa: E402
    BodyMeasurement,
    BlockBreaker,
    Calibration,
    GameInput,
    InputClassifier,
    PositionCalibration,
    keyboard_action,
)
from palettes import FC6_LIMIT, FC6_WHITE  # noqa: E402


def main() -> int:
    errors: list[str] = []
    position_calibration = PositionCalibration(.3625, .4494, .5500)
    for x, expected in ((.3625, 0.0), (.4494, .5), (.5500, 1.0)):
        if abs(position_calibration.map(x) - expected) > 1e-6:
            errors.append(f"校正済み横位置を対応付けない: x={x}")
    if position_calibration.map(.3625, mirror=True) != 1.0 or position_calibration.map(.5500, mirror=True) != 0.0:
        errors.append("校正済み横位置を鏡像反転しない")

    calibration_data = {
        "version": "1.0-multi",
        "date": "selftest",
        "camera": {
            "device": 0,
            "requested_width": 320,
            "requested_height": 240,
            "fps": 30.0,
            "rotation": "ccw",
            "exposure": {"requested": {"auto": 1.0, "shutter": 312.0, "gain": 2.0}},
        },
        "ROI": {"after_rotation": None, "processed_size": [240, 320]},
        "zones": {
            "MID": {
                "valid": True,
                "status": "PASS",
                "baseline": {"x": .4494, "y": .2925, "bottom": .7844, "area": .2229},
                "thresholds": {
                    "center_tolerance": {"x": .0081},
                    "jump": {"rise_y_min": .0554, "rise_bottom_min": .2445},
                    "left": {"delta_min": .0869, "x_max": .3625},
                    "right": {"delta_min": .1006, "x_min": .5500},
                },
            }
        },
    }
    loaded = Calibration(calibration_data, Path("selftest-calibration.json"), "MID")
    if loaded.position_calibration is None or abs(loaded.position_calibration.map(.4494) - .5) > 1e-6:
        errors.append("校正JSONのLEFT/CENTER/RIGHTを横位置へ対応付けない")

    classifier = InputClassifier(samples=3)
    for step in range(3):
        classifier.update(BodyMeasurement(.5, .5, .9, .2), step * .05)
    if not classifier.calibrated:
        errors.append("姿勢校正を完了しない")
    left = classifier.update(BodyMeasurement(.22, .5, .9, .2), .20)
    left = classifier.update(BodyMeasurement(.20, .5, .9, .2), .35)
    if left.lateral != -1:
        errors.append("LEFTを確定しない")
    jump = classifier.update(BodyMeasurement(.5, .35, .77, .2), 1.2)
    if not jump.jump:
        errors.append("JUMPを確定しない")
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
        errors.append("JUMP前に残機減少の点滅を終了する")
    game.step(0, GameInput(launch=True), 2.1)
    if game.life_loss_feedback_active:
        errors.append("JUMP後も残機減少の点滅を表示する")
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
