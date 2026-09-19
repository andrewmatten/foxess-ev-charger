"""Modbus TCP client for FoxESS EV Charger."""
from __future__ import annotations

import logging
import socket
import threading

_LOGGER = logging.getLogger(__name__)

# ── Modbus Function Codes ─────────────────────────────────────────────────────
FC_READ_HOLDING     = 0x03   # Lesen R/W und R-Only Register
FC_WRITE_SINGLE     = 0x06   # Schreiben W-Only Register  (0x4000–0x4003)
FC_WRITE_MULTIPLE   = 0x10   # Schreiben R/W Register     (0x3000–0x300B)

# ── W-Only Register Adressen (FC 0x06) ───────────────────────────────────────
WRITE_ONLY_REGISTERS = {0x4000, 0x4001, 0x4002, 0x4003}

# MBAP header is always exactly 7 bytes: Transaction ID(2) + Protocol ID(2) +
# Length(2) + Unit ID(1). The Length field counts everything *after* itself,
# i.e. Unit ID + PDU.
MBAP_HEADER_LEN = 7

# Sane bounds on the MBAP Length field: it must cover at least the Unit ID
# plus a 1-byte PDU (2), and per the Modbus TCP spec a PDU is at most 253
# bytes, so Unit ID + PDU is at most 254. A value outside this range means
# the header itself is corrupt - trying to read whatever it claims (which
# could be enormous, or zero) is how a single bad byte turns into a hung
# connection instead of a clean, fast failure.
MIN_MBAP_LENGTH = 2
MAX_MBAP_LENGTH = 254


