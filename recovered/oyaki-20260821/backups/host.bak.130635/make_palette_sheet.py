#!/usr/bin/env python3
"""パレット見本画像（docs/assets/*_palette.png）を palettes.py から再生成する。

パレット定義を変更したら本スクリプトを実行し、見本画像を作り直す。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from palettes import FC6, MSX16, MSX16_TRANSPARENT, PaletteMode  # noqa: E402

DOCS_ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"
FONT_CANDIDATES = (
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
)

COLUMNS = 4
CELL_W = 260
CELL_H = 72
MARGIN = 24
HEADER_H = 76


def load_font(size: int) -> ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def text_color(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """背景色に対して読める文字色を選ぶ。"""
    luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return (0, 0, 0) if luma >= 140 else (255, 255, 255)


def build_sheet(mode: PaletteMode) -> Image.Image:
    if mode == PaletteMode.FC6:
        entries = [(i, FC6[i][:3], FC6[i][3]) for i in range(len(FC6))]
        title = f"FC6 palette — {len(FC6)} colors, index 0x00-0x{len(FC6) - 1:02X}"
        digits = 2
    else:
        entries = [(i, MSX16[i][:3], MSX16[i][3]) for i in range(len(MSX16))]
        title = f"MSX16 palette — {len(MSX16)} colors, index 0x0-0x{len(MSX16) - 1:X}"
        digits = 1

    rows = (len(entries) + COLUMNS - 1) // COLUMNS
    width = MARGIN * 2 + COLUMNS * CELL_W
    height = MARGIN * 2 + HEADER_H + rows * CELL_H
    sheet = Image.new("RGB", (width, height), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)

    title_font = load_font(28)
    index_font = load_font(22)
    hex_font = load_font(18)
    rgb_font = load_font(15)

    draw.text((MARGIN, MARGIN), title, font=title_font, fill=(255, 255, 255))
    draw.text(
        (MARGIN, MARGIN + 38),
        "1 byte/pixel, no bit packing. Source of truth: host/palettes.py",
        font=rgb_font,
        fill=(170, 170, 170),
    )

    for position, (index, rgb, alpha) in enumerate(entries):
        row, column = divmod(position, COLUMNS)
        x0 = MARGIN + column * CELL_W
        y0 = MARGIN + HEADER_H + row * CELL_H
        x1 = x0 + CELL_W - 6
        y1 = y0 + CELL_H - 6

        if alpha == 0:
            # 透明色は市松模様で示す。
            draw.rectangle((x0, y0, x1, y1), fill=(90, 90, 90))
            for cy in range(y0, y1, 10):
                for cx in range(x0 + ((cy - y0) // 10 % 2) * 10, x1, 20):
                    draw.rectangle(
                        (cx, cy, min(cx + 9, x1), min(cy + 9, y1)), fill=(130, 130, 130)
                    )
            label = (255, 255, 255)
        else:
            draw.rectangle((x0, y0, x1, y1), fill=rgb)
            label = text_color(rgb)

        draw.rectangle((x0, y0, x1, y1), outline=(60, 60, 60))
        draw.text((x0 + 12, y0 + 8), f"0x{index:0{digits}X}", font=index_font, fill=label)
        note = "transparent" if alpha == 0 else "#{:02X}{:02X}{:02X}".format(*rgb)
        draw.text((x0 + 12, y0 + 36), note, font=hex_font, fill=label)
        draw.text(
            (x0 + CELL_W - 118, y0 + 40),
            "{:3d},{:3d},{:3d}".format(*rgb),
            font=rgb_font,
            fill=label,
        )
    return sheet


def main() -> int:
    parser = argparse.ArgumentParser(description="パレット見本画像を再生成する")
    parser.add_argument("--palette", choices=("fc6", "msx16", "both"), default="fc6")
    parser.add_argument("--out-dir", type=Path, default=DOCS_ASSETS)
    args = parser.parse_args()

    targets = (
        [PaletteMode.FC6, PaletteMode.MSX16]
        if args.palette == "both"
        else [PaletteMode.FC6 if args.palette == "fc6" else PaletteMode.MSX16]
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for mode in targets:
        name = "fc6_palette.png" if mode == PaletteMode.FC6 else "msx16_palette.png"
        path = args.out_dir / name
        sheet = build_sheet(mode)
        sheet.save(path)
        print(f"saved: {path} ({sheet.width}x{sheet.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
