#!/usr/bin/env python3
"""STRUCTURE SensorのOpenNI2深度フレームを受け取る。"""
from __future__ import annotations

import math
import os
import struct
import subprocess
import threading
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


def _read_into(stream: Any, buffer: bytearray) -> None:
    """フレーム本体を確保済みバッファへ直接読む（連結とコピーを作らない）。"""
    view = memoryview(buffer)
    offset, size = 0, len(buffer)
    while offset < size:
        read = stream.readinto(view[offset:])
        if not read:
            raise RuntimeError("STRUCTURE Sensor取得ヘルパーが終了した")
        offset += read


class StructureSensorSource:
    """OpenNI2対応C++ヘルパーからCV_16UC1/mmフレームを読む。"""

    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        fps: float = 30.0,
        helper: str | os.PathLike[str] | None = None,
        decimate: int = 1,
    ) -> None:
        if width <= 0 or height <= 0 or fps <= 0:
            raise ValueError("STRUCTURE Sensorの幅・高さ・FPSは正でなければならない")
        if not 1 <= int(decimate) <= 16:
            raise ValueError(f"間引き幅は1〜16: {decimate}")
        helper_path = Path(
            helper
            or os.environ.get("STRUCTURE_DEPTH_CAPTURE", "")
            or Path(__file__).with_name("structure_depth_capture")
        )
        if not helper_path.is_file() or not os.access(helper_path, os.X_OK):
            raise RuntimeError(f"STRUCTURE Sensor取得ヘルパーがない、または実行できない: {helper_path}")

        self.requested_size = (int(width), int(height))
        self.requested_fps = float(fps)
        self.decimate = int(decimate)
        self.helper_path = helper_path
        # 間引かないときは引数を渡さない。--decimate を知らない旧ヘルパーでも動く。
        command = [
            str(helper_path),
            "--width", str(width),
            "--height", str(height),
            "--fps", f"{fps:g}",
        ]
        if self.decimate > 1:
            command += ["--decimate", str(self.decimate)]
        self.process = subprocess.Popen(
            command,
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
        # 取得と処理を分け、処理が遅れてもパイプに古いフレームを溜めない。
        # 溜めたままだと readFrame とパイプの滞留がそのまま入力遅延になる。
        self.dropped = 0
        self._latest: np.ndarray | None = None
        self._latest_frame_id: int | None = None
        self._latest_shape = self.requested_size
        self._failure: BaseException | None = None
        self._closing = False
        self._condition = threading.Condition()
        self._pump = threading.Thread(target=self._pump_frames, name="structure-depth-pump", daemon=True)
        self._pump.start()

    @property
    def is_depth(self) -> bool:
        return True

    def _read_frame(self) -> tuple[int, int, int, np.ndarray]:
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
        payload = bytearray(payload_size)
        _read_into(self.process.stdout, payload)
        image = np.frombuffer(payload, dtype="<u2").reshape((height, width))
        return int(frame_id), int(width), int(height), image

    def _pump_frames(self) -> None:
        """ヘルパー出力を常に読み切り、未処理の最新フレームだけを保持する。"""
        try:
            while True:
                frame_id, width, height, image = self._read_frame()
                with self._condition:
                    if self._closing:
                        return
                    if self._latest is not None:
                        # 前のフレームが処理されないまま次が来た＝処理落ち。古い方を捨てる。
                        self.dropped += 1
                    self._latest = image
                    self._latest_frame_id = frame_id
                    self._latest_shape = (width, height)
                    self._condition.notify_all()
        except BaseException as error:  # noqa: BLE001 - 読み取りスレッドの失敗はread()へ渡す
            with self._condition:
                if not self._closing:
                    self._failure = error
                self._condition.notify_all()

    def read(self, timeout: float = 5.0) -> np.ndarray:
        """未処理の最新フレームを返す。溜まった古いフレームは捨てる。"""
        with self._condition:
            if not self._condition.wait_for(
                lambda: self._latest is not None or self._failure is not None or self._closing,
                timeout=timeout,
            ):
                raise RuntimeError(f"STRUCTURE Sensorフレームが{timeout:g}秒届かない")
            if self._latest is None:
                if self._failure is not None:
                    raise RuntimeError(f"STRUCTURE Sensorフレームを取得できない: {self._failure}")
                raise RuntimeError("STRUCTURE Sensor取得を終了した")
            image = self._latest
            width, height = self._latest_shape
            self._latest = None
            self.last_shape = (width, height)
            self.last_dtype = str(image.dtype)
            self.last_frame_id = self._latest_frame_id
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
            "decimate": self.decimate,
            "dropped": self.dropped,
        }

    def close(self) -> None:
        condition = getattr(self, "_condition", None)
        if condition is not None:
            with condition:
                self._closing = True
                condition.notify_all()
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
