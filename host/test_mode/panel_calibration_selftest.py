#!/usr/bin/env python3
"""32x32パネル校正UDPツールの機械的自己テスト。"""
from __future__ import annotations

import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from panel_calibration import (  # noqa: E402
    COLOR_INDEX,
    HEADER,
    MAGIC,
    WIDTH,
    build_frame,
    make_packets,
    parse_panel,
)


def main() -> int:
    errors: list[str] = []
    black = COLOR_INDEX["black"]
    white = COLOR_INDEX["white"]
    frame = build_frame(WIDTH, 128, white, black, (1, 2))
    if len(frame) != WIDTH * 128:
        errors.append(f"フレーム長が不正: {len(frame)}")
    selected = 0
    for row in range(128):
        for column in range(WIDTH):
            expected = white if 32 <= row < 64 and 64 <= column < 96 else black
            if frame[row * WIDTH + column] != expected:
                selected += 1
    if selected:
        errors.append(f"選択パネル以外を含むフレームになっている: {selected}")

    packets = make_packets(2, 12345, frame, 1200)
    rebuilt = bytearray()
    for index, packet in enumerate(packets):
        header = HEADER.unpack_from(packet)
        magic, frame_id, target, palette, chunk_id, count, size, crc = header
        body = packet[HEADER.size:]
        if (magic, frame_id, target, palette, chunk_id, count, size) != (
            MAGIC,
            12345,
            2,
            0,
            index,
            len(packets),
            len(body),
        ):
            errors.append(f"ヘッダーが不正: packet={index} header={header}")
        if zlib.crc32(body) & 0xFFFFFFFF != crc:
            errors.append(f"CRC32が不一致: packet={index}")
        rebuilt.extend(body)
    if bytes(rebuilt) != frame:
        errors.append("パケット再結合が元フレームと一致しない")

    if parse_panel("all") is not None or parse_panel("2x3") != (2, 3):
        errors.append("パネル指定の解析が不正")

    for message in errors:
        print(f"ERROR: {message}")
    print(f"{len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
