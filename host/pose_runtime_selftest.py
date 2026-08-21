#!/usr/bin/env python3
"""姿勢ゲーム実行アダプタの校正値検証と判定ロジックの最小テスト。"""
from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path

from pose_input import PoseMeasurement
from pose_runtime import (
    POSE_COORDINATE_SPACE,
    POSE_DIRECTION_CONVENTION,
    PoseInputClassifier,
    load_pose_calibration,
)


def main() -> int:
    errors: list[str] = []
    base = PoseMeasurement(.5, .5, .9, .2, .25, 1.0, 1.0)
    payload = {
        "status": "PASS",
        "valid": True,
        "coordinate_space": POSE_COORDINATE_SPACE,
        "direction_convention": POSE_DIRECTION_CONVENTION,
        "camera": {"device": 0, "rotation": "none", "width": 640, "height": 480, "exposure": [1, 2, 3]},
        "baseline": {"x": base.x, "y": base.y, "bottom": base.bottom, "area": base.area, "scale": base.scale},
        "thresholds": {
            "units": "torso_lengths",
            "center_tolerance": {"x": .03, "y": .03, "bottom": .03},
            "left": {"delta_min": .10},
            "right": {"delta_min": .10},
            "jump": {"rise_y_min": .10, "rise_bottom_min": .10},
        },
        "quality": {"valid": True, "reasons": []},
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "pose_calibration.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        calibration = load_pose_calibration(path)
        classifier = PoseInputClassifier(calibration)

        left = replace(base, x=base.x + .11 * base.scale)
        for now in (0.0, .05, .13):
            state = classifier.update(left, now)
        if state.lateral != -1:
            errors.append("本人基準のLEFTを確定しない")

        classifier.reset()
        right = replace(base, x=base.x - .11 * base.scale)
        for now in (0.0, .05, .13):
            state = classifier.update(right, now)
        if state.lateral != 1:
            errors.append("本人基準のRIGHTを確定しない")

        classifier.reset()
        jump = replace(base, y=base.y - .11 * base.scale, bottom=base.bottom - .11 * base.scale)
        classifier.update(jump, 1.0)
        if not classifier.update(jump, 1.05).jump:
            errors.append("胴長単位のJUMPを確定しない")

        payload["status"] = "FAIL"
        payload["valid"] = False
        path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            load_pose_calibration(path)
        except ValueError:
            pass
        else:
            errors.append("FAIL校正を拒否しない")

    for error in errors:
        print(f"ERROR: {error}")
    print(f"pose-runtime selftest: {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
