#!/usr/bin/env python3
"""PC側校正UIの通信・配線・設定変換の自己テスト。"""
from __future__ import annotations

import sys
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from panel_calibration_ui import (  # noqa: E402
    CHAIN_LENGTH,
    COLOR_INDEX,
    PANEL_COLS,
    PANEL_ROWS,
    PARALLEL,
    TARGETS,
    gains_from_hue,
    format_gain_config,
    fc6_rgb,
    color_label,
    hue_from_gains,
    identity_brightness_table,
    identity_gain_table,
    iter_panel_refs,
    make_pattern_frame,
    parse_calibration_config,
    parse_brightness_config,
    parse_gain_config,
)


def main() -> int:
    errors: list[str] = []
    refs = iter_panel_refs()
    if len(TARGETS) != 3:
        errors.append(f"表示Pi数が3ではない: {len(TARGETS)}")
    expected_targets = {"pi1": 0, "pi2": 2, "pi4": 1}
    actual_targets = {target.key: target.target_id for target in TARGETS}
    if actual_targets != expected_targets:
        errors.append(f"実機target IDが不正: {actual_targets}")
    expected_count = 3 * PANEL_ROWS * PANEL_COLS
    if len(refs) != expected_count:
        errors.append(f"パネル数が72ではない: {len(refs)}")
    if len({ref.key for ref in refs}) != expected_count:
        errors.append("パネル識別子が重複している")

    for target in TARGETS:
        target_refs = [ref for ref in refs if ref.display == target]
        slots = {(ref.lane, ref.config_chain) for ref in target_refs}
        expected_slots = {
            (lane, chain)
            for lane in range(PARALLEL)
            for chain in range(CHAIN_LENGTH)
        }
        if len(target_refs) != 24 or slots != expected_slots:
            errors.append(f"{target.key}: 24枚と3x8設定スロットの対応が不正")

    first = refs[0]
    if (first.row, first.column, first.lane, first.connector_chain, first.config_chain) != (
        0,
        0,
        0,
        5,
        2,
    ):
        errors.append("先頭パネルの実機配線マップが不正")
    lower_left = next(
        ref for ref in refs if ref.display == TARGETS[0] and ref.row == 2 and ref.column == 0
    )
    if (lower_left.lane, lower_left.connector_chain, lower_left.config_chain) != (2, 6, 1):
        errors.append("下段パネルの実機配線マップが不正")

    table = identity_gain_table()
    table[(2, 1)] = (0.83, 1.07, 1.21)
    if parse_gain_config(format_gain_config(table)) != table:
        errors.append("設定ファイルのformat/parse往復が一致しない")
    brightness = identity_brightness_table()
    brightness[(2, 1)] = 0.72
    formatted = format_gain_config(table, brightness)
    parsed_gains, parsed_brightness = parse_calibration_config(formatted)
    if parsed_gains != table or parsed_brightness != brightness:
        errors.append("RGB・輝度設定ファイルのformat/parse往復が一致しない")
    if parse_brightness_config(format_gain_config(table)) != identity_brightness_table():
        errors.append("旧5列設定の輝度を1.00倍として読めない")

    if gains_from_hue(0.0, 0.0) != (1.0, 1.0, 1.0):
        errors.append("色相環の中心が恒等補正になっていない")
    if gains_from_hue(0.0, 1.0) != (2.0, 0.0, 0.0):
        errors.append("色相環の外周RGBゲイン変換が不正")
    green_gains = gains_from_hue(1.0 / 3.0, 0.5)
    if green_gains != (0.5, 1.5, 0.5):
        errors.append("色相環の中間補正量変換が不正")
    restored_hue, restored_strength = hue_from_gains(green_gains)
    if abs(restored_hue - 1.0 / 3.0) > 1.0 / 360.0 or abs(restored_strength - 0.5) > 0.01:
        errors.append("RGBゲインから色相環位置への同期が不正")
    if len(fc6_rgb(COLOR_INDEX["white"])) != 3:
        errors.append("FC6色のRGB値が3要素ではない")
    if not color_label(COLOR_INDEX["white"]).startswith("FC6 0x33"):
        errors.append("FC6色ラベルの生成が不正")

    all_frame = make_pattern_frame(None, "red")
    if len(all_frame) != 192 * 128 or set(all_frame) != {COLOR_INDEX["red"]}:
        errors.append("全72枚単色フレームが全面同色になっていない")
    selected_frame = make_pattern_frame(first, "blue")
    selected_color = COLOR_INDEX["blue"]
    background_color = COLOR_INDEX["black"]
    for y in range(128):
        for x in range(192):
            expected = (
                selected_color
                if first.row * 32 <= y < (first.row + 1) * 32
                and first.column * 32 <= x < (first.column + 1) * 32
                else background_color
            )
            if selected_frame[y * 192 + x] != expected:
                errors.append("1枚選択フレームの領域が不正")
                break
        if errors and errors[-1] == "1枚選択フレームの領域が不正":
            break

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print(f"{len(errors)} errors")
        return 1
    print("0 errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
