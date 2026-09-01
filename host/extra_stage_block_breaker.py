#!/usr/bin/env python3
"""Keyboard prototype: classic block breaker followed by an extra boss stage."""
from __future__ import annotations

import argparse
import importlib.util
import math
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


DEFAULT_GAME = Path(__file__).resolve().with_name("block_breaker.py")


@dataclass
class Block:
    x: float
    y: float
    width: float
    height: float
    color: int


def load_game_module(path: Path):
    if not path.is_file():
        raise RuntimeError(f"既存ゲームが見つかりません: {path}")
    host = str(path.parent)
    if host not in sys.path:
        sys.path.insert(0, host)
    spec = importlib.util.spec_from_file_location("hackathon_block_breaker", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"既存ゲームを読み込めません: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ExtraStageGame:
    """通常ブロック崩し1面のあと、既存のボス戦へ接続する。"""

    stage_clear_seconds = 2.0
    warning_interval = 1.4
    warning_blinks = 2
    boss_entrance_seconds = 3.6
    boss_hp_intro_seconds = 1.5
    defeat_fast_seconds = .48
    defeat_slow_interval = .5
    defeat_slow_blinks = 3
    defeat_explosion_seconds = 1.6
    defeat_clear_seconds = 1.8
    # 各区間の冒頭で赤くなり、1.4秒かけてステージへフェードアウトする。
    transition_seconds = stage_clear_seconds + warning_interval * warning_blinks

    def __init__(self, game_module) -> None:
        self.m = game_module
        self.normal_paddle_width = 42.0
        self.paddle_width, self.paddle_height, self.paddle_y = self.normal_paddle_width, 6.0, 350.0
        self.ball_radius, self.paddle_speed, self.initial_speed = 3.0, 175.0, 175.0
        self.cheat_wide_paddle = False
        self.boss = None
        self.phase = "classic"
        self.transition_remaining = 0.0
        self.transition_hide_scene = False
        self.boss_entrance_remaining = 0.0
        self.boss_hp_intro_remaining = 0.0
        self.defeat_remaining = 0.0
        self.score = 0
        self.lives = 3
        self.paddle_x = 0.0
        self.ball = self.m.Ball(0.0, 0.0)
        self.blocks = []
        self.serving = True
        self.game_over_until = 0.0
        self.reset()

    def reset(self) -> None:
        self.phase, self.boss = "classic", None
        self.cheat_wide_paddle = False
        self.paddle_width = self.normal_paddle_width
        self.transition_remaining = 0.0
        self.transition_hide_scene = False
        self.boss_entrance_remaining = 0.0
        self.boss_hp_intro_remaining = 0.0
        self.defeat_remaining = 0.0
        self.score, self.lives = 0, 3
        self.paddle_x = (self.m.CANVAS_WIDTH - self.paddle_width) / 2
        self.serving = True
        self.game_over_until = 0.0
        self.ball = self.m.Ball(0.0, 0.0)
        self._serve_ball()
        self.blocks = self._make_blocks()

    def _make_blocks(self):
        columns, rows, margin, gap = 8, 6, 8, 2
        width = (self.m.CANVAS_WIDTH - margin * 2 - gap * (columns - 1)) / columns
        colors = (0x00, 0x05, 0x0A, 0x0E, 0x12, 0x16, 0x1E, 0x22, 0x29, 0x2D)
        return [
            Block(margin + col * (width + gap), 48 + row * 14, width, 12, colors[(row + col) % len(colors)])
            for row in range(rows) for col in range(columns)
        ]

    def _serve_ball(self) -> None:
        self.ball.x = self.paddle_x + self.paddle_width / 2
        self.ball.y = self.paddle_y - self.ball_radius - 1
        self.ball.vx = self.ball.vy = 0.0

    def _launch(self) -> None:
        self.serving = False
        self.ball.vx, self.ball.vy = self.initial_speed * .52, -self.initial_speed * .86

    def _hit_block(self, block) -> bool:
        near_x = min(max(self.ball.x, block.x), block.x + block.width)
        near_y = min(max(self.ball.y, block.y), block.y + block.height)
        dx, dy = self.ball.x - near_x, self.ball.y - near_y
        if dx * dx + dy * dy > self.ball_radius ** 2:
            return False
        if abs(dx) > abs(dy):
            self.ball.vx = -self.ball.vx
        else:
            self.ball.vy = -self.ball.vy
        return True

    def _lose_ball(self, now: float) -> None:
        self.lives -= 1
        if self.lives <= 0:
            self.game_over_until = now + 1.8
        self.serving = True
        self._serve_ball()

    def start_boss_transition(self, hide_scene: bool = False) -> None:
        if self.phase == "classic":
            self.phase = "transition"
            self.transition_remaining = self.transition_seconds
            self.transition_hide_scene = hide_scene

    def _start_boss_entrance(self) -> None:
        # 通常面はこのラッパー側が持つため、既存ゲームからはボス戦だけを使う。
        self.boss = self.m.BlockBreaker(start_phase="boss")
        if self.cheat_wide_paddle:
            self.boss.paddle_width = float(self.m.CANVAS_WIDTH)
            self.boss.paddle_x = 0.0
        self.boss_target_y = float(self.boss.boss_y)
        self.boss.boss_y = -float(self.boss.boss_height)
        self.boss_entrance_remaining = self.boss_entrance_seconds
        self.phase = "boss_entrance"

    def toggle_wide_paddle(self) -> None:
        """Cキー用。UIを出さず、パドルを通常幅と画面全幅で切り替える。"""
        self.cheat_wide_paddle = not self.cheat_wide_paddle
        width = float(self.m.CANVAS_WIDTH) if self.cheat_wide_paddle else self.normal_paddle_width
        if self.phase in ("boss", "boss_hp_intro") and self.boss is not None:
            center = self.boss.paddle_x + self.boss.paddle_width / 2
            self.boss.paddle_width = width
            self.boss.paddle_x = min(max(0.0, center - width / 2), self.m.CANVAS_WIDTH - width)
        else:
            center = self.paddle_x + self.paddle_width / 2
            self.paddle_width = width
            self.paddle_x = min(max(0.0, center - width / 2), self.m.CANVAS_WIDTH - width)

    def step(self, dt: float, controls, now: float) -> None:
        dt = min(.04, max(0.0, dt))
        if self.phase == "boss":
            # ボス撃破・全滅の結果表示が終わったら、ボス戦だけを再生成せず
            # ラッパー全体を通常ステージ1へ戻す。
            if self.boss.boss_defeated and self.boss.clear_remaining <= dt:
                self.reset()
                return
            if self.boss.game_over_until and now >= self.boss.game_over_until:
                self.reset()
                return
            was_defeated = self.boss.boss_defeated
            self.boss.step(dt, controls, now)
            if not was_defeated and self.boss.boss_defeated:
                # 最後の一撃では通常の2回被弾点滅を見せず、専用撃破演出へ入る。
                self.boss.damage_effect_remaining = 0.0
                self.defeat_remaining = self.defeat_fast_seconds
                self.phase = "boss_defeat_fast"
            return
        if self.phase == "boss_hp_intro":
            # HPバーの出現中でもJUMP TO LAUNCHを表示し、操作を待たせない。
            self.boss.step(dt, controls, now)
            self.boss_hp_intro_remaining = max(0.0, self.boss_hp_intro_remaining - dt)
            if self.boss_hp_intro_remaining == 0.0:
                self.phase = "boss"
            return
        if self.phase in ("boss_defeat_fast", "boss_defeat_slow", "boss_explosion", "boss_clear"):
            self.defeat_remaining = max(0.0, self.defeat_remaining - dt)
            if self.defeat_remaining == 0.0:
                if self.phase == "boss_defeat_fast":
                    self.phase = "boss_defeat_slow"
                    self.defeat_remaining = self.defeat_slow_interval * self.defeat_slow_blinks * 2
                elif self.phase == "boss_defeat_slow":
                    self.phase = "boss_explosion"
                    self.defeat_remaining = self.defeat_explosion_seconds
                elif self.phase == "boss_explosion":
                    self.phase = "boss_clear"
                    self.defeat_remaining = self.defeat_clear_seconds
                else:
                    self.reset()
            return
        if self.phase == "boss_entrance":
            self.boss_entrance_remaining = max(0.0, self.boss_entrance_remaining - dt)
            progress = 1.0 - self.boss_entrance_remaining / self.boss_entrance_seconds
            eased = 1.0 - (1.0 - progress) ** 3
            start_y = -float(self.boss.boss_height)
            self.boss.boss_y = start_y + (self.boss_target_y - start_y) * eased
            if self.boss_entrance_remaining == 0.0:
                self.boss.boss_y = self.boss_target_y
                self.boss_hp_intro_remaining = self.boss_hp_intro_seconds
                self.phase = "boss_hp_intro"
            return
        if self.phase == "transition":
            self.transition_remaining -= dt
            if self.transition_remaining <= 0:
                self._start_boss_entrance()
            return
        if self.game_over_until:
            if now >= self.game_over_until:
                self.reset()
            return
        limit = self.m.CANVAS_WIDTH - self.paddle_width
        self.paddle_x = min(max(0.0, self.paddle_x + controls.lateral * self.paddle_speed * dt), limit)
        if self.serving:
            self._serve_ball()
            if controls.launch:
                self._launch()
            return
        steps = max(1, min(8, math.ceil(math.hypot(self.ball.vx * dt, self.ball.vy * dt) / 2)))
        for _ in range(steps):
            ball = self.ball
            ball.x += ball.vx * dt / steps
            ball.y += ball.vy * dt / steps
            if ball.x - self.ball_radius < 0 or ball.x + self.ball_radius >= self.m.CANVAS_WIDTH:
                ball.x = min(max(self.ball_radius, ball.x), self.m.CANVAS_WIDTH - self.ball_radius - 1)
                ball.vx = -ball.vx
            if ball.y - self.ball_radius < 26:
                ball.y, ball.vy = 26 + self.ball_radius, abs(ball.vy)
            if (ball.vy > 0 and ball.y + self.ball_radius >= self.paddle_y
                    and ball.y - self.ball_radius <= self.paddle_y + self.paddle_height
                    and self.paddle_x - self.ball_radius <= ball.x <= self.paddle_x + self.paddle_width + self.ball_radius):
                ball.y = self.paddle_y - self.ball_radius - 1
                hit = (ball.x - (self.paddle_x + self.paddle_width / 2)) / (self.paddle_width / 2)
                speed = min(300, math.hypot(ball.vx, ball.vy) * 1.015)
                ball.vx = speed * hit * .92
                ball.vy = -max(80, math.sqrt(max(1, speed * speed - ball.vx * ball.vx)))
            for index, block in enumerate(self.blocks):
                if self._hit_block(block):
                    del self.blocks[index]
                    self.score += 10
                    break
            if not self.blocks:
                self.start_boss_transition()
                return
            if ball.y - self.ball_radius > self.m.CANVAS_HEIGHT:
                self._lose_ball(now)
                return

    def _text(self, frame, text, origin, color, scale) -> None:
        mask = np.zeros(frame.shape, np.uint8)
        cv2.putText(mask, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, 255, 1, cv2.LINE_AA)
        frame[mask > 96] = color

    def _render_boss_entrance(self):
        frame = np.full((self.m.CANVAS_HEIGHT, self.m.CANVAS_WIDTH), self.m.SKY, np.uint8)
        elapsed = self.boss_entrance_seconds - self.boss_entrance_remaining
        progress = min(1.0, max(0.0, elapsed / self.boss_entrance_seconds))
        phase = int(elapsed / .055)
        shake_scale = 1.0
        shake_x = round((-2, 1, -1, 2, -1, 1)[phase % 6] * shake_scale)
        shake_y = round((1, -1, 0, 1, 0, -1)[phase % 6] * shake_scale)
        for index in range(30):
            star_y = min(self.m.CANVAS_HEIGHT - 1, max(0, 30 + (index * 71 + 13) % 300 + shake_y))
            star_x = (index * 47 + 19 + shake_x) % self.m.CANVAS_WIDTH
            frame[star_y, star_x] = self.m.SKY_DOT
        # 砂埃を先に描き、その上へボスを重ねることで軌跡を背面へ置く。
        for index in range(30):
            age = .10 + (index % 10) * .075
            past_elapsed = max(0.0, elapsed - age)
            past_progress = min(1.0, past_elapsed / self.boss_entrance_seconds)
            past_eased = 1.0 - (1.0 - past_progress) ** 3
            start_y = -float(self.boss.boss_height)
            past_y = start_y + (self.boss_target_y - start_y) * past_eased
            seed = index * 41
            edge_x = self.boss.boss_x + (4 if index % 2 == 0 else self.boss.boss_width - 4)
            px = int(edge_x + ((seed * 7) % 17) - 8)
            py = int(past_y + 8 + (seed * 11) % max(12, self.boss.boss_height - 12))
            if 0 <= px < self.m.CANVAS_WIDTH and 24 <= py < self.m.CANVAS_HEIGHT:
                color = 0x0D if index % 3 else self.m.DIM
                cv2.circle(frame, (px, py), 1 + seed % 4, color, -1, lineType=cv2.LINE_8)
        x = int(round(self.boss.boss_x)) + shake_x
        y = int(round(self.boss.boss_y)) + shake_y
        x = min(max(0, x), self.m.CANVAS_WIDTH - self.boss.boss_width)
        dst_y0, dst_y1 = max(0, y), min(self.m.CANVAS_HEIGHT, y + self.boss.boss_height)
        if dst_y0 < dst_y1:
            src_y0 = dst_y0 - y
            src_y1 = src_y0 + dst_y1 - dst_y0
            region = frame[dst_y0:dst_y1, x:x + self.boss.boss_width]
            mask = self.boss.boss_mask[src_y0:src_y1]
            sprite = self.boss.boss_sprite[src_y0:src_y1]
            region[mask] = sprite[mask]
        return frame

    def _render_boss_hp_intro(self):
        frame = self.boss.render("READY")
        progress = 1.0 - self.boss_hp_intro_remaining / self.boss_hp_intro_seconds
        hp_x, hp_y, hp_width, hp_height = 7, 7, 143, 10
        # 既存の完成済みHPバーだけを消し、左端から枠と中身を同時に伸ばす。
        frame[hp_y:hp_y + hp_height, hp_x:hp_x + hp_width] = self.m.SKY
        visible = max(1, round(hp_width * min(1.0, max(0.0, progress))))
        frame[hp_y:hp_y + hp_height, hp_x:hp_x + visible] = self.m.TEXT
        if visible > 4:
            frame[hp_y + 2:hp_y + hp_height - 2, hp_x + 2:hp_x + visible - 2] = 0x16
        return frame

    def _render_defeat_scene(self, flash: bool = False):
        """既存BOSS DOWN文字を消した撃破演出用のボス画面。"""
        frame = self.boss.render("READY")
        frame[190:275, :] = self.m.SKY
        x, y = int(round(self.boss.boss_x)), int(round(self.boss.boss_y))
        region = frame[y:y + self.boss.boss_height, x:x + self.boss.boss_width]
        if flash:
            region[self.boss.boss_mask] = self.m.FC6_WHITE
        return frame

    def _render_boss_explosion(self):
        frame = np.full((self.m.CANVAS_HEIGHT, self.m.CANVAS_WIDTH), self.m.SKY, np.uint8)
        for index in range(30):
            frame[30 + (index * 71 + 13) % 300, (index * 47 + 19) % self.m.CANVAS_WIDTH] = self.m.SKY_DOT
        progress = 1.0 - self.defeat_remaining / self.defeat_explosion_seconds
        center_x = self.boss.boss_width / 2
        center_y = self.boss.boss_height / 2
        world_center_x = int(self.boss.boss_x + center_x)
        world_center_y = int(self.boss.boss_y + center_y)
        # 爆発直後の白フラッシュと、赤・黄の二重衝撃波。
        if progress < .09:
            frame.fill(self.m.FC6_WHITE)
        ring_radius = max(1, int(progress * 115))
        cv2.circle(frame, (world_center_x, world_center_y), ring_radius, 0x06, 3, lineType=cv2.LINE_8)
        if ring_radius > 9:
            cv2.circle(frame, (world_center_x, world_center_y), ring_radius - 9, 0x0E, 2, lineType=cv2.LINE_8)

        # 小さな火花を破片より速く外周へ飛ばす。
        for index in range(56):
            angle = index * 2.399963 + (index % 5) * .07
            speed = 75 + (index * 29) % 105
            distance = speed * progress
            px = int(world_center_x + math.cos(angle) * distance)
            py = int(world_center_y + math.sin(angle) * distance + 38 * progress * progress)
            if 0 <= px < self.m.CANVAS_WIDTH and 24 <= py < self.m.CANVAS_HEIGHT:
                color = self.m.FC6_WHITE if index % 7 == 0 else 0x0E if index % 3 else 0x06
                cv2.circle(frame, (px, py), 1 + (index % 3 == 0), int(color), -1, lineType=cv2.LINE_8)

        tile = 4
        for sy in range(0, self.boss.boss_height, tile):
            for sx in range(0, self.boss.boss_width, tile):
                source_mask = self.boss.boss_mask[sy:sy + tile, sx:sx + tile]
                if not np.any(source_mask):
                    continue
                dx, dy = sx + tile / 2 - center_x, sy + tile / 2 - center_y
                length = max(1.0, math.hypot(dx, dy))
                speed = 72.0 + ((sx * 17 + sy * 11) % 82)
                px = int(round(self.boss.boss_x + sx + dx / length * speed * progress))
                py = int(round(self.boss.boss_y + sy + dy / length * speed * progress + 46 * progress * progress))
                h, w = source_mask.shape
                dst_x0, dst_y0 = max(0, px), max(24, py)
                dst_x1, dst_y1 = min(self.m.CANVAS_WIDTH, px + w), min(self.m.CANVAS_HEIGHT, py + h)
                if dst_x0 >= dst_x1 or dst_y0 >= dst_y1:
                    continue
                src_x0, src_y0 = dst_x0 - px, dst_y0 - py
                src_x1, src_y1 = src_x0 + dst_x1 - dst_x0, src_y0 + dst_y1 - dst_y0
                mask = source_mask[src_y0:src_y1, src_x0:src_x1]
                sprite = self.boss.boss_sprite[sy:sy + tile, sx:sx + tile][src_y0:src_y1, src_x0:src_x1]
                region = frame[dst_y0:dst_y1, dst_x0:dst_x1]
                region[mask] = sprite[mask]
        return frame

    def _render_boss_clear(self):
        frame = np.full((self.m.CANVAS_HEIGHT, self.m.CANVAS_WIDTH), self.m.SKY, np.uint8)
        self._text(frame, "BOSS DOWN", (39, 228), 0x12, .68)
        return frame

    def _red_fade(self, frame, opacity: float) -> None:
        """粒状の疑似透過を使わず、画面全体を均一な赤系色でフェードする。"""
        opacity = min(.88, max(0.0, opacity))
        if opacity > .68:
            frame.fill(0x06)
        elif opacity > .44:
            frame.fill(0x05)
        elif opacity > .20:
            frame.fill(0x04)
        elif opacity > .04:
            frame.fill(0x00)

    def _render_warning(self, frame, cycle_elapsed: float) -> None:
        """赤い警告帯・走査線・脈動を組み合わせたボス前WARNING。"""
        progress = min(1.0, max(0.0, cycle_elapsed / self.warning_interval))
        opacity = (1.0 - progress) * .88
        self._red_fade(frame, opacity)
        if opacity <= .04:
            return

        # 外周は開始時に強く光り、フェードに合わせて細くなる。
        border = 3 if opacity > .58 else 2 if opacity > .25 else 1
        frame[:border, :] = 0x06
        frame[-border:, :] = 0x06
        frame[:, :border] = 0x06
        frame[:, -border:] = 0x06

        # 中央の暗赤色警告帯。上下の斜線は危険領域が迫る印象を作る。
        band_top, band_bottom = 166, 226
        frame[band_top:band_bottom, :] = 0x00
        stripe_color = 0x06 if opacity > .38 else 0x04
        for x in range(-24, self.m.CANVAS_WIDTH + 24, 18):
            cv2.line(frame, (x, band_top - 7), (x + 13, band_top + 3), stripe_color, 3, cv2.LINE_8)
            cv2.line(frame, (x, band_bottom - 3), (x + 13, band_bottom + 7), stripe_color, 3, cv2.LINE_8)

        text_color = self.m.TEXT if opacity > .22 else 0x32
        self._text(frame, "WARNING", (39, 204), text_color, .78)
        self._text(frame, ">>>", (7, 204), stripe_color, .42)
        self._text(frame, "<<<", (157, 204), stripe_color, .42)

    def render(self):
        if self.phase == "boss":
            return self.boss.render("READY")
        if self.phase == "boss_hp_intro":
            return self._render_boss_hp_intro()
        if self.phase == "boss_defeat_fast":
            elapsed = self.defeat_fast_seconds - self.defeat_remaining
            flash = int(elapsed / (self.defeat_fast_seconds / 6)) % 2 == 0
            return self._render_defeat_scene(flash)
        if self.phase == "boss_defeat_slow":
            elapsed = self.defeat_slow_interval * self.defeat_slow_blinks * 2 - self.defeat_remaining
            flash = int(elapsed / self.defeat_slow_interval) % 2 == 0
            return self._render_defeat_scene(flash)
        if self.phase == "boss_explosion":
            return self._render_boss_explosion()
        if self.phase == "boss_clear":
            return self._render_boss_clear()
        if self.phase == "boss_entrance":
            return self._render_boss_entrance()
        frame = np.full((self.m.CANVAS_HEIGHT, self.m.CANVAS_WIDTH), self.m.SKY, np.uint8)
        for index in range(30):
            frame[30 + (index * 71 + 13) % 300, (index * 47 + 19) % self.m.CANVAS_WIDTH] = self.m.SKY_DOT
        hide_scene = self.phase == "transition" and self.transition_hide_scene
        if not hide_scene:
            frame[22:24, :] = self.m.DIM
            self._text(frame, f"SCORE {self.score:05d}", (6, 17), self.m.TEXT, .38)
            # ボス戦と同じ位置・形の白丸で残機を表示する。
            for life in range(self.lives):
                cv2.circle(frame, (164 + life * 11, 12), 3, int(self.m.TEXT), -1, lineType=cv2.LINE_8)
            for block in self.blocks:
                x0, y0 = int(round(block.x)), int(round(block.y))
                x1, y1 = int(round(block.x + block.width)), int(round(block.y + block.height))
                frame[y0:y1, x0:x1] = block.color
                frame[y0:y0 + 2, x0:x1] = self.m.TEXT
            x = int(round(self.paddle_x))
            frame[int(self.paddle_y):int(self.paddle_y + self.paddle_height), x:x + int(self.paddle_width)] = self.m.PADDLE
            frame[int(self.paddle_y):int(self.paddle_y + 2), x:x + int(self.paddle_width)] = self.m.PADDLE_EDGE
            cv2.circle(frame, (round(self.ball.x), round(self.ball.y)), round(self.ball_radius), int(self.m.BALL), -1)
        if self.phase == "transition":
            elapsed = self.transition_seconds - self.transition_remaining
            if elapsed < self.stage_clear_seconds:
                self._text(frame, "STAGE CLEAR", (35, 198), 0x12, .68)
            else:
                warning_elapsed = elapsed - self.stage_clear_seconds
                cycle = min(self.warning_blinks - 1, int(warning_elapsed / self.warning_interval))
                cycle_elapsed = warning_elapsed - cycle * self.warning_interval
                self._render_warning(frame, cycle_elapsed)
            return frame
        if self.game_over_until:
            self._text(frame, "GAME OVER", (43, 228), 0x06, .72)
        elif self.serving:
            self._text(frame, "SPACE TO LAUNCH", (22, 232), 0x0E, .48)
        return frame


def main() -> int:
    parser = argparse.ArgumentParser(description="通常面からボス戦へ続くキーボード試作版")
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="既存block_breaker.pyの場所")
    parser.add_argument("--scale", type=int, default=2)
    parser.add_argument("--fps", type=float, default=60.0)
    parser.add_argument("--frames", type=int, default=0, help="テスト用。0なら終了まで実行")
    parser.add_argument("--headless", action="store_true", help="画面を出さずロジックだけ実行")
    args = parser.parse_args()
    m = load_game_module(args.game)
    game = ExtraStageGame(m)
    keyboard_state = None if args.headless else m.X11KeyboardState()
    running = True
    def stop(_signum, _frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print("keys: A/D or LEFT/RIGHT = paddle, SPACE/W/UP = launch, C = full-width paddle, S = clear stage, B = boss skip, R = reset, Q/ESC = quit")
    last = deadline = time.monotonic()
    frame_id = 0
    lateral, lateral_until, launch = 0, 0.0, False
    period = 1 / max(1.0, args.fps)
    try:
        while running and (args.frames <= 0 or frame_id < args.frames):
            now = time.monotonic()
            if lateral and now >= lateral_until:
                lateral = 0
            # X11では現在のキー状態を毎フレーム読む。キーリピート開始を待たないため、
            # 長押し開始・キー解放のどちらにも即座に反応する。
            x11_lateral = keyboard_state.lateral() if keyboard_state is not None else 0
            game.step(now - last, m.GameInput(x11_lateral or lateral, launch), now)
            launch, last = False, now
            indexed = game.render()
            if indexed.shape != (m.CANVAS_HEIGHT, m.CANVAS_WIDTH) or int(indexed.max()) >= m.FC6_LIMIT:
                raise RuntimeError("描画フレームがFC6の192x384条件を満たしません")
            if not args.headless:
                display = m.preview(indexed)
                if args.scale != 1:
                    display = cv2.resize(display, (m.CANVAS_WIDTH * args.scale, m.CANVAS_HEIGHT * args.scale), interpolation=cv2.INTER_NEAREST)
                cv2.imshow("Block Breaker + Extra Boss Stage", display)
                key = cv2.waitKeyEx(1)
                action = m.keyboard_action(key)
                if action == "quit":
                    running = False
                elif action == "left":
                    if keyboard_state is None or not keyboard_state.available:
                        lateral, lateral_until = -1, now + m.KEYBOARD_EVENT_HOLD_SECONDS
                elif action == "right":
                    if keyboard_state is None or not keyboard_state.available:
                        lateral, lateral_until = 1, now + m.KEYBOARD_EVENT_HOLD_SECONDS
                elif action == "launch":
                    launch = True
                elif action == "reset":
                    game.reset()
                elif key & 0xFF in (ord("c"), ord("C")):
                    game.toggle_wide_paddle()
                elif key & 0xFF in (ord("s"), ord("S")):
                    game.start_boss_transition(hide_scene=True)
                elif key & 0xFF in (ord("b"), ord("B")):
                    game.start_boss_transition()
            frame_id += 1
            deadline += period
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -period:
                deadline = time.monotonic()
    finally:
        if keyboard_state is not None:
            keyboard_state.close()
        if not args.headless:
            cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
