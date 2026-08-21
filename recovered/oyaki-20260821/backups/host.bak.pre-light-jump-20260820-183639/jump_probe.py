#!/usr/bin/env python3
"""ジャンプ判定の実測。block_breaker と同じ前処理・同じ baseline 取得で
rise_y / rise_bottom がどこまで出るかを測り、閾値と突き合わせる。"""
import sys, time
from pathlib import Path
import numpy as np
import block_breaker as bb

calib = bb.load_calibration(Path("../camera_calibration.json"))
seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0

cam = bb.CameraController(
    calib.device, calib.capture_width, calib.capture_height, 2.0, 420, calib.roi,
    calib.jump_rise_y_min, calib.jump_rise_bottom_min,
    rotation=calib.rotation, process_size=calib.process_size,
    capture_fps=calib.capture_fps, exposure=calib.exposure,
    left_delta_min=calib.left_delta_min, right_delta_min=calib.right_delta_min,
    center_tolerance=calib.center_tolerance)
cl = cam.classifier
print(f"閾値: rise_y>={cl.jump_rise_y_min:.4f}  rise_bottom>={cl.jump_rise_bottom_min:.4f}", flush=True)
print("--- 背景学習2秒。カメラに写らないでください ---", flush=True)

t0 = time.monotonic()
rows, jumps, announced = [], 0, False
while time.monotonic() - t0 < seconds + 2.0:
    now = time.monotonic()
    state = cam.read(now)
    if cam.stage != "BACKGROUND" and not announced:
        print("--- 計測開始。カメラ正面に立ち、数回ジャンプしてください ---", flush=True)
        announced = True
    if state.jump:
        jumps += 1
        print(f"  JUMP検出 #{jumps}", flush=True)
    base, last = cl.baseline, cl.last
    if base is not None and last is not None:
        rows.append((base.y - last.y, base.bottom - last.bottom, last.area))
    time.sleep(0.015)
cam.close()

if not rows:
    print("計測なし: 人物が一度も検出されていない（baseline未確立）")
    raise SystemExit(1)
a = np.asarray(rows)
print(f"\n計測 {len(a)} フレーム / JUMP検出 {jumps} 回")
for i, (name, thr) in enumerate((("rise_y", cl.jump_rise_y_min), ("rise_bottom", cl.jump_rise_bottom_min))):
    col = a[:, i]
    over = int((col >= thr).sum())
    print(f"  {name:12s} max={col.max():+.4f} p95={np.percentile(col,95):+.4f} "
          f"median={np.median(col):+.4f} 閾値{thr:.4f}超え={over}フレーム({over/len(col)*100:.1f}%)")
both = int(((a[:,0] >= cl.jump_rise_y_min) & (a[:,1] >= cl.jump_rise_bottom_min)).sum())
print(f"  両方同時に満たしたフレーム: {both}")
print(f"  area median={np.median(a[:,2]):.4f} (baseline area={cl.baseline.area:.4f})")
print(f"  baseline: y={cl.baseline.y:.4f} bottom={cl.baseline.bottom:.4f}")
