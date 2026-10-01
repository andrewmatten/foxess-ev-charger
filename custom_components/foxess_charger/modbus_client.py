"""Modbus TCP client for FoxESS EV Charger.

Two API levels share one framing implementation:

- Strict API (`read_registers_strict`, `write_register_strict`): raises
  `ModbusExceptionResponse` for a definite device refusal and
  `ModbusNoResponse` when no valid answer was obtained. Used by the async
  transport, which must tell "refused" apart from "outcome unknown".
- Legacy API (`read_registers`, `read_uint32`, `read_ascii`,
  `write_holding_register`): the original None/bool contract, implemented on
  top of the strict API.

Every transaction runs against one absolute deadline covering connect, send
and the complete response. Per-recv timeouts alone would let a peer that
dribbles one byte just inside each timeout stretch a frame indefinitely.
"""
from __future__ import annotations

import logging
import socket
import threading
import time

_LOGGER = logging.getLogger(__name__)

# ── Modbus Function Codes ─────────────────────────────────────────────────────
FC_READ_HOLDING     = 0x03   # read R/W and read-only registers
FC_WRITE_SINGLE     = 0x06   # write the write-only command registers (0x4000–0x4003)
FC_WRITE_MULTIPLE   = 0x10   # write R/W config registers (0x3000–0x300B)

# Write-only registers accept FC06 only; everything else is written with FC16.
WRITE_ONLY_REGISTERS = {0x4000, 0x4001, 0x4002, 0x4003}

# MBAP header is always exactly 7 bytes: Transaction ID(2) + Protocol ID(2) +
# Length(2) + Unit ID(1). The Length field counts everything *after* itself,
# i.e. Unit ID + PDU.
MBAP_HEADER_LEN = 7

# Sane bounds on the MBAP Length field: Unit ID plus at least a 1-byte PDU,
# and a PDU is at most 253 bytes. Anything outside means a corrupt header;
# reading what it claims would turn one bad byte into a hung connection.
MIN_MBAP_LENGTH = 2
MAX_MBAP_LENGTH = 254

# Max registers per FC03 request per the Modbus spec.
MAX_READ_COUNT = 125

DEFAULT_TRANSACTION_TIMEOUT = 5.0


class ModbusError(Exception):
    """Base class for strict-API failures."""


class ModbusExceptionResponse(ModbusError):
    """The device answered our exact request with a Modbus exception PDU.

    A definite refusal: the frame was complete, matched our TID and unit, and
    carried our own function code with the exception bit set.
    """

    def __init__(self, function_code: int, code: int, address: int) -> None:
        super().__init__(
            f"Modbus exception 0x{code:02X} for FC 0x{function_code:02X} "
            f"@ 0x{address:04X}"
        )
        self.function_code = function_code
        self.code = code
        self.address = address


class ModbusNoResponse(ModbusError):
    """No valid answer to our request was obtained.

    `request_sent` is False only when the failure happened before any request
    byte could have reached the device (connection could not be opened). In
    every other case a write may or may not have been applied.
    """

    def __init__(self, message: str, *, request_sent: bool) -> None:
        super().__init__(message)
        self.request_sent = request_sent


class _DeadlineExceeded(OSError):
    """Whole-transaction deadline elapsed while waiting for bytes."""


class _NoReplyYet(OSError):
    """Deadline elapsed without a single byte of a reply arriving.

    Distinct from a partial/garbled frame: nothing is known to be wrong with
    the connection, the peer may simply still be working on the request. The
    late reply, if it ever comes, is still readable as a well-formed (if
    stale) frame by whichever transaction reads next - closing the socket
    here would only throw that frame away along with a socket that didn't
    need replacing.
    """


def _check_u16(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} out of range: {value}")


