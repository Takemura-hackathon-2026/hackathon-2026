#!/usr/bin/env python3
"""ホスト常時起動用の待機表示。

既定では現在時刻と主機・Piの温度を192x384の縦画面へ表示する。
``--mode palette`` はFC6全52色を8x8タイルへ並べた市松状グラデーション、
``--mode logo`` は既定のPNGから白い余白を除いたロゴ領域を表示する。
ゲームや校正の処理はここでは起動しない。
"""
from __future__ import annotations

import argparse
import math
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np

HOST_ROOT = Path(__file__).resolve().parent
TEST_MODE_ROOT = HOST_ROOT / "test_mode"
if str(TEST_MODE_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_MODE_ROOT))
if str(HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(HOST_ROOT))

from palettes import FC6, FC6_BLACK, FC6_LIMIT, PaletteMode  # noqa: E402
from test_mode import (  # noqa: E402
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    DEFAULT_IMAGE,
    PI_COUNT,
    UdpFrameSender,
    crop_logo,
    fit_image,
    load_webp,
    parse_int,
    parse_pi,
    quantize_to_palette,
)

MAX_LOGO_WIDTH = 160
MAX_LOGO_HEIGHT = 320
GRID_COLUMNS = 8
GRID_ROWS = 8
GRID_CELL_WIDTH = CANVAS_WIDTH // GRID_COLUMNS
GRID_CELL_HEIGHT = CANVAS_HEIGHT // GRID_ROWS
HEALTH_PORT = 5101
HEALTH_TIMEOUT = 3.0
STATUS_LABEL = 0x32
STATUS_TEXT = 0x33
STATUS_OK = 0x15
STATUS_WARN = 0x0E
STATUS_ERROR = 0x04


def read_temperature_c() -> float | None:
    """Linux thermal sysfsから主機またはPiの温度を読む。"""
    candidates = [Path("/sys/class/thermal/thermal_zone0/temp")]
    candidates.extend(sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")))
    candidates.extend(sorted(Path("/sys/class/hwmon").glob("hwmon*/temp*_input")))
    for path in candidates:
        try:
            raw = int(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            continue
        if 10_000 <= raw <= 200_000:
            return raw / 1000.0
        if -50 <= raw <= 200:
            return float(raw)
    return None


def format_temperature(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "--.-C"
    return f"{value:4.1f}C"


class HealthReceiver:
    """各PiからのPIHEALTH報告を受け取る。"""

    def __init__(self, port: int = HEALTH_PORT) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("0.0.0.0", port))
        self.socket.setblocking(False)
        self.reports: dict[int, dict[str, str]] = {}
        self.last_seen: dict[int, float] = {}

    def poll(self) -> None:
        while True:
            try:
                packet, _address = self.socket.recvfrom(512)
            except BlockingIOError:
                return
            except OSError:
                return
            text = packet.decode("ascii", "ignore")
            if not text.startswith("PIHEALTH"):
                continue
            fields: dict[str, str] = {}
            for token in text.split()[1:]:
                if "=" in token:
                    key, value = token.split("=", 1)
                    fields[key] = value
            try:
                target = int(fields["target"])
            except (KeyError, ValueError):
                continue
            if 0 <= target < PI_COUNT:
                self.reports[target] = fields
                self.last_seen[target] = time.monotonic()

    def status(self, target: int) -> tuple[str, dict[str, str]]:
        seen = self.last_seen.get(target)
        if seen is None:
            return "NO SIGNAL", {}
        fields = self.reports.get(target, {})
        if time.monotonic() - seen > HEALTH_TIMEOUT:
            return "LOST", fields
        return "OK", fields

    def close(self) -> None:
        self.socket.close()


def render_status_frame(now: float, health: HealthReceiver, background: int = FC6_BLACK) -> np.ndarray:
    """時刻・主機温度・Pi温度を192x384の縦画面へ描画する。"""
    if not 0 <= background < FC6_LIMIT:
        raise ValueError(f"背景インデックス {background:#x} はFC6範囲外")
    frame = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), background, dtype=np.uint8)

    def text(value: str, x: int, y: int, color: int, scale: float, thickness: int = 1) -> None:
        mask = np.zeros(frame.shape, dtype=np.uint8)
        cv2.putText(mask, value, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, 255, thickness, cv2.LINE_AA)
        frame[mask > 96] = color

    text("LED STATUS", 5, 16, STATUS_OK, 0.36)
    text(time.strftime("%H:%M:%S", time.localtime(now)), 5, 55, STATUS_TEXT, 0.62, 2)
    text("HOST TEMP", 6, 92, STATUS_LABEL, 0.30)
    text(format_temperature(read_temperature_c()), 6, 119, STATUS_TEXT, 0.48)

    for target in range(PI_COUNT):
        state, fields = health.status(target)
        if state == "OK":
            color = STATUS_OK
        elif state == "LOST":
            color = STATUS_WARN
        else:
            color = STATUS_ERROR
        y = 154 + target * 54
        text(f"PI{target + 1}", 6, y, color, 0.36)
        try:
            temperature = float(fields.get("temp_c", fields.get("temp", "nan")))
        except ValueError:
            temperature = math.nan
        text(f"{state} {format_temperature(temperature)}", 6, y + 26, STATUS_TEXT, 0.34)
    return frame


