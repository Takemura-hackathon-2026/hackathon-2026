#!/usr/bin/env python3
"""カメラ非依存の姿勢推定校正の検証。閾値導出と品質ゲートを確かめる。"""
from __future__ import annotations

import sys
from pathlib import Path

HOST = Path(__file__).resolve().parent
sys.path.insert(0, str(HOST))

from pose_calibrate import DIRECTION_CONVENTION, MIN_SAMPLES, STAGES, analyze  # noqa: E402
from pose_input import PoseMeasurement  # noqa: E402


def sample(x: float, y: float, bottom: float, scale: float = 0.20) -> PoseMeasurement:
    return PoseMeasurement(
        x=x, y=y, bottom=bottom, area=0.10, scale=scale, confidence=0.9, visible=0.9
    )


def build(count: int = MIN_SAMPLES + 10) -> tuple[dict, dict]:
    """左右とジャンプが中心から明確に分離した、合格するはずの実測を作る。"""
    samples = {
        # 静止: ごく小さなばらつき。中央値がちょうど0.50になるよう対称にする。
        "CENTER/STANCE": [sample(0.50 + 0.001 * ((i % 3) - 1), 0.40, 0.90) for i in range(count)],
        # 正面カメラでは本人の左が画像右、本人の右が画像左。
        # 胴長0.20に対し 0.06 動く = 胴長0.3個ぶん。
        "LEFT": [sample(0.56, 0.40, 0.90) for _ in range(count)],
        "RIGHT": [sample(0.44, 0.40, 0.90) for _ in range(count)],
        # ジャンプ: 足元が0.06上がる = 胴長0.3個ぶん
        "JUMP": [sample(0.50, 0.34, 0.84) for _ in range(count)],
        "VALIDATE": [sample(0.50, 0.40, 0.90) for _ in range(count)],
    }
    attempts = {stage: count for stage in STAGES}
    return samples, attempts


def main() -> int:
    errors: list[str] = []

    samples, attempts = build()
    result = analyze(samples, attempts)
    quality = result["quality"]
    if not quality["valid"]:
        errors.append(f"分離した実測でvalidにならない: {quality['reasons']}")
    thresholds = result["thresholds"]
    if thresholds.get("units") != "torso_lengths":
        errors.append("閾値の単位が胴長でない")
    if DIRECTION_CONVENTION != "player_relative":
        errors.append(f"左右規約が本人基準でない: {DIRECTION_CONVENTION}")
    for key in ("left", "right", "jump", "center_tolerance"):
        if key not in thresholds:
            errors.append(f"閾値 {key} が出ていない")
    if "left" in thresholds and abs(thresholds["left"]["delta_min"] - 0.30) > 1e-6:
        errors.append(f"LEFT閾値が胴長0.3個ぶんにならない: {thresholds['left']['delta_min']}")
    if "jump" in thresholds and abs(thresholds["jump"]["rise_bottom_min"] - 0.30) > 1e-6:
        errors.append(f"JUMP閾値が胴長0.3個ぶんにならない: {thresholds['jump']['rise_bottom_min']}")

    # 距離不変性: 全サンプルの胴長と位置を半分にしても閾値は変わらない。
    far = {
        stage: [
            sample(0.50 + (m.x - 0.50) / 2, 0.40 + (m.y - 0.40) / 2, 0.90 + (m.bottom - 0.90) / 2, 0.10)
            for m in values
        ]
        for stage, values in samples.items()
    }
    far_result = analyze(far, attempts)
    for key, field in (("left", "delta_min"), ("jump", "rise_bottom_min")):
        near_value = thresholds[key][field]
        far_value = far_result["thresholds"][key][field]
        if abs(near_value - far_value) > 1e-6:
            errors.append(f"距離で閾値が変わる {key}.{field}: {near_value:.6f} vs {far_value:.6f}")

    # 検出率が記録されること。
    rate = result["motion_stats"]["LEFT"]["detection_rate"]
    if abs(rate - 1.0) > 1e-9:
        errors.append(f"検出率が正しくない: {rate}")
    half = analyze(samples, {stage: attempts[stage] * 2 for stage in STAGES})
    if abs(half["motion_stats"]["LEFT"]["detection_rate"] - 0.5) > 1e-9:
        errors.append("検出率が試行回数を反映しない")

    # サンプル不足を検出すること。
    few = {stage: values[:3] for stage, values in samples.items()}
    few_result = analyze(few, {stage: 3 for stage in STAGES})
    if few_result["quality"]["valid"]:
        errors.append("サンプル不足でvalidになる")
    if not any("samples_insufficient" in r for r in few_result["quality"]["reasons"]):
        errors.append("サンプル不足の理由が出ない")

    # 左右が中心と分離しない場合を検出すること（横付けで奥行き方向になった等）。
    flat = dict(samples)
    flat["LEFT"] = [sample(0.5005, 0.40, 0.90) for _ in range(len(samples["LEFT"]))]
    flat_result = analyze(flat, attempts)
    if "left_motion_not_separated_from_center" not in flat_result["quality"]["reasons"]:
        errors.append("左右の非分離を検出しない")

    # 単発の姿勢推定外れ値では落とさず、ノイズが一定割合続く場合は検出すること。
    noisy = dict(samples)
    noisy["CENTER/STANCE"] = list(samples["CENTER/STANCE"]) + [sample(0.50, 0.40, 0.60) for _ in range(6)]
    noisy_result = analyze(noisy, attempts)
    if "center_noise_exceeds_jump_threshold" not in noisy_result["quality"]["reasons"]:
        errors.append("静止時ノイズが閾値を越えることを検出しない")

    # 中心の実測が無い場合。
    empty = analyze({stage: [] for stage in STAGES}, {stage: 0 for stage in STAGES})
    if empty["quality"]["valid"] or empty["baseline"] is not None:
        errors.append("中心の実測が無いのにvalid/baselineを返す")

    for error in errors:
        print(f"ERROR: {error}")
    print(f"pose-calibrate selftest: {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
