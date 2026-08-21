#!/usr/bin/env python3
"""姿勢校正値をゲーム入力へ接続する共通ランタイム。"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from camera_calibrate import rotate_frame
from pose_input import DEFAULT_MODEL_DIR, PoseMeasurement, PoseTracker

POSE_COORDINATE_SPACE = "pose_landmarks_normalized_by_torso"
POSE_DIRECTION_CONVENTION = "player_relative"


@dataclass(frozen=True)
class PoseCalibration:
    """pose_calibrate.py が書いた校正結果のうち、ゲーム判定に必要な部分。"""

    baseline: PoseMeasurement
    center_tolerance_x: float
    center_tolerance_y: float
    center_tolerance_bottom: float
    left_delta_min: float
    right_delta_min: float
    jump_rise_y_min: float
    jump_rise_bottom_min: float
    rotation: str
    device: int | str
    width: int
    height: int
    exposure: tuple[float, float, float] | None
    date: str
    source: Path


@dataclass(frozen=True)
class PoseState:
    """ゲーム本体のInputStateに依存しない姿勢入力結果。"""

    lateral: int = 0
    jump: bool = False
    body_present: bool = False
    calibrated: bool = True
    position: float | None = None


def _require(mapping: dict, key: str, where: str) -> object:
    if key not in mapping or mapping[key] is None:
        raise ValueError(f"校正ファイルの {where}.{key} が欠けている（校正をやり直す）")
    return mapping[key]


def _positive(value: object, where: str) -> float:
    number = float(value)  # type: ignore[arg-type]
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"校正ファイルの {where} が正の有限値でない: {value!r}")
    return number


def _nonnegative(value: object, where: str) -> float:
    number = float(value)  # type: ignore[arg-type]
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"校正ファイルの {where} が0以上の有限値でない: {value!r}")
    return number


def _pose_exposure_from(camera: dict) -> tuple[float, float, float] | None:
    value = camera.get("exposure")
    if isinstance(value, dict):
        requested = value.get("requested") or {}
        value = requested.get("values") or (
            requested.get("auto"),
            requested.get("shutter"),
            requested.get("gain"),
        )
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        numbers = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in numbers):
        return None
    return numbers  # type: ignore[return-value]


def load_pose_calibration(path: Path) -> PoseCalibration:
    """PASS済みで、姿勢入力と座標系が一致する校正だけを読み込む。"""
    if not path.exists():
        raise ValueError(f"姿勢校正ファイルがない: {path}（先に pose_calibrate.py を実行する）")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"姿勢校正ファイルを読めない: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"姿勢校正ファイルの形式が不正: {path}")

    status = payload.get("status")
    quality = payload.get("quality")
    reasons = quality.get("reasons", []) if isinstance(quality, dict) else []
    if not payload.get("valid") or status != "PASS":
        raise ValueError(f"姿勢校正が未完了（status={status} reasons={reasons}）: {path}")
    if payload.get("coordinate_space") != POSE_COORDINATE_SPACE:
        raise ValueError(
            f"姿勢校正の座標系が想定と違う: {payload.get('coordinate_space')!r} != {POSE_COORDINATE_SPACE!r}"
        )
    if payload.get("direction_convention") != POSE_DIRECTION_CONVENTION:
        raise ValueError(
            "姿勢校正の左右定義が想定と違う: "
            f"{payload.get('direction_convention')!r} != {POSE_DIRECTION_CONVENTION!r}"
        )

    baseline_data = payload.get("baseline")
    thresholds = payload.get("thresholds")
    camera = payload.get("camera")
    if not isinstance(baseline_data, dict) or not isinstance(thresholds, dict) or not isinstance(camera, dict):
        raise ValueError(f"姿勢校正ファイルのbaseline/thresholds/cameraが不正: {path}")
    if thresholds.get("units") != "torso_lengths":
        raise ValueError(f"姿勢校正の閾値単位が不正: {thresholds.get('units')!r}")

    baseline = PoseMeasurement(
        float(_require(baseline_data, "x", "baseline")),
        float(_require(baseline_data, "y", "baseline")),
        float(_require(baseline_data, "bottom", "baseline")),
        float(_require(baseline_data, "area", "baseline")),
        _positive(_require(baseline_data, "scale", "baseline"), "baseline.scale"),
        1.0,
        1.0,
    )
    if not all(math.isfinite(value) for value in (baseline.x, baseline.y, baseline.bottom, baseline.area)):
        raise ValueError(f"姿勢校正のbaselineに有限でない値がある: {path}")

    tolerance = thresholds.get("center_tolerance")
    left = thresholds.get("left")
    right = thresholds.get("right")
    jump = thresholds.get("jump")
    if not all(isinstance(value, dict) for value in (tolerance, left, right, jump)):
        raise ValueError(f"姿勢校正のthresholds内に必要な項目がない: {path}")

    rotation = str(camera.get("rotation") or "none")
    if rotation not in {"none", "cw", "ccw", "180"}:
        raise ValueError(f"姿勢校正のrotationが不正: {rotation!r}")
    width = int(camera.get("width") or 0)
    height = int(camera.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError(f"姿勢校正のカメラ解像度が不正: {width}x{height}")

    return PoseCalibration(
        baseline=baseline,
        center_tolerance_x=_nonnegative(tolerance["x"], "thresholds.center_tolerance.x"),  # type: ignore[index]
        center_tolerance_y=_nonnegative(tolerance["y"], "thresholds.center_tolerance.y"),  # type: ignore[index]
        center_tolerance_bottom=_nonnegative(tolerance["bottom"], "thresholds.center_tolerance.bottom"),  # type: ignore[index]
        left_delta_min=_positive(left["delta_min"], "thresholds.left.delta_min"),  # type: ignore[index]
        right_delta_min=_positive(right["delta_min"], "thresholds.right.delta_min"),  # type: ignore[index]
        jump_rise_y_min=_positive(jump["rise_y_min"], "thresholds.jump.rise_y_min"),  # type: ignore[index]
        jump_rise_bottom_min=_positive(jump["rise_bottom_min"], "thresholds.jump.rise_bottom_min"),  # type: ignore[index]
        rotation=rotation,
        device=camera.get("device", 0),
        width=width,
        height=height,
        exposure=_pose_exposure_from(camera),
        date=str(payload.get("date") or "unknown"),
        source=path,
    )


class PoseInputClassifier:
    """胴長で正規化した計測値を本人基準の左右・ジャンプへ変換する。"""

    def __init__(self, calibration: PoseCalibration) -> None:
        self.calibration = calibration
        self.last: PoseMeasurement | None = None
        self.last_time = 0.0
        self.lateral = 0
        self.candidate = 0
        self.candidate_since = 0.0
        self.jump_latched = False
        self.jump_candidates = 0
        self.last_jump = -math.inf

    def reset(self) -> None:
        self.__init__(self.calibration)

    def update(self, body: PoseMeasurement | None, now: float) -> PoseState:
        if body is None or not body.valid:
            self.last = None
            self.lateral = 0
            self.candidate = 0
            self.jump_latched = False
            self.jump_candidates = 0
            return PoseState()

        base = self.calibration.baseline
        offset_x = (body.x - base.x) / body.scale
        # 正面カメラでは参加者本人の左が画像右、本人の右が画像左。
        left_amount, right_amount = offset_x, -offset_x
        if left_amount >= self.calibration.left_delta_min:
            target = -1
        elif right_amount >= self.calibration.right_delta_min:
            target = 1
        elif abs(offset_x) <= self.calibration.center_tolerance_x:
            target = 0
        else:
            target = self.lateral

        if target != self.lateral:
            if target != self.candidate:
                self.candidate, self.candidate_since = target, now
            elif target == 0 or now - self.candidate_since >= 0.12:
                self.lateral, self.candidate = target, target
        else:
            self.candidate = target

        rise_y = (base.y - body.y) / body.scale
        rise_bottom = (base.bottom - body.bottom) / body.scale
        pose = (
            rise_y >= self.calibration.jump_rise_y_min
            and rise_bottom >= self.calibration.jump_rise_bottom_min
        )
        self.jump_candidates = self.jump_candidates + 1 if pose else 0
        jump = (
            self.jump_candidates >= 2
            and not self.jump_latched
            and now - self.last_jump >= 0.65
        )
        if jump:
            self.jump_latched, self.last_jump = True, now
        elif (
            rise_y < max(self.calibration.center_tolerance_y, self.calibration.jump_rise_y_min * .45)
            and rise_bottom
            < max(self.calibration.center_tolerance_bottom, self.calibration.jump_rise_bottom_min * .45)
        ):
            self.jump_latched = False

        self.last, self.last_time = body, now
        return PoseState(self.lateral, jump, True, True, position=min(1.0, max(0.0, 1.0 - body.x)))


class PoseCameraController:
    """カメラ → BlazePose追跡 → 姿勢校正値による入力。"""

    def __init__(
        self,
        device: int | str,
        calibration: PoseCalibration,
        model_dir: Path = DEFAULT_MODEL_DIR,
        threads: int = 4,
        redetect_after: int = 5,
        exposure: tuple[float, float, float] | None = None,
    ) -> None:
        self.capture = cv2.VideoCapture(device)
        if not self.capture.isOpened():
            raise RuntimeError(f"カメラ {device} を開けない")
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, calibration.width)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, calibration.height)
        self.capture.set(cv2.CAP_PROP_FPS, 60)
        self.exposure = exposure if exposure is not None else calibration.exposure
        if self.exposure is not None:
            self.capture.set(cv2.CAP_PROP_AUTO_EXPOSURE, self.exposure[0])
            self.capture.set(cv2.CAP_PROP_EXPOSURE, self.exposure[1])
            self.capture.set(cv2.CAP_PROP_GAIN, self.exposure[2])
        try:
            self.tracker = PoseTracker(model_dir=model_dir, threads=threads, redetect_after=redetect_after)
        except Exception:
            self.capture.release()
            raise
        self.calibration = calibration
        self.classifier = PoseInputClassifier(calibration)
        self.debug: np.ndarray | None = None
        self.last_state = PoseState()

    @property
    def stage(self) -> str:
        return "READY" if self.last_state.body_present else "POSE"

    def read(self, now: float) -> PoseState:
        ok, source = self.capture.read()
        if not ok:
            self.last_state = PoseState()
            return self.last_state
        frame = source if self.calibration.rotation == "none" else rotate_frame(source, self.calibration.rotation)
        measurement = self.tracker.update(frame)
        self.last_state = self.classifier.update(measurement, now)

        debug = frame.copy()
        landmarks = self.tracker.landmarks
        if landmarks is not None:
            for point in np.asarray(landmarks)[:33, :2]:
                x, y = int(round(float(point[0]))), int(round(float(point[1])))
                if 0 <= x < debug.shape[1] and 0 <= y < debug.shape[0]:
                    cv2.circle(debug, (x, y), 2, (0, 220, 0), -1, lineType=cv2.LINE_AA)
        label = "POSE READY" if measurement is not None else "POSE SEARCH"
        cv2.putText(debug, label, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, .6, (0, 230, 230), 1, cv2.LINE_AA)
        if measurement is not None:
            cv2.putText(
                debug,
                f"x={measurement.x:.3f} scale={measurement.scale:.3f} lat={self.last_state.lateral}",
                (6, 42),
                cv2.FONT_HERSHEY_SIMPLEX,
                .48,
                (0, 230, 230),
                1,
                cv2.LINE_AA,
            )
        self.debug = debug
        return self.last_state

    def close(self) -> None:
        self.capture.release()

    def show_debug(self) -> None:
        if self.debug is not None:
            cv2.imshow("block breaker pose", self.debug)
