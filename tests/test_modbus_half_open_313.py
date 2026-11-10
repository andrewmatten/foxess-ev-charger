"""A half-open connection (peer gone, no RST) must not be kept forever: after
a few silent timeouts in a row the client closes the socket and re-dials."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.foxess_charger import modbus_client as mc
from custom_components.foxess_charger.modbus_client import FoxESSModbusClient, ModbusNoResponse

from test_modbus_client import HOST, PORT, SLAVE_ID, build_frame, fc03_response_pdu

N = 3


class ScriptedSocket:
    """recv() pops scripted frames, or raises TimeoutError when none are left
    (a peer that says nothing)."""

    def __init__(self) -> None:
        self.script: list[bytes] = []
        self.sent: list[bytes] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        if not self.script:
            raise TimeoutError("timed out")
        chunk = self.script.pop(0)
        if len(chunk) > n:
            self.script.insert(0, chunk[n:])
            chunk = chunk[:n]
        return chunk

    def settimeout(self, t: float) -> None:
        pass

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def dial():
    socks: list[ScriptedSocket] = []

    def _create(*_a, **_k):
        socks.append(ScriptedSocket())
        return socks[-1]

    with patch.object(mc.socket, "create_connection", side_effect=_create):
        yield FoxESSModbusClient(HOST, PORT, SLAVE_ID, timeout=0.05), socks


def _read(client):
    return client.read_registers_strict(0x1000, 1)


def _silent(client):
    with pytest.raises(ModbusNoResponse) as ei:
        _read(client)
    assert ei.value.request_sent is True


def _good(client, socks):
    tid = (client._tid + 1) & 0xFFFF
    socks[-1].script = [build_frame(tid, SLAVE_ID, fc03_response_pdu([7]))]
    assert _read(client) == [7]


def test_closes_after_n_silent_timeouts_and_redials(dial):
    client, socks = dial
    for _ in range(N):
        _silent(client)
    assert socks[0].closed and len(socks) == 1
    assert len(socks[0].sent) == N
    _silent(client)
    assert len(socks) == 2 and not socks[1].closed


def test_good_reply_resets_the_counter(dial):
    client, socks = dial
    _silent(client)
    _good(client, socks)
    _silent(client)
    _silent(client)
    assert len(socks) == 1 and not socks[0].closed


def test_late_reply_after_single_timeout_is_discarded(dial):
    client, socks = dial
    _silent(client)
    # late answer to request 1, then the real one for request 2
    socks[0].script = [
        build_frame(1, SLAVE_ID, fc03_response_pdu([9])),
        build_frame(2, SLAVE_ID, fc03_response_pdu([7])),
    ]
    assert _read(client) == [7]
    assert client.stale_replies == 1 and len(socks) == 1 and not socks[0].closed
