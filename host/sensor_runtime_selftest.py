#!/usr/bin/env python3
"""センサー実行時設定・Unixソケット・GUI接続組み立ての自己テスト。"""
from __future__ import annotations

import json
import socket
import tempfile
import time
from pathlib import Path

import numpy as np

from block_breaker import SensorController
from sensor_calibration_ui import SSHSpec, runtime_ssh_command
from sensor_runtime import SensorRuntimeServer, load_settings, save_settings, validate_settings


SETTINGS: dict[str, object] = {
    "flip_vertical": False,
    "flip_horizontal": True,
    "lateral_left_delta_min": 0.10,
    "lateral_right_delta_min": 0.11,
    "lateral_center_deadband": 0.045,
    "lateral_confirm_frames": 4,
    "depth_min_change_mm": 0.0,
    "min_foreground_area": 420,
}


class FakeCapture:
    def read(self) -> np.ndarray:
        return np.full((48, 64), 4000, dtype=np.uint16)

    def close(self) -> None:
        pass


def main() -> int:
    errors: list[str] = []
    if validate_settings({}, SETTINGS) != SETTINGS:
        errors.append("既定設定の正規化が一致しない")
    for bad in (
        {"lateral_center_deadband": 0.2},
        {"lateral_confirm_frames": 1},
        {"depth_min_change_mm": float("nan")},
        {"unknown": 1},
    ):
        try:
            validate_settings(bad, SETTINGS)
            errors.append(f"不正設定を受理した: {bad}")
        except ValueError:
            pass

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = root / "settings.json"
        socket_path = root / "runtime.sock"
        save_settings(config, SETTINGS)
        if load_settings(config, SETTINGS) != SETTINGS:
            errors.append("設定JSONの保存・読込が一致しない")

        server = SensorRuntimeServer(socket_path, config, SETTINGS)
        server.start()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1.0)
        try:
            client.connect(str(socket_path))
            stream = client.makefile("rwb", buffering=0)
            hello = json.loads(stream.readline())
            initial = json.loads(stream.readline())
            if hello.get("type") != "hello" or initial.get("type") != "telemetry":
                errors.append("接続時のhello/telemetry順が不正")
            stream.write(
                (
                    json.dumps(
                        {
                            "command": "set",
                            "settings": {"lateral_left_delta_min": 0.08},
                            "persist": True,
                        }
                    )
                    + "\n"
                ).encode()
            )
            deadline = time.monotonic() + 1.0
            commands: list[dict[str, object]] = []
            while time.monotonic() < deadline and not commands:
                commands = server.pop_commands()
                time.sleep(0.01)
            if not commands or commands[0]["settings"]["lateral_left_delta_min"] != 0.08:  # type: ignore[index]
                errors.append("setコマンドをmain loopへ渡さない")
            server.publish({"sequence": 7, "stage": "READY"})
            received: list[dict[str, object]] = []
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not any(item.get("sequence") == 7 for item in received):
                received.append(json.loads(stream.readline()))
            if not any(item.get("sequence") == 7 for item in received):
                errors.append("最新テレメトリーを配信しない")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"Unixソケット検証失敗: {exc}")
        finally:
            client.close()
            server.close()

    sensor = SensorController(
        64,
        48,
        background_seconds=2.0,
        min_area=420,
        roi=None,
        jump_rise_y_min=0.05,
        jump_rise_bottom_min=0.04,
        depth_min_change_mm=0.0,
        capture=FakeCapture(),
        flip_horizontal=True,
    )
    try:
        changed = dict(SETTINGS)
        changed.update(
            flip_vertical=True,
            flip_horizontal=False,
            lateral_left_delta_min=0.08,
            lateral_confirm_frames=2,
            depth_min_change_mm=90.0,
            min_foreground_area=500,
        )
        if not sensor.apply_runtime_settings(changed, now=10.0):
            errors.append("向き変更で背景再学習を開始しない")
        if sensor.started != 10.0 or sensor.depth_min_change_mm != 90.0 or sensor.foreground_gate.min_area != 500:
            errors.append("SensorControllerへ設定値を反映しない")
        if sensor.person_tracker.classifier_options["lateral_confirm_frames"] != 2:
            errors.append("左右確定フレームを分類器へ反映しない")
    finally:
        sensor.close()

    command = runtime_ssh_command(SSHSpec())
    if not any("%%en9" in part for part in command) or not command[-1].endswith("sensor_runtime.py client"):
        errors.append(f"Mac用SSHコマンドが不正: {command}")

    for error in errors:
        print(f"ERROR: {error}")
    print(f"{len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
