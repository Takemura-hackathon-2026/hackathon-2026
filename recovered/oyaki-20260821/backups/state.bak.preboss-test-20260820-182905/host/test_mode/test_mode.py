#!/usr/bin/env python3
"""RGB LED ゲーム 主機側テストモード（WebP版）。

ローカルの `test.webp` を読み込み、192x384 の論理画面内を DVD スクリーンセーバー
のように反射移動させる。色は FC6 / MSX16 の登録済みインデックスだけを使い、
ローカルプレビューと、192x96 の 4 スライスの UDP 送信を行う。

計画書 §4.7 の WBMP テストモードを WebP 素材へ置き換えたもの。
AI・カメラ・画像圧縮・Pi 側描画は使用しない。
"""
from __future__ import annotations

import argparse
import math
import signal
import socket
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np

# 共有パレット定義を親ディレクトリから読み込む。
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from palettes import (  # noqa: E402
    FC6,
    FC6_BLACK,
    FC6_LIMIT,
    MSX16,
    MSX16_BLACK,
    MSX16_LIMIT,
    MSX16_TRANSPARENT,
    PaletteMode,
)
from profiler import (  # noqa: E402
    Profiler,
    add_profile_arguments,
    finish_profile,
)

CANVAS_WIDTH = 192
CANVAS_HEIGHT = 384
PI_COUNT = 4
PI_HEIGHT = 96
MAGIC = 0x524C4544  # ASCII: RLED
HEADER = struct.Struct("!IIBBHHHI")

# 着色は色相を選び直さず、パレット定義順のインデックスをそのまま順繰りに使う。
# 既定の開始番号。MSX16 の 0 は透明であり送出しないため 1 から始める。
FC6_COLOR_START = 0x00
MSX16_COLOR_START = MSX16_TRANSPARENT + 1
DEFAULT_IMAGE = Path(__file__).resolve().with_name("single-eye-catch_2800x1040.png")


def color_sequence(mode: PaletteMode, start: int | None = None) -> tuple[int, ...]:
    """開始番号から最終番号（FC6: 51 / MSX16: 15）までの巡回表を返す。"""
    if mode == PaletteMode.FC6:
        limit, lowest, default_start = FC6_LIMIT, 0, FC6_COLOR_START
    else:
        limit, lowest, default_start = (
            MSX16_LIMIT,
            MSX16_TRANSPARENT + 1,
            MSX16_COLOR_START,
        )
    value = default_start if start is None else start
    if not lowest <= value < limit:
        raise ValueError(
            f"開始インデックス {value:#x} は {mode.name} では不正"
            f"（{lowest:#x}〜{limit - 1:#x}）"
        )
    return tuple(range(value, limit))


# ---------------------------------------------------------------------------
# 素材読込み
# ---------------------------------------------------------------------------
def load_webp(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """画像を (RGB配列, 不透明マスク) として読み込む。

    既存名との互換性を保つため関数名は ``load_webp`` のままだが、PNGも扱う。
    アルファ付き画像はアルファ 128 以上を前景として扱う。
    """
    if not path.exists():
        raise RuntimeError(f"画像が見つからない: {path}")
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise RuntimeError(f"画像の読込みに失敗した: {path}")

    if image.ndim == 2:
        rgb = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        alpha = np.full(image.shape[:2], 255, dtype=np.uint8)
    elif image.shape[2] == 4:
        rgb = cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)
        alpha = image[:, :, 3]
    elif image.shape[2] == 3:
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        alpha = np.full(image.shape[:2], 255, dtype=np.uint8)
    else:
        raise ValueError(f"未対応のチャンネル数: {image.shape}")

    return rgb, alpha >= 128


def fit_image(
    rgb: np.ndarray, opaque: np.ndarray, max_width: int, max_height: int
) -> tuple[np.ndarray, np.ndarray]:
    """論理画面より大きい素材を最近傍で縮小する。"""
    height, width = opaque.shape
    if width <= max_width and height <= max_height:
        return rgb, opaque
    scale = min(max_width / width, max_height / height)
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    rgb = cv2.resize(rgb, new_size, interpolation=cv2.INTER_NEAREST)
    opaque = cv2.resize(
        opaque.astype(np.uint8), new_size, interpolation=cv2.INTER_NEAREST
    ).astype(bool)
    return rgb, opaque


