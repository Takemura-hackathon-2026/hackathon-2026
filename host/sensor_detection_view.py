#!/usr/bin/env python3
"""ゲームと同じSTRUCTURE Sensor検知結果をFC6/UDPで表示する。"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np

HOST = Path(__file__).resolve().parent
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

from block_breaker import ForegroundGate, SensorController, parse_roi  # noqa: E402
from palettes import FC6_BLACK, FC6_LIGHT_GRAY, FC6_WHITE, PaletteMode  # noqa: E402
from test_mode import CANVAS_HEIGHT, CANVAS_WIDTH, PI_COUNT, UdpFrameSender, parse_pi  # noqa: E402


DEFAULT_PI = (
    "192.168.10.101:5000",
    "192.168.10.104:5000",
    "192.168.10.102:5000",
)


def render_detection(sensor: SensorController) -> np.ndarray:
    """ゲームと同じ検知結果を、確認可能な深度ビューへ変換する。

    背景深度はFC6の深度ランプで暗く表示し、床・左右端はゲームと同じく黒にする。
    明灰色は背景差分・床/左右除外まで通った候補、白は形状・深度・継続判定まで
    通ってゲーム入力になった人物領域である。ゲームの入力として使うのは白だけ。
    """
    frame = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), FC6_BLACK, dtype=np.uint8)
    depth = getattr(sensor, "depth_image", None)
    if depth is not None:
        depth = np.asarray(depth)
        valid = depth > 0
        height, width = depth.shape
        floor_start = min(height, max(0, int(round(height * ForegroundGate.FLOOR_CUTOFF_RATIO))))
        side_start = min(width // 2, max(0, int(round(width * ForegroundGate.SIDE_CUTOFF_RATIO))))
        visible = valid.copy()
        visible[floor_start:, :] = False
        visible[:, :side_start] = False
        visible[:, width - side_start:] = False
        depth_view = np.full(depth.shape, FC6_BLACK, dtype=np.uint8)
        if np.any(valid):
            values = depth[valid].astype(np.float32)
            low, high = np.percentile(values, (2.0, 98.0))
            if high <= low:
                high = low + 1.0
            scaled = np.clip((high - depth.astype(np.float32)) * 47.0 / (high - low), 0.0, 47.0)
            depth_view[visible] = scaled[visible].astype(np.uint8)
        frame = cv2.resize(depth_view, (CANVAS_WIDTH, CANVAS_HEIGHT), interpolation=cv2.INTER_NEAREST)
    if sensor.mask is not None:
        candidate = cv2.resize(
            (np.asarray(sensor.mask) > 0).astype(np.uint8),
            (CANVAS_WIDTH, CANVAS_HEIGHT),
            interpolation=cv2.INTER_NEAREST,
        )
        frame[candidate > 0] = FC6_LIGHT_GRAY
    if sensor.accepted_mask is not None:
        accepted = cv2.resize(
            (np.asarray(sensor.accepted_mask) > 0).astype(np.uint8),
            (CANVAS_WIDTH, CANVAS_HEIGHT),
            interpolation=cv2.INTER_NEAREST,
        )
        frame[accepted > 0] = FC6_WHITE
    return frame


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ゲームと同じ人物検知をLEDへ表示")
    parser.add_argument("--sensor-width", type=int, default=640)
    parser.add_argument("--sensor-height", type=int, default=480)
    parser.add_argument("--background-seconds", type=float, default=2.0)
    parser.add_argument("--min-foreground-area", type=int, default=420)
    parser.add_argument(
        "--depth-min-change-mm",
        type=float,
        default=0.0,
        help="深度の手前側変化量。0はゲームと同じく背景ノイズから自動決定",
    )
    parser.add_argument("--roi", type=parse_roi, default=None, help="検出ROI x,y,width,height")
    parser.add_argument("--jump-rise-y-min", type=float, default=0.05)
    parser.add_argument("--jump-rise-bottom-min", type=float, default=0.04)
    parser.add_argument("--fps", type=float, default=60.0)
    parser.add_argument("--frames", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=0.0)
    parser.add_argument("--send", action="store_true", help="3台のPiへ送信")
    parser.add_argument("--pi", action="append", default=[], metavar="HOST[:PORT]")
    parser.add_argument("--chunk-size", type=int, default=1200)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
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
        or args.frames < 0
        or (args.send and len(args.pi) != PI_COUNT)
    ):
        print("error: センサー・閾値・フレーム・Pi指定が不正", file=sys.stderr)
        return 2

    sensor: SensorController | None = None
    sender: UdpFrameSender | None = None
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
            debug_preview=False,
        )
        destinations = args.pi if args.pi else list(DEFAULT_PI)
        if args.send:
            sender = UdpFrameSender([parse_pi(value) for value in destinations], args.chunk_size)
        print(
            "sensor detection view: "
            f"source=structure-depth canvas={CANVAS_WIDTH}x{CANVAS_HEIGHT} "
            f"base=filtered-depth gray=filtered-candidate white=accepted-game-body "
            f"send={'yes' if sender else 'no'}",
            flush=True,
        )
        started = last = time.monotonic()
        frame_id = 0
        frame_count = 0
        candidate_count = 0
        body_count = 0
        next_report = started + 1.0
        period = 1.0 / args.fps

        while running and (args.frames <= 0 or frame_count < args.frames):
            now = time.monotonic()
            if args.seconds > 0 and now - started >= args.seconds:
                break
            state = sensor.read(now)
            frame = render_detection(sensor)
            if frame.shape != (CANVAS_HEIGHT, CANVAS_WIDTH):
                raise RuntimeError(f"送出フレーム形状が不正: {frame.shape}")
            if sender:
                sender.send(frame_id, PaletteMode.FC6, frame)
            frame_id += 1
            frame_count += 1
            if sensor.mask is not None and bool(np.any(sensor.mask)):
                candidate_count += 1
            if state.body_present:
                body_count += 1

            if now >= next_report:
                elapsed = max(now - started, 1e-9)
                print(
                    f"status stage={sensor.stage} frames={frame_count} "
                    f"candidate={candidate_count} body={body_count} "
                    f"fps={frame_count / elapsed:.1f}",
                    flush=True,
                )
                next_report = now + 1.0

            deadline = last + period
            sleep_time = deadline - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            last = deadline if sleep_time > 0 else time.monotonic()

        elapsed = max(time.monotonic() - started, 1e-9)
        print(
            f"summary: frames={frame_count} candidate={candidate_count} "
            f"body={body_count} fps={frame_count / elapsed:.1f}",
            flush=True,
        )
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        if sender is not None:
            sender.close()
        if sensor is not None:
            sensor.close()


if __name__ == "__main__":
    raise SystemExit(main())
