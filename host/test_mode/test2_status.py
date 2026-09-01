#!/usr/bin/env python3
"""TEST2: 主機と各 Pi の状況を文字で交互に表示する。

192x384 の論理画面へ 1 ページずつ状態を描き、主機 → PI1 → PI2 → PI3 →
主機 … と切り替えながら 3 台へ送る。文字はパレット登録色のみで描画する。

各 Pi の情報は `pi_client` が UDP 5101 へ 1 秒ごとに送る死活報告から得る。
報告が途絶えた Pi は NO SIGNAL として表示する。
"""
from __future__ import annotations

import argparse
import signal
import socket
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
    parse_pi,
)

HEALTH_PORT = 5101
HEALTH_TIMEOUT = 3.0  # この秒数報告が無ければ NO SIGNAL 扱い

# ページ見出しの色。FC6 / MSX16 それぞれの登録インデックスから選ぶ。
TITLE_COLORS = {
    PaletteMode.FC6: (0x0E, 0x15, 0x22, 0x29, 0x2D),  # 黄, 緑, 空, 紫, マゼンタ
    PaletteMode.MSX16: (0x0A, 0x02, 0x07, 0x04, 0x0D),
}
OK_COLOR = {PaletteMode.FC6: 0x15, PaletteMode.MSX16: 0x02}       # 緑
WARN_COLOR = {PaletteMode.FC6: 0x0A, PaletteMode.MSX16: 0x09}     # 橙
ERROR_COLOR = {PaletteMode.FC6: 0x04, PaletteMode.MSX16: 0x08}    # 赤
TEXT_COLOR = {PaletteMode.FC6: FC6_WHITE, PaletteMode.MSX16: 0x0F}
DIM_COLOR = {PaletteMode.FC6: 0x32, PaletteMode.MSX16: 0x0E}      # 明るい灰


class HealthReceiver:
    """各 Pi からの死活報告を受け取る。"""

    def __init__(self, port: int = HEALTH_PORT) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("0.0.0.0", port))
        self.socket.setblocking(False)
        self.reports: dict[int, dict[str, str]] = {}
        self.last_seen: dict[int, float] = {}
        self.source: dict[int, str] = {}

    def poll(self) -> None:
        while True:
            try:
                packet, addr = self.socket.recvfrom(512)
            except BlockingIOError:
                return
            except OSError:
                return
            try:
                text = packet.decode("ascii", "ignore")
            except UnicodeDecodeError:
                continue
            if not text.startswith("PIHEALTH"):
                continue
            fields: dict[str, str] = {}
            for token in text.split()[1:]:
                if "=" in token:
                    key, value = token.split("=", 1)
                    fields[key] = value
            try:
                target = int(fields["target"])
            except (KeyError, ValueError):
                continue
            self.reports[target] = fields
            self.last_seen[target] = time.monotonic()
            self.source[target] = addr[0]

    def status(self, target: int) -> tuple[str, dict[str, str], float]:
        """(状態, 報告内容, 最終受信からの経過秒) を返す。"""
        seen = self.last_seen.get(target)
        if seen is None:
            return "NO SIGNAL", {}, float("inf")
        age = time.monotonic() - seen
        if age > HEALTH_TIMEOUT:
            return "LOST", self.reports.get(target, {}), age
        return "OK", self.reports.get(target, {}), age

    def close(self) -> None:
        self.socket.close()


