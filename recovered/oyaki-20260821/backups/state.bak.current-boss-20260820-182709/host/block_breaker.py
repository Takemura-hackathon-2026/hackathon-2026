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
import json
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


def rotate_frame(frame: np.ndarray, rotation: str) -> np.ndarray:
    """カメラフレームを回転する。ROI・背景差分・計測より先に呼ぶ。

    camera_calibrate.py の同名関数と同一の実装。キャリブレーション結果の閾値は
    「回転後の座標系」で測られているため、両者がずれると閾値の意味が変わる。
    """
    if rotation == "none":
        return np.ascontiguousarray(frame)
    if rotation == "cw":
        return np.ascontiguousarray(np.rot90(frame, k=3))
    if rotation == "ccw":
        return np.ascontiguousarray(np.rot90(frame, k=1))
    if rotation == "180":
        return np.ascontiguousarray(np.rot90(frame, k=2))
    raise ValueError(f"未知の回転: {rotation}")


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
    #: 0..1 の絶対位置（0が画面左端）。カメラ入力時のみ入る。
    position: float | None = None


class InputClassifier:
    """校正済み重心の時系列をLEFT/RIGHT/JUMPへ変換する。"""

    #: 既定閾値のまま使う場合の「強制切替」倍率。--calibration 指定時も同じ比率を保つ。
    FORCE_RATIO = 1.7

    def __init__(
        self,
        samples: int = 30,
        jump_rise_y_min: float = 0.05,
        jump_rise_bottom_min: float = 0.04,
        left_delta_min: float = 0.10,
        right_delta_min: float = 0.10,
        center_tolerance: float = 0.045,
        baseline: "BodyMeasurement | None" = None,
        mirror: bool = False,
    ) -> None:
        self.mirror = bool(mirror)
        self.required_samples = max(3, samples)
        if (
            not math.isfinite(jump_rise_y_min)
            or not math.isfinite(jump_rise_bottom_min)
            or jump_rise_y_min <= 0
            or jump_rise_bottom_min <= 0
        ):
            raise ValueError("ジャンプ閾値は正の値")
        for name, value in (
            ("left_delta_min", left_delta_min),
            ("right_delta_min", right_delta_min),
            ("center_tolerance", center_tolerance),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} は正の値")
        if center_tolerance >= min(left_delta_min, right_delta_min):
            raise ValueError("center_tolerance は left/right の delta_min より小さい必要がある")
        self.jump_rise_y_min = float(jump_rise_y_min)
        self.jump_rise_bottom_min = float(jump_rise_bottom_min)
        self.left_delta_min = float(left_delta_min)
        self.right_delta_min = float(right_delta_min)
        self.center_tolerance = float(center_tolerance)
        self.samples: list[BodyMeasurement] = []
        self.baseline: BodyMeasurement | None = baseline
        self.fixed_baseline = baseline
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
        self.__init__(
            self.required_samples,
            self.jump_rise_y_min,
            self.jump_rise_bottom_min,
            self.left_delta_min,
            self.right_delta_min,
            self.center_tolerance,
            self.fixed_baseline,
            self.mirror,
        )

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
        # direction は画像座標での向き（-1 が画像の左）。閾値はキャリブレ時と同じく
        # 画像座標で測ってあるので、判定は direction で行う。
        # target はゲームへ渡す向き。カメラは人を正面から撮るため、プレイヤーが自分の
        # 左へ動くと画像上では右へ動く。mirror はこの鏡像を吸収する。
        if offset <= -self.left_delta_min:
            direction, gate = -1, self.left_delta_min
        elif offset >= self.right_delta_min:
            direction, gate = 1, self.right_delta_min
        elif abs(offset) <= self.center_tolerance:
            direction, gate = 0, self.center_tolerance
        else:
            direction = -self.lateral if self.mirror else self.lateral
            gate = self.left_delta_min if direction < 0 else self.right_delta_min
        target = -direction if self.mirror else direction
        force = gate * self.FORCE_RATIO
        if target != self.lateral:
            if target != self.candidate:
                self.candidate, self.candidate_since = target, now
            elif target == 0 or ((velocity_x * direction >= .03 or abs(offset) >= force) and now - self.candidate_since >= .12):
                self.lateral, self.candidate = target, target
        else:
            self.candidate = target

        rise_y, rise_bottom = base.y - body.y, base.bottom - body.bottom
        pose = rise_y >= self.jump_rise_y_min and rise_bottom >= self.jump_rise_bottom_min
        jump = pose and not self.jump_latched and now - self.last_jump >= .65
        if jump:
            self.jump_latched, self.last_jump = True, now
        elif rise_y < self.jump_rise_y_min * .45 and rise_bottom < self.jump_rise_bottom_min * .45:
            self.jump_latched = False
        self.last, self.last_time = body, now
        return InputState(self.lateral, jump, True, True)


