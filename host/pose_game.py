#!/usr/bin/env python3
"""既存のblock_breakerへ姿勢校正値を接続して実行するゲームランチャー。"""
from __future__ import annotations

import argparse
import math
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

from block_breaker import BlockBreaker, GameInput  # noqa: E402
from palettes import FC6, FC6_LIMIT, PaletteMode  # noqa: E402
from pose_runtime import (  # noqa: E402
    DEFAULT_MODEL_DIR,
    PoseCameraController,
    load_pose_calibration,
)
from test_mode import CANVAS_HEIGHT, CANVAS_WIDTH, PI_COUNT, UdpFrameSender, parse_pi  # noqa: E402


def parse_camera(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        path = Path(value)
        if not path.exists():
            raise argparse.ArgumentTypeError(f"カメラデバイスがない: {value}") from None
        return str(path)


def parse_exposure(value: str) -> tuple[float, float, float]:
    try:
        parts = tuple(float(part) for part in value.split("/"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("露出は auto/shutter/gain 形式") from exc
    if len(parts) != 3 or not all(math.isfinite(part) for part in parts):
        raise argparse.ArgumentTypeError("露出は有限な3値 auto/shutter/gain")
    return parts  # type: ignore[return-value]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="姿勢校正値を使う192x384 LEDブロック崩し")
    result.add_argument("--camera", type=parse_camera, default=0)
    result.add_argument("--pose-calibration", type=Path, default=HOST.parent / "pose_calibration.json")
    result.add_argument("--pose-model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    result.add_argument("--pose-threads", type=int, default=4)
    result.add_argument("--pose-redetect-after", type=int, default=5)
    result.add_argument("--exposure", type=parse_exposure, default=None)
    result.add_argument("--fps", type=float, default=60.0)
    result.add_argument("--frames", type=int, default=0)
    result.add_argument("--seconds", type=float, default=0.0)
    result.add_argument("--send", action="store_true")
    result.add_argument("--pi", action="append", default=[], metavar="HOST[:PORT]")
    result.add_argument("--chunk-size", type=int, default=1200)
    result.add_argument("--no-preview", action="store_true")
    result.add_argument("--preview-scale", type=int, default=2)
    result.add_argument("--debug-camera", action="store_true")
    result.add_argument("--boundaries", action="store_true")
    result.add_argument("--position-gain", type=float, default=None)
    result.add_argument("--position-deadzone", type=float, default=None)
    return result


def preview(indexed: np.ndarray) -> np.ndarray:
    return np.asarray([item[:3] for item in FC6], np.uint8)[indexed][:, :, ::-1]


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if (
        args.fps <= 0
        or args.preview_scale <= 0
        or args.pose_threads < 0
        or args.pose_redetect_after <= 0
        or (args.send and len(args.pi) != PI_COUNT)
    ):
        print("error: fps/preview-scale/pose設定/piの指定が不正", file=sys.stderr)
        return 2

    try:
        calibration = load_pose_calibration(args.pose_calibration)
        camera = PoseCameraController(
            args.camera,
            calibration,
            model_dir=args.pose_model_dir,
            threads=args.pose_threads,
            redetect_after=args.pose_redetect_after,
            exposure=args.exposure,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.position_gain is not None:
        if not 0.0 < args.position_gain <= 1.0:
            camera.close()
            print("error: --position-gain は0より大きく1以下", file=sys.stderr)
            return 2
        if hasattr(BlockBreaker, "position_gain"):
            BlockBreaker.position_gain = args.position_gain
    if args.position_deadzone is not None:
        if args.position_deadzone < 0:
            camera.close()
            print("error: --position-deadzone は0以上", file=sys.stderr)
            return 2
        if hasattr(BlockBreaker, "position_deadzone"):
            BlockBreaker.position_deadzone = args.position_deadzone

    sender = None
    try:
        sender = UdpFrameSender([parse_pi(value) for value in args.pi], args.chunk_size) if args.send else None
        game = BlockBreaker()
        running = True
        frame_id = 0
        started = last = deadline = time.monotonic()
        period = 1 / args.fps
        game_input_fields = getattr(GameInput, "__dataclass_fields__", {})

        def stop(_signum: int, _frame: object) -> None:
            nonlocal running
            running = False

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        print(
            f"pose game: canvas={CANVAS_WIDTH}x{CANVAS_HEIGHT} camera={args.camera} "
            f"calibration={calibration.source.name} rotation={calibration.rotation} "
            f"direction=player_relative send={'yes' if sender else 'no'}"
        )
        print(
            f"thresholds(torso): left>={calibration.left_delta_min:.4f} "
            f"right>={calibration.right_delta_min:.4f} "
            f"jump rise_y>={calibration.jump_rise_y_min:.4f} "
            f"rise_bottom>={calibration.jump_rise_bottom_min:.4f}"
        )
        while running and (args.frames <= 0 or frame_id < args.frames):
            now = time.monotonic()
            if args.seconds and now - started >= args.seconds:
                break
            pose = camera.read(now)
            controls_kwargs = {"lateral": pose.lateral, "launch": pose.jump}
            # oyakiのカスタム版は絶対位置追従も持つ。Git版の旧ゲームは相対移動のみ
            # なので、同じランチャーで両方を扱い、存在する場合だけ位置を渡す。
            if "position" in game_input_fields:
                controls_kwargs["position"] = pose.position
            controls = GameInput(**controls_kwargs)
            game.step(min(.05, now - last), controls, now)
            indexed = game.render(camera.stage, args.boundaries)
            if indexed.shape != (CANVAS_HEIGHT, CANVAS_WIDTH) or int(indexed.max()) >= FC6_LIMIT:
                raise RuntimeError("送出フレームがFC6の192x384条件を満たさない")
            if sender is not None:
                sender.send(frame_id, PaletteMode.FC6, indexed)
            if not args.no_preview:
                display = preview(indexed)
                if args.preview_scale != 1:
                    display = cv2.resize(
                        display,
                        (CANVAS_WIDTH * args.preview_scale, CANVAS_HEIGHT * args.preview_scale),
                        interpolation=cv2.INTER_NEAREST,
                    )
                cv2.imshow("RGB LED pose game", display)
                if args.debug_camera:
                    camera.show_debug()
                key = cv2.waitKeyEx(1)
                if key == 27 or (key & 0xFF) == ord("q"):
                    running = False
                elif (key & 0xFF) == ord("r"):
                    game.reset(full=True)
            frame_id += 1
            last = now
            deadline += period
            sleep_time = deadline - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            elif sleep_time < -period:
                deadline = time.monotonic()
        print(f"pose game: frames={frame_id} stage={camera.stage}")
        return 0
    finally:
        if sender is not None:
            sender.close()
        camera.close()
        if not args.no_preview:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    raise SystemExit(main())
