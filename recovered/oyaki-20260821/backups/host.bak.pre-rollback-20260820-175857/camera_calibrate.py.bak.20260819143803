#!/usr/bin/env python3
"""固定照明に強い、主機側カメラキャリブレーション。

カメラ映像を主機だけで処理し、LEDへはFC6インデックスの192x384フレームだけを
送る。カメラを開かない ``--demo`` と、カメラ・UDPを使わない自己テストからも
利用できるよう、計測・判定・描画を小さな部品へ分離している。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import select
import signal
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

import cv2
import numpy as np

# python3 host/camera_calibrate.py で実行できるよう、既存モジュールを直接参照する。
HOST_ROOT = Path(__file__).resolve().parent
TEST_MODE_ROOT = HOST_ROOT / "test_mode"
for import_path in (HOST_ROOT, TEST_MODE_ROOT):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from palettes import FC6, FC6_BLACK, FC6_LIMIT, FC6_WHITE, PaletteMode  # noqa: E402
from test_mode import PI_COUNT, UdpFrameSender, parse_pi  # noqa: E402


# 既存test_modeの論理画面は幅192×高さ384（NumPy配列は高さ×幅）。
CANVAS_HEIGHT = 384
CANVAS_WIDTH = 192
PROCESS_HEIGHT = 320
PROCESS_WIDTH = 240
VERSION = "1.0"

MAIN_STAGES = (
    "BACKGROUND",
    "CENTER/STANCE",
    "LEFT",
    "RIGHT",
    "JUMP",
    "VALIDATE",
)
DISPLAY_STAGES = MAIN_STAGES + ("PASS", "RETRY", "FAIL")
MEASUREMENT_STAGES = MAIN_STAGES[1:]
DEFAULT_DURATIONS = {
    "BACKGROUND": 12.0,
    "CENTER/STANCE": 12.0,
    "LEFT": 10.0,
    "RIGHT": 10.0,
    "JUMP": 10.0,
    "VALIDATE": 8.0,
}

# LED表示に使うFC6インデックス。RGB値を直接生成しない。
LED_GREEN = 0x15
LED_YELLOW = 0x0E
LED_CYAN = 0x1E
LED_RED = 0x04
LED_BLUE = 0x21
LED_GRAY = 0x31


# 5x7の小型ASCIIフォント。LEDには短い英語の指示を表示する。
FONT_5X7: dict[str, tuple[str, ...]] = {
    " ": ("00000",) * 7,
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "/": ("00001", "00010", "00100", "01000", "10000", "00000", "00000"),
    ":": ("00000", "00100", "00100", "00000", "00100", "00100", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "11100"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
}


def parse_rect(value: str) -> tuple[int, int, int, int]:
    """x,y,width,height を解析する。座標系は回転・ROI後の240x320画像。"""
    try:
        parts = tuple(int(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("領域は x,y,width,height の整数") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("領域は x,y,width,height の4値")
    x, y, width, height = parts
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("領域のx,yは非負、幅と高さは正")
    return parts  # type: ignore[return-value]


def parse_exposure(value: str) -> tuple[float, float, float]:
    """露出設定 ``auto/shutter/gain`` を読む。既定値は1/312/2。"""
    try:
        parts = tuple(float(part) for part in value.split("/"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("露出は auto/shutter/gain 形式") from exc
    if len(parts) != 3 or not all(math.isfinite(part) for part in parts):
        raise argparse.ArgumentTypeError("露出は有限な3値 auto/shutter/gain")
    return parts  # type: ignore[return-value]


def rotate_frame(frame: np.ndarray, rotation: str) -> np.ndarray:
    """カメラフレームを回転する。ROI・背景差分・計測より先に呼ぶ。"""
    if rotation == "none":
        return np.ascontiguousarray(frame)
    if rotation == "cw":
        return np.ascontiguousarray(np.rot90(frame, k=3))
    if rotation == "ccw":
        return np.ascontiguousarray(np.rot90(frame, k=1))
    if rotation == "180":
        return np.ascontiguousarray(np.rot90(frame, k=2))
    raise ValueError(f"未知の回転: {rotation}")


def rotate_point(x: int, y: int, width: int, height: int, rotation: str) -> tuple[int, int]:
    """元画像上の画素座標を回転後の画素座標へ変換する。"""
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError("点が画像範囲外")
    if rotation == "none":
        return x, y
    if rotation == "cw":
        return height - 1 - y, x
    if rotation == "ccw":
        return y, width - 1 - x
    if rotation == "180":
        return width - 1 - x, height - 1 - y
    raise ValueError(f"未知の回転: {rotation}")


def _font_text(frame: np.ndarray, text: str, x: int, y: int, color: int, scale: int = 2) -> int:
    """FC6インデックス配列へASCII文字列を描画し、描画後のxを返す。"""
    scale = max(1, int(scale))
    cursor = x
    for char in text.upper():
        glyph = FONT_5X7.get(char, FONT_5X7["-"])
        for row, pattern in enumerate(glyph):
            for column, bit in enumerate(pattern):
                if bit == "1":
                    y0, x0 = y + row * scale, cursor + column * scale
                    y1, x1 = min(frame.shape[0], y0 + scale), min(frame.shape[1], x0 + scale)
                    if y0 < frame.shape[0] and x0 < frame.shape[1]:
                        frame[y0:y1, x0:x1] = color
        cursor += 6 * scale
    return cursor


def render_led_frame(
    stage: str,
    instruction: str,
    progress: float,
    candidate_valid: bool,
    result: str | None = None,
    frame_id: int = 0,
    remaining: float | None = None,
) -> np.ndarray:
    """ステージ情報をFC6インデックスの192x384画面へ描く。"""
    frame = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), FC6_BLACK, dtype=np.uint8)
    progress = min(1.0, max(0.0, float(progress)))
    stage_color = LED_CYAN if stage in MAIN_STAGES[:2] else LED_BLUE
    if stage in ("PASS", "RETRY", "FAIL"):
        stage_color = {"PASS": LED_GREEN, "RETRY": LED_YELLOW, "FAIL": LED_RED}[stage]
    _font_text(frame, stage, 8, 8, stage_color, 3)
    _font_text(frame, instruction[:28], 8, 42, FC6_WHITE, 2)

    frame[76:78, 8:376] = LED_GRAY
    end = 8 + int(round(368 * progress))
    if end > 8:
        frame[76:78, 8:end] = LED_GREEN if candidate_valid else LED_YELLOW

    candidate_label = "CANDIDATE VALID" if candidate_valid else "CANDIDATE INVALID"
    _font_text(frame, candidate_label, 8, 94, LED_GREEN if candidate_valid else LED_RED, 2)
    if remaining is not None:
        _font_text(frame, f"TIME {max(0.0, remaining):04.1f}S", 8, 126, FC6_WHITE, 2)
    if result:
        _font_text(frame, f"RESULT {result}", 8, 154, stage_color, 2)
    _font_text(frame, f"ID {int(frame_id) & 0xFFFFFFFF:08X}", 262, 178, LED_GRAY, 1)
    return frame


def indexed_to_bgr(indexed: np.ndarray) -> np.ndarray:
    """FC6インデックスをOpenCVプレビュー用BGRへ変換する。"""
    lut = np.asarray([entry[:3] for entry in FC6], dtype=np.uint8)
    rgb = lut[indexed]
    return np.ascontiguousarray(rgb[:, :, ::-1])


def write_indexed_png(path: Path, indexed: np.ndarray) -> None:
    """外部画像ライブラリに依存せず、FC6色のインデックス画像をPNG保存する。"""
    if indexed.ndim != 2 or indexed.dtype != np.uint8:
        raise ValueError("PNG画像はuint8の2次元インデックス配列")
    if int(indexed.max(initial=0)) >= FC6_LIMIT:
        raise ValueError("FC6範囲外のインデックス")
    rgb = np.asarray([entry[:3] for entry in FC6], dtype=np.uint8)[indexed]
    raw = b"".join(b"\x00" + row.tobytes(order="C") for row in rgb)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            len(payload).to_bytes(4, "big")
            + kind
            + payload
            + (zlib.crc32(kind + payload) & 0xFFFFFFFF).to_bytes(4, "big")
        )

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = (
        indexed.shape[1].to_bytes(4, "big")
        + indexed.shape[0].to_bytes(4, "big")
        + bytes((8, 2, 0, 0, 0))
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class FrameIdGenerator:
    """Unixミリ秒を初期値にし、同一プロセス内では時計逆行にも単調に追従する。"""

    def __init__(self, clock_ms: Callable[[], int] | None = None) -> None:
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.last: int | None = None

    def next(self) -> int:
        candidate = int(self.clock_ms()) & 0xFFFFFFFF
        if self.last is not None and candidate <= self.last:
            candidate = (self.last + 1) & 0xFFFFFFFF
        self.last = candidate
        return candidate


@dataclass(frozen=True)
class Measurement:
    """240x320処理画像内で正規化した人物候補の実測値。"""

    x: float
    y: float
    bottom: float
    area: float
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
    persistence: int = 1
    background_score: float = 0.0


@dataclass
class BackgroundModel:
    median: np.ndarray
    std: np.ndarray
    stable_light_mask: np.ndarray
    fixed_light_mask: np.ndarray
    noise_p95: float
    frame_count: int

    @property
    def ignore_mask(self) -> np.ndarray:
        return self.stable_light_mask | self.fixed_light_mask

    @property
    def stable_light_fraction(self) -> float:
        return float(np.mean(self.stable_light_mask))

    @property
    def fixed_light_fraction(self) -> float:
        return float(np.mean(self.fixed_light_mask))


def build_background_model(
    frames: Sequence[np.ndarray], fixed_regions: Sequence[tuple[int, int, int, int]] = ()
) -> BackgroundModel:
    """背景フレームから安定高輝度領域とノイズ統計を作る。"""
    if len(frames) < 2:
        raise ValueError("背景モデルには2フレーム以上必要")
    stack = np.asarray([np.asarray(frame, dtype=np.float32) for frame in frames])
    if stack.ndim != 3 or stack.shape[1:] != (PROCESS_HEIGHT, PROCESS_WIDTH):
        raise ValueError(f"背景画像は{PROCESS_WIDTH}x{PROCESS_HEIGHT}のグレースケール")
    median = np.median(stack, axis=0)
    std = np.std(stack, axis=0)
    bright_cut = max(220.0, float(np.percentile(median, 97.0)))
    stable_std_cut = max(4.0, float(np.percentile(std, 50.0) * 1.5))
    stable = (median >= bright_cut) & (std <= stable_std_cut)
    # 画像全体が白い場合に人の変化まで一律除外しない。
    if float(np.mean(stable)) > 0.40:
        stable = np.zeros_like(stable, dtype=bool)
    fixed = np.zeros_like(stable, dtype=bool)
    for x, y, width, height in fixed_regions:
        if x + width > PROCESS_WIDTH or y + height > PROCESS_HEIGHT:
            raise ValueError(f"固定領域 {x},{y},{width},{height} が240x320を超える")
        fixed[y:y + height, x:x + width] = True
    noise_p95 = float(np.percentile(np.abs(stack - median[None, :, :]), 95.0))
    return BackgroundModel(
        median=median.astype(np.float32),
        std=std.astype(np.float32),
        stable_light_mask=stable,
        fixed_light_mask=fixed,
        noise_p95=noise_p95,
        frame_count=len(frames),
    )


@dataclass(frozen=True)
class Detection:
    measurement: Measurement | None
    candidate_valid: bool
    mask: np.ndarray


class CandidateDetector:
    """背景統計と形状・持続性を併用して人物候補をゲートする。"""

    def __init__(
        self,
        model: BackgroundModel,
        min_area_ratio: float = 0.008,
        min_height_ratio: float = 0.18,
        min_width_ratio: float = 0.04,
        min_fill_ratio: float = 0.12,
        min_persistence: int = 2,
    ) -> None:
        self.model = model
        self.min_area_ratio = min_area_ratio
        self.min_height_ratio = min_height_ratio
        self.min_width_ratio = min_width_ratio
        self.min_fill_ratio = min_fill_ratio
        self.min_persistence = max(1, min_persistence)
        self.last_center: tuple[float, float] | None = None
        self.persistence = 0

    def reset(self) -> None:
        self.last_center = None
        self.persistence = 0

    def detect(self, gray: np.ndarray) -> Detection:
        image = np.asarray(gray, dtype=np.uint8)
        if image.shape != (PROCESS_HEIGHT, PROCESS_WIDTH):
            raise ValueError("検出画像は240x320")
        diff = np.abs(image.astype(np.float32) - self.model.median)
        threshold = max(10.0, self.model.noise_p95 * 3.0)
        binary = (diff >= threshold) & ~self.model.ignore_mask
        mask = (binary.astype(np.uint8) * 255)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        chosen: tuple[float, tuple[int, int, int, int], float, float, float] | None = None
        total = float(PROCESS_HEIGHT * PROCESS_WIDTH)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True):
            area = float(cv2.contourArea(contour))
            x, y, width, height = cv2.boundingRect(contour)
            area_ratio = area / total
            fill_ratio = area / max(1.0, float(width * height))
            if area_ratio < self.min_area_ratio or area_ratio > 0.80:
                continue
            if height / PROCESS_HEIGHT < self.min_height_ratio:
                continue
            if width / PROCESS_WIDTH < self.min_width_ratio:
                continue
            if fill_ratio < self.min_fill_ratio:
                continue
            moments = cv2.moments(contour)
            if moments["m00"] == 0:
                continue
            center_x = float(moments["m10"] / moments["m00"] / PROCESS_WIDTH)
            center_y = float(moments["m01"] / moments["m00"] / PROCESS_HEIGHT)
            if not (0.01 <= center_x <= 0.99 and 0.01 <= center_y <= 0.99):
                continue
            mean_diff = float(np.mean(diff[y:y + height, x:x + width]))
            background_score = mean_diff / max(threshold, 1.0)
            if background_score < 0.90:
                continue
            chosen = (
                area_ratio,
                (x, y, width, height),
                center_x,
                center_y,
                background_score,
            )
            break

        if chosen is None:
            self.reset()
            return Detection(None, False, mask)

        area_ratio, bbox, center_x, center_y, background_score = chosen
        if self.last_center is not None:
            distance = math.hypot(center_x - self.last_center[0], center_y - self.last_center[1])
            self.persistence = self.persistence + 1 if distance <= 0.10 else 1
        else:
            self.persistence = 1
        self.last_center = (center_x, center_y)
        x, y, width, height = bbox
        measurement = Measurement(
            x=center_x,
            y=center_y,
            bottom=float(y + height) / PROCESS_HEIGHT,
            area=area_ratio,
            bbox=bbox,
            persistence=self.persistence,
            background_score=background_score,
        )
        return Detection(measurement if self.persistence >= self.min_persistence else None, self.persistence >= self.min_persistence, mask)


def _finite_float(value: float | int) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("有限でない測定値")
    return result


def robust_stats(values: Sequence[float]) -> dict[str, float | int]:
    """median/MADと分位点を返す。空系列はcountだけを返す。"""
    if not values:
        return {"count": 0}
    array = np.asarray([_finite_float(value) for value in values], dtype=np.float64)
    median = float(np.median(array))
    mad = float(np.median(np.abs(array - median)))
    return {
        "count": int(array.size),
        "min": float(np.min(array)),
        "p10": float(np.percentile(array, 10)),
        "p25": float(np.percentile(array, 25)),
        "median": median,
        "p75": float(np.percentile(array, 75)),
        "p90": float(np.percentile(array, 90)),
        "max": float(np.max(array)),
        "mad": mad,
    }


def _series(samples: Sequence[Measurement], field: str) -> list[float]:
    return [_finite_float(getattr(sample, field)) for sample in samples]


def analyze_calibration(
    samples: dict[str, Sequence[Measurement]],
    background_frames: int,
    min_samples: int = 8,
) -> dict[str, object]:
    """実測分布から校正結果・閾値・品質ゲートを算出する。"""
    reasons: list[str] = []
    if background_frames < 2:
        reasons.append("background_samples_insufficient")
    counts = {stage: len(samples.get(stage, ())) for stage in MEASUREMENT_STAGES}
    for stage, count in counts.items():
        if count < min_samples:
            reasons.append(f"{stage.lower().replace('/', '_')}_samples_insufficient")

    stats: dict[str, dict[str, object]] = {}
    for stage in MEASUREMENT_STAGES:
        stage_samples = samples.get(stage, ())
        stats[stage] = {
            field: robust_stats(_series(stage_samples, field))
            for field in ("x", "y", "bottom", "area", "persistence", "background_score")
        }

    center = list(samples.get("CENTER/STANCE", ()))
    baseline_values = {field: robust_stats(_series(center, field)) for field in ("x", "y", "bottom", "area")}
    if center:
        baseline = {field: float(baseline_values[field]["median"]) for field in ("x", "y", "bottom", "area")}
        baseline["mad"] = {field: float(baseline_values[field]["mad"]) for field in ("x", "y", "bottom", "area")}
    else:
        baseline = {field: None for field in ("x", "y", "bottom", "area")}
        baseline["mad"] = {field: None for field in ("x", "y", "bottom", "area")}

    thresholds: dict[str, object] = {
        "center_tolerance": {"x": None, "y": None, "bottom": None},
        "left": {"x_max": None, "delta_min": None, "source": "LEFT measured p25 offset"},
        "right": {"x_min": None, "delta_min": None, "source": "RIGHT measured p25 offset"},
        "jump": {"rise_y_min": None, "rise_bottom_min": None, "source": "JUMP measured p25 rise"},
    }
    if center:
        center_x, center_y, center_bottom = baseline["x"], baseline["y"], baseline["bottom"]
        center_mad_x = float(baseline["mad"]["x"])
        center_mad_y = float(baseline["mad"]["y"])
        center_mad_bottom = float(baseline["mad"]["bottom"])
        # 画素量子化由来の最小幅だけを解像度から算出し、InputClassifierの固定値は使わない。
        x_tolerance = max(3.0 * center_mad_x, 2.0 / PROCESS_WIDTH)
        y_tolerance = max(3.0 * center_mad_y, 2.0 / PROCESS_HEIGHT)
        bottom_tolerance = max(3.0 * center_mad_bottom, 2.0 / PROCESS_HEIGHT)
        thresholds["center_tolerance"] = {"x": x_tolerance, "y": y_tolerance, "bottom": bottom_tolerance}

        left_offsets = [center_x - sample.x for sample in samples.get("LEFT", ())]
        right_offsets = [sample.x - center_x for sample in samples.get("RIGHT", ())]
        jump_rise_y = [center_y - sample.y for sample in samples.get("JUMP", ())]
        jump_rise_bottom = [center_bottom - sample.bottom for sample in samples.get("JUMP", ())]
        left_stats, right_stats = robust_stats(left_offsets), robust_stats(right_offsets)
        jump_y_stats, jump_bottom_stats = robust_stats(jump_rise_y), robust_stats(jump_rise_bottom)
        if left_offsets:
            left_delta = float(left_stats["p25"])
            thresholds["left"] = {"x_max": center_x - left_delta, "delta_min": left_delta, "source": "LEFT measured p25 offset"}
            if left_stats["median"] <= x_tolerance:
                reasons.append("left_motion_not_separated_from_center")
        if right_offsets:
            right_delta = float(right_stats["p25"])
            thresholds["right"] = {"x_min": center_x + right_delta, "delta_min": right_delta, "source": "RIGHT measured p25 offset"}
            if right_stats["median"] <= x_tolerance:
                reasons.append("right_motion_not_separated_from_center")
        if jump_rise_y and jump_rise_bottom:
            jump_y_delta = float(jump_y_stats["p25"])
            jump_bottom_delta = float(jump_bottom_stats["p25"])
            thresholds["jump"] = {
                "rise_y_min": jump_y_delta,
                "rise_bottom_min": jump_bottom_delta,
                "source": "JUMP measured p25 rise",
            }
            if jump_y_stats["median"] <= y_tolerance or jump_bottom_stats["median"] <= bottom_tolerance:
                reasons.append("jump_motion_not_separated_from_center")
        stats["LEFT"]["offset_x"] = left_stats
        stats["RIGHT"]["offset_x"] = right_stats
        stats["JUMP"]["rise_y"] = jump_y_stats
        stats["JUMP"]["rise_bottom"] = jump_bottom_stats
    else:
        stats["LEFT"]["offset_x"] = {"count": 0}
        stats["RIGHT"]["offset_x"] = {"count": 0}
        stats["JUMP"]["rise_y"] = {"count": 0}
        stats["JUMP"]["rise_bottom"] = {"count": 0}

    valid = not reasons
    return {
        "baseline": baseline,
        "motion_stats": stats,
        "thresholds": thresholds,
        "quality": {
            "valid": valid,
            "reasons": reasons,
            "minimum_samples_per_stage": min_samples,
            "background_frames": background_frames,
        },
        "sample_counts": counts,
        "valid": valid,
    }


class CalibrationSession:
    """候補が有効な時間だけ各ステージを進める状態機械。"""

    def __init__(self, durations: dict[str, float] | None = None, min_samples: int = 8) -> None:
        self.durations = {stage: float((durations or DEFAULT_DURATIONS)[stage]) for stage in MAIN_STAGES}
        if any(value <= 0 for value in self.durations.values()):
            raise ValueError("各ステージ時間は正")
        self.min_samples = max(1, int(min_samples))
        self.stage_index = 0
        self.active_elapsed = 0.0
        self.status = "RUNNING"
        self.abort_reason: str | None = None
        self.background_frames = 0
        self.samples: dict[str, list[Measurement]] = {stage: [] for stage in MEASUREMENT_STAGES}
        self.last_candidate_valid = False

    @property
    def stage(self) -> str:
        if self.status == "PASS":
            return "PASS"
        if self.status == "RETRY":
            return "RETRY"
        if self.status == "FAIL":
            return "FAIL"
        return MAIN_STAGES[self.stage_index]

    @property
    def progress(self) -> float:
        if self.status != "RUNNING":
            return 1.0
        return min(1.0, self.active_elapsed / self.durations[self.stage])

    @property
    def remaining(self) -> float:
        if self.status != "RUNNING":
            return 0.0
        return max(0.0, self.durations[self.stage] - self.active_elapsed)

    def _baseline(self) -> dict[str, float] | None:
        center = self.samples["CENTER/STANCE"]
        if not center:
            return None
        return {field: float(np.median(_series(center, field))) for field in ("x", "y", "bottom", "area")}

    def _center_gates(self) -> tuple[float, float, float]:
        center = self.samples["CENTER/STANCE"]
        if not center:
            return 2.0 / PROCESS_WIDTH, 2.0 / PROCESS_HEIGHT, 2.0 / PROCESS_HEIGHT
        values = {field: np.asarray(_series(center, field), dtype=np.float64) for field in ("x", "y", "bottom")}
        gates = []
        for field, pixels in (("x", PROCESS_WIDTH), ("y", PROCESS_HEIGHT), ("bottom", PROCESS_HEIGHT)):
            median = float(np.median(values[field]))
            mad = float(np.median(np.abs(values[field] - median)))
            gates.append(max(3.0 * mad, 2.0 / pixels))
        return tuple(gates)  # type: ignore[return-value]

    def eligible(self, measurement: Measurement | None, candidate_valid: bool, background_ready: bool) -> bool:
        stage = self.stage
        if stage == "BACKGROUND":
            return background_ready and not candidate_valid
        if stage not in MEASUREMENT_STAGES or measurement is None or not candidate_valid:
            return False
        baseline = self._baseline()
        if stage == "CENTER/STANCE":
            return True
        if baseline is None:
            return False
        gate_x, gate_y, gate_bottom = self._center_gates()
        if stage == "LEFT":
            return measurement.x < baseline["x"] - gate_x
        if stage == "RIGHT":
            return measurement.x > baseline["x"] + gate_x
        if stage == "JUMP":
            return (
                baseline["y"] - measurement.y > gate_y
                and baseline["bottom"] - measurement.bottom > gate_bottom
            )
        if stage == "VALIDATE":
            return (
                abs(measurement.x - baseline["x"]) <= gate_x
                and abs(measurement.y - baseline["y"]) <= gate_y
                and abs(measurement.bottom - baseline["bottom"]) <= gate_bottom
            )
        return False

    def update(
        self,
        dt: float,
        measurement: Measurement | None,
        candidate_valid: bool,
        background_ready: bool,
        background_frames: int = 0,
    ) -> None:
        if self.status != "RUNNING":
            return
        dt = max(0.0, min(float(dt), 1.0))
        self.last_candidate_valid = bool(candidate_valid)
        self.background_frames = max(self.background_frames, int(background_frames))
        if self.eligible(measurement, candidate_valid, background_ready):
            self.active_elapsed += dt
            if measurement is not None and self.stage in MEASUREMENT_STAGES:
                self.samples[self.stage].append(measurement)
        if self.active_elapsed < self.durations[self.stage]:
            return
        if self.stage == "BACKGROUND":
            if not background_ready:
                self.status = "FAIL"
                self.abort_reason = "background_model_not_ready"
                return
            self.stage_index += 1
            self.active_elapsed = 0.0
            return
        if self.stage == "VALIDATE":
            analysis = analyze_calibration(self.samples, self.background_frames, self.min_samples)
            self.status = "PASS" if bool(analysis["valid"]) else "RETRY"
            return
        self.stage_index += 1
        self.active_elapsed = 0.0

    def reset_current_stage(self) -> None:
        """現在ステージの進捗と実測を破棄する。背景モデルは呼び出し側が再取得する。"""
        if self.status != "RUNNING":
            return
        self.active_elapsed = 0.0
        self.last_candidate_valid = False
        if self.stage == "BACKGROUND":
            self.background_frames = 0
        elif self.stage in MEASUREMENT_STAGES:
            self.samples[self.stage].clear()

    def abort(self, reason: str = "user_abort") -> None:
        if self.status == "RUNNING":
            self.status = "FAIL"
            self.abort_reason = reason

    def analysis(self) -> dict[str, object]:
        analysis = analyze_calibration(self.samples, self.background_frames, self.min_samples)
        if self.abort_reason:
            quality = dict(analysis["quality"])
            quality["valid"] = False
            quality["reasons"] = list(quality["reasons"]) + [self.abort_reason]
            analysis["quality"] = quality
            analysis["valid"] = False
        return analysis


def make_calibration_payload(
    session: CalibrationSession,
    camera: dict[str, object],
    roi: tuple[int, int, int, int] | None,
    fixed_regions: Sequence[tuple[int, int, int, int]],
    background_model: BackgroundModel | None,
) -> dict[str, object]:
    """厳格JSONへ書ける最終結果を作る。"""
    analysis = session.analysis()
    background = {
        "frames": int(session.background_frames),
        "ready": background_model is not None,
        "noise_p95": None if background_model is None else background_model.noise_p95,
        "stable_bright_mask_fraction": None if background_model is None else background_model.stable_light_fraction,
        "fixed_mask_fraction": None if background_model is None else background_model.fixed_light_fraction,
        "median_luma": None if background_model is None else float(np.median(background_model.median)),
        "std_luma": None if background_model is None else float(np.median(background_model.std)),
    }
    payload: dict[str, object] = {
        "version": VERSION,
        "date": datetime.now(timezone.utc).isoformat(),
        "valid": bool(analysis["valid"]) and session.status == "PASS",
        "status": session.status,
        "camera": camera,
        "ROI": {
            "after_rotation": None if roi is None else list(roi),
            "processed_size": [PROCESS_WIDTH, PROCESS_HEIGHT],
        },
        "fixed_light": {
            "regions": [list(region) for region in fixed_regions],
            "coordinate_space": "processed_240x320_after_rotation_and_roi",
            "stable_background_mask": "stable_high_luminance_low_variance_only",
        },
        "background": background,
        "baseline": analysis["baseline"],
        "motion_stats": analysis["motion_stats"],
        "thresholds": analysis["thresholds"],
        "quality": analysis["quality"],
        "sample_counts": analysis["sample_counts"],
    }
    # allow_nan=Falseで失敗を早期検知する。Noneは欠測値として許可する。
    json.dumps(payload, ensure_ascii=False, allow_nan=False)
    return payload


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """同一ディレクトリの一時ファイルからatomic replaceする。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def invalid_result_path(path: Path) -> Path:
    suffix = path.suffix or ".json"
    stem = path.name[: -len(path.suffix)] if path.suffix else path.name
    return path.with_name(f"{stem}.invalid{suffix}")


