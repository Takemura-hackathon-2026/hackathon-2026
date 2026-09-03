#!/usr/bin/env python3
"""PC側（macOS / Windows）の72枚パネルRGB・輝度キャリブレーションUI。

外部GUIライブラリに依存せず、Python標準のTkinterを使う。画面上のパネルを選ぶと
対象Piへ32x32の単色パターンを継続送信し、色相環または数値入力で設定した
R/G/Bゲインと輝度を「適用」ボタンで対象Piの設定ファイルへ反映して表示クライアントを再起動する。

WindowsではPython標準のTkinterと、Windows OpenSSH Clientのssh.exeを使う。
"""
from __future__ import annotations

import argparse
import colorsys
import math
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover - GUI未搭載Pythonでの案内用
    tk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]


MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))
HOST_DIR = MODULE_DIR.parent
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

from panel_calibration import (  # noqa: E402
    COLOR_INDEX,
    DEFAULT_SLICE_HEIGHT,
    PANEL_SIZE,
    WIDTH,
    build_frame,
    make_packets,
)
from palettes import FC6 as FC6_PALETTE  # noqa: E402


SSH_USER = "takemuralab"
CONTROL_HOST = "192.168.10.2"
CALIBRATION_PATH = "/etc/hackathon-2026/panel_calibration.conf"
CHUNK_SIZE = 1200
FPS = 10.0
CHAIN_LENGTH = 8
PARALLEL = 3
PANEL_ROWS = DEFAULT_SLICE_HEIGHT // PANEL_SIZE
PANEL_COLS = WIDTH // PANEL_SIZE
CHANNELS = ("red", "green", "blue")
CHANNEL_INDEX = {name: index for index, name in enumerate(CHANNELS)}
CHAIN_NAMES = "ABC"

# 実機で確認した値。制御Piの送信順（.101, .104, .102）とは異なるため、
# UIではtarget_idを暗黙に計算せず、表示Piごとに明示する。
DISPLAY_TARGETS = (
    ("pi1", "表示1 / pi1", "192.168.10.101", 0, "pi-client@0.service"),
    ("pi2", "表示2 / pi2", "192.168.10.102", 2, "pi-client@2.service"),
    ("pi4", "表示3 / pi4", "192.168.10.104", 1, "pi-client@1.service"),
)

# 論理スライス(row, col) -> HUB75コネクタ上のレーンとチェーン番号。
# pi-client.ccで chain_x = (7 - connector_chain) * 32 としているため、
# 設定ファイルのchainは「canvas chain」へ反転して保持する。
PANEL_MAP: tuple[tuple[tuple[int, int], ...], ...] = (
    ((0, 5), (0, 4), (0, 3), (0, 2), (0, 1), (0, 0)),
    ((0, 6), (0, 7), (1, 3), (1, 2), (1, 1), (1, 0)),
    ((2, 6), (2, 7), (1, 4), (1, 5), (1, 6), (1, 7)),
    ((2, 5), (2, 4), (2, 3), (2, 2), (2, 1), (2, 0)),
)


@dataclass(frozen=True)
class DisplayTarget:
    key: str
    label: str
    host: str
    target_id: int
    service: str


@dataclass(frozen=True)
class PanelRef:
    display: DisplayTarget
    row: int
    column: int
    lane: int
    connector_chain: int
    config_chain: int

    @property
    def key(self) -> str:
        return f"{self.display.key}:{self.row}:{self.column}"

    @property
    def physical_label(self) -> str:
        return (
            f"{CHAIN_NAMES[self.lane]}{self.connector_chain} "
            f"/ 設定{self.lane},{self.config_chain}"
        )


TARGETS = tuple(DisplayTarget(*values) for values in DISPLAY_TARGETS)


def iter_panel_refs(
    targets: Sequence[DisplayTarget] = TARGETS,
) -> tuple[PanelRef, ...]:
    refs: list[PanelRef] = []
    for display in targets:
        for row in range(PANEL_ROWS):
            for column in range(PANEL_COLS):
                lane, connector_chain = PANEL_MAP[row][column]
                refs.append(
                    PanelRef(
                        display=display,
                        row=row,
                        column=column,
                        lane=lane,
                        connector_chain=connector_chain,
                        config_chain=CHAIN_LENGTH - 1 - connector_chain,
                    )
                )
    return tuple(refs)


def identity_gain_table() -> dict[tuple[int, int], tuple[float, float, float]]:
    return {
        (lane, chain): (1.0, 1.0, 1.0)
        for lane in range(PARALLEL)
        for chain in range(CHAIN_LENGTH)
    }


def identity_brightness_table() -> dict[tuple[int, int], float]:
    return {
        (lane, chain): 1.0
        for lane in range(PARALLEL)
        for chain in range(CHAIN_LENGTH)
    }


def parse_calibration_config(
    text: str,
) -> tuple[
    dict[tuple[int, int], tuple[float, float, float]],
    dict[tuple[int, int], float],
]:
    """Pi設定を読み、RGBゲインとパネル共通輝度を返す。

    5列の既存形式は輝度1.00倍として受け入れ、6列目がある場合だけ
    パネル別輝度を読み取る。
    """
    gain_table = identity_gain_table()
    brightness_table = identity_brightness_table()
    seen: set[tuple[int, int]] = set()
    for line_number, original in enumerate(text.splitlines(), start=1):
        code = original.split("#", 1)[0].strip()
        if not code:
            continue
        fields = code.split()
        if len(fields) not in (5, 6):
            raise ValueError(f"設定{line_number}行目の列数が不正です")
        try:
            lane, chain = int(fields[0]), int(fields[1])
            gains = tuple(float(value) for value in fields[2:5])
            brightness = float(fields[5]) if len(fields) == 6 else 1.0
        except ValueError as exc:
            raise ValueError(f"設定{line_number}行目に数値でない値があります") from exc
        key = (lane, chain)
        if not 0 <= lane < PARALLEL or not 0 <= chain < CHAIN_LENGTH:
            raise ValueError(f"設定{line_number}行目のlane/chainが範囲外です")
        if key in seen:
            raise ValueError(f"設定{line_number}行目が重複しています")
        if any(
            not math.isfinite(value) or value < 0.0 or value > 2.0
            for value in (*gains, brightness)
        ):
            raise ValueError(f"設定{line_number}行目の倍率が0〜2倍の範囲外です")
        seen.add(key)
        gain_table[key] = gains  # type: ignore[assignment]
        brightness_table[key] = brightness
    return gain_table, brightness_table


