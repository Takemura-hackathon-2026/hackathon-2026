#!/usr/bin/env python3
"""Structure Sensorを処理し、判定済み入力だけを制御ノードへ送信する。"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Iterable

HOST = Path(__file__).resolve().parent
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

from block_breaker import (  # noqa: E402
    DEFAULT_SENSOR_SETTINGS,
    DEFAULT_START_SETTINGS,
    SensorController,
    load_sensor_calibration,
    parse_roi,
)
from input_transport import InputStateSender, SensorControlReceiver, parse_endpoint  # noqa: E402


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="STRUCTURE Sensor判定結果を制御ノードへUDP送信")
    result.add_argument("--destination", required=True, metavar="HOST[:PORT]")
    result.add_argument("--control-bind", default="0.0.0.0:5201", metavar="HOST[:PORT]")
    result.add_argument("--sensor-width", type=int, default=640)
    result.add_argument("--sensor-height", type=int, default=480)
    result.add_argument("--sensor-fps", type=float, default=30.0)
    result.add_argument(
        "--capture-decimate",
        type=int,
        default=1,
        help="深度をN画素おきに間引いて取得（既定1。2で転送量1/4）",
    )
    result.add_argument("--sensor-background-seconds", type=float, default=2.0)
    result.add_argument("--min-foreground-area", type=int, default=420)
    result.add_argument("--depth-min-change-mm", type=float, default=0.0)
    result.add_argument("--roi", type=parse_roi, default=None)
    result.add_argument(
        "--flip-vertical",
        action="store_true",
        help="上下逆に設置したSTRUCTURE Sensorの深度フレームを上下反転する",
    )
    result.add_argument(
        "--flip-horizontal",
        action="store_true",
        help="左右逆に設置したSTRUCTURE Sensorの深度フレームを左右反転する",
    )
    result.add_argument("--start-mode", choices=("still", "passby", "arm-circle"), default="still")
    result.add_argument("--start-still-seconds", type=float, default=3.0)
    result.add_argument("--start-still-tolerance", type=float, default=.035)
    result.add_argument("--passby-confirm-frames", type=int, default=4)
    result.add_argument("--passby-rearm-frames", type=int, default=15)
    result.add_argument("--jump-rise-y-min", type=float, default=None, help="未指定時は校正値、既定0.05")
    result.add_argument("--jump-rise-bottom-min", type=float, default=None, help="未指定時は校正値、既定0.04")
    result.add_argument("--lateral-left-delta-min", type=float, default=None, help="未指定時は校正値、既定0.10")
    result.add_argument("--lateral-right-delta-min", type=float, default=None, help="未指定時は校正値、既定0.10")
    result.add_argument("--lateral-center-deadband", type=float, default=None, help="未指定時は既定0.045")
    result.add_argument("--calibration", type=Path, default=None)
    result.add_argument("--seconds", type=float, default=0.0)
    result.add_argument("--frames", type=int, default=0)
    return result


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if (
        args.sensor_width <= 0
        or args.sensor_height <= 0
        or args.sensor_fps <= 0
        or args.sensor_background_seconds <= 0
        or args.min_foreground_area <= 0
        or args.depth_min_change_mm < 0
        or args.start_still_seconds <= 0
        or not 0.0 <= args.start_still_tolerance < 1.0
        or args.passby_confirm_frames < 2
        or args.passby_rearm_frames < 2
        or args.seconds < 0
        or args.frames < 0
        or not 1 <= args.capture_decimate <= 16
    ):
        print("error: センサー、判定、実行時間の引数が不正", file=sys.stderr)
        return 2

    calibration_path = args.calibration or HOST.parent / "camera_calibration.json"
    learned = load_sensor_calibration(calibration_path)
    jump_rise_y_min = args.jump_rise_y_min if args.jump_rise_y_min is not None else learned.get(
        "jump_rise_y_min", DEFAULT_SENSOR_SETTINGS["jump_rise_y_min"]
    )
    jump_rise_bottom_min = args.jump_rise_bottom_min if args.jump_rise_bottom_min is not None else learned.get(
        "jump_rise_bottom_min", DEFAULT_SENSOR_SETTINGS["jump_rise_bottom_min"]
    )
    lateral_left_delta_min = args.lateral_left_delta_min if args.lateral_left_delta_min is not None else learned.get(
        "lateral_left_delta_min", DEFAULT_SENSOR_SETTINGS["lateral_left_delta_min"]
    )
    lateral_right_delta_min = args.lateral_right_delta_min if args.lateral_right_delta_min is not None else learned.get(
        "lateral_right_delta_min", DEFAULT_SENSOR_SETTINGS["lateral_right_delta_min"]
    )
    lateral_center_deadband = args.lateral_center_deadband if args.lateral_center_deadband is not None else DEFAULT_SENSOR_SETTINGS[
        "lateral_center_deadband"
    ]
    if (
        not all(
            value > 0.0 and value <= 1.0
            for value in (
                jump_rise_y_min,
                jump_rise_bottom_min,
                lateral_left_delta_min,
                lateral_right_delta_min,
                lateral_center_deadband,
            )
        )
    ):
        print("error: 校正済みのジャンプ・左右閾値が不正", file=sys.stderr)
        return 2
    sensor: SensorController | None = None
    sender: InputStateSender | None = None
    control_receiver: SensorControlReceiver | None = None
    try:
        destination = parse_endpoint(args.destination, "127.0.0.1")
        control_bind = parse_endpoint(args.control_bind, "0.0.0.0", default_port=5201)
        sensor = SensorController(
            args.sensor_width,
            args.sensor_height,
            args.sensor_background_seconds,
            args.min_foreground_area,
            args.roi,
            jump_rise_y_min,
            jump_rise_bottom_min,
            args.depth_min_change_mm,
            capture_fps=args.sensor_fps,
            start_mode=args.start_mode,
            passby_confirm_frames=args.passby_confirm_frames,
            passby_rearm_frames=args.passby_rearm_frames,
            still_seconds=args.start_still_seconds,
            still_position_tolerance=args.start_still_tolerance,
            start_center_tolerance=learned.get("center_tolerance", DEFAULT_START_SETTINGS["center_tolerance"]),
            start_width_gain=learned.get("width_gain_min", DEFAULT_START_SETTINGS["width_gain_min"]),
            start_upper_width_gain=learned.get("upper_width_gain_min", DEFAULT_START_SETTINGS["upper_width_gain_min"]),
            start_upper_width_min=learned.get("upper_width_min", DEFAULT_START_SETTINGS["upper_width_min"]),
            start_area_gain=learned.get("area_gain_min", DEFAULT_START_SETTINGS["area_gain_min"]),
            lateral_left_delta_min=lateral_left_delta_min,
            lateral_right_delta_min=lateral_right_delta_min,
            lateral_center_deadband=lateral_center_deadband,
            debug_preview=False,
            capture_decimate=args.capture_decimate,
            flip_vertical=args.flip_vertical,
            flip_horizontal=args.flip_horizontal,
        )
        sender = InputStateSender(destination)
        control_receiver = SensorControlReceiver(control_bind)
    except (OSError, RuntimeError, ValueError) as exc:
        if sender is not None:
            sender.close()
        if sensor is not None:
            sensor.close()
        if control_receiver is not None:
            control_receiver.close()
        print(f"error: {exc}", file=sys.stderr)
        return 2

    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    started = time.monotonic()
    sequence = 0
    last_stage: str | None = None
    last_report = started
    print(
        f"sensor agent: destination={destination[0]}:{destination[1]} "
        f"sensor={args.sensor_width}x{args.sensor_height}@{args.sensor_fps:g} "
        f"start_mode={args.start_mode} flip_vertical={args.flip_vertical} "
        f"flip_horizontal={args.flip_horizontal}",
        flush=True,
    )
    try:
        while running and (args.frames <= 0 or sequence < args.frames):
            now = time.monotonic()
            if args.seconds > 0 and now - started >= args.seconds:
                break
            if control_receiver.poll_reselect():
                sensor.reselect_player()
                print("event=remote-reselect-player", flush=True)
            state = sensor.read(now)
            sender.send(state, sequence)
            if sensor.stage != last_stage:
                print(f"sensor_stage={sensor.stage}", flush=True)
                last_stage = sensor.stage
            if now - last_report >= 5.0:
                print(
                    f"health sent={sender.sent} stage={sensor.stage} "
                    f"body={int(state.body_present)} lateral={state.lateral} "
                    f"player={state.player_id} people={state.people_detected}",
                    flush=True,
                )
                last_report = now
            sequence = (sequence + 1) & 0xFFFFFFFF
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if sender is not None:
            sender.close()
        if sensor is not None:
            sensor.close()
        if control_receiver is not None:
            control_receiver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
