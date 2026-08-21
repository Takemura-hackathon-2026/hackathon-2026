#!/usr/bin/env python3
"""姿勢推定入力段の実測プローブ。

実カメラで fps・検出率・計測値のばらつきを測る。設置位置合わせと、
背景差分方式との比較に使う。LEDへは何も送らない。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import cv2

HOST = Path(__file__).resolve().parent
sys.path.insert(0, str(HOST))

from pose_input import DEFAULT_MODEL_DIR, PoseTracker  # noqa: E402


def parse_camera(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


def parse_exposure(value: str) -> tuple[float, float, float]:
    parts = tuple(float(part) for part in value.split("/"))
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("露出は auto/shutter/gain 形式")
    return parts  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="姿勢推定入力段の実測プローブ")
    parser.add_argument("--camera", type=parse_camera, default=0)
    parser.add_argument("--camera-width", type=int, default=640)
    parser.add_argument("--camera-height", type=int, default=480)
    parser.add_argument("--exposure", type=parse_exposure, default=None)
    parser.add_argument("--rotation", choices=("none", "cw", "ccw", "180"), default="none")
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--show", action="store_true", help="骨格を重ねたプレビューを出す")
    args = parser.parse_args(argv)

    capture = cv2.VideoCapture(args.camera)
    if not capture.isOpened():
        print(f"error: カメラ {args.camera} を開けない", file=sys.stderr)
        return 2
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, args.camera_width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, args.camera_height)
    if args.exposure is not None:
        capture.set(cv2.CAP_PROP_AUTO_EXPOSURE, args.exposure[0])
        capture.set(cv2.CAP_PROP_EXPOSURE, args.exposure[1])
        capture.set(cv2.CAP_PROP_GAIN, args.exposure[2])

    rotations = {
        "cw": cv2.ROTATE_90_CLOCKWISE,
        "ccw": cv2.ROTATE_90_COUNTERCLOCKWISE,
        "180": cv2.ROTATE_180,
    }
    tracker = PoseTracker(model_dir=args.model_dir, threads=args.threads)

    frames = hits = 0
    xs: list[float] = []
    scales: list[float] = []
    bottoms: list[float] = []
    latency: list[float] = []
    start = time.perf_counter()
    while time.perf_counter() - start < args.seconds:
        ok, frame = capture.read()
        if not ok:
            break
        if args.rotation != "none":
            frame = cv2.rotate(frame, rotations[args.rotation])
        t0 = time.perf_counter()
        measurement = tracker.update(frame)
        latency.append((time.perf_counter() - t0) * 1000.0)
        frames += 1
        if measurement is not None:
            hits += 1
            xs.append(measurement.x)
            scales.append(measurement.scale)
            bottoms.append(measurement.bottom)
        if args.show:
            if tracker.landmarks is not None:
                for point in tracker.landmarks[:33, :2]:
                    cv2.circle(frame, (int(point[0]), int(point[1])), 2, (0, 255, 0), -1)
            cv2.imshow("pose probe", frame)
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break
    elapsed = time.perf_counter() - start
    capture.release()
    if args.show:
        cv2.destroyAllWindows()

    print(f"フレーム: {frames}  経過: {elapsed:.1f}s  実fps: {frames / max(elapsed, 1e-9):.1f}")
    print(f"検出率: {hits}/{frames} ({100.0 * hits / max(frames, 1):.1f}%)  人物検出の実行回数: {tracker.detections}")
    if latency:
        print(f"推論時間: 中央値 {statistics.median(latency):.1f}ms  最大 {max(latency):.1f}ms")
    if len(xs) >= 2:
        print(f"x      : 中央値 {statistics.median(xs):.4f}  標準偏差 {statistics.pstdev(xs):.4f}")
        print(f"bottom : 中央値 {statistics.median(bottoms):.4f}  標準偏差 {statistics.pstdev(bottoms):.4f}")
        print(f"scale  : 中央値 {statistics.median(scales):.4f}  標準偏差 {statistics.pstdev(scales):.4f}")
    elif hits == 0:
        print("人物を検出できなかった（カメラの前に立って再実行する）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
