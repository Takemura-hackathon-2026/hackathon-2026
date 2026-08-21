#!/usr/bin/env python3
"""ジャンプ判定専用CLIのカメラ非依存セルフテスト。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jump_detector import build_parser, parse_roi  # noqa: E402


def main() -> int:
    errors: list[str] = []
    args = build_parser().parse_args([])
    if args.jump_rise_y_min != 0.05 or args.jump_rise_bottom_min != 0.04:
        errors.append("既定のジャンプ閾値が不正")
    args = build_parser().parse_args(["--jump-rise-y-min", "0.03", "--jump-rise-bottom-min", "0.025"])
    if args.jump_rise_y_min != 0.03 or args.jump_rise_bottom_min != 0.025:
        errors.append("ジャンプ閾値CLIを解釈しない")
    if parse_roi("1,2,30,40") != (1, 2, 30, 40):
        errors.append("ROIを解釈しない")
    try:
        parse_roi("1,2,-1,40")
    except argparse.ArgumentTypeError:
        pass
    else:
        errors.append("不正ROIを拒否しない")
    for error in errors:
        print(f"ERROR: {error}")
    print(f"jump-detector selftest: {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
