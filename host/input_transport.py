#!/usr/bin/env python3
"""センサーノードから制御ノードへゲーム入力だけを送るUDP通信。"""
from __future__ import annotations

import math
import socket
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Protocol


MAGIC = b"RINP"
VERSION = 2
DEFAULT_PORT = 5200
DEFAULT_CONTROL_PORT = 5201
FLAG_JUMP = 1 << 0
FLAG_BODY_PRESENT = 1 << 1
FLAG_CALIBRATED = 1 << 2
FLAG_LAUNCH = 1 << 3
FLAG_START_TRIGGER = 1 << 4
FLAG_PLAYER_CHANGED = 1 << 5
PACKET_WITHOUT_CRC = struct.Struct("!4sBBbBIQffiH")
CRC = struct.Struct("!I")
PACKET_SIZE = PACKET_WITHOUT_CRC.size + CRC.size
CONTROL_MAGIC = b"RCTL"
CONTROL_VERSION = 1
CONTROL_RESELECT_PLAYER = 1
CONTROL_WITHOUT_CRC = struct.Struct("!4sBBI")
CONTROL_SIZE = CONTROL_WITHOUT_CRC.size + CRC.size


class InputStateLike(Protocol):
    lateral: int
    jump: bool
    body_present: bool
    calibrated: bool
    launch: bool
    start_trigger: bool
    body_x: float | None
    start_hold_remaining: float | None
    player_id: int | None
    people_detected: int
    player_changed: bool


@dataclass(frozen=True)
class RemoteInputState:
    lateral: int = 0
    jump: bool = False
    body_present: bool = False
    calibrated: bool = False
    launch: bool = False
    start_trigger: bool = False
    body_x: float | None = None
    start_hold_remaining: float | None = None
    player_id: int | None = None
    people_detected: int = 0
    player_changed: bool = False

    def without_events(self) -> "RemoteInputState":
        return RemoteInputState(
            lateral=self.lateral,
            body_present=self.body_present,
            calibrated=self.calibrated,
            body_x=self.body_x,
            start_hold_remaining=self.start_hold_remaining,
            player_id=self.player_id,
            people_detected=self.people_detected,
        )


@dataclass(frozen=True)
class DecodedInput:
    sequence: int
    sent_monotonic_ns: int
    state: RemoteInputState


