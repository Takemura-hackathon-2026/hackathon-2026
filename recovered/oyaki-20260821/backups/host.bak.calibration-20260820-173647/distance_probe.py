#!/usr/bin/env python3
"""近影・中距離・遠影の3距離帯を、LEDへ表示しながら継続的に実測・キャリブレする。

背景:
  camera_calibration.json の閾値は単一の立ち位置で測ったもので、立ち位置が変わると
  同じ動作でも測定値が変わる。実測では、体が画角に収まらない距離だと輪郭の下端が
  画面下端に張り付き、ジャンプしても rise_bottom が 0 のままになる。
  そのため距離帯ごとに基準姿勢と閾値を持つ。

画面と操作:
  LED 192x384 に現在の距離帯・実測値・サンプル数・算出閾値を出す。
  操作は親機に接続したキーボード（このプロセスの標準入力）で行う。
  ssh 越しに動かす場合は tty が要る（ssh -t）。tty が無いときは表示のみで動く。

出力:
  camera_calibration_multi.json。距離帯ごとに camera_calibration.json と同じ構造の
  thresholds を持つので、そのまま block_breaker へ渡せる形にしてある。

閾値の算出方法は camera_calibrate.py の記録（thresholds[*].source）に合わせた。
  left/right delta_min = 各ステージで測った offset の p25
  jump rise_y/rise_bottom = JUMP で測った rise の p25
  center_tolerance = STANCE の MAD の 3 倍
"""
from __future__ import annotations

import argparse
import json
import os
import select
import struct
import sys
import termios
import tty
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

HOST = Path(__file__).resolve().parent
for directory in (HOST, HOST / "test_mode"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import camera_calibrate as cc  # noqa: E402  既存の描画・前処理・カメラ資産を再利用する
from test_mode import PaletteMode, UdpFrameSender, parse_pi  # noqa: E402

# Linux input_event: struct timeval(16) + type(2) + code(2) + value(4) = 24 バイト
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EV_KEY = 0x01
# このツールで使うキーだけを引く。スキャンコードは linux/input-event-codes.h より。
KEYCODES = {2: "1", 3: "2", 4: "3", 16: "q", 17: "w", 19: "r", 31: "s",
            36: "j", 38: "l", 46: "c", 47: "v", 48: "b"}


def find_keyboard() -> str | None:
    """/proc/bus/input/devices から物理キーボードの event ノードを選ぶ。

    親機には画面が無くコンソールへログインできないため、tty ではなく入力デバイスを
    直接読む。カメラ(See3CAM_130)のように kbd ハンドラを持つだけの機器を拾わないよう、
    名前に KEYBOARD を含み LED を持つものを優先する。
    """
    try:
        blocks = Path("/proc/bus/input/devices").read_text().split("\n\n")
    except OSError:
        return None
    best: tuple[int, str] | None = None
    for block in blocks:
        if "Handlers=" not in block:
            continue
        name = ""
        handlers = ""
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip().strip('"').upper()
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1]
        if "kbd" not in handlers:
            continue
        node = next((part for part in handlers.split() if part.startswith("event")), None)
        if node is None:
            continue
        score = 0
        if "KEYBOARD" in name:
            score += 4
        if "leds" in handlers:
            score += 2
        if "CONSUMER" in name or "SYSTEM CONTROL" in name or "BUTTON" in name:
            score -= 4
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, f"/dev/input/{node}")
    return None if best is None else best[1]