def write_calibration_result(path: Path, payload: dict[str, object]) -> Path:
    """validは指定先、invalid/失敗は別名へ保存し、既存validを守る。"""
    target = path if bool(payload.get("valid")) else invalid_result_path(path)
    atomic_write_json(target, payload)
    return target


def _gray_to_canvas(gray: np.ndarray) -> np.ndarray:
    resized = cv2.resize(np.asarray(gray, dtype=np.uint8), (CANVAS_WIDTH, CANVAS_HEIGHT), interpolation=cv2.INTER_AREA)
    return np.rint(resized.astype(np.float32) * (FC6_LIMIT - 1) / 255.0).astype(np.uint8)


def run_demo(output_dir: Path) -> int:
    """カメラ・ネットワークなしで全ステージと診断画像を保存する。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    for index, stage in enumerate(DISPLAY_STAGES, start=1):
        instruction = {
            "BACKGROUND": "NO PERSON",
            "CENTER/STANCE": "STAND STILL",
            "LEFT": "MOVE LEFT",
            "RIGHT": "MOVE RIGHT",
            "JUMP": "JUMP NOW",
            "VALIDATE": "RETURN CENTER",
            "PASS": "CALIBRATION OK",
            "RETRY": "TRY AGAIN",
            "FAIL": "CALIBRATION FAIL",
        }[stage]
        frame = render_led_frame(
            stage,
            instruction,
            0.65 if stage in MAIN_STAGES else 1.0,
            stage in ("CENTER/STANCE", "LEFT", "RIGHT", "JUMP", "VALIDATE", "PASS"),
            stage if stage in ("PASS", "RETRY", "FAIL") else None,
            frame_id=index,
            remaining=3.2 if stage in MAIN_STAGES else 0.0,
        )
        write_indexed_png(output_dir / f"stage_{index:02d}_{stage.replace('/', '_')}.png", frame)

    rng = np.random.default_rng(20260819)
    background = np.full((PROCESS_HEIGHT, PROCESS_WIDTH), 45, dtype=np.uint8)
    background[32:92, 52:194] = 245  # READMEに記載する固定照明領域の例
    background = np.clip(background.astype(np.int16) + rng.integers(-2, 3, background.shape), 0, 255).astype(np.uint8)
    candidate = background.copy()
    candidate[90:285, 92:150] = 220
    candidate[70:115, 102:140] = 230
    write_indexed_png(output_dir / "diagnostic_background.png", _gray_to_canvas(background))
    candidate_indexed = _gray_to_canvas(candidate)
    candidate_indexed[76:178, 148:238] = LED_GREEN
    write_indexed_png(output_dir / "diagnostic_candidate.png", candidate_indexed)
    print(f"demo: {output_dir} に{len(DISPLAY_STAGES) + 2}枚のPNGを生成")
    return 0


class CameraSource:
    """OpenCVカメラの設定・readback・cleanupを閉じ込める。"""

    def __init__(self, device: int, width: int, height: int, fps: float, exposure: tuple[float, float, float]) -> None:
        self.device = device
        self.requested_size = (int(width), int(height))
        self.capture = cv2.VideoCapture(device)
        if not self.capture.isOpened():
            self.capture.release()
            raise RuntimeError(f"カメラ {device} を開けない")
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.capture.set(cv2.CAP_PROP_FPS, fps)
        self.capture.set(cv2.CAP_PROP_AUTO_EXPOSURE, exposure[0])
        self.capture.set(cv2.CAP_PROP_EXPOSURE, exposure[1])
        self.capture.set(cv2.CAP_PROP_GAIN, exposure[2])
        self.requested = {"auto": exposure[0], "shutter": exposure[1], "gain": exposure[2]}
        self.last_shape: tuple[int, int] | None = None

    def read(self) -> np.ndarray:
        ok, frame = self.capture.read()
        if not ok or frame is None:
            raise RuntimeError("カメラフレームを読めない")
        self.last_shape = (int(frame.shape[1]), int(frame.shape[0]))
        return frame

    def metadata(self, rotation: str) -> dict[str, object]:
        def readback(prop: int, integer: bool = False) -> int | float | None:
            value = float(self.capture.get(prop))
            if not math.isfinite(value):
                return None
            return int(round(value)) if integer else value

        width = readback(cv2.CAP_PROP_FRAME_WIDTH, integer=True)
        height = readback(cv2.CAP_PROP_FRAME_HEIGHT, integer=True)
        if (width is None or width <= 0) and self.last_shape is not None:
            width = self.last_shape[0]
        if (height is None or height <= 0) and self.last_shape is not None:
            height = self.last_shape[1]
        return {
            "device": int(self.device),
            "requested_width": self.requested_size[0],
            "requested_height": self.requested_size[1],
            "width": width,
            "height": height,
            "fps": readback(cv2.CAP_PROP_FPS),
            "rotation": rotation,
            "exposure": {
                "requested": self.requested,
                "auto_readback": readback(cv2.CAP_PROP_AUTO_EXPOSURE),
                "shutter_readback": readback(cv2.CAP_PROP_EXPOSURE),
                "gain_readback": readback(cv2.CAP_PROP_GAIN),
            },
        }

    def close(self) -> None:
        self.capture.release()


def _process_frame(source: np.ndarray, rotation: str, roi: tuple[int, int, int, int] | None) -> tuple[np.ndarray, np.ndarray]:
    rotated = rotate_frame(source, rotation)
    if roi is not None:
        x, y, width, height = roi
        if x + width > rotated.shape[1] or y + height > rotated.shape[0]:
            raise ValueError(f"ROI {roi} が回転後画像{rotated.shape[1]}x{rotated.shape[0]}を超える")
        rotated = rotated[y:y + height, x:x + width]
    processed = cv2.resize(rotated, (PROCESS_WIDTH, PROCESS_HEIGHT), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(processed, cv2.COLOR_BGR2GRAY) if processed.ndim == 3 else processed
    return processed, gray


def _stdin_key() -> str | None:
    if not sys.stdin.isatty():
        return None
    ready, _, _ = select.select([sys.stdin], [], [], 0.0)
    if ready:
        return sys.stdin.read(1).lower()
    return None


def _stage_instruction(stage: str) -> str:
    return {
        "BACKGROUND": "NO PERSON",
        "CENTER/STANCE": "STAND STILL",
        "LEFT": "MOVE LEFT",
        "RIGHT": "MOVE RIGHT",
        "JUMP": "JUMP NOW",
        "VALIDATE": "RETURN CENTER",
        "PASS": "CALIBRATION OK",
        "RETRY": "TRY AGAIN",
        "FAIL": "CALIBRATION FAIL",
    }[stage]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="固定照明に強いカメラ姿勢・移動・ジャンプ校正")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--camera-width", type=int, default=320)
    parser.add_argument("--camera-height", type=int, default=240)
    parser.add_argument("--camera-fps", type=float, default=30.0)
    parser.add_argument("--rotation", choices=("none", "cw", "ccw", "180"), default="none")
    parser.add_argument("--roi", type=parse_rect, default=None, help="回転後カメラ画像のROI x,y,width,height")
    parser.add_argument(
        "--fixed-light", action="append", type=parse_rect, default=[], metavar="X,Y,W,H",
        help="処理後240x320画像の固定照明領域。複数指定可",
    )
    parser.add_argument("--exposure", type=parse_exposure, default=(1.0, 312.0, 2.0), help="auto/shutter/gain（既定1/312/2）")
    parser.add_argument("--background-seconds", type=float, default=12.0)
    parser.add_argument("--stance-seconds", type=float, default=12.0)
    parser.add_argument("--left-seconds", type=float, default=10.0)
    parser.add_argument("--right-seconds", type=float, default=10.0)
    parser.add_argument("--jump-seconds", type=float, default=10.0)
    parser.add_argument("--validate-seconds", type=float, default=8.0)
    parser.add_argument("--background-frames", type=int, default=15)
    parser.add_argument("--min-samples", type=int, default=8)
    parser.add_argument("--output", type=Path, default=Path("camera_calibration.json"))
    parser.add_argument("--send", action="store_true", help="既存UdpFrameSenderで4台へ送る")
    parser.add_argument("--pi", action="append", default=None, metavar="HOST[:PORT]")
    parser.add_argument("--chunk-size", type=int, default=1200)
    parser.add_argument("--preview", action="store_true", help="OpenCVプレビューを表示")
    parser.add_argument("--demo", action="store_true", help="カメラ・ネットワークなしの表示デモ")
    parser.add_argument("--demo-output", type=Path, default=Path("/tmp/camera-calibrate-demo"))
    return parser


def run_camera(args: argparse.Namespace) -> int:
    durations = {
        "BACKGROUND": args.background_seconds,
        "CENTER/STANCE": args.stance_seconds,
        "LEFT": args.left_seconds,
        "RIGHT": args.right_seconds,
        "JUMP": args.jump_seconds,
        "VALIDATE": args.validate_seconds,
    }
    session = CalibrationSession(durations, args.min_samples)
    source: CameraSource | None = None
    sender: UdpFrameSender | None = None
    builder = BackgroundBuilder(args.background_frames, args.fixed_light)
    detector: CandidateDetector | None = None
    preview_requested = bool(args.preview)
    running = True
    old_handlers: dict[int, object] = {}

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    try:
        source = CameraSource(args.camera, args.camera_width, args.camera_height, args.camera_fps, args.exposure)
        if args.send:
            values = args.pi if args.pi is not None else [
                "192.168.10.101:5000", "192.168.10.102:5000", "192.168.10.103:5000", "192.168.10.104:5000"
            ]
            if len(values) != PI_COUNT:
                raise ValueError("--send時の--piはちょうど4個")
            sender = UdpFrameSender([parse_pi(value) for value in values], args.chunk_size)
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, stop)

        frame_ids = FrameIdGenerator()
        last = time.monotonic()
        deadline = last
        period = 1.0 / max(1.0, args.camera_fps)
        processed_preview: np.ndarray | None = None
        print(f"camera calibration: rotation={args.rotation} duration={sum(durations.values()):.1f}s send={'yes' if sender else 'no'}")
        print("keys: q/ESC 中止, r 現在ステージをリセット（--preview時はOpenCV、headless時はTTY）")
        while running and session.status == "RUNNING":
            now = time.monotonic()
            dt = min(0.5, max(0.0, now - last))
            last = now
            source_frame = source.read()
            processed, gray = _process_frame(source_frame, args.rotation, args.roi)
            processed_preview = processed
            if builder.model is None:
                builder.add(gray)
            if builder.model is not None and detector is None:
                detector = CandidateDetector(builder.model)
            detection = detector.detect(gray) if detector is not None else Detection(None, False, np.zeros_like(gray))
            session.update(
                dt,
                detection.measurement,
                detection.candidate_valid,
                builder.model is not None,
                builder.frame_count,
            )
            frame_id = frame_ids.next()
            indexed = render_led_frame(
                session.stage,
                _stage_instruction(session.stage),
                session.progress,
                detection.candidate_valid,
                session.stage if session.stage in ("PASS", "RETRY", "FAIL") else None,
                frame_id,
                session.remaining,
            )
            if sender is not None:
                sender.send(frame_id, PaletteMode.FC6, indexed)

            key: str | None = None
            if args.preview:
                try:
                    preview = cv2.resize(indexed_to_bgr(indexed), None, fx=2.0, fy=2.0, interpolation=cv2.INTER_NEAREST)
                    cv2.imshow("camera calibration LED", preview)
                    cv2.imshow("camera calibration camera", processed_preview)
                    cv2.imshow("camera calibration mask", detection.mask)
                    pressed = cv2.waitKey(1)
                    if pressed >= 0:
                        key = chr(pressed & 0xFF).lower()
                        if pressed == 27:
                            key = "q"
                except cv2.error as exc:
                    print(f"warning: OpenCVプレビューを停止: {exc}", file=sys.stderr)
                    args.preview = False
            if key is None:
                key = _stdin_key()
            if key == "q":
                session.abort("user_abort")
                running = False
            elif key == "r":
                session.reset_current_stage()
                if session.stage == "BACKGROUND":
                    builder.reset()
                    detector = None

            deadline += period
            sleep_time = deadline - time.monotonic()
            if sleep_time > 0:
                time.sleep(min(sleep_time, 0.25))
            elif sleep_time < -period:
                deadline = time.monotonic()
        if not running and session.status == "RUNNING":
            session.abort("signal_abort")

        camera_meta = source.metadata(args.rotation)
        payload = make_calibration_payload(session, camera_meta, args.roi, args.fixed_light, builder.model)
        target = write_calibration_result(args.output, payload)
        print(f"result: {session.status} valid={payload['valid']} output={target}")
        return 0 if bool(payload["valid"]) else 1
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if sender is not None:
            sender.close()
        if source is not None:
            source.close()
        if preview_requested:
            cv2.destroyAllWindows()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


class BackgroundBuilder:
    """背景モデルを最初の安定フレームだけから一度作る。"""

    def __init__(self, minimum_frames: int, fixed_regions: Sequence[tuple[int, int, int, int]]) -> None:
        self.minimum_frames = max(2, int(minimum_frames))
        self.fixed_regions = tuple(fixed_regions)
        self.frames: list[np.ndarray] = []
        self.model: BackgroundModel | None = None

    @property
    def frame_count(self) -> int:
        return len(self.frames) if self.model is None else self.model.frame_count

    def add(self, gray: np.ndarray) -> None:
        if self.model is not None:
            return
        self.frames.append(np.asarray(gray, dtype=np.uint8).copy())
        if len(self.frames) >= self.minimum_frames:
            self.model = build_background_model(self.frames, self.fixed_regions)

    def reset(self) -> None:
        self.frames.clear()
        self.model = None


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.demo:
        if args.send:
            print("error: --demoでは--sendを指定できない", file=sys.stderr)
            return 2
        return run_demo(args.demo_output)
    if args.background_frames < 2 or args.min_samples < 1 or args.camera_fps <= 0:
        print("error: background-frames/min-samples/camera-fpsが不正", file=sys.stderr)
        return 2
    for name in MAIN_STAGES:
        duration = {
            "BACKGROUND": args.background_seconds,
            "CENTER/STANCE": args.stance_seconds,
            "LEFT": args.left_seconds,
            "RIGHT": args.right_seconds,
            "JUMP": args.jump_seconds,
            "VALIDATE": args.validate_seconds,
        }[name]
        if duration <= 0:
            print(f"error: {name}の時間は正", file=sys.stderr)
            return 2
    return run_camera(args)


if __name__ == "__main__":
    raise SystemExit(main())
