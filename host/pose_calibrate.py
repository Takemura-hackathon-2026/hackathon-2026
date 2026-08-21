#!/usr/bin/env python3
"""姿勢推定にもとづく校正。LEDへ指示を出しながら実測値を集める。

camera_calibrate.py との違いは2点。

1. 背景モデルを作らないため BACKGROUND ステージがない。廊下・扉の開閉・
   通行人・環境光の変動という設置条件では背景差分が成立しないので、
   そもそも背景を仮定しない。
2. 閾値を胴長（肩中心〜腰中心）で割った単位で出す。立ち位置を固定できない
   ため、フレーム比のままでは同じ動作でも距離によって値が変わる。胴長で
   割れば距離が変わっても同じ値になる。

LED表示・統計・JSON書き出しは camera_calibrate.py の実装を共有する。
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np

HOST = Path(__file__).resolve().parent
for directory in (HOST, HOST / "test_mode"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from camera_calibrate import (  # noqa: E402
    atomic_write_json,
    indexed_to_bgr,
    render_led_frame,
    robust_stats,
    rotate_frame,
)
from pose_input import DEFAULT_MODEL_DIR, PoseMeasurement, PoseTracker  # noqa: E402
from palettes import PaletteMode  # noqa: E402
from test_mode import PI_COUNT, UdpFrameSender, parse_pi  # noqa: E402

VERSION = "1.0"
COORDINATE_SPACE = "pose_landmarks_normalized_by_torso"
# LEDの指示と校正値の左右は、カメラではなく参加者本人から見た左右。
# 正面カメラでは本人の左が画像右、本人の右が画像左へ写る。
DIRECTION_CONVENTION = "player_relative"
STAGES = ("CENTER/STANCE", "LEFT", "RIGHT", "JUMP", "VALIDATE")
DEFAULT_DURATIONS = {
    "CENTER/STANCE": 10.0,
    "LEFT": 8.0,
    "RIGHT": 8.0,
    "JUMP": 10.0,
    "VALIDATE": 6.0,
}
INSTRUCTIONS = {
    "CENTER/STANCE": "STAND STILL AT CENTER",
    "LEFT": "STEP LEFT AND HOLD",
    "RIGHT": "STEP RIGHT AND HOLD",
    "JUMP": "JUMP REPEATEDLY",
    "VALIDATE": "RETURN TO CENTER",
    "PASS": "CALIBRATION OK",
    "FAIL": "CALIBRATION FAIL",
}
MIN_SAMPLES = 20
# 各ステージの開始直後は、前ステージからの移動・着地・姿勢の立て直しが混ざる。
# LEDの指示どおり「保持」した区間だけを校正値へ使うため、先頭25%を捨てる。
SETTLE_FRACTION = 0.25


def parse_camera(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        if not Path(value).exists():
            raise argparse.ArgumentTypeError(f"カメラデバイスがない: {value}") from None
        return value


def parse_exposure(value: str) -> tuple[float, float, float]:
    parts = tuple(float(part) for part in value.split("/"))
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("露出は auto/shutter/gain 形式")
    return parts  # type: ignore[return-value]


def _series(samples: Sequence[PoseMeasurement], field: str) -> list[float]:
    return [float(getattr(sample, field)) for sample in samples]


def analyze(
    samples: dict[str, list[PoseMeasurement]],
    attempts: dict[str, int],
) -> dict[str, object]:
    """ステージ別実測から、胴長で正規化した閾値を導く。"""
    reasons: list[str] = []
    for stage in STAGES:
        if len(samples.get(stage, ())) < MIN_SAMPLES:
            reasons.append(f"{stage.lower().replace('/', '_')}_samples_insufficient")

    stats: dict[str, dict[str, object]] = {}
    for stage in STAGES:
        stage_samples = samples.get(stage, [])
        stats[stage] = {
            field: robust_stats(_series(stage_samples, field))
            for field in ("x", "y", "bottom", "area", "scale", "confidence", "visible")
        }
        got, tried = len(stage_samples), attempts.get(stage, 0)
        stats[stage]["detection_rate"] = round(got / tried, 4) if tried else 0.0

    center = samples.get("CENTER/STANCE", [])
    if not center:
        return {
            "baseline": None,
            "thresholds": None,
            "motion_stats": stats,
            "quality": {"valid": False, "reasons": reasons or ["center_missing"]},
        }

    baseline = {
        field: float(robust_stats(_series(center, field))["median"])
        for field in ("x", "y", "bottom", "area", "scale")
    }
    base_scale = baseline["scale"]
    if base_scale <= 0:
        reasons.append("baseline_scale_invalid")

    def normalized(stage: str, expression) -> list[float]:
        """胴長で割った量。距離が変わっても同じ値になる。"""
        return [expression(sample) / sample.scale for sample in samples.get(stage, []) if sample.scale > 0]

    center_x = normalized("CENTER/STANCE", lambda s: s.x - baseline["x"])
    center_y = normalized("CENTER/STANCE", lambda s: baseline["y"] - s.y)
    center_bottom = normalized("CENTER/STANCE", lambda s: baseline["bottom"] - s.bottom)
    # 正面カメラの鏡像を吸収する。本人の左は画像上ではx増加、本人の右はx減少。
    left = normalized("LEFT", lambda s: s.x - baseline["x"])
    right = normalized("RIGHT", lambda s: baseline["x"] - s.x)
    # ジャンプのステージには着地・静止のフレームも含まれるため、上昇した値だけを
    # 閾値導出へ使う。これで「繰り返しジャンプ」の地上区間がp25を負にしない。
    jump_y = [value for value in normalized("JUMP", lambda s: baseline["y"] - s.y) if value > 0]
    jump_bottom = [
        value for value in normalized("JUMP", lambda s: baseline["bottom"] - s.bottom) if value > 0
    ]

    center_stats = {
        "offset_x": robust_stats(center_x),
        "rise_y": robust_stats(center_y),
        "rise_bottom": robust_stats(center_bottom),
        "offset_x_abs": robust_stats([abs(value) for value in center_x]),
        "rise_bottom_abs": robust_stats([abs(value) for value in center_bottom]),
    }
    left_stats, right_stats = robust_stats(left), robust_stats(right)
    jump_y_stats, jump_bottom_stats = robust_stats(jump_y), robust_stats(jump_bottom)

    thresholds: dict[str, object] = {"units": "torso_lengths"}
    if center_x:
        # 静止時のばらつきの3MAD を許容幅にする。量子化下限は設けない
        # （胴長正規化後は画素量子化が直接効かないため）。
        thresholds["center_tolerance"] = {
            "x": 3.0 * float(center_stats["offset_x"]["mad"]),
            "y": 3.0 * float(center_stats["rise_y"]["mad"]),
            "bottom": 3.0 * float(center_stats["rise_bottom"]["mad"]),
        }
    for name, values, stat in (("left", left, left_stats), ("right", right, right_stats)):
        if values:
            delta_min = float(stat["p25"])
            thresholds[name] = {"delta_min": delta_min, "source": f"{name.upper()} measured p25 offset"}
            if delta_min <= 0:
                reasons.append(f"{name}_motion_direction_invalid")
            tolerance = thresholds.get("center_tolerance", {}).get("x")  # type: ignore[union-attr]
            if tolerance is not None and float(stat["median"]) <= tolerance:
                reasons.append(f"{name}_motion_not_separated_from_center")
    if len(jump_y) < MIN_SAMPLES or len(jump_bottom) < MIN_SAMPLES:
        reasons.append("jump_rise_samples_insufficient")
    elif jump_y and jump_bottom:
        thresholds["jump"] = {
            "rise_y_min": float(jump_y_stats["p25"]),
            "rise_bottom_min": float(jump_bottom_stats["p25"]),
            "source": "JUMP measured p25 rise",
        }
        tolerance = thresholds.get("center_tolerance", {})  # type: ignore[assignment]
        if isinstance(tolerance, dict) and tolerance.get("bottom") is not None:
            if float(jump_bottom_stats["median"]) <= float(tolerance["bottom"]):
                reasons.append("jump_motion_not_separated_from_center")
    # 静止時ノイズの95パーセンタイルが本番の閾値を越えるなら、その閾値では
    # 誤爆する。最大値は単発の姿勢推定外れ値で校正全体を落とすため使わない。
    if center_x and "left" in thresholds:
        if float(center_stats["offset_x_abs"]["p95"]) >= float(thresholds["left"]["delta_min"]):  # type: ignore[index]
            reasons.append("center_noise_exceeds_left_threshold")
    if center_bottom and "jump" in thresholds:
        if float(center_stats["rise_bottom_abs"]["p95"]) >= float(thresholds["jump"]["rise_bottom_min"]):  # type: ignore[index]
            reasons.append("center_noise_exceeds_jump_threshold")

    stats["CENTER/STANCE"].update(center_stats)
    stats["LEFT"]["offset_x"] = left_stats
    stats["RIGHT"]["offset_x"] = right_stats
    stats["JUMP"]["rise_y"] = jump_y_stats
    stats["JUMP"]["rise_bottom"] = jump_bottom_stats

    return {
        "baseline": baseline,
        "thresholds": thresholds,
        "motion_stats": stats,
        "quality": {
            "valid": not reasons,
            "reasons": reasons,
            "minimum_samples_per_stage": MIN_SAMPLES,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="姿勢推定にもとづく校正（LEDへ指示を表示）")
    parser.add_argument("--camera", type=parse_camera, default=0)
    parser.add_argument("--camera-width", type=int, default=640)
    parser.add_argument("--camera-height", type=int, default=480)
    parser.add_argument("--exposure", type=parse_exposure, default=None)
    parser.add_argument("--rotation", choices=("none", "cw", "ccw", "180"), default="none")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--output", type=Path, default=HOST.parent / "pose_calibration.json")
    parser.add_argument("--send", action="store_true", help="LED4台へ指示を送る")
    parser.add_argument("--pi", action="append", default=[], metavar="HOST[:PORT]")
    parser.add_argument("--chunk-size", type=int, default=1200)
    parser.add_argument("--preview", action="store_true", help="手元にプレビューを出す")
    parser.add_argument("--countdown", type=float, default=6.0, help="開始前の準備時間[秒]")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.send and len(args.pi) != PI_COUNT:
        print(f"error: --send のときは --pi を{PI_COUNT}個指定する", file=sys.stderr)
        return 2

    capture = cv2.VideoCapture(args.camera)
    if not capture.isOpened():
        print(f"error: カメラ {args.camera} を開けない", file=sys.stderr)
        return 2
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, args.camera_width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, args.camera_height)
    # release() 後の capture.get() は環境によって -1 を返すため、校正中に
    # 実際に使った解像度を先に記録しておく。-1 をJSONへ保存すると本番側が
    # カメラ条件を検証できず、校正値を安全に接続できない。
    capture_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    capture_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if capture_width <= 0:
        capture_width = args.camera_width
    if capture_height <= 0:
        capture_height = args.camera_height
    if args.exposure is not None:
        capture.set(cv2.CAP_PROP_AUTO_EXPOSURE, args.exposure[0])
        capture.set(cv2.CAP_PROP_EXPOSURE, args.exposure[1])
        capture.set(cv2.CAP_PROP_GAIN, args.exposure[2])

    try:
        tracker = PoseTracker(model_dir=args.model_dir, threads=args.threads)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        capture.release()
        return 2
    sender = UdpFrameSender([parse_pi(value) for value in args.pi], args.chunk_size) if args.send else None

    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    samples: dict[str, list[PoseMeasurement]] = {stage: [] for stage in STAGES}
    attempts: dict[str, int] = {stage: 0 for stage in STAGES}
    frame_id = 0

    def show(stage: str, instruction: str, progress: float, valid: bool, remaining: float | None) -> None:
        nonlocal frame_id
        indexed = render_led_frame(stage, instruction, progress, valid, None, frame_id, remaining)
        if sender is not None:
            sender.send(frame_id, PaletteMode.FC6, indexed)
        if args.preview:
            cv2.imshow("pose calibrate", indexed_to_bgr(indexed))
            cv2.waitKey(1)
        frame_id += 1

    print(f"pose calibration: rotation={args.rotation} send={'yes' if sender else 'no'}")
    print("順に指示が出る。LEDの表示に従って動く。")

    schedule: list[tuple[str, float]] = [("CENTER/STANCE", args.countdown)]
    try:
        # 準備時間。ここでは記録しない。
        start = time.monotonic()
        while running and time.monotonic() - start < args.countdown:
            ok, frame = capture.read()
            if not ok:
                break
            remaining = args.countdown - (time.monotonic() - start)
            show("CENTER/STANCE", f"GET READY {remaining:.0f}", 0.0, False, remaining)

        for stage in STAGES:
            if not running:
                break
            duration = DEFAULT_DURATIONS[stage]
            start = time.monotonic()
            while running:
                elapsed = time.monotonic() - start
                if elapsed >= duration:
                    break
                ok, frame = capture.read()
                if not ok:
                    break
                if args.rotation != "none":
                    frame = rotate_frame(frame, args.rotation)
                measurement = tracker.update(frame)
                # ステージ遷移中のフレームは追跡器のウォームアップだけ行い、
                # 実測値・検出率には含めない。
                if elapsed >= duration * SETTLE_FRACTION:
                    attempts[stage] += 1
                    if measurement is not None and measurement.valid:
                        samples[stage].append(measurement)
                show(
                    stage,
                    INSTRUCTIONS[stage],
                    elapsed / duration,
                    measurement is not None,
                    duration - elapsed,
                )
            got, tried = len(samples[stage]), attempts[stage]
            rate = 100.0 * got / max(tried, 1)
            print(f"  {stage:14} {got:4d}/{tried:4d} 検出 ({rate:5.1f}%)")
    finally:
        capture.release()
        if args.preview:
            cv2.destroyAllWindows()

    analysis = analyze(samples, attempts)
    quality = analysis["quality"]
    status = "PASS" if quality["valid"] else "FAIL"  # type: ignore[index]
    payload = {
        "version": VERSION,
        "status": status,
        "valid": quality["valid"],  # type: ignore[index]
        "detector": "mediapipe_blazepose",
        "coordinate_space": COORDINATE_SPACE,
        "direction_convention": DIRECTION_CONVENTION,
        "camera": {
            "device": args.camera if isinstance(args.camera, int) else str(args.camera),
            "rotation": args.rotation,
            "width": capture_width,
            "height": capture_height,
            "exposure": None if args.exposure is None else list(args.exposure),
        },
        "person_detections": tracker.detections,
        **analysis,
    }
    json.dumps(payload, ensure_ascii=False, allow_nan=False)
    atomic_write_json(args.output, payload)

    if sender is not None:
        show(status, INSTRUCTIONS[status], 1.0, status == "PASS", None)
        sender.close()

    print(f"\nstatus: {status}  -> {args.output}")
    for reason in quality["reasons"]:  # type: ignore[index]
        print(f"  reason: {reason}")
    thresholds = analysis.get("thresholds")
    if isinstance(thresholds, dict):
        print(f"  閾値（単位: 胴長）: {json.dumps(thresholds, ensure_ascii=False)}")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
