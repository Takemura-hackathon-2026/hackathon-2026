#!/usr/bin/env python3
"""2ノード入力UDP通信の自己テスト。"""
from __future__ import annotations

import math
import socket
import time

from input_transport import (
    InputStateReceiver,
    InputStateSender,
    RemoteInputState,
    SensorControlReceiver,
    SensorControlSender,
    decode_input,
    encode_input,
    parse_endpoint,
)


def main() -> int:
    errors: list[str] = []
    source = RemoteInputState(
        lateral=-1,
        jump=True,
        body_present=True,
        calibrated=True,
        launch=True,
        start_trigger=True,
        body_x=0.375,
        start_hold_remaining=1.25,
        player_id=7,
        people_detected=3,
        player_changed=True,
    )
    packet = encode_input(source, 0xFFFFFFFE, 123456789)
    decoded = decode_input(packet)
    if decoded.sequence != 0xFFFFFFFE or decoded.sent_monotonic_ns != 123456789:
        errors.append("sequence/timestampの往復が不一致")
    if decoded.state != source:
        errors.append(f"入力状態の往復が不一致: {decoded.state!r}")
    none_x = decode_input(encode_input(RemoteInputState(), 1)).state.body_x
    if none_x is not None:
        errors.append("body_x=Noneの往復が不一致")
    broken = bytearray(packet)
    broken[8] ^= 1
    try:
        decode_input(bytes(broken))
        errors.append("CRC破損パケットを受理する")
    except ValueError:
        pass
    try:
        encode_input(RemoteInputState(lateral=2), 1)
        errors.append("不正lateralを送信できる")
    except ValueError:
        pass
    try:
        encode_input(RemoteInputState(body_x=math.inf), 1)
        errors.append("不正body_xを送信できる")
    except ValueError:
        pass
    if parse_endpoint("192.168.10.2:5201", "0.0.0.0") != ("192.168.10.2", 5201):
        errors.append("IPv4 endpoint解析が不正")

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    receiver = InputStateReceiver(("127.0.0.1", port), timeout_seconds=0.05)
    sender = InputStateSender(("127.0.0.1", port))

    def wait_for_input(target: InputStateReceiver) -> tuple[RemoteInputState, bool]:
        deadline = time.monotonic() + 0.20
        state, connected = target.read()
        while not connected and time.monotonic() < deadline:
            time.sleep(0.001)
            state, connected = target.read()
        return state, connected

    try:
        sender.send(source, 10)
        state, connected = wait_for_input(receiver)
        if not connected or state != source:
            errors.append("UDPで最新状態を受信できない")
        repeated, connected = receiver.read()
        if not connected or repeated.jump or repeated.launch or repeated.start_trigger or repeated.player_changed:
            errors.append("イベントフラグを同一sequenceで再発火する")
        sender.send(RemoteInputState(start_trigger=True, calibrated=True), 11)
        sender.send(RemoteInputState(lateral=1, calibrated=True), 12)
        time.sleep(0.005)
        coalesced, connected = receiver.read()
        if not connected or not coalesced.start_trigger or coalesced.lateral != 1:
            errors.append("poll間のイベントを最新連続状態と統合しない")
        sender.send(RemoteInputState(lateral=-1, calibrated=True), 9)
        time.sleep(0.005)
        old, _connected = receiver.read()
        if old.lateral != 1 or receiver.old != 1:
            errors.append("古いsequenceを破棄しない")
        time.sleep(0.06)
        stale, connected = receiver.read()
        if connected or stale != RemoteInputState():
            errors.append("タイムアウト時にニュートラルへ戻らない")
        sender.send(RemoteInputState(body_present=True, calibrated=True, body_x=.5), 0)
        restarted, connected = wait_for_input(receiver)
        if not connected or not restarted.body_present or restarted.body_x != .5:
            errors.append("センサー再起動後のsequenceリセットを受理しない")
    finally:
        sender.close()
        receiver.close()

    wrap_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    wrap_probe.bind(("127.0.0.1", 0))
    wrap_port = wrap_probe.getsockname()[1]
    wrap_probe.close()
    wrap_receiver = InputStateReceiver(("127.0.0.1", wrap_port), timeout_seconds=0.05)
    wrap_sender = InputStateSender(("127.0.0.1", wrap_port))
    try:
        wrap_sender.send(RemoteInputState(lateral=1, calibrated=True), 0xFFFFFFFF)
        wait_for_input(wrap_receiver)
        wrap_sender.send(RemoteInputState(lateral=0, calibrated=True), 0)
        time.sleep(0.005)
        wrapped, connected = wrap_receiver.read()
        if not connected or wrapped.lateral != 0:
            errors.append("sequenceの32bit周回を受理しない")
    finally:
        wrap_sender.close()
        wrap_receiver.close()

    control_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    control_probe.bind(("127.0.0.1", 0))
    control_port = control_probe.getsockname()[1]
    control_probe.close()
    control_receiver = SensorControlReceiver(("127.0.0.1", control_port))
    control_sender = SensorControlSender(("127.0.0.1", control_port))
    try:
        control_sender.reselect_player()
        deadline = time.monotonic() + .20
        while not control_receiver.poll_reselect() and time.monotonic() < deadline:
            time.sleep(.001)
        if control_receiver.latest_sequence != 0:
            errors.append("人物再選択通知を受信しない")
        if control_receiver.poll_reselect():
            errors.append("人物再選択通知を重複発火する")
    finally:
        control_sender.close()
        control_receiver.close()

    for error in errors:
        print(f"ERROR: {error}")
    print(f"{len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
