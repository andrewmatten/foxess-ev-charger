"""ModbusRegisterIO: outcome mapping, serialization, cancellation drain, close."""
from __future__ import annotations

import asyncio
import threading

import pytest

from custom_components.foxess_charger import modbus_client as mc
from custom_components.foxess_charger.transport import (
    CommandRefused,
    ModbusRegisterIO,
    RegisterIO,
    TransportError,
    UnknownOutcome,
)

from test_modbus_client import SLAVE_ID, build_frame, fc03_response_pdu, make_client


class BlockingClient:
    """Sync client stand-in whose calls can be held open to observe overlap."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()
        self.active = 0
        self.max_active = 0
        self.calls: list[tuple] = []
        self.timeouts: list[float | None] = []
        self.disconnected = False
        self.disconnect_while_active = False
        self._mutex = threading.Lock()
        self.raise_on_write: Exception | None = None
        self.raise_on_read: Exception | None = None

    def _enter(self, call):
        with self._mutex:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(call)
        self.started.set()
        self.release.wait(5)

    def _exit(self):
        with self._mutex:
            self.active -= 1

    def read_registers_strict(self, address, count, *, timeout=None):
        self.timeouts.append(timeout)
        self._enter(("read", address, count))
        try:
            if self.raise_on_read:
                raise self.raise_on_read
            return list(range(count))
        finally:
            self._exit()

    def write_register_strict(self, address, value, *, timeout=None):
        self.timeouts.append(timeout)
        self._enter(("write", address, value))
        try:
            if self.raise_on_write:
                raise self.raise_on_write
        finally:
            self._exit()

    def disconnect(self):
        with self._mutex:
            if self.active:
                self.disconnect_while_active = True
        self.disconnected = True


async def _wait_started(client: BlockingClient) -> None:
    for _ in range(200):
        if client.started.is_set():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("worker never started")


def test_satisfies_protocol():
    assert isinstance(ModbusRegisterIO(BlockingClient()), RegisterIO)


async def test_read_returns_tuple_and_passes_deadline():
    client = BlockingClient()
    io = ModbusRegisterIO(client, deadline=1.5)
    assert await io.read(0x1000, 3) == (0, 1, 2)
    assert client.timeouts == [1.5]


async def test_default_deadline_is_five_seconds():
    """Bumped from 2.0 s after an incident - the real
    charger sometimes takes more than 2 s to answer, and 2 s was too tight
    a margin (see CHANGELOG)."""
    client = BlockingClient()
    await ModbusRegisterIO(client).read(0x1000, 1)
    assert client.timeouts == [5.0]


async def test_reads_are_never_cached():
    client = BlockingClient()
    io = ModbusRegisterIO(client)
    await io.read(0x1000, 1)
    await io.read(0x1000, 1)
    assert len(client.calls) == 2


async def test_exception_response_to_write_is_command_refused():
    client = BlockingClient()
    client.raise_on_write = mc.ModbusExceptionResponse(0x10, 0x03, 0x3002)
    with pytest.raises(CommandRefused) as err:
        await ModbusRegisterIO(client).write(0x3002, 0)
    assert err.value.code == 0x03
    assert not isinstance(err.value, UnknownOutcome)


async def test_lost_reply_after_send_is_unknown_outcome():
    client = BlockingClient()
    client.raise_on_write = mc.ModbusNoResponse("timeout", request_sent=True)
    with pytest.raises(UnknownOutcome):
        await ModbusRegisterIO(client).write(0x3002, 0)


async def test_connect_failure_before_send_is_plain_transport_error():
    client = BlockingClient()
    client.raise_on_write = mc.ModbusNoResponse("refused", request_sent=False)
    with pytest.raises(TransportError) as err:
        await ModbusRegisterIO(client).write(0x3002, 0)
    assert not isinstance(err.value, (UnknownOutcome, CommandRefused))


async def test_read_failures_are_transport_errors():
    client = BlockingClient()
    client.raise_on_read = mc.ModbusNoResponse("garbled", request_sent=True)
    with pytest.raises(TransportError):
        await ModbusRegisterIO(client).read(0x1000, 1)
    client.raise_on_read = mc.ModbusExceptionResponse(0x03, 0x02, 0x300A)
    with pytest.raises(CommandRefused):
        await ModbusRegisterIO(client).read(0x300A, 2)


async def test_unexpected_worker_exception_on_write_is_unknown_outcome():
    client = BlockingClient()
    client.raise_on_write = RuntimeError("boom")
    with pytest.raises(UnknownOutcome):
        await ModbusRegisterIO(client).write(0x3002, 0)


@pytest.mark.parametrize("args", [(-1, 0), (0x3002, 0x10000), (0x3002, True), (0x3002, 1.5)])
async def test_invalid_write_rejected_before_worker(args):
    client = BlockingClient()
    with pytest.raises((ValueError, TypeError)):
        await ModbusRegisterIO(client).write(*args)
    assert client.calls == []


async def test_operations_are_serialized():
    client = BlockingClient()
    client.release.clear()
    io = ModbusRegisterIO(client)
    tasks = [asyncio.create_task(io.read(0x1000, 1)) for _ in range(3)]
    tasks.append(asyncio.create_task(io.write(0x3002, 10)))
    await _wait_started(client)
    await asyncio.sleep(0.05)
    client.release.set()
    await asyncio.gather(*tasks)
    assert client.max_active == 1
    assert len(client.calls) == 4


async def test_cancelled_caller_drains_before_next_operation():
    """Cancelling the awaiting caller must not free the transport while its
    worker thread is still on the wire."""
    client = BlockingClient()
    client.release.clear()
    io = ModbusRegisterIO(client)
    first = asyncio.create_task(io.write(0x3002, 10))
    await _wait_started(client)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(io.write(0x3002, 0))
    await asyncio.sleep(0.05)
    assert len(client.calls) == 1, "next op started while cancelled worker still running"
    client.release.set()
    await second
    assert client.max_active == 1
    assert [c[2] for c in client.calls] == [10, 0]


async def test_close_waits_for_in_flight_then_disconnects():
    client = BlockingClient()
    client.release.clear()
    io = ModbusRegisterIO(client)
    op = asyncio.create_task(io.read(0x1000, 1))
    await _wait_started(client)
    closer = asyncio.create_task(io.close())
    await asyncio.sleep(0.05)
    assert not client.disconnected
    client.release.set()
    await closer
    assert await op == (0,)
    assert client.disconnected
    assert not client.disconnect_while_active


async def test_close_after_cancelled_caller_does_not_race_worker():
    client = BlockingClient()
    client.release.clear()
    io = ModbusRegisterIO(client)
    op = asyncio.create_task(io.write(0x4001, 2))
    await _wait_started(client)
    op.cancel()
    closer = asyncio.create_task(io.close())
    await asyncio.sleep(0.05)
    assert not client.disconnected
    client.release.set()
    await closer
    assert client.disconnected and not client.disconnect_while_active


async def test_operations_after_close_fail_without_wire_access():
    client = BlockingClient()
    io = ModbusRegisterIO(client)
    await io.close()
    with pytest.raises(TransportError):
        await io.read(0x1000, 1)
    with pytest.raises(TransportError):
        await io.write(0x3002, 0)
    assert client.calls == []
    await io.close()  # idempotent


async def test_queued_operation_fails_once_close_requested():
    client = BlockingClient()
    client.release.clear()
    io = ModbusRegisterIO(client)
    first = asyncio.create_task(io.read(0x1000, 1))
    await _wait_started(client)
    queued = asyncio.create_task(io.write(0x3002, 10))
    await asyncio.sleep(0)
    closer = asyncio.create_task(io.close())
    await asyncio.sleep(0)
    client.release.set()
    await first
    await closer
    with pytest.raises(TransportError):
        await queued
    assert [c[0] for c in client.calls] == ["read"]


async def test_end_to_end_with_real_client_framing():
    """ModbusRegisterIO over the real sync client and a scripted socket."""
    ok = build_frame(1, SLAVE_ID, fc03_response_pdu([7, 8]))
    refused = build_frame(2, SLAVE_ID, bytes([0x10 | 0x80, 0x03]))
    client, _ = make_client([ok, refused])
    io = ModbusRegisterIO(client)
    assert await io.read(0x1000, 2) == (7, 8)
    with pytest.raises(CommandRefused):
        await io.write(0x3002, 0)
    lost_client, _ = make_client([])
    with pytest.raises(UnknownOutcome):
        await ModbusRegisterIO(lost_client).write(0x4001, 2)
