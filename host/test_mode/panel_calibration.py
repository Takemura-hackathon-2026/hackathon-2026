#!/usr/bin/env python3
"""1台の表示Piへ単色フレームを送り、パネル個体差を調整する。"""
from __future__ import annotations

import argparse
import math
import socket
import struct
import time
import zlib

# pi-client.cc と同じUDPフォーマット。1台だけを独立して校正できる。
MAGIC = 0x524C4544  # "RLED"
HEADER = struct.Struct("!IIBBHHHI")
WIDTH = 192
HEIGHT = 96
PALETTE_FC6 = 0
COLOR_INDEX = {
    "black": 48,
    "red": 4,
    "green": 17,
    "blue": 36,
    "white": 51,
    "gray": 50,
}


def parse_destination(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator or not host or not port.isdecimal():
        raise argparse.ArgumentTypeError("HOST:PORT 形式で指定してください")
    number = int(port)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("ポートは1〜65535です")
    return host, number


def send_frame(
    sock: socket.socket,
    destination: tuple[str, int],
    target_id: int,
    frame_id: int,
    frame: bytes,
    chunk_size: int,
) -> None:
    chunk_count = math.ceil(len(frame) / chunk_size)
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
        sock.sendto(header + chunk, destination)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="1台の表示PiへFC6単色フレームを繰り返し送り、色補正を確認する。"
    )
    parser.add_argument("--pi", required=True, type=parse_destination, help="表示PiのHOST:PORT")
    parser.add_argument("--target-id", required=True, type=int, choices=range(4))
    parser.add_argument("--color", choices=COLOR_INDEX, default="white")
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--seconds", type=float, default=0.0, help="0ならCtrl-Cまで送信")
    parser.add_argument("--chunk-size", type=int, default=1200)
    args = parser.parse_args()
    if not 1.0 <= args.fps <= 60.0:
        parser.error("--fps は1〜60です")
    if args.seconds < 0:
        parser.error("--seconds は0以上です")
    if not 256 <= args.chunk_size <= 1400:
        parser.error("--chunk-size は256〜1400です")

    frame = bytes([COLOR_INDEX[args.color]]) * (WIDTH * HEIGHT)
    interval = 1.0 / args.fps
    deadline = None if args.seconds == 0 else time.monotonic() + args.seconds
    print(
        f"calibration: target={args.target_id} pi={args.pi[0]}:{args.pi[1]} "
        f"color={args.color}; Ctrl-Cで停止"
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    frame_id = 0
    try:
        while deadline is None or time.monotonic() < deadline:
            started = time.monotonic()
            send_frame(sock, args.pi, args.target_id, frame_id, frame, args.chunk_size)
            frame_id += 1
            time.sleep(max(0.0, interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
