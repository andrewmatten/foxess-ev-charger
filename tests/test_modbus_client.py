"""Unit tests for FoxESSModbusClient's transport layer.

These mock the raw socket (via unittest.mock, patching
socket.create_connection to return a fake socket-like object) rather than
talking to a real charger or a real TCP stack. No HA test harness needed:
FoxESSModbusClient touches no hass/coordinator state, only sockets.

Covers the framing rewrite in modbus_client.py: reading the MBAP header and
body in a loop instead of trusting a single recv() to return one whole
frame, plus the validity checks layered on top (Transaction ID, Unit ID,
function code, byte_count, write-echo verification) and the
reconnect-after-framing-failure behaviour.
"""
from __future__ import annotations

from unittest.mock import patch

from custom_components.foxess_charger.modbus_client import (
    FC_READ_HOLDING,
    FC_WRITE_MULTIPLE,
    FC_WRITE_SINGLE,
    FoxESSModbusClient,
)

HOST, PORT, SLAVE_ID = "192.0.2.1", 502, 1


class FakeSocket:
    """A minimal socket.socket stand-in.

    `chunks` is the sequence of byte strings successive recv() calls return
    - however many bytes are requested, at most one chunk's worth is ever
    handed back per call (splitting the chunk and keeping the remainder for
    next time if it's larger than what was asked for), mirroring how a real
    TCP recv() can return less than requested even when more data is
    already available. An empty list (or an exhausted one) makes recv()
    return b"" - a closed connection, per socket semantics.
    """

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout: float | None = None

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if len(chunk) > n:
            self._chunks.insert(0, chunk[n:])
            chunk = chunk[:n]
        return chunk

    def settimeout(self, t: float) -> None:
        self.timeout = t

    def close(self) -> None:
        self.closed = True


def build_frame(tid: int, unit_id: int, pdu: bytes) -> bytes:
    """Builds a complete MBAP + PDU frame, the same way the client does."""
    length = 1 + len(pdu)
    return (
        tid.to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + length.to_bytes(2, "big")
        + unit_id.to_bytes(1, "big")
        + pdu
    )


def fc03_response_pdu(registers: list[int]) -> bytes:
    data = b"".join(v.to_bytes(2, "big") for v in registers)
    return FC_READ_HOLDING.to_bytes(1, "big") + len(data).to_bytes(1, "big") + data


def make_client(
    chunks: list[bytes], extra_socket_chunks: list[list[bytes]] | None = None,
) -> tuple[FoxESSModbusClient, list[FakeSocket]]:
    """Builds a client whose socket.create_connection() calls hand out
    FakeSocket instances, one per connection - `chunks` for the first
    connection, then one entry of `extra_socket_chunks` per reconnect after
    that (for tests that force more than one connection to be opened).
    Returns the client plus the list of FakeSockets created, in order, so a
    test can assert whether/when a reconnect happened.

    The patcher is intentionally never stopped - it's scoped to the test via
    pytest's per-test import/patch lifecycle (each test gets a fresh
    unittest.mock.patch call), and stopping it would only matter if
    something else in the same process needed the real
    socket.create_connection afterwards, which nothing here does.
    """
    client = FoxESSModbusClient(HOST, PORT, SLAVE_ID)
    created: list[FakeSocket] = []
    remaining_chunks = [chunks] + list(extra_socket_chunks or [])

    def _fake_create_connection(*_args, **_kwargs):
        next_chunks = remaining_chunks.pop(0) if remaining_chunks else []
        sock = FakeSocket(next_chunks)
        created.append(sock)
        return sock

    patch(
        "custom_components.foxess_charger.modbus_client.socket.create_connection",
        side_effect=_fake_create_connection,
    ).start()
    return client, created


