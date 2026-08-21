#!/usr/bin/env python3
"""camera_calibrate.py のカメラ・ネットワーク非依存assertテスト。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np

from camera_calibrate import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    DISPLAY_STAGES,
    MEASUREMENT_STAGES,
    PROCESS_HEIGHT,
    PROCESS_WIDTH,
    CalibrationSession,
    CandidateDetector,
    FrameIdGenerator,
    Measurement,
    atomic_write_json,
    build_background_model,
    make_calibration_payload,
    render_led_frame,
    rotate_frame,
    rotate_point,
    write_calibration_result,
)


def _measurement(stage: str, index: int = 0) -> Measurement:
    if stage == "CENTER/STANCE" or stage == "VALIDATE":
        return Measurement(0.50 + (index % 3 - 1) * 0.002, 0.56, 0.90, 0.16, bbox=(100, 100, 40, 160), persistence=4, background_score=4.0)
    if stage == "LEFT":
        return Measurement(0.25 + (index % 3 - 1) * 0.004, 0.56, 0.90, 0.17, bbox=(45, 100, 40, 160), persistence=4, background_score=4.0)
    if stage == "RIGHT":
        return Measurement(0.75 + (index % 3 - 1) * 0.004, 0.56, 0.90, 0.17, bbox=(155, 100, 40, 160), persistence=4, background_score=4.0)
    if stage == "JUMP":
        return Measurement(0.50 + (index % 3 - 1) * 0.002, 0.28, 0.58, 0.14, bbox=(100, 50, 40, 130), persistence=4, background_score=4.0)
    raise AssertionError(stage)


def _run_session(min_samples: int, samples_per_stage: int, dt: float = 0.25) -> CalibrationSession:
    durations = {stage: 1.0 for stage in ("BACKGROUND", "CENTER/STANCE", "LEFT", "RIGHT", "JUMP", "VALIDATE")}
    session = CalibrationSession(durations, min_samples=min_samples)
    session.update(1.0, None, False, True, background_frames=12)
    for stage in MEASUREMENT_STAGES:
        for index in range(samples_per_stage):
            session.update(dt, _measurement(stage, index), True, True, background_frames=12)
    return session


def test_rendering() -> None:
    for index, stage in enumerate(DISPLAY_STAGES):
        frame = render_led_frame(stage, "STAND STILL", 0.5, stage == "PASS", stage if stage in ("PASS", "RETRY", "FAIL") else None, index)
        assert frame.shape == (CANVAS_HEIGHT, CANVAS_WIDTH), (stage, frame.shape)
        assert frame.dtype == np.uint8
        assert int(frame.min()) >= 0 and int(frame.max()) < 0x34


def test_frame_ids() -> None:
    ticks = iter((1000, 999, 999, 1001))
    generator = FrameIdGenerator(lambda: next(ticks))
    values = [generator.next() for _ in range(4)]
    assert values == [1000, 1001, 1002, 1003], values


def test_rotations() -> None:
    image = np.arange(6, dtype=np.uint8).reshape(2, 3)
    expected = {
        "none": np.array([[0, 1, 2], [3, 4, 5]], dtype=np.uint8),
        "cw": np.array([[3, 0], [4, 1], [5, 2]], dtype=np.uint8),
        "ccw": np.array([[2, 5], [1, 4], [0, 3]], dtype=np.uint8),
        "180": np.array([[5, 4, 3], [2, 1, 0]], dtype=np.uint8),
    }
    for rotation, expected_image in expected.items():
        assert np.array_equal(rotate_frame(image, rotation), expected_image), rotation
    assert rotate_point(0, 0, 3, 2, "cw") == (1, 0)
    assert rotate_point(0, 0, 3, 2, "ccw") == (0, 2)
    assert rotate_point(0, 0, 3, 2, "180") == (2, 1)


def test_background_noise_is_not_candidate() -> None:
    rng = np.random.default_rng(7)
    base = np.full((PROCESS_HEIGHT, PROCESS_WIDTH), 70, dtype=np.uint8)
    frames = [np.clip(base.astype(np.int16) + rng.integers(-2, 3, base.shape), 0, 255).astype(np.uint8) for _ in range(12)]
    model = build_background_model(frames)
    detector = CandidateDetector(model)
    for frame in frames[2:] + frames[:3]:
        detection = detector.detect(frame)
        assert not detection.candidate_valid
        assert detection.measurement is None

    session = CalibrationSession({stage: 1.0 for stage in ("BACKGROUND", "CENTER/STANCE", "LEFT", "RIGHT", "JUMP", "VALIDATE")}, 2)
    session.update(1.0, None, False, True, background_frames=12)
    assert session.stage == "CENTER/STANCE"
    for _ in range(10):
        session.update(1.0, None, False, True, background_frames=12)
    assert session.stage == "CENTER/STANCE"
    assert session.active_elapsed == 0.0


def test_valid_distribution_and_thresholds() -> dict[str, object]:
    session = _run_session(min_samples=4, samples_per_stage=4)
    assert session.status == "PASS", session.status
    result = session.analysis()
    assert result["valid"] is True
    thresholds = result["thresholds"]
    assert thresholds["left"]["delta_min"] > 0
    assert thresholds["right"]["delta_min"] > 0
    assert thresholds["jump"]["rise_y_min"] > 0
    assert thresholds["jump"]["rise_bottom_min"] > 0
    payload = make_calibration_payload(
        session,
        {
            "width": 320,
            "height": 240,
            "fps": 30.0,
            "rotation": "ccw",
            "exposure": {"requested": {"auto": 1.0, "shutter": 312.0, "gain": 2.0}, "auto_readback": 1.0, "shutter_readback": 312.0, "gain_readback": 2.0},
        },
        None,
        [(52, 32, 142, 60)],
        None,
    )
    encoded = json.dumps(payload, allow_nan=False, ensure_ascii=False)
    reloaded = json.loads(encoded)
    assert reloaded["valid"] is True
    return payload


def test_insufficient_motion_is_invalid() -> None:
    session = _run_session(min_samples=4, samples_per_stage=1, dt=1.0)
    assert session.status == "RETRY", session.status
    result = session.analysis()
    assert result["valid"] is False
    reasons = result["quality"]["reasons"]
    assert any("left_samples_insufficient" in reason for reason in reasons)
    assert any("right_samples_insufficient" in reason for reason in reasons)
    assert any("jump_samples_insufficient" in reason for reason in reasons)


def test_strict_json_and_invalid_atomic_output(valid_payload: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory(prefix="camera-calibrate-selftest-") as temporary:
        directory = Path(temporary)
        output = directory / "calibration.json"
        atomic_write_json(output, {"version": "old", "valid": True})
        old_content = output.read_text(encoding="utf-8")

        invalid = dict(valid_payload)
        invalid["valid"] = False
        invalid["status"] = "RETRY"
        invalid["quality"] = {"valid": False, "reasons": ["test_invalid"]}
        invalid_target = write_calibration_result(output, invalid)
        assert invalid_target != output
        assert output.read_text(encoding="utf-8") == old_content
        assert json.loads(invalid_target.read_text(encoding="utf-8"))["valid"] is False
        json.dumps(invalid, allow_nan=False)
        valid_target = write_calibration_result(output, valid_payload)
        assert valid_target == output
        assert json.loads(output.read_text(encoding="utf-8"))["valid"] is True


def test_reset_and_abort() -> None:
    durations = {stage: 1.0 for stage in ("BACKGROUND", "CENTER/STANCE", "LEFT", "RIGHT", "JUMP", "VALIDATE")}
    session = CalibrationSession(durations, 2)
    session.update(1.0, None, False, True, background_frames=12)
    session.update(0.5, _measurement("CENTER/STANCE"), True, True, background_frames=12)
    assert session.active_elapsed > 0
    session.reset_current_stage()
    assert session.active_elapsed == 0.0
    assert not session.samples["CENTER/STANCE"]
    session.abort("test_abort")
    assert session.status == "FAIL"
    assert session.stage == "FAIL"
    assert session.analysis()["valid"] is False


def main() -> int:
    try:
        test_rendering()
        test_frame_ids()
        test_rotations()
        test_background_noise_is_not_candidate()
        valid_payload = test_valid_distribution_and_thresholds()
        test_insufficient_motion_is_invalid()
        test_strict_json_and_invalid_atomic_output(valid_payload)
        test_reset_and_abort()
    except (AssertionError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"selftest: 1 error: {exc}")
        return 1
    print("selftest: 8 tests, 0 errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