def crop_logo(
    rgb: np.ndarray,
    opaque: np.ndarray,
    threshold: int = 245,
    margin: int = 8,
) -> tuple[np.ndarray, np.ndarray]:
    """白い余白を除き、画像内のロゴ領域だけを切り抜く。

    ロゴ画像の白背景と青いマークを前提にするが、アルファ値だけには依存しない。
    そのため、元PNGをRGB LED用の背景色へ合成する場合でも切り抜き範囲が安定する。
    """
    image = np.asarray(rgb)
    mask = np.asarray(opaque, dtype=bool)
    if image.ndim != 3 or image.shape[2] != 3 or mask.shape != image.shape[:2]:
        raise ValueError("ロゴ画像とマスクの形状が不正")
    if not 0 <= threshold <= 255 or margin < 0:
        raise ValueError("ロゴ切り抜きのthreshold/marginが不正")

    content = mask & np.any(image < threshold, axis=2)
    coordinates = np.argwhere(content)
    if coordinates.size == 0:
        raise ValueError("ロゴ領域を検出できない")
    y0, x0 = coordinates.min(axis=0)
    y1, x1 = coordinates.max(axis=0) + 1
    y0 = max(0, int(y0) - margin)
    x0 = max(0, int(x0) - margin)
    y1 = min(image.shape[0], int(y1) + margin)
    x1 = min(image.shape[1], int(x1) + margin)
    return image[y0:y1, x0:x1], mask[y0:y1, x0:x1]


# ---------------------------------------------------------------------------
# パレット量子化
# ---------------------------------------------------------------------------
def palette_rgb(mode: PaletteMode) -> np.ndarray:
    entries = FC6 if mode == PaletteMode.FC6 else MSX16
    return np.asarray([entry[:3] for entry in entries], dtype=np.int16)


def quantizable_indices(mode: PaletteMode) -> np.ndarray:
    """量子化先として使えるインデックス。

    MSX16 のインデックス 0 は透明であり、送出値としては使わない。
    FC6 は 52 色すべてを使う。
    """
    if mode == PaletteMode.MSX16:
        return np.arange(MSX16_TRANSPARENT + 1, MSX16_LIMIT, dtype=np.uint8)
    return np.arange(0, FC6_LIMIT, dtype=np.uint8)


def quantize_to_palette(rgb: np.ndarray, mode: PaletteMode) -> np.ndarray:
    """RGB 画像を最近傍色でパレットインデックスへ量子化する（主機側で完結）。"""
    candidates = quantizable_indices(mode)
    lut = palette_rgb(mode)[candidates].astype(np.int32)  # (N, 3)
    flat = rgb.reshape(-1, 3).astype(np.int32)
    # (画素数, N) の二乗距離。二乗和が int16 を溢れるため int32 で計算する。
    diff = flat[:, None, :] - lut[None, :, :]
    dist = np.einsum("pnc,pnc->pn", diff, diff)
    nearest = candidates[np.argmin(dist, axis=1)]
    return nearest.reshape(rgb.shape[:2]).astype(np.uint8)


def parse_int(value: str) -> int:
    return int(value, 0)


def parse_pi(value: str) -> tuple[str, int]:
    if ":" in value:
        host, port_text = value.rsplit(":", 1)
        return host, int(port_text)
    return value, 5000


# ---------------------------------------------------------------------------
# 反射移動
# ---------------------------------------------------------------------------
@dataclass
class Bouncer:
    x: float
    y: float
    vx: float
    vy: float
    width: int
    height: int
    canvas_width: int = CANVAS_WIDTH
    canvas_height: int = CANVAS_HEIGHT

    def update(self, dt: float) -> int:
        """位置を進め、境界への衝突回数を返す。"""
        collisions = 0
        self.x += self.vx * dt
        self.y += self.vy * dt
        max_x = max(0.0, float(self.canvas_width - self.width))
        max_y = max(0.0, float(self.canvas_height - self.height))

        if max_x == 0:
            self.x = 0
            self.vx = 0
        else:
            while self.x < 0 or self.x > max_x:
                if self.x < 0:
                    self.x = -self.x
                    self.vx = abs(self.vx)
                    collisions += 1
                else:
                    self.x = 2 * max_x - self.x
                    self.vx = -abs(self.vx)
                    collisions += 1

        if max_y == 0:
            self.y = 0
            self.vy = 0
        else:
            while self.y < 0 or self.y > max_y:
                if self.y < 0:
                    self.y = -self.y
                    self.vy = abs(self.vy)
                    collisions += 1
                else:
                    self.y = 2 * max_y - self.y
                    self.vy = -abs(self.vy)
                    collisions += 1
        return collisions