class EventKeys:
    """入力デバイスから直接キー押下を読む。tty もログインも要らない。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def close(self) -> None:
        os.close(self.fd)

    def read(self) -> str | None:
        ready, _, _ = select.select([self.fd], [], [], 0.0)
        if not ready:
            return None
        try:
            data = os.read(self.fd, EVENT_SIZE * 64)
        except (BlockingIOError, OSError):
            return None
        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _, _, kind, code, value = struct.unpack(EVENT_FORMAT, data[offset:offset + EVENT_SIZE])
            if kind == EV_KEY and value == 1 and code in KEYCODES:  # 1 = 押した瞬間のみ
                return KEYCODES[code]
        return None


class RawKeys:
    """1キー押下を即読むための端末設定。

    camera_calibrate.py の _stdin_key() は select+read(1) だが端末が canonical mode の
    ままなので Enter を押すまで届かない。ここでは cbreak にしてエコーも切る。
    Ctrl+C を効かせたいので raw ではなく cbreak を使う。終了時は必ず元へ戻す。
    """

    def __init__(self) -> None:
        self.fd: int | None = None
        self.saved = None

    def __enter__(self) -> "RawKeys":
        if not sys.stdin.isatty():
            return self
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[3] &= ~termios.ECHO  # 押したキーを画面に出さない
        termios.tcsetattr(self.fd, termios.TCSADRAIN, attrs)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None and self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def read(self) -> str | None:
        if self.fd is None:
            return None
        ready, _, _ = select.select([sys.stdin], [], [], 0.0)
        if not ready:
            return None
        char = sys.stdin.read(1)
        return char.lower() if char else None


ZONES = ("NEAR", "MID", "FAR")
LABELS = ("STANCE", "LEFT", "RIGHT", "JUMP")
MAD_TO_TOLERANCE = 3.0  # camera_calibration.json の center_tolerance = MAD*3 に一致
OUTPUT_DEFAULT = HOST.parent / "camera_calibration_multi.json"


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=float), q))


def mad(values: list[float]) -> float:
    data = np.asarray(values, dtype=float)
    return float(np.median(np.abs(data - np.median(data))))


class Zone:
    """1距離帯ぶんのサンプルと、そこから導いた閾値。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.samples: dict[str, list[dict[str, float]]] = {label: [] for label in LABELS}

    def clear(self) -> None:
        for label in LABELS:
            self.samples[label].clear()

    def count(self, label: str) -> int:
        return len(self.samples[label])

    @property
    def baseline(self) -> dict[str, float] | None:
        rows = self.samples["STANCE"]
        if len(rows) < 8:
            return None
        return {key: float(np.median([row[key] for row in rows])) for key in ("x", "y", "bottom", "area")}

    def gates(self) -> tuple[float, float, float]:
        """STANCE のばらつきから、動きとみなす最小幅を出す。

        camera_calibrate.py の _center_gates と同じ MAD*3（下限は2画素ぶん）。
        """
        center = self.samples["STANCE"]
        if not center:
            return 2.0 / 240, 2.0 / 320, 2.0 / 320
        out = []
        for field, pixels in (("x", 240), ("y", 320), ("bottom", 320)):
            values = np.asarray([row[field] for row in center], dtype=float)
            out.append(max(3.0 * float(np.median(np.abs(values - np.median(values)))), 2.0 / pixels))
        return out[0], out[1], out[2]

    def accepts(self, label: str, m: dict[str, float]) -> bool:
        """そのラベルの動きが実際に起きているフレームだけ採る。

        camera_calibrate.py の eligible と同じ条件。これを通さずに収集すると、
        たとえば JUMP で着地中のフレームまで混ざり、p25 が 0 に潰れて閾値にならない。
        """
        if label == "STANCE":
            return True
        base = self.baseline
        if base is None:
            return False
        gate_x, gate_y, gate_bottom = self.gates()
        if label == "LEFT":
            return m["x"] < base["x"] - gate_x
        if label == "RIGHT":
            return m["x"] > base["x"] + gate_x
        if label == "JUMP":
            return base["y"] - m["y"] > gate_y and base["bottom"] - m["bottom"] > gate_bottom
        return False

    def thresholds(self) -> dict | None:
        """全ラベルが揃っていれば閾値を返す。足りなければ None。"""
        base = self.baseline
        if base is None:
            return None
        need = {label: 8 for label in LABELS}
        if any(self.count(label) < need[label] for label in LABELS):
            return None
        left_offsets = [base["x"] - row["x"] for row in self.samples["LEFT"]]
        right_offsets = [row["x"] - base["x"] for row in self.samples["RIGHT"]]
        rise_y = [base["y"] - row["y"] for row in self.samples["JUMP"]]
        rise_bottom = [base["bottom"] - row["bottom"] for row in self.samples["JUMP"]]
        stance_x = [row["x"] for row in self.samples["STANCE"]]
        tolerance = mad(stance_x) * MAD_TO_TOLERANCE
        left_min = percentile(left_offsets, 25)
        right_min = percentile(right_offsets, 25)
        rise_y_min = percentile(rise_y, 25)
        rise_bottom_min = percentile(rise_bottom, 25)
        # 動きが取れていない軸があれば閾値として成立しない。特に体が画角からはみ出す
        # 距離だと、跳んでも輪郭の下端が画面下端に張り付いたままで rise_bottom が 0 になる。
        # そのまま保存すると block_breaker 側で「ジャンプ閾値は正の値」に弾かれる。
        if min(left_min, right_min, rise_y_min, rise_bottom_min) <= 0:
            return None
        # block_breaker の InputClassifier は center_tolerance < delta_min を要求する。
        if tolerance <= 0 or tolerance >= min(left_min, right_min):
            tolerance = min(left_min, right_min) * 0.3
        return {
            "center_tolerance": {"x": tolerance, "source": "STANCE measured MAD x3"},
            "left": {"delta_min": left_min, "x_max": percentile([row["x"] for row in self.samples["LEFT"]], 75),
                     "source": "LEFT measured p25 offset"},
            "right": {"delta_min": right_min, "x_min": percentile([row["x"] for row in self.samples["RIGHT"]], 25),
                      "source": "RIGHT measured p25 offset"},
            "jump": {"rise_y_min": rise_y_min, "rise_bottom_min": rise_bottom_min,
                     "source": "JUMP measured p25 rise"},
        }

    def blocked_reason(self) -> str | None:
        """閾値を出せない理由。出せるなら None。"""
        if self.count("STANCE") < 8:
            return "NEED STANCE"
        for label in ("LEFT", "RIGHT", "JUMP"):
            if self.count(label) < 8:
                return f"NEED {label}"
        if self.thresholds() is None:
            base = self.baseline
            if base is not None and base["bottom"] >= 0.99:
                return "FEET CUT OFF"
            rows = self.samples["JUMP"]
            if base is not None and rows:
                if percentile([base["bottom"] - row["bottom"] for row in rows], 25) <= 0:
                    return "NO JUMP RISE"
                if percentile([base["y"] - row["y"] for row in rows], 25) <= 0:
                    return "NO JUMP LIFT"
            return "NO LR MOVE"
        return None