class Person:
    """追跡中の一人。基準はその人自身の直近の静止姿勢から作る。

    固定の baseline を全員へ当てると体格や立ち位置の差でずれるため、
    人ごとに直近の中央値を基準にする。これで誰がどこに立っても成立する。
    """

    __slots__ = ("uid", "x", "y", "bottom", "area", "last_seen", "history", "airborne", "last_jump")

    def __init__(self, uid: int, m: BodyMeasurement, now: float) -> None:
        self.uid = uid
        self.update(m, now)
        self.history: list[tuple[float, float, float]] = [(now, m.y, m.bottom)]
        self.airborne = False
        self.last_jump = -math.inf

    def update(self, m: BodyMeasurement, now: float) -> None:
        self.x, self.y, self.bottom, self.area = m.x, m.y, m.bottom, m.area
        self.last_seen = now

    def push(self, now: float, window: float) -> None:
        self.history.append((now, self.y, self.bottom))
        cutoff = now - window
        while len(self.history) > 2 and self.history[0][0] < cutoff:
            self.history.pop(0)

    def rest(self) -> tuple[float, float]:
        """直近の静止姿勢。跳んでいる間は少数派なので中央値で潰れる。"""
        ys = sorted(row[1] for row in self.history)
        bottoms = sorted(row[2] for row in self.history)
        middle = len(ys) // 2
        return ys[middle], bottoms[middle]

    def rise(self) -> tuple[float, float]:
        rest_y, rest_bottom = self.rest()
        return rest_y - self.y, rest_bottom - self.bottom


