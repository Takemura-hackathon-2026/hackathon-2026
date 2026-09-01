#!/usr/bin/env python3
"""テストモードの機械的検証（計画書 §10 の該当項目のみ）。

実行すると 0 errors で終了することを完了条件とする。
  - 送出インデックスが選択パレットの範囲内か（FC6: 0x00-0x33 / MSX16: 0x01-0x0F）
  - 192x384 が 192x128 の 3 スライスへ正しく分割されるか
  - UDP ヘッダー + CRC32 が往復で一致するか
  - 巡回表が開始番号から最終番号までの連番か
  - 反射移動が論理画面外へ出ないか
"""
from __future__ import annotations

import sys
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mode import (  # noqa: E402
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    HEADER,
    MAGIC,
    PI_COUNT,
    PI_HEIGHT,
    Bouncer,
    TestRenderer,
    fit_image,
    load_webp,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from palettes import FC6_LIMIT, MSX16_LIMIT, PaletteMode  # noqa: E402

IMAGE = Path(__file__).resolve().parent / "test.webp"


def make_renderer(mode: PaletteMode, render_mode: str) -> TestRenderer:
    rgb, opaque = load_webp(IMAGE)
    rgb, opaque = fit_image(rgb, opaque, CANVAS_WIDTH, CANVAS_HEIGHT)
    return TestRenderer(
        rgb=rgb,
        opaque=opaque,
        palette_mode=mode,
        background_index=None,
        render_mode=render_mode,
        color_style="cycle",
        color_start=None,
        stripe_width=3,
        rainbow_hz=0.8,
        mask_threshold=128,
        invert=False,
        speed_x=83.0,
        speed_y=109.0,
    )


def check_index_range(errors: list[str]) -> None:
    for mode, limit in ((PaletteMode.FC6, FC6_LIMIT), (PaletteMode.MSX16, MSX16_LIMIT)):
        for render_mode in ("image", "mask"):
            renderer = make_renderer(mode, render_mode)
            lo, hi = 0x100, -1
            for step in range(240):
                renderer.update(1.0 / 60.0)
                frame = renderer.render(step / 60.0)
                lo = min(lo, int(frame.min()))
                hi = max(hi, int(frame.max()))
            if hi >= limit:
                errors.append(f"{mode.name}/{render_mode}: 範囲外インデックス {hi:#x}")
            if mode == PaletteMode.MSX16 and lo == 0:
                errors.append(f"{mode.name}/{render_mode}: 透明インデックス 0 を送出している")


def check_color_sequence(errors: list[str]) -> None:
    """巡回表が開始番号から最終番号までの連番であること。"""
    from test_mode import color_sequence

    cases = (
        (PaletteMode.FC6, None, 0x00, FC6_LIMIT - 1),
        (PaletteMode.FC6, 0x10, 0x10, FC6_LIMIT - 1),
        (PaletteMode.MSX16, None, 0x01, MSX16_LIMIT - 1),
        (PaletteMode.MSX16, 0x04, 0x04, MSX16_LIMIT - 1),
    )
    for mode, start, first, last in cases:
        sequence = color_sequence(mode, start)
        if sequence != tuple(range(first, last + 1)):
            errors.append(f"{mode.name} start={start}: 巡回表が連番でない {sequence}")

    # 範囲外の開始番号は拒否する。
    for mode, bad in ((PaletteMode.FC6, FC6_LIMIT), (PaletteMode.MSX16, 0x00)):
        try:
            color_sequence(mode, bad)
        except ValueError:
            continue
        errors.append(f"{mode.name}: 開始番号 {bad:#x} を拒否しない")

    # パレット切替で範囲外になっても既定値へ戻り、送出値が範囲内に収まること。
    renderer = make_renderer(PaletteMode.FC6, "mask")
    renderer.color_start = FC6_LIMIT - 4
    renderer.palette_mode = PaletteMode.MSX16
    frame = renderer.render(0.0)
    if int(frame.max()) >= MSX16_LIMIT:
        errors.append(f"パレット切替後に範囲外インデックス {frame.max():#x}")


def check_quantize(errors: list[str]) -> None:
    """代表色が同色のインデックスへ落ちること（距離計算の桁溢れ検出）。"""
    from test_mode import palette_rgb, quantize_to_palette

    samples = np.array([[[255, 255, 255], [0, 0, 0], [255, 0, 0], [0, 0, 255]]], np.uint8)
    for mode in (PaletteMode.FC6, PaletteMode.MSX16):
        indices = quantize_to_palette(samples, mode).ravel()
        got = palette_rgb(mode)[indices]
        for source, index, mapped in zip(samples[0], indices, got):
            distance = int(((source.astype(np.int32) - mapped) ** 2).sum())
            # パレット内の最良候補との差が大きい場合は距離計算の誤り。
            best = int(((palette_rgb(mode).astype(np.int32) - source) ** 2).sum(axis=1).min())
            if distance > best:
                errors.append(
                    f"{mode.name}: RGB{tuple(int(v) for v in source)} -> {index:#04x} "
                    f"(距離 {distance} > 最良 {best})"
                )


def check_slices(errors: list[str]) -> None:
    renderer = make_renderer(PaletteMode.FC6, "image")
    frame = renderer.render(0.0)
    if frame.shape != (CANVAS_HEIGHT, CANVAS_WIDTH):
        errors.append(f"フレーム形状が不正: {frame.shape}")
        return
    rebuilt = []
    for target_id in range(PI_COUNT):
        y0 = target_id * PI_HEIGHT
        payload = frame[y0:y0 + PI_HEIGHT, :].tobytes(order="C")
        if len(payload) != CANVAS_WIDTH * PI_HEIGHT:
            errors.append(f"target {target_id}: スライス長 {len(payload)}")
        rebuilt.append(np.frombuffer(payload, dtype=np.uint8).reshape(PI_HEIGHT, CANVAS_WIDTH))
    if not np.array_equal(np.vstack(rebuilt), frame):
        errors.append("3 スライス再結合が元フレームと一致しない")


def check_header_roundtrip(errors: list[str]) -> None:
    payload = bytes(range(64)) * 18  # 1152 byte
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    packet = HEADER.pack(MAGIC, 12345, 2, 1, 3, 16, len(payload), crc) + payload
    magic, frame_id, target, mode, chunk_id, count, size, got_crc = HEADER.unpack_from(packet)
    body = packet[HEADER.size:]
    if (magic, frame_id, target, mode, chunk_id, count, size) != (
        MAGIC, 12345, 2, 1, 3, 16, len(payload)
    ):
        errors.append("ヘッダーの往復が一致しない")
    if zlib.crc32(body) & 0xFFFFFFFF != got_crc:
        errors.append("CRC32 が一致しない")


def check_bounce(errors: list[str]) -> None:
    bouncer = Bouncer(0.0, 0.0, 997.0, 1301.0, 64, 64)
    for _ in range(20000):
        bouncer.update(1.0 / 60.0)
        if not (0.0 <= bouncer.x <= CANVAS_WIDTH - 64 and 0.0 <= bouncer.y <= CANVAS_HEIGHT - 64):
            errors.append(f"反射移動が画面外へ出た: ({bouncer.x:.2f}, {bouncer.y:.2f})")
            return


def main() -> int:
    errors: list[str] = []
    check_index_range(errors)
    check_color_sequence(errors)
    check_quantize(errors)
    check_slices(errors)
    check_header_roundtrip(errors)
    check_bounce(errors)
    for message in errors:
        print(f"ERROR: {message}")
    print(f"{len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