class Detector:
    """背景差分 → 最大連結成分 → 正規化計測。block_breaker と同じ手順にそろえる。"""

    def __init__(self, min_area: int) -> None:
        self.min_area = max(1, min_area)
        self.subtractor = cv2.createBackgroundSubtractorMOG2(history=240, varThreshold=20, detectShadows=False)
        self.background_until = 0.0
        self.mask: np.ndarray | None = None

    def learn_background(self, now: float, seconds: float) -> None:
        self.subtractor = cv2.createBackgroundSubtractorMOG2(history=240, varThreshold=20, detectShadows=False)
        self.background_until = now + seconds

    def measure(self, gray: np.ndarray, now: float) -> dict[str, float] | None:
        learning = now < self.background_until
        foreground = self.subtractor.apply(gray, learningRate=.35 if learning else 0.0)
        _, mask = cv2.threshold(foreground, 200, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        self.mask = mask
        if learning:
            return None
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        contour = max(contours, key=cv2.contourArea)
        area = float(cv2.contourArea(contour))
        moments = cv2.moments(contour)
        if area < self.min_area or not moments["m00"]:
            return None
        height, width = gray.shape[:2]
        _, y, _, h = cv2.boundingRect(contour)
        return {
            "x": float(moments["m10"] / moments["m00"] / width),
            "y": float(moments["m01"] / moments["m00"] / height),
            "bottom": float((y + h) / height),
            "area": area / float(width * height),
        }


def render(zone: Zone, collecting: str | None, measurement: dict[str, float] | None,
           learning: bool, message: str, frame_id: int, verify: bool,
           accepted: bool = False) -> np.ndarray:
    """LED 192x384 へ現在の状態を描く。"""
    frame = np.full((cc.CANVAS_HEIGHT, cc.CANVAS_WIDTH), cc.FC6_BLACK, dtype=np.uint8)
    cc._font_text(frame, zone.name, 6, 6, cc.LED_CYAN, 4)

    if learning:
        mode, color = "BG LEARN", cc.LED_YELLOW
    elif verify:
        mode, color = "VERIFY", cc.LED_BLUE
    elif collecting:
        mode = f"REC {collecting}" + ("*" if accepted else "")
        color = cc.LED_RED if accepted else cc.LED_YELLOW
    else:
        mode, color = "IDLE", cc.LED_GRAY
    cc._font_text(frame, mode, 6, 44, color, 2)

    present = measurement is not None
    cc._font_text(frame, "BODY OK" if present else "NO BODY",
                  6, 68, cc.LED_GREEN if present else cc.LED_RED, 2)

    # 実測値。BOTTOM は画角に体が収まっているかの判断に直結するので最初に出す。
    if measurement is not None:
        rows = (
            ("BOT", measurement["bottom"], cc.LED_RED if measurement["bottom"] >= 0.99 else cc.FC6_WHITE),
            ("Y", measurement["y"], cc.FC6_WHITE),
            ("X", measurement["x"], cc.FC6_WHITE),
            ("AR", measurement["area"], cc.FC6_WHITE),
        )
        for index, (name, value, tint) in enumerate(rows):
            cc._font_text(frame, f"{name} {value:0.3f}", 6, 94 + index * 20, tint, 2)
    else:
        cc._font_text(frame, "- - -", 6, 94, cc.LED_GRAY, 2)

    # 足元が切れていると跳んでも rise_bottom が動かない。最優先で警告する。
    if measurement is not None and measurement["bottom"] >= 0.99:
        cc._font_text(frame, "FEET CUT OFF", 6, 178, cc.LED_RED, 2)
    elif message:
        cc._font_text(frame, message[:15], 6, 178, cc.LED_CYAN, 2)

    frame[200:202, 6:186] = cc.LED_GRAY
    counts = " ".join(f"{label[0]}{zone.count(label):02d}" for label in LABELS)
    cc._font_text(frame, counts, 6, 208, cc.FC6_WHITE, 2)

    thresholds = zone.thresholds()
    if thresholds is None:
        cc._font_text(frame, zone.blocked_reason() or "NO DATA", 6, 232, cc.LED_YELLOW, 2)
    else:
        cc._font_text(frame, "READY", 6, 232, cc.LED_GREEN, 2)
        cc._font_text(frame, f"L {thresholds['left']['delta_min']:0.3f}", 6, 252, cc.LED_GREEN, 2)
        cc._font_text(frame, f"R {thresholds['right']['delta_min']:0.3f}", 6, 270, cc.LED_GREEN, 2)
        cc._font_text(frame, f"JY {thresholds['jump']['rise_y_min']:0.3f}", 6, 288, cc.LED_GREEN, 2)
        cc._font_text(frame, f"JB {thresholds['jump']['rise_bottom_min']:0.3f}", 6, 306, cc.LED_GREEN, 2)

    # キー割り当て。親機に繋いだキーボードで操作する。
    frame[324:325, 6:186] = cc.LED_GRAY
    for index, line in enumerate((
        "1 2 3 : ZONE NEAR MID FAR",
        "S L R J : REC STANCE LRJ",
        "B : BG RELEARN  C : CLEAR",
        "V : VERIFY  W : SAVE  Q : END",
    )):
        cc._font_text(frame, line, 6, 330 + index * 11, cc.LED_GRAY, 1)
    cc._font_text(frame, f"ID {int(frame_id) & 0xFFFFFFFF:08X}", 6, 376, cc.LED_GRAY, 1)
    return frame


def save(path: Path, zones: dict[str, Zone], camera_meta: dict, roi, process_size) -> tuple[bool, str]:
    payload_zones = {}
    for name, zone in zones.items():
        thresholds = zone.thresholds()
        if thresholds is None:
            continue
        payload_zones[name] = {
            "baseline": zone.baseline,
            "thresholds": thresholds,
            "sample_counts": {label: zone.count(label) for label in LABELS},
            "valid": True,
            "status": "PASS",
        }
    if not payload_zones:
        return False, "NO ZONE READY"
    payload = {
        "version": "1.0-multi",
        "date": datetime.now(timezone.utc).isoformat(),
        "camera": camera_meta,
        "ROI": {"after_rotation": None if roi is None else list(roi), "processed_size": list(process_size)},
        "zones": payload_zones,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    tmp.replace(path)
    return True, f"SAVED {'/'.join(sorted(payload_zones))}"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="近影・中距離・遠影の3距離帯をLED表示つきで実測する")
    result.add_argument("--camera", type=int, default=0)
    result.add_argument("--camera-width", type=int, default=320)
    result.add_argument("--camera-height", type=int, default=240)
    result.add_argument("--camera-fps", type=float, default=30.0)
    result.add_argument("--rotation", choices=("none", "cw", "ccw", "180"), default="ccw")
    result.add_argument("--roi", type=cc.parse_rect, default=None, help="回転後カメラ画像のROI x,y,width,height")
    result.add_argument("--exposure", type=cc.parse_exposure, default=(1.0, 312.0, 2.0), help="auto/shutter/gain")
    result.add_argument("--min-area", type=int, default=420)
    result.add_argument("--background-seconds", type=float, default=3.0)
    result.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    result.add_argument("--send", action="store_true")
    result.add_argument("--pi", action="append", default=[], metavar="HOST[:PORT]")
    result.add_argument("--chunk-size", type=int, default=1200)
    result.add_argument("--fps", type=float, default=30.0)
    result.add_argument("--seconds", type=float, default=0.0, help="0以外なら指定秒で自動終了（検証用）")
    result.add_argument(
        "--keyboard-device",
        default="auto",
        help="操作に使う入力デバイス。auto は自動検出、none で tty 入力のみ（既定 auto）",
    )
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.send and len(args.pi) != 4:
        print("error: --send のときは --pi を4個", file=sys.stderr)
        return 2

    zones = {name: Zone(name) for name in ZONES}
    # 既存の結果があれば読み、続きから測れるようにする（継続的なキャリブレのため）。
    if args.output.exists():
        try:
            previous = json.loads(args.output.read_text())
            restored = sorted(previous.get("zones", {}))
            message = f"LOADED {'/'.join(restored)}" if restored else "NO PREVIOUS ZONE"
        except (json.JSONDecodeError, OSError) as exc:
            message = f"LOAD FAILED {exc.__class__.__name__}"
    else:
        message = "NEW SESSION"

    source = cc.CameraSource(args.camera, args.camera_width, args.camera_height, args.camera_fps, args.exposure)
    detector = Detector(args.min_area)
    sender = UdpFrameSender([parse_pi(value) for value in args.pi], args.chunk_size) if args.send else None
    # 親機は画面が無くコンソールへログインできないので、既定では入力デバイスを直接読む。
    events: EventKeys | None = None
    device_note = ""
    if args.keyboard_device != "none":
        path = find_keyboard() if args.keyboard_device == "auto" else args.keyboard_device
        if path is None:
            device_note = "NO KEYBOARD FOUND"
        else:
            try:
                events = EventKeys(path)
                device_note = f"KBD {path.rsplit('/', 1)[-1]}"
            except PermissionError:
                device_note = "KBD PERM DENIED"
            except OSError as exc:
                device_note = f"KBD OPEN FAIL {exc.errno}"
    if events is None and not sys.stdin.isatty():
        message = device_note or "NO INPUT: VIEW ONLY"
    elif device_note:
        message = device_note
    print(f"入力: {device_note or 'tty'}", file=sys.stderr)

    zone_name = ZONES[0]
    collecting: str | None = None
    verify = False
    frame_id = 0
    started = time.monotonic()
    detector.learn_background(started, args.background_seconds)
    interval = 1.0 / max(1.0, args.fps)

    try:
      with RawKeys() as keys:
        while True:
            now = time.monotonic()
            if args.seconds and now - started >= args.seconds:
                break
            try:
                raw = source.read()
            except RuntimeError:
                continue
            _, gray = cc._process_frame(raw, args.rotation, args.roi)
            measurement = detector.measure(gray, now)
            learning = now < detector.background_until
            accepted = False
            if collecting and measurement is not None and not learning:
                if zones[zone_name].accepts(collecting, measurement):
                    zones[zone_name].samples[collecting].append(measurement)
                    accepted = True

            key = (events.read() if events is not None else None) or keys.read()
            if key:
                if key == "q":
                    break
                if key in ("1", "2", "3"):
                    zone_name, collecting = ZONES[int(key) - 1], None
                    message = f"ZONE {zone_name}"
                elif key == "b":
                    detector.learn_background(now, args.background_seconds)
                    collecting, message = None, "BG RELEARN"
                elif key in ("s", "l", "r", "j"):
                    label = {"s": "STANCE", "l": "LEFT", "r": "RIGHT", "j": "JUMP"}[key]
                    collecting = None if collecting == label else label
                    message = f"REC {label}" if collecting else f"STOP {label}"
                elif key == "c":
                    zones[zone_name].clear()
                    collecting, message = None, f"CLEARED {zone_name}"
                elif key == "v":
                    verify, collecting = not verify, None
                    message = "VERIFY ON" if verify else "VERIFY OFF"
                elif key == "w":
                    ok, message = save(args.output, zones, source.metadata(args.rotation),
                                       args.roi, (cc.PROCESS_WIDTH, cc.PROCESS_HEIGHT))
                    if not ok:
                        message = f"SAVE SKIPPED: {message}"

            indexed = render(zones[zone_name], collecting, measurement, learning, message, frame_id, verify, accepted)
            if int(indexed.max(initial=0)) >= cc.FC6_LIMIT:
                raise RuntimeError("FC6範囲外の画素を生成した")
            if sender:
                sender.send(frame_id, PaletteMode.FC6, indexed)
            frame_id += 1
            time.sleep(interval)
    finally:
        source.capture.release()
        if events is not None:
            events.close()
        if sender:
            sender.close()

    for name in ZONES:
        zone = zones[name]
        counts = " ".join(f"{label}={zone.count(label)}" for label in LABELS)
        print(f"{name}: {counts} -> {'OK' if zone.thresholds() else zone.blocked_reason()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
