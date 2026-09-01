#!/usr/bin/env python3
"""Development-only UDP receiver that reconstructs three Pi slices on one PC."""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
import zlib
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
from palettes import FC6, MSX16, PaletteMode  # noqa: E402

WIDTH, HEIGHT, PI_COUNT, PI_HEIGHT = 192, 384, 3, 128
MAGIC = 0x524C4544
HEADER = struct.Struct("!IIBBHHHI")


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview packets emitted by test_mode.py")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--scale", type=int, default=2)
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.05)

    # (frame_id, target_id) -> {chunk_id: payload}; metadata stores count/mode/time.
    chunks: dict[tuple[int, int], dict[int, bytes]] = defaultdict(dict)
    meta: dict[tuple[int, int], tuple[int, int, float]] = {}
    slices: dict[int, tuple[int, int, bytes]] = {}
    latest_complete = -1

    print(f"listening on UDP {args.port}; q/ESC quits")
    try:
        while True:
            try:
                packet, _addr = sock.recvfrom(2048)
            except socket.timeout:
                packet = b""

            if packet and len(packet) >= HEADER.size:
                magic, frame_id, target, mode, chunk_id, count, size, crc = HEADER.unpack_from(packet)
                payload = packet[HEADER.size:]
                if (
                    magic == MAGIC
                    and 0 <= target < PI_COUNT
                    and mode in (0, 1)
                    and len(payload) == size
                    and zlib.crc32(payload) & 0xFFFFFFFF == crc
                    and chunk_id < count
                ):
                    key = (frame_id, target)
                    chunks[key][chunk_id] = payload
                    meta[key] = (count, mode, time.monotonic())
                    if len(chunks[key]) == count:
                        assembled = b"".join(chunks[key][i] for i in range(count))
                        if len(assembled) == WIDTH * PI_HEIGHT:
                            slices[target] = (frame_id, mode, assembled)
                        chunks.pop(key, None)
                        meta.pop(key, None)

            # Expire incomplete frames after 250ms.
            cutoff = time.monotonic() - 0.25
            for key, (_count, _mode, timestamp) in list(meta.items()):
                if timestamp < cutoff:
                    meta.pop(key, None)
                    chunks.pop(key, None)

            # Only display a frame when all three slices have exactly the same frame_id/mode.
            if len(slices) == PI_COUNT:
                frame_ids = {item[0] for item in slices.values()}
                modes = {item[1] for item in slices.values()}
                if len(frame_ids) == 1 and len(modes) == 1:
                    frame_id = next(iter(frame_ids))
                    mode = next(iter(modes))
                    if frame_id > latest_complete:
                        indexed = np.vstack([
                            np.frombuffer(slices[i][2], dtype=np.uint8).reshape(PI_HEIGHT, WIDTH)
                            for i in range(PI_COUNT)
                        ])
                        palette = FC6 if mode == int(PaletteMode.FC6) else MSX16
                        lut = np.asarray([entry[:3] for entry in palette], dtype=np.uint8)
                        if indexed.max(initial=0) < len(lut):
                            image = lut[indexed][:, :, ::-1]
                            image = cv2.resize(
                                image,
                                (WIDTH * args.scale, HEIGHT * args.scale),
                                interpolation=cv2.INTER_NEAREST,
                            )
                            cv2.imshow("UDP three-Pi preview", image)
                            latest_complete = frame_id

            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
    finally:
        sock.close()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