class TestRenderer:
    def __init__(
        self,
        rgb: np.ndarray,
        opaque: np.ndarray,
        palette_mode: PaletteMode,
        background_index: int | None,
        render_mode: str,
        color_style: str,
        color_start: int | None,
        stripe_width: int,
        rainbow_hz: float,
        mask_threshold: int,
        invert: bool,
        speed_x: float,
        speed_y: float,
    ) -> None:
        self.source_rgb = rgb
        self.opaque = opaque
        self.palette_mode = palette_mode
        self.background_override = background_index
        self.render_mode = render_mode
        self.color_style = color_style
        self.color_start = color_start
        self.stripe_width = max(1, stripe_width)
        self.rainbow_hz = max(0.0, rainbow_hz)
        self.phase_offset = 0

        # mask モード用: 輝度しきい値で 1bit 化（WBMP テストモード相当）。
        luma = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        mask = luma >= mask_threshold
        if invert:
            mask = np.logical_not(mask)
        self.mask = np.logical_and(mask, opaque)

        # image モード用: パレットごとに量子化結果をキャッシュする。
        self._quantized: dict[PaletteMode, np.ndarray] = {}

        height, width = opaque.shape
        self.bouncer = Bouncer(0.0, 0.0, speed_x, speed_y, width, height)
        self.start_time = time.monotonic()

    @property
    def palette(self) -> Sequence[tuple[int, int, int, int]]:
        return FC6 if self.palette_mode == PaletteMode.FC6 else MSX16

    @property
    def colors(self) -> Sequence[int]:
        """開始番号から最終番号までのインデックス巡回表。"""
        try:
            return color_sequence(self.palette_mode, self.color_start)
        except ValueError:
            # 実行中のパレット切替で範囲外になった開始番号は既定値へ戻す。
            return color_sequence(self.palette_mode, None)

    @property
    def default_background(self) -> int:
        return FC6_BLACK if self.palette_mode == PaletteMode.FC6 else MSX16_BLACK

    @property
    def background_index(self) -> int:
        value = (
            self.default_background
            if self.background_override is None
            else self.background_override
        )
        limit = FC6_LIMIT if self.palette_mode == PaletteMode.FC6 else MSX16_LIMIT
        if not 0 <= value < limit:
            raise ValueError(
                f"背景インデックス {value:#x} は {self.palette_mode.name} では不正"
            )
        # MSX のインデックス 0 は透明であり、そのまま送出しない。
        if self.palette_mode == PaletteMode.MSX16 and value == MSX16_TRANSPARENT:
            return MSX16_BLACK
        return value

    def quantized(self) -> np.ndarray:
        cached = self._quantized.get(self.palette_mode)
        if cached is None:
            cached = quantize_to_palette(self.source_rgb, self.palette_mode)
            self._quantized[self.palette_mode] = cached
        return cached

    def toggle_palette(self) -> None:
        self.palette_mode = (
            PaletteMode.MSX16
            if self.palette_mode == PaletteMode.FC6
            else PaletteMode.FC6
        )
        self.phase_offset %= len(self.colors)

    def toggle_render_mode(self) -> None:
        self.render_mode = "mask" if self.render_mode == "image" else "image"

    def reset(self) -> None:
        self.bouncer.x = 0.0
        self.bouncer.y = 0.0
        self.bouncer.vx = abs(self.bouncer.vx) or 80.0
        self.bouncer.vy = abs(self.bouncer.vy) or 105.0
        self.phase_offset = 0
        self.start_time = time.monotonic()

    def update(self, dt: float) -> None:
        collisions = self.bouncer.update(dt)
        if collisions:
            # DVD ロゴ同様、反射で色位相を進める。
            self.phase_offset = (self.phase_offset + collisions) % len(self.colors)

    def render(self, now: float) -> np.ndarray:
        frame = np.full(
            (CANVAS_HEIGHT, CANVAS_WIDTH), self.background_index, dtype=np.uint8
        )
        x = int(round(self.bouncer.x))
        y = int(round(self.bouncer.y))
        h, w = self.opaque.shape
        region = frame[y:y + h, x:x + w]

        if self.render_mode == "image":
            region[self.opaque] = self.quantized()[self.opaque]
            return frame

        sequence = self.colors
        temporal_phase = int(
            (now - self.start_time) * self.rainbow_hz * len(sequence)
        )
        base_phase = (temporal_phase + self.phase_offset) % len(sequence)
        if self.color_style == "solid":
            # 画像全体を 1 色にし、時間経過と反射で次の番号へ送る。
            region[self.mask] = sequence[base_phase]
        else:
            # 帯ごとに開始番号から最終番号までを順繰りに割り当てる。
            yy, xx = np.indices((h, w), dtype=np.int32)
            band = ((xx + yy) // self.stripe_width + base_phase) % len(sequence)
            colors = np.take(np.asarray(sequence, dtype=np.uint8), band)
            region[self.mask] = colors[self.mask]
        return frame

    def rgb_preview(self, indexed: np.ndarray) -> np.ndarray:
        lut = np.asarray([entry[:3] for entry in self.palette], dtype=np.uint8)
        return lut[indexed][:, :, ::-1]  # RGB -> BGR（OpenCV 表示用）


# ---------------------------------------------------------------------------
# GIF 書き出し
# ---------------------------------------------------------------------------
def record_gif(
    renderer: TestRenderer,
    path: Path,
    seconds: float,
    fps: float,
    scale: int,
) -> int:
    """シミュレーションを GIF として保存し、書き出したフレーム数を返す。

    実時間ではなく固定の時間刻みで進めるため、再生結果が毎回同じになる。
    送出インデックスをそのまま GIF のパレットインデックスとして書くので、
    LED へ送る値と GIF の見た目が一致する。
    """
    from PIL import Image  # GIF 書き出し時のみ必要

    if seconds <= 0 or fps <= 0:
        raise ValueError("--gif-seconds と --gif-fps は 0 より大きい値")
    if scale <= 0:
        raise ValueError("--gif-scale は 0 より大きい値")

    dt = 1.0 / fps
    count = max(1, int(round(seconds * fps)))
    lut = bytearray()
    for entry in renderer.palette:
        lut.extend(entry[:3])
    lut.extend(bytes(768 - len(lut)))  # GIF のパレットは 256 色分必要

    frames: list[Image.Image] = []
    for step in range(count):
        renderer.update(dt)
        indexed = renderer.render(step * dt)
        if scale != 1:
            indexed = np.repeat(np.repeat(indexed, scale, axis=0), scale, axis=1)
        image = Image.fromarray(indexed, mode="P")
        image.putpalette(bytes(lut))
        frames.append(image)

    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=max(20, int(round(1000.0 / fps))),
        loop=0,
        optimize=False,
        disposal=1,
    )
    return len(frames)