def parse_gain_config(
    text: str,
) -> dict[tuple[int, int], tuple[float, float, float]]:
    """Piの設定からRGBゲインを読み、未記載のスロットは1.00倍で補う。"""
    return parse_calibration_config(text)[0]


def parse_brightness_config(text: str) -> dict[tuple[int, int], float]:
    """Piの設定からパネル共通輝度を読み、旧5列形式は1.00倍にする。"""
    return parse_calibration_config(text)[1]


def format_gain_config(
    table: dict[tuple[int, int], tuple[float, float, float]],
    brightness_table: dict[tuple[int, int], float] | None = None,
) -> str:
    if brightness_table is None:
        lines = [
            "# 32x32パネル単位のRGB補正。laneはHUB75出力レーン、chainはcanvas chain。",
            "# 倍率は0.00〜2.00。PC UIから適用した値。",
            "# lane chain red_gain green_gain blue_gain",
        ]
    else:
        lines = [
            "# 32x32パネル単位のRGB補正・輝度補正。laneはHUB75出力レーン、chainはcanvas chain。",
            "# RGB倍率は色味、brightnessはRGB共通の輝度倍率。各倍率は0.00〜2.00。",
            "# lane chain red_gain green_gain blue_gain brightness",
        ]
    for lane in range(PARALLEL):
        for chain in range(CHAIN_LENGTH):
            red, green, blue = table.get((lane, chain), (1.0, 1.0, 1.0))
            if brightness_table is None:
                lines.append(f"{lane} {chain} {red:.2f} {green:.2f} {blue:.2f}")
            else:
                brightness = brightness_table.get((lane, chain), 1.0)
                lines.append(
                    f"{lane} {chain} {red:.2f} {green:.2f} {blue:.2f} "
                    f"{brightness:.2f}"
                )
    return "\n".join(lines) + "\n"


def copy_gain_table(
    table: dict[tuple[int, int], tuple[float, float, float]],
) -> dict[tuple[int, int], tuple[float, float, float]]:
    return {key: tuple(values) for key, values in table.items()}


def copy_brightness_table(
    table: dict[tuple[int, int], float],
) -> dict[tuple[int, int], float]:
    return dict(table)


def make_pattern_frame(panel: PanelRef | None, color: str | int) -> bytes:
    """1枚またはスライス全体を指定色にしたFC6フレームを作る。"""
    color_index = resolve_color_index(color)
    selected = None if panel is None else (panel.row, panel.column)
    return build_frame(
        WIDTH,
        DEFAULT_SLICE_HEIGHT,
        color_index,
        COLOR_INDEX["black"],
        selected,
    )


def resolve_color_index(color: str | int) -> int:
    if isinstance(color, str):
        if color not in COLOR_INDEX:
            raise ValueError(f"未対応の色: {color}")
        return COLOR_INDEX[color]
    if (
        isinstance(color, bool)
        or not isinstance(color, int)
        or not 0 <= color < len(FC6_PALETTE)
    ):
        raise ValueError(f"FC6色番号が範囲外です: {color}")
    return int(color)


def fc6_rgb(index: int) -> tuple[int, int, int]:
    if not 0 <= index < len(FC6_PALETTE):
        raise ValueError(f"FC6色番号が範囲外です: {index}")
    return tuple(FC6_PALETTE[index][:3])  # type: ignore[return-value]


FIXED_COLOR_BY_INDEX = {index: name for name, index in COLOR_INDEX.items()}


def color_label(index: int) -> str:
    """FC6番号を、既存の校正用プリセット名付きで表示する。"""
    preset = FIXED_COLOR_BY_INDEX.get(index)
    suffix = f" / {preset}" if preset is not None else ""
    return f"FC6 0x{index:02X}{suffix}"


def gains_from_hue(hue: float, strength: float) -> tuple[float, float, float]:
    """色相環の位置を、中心1.00倍・外周0〜2倍のRGBゲインへ変換する。"""
    hue_rgb = colorsys.hsv_to_rgb(hue % 1.0, 1.0, 1.0)
    amount = max(0.0, min(1.0, float(strength)))
    return tuple(
        round(1.0 + amount * (2.0 * component - 1.0), 2)
        for component in hue_rgb
    )  # type: ignore[return-value]


def hue_from_gains(gains: tuple[float, float, float]) -> tuple[float, float]:
    """既存のRGBゲインを色相環へ近似表示する（数値入力との同期用）。"""
    target = tuple(value - 1.0 for value in gains)
    if sum(value * value for value in target) < 0.0001:
        return 0.0, 0.0

    best_error = float("inf")
    best_hue = 0.0
    best_strength = 0.0
    for step in range(360):
        hue = step / 360.0
        direction = tuple(
            2.0 * component - 1.0
            for component in colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        )
        denominator = sum(value * value for value in direction)
        strength = max(
            0.0,
            min(
                1.0,
                sum(value * axis for value, axis in zip(target, direction))
                / denominator,
            ),
        )
        error = sum(
            (value - strength * axis) ** 2
            for value, axis in zip(target, direction)
        )
        if error < best_error:
            best_error = error
            best_hue = hue
            best_strength = strength
    return best_hue, best_strength