class TestPartialFrames:
    def test_response_split_across_two_recv_calls(self):
        """A response arriving in two TCP segments must still be assembled
        into one frame - never trust a single recv() to return it whole."""
        pdu = fc03_response_pdu([10, 20])
        frame = build_frame(tid=1, unit_id=SLAVE_ID, pdu=pdu)
        # Split partway through the header on purpose.
        client, _ = make_client([frame[:4], frame[4:]])

        result = client.read_registers(0x1000, 2)

        assert result == [10, 20]
        assert client.short_reads == 0
        assert client.txid_mismatches == 0

    def test_connection_closed_before_full_header_is_a_short_read(self):
        """Peer closes mid-header (recv() returns b"") - counted as a short
        read, not silently treated as an empty/zero response."""
        client, _ = make_client([b"\x00\x01\x00"])  # only 3 of 7 header bytes

        result = client.read_registers(0x1000, 2)

        assert result is None
        assert client.short_reads == 1

    def test_connection_closed_before_full_body_is_a_short_read(self):
        pdu = fc03_response_pdu([10, 20])
        frame = build_frame(tid=1, unit_id=SLAVE_ID, pdu=pdu)
        # Full header, but the body gets cut short.
        client, _ = make_client([frame[:7], frame[7:9]])

        result = client.read_registers(0x1000, 2)

        assert result is None
        assert client.short_reads == 1


class TestFrameValidation:
    def test_wrong_transaction_id_is_rejected(self):
        pdu = fc03_response_pdu([10, 20])
        frame = build_frame(tid=999, unit_id=SLAVE_ID, pdu=pdu)  # client sent tid=1
        client, _ = make_client([frame])

        result = client.read_registers(0x1000, 2)

        assert result is None
        assert client.txid_mismatches == 1

    def test_wrong_unit_id_is_rejected(self):
        pdu = fc03_response_pdu([10, 20])
        frame = build_frame(tid=1, unit_id=SLAVE_ID + 1, pdu=pdu)
        client, _ = make_client([frame])

        result = client.read_registers(0x1000, 2)

        assert result is None
        assert client.unit_id_mismatches == 1

    def test_wrong_function_code_in_response_is_rejected(self):
        """Structurally valid frame (right TID, right Unit ID), but the PDU's
        function code isn't the one we asked for and isn't an exception
        either - must not be decoded as if it were register data."""
        bogus_pdu = (0x04).to_bytes(1, "big") + (2).to_bytes(1, "big") + b"\x00\x0a"
        frame = build_frame(tid=1, unit_id=SLAVE_ID, pdu=bogus_pdu)
        client, _ = make_client([frame])

        result = client.read_registers(0x1000, 1)

        assert result is None

    def test_modbus_exception_response_is_rejected(self):
        exception_pdu = (FC_READ_HOLDING | 0x80).to_bytes(1, "big") + (0x02).to_bytes(1, "big")
        frame = build_frame(tid=1, unit_id=SLAVE_ID, pdu=exception_pdu)
        client, _ = make_client([frame])

        result = client.read_registers(0x1000, 1)

        assert result is None

    def test_mismatched_byte_count_is_rejected(self):
        """byte_count field claims more data than the PDU actually has -
        a device-level inconsistency, not a TCP truncation (the frame is
        exactly as long as its own MBAP header declared)."""
        data = (10).to_bytes(2, "big")
        pdu = FC_READ_HOLDING.to_bytes(1, "big") + (4).to_bytes(1, "big") + data  # claims 4, has 2
        frame = build_frame(tid=1, unit_id=SLAVE_ID, pdu=pdu)
        client, _ = make_client([frame])

        result = client.read_registers(0x1000, 2)

        assert result is None
        assert client.short_reads == 1

    def test_nonzero_protocol_id_is_rejected(self):
        """MBAP Protocol ID (header bytes 2-3) must always be zero per the
        Modbus TCP spec - a nonzero value means the header can't be trusted,
        even if everything else about the frame looks fine."""
        pdu = fc03_response_pdu([10, 20])
        frame = (
            (1).to_bytes(2, "big")               # Transaction ID
            + (1).to_bytes(2, "big")              # Protocol ID = 1 - invalid
            + (1 + len(pdu)).to_bytes(2, "big")   # Length
            + SLAVE_ID.to_bytes(1, "big")
            + pdu
        )
        client, created = make_client([frame])

        result = client.read_registers(0x1000, 2)

        assert result is None
        assert client.malformed_headers == 1
        assert created[0].closed is True

    def test_byte_count_not_matching_requested_count_is_rejected(self):
        """Declared byte_count is internally consistent with the frame's
        own length (so the existing frame-length check above doesn't catch
        it), but doesn't match what was actually requested - e.g. the
        device answered with fewer registers than asked for."""
        data = (10).to_bytes(2, "big")  # only 1 register's worth
        pdu = FC_READ_HOLDING.to_bytes(1, "big") + (2).to_bytes(1, "big") + data
        frame = build_frame(tid=1, unit_id=SLAVE_ID, pdu=pdu)
        client, created = make_client([frame])

        result = client.read_registers(0x1000, 2)  # requested 2 registers (4 bytes)

        assert result is None
        assert client.short_reads == 1
        assert created[0].closed is True

    def test_malformed_header_length_is_rejected(self):
        """A Length field outside what any real Modbus TCP frame can
        legitimately declare - must fail fast, not try to recv() a bogus
        number of bytes."""
        frame = (
            (1).to_bytes(2, "big")       # Transaction ID
            + (0).to_bytes(2, "big")     # Protocol ID
            + (0).to_bytes(2, "big")     # Length = 0 - invalid (< MIN_MBAP_LENGTH)
            + SLAVE_ID.to_bytes(1, "big")
        )
        client, _ = make_client([frame])

        result = client.read_registers(0x1000, 1)

        assert result is None
        assert client.malformed_headers == 1


