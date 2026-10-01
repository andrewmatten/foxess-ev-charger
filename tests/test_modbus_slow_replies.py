"""Slow-reply incident: the real charger sometimes takes more than 2 s
to answer a Modbus TCP request. With `transport.DEFAULT_DEADLINE` at 2.0 s,
a slow-but-otherwise-fine reply timed out; the client then read that late
reply as the answer to the *next* request, saw a Transaction ID mismatch,
and reset the connection - forever, once the charger was consistently a
little slow.

These use a real local TCP peer (not a mock) so timing behaves as it does on
the wire, in the pattern of test_modbus_client_outcomes.py's DribblingPeer.
"""
from __future__ import annotations

import socket
import threading

import pytest

from custom_components.foxess_charger import modbus_client as mc
from custom_components.foxess_charger.modbus_client import FoxESSModbusClient
from custom_components.foxess_charger.transport import ModbusRegisterIO, UnknownOutcome

from test_modbus_client import SLAVE_ID, build_frame, fc03_response_pdu


class SlowPeer:
    """A local TCP peer that answers every request on the one connection it
    accepts, in order - real Modbus TCP servers don't open a fresh
    connection per request. `plan` maps a Transaction ID to (delay in
    seconds, the PDU to reply with); a TID not in `plan` gets no reply at
    all (the connection is simply left open, request unanswered).
    """

    def __init__(self, plan: dict[int, tuple[float, bytes]]) -> None:
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self._plan = plan
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @staticmethod
    def _recv_n(conn: socket.socket, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _run(self) -> None:
        self._server.settimeout(0.1)
        while True:
            if self._stop.is_set():
                return
            try:
                conn, _ = self._server.accept()
                self.accepted += 1
                break
            except TimeoutError:
                continue
            except OSError:
                return
        with conn:
            # Comfortably longer than any delay used by a test, short enough
            # that a request nobody answers (test (c)) doesn't hang the
            # thread for the full deadline the client itself is waiting out.
            conn.settimeout(6)
            while not self._stop.is_set():
                try:
                    header = self._recv_n(conn, 7)
                    if header is None:
                        return
                    length = int.from_bytes(header[4:6], "big")
                    if self._recv_n(conn, length - 1) is None:
                        return
                    tid = int.from_bytes(header[:2], "big")
                    plan = self._plan.get(tid)
                    if plan is None:
                        continue  # request never answered
                    delay, pdu = plan
                    if self._stop.wait(delay):
                        return
                    conn.sendall(build_frame(tid, SLAVE_ID, pdu))
                except OSError:
                    return

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._server.close()


@pytest.mark.enable_socket
def test_a_three_second_reply_succeeds_with_the_five_second_default_deadline():
    """(a) A charger that takes 3 s to answer must succeed against the
    client's own default deadline, which matches the transport's
    (transport.DEFAULT_DEADLINE, currently 5.0 s)."""
    peer = SlowPeer({1: (3.0, fc03_response_pdu([42]))})
    try:
        client = FoxESSModbusClient("127.0.0.1", peer.port, SLAVE_ID)
        assert client.read_registers_strict(0x1000, 1) == [42]
    finally:
        peer.close()


@pytest.mark.enable_socket
async def test_transport_default_deadline_absorbs_a_three_second_reply():
    """(a) at the transport layer, which is what actually broke live: the
    sync client's own default was already 5.0 s, but ModbusRegisterIO
    (transport.py) used a separate, shorter DEFAULT_DEADLINE (2.0 s)."""
    peer = SlowPeer({1: (3.0, fc03_response_pdu([42]))})
    try:
        client = FoxESSModbusClient("127.0.0.1", peer.port, SLAVE_ID)
        io = ModbusRegisterIO(client)  # uses transport.DEFAULT_DEADLINE
        assert await io.read(0x1000, 1) == (42,)
    finally:
        peer.close()


@pytest.mark.enable_socket
def test_a_three_second_reply_used_to_time_out_at_the_old_two_second_deadline():
    """Same 3 s reply, at the deadline the live transport used before this
    fix (2.0 s) - reproduces the field failure directly: nothing wrong with
    the reply, the deadline was simply too short for how slow the charger
    can legitimately be."""
    peer = SlowPeer({1: (3.0, fc03_response_pdu([42]))})
    try:
        client = FoxESSModbusClient("127.0.0.1", peer.port, SLAVE_ID)
        with pytest.raises(mc.ModbusNoResponse):
            client.read_registers_strict(0x1000, 1, timeout=2.0)
    finally:
        peer.close()


@pytest.mark.enable_socket
def test_late_reply_to_an_abandoned_request_is_discarded_not_mistaken_for_the_next():
    """(b) Request 1 (TID 1) times out client-side after 1 s with nothing
    received; its reply is still on the way, arriving 1 s later. Request 2
    (TID 2), sent right after request 1 gives up, then reads that late TID-1
    frame first - it must be discarded as `stale`, not mistaken for request
    2's own reply or treated as corruption. Request 2 must still succeed,
    on the same connection, with no reset."""
    peer = SlowPeer({
        1: (2.0, fc03_response_pdu([111])),   # arrives 1 s after request 1's own timeout
        2: (0.1, fc03_response_pdu([222])),
    })
    try:
        client = FoxESSModbusClient("127.0.0.1", peer.port, SLAVE_ID)

        with pytest.raises(mc.ModbusNoResponse):
            client.read_registers_strict(0x1000, 1, timeout=1.0)

        assert client.read_registers_strict(0x1000, 1, timeout=5.0) == [222]

        assert client.stale_replies == 1
        assert client.txid_mismatches == 0
        assert peer.accepted == 1  # never reconnected
    finally:
        peer.close()


@pytest.mark.enable_socket
async def test_write_reply_beyond_the_deadline_is_unknown_outcome_not_silent_success():
    """(c) A write whose reply never arrives within the deadline (charger
    even slower than the deadline allows, or the reply is genuinely lost)
    must still surface as UnknownOutcome through the transport - the
    outcome really is unknown, never reported as a quiet success."""
    peer = SlowPeer({})  # request 1 is never answered at all
    try:
        client = FoxESSModbusClient("127.0.0.1", peer.port, SLAVE_ID)
        io = ModbusRegisterIO(client)
        with pytest.raises(UnknownOutcome):
            await io.write(0x4001, 2)
    finally:
        peer.close()
