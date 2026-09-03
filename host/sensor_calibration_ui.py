#!/usr/bin/env python3
"""Macから実機STRUCTURE Sensorをリアルタイム調整するTkinter GUI。"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover - Tkなし環境向け
    tk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

try:
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover - 起動時に明示する
    Image = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]


DEFAULT_HOST = "fe80::2fd7:1c6e:6c7c:53c9"
DEFAULT_INTERFACE = "en9"
DEFAULT_USER = "takemuralab"
DEFAULT_REPO = "/home/takemuralab/hackathon-2026"
LINK_LOCAL_PATTERN = re.compile(r"(fe80::[0-9a-f:]+)%([a-zA-Z0-9]+)", re.IGNORECASE)
LATERAL_LABELS = {-1: "LEFT", 0: "CENTER", 1: "RIGHT"}


@dataclass(frozen=True)
class SSHSpec:
    host: str = DEFAULT_HOST
    interface: str = DEFAULT_INTERFACE
    user: str = DEFAULT_USER
    repo: str = DEFAULT_REPO

    @property
    def scoped_host(self) -> str:
        host = self.host.split("%", 1)[0]
        return f"{host}%%{self.interface}"


def ssh_base(spec: SSHSpec) -> list[str]:
    executable = os.environ.get("SENSOR_CALIBRATION_SSH", "").strip() or shutil.which("ssh")
    if not executable:
        raise RuntimeError("sshが見つかりません")
    return [
        executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        f"HostName={spec.scoped_host}",
        "-l",
        spec.user,
        "sensor-calibration-runtime",
    ]


def runtime_ssh_command(spec: SSHSpec) -> list[str]:
    remote = shlex.join(
        [
            str(Path(spec.repo) / ".venv/bin/python"),
            str(Path(spec.repo) / "host/sensor_runtime.py"),
            "client",
        ]
    )
    return [*ssh_base(spec), remote]


def execute_ssh(spec: SSHSpec, remote_args: Iterable[str], timeout: float = 8.0) -> subprocess.CompletedProcess[str]:
    remote = shlex.join(list(remote_args))
    return subprocess.run(
        [*ssh_base(spec), remote],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def discover_sensor(interface: str, user: str = DEFAULT_USER) -> str:
    """リンクローカル応答のhostnameをSSHで確認してpi3-sensorを探す。"""
    ping = shutil.which("ping6") or shutil.which("ping")
    if not ping:
        raise RuntimeError("ping6が見つかりません")
    command = [ping, "-c", "2", "-I", interface, "ff02::1"]
    result = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=6,
        check=False,
    )
    candidates: list[str] = []
    for address, found_interface in LINK_LOCAL_PATTERN.findall(result.stdout + result.stderr):
        if found_interface == interface and address not in candidates:
            candidates.append(address)
    if not candidates:
        raise RuntimeError(f"{interface}上にIPv6リンクローカル応答がありません")
    for address in candidates:
        spec = SSHSpec(host=address, interface=interface, user=user)
        try:
            check = execute_ssh(spec, ("hostname",), timeout=4.0)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if check.returncode == 0 and check.stdout.strip() == "pi3-sensor":
            return address
    raise RuntimeError("応答した機器の中にpi3-sensorが見つかりません")


class RuntimeConnection:
    """SSH上のNDJSONストリームをワーカースレッドで送受信する。"""

    def __init__(self, spec: SSHSpec, messages: queue.Queue[tuple[str, object]]) -> None:
        self.spec = spec
        self.messages = messages
        self.process: subprocess.Popen[str] | None = None
        self._write_lock = threading.Lock()

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.process = subprocess.Popen(
            runtime_ssh_command(self.spec),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(
            target=self._read_stdout,
            args=(self.process,),
            name="sensor-ui-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(self.process,),
            name="sensor-ui-stderr",
            daemon=True,
        ).start()

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    self.messages.put(("error", f"JSONでない受信データ: {line.strip()}"))
                    continue
                self.messages.put(("message", payload))
        finally:
            returncode = process.wait()
            self.messages.put(("closed", returncode))

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            text = line.strip()
            if text:
                self.messages.put(("stderr", text))

    def send(self, payload: Mapping[str, object]) -> None:
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            raise RuntimeError("センサーへ接続されていません")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            process.stdin.write(line)
            process.stdin.flush()

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


if tk is not None:

    class OffsetMeter(tk.Canvas):
        """左右閾値と現在の基準位置差を同じ軸に描く。"""

        def __init__(self, master: tk.Misc) -> None:
            super().__init__(master, height=78, background="#15191f", highlightthickness=0)
            self.bind("<Configure>", lambda _event: self.draw(None, 0.10, 0.10, 0.045))

        def draw(self, offset: float | None, left: float, right: float, deadband: float) -> None:
            self.delete("all")
            width = max(20, self.winfo_width())
            middle = width / 2
            scale = (width - 36) / 1.0
            def x(value: float) -> float:
                return max(12.0, min(width - 12.0, middle + value * scale))
            self.create_rectangle(x(-left), 18, x(-deadband), 58, fill="#3b6ca8", outline="")
            self.create_rectangle(x(-deadband), 18, x(deadband), 58, fill="#424a55", outline="")
            self.create_rectangle(x(deadband), 18, x(right), 58, fill="#a85a3b", outline="")
            self.create_line(middle, 10, middle, 66, fill="#e2e8f0", width=1)
            self.create_text(x(-left), 70, text=f"-{left:.3f}", fill="#9fbde1", anchor="s")
            self.create_text(x(right), 70, text=f"+{right:.3f}", fill="#e7af98", anchor="s")
            if offset is not None:
                marker = x(offset)
                self.create_line(marker, 7, marker, 64, fill="#f8e16c", width=4)
                self.create_text(marker, 8, text=f"{offset:+.3f}", fill="#fff3a8", anchor="s")


    class SensorCalibrationApp:
        def __init__(self, root: tk.Tk, spec: SSHSpec) -> None:
            self.root = root
            self.root.title("STRUCTURE Sensor リアルタイム校正")
            self.root.geometry("1220x790")
            self.root.minsize(980, 680)
            self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
            self.connection: RuntimeConnection | None = None
            self.preview_photo: object | None = None
            self.connected = False
            self.loading_settings = False
            self.dirty = False

            self.host_var = tk.StringVar(value=spec.host)
            self.interface_var = tk.StringVar(value=spec.interface)
            self.status_var = tk.StringVar(value="未接続")
            self.flip_vertical_var = tk.BooleanVar(value=False)
            self.flip_horizontal_var = tk.BooleanVar(value=True)
            self.left_var = tk.DoubleVar(value=0.10)
            self.right_var = tk.DoubleVar(value=0.10)
            self.deadband_var = tk.DoubleVar(value=0.045)
            self.confirm_frames_var = tk.IntVar(value=4)
            self.depth_var = tk.DoubleVar(value=0.0)
            self.area_var = tk.IntVar(value=420)
            self.metric_vars = {
                name: tk.StringVar(value="—")
                for name in (
                    "stage", "fps", "body", "calibrated", "lateral", "body_x",
                    "baseline_x", "offset_x", "area", "depth_gain", "noise", "player", "people",
                )
            }
            self._build_ui()
            for variable in (
                self.flip_vertical_var, self.flip_horizontal_var, self.left_var,
                self.right_var, self.deadband_var, self.confirm_frames_var,
                self.depth_var, self.area_var,
            ):
                variable.trace_add("write", self._mark_dirty)
            self.root.protocol("WM_DELETE_WINDOW", self.close)
            self.root.after(50, self._poll_messages)
            self.root.after(250, self.connect)

        def _build_ui(self) -> None:
            outer = ttk.Frame(self.root, padding=10)
            outer.pack(fill=tk.BOTH, expand=True)
            connection = ttk.LabelFrame(outer, text="実機接続", padding=8)
            connection.pack(fill=tk.X)
            ttk.Label(connection, text="IPv6").grid(row=0, column=0, sticky="w")
            ttk.Entry(connection, textvariable=self.host_var, width=35).grid(row=0, column=1, sticky="ew", padx=6)
            ttk.Label(connection, text="IF").grid(row=0, column=2, sticky="w")
            ttk.Entry(connection, textvariable=self.interface_var, width=8).grid(row=0, column=3, padx=6)
            ttk.Button(connection, text="自動検出", command=self.discover).grid(row=0, column=4, padx=3)
            ttk.Button(connection, text="接続", command=self.connect).grid(row=0, column=5, padx=3)
            ttk.Label(connection, textvariable=self.status_var).grid(row=0, column=6, sticky="e", padx=(12, 0))
            connection.columnconfigure(1, weight=1)
            connection.columnconfigure(6, weight=1)

            content = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
            content.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
            controls = ttk.Frame(content, padding=(0, 0, 8, 0))
            monitor = ttk.Frame(content, padding=(8, 0, 0, 0))
            content.add(controls, weight=2)
            content.add(monitor, weight=3)

            orientation = ttk.LabelFrame(controls, text="向き", padding=10)
            orientation.pack(fill=tk.X)
            ttk.Checkbutton(orientation, text="上下反転", variable=self.flip_vertical_var).pack(anchor="w")
            ttk.Checkbutton(orientation, text="左右反転", variable=self.flip_horizontal_var).pack(anchor="w")
            ttk.Label(orientation, text="向き変更を適用すると背景を自動再学習します").pack(anchor="w", pady=(6, 0))

            lateral = ttk.LabelFrame(controls, text="左右判定", padding=10)
            lateral.pack(fill=tk.X, pady=(10, 0))
            self._numeric_row(lateral, 0, "左閾値", self.left_var, 0.005, 0.5, 0.005)
            self._numeric_row(lateral, 1, "右閾値", self.right_var, 0.005, 0.5, 0.005)
            self._numeric_row(lateral, 2, "中央不感帯", self.deadband_var, 0.001, 0.5, 0.001)
            self._numeric_row(lateral, 3, "確定フレーム", self.confirm_frames_var, 2, 12, 1)
            ttk.Label(lateral, text="閾値が小さいほど敏感、確定フレームが少ないほど速い").grid(
                row=4, column=0, columnspan=3, sticky="w", pady=(5, 0)
            )

            detection = ttk.LabelFrame(controls, text="人物検出感度", padding=10)
            detection.pack(fill=tk.X, pady=(10, 0))
            self._numeric_row(detection, 0, "深度差分 mm", self.depth_var, 0, 500, 5)
            self._numeric_row(detection, 1, "最小領域 px", self.area_var, 1, 5000, 20)
            ttk.Label(detection, text="0 mmは背景ノイズから自動。両方とも小さいほど敏感").grid(
                row=2, column=0, columnspan=3, sticky="w", pady=(5, 0)
            )

            actions = ttk.Frame(controls)
            actions.pack(fill=tk.X, pady=(12, 0))
            ttk.Button(actions, text="一時適用", command=lambda: self.apply(False)).pack(side=tk.LEFT)
            ttk.Button(actions, text="適用して保存", command=lambda: self.apply(True)).pack(side=tk.LEFT, padx=6)
            ttk.Button(actions, text="背景再学習", command=self.relearn).pack(side=tk.LEFT, padx=6)
            ttk.Button(actions, text="人物再選択", command=self.reselect).pack(side=tk.LEFT)

            ttk.Label(controls, text="左右オフセット（黄＝現在値）").pack(anchor="w", pady=(14, 3))
            self.offset_meter = OffsetMeter(controls)
            self.offset_meter.pack(fill=tk.X)

            preview_box = ttk.LabelFrame(monitor, text="リアルタイム深度・検出", padding=8)
            preview_box.pack(fill=tk.BOTH, expand=True)
            self.preview_label = ttk.Label(preview_box, text="接続待ち", anchor="center")
            self.preview_label.pack(fill=tk.BOTH, expand=True)
            ttk.Label(
                preview_box,
                text="灰: 深度 / 橙: 候補 / 緑: 確定人物 / 黄線: 人物X / 白線: 基準X",
            ).pack(anchor="w", pady=(5, 0))

            metrics = ttk.LabelFrame(monitor, text="取得データ", padding=8)
            metrics.pack(fill=tk.X, pady=(10, 0))
            labels = (
                ("stage", "状態"), ("fps", "FPS"), ("body", "人物"),
                ("calibrated", "姿勢校正"), ("lateral", "左右入力"), ("body_x", "人物X"),
                ("baseline_x", "基準X"), ("offset_x", "差分X"), ("area", "領域率"),
                ("depth_gain", "深度差"), ("noise", "背景ノイズ"),
                ("player", "人物ID"), ("people", "人数"),
            )
            for index, (key, label) in enumerate(labels):
                row, column = divmod(index, 3)
                ttk.Label(metrics, text=f"{label}:").grid(row=row, column=column * 2, sticky="e", padx=(8, 3), pady=2)
                ttk.Label(metrics, textvariable=self.metric_vars[key], width=12).grid(
                    row=row, column=column * 2 + 1, sticky="w", pady=2
                )

        def _numeric_row(
            self,
            parent: ttk.LabelFrame,
            row: int,
            label: str,
            variable: tk.Variable,
            low: float,
            high: float,
            increment: float,
        ) -> None:
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Scale(parent, from_=low, to=high, variable=variable).grid(row=row, column=1, sticky="ew", padx=6)
            ttk.Spinbox(
                parent,
                from_=low,
                to=high,
                increment=increment,
                textvariable=variable,
                width=8,
            ).grid(row=row, column=2, sticky="e")
            parent.columnconfigure(1, weight=1)

        def _mark_dirty(self, *_args: object) -> None:
            if not self.loading_settings:
                self.dirty = True

        def _spec(self) -> SSHSpec:
            return SSHSpec(host=self.host_var.get().strip(), interface=self.interface_var.get().strip())

        def discover(self) -> None:
            self.status_var.set("pi3-sensorを検索中…")
            interface = self.interface_var.get().strip()
            def work() -> None:
                try:
                    address = discover_sensor(interface)
                    self.messages.put(("discovered", address))
                except Exception as exc:  # noqa: BLE001
                    self.messages.put(("error", f"自動検出失敗: {exc}"))
            threading.Thread(target=work, name="sensor-ui-discover", daemon=True).start()

        def connect(self) -> None:
            self.disconnect()
            try:
                connection = RuntimeConnection(self._spec(), self.messages)
                connection.start()
            except (OSError, RuntimeError) as exc:
                self.status_var.set(f"接続失敗: {exc}")
                return
            self.connection = connection
            self.connected = False
            self.status_var.set("接続中…")

        def disconnect(self) -> None:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            self.connected = False

        def _settings(self) -> dict[str, object]:
            settings = {
                "flip_vertical": bool(self.flip_vertical_var.get()),
                "flip_horizontal": bool(self.flip_horizontal_var.get()),
                "lateral_left_delta_min": float(self.left_var.get()),
                "lateral_right_delta_min": float(self.right_var.get()),
                "lateral_center_deadband": float(self.deadband_var.get()),
                "lateral_confirm_frames": int(self.confirm_frames_var.get()),
                "depth_min_change_mm": float(self.depth_var.get()),
                "min_foreground_area": int(self.area_var.get()),
            }
            if settings["lateral_center_deadband"] > min(
                settings["lateral_left_delta_min"], settings["lateral_right_delta_min"]  # type: ignore[arg-type]
            ):
                raise ValueError("中央不感帯は左右閾値以下にしてください")
            return settings

        def _send(self, payload: Mapping[str, object]) -> None:
            try:
                if self.connection is None:
                    raise RuntimeError("未接続です")
                self.connection.send(payload)
            except (OSError, RuntimeError, ValueError) as exc:
                self.status_var.set(f"送信失敗: {exc}")
                if messagebox is not None:
                    messagebox.showerror("センサー設定", str(exc))

        def apply(self, persist: bool) -> None:
            try:
                settings = self._settings()
            except ValueError as exc:
                if messagebox is not None:
                    messagebox.showerror("入力値が不正", str(exc))
                return
            self._send({"command": "set", "settings": settings, "persist": persist})
            if persist:
                self.dirty = False
                self.status_var.set("設定を適用・保存中…")
            else:
                self.status_var.set("設定を一時適用中…")

        def relearn(self) -> None:
            self._send({"command": "relearn_background"})
            self.status_var.set("背景を再学習中…（画面から離れてください）")

        def reselect(self) -> None:
            self._send({"command": "reselect_player"})

        def _load_settings(self, settings: Mapping[str, object]) -> None:
            if self.dirty:
                return
            self.loading_settings = True
            try:
                self.flip_vertical_var.set(bool(settings["flip_vertical"]))
                self.flip_horizontal_var.set(bool(settings["flip_horizontal"]))
                self.left_var.set(float(settings["lateral_left_delta_min"]))
                self.right_var.set(float(settings["lateral_right_delta_min"]))
                self.deadband_var.set(float(settings["lateral_center_deadband"]))
                self.confirm_frames_var.set(int(settings["lateral_confirm_frames"]))
                self.depth_var.set(float(settings["depth_min_change_mm"]))
                self.area_var.set(int(settings["min_foreground_area"]))
            finally:
                self.loading_settings = False
                self.dirty = False

        @staticmethod
        def _number(value: object, digits: int = 3, suffix: str = "") -> str:
            if value is None:
                return "—"
            return f"{float(value):.{digits}f}{suffix}"

        def _telemetry(self, payload: Mapping[str, object]) -> None:
            self.connected = True
            self.status_var.set("接続中・リアルタイム受信")
            settings = payload.get("settings")
            if isinstance(settings, dict):
                self._load_settings(settings)
            self.metric_vars["stage"].set(str(payload.get("stage", "—")))
            self.metric_vars["fps"].set(self._number(payload.get("fps"), 1))
            self.metric_vars["body"].set("検出" if payload.get("body_present") else "なし")
            self.metric_vars["calibrated"].set("完了" if payload.get("calibrated") else "未完了")
            self.metric_vars["lateral"].set(LATERAL_LABELS.get(int(payload.get("lateral", 0)), "?"))
            self.metric_vars["body_x"].set(self._number(payload.get("body_x")))
            self.metric_vars["baseline_x"].set(self._number(payload.get("baseline_x")))
            self.metric_vars["offset_x"].set(self._number(payload.get("offset_x")))
            self.metric_vars["area"].set(self._number(payload.get("area"), 3))
            self.metric_vars["depth_gain"].set(self._number(payload.get("depth_gain_mm"), 0, " mm"))
            self.metric_vars["noise"].set(self._number(payload.get("noise_p95_mm"), 1, " mm"))
            self.metric_vars["player"].set(str(payload.get("player_id") if payload.get("player_id") is not None else "—"))
            self.metric_vars["people"].set(str(payload.get("people_detected", 0)))
            self.offset_meter.draw(
                None if payload.get("offset_x") is None else float(payload["offset_x"]),
                float(self.left_var.get()),
                float(self.right_var.get()),
                float(self.deadband_var.get()),
            )
            encoded = payload.get("preview_jpeg")
            if isinstance(encoded, str) and Image is not None and ImageTk is not None:
                try:
                    image = Image.open(io.BytesIO(base64.b64decode(encoded)))
                    image.thumbnail((700, 470), Image.Resampling.NEAREST)
                    self.preview_photo = ImageTk.PhotoImage(image)
                    self.preview_label.configure(image=self.preview_photo, text="")
                except (ValueError, OSError):
                    pass

        def _handle_message(self, payload: object) -> None:
            if not isinstance(payload, dict):
                return
            kind = payload.get("type")
            if kind == "hello":
                settings = payload.get("settings")
                if isinstance(settings, dict):
                    self._load_settings(settings)
                self.connected = True
                self.status_var.set("接続完了・データ待ち")
            elif kind == "telemetry":
                self._telemetry(payload)
            elif kind == "ack":
                command = payload.get("command")
                self.status_var.set(f"反映済み: {command}")
            elif kind == "error":
                self.status_var.set(f"実機エラー: {payload.get('message', '不明')}")

        def _poll_messages(self) -> None:
            try:
                while True:
                    kind, payload = self.messages.get_nowait()
                    if kind == "message":
                        self._handle_message(payload)
                    elif kind == "discovered":
                        self.host_var.set(str(payload))
                        self.status_var.set(f"検出: {payload}")
                        self.connect()
                    elif kind == "stderr":
                        self.status_var.set(str(payload))
                    elif kind == "error":
                        self.status_var.set(str(payload))
                    elif kind == "closed" and self.connection is not None:
                        self.connected = False
                        self.status_var.set(f"切断（終了コード {payload}）")
            except queue.Empty:
                pass
            self.root.after(50, self._poll_messages)

        def close(self) -> None:
            if self.dirty and messagebox is not None:
                if not messagebox.askyesno("未保存の設定", "未保存の変更があります。終了しますか？"):
                    return
            self.disconnect()
            self.root.destroy()

else:  # pragma: no cover

    class SensorCalibrationApp:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("tkinterが利用できません")


def probe(spec: SSHSpec, timeout: float = 20.0) -> dict[str, object]:
    """GUIなしでSSH接続し、最初の実テレメトリーを返す。"""
    process = subprocess.Popen(
        runtime_ssh_command(spec),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    deadline = time.monotonic() + timeout
    result: dict[str, object] | None = None
    assert process.stdout is not None
    try:
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            payload = json.loads(line)
            if payload.get("type") == "telemetry" and payload.get("sequence") is not None:
                result = payload
                break
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
    if result is None:
        error = ""
        if process.stderr is not None:
            error = process.stderr.read().strip()
        raise RuntimeError(error or "実テレメトリーを受信できません")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mac用STRUCTURE Sensorリアルタイム校正GUI")
    parser.add_argument("--host", default=os.environ.get("SENSOR_PI_HOST", DEFAULT_HOST))
    parser.add_argument("--interface", default=os.environ.get("SENSOR_PI_INTERFACE", DEFAULT_INTERFACE))
    parser.add_argument("--probe", action="store_true", help="GUIを開かず実テレメトリーを1件受信")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = SSHSpec(host=args.host, interface=args.interface)
    if args.probe:
        payload = probe(spec)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if tk is None or Image is None or ImageTk is None:
        print("error: TkinterとPillowが必要です", file=sys.stderr)
        return 2
    root = tk.Tk()
    SensorCalibrationApp(root, spec)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
