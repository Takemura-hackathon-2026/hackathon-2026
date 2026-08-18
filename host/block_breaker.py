#!/usr/bin/env python3
"""USBカメラで操作する192x384 RGB LEDブロック崩し。

主機でカメラ判定、ゲーム更新、FC6パレット番号での描画を完結する。完成フレームは
既存の UdpFrameSender が192x96ずつ4台のPiへ送るため、Pi側へ人物映像・人物マスク・
ゲームロジックを渡さない。
"""
from __future__ import annotations

import argparse
import math
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np

HOST = Path(__file__).resolve().parent
for directory in (HOST, HOST / "test_mode"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from palettes import FC6, FC6_BLACK, FC6_LIMIT, FC6_WHITE, PaletteMode  # noqa: E402
from test_mode import CANVAS_HEIGHT, CANVAS_WIDTH, PI_COUNT, UdpFrameSender, parse_pi  # noqa: E402


# 正本 host/palettes.py 内のFC6インデックスだけを使う。
SKY, SKY_DOT, PADDLE, PADDLE_EDGE, BALL = 0x1C, 0x20, 0x1E, 0x22, FC6_WHITE
TEXT, DIM = FC6_WHITE, 0x31
BLOCK_COLORS = (0x00, 0x05, 0x0A, 0x0E, 0x12, 0x16, 0x1E, 0x22, 0x29, 0x2D)

# cv2.waitKeyEx() の値はOS/バックエンドで異なる。ASCIIに加え、Linux/X11・
# macOS/Cocoa・Windowsで使われる代表値を受け入れる。
LEFT_KEYS = frozenset((81, 2424832, 65361, 63234))
RIGHT_KEYS = frozenset((83, 2555904, 65363, 63235))
UP_KEYS = frozenset((82, 2490368, 65362, 63232))


@dataclass(frozen=True)
class BodyMeasurement:
    """ROI内で0〜1に正規化した重心・下端・面積。"""

    x: float
    y: float
    bottom: float
    area: float


@dataclass(frozen=True)
class InputState:
    lateral: int = 0  # -1 LEFT, 0 IDLE, 1 RIGHT
    jump: bool = False  # 1フレームのイベント
    body_present: bool = False
    calibrated: bool = False


class InputClassifier:
    """校正済み重心の時系列をLEFT/RIGHT/JUMPへ変換する。"""

    def __init__(self, samples: int = 30) -> None:
        self.required_samples = max(3, samples)
        self.samples: list[BodyMeasurement] = []
        self.baseline: BodyMeasurement | None = None
        self.last: BodyMeasurement | None = None
        self.last_time = 0.0
        self.lateral = 0
        self.candidate = 0
        self.candidate_since = 0.0
        self.jump_latched = False
        self.last_jump = -math.inf

    @property
    def calibrated(self) -> bool:
        return self.baseline is not None

    def reset(self) -> None:
        self.__init__(self.required_samples)

    def update(self, body: BodyMeasurement | None, now: float) -> InputState:
        if body is None:
            self.last = None
            self.lateral = 0
            self.candidate = 0
            return InputState(calibrated=self.calibrated)
        if self.baseline is None:
            self.samples.append(body)
            if len(self.samples) >= self.required_samples:
                data = np.asarray([[v.x, v.y, v.bottom, v.area] for v in self.samples])
                values = np.median(data, axis=0)
                self.baseline = BodyMeasurement(*[float(v) for v in values])
                self.samples.clear()
            self.last, self.last_time = body, now
            return InputState(body_present=True, calibrated=self.calibrated)

        base = self.baseline
        velocity_x = 0.0 if self.last is None else (body.x - self.last.x) / max(.001, now - self.last_time)
        offset = body.x - base.x
        target = -1 if offset <= -.10 else 1 if offset >= .10 else 0 if abs(offset) <= .045 else self.lateral
        if target != self.lateral:
            if target != self.candidate:
                self.candidate, self.candidate_since = target, now
            elif target == 0 or ((velocity_x * target >= .03 or abs(offset) >= .17) and now - self.candidate_since >= .12):
                self.lateral, self.candidate = target, target
        else:
            self.candidate = target

        rise_y, rise_bottom = base.y - body.y, base.bottom - body.bottom
        pose = rise_y >= .075 and rise_bottom >= .06
        jump = pose and not self.jump_latched and now - self.last_jump >= .65
        if jump:
            self.jump_latched, self.last_jump = True, now
        elif rise_y < .034 and rise_bottom < .027:
            self.jump_latched = False
        self.last, self.last_time = body, now
        return InputState(self.lateral, jump, True, True)


class CameraController:
    """MOG2背景差分 → 最大連結成分 → InputClassifier の入力段。"""

    def __init__(
        self,
        device: int,
        width: int,
        height: int,
        background_seconds: float,
        min_area: int,
        roi: tuple[int, int, int, int] | None,
    ) -> None:
        self.capture = cv2.VideoCapture(device)
        if not self.capture.isOpened():
            raise RuntimeError(f"カメラ {device} を開けない")
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.capture.set(cv2.CAP_PROP_FPS, 60)
        self.background_seconds = max(.2, background_seconds)
        self.min_area = max(1, min_area)
        self.roi = roi
        self.started: float | None = None
        self.subtractor = cv2.createBackgroundSubtractorMOG2(history=240, varThreshold=20, detectShadows=False)
        self.classifier = InputClassifier()
        self.debug: np.ndarray | None = None
        self.mask: np.ndarray | None = None

    @property
    def stage(self) -> str:
        if self.started is None or time.monotonic() - self.started < self.background_seconds:
            return "BACKGROUND"
        return "READY" if self.classifier.calibrated else "STANCE"

    def close(self) -> None:
        self.capture.release()

    def read(self, now: float) -> InputState:
        ok, source = self.capture.read()
        if not ok:
            return InputState(calibrated=self.classifier.calibrated)
        if self.started is None:
            self.started = now
        if self.roi is not None:
            x, y, width, height = self.roi
            source_height, source_width = source.shape[:2]
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > source_width or y + height > source_height:
                raise ValueError(
                    f"ROI {x},{y},{width},{height} がカメラ画像 {source_width}x{source_height} を超える"
                )
            source = source[y:y + height, x:x + width]
        width = 240
        height = max(1, round(source.shape[0] * width / source.shape[1]))
        image = cv2.resize(source, (width, height), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        background_phase = now - self.started < self.background_seconds
        foreground = self.subtractor.apply(gray, learningRate=.35 if background_phase else 0.0)
        _, mask = cv2.threshold(foreground, 200, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        body: BodyMeasurement | None = None
        contour = None
        if not background_phase:
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                contour = max(contours, key=cv2.contourArea)
                area = float(cv2.contourArea(contour))
                moments = cv2.moments(contour)
                if area >= self.min_area and moments["m00"]:
                    _, y, _, h = cv2.boundingRect(contour)
                    body = BodyMeasurement(
                        float(moments["m10"] / moments["m00"] / width),
                        float(moments["m01"] / moments["m00"] / height),
                        float((y + h) / height),
                        area / float(width * height),
                    )
        debug = image.copy()
        if contour is not None:
            cv2.drawContours(debug, [contour], -1, (0, 255, 0), 1)
        cv2.putText(debug, self.stage, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 230, 230), 1, cv2.LINE_AA)
        self.debug, self.mask = debug, mask
        return InputState() if background_phase else self.classifier.update(body, now)

    def show_debug(self) -> None:
        if self.debug is not None:
            cv2.imshow("block breaker camera", self.debug)
        if self.mask is not None:
            cv2.imshow("block breaker foreground mask", self.mask)


@dataclass
class Block:
    x: float
    y: float
    width: float
    height: float
    color: int


@dataclass
class Ball:
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0


@dataclass(frozen=True)
class GameInput:
    lateral: int = 0
    launch: bool = False


def keyboard_action(key: int) -> str | None:
    """OpenCVの拡張キーコードをゲーム操作へ正規化する。"""
    ascii_key = key & 0xFF
    if key in LEFT_KEYS or ascii_key in (ord("a"), ord("h")):
        return "left"
    if key in RIGHT_KEYS or ascii_key in (ord("d"), ord("l")):
        return "right"
    if key in UP_KEYS or ascii_key in (ord(" "), ord("w"), ord("k")):
        return "launch"
    if ascii_key == ord("r"):
        return "reset"
    if ascii_key == ord("q") or key == 27:
        return "quit"
    return None


class BlockBreaker:
    paddle_width, paddle_height, paddle_y = 42.0, 6.0, 350.0
    ball_radius, paddle_speed, initial_speed = 3.0, 175.0, 175.0

    def __init__(self) -> None:
        self.level = 1
        self.score = 0
        self.lives = 3
        self.paddle_x = 0.0
        self.ball = Ball(0.0, 0.0)
        self.blocks: list[Block] = []
        self.serving = True
        self.game_over_until = 0.0
        self.reset(full=True)

    def reset(self, full: bool = False) -> None:
        if full:
            self.level, self.score, self.lives = 1, 0, 3
        self.paddle_x = (CANVAS_WIDTH - self.paddle_width) / 2
        self.serving = True
        self.ball = Ball(self.paddle_x + self.paddle_width / 2, self.paddle_y - self.ball_radius - 1)
        self.blocks = self._make_blocks()

    def _make_blocks(self) -> list[Block]:
        columns, rows, margin, gap = 8, min(10, 5 + self.level), 8, 2
        width = (CANVAS_WIDTH - margin * 2 - gap * (columns - 1)) / columns
        return [
            Block(margin + col * (width + gap), 48 + row * 14, width, 12, BLOCK_COLORS[(row + col + self.level - 1) % len(BLOCK_COLORS)])
            for row in range(rows) for col in range(columns)
        ]

    def _launch(self) -> None:
        speed = self.initial_speed + (self.level - 1) * 13
        self.serving = False
        self.ball.vx, self.ball.vy = speed * .52, -speed * .86

    def _hit_block(self, block: Block) -> bool:
        ball = self.ball
        near_x = min(max(ball.x, block.x), block.x + block.width)
        near_y = min(max(ball.y, block.y), block.y + block.height)
        dx, dy = ball.x - near_x, ball.y - near_y
        if dx * dx + dy * dy > self.ball_radius ** 2:
            return False
        if abs(dx) > abs(dy):
            ball.vx = -ball.vx
        else:
            ball.vy = -ball.vy
        return True

    def _lose_ball(self, now: float) -> None:
        self.lives -= 1
        if self.lives <= 0:
            self.game_over_until = now + 1.8
        self.serving = True
        self.ball.vx = self.ball.vy = 0.0
        self.ball.x, self.ball.y = self.paddle_x + self.paddle_width / 2, self.paddle_y - self.ball_radius - 1

    def step(self, dt: float, controls: GameInput, now: float) -> None:
        dt = min(.04, max(0.0, dt))
        if self.game_over_until:
            if now >= self.game_over_until:
                self.game_over_until = 0.0
                self.reset(full=True)
            return
        self.paddle_x = min(max(0.0, self.paddle_x + max(-1, min(1, controls.lateral)) * self.paddle_speed * dt), CANVAS_WIDTH - self.paddle_width)
        if self.serving:
            self.ball.x, self.ball.y = self.paddle_x + self.paddle_width / 2, self.paddle_y - self.ball_radius - 1
            if controls.launch:
                self._launch()
            return
        steps = max(1, min(8, math.ceil(math.hypot(self.ball.vx * dt, self.ball.vy * dt) / 2)))
        for _ in range(steps):
            ball = self.ball
            ball.x += ball.vx * dt / steps
            ball.y += ball.vy * dt / steps
            if ball.x - self.ball_radius < 0 or ball.x + self.ball_radius >= CANVAS_WIDTH:
                ball.x = min(max(self.ball_radius, ball.x), CANVAS_WIDTH - self.ball_radius - 1)
                ball.vx = -ball.vx
            if ball.y - self.ball_radius < 26:
                ball.y, ball.vy = 26 + self.ball_radius, abs(ball.vy)
            if ball.vy > 0 and ball.y + self.ball_radius >= self.paddle_y and ball.y - self.ball_radius <= self.paddle_y + self.paddle_height and self.paddle_x - self.ball_radius <= ball.x <= self.paddle_x + self.paddle_width + self.ball_radius:
                ball.y = self.paddle_y - self.ball_radius - 1
                hit = (ball.x - (self.paddle_x + self.paddle_width / 2)) / (self.paddle_width / 2)
                speed = min(300, math.hypot(ball.vx, ball.vy) * 1.015)
                ball.vx = speed * hit * .92
                ball.vy = -max(80, math.sqrt(max(1, speed * speed - ball.vx * ball.vx)))
            for index, block in enumerate(self.blocks):
                if self._hit_block(block):
                    del self.blocks[index]
                    self.score += 10 * self.level
                    break
            if not self.blocks:
                self.level += 1
                self.blocks = self._make_blocks()
                self.serving = True
                self.ball.vx = self.ball.vy = 0.0
                return
            if ball.y - self.ball_radius > CANVAS_HEIGHT:
                self._lose_ball(now)
                return

    @staticmethod
    def _text(frame: np.ndarray, text: str, origin: tuple[int, int], color: int, scale: float) -> None:
        mask = np.zeros(frame.shape, np.uint8)
        cv2.putText(mask, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, 255, 1, cv2.LINE_AA)
        frame[mask > 96] = color

    def render(self, camera_stage: str, boundaries: bool = False) -> np.ndarray:
        frame = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), SKY, np.uint8)
        for index in range(30):
            frame[30 + (index * 71 + 13) % 300, (index * 47 + 19) % CANVAS_WIDTH] = SKY_DOT
        frame[22:24, :] = DIM
        self._text(frame, f"SCORE {self.score:05d}", (6, 17), TEXT, .38)
        self._text(frame, f"L{self.level} {self.lives}UP", (130, 17), TEXT, .34)
        for block in self.blocks:
            x0, y0 = int(round(block.x)), int(round(block.y))
            x1, y1 = int(round(block.x + block.width)), int(round(block.y + block.height))
            frame[y0:y1, x0:x1] = block.color
            frame[y0:y0 + 2, x0:x1] = TEXT
        x = int(round(self.paddle_x))
        frame[int(self.paddle_y):int(self.paddle_y + self.paddle_height), x:x + int(self.paddle_width)] = PADDLE
        frame[int(self.paddle_y):int(self.paddle_y + 2), x:x + int(self.paddle_width)] = PADDLE_EDGE
        cv2.circle(frame, (int(round(self.ball.x)), int(round(self.ball.y))), int(self.ball_radius), int(BALL), -1, lineType=cv2.LINE_8)
        if self.game_over_until:
            self._text(frame, "GAME OVER", (43, 228), 0x06, .72)
        elif self.serving:
            self._text(frame, "JUMP TO LAUNCH" if camera_stage == "READY" else "CAMERA CAL", (25, 232), 0x0E, .48)
            if camera_stage == "BACKGROUND":
                self._text(frame, "CLEAR CAMERA", (32, 252), TEXT, .40)
            elif camera_stage == "STANCE":
                self._text(frame, "STAND STILL", (37, 252), TEXT, .40)
        if boundaries:
            for y in (96, 192, 288):
                frame[y:y + 1, :] = DIM
        return frame


