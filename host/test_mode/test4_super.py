#!/usr/bin/env python3
"""SUPERTESTMODE: 斜めN字配置の特殊ディスプレーで test.webp を反射移動させる。

4 つの 192x128 ブロックが縦一列ではなく、隣り合うブロックの角 64dot だけで
繋がるジグザグ配置になっている構成に対応する。

配置の根拠（実機の接続）:
    Pi1 の右上 64dot ↔ Pi3 の左下 64dot
    Pi3 の右下 64dot ↔ Pi2 の左上 64dot
    Pi2 の右上 64dot ↔ Pi4 の左下 64dot

ここから各ブロックの原点は次のようになる（y は下向き、単位 pixel）。

    Pi3 (target 2) ... ( 128,   0)      Pi4 (target 3) ... ( 384,   0)
    Pi1 (target 0) ... (   0, 128)      Pi2 (target 1) ... ( 256, 128)

    +--------+--------+--------+--------+--------+--------+
    |        |   Pi3      |        |   Pi4      |         |   y=0..127
    +--------+--------+--------+--------+--------+--------+
    |   Pi1      |        |   Pi2      |        |         |   y=128..255
    +--------+--------+--------+--------+--------+--------+
    x=0     128      256      384      448     576

仮想画面は 576x256。パネルが存在しない領域へ図形が入ると、その間だけ見えなく
なる。反射は仮想画面の外周で行う。

現行の3台構成には含まれない旧配置であり、`--send`は無効化している。

配置は LAYOUT で定義しており、実機の並びが違えばここだけ直せばよい。
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from dataclasses import dataclass
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
    CANVAS_WIDTH as BLOCK_WIDTH,
    PI_COUNT,
    PI_HEIGHT as BLOCK_HEIGHT,
    Bouncer,
    UdpFrameSender,
    color_sequence,
    fit_image,
    load_webp,
    parse_int,
    parse_pi,
    quantize_to_palette,
)

LINK_SIZE = 64  # 隣接ブロックが繋がる幅


@dataclass(frozen=True)
class Block:
    """仮想画面上でのブロック配置。"""

    target_id: int
    name: str
    x: int
    y: int


# 実機の接続に対応する配置。並びが違う場合はここだけ書き換える。
LAYOUT: tuple[Block, ...] = (
    Block(target_id=0, name="PI1", x=0, y=BLOCK_HEIGHT),
    Block(target_id=2, name="PI3", x=128, y=0),
    Block(target_id=1, name="PI2", x=256, y=BLOCK_HEIGHT),
    Block(target_id=3, name="PI4", x=384, y=0),
)

SUPER_PI_COUNT = len(LAYOUT)
VIRTUAL_WIDTH = max(block.x for block in LAYOUT) + BLOCK_WIDTH   # 576
VIRTUAL_HEIGHT = max(block.y for block in LAYOUT) + BLOCK_HEIGHT  # 256

LABEL_COLOR = {PaletteMode.FC6: FC6_WHITE, PaletteMode.MSX16: 0x0F}
LINK_COLOR = {PaletteMode.FC6: 0x15, PaletteMode.MSX16: 0x02}  # 緑


def link_regions() -> list[tuple[int, int, int, int]]:
    """隣り合うブロックが繋がる 64dot の重なりを (x0, y0, x1, y1) で返す。"""
    regions = []
    order = list(LAYOUT)
    for current, following in zip(order, order[1:]):
        x0 = max(current.x, following.x)
        x1 = min(current.x + BLOCK_WIDTH, following.x + BLOCK_WIDTH)
        y0 = min(current.y, following.y) + BLOCK_HEIGHT - LINK_SIZE
        y1 = y0 + LINK_SIZE
        if x1 > x0:
            regions.append((x0, y0, x1, y1))
    return regions


class SuperRenderer:
    def __init__(
        self,
        rgb: np.ndarray,
        opaque: np.ndarray,
        palette_mode: PaletteMode,
        background: int,
        render_mode: str,
        color_start: int | None,
        mask_threshold: int,
        invert: bool,
        speed_x: float,
        speed_y: float,
    ) -> None:
        self.palette_mode = palette_mode
        self.background = background
        self.render_mode = render_mode
        self.color_start = color_start
        self.phase_offset = 0
        self.source_rgb = rgb
        self.opaque = opaque

        luma = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        mask = luma >= mask_threshold
        if invert:
            mask = np.logical_not(mask)
        self.mask = np.logical_and(mask, opaque)
        self.quantized = quantize_to_palette(rgb, palette_mode)

        height, width = opaque.shape
        self.bouncer = Bouncer(
            0.0, 0.0, speed_x, speed_y, width, height, VIRTUAL_WIDTH, VIRTUAL_HEIGHT
        )

    @property
    def colors(self) -> Sequence[int]:
        return color_sequence(self.palette_mode, self.color_start)

    def update(self, dt: float) -> None:
        collisions = self.bouncer.update(dt)
        if collisions:
            self.phase_offset = (self.phase_offset + collisions) % len(self.colors)

    def render(self, label: bool, show_links: bool) -> np.ndarray:
        """仮想画面（576x256）を描く。パネルが無い領域も背景で埋める。"""
        frame = np.full(
            (VIRTUAL_HEIGHT, VIRTUAL_WIDTH), self.background, dtype=np.uint8
        )
        if show_links:
            for x0, y0, x1, y1 in link_regions():
                frame[y0:y1, x0:x1] = LINK_COLOR[self.palette_mode]

        x = int(round(self.bouncer.x))
        y = int(round(self.bouncer.y))
        h, w = self.opaque.shape
        region = frame[y:y + h, x:x + w]
        if self.render_mode == "image":
            region[self.opaque] = self.quantized[self.opaque]
        else:
            sequence = self.colors
            region[self.mask] = sequence[self.phase_offset % len(sequence)]

        if label:
            for block in LAYOUT:
                text_mask = np.zeros((BLOCK_HEIGHT, BLOCK_WIDTH), dtype=np.uint8)
                cv2.putText(
                    text_mask,
                    block.name,
                    (4, BLOCK_HEIGHT - 5),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.36,
                    255,
                    1,
                    cv2.LINE_AA,
                )
                band = frame[
                    block.y:block.y + BLOCK_HEIGHT, block.x:block.x + BLOCK_WIDTH
                ]
                band[text_mask > 96] = LABEL_COLOR[self.palette_mode]
        return frame

    def slices(self, frame: np.ndarray) -> list[np.ndarray]:
        """legacy 4台配置のtarget_id順に192x128のスライスを切り出す。"""
        pieces: list[np.ndarray | None] = [None] * SUPER_PI_COUNT
        for block in LAYOUT:
            pieces[block.target_id] = frame[
                block.y:block.y + BLOCK_HEIGHT, block.x:block.x + BLOCK_WIDTH
            ]
        if any(piece is None for piece in pieces):
            raise ValueError("LAYOUT に target_id の抜けがある")
        return [np.ascontiguousarray(piece) for piece in pieces]  # type: ignore[arg-type]

    def rgb_preview(self, indexed: np.ndarray) -> np.ndarray:
        palette = FC6 if self.palette_mode == PaletteMode.FC6 else MSX16
        lut = np.asarray([entry[:3] for entry in palette], dtype=np.uint8)
        return lut[indexed][:, :, ::-1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SUPERTESTMODE: 斜めN字配置で画像を反射移動させる"
    )
    parser.add_argument("--image", type=Path, default=Path("test.webp"))
    parser.add_argument("--palette", choices=("fc6", "msx16"), default="fc6")
    parser.add_argument("--render-mode", choices=("mask", "image"), default="mask")
    parser.add_argument("--background", type=parse_int, default=None)
    parser.add_argument("--color-start", type=parse_int, default=None)
    parser.add_argument("--mask-threshold", type=int, default=128)
    parser.add_argument("--invert", action="store_true")
    parser.add_argument("--speed-x", type=float, default=97.0)
    parser.add_argument("--speed-y", type=float, default=61.0)
    parser.add_argument("--fps", type=float, default=24.0)
    parser.add_argument("--seconds", type=float, default=0.0)
    parser.add_argument("--no-label", action="store_true", help="PI番号を描かない")
    parser.add_argument("--show-links", action="store_true", help="接続64dotを塗る")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--pi", action="append", default=[], metavar="HOST[:PORT]")
    parser.add_argument("--chunk-size", type=int, default=1200)
    parser.add_argument("--preview-scale", type=int, default=2)
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument("--save", type=Path, default=None, help="仮想画面をPNG保存して終了")
    add_profile_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    palette_mode = PaletteMode.FC6 if args.palette == "fc6" else PaletteMode.MSX16
    limit = len(FC6) if palette_mode == PaletteMode.FC6 else len(MSX16)
    background = (
        (FC6_BLACK if palette_mode == PaletteMode.FC6 else MSX16_BLACK)
        if args.background is None
        else args.background
    )
    if not 0 <= background < limit:
        print(f"error: 背景インデックス {background:#x} は不正", file=sys.stderr)
        return 2

    try:
        rgb, opaque = load_webp(args.image)
        rgb, opaque = fit_image(rgb, opaque, BLOCK_WIDTH, BLOCK_HEIGHT)
        color_sequence(palette_mode, args.color_start)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    renderer = SuperRenderer(
        rgb=rgb,
        opaque=opaque,
        palette_mode=palette_mode,
        background=background,
        render_mode=args.render_mode,
        color_start=args.color_start,
        mask_threshold=args.mask_threshold,
        invert=args.invert,
        speed_x=args.speed_x,
        speed_y=args.speed_y,
    )

    if args.save is not None:
        frame = renderer.render(not args.no_label, args.show_links)
        preview = renderer.rgb_preview(frame)
        scale = max(1, args.preview_scale)
        preview = cv2.resize(
            preview,
            (VIRTUAL_WIDTH * scale, VIRTUAL_HEIGHT * scale),
            interpolation=cv2.INTER_NEAREST,
        )
        cv2.imwrite(str(args.save), preview)
        print(f"saved: {args.save}")
        return 0

    profiler = Profiler(enabled=args.profile, label="TEST4")
    sender: UdpFrameSender | None = None
    if args.send:
        print(
            "error: TEST4の旧4台特殊配置は現行3台構成では送信できません。"
            "--saveまたはローカルプレビューを使用してください",
            file=sys.stderr,
        )
        return 2

    running = True

    def stop_handler(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    placement = " ".join(f"{b.name}@({b.x},{b.y})->t{b.target_id}" for b in LAYOUT)
    print(
        f"SUPERTESTMODE: 仮想画面 {VIRTUAL_WIDTH}x{VIRTUAL_HEIGHT} "
        f"palette={palette_mode.name} fps={args.fps:g}\n  配置: {placement}"
    )

    frame_period = 1.0 / args.fps
    next_deadline = time.monotonic()
    started = next_deadline
    last_update = next_deadline
    frame_id = 0
    try:
        while running:
            now = time.monotonic()
            if args.seconds > 0 and now - started >= args.seconds:
                break
            dt = min(0.1, max(0.0, now - last_update))
            last_update = now
            with profiler.span("update"):
                renderer.update(dt)

            with profiler.span("render"):
                frame = renderer.render(not args.no_label, args.show_links)
            if sender is not None:
                with profiler.span("slice"):
                    pieces = renderer.slices(frame)
                with profiler.span("send"):
                    sender.send_slices(frame_id, palette_mode, pieces)

            if not args.no_preview:
                with profiler.span("preview"):
                    preview = renderer.rgb_preview(frame)
                    if args.preview_scale != 1:
                        preview = cv2.resize(
                            preview,
                            (VIRTUAL_WIDTH * args.preview_scale,
                             VIRTUAL_HEIGHT * args.preview_scale),
                            interpolation=cv2.INTER_NEAREST,
                        )
                    cv2.imshow("SUPERTESTMODE", preview)
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