class FoxESSModbusClient:
    """Minimal Modbus TCP client over raw sockets.

    Holds one persistent TCP connection reused across calls; a poll issues
    several reads and reconnecting for each would load the charger's embedded
    stack. A lock serializes whole transactions.

    A reply carrying an older Transaction ID than the one just sent is a
    late answer to a request this client already gave up on - it is
    discarded (counted in `stale_replies`) and reading continues, still
    within the same deadline, for the reply that actually matches. Likewise,
    a deadline that elapses with no bytes of a reply received yet leaves the
    connection open, on the chance the peer is just slow: closing it would
    only guarantee the next transaction has to reconnect *and* still risks
    meeting that same late reply as a mismatch. Every other framing or
    socket failure - a malformed header, a wrong Unit ID, a frame that
    stops arriving partway through, an actual socket error - closes the
    socket so the next call starts on a clean stream.
    """

    def __init__(
        self, host: str, port: int, slave_id: int,
        timeout: float = DEFAULT_TRANSACTION_TIMEOUT,
    ) -> None:
        self._host      = host
        self._port      = port
        self._slave_id  = slave_id
        self._timeout   = timeout
        self._tid       = 0
        self._sock: socket.socket | None = None
        self._lock       = threading.Lock()

        # Transport-health counters surfaced by diagnostics.
        self.txid_mismatches     = 0
        # Header or body never fully arrived (peer closed, or the deadline
        # ran out mid-frame), or FC03 byte_count inconsistent with the frame
        # or the request.
        self.short_reads         = 0
        self.connection_errors   = 0
        # Header arrived but declares an impossible Length/Protocol ID.
        self.malformed_headers   = 0
        self.unit_id_mismatches  = 0
        # Structurally valid write reply that does not echo what was sent.
        self.write_echo_mismatches = 0
        # A well-formed reply for an older, already-abandoned transaction,
        # discarded while waiting for the one that matches. Distinct from
        # txid_mismatches, which is a frame that can't be explained as a
        # late reply and forces a reconnect.
        self.stale_replies       = 0

    # ── Framing ───────────────────────────────────────────────────────────────

    def _next_tid(self) -> int:
        self._tid = (self._tid + 1) % 0xFFFF
        return self._tid

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExceeded("transaction deadline exceeded")
        return remaining

    def _recv_exact(
        self, sock: socket.socket, n: int, deadline: float, *, no_reply_ok: bool = False,
    ) -> bytes | None:
        """Reads exactly `n` bytes before `deadline`.

        TCP is a byte stream, so a frame may arrive over many recv() calls.
        The socket timeout is re-armed to the time left on the transaction
        before every recv(), so the total can never exceed the deadline.
        Returns None if the peer closes mid-frame.

        `no_reply_ok` covers only the read that starts a fresh frame (the
        MBAP header): a timeout with not one byte of it collected yet means
        no reply has arrived at all, raised as `_NoReplyYet` rather than a
        plain timeout. Once a header has actually been read, the frame has
        started arriving - a timeout partway through its body is a stalled,
        unreliable stream, not "no reply", regardless of how many bytes of
        the body happen to be in `buf` at that point.
        """
        buf = bytearray()
        while len(buf) < n:
            try:
                sock.settimeout(self._remaining(deadline))
                chunk = sock.recv(n - len(buf))
            except (_DeadlineExceeded, TimeoutError) as ex:
                if no_reply_ok and not buf:
                    raise _NoReplyYet(str(ex)) from ex
                raise
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    @staticmethod
    def _is_stale_tid(got_tid: int, sent_tid: int) -> bool:
        """True if `got_tid` is an earlier, already-abandoned transaction's
        TID rather than an unexplained one.

        TIDs increase by 1 per request (wrapping mod 0x10000), so a late
        reply to a request this client gave up on trails the one just sent
        by a small distance. Anything on the *other* side of that distance -
        a TID supposedly ahead of the one just allocated - cannot be a late
        reply to anything this client sent, so it is treated as unexpected
        instead (and still resets the connection).
        """
        return 0 < (sent_tid - got_tid) % 0x10000 < 0x8000

    def _build_mbap(self, pdu: bytes) -> bytes:
        tid     = self._next_tid()
        length  = 1 + len(pdu)
        return (
            tid.to_bytes(2, "big")
            + (0).to_bytes(2, "big")
            + length.to_bytes(2, "big")
            + self._slave_id.to_bytes(1, "big")
            + pdu
        )

    def _fail(self, message: str, *, sent: bool, counter: str | None = None,
              level: int = logging.WARNING, close: bool = True) -> ModbusNoResponse:
        if counter:
            setattr(self, counter, getattr(self, counter) + 1)
        _LOGGER.log(level, "Modbus TCP %s:%s – %s – %s",
                    self._host, self._port, message,
                    "resetting connection" if close else "keeping connection open")
        if close:
            self._close()
        return ModbusNoResponse(message, request_sent=sent)

    def _transact(self, pdu: bytes, timeout: float | None) -> bytes:
        """Sends one PDU and returns the complete response frame.

        Validates everything available at frame level: Protocol ID, Length,
        Transaction ID and Unit ID. The TID is allocated inside the lock so
        concurrent callers can never send the same TID. Must be called with
        the lock held.

        A reply bearing an older TID than the one just sent is a late
        answer to a transaction this client already gave up on; it is
        dropped (its declared length still fully read off the wire, so the
        stream stays aligned) and reading continues, within the same
        deadline, for a reply that actually matches.
        """
        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
        request = self._build_mbap(pdu)
        sent_tid = int.from_bytes(request[:2], "big")
        sent = False
        try:
            if self._sock is None:
                self._sock = socket.create_connection(
                    (self._host, self._port), timeout=self._remaining(deadline),
                )
            sock = self._sock
            sock.settimeout(self._remaining(deadline))
            # From here on, some or all of the request may have been delivered.
            sent = True
            sock.sendall(request)

            while True:
                header = self._recv_exact(sock, MBAP_HEADER_LEN, deadline, no_reply_ok=True)
                if header is None:
                    raise self._fail("connection closed before a complete MBAP header",
                                     sent=True, counter="short_reads")

                protocol_id = int.from_bytes(header[2:4], "big")
                if protocol_id != 0:
                    raise self._fail(f"nonzero MBAP Protocol ID 0x{protocol_id:04X}",
                                     sent=True, counter="malformed_headers",
                                     level=logging.ERROR)
                length = int.from_bytes(header[4:6], "big")
                if not MIN_MBAP_LENGTH <= length <= MAX_MBAP_LENGTH:
                    raise self._fail(f"invalid MBAP Length {length}", sent=True,
                                     counter="malformed_headers", level=logging.ERROR)

                body = self._recv_exact(sock, length - 1, deadline)
                if body is None:
                    raise self._fail(f"connection closed before the {length - 1}-byte body",
                                     sent=True, counter="short_reads")

                got_tid = int.from_bytes(header[:2], "big")
                if got_tid != sent_tid:
                    if self._is_stale_tid(got_tid, sent_tid):
                        self.stale_replies += 1
                        _LOGGER.debug(
                            "Modbus TCP %s:%s – stale reply for an abandoned "
                            "transaction (sent %04X, got %04X) – discarding, "
                            "still waiting for %04X",
                            self._host, self._port, sent_tid, got_tid, sent_tid)
                        continue
                    raise self._fail(
                        f"Transaction ID mismatch (sent {sent_tid:04X}, got {got_tid:04X})",
                        sent=True, counter="txid_mismatches")
                if header[6] != self._slave_id:
                    raise self._fail(
                        f"Unit ID mismatch (sent {self._slave_id}, got {header[6]})",
                        sent=True, counter="unit_id_mismatches")
                return header + body
        except ModbusNoResponse:
            raise
        except _NoReplyYet as ex:
            raise self._fail(f"no reply within the deadline: {ex}", sent=sent,
                             counter="short_reads", close=False) from ex
        except _DeadlineExceeded as ex:
            counter = "short_reads" if sent else "connection_errors"
            raise self._fail(f"deadline exceeded: {ex}", sent=sent, counter=counter) from ex
        except (OSError, ValueError) as ex:
            raise self._fail(f"connection error: {ex}", sent=sent,
                             counter="connection_errors", level=logging.ERROR) from ex

    def _check_exception(self, response: bytes, function_code: int, address: int) -> None:
        """Raises ModbusExceptionResponse for a well-formed exception reply to
        our own function code. The exception PDU is exactly 2 bytes."""
        if len(response) == 9 and response[7] == (function_code | 0x80):
            raise ModbusExceptionResponse(function_code, response[8], address)

    def _send_recv(self, pdu: bytes, timeout: float = DEFAULT_TRANSACTION_TIMEOUT) -> bytes | None:
        """Legacy helper: complete validated frame, or None on any failure."""
        with self._lock:
            try:
                return self._transact(pdu, timeout)
            except ModbusNoResponse:
                return None

    # ── Strict API ────────────────────────────────────────────────────────────

    def read_registers_strict(
        self, address: int, count: int, *, timeout: float | None = None,
    ) -> list[int]:
        """FC03 read of `count` holding registers from `address`.

        Always a fresh wire request. Raises ModbusExceptionResponse or
        ModbusNoResponse.
        """
        _check_u16("address", address)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_READ_COUNT:
            raise ValueError(f"invalid register count: {count!r}")
        pdu = bytes([FC_READ_HOLDING]) + address.to_bytes(2, "big") + count.to_bytes(2, "big")
        with self._lock:
            response = self._transact(pdu, timeout)
            self._check_exception(response, FC_READ_HOLDING, address)
            if len(response) < 9 or response[7] != FC_READ_HOLDING:
                raise self._fail(
                    f"FC03 @ 0x{address:04X}: unexpected reply PDU {response[7:].hex()}",
                    sent=True)
            byte_count = response[8]
            if len(response) != 9 + byte_count or byte_count != count * 2:
                # Either the device's byte_count contradicts its own MBAP
                # length, or it answered with a different register count than
                # requested. Slicing by fixed offsets would misalign data.
                raise self._fail(
                    f"FC03 @ 0x{address:04X}: byte_count {byte_count} inconsistent "
                    f"(frame {len(response)} bytes, requested {count} registers)",
                    sent=True, counter="short_reads")
        payload = response[9:]
        registers = [int.from_bytes(payload[i:i + 2], "big") for i in range(0, byte_count, 2)]
        _LOGGER.debug("FC03 Read 0x%04X count=%d → %s", address, count, registers)
        return registers

    def write_register_strict(
        self, address: int, value: int, *, timeout: float | None = None,
    ) -> None:
        """Writes one register: FC06 for write-only command registers, FC16
        (quantity 1) for R/W registers.

        Returning means the device acknowledged the request with a correct
        echo. That says nothing about whether the value took effect.
        Raises ModbusExceptionResponse (definite refusal) or ModbusNoResponse
        (outcome unknown unless `request_sent` is False).
        """
        _check_u16("address", address)
        _check_u16("value", value)
        if address in WRITE_ONLY_REGISTERS:
            fc = FC_WRITE_SINGLE
            pdu = bytes([fc]) + address.to_bytes(2, "big") + value.to_bytes(2, "big")
            expected_echo = pdu
        else:
            fc = FC_WRITE_MULTIPLE
            pdu = (bytes([fc]) + address.to_bytes(2, "big") + (1).to_bytes(2, "big")
                   + bytes([2]) + value.to_bytes(2, "big"))
            # FC16 echoes function, address and quantity, not the value.
            expected_echo = pdu[:5]
        with self._lock:
            response = self._transact(pdu, timeout)
            self._check_exception(response, fc, address)
            if response[7:] != expected_echo:
                raise self._fail(
                    f"FC{fc:02X} write 0x{address:04X}={value}: reply does not echo "
                    f"the request ({response[7:].hex()})",
                    sent=True, counter="write_echo_mismatches")
        _LOGGER.debug("FC%02X Write 0x%04X = %d acknowledged", fc, address, value)

    # ── Legacy API ────────────────────────────────────────────────────────────

    def read_registers(self, address: int, count: int, quiet: bool = False) -> list[int] | None:
        """FC03 read; None on any failure.

        `quiet=True` logs an exception response at DEBUG, for reads expected
        to be refused on some hardware (phase-switch-box on single-phase units).
        """
        try:
            return self.read_registers_strict(address, count)
        except ModbusExceptionResponse as ex:
            (_LOGGER.debug if quiet else _LOGGER.error)(
                "Modbus FC03 Exception 0x%02X @ 0x%04X", ex.code, address)
            return None
        except ModbusNoResponse:
            return None

    def read_uint32(self, address: int) -> int | None:
        """Reads a big-endian UINT32 from two consecutive registers."""
        regs = self.read_registers(address, 2)
        if regs and len(regs) >= 2:
            return (regs[0] << 16) | regs[1]
        return None

    def read_ascii(self, address: int, reg_count: int) -> str | None:
        """Reads `reg_count` registers as ASCII (two chars per register,
        big-endian, NUL-padded) - e.g. model code 0x101E, serial 0x1022."""
        regs = self.read_registers(address, reg_count)
        if regs is None:
            return None
        return decode_ascii(regs)

    def write_holding_register(self, address: int, value: int) -> bool:
        """Writes one register (FC06 for 0x4000–0x4003, FC16 otherwise).
        True only for a correctly echoed acknowledgement."""
        try:
            self.write_register_strict(address, value)
            return True
        except ModbusExceptionResponse as ex:
            _LOGGER.error("FC%02X Exception 0x%02X @ 0x%04X value=%d",
                          ex.function_code, ex.code, address, value)
            return False
        except ModbusNoResponse:
            return False

    def disconnect(self) -> None:
        """Closes the persistent connection; waits for any in-flight transaction."""
        with self._lock:
            self._close()


def decode_ascii(regs: list[int] | tuple[int, ...]) -> str:
    raw = bytearray()
    for reg in regs:
        raw.append((reg >> 8) & 0xFF)
        raw.append(reg & 0xFF)
    return raw.decode("ascii", errors="replace").rstrip("\x00").strip()