def build_palette_checkerboard_frame(background: int = FC6_BLACK) -> np.ndarray:
    """FC6全色を左上から8x8の市松状タイルへ行優先で配置する。"""
    if not 0 <= background < FC6_LIMIT:
        raise ValueError(f"背景インデックス {background:#x} はFC6範囲外")

    # FC6の52色を一度ずつ使い、64マスに足りない12マスは背景で埋める。
    indices = list(range(FC6_LIMIT))
    indices.extend([background] * (GRID_COLUMNS * GRID_ROWS - len(indices)))
    frame = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), background, dtype=np.uint8)
    for position, index in enumerate(indices):
        row, column = divmod(position, GRID_COLUMNS)
        y0 = row * GRID_CELL_HEIGHT
        x0 = column * GRID_CELL_WIDTH
        y1 = y0 + GRID_CELL_HEIGHT
        x1 = x0 + GRID_CELL_WIDTH
        # 奇数タイルだけ1px内側へ入れ、隣接タイルとの境界を市松状に見せる。
        inset = 1 if (row + column) % 2 else 0
        frame[y0 + inset:y1 - inset, x0 + inset:x1 - inset] = index
    return frame


def build_logo_frame(image_path: Path, background: int | None = None) -> tuple[np.ndarray, tuple[int, int]]:
    """ロゴ素材を中央配置したFC6インデックスフレームを返す。"""
    rgb, opaque = load_webp(image_path)
    if image_path.resolve() == DEFAULT_IMAGE.resolve():
        rgb, opaque = crop_logo(rgb, opaque)
    rgb, opaque = fit_image(rgb, opaque, MAX_LOGO_WIDTH, MAX_LOGO_HEIGHT)

    background_index = FC6_BLACK if background is None else int(background)
    if not 0 <= background_index < FC6_LIMIT:
        raise ValueError(f"背景インデックス {background_index:#x} はFC6範囲外")

    indexed = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), background_index, dtype=np.uint8)
    quantized = quantize_to_palette(rgb, PaletteMode.FC6)
    height, width = opaque.shape
    x0 = (CANVAS_WIDTH - width) // 2
    y0 = (CANVAS_HEIGHT - height) // 2
    indexed[y0:y0 + height, x0:x0 + width][opaque] = quantized[opaque]
    return indexed, (width, height)