if tk is not None:

    class HueWheel(tk.Canvas):
        """クリックした色相を返す、依存ライブラリ不要のTk色相環。"""

        def __init__(
            self,
            parent: "tk.Misc",
            size: int = 144,
            on_change: Callable[[float, float], None] | None = None,
        ) -> None:
            super().__init__(
                parent,
                width=size,
                height=size,
                highlightthickness=0,
                bd=0,
            )
            self.size = size
            self.center = size / 2.0
            self.outer = size * 0.46
            self.inner = size * 0.29
            self._hue = 0.0
            self._strength = 0.0
            self._on_change = on_change
            self._draw()
            self.bind("<Button-1>", self._choose)
            self.bind("<B1-Motion>", self._choose)

        @staticmethod
        def _hex(rgb: tuple[int, int, int]) -> str:
            return "#%02x%02x%02x" % rgb

        def _draw(self) -> None:
            self.delete("all")
            bbox = (
                self.center - self.outer,
                self.center - self.outer,
                self.center + self.outer,
                self.center + self.outer,
            )
            ring_width = max(8, int(self.outer - self.inner))
            for start in range(0, 360, 2):
                rgb = tuple(
                    round(component * 255)
                    for component in colorsys.hsv_to_rgb(start / 360.0, 1.0, 1.0)
                )
                self.create_arc(
                    bbox,
                    start=start,
                    extent=2.4,
                    style=tk.ARC,
                    outline=self._hex(rgb),
                    width=ring_width,
                )

            center_radius = self.inner * 0.56
            center_bbox = (
                self.center - center_radius,
                self.center - center_radius,
                self.center + center_radius,
                self.center + center_radius,
            )
            correction_rgb = tuple(
                round(component * 255)
                for component in colorsys.hsv_to_rgb(
                    self._hue,
                    self._strength,
                    1.0,
                )
            )
            self.create_oval(
                center_bbox,
                fill=self._hex(correction_rgb),
                outline="#303030",
                width=1,
            )

            angle = math.radians(self._hue * 360.0)
            marker_radius = self.inner * 0.56 + self._strength * (
                self.outer - self.inner * 0.56
            )
            marker_x = self.center + math.cos(angle) * marker_radius
            marker_y = self.center - math.sin(angle) * marker_radius
            marker_size = 5.0
            self.create_oval(
                marker_x - marker_size,
                marker_y - marker_size,
                marker_x + marker_size,
                marker_y + marker_size,
                fill="#ffffff",
                outline="#111111",
                width=2,
            )

        def set_correction(self, hue: float, strength: float) -> None:
            self._hue = hue % 1.0
            self._strength = max(0.0, min(1.0, float(strength)))
            self._draw()

        def _choose(self, event: "tk.Event") -> None:
            dx = event.x - self.center
            dy = self.center - event.y
            radius = math.hypot(dx, dy)
            if radius > self.outer * 1.12:
                return
            neutral_radius = self.inner * 0.56
            if radius >= neutral_radius:
                self._hue = (math.degrees(math.atan2(dy, dx)) % 360.0) / 360.0
            self._strength = max(
                0.0,
                min(
                    1.0,
                    (radius - neutral_radius) / (self.outer - neutral_radius),
                ),
            )
            self._draw()
            if self._on_change is not None:
                self._on_change(self._hue, self._strength)

else:  # pragma: no cover - tkinterなしの環境ではUIを起動できない

    class HueWheel:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("tkinterが利用できないため色相環を作成できません")


def ssh_command(host: str, *remote_args: str) -> list[str]:
    executable = os.environ.get("PANEL_CALIBRATION_SSH", "").strip()
    if not executable:
        executable = shutil.which("ssh") or ""
    if not executable:
        raise RuntimeError(
            "ssh.exeが見つかりません。Windowsの『OpenSSH Client』を有効化するか、"
            "PANEL_CALIBRATION_SSHにssh.exeのフルパスを設定してください"
        )
    return [
        executable,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        f"{SSH_USER}@{host}",
        *remote_args,
    ]


def execute_ssh(
    host: str,
    *remote_args: str,
    input_text: str | None = None,
    timeout: float = 12.0,
) -> subprocess.CompletedProcess[str]:
    run_options: dict[str, object] = {
        "input": input_text,
        "text": True,
        # ssh経由の設定ファイルはUTF-8。Windowsのcp932/cp1252に依存させない。
        "encoding": "utf-8",
        "errors": "replace",
        "capture_output": True,
        "timeout": timeout,
        "check": False,
    }
    if sys.platform == "win32":
        # 各SSH操作のたびに黒いコンソールを開かない。
        run_options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(
            ssh_command(host, *remote_args),
            **run_options,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ssh.exeを起動できません。OpenSSH Clientの導入状態を確認してください"
        ) from exc


