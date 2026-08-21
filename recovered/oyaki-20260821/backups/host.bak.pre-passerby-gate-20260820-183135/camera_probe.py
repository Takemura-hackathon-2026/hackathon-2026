#!/usr/bin/env python3
"""USB カメラの能力を V4L2 へ直接問い合わせる（標準ライブラリのみ）。

主機はインターネットへ常時接続していないため、`v4l2-ctl`（v4l-utils）や OpenCV を
入れずに、対応フォーマット・解像度・フレームレートを確認できるようにする。
ioctl を fcntl で直接叩くだけで、外部依存はない。

    python3 camera_probe.py                # /dev/video0
    python3 camera_probe.py /dev/video1
    python3 camera_probe.py --json out.json

計画書 §3（背景差分による LEFT/RIGHT/JUMP 判定）の入力段が、どの解像度で何 fps
出せるかを決めるための下調べ。実際の取得レートの測定は別（GStreamer を使う）。
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import os
import sys
from pathlib import Path

# ioctl 番号: (dir << 30) | (size << 16) | (type << 8) | nr
_IOC_READ = 2
_IOC_WRITE = 1
_V4L2 = ord("V")


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (_V4L2 << 8) | nr


VIDIOC_QUERYCAP = _ioc(_IOC_READ, 0, 104)
VIDIOC_ENUM_FMT = _ioc(_IOC_READ | _IOC_WRITE, 2, 64)
VIDIOC_ENUM_FRAMESIZES = _ioc(_IOC_READ | _IOC_WRITE, 74, 44)
VIDIOC_ENUM_FRAMEINTERVALS = _ioc(_IOC_READ | _IOC_WRITE, 75, 52)

BUF_TYPE_VIDEO_CAPTURE = 1
FRMSIZE_DISCRETE, FRMSIZE_CONTINUOUS, FRMSIZE_STEPWISE = 1, 2, 3

# v4l2_capability のうち参照するビット。
CAPABILITIES = (
    (0x00000001, "VIDEO_CAPTURE"),
    (0x00001000, "VIDEO_CAPTURE_MPLANE"),
    (0x01000000, "STREAMING"),
    (0x00000001 << 16, "READWRITE"),
)


def fourcc(value: int) -> str:
    return "".join(chr((value >> shift) & 0xFF) for shift in (0, 8, 16, 24)).strip()


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def query_capability(fd: int) -> dict[str, object]:
    buf = ctypes.create_string_buffer(104)
    fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
    raw = buf.raw
    version, capabilities, device_caps = (
        int.from_bytes(raw[80:84], "little"),
        int.from_bytes(raw[84:88], "little"),
        int.from_bytes(raw[88:92], "little"),
    )
    flags = [name for bit, name in CAPABILITIES if device_caps & bit]
    return {
        "driver": _cstr(raw[0:16]),
        "card": _cstr(raw[16:48]),
        "bus_info": _cstr(raw[48:80]),
        "version": f"{version >> 16}.{(version >> 8) & 0xFF}.{version & 0xFF}",
        "capabilities": flags,
    }


def enum_intervals(fd: int, pixel_format: int, width: int, height: int) -> list[str]:
    """この解像度で選べるフレームレートを列挙する。"""
    results: list[str] = []
    for index in range(64):
        buf = ctypes.create_string_buffer(52)
        buf[0:4] = index.to_bytes(4, "little")
        buf[4:8] = pixel_format.to_bytes(4, "little")
        buf[8:12] = width.to_bytes(4, "little")
        buf[12:16] = height.to_bytes(4, "little")
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, buf)
        except OSError:
            break
        raw = buf.raw
        kind = int.from_bytes(raw[16:20], "little")
        if kind == FRMSIZE_DISCRETE:
            numerator = int.from_bytes(raw[20:24], "little")
            denominator = int.from_bytes(raw[24:28], "little")
            if numerator:
                results.append(f"{denominator / numerator:g}")
        else:
            # 連続・段階指定。最短間隔＝最大 fps だけ拾う。
            numerator = int.from_bytes(raw[20:24], "little")
            denominator = int.from_bytes(raw[24:28], "little")
            if numerator:
                results.append(f"<={denominator / numerator:g}")
            break
    return results


def enum_framesizes(fd: int, pixel_format: int) -> list[dict[str, object]]:
    sizes: list[dict[str, object]] = []
    for index in range(128):
        buf = ctypes.create_string_buffer(44)
        buf[0:4] = index.to_bytes(4, "little")
        buf[4:8] = pixel_format.to_bytes(4, "little")
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMESIZES, buf)
        except OSError:
            break
        raw = buf.raw
        kind = int.from_bytes(raw[8:12], "little")
        if kind == FRMSIZE_DISCRETE:
            width = int.from_bytes(raw[12:16], "little")
            height = int.from_bytes(raw[16:20], "little")
            sizes.append({
                "width": width,
                "height": height,
                "fps": enum_intervals(fd, pixel_format, width, height),
            })
        else:
            sizes.append({
                "min_width": int.from_bytes(raw[12:16], "little"),
                "max_width": int.from_bytes(raw[16:20], "little"),
                "min_height": int.from_bytes(raw[24:28], "little"),
                "max_height": int.from_bytes(raw[28:32], "little"),
                "stepwise": True,
            })
            break
    return sizes


def enum_formats(fd: int) -> list[dict[str, object]]:
    formats: list[dict[str, object]] = []
    for index in range(64):
        buf = ctypes.create_string_buffer(64)
        buf[0:4] = index.to_bytes(4, "little")
        buf[4:8] = BUF_TYPE_VIDEO_CAPTURE.to_bytes(4, "little")
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FMT, buf)
        except OSError:
            break
        raw = buf.raw
        pixel_format = int.from_bytes(raw[44:48], "little")
        formats.append({
            "fourcc": fourcc(pixel_format),
            "description": _cstr(raw[12:44]),
            "compressed": bool(int.from_bytes(raw[8:12], "little") & 0x0001),
            "sizes": enum_framesizes(fd, pixel_format),
        })
    return formats


def probe(device: str) -> dict[str, object]:
    fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    try:
        return {
            "device": device,
            "capability": query_capability(fd),
            "formats": enum_formats(fd),
        }
    finally:
        os.close(fd)


def report(info: dict[str, object]) -> None:
    capability = info["capability"]
    print(f"device : {info['device']}")
    print(f"card   : {capability['card']}")
    print(f"driver : {capability['driver']} {capability['version']}")
    print(f"bus    : {capability['bus_info']}")
    print(f"caps   : {', '.join(capability['capabilities']) or '-'}")
    for entry in info["formats"]:
        mark = "圧縮" if entry["compressed"] else "非圧縮"
        print(f"\n[{entry['fourcc']}] {entry['description']} ({mark})")
        for size in entry["sizes"]:
            if size.get("stepwise"):
                print(f"  {size['min_width']}x{size['min_height']} "
                      f"〜 {size['max_width']}x{size['max_height']}（連続）")
                continue
            rates = ", ".join(f"{value}fps" for value in size["fps"]) or "-"
            print(f"  {size['width']:>5}x{size['height']:<5} {rates}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V4L2 カメラの能力を調べる")
    parser.add_argument("device", nargs="?", default="/dev/video0")
    parser.add_argument("--json", type=Path, default=None, help="結果を JSON で保存")
    args = parser.parse_args(argv)

    try:
        info = probe(args.device)
    except PermissionError:
        print(
            f"error: {args.device} を開けない（権限不足）。\n"
            f"       現在のユーザーが video グループに入っているか確認する:\n"
            f"         sudo usermod -aG video $USER   # 実行後に再ログイン",
            file=sys.stderr,
        )
        return 2
    except FileNotFoundError:
        print(f"error: {args.device} が存在しない", file=sys.stderr)
        return 2
    except OSError as exc:
        if exc.errno == errno.ENOTTY:
            print(f"error: {args.device} は V4L2 デバイスではない", file=sys.stderr)
            return 2
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report(info)
    if args.json is not None:
        args.json.write_text(json.dumps(info, indent=2, ensure_ascii=False))
        print(f"\njson: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
