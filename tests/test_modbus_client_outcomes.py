"""Outcome classification and whole-transaction deadlines for FoxESSModbusClient.

The legacy bool/None API collapses "the charger refused" and "we never saw a
valid reply" into the same False/None. The strict API must keep them apart so
the transport can report a definite refusal separately from an unknown
outcome. Deadline tests use a real local socket pair so socket timeouts
behave as they do on the wire.
"""
from __future__ import annotations

import socket
import threading
import time

from unittest.mock import patch

import pytest

from custom_components.foxess_charger import modbus_client as mc
from custom_components.foxess_charger.modbus_client import (
    FC_READ_HOLDING,
    FC_WRITE_MULTIPLE,
    FC_WRITE_SINGLE,
    FoxESSModbusClient,
)

from test_modbus_client import SLAVE_ID, build_frame, fc03_response_pdu, make_client


@pytest.fixture(autouse=True)
def _isolate_socket_patches():
    # make_client() starts a create_connection patch it never stops; clear
    # any left by earlier tests so the real-socket tests here get real sockets.
    patch.stopall()
    yield
    patch.stopall()


class DribblingPeer:
    """Local TCP peer that answers each request one byte at a time.

    Every byte arrives well inside any per-recv timeout, so only a deadline
    covering the whole transaction can stop the frame being stretched out.
    """

    def __init__(self, response_for_tid, interval: float) -> None:
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self._response_for_tid = response_for_tid
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._server.settimeout(0.1)
        while True:
            if self._stop.is_set():
                return
            try:
                conn, _ = self._server.accept()
                break
            except TimeoutError:
                continue
            except OSError:
                return
        with conn:
            conn.settimeout(3)
            try:
                request = conn.recv(260)
            except OSError:
                return
            if not request:
                return
            tid = int.from_bytes(request[:2], "big")
            for byte in self._response_for_tid(tid):
                if self._stop.wait(self._interval):
                    return
                try:
                    conn.sendall(bytes([byte]))
                except OSError:
                    return

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._server.close()


@pytest.mark.enable_socket
def test_dribbled_frame_cannot_exceed_transaction_deadline():
    """11-byte FC03 reply at 0.6 s/byte takes 6.6 s; every recv() returns
    within the 5 s default timeout, but the whole call must give up at the
    deadline rather than return the late frame."""
    peer = DribblingPeer(
        lambda tid: build_frame(tid, SLAVE_ID, fc03_response_pdu([42])), 0.6,
    )
    try:
        client = FoxESSModbusClient("127.0.0.1", peer.port, SLAVE_ID)
        started = time.monotonic()
        result = client.read_registers(0x1000, 1)
        elapsed = time.monotonic() - started
    finally:
        peer.close()
    assert result is None
    assert elapsed < 5.6


@pytest.mark.enable_socket
def test_strict_read_deadline_is_configurable():
    peer = DribblingPeer(
        lambda tid: build_frame(tid, SLAVE_ID, fc03_response_pdu([42])), 0.1,
    )
    try:
        client = FoxESSModbusClient("127.0.0.1", peer.port, SLAVE_ID)
        started = time.monotonic()
        with pytest.raises(mc.ModbusNoResponse):
            client.read_registers_strict(0x1000, 1, timeout=0.45)
        assert time.monotonic() - started < 0.9
    finally:
        peer.close()


def test_strict_read_returns_registers():
    frame = build_frame(1, SLAVE_ID, fc03_response_pdu([10, 20]))
    client, _ = make_client([frame[:3], frame[3:]])
    assert client.read_registers_strict(0x1000, 2) == [10, 20]


def test_write_exception_response_is_refusal():
    pdu = bytes([FC_WRITE_MULTIPLE | 0x80, 0x03])
    client, created = make_client([build_frame(1, SLAVE_ID, pdu)])
    with pytest.raises(mc.ModbusExceptionResponse) as err:
        client.write_register_strict(0x3002, 0)
    assert err.value.code == 0x03
    # A well-formed refusal leaves the stream in sync.
    assert created[0].closed is False


def test_read_exception_response_is_refusal():
    pdu = bytes([FC_READ_HOLDING | 0x80, 0x02])
    client, _ = make_client([build_frame(1, SLAVE_ID, pdu)])
    with pytest.raises(mc.ModbusExceptionResponse):
        client.read_registers_strict(0x300A, 2)