def parse_endpoint(value: str, default_host: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    text = value.strip()
    if not text:
        raise ValueError("接続先が空")
    host, port = text, default_port
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError(f"IPv6アドレスの閉じ括弧がない: {value}")
        host = text[1:end]
        suffix = text[end + 1:]
        if suffix:
            if not suffix.startswith(":"):
                raise ValueError(f"接続先の形式が不正: {value}")
            port = int(suffix[1:])
    elif text.count(":") == 1:
        host, raw_port = text.rsplit(":", 1)
        port = int(raw_port)
    elif text.count(":") > 1:
        host = text
    if not host:
        host = default_host
    if not 1 <= port <= 65535:
        raise ValueError(f"ポート範囲が不正: {port}")
    return host, port


def _flags(state: InputStateLike) -> int:
    return (
        (FLAG_JUMP if state.jump else 0)
        | (FLAG_BODY_PRESENT if state.body_present else 0)
        | (FLAG_CALIBRATED if state.calibrated else 0)
        | (FLAG_LAUNCH if state.launch else 0)
        | (FLAG_START_TRIGGER if state.start_trigger else 0)
        | (FLAG_PLAYER_CHANGED if state.player_changed else 0)
    )


def encode_input(state: InputStateLike, sequence: int, sent_monotonic_ns: int | None = None) -> bytes:
    lateral = int(state.lateral)
    if lateral not in (-1, 0, 1):
        raise ValueError(f"lateralは-1/0/1: {lateral}")
    if state.body_x is None:
        body_x = math.nan
    else:
        body_x = float(state.body_x)
        if not math.isfinite(body_x) or not 0.0 <= body_x <= 1.0:
            raise ValueError(f"body_xは0〜1またはNone: {body_x}")
    if state.start_hold_remaining is None:
        start_hold_remaining = math.nan
    else:
        start_hold_remaining = float(state.start_hold_remaining)
        if not math.isfinite(start_hold_remaining) or start_hold_remaining < 0.0:
            raise ValueError(f"start_hold_remainingは0以上またはNone: {start_hold_remaining}")
    player_id = -1 if state.player_id is None else int(state.player_id)
    if player_id < -1:
        raise ValueError(f"player_idは0以上またはNone: {player_id}")
    people_detected = int(state.people_detected)
    if not 0 <= people_detected <= 0xFFFF:
        raise ValueError(f"people_detectedは0〜65535: {people_detected}")
    payload = PACKET_WITHOUT_CRC.pack(
        MAGIC,
        VERSION,
        _flags(state),
        lateral,
        0,
        int(sequence) & 0xFFFFFFFF,
        int(time.monotonic_ns() if sent_monotonic_ns is None else sent_monotonic_ns),
        body_x,
        start_hold_remaining,
        player_id,
        people_detected,
    )
    return payload + CRC.pack(zlib.crc32(payload) & 0xFFFFFFFF)


def decode_input(packet: bytes) -> DecodedInput:
    if len(packet) != PACKET_SIZE:
        raise ValueError(f"入力パケット長が不正: {len(packet)}")
    payload, raw_crc = packet[:-CRC.size], packet[-CRC.size:]
    expected_crc = CRC.unpack(raw_crc)[0]
    actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise ValueError("入力パケットCRC不一致")
    magic, version, flags, lateral, reserved, sequence, sent_ns, body_x, hold_remaining, player_id, people_detected = PACKET_WITHOUT_CRC.unpack(payload)
    if magic != MAGIC or version != VERSION or reserved != 0:
        raise ValueError("入力パケットのmagic/version/reservedが不正")
    if lateral not in (-1, 0, 1):
        raise ValueError(f"入力パケットのlateralが不正: {lateral}")
    if flags & ~(
        FLAG_JUMP
        | FLAG_BODY_PRESENT
        | FLAG_CALIBRATED
        | FLAG_LAUNCH
        | FLAG_START_TRIGGER
        | FLAG_PLAYER_CHANGED
    ):
        raise ValueError(f"入力パケットに未定義flags: {flags:#x}")
    decoded_body_x: float | None = None if math.isnan(body_x) else float(body_x)
    if decoded_body_x is not None and (not math.isfinite(decoded_body_x) or not 0.0 <= decoded_body_x <= 1.0):
        raise ValueError(f"入力パケットのbody_xが不正: {decoded_body_x}")
    decoded_hold: float | None = None if math.isnan(hold_remaining) else float(hold_remaining)
    if decoded_hold is not None and (not math.isfinite(decoded_hold) or decoded_hold < 0.0):
        raise ValueError(f"入力パケットのstart_hold_remainingが不正: {decoded_hold}")
    decoded_player_id: int | None = None if player_id == -1 else int(player_id)
    if decoded_player_id is not None and decoded_player_id < 0:
        raise ValueError(f"入力パケットのplayer_idが不正: {decoded_player_id}")
    return DecodedInput(
        sequence=sequence,
        sent_monotonic_ns=sent_ns,
        state=RemoteInputState(
            lateral=lateral,
            jump=bool(flags & FLAG_JUMP),
            body_present=bool(flags & FLAG_BODY_PRESENT),
            calibrated=bool(flags & FLAG_CALIBRATED),
            launch=bool(flags & FLAG_LAUNCH),
            start_trigger=bool(flags & FLAG_START_TRIGGER),
            body_x=decoded_body_x,
            start_hold_remaining=decoded_hold,
            player_id=decoded_player_id,
            people_detected=people_detected,
            player_changed=bool(flags & FLAG_PLAYER_CHANGED),
        ),
    )


def _newer_sequence(candidate: int, current: int | None) -> bool:
    if current is None:
        return True
    delta = (candidate - current) & 0xFFFFFFFF
    return 0 < delta < 0x80000000


class InputStateSender:
    def __init__(self, destination: tuple[str, int]) -> None:
        self.destination = destination
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sent = 0

    def send(self, state: InputStateLike, sequence: int) -> None:
        packet = encode_input(state, sequence)
        written = self.socket.sendto(packet, self.destination)
        if written != len(packet):
            raise OSError(f"入力パケットの送信長が不正: {written}/{len(packet)}")
        self.sent += 1

    def close(self) -> None:
        self.socket.close()


class InputStateReceiver:
    """最新状態を保持し、イベントフラグは新しいsequenceで一度だけ返す。"""

    def __init__(self, bind: tuple[str, int], timeout_seconds: float = 0.20) -> None:
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ValueError("入力タイムアウトは正の有限値")
        self.bind = bind
        self.timeout_seconds = timeout_seconds
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(bind)
        self.socket.setblocking(False)
        self.latest: DecodedInput | None = None
        self.last_arrival = 0.0
        self.pending_jump = False
        self.pending_launch = False
        self.pending_start_trigger = False
        self.pending_player_changed = False
        self.received = 0
        self.invalid = 0
        self.old = 0

    def read(self, now: float | None = None) -> tuple[RemoteInputState, bool]:
        current_time = time.monotonic() if now is None else now
        while True:
            try:
                packet, _source = self.socket.recvfrom(256)
            except BlockingIOError:
                break
            try:
                decoded = decode_input(packet)
            except ValueError:
                self.invalid += 1
                continue
            self.received += 1
            if self.latest is not None and not _newer_sequence(decoded.sequence, self.latest.sequence):
                self.old += 1
                continue
            self.latest = decoded
            self.last_arrival = current_time
            self.pending_jump = self.pending_jump or decoded.state.jump
            self.pending_launch = self.pending_launch or decoded.state.launch
            self.pending_start_trigger = self.pending_start_trigger or decoded.state.start_trigger
            self.pending_player_changed = self.pending_player_changed or decoded.state.player_changed
        if self.latest is None or current_time - self.last_arrival > self.timeout_seconds:
            self.pending_jump = False
            self.pending_launch = False
            self.pending_start_trigger = False
            self.pending_player_changed = False
            return RemoteInputState(), False
        latest = self.latest.state
        state = RemoteInputState(
            lateral=latest.lateral,
            jump=self.pending_jump,
            body_present=latest.body_present,
            calibrated=latest.calibrated,
            launch=self.pending_launch,
            start_trigger=self.pending_start_trigger,
            body_x=latest.body_x,
            start_hold_remaining=latest.start_hold_remaining,
            player_id=latest.player_id,
            people_detected=latest.people_detected,
            player_changed=self.pending_player_changed,
        )
        self.pending_jump = False
        self.pending_launch = False
        self.pending_start_trigger = False
        self.pending_player_changed = False
        return state, True

    def close(self) -> None:
        self.socket.close()


def encode_control_reselect(sequence: int) -> bytes:
    payload = CONTROL_WITHOUT_CRC.pack(
        CONTROL_MAGIC,
        CONTROL_VERSION,
        CONTROL_RESELECT_PLAYER,
        int(sequence) & 0xFFFFFFFF,
    )
    return payload + CRC.pack(zlib.crc32(payload) & 0xFFFFFFFF)


def decode_control_reselect(packet: bytes) -> int:
    if len(packet) != CONTROL_SIZE:
        raise ValueError(f"制御パケット長が不正: {len(packet)}")
    payload, raw_crc = packet[:-CRC.size], packet[-CRC.size:]
    if zlib.crc32(payload) & 0xFFFFFFFF != CRC.unpack(raw_crc)[0]:
        raise ValueError("制御パケットCRC不一致")
    magic, version, command, sequence = CONTROL_WITHOUT_CRC.unpack(payload)
    if magic != CONTROL_MAGIC or version != CONTROL_VERSION or command != CONTROL_RESELECT_PLAYER:
        raise ValueError("制御パケットのmagic/version/commandが不正")
    return sequence


class SensorControlSender:
    """制御PiからセンサーPiへ人物ロック解除を通知する。"""

    def __init__(self, destination: tuple[str, int]) -> None:
        self.destination = destination
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sequence = 0

    def reselect_player(self) -> None:
        packet = encode_control_reselect(self.sequence)
        written = self.socket.sendto(packet, self.destination)
        if written != len(packet):
            raise OSError(f"制御パケットの送信長が不正: {written}/{len(packet)}")
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF

    def close(self) -> None:
        self.socket.close()


class SensorControlReceiver:
    """センサーPiで人物ロック解除通知を重複なく受け取る。"""

    def __init__(self, bind: tuple[str, int]) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(bind)
        self.socket.setblocking(False)
        self.latest_sequence: int | None = None
        self.invalid = 0

    def poll_reselect(self) -> bool:
        reselect = False
        while True:
            try:
                packet, _source = self.socket.recvfrom(128)
            except BlockingIOError:
                break
            try:
                sequence = decode_control_reselect(packet)
            except ValueError:
                self.invalid += 1
                continue
            if _newer_sequence(sequence, self.latest_sequence):
                self.latest_sequence = sequence
                reselect = True
        return reselect

    def close(self) -> None:
        self.socket.close()
