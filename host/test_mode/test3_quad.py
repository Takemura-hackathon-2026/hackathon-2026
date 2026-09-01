#!/usr/bin/env python3
"""TEST3: Pi分割モード。各 Pi の担当領域へ同じ画像を1枚ずつ表示する。

192x384 の論理画面を 192x128 の 3 帯に分け、それぞれへ同一画像を描く。
1 台ずつの色再現・向き・欠けを個別に確認するためのモード。

主機で画像をパレット量子化し、各帯へ配置してから Pi 台数ぶんに分割して送る。
Pi 側の処理は通常運用と同じ（受信・LUT変換・HUB75出力のみ）。
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from palettes import (  # noqa: E402
    FC6,
    FC6_BLACK,
    FC6_WHITE,
    MSX16,
    MSX16_BLACK,
    PaletteMode,
)
from profiler import (  # noqa: E402
    Profiler,
    add_profile_arguments,
    finish_profile,
)
from test_mode import (  # noqa: E402
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    PI_COUNT,
    PI_HEIGHT,
    UdpFrameSender,
    load_webp,
    parse_int,
    parse_pi,
    quantize_to_palette,
)

LABEL_COLOR = {PaletteMode.FC6: FC6_WHITE, PaletteMode.MSX16: 0x0F}
MARKER_COLOR = {PaletteMode.FC6: 0x15, PaletteMode.MSX16: 0x02}  # 緑


def fit_to_slice(
    rgb: np.ndarray, opaque: np.ndarray, width: int, height: int, stretch: bool
) -> tuple[np.ndarray, np.ndarray, int, int]:
    """1 帯へ収まる大きさへ変換し、(画像, 不透明マスク, x原点, y原点) を返す。"""
    source_h, source_w = opaque.shape
    if stretch:
        new_w, new_h = width, height
    else:
        scale = min(width / source_w, height / source_h)
        new_w = max(1, int(source_w * scale))
        new_h = max(1, int(source_h * scale))
    resized = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)
    mask = cv2.resize(
        opaque.astype(np.uint8), (new_w, new_h), interpolation=cv2.INTER_NEAREST
    ).astype(bool)
    return resized, mask, (width - new_w) // 2, (height - new_h) // 2


def build_frame(
    rgb: np.ndarray,
    opaque: np.ndarray,
    palette_mode: PaletteMode,
    background: int,
    stretch: bool,
    label: bool,
    marker: bool,
) -> np.ndarray:
    frame = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), background, dtype=np.uint8)
    fitted, mask, x0, y0 = fit_to_slice(rgb, opaque, CANVAS_WIDTH, PI_HEIGHT, stretch)
    indexed = quantize_to_palette(fitted, palette_mode)

    for target_id in range(PI_COUNT):
        top = target_id * PI_HEIGHT
        region = frame[top + y0:top + y0 + fitted.shape[0], x0:x0 + fitted.shape[1]]
        region[mask] = indexed[mask]

        if marker:
            # 帯の四隅へ 3px の目印を置き、欠けと向きを判定できるようにする。
            color = MARKER_COLOR[palette_mode]
            frame[top:top + 3, 0:3] = color                       # 左上
            frame[top:top + 3, CANVAS_WIDTH - 3:] = color          # 右上
            frame[top + PI_HEIGHT - 3:top + PI_HEIGHT, 0:3] = color
            frame[top + PI_HEIGHT - 3:top + PI_HEIGHT, CANVAS_WIDTH - 3:] = color

        if label:
            text_mask = np.zeros((PI_HEIGHT, CANVAS_WIDTH), dtype=np.uint8)
            cv2.putText(
                text_mask,
                f"PI{target_id + 1}",
                (6, PI_HEIGHT - 6),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.42,
                255,
                1,
                cv2.LINE_AA,
            )
            band = frame[top:top + PI_HEIGHT, :]
            band[text_mask > 96] = LABEL_COLOR[palette_mode]
    return frame


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TEST3: Pi分割モード。各 Pi の担当領域へ同じ画像を表示する"
    )
    parser.add_argument("--image", type=Path, default=Path("color_bar.webp"))
    parser.add_argument("--palette", choices=("fc6", "msx16"), default="fc6")
    parser.add_argument("--background", type=parse_int, default=None)
    parser.add_argument("--stretch", action="store_true", help="縦横比を無視して帯いっぱいに広げる")
    parser.add_argument("--no-label", action="store_true", help="PI番号を描かない")
    parser.add_argument("--no-marker", action="store_true", help="四隅の目印を描かない")
    parser.add_argument("--fps", type=float, default=24.0)
    parser.add_argument("--seconds", type=float, default=0.0, help="実行秒数。0は無制限")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--pi", action="append", default=[], metavar="HOST[:PORT]")
    parser.add_argument("--chunk-size", type=int, default=1200)
    parser.add_argument("--preview-scale", type=int, default=2)
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument("--save", type=Path, default=None, help="プレビューをPNG保存して終了")
    add_profile_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    palette_mode = PaletteMode.FC6 if args.palette == "fc6" else PaletteMode.MSX16
    default_background = (
        FC6_BLACK if palette_mode == PaletteMode.FC6 else MSX16_BLACK
    )
    background = default_background if args.background is None else args.background
    limit = len(FC6) if palette_mode == PaletteMode.FC6 else len(MSX16)
    if not 0 <= background < limit:
        print(f"error: 背景インデックス {background:#x} は {palette_mode.name} では不正",
              file=sys.stderr)
        return 2

    try:
        rgb, opaque = load_webp(args.image)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    frame = build_frame(
        rgb,
        opaque,
        palette_mode,
        background,
        args.stretch,
        not args.no_label,
        not args.no_marker,
    )

    palette = FC6 if palette_mode == PaletteMode.FC6 else MSX16
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
        return 0

    profiler = Profiler(enabled=args.profile, label="TEST3")
    sender: UdpFrameSender | None = None
    if args.send:
        if len(args.pi) != PI_COUNT:
            print(f"error: --send には --pi をちょうど {PI_COUNT} 個指定する", file=sys.stderr)
            return 2
        sender = UdpFrameSender(
            [parse_pi(item) for item in args.pi], args.chunk_size, profiler
        )

    running = True

    def stop_handler(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    print(
        f"TEST3 {PI_COUNT}分割: {args.image} palette={palette_mode.name} "
        f"帯={CANVAS_WIDTH}x{PI_HEIGHT} x{PI_COUNT} fps={args.fps:g} "
        f"range=0x00-{frame.max():#04x}"
    )

    frame_period = 1.0 / args.fps
    next_deadline = time.monotonic()
    started = next_deadline
    frame_id = 0
    try:
        while running:
            now = time.monotonic()
            if args.seconds > 0 and now - started >= args.seconds:
                break
            if sender is not None:
                with profiler.span("send"):
                    sender.send(frame_id, palette_mode, frame)
            if not args.no_preview:
                with profiler.span("preview"):
                    cv2.imshow(f"TEST3 {PI_COUNT}分割", preview)
                with profiler.span("waitkey"):
                    if (cv2.waitKey(1) & 0xFF) in (27, ord("q")):
                        running = False
            frame_id += 1
            profiler.frame()
            next_deadline += frame_period
            sleep_time = next_deadline - time.monotonic()
            if sleep_time > 0:
                profiler.count("slack_us", int(sleep_time * 1e6))
                with profiler.span("sleep"):
                    time.sleep(sleep_time)
            else:
                profiler.count("late_frames")
                if sleep_time < -frame_period:
                    next_deadline = time.monotonic()
    finally:
        if sender is not None:
            sender.close()
        if not args.no_preview:
            cv2.destroyAllWindows()
        finish_profile(profiler, args.profile_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
