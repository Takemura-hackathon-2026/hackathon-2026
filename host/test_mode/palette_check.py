#!/usr/bin/env python3
"""全色テストパターン（計画書 §13 実装開始条件 2・3）。

FC6 52 色 / MSX16 16 色を 192x384 の論理画面へ並べ、主機プレビューと
LED 実機表示のインデックス一致を目視確認するための静止パターンを出す。
インデックスは左上から行優先で並び、Pi 境界（Y=128/256）に区切り線を引く。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from palettes import (  # noqa: E402
    FC6,
    FC6_BLACK,
    FC6_LIMIT,
    FC6_WHITE,
    MSX16,
    MSX16_BLACK,
    MSX16_LIMIT,
    PaletteMode,
)
from test_mode import (  # noqa: E402
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    PI_COUNT,
    PI_HEIGHT,
    UdpFrameSender,
    parse_pi,
)

COLUMNS = 4


def build_pattern(mode: PaletteMode) -> np.ndarray:
    """全インデックスのカラーバーを生成する。"""
    if mode == PaletteMode.FC6:
        indices = list(range(FC6_LIMIT))
        background, marker = FC6_BLACK, FC6_WHITE
    else:
        # MSX16 のインデックス 0 は透明であり、送出値としては使わない。
        indices = list(range(1, MSX16_LIMIT))
        background, marker = MSX16_BLACK, MSX16_LIMIT - 1

    frame = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), background, dtype=np.uint8)
    rows = (len(indices) + COLUMNS - 1) // COLUMNS
    cell_w = CANVAS_WIDTH // COLUMNS
    cell_h = CANVAS_HEIGHT // rows
    for position, index in enumerate(indices):
        row, column = divmod(position, COLUMNS)
        y0 = row * cell_h
        x0 = column * cell_w
        frame[y0:y0 + cell_h - 1, x0:x0 + cell_w - 1] = index

    # Pi 境界に 1px の目印を入れ、上下の切れ目を実機で確認できるようにする。
    for target_id in range(1, PI_COUNT):
        frame[target_id * PI_HEIGHT, :] = marker
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description="FC6/MSX16 の全色テストパターン")
    parser.add_argument("--palette", choices=("fc6", "msx16"), default="fc6")
    parser.add_argument("--preview-scale", type=int, default=2)
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument("--save", type=Path, default=None, help="プレビュー画像を PNG 保存する")
    parser.add_argument("--seconds", type=float, default=0.0, help="送信を継続する秒数")
    parser.add_argument("--fps", type=float, default=60.0)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--pi", action="append", default=[], metavar="HOST[:PORT]")
    parser.add_argument("--chunk-size", type=int, default=1200)
    args = parser.parse_args()

    mode = PaletteMode.FC6 if args.palette == "fc6" else PaletteMode.MSX16
    frame = build_pattern(mode)
    palette = FC6 if mode == PaletteMode.FC6 else MSX16
    lut = np.asarray([entry[:3] for entry in palette], dtype=np.uint8)
    preview = lut[frame][:, :, ::-1]
    if args.preview_scale != 1:
        preview = cv2.resize(
            preview,
            (CANVAS_WIDTH * args.preview_scale, CANVAS_HEIGHT * args.preview_scale),
            interpolation=cv2.INTER_NEAREST,
        )

    if args.save is not None:
        cv2.imwrite(str(args.save), preview)
        print(f"saved: {args.save}")

    sender = None
    if args.send:
        if len(args.pi) != PI_COUNT:
            print(f"error: --send には --pi をちょうど {PI_COUNT} 個指定する", file=sys.stderr)
            return 2
        sender = UdpFrameSender([parse_pi(item) for item in args.pi], args.chunk_size)

    print(
        f"pattern: {mode.name}, colors={FC6_LIMIT if mode == PaletteMode.FC6 else MSX16_LIMIT - 1}, "
        f"range=0x00-{frame.max():#04x}"
    )

    try:
        if sender is not None:
            deadline = time.monotonic() + max(args.seconds, 1.0)
            frame_id = 0
            while time.monotonic() < deadline:
                sender.send(frame_id, mode, frame)
                frame_id += 1
                time.sleep(1.0 / args.fps)
            print(f"sent {frame_id} frames")
        if not args.no_preview:
            cv2.imshow("palette check", preview)
            print("keys: q/ESC 終了")
            while True:
                key = cv2.waitKey(50) & 0xFF
                if key in (27, ord("q")):
                    break
    finally:
        if sender is not None:
            sender.close()
        if not args.no_preview:
            cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