def require_ssh(
    host: str,
    *remote_args: str,
    input_text: str | None = None,
    timeout: float = 12.0,
) -> str:
    result = execute_ssh(
        host,
        *remote_args,
        input_text=input_text,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "SSHコマンド失敗"
        raise RuntimeError(f"{host}: {detail}")
    return result.stdout


def remote_service_state(host: str, service: str) -> str:
    result = execute_ssh(host, "systemctl", "is-active", service)
    if result.returncode not in (0, 3):
        detail = result.stderr.strip() or "systemctl is-active失敗"
        raise RuntimeError(f"{host}: {detail}")
    return result.stdout.strip() or "unknown"


def read_remote_calibration(
    target: DisplayTarget,
) -> tuple[
    dict[tuple[int, int], tuple[float, float, float]],
    dict[tuple[int, int], float],
]:
    text = require_ssh(target.host, "cat", CALIBRATION_PATH)
    return parse_calibration_config(text)


def read_remote_gain_table(
    target: DisplayTarget,
) -> dict[tuple[int, int], tuple[float, float, float]]:
    """互換用に、Pi設定からRGBゲインだけを返す。"""
    return read_remote_calibration(target)[0]


def apply_remote_gain_table(
    target: DisplayTarget,
    table: dict[tuple[int, int], tuple[float, float, float]],
    brightness_table: dict[tuple[int, int], float] | None = None,
) -> None:
    config = format_gain_config(table, brightness_table)
    # sudo -n teeで設定をroot所有のまま更新し、クライアントを再起動して反映する。
    require_ssh(
        target.host,
        "sudo",
        "-n",
        "tee",
        CALIBRATION_PATH,
        input_text=config,
    )
    require_ssh(target.host, "sudo", "-n", "systemctl", "restart", target.service)
    state = remote_service_state(target.host, target.service)
    if state != "active":
        raise RuntimeError(f"{target.label}: 再起動後の状態が{state}です")


def change_control_service(action: str) -> tuple[str, str]:
    if action not in ("start", "stop"):
        raise ValueError(f"未対応のsystemd操作: {action}")
    before = remote_service_state(CONTROL_HOST, "pi3-control.service")
    if action == "stop" and before == "active":
        require_ssh(
            CONTROL_HOST,
            "sudo",
            "-n",
            "systemctl",
            "stop",
            "pi3-control.service",
        )
    elif action == "start" and before != "active":
        require_ssh(
            CONTROL_HOST,
            "sudo",
            "-n",
            "systemctl",
            "start",
            "pi3-control.service",
        )
    after = remote_service_state(CONTROL_HOST, "pi3-control.service")
    expected = "inactive" if action == "stop" else "active"
    if after != expected:
        raise RuntimeError(
            f"pi3-control.serviceの{action}後状態が{after}です（期待値: {expected}）"
        )
    return before, after


class PatternSender:
    """1枚または全72枚へ単色パターンを送るバックグラウンド送信機。"""

    def __init__(self, on_error: Callable[[str], None] | None = None) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        self._lock = threading.Lock()
        # panelがNoneなら、3台それぞれの24枚全体へ送る。
        self._pattern: tuple[PanelRef | None, str | int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame_id = int(time.monotonic() * 1000) & 0xFFFFFFFF
        self._on_error = on_error

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="panel-calibration-udp",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def set_pattern(self, panel: PanelRef, color: str | int) -> None:
        make_pattern_frame(panel, color)
        with self._lock:
            self._pattern = (panel, color)

    def set_all_pattern(self, color: str | int) -> None:
        make_pattern_frame(None, color)
        with self._lock:
            self._pattern = (None, color)

    def close(self) -> None:
        self.stop()
        self._socket.close()

    def _run(self) -> None:
        interval = 1.0 / FPS
        while not self._stop.is_set():
            started = time.monotonic()
            with self._lock:
                pattern = self._pattern
            if pattern is not None:
                panel, color = pattern
                frame = make_pattern_frame(panel, color)
                targets = (panel.display,) if panel is not None else TARGETS
                for target in targets:
                    packets = make_packets(
                        target.target_id,
                        self._frame_id,
                        frame,
                        CHUNK_SIZE,
                    )
                    try:
                        for packet in packets:
                            self._socket.sendto(packet, (target.host, 5000))
                    except OSError as exc:
                        self._stop.set()
                        if self._on_error is not None:
                            self._on_error(f"{target.label}への単色送信失敗: {exc}")
                        break
                self._frame_id = (self._frame_id + 1) & 0xFFFFFFFF
            self._stop.wait(max(0.0, interval - (time.monotonic() - started)))


class CalibrationApp:
    def __init__(self, root: "tk.Tk", load_remote: bool = True) -> None:
        self.root = root
        self.root.title("72枚パネル RGB・輝度キャリブレーション")
        self.root.geometry("1320x900")
        self.root.minsize(980, 700)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.refs = iter_panel_refs()
        self.gains = {target.key: identity_gain_table() for target in TARGETS}
        self.brightnesses = {
            target.key: identity_brightness_table() for target in TARGETS
        }
        self.dirty: set[str] = set()
        self.panel_vars: dict[str, dict[str, tk.StringVar]] = {}
        self.cards: dict[str, ttk.LabelFrame] = {}
        self.selected: PanelRef | None = None
        self.pattern_active = False
        self.all_pattern_active = False
        self.calibration_mode = False
        self.control_was_active: bool | None = None
        self.remote_job_active = False
        self.load_count = 0

        self.status_var = tk.StringVar(value="起動中…")
        self.selected_var = tk.StringVar(value="パネル未選択")
        self.test_color_index = COLOR_INDEX["white"]
        self.preset_var = tk.StringVar(value="white")
        self.selected_color_var = tk.StringVar(
            value=f"選択色: {color_label(self.test_color_index)} #FFFFFF"
        )
        self.correction_var = tk.StringVar(
            value="選択パネル補正: R 1.00 / G 1.00 / B 1.00 / 輝度 1.00"
        )
        self.mode_var = tk.StringVar(value="校正モード開始")
        self.pattern_var = tk.StringVar(value="単色表示開始")
        self.all_pattern_var = tk.StringVar(value="全部単色表示開始")

        self._build_header()
        self._build_panel_area()
        self._build_log()

        self.sender = PatternSender(
            on_error=lambda message: self.root.after(0, lambda: self._log(message)),
        )
        self.select_panel(self.refs[0])
        if load_remote:
            self.load_remote_configs()
        else:
            self._set_status("ローカル表示（Pi設定の読み込みを省略）")

    def _build_header(self) -> None:
        header = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        header.pack(fill="x")
        ttk.Label(
            header,
            text="72枚パネル RGB・輝度キャリブレーション",
            font=("TkDefaultFont", 16, "bold"),
        ).grid(row=0, column=0, columnspan=9, sticky="w")
        ttk.Label(header, textvariable=self.status_var).grid(
            row=1, column=0, columnspan=9, sticky="w", pady=(2, 6)
        )

        self.mode_button = ttk.Button(
            header,
            textvariable=self.mode_var,
            command=self.toggle_calibration_mode,
        )
        self.mode_button.grid(row=2, column=0, padx=(0, 6), sticky="w")
        self.pattern_button = ttk.Button(
            header,
            textvariable=self.pattern_var,
            command=self.toggle_pattern,
        )
        self.pattern_button.grid(row=2, column=1, padx=(0, 12), sticky="w")
        self.all_pattern_button = ttk.Button(
            header,
            textvariable=self.all_pattern_var,
            command=self.toggle_all_pattern,
        )
        self.all_pattern_button.grid(row=2, column=2, padx=(0, 12), sticky="w")
        ttk.Label(header, text="テスト色プリセット").grid(row=2, column=3, sticky="e")
        preset_box = ttk.Combobox(
            header,
            textvariable=self.preset_var,
            values=tuple(COLOR_INDEX),
            width=12,
            state="readonly",
        )
        preset_box.grid(row=2, column=4, padx=(4, 16), sticky="w")
        preset_box.bind("<<ComboboxSelected>>", self._preset_changed)

        self.apply_selected_button = ttk.Button(
            header,
            text="選択Piへ適用",
            command=self.apply_selected,
        )
        self.apply_selected_button.grid(row=2, column=5, padx=(0, 6), sticky="w")
        self.apply_all_button = ttk.Button(
            header,
            text="全3台へ適用",
            command=self.apply_all,
        )
        self.apply_all_button.grid(row=2, column=6, padx=(0, 6), sticky="w")
        ttk.Button(header, text="選択Piを1.00へ戻す", command=self.reset_selected).grid(
            row=2, column=7, padx=(0, 6), sticky="w"
        )
        ttk.Button(header, text="Piから再読込", command=self.reload_remote).grid(
            row=2, column=8, sticky="w"
        )

        wheel_frame = ttk.LabelFrame(
            header,
            text="選択パネルのRGB補正（色相環）",
            padding=4,
        )
        wheel_frame.grid(row=3, column=0, columnspan=9, sticky="w", pady=(6, 0))
        self.hue_wheel = HueWheel(
            wheel_frame,
            size=144,
            on_change=self._correction_changed,
        )
        self.hue_wheel.pack(side="left")
        ttk.Label(
            wheel_frame,
            text=(
                "現在選択中のパネルを補正\n"
                "中心=1.00倍、外周=最大補正\n"
                "角度=補正する色味、距離=RGB補正量\n"
                "輝度は各パネルの数値欄で調整"
            ),
        ).pack(side="left", padx=(8, 0), anchor="center")
        ttk.Label(
            wheel_frame,
            textvariable=self.correction_var,
            foreground="#174a7e",
        ).pack(side="left", padx=(12, 0), anchor="center")
        ttk.Label(
            wheel_frame,
            textvariable=self.selected_color_var,
            foreground="#555555",
        ).pack(side="left", padx=(12, 0), anchor="center")

        ttk.Label(
            header,
            textvariable=self.selected_var,
            foreground="#174a7e",
        ).grid(row=4, column=0, columnspan=9, sticky="w", pady=(6, 0))
        for column in range(9):
            header.grid_columnconfigure(column, weight=1 if column == 8 else 0)

    def _build_panel_area(self) -> None:
        outer = ttk.Frame(self.root, padding=(8, 2, 8, 2))
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.panel_area = ttk.Frame(self.canvas)
        window = self.canvas.create_window((0, 0), window=self.panel_area, anchor="nw")
        self.panel_area.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")
            ),
        )
        self.canvas.bind(
            "<Configure>",
            lambda event: self.canvas.itemconfigure(window, width=event.width),
        )
        self.canvas.bind_all("<MouseWheel>", self._mousewheel)

        style = ttk.Style(self.root)
        style.configure("Selected.TLabelframe", borderwidth=3, relief="solid")
        for target in TARGETS:
            group = ttk.LabelFrame(
                self.panel_area,
                text=(
                    f"{target.label}  {target.host}  "
                    f"target_id={target.target_id}  （24枚）"
                ),
                padding=5,
            )
            group.pack(fill="x", padx=3, pady=5)
            for column in range(PANEL_COLS):
                group.grid_columnconfigure(column, weight=1)
            for ref in (item for item in self.refs if item.display == target):
                self._build_panel_card(group, ref)

    def _build_panel_card(self, parent: "ttk.LabelFrame", ref: PanelRef) -> None:
        card = ttk.LabelFrame(
            parent,
            text=f"{ref.row},{ref.column}",
            padding=3,
        )
        card.grid(
            row=ref.row,
            column=ref.column,
            padx=3,
            pady=3,
            sticky="nsew",
        )
        self.cards[ref.key] = card
        self.panel_vars[ref.key] = {}
        ttk.Label(card, text=ref.physical_label, font=("TkDefaultFont", 8)).pack(
            anchor="w"
        )
        for channel, short, color in (
            ("red", "R", "#b51e2a"),
            ("green", "G", "#187a35"),
            ("blue", "B", "#2155a3"),
        ):
            line = ttk.Frame(card)
            line.pack(fill="x", pady=(1, 0))
            ttk.Label(line, text=short, foreground=color, width=2).pack(side="left")
            value_var = tk.StringVar(value="1.00")
            self.panel_vars[ref.key][channel] = value_var
            entry = ttk.Entry(
                line,
                textvariable=value_var,
                width=6,
                justify="right",
            )
            entry.pack(side="right", padx=(4, 0))
            entry.bind(
                "<Return>",
                lambda _event, panel=ref, name=channel: self._entry_changed(
                    panel, name
                ),
            )
            entry.bind(
                "<FocusOut>",
                lambda _event, panel=ref, name=channel: self._entry_changed(
                    panel, name
                ),
            )
        line = ttk.Frame(card)
        line.pack(fill="x", pady=(1, 0))
        ttk.Label(line, text="輝度", foreground="#8a5a00", width=3).pack(side="left")
        brightness_var = tk.StringVar(value="1.00")
        self.panel_vars[ref.key]["brightness"] = brightness_var
        brightness_entry = ttk.Entry(
            line,
            textvariable=brightness_var,
            width=6,
            justify="right",
        )
        brightness_entry.pack(side="right", padx=(4, 0))
        brightness_entry.bind(
            "<Return>",
            lambda _event, panel=ref: self._entry_changed(panel, "brightness"),
        )
        brightness_entry.bind(
            "<FocusOut>",
            lambda _event, panel=ref: self._entry_changed(panel, "brightness"),
        )
        ttk.Label(card, text="R/G/B=色味  輝度=明るさ（0.00〜2.00）", font=("TkDefaultFont", 8)).pack(
            anchor="w", pady=(2, 0)
        )
        ttk.Button(card, text="この1枚を選択", command=lambda: self.select_panel(ref)).pack(
            fill="x", pady=(3, 0)
        )

    def _build_log(self) -> None:
        frame = ttk.LabelFrame(self.root, text="操作ログ", padding=4)
        frame.pack(fill="x", padx=8, pady=(2, 8))
        self.log_text = tk.Text(frame, height=5, wrap="word", state="disabled")
        self.log_text.pack(fill="x")

    def _mousewheel(self, event: "tk.Event") -> None:
        if getattr(event, "delta", 0):
            self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def _set_status(self, message: str) -> None:
        dirty = f" / 未適用: {len(self.dirty)}台" if self.dirty else ""
        self.status_var.set(message + dirty)

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self._set_status(message)

    def select_panel(self, panel: PanelRef) -> None:
        if self.selected is not None:
            old_card = self.cards[self.selected.key]
            old_card.configure(
                style="TLabelframe",
                text=f"{self.selected.row},{self.selected.column}",
            )
        self.selected = panel
        self.cards[panel.key].configure(
            style="Selected.TLabelframe",
            text=f"★ {panel.row},{panel.column}",
        )
        self.selected_var.set(
            f"選択: {panel.display.label} / row={panel.row} col={panel.column} / "
            f"{panel.physical_label}"
        )
        self._sync_correction_wheel(panel)
        if self.pattern_active:
            self.sender.set_pattern(panel, self.test_color_index)

    def _entry_changed(self, panel: PanelRef, channel: str) -> None:
        variable = self.panel_vars[panel.key][channel]
        raw = variable.get().strip()
        try:
            value = float(raw)
        except ValueError:
            self._restore_entry(panel, channel, f"数値を入力してください: {raw}")
            return
        if not math.isfinite(value) or not 0.0 <= value <= 2.0:
            self._restore_entry(panel, channel, "倍率は0.00〜2.00で入力してください")
            return
        value = round(value, 2)
        key = (panel.lane, panel.config_chain)
        if channel == "brightness":
            self.brightnesses[panel.display.key][key] = value
            variable.set(f"{value:.2f}")
            self.dirty.add(panel.display.key)
            if self.selected is not None and self.selected.key == panel.key:
                self._sync_correction_wheel(panel)
            self._set_status("輝度を変更。『適用』でPiへ反映してください")
            return
        old = self.gains[panel.display.key][key]
        values = list(old)
        values[CHANNEL_INDEX[channel]] = value
        self.gains[panel.display.key][key] = tuple(values)  # type: ignore[assignment]
        variable.set(f"{value:.2f}")
        self.dirty.add(panel.display.key)
        if self.selected is not None and self.selected.key == panel.key:
            self._sync_correction_wheel(panel)
        self._set_status("数値変更。『適用』でPiへ反映してください")

    def _restore_entry(self, panel: PanelRef, channel: str, message: str) -> None:
        key = (panel.lane, panel.config_chain)
        if channel == "brightness":
            current = self.brightnesses[panel.display.key][key]
        else:
            current = self.gains[panel.display.key][key][CHANNEL_INDEX[channel]]
        self.panel_vars[panel.key][channel].set(f"{current:.2f}")
        self._log(f"{panel.display.label} {panel.row},{panel.column}: {message}")

    def _set_gain(
        self,
        panel: PanelRef,
        channel: str,
        value: float,
        mark_dirty: bool,
    ) -> None:
        bounded = max(0.0, min(2.0, float(value)))
        self.panel_vars[panel.key][channel].set(f"{bounded:.2f}")
        key = (panel.lane, panel.config_chain)
        old = self.gains[panel.display.key][key]
        values = list(old)
        values[CHANNEL_INDEX[channel]] = bounded
        self.gains[panel.display.key][key] = tuple(values)  # type: ignore[assignment]
        if mark_dirty:
            self.dirty.add(panel.display.key)

    def _set_brightness(
        self,
        panel: PanelRef,
        value: float,
        mark_dirty: bool,
    ) -> None:
        bounded = max(0.0, min(2.0, float(value)))
        self.panel_vars[panel.key]["brightness"].set(f"{bounded:.2f}")
        key = (panel.lane, panel.config_chain)
        self.brightnesses[panel.display.key][key] = bounded
        if mark_dirty:
            self.dirty.add(panel.display.key)

    def _sync_correction_wheel(self, panel: PanelRef) -> None:
        gains = self.gains[panel.display.key][(panel.lane, panel.config_chain)]
        brightness = self.brightnesses[panel.display.key][
            (panel.lane, panel.config_chain)
        ]
        hue, strength = hue_from_gains(gains)
        self.hue_wheel.set_correction(hue, strength)
        self.correction_var.set(
            f"{panel.display.key} row={panel.row},col={panel.column}: "
            f"R {gains[0]:.2f} / G {gains[1]:.2f} / B {gains[2]:.2f} / "
            f"輝度 {brightness:.2f}"
        )

    def _set_test_color(
        self,
        index: int,
        source: str,
        requested_rgb: tuple[int, int, int],
        log_change: bool = True,
    ) -> None:
        self.test_color_index = resolve_color_index(index)
        actual_rgb = fc6_rgb(self.test_color_index)
        requested_hex = "#%02X%02X%02X" % requested_rgb
        actual_hex = "#%02X%02X%02X" % actual_rgb
        self.selected_color_var.set(
            f"選択色: {source} {requested_hex} → "
            f"{color_label(self.test_color_index)} {actual_hex}"
        )
        if self.pattern_active and self.selected is not None:
            self.sender.set_pattern(self.selected, self.test_color_index)
        elif self.all_pattern_active:
            self.sender.set_all_pattern(self.test_color_index)
        if log_change:
            self._log(f"テスト色を{color_label(self.test_color_index)}へ変更")

    def _preset_changed(self, _event: "tk.Event") -> None:
        name = self.preset_var.get()
        if name not in COLOR_INDEX:
            return
        index = COLOR_INDEX[name]
        self._set_test_color(index, f"プリセット {name}", fc6_rgb(index))

    def _correction_changed(
        self,
        hue: float,
        strength: float,
    ) -> None:
        if self.selected is None:
            return
        gains = gains_from_hue(hue, strength)
        for channel, value in zip(CHANNELS, gains):
            self._set_gain(self.selected, channel, value, mark_dirty=True)
        brightness = self.brightnesses[self.selected.display.key][
            (self.selected.lane, self.selected.config_chain)
        ]
        self.correction_var.set(
            f"{self.selected.display.key} row={self.selected.row},col={self.selected.column}: "
            f"色相 {hue * 360.0:.0f}° / 強度 {strength * 100.0:.0f}% → "
            f"R {gains[0]:.2f} / G {gains[1]:.2f} / B {gains[2]:.2f} / "
            f"輝度 {brightness:.2f}"
        )
        self._set_status("色相環で補正値を変更。『適用』でPiへ反映してください")

    def toggle_pattern(self) -> None:
        if self.pattern_active:
            self.sender.stop()
            self.pattern_active = False
            self.pattern_var.set("単色表示開始")
            self._log("単色パターン送信を停止")
            return
        if self.all_pattern_active:
            self.sender.stop()
            self.all_pattern_active = False
            self.all_pattern_var.set("全部単色表示開始")
        if not self.calibration_mode:
            messagebox.showwarning(
                "校正モード未開始",
                "先に『校正モード開始』を押してゲーム送信を止めてください。",
            )
            return
        if self.selected is None:
            return
        self.sender.start()
        self.sender.set_pattern(self.selected, self.test_color_index)
        self.pattern_active = True
        self.pattern_var.set("単色表示停止")
        self._log(
            f"{self.selected.display.label} row={self.selected.row},col={self.selected.column} "
            f"へ{color_label(self.test_color_index)}を送信開始"
        )

    def toggle_all_pattern(self) -> None:
        if self.all_pattern_active:
            self.sender.stop()
            self.all_pattern_active = False
            self.all_pattern_var.set("全部単色表示開始")
            self._log("全72枚の単色パターン送信を停止")
            return
        if self.pattern_active:
            self.sender.stop()
            self.pattern_active = False
            self.pattern_var.set("単色表示開始")
        if not self.calibration_mode:
            messagebox.showwarning(
                "校正モード未開始",
                "先に『校正モード開始』を押してゲーム送信を止めてください。",
            )
            return
        self.sender.start()
        self.sender.set_all_pattern(self.test_color_index)
        self.all_pattern_active = True
        self.all_pattern_var.set("全部単色表示停止")
        self._log(f"全72枚へ{color_label(self.test_color_index)}を送信開始")

    def _set_remote_buttons(self, enabled: bool) -> None:
        state = "!disabled" if enabled else "disabled"
        for button in (self.apply_selected_button, self.apply_all_button):
            button.state([state])

    def _run_background(
        self,
        label: str,
        work: Callable[[], object],
        on_success: Callable[[object], None],
    ) -> None:
        def worker() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - UIへ失敗理由を返す
                message = f"{label}失敗: {exc}"
                self.root.after(0, lambda: self._log(message))
                return
            self.root.after(0, lambda: on_success(result))

        threading.Thread(target=worker, name=f"ui-{label}", daemon=True).start()

    def load_remote_configs(self) -> None:
        self.load_count = 0
        self._set_status("3台のPiから補正値を読み込み中…")
        for target in TARGETS:
            self._run_background(
                f"{target.label}設定読込",
                lambda target=target: (target, *read_remote_calibration(target)),
                self._loaded_remote_config,
            )

    def _loaded_remote_config(self, result: object) -> None:
        target, table, brightness_table = result  # type: ignore[misc]
        assert isinstance(target, DisplayTarget)
        assert isinstance(table, dict)
        assert isinstance(brightness_table, dict)
        self.gains[target.key] = table
        self.brightnesses[target.key] = brightness_table
        for panel in (item for item in self.refs if item.display == target):
            values = table[(panel.lane, panel.config_chain)]
            for channel, value in zip(CHANNELS, values):
                self._set_gain(panel, channel, value, mark_dirty=False)
            self._set_brightness(
                panel,
                brightness_table[(panel.lane, panel.config_chain)],
                mark_dirty=False,
            )
        if self.selected is not None and self.selected.display == target:
            self._sync_correction_wheel(self.selected)
        self.dirty.discard(target.key)
        self.load_count += 1
        self._log(f"{target.label}の補正値を読み込みました")
        if self.load_count == len(TARGETS):
            self._set_status("Pi設定の読み込み完了。パネルを選んで校正してください")

    def reload_remote(self) -> None:
        if self.dirty:
            proceed = messagebox.askyesno(
                "未適用の変更",
                "未適用の数値変更があります。破棄してPiから再読込しますか？",
            )
            if not proceed:
                return
        self.load_remote_configs()

    def _apply_targets(self, targets: Sequence[DisplayTarget]) -> None:
        if self.remote_job_active:
            self._log("別のPi操作が実行中です")
            return
        snapshots = {
            target.key: (
                copy_gain_table(self.gains[target.key]),
                copy_brightness_table(self.brightnesses[target.key]),
            )
            for target in targets
        }
        self.remote_job_active = True
        self._set_remote_buttons(False)
        self._set_status("Piへ設定を適用中…")

        def work() -> object:
            results: list[tuple[DisplayTarget, str | None]] = []
            for target in targets:
                try:
                    gains, brightnesses = snapshots[target.key]
                    apply_remote_gain_table(target, gains, brightnesses)
                except Exception as exc:  # noqa: BLE001 - Piごとに結果を残す
                    results.append((target, str(exc)))
                else:
                    results.append((target, None))
            return results

        self._run_background("Pi設定適用", work, self._applied_targets)

    def _applied_targets(self, result: object) -> None:
        results = result  # type: ignore[assignment]
        success = 0
        for target, error in results:
            if error is None:
                self.dirty.discard(target.key)
                success += 1
                self._log(f"{target.label}: 補正値を書込み、{target.service}を再起動しました")
            else:
                self._log(f"{target.label}: 適用失敗: {error}")
        self.remote_job_active = False
        self._set_remote_buttons(True)
        self._set_status(f"適用完了 {success}/{len(results)}台")

    def apply_selected(self) -> None:
        if self.selected is None:
            return
        self._apply_targets((self.selected.display,))

    def apply_all(self) -> None:
        self._apply_targets(TARGETS)

    def reset_selected(self) -> None:
        if self.selected is None:
            return
        target = self.selected.display
        for panel in (item for item in self.refs if item.display == target):
            for channel in CHANNELS:
                self._set_gain(panel, channel, 1.0, mark_dirty=True)
            self._set_brightness(panel, 1.0, mark_dirty=True)
        self._sync_correction_wheel(self.selected)
        self._log(f"{target.label}の24枚を1.00倍へ戻しました（未適用）")

    def toggle_calibration_mode(self) -> None:
        if self.remote_job_active:
            self._log("Pi操作中は校正モードを切り替えられません")
            return
        self.mode_button.state(["disabled"])
        if self.calibration_mode:
            self.sender.stop()
            self.pattern_active = False
            self.all_pattern_active = False
            self.pattern_var.set("単色表示開始")
            self.all_pattern_var.set("全部単色表示開始")
            should_start = self.control_was_active is True

            def finish(_result: object) -> None:
                self.calibration_mode = False
                self.control_was_active = None
                self.mode_var.set("校正モード開始")
                self.mode_button.state(["!disabled"])
                self._log("校正モード終了。ゲーム送信を復帰しました")

            if should_start:
                self._run_background(
                    "ゲーム送信復帰",
                    lambda: change_control_service("start"),
                    finish,
                )
            else:
                finish(None)
            return

        def entered(result: object) -> None:
            before, _after = result  # type: ignore[misc]
            self.control_was_active = before == "active"
            self.calibration_mode = True
            self.mode_var.set("校正モード終了")
            self.mode_button.state(["!disabled"])
            self._log("校正モード開始。pi3-control.serviceを停止しました")

        self._run_background(
            "ゲーム送信停止",
            lambda: change_control_service("stop"),
            entered,
        )

    def close(self) -> None:
        if self.dirty and messagebox is not None:
            proceed = messagebox.askyesno(
                "未適用の変更",
                "未適用の変更があります。保存せず終了しますか？",
            )
            if not proceed:
                return
        self.sender.close()
        if self.calibration_mode and self.control_was_active:
            try:
                change_control_service("start")
                self._log("終了処理でゲーム送信を復帰しました")
            except Exception as exc:  # noqa: BLE001 - 終了時にも明示する
                self._log(f"ゲーム送信の復帰に失敗: {exc}")
        self.root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="72枚の32x32 LEDパネルを個別にRGB・輝度補正するPC UI（macOS / Windows）"
    )
    parser.add_argument(
        "--no-remote-load",
        action="store_true",
        help="起動時のPi設定読み込みを行わず、全数値入力を1.00で開始する",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if tk is None:
        print(
            "error: tkinterが使えません。Tk対応のPythonで実行してください。",
            file=sys.stderr,
        )
        return 2
    root = tk.Tk()
    app = CalibrationApp(root, load_remote=not args.no_remote_load)
    root.mainloop()
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
