#!/usr/bin/env python3
"""稼働中のセンサーエージェントをMac GUIから監視・調整するローカル通信。"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import queue
import socket
import sys
import threading
from pathlib import Path
from typing import BinaryIO, Iterable, Mapping

import cv2
import numpy as np


DEFAULT_SOCKET_PATH = Path("/tmp/hackathon-sensor-runtime.sock")
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "sensor_runtime_config.json"
SETTING_NAMES = (
    "flip_vertical",
    "flip_horizontal",
    "lateral_left_delta_min",
    "lateral_right_delta_min",
    "lateral_center_deadband",
    "lateral_confirm_frames",
    "depth_min_change_mm",
    "min_foreground_area",
)


def validate_settings(
    values: Mapping[str, object],
    base: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """部分設定を既存値へ重ね、型・範囲・左右閾値の関係を検証する。"""
    unknown = set(values) - set(SETTING_NAMES)
    if unknown:
        raise ValueError(f"未知のセンサー設定: {', '.join(sorted(unknown))}")
    merged = dict(base or {})
    merged.update(values)
    missing = [name for name in SETTING_NAMES if name not in merged]
    if missing:
        raise ValueError(f"不足しているセンサー設定: {', '.join(missing)}")

    for name in ("flip_vertical", "flip_horizontal"):
        if type(merged[name]) is not bool:
            raise ValueError(f"{name}はtrue/falseで指定してください")
    float_ranges = {
        "lateral_left_delta_min": (0.005, 0.5),
        "lateral_right_delta_min": (0.005, 0.5),
        "lateral_center_deadband": (0.001, 0.5),
        "depth_min_change_mm": (0.0, 1000.0),
    }
    for name, (low, high) in float_ranges.items():
        try:
            value = float(merged[name])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}は数値で指定してください") from exc
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name}は{low:g}〜{high:g}で指定してください")
        merged[name] = value
    if float(merged["lateral_center_deadband"]) > min(
        float(merged["lateral_left_delta_min"]),
        float(merged["lateral_right_delta_min"]),
    ):
        raise ValueError("中央不感帯は左右閾値以下にしてください")

    int_ranges = {
        "lateral_confirm_frames": (2, 12),
        "min_foreground_area": (1, 100000),
    }
    for name, (low, high) in int_ranges.items():
        value = merged[name]
        if isinstance(value, bool):
            raise ValueError(f"{name}は整数で指定してください")
        try:
            integer = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}は整数で指定してください") from exc
        if float(value) != integer or not low <= integer <= high:
            raise ValueError(f"{name}は{low}〜{high}の整数で指定してください")
        merged[name] = integer
    return {name: merged[name] for name in SETTING_NAMES}


def load_settings(path: Path, defaults: Mapping[str, object]) -> dict[str, object]:
    """保存済み設定を読み、未作成時は実行引数由来の既定値を返す。"""
    normalized_defaults = validate_settings(defaults)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return normalized_defaults
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"センサー設定JSONが不正: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"センサー設定JSONのルートはobject: {path}")
    return validate_settings(raw, normalized_defaults)


def save_settings(path: Path, settings: Mapping[str, object]) -> None:
    """検証済み設定だけをatomic writeする。"""
    normalized = validate_settings(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    text = json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _json_line(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


class SensorRuntimeServer:
    """Unixソケット上で最新テレメトリーを配信し、GUI操作をmain loopへ渡す。"""

    def __init__(
        self,
        socket_path: Path,
        config_path: Path,
        settings: Mapping[str, object],
    ) -> None:
        self.socket_path = Path(socket_path)
        self.config_path = Path(config_path)
        self._settings = validate_settings(settings)
        self._commands: queue.Queue[dict[str, object]] = queue.Queue()
        self._lock = threading.Lock()
        self._latest = _json_line({"type": "telemetry", "connected": True})
        self._generation = 0
        self._running = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    @property
    def settings(self) -> dict[str, object]:
        with self._lock:
            return dict(self._settings)

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        server.listen(4)
        server.settimeout(0.2)
        self._server = server
        self._running.set()
        self._thread = threading.Thread(
            target=self._accept_loop,
            name="sensor-runtime-server",
            daemon=True,
        )
        self._thread.start()

    def _accept_loop(self) -> None:
        assert self._server is not None
        while self._running.is_set():
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._client_loop,
                args=(connection,),
                name="sensor-runtime-client",
                daemon=True,
            ).start()

    def _client_loop(self, connection: socket.socket) -> None:
        connection.settimeout(0.05)
        buffer = bytearray()
        last_generation = -1
        try:
            connection.sendall(
                _json_line(
                    {
                        "type": "hello",
                        "settings": self.settings,
                        "config_path": str(self.config_path),
                    }
                )
            )
            while self._running.is_set():
                try:
                    block = connection.recv(65536)
                    if not block:
                        return
                    buffer.extend(block)
                    while b"\n" in buffer:
                        raw, _, remaining = buffer.partition(b"\n")
                        buffer = bytearray(remaining)
                        if raw.strip():
                            self._handle_command(connection, raw)
                except socket.timeout:
                    pass
                with self._lock:
                    generation = self._generation
                    latest = self._latest
                if generation != last_generation:
                    connection.sendall(latest)
                    last_generation = generation
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            connection.close()

    def _handle_command(self, connection: socket.socket, raw: bytes) -> None:
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("コマンドはobjectで指定してください")
            command = payload.get("command")
            if command == "set":
                values = payload.get("settings")
                if not isinstance(values, dict):
                    raise ValueError("settingsはobjectで指定してください")
                with self._lock:
                    normalized = validate_settings(values, self._settings)
                    self._settings = normalized
                queued = {
                    "command": "set",
                    "settings": normalized,
                    "persist": bool(payload.get("persist", False)),
                }
                self._commands.put(queued)
            elif command in ("relearn_background", "reselect_player"):
                self._commands.put({"command": command})
            elif command == "ping":
                pass
            else:
                raise ValueError(f"未知のコマンド: {command}")
            connection.sendall(_json_line({"type": "ack", "command": command}))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            connection.sendall(_json_line({"type": "error", "message": str(exc)}))

    def pop_commands(self) -> list[dict[str, object]]:
        commands: list[dict[str, object]] = []
        while True:
            try:
                commands.append(self._commands.get_nowait())
            except queue.Empty:
                return commands

    def publish(self, telemetry: Mapping[str, object]) -> None:
        line = _json_line({"type": "telemetry", **telemetry})
        with self._lock:
            self._latest = line
            self._generation += 1

    def close(self) -> None:
        self._running.clear()
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


def build_preview(sensor: object, state: object, settings: Mapping[str, object]) -> str | None:
    """深度、候補、確定人物、左右閾値をJPEGへまとめる。"""
    depth = getattr(sensor, "depth_image", None)
    if depth is None:
        return None
    depth_array = np.asarray(depth)
    valid = depth_array > 0
    gray = np.zeros(depth_array.shape, dtype=np.uint8)
    if np.any(valid):
        values = depth_array[valid].astype(np.float32)
        low, high = np.percentile(values, (2.0, 98.0))
        if high <= low:
            high = low + 1.0
        scaled = np.clip(
            (high - depth_array.astype(np.float32)) * 220.0 / (high - low),
            0.0,
            220.0,
        )
        gray[valid] = scaled[valid].astype(np.uint8)
    preview = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    mask = getattr(sensor, "mask", None)
    if mask is not None:
        preview[np.asarray(mask) > 0] = (0, 165, 255)
    accepted = getattr(sensor, "accepted_mask", None)
    if accepted is not None:
        preview[np.asarray(accepted) > 0] = (70, 220, 70)

    tracker = getattr(sensor, "person_tracker", None)
    active_id = getattr(tracker, "active_id", None)
    track = getattr(tracker, "tracks", {}).get(active_id) if tracker is not None else None
    classifier = getattr(track, "classifier", None)
    baseline = getattr(classifier, "baseline", None)
    width = preview.shape[1]
    if baseline is not None:
        base_x = int(round(float(baseline.x) * (width - 1)))
        left_x = int(round((float(baseline.x) - float(settings["lateral_left_delta_min"])) * (width - 1)))
        right_x = int(round((float(baseline.x) + float(settings["lateral_right_delta_min"])) * (width - 1)))
        cv2.line(preview, (max(0, left_x), 0), (max(0, left_x), preview.shape[0] - 1), (255, 120, 50), 1)
        cv2.line(preview, (min(width - 1, right_x), 0), (min(width - 1, right_x), preview.shape[0] - 1), (50, 120, 255), 1)
        cv2.line(preview, (base_x, 0), (base_x, preview.shape[0] - 1), (240, 240, 240), 1)
    body_x = getattr(state, "body_x", None)
    if body_x is not None:
        x = int(round(float(body_x) * (width - 1)))
        cv2.line(preview, (x, 0), (x, preview.shape[0] - 1), (255, 255, 0), 2)
    ok, encoded = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 72])
    if not ok:
        return None
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def build_telemetry(
    sensor: object,
    state: object,
    settings: Mapping[str, object],
    fps: float,
    sequence: int,
) -> dict[str, object]:
    tracker = getattr(sensor, "person_tracker", None)
    active_id = getattr(tracker, "active_id", None)
    track = getattr(tracker, "tracks", {}).get(active_id) if tracker is not None else None
    classifier = getattr(track, "classifier", None)
    baseline = getattr(classifier, "baseline", None)
    body = getattr(track, "body", None)
    body_x = getattr(state, "body_x", None)
    baseline_x = None if baseline is None else float(baseline.x)
    return {
        "sequence": int(sequence),
        "stage": str(getattr(sensor, "stage", "UNKNOWN")),
        "fps": round(float(fps), 2),
        "body_present": bool(getattr(state, "body_present", False)),
        "calibrated": bool(getattr(state, "calibrated", False)),
        "lateral": int(getattr(state, "lateral", 0)),
        "jump": bool(getattr(state, "jump", False)),
        "body_x": None if body_x is None else float(body_x),
        "body_y": None if body is None else float(body.y),
        "baseline_x": baseline_x,
        "offset_x": None if body_x is None or baseline_x is None else float(body_x) - baseline_x,
        "area": None if body is None else float(body.area),
        "depth_gain_mm": None if track is None else float(track.depth_gain),
        "player_id": getattr(state, "player_id", None),
        "people_detected": int(getattr(state, "people_detected", 0)),
        "start_hold_remaining": getattr(state, "start_hold_remaining", None),
        "noise_p95_mm": round(float(getattr(sensor, "depth_noise_p95", 0.0)), 2),
        "settings": dict(settings),
        "preview_jpeg": build_preview(sensor, state, settings),
    }


def bridge_client(socket_path: Path, input_stream: BinaryIO, output_stream: BinaryIO) -> int:
    """SSH標準入出力とUnixソケットを双方向に中継する。"""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(socket_path))
    except OSError as exc:
        print(f"error: センサー実行時ソケットへ接続できない: {exc}", file=sys.stderr)
        client.close()
        return 2

    def copy_input() -> None:
        try:
            while True:
                block = input_stream.readline()
                if not block:
                    try:
                        client.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                client.sendall(block)
        except OSError:
            return

    threading.Thread(target=copy_input, name="sensor-runtime-stdin", daemon=True).start()
    try:
        while True:
            block = client.recv(65536)
            if not block:
                return 0
            output_stream.write(block)
            output_stream.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        return 0
    finally:
        client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="稼働中センサーのローカル監視ソケットへ接続")
    parser.add_argument("command", choices=("client",))
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "client":
        return bridge_client(args.socket, sys.stdin.buffer, sys.stdout.buffer)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