class Tracker:
    """重心の近さでフレーム間の対応を取る簡易トラッカー。"""

    def __init__(self, match_distance: float, forget_seconds: float, history_seconds: float) -> None:
        self.match_distance = match_distance
        self.forget_seconds = forget_seconds
        self.history_seconds = history_seconds
        self.people: list[Person] = []
        self.next_uid = 1

    def update(self, measurements: list[BodyMeasurement], now: float) -> list[Person]:
        unmatched = list(self.people)
        for m in measurements:
            best, best_distance = None, self.match_distance
            for person in unmatched:
                distance = math.hypot(person.x - m.x, person.y - m.y)
                if distance < best_distance:
                    best, best_distance = person, distance
            if best is None:
                best = Person(self.next_uid, m, now)
                self.next_uid += 1
                self.people.append(best)
            else:
                unmatched.remove(best)
                best.update(m, now)
            best.push(now, self.history_seconds)
        self.people = [p for p in self.people if now - p.last_seen <= self.forget_seconds]
        return self.people


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
        jump_rise_y_min: float,
        jump_rise_bottom_min: float,
        rotation: str = "none",
        process_size: tuple[int, int] | None = None,
        capture_fps: float = 60.0,
        exposure: tuple[float, float, float] | None = None,
        left_delta_min: float = 0.10,
        right_delta_min: float = 0.10,
        center_tolerance: float = 0.045,
        baseline: "BodyMeasurement | None" = None,
        mirror: bool = False,
        play_range: tuple[float, float] | None = None,
    ) -> None:
        self.play_range = play_range
        self.capture = cv2.VideoCapture(device)
        if not self.capture.isOpened():
            raise RuntimeError(f"カメラ {device} を開けない")
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.capture.set(cv2.CAP_PROP_FPS, capture_fps)
        if exposure is not None:
            # キャリブレーション時と同じ露出でないと背景差分の前提が変わる。
            self.capture.set(cv2.CAP_PROP_AUTO_EXPOSURE, exposure[0])
            self.capture.set(cv2.CAP_PROP_EXPOSURE, exposure[1])
            self.capture.set(cv2.CAP_PROP_GAIN, exposure[2])
        self.background_seconds = max(.2, background_seconds)
        self.min_area = max(1, min_area)
        self.roi = roi
        self.rotation = rotation
        # process_size を渡すと、キャリブレーションと同じ「回転→ROI→固定サイズ」の
        # 前処理になる。None のときは従来どおり「ROI→幅240のアスペクト維持」。
        self.process_size = process_size
        self.started: float | None = None
        self.subtractor = cv2.createBackgroundSubtractorMOG2(history=240, varThreshold=20, detectShadows=False)
        self.classifier = InputClassifier(
            jump_rise_y_min=jump_rise_y_min,
            jump_rise_bottom_min=jump_rise_bottom_min,
            left_delta_min=left_delta_min,
            right_delta_min=right_delta_min,
            center_tolerance=center_tolerance,
            baseline=baseline,
            mirror=mirror,
        )
        self.debug: np.ndarray | None = None
        self.mask: np.ndarray | None = None
        self.probe: dict[str, float] | None = None
        self.people_count = 0
        self.active_uid: int | None = None
        self.jump_edge = False
        self.tracker = Tracker(match_distance=0.28, forget_seconds=0.8, history_seconds=1.2)

    @property
    def stage(self) -> str:
        if self.started is None or time.monotonic() - self.started < self.background_seconds:
            return "BACKGROUND"
        return "READY" if self.classifier.calibrated else "STANCE"

    @property
    def uses_fixed_baseline(self) -> bool:
        return self.classifier.fixed_baseline is not None

    def close(self) -> None:
        self.capture.release()

    def read(self, now: float) -> InputState:
        ok, source = self.capture.read()
        if not ok:
            return InputState(calibrated=self.classifier.calibrated)
        if self.started is None:
            self.started = now
        # 回転はROI・背景差分・計測より先。camera_calibrate.py の _process_frame と同順。
        source = rotate_frame(source, self.rotation)
        if self.roi is not None:
            x, y, width, height = self.roi
            source_height, source_width = source.shape[:2]
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > source_width or y + height > source_height:
                raise ValueError(
                    f"ROI {x},{y},{width},{height} が回転後画像 {source_width}x{source_height} を超える"
                )
            source = source[y:y + height, x:x + width]
        if self.process_size is not None:
            width, height = self.process_size
        else:
            width = 240
            height = max(1, round(source.shape[0] * width / source.shape[1]))
        image = cv2.resize(source, (width, height), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        background_phase = now - self.started < self.background_seconds
        foreground = self.subtractor.apply(gray, learningRate=.35 if background_phase else 0.0)
        _, mask = cv2.threshold(foreground, 200, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        bodies: list[BodyMeasurement] = []
        drawn: list = []
        if not background_phase:
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            # 最大ひとつではなく、面積を満たす輪郭すべてを人として拾う。
            for contour in contours:
                area = float(cv2.contourArea(contour))
                if area < self.min_area:
                    continue
                moments = cv2.moments(contour)
                if not moments["m00"]:
                    continue
                _, y, _, h = cv2.boundingRect(contour)
                bodies.append(BodyMeasurement(
                    float(moments["m10"] / moments["m00"] / width),
                    float(moments["m01"] / moments["m00"] / height),
                    float((y + h) / height),
                    area / float(width * height),
                ))
                drawn.append(contour)
        body = self._select(bodies, now) if not background_phase else None
        debug = image.copy()
        for contour in drawn:
            cv2.drawContours(debug, [contour], -1, (0, 255, 0), 1)
        cv2.putText(debug, self.stage, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 230, 230), 1, cv2.LINE_AA)
        self.debug, self.mask = debug, mask
        self.people_count = len(bodies)
        if background_phase:
            return InputState()
        state = self.classifier.update(body, now)
        if body is not None:
            # mirror はカメラが人を正面から撮ることによる鏡像を吸収する。
            position = 1.0 - body.x if self.classifier.mirror else body.x
            if self.play_range is not None:
                low, high = self.play_range
                position = (position - low) / max(1e-6, high - low)
            state = InputState(state.lateral, state.jump or self.jump_edge, True, True,
                               position=min(1.0, max(0.0, position)))
        self.jump_edge = False
        # 診断用。判定に使った実測値をそのまま残す（--jump-debug が読む）。
        base, last = self.classifier.baseline, self.classifier.last
        if base is not None and last is not None:
            self.probe = {
                "rise_y": base.y - last.y,
                "rise_bottom": base.bottom - last.bottom,
                "offset_x": last.x - base.x,
                "area": last.area,
            }
        else:
            self.probe = None
        return state

    def _select(self, bodies: list[BodyMeasurement], now: float) -> BodyMeasurement | None:
        """追跡を更新し、操作者を1人選ぶ。

        ジャンプした人がいればその人へ乗り換える。複数人が写る場では、跳んだ人が
        「自分が操作する」と主張する形になる。誰も跳んでいなければ現在の操作者を
        維持し、居なくなったときだけ最大面積の人へ引き継ぐ。
        """
        people = self.tracker.update(bodies, now)
        if not people:
            self.active_uid = None
            self.jump_edge = False
            return None
        jumped: Person | None = None
        for person in people:
            rise_y, rise_bottom = person.rise()
            if rise_y >= self.classifier.jump_rise_y_min and rise_bottom >= self.classifier.jump_rise_bottom_min:
                if not person.airborne and now - person.last_jump >= .65:
                    person.airborne = True
                    person.last_jump = now
                    if jumped is None or person.area > jumped.area:
                        jumped = person
            elif (rise_y < self.classifier.jump_rise_y_min * .45
                  and rise_bottom < self.classifier.jump_rise_bottom_min * .45):
                person.airborne = False
        if jumped is not None:
            self.active_uid = jumped.uid
            self.jump_edge = True
        active = next((p for p in people if p.uid == self.active_uid), None)
        if active is None:
            active = max(people, key=lambda p: p.area)
            self.active_uid = active.uid
        return BodyMeasurement(active.x, active.y, active.bottom, active.area)

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
    #: 0..1 の絶対位置。カメラ入力ではこれを使い、パドルを立ち位置へ直接置く。
    #: None のときだけ lateral による相対移動へ落ちる（キーボード操作など）。
    position: float | None = None


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
    #: 絶対位置追従の強さ。1.0 で完全直結、小さいほど滑らかだが遅れる。
    position_gain = 0.85
    #: この画素数までの誤差は輪郭の揺れとみなして動かさない。
    #: 誤差からこの分を差し引いてから追従するので、境界で段差にならない。
    position_deadzone = 3.0
    boss_scale = 1.20
    boss_y = 34.0  # 上端のプレイ領域(y=26)との間に8pxを残す。
    boss_max_hp, boss_damage = 100, 10
    damage_effect_duration = .24
    boss_transition_duration = .48  # 3回の点滅（点灯・消灯を3周期）
    boss_move_speed = 52.0
    clear_delay = 1.8

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
        # リセット前から残っているジャンプを発射として扱わない。
        # いったん launch=False を観測した後の新しいジャンプだけを受理する。
        self.launch_armed = False
        self.game_started = False
        self.boss_collision_armed = False
        self.damage_effect_remaining = 0.0
        self.life_loss_feedback_active = False
        self.life_loss_blink_elapsed = 0.0
        self.life_loss_slot = -1
        self.boss_transition_remaining = 0.0
        self.boss_move_active = False
        self.boss_move_vx = self.boss_move_speed
        self.clear_remaining = 0.0
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
            self.clear_remaining = 0.0
            self.game_over_until = 0.0
        self.paddle_x = (CANVAS_WIDTH - self.paddle_width) / 2
        self.serving = True
        self.launch_armed = False
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
            self.clear_remaining = self.clear_delay
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
        self.launch_armed = False
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
        if self.boss_defeated:
            self.clear_remaining = max(0.0, self.clear_remaining - dt)
            if self.clear_remaining == 0.0:
                self.reset(full=True)
            return
        if self.game_over_until:
            if now >= self.game_over_until:
                self.game_over_until = 0.0
                self.reset(full=True)
            return
        if self.boss_defeated:
            return
        limit = CANVAS_WIDTH - self.paddle_width
        if controls.position is not None:
            # 立ち位置をそのままパドル位置にする。相対移動のような追従待ちが無いので
            # 入力からの遅れは実質カメラの1フレームぶんになる。
            target = min(max(0.0, float(controls.position)), 1.0) * limit
            # 小刻みな揺れだけを殺し、大きな移動には高いゲインでそのまま追従する。
            # 平滑化を強めるとラグとして体感されるので、揺れの除去は不感帯で行う。
            error = target - self.paddle_x
            if abs(error) > self.position_deadzone:
                self.paddle_x += (error - math.copysign(self.position_deadzone, error)) * self.position_gain
        else:
            self.paddle_x = self.paddle_x + max(-1, min(1, controls.lateral)) * self.paddle_speed * dt
        self.paddle_x = min(max(0.0, self.paddle_x), limit)
        if self.serving:
            self._place_ball_at_mouth()
            if not controls.launch:
                self.launch_armed = True
            elif self.launch_armed:
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


class Calibration:
    """camera_calibrate.py が出力した camera_calibration.json の必要項目。

    閾値は「回転後・ROI適用後・processed_size へリサイズした座標系」で測られている。
    そのため撮影条件（回転・ROI・処理サイズ・露出）も併せて取り込む必要がある。
    """

    def __init__(self, data: dict, path: Path, zone: str | None = None) -> None:
        # distance_probe.py が出す複数距離帯형式にも対応する。
        zones = data.get("zones")
        self.zone = None
        if zones:
            available = sorted(zones)
            if zone is None:
                raise ValueError(f"{path} は距離帯を持つ。--calibration-zone で選ぶ: {available}")
            if zone not in zones:
                raise ValueError(f"{path} に距離帯 {zone} が無い。選べるのは {available}")
            entry = zones[zone]
            self.zone = zone
        else:
            if zone is not None:
                raise ValueError(f"{path} は単一のキャリブレーションで、距離帯を選べない")
            entry = data
        if not entry.get("valid") or entry.get("status") != "PASS":
            reasons = entry.get("quality", {}).get("reasons") or ["理由の記録なし"]
            raise ValueError(f"{path} は valid/PASS ではない: {reasons}")
        thresholds = entry["thresholds"]
        camera = data["camera"]
        self.path = path
        self.date = data.get("date", "不明")
        self.jump_rise_y_min = float(thresholds["jump"]["rise_y_min"])
        self.jump_rise_bottom_min = float(thresholds["jump"]["rise_bottom_min"])
        self.left_delta_min = float(thresholds["left"]["delta_min"])
        self.right_delta_min = float(thresholds["right"]["delta_min"])
        self.center_tolerance = float(thresholds["center_tolerance"]["x"])
        # 閾値はこの baseline を基準に測った相対量なので、必ずセットで使う。
        # 別の baseline に当てると rise/offset の意味が変わり判定が成立しない。
        base = entry.get("baseline") or {}
        self.baseline = None
        if all(key in base for key in ("x", "y", "bottom", "area")):
            self.baseline = BodyMeasurement(
                float(base["x"]), float(base["y"]), float(base["bottom"]), float(base["area"])
            )
        self.rotation = str(camera["rotation"])
        self.capture_width = int(camera["requested_width"])
        self.capture_height = int(camera["requested_height"])
        self.capture_fps = float(camera["fps"])
        requested = camera["exposure"]["requested"]
        self.exposure = (float(requested["auto"]), float(requested["shutter"]), float(requested["gain"]))
        self.device = int(camera["device"])
        size = data["ROI"]["processed_size"]
        self.process_size = (int(size[0]), int(size[1]))
        after = data["ROI"]["after_rotation"]
        self.roi = None if after is None else tuple(int(v) for v in after)

    def summary(self) -> str:
        return (
            f"calibration {self.path}{'' if self.zone is None else ' zone=' + self.zone} ({self.date})\n"
            f"  rotation={self.rotation} process={self.process_size[0]}x{self.process_size[1]} "
            f"roi={self.roi} capture={self.capture_width}x{self.capture_height}@{self.capture_fps}\n"
            f"  jump rise_y>={self.jump_rise_y_min:.4f} rise_bottom>={self.jump_rise_bottom_min:.4f}\n"
            f"  lateral left>={self.left_delta_min:.4f} right>={self.right_delta_min:.4f} "
            f"center<={self.center_tolerance:.4f}\n"
            f"  baseline "
            + ("なし（起動時に自前で取る）" if self.baseline is None else
               f"x={self.baseline.x:.4f} y={self.baseline.y:.4f} "
               f"bottom={self.baseline.bottom:.4f} area={self.baseline.area:.4f}")
        )


def load_calibration(path: Path, zone: str | None = None) -> Calibration:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"{path} が無い") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} を JSON として読めない: {exc}") from exc
    return Calibration(data, path, zone)


