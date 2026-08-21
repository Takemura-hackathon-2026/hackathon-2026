#!/usr/bin/env python3
"""カメラ非依存の姿勢推定入力段の検証。

モデルが無い環境でも幾何計算部分は検証できるようにする。モデルがある場合は
合成画像で1回だけ推論を回し、経路が通ることまで見る。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HOST = Path(__file__).resolve().parent
sys.path.insert(0, str(HOST))

from pose_input import (  # noqa: E402
    DEFAULT_MODEL_DIR,
    FOOT_POINTS,
    LEFT_HIP,
    LEFT_SHOULDER,
    RIGHT_HIP,
    RIGHT_SHOULDER,
    measure,
    person_row_from_landmarks,
)

WIDTH, HEIGHT = 240, 320


def synthetic(hip_x: float, hip_y: float, torso: float, foot_y: float) -> np.ndarray:
    """立っている人を模したランドマークを作る（画像座標）。"""
    landmarks = np.zeros((39, 5), dtype=np.float64)
    landmarks[:, 3:] = 1.0  # visibility / presence
    landmarks[LEFT_HIP] = [hip_x - 8, hip_y, 0, 1, 1]
    landmarks[RIGHT_HIP] = [hip_x + 8, hip_y, 0, 1, 1]
    landmarks[LEFT_SHOULDER] = [hip_x - 10, hip_y - torso, 0, 1, 1]
    landmarks[RIGHT_SHOULDER] = [hip_x + 10, hip_y - torso, 0, 1, 1]
    for index in FOOT_POINTS:
        landmarks[index] = [hip_x, foot_y, 0, 1, 1]
    return landmarks


def main() -> int:
    errors: list[str] = []

    base = synthetic(hip_x=120, hip_y=180, torso=60, foot_y=290)
    m = measure(base, 0.9, WIDTH, HEIGHT)
    if m is None:
        errors.append("立位の計測に失敗する")
        print("1 errors")
        return 1
    if abs(m.x - 120 / WIDTH) > 1e-9:
        errors.append(f"腰中心xが合わない: {m.x}")
    if abs(m.bottom - 290 / HEIGHT) > 1e-9:
        errors.append(f"足元bottomが合わない: {m.bottom}")
    if abs(m.scale - 60 / HEIGHT) > 1e-9:
        errors.append(f"胴長scaleが合わない: {m.scale}")
    if not m.valid:
        errors.append("validがFalse")

    # 距離不変性: 同じ姿勢で人物が小さく写っても、胴長で割った量は変わらない。
    near = measure(synthetic(120, 180, 60, 290), 0.9, WIDTH, HEIGHT)
    far = measure(synthetic(120, 180, 30, 235), 0.9, WIDTH, HEIGHT)
    if near is None or far is None:
        errors.append("遠近の計測に失敗する")
    else:
        near_ratio = (near.bottom - near.y) / near.scale
        far_ratio = (far.bottom - far.y) / far.scale
        if abs(near_ratio - far_ratio) > 1e-6:
            errors.append(f"胴長で割った量が距離で変わる: {near_ratio:.6f} vs {far_ratio:.6f}")

    # ジャンプ: 足元が上がると bottom が減り、胴長換算の上昇量が正になる。
    jump = measure(synthetic(120, 150, 60, 240), 0.9, WIDTH, HEIGHT)
    if jump is None or jump.bottom >= m.bottom:
        errors.append("ジャンプでbottomが上がらない")
    elif (m.bottom - jump.bottom) / m.scale <= 0:
        errors.append("胴長換算の上昇量が正にならない")

    # 左右: 腰中心xが動く。
    left = measure(synthetic(60, 180, 60, 290), 0.9, WIDTH, HEIGHT)
    if left is None or left.x >= m.x:
        errors.append("左移動でxが減らない")

    # 追跡用の行: 腰中心が入り、全身点が腰より上にある。
    row = person_row_from_landmarks(base)
    if row.shape != (13,):
        errors.append(f"追跡行の形状が不正: {row.shape}")
    elif abs(row[4] - 120) > 1e-9 or abs(row[5] - 180) > 1e-9:
        errors.append("追跡行の腰中心が合わない")
    elif row[7] >= row[5]:
        errors.append("全身点が腰より上にない")

    # 不正入力を弾く。
    for bad, label in (
        (np.zeros((10, 5)), "行数不足"),
        (np.zeros((39, 2)), "列数不足"),
    ):
        try:
            measure(bad, 0.9, WIDTH, HEIGHT)
        except ValueError:
            pass
        else:
            errors.append(f"不正なランドマークを受け入れる: {label}")

    # 胴長ゼロは None を返す（例外にしない）。
    degenerate = synthetic(120, 180, 0, 290)
    if measure(degenerate, 0.9, WIDTH, HEIGHT) is not None:
        errors.append("胴長ゼロでNoneを返さない")

    models = [DEFAULT_MODEL_DIR / name for name in ("person_detection.onnx", "pose_estimation.onnx")]
    if all(path.exists() for path in models):
        from pose_input import PoseTracker  # noqa: PLC0415

        tracker = PoseTracker(threads=2)
        blank = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        result = tracker.update(blank)
        if result is not None:
            errors.append("人物のいない画像で計測値を返す")
        if tracker.detections != 1:
            errors.append(f"人物検出の回数が想定と違う: {tracker.detections}")
    else:
        print(f"SKIP: モデルがない ({DEFAULT_MODEL_DIR})")

    for error in errors:
        print(f"ERROR: {error}")
    print(f"pose-input selftest: {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
