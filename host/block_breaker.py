#!/usr/bin/env python3
"""USBカメラで操作する192x384 RGB LEDブロック崩し。

主機でカメラ判定、ゲーム更新、FC6パレット番号での描画を完結する。完成フレームは
既存の UdpFrameSender が192x96ずつ4台のPiへ送るため、Pi側へ人物映像・人物マスク・
ゲームロジックを渡さない。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
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

# cv2.waitKeyEx() の値はOS/バックエンドで異なる。ASCIIに加え、Linux/X11・
# macOS/Cocoa・Windowsで使われる代表値を受け入れる。
LEFT_KEYS = frozenset((81, 2424832, 65361, 63234))
RIGHT_KEYS = frozenset((83, 2555904, 65363, 63235))
UP_KEYS = frozenset((82, 2490368, 65362, 63232))
# OpenCVのwaitKeyExにはキー解放イベントがないため、キーリピート間を埋める最小限の保持時間を持たせる。
# これを長くすると、キーを離した後も慣性のように動いて見える。
# X11のキー状態取得が使えない環境でのフォールバック用。
KEYBOARD_EVENT_HOLD_SECONDS = 0.08
BOSS_IMAGE = HOST / "assets" / "takemuraface_fc6.png"


def load_boss_sprite(path: Path = BOSS_IMAGE) -> tuple[np.ndarray, np.ndarray]:
    """透過PNGをFC6インデックス画像と不透明マスクへ変換する。"""
    source = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if source is None:
        raise RuntimeError(f"ボス画像を読み込めない: {path}")
    if source.ndim != 3 or source.shape[2] != 4:
        raise RuntimeError(f"ボス画像はRGBA PNGでなければならない: {path}")
    rgb = source[:, :, :3][:, :, ::-1].astype(np.int32)
    palette = np.asarray([color[:3] for color in FC6], dtype=np.int32)
    distance = np.sum((rgb[:, :, None, :] - palette[None, None, :, :]) ** 2, axis=3)
    indexed = np.argmin(distance, axis=2).astype(np.uint8)
    return indexed, source[:, :, 3] > 0


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


class X11KeyboardState:
    """X11のキー状態を毎フレーム読む。キーリピート待ちと解放遅延をなくす。"""

    def __init__(self) -> None:
        self._lib = None
        self._display = None
        self._left_codes: set[int] = set()
        self._right_codes: set[int] = set()
        library = ctypes.util.find_library("X11")
        if not library:
            return
        try:
            lib = ctypes.CDLL(library)
            lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            lib.XOpenDisplay.restype = ctypes.c_void_p
            lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
            lib.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            lib.XKeysymToKeycode.restype = ctypes.c_ubyte
            display = lib.XOpenDisplay(None)
            if not display:
                return
            self._lib, self._display = lib, display
            self._left_codes = self._keycodes(0xFF51, ord("a"), ord("A"), ord("h"), ord("H"))
            self._right_codes = self._keycodes(0xFF53, ord("d"), ord("D"), ord("l"), ord("L"))
        except (AttributeError, OSError):
            self.close()

    @property
    def available(self) -> bool:
        return self._lib is not None and self._display is not None

    def _keycodes(self, *keysyms: int) -> set[int]:
        assert self._lib is not None and self._display is not None
        return {
            int(code)
            for code in (self._lib.XKeysymToKeycode(self._display, ctypes.c_ulong(keysym)) for keysym in keysyms)
            if code
        }

    def lateral(self) -> int:
        if not self.available:
            return 0
        keymap = (ctypes.c_ubyte * 32)()
        self._lib.XQueryKeymap(self._display, ctypes.cast(keymap, ctypes.c_void_p))

        def pressed(codes: set[int]) -> bool:
            return any(bool(keymap[code // 8] & (1 << (code % 8))) for code in codes)

        left, right = pressed(self._left_codes), pressed(self._right_codes)
        return -1 if left and not right else 1 if right and not left else 0

    def close(self) -> None:
        if self._lib is not None and self._display is not None:
            self._lib.XCloseDisplay(self._display)
        self._lib = self._display = None


class BlockBreaker:
    paddle_width, paddle_height, paddle_y = 42.0, 6.0, 350.0
    ball_radius, paddle_speed, initial_speed = 3.0, 175.0, 175.0
    boss_scale = 1.20
    boss_y = 34.0  # 上端のプレイ領域(y=26)との間に8pxを残す。
    boss_max_hp, boss_damage = 100, 10
    damage_effect_duration = .24
    boss_transition_duration = .48  # 3回の点滅（点灯・消灯を3周期）
    boss_move_speed = 52.0

    def __init__(self) -> None:
        sprite, mask = load_boss_sprite()
        size = (round(sprite.shape[1] * self.boss_scale), round(sprite.shape[0] * self.boss_scale))
        self.boss_sprite = cv2.resize(sprite, size, interpolation=cv2.INTER_NEAREST)
        self.boss_mask = cv2.resize(mask.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST) > 0
        self.boss_height, self.boss_width = self.boss_sprite.shape
        self.boss_x = (CANVAS_WIDTH - self.boss_width) / 2
        eroded = cv2.erode(self.boss_mask.astype(np.uint8), np.ones((3, 3), np.uint8))
        self.boss_edge = self.boss_mask & (eroded == 0)
        self.boss_edge_points = np.argwhere(self.boss_edge)
        self.lives = 3
        self.boss_hp = self.boss_max_hp
        self.boss_defeated = False
        self.paddle_x = 0.0
        self.ball = Ball(0.0, 0.0)
        self.serving = True
        self.game_started = False
        self.boss_collision_armed = False
        self.damage_effect_remaining = 0.0
        self.life_loss_feedback_active = False
        self.life_loss_blink_elapsed = 0.0
        self.life_loss_slot = -1
        self.boss_transition_remaining = 0.0
        self.boss_move_active = False
        self.boss_move_vx = self.boss_move_speed
        self.game_over_until = 0.0
        self.reset(full=True)

    def reset(self, full: bool = False) -> None:
        if full:
            self.lives = 3
            self.boss_hp = self.boss_max_hp
            self.boss_defeated = False
            self.game_started = False
            self.damage_effect_remaining = 0.0
            self.life_loss_feedback_active = False
            self.life_loss_blink_elapsed = 0.0
            self.life_loss_slot = -1
            self.boss_x = (CANVAS_WIDTH - self.boss_width) / 2
            self.boss_transition_remaining = 0.0
            self.boss_move_active = False
            self.boss_move_vx = self.boss_move_speed
            self.game_over_until = 0.0
        self.paddle_x = (CANVAS_WIDTH - self.paddle_width) / 2
        self.serving = True
        self.boss_collision_armed = False
        self._place_ball_at_mouth()

    def _place_ball_at_mouth(self) -> None:
        self.ball = Ball(
            self.boss_x + self.boss_width * .53,
            self.boss_y + self.boss_height * .70,
        )

    def _launch(self) -> None:
        self.serving = False
        self.game_started = True
        self.life_loss_feedback_active = False
        self.life_loss_blink_elapsed = 0.0
        self.life_loss_slot = -1
        # ボスの口元からプレイヤー側へ飛び出す角度。
        self.ball.vx, self.ball.vy = self.initial_speed * .80, self.initial_speed * .60

    def _ball_overlaps_boss(self) -> bool:
        left = max(0, int(math.floor(self.ball.x - self.ball_radius - self.boss_x)))
        right = min(self.boss_width, int(math.ceil(self.ball.x + self.ball_radius - self.boss_x + 1)))
        top = max(0, int(math.floor(self.ball.y - self.ball_radius - self.boss_y)))
        bottom = min(self.boss_height, int(math.ceil(self.ball.y + self.ball_radius - self.boss_y + 1)))
        if left >= right or top >= bottom:
            return False
        yy, xx = np.ogrid[top:bottom, left:right]
        circle = (
            (xx + self.boss_x - self.ball.x) ** 2
            + (yy + self.boss_y - self.ball.y) ** 2
            <= self.ball_radius ** 2
        )
        return bool(np.any(self.boss_mask[top:bottom, left:right] & circle))

    def _boss_contact_normal(self) -> tuple[float, float, float, float]:
        """ボール中心に最も近い不透明輪郭点と、そこから外向きの法線を返す。"""
        if len(self.boss_edge_points) == 0:
            return 0.0, -1.0, self.ball.x, self.ball.y
        edge_y = self.boss_edge_points[:, 0].astype(np.float64) + self.boss_y
        edge_x = self.boss_edge_points[:, 1].astype(np.float64) + self.boss_x
        distance = (edge_x - self.ball.x) ** 2 + (edge_y - self.ball.y) ** 2
        nearest = int(np.argmin(distance))
        contact_x, contact_y = float(edge_x[nearest]), float(edge_y[nearest])
        local_x = int(round(self.ball.x - self.boss_x))
        local_y = int(round(self.ball.y - self.boss_y))
        center_inside = (
            0 <= local_x < self.boss_width
            and 0 <= local_y < self.boss_height
            and bool(self.boss_mask[local_y, local_x])
        )
        if center_inside:
            nx, ny = contact_x - self.ball.x, contact_y - self.ball.y
        else:
            nx, ny = self.ball.x - contact_x, self.ball.y - contact_y
        length = math.hypot(nx, ny)
        if length < 1e-6:
            speed = math.hypot(self.ball.vx, self.ball.vy)
            if speed < 1e-6:
                return 0.0, -1.0, contact_x, contact_y
            nx, ny, length = -self.ball.vx, -self.ball.vy, speed
        return nx / length, ny / length, contact_x, contact_y

    def _hit_boss(self) -> bool:
        overlaps = self._ball_overlaps_boss()
        if not self.boss_collision_armed:
            if not overlaps:
                self.boss_collision_armed = True
            return False
        if not overlaps or self.boss_defeated:
            return False
        nx, ny, contact_x, contact_y = self._boss_contact_normal()
        incoming = self.ball.vx * nx + self.ball.vy * ny
        # 法線方向へ離れている接触は、前フレームの押し出しが残っただけなので無視する。
        if incoming >= 0.0:
            return False
        self.ball.vx -= 2.0 * incoming * nx
        self.ball.vy -= 2.0 * incoming * ny
        # 輪郭からボール半径+1pxだけ外へ押し出し、同じ衝突を連続計上しない。
        self.ball.x = contact_x + nx * (self.ball_radius + 1.0)
        self.ball.y = contact_y + ny * (self.ball_radius + 1.0)
        self.boss_hp = max(0, self.boss_hp - self.boss_damage)
        self.damage_effect_remaining = self.damage_effect_duration
        if self.boss_hp <= self.boss_max_hp // 2 and not self.boss_move_active and self.boss_transition_remaining <= 0.0:
            self.boss_transition_remaining = self.boss_transition_duration
        if self.boss_hp == 0:
            self.boss_defeated = True
            self.serving = True
            self.ball.vx = self.ball.vy = 0.0
        return True

    def _lose_ball(self, now: float) -> None:
        self.lives -= 1
        if self.lives <= 0:
            self.game_over_until = now + 1.8
            self.life_loss_feedback_active = False
            self.life_loss_blink_elapsed = 0.0
            self.life_loss_slot = -1
        else:
            self.life_loss_feedback_active = True
            self.life_loss_blink_elapsed = 0.0
            self.life_loss_slot = self.lives
        self.serving = True
        self.boss_collision_armed = False
        self.ball.vx = self.ball.vy = 0.0
        self._place_ball_at_mouth()

    def step(self, dt: float, controls: GameInput, now: float) -> None:
        dt = min(.04, max(0.0, dt))
        self.damage_effect_remaining = max(0.0, self.damage_effect_remaining - dt)
        if self.life_loss_feedback_active:
            self.life_loss_blink_elapsed += dt
        if self.boss_transition_remaining > 0.0:
            self.boss_transition_remaining = max(0.0, self.boss_transition_remaining - dt)
            if self.boss_transition_remaining == 0.0:
                self.boss_move_active = True
        if self.boss_move_active and not self.boss_defeated:
            self.boss_x += self.boss_move_vx * dt
            if self.boss_x <= 0.0:
                self.boss_x, self.boss_move_vx = 0.0, abs(self.boss_move_vx)
            elif self.boss_x + self.boss_width >= CANVAS_WIDTH:
                self.boss_x = CANVAS_WIDTH - self.boss_width
                self.boss_move_vx = -abs(self.boss_move_vx)
        if self.game_over_until:
            if now >= self.game_over_until:
                self.game_over_until = 0.0
                self.reset(full=True)
            return
        if self.boss_defeated:
            return
        self.paddle_x = min(max(0.0, self.paddle_x + max(-1, min(1, controls.lateral)) * self.paddle_speed * dt), CANVAS_WIDTH - self.paddle_width)
        if self.serving:
            self._place_ball_at_mouth()
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
            if self._hit_boss():
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
        # 左上はボスHP、右上は残機。HPは現在値に応じて動的に縮む。
        hp_x, hp_y, hp_width, hp_height = 7, 7, 143, 10
        frame[hp_y:hp_y + hp_height, hp_x:hp_x + hp_width] = TEXT
        frame[hp_y + 2:hp_y + hp_height - 2, hp_x + 2:hp_x + hp_width - 2] = FC6_BLACK
        fill = round((hp_width - 4) * self.boss_hp / self.boss_max_hp)
        if fill:
            hp_color = 0x16 if self.boss_hp > 50 else 0x0E if self.boss_hp > 20 else 0x05
            frame[hp_y + 2:hp_y + hp_height - 2, hp_x + 2:hp_x + 2 + fill] = hp_color
        active_play = not self.serving and not self.boss_defeated and not self.game_over_until
        show_lives = self.game_started and not self.boss_defeated and not self.game_over_until
        if show_lives:
            for life in range(self.lives):
                cv2.circle(frame, (164 + life * 11, 12), 3, int(TEXT), -1, lineType=cv2.LINE_8)
            if self.life_loss_feedback_active and 0 <= self.life_loss_slot < 3:
                phase = int(self.life_loss_blink_elapsed / .12)
                if phase % 2 == 0:
                    cv2.circle(frame, (164 + self.life_loss_slot * 11, 12), 3, int(TEXT), 1, lineType=cv2.LINE_8)
        frame[22:24, :] = DIM

        boss_x, boss_y = int(self.boss_x), int(self.boss_y)
        boss_region = frame[boss_y:boss_y + self.boss_height, boss_x:boss_x + self.boss_width]
        boss_region[self.boss_mask] = self.boss_sprite[self.boss_mask]
        if self.boss_transition_remaining > 0.0:
            # 点灯→消灯を3周期。点滅中はボスの位置を固定する。
            phase = int((self.boss_transition_duration - self.boss_transition_remaining) / (self.boss_transition_duration / 6))
            if phase in (0, 2, 4):
                boss_region[self.boss_mask] = FC6_WHITE
        elif self.damage_effect_remaining > 0.0:
            # 点灯→消灯→点灯→消灯の4区間で、ボスを2回だけ点滅させる。
            phase = int((self.damage_effect_duration - self.damage_effect_remaining) / (self.damage_effect_duration / 4))
            if phase in (0, 2):
                boss_region[self.boss_mask] = FC6_WHITE
        x = int(round(self.paddle_x))
        frame[int(self.paddle_y):int(self.paddle_y + self.paddle_height), x:x + int(self.paddle_width)] = PADDLE
        frame[int(self.paddle_y):int(self.paddle_y + 2), x:x + int(self.paddle_width)] = PADDLE_EDGE
        if active_play:
            cv2.circle(frame, (int(round(self.ball.x)), int(round(self.ball.y))), int(self.ball_radius), int(BALL), -1, lineType=cv2.LINE_8)
        if self.boss_defeated:
            self._text(frame, "BOSS DOWN", (39, 228), 0x12, .68)
            self._text(frame, "R TO RETRY", (47, 249), TEXT, .40)
        elif self.game_over_until:
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
    keyboard_state = X11KeyboardState() if not args.no_preview else None
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
            x11_lateral = keyboard_state.lateral() if keyboard_state is not None else 0
            if manual_lateral and now >= manual_until:
                manual_lateral = 0
            lateral = x11_lateral or manual_lateral or (body.lateral if body.calibrated else 0)
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
                    if keyboard_state is None or not keyboard_state.available:
                        manual_lateral, manual_until = -1, now + KEYBOARD_EVENT_HOLD_SECONDS
                elif action == "right":
                    if keyboard_state is None or not keyboard_state.available:
                        manual_lateral, manual_until = 1, now + KEYBOARD_EVENT_HOLD_SECONDS
                elif action == "launch":
                    manual_jump = True
                elif action == "reset":
                    game.reset(full=True)
            frame_id += 1
            deadline += period
            sleep_time = deadline - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            elif sleep_time < -period:
                deadline = time.monotonic()
    finally:
        if sender:
            sender.close()
        if camera:
            camera.close()
        if keyboard_state is not None:
            keyboard_state.close()
        if not args.no_preview:
            cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