def build_waiting_frame(
    mode: str,
    image_path: Path,
    background: int | None = None,
) -> tuple[np.ndarray, str]:
    """待機モードに応じたFC6フレームと表示名を返す。"""
    background_index = FC6_BLACK if background is None else int(background)
    if mode == "palette":
        return build_palette_checkerboard_frame(background_index), "palette-8x8"
    frame, size = build_logo_frame(image_path, background_index)
    return frame, f"logo-{size[0]}x{size[1]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ホスト常時起動用のFC6待機表示")
    parser.add_argument(
        "--mode",
        choices=("status", "palette", "logo"),
        default="status",
        help="status=時刻・温度, palette=全52色の8x8市松状グラデーション, logo=ロゴ表示",
    )
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE, help="ロゴ素材（PNG/WebP）")
    parser.add_argument("--background", type=parse_int, default=None, help="FC6背景インデックス（例: 0x30）")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--frames", type=int, default=0, help="指定フレーム数で終了。0は無制限")
    parser.add_argument("--seconds", type=float, default=0.0, help="指定秒数で終了。0は無制限")
    parser.add_argument("--health-port", type=int, default=HEALTH_PORT, help="Pi温度報告のUDPポート")
    parser.add_argument("--send", action="store_true", help=f"{PI_COUNT}台のPiへUDP送信する")
    parser.add_argument("--pi", action="append", default=[], metavar="HOST[:PORT]")
    parser.add_argument("--chunk-size", type=int, default=1200)
    parser.add_argument("--preview", action="store_true", help="OpenCVプレビューを表示する")
    parser.add_argument("--preview-scale", type=int, default=2)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        args.fps <= 0
        or args.preview_scale <= 0
        or args.frames < 0
        or args.seconds < 0
        or args.health_port <= 0
        or (args.send and len(args.pi) != PI_COUNT)
    ):
        print("error: fps/preview-scale/frames/seconds または --pi の指定が不正", file=sys.stderr)
        return 2

    health: HealthReceiver | None = None
    background_index = FC6_BLACK if args.background is None else int(args.background)
    try:
        if args.mode == "status":
            if not 0 <= background_index < FC6_LIMIT:
                raise ValueError(f"背景インデックス {background_index:#x} はFC6範囲外")
            health = HealthReceiver(args.health_port)
            indexed = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), background_index, dtype=np.uint8)
            screen_name = "status-384"
        else:
            indexed, screen_name = build_waiting_frame(args.mode, args.image, background_index)
        sender = (
            UdpFrameSender([parse_pi(value) for value in args.pi], args.chunk_size)
            if args.send
            else None
        )
    except (OSError, RuntimeError, ValueError) as exc:
        if health is not None:
            health.close()
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if indexed.shape != (CANVAS_HEIGHT, CANVAS_WIDTH) or int(indexed.max()) >= FC6_LIMIT:
        print("error: 待機フレームがFC6の192x384条件を満たさない", file=sys.stderr)
        if sender is not None:
            sender.close()
        return 2

    running = True

    def stop_handler(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    palette_lut = np.asarray([entry[:3] for entry in FC6], dtype=np.uint8)
    display = np.empty((0, 0, 3), dtype=np.uint8)

    print(
        f"standby: screen={screen_name} image={args.image} "
        f"canvas={CANVAS_WIDTH}x{CANVAS_HEIGHT} fps={args.fps:g} send={'yes' if sender else 'no'}"
    )
    frame_id = 0
    started = deadline = time.monotonic()
    period = 1.0 / args.fps
    try:
        while running and (args.frames <= 0 or frame_id < args.frames):
            now = time.monotonic()
            if args.seconds > 0 and now - started >= args.seconds:
                break
            if health is not None:
                health.poll()
                indexed = render_status_frame(time.time(), health, background_index)
                display = palette_lut[indexed][:, :, ::-1]
                if args.preview_scale != 1:
                    display = cv2.resize(
                        display,
                        (CANVAS_WIDTH * args.preview_scale, CANVAS_HEIGHT * args.preview_scale),
                        interpolation=cv2.INTER_NEAREST,
                    )
            elif display.size == 0:
                display = palette_lut[indexed][:, :, ::-1]
                if args.preview_scale != 1:
                    display = cv2.resize(
                        display,
                        (CANVAS_WIDTH * args.preview_scale, CANVAS_HEIGHT * args.preview_scale),
                        interpolation=cv2.INTER_NEAREST,
                    )
            if sender is not None:
                sender.send(frame_id, PaletteMode.FC6, indexed)
            if args.preview:
                cv2.imshow("RGB LED standby", display)
                if (cv2.waitKey(1) & 0xFF) in (27, ord("q")):
                    running = False
            frame_id += 1
            deadline += period
            sleep_time = deadline - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            elif sleep_time < -period:
                deadline = time.monotonic()
    finally:
        if sender is not None:
            sender.close()
        if health is not None:
            health.close()
        if args.preview:
            cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
