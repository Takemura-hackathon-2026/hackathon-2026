#!/usr/bin/env python3
"""STRUCTURE Sensorで操作する192x384 RGB LEDブロック崩し。

主機でセンサー判定、ゲーム更新、FC6パレット番号での描画を完結する。完成フレームは
既存の UdpFrameSender が192x96ずつ4台のPiへ送るため、Pi側へ人物映像・人物マスク・
ゲームロジックを渡さない。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import math
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np

HOST = Path(__file__).resolve().parent
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

from palettes import FC6, FC6_BLACK, FC6_LIMIT, FC6_WHITE, PaletteMode  # noqa: E402
from frame_source import StructureSensorSource, depth_preview  # noqa: E402
from test_mode import CANVAS_HEIGHT, CANVAS_WIDTH, PI_COUNT, UdpFrameSender, parse_pi  # noqa: E402


# 正本 host/palettes.py 内のFC6インデックスだけを使う。
SKY, SKY_DOT, PADDLE, PADDLE_EDGE, BALL = 0x1C, 0x20, 0x1E, 0x22, FC6_WHITE
TEXT, DIM = FC6_WHITE, 0x31

# cv2.waitKeyEx() の値はOS/バックエンドで異なる。ASCIIに加え、Linux/X11・
# macOS/Cocoa・Windowsで使われる代表値を受け入れる。
LEFT_KEYS = frozenset((81, 2424832, 65361, 63234))
RIGHT_KEYS = frozenset((83, 2555904, 65363, 63235))
UP_KEYS = frozenset((82, 2490368, 65362, 63232))
# OpenCVのwaitKeyExにはキー解放イベントがないため、ネイティブのキー状態取得が
# 使えない環境ではキーリピート間を埋めるフォールバック保持時間を持たせる。
KEYBOARD_EVENT_HOLD_SECONDS = 0.20


@dataclass(frozen=True)
class BossProfile:
    key: str
    display_name: str
    image: Path
    max_hp: int
    movement: str
    move_speed: float
    flip_horizontal: bool = False
    final_boss: bool = False
    beam_attack: bool = False
    vibration_amplitude: float = 0.0
    vibration_frequency: float = 0.0
    barrier_hits_required: int = 0
    critical_hits_required: int = 0
    damage_per_hit: int = 10


BOSS_PROFILES = (
    BossProfile("takemura", "Prof. Takemura", HOST / "assets" / "takemuraface_fc6.png", 100, "horizontal", 52.0),
    BossProfile("tuchiya", "Prof. Tuchiya", HOST / "assets" / "tsuchiyaface.png", 1, "horizontal", 52.0, barrier_hits_required=5, damage_per_hit=1),
    BossProfile("inaba", "Prof. Inaba", HOST / "assets" / "inabaface.png", 3, "horizontal", 52.0, critical_hits_required=3, damage_per_hit=1),
    BossProfile("meka_takemura", "MEKA.TKMR", HOST / "assets" / "robo_takemuraface.png", 160, "horizontal", 110.0, final_boss=True, beam_attack=True),
    BossProfile("ohki", "Prof. Ohki", HOST / "assets" / "ookiface.png", 60, "figure8", 1.9, flip_horizontal=True),
    BossProfile("inagaki", "Prof. Inagaki", HOST / "assets" / "inagakiface.png", 100, "vibrate_horizontal", 68.0, vibration_amplitude=20.0, vibration_frequency=1.8),
)
BOSS_IMAGES = tuple(profile.image for profile in BOSS_PROFILES)
DEFAULT_START_SETTINGS = {
    "center_tolerance": 0.18,
    "width_gain_min": 0.06,
    "upper_width_gain_min": 0.04,
    "upper_width_min": 0.30,
    "area_gain_min": 0.05,
}


def load_boss_sprite(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """RGB/RGBA PNGをFC6インデックス画像と不透明マスクへ変換する。"""
    source = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if source is None:
        raise RuntimeError(f"ボス画像を読み込めない: {path}")
    if source.ndim != 3 or source.shape[2] not in (3, 4):
        raise RuntimeError(f"ボス画像はRGB/RGBA PNGでなければならない: {path}")
    rgb = source[:, :, :3][:, :, ::-1].astype(np.int32)
    palette = np.asarray([color[:3] for color in FC6], dtype=np.int32)
    distance = np.sum((rgb[:, :, None, :] - palette[None, None, :, :]) ** 2, axis=3)
    indexed = np.argmin(distance, axis=2).astype(np.uint8)
    if source.shape[2] == 4:
        mask = source[:, :, 3] > 0
    else:
        # クリップボード由来のRGB画像は白背景になりやすいので、外周につながる
        # 白領域だけを透明化し、顔の中の白目や装飾は残す。
        white = np.all(source[:, :, :3] >= 245, axis=2).astype(np.uint8)
        component_count, labels, stats, _ = cv2.connectedComponentsWithStats(white, connectivity=8)
        if component_count > 1:
            # 白い背景が外周に届かない画像でも、最大の白領域を背景として扱う。
            background_label = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            mask = labels != background_label
        else:
            mask = white == 0
    return indexed, mask


@dataclass(frozen=True)
class BodyMeasurement:
    """ROI内で0〜1に正規化した重心・下端・面積。"""

    x: float
    y: float
    bottom: float
    area: float
    width: float = 0.0
    height: float = 0.0
    upper_width: float = 0.0


@dataclass(frozen=True)
class InputState:
    lateral: int = 0  # -1 LEFT, 0 IDLE, 1 RIGHT
    jump: bool = False  # 1フレームのイベント
    body_present: bool = False
    calibrated: bool = False
    launch: bool = False  # 腕で輪を作った1フレームの開始イベント（互換用）
    start_trigger: bool = False  # 開始条件を満たし、カウントダウンを開始するイベント
    body_x: float | None = None  # 人物中心X（ROI内で0〜1）。バー追従用
    start_hold_remaining: float | None = None  # 静止開始までの残り秒数。人物未検出時はNone。
    player_id: int | None = None  # 現在バーを操作している追跡対象
    people_detected: int = 0  # 今フレームで安定検出した人数
    player_changed: bool = False  # ジャンプまたは離脱で操作対象が交代したフレーム


class InputClassifier:
    """校正済み人物領域の時系列をLEFT/RIGHT/JUMP/開始へ変換する。"""

    def __init__(
        self,
        samples: int = 30,
        jump_rise_y_min: float = 0.05,
        jump_rise_bottom_min: float = 0.04,
        lateral_confirm_frames: int = 4,
        start_center_tolerance: float = 0.18,
        start_width_gain: float = 0.06,
        start_upper_width_gain: float = 0.04,
        start_upper_width_min: float = 0.30,
        start_area_gain: float = 0.05,
        start_confirm_frames: int = 4,
        start_rearm_frames: int = 6,
    ) -> None:
        self.required_samples = max(3, samples)
        if (
            not math.isfinite(jump_rise_y_min)
            or not math.isfinite(jump_rise_bottom_min)
            or jump_rise_y_min <= 0
            or jump_rise_bottom_min <= 0
        ):
            raise ValueError("ジャンプ閾値は正の値")
        self.jump_rise_y_min = float(jump_rise_y_min)
        self.jump_rise_bottom_min = float(jump_rise_bottom_min)
        self.lateral_confirm_frames = max(2, int(lateral_confirm_frames))
        if (
            not math.isfinite(start_center_tolerance)
            or not math.isfinite(start_width_gain)
            or not math.isfinite(start_upper_width_gain)
            or not math.isfinite(start_upper_width_min)
            or not math.isfinite(start_area_gain)
            or start_center_tolerance <= 0
            or start_width_gain <= 0
            or start_upper_width_gain <= 0
            or start_upper_width_min <= 0
            or start_area_gain <= 0
        ):
            raise ValueError("腕輪スタート判定の閾値は正の有限値")
        self.start_center_tolerance = float(start_center_tolerance)
        self.start_width_gain = float(start_width_gain)
        self.start_upper_width_gain = float(start_upper_width_gain)
        self.start_upper_width_min = float(start_upper_width_min)
        self.start_area_gain = float(start_area_gain)
        self.start_confirm_frames = max(2, int(start_confirm_frames))
        self.start_rearm_frames = max(2, int(start_rearm_frames))
        self.samples: list[BodyMeasurement] = []
        self.baseline: BodyMeasurement | None = None
        self.last: BodyMeasurement | None = None
        self.last_time = 0.0
        self.x_history: list[float] = []
        self.lateral = 0
        self.candidate = 0
        self.candidate_frames = 0
        self.jump_latched = False
        self.last_jump = -math.inf
        self.start_latched = False
        self.start_pose_frames = 0
        self.start_rearm_count = 0

    @property
    def calibrated(self) -> bool:
        return self.baseline is not None

    def reset(self) -> None:
        self.__init__(
            self.required_samples,
            self.jump_rise_y_min,
            self.jump_rise_bottom_min,
            self.lateral_confirm_frames,
            self.start_center_tolerance,
            self.start_width_gain,
            self.start_upper_width_gain,
            self.start_upper_width_min,
            self.start_area_gain,
            self.start_confirm_frames,
            self.start_rearm_frames,
        )

    def _arm_circle_pose(self, body: BodyMeasurement, baseline: BodyMeasurement) -> bool:
        """深度領域の広がりから、両腕で輪を作る姿勢を近似する。"""
        # 現在のSTRUCTURE Sensor経路には関節点がないため、中央維持、人物幅、
        # 上半身幅、面積の増加を組み合わせる。単なるジャンプは幅が増えない。
        if body.width <= 0.0 or body.upper_width <= 0.0:
            return False
        if abs(body.x - baseline.x) > self.start_center_tolerance:
            return False
        return (
            body.width - baseline.width >= self.start_width_gain
            and body.upper_width - baseline.upper_width >= self.start_upper_width_gain
            and body.upper_width >= self.start_upper_width_min
            and body.area >= baseline.area * (1.0 + self.start_area_gain)
        )

    def update(self, body: BodyMeasurement | None, now: float) -> InputState:
        if body is None:
            self.last = None
            self.x_history.clear()
            self.lateral = 0
            self.candidate = 0
            self.candidate_frames = 0
            self.start_latched = False
            self.start_pose_frames = 0
            self.start_rearm_count = 0
            return InputState(calibrated=self.calibrated)
        if self.baseline is None:
            self.samples.append(body)
            if len(self.samples) >= self.required_samples:
                data = np.asarray(
                    [[v.x, v.y, v.bottom, v.area, v.width, v.height, v.upper_width] for v in self.samples]
                )
                values = np.median(data, axis=0)
                self.baseline = BodyMeasurement(*[float(v) for v in values])
                self.samples.clear()
                self.x_history = [body.x]
            self.last, self.last_time = body, now
            return InputState(body_present=True, calibrated=self.calibrated, body_x=body.x)

        base = self.baseline
        self.x_history.append(body.x)
        if len(self.x_history) > 5:
            self.x_history.pop(0)
        filtered_x = float(np.median(np.asarray(self.x_history, dtype=np.float32)))
        offset = filtered_x - base.x
        target = -1 if offset <= -.10 else 1 if offset >= .10 else 0 if abs(offset) <= .045 else self.lateral
        if target != self.lateral:
            if target != self.candidate:
                self.candidate, self.candidate_frames = target, 1
            else:
                self.candidate_frames += 1
                if self.candidate_frames >= self.lateral_confirm_frames:
                    self.lateral, self.candidate = target, target
                    self.candidate_frames = 0
        else:
            self.candidate = target
            self.candidate_frames = 0

        rise_y, rise_bottom = base.y - body.y, base.bottom - body.bottom
        pose = rise_y >= self.jump_rise_y_min and rise_bottom >= self.jump_rise_bottom_min
        jump = pose and not self.jump_latched and now - self.last_jump >= .65
        if jump:
            self.jump_latched, self.last_jump = True, now
        elif rise_y < self.jump_rise_y_min * .45 and rise_bottom < self.jump_rise_bottom_min * .45:
            self.jump_latched = False
        arm_circle = self._arm_circle_pose(body, base)
        launch = False
        if arm_circle:
            self.start_pose_frames += 1
            self.start_rearm_count = 0
            if self.start_pose_frames >= self.start_confirm_frames and not self.start_latched:
                self.start_latched = True
                launch = True
        else:
            self.start_pose_frames = 0
            if self.start_latched:
                self.start_rearm_count += 1
                if self.start_rearm_count >= self.start_rearm_frames:
                    self.start_latched = False
                    self.start_rearm_count = 0
        self.last, self.last_time = body, now
        return InputState(
            lateral=self.lateral,
            jump=jump,
            body_present=True,
            calibrated=True,
            launch=launch,
            # パドル追従は、遅れの出る移動平均ではなく検出フレームの重心を使う。
            # filtered_x は従来どおり左右・開始ジェスチャーの安定判定にだけ使う。
            body_x=body.x,
        )


class PassbyStartDetector:
    """人物候補の出現を一度だけ開始イベントへ変換する。"""

    def __init__(self, confirm_frames: int = 4, rearm_frames: int = 15) -> None:
        self.confirm_frames = max(2, int(confirm_frames))
        self.rearm_frames = max(2, int(rearm_frames))
        self.present_frames = 0
        self.absent_frames = 0
        self.latched = False

    def reset(self) -> None:
        self.present_frames = 0
        self.absent_frames = 0
        self.latched = False

    def update(self, body_present: bool) -> bool:
        """人物が連続検知された瞬間だけTrueを返す。"""
        if body_present:
            self.present_frames += 1
            self.absent_frames = 0
            if not self.latched and self.present_frames >= self.confirm_frames:
                self.latched = True
                return True
            return False
        self.present_frames = 0
        if self.latched:
            self.absent_frames += 1
            if self.absent_frames >= self.rearm_frames:
                self.latched = False
                self.absent_frames = 0
        return False


class StillStartDetector:
    """同じ人物が一定位置に留まった時間を、開始イベントへ変換する。"""

    def __init__(self, hold_seconds: float = 3.0, position_tolerance: float = .035) -> None:
        if not math.isfinite(hold_seconds) or hold_seconds <= 0.0:
            raise ValueError("静止開始時間は0より大きい必要があります")
        if not math.isfinite(position_tolerance) or not 0.0 <= position_tolerance < 1.0:
            raise ValueError("静止開始の位置許容幅は0以上1未満にしてください")
        self.hold_seconds = float(hold_seconds)
        self.position_tolerance = float(position_tolerance)
        self.anchor_x: float | None = None
        self.started_at: float | None = None
        self.latched = False

    def reset(self) -> None:
        self.anchor_x = None
        self.started_at = None
        self.latched = False

    def update(self, body: BodyMeasurement | None, now: float) -> tuple[bool, float | None]:
        """静止を確定した1フレームだけTrue、保持中は残り秒数を返す。"""
        if body is None:
            self.reset()
            return False, None
        if self.anchor_x is None or self.started_at is None:
            self.anchor_x, self.started_at = body.x, now
            return False, self.hold_seconds
        if abs(body.x - self.anchor_x) > self.position_tolerance:
            # パドルを動かすほど横へ移動したら、今の位置を新たな静止開始点にする。
            self.anchor_x, self.started_at, self.latched = body.x, now, False
            return False, self.hold_seconds
        remaining = max(0.0, self.hold_seconds - (now - self.started_at))
        if not self.latched and remaining <= 0.0:
            self.latched = True
            return True, 0.0
        return False, remaining


@dataclass(frozen=True)
class PersonCandidate:
    """1フレームの人物候補。contour はデバッグ描画と確定マスク用。"""

    body: BodyMeasurement
    contour: np.ndarray
    depth_gain: float  # 背景との差分mm。大きいほどセンサー手前。


class ForegroundGate:
    """深度差分から、床・背景変化ではない人物候補を全員ぶん抽出する。"""

    # 実機フレームの下端約16%は床面。人物の上半身を残し、床帯だけを候補から外す。
    FLOOR_CUTOFF_RATIO = 0.84
    # センサー左右端は反射・無効深度のちらつきが出るため、人物候補から外す。
    SIDE_CUTOFF_RATIO = 0.15
    MIN_AREA_RATIO = 0.012
    MAX_AREA_RATIO = 0.60
    MIN_HEIGHT_RATIO = 0.25
    MIN_WIDTH_RATIO = 0.08
    MAX_WIDTH_RATIO = 0.65
    MIN_ASPECT_RATIO = 1.05
    MAX_ASPECT_RATIO = 8.0
    MIN_FILL_RATIO = 0.10
    MIN_DEPTH_GAIN_MM = 80.0
    MIN_PERSISTENCE = 3
    MAX_CENTER_STEP = 0.12

    def __init__(self, min_area: int) -> None:
        self.min_area = max(1, int(min_area))
        self.last_center: tuple[float, float] | None = None
        self.last_size: tuple[float, float] | None = None
        self.persistence = 0

    def reset(self) -> None:
        self.last_center = None
        self.last_size = None
        self.persistence = 0

    def detect_all(
        self,
        mask: np.ndarray,
        nearer: np.ndarray,
        threshold: float,
    ) -> tuple[list[PersonCandidate], np.ndarray]:
        """分離して見える人物候補をすべて返す。追跡の継続判定はPersonTrackerが行う。"""
        binary = np.asarray(mask, dtype=np.uint8).copy()
        depth_gain = np.asarray(nearer, dtype=np.float32)
        if binary.ndim != 2 or depth_gain.shape != binary.shape:
            raise ValueError("人物候補のマスクと深度差分の形状が一致しない")
        height, width = binary.shape
        floor_start = min(height, max(0, int(round(height * self.FLOOR_CUTOFF_RATIO))))
        binary[floor_start:, :] = 0
        side_start = min(width // 2, max(0, int(round(width * self.SIDE_CUTOFF_RATIO))))
        binary[:, :side_start] = 0
        binary[:, width - side_start:] = 0

        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        total = float(width * height)
        min_area = max(float(self.min_area), total * self.MIN_AREA_RATIO)
        candidates: list[PersonCandidate] = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            x, y, box_width, box_height = cv2.boundingRect(contour)
            area_ratio = area / total
            if area < min_area or area_ratio > self.MAX_AREA_RATIO:
                continue
            if box_height / height < self.MIN_HEIGHT_RATIO:
                continue
            if box_width / width < self.MIN_WIDTH_RATIO or box_width / width > self.MAX_WIDTH_RATIO:
                continue
            aspect_ratio = box_height / max(1.0, float(box_width))
            if aspect_ratio < self.MIN_ASPECT_RATIO or aspect_ratio > self.MAX_ASPECT_RATIO:
                continue
            fill_ratio = area / max(1.0, float(box_width * box_height))
            if fill_ratio < self.MIN_FILL_RATIO:
                continue
            moments = cv2.moments(contour)
            if moments["m00"] == 0:
                continue
            center_x = float(moments["m10"] / moments["m00"] / width)
            center_y = float(moments["m01"] / moments["m00"] / height)
            if not (0.04 <= center_x <= 0.96 and 0.04 <= center_y <= 0.96):
                continue
            component = np.zeros_like(binary)
            cv2.drawContours(component, [contour], -1, 255, -1)
            values = depth_gain[component > 0]
            if values.size == 0:
                continue
            median_gain = float(np.median(values))
            if median_gain < max(self.MIN_DEPTH_GAIN_MM, float(threshold) * 0.75):
                continue
            local_component = component[y:y + box_height, x:x + box_width] > 0
            upper_height = max(1, int(round(box_height * 0.72)))
            row_spans: list[int] = []
            for row in local_component[:upper_height]:
                columns = np.flatnonzero(row)
                if columns.size:
                    row_spans.append(int(columns[-1] - columns[0] + 1))
            upper_width = max(row_spans, default=0) / width
            candidates.append(
                PersonCandidate(
                    BodyMeasurement(
                        center_x,
                        center_y,
                        float(y + box_height) / height,
                        area_ratio,
                        box_width / width,
                        box_height / height,
                        upper_width,
                    ),
                    contour,
                    median_gain,
                )
            )
        return sorted(candidates, key=lambda candidate: candidate.body.x), binary

    def detect(
        self,
        mask: np.ndarray,
        nearer: np.ndarray,
        threshold: float,
    ) -> tuple[BodyMeasurement | None, np.ndarray | None, np.ndarray]:
        """単一候補向けの互換API。新規処理はdetect_all()とPersonTrackerを使う。"""
        candidates, binary = self.detect_all(mask, nearer, threshold)
        if not candidates:
            self.reset()
            return None, None, binary
        chosen = max(candidates, key=lambda candidate: candidate.body.area)
        body = chosen.body
        size = (body.width, body.height)
        if self.last_center is not None and self.last_size is not None:
            center_step = math.hypot(body.x - self.last_center[0], body.y - self.last_center[1])
            size_step = max(abs(size[0] - self.last_size[0]), abs(size[1] - self.last_size[1]))
            self.persistence = self.persistence + 1 if center_step <= self.MAX_CENTER_STEP and size_step <= .35 else 1
        else:
            self.persistence = 1
        self.last_center, self.last_size = (body.x, body.y), size
        return (body if self.persistence >= self.MIN_PERSISTENCE else None), chosen.contour, binary


@dataclass
class PersonTrack:
    """候補領域と個別の入力判定器を結び付けた、継続中の1人。"""

    track_id: int
    body: BodyMeasurement
    contour: np.ndarray
    depth_gain: float
    classifier: InputClassifier
    state: InputState
    first_seen: float
    last_seen: float
    seen_frames: int = 1
    visible: bool = True


class PersonTracker:
    """複数人物を近傍対応付けし、手前の一人をロックして追従する。"""

    MATCH_DISTANCE = .18
    MAX_MISSING_SECONDS = .70

    def __init__(self, classifier_options: dict[str, float | int]) -> None:
        self.classifier_options = dict(classifier_options)
        self.tracks: dict[int, PersonTrack] = {}
        self.active_id: int | None = None
        self.next_track_id = 1

    def reset(self) -> None:
        self.tracks.clear()
        self.active_id = None
        self.next_track_id = 1

    def release_active(self) -> None:
        """次球開始時に、手前の人を選び直せるよう現在のロックを解除する。"""
        self.active_id = None

    @staticmethod
    def _match_cost(track: PersonTrack, candidate: PersonCandidate) -> float:
        body = candidate.body
        center = math.hypot(body.x - track.body.x, body.y - track.body.y)
        size_change = max(abs(body.width - track.body.width), abs(body.height - track.body.height))
        # 交差時も、位置の近さを優先しつつ大きさの急変には小さな罰則を掛ける。
        return center + size_change * .25

    def _new_track(self, candidate: PersonCandidate, now: float) -> PersonTrack:
        classifier = InputClassifier(**self.classifier_options)
        state = classifier.update(candidate.body, now)
        track = PersonTrack(
            self.next_track_id,
            candidate.body,
            candidate.contour,
            candidate.depth_gain,
            classifier,
            state,
            now,
            now,
        )
        self.tracks[track.track_id] = track
        self.next_track_id += 1
        return track

    def update(self, candidates: list[PersonCandidate], now: float) -> tuple[PersonTrack | None, bool, int]:
        """現在の操作対象、対象交代フラグ、安定検出人数を返す。"""
        for track in self.tracks.values():
            track.visible = False
        pairs: list[tuple[float, int, int]] = []
        for track_id, track in self.tracks.items():
            for candidate_index, candidate in enumerate(candidates):
                cost = self._match_cost(track, candidate)
                if cost <= self.MATCH_DISTANCE:
                    pairs.append((cost, track_id, candidate_index))
        matched_tracks: set[int] = set()
        matched_candidates: set[int] = set()
        for _cost, track_id, candidate_index in sorted(pairs):
            if track_id in matched_tracks or candidate_index in matched_candidates:
                continue
            track = self.tracks[track_id]
            candidate = candidates[candidate_index]
            track.body, track.contour, track.depth_gain = candidate.body, candidate.contour, candidate.depth_gain
            track.last_seen, track.seen_frames, track.visible = now, track.seen_frames + 1, True
            track.state = track.classifier.update(candidate.body, now)
            matched_tracks.add(track_id)
            matched_candidates.add(candidate_index)
        for candidate_index, candidate in enumerate(candidates):
            if candidate_index not in matched_candidates:
                self._new_track(candidate, now)
        self.tracks = {
            track_id: track
            for track_id, track in self.tracks.items()
            if now - track.last_seen <= self.MAX_MISSING_SECONDS
        }
        stable_visible = [
            track
            for track in self.tracks.values()
            if track.visible and track.seen_frames >= ForegroundGate.MIN_PERSISTENCE
        ]
        previous_active = self.active_id
        active = self.tracks.get(self.active_id) if self.active_id is not None else None
        if active is None or not active.visible:
            if active is not None and now - active.last_seen <= self.MAX_MISSING_SECONDS:
            # 一瞬の隠れで、近くの別人へ操作権を奪われないようロックを維持する。
                return None, False, len(stable_visible)
            if stable_visible:
                # 開始時・ロック対象の離脱後は、背景との差分が最も大きい（最も手前の）人を選ぶ。
                self.active_id = max(
                    stable_visible,
                    key=lambda track: (track.depth_gain, -track.track_id),
                ).track_id
                active = self.tracks[self.active_id]
            else:
                self.active_id = None
                active = None
        changed = active is not None and self.active_id != previous_active
        return active, changed, len(stable_visible)


class SensorController:
    """STRUCTURE Sensorの深度背景差分 → 人物候補ゲート → 入力段。"""

    def __init__(
        self,
        width: int,
        height: int,
        background_seconds: float,
        min_area: int,
        roi: tuple[int, int, int, int] | None,
        jump_rise_y_min: float,
        jump_rise_bottom_min: float,
        depth_min_change_mm: float,
        capture: StructureSensorSource | None = None,
        start_mode: str = "still",
        passby_confirm_frames: int = 4,
        passby_rearm_frames: int = 15,
        still_seconds: float = 3.0,
        still_position_tolerance: float = .035,
        start_center_tolerance: float = 0.18,
        start_width_gain: float = 0.06,
        start_upper_width_gain: float = 0.04,
        start_upper_width_min: float = 0.30,
        start_area_gain: float = 0.05,
    ) -> None:
        if start_mode not in ("still", "passby", "arm-circle"):
            raise ValueError("開始モードはstill、passby、arm-circleのいずれか")
        self.capture = capture if capture is not None else StructureSensorSource(width, height, 60.0)
        self.start_mode = start_mode
        self.background_seconds = max(.2, background_seconds)
        self.min_area = max(1, min_area)
        self.roi = roi
        self.depth_min_change_mm = max(0.0, float(depth_min_change_mm))
        self.started: float | None = None
        self.depth_frames: list[np.ndarray] = []
        self.depth_background: np.ndarray | None = None
        self.depth_noise_p95 = 0.0
        self.foreground_gate = ForegroundGate(self.min_area)
        self.person_tracker = PersonTracker(
            {
                "jump_rise_y_min": jump_rise_y_min,
                "jump_rise_bottom_min": jump_rise_bottom_min,
                "start_center_tolerance": start_center_tolerance,
                "start_width_gain": start_width_gain,
                "start_upper_width_gain": start_upper_width_gain,
                "start_upper_width_min": start_upper_width_min,
                "start_area_gain": start_area_gain,
            }
        )
        self.passby_detector = PassbyStartDetector(passby_confirm_frames, passby_rearm_frames)
        self.still_detector = StillStartDetector(still_seconds, still_position_tolerance)
        self.debug: np.ndarray | None = None
        self.mask: np.ndarray | None = None
        # ForegroundGateで形状・継続判定まで通った、ゲーム入力と同一の確定領域。
        self.accepted_mask: np.ndarray | None = None
        self.depth_image: np.ndarray | None = None

    @property
    def stage(self) -> str:
        if self.started is None or time.monotonic() - self.started < self.background_seconds:
            return "BACKGROUND"
        if self.start_mode in ("still", "passby"):
            return "READY"
        active = self.person_tracker.tracks.get(self.person_tracker.active_id)
        return "READY" if active is not None and active.state.calibrated else "STANCE"

    def close(self) -> None:
        self.capture.close()

    def reselect_player(self) -> None:
        """残機減少後に操作対象を選び直し、次の人の静止開始を待つ。"""
        self.person_tracker.release_active()
        self.passby_detector.reset()
        self.still_detector.reset()

    def read(self, now: float) -> InputState:
        source = self.capture.read()
        if self.started is None:
            self.started = now
        if self.roi is not None:
            x, y, width, height = self.roi
            source_height, source_width = source.shape[:2]
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > source_width or y + height > source_height:
                raise ValueError(
                    f"ROI {x},{y},{width},{height} が深度画像 {source_width}x{source_height} を超える"
                )
            source = source[y:y + height, x:x + width]
        width = 240
        height = max(1, round(source.shape[0] * width / source.shape[1]))
        background_phase = now - self.started < self.background_seconds
        image = cv2.resize(source, (width, height), interpolation=cv2.INTER_NEAREST)
        self.depth_image = np.asarray(image).copy()
        if background_phase:
            self.depth_frames.append(np.asarray(image).copy())
        elif self.depth_background is None:
            if not self.depth_frames:
                raise RuntimeError("STRUCTURE Sensorの背景フレームがない")
            stack = np.asarray(self.depth_frames, dtype=np.float32)
            self.depth_background = np.median(stack, axis=0).astype(np.float32)
            valid_background = (stack > 0) & (self.depth_background[None, :, :] > 0)
            noise_values = np.abs(stack - self.depth_background[None, :, :])[valid_background]
            self.depth_noise_p95 = float(np.percentile(noise_values, 95.0)) if noise_values.size else 0.0
            self.depth_frames.clear()
            self.foreground_gate.reset()
        candidates: list[PersonCandidate] = []
        if background_phase or self.depth_background is None:
            mask = np.zeros(image.shape, dtype=np.uint8)
        else:
            depth = np.asarray(image, dtype=np.float32)
            background = self.depth_background
            valid = (depth > 0) & (background > 0)
            # 人物は背景より手前に現れるため、遠くなる変化は入力候補にしない。
            nearer = background - depth
            threshold = max(60.0, self.depth_noise_p95 * 3.0, self.depth_min_change_mm)
            mask = ((valid & (nearer >= threshold)).astype(np.uint8) * 255)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
            candidates, mask = self.foreground_gate.detect_all(mask, nearer, threshold)
        if background_phase:
            self.person_tracker.reset()
            self.passby_detector.reset()
            self.still_detector.reset()
            active, player_changed, people_detected = None, False, 0
        else:
            active, player_changed, people_detected = self.person_tracker.update(candidates, now)
        body = active.body if active is not None else None
        contour = active.contour if active is not None else None
        state = active.state if active is not None else InputState()
        accepted_mask = np.zeros_like(mask)
        if body is not None and contour is not None:
            cv2.drawContours(accepted_mask, [contour], -1, 255, -1)
        debug = depth_preview(image)
        for candidate in candidates:
            cv2.drawContours(debug, [candidate.contour], -1, (255, 170, 0), 1)
        if body is not None and contour is not None:
            cv2.drawContours(debug, [contour], -1, (0, 255, 0), 1)
            cv2.putText(
                debug,
                f"P{active.track_id}/{people_detected}",
                (max(0, int(body.x * width) - 20), max(30, int(body.y * height) - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                .42,
                (0, 255, 0),
                1,
                cv2.LINE_AA,
            )
        cv2.putText(debug, self.stage, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 230, 230), 1, cv2.LINE_AA)
        self.debug, self.mask, self.accepted_mask = debug, mask, accepted_mask
        if background_phase:
            return InputState()
        if player_changed:
            self.passby_detector.reset()
            self.still_detector.reset()
        if self.start_mode == "passby":
            self.still_detector.reset()
            start_trigger = self.passby_detector.update(body is not None)
            start_hold_remaining = None
        elif self.start_mode == "still":
            self.passby_detector.reset()
            start_trigger, start_hold_remaining = self.still_detector.update(body, now)
        else:
            self.passby_detector.reset()
            self.still_detector.reset()
            start_trigger = state.launch
            start_hold_remaining = None
        return InputState(
            lateral=state.lateral,
            jump=state.jump,
            body_present=state.body_present,
            calibrated=state.calibrated,
            launch=state.launch,
            start_trigger=start_trigger,
            body_x=state.body_x,
            start_hold_remaining=start_hold_remaining,
            player_id=active.track_id if active is not None else None,
            people_detected=people_detected,
            player_changed=player_changed,
        )

    def show_debug(self) -> None:
        if self.debug is not None:
            cv2.imshow("block breaker depth", self.debug)
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
    paddle_center_x: float | None = None


@dataclass(frozen=True)
class PaddleFollower:
    """人物の横位置を、バーの到達可能な中心座標へ写像する。"""

    canvas_width: float
    paddle_width: float
    play_range: tuple[float, float] = (.15, .85)
    deadzone: float = 3.0
    gain: float = .85
    mirror: bool = False

    def __post_init__(self) -> None:
        low, high = self.play_range
        if (
            not math.isfinite(self.canvas_width)
            or not math.isfinite(self.paddle_width)
            or self.canvas_width <= self.paddle_width
            or not math.isfinite(low)
            or not math.isfinite(high)
            or not 0.0 <= low < high <= 1.0
            or not math.isfinite(self.deadzone)
            or self.deadzone < 0.0
            or not math.isfinite(self.gain)
            or not 0.0 < self.gain <= 1.0
        ):
            raise ValueError("パドル追従の範囲・不感帯・ゲインが不正")

    def target_center(self, body_x: float) -> float:
        """ROI内の人物中心を、バーが端まで届く中心座標へ変換する。"""
        if not math.isfinite(float(body_x)):
            raise ValueError("人物中心Xが不正")
        normalized = 1.0 - float(body_x) if self.mirror else float(body_x)
        low, high = self.play_range
        progress = min(1.0, max(0.0, (normalized - low) / (high - low)))
        left_center = self.paddle_width / 2.0
        right_center = self.canvas_width - left_center
        return left_center + (right_center - left_center) * progress

    def follow(self, body_x: float, current_center: float) -> float:
        """不感帯の外だけを高ゲインで追い、静止時の小さな揺れを止める。"""
        if not math.isfinite(float(current_center)):
            raise ValueError("現在のパドル中心Xが不正")
        target = self.target_center(body_x)
        delta = target - float(current_center)
        if abs(delta) <= self.deadzone:
            return float(current_center)
        return float(current_center) + math.copysign((abs(delta) - self.deadzone) * self.gain, delta)


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


class MacKeyboardState:
    """macOSのCoreGraphicsで、キーリピートに依存せず押下状態を読む。"""

    # macOSの仮想キーコード。矢印に加え、A/D/H/Lも受け付ける。
    _LEFT_CODES = (123, 0, 4)   # ←, A, H
    _RIGHT_CODES = (124, 2, 37)  # →, D, L

    def __init__(self) -> None:
        self._lib = None
        self._key_state = None
        if sys.platform != "darwin":
            return
        library = ctypes.util.find_library("CoreGraphics") or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        try:
            lib = ctypes.CDLL(library)
            key_state = lib.CGEventSourceKeyState
            key_state.argtypes = [ctypes.c_int, ctypes.c_uint16]
            key_state.restype = ctypes.c_bool
            self._lib, self._key_state = lib, key_state
        except (AttributeError, OSError):
            self.close()

    @property
    def available(self) -> bool:
        return self._key_state is not None

    def lateral(self) -> int:
        if not self.available:
            return 0
        pressed = lambda codes: any(self._key_state(0, code) for code in codes)
        left, right = pressed(self._LEFT_CODES), pressed(self._RIGHT_CODES)
        return -1 if left and not right else 1 if right and not left else 0

    def close(self) -> None:
        self._lib = self._key_state = None


def create_keyboard_state() -> MacKeyboardState | X11KeyboardState:
    """実行OSに合わせ、長押しを毎フレーム取得できる入力バックエンドを選ぶ。"""
    mac = MacKeyboardState()
    if mac.available:
        return mac
    x11 = X11KeyboardState()
    return x11


class BlockBreaker:
    paddle_width, paddle_height, paddle_y = 42.0, 6.0, 350.0
    ball_radius, paddle_speed, initial_speed = 3.0, 175.0, 175.0
    boss_scale = 1.20
    boss_y = 34.0  # 上端のプレイ領域(y=26)との間に8pxを残す。
    boss_max_hp, boss_damage = 100, 10
    damage_effect_duration = .24
    boss_transition_duration = .48  # 3回の点滅（点灯・消灯を3周期）
    boss_move_speed = 52.0
    clear_delay = 1.8
    beam_period = 3.6
    beam_warning_duration = 1.50
    beam_active_duration = .10

    def __init__(
        self,
        boss_profile: BossProfile | None = None,
        boss_image: Path | None = None,
        auto_reset_on_clear: bool = True,
    ) -> None:
        if boss_profile is None:
            if boss_image is None:
                boss_profile = BOSS_PROFILES[0]
            else:
                boss_profile = next((profile for profile in BOSS_PROFILES if profile.image == boss_image), None)
                if boss_profile is None:
                    raise ValueError(f"未登録のボス画像: {boss_image}")
        self.boss_profile = boss_profile
        self.auto_reset_on_clear = auto_reset_on_clear
        self.boss_image = boss_profile.image
        self.boss_max_hp = boss_profile.max_hp
        self.boss_move_speed = boss_profile.move_speed
        self.boss_damage = boss_profile.damage_per_hit
        sprite, mask = load_boss_sprite(self.boss_image)
        size = (round(sprite.shape[1] * self.boss_scale), round(sprite.shape[0] * self.boss_scale))
        self._base_boss_sprite = cv2.resize(sprite, size, interpolation=cv2.INTER_NEAREST)
        self._base_boss_mask = cv2.resize(mask.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST) > 0
        self.boss_sprite = self._base_boss_sprite
        self.boss_mask = self._base_boss_mask
        self._orientation_flipped = False
        self.boss_height, self.boss_width = self.boss_sprite.shape
        self.boss_x = (CANVAS_WIDTH - self.boss_width) / 2
        eroded = cv2.erode(self.boss_mask.astype(np.uint8), np.ones((3, 3), np.uint8))
        self.boss_edge = self.boss_mask & (eroded == 0)
        self.boss_edge_points = np.argwhere(self.boss_edge)
        self.lives = 3
        self.boss_hp = self.boss_max_hp
        self.barrier_hits = 0
        self.barrier_broken = not bool(boss_profile.barrier_hits_required)
        self.critical_hits = 0
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
        self.life_loss_event = False
        self.boss_transition_remaining = 0.0
        self.boss_move_active = False
        self.boss_move_vx = self.boss_move_speed
        self.boss_move_phase = 0.0
        self.boss_base_x = self.boss_x
        self.boss_base_y = self.boss_y
        self.beam_phase = 0.0
        self.clear_remaining = 0.0
        self.game_over_until = 0.0
        self.reset(full=True)

    def _set_boss_orientation(self) -> None:
        """横顔ボスだけ、現在位置から画面中央を向くように左右反転する。"""
        if not self.boss_profile.flip_horizontal:
            return
        should_flip = self.boss_x + self.boss_width / 2.0 < CANVAS_WIDTH / 2.0
        if should_flip == self._orientation_flipped:
            return
        self._orientation_flipped = should_flip
        if should_flip:
            self.boss_sprite = np.ascontiguousarray(np.fliplr(self._base_boss_sprite))
            self.boss_mask = np.ascontiguousarray(np.fliplr(self._base_boss_mask))
        else:
            self.boss_sprite = self._base_boss_sprite
            self.boss_mask = self._base_boss_mask
        eroded = cv2.erode(self.boss_mask.astype(np.uint8), np.ones((3, 3), np.uint8))
        self.boss_edge = self.boss_mask & (eroded == 0)
        self.boss_edge_points = np.argwhere(self.boss_edge)

    def reset(self, full: bool = False) -> None:
        if full:
            self.lives = 3
            self.boss_hp = self.boss_max_hp
            self.barrier_hits = 0
            self.barrier_broken = not bool(self.boss_profile.barrier_hits_required)
            self.critical_hits = 0
            self.boss_defeated = False
            self.game_started = False
            self.damage_effect_remaining = 0.0
            self.life_loss_feedback_active = False
            self.life_loss_blink_elapsed = 0.0
            self.life_loss_slot = -1
            self.life_loss_event = False
            self.boss_x = (CANVAS_WIDTH - self.boss_width) / 2
            self.boss_y = 34.0
            self.boss_base_x = self.boss_x
            self.boss_base_y = self.boss_y
            self.boss_transition_remaining = 0.0
            # ボスは登場直後から動き、HP半分到達時の点滅演出とは独立させる。
            self.boss_move_active = True
            self.boss_move_vx = self.boss_move_speed
            self.boss_move_phase = 0.0
            self.beam_phase = 0.0
            self._set_boss_orientation()
            self.clear_remaining = 0.0
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
        self.beam_phase = 0.0
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

    def _critical_point(self) -> tuple[float, float]:
        """稲葉の頭上に置く急所の中心座標。"""
        return self.boss_x + self.boss_width / 2.0, max(26.0, self.boss_y - 5.0)

    def _ball_hits_critical(self) -> bool:
        if not self.boss_profile.critical_hits_required:
            return False
        critical_x, critical_y = self._critical_point()
        reach = self.ball_radius + 2.0
        return (self.ball.x - critical_x) ** 2 + (self.ball.y - critical_y) ** 2 <= reach ** 2

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
        body_overlap = self._ball_overlaps_boss()
        critical_overlap = self._ball_hits_critical()
        overlaps = body_overlap or critical_overlap
        if not self.boss_collision_armed:
            if not overlaps:
                self.boss_collision_armed = True
            return False
        if not overlaps or self.boss_defeated:
            return False
        if critical_overlap and not body_overlap:
            critical_x, critical_y = self._critical_point()
            nx, ny, contact_x, contact_y = 0.0, -1.0, critical_x, critical_y
        else:
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
        self.damage_effect_remaining = self.damage_effect_duration
        profile = self.boss_profile
        if profile.barrier_hits_required and not self.barrier_broken:
            self.barrier_hits += 1
            if self.barrier_hits >= profile.barrier_hits_required:
                self.barrier_broken = True
            return True
        if profile.critical_hits_required:
            if not critical_overlap:
                return True
            self.critical_hits += 1
        self.boss_hp = max(0, self.boss_hp - self.boss_damage)
        if self.boss_hp <= self.boss_max_hp // 2 and self.boss_transition_remaining <= 0.0:
            self.boss_transition_remaining = self.boss_transition_duration
        if self.boss_hp == 0:
            self.boss_defeated = True
            self.serving = True
            self.ball.vx = self.ball.vy = 0.0
            self.clear_remaining = self.clear_delay
        return True

    def _lose_ball(self, now: float) -> None:
        self.lives -= 1
        # main() が次球用カウントダウンを始めるための一度きりのイベント。
        self.life_loss_event = self.lives > 0
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
        if self.boss_profile.barrier_hits_required and not self.barrier_broken:
            self.barrier_hits = 0
        self.ball.vx = self.ball.vy = 0.0
        self._place_ball_at_mouth()

    def consume_life_loss_event(self) -> bool:
        """残機が残るミスを一度だけ取得する。ゲームオーバーではFalse。"""
        event = self.life_loss_event
        self.life_loss_event = False
        return event

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
            profile = self.boss_profile
            if profile.movement == "figure8":
                self.boss_move_phase = (self.boss_move_phase + profile.move_speed * dt) % (2 * math.pi)
                x_amplitude = max(8.0, (CANVAS_WIDTH - self.boss_width) * .46)
                y_amplitude = min(26.0, max(8.0, self.boss_height * .24))
                self.boss_x = self.boss_base_x + x_amplitude * math.sin(self.boss_move_phase)
                self.boss_y = self.boss_base_y + y_amplitude * math.sin(2.0 * self.boss_move_phase)
                self.boss_x = min(max(0.0, self.boss_x), CANVAS_WIDTH - self.boss_width)
                self.boss_y = min(max(26.0, self.boss_y), CANVAS_HEIGHT - self.boss_height - 4.0)
            else:
                self.boss_x += self.boss_move_vx * dt
                if self.boss_x <= 0.0:
                    self.boss_x, self.boss_move_vx = 0.0, abs(self.boss_move_vx)
                elif self.boss_x + self.boss_width >= CANVAS_WIDTH:
                    self.boss_x = CANVAS_WIDTH - self.boss_width
                    self.boss_move_vx = -abs(self.boss_move_vx)
                if profile.movement == "vibrate_horizontal":
                    self.boss_move_phase = (self.boss_move_phase + profile.vibration_frequency * dt) % (2 * math.pi)
                    amplitude = profile.vibration_amplitude or min(20.0, self.boss_height * .20)
                    self.boss_y = self.boss_base_y + amplitude * math.sin(self.boss_move_phase)
                    self.boss_y = min(max(26.0, self.boss_y), CANVAS_HEIGHT - self.boss_height - 4.0)
            self._set_boss_orientation()
        if self.boss_defeated:
            self.clear_remaining = max(0.0, self.clear_remaining - dt)
            if self.clear_remaining == 0.0 and self.auto_reset_on_clear:
                self.reset(full=True)
            return
        if self.game_over_until:
            if now >= self.game_over_until:
                self.game_over_until = 0.0
                self.reset(full=True)
            return
        if self.boss_defeated:
            return
        if controls.paddle_center_x is not None and math.isfinite(float(controls.paddle_center_x)):
            self.paddle_x = min(
                max(0.0, float(controls.paddle_center_x) - self.paddle_width / 2),
                CANVAS_WIDTH - self.paddle_width,
            )
        else:
            self.paddle_x = min(
                max(0.0, self.paddle_x + max(-1, min(1, controls.lateral)) * self.paddle_speed * dt),
                CANVAS_WIDTH - self.paddle_width,
            )
        if self.serving:
            self._place_ball_at_mouth()
            if controls.launch:
                self._launch()
            return
        if self.boss_profile.beam_attack:
            self.beam_phase = (self.beam_phase + dt) % self.beam_period
            beam_active = self.beam_phase >= self.beam_period - self.beam_active_duration
            if beam_active:
                beam_x = self.boss_x + self.boss_width / 2.0
                if self.paddle_x - self.ball_radius <= beam_x <= self.paddle_x + self.paddle_width + self.ball_radius:
                    self._lose_ball(now)
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

    @classmethod
    def _center_text(cls, frame: np.ndarray, text: str, y: int, color: int, scale: float) -> None:
        (width, _height), _baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        x = max(0, (CANVAS_WIDTH - width) // 2)
        cls._text(frame, text, (x, y), color, scale)

    def render(
        self,
        sensor_stage: str,
        boundaries: bool = False,
        countdown: int | None = None,
        start_mode: str = "arm-circle",
        start_hold_remaining: float | None = None,
    ) -> np.ndarray:
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
            # 点灯→消灯を3周期。ボスの移動は登場時から継続する。
            phase = int((self.boss_transition_duration - self.boss_transition_remaining) / (self.boss_transition_duration / 6))
            if phase in (0, 2, 4):
                boss_region[self.boss_mask] = FC6_WHITE
        elif self.damage_effect_remaining > 0.0:
            # 点灯→消灯→点灯→消灯の4区間で、ボスを2回だけ点滅させる。
            phase = int((self.damage_effect_duration - self.damage_effect_remaining) / (self.damage_effect_duration / 4))
            if phase in (0, 2):
                boss_region[self.boss_mask] = FC6_WHITE
        if self.boss_profile.barrier_hits_required and not self.barrier_broken:
            barrier_color = 0x16 if (self.barrier_hits + int(time.monotonic() * 8)) % 2 else 0x0E
            shield_center = (boss_x + self.boss_width // 2, boss_y + self.boss_height // 2)
            shield_radius = max(self.boss_width, self.boss_height) // 2 + 7
            cv2.circle(frame, shield_center, shield_radius, barrier_color, 2, lineType=cv2.LINE_8)
            self._center_text(frame, f"BARRIER {self.barrier_hits}/{self.boss_profile.barrier_hits_required}", 174, TEXT, .34)
        if self.boss_profile.critical_hits_required and not self.boss_defeated:
            critical_x, critical_y = self._critical_point()
            marker_color = 0x06 if int(time.monotonic() * 7) % 2 else 0x16
            cv2.drawMarker(
                frame,
                (int(round(critical_x)), int(round(critical_y))),
                marker_color,
                cv2.MARKER_DIAMOND,
                7,
                1,
                line_type=cv2.LINE_8,
            )
            self._center_text(frame, f"WEAK {self.critical_hits}/{self.boss_profile.critical_hits_required}", 174, TEXT, .34)
        if self.boss_profile.beam_attack and self.game_started and not self.serving and not self.boss_defeated:
            beam_warning = self.beam_phase >= self.beam_period - self.beam_warning_duration
            beam_active = self.beam_phase >= self.beam_period - self.beam_active_duration
            if beam_warning:
                beam_x = int(round(self.boss_x + self.boss_width / 2.0))
                beam_top = max(26, boss_y + self.boss_height)
                beam_bottom = int(round(self.paddle_y))
                if beam_active:
                    frame[beam_top:beam_bottom, max(0, beam_x - 2):min(CANVAS_WIDTH, beam_x + 3)] = 0x06
                    frame[beam_top:beam_bottom, max(0, beam_x - 4):min(CANVAS_WIDTH, beam_x - 2)] = 0x0E
                    frame[beam_top:beam_bottom, max(0, beam_x + 3):min(CANVAS_WIDTH, beam_x + 5)] = 0x0E
                else:
                    for line_y in range(beam_top, beam_bottom, 8):
                        frame[line_y:line_y + 2, max(0, beam_x - 1):min(CANVAS_WIDTH, beam_x + 2)] = 0x0E
        # 名前は登場〜最初のスタート待ちだけ表示し、プレイ中の情報量を抑える。
        if self.serving and not self.game_started and not self.boss_defeated:
            self._center_text(frame, self.boss_profile.display_name, 150, TEXT, .42)
        x = int(round(self.paddle_x))
        frame[int(self.paddle_y):int(self.paddle_y + self.paddle_height), x:x + int(self.paddle_width)] = PADDLE
        frame[int(self.paddle_y):int(self.paddle_y + 2), x:x + int(self.paddle_width)] = PADDLE_EDGE
        if active_play:
            cv2.circle(frame, (int(round(self.ball.x)), int(round(self.ball.y))), int(self.ball_radius), int(BALL), -1, lineType=cv2.LINE_8)
        if self.boss_defeated:
            self._center_text(frame, "BOSS DOWN", 228, 0x12, .68)
        elif self.game_over_until:
            self._center_text(frame, "GAME OVER", 228, 0x06, .72)
        elif self.serving:
            if countdown is not None and countdown > 0:
                self._center_text(frame, f"START IN {countdown}", 232, 0x0E, .58)
            else:
                if start_mode == "still":
                    prompt = "STAND STILL TO START"
                elif start_mode == "passby":
                    prompt = "WALK PAST TO START"
                else:
                    prompt = "ARMS CIRCLE TO LAUNCH"
                self._center_text(frame, prompt if sensor_stage == "READY" else "SENSOR CAL", 232, 0x0E, .42)
            if sensor_stage == "BACKGROUND":
                self._center_text(frame, "CLEAR SENSOR", 252, TEXT, .40)
            elif sensor_stage == "STANCE":
                self._center_text(frame, "STAND STILL", 252, TEXT, .40)
            elif start_mode == "still" and start_hold_remaining is not None:
                self._center_text(frame, f"HOLD {math.ceil(start_hold_remaining)}", 252, TEXT, .40)
        if boundaries:
            for y in (96, 192, 288):
                frame[y:y + 1, :] = DIM
        return frame

@dataclass
class ClassicBlock:
    x: float
    y: float
    width: float
    height: float
    color: int


class ClassicThenBoss:
    """穴あき通常面のあと、通常ボス3体→最終ボスの連戦へ進むゲーム本体。"""

    paddle_width, paddle_height, paddle_y = BlockBreaker.paddle_width, BlockBreaker.paddle_height, BlockBreaker.paddle_y
    ball_radius, paddle_speed, initial_speed = BlockBreaker.ball_radius, BlockBreaker.paddle_speed, BlockBreaker.initial_speed
    stage_clear_delay = 1.8
    warning_seconds = 1.5
    final_warning_seconds = 1.5
    victory_seconds = 4.0
    normal_boss_count = 3
    classic_rows = 4
    classic_columns = 5
    # 最初から縦に抜いた通路。ボールが奥まで入りやすく、連続してブロックを崩せる。
    opening_columns = frozenset((1, 3))
    block_colors = (0x00, 0x05, 0x0A, 0x0E, 0x12, 0x16, 0x1E, 0x22, 0x29, 0x2D)

    def __init__(self) -> None:
        self.phase = "classic"
        self.blocks: list[ClassicBlock] = []
        self.boss: BlockBreaker | None = None
        self.score = 0
        self.lives = 3
        self.ball = Ball(0.0, 0.0)
        self._paddle_x = 0.0
        self._serving = True
        self._game_started = False
        self._game_over_until = 0.0
        self._life_loss_event = False
        self._stage_start_request = False
        self.stage_clear_remaining = 0.0
        self.warning_remaining = 0.0
        self.warning_duration = self.warning_seconds
        self.boss_order: list[BossProfile] = []
        self.boss_index = -1
        self.victory_remaining = 0.0
        self.reset(full=True)

    @property
    def paddle_x(self) -> float:
        return self.boss.paddle_x if self.boss is not None else self._paddle_x

    @paddle_x.setter
    def paddle_x(self, value: float) -> None:
        if self.boss is not None:
            self.boss.paddle_x = value
        else:
            self._paddle_x = value

    @property
    def serving(self) -> bool:
        return self.boss.serving if self.boss is not None else self._serving

    @property
    def game_started(self) -> bool:
        if self.phase == "warning":
            return True
        return self.boss.game_started if self.boss is not None else self._game_started

    @property
    def game_over_until(self) -> float:
        return self.boss.game_over_until if self.boss is not None else self._game_over_until

    def _make_blocks(self) -> list[ClassicBlock]:
        columns, rows, margin, gap = self.classic_columns, self.classic_rows, 8, 2
        width = (CANVAS_WIDTH - margin * 2 - gap * (columns - 1)) / columns
        return [
            ClassicBlock(
                margin + column * (width + gap),
                48 + row * 14,
                width,
                12,
                self.block_colors[(row + column) % len(self.block_colors)],
            )
            for row in range(rows)
            for column in range(columns)
            if column not in self.opening_columns
        ]

    def reset(self, full: bool = False) -> None:
        self.phase = "classic"
        self.boss = None
        self.score = 0
        self.lives = 3
        self._paddle_x = (CANVAS_WIDTH - self.paddle_width) / 2
        self._serving = True
        self._game_started = False
        self._game_over_until = 0.0
        self._life_loss_event = False
        self._stage_start_request = False
        self.stage_clear_remaining = 0.0
        self.warning_remaining = 0.0
        self.warning_duration = self.warning_seconds
        self.boss_order = []
        self.boss_index = -1
        self.victory_remaining = 0.0
        self.blocks = self._make_blocks()
        self._place_ball_on_paddle()

    def _place_ball_on_paddle(self) -> None:
        self.ball = Ball(self._paddle_x + self.paddle_width / 2, self.paddle_y - self.ball_radius - 1)

    def _move_paddle(self, dt: float, controls: GameInput) -> None:
        if controls.paddle_center_x is not None and math.isfinite(float(controls.paddle_center_x)):
            self._paddle_x = min(
                max(0.0, float(controls.paddle_center_x) - self.paddle_width / 2),
                CANVAS_WIDTH - self.paddle_width,
            )
        else:
            self._paddle_x = min(
                max(0.0, self._paddle_x + max(-1, min(1, controls.lateral)) * self.paddle_speed * dt),
                CANVAS_WIDTH - self.paddle_width,
            )

    def _launch_classic(self) -> None:
        self._serving = False
        self._game_started = True
        self.ball.vx, self.ball.vy = self.initial_speed * .52, -self.initial_speed * .86

    def _hit_block(self, block: ClassicBlock) -> bool:
        near_x = min(max(self.ball.x, block.x), block.x + block.width)
        near_y = min(max(self.ball.y, block.y), block.y + block.height)
        dx, dy = self.ball.x - near_x, self.ball.y - near_y
        if dx * dx + dy * dy > self.ball_radius ** 2:
            return False
        if abs(dx) > abs(dy):
            self.ball.vx = -self.ball.vx
        else:
            self.ball.vy = -self.ball.vy
        return True

    def _bounce_edges(self) -> None:
        ball = self.ball
        if ball.x - self.ball_radius < 0 or ball.x + self.ball_radius >= CANVAS_WIDTH:
            ball.x = min(max(self.ball_radius, ball.x), CANVAS_WIDTH - self.ball_radius - 1)
            ball.vx = -ball.vx
        if ball.y - self.ball_radius < 26:
            ball.y, ball.vy = 26 + self.ball_radius, abs(ball.vy)
        if (
            ball.vy > 0
            and ball.y + self.ball_radius >= self.paddle_y
            and ball.y - self.ball_radius <= self.paddle_y + self.paddle_height
            and self._paddle_x - self.ball_radius <= ball.x <= self._paddle_x + self.paddle_width + self.ball_radius
        ):
            ball.y = self.paddle_y - self.ball_radius - 1
            hit = (ball.x - (self._paddle_x + self.paddle_width / 2)) / (self.paddle_width / 2)
            speed = min(300, math.hypot(ball.vx, ball.vy) * 1.015)
            ball.vx = speed * hit * .92
            ball.vy = -max(80, math.sqrt(max(1, speed * speed - ball.vx * ball.vx)))

    def _lose_classic_ball(self, now: float) -> None:
        self.lives -= 1
        self._life_loss_event = self.lives > 0
        self._serving = True
        self.ball.vx = self.ball.vy = 0.0
        if self.lives <= 0:
            self._game_over_until = now + 1.8
        self._place_ball_on_paddle()

    def _start_random_boss(self) -> None:
        normal_profiles = tuple(profile for profile in BOSS_PROFILES if not profile.final_boss)
        final_profiles = tuple(profile for profile in BOSS_PROFILES if profile.final_boss)
        count = min(self.normal_boss_count, len(normal_profiles))
        self.boss_order = list(random.sample(normal_profiles, count)) + list(final_profiles)
        self.boss_index = 0
        self._start_boss(self.boss_order[self.boss_index])

    def _start_boss(self, profile: BossProfile) -> None:
        self.phase = "warning"
        self.warning_duration = self.final_warning_seconds if profile.final_boss else self.warning_seconds
        self.warning_remaining = self.warning_duration
        self.lives = 3
        self.boss = BlockBreaker(boss_profile=profile, auto_reset_on_clear=False)

    def _advance_boss(self) -> None:
        if self.boss_index + 1 < len(self.boss_order):
            self.boss_index += 1
            self._start_boss(self.boss_order[self.boss_index])
            return
        self.phase = "victory"
        self.warning_remaining = 0.0
        self.victory_remaining = self.victory_seconds
        self._stage_start_request = False

    def _finish_warning(self) -> None:
        self.phase = "boss"
        self.warning_remaining = 0.0
        self._stage_start_request = True

    def consume_life_loss_event(self) -> bool:
        if self.boss is not None:
            event = self.boss.consume_life_loss_event()
            self.lives = self.boss.lives
            return event
        event = self._life_loss_event
        self._life_loss_event = False
        return event

    def consume_stage_start_request(self) -> bool:
        event = self._stage_start_request
        self._stage_start_request = False
        return event

    def step(self, dt: float, controls: GameInput, now: float) -> None:
        dt = min(.04, max(0.0, dt))
        if self.phase == "warning":
            self.warning_remaining = max(0.0, self.warning_remaining - dt)
            if self.warning_remaining == 0.0:
                self._finish_warning()
            return
        if self.phase == "victory":
            self.victory_remaining = max(0.0, self.victory_remaining - dt)
            if self.victory_remaining == 0.0:
                self.reset(full=True)
            return
        if self.boss is not None:
            if self.boss.game_over_until and now >= self.boss.game_over_until:
                self.reset(full=True)
                return
            self.boss.step(dt, controls, now)
            if self.boss.boss_defeated and self.boss.clear_remaining == 0.0:
                self._advance_boss()
            return
        if self.phase == "transition":
            self.stage_clear_remaining = max(0.0, self.stage_clear_remaining - dt)
            if self.stage_clear_remaining == 0.0:
                self._start_random_boss()
            return
        if self._game_over_until:
            if now >= self._game_over_until:
                self.reset(full=True)
            return
        self._move_paddle(dt, controls)
        if self._serving:
            self._place_ball_on_paddle()
            if controls.launch:
                self._launch_classic()
            return
        steps = max(1, min(8, math.ceil(math.hypot(self.ball.vx * dt, self.ball.vy * dt) / 2)))
        for _ in range(steps):
            self.ball.x += self.ball.vx * dt / steps
            self.ball.y += self.ball.vy * dt / steps
            self._bounce_edges()
            for index, block in enumerate(self.blocks):
                if self._hit_block(block):
                    del self.blocks[index]
                    self.score += 10
                    break
            if not self.blocks:
                self.phase = "transition"
                self.stage_clear_remaining = self.stage_clear_delay
                self._serving = True
                self.ball.vx = self.ball.vy = 0.0
                return
            if self.ball.y - self.ball_radius > CANVAS_HEIGHT:
                self._lose_classic_ball(now)
                return

    def _render_classic(
        self,
        sensor_stage: str,
        boundaries: bool,
        countdown: int | None,
        start_mode: str,
        start_hold_remaining: float | None,
    ) -> np.ndarray:
        frame = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), SKY, np.uint8)
        for index in range(30):
            frame[30 + (index * 71 + 13) % 300, (index * 47 + 19) % CANVAS_WIDTH] = SKY_DOT
        frame[22:24, :] = DIM
        BlockBreaker._text(frame, f"SCORE {self.score:05d}", (6, 17), TEXT, .38)
        for life in range(self.lives):
            cv2.circle(frame, (164 + life * 11, 12), 3, int(TEXT), -1, lineType=cv2.LINE_8)
        for block in self.blocks:
            x0, y0 = int(round(block.x)), int(round(block.y))
            x1, y1 = int(round(block.x + block.width)), int(round(block.y + block.height))
            frame[y0:y1, x0:x1] = block.color
            frame[y0:y0 + 2, x0:x1] = TEXT
        x = int(round(self._paddle_x))
        frame[int(self.paddle_y):int(self.paddle_y + self.paddle_height), x:x + int(self.paddle_width)] = PADDLE
        frame[int(self.paddle_y):int(self.paddle_y + 2), x:x + int(self.paddle_width)] = PADDLE_EDGE
        if self.phase != "transition" and not self._game_over_until:
            cv2.circle(frame, (int(round(self.ball.x)), int(round(self.ball.y))), int(self.ball_radius), int(BALL), -1, lineType=cv2.LINE_8)
        if self.phase == "transition":
            BlockBreaker._center_text(frame, "STAGE CLEAR", 228, 0x12, .62)
        elif self._game_over_until:
            BlockBreaker._center_text(frame, "GAME OVER", 228, 0x06, .72)
        elif self._serving:
            if countdown is not None and countdown > 0:
                BlockBreaker._center_text(frame, f"START IN {countdown}", 232, 0x0E, .58)
            else:
                prompt = "STAND STILL TO START" if start_mode == "still" else "WALK PAST TO START" if start_mode == "passby" else "ARMS CIRCLE TO LAUNCH"
                BlockBreaker._center_text(frame, prompt if sensor_stage == "READY" else "SENSOR CAL", 232, 0x0E, .42)
            if sensor_stage == "BACKGROUND":
                BlockBreaker._center_text(frame, "CLEAR SENSOR", 252, TEXT, .40)
            elif sensor_stage == "STANCE":
                BlockBreaker._center_text(frame, "STAND STILL", 252, TEXT, .40)
            elif start_mode == "still" and start_hold_remaining is not None:
                BlockBreaker._center_text(frame, f"HOLD {math.ceil(start_hold_remaining)}", 252, TEXT, .40)
        if boundaries:
            for y in (96, 192, 288):
                frame[y:y + 1, :] = DIM
        return frame

    def _render_warning(self) -> np.ndarray:
        frame = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), SKY, np.uint8)
        for index in range(30):
            frame[30 + (index * 71 + 13) % 300, (index * 47 + 19) % CANVAS_WIDTH] = SKY_DOT
        elapsed = self.warning_duration - self.warning_remaining
        final_boss = self.boss is not None and self.boss.boss_profile.final_boss
        blink = int(elapsed / (.12 if final_boss else .20)) % 2 == 0
        border = 5 if final_boss and blink else 3 if blink else 1
        frame[:border, :] = 0x04
        frame[-border:, :] = 0x04
        frame[:, :border] = 0x04
        frame[:, -border:] = 0x04
        if self.boss is not None:
            if final_boss:
                # 最終ボスは中央に立ち絵を出し、走査線とFINAL表示を重ねて登場感を出す。
                sprite = self.boss.boss_sprite
                mask = self.boss.boss_mask
                x = (CANVAS_WIDTH - self.boss.boss_width) // 2
                y = 38
                region = frame[y:y + self.boss.boss_height, x:x + self.boss.boss_width]
                region[mask] = sprite[mask]
                accent = 0x16 if blink else 0x0E
                for line_y in range(30, 184, 12):
                    frame[line_y:line_y + 1, 8:CANVAS_WIDTH - 8] = accent
                BlockBreaker._center_text(frame, "FINAL BOSS", 218, TEXT if blink else 0x06, .60)
                BlockBreaker._center_text(frame, self.boss.boss_profile.display_name, 252, TEXT, .46)
            else:
                BlockBreaker._center_text(frame, "WARNING", 206, TEXT if blink else 0x06, .78)
                BlockBreaker._center_text(frame, self.boss.boss_profile.display_name, 246, TEXT, .48)
        else:
            BlockBreaker._center_text(frame, "WARNING", 206, TEXT if blink else 0x06, .78)
        return frame

    def _render_victory(self) -> np.ndarray:
        frame = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), SKY, np.uint8)
        for index in range(30):
            frame[30 + (index * 71 + 13) % 300, (index * 47 + 19) % CANVAS_WIDTH] = SKY_DOT
        elapsed = self.victory_seconds - self.victory_remaining
        pulse = int(elapsed / .16) % 2 == 0
        border = 4 if pulse else 2
        frame[:border, :] = 0x16
        frame[-border:, :] = 0x16
        frame[:, :border] = 0x16
        frame[:, -border:] = 0x16
        BlockBreaker._center_text(frame, "ALL BOSSES DOWN", 184, TEXT if pulse else 0x0E, .56)
        BlockBreaker._center_text(frame, "STAGE COMPLETE", 222, TEXT, .48)
        return frame

    def render(
        self,
        sensor_stage: str,
        boundaries: bool = False,
        countdown: int | None = None,
        start_mode: str = "arm-circle",
        start_hold_remaining: float | None = None,
    ) -> np.ndarray:
        if self.phase == "warning":
            return self._render_warning()
        if self.phase == "victory":
            return self._render_victory()
        if self.boss is not None:
            return self.boss.render(sensor_stage, boundaries, countdown, start_mode, start_hold_remaining)
        return self._render_classic(sensor_stage, boundaries, countdown, start_mode, start_hold_remaining)


def parse_roi(value: str) -> tuple[int, int, int, int]:
    try:
        parts = tuple(int(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ROI は x,y,width,height の整数") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ROI は x,y,width,height の4値")
    return parts  # type: ignore[return-value]


def parse_play_range(value: str) -> tuple[float, float]:
    try:
        low, high = (float(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("プレイ範囲は LOW,HIGH の数値") from exc
    if not all(math.isfinite(part) for part in (low, high)) or not 0.0 <= low < high <= 1.0:
        raise argparse.ArgumentTypeError("プレイ範囲は 0 <= LOW < HIGH <= 1")
    return low, high


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="STRUCTURE Sensorまたはキーボードで操作する192x384 LEDブロック崩し")
    result.add_argument("--keyboard", action="store_true", help="センサーを使わず、プレビューをキーボードで操作")
    result.add_argument("--sensor-width", type=int, default=640)
    result.add_argument("--sensor-height", type=int, default=480)
    result.add_argument("--sensor-background-seconds", type=float, default=2.0)
    result.add_argument("--min-foreground-area", type=int, default=420)
    result.add_argument("--depth-min-change-mm", type=float, default=0.0, help="深度の手前側変化量。0は背景ノイズから自動決定")
    result.add_argument("--roi", type=parse_roi, default=None, help="検出ROI x,y,width,height")
    result.add_argument("--mirror", action="store_true", help="カメラの左右を反転してパドルへ対応付ける")
    result.add_argument(
        "--play-range",
        type=parse_play_range,
        default=(.15, .85),
        metavar="LOW,HIGH",
        help="人物位置がバーの左右端に対応するROI内範囲（既定: 0.15,0.85）",
    )
    result.add_argument("--position-deadzone", type=float, default=3.0, help="バー追従を止める誤差（LED px、既定3）")
    result.add_argument("--position-gain", type=float, default=.85, help="不感帯外の追従ゲイン（0超1以下、既定0.85）")
    result.add_argument("--start-mode", choices=("still", "passby", "arm-circle"), default="still", help="開始条件（既定: 3秒静止）")
    result.add_argument("--start-countdown-seconds", type=float, default=3.0, help="開始検知からゲーム開始までの秒数（既定3）")
    result.add_argument("--start-still-seconds", type=float, default=3.0, help="静止開始を確定する保持時間（既定3秒）")
    result.add_argument("--start-still-tolerance", type=float, default=.035, help="静止とみなす人物中心Xの許容幅（ROI比、既定0.035）")
    result.add_argument("--passby-confirm-frames", type=int, default=4, help="通過検知を確定する連続フレーム数")
    result.add_argument("--passby-rearm-frames", type=int, default=15, help="再通過を受け付けるまでの無検知フレーム数")
    result.add_argument("--jump-rise-y-min", type=float, default=0.05, help="ジャンプ判定の重心上昇量（既定0.05）")
    result.add_argument("--jump-rise-bottom-min", type=float, default=0.04, help="ジャンプ判定の下端上昇量（既定0.04）")
    result.add_argument("--calibration", type=Path, default=None, help="🙆学習済みcamera_calibration.json（既定: リポジトリ直下/camera_calibration.json）")
    result.add_argument("--start-center-tolerance", type=float, default=None, help="腕輪スタート時の中央許容幅。未指定時は校正値")
    result.add_argument("--start-width-gain", type=float, default=None, help="腕輪スタート時の人物幅増加。未指定時は校正値")
    result.add_argument("--start-upper-width-gain", type=float, default=None, help="腕輪スタート時の上半身幅増加。未指定時は校正値")
    result.add_argument("--start-upper-width-min", type=float, default=None, help="腕輪スタート時の上半身幅下限。未指定時は校正値")
    result.add_argument("--start-area-gain", type=float, default=None, help="腕輪スタート時の人物面積増加率。未指定時は校正値")
    result.add_argument("--fps", type=float, default=60.0)
    result.add_argument("--frames", type=int, default=0)
    result.add_argument("--seconds", type=float, default=0.0)
    result.add_argument("--send", action="store_true")
    result.add_argument("--pi", action="append", default=[], metavar="HOST[:PORT]")
    result.add_argument("--chunk-size", type=int, default=1200)
    result.add_argument("--no-preview", action="store_true")
    result.add_argument("--preview-scale", type=int, default=2)
    result.add_argument("--debug-depth", action="store_true", help="深度プレビューと前景マスクを表示")
    result.add_argument("--boundaries", action="store_true")
    return result


def preview(indexed: np.ndarray) -> np.ndarray:
    return np.asarray([item[:3] for item in FC6], np.uint8)[indexed][:, :, ::-1]


def load_start_calibration(path: Path | None) -> dict[str, float]:
    """valid校正JSONから🙆判定の学習値だけを安全に読み込む。"""
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("valid") is not True:
            return {}
        thresholds = data.get("thresholds")
        start = thresholds.get("start") if isinstance(thresholds, dict) else None
        if not isinstance(start, dict):
            return {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    result: dict[str, float] = {}
    for key in DEFAULT_START_SETTINGS:
        value = start.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0.0:
            result[key] = number
    return result


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    calibration_path = args.calibration or HOST.parent / "camera_calibration.json"
    learned_start = load_start_calibration(calibration_path)
    start_center_tolerance = (
        args.start_center_tolerance
        if args.start_center_tolerance is not None
        else learned_start.get("center_tolerance", DEFAULT_START_SETTINGS["center_tolerance"])
    )
    start_width_gain = (
        args.start_width_gain
        if args.start_width_gain is not None
        else learned_start.get("width_gain_min", DEFAULT_START_SETTINGS["width_gain_min"])
    )
    start_upper_width_gain = (
        args.start_upper_width_gain
        if args.start_upper_width_gain is not None
        else learned_start.get("upper_width_gain_min", DEFAULT_START_SETTINGS["upper_width_gain_min"])
    )
    start_upper_width_min = (
        args.start_upper_width_min
        if args.start_upper_width_min is not None
        else learned_start.get("upper_width_min", DEFAULT_START_SETTINGS["upper_width_min"])
    )
    start_area_gain = (
        args.start_area_gain
        if args.start_area_gain is not None
        else learned_start.get("area_gain_min", DEFAULT_START_SETTINGS["area_gain_min"])
    )
    if (
        args.fps <= 0
        or args.sensor_width <= 0
        or args.sensor_height <= 0
        or args.preview_scale <= 0
        or args.jump_rise_y_min <= 0
        or args.jump_rise_bottom_min <= 0
        or start_center_tolerance <= 0
        or start_width_gain <= 0
        or start_upper_width_gain <= 0
        or start_upper_width_min <= 0
        or start_area_gain <= 0
        or args.start_countdown_seconds <= 0
        or args.start_still_seconds <= 0
        or not 0.0 <= args.start_still_tolerance < 1.0
        or args.passby_confirm_frames < 2
        or args.passby_rearm_frames < 2
        or args.depth_min_change_mm < 0
        or args.position_deadzone < 0
        or not 0.0 < args.position_gain <= 1.0
        or (args.send and len(args.pi) != PI_COUNT)
    ):
        print("error: --fps/--preview-scale/--jump-rise-* または --pi の指定が不正", file=sys.stderr)
        return 2
    sensor: SensorController | None = None
    try:
        keyboard_mode = args.keyboard
        if not keyboard_mode:
            sensor = SensorController(
                args.sensor_width,
                args.sensor_height,
                args.sensor_background_seconds,
                args.min_foreground_area,
                args.roi,
                args.jump_rise_y_min,
                args.jump_rise_bottom_min,
                args.depth_min_change_mm,
                start_mode=args.start_mode,
                passby_confirm_frames=args.passby_confirm_frames,
                passby_rearm_frames=args.passby_rearm_frames,
                still_seconds=args.start_still_seconds,
                still_position_tolerance=args.start_still_tolerance,
                start_center_tolerance=start_center_tolerance,
                start_width_gain=start_width_gain,
                start_upper_width_gain=start_upper_width_gain,
                start_upper_width_min=start_upper_width_min,
                start_area_gain=start_area_gain,
            )
        sender = UdpFrameSender([parse_pi(value) for value in args.pi], args.chunk_size) if args.send else None
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}（開発用には --keyboard）", file=sys.stderr)
        return 2
    keyboard_state = create_keyboard_state() if not args.no_preview else None
    game, running, frame_id = ClassicThenBoss(), True, 0
    follower = PaddleFollower(
        CANVAS_WIDTH,
        game.paddle_width,
        args.play_range,
        args.position_deadzone,
        args.position_gain,
        args.mirror,
    )
    manual_lateral, manual_until, manual_launch = 0, 0.0, False
    countdown_remaining = 0.0
    awaiting_player_hold = False
    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    started = last = deadline = time.monotonic()
    period = 1 / args.fps
    input_label = "keyboard" if keyboard_mode else "structure-depth"
    print(
        f"block breaker: canvas={CANVAS_WIDTH}x{CANVAS_HEIGHT} palette=FC6 "
        f"input={input_label} send={'yes' if sender else 'no'} "
        f"start_mode={args.start_mode} countdown={args.start_countdown_seconds:.1f}s",
        flush=True,
    )
    if args.start_mode == "arm-circle" and learned_start:
        print(f"start calibration=learned path={calibration_path}", flush=True)
    elif args.start_mode == "arm-circle":
        print("start calibration=defaults", flush=True)
    elif args.start_mode == "passby":
        print("start calibration=not-used mode=passby", flush=True)
    else:
        print(
            f"start calibration=not-used mode=still hold={args.start_still_seconds:.1f}s "
            f"tolerance={args.start_still_tolerance:.3f}",
            flush=True,
        )
    if not args.no_preview:
        print("keys: A/D or LEFT/RIGHT = paddle, SPACE/W/UP = launch, R = reset, Q/ESC = quit", flush=True)
    last_sensor_stage: str | None = None
    last_lateral: int | None = None
    last_player_id: int | None = None
    try:
        while running and (args.frames <= 0 or frame_id < args.frames):
            now = time.monotonic()
            if args.seconds and now - started >= args.seconds:
                break
            body = sensor.read(now) if sensor else InputState(calibrated=True)
            sensor_stage = sensor.stage if sensor else "READY"
            if sensor_stage != last_sensor_stage:
                print(f"sensor_stage={sensor_stage}", flush=True)
                last_sensor_stage = sensor_stage
            if body.player_changed or (body.player_id is not None and body.player_id != last_player_id):
                print(
                    f"control=player:{body.player_id} people={body.people_detected} lock=front",
                    flush=True,
                )
            last_player_id = body.player_id
            if body.launch and args.start_mode == "arm-circle":
                print("event=arm-circle-launch", flush=True)
            if (
                body.start_trigger
                and game.serving
                and (not game.game_started or awaiting_player_hold)
                and countdown_remaining <= 0.0
            ):
                countdown_remaining = args.start_countdown_seconds
                awaiting_player_hold = False
                print(
                    f"event={args.start_mode}-start-detected countdown={args.start_countdown_seconds:.1f}s",
                    flush=True,
                )
            if body.lateral != last_lateral:
                print(f"input=lateral:{body.lateral}", flush=True)
                last_lateral = body.lateral
            x11_lateral = keyboard_state.lateral() if keyboard_state is not None else 0
            if manual_lateral and now >= manual_until:
                manual_lateral = 0
            lateral = x11_lateral or manual_lateral or (body.lateral if body.calibrated else 0)
            paddle_center_x = None
            if not keyboard_mode and body.body_present and body.body_x is not None:
                current_center = game.paddle_x + game.paddle_width / 2.0
                paddle_center_x = follower.follow(body.body_x, current_center)
            dt = min(.05, max(0.0, now - last))
            countdown_launch = False
            if countdown_remaining > 0.0:
                countdown_remaining = max(0.0, countdown_remaining - dt)
                if countdown_remaining == 0.0:
                    countdown_launch = True
                    print("event=game-launch-countdown", flush=True)
            was_serving, was_game_started = game.serving, game.game_started
            game.step(
                dt,
                GameInput(
                    lateral=lateral,
                    launch=countdown_launch or manual_launch,
                    paddle_center_x=paddle_center_x,
                ),
                now,
            )
            if game.consume_life_loss_event():
                countdown_remaining = 0.0
                if sensor is not None:
                    sensor.reselect_player()
                    awaiting_player_hold = True
                    print("event=life-lost select=front-player wait=still-hold", flush=True)
                else:
                    print("event=life-lost wait=manual-launch", flush=True)
            stage_start_requested = game.consume_stage_start_request()
            if was_game_started and not game.game_started and not stage_start_requested:
                countdown_remaining = 0.0
                awaiting_player_hold = False
                if sensor is not None:
                    sensor.reselect_player()
                print("event=round-reset select=front-player", flush=True)
            if stage_start_requested:
                countdown_remaining = 0.0
                if sensor is not None:
                    sensor.reselect_player()
                    awaiting_player_hold = True
                    print("event=stage-clear boss=random select=front-player wait=still-hold", flush=True)
                else:
                    print("event=stage-clear boss=random wait=manual-launch", flush=True)
            if was_serving and not game.serving:
                print("event=game-launch", flush=True)
            manual_launch, last = False, now
            countdown_display = int(math.ceil(countdown_remaining)) if countdown_remaining > 0.0 else None
            indexed = game.render(
                sensor_stage,
                args.boundaries,
                countdown_display,
                args.start_mode,
                body.start_hold_remaining,
            )
            if indexed.shape != (CANVAS_HEIGHT, CANVAS_WIDTH) or int(indexed.max()) >= FC6_LIMIT:
                raise RuntimeError("送出フレームがFC6の192x384条件を満たさない")
            if sender:
                sender.send(frame_id, PaletteMode.FC6, indexed)
            if not args.no_preview:
                display = preview(indexed)
                if args.preview_scale != 1:
                    display = cv2.resize(display, (CANVAS_WIDTH * args.preview_scale, CANVAS_HEIGHT * args.preview_scale), interpolation=cv2.INTER_NEAREST)
                cv2.imshow("RGB LED block breaker", display)
                if args.debug_depth and sensor:
                    sensor.show_debug()
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
                    manual_launch = True
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
        if sensor:
            sensor.close()
        if keyboard_state is not None:
            keyboard_state.close()
        if not args.no_preview:
            cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