def parse_roi(value: str) -> tuple[int, int, int, int]:
    try:
        parts = tuple(int(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ROI は x,y,width,height の整数") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ROI は x,y,width,height の4値")
    return parts  # type: ignore[return-value]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="USBカメラまたはキーボードで操作する192x384 LEDブロック崩し")
    result.add_argument("--camera", type=int, default=0)
    result.add_argument("--keyboard", action="store_true", help="カメラを使わず、プレビューをキーボードで操作")
    result.add_argument("--no-camera", action="store_true", help="--keyboard の後方互換エイリアス")
    result.add_argument("--camera-width", type=int, default=640)
    result.add_argument("--camera-height", type=int, default=480)
    result.add_argument("--camera-background-seconds", type=float, default=2.0)
    result.add_argument("--min-foreground-area", type=int, default=420)
    result.add_argument("--roi", type=parse_roi, default=None, help="検出ROI x,y,width,height")
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
    return result


def preview(indexed: np.ndarray) -> np.ndarray:
    return np.asarray([item[:3] for item in FC6], np.uint8)[indexed][:, :, ::-1]


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.fps <= 0 or args.preview_scale <= 0 or (args.send and len(args.pi) != PI_COUNT):
        print("error: --fps/--preview-scale または --pi の指定が不正", file=sys.stderr)
        return 2
    camera: CameraController | None = None
    try:
        keyboard_mode = args.keyboard or args.no_camera
        if not keyboard_mode:
            camera = CameraController(
                args.camera,
                args.camera_width,
                args.camera_height,
                args.camera_background_seconds,
                args.min_foreground_area,
                args.roi,
            )
        sender = UdpFrameSender([parse_pi(value) for value in args.pi], args.chunk_size) if args.send else None
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}（開発用には --keyboard）", file=sys.stderr)
        return 2
    game, running, frame_id = BlockBreaker(), True, 0
    manual_lateral, manual_until, manual_jump = 0, 0.0, False
    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    started = last = deadline = time.monotonic()
    period = 1 / args.fps
    input_label = "keyboard" if keyboard_mode else f"camera={args.camera}"
    print(f"block breaker: canvas={CANVAS_WIDTH}x{CANVAS_HEIGHT} palette=FC6 input={input_label} send={'yes' if sender else 'no'}")
    if not args.no_preview:
        print("keys: A/D or LEFT/RIGHT = paddle, SPACE/W/UP = launch, R = reset, Q/ESC = quit")
    try:
        while running and (args.frames <= 0 or frame_id < args.frames):
            now = time.monotonic()
            if args.seconds and now - started >= args.seconds:
                break
            body = camera.read(now) if camera else InputState(calibrated=True)
            if manual_lateral and now >= manual_until:
                manual_lateral = 0
            lateral = manual_lateral if manual_lateral else (body.lateral if body.calibrated else 0)
            game.step(min(.05, now - last), GameInput(lateral, body.jump or manual_jump), now)
            manual_jump, last = False, now
            indexed = game.render(camera.stage if camera else "READY", args.boundaries)
            if indexed.shape != (CANVAS_HEIGHT, CANVAS_WIDTH) or int(indexed.max()) >= FC6_LIMIT:
                raise RuntimeError("送出フレームがFC6の192x384条件を満たさない")
            if sender:
                sender.send(frame_id, PaletteMode.FC6, indexed)
            if not args.no_preview:
                display = preview(indexed)
                if args.preview_scale != 1:
                    display = cv2.resize(display, (CANVAS_WIDTH * args.preview_scale, CANVAS_HEIGHT * args.preview_scale), interpolation=cv2.INTER_NEAREST)
                cv2.imshow("RGB LED block breaker", display)
                if args.debug_camera and camera:
                    camera.show_debug()
                action = keyboard_action(cv2.waitKeyEx(1))
                if action == "quit":
                    running = False
                elif action == "left":
                    manual_lateral, manual_until = -1, now + .16
                elif action == "right":
                    manual_lateral, manual_until = 1, now + .16
                elif action == "launch":
                    manual_jump = True
                elif action == "reset":
                    game.reset(full=True)
            frame_id += 1
            deadline += period
            if deadline > time.monotonic():
                time.sleep(deadline - time.monotonic())
            elif deadline < time.monotonic() - period:
                deadline = time.monotonic()
    finally:
        if sender:
            sender.close()
        if camera:
            camera.close()
        if not args.no_preview:
            cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