class StatusRenderer:
    def __init__(self, palette_mode: PaletteMode, destinations: Sequence[str]) -> None:
        self.palette_mode = palette_mode
        self.destinations = destinations
        self.start_time = time.monotonic()

    @property
    def palette(self):
        return FC6 if self.palette_mode == PaletteMode.FC6 else MSX16

    @property
    def background(self) -> int:
        return FC6_BLACK if self.palette_mode == PaletteMode.FC6 else MSX16_BLACK

    def _text(
        self,
        frame: np.ndarray,
        origin: tuple[int, int],
        text: str,
        color_index: int,
        scale: float = 0.34,
        thickness: int = 1,
    ) -> None:
        """パレット番号だけを使って文字を焼き込む。"""
        mask = np.zeros(frame.shape, dtype=np.uint8)
        cv2.putText(
            mask,
            text,
            origin,
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            255,
            thickness,
            cv2.LINE_AA,
        )
        frame[mask > 96] = color_index

    def _page_header(self, frame: np.ndarray, title: str, color_index: int) -> None:
        frame[0:16, :] = color_index
        mask = np.zeros(frame.shape, dtype=np.uint8)
        cv2.putText(mask, title, (4, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.42, 255, 1,
                    cv2.LINE_AA)
        frame[mask > 96] = self.background

    def render_host(self, frame_id: int, send_fps: float) -> np.ndarray:
        frame = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), self.background, dtype=np.uint8)
        titles = TITLE_COLORS[self.palette_mode]
        self._page_header(frame, "HOST", titles[0])

        uptime = time.monotonic() - self.start_time
        lines = [
            ("PALETTE", self.palette_mode.name, TEXT_COLOR[self.palette_mode]),
            ("SEND FPS", f"{send_fps:.1f}", OK_COLOR[self.palette_mode]
             if send_fps >= 30 else WARN_COLOR[self.palette_mode]),
            ("FRAME ID", str(frame_id), TEXT_COLOR[self.palette_mode]),
            ("CANVAS", f"{CANVAS_WIDTH}x{CANVAS_HEIGHT}", DIM_COLOR[self.palette_mode]),
            ("SLICE", f"{CANVAS_WIDTH}x{PI_HEIGHT} x{PI_COUNT}", DIM_COLOR[self.palette_mode]),
            ("UPTIME", f"{int(uptime)}s", TEXT_COLOR[self.palette_mode]),
        ]
        y = 40
        for label, value, color in lines:
            self._text(frame, (6, y), label, DIM_COLOR[self.palette_mode], 0.3)
            self._text(frame, (6, y + 16), value, color, 0.42)
            y += 40
        # 送信先一覧を最下部に小さく並べる。
        y = CANVAS_HEIGHT - 8 - 12 * len(self.destinations)
        for index, host in enumerate(self.destinations):
            self._text(frame, (6, y), f"{index}:{host}", DIM_COLOR[self.palette_mode], 0.28)
            y += 12
        return frame

    def render_pi(
        self, target: int, state: str, fields: dict[str, str], age: float, host: str
    ) -> np.ndarray:
        frame = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), self.background, dtype=np.uint8)
        titles = TITLE_COLORS[self.palette_mode]
        self._page_header(frame, f"PI{target + 1}", titles[(target + 1) % len(titles)])

        if state == "OK":
            state_color = OK_COLOR[self.palette_mode]
        elif state == "LOST":
            state_color = WARN_COLOR[self.palette_mode]
        else:
            state_color = ERROR_COLOR[self.palette_mode]

        dropped = fields.get("dropped", "-")
        drop_color = (
            ERROR_COLOR[self.palette_mode]
            if dropped not in ("-", "0")
            else TEXT_COLOR[self.palette_mode]
        )
        fps_text = fields.get("fps", "-")
        try:
            fps_color = (
                OK_COLOR[self.palette_mode]
                if float(fps_text) >= 30
                else WARN_COLOR[self.palette_mode]
            )
        except ValueError:
            fps_color = DIM_COLOR[self.palette_mode]

        lines = [
            ("STATE", state, state_color),
            ("ADDR", host, DIM_COLOR[self.palette_mode]),
            ("TARGET ID", str(target), TEXT_COLOR[self.palette_mode]),
            ("SHOWN", fields.get("displayed", "-"), TEXT_COLOR[self.palette_mode]),
            ("DROPPED", dropped, drop_color),
            ("FPS", fps_text, fps_color),
            ("ROTATE", "180" if fields.get("rot") == "1" else "0",
             DIM_COLOR[self.palette_mode]),
            ("SEEN", "-" if age == float("inf") else f"{age:.1f}s ago",
             DIM_COLOR[self.palette_mode]),
        ]
        y = 36
        for label, value, color in lines:
            self._text(frame, (6, y), label, DIM_COLOR[self.palette_mode], 0.28)
            self._text(frame, (6, y + 14), value, color, 0.38)
            y += 34
        return frame

    def rgb_preview(self, indexed: np.ndarray) -> np.ndarray:
        lut = np.asarray([entry[:3] for entry in self.palette], dtype=np.uint8)
        return lut[indexed][:, :, ::-1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TEST2: 主機と各 Pi の状況を交互に文字表示する"
    )
    parser.add_argument("--palette", choices=("fc6", "msx16"), default="fc6")
    parser.add_argument("--page-seconds", type=float, default=3.0, help="1ページの表示秒数")
    parser.add_argument("--fps", type=float, default=30.0, help="送信フレームレート")
    parser.add_argument("--seconds", type=float, default=0.0, help="実行秒数。0は無制限")
    parser.add_argument("--health-port", type=int, default=HEALTH_PORT)
    parser.add_argument("--boundary", action="store_true", help="Pi境界に区切り線を引く")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--pi", action="append", default=[], metavar="HOST[:PORT]")
    parser.add_argument("--chunk-size", type=int, default=1200)
    parser.add_argument("--preview-scale", type=int, default=2)
    parser.add_argument("--no-preview", action="store_true")
    add_profile_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    palette_mode = PaletteMode.FC6 if args.palette == "fc6" else PaletteMode.MSX16

    destinations = [parse_pi(item) for item in args.pi] if args.pi else []
    profiler = Profiler(enabled=args.profile, label="TEST2")
    sender: UdpFrameSender | None = None
    if args.send:
        if len(destinations) != PI_COUNT:
            print(f"error: --send には --pi をちょうど {PI_COUNT} 個指定する", file=sys.stderr)
            return 2
        sender = UdpFrameSender(destinations, args.chunk_size, profiler)

    renderer = StatusRenderer(
        palette_mode, [host for host, _port in destinations] or ["-"] * PI_COUNT
    )
    health = HealthReceiver(args.health_port)

    running = True

    def stop_handler(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    frame_period = 1.0 / args.fps
    next_deadline = time.monotonic()
    started = next_deadline
    frame_id = 0
    fps_window_start = next_deadline
    fps_window_frames = 0
    send_fps = 0.0
    page_count = 1 + PI_COUNT  # HOST + PI1..PI3

    print(
        f"TEST2: palette={palette_mode.name} page={args.page_seconds:g}s "
        f"fps={args.fps:g} health_port={args.health_port} send={'yes' if sender else 'no'}"
    )

    try:
        while running:
            now = time.monotonic()
            if args.seconds > 0 and now - started >= args.seconds:
                break
            with profiler.span("health"):
                health.poll()

            page = int((now - started) / args.page_seconds) % page_count
            with profiler.span("render"):
                if page == 0:
                    indexed = renderer.render_host(frame_id, send_fps)
                else:
                    target = page - 1
                    state, fields, age = health.status(target)
                    host = health.source.get(
                        target,
                        destinations[target][0] if target < len(destinations) else "-",
                    )
                    indexed = renderer.render_pi(target, state, fields, age, host)

            if args.boundary:
                for index in range(1, PI_COUNT):
                    indexed[index * PI_HEIGHT, :] = DIM_COLOR[palette_mode]

            if sender is not None:
                with profiler.span("send"):
                    sender.send(frame_id, palette_mode, indexed)

            if not args.no_preview:
                with profiler.span("preview"):
                    preview = renderer.rgb_preview(indexed)
                    if args.preview_scale != 1:
                        preview = cv2.resize(
                            preview,
                            (CANVAS_WIDTH * args.preview_scale,
                             CANVAS_HEIGHT * args.preview_scale),
                            interpolation=cv2.INTER_NEAREST,
                        )
                    cv2.imshow("TEST2 status", preview)
                with profiler.span("waitkey"):
                    if (cv2.waitKey(1) & 0xFF) in (27, ord("q")):
                        running = False

            frame_id += 1
            profiler.frame()
            fps_window_frames += 1
            if now - fps_window_start >= 1.0:
                send_fps = fps_window_frames / (now - fps_window_start)
                fps_window_start = now
                fps_window_frames = 0

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
        health.close()
        if not args.no_preview:
            cv2.destroyAllWindows()
        finish_profile(profiler, args.profile_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