class FoxESSModbusClient:
    """Minimal Modbus TCP client (raw sockets, kein pymodbus).

    Holds one persistent TCP connection, reused across reads/writes, instead
    of opening a fresh connection per call. A single poll cycle issues several
    register reads - reconnecting for every one of them adds needless TCP
    handshake overhead and load on the charger's embedded stack. The socket
    is protected by a lock because reads (from the coordinator's poll loop)
    and writes (from switch/number/select entities) run as independent
    executor jobs and could otherwise land on different threads at once.
    Any send/recv failure closes and clears the socket so the next call
    reconnects cleanly rather than reusing a broken connection.
    """

    def __init__(self, host: str, port: int, slave_id: int) -> None:
        self._host      = host
        self._port      = port
        self._slave_id  = slave_id
        self._tid       = 0
        self._sock: socket.socket | None = None
        self._lock       = threading.Lock()

        # Transport-health counters. The 2026-08/09 desync was invisible until
        # it had already corrupted long-term statistics - the live values
        # looked fine the whole time. These are surfaced as a diagnostics
        # sensor so "are the transport fixes actually firing?" is one glance
        # rather than an archaeology session through the logs.
        self.txid_mismatches     = 0
        self.short_reads         = 0
        self.connection_errors   = 0
        # A single recv(1024) call used to be trusted to return one complete
        # frame - TCP doesn't guarantee that, so short_reads covers both "the
        # MBAP header itself never fully arrived" and "the PDU body never
        # fully arrived" (see _recv_exact / _send_recv below).
        #
        # malformed_headers is a distinct failure: the header DID arrive
        # (7 bytes), but its declared Length field is outside what a real
        # Modbus TCP frame can ever legitimately claim - a corrupt header,
        # not a truncated read. Counted separately because "we gave up
        # trying to read a bogus giant/zero length" is a meaningfully
        # different transport fault than "the peer went away mid-frame".
        self.malformed_headers   = 0
        # The Unit ID byte in the response doesn't match the slave ID we
        # addressed the request to - same crossed-wires risk as a
        # Transaction ID mismatch, just a different field.
        self.unit_id_mismatches  = 0
        # A write whose response frame is structurally fine (right length,
        # right Transaction ID, no exception bit) but doesn't actually echo
        # back the function code/address/value we sent - i.e. it "looks"
        # successful but doesn't correspond to the write that was issued.
        self.write_echo_mismatches = 0

    # ── Interne Hilfsmethoden ─────────────────────────────────────────────────

    def _next_tid(self) -> int:
        self._tid = (self._tid + 1) % 0xFFFF
        return self._tid

    def _ensure_connected(self, timeout: float) -> socket.socket:
        if self._sock is None:
            self._sock = socket.create_connection((self._host, self._port), timeout=timeout)
        else:
            self._sock.settimeout(timeout)
        return self._sock

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
        """Reads exactly `n` bytes, looping recv() until satisfied.

        A single recv() call is never assumed to return a complete frame -
        TCP is a byte stream, not a message stream, and the response can
        legitimately arrive split across multiple recv() calls. Returns None
        if the peer closes the connection (recv() returning an empty bytes
        object) before `n` bytes have arrived. Socket-level exceptions
        (timeout, connection reset, etc.) are deliberately left to propagate
        to the caller rather than being swallowed here.
        """
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None  # peer closed mid-frame
            buf.extend(chunk)
        return bytes(buf)

    def _send_recv(self, pdu: bytes, timeout: float = 5.0) -> bytes | None:
        """Sends one PDU and returns the complete, validated raw response frame.

        The MBAP header (and with it the Transaction ID) is built *inside* the
        lock. It used to be built by the caller, before the lock was taken -
        which meant two threads could interleave inside _next_tid() and both
        send the same TID. Serialising send/recv made that harmless in
        practice, but it silently weakened the exact TID check added to fix
        the 2026-08/09 cross-wiring incidents, and would have become a real
        bug the moment anything pipelined requests.

        Reads the 7-byte MBAP header first, extracts the declared Length
        field, then reads exactly that many more bytes - never trusts a
        single recv() to have returned a whole frame. Any framing failure
        (incomplete header, an invalid declared length, an incomplete body,
        or a Transaction ID mismatch) closes the connection via _close() so
        the next call reconnects cleanly instead of reusing a stream that
        might have a stale frame sitting in it.
        """
        with self._lock:
            request = self._build_mbap(pdu)
            try:
                sock = self._ensure_connected(timeout)
                sock.sendall(request)
                header = self._recv_exact(sock, MBAP_HEADER_LEN)
            except Exception as ex:
                self.connection_errors += 1
                _LOGGER.error("Modbus TCP %s:%s – Verbindungsfehler: %s", self._host, self._port, ex)
                self._close()
                return None

            if header is None:
                self.short_reads += 1
                _LOGGER.warning(
                    "Modbus TCP %s:%s – connection closed before a complete "
                    "MBAP header arrived – resetting connection",
                    self._host, self._port,
                )
                self._close()
                return None

            # MBAP Protocol ID (header bytes 2-3) must always be zero per the
            # Modbus TCP spec - it's the field that distinguishes Modbus
            # from other protocols that might share a TCP framing layout.
            # Checked as its own case (not folded into the Transaction ID/
            # Unit ID crossed-wires checks below, which need the full frame)
            # since it's available the moment the header itself arrives, and
            # a nonzero value here means the rest of the header can't be
            # trusted either - the same "reset the connection, don't guess"
            # treatment as any other corrupt-header case.
            protocol_id = int.from_bytes(header[2:4], "big")
            if protocol_id != 0:
                self.malformed_headers += 1
                _LOGGER.error(
                    "Modbus TCP %s:%s – MBAP header declares a nonzero "
                    "Protocol ID (0x%04X, expected 0x0000 per the Modbus "
                    "TCP spec) – resetting connection",
                    self._host, self._port, protocol_id,
                )
                self._close()
                return None

            length = int.from_bytes(header[4:6], "big")
            if not (MIN_MBAP_LENGTH <= length <= MAX_MBAP_LENGTH):
                self.malformed_headers += 1
                _LOGGER.error(
                    "Modbus TCP %s:%s – MBAP header declares an invalid "
                    "Length field (%d, expected %d–%d) – resetting connection",
                    self._host, self._port, length, MIN_MBAP_LENGTH, MAX_MBAP_LENGTH,
                )
                self._close()
                return None

            try:
                body = self._recv_exact(sock, length - 1)  # Unit ID already in header
            except Exception as ex:
                self.connection_errors += 1
                _LOGGER.error("Modbus TCP %s:%s – Verbindungsfehler: %s", self._host, self._port, ex)
                self._close()
                return None

            if body is None:
                self.short_reads += 1
                _LOGGER.warning(
                    "Modbus TCP %s:%s – connection closed before the declared "
                    "%d-byte frame body arrived – resetting connection",
                    self._host, self._port, length - 1,
                )
                self._close()
                return None

            response = header + body

            # MBAP Transaction ID must echo what we sent. Root cause of the
            # 2026-08-27/09-02 bad-energy-read incidents: two back-to-back
            # reads (total_energy then current_energy) got their responses
            # crossed around a charger fault - current_energy briefly showed
            # total_energy's real value, with no error anywhere, because
            # nothing here ever checked which request a response belonged
            # to. Reset the connection (not just discard this one response)
            # since once a stream is desynced, the next recv() is just as
            # likely to be another stale frame from the same backlog. This
            # check now runs against a fully-read frame, never a partial one.
            if response[:2] != request[:2]:
                self.txid_mismatches += 1
                _LOGGER.warning(
                    "Modbus TCP %s:%s – Transaction ID mismatch (sent %s, got %s) – "
                    "resetting connection",
                    self._host, self._port, request[:2].hex(), response[:2].hex(),
                )
                self._close()
                return None

            # Unit ID must also match what we sent - same crossed-wires risk
            # as a Transaction ID mismatch (e.g. a shared gateway routing a
            # response meant for a different slave), just a different field.
            if response[6] != self._slave_id:
                self.unit_id_mismatches += 1
                _LOGGER.warning(
                    "Modbus TCP %s:%s – Unit ID mismatch (sent %d, got %d) – "
                    "resetting connection",
                    self._host, self._port, self._slave_id, response[6],
                )
                self._close()
                return None

            return response

    def _build_mbap(self, pdu: bytes) -> bytes:
        """Baut den vollständigen Modbus TCP ADU (MBAP + PDU)."""
        tid     = self._next_tid()
        length  = 1 + len(pdu)   # Unit ID (1) + PDU
        return (
            tid.to_bytes(2, "big")              +  # Transaction ID
            (0).to_bytes(2, "big")              +  # Protocol ID
            length.to_bytes(2, "big")           +  # Length
            self._slave_id.to_bytes(1, "big")   +  # Unit ID
            pdu
        )

    # ── Öffentliche Methoden ──────────────────────────────────────────────────

    def read_registers(self, address: int, count: int, quiet: bool = False) -> list[int] | None:
        """Liest `count` Holding-Register ab `address` (FC 0x03).

        `quiet=True` logs an exception response at DEBUG instead of ERROR -
        for reads that are expected to fail on some hardware (e.g. phase-
        switch-box registers on single-phase units), so a normal condition
        doesn't spam the log at ERROR level forever.
        """
        pdu = (
            FC_READ_HOLDING.to_bytes(1, "big") +
            address.to_bytes(2, "big")         +
            count.to_bytes(2, "big")
        )
        response = self._send_recv(pdu)
        if response is None:
            return None

        # By this point _send_recv has already guaranteed `response` is a
        # complete frame matching its own declared MBAP length - no more
        # "recv() returned a partial frame" cases to guard against here.
        # What's left is validating the PDU's own content.

        # Modbus Exception prüfen
        if len(response) >= 9 and response[7] == (FC_READ_HOLDING | 0x80):
            log = _LOGGER.debug if quiet else _LOGGER.error
            log("Modbus FC03 Exception 0x%02X @ 0x%04X", response[8], address)
            return None

        if len(response) < 9:
            # A well-formed-but-abnormally-short response - the frame is
            # complete per its own header, so this isn't a transport
            # truncation (no need to reset the connection), just a reply
            # that doesn't contain enough PDU to be a valid FC03 answer.
            _LOGGER.warning(
                "FC03: response too short to be valid (%d bytes) @ 0x%04X",
                len(response), address,
            )
            return None

        if response[7] != FC_READ_HOLDING:
            # Not an exception (checked above) and not an FC03 echo either -
            # an unexpected function code in an otherwise well-formed frame.
            # Trusting response[8] as byte_count here without this check
            # would decode whatever this PDU actually is as if it were
            # register data.
            _LOGGER.warning(
                "FC03: unexpected function code 0x%02X in response @ 0x%04X",
                response[7], address,
            )
            return None

        byte_count = response[8]
        if len(response) != 9 + byte_count:
            # The device's own declared byte_count disagrees with the frame
            # length its own MBAP header declared - an internal inconsistency
            # in the device's response, not a TCP-level truncation (the
            # loop-based read above already guarantees the frame is exactly
            # as long as advertised). Reset the connection out of the same
            # caution as a Transaction ID mismatch: this is either a device
            # bug or a symptom of desync, and it isn't safe to guess which
            # bytes are actually the registers.
            self.short_reads += 1
            _LOGGER.warning(
                "FC03: declared byte_count disagrees with frame length @ 0x%04X "
                "(frame=%d bytes, byte_count=%d) – resetting connection",
                address, len(response), byte_count,
            )
            self.disconnect()
            return None

        # The declared byte_count can be internally consistent with the
        # frame's own length (checked above) while still not matching what
        # was actually *requested* - e.g. the device answered with fewer (or
        # an odd number of) registers than asked for. Trusting it blindly
        # would silently hand back a shorter/misaligned register list to a
        # caller that indexes into it by a fixed offset (see _fetch() in
        # __init__.py, which slices this batch by address), so this is the
        # same "reject and reconnect" treatment as any other framing
        # inconsistency, not just a length mismatch to shrug off.
        if byte_count % 2 != 0 or byte_count != count * 2:
            self.short_reads += 1
            _LOGGER.warning(
                "FC03: declared byte_count %d doesn't match the %d "
                "registers actually requested (expected %d bytes) @ 0x%04X "
                "– resetting connection",
                byte_count, count, count * 2, address,
            )
            self.disconnect()
            return None

        payload    = response[9: 9 + byte_count]
        registers  = [int.from_bytes(payload[i:i+2], "big") for i in range(0, byte_count, 2)]
        _LOGGER.debug("FC03 Read 0x%04X count=%d → %s", address, count, registers)
        return registers

    def read_uint32(self, address: int) -> int | None:
        """Liest einen UINT32-Wert aus zwei aufeinanderfolgenden Registern."""
        regs = self.read_registers(address, 2)
        if regs and len(regs) >= 2:
            return (regs[0] << 16) | regs[1]
        return None

    def read_ascii(self, address: int, reg_count: int) -> str | None:
        """Liest `reg_count` Register ab `address` und dekodiert sie als
        ASCII-String (zwei Zeichen pro Register, big-endian, rechtsseitig
        mit Nullbytes aufgefüllt - z.B. Id Model Code 0x101E, Id Serial
        Number 0x1022)."""
        regs = self.read_registers(address, reg_count)
        if regs is None:
            return None
        raw = bytearray()
        for reg in regs:
            raw.append((reg >> 8) & 0xFF)
            raw.append(reg & 0xFF)
        return raw.decode("ascii", errors="replace").rstrip("\x00").strip()

    def write_holding_register(self, address: int, value: int) -> bool:
        """
        Schreibt ein einzelnes Register.

        R/W Register (0x3000–0x300B) → FC 0x10 (Write Multiple Registers)
        W-Only Register (0x4000–0x4003) → FC 0x06 (Write Single Register)
        """
        if address in WRITE_ONLY_REGISTERS:
            return self._write_single(address, value)
        else:
            return self._write_multiple(address, value)

    # ── Private Write-Methoden ────────────────────────────────────────────────

    def _verify_write_echo(
        self, response: bytes, function_code: int, address: int,
        second_field: int, second_field_name: str,
    ) -> bool:
        """Confirms a write response's PDU actually echoes the function
        code, register address, and second field (value for FC06, quantity
        for FC10) that were sent - not just a same-length reply that happens
        to look right. Per spec both FC06 and FC10 echo these fields back
        verbatim on success; a response that's structurally valid (right
        length, right Transaction ID) but doesn't match what was sent is a
        real anomaly worth surfacing rather than treating as success.
        """
        if len(response) < 12:
            return False
        if response[7] != function_code:
            _LOGGER.warning(
                "Write echo mismatch @ 0x%04X: expected function code 0x%02X, got 0x%02X",
                address, function_code, response[7],
            )
            return False
        echoed_address = int.from_bytes(response[8:10], "big")
        echoed_second  = int.from_bytes(response[10:12], "big")
        if echoed_address != address:
            _LOGGER.warning(
                "Write echo mismatch: expected address 0x%04X, got 0x%04X",
                address, echoed_address,
            )
            return False
        if echoed_second != second_field:
            _LOGGER.warning(
                "Write echo mismatch @ 0x%04X: expected %s=%d, got %d",
                address, second_field_name, second_field, echoed_second,
            )
            return False
        return True

    def _write_single(self, address: int, value: int) -> bool:
        """FC 0x06 – Write Single Register (W-Only Register 0x4000–0x4003)."""
        pdu = (
            FC_WRITE_SINGLE.to_bytes(1, "big") +
            address.to_bytes(2, "big")         +
            value.to_bytes(2, "big")
        )
        response = self._send_recv(pdu)
        if response is None:
            return False

        if len(response) >= 9 and response[7] == (FC_WRITE_SINGLE | 0x80):
            _LOGGER.error(
                "FC06 Exception 0x%02X @ 0x%04X value=%d",
                response[8], address, value,
            )
            return False

        success = self._verify_write_echo(response, FC_WRITE_SINGLE, address, value, "value")
        if success:
            _LOGGER.debug("FC06 Write 0x%04X = %d ✓", address, value)
        else:
            self.write_echo_mismatches += 1
            _LOGGER.warning(
                "FC06 Write 0x%04X = %d: response did not echo the write (%d Bytes): %s",
                address, value, len(response), response.hex(),
            )
        return success

    def _write_multiple(self, address: int, value: int) -> bool:
        """FC 0x10 – Write Multiple Registers (R/W Register 0x3000–0x300B)."""
        pdu = (
            FC_WRITE_MULTIPLE.to_bytes(1, "big") +
            address.to_bytes(2, "big")           +
            (1).to_bytes(2, "big")               +  # Quantity = 1 Register
            (2).to_bytes(1, "big")               +  # ByteCount = 2
            value.to_bytes(2, "big")
        )
        response = self._send_recv(pdu)
        if response is None:
            return False

        if len(response) >= 9 and response[7] == (FC_WRITE_MULTIPLE | 0x80):
            _LOGGER.error(
                "FC10 Exception 0x%02X @ 0x%04X value=%d",
                response[8], address, value,
            )
            return False

        # FC10's response echoes address + quantity (not the value itself) -
        # quantity is always 1 here (we only ever write one register at a time).
        success = self._verify_write_echo(response, FC_WRITE_MULTIPLE, address, 1, "quantity")
        if success:
            _LOGGER.debug("FC10 Write 0x%04X = %d ✓", address, value)
        else:
            self.write_echo_mismatches += 1
            _LOGGER.warning(
                "FC10 Write 0x%04X = %d: response did not echo the write (%d Bytes): %s",
                address, value, len(response), response.hex(),
            )
        return success

    def disconnect(self) -> None:
        """Schließt die persistente Verbindung, falls vorhanden."""
        with self._lock:
            self._close()
