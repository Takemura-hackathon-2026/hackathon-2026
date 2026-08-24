#!/usr/bin/env python3
"""Camera/display-free checks for the classic-to-extra-stage prototype."""
from __future__ import annotations

import time

import numpy as np

import extra_stage_block_breaker as extra


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    module = extra.load_game_module(extra.DEFAULT_GAME)
    game = extra.ExtraStageGame(module)
    frame = game.render()
    check(frame.shape == (384, 192), "classic frame size")
    check(len(game.blocks) == 48 and game.lives == 3, "classic initial state")

    game.start_boss_transition(hide_scene=True)
    check(game.phase == "transition", "S-style transition")
    for _ in range(121):
        game.step(.04, module.GameInput(), time.monotonic())
    check(game.phase == "boss_entrance", "warning to boss entrance")
    for _ in range(91):
        game.step(.04, module.GameInput(), time.monotonic())
    check(game.phase == "boss_hp_intro", "boss entrance to HP intro")
    hp_start = np.count_nonzero(game.render()[7:17, 7:150] == module.TEXT)
    for _ in range(18):
        game.step(.04, module.GameInput(), time.monotonic())
    hp_middle = np.count_nonzero(game.render()[7:17, 7:150] == module.TEXT)
    check(hp_middle > hp_start, "HP bar grows left-to-right")

    game.phase = "boss"
    game.boss.step = lambda _dt, _controls, _now: setattr(game.boss, "boss_defeated", True)
    game.step(.04, module.GameInput(), time.monotonic())
    check(game.phase == "boss_defeat_fast", "final hit starts defeat sequence")
    for _ in range(14):
        game.step(.04, module.GameInput(), time.monotonic())
    check(game.phase == "boss_defeat_slow", "fast blink to slow blink")
    for _ in range(76):
        game.step(.04, module.GameInput(), time.monotonic())
    check(game.phase == "boss_explosion", "slow blink to explosion")
    for _ in range(41):
        game.step(.04, module.GameInput(), time.monotonic())
    check(game.phase == "boss_clear", "explosion to clear")
    for _ in range(46):
        game.step(.04, module.GameInput(), time.monotonic())
    check(game.phase == "classic" and len(game.blocks) == 48, "clear returns to stage 1")
    print("extra_stage_block_breaker_selftest: 0 errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
