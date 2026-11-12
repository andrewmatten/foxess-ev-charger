"""Message pacing (protocol document V1.06, 1.4: messages 500 ms apart).

The client spaces the *sends* of consecutive requests by `min_interval`.
The wait happens before the transaction deadline starts, so pacing never
shortens the time a reply is given. Default is no pacing.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from custom_components.foxess_charger.modbus_client import (
    PROTOCOL_MESSAGE_INTERVAL,
    FoxESSModbusClient,
)

from test_modbus_client import SLAVE_ID, fc03_response_pdu
from test_modbus_slow_replies import SlowPeer


@pytest.fixture(autouse=True)
def _real_sockets():
    # test_modbus_client.make_client() leaves socket.create_connection
    # patched for the rest of the process; these tests need the real one.
    patch.stopall()
    yield


def _peer(n: int = 6) -> SlowPeer:
    return SlowPeer({tid: (0.0, fc03_response_pdu([7])) for tid in range(1, n + 1)})


@pytest.mark.enable_socket
def test_consecutive_requests_are_spaced_by_min_interval():
    peer = _peer()
    client = FoxESSModbusClient("127.0.0.1", peer.port, SLAVE_ID, min_interval=0.2)
    try:
        sends = []
        for _ in range(4):
            assert client.read_registers_strict(0x1000, 1) == [7]
            sends.append(time.monotonic())
        gaps = [b - a for a, b in zip(sends, sends[1:])]
        assert all(gap >= 0.19 for gap in gaps), gaps
        assert sum(gaps) < 1.5                   # paced, not stalled
    finally:
        client.disconnect()
        peer.close()


@pytest.mark.enable_socket
def test_no_pacing_by_default():
    peer = _peer()
    client = FoxESSModbusClient("127.0.0.1", peer.port, SLAVE_ID)
    try:
        start = time.monotonic()
        for _ in range(4):
            assert client.read_registers_strict(0x1000, 1) == [7]
        assert time.monotonic() - start < 0.3
    finally:
        client.disconnect()
        peer.close()


@pytest.mark.enable_socket
def test_pacing_does_not_shorten_the_reply_deadline():
    # Second reply takes 0.35 s; with a 0.5 s deadline it only fits if the
    # 0.3 s pacing wait is not counted against the transaction.
    peer = SlowPeer({1: (0.0, fc03_response_pdu([7])), 2: (0.35, fc03_response_pdu([9]))})
    client = FoxESSModbusClient(
        "127.0.0.1", peer.port, SLAVE_ID, timeout=0.5, min_interval=0.3)
    try:
        assert client.read_registers_strict(0x1000, 1) == [7]
        assert client.read_registers_strict(0x1000, 1) == [9]
    finally:
        client.disconnect()
        peer.close()


def test_negative_interval_is_rejected():
    with pytest.raises(ValueError):
        FoxESSModbusClient("127.0.0.1", 502, SLAVE_ID, min_interval=-1)


def test_protocol_interval_is_half_a_second():
    assert PROTOCOL_MESSAGE_INTERVAL == 0.5