def parse_range(value: str) -> tuple[float, float]:
    try:
        low, high = (float(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("LOW,HIGH の実数2値") from exc
    if not 0.0 <= low < high <= 1.0:
        raise argparse.ArgumentTypeError("0 <= LOW < HIGH <= 1 であること")
    return low, high


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
    result.add_argument(
        "--calibration",
        type=Path,
        default=None,
        help="camera_calibrate.py の camera_calibration.json から閾値・回転・ROI・露出を読む",
    )
    result.add_argument(
        "--calibration-zone",
        default=None,
        help="distance_probe.py の camera_calibration_multi.json を使うとき、距離帯 NEAR/MID/FAR を選ぶ",
    )
    result.add_argument("--jump-rise-y-min", type=float, default=0.05, help="ジャンプ判定の重心上昇量（既定0.05）")
    result.add_argument("--jump-rise-bottom-min", type=float, default=0.04, help="ジャンプ判定の下端上昇量（既定0.04）")
    result.add_argument("--fps", type=float, default=60.0)
    result.add_argument("--frames", type=int, default=0)
    result.add_argument("--seconds", type=float, default=0.0)
    result.add_argument("--send", action="store_true")
    result.add_argument("--pi", action="append", default=[], metavar="HOST[:PORT]")
    result.add_argument("--chunk-size", type=int, default=1200)
    result.add_argument("--no-preview", action="store_true")
    result.add_argument("--preview-scale", type=int, default=2)
    result.add_argument("--debug-camera", action="store_true")
    result.add_argument(
        "--jump-debug",
        action="store_true",
        help="1秒ごとに rise_y/rise_bottom/offset_x の実測を stderr へ出す（判定の切り分け用）",
    )
    result.add_argument("--boundaries", action="store_true")
    result.add_argument(
        "--mirror",
        action="store_true",
        help="左右を反転する。キャリブレをカメラから見た向きで取った場合に付ける",
    )
    result.add_argument(
        "--play-range",
        type=parse_range,
        default=None,
        metavar="LOW,HIGH",
        help="画面端に対応させるカメラ内の横位置 0..1（例 0.2,0.8）。狭めると小さい移動で端まで届く",
    )
    result.add_argument(
        "--position-gain",
        type=float,
        default=None,
        help="立ち位置へのパドル追従の強さ 0<g<=1（既定0.85）。1に近いほど直結でラグが減る",
    )
    result.add_argument(
        "--position-deadzone",
        type=float,
        default=None,
        metavar="PIXELS",
        help="この画素数までの誤差は揺れとみなして動かさない（既定3.0）。上げるほど揺れに鈍くなる",
    )
    return result


def preview(indexed: np.ndarray) -> np.ndarray:
    return np.asarray([item[:3] for item in FC6], np.uint8)[indexed][:, :, ::-1]


def given_options(argv: Iterable[str] | None) -> set[str]:
    """コマンドラインに実際に現れたオプション名。calibration より CLI を優先するため。"""
    source = list(sys.argv[1:] if argv is None else argv)
    return {item.split("=", 1)[0] for item in source if item.startswith("--")}


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    calibration: Calibration | None = None
    if args.calibration is not None:
        try:
            calibration = load_calibration(args.calibration, args.calibration_zone)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        given = given_options(argv)
        # CLI で明示した値は calibration より優先する。
        for option, attribute, value in (
            ("--jump-rise-y-min", "jump_rise_y_min", calibration.jump_rise_y_min),
            ("--jump-rise-bottom-min", "jump_rise_bottom_min", calibration.jump_rise_bottom_min),
            ("--camera", "camera", calibration.device),
            ("--camera-width", "camera_width", calibration.capture_width),
            ("--camera-height", "camera_height", calibration.capture_height),
            ("--roi", "roi", calibration.roi),
        ):
            if option not in given:
                setattr(args, attribute, value)
        print(calibration.summary(), file=sys.stderr)
    if (
        args.fps <= 0
        or args.preview_scale <= 0
        or args.jump_rise_y_min <= 0
        or args.jump_rise_bottom_min <= 0
        or (args.send and len(args.pi) != PI_COUNT)
    ):
        print("error: --fps/--preview-scale/--jump-rise-* または --pi の指定が不正", file=sys.stderr)
        return 2
    if args.position_gain is not None:
        if not 0.0 < args.position_gain <= 1.0:
            print("error: --position-gain は 0 より大きく 1 以下", file=sys.stderr)
            return 2
        BlockBreaker.position_gain = args.position_gain
    if args.position_deadzone is not None:
        if args.position_deadzone < 0:
            print("error: --position-deadzone は 0 以上", file=sys.stderr)
            return 2
        BlockBreaker.position_deadzone = args.position_deadzone
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
                args.jump_rise_y_min,
                args.jump_rise_bottom_min,
                rotation="none" if calibration is None else calibration.rotation,
                process_size=None if calibration is None else calibration.process_size,
                capture_fps=60.0 if calibration is None else calibration.capture_fps,
                exposure=None if calibration is None else calibration.exposure,
                left_delta_min=0.10 if calibration is None else calibration.left_delta_min,
                right_delta_min=0.10 if calibration is None else calibration.right_delta_min,
                center_tolerance=0.045 if calibration is None else calibration.center_tolerance,
                baseline=None if calibration is None else calibration.baseline,
                mirror=args.mirror,
                play_range=args.play_range,
            )
        sender = UdpFrameSender([parse_pi(value) for value in args.pi], args.chunk_size) if args.send else None
        debug_peak = {"rise_y": -9.0, "rise_bottom": -9.0, "offset_abs": 0.0, "seen": 0, "jumps": 0}
        debug_last = time.monotonic()
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
            if args.jump_debug and camera is not None:
                probe = camera.probe
                if probe is not None:
                    debug_peak["rise_y"] = max(debug_peak["rise_y"], probe["rise_y"])
                    debug_peak["rise_bottom"] = max(debug_peak["rise_bottom"], probe["rise_bottom"])
                    debug_peak["offset_abs"] = max(debug_peak["offset_abs"], abs(probe["offset_x"]))
                    debug_peak["seen"] += 1
                if body.jump:
                    debug_peak["jumps"] += 1
                if now - debug_last >= 1.0:
                    thresholds = camera.classifier
                    print(
                        f"[jump-debug] stage={camera.stage} 検出={debug_peak['seen']}f "
                        f"rise_y max={debug_peak['rise_y']:+.4f}/{thresholds.jump_rise_y_min:.4f} "
                        f"rise_bottom max={debug_peak['rise_bottom']:+.4f}/{thresholds.jump_rise_bottom_min:.4f} "
                        f"|offset| max={debug_peak['offset_abs']:.4f}"
                        f"(L{thresholds.left_delta_min:.3f}/R{thresholds.right_delta_min:.3f}) "
                        f"JUMP={debug_peak['jumps']}",
                        file=sys.stderr,
                        flush=True,
                    )
                    debug_peak = {"rise_y": -9.0, "rise_bottom": -9.0, "offset_abs": 0.0, "seen": 0, "jumps": 0}
                    debug_last = now
            x11_lateral = keyboard_state.lateral() if keyboard_state is not None else 0
            if manual_lateral and now >= manual_until:
                manual_lateral = 0
            lateral = x11_lateral or manual_lateral or (body.lateral if body.calibrated else 0)
            # 手動操作している間は絶対位置を無視して、キーボードの相対移動を優先する。
            position = None if x11_lateral or manual_lateral else body.position
            game.step(min(.05, now - last),
                      GameInput(lateral, body.jump or manual_jump, position), now)
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