def test_write_lost_reply_is_no_response_not_refusal():
    client, created = make_client([])  # peer closes without answering
    with pytest.raises(mc.ModbusNoResponse) as err:
        client.write_register_strict(0x4001, 2)
    assert not isinstance(err.value, mc.ModbusExceptionResponse)
    assert err.value.request_sent is True
    assert created[0].sent, "request must have gone out"


def test_connect_failure_reports_request_not_sent(monkeypatch):
    def refuse(*_a, **_k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(mc.socket, "create_connection", refuse)
    client = FoxESSModbusClient("192.0.2.1", 502, SLAVE_ID)
    with pytest.raises(mc.ModbusNoResponse) as err:
        client.write_register_strict(0x3002, 10)
    assert err.value.request_sent is False
    assert client.write_holding_register(0x3002, 10) is False


@pytest.mark.parametrize(
    "pdu",
    [
        bytes([FC_WRITE_SINGLE]) + (0x4001).to_bytes(2, "big") + (1).to_bytes(2, "big"),
        bytes([FC_WRITE_MULTIPLE]) + (0x3003).to_bytes(2, "big") + (1).to_bytes(2, "big"),
        bytes([FC_WRITE_MULTIPLE]) + (0x3002).to_bytes(2, "big") + (2).to_bytes(2, "big"),
        bytes([FC_WRITE_MULTIPLE]) + (0x3002).to_bytes(2, "big"),
    ],
    ids=["wrong-function", "wrong-address", "wrong-quantity", "truncated-echo"],
)
def test_write_echo_mismatch_is_no_response(pdu):
    client, created = make_client([build_frame(1, SLAVE_ID, pdu)])
    with pytest.raises(mc.ModbusNoResponse) as err:
        client.write_register_strict(0x3002, 10)
    assert err.value.request_sent is True
    assert client.write_echo_mismatches == 1
    assert created[0].closed is True


def test_write_mismatched_tid_is_no_response():
    pdu = bytes([FC_WRITE_SINGLE]) + (0x4001).to_bytes(2, "big") + (2).to_bytes(2, "big")
    client, _ = make_client([build_frame(77, SLAVE_ID, pdu)])
    with pytest.raises(mc.ModbusNoResponse):
        client.write_register_strict(0x4001, 2)
    assert client.txid_mismatches == 1


def test_write_mismatched_unit_is_no_response():
    pdu = bytes([FC_WRITE_SINGLE]) + (0x4001).to_bytes(2, "big") + (2).to_bytes(2, "big")
    client, _ = make_client([build_frame(1, SLAVE_ID + 1, pdu)])
    with pytest.raises(mc.ModbusNoResponse):
        client.write_register_strict(0x4001, 2)


def test_exception_for_other_function_is_not_a_refusal():
    """An exception PDU for a different function code does not answer our
    request - it is a crossed/garbled reply, not a refusal."""
    pdu = bytes([FC_WRITE_SINGLE | 0x80, 0x03])
    client, _ = make_client([build_frame(1, SLAVE_ID, pdu)])
    with pytest.raises(mc.ModbusNoResponse):
        client.write_register_strict(0x3002, 10)


def test_write_uses_fc06_for_write_only_and_fc16_for_rw():
    ok06 = bytes([FC_WRITE_SINGLE]) + (0x4001).to_bytes(2, "big") + (2).to_bytes(2, "big")
    ok16 = bytes([FC_WRITE_MULTIPLE]) + (0x3002).to_bytes(2, "big") + (1).to_bytes(2, "big")
    client, created = make_client([build_frame(1, SLAVE_ID, ok06), build_frame(2, SLAVE_ID, ok16)])
    client.write_register_strict(0x4001, 2)
    client.write_register_strict(0x3002, 10)
    assert created[0].sent[0][7] == FC_WRITE_SINGLE
    assert created[0].sent[1][7] == FC_WRITE_MULTIPLE


@pytest.mark.parametrize("value", [-1, 0x10000, True, 1.0])
def test_out_of_range_write_value_rejected_before_wire(value):
    client, created = make_client([])
    with pytest.raises((ValueError, TypeError)):
        client.write_register_strict(0x3002, value)
    assert not created
