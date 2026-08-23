#!/usr/bin/env python3
"""ジャンプ判定だけを確認するSTRUCTURE Sensor CLI。

ゲーム更新・左右移動・LED送信は行わず、背景差分と
``InputClassifier`` から得た JUMP イベントだけを表示する。
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

import cv2

HOST = Path(__file__).resolve().parent
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

from block_breaker import SensorController  # noqa: E402


def parse_roi(value: str) -> tuple[int, int, int, int]:
    try:
        parts = tuple(int(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ROIはx,y,width,heightの整数") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ROIはx,y,width,heightの4値")
    x, y, width, height = parts
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("ROIの座標・サイズが不正")
    return x, y, width, height


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ゲーム・LEDなしのジャンプ判定確認")
    parser.add_argument("--sensor-width", type=int, default=640)
    parser.add_argument("--sensor-height", type=int, default=480)
    parser.add_argument("--background-seconds", type=float, default=2.0)
    parser.add_argument("--min-foreground-area", type=int, default=420)
    parser.add_argument("--depth-min-change-mm", type=float, default=0.0, help="深度の手前側変化量。0は背景ノイズから自動決定")
    parser.add_argument("--roi", type=parse_roi, default=None, help="検出ROI x,y,width,height")
    parser.add_argument("--jump-rise-y-min", type=float, default=0.05, help="重心上昇の閾値（既定0.05）")
    parser.add_argument("--jump-rise-bottom-min", type=float, default=0.04, help="下端上昇の閾値（既定0.04）")
    parser.add_argument("--fps", type=float, default=30.0, help="読み取り周期（既定30）")
    parser.add_argument("--seconds", type=float, default=30.0, help="計測秒数。0以下は無期限")
    parser.add_argument("--frames", type=int, default=0, help="最大フレーム数。0は無制限")
    parser.add_argument("--preview", action="store_true", help="深度プレビューとマスクを表示")
    parser.add_argument("--preview-scale", type=int, default=2)
    parser.add_argument("--require-jump", action="store_true", help="ジャンプ0件なら終了コード1")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        args.sensor_width <= 0
        or args.sensor_height <= 0
        or args.background_seconds < 0
        or args.min_foreground_area <= 0
        or args.jump_rise_y_min <= 0
        or args.jump_rise_bottom_min <= 0
        or args.depth_min_change_mm < 0
        or args.fps <= 0
        or args.preview_scale <= 0
        or args.frames < 0
    ):
        print("error: センサー・閾値・フレーム指定が不正", file=sys.stderr)
        return 2

    sensor: SensorController | None = None
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        sensor = SensorController(
            args.sensor_width,
            args.sensor_height,
            args.background_seconds,
            args.min_foreground_area,
            args.roi,
            args.jump_rise_y_min,
            args.jump_rise_bottom_min,
            args.depth_min_change_mm,
        )
        print(
            "jump detector: "
            f"source=structure-depth threshold_y={args.jump_rise_y_min:g} "
            f"threshold_bottom={args.jump_rise_bottom_min:g} preview={'yes' if args.preview else 'no'}"
        )
        print("背景学習後にジャンプしてください。終了: Ctrl-C" + (" / Q" if args.preview else ""))
        started = last = time.monotonic()
        frame_count = 0
        body_count = 0
        jump_count = 0
        next_report = started + 1.0
        period = 1.0 / args.fps

        while running and (args.frames <= 0 or frame_count < args.frames):
            now = time.monotonic()
            if args.seconds > 0 and now - started >= args.seconds:
                break
            state = sensor.read(now)
            frame_count += 1
            if state.body_present:
                body_count += 1
            if state.jump:
                jump_count += 1
                print(f"JUMP frame={frame_count} elapsed={now - started:.2f}s")

            if args.preview:
                if sensor.debug is not None:
                    debug = sensor.debug.copy()
                    cv2.putText(debug, f"JUMP {jump_count}", (6, 42), cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 255, 255), 1, cv2.LINE_AA)
                    if args.preview_scale != 1:
                        debug = cv2.resize(debug, None, fx=args.preview_scale, fy=args.preview_scale, interpolation=cv2.INTER_NEAREST)
                    cv2.imshow("jump detector depth", debug)
                if sensor.mask is not None:
                    mask = sensor.mask
                    if args.preview_scale != 1:
                        mask = cv2.resize(mask, None, fx=args.preview_scale, fy=args.preview_scale, interpolation=cv2.INTER_NEAREST)
                    cv2.imshow("jump detector mask", mask)
                key = cv2.waitKeyEx(1)
                if key == 27 or (key & 0xFF) in (ord("q"), ord("Q")):
                    running = False

            if now >= next_report:
                elapsed = max(now - started, 1e-9)
                print(f"status stage={sensor.stage} frames={frame_count} body={body_count} jumps={jump_count} fps={frame_count / elapsed:.1f}")
                next_report = now + 1.0

            deadline = last + period
            sleep_time = deadline - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            last = deadline if sleep_time > 0 else time.monotonic()

        elapsed = max(time.monotonic() - started, 1e-9)
        print(f"summary: frames={frame_count} body={body_count} jumps={jump_count} fps={frame_count / elapsed:.1f}")
        return 1 if args.require_jump and jump_count == 0 else 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if sensor is not None:
            sensor.close()
        if args.preview:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    raise SystemExit(main())
