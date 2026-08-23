#!/usr/bin/env python3
"""STRUCTURE SensorのOpenNI2深度フレームを受け取る。"""
from __future__ import annotations

import math
import os
import struct
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np


FRAME_HEADER = struct.Struct("<4sIIII")
FRAME_MAGIC = b"SDP1"


def depth_preview(depth: np.ndarray) -> np.ndarray:
    """深度画像を人間が確認できるBGRプレビューへ変換する。"""
    image = np.asarray(depth)
    if image.ndim != 2:
        raise ValueError("深度画像は2次元でなければならない")
    valid = image > 0
    preview = np.zeros(image.shape, dtype=np.uint8)
    if np.any(valid):
        values = image[valid].astype(np.float32)
        low, high = np.percentile(values, (2.0, 98.0))
        if not math.isfinite(float(low)) or not math.isfinite(float(high)):
            return cv2.cvtColor(preview, cv2.COLOR_GRAY2BGR)
        if high <= low:
            high = low + 1.0
        # 近い領域を明るくする（深度値はmmで大きいほど遠い）。
        scaled = (high - image.astype(np.float32)) * (255.0 / (high - low))
        preview = np.clip(scaled, 0.0, 255.0).astype(np.uint8)
        preview[~valid] = 0
    return cv2.applyColorMap(preview, cv2.COLORMAP_TURBO)


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise RuntimeError("STRUCTURE Sensor取得ヘルパーが終了した")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class StructureSensorSource:
    """OpenNI2対応C++ヘルパーからCV_16UC1/mmフレームを読む。"""

    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        fps: float = 30.0,
        helper: str | os.PathLike[str] | None = None,
    ) -> None:
        if width <= 0 or height <= 0 or fps <= 0:
            raise ValueError("STRUCTURE Sensorの幅・高さ・FPSは正でなければならない")
        helper_path = Path(
            helper
            or os.environ.get("STRUCTURE_DEPTH_CAPTURE", "")
            or Path(__file__).with_name("structure_depth_capture")
        )
        if not helper_path.is_file() or not os.access(helper_path, os.X_OK):
            raise RuntimeError(f"STRUCTURE Sensor取得ヘルパーがない、または実行できない: {helper_path}")

        self.requested_size = (int(width), int(height))
        self.requested_fps = float(fps)
        self.helper_path = helper_path
        self.process = subprocess.Popen(
            [
                str(helper_path),
                "--width", str(width),
                "--height", str(height),
                "--fps", f"{fps:g}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )
        if self.process.stdout is None:
            self.close()
            raise RuntimeError("STRUCTURE Sensor取得ヘルパーの出力を開けない")
        self.last_shape: tuple[int, int] | None = None
        self.last_dtype: str | None = None
        self.last_frame_id: int | None = None

    @property
    def is_depth(self) -> bool:
        return True

    def read(self) -> np.ndarray:
        if self.process.stdout is None:
            raise RuntimeError("STRUCTURE Sensor取得ヘルパーの出力がない")
        header = _read_exact(self.process.stdout, FRAME_HEADER.size)
        magic, frame_id, width, height, payload_size = FRAME_HEADER.unpack(header)
        if magic != FRAME_MAGIC:
            raise RuntimeError(f"STRUCTURE Sensorフレームのマジックが不正: {magic!r}")
        expected_size = width * height * np.dtype(np.uint16).itemsize
        if width <= 0 or height <= 0 or payload_size != expected_size:
            raise RuntimeError(
                f"STRUCTURE Sensorフレームサイズが不正: {width}x{height} payload={payload_size}"
            )
        payload = _read_exact(self.process.stdout, payload_size)
        image = np.frombuffer(payload, dtype="<u2").reshape((height, width)).copy()
        self.last_shape = (int(width), int(height))
        self.last_dtype = str(image.dtype)
        self.last_frame_id = int(frame_id)
        return image

    def metadata(self, rotation: str) -> dict[str, Any]:
        width, height = self.last_shape or self.requested_size
        return {
            "source": "structure",
            "device": "OpenNI2 ASUS",
            "requested_width": self.requested_size[0],
            "requested_height": self.requested_size[1],
            "width": width,
            "height": height,
            "fps": self.requested_fps,
            "rotation": rotation,
            "backend": "OpenNI2 ASUS via structure_depth_capture",
            "pixel_format": "CV_16UC1",
            "unit": "mm",
            "last_dtype": self.last_dtype,
            "last_frame_id": self.last_frame_id,
            "capture_helper": str(self.helper_path),
        }

    def close(self) -> None:
        if getattr(self, "process", None) is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2.0)
        if self.process.stdout is not None:
            self.process.stdout.close()
