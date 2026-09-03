#!/usr/bin/env python3
"""32x32パネルを1枚ずつ単色表示するためのUDP校正ツール。"""
from __future__ import annotations

import argparse
import math
import socket
import struct
import time
import zlib

MAGIC = 0x524C4544  # "RLED"
HEADER = struct.Struct("!IIBBHHHI")
WIDTH = 192
DEFAULT_SLICE_HEIGHT = 128
PANEL_SIZE = 32
PALETTE_FC6 = 0

# host/palettes.py / pi-client/pi_client.cc と同じFC6インデックス。
COLOR_INDEX = {
    "black": 0x30,
    "red": 0x04,
    "green": 0x11,
    "blue": 0x24,
    "gray": 0x31,
    "light-gray": 0x32,
    "white": 0x33,
}


def parse_destination(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator or not host or not port.isdecimal():
        raise argparse.ArgumentTypeError("HOST:PORT 形式で指定してください")
    number = int(port)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("ポートは1〜65535です")
    return host, number


def parse_panel(value: str) -> tuple[int, int] | None:
    if value.lower() == "all":
        return None
    parts = value.replace("x", ",").split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("ROW,COL または all で指定してください")
    try:
        row, column = (int(part.strip()) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ROW,COL は整数で指定してください") from exc
    if row < 0 or column < 0:
        raise argparse.ArgumentTypeError("ROW,COL は0以上で指定してください")
    return row, column


def build_frame(
    width: int,
    height: int,
    color_index: int,
    background_index: int,
    panel: tuple[int, int] | None,
) -> bytes:
    if width <= 0 or width % PANEL_SIZE != 0:
        raise ValueError("width は32の倍数で指定してください")
    if height <= 0 or height % PANEL_SIZE != 0:
        raise ValueError("slice height は32の倍数で指定してください")
    if panel is None:
        return bytes([color_index]) * (width * height)

    panel_row, panel_column = panel
    rows = height // PANEL_SIZE
    columns = width // PANEL_SIZE
    if panel_row >= rows or panel_column >= columns:
        raise ValueError(
            f"panel {panel_row},{panel_column} は範囲外です（row=0..{rows - 1}, col=0..{columns - 1}）"
        )
    frame = bytearray([background_index]) * (width * height)
    color = bytes([color_index]) * PANEL_SIZE
    x0 = panel_column * PANEL_SIZE
    y0 = panel_row * PANEL_SIZE
    for y in range(y0, y0 + PANEL_SIZE):
        offset = y * width + x0
        frame[offset:offset + PANEL_SIZE] = color
    return bytes(frame)


def make_packets(
    target_id: int,
    frame_id: int,
    frame: bytes,
    chunk_size: int,
) -> tuple[bytes, ...]:
    if not 0 <= target_id <= 3:
        raise ValueError("target_id は0〜3です")
    if not 256 <= chunk_size <= 1400:
        raise ValueError("chunk_size は256〜1400です")
    if not frame:
        raise ValueError("空フレームは送信できません")
    chunk_count = math.ceil(len(frame) / chunk_size)
    packets = []
    for chunk_id in range(chunk_count):
        start = chunk_id * chunk_size
        chunk = frame[start:start + chunk_size]
        header = HEADER.pack(
            MAGIC,
            frame_id & 0xFFFFFFFF,
            target_id,
            PALETTE_FC6,
            chunk_id,
            chunk_count,
            len(chunk),
            zlib.crc32(chunk) & 0xFFFFFFFF,
        )
        packets.append(header + chunk)
    return tuple(packets)


def send_frame(
    sock: socket.socket,
    destination: tuple[str, int],
    target_id: int,
    frame_id: int,
    frame: bytes,
    chunk_size: int,
) -> None:
    for packet in make_packets(target_id, frame_id, frame, chunk_size):
        sock.sendto(packet, destination)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="表示Piの32x32パネルを1枚ずつ単色表示して色補正を確認する"
    )
    parser.add_argument("--pi", required=True, type=parse_destination, help="表示PiのHOST:PORT")
    parser.add_argument("--target-id", required=True, type=int, choices=range(4))
    parser.add_argument(
        "--panel",
        type=parse_panel,
        default=None,
        metavar="ROW,COL|all",
        help="論理スライス上の32x32パネル。既定はall",
    )
    parser.add_argument("--slice-height", type=int, default=DEFAULT_SLICE_HEIGHT)
    parser.add_argument("--color", choices=COLOR_INDEX, default="white")
    parser.add_argument("--background", choices=COLOR_INDEX, default="black")
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--seconds", type=float, default=0.0, help="0ならCtrl-Cまで送信")
    parser.add_argument("--frame-id", type=int, default=None, help="開始frame_id。未指定なら現在時刻から生成")
    parser.add_argument("--chunk-size", type=int, default=1200)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1.0 <= args.fps <= 60.0:
        build_parser().error("--fps は1〜60です")
    if args.seconds < 0:
        build_parser().error("--seconds は0以上です")
    if args.slice_height <= 0 or args.slice_height % PANEL_SIZE != 0:
        build_parser().error("--slice-height は32の倍数です")
    if not 256 <= args.chunk_size <= 1400:
        build_parser().error("--chunk-size は256〜1400です")

    panel = args.panel
    try:
        frame = build_frame(
            WIDTH,
            args.slice_height,
            COLOR_INDEX[args.color],
            COLOR_INDEX[args.background],
            panel,
        )
    except ValueError as exc:
        build_parser().error(str(exc))

    interval = 1.0 / args.fps
    deadline = None if args.seconds == 0 else time.monotonic() + args.seconds
    frame_id = (
        args.frame_id & 0xFFFFFFFF
        if args.frame_id is not None
        else int(time.monotonic() * 1000) & 0xFFFFFFFF
    )
    panel_label = "all" if panel is None else f"{panel[0]},{panel[1]}"
    print(
        f"calibration: target={args.target_id} pi={args.pi[0]}:{args.pi[1]} "
        f"slice={WIDTH}x{args.slice_height} panel={panel_label} "
        f"color={args.color} background={args.background}; Ctrl-Cで停止"
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        while deadline is None or time.monotonic() < deadline:
            started = time.monotonic()
            send_frame(sock, args.pi, args.target_id, frame_id, frame, args.chunk_size)
            frame_id = (frame_id + 1) & 0xFFFFFFFF
            time.sleep(max(0.0, interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
