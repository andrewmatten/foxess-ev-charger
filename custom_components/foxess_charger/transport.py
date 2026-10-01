"""Async register transport for the FoxESS charger.

`ModbusRegisterIO` adapts the synchronous `FoxESSModbusClient` to the async
`RegisterIO` protocol. Guarantees:

- One operation on the wire at a time, including across caller cancellation:
  a cancelled caller stops waiting, but the transport stays busy until its
  worker thread has actually finished, so the next operation can never
  overlap a request that is still in flight.
- Every operation is bounded by a whole-transaction deadline enforced by the
  sync client (connect, send and full response).
- Reads are always fresh wire requests.
- A successful write means only that the device acknowledged it. Whether the
  value took effect must be established by reading it back.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

from .modbus_client import ModbusExceptionResponse, ModbusNoResponse

DEFAULT_DEADLINE = 5.0

_T = TypeVar("_T")


class TransportError(Exception):
    """Operation failed. For a write, raised directly only when the request
    definitely never reached the device."""


class CommandRefused(TransportError):
    """The device answered with a Modbus exception: definitely not applied."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class UnknownOutcome(TransportError):
    """A write may or may not have been applied (timeout, lost or garbled
    reply, disconnect mid-transaction). Read back to find out."""


@runtime_checkable
class RegisterIO(Protocol):
    async def read(self, address: int, count: int) -> tuple[int, ...]: ...

    async def write(self, address: int, value: int) -> None: ...

    async def close(self) -> None: ...


def _check_u16(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} out of range: {value}")


# Framing-anomaly counters kept by the sync client (modbus_client.py).
CLIENT_COUNTERS = (
    "txid_mismatches", "short_reads", "connection_errors",
    "malformed_headers", "unit_id_mismatches", "write_echo_mismatches",
    "stale_replies",
)


class ModbusRegisterIO:
    """Serialized, cancellation-safe async wrapper around the sync client."""

    def __init__(self, client: Any, *, deadline: float = DEFAULT_DEADLINE) -> None:
        if deadline <= 0:
            raise ValueError("deadline must be positive")
        self._client = client
        self._deadline = deadline
        self._lock = asyncio.Lock()
        self._closing = False
        self._closed = False

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def counters(self) -> dict[str, int]:
        """Live values of the client's framing counters (those it has)."""
        return {
            name: getattr(self._client, name)
            for name in CLIENT_COUNTERS if hasattr(self._client, name)
        }

    async def _run(self, fn: Callable[[], _T], *, closing_ok: bool = False) -> _T:
        if self._closing and not closing_ok:
            raise TransportError("transport closed")
        await self._lock.acquire()
        if self._closing and not closing_ok:
            self._lock.release()
            raise TransportError("transport closed")
        try:
            worker = asyncio.get_running_loop().run_in_executor(None, fn)
        except BaseException:
            self._lock.release()
            raise

        def _done(fut: asyncio.Future) -> None:
            # Release only once the thread has really finished, whatever
            # happened to the caller. Retrieve the exception so an abandoned
            # worker's failure is not reported as never retrieved.
            if not fut.cancelled():
                fut.exception()
            self._lock.release()

        worker.add_done_callback(_done)
        return await asyncio.shield(worker)

    async def read(self, address: int, count: int) -> tuple[int, ...]:
        _check_u16("address", address)
        try:
            regs = await self._run(
                lambda: self._client.read_registers_strict(
                    address, count, timeout=self._deadline)
            )
        except ModbusExceptionResponse as ex:
            raise CommandRefused(str(ex), ex.code) from ex
        except (ModbusNoResponse, OSError) as ex:
            raise TransportError(str(ex)) from ex
        return tuple(regs)

    async def write(self, address: int, value: int) -> None:
        _check_u16("address", address)
        _check_u16("value", value)
        try:
            await self._run(
                lambda: self._client.write_register_strict(
                    address, value, timeout=self._deadline)
            )
        except ModbusExceptionResponse as ex:
            raise CommandRefused(str(ex), ex.code) from ex
        except ModbusNoResponse as ex:
            if ex.request_sent:
                raise UnknownOutcome(str(ex)) from ex
            raise TransportError(str(ex)) from ex
        except (TransportError, asyncio.CancelledError):
            raise
        except Exception as ex:  # anything else mid-transaction: can't know
            raise UnknownOutcome(f"write 0x{address:04X} failed: {ex!r}") from ex

    async def close(self) -> None:
        """Refuses new work, waits for the in-flight operation, then closes
        the socket. Operations already queued fail with TransportError."""
        self._closing = True
        if self._closed:
            return
        # Runs through the same serialized path, so it queues behind the
        # in-flight worker and is itself drained if close() is cancelled.
        await self._run(self._client.disconnect, closing_ok=True)
        self._closed = True
