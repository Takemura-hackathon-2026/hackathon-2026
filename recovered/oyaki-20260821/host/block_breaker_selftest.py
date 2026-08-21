#!/usr/bin/env python3
"""カメラ非依存のブロック崩し・入力分類器検証。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from block_breaker import (  # noqa: E402
    DEFAULT_CALIBRATION,
    BodyMeasurement,
    BlockBreaker,
    GameInput,
    InputClassifier,
    keyboard_action,
    load_calibration,
)
from palettes import FC6_LIMIT  # noqa: E402


def check_calibrated_classifier(errors: list[str]) -> None:
    """実際の校正JSONを読み、静止時ノイズで誤爆しないことを確認する。"""
    if not DEFAULT_CALIBRATION.exists():
        print(f"SKIP: 校正JSONがない ({DEFAULT_CALIBRATION})")
        return
    calibration = load_calibration(DEFAULT_CALIBRATION)
    base = calibration.baseline

    # 校正が測った静止時ノイズの p10 相当。ここで発火してはいけない。
    classifier = InputClassifier(calibration=calibration)
    if not classifier.calibrated:
        errors.append("校正済みなのにcalibratedがFalse")
    noisy = BodyMeasurement(base.x - 0.1012, base.y - 0.0979, base.bottom - 0.0687, base.area)
    fired_jump = False
    state = None
    for step in range(8):
        state = classifier.update(noisy, step * 0.05)
        fired_jump = fired_jump or state.jump
    if fired_jump:
        errors.append("静止時ノイズでJUMPが発火する")
    if state is None or state.lateral != 0:
        errors.append("静止時ノイズでLEFT/RIGHTが発火する")

    # 実測p25を超える本物の動作は確定すること。
    classifier.reset()
    left = BodyMeasurement(base.x - calibration.left_delta_min * 1.05, base.y, base.bottom, base.area)
    for step in range(8):
        state = classifier.update(left, step * 0.05)
    if state.lateral != -1:
        errors.append("実測p25を超える左移動をLEFTと判定しない")

    classifier.reset()
    jump = BodyMeasurement(
        base.x,
        base.y - calibration.jump_rise_y_min * 1.05,
        base.bottom - calibration.jump_rise_bottom_min * 1.05,
        base.area,
    )
    if not classifier.update(jump, 5.0).jump:
        errors.append("実測p25を超えるジャンプをJUMPと判定しない")

    # 閾値が固定値ではなく校正値から来ていること。
    if classifier.jump_rise_bottom_min != calibration.jump_rise_bottom_min:
        errors.append("JUMP閾値が校正値になっていない")
    if classifier.enter_left != calibration.left_delta_min:
        errors.append("LEFT閾値が校正値になっていない")
    classifier.reset()
    if classifier.enter_left != calibration.left_delta_min:
        errors.append("reset後に校正値を維持しない")


def main() -> int:
    errors: list[str] = []
    classifier = InputClassifier(samples=3)
    for step in range(3):
        classifier.update(BodyMeasurement(.5, .5, .9, .2), step * .05)
    if not classifier.calibrated:
        errors.append("姿勢校正を完了しない")
    left = classifier.update(BodyMeasurement(.22, .5, .9, .2), .20)
    left = classifier.update(BodyMeasurement(.20, .5, .9, .2), .35)
    if left.lateral != -1:
        errors.append("LEFTを確定しない")
    # 小さめのジャンプ（上昇0.06、下端上昇0.05）も確定できること。
    jump = classifier.update(BodyMeasurement(.5, .44, .85, .2), 1.2)
    if not jump.jump:
        errors.append("JUMPを確定しない")
    classifier.reset()
    if classifier.jump_rise_y_min != .05 or classifier.jump_rise_bottom_min != .04:
        errors.append("JUMP閾値をreset後も維持しない")
    for key, expected in ((ord("a"), "left"), (83, "right"), (ord(" "), "launch"), (ord("r"), "reset")):
        if keyboard_action(key) != expected:
            errors.append(f"キーボード操作の変換が不正: {key} -> {keyboard_action(key)}")
    game = BlockBreaker()
    start_x = game.paddle_x
    game.step(.1, GameInput(lateral=1), .1)
    if game.paddle_x <= start_x:
        errors.append("RIGHTでパドルが動かない")
    game.step(0, GameInput(launch=True), .2)
    if game.serving or game.ball.vy >= 0:
        errors.append("launchでボールを発射しない")
    target = game.blocks[0]
    game.ball.x, game.ball.y, game.ball.vx, game.ball.vy = target.x + target.width / 2, target.y + target.height + 5, 0, -220
    before = len(game.blocks)
    game.step(.04, GameInput(), .3)
    if len(game.blocks) != before - 1:
        errors.append("ブロック衝突でブロックを消さない")
    for step in range(120):
        frame = game.render("READY")
        if frame.shape != (384, 192) or int(frame.max()) >= FC6_LIMIT:
            errors.append("FC6の192x384フレームを維持しない")
            break
        game.step(1 / 60, GameInput(-1 if step % 60 < 30 else 1), .4 + step / 60)
    check_calibrated_classifier(errors)
    for error in errors:
        print(f"ERROR: {error}")
    print(f"{len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
