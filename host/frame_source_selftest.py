#!/usr/bin/env python3
"""深度フレーム取得のセルフテスト。実センサーなしで擬似ヘルパーを使う。

処理が取得より遅いとき、古いフレームを順に処理せず最新フレームだけを返すことを
確認する。ここが崩れると、そのまま人物位置とバーの追従遅れになる。
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from frame_source import StructureSensorSource  # noqa: E402


FAKE_HELPER = '''#!/usr/bin/env python3
"""frame_idを全画素へ埋めた擬似深度フレームを一定レートで書く。"""
import struct, sys, time

width, height, fps, decimate = 64, 48, 100.0, 1
arguments = sys.argv[1:]
for index, argument in enumerate(arguments):
    if argument == "--width":
        width = int(arguments[index + 1])
    elif argument == "--height":
        height = int(arguments[index + 1])
    elif argument == "--fps":
        fps = float(arguments[index + 1])
    elif argument == "--decimate":
        decimate = int(arguments[index + 1])
width, height = width // decimate, height // decimate
stream = sys.stdout.buffer
period = 1.0 / fps
deadline = time.monotonic()
for frame_id in range(2000):
    payload = struct.pack("<H", frame_id % 65535) * (width * height)
    stream.write(struct.pack("<4sIIII", b"SDP1", frame_id, width, height, len(payload)))
    stream.write(payload)
    stream.flush()
    deadline += period
    delay = deadline - time.monotonic()
    if delay > 0:
        time.sleep(delay)
'''


def main() -> int:
    errors: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        helper = Path(directory) / "fake_depth_capture.py"
        helper.write_text(FAKE_HELPER, encoding="utf-8")
        helper.chmod(helper.stat().st_mode | stat.S_IXUSR)

        # 取得100fpsに対し処理は約10fps。滞留があればframe_idは1ずつしか進まない。
        source = StructureSensorSource(64, 48, 100.0, helper=helper)
        frame_ids: list[int] = []
        try:
            for _ in range(6):
                image = source.read()
                if image.shape != (48, 64) or image.dtype != "uint16":
                    errors.append(f"深度フレームの形状・型が不正: {image.shape} {image.dtype}")
                    break
                frame_ids.append(int(image[0, 0]))
                time.sleep(.10)
        except RuntimeError as error:
            errors.append(f"擬似ヘルパーからフレームを取得できない: {error}")
        finally:
            source.close()
        gaps = [later - earlier for earlier, later in zip(frame_ids, frame_ids[1:])]
        if len(gaps) < 5:
            errors.append(f"取得フレーム数が足りない: {frame_ids}")
        elif min(gaps) < 5:
            errors.append(f"古いフレームを順に返している（最新破棄が効いていない）: {frame_ids}")
        if not errors and source.dropped <= 0:
            errors.append("滞留フレームを捨てた数を数えていない")

        # --decimate を渡したとき、間引き後の形状がそのまま届くこと。
        decimated = StructureSensorSource(64, 48, 100.0, helper=helper, decimate=2)
        try:
            image = decimated.read()
            if image.shape != (24, 32):
                errors.append(f"間引き後の深度フレーム形状が不正: {image.shape}")
            if decimated.metadata("none")["decimate"] != 2:
                errors.append("metadataに間引き幅が出ない")
        except RuntimeError as error:
            errors.append(f"間引き指定でフレームを取得できない: {error}")
        finally:
            decimated.close()
        for invalid in (0, 17):
            try:
                StructureSensorSource(64, 48, 100.0, helper=helper, decimate=invalid)
            except ValueError:
                pass
            else:
                errors.append(f"不正な間引き幅を拒否しない: {invalid}")

        # ヘルパーが起動できない場合は例外で分かること。
        missing = Path(directory) / "does-not-exist"
        try:
            StructureSensorSource(64, 48, 30.0, helper=missing)
        except RuntimeError:
            pass
        else:
            errors.append("存在しない取得ヘルパーを拒否しない")

        # ヘルパーが即終了した場合、read()が待ち続けずに失敗すること。
        dead = Path(directory) / "dead_helper.py"
        dead.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        dead.chmod(dead.stat().st_mode | stat.S_IXUSR)
        dead_source = StructureSensorSource(64, 48, 30.0, helper=dead)
        try:
            dead_source.read(timeout=2.0)
        except RuntimeError:
            pass
        else:
            errors.append("終了した取得ヘルパーで失敗しない")
        finally:
            dead_source.close()

    for error in errors:
        print(f"ERROR: {error}")
    print(f"frame-source selftest: {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