# ---------------------------------------------------------------------------
# UDP 送信
# ---------------------------------------------------------------------------
class UdpFrameSender:
    def __init__(
        self,
        destinations: Sequence[tuple[str, int]],
        chunk_size: int,
        profiler: Profiler | None = None,
    ) -> None:
        if len(destinations) != PI_COUNT:
            raise ValueError(f"Pi の宛先はちょうど {PI_COUNT} 件必要")
        if not 256 <= chunk_size <= 1400:
            raise ValueError("チャンクサイズは 256〜1400 バイト")
        self.destinations = destinations
        self.chunk_size = chunk_size
        # 計測は既定で無効。無効な Profiler は span() が素通りする。
        self.profiler = profiler if profiler is not None else Profiler(enabled=False)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)

    def close(self) -> None:
        self.socket.close()

    def send(self, frame_id: int, palette_mode: PaletteMode, indexed: np.ndarray) -> None:
        """192x384 の全画面を上から 96 行ずつ 4 分割して送る。"""
        if indexed.shape != (CANVAS_HEIGHT, CANVAS_WIDTH):
            raise ValueError(f"想定外のフレーム形状: {indexed.shape}")
        slices = [
            indexed[target_id * PI_HEIGHT:(target_id + 1) * PI_HEIGHT, :]
            for target_id in range(PI_COUNT)
        ]
        self.send_slices(frame_id, palette_mode, slices)

    def send_slices(
        self,
        frame_id: int,
        palette_mode: PaletteMode,
        slices: Sequence[np.ndarray],
    ) -> None:
        """target_id 順に並べた 192x96 のスライスをそのまま送る。

        論理画面の並びが縦一列でない特殊配置でも、割り当てだけを差し替えて
        同じ伝送経路を使えるようにする。
        """
        if len(slices) != PI_COUNT:
            raise ValueError(f"スライスは {PI_COUNT} 枚必要: {len(slices)}")
        profiler = self.profiler
        for target_id, destination in enumerate(self.destinations):
            piece = slices[target_id]
            if piece.shape != (PI_HEIGHT, CANVAS_WIDTH):
                raise ValueError(f"想定外のスライス形状: {piece.shape}")
            with profiler.span("tobytes"):
                payload = np.ascontiguousarray(piece).tobytes(order="C")
            chunk_count = math.ceil(len(payload) / self.chunk_size)
            for chunk_id in range(chunk_count):
                start = chunk_id * self.chunk_size
                chunk = payload[start:start + self.chunk_size]
                with profiler.span("crc32"):
                    crc = zlib.crc32(chunk) & 0xFFFFFFFF
                header = HEADER.pack(
                    MAGIC,
                    frame_id & 0xFFFFFFFF,
                    target_id,
                    int(palette_mode),
                    chunk_id,
                    chunk_count,
                    len(chunk),
                    crc,
                )
                packet = header + chunk
                with profiler.span("sendto"):
                    self.socket.sendto(packet, destination)
                profiler.count("tx_packets")
                profiler.count("tx_bytes", len(packet))
                # 実際に線に乗るバイト数（Ethernet 18 + preamble/IFG 20 + IP 20 + UDP 8）。
                profiler.count("wire_bytes", len(packet) + 66)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="test.webp を FC6/MSX16 パレットで DVD ロゴ風に反射移動させる。"
    )
    parser.add_argument(
        "--image",
        type=Path,
        default=Path("test.webp"),
        help="WebP ファイル",
    )
    parser.add_argument("--palette", choices=("fc6", "msx16"), default="fc6")
    parser.add_argument(
        "--render-mode",
        choices=("image", "mask"),
        default="image",
        help="image=画像をパレット量子化, mask=1bit化してパレット順に着色",
    )
    parser.add_argument("--background", type=parse_int, default=None, help="パレット番号 例: 0x30")
    parser.add_argument("--mask-threshold", type=int, default=128, help="mask モードの輝度しきい値")
    parser.add_argument("--invert", action="store_true", help="mask モードの前景/背景を反転")
    parser.add_argument("--no-fit", action="store_true", help="画面より大きい画像を縮小せずエラーにする")
    parser.add_argument(
        "--color-style",
        choices=("cycle", "solid"),
        default="cycle",
        help="cycle=帯ごとにパレット順で着色, solid=全体を1色にして順送り",
    )
    parser.add_argument(
        "--color-start",
        type=parse_int,
        default=None,
        help="巡回の開始インデックス。既定は FC6=0x00、MSX16=0x01。"
             "終端は FC6=51(0x33)、MSX16=15(0x0F)",
    )
    parser.add_argument("--stripe-width", type=int, default=3, help="色帯の幅 [pixel]")
    parser.add_argument("--rainbow-hz", type=float, default=0.8, help="巡回速度 [周/秒]")
    parser.add_argument("--speed-x", type=float, default=83.0, help="X方向速度 [pixel/s]")
    parser.add_argument("--speed-y", type=float, default=109.0, help="Y方向速度 [pixel/s]")
    parser.add_argument("--fps", type=float, default=60.0)
    parser.add_argument("--preview-scale", type=int, default=2)
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument("--frames", type=int, default=0, help="N フレームで終了。0 は無制限")
    parser.add_argument("--send", action="store_true", help="192x96 の 4 スライスを UDP 送信する")
    parser.add_argument(
        "--pi",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help="Pi の宛先。--send 時はちょうど 4 回指定する",
    )
    parser.add_argument("--chunk-size", type=int, default=1200)
    parser.add_argument("--gif", type=Path, default=None, help="シミュレーションを GIF 保存して終了")
    parser.add_argument("--gif-seconds", type=float, default=8.0, help="GIF の長さ [秒]")
    parser.add_argument("--gif-fps", type=float, default=20.0, help="GIF のフレームレート")
    parser.add_argument("--gif-scale", type=int, default=2, help="GIF の拡大率（最近傍）")
    add_profile_arguments(parser)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.fps <= 0:
        raise SystemExit("--fps は 0 より大きい値")
    if args.preview_scale <= 0:
        raise SystemExit("--preview-scale は 0 より大きい値")

    try:
        rgb, opaque = load_webp(args.image)
        if args.image.resolve() == DEFAULT_IMAGE.resolve():
            rgb, opaque = crop_logo(rgb, opaque)
        if args.no_fit and (
            opaque.shape[1] > CANVAS_WIDTH or opaque.shape[0] > CANVAS_HEIGHT
        ):
            raise ValueError(
                f"画像 {opaque.shape[1]}x{opaque.shape[0]} が {CANVAS_WIDTH}x{CANVAS_HEIGHT} を超える"
            )
        if not args.no_fit:
            rgb, opaque = fit_image(rgb, opaque, CANVAS_WIDTH, CANVAS_HEIGHT)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    palette_mode = PaletteMode.FC6 if args.palette == "fc6" else PaletteMode.MSX16
    try:
        sequence = color_sequence(palette_mode, args.color_start)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    renderer = TestRenderer(
        rgb=rgb,
        opaque=opaque,
        palette_mode=palette_mode,
        background_index=args.background,
        render_mode=args.render_mode,
        color_style=args.color_style,
        color_start=args.color_start,
        stripe_width=args.stripe_width,
        rainbow_hz=args.rainbow_hz,
        mask_threshold=args.mask_threshold,
        invert=args.invert,
        speed_x=args.speed_x,
        speed_y=args.speed_y,
    )

    if args.gif is not None:
        try:
            written = record_gif(
                renderer, args.gif, args.gif_seconds, args.gif_fps, args.gif_scale
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(
            f"gif: {args.gif} {written} frames, {args.gif_fps:g}fps, "
            f"{CANVAS_WIDTH * args.gif_scale}x{CANVAS_HEIGHT * args.gif_scale}, "
            f"palette={renderer.palette_mode.name}, colors={sequence[0]:#04x}-{sequence[-1]:#04x}"
        )
        return 0

    profiler = Profiler(enabled=args.profile, label="TEST1")

    sender: UdpFrameSender | None = None
    if args.send:
        if len(args.pi) != PI_COUNT:
            print("error: --send には --pi HOST[:PORT] をちょうど 4 個指定する", file=sys.stderr)
            return 2
        try:
            sender = UdpFrameSender(
                [parse_pi(item) for item in args.pi], args.chunk_size, profiler
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    running = True
    paused = False

    def stop_handler(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    frame_period = 1.0 / args.fps
    next_deadline = time.monotonic()
    last_update = next_deadline
    frame_id = 0

    print(
        f"test mode: {args.image} {opaque.shape[1]}x{opaque.shape[0]}, "
        f"palette={renderer.palette_mode.name}, render={renderer.render_mode}, "
        f"colors={sequence[0]:#04x}-{sequence[-1]:#04x}({len(sequence)}), "
        f"canvas={CANVAS_WIDTH}x{CANVAS_HEIGHT}, fps={args.fps:g}"
    )
    if not args.no_preview:
        print("keys: q/ESC 終了, SPACE 一時停止, f FC6, m MSX16, p パレット切替, i 描画モード切替, r リセット")

    try:
        while running and (args.frames <= 0 or frame_id < args.frames):
            now = time.monotonic()
            dt = min(0.1, max(0.0, now - last_update))
            last_update = now
            if not paused:
                with profiler.span("update"):
                    renderer.update(dt)

            with profiler.span("render"):
                indexed = renderer.render(now)
            if sender is not None:
                with profiler.span("send"):
                    sender.send(frame_id, renderer.palette_mode, indexed)

            if not args.no_preview:
                with profiler.span("preview"):
                    preview = renderer.rgb_preview(indexed)
                    if args.preview_scale != 1:
                        preview = cv2.resize(
                            preview,
                            (
                                CANVAS_WIDTH * args.preview_scale,
                                CANVAS_HEIGHT * args.preview_scale,
                            ),
                            interpolation=cv2.INTER_NEAREST,
                        )
                    cv2.imshow("RGB LED test mode", preview)
                with profiler.span("waitkey"):
                    key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")):
                    running = False
                elif key == ord(" "):
                    paused = not paused
                elif key == ord("f"):
                    renderer.palette_mode = PaletteMode.FC6
                elif key == ord("m"):
                    renderer.palette_mode = PaletteMode.MSX16
                elif key == ord("p"):
                    renderer.toggle_palette()
                elif key == ord("i"):
                    renderer.toggle_render_mode()
                elif key == ord("r"):
                    renderer.reset()

            frame_id += 1
            profiler.frame()
            next_deadline += frame_period
            sleep_time = next_deadline - time.monotonic()
            if sleep_time > 0:
                # 余った時間。ここが 0 に近いなら主機が律速している。
                profiler.count("slack_us", int(sleep_time * 1e6))
                with profiler.span("sleep"):
                    time.sleep(sleep_time)
            else:
                profiler.count("late_frames")
                if sleep_time < -frame_period:
                    # 期限を過ぎた分は捨て、遅延フレームを連射しない。
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