class TestWriteEcho:
    def test_write_single_echo_mismatch_is_a_failure(self):
        """FC06 response structurally fine, but echoes back the wrong value -
        must not be treated as a successful write."""
        wrong_echo_pdu = (
            FC_WRITE_SINGLE.to_bytes(1, "big")
            + (0x4001).to_bytes(2, "big")
            + (999).to_bytes(2, "big")  # we're about to write 1, this echoes 999
        )
        frame = build_frame(tid=1, unit_id=SLAVE_ID, pdu=wrong_echo_pdu)
        client, _ = make_client([frame])

        success = client.write_holding_register(0x4001, 1)

        assert success is False
        assert client.write_echo_mismatches == 1

    def test_write_single_correct_echo_succeeds(self):
        pdu = (
            FC_WRITE_SINGLE.to_bytes(1, "big")
            + (0x4001).to_bytes(2, "big")
            + (1).to_bytes(2, "big")
        )
        frame = build_frame(tid=1, unit_id=SLAVE_ID, pdu=pdu)
        client, _ = make_client([frame])

        assert client.write_holding_register(0x4001, 1) is True
        assert client.write_echo_mismatches == 0

    def test_write_multiple_echo_mismatch_is_a_failure(self):
        """FC10 response echoes the wrong address - must not be treated as
        a successful write, even though the frame is otherwise well-formed."""
        wrong_echo_pdu = (
            FC_WRITE_MULTIPLE.to_bytes(1, "big")
            + (0x3099).to_bytes(2, "big")  # we're writing to 0x3001, not 0x3099
            + (1).to_bytes(2, "big")
        )
        frame = build_frame(tid=1, unit_id=SLAVE_ID, pdu=wrong_echo_pdu)
        client, _ = make_client([frame])

        success = client.write_holding_register(0x3001, 100)

        assert success is False
        assert client.write_echo_mismatches == 1

    def test_write_multiple_correct_echo_succeeds(self):
        pdu = (
            FC_WRITE_MULTIPLE.to_bytes(1, "big")
            + (0x3001).to_bytes(2, "big")
            + (1).to_bytes(2, "big")
        )
        frame = build_frame(tid=1, unit_id=SLAVE_ID, pdu=pdu)
        client, _ = make_client([frame])

        assert client.write_holding_register(0x3001, 100) is True
        assert client.write_echo_mismatches == 0


class TestReconnectAfterFramingFailure:
    def test_next_call_gets_a_fresh_socket_after_a_txid_mismatch(self):
        bad_frame = build_frame(tid=999, unit_id=SLAVE_ID, pdu=fc03_response_pdu([10]))
        # The client's TID counter advances on every request: the first call
        # sends tid=1 (rejected above as a mismatch), the second sends tid=2.
        good_frame = build_frame(tid=2, unit_id=SLAVE_ID, pdu=fc03_response_pdu([10]))
        client, created = make_client([bad_frame], extra_socket_chunks=[[good_frame]])

        first = client.read_registers(0x1000, 1)
        assert first is None
        assert len(created) == 1
        assert created[0].closed is True

        # Second call must reconnect (a new socket.create_connection()) -
        # not try to reuse the closed/desynced one.
        second = client.read_registers(0x1000, 1)

        assert second == [10]
        assert len(created) == 2  # a fresh socket was created for the retry

    def test_short_read_closes_connection_for_next_reconnect(self):
        client, created = make_client([b"\x00\x01\x00"])  # incomplete header

        result = client.read_registers(0x1000, 1)

        assert result is None
        assert created[0].closed is True
