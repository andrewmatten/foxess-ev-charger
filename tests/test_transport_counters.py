"""Modbus client framing counters reach diagnostics through the one writer.

The sync client counts framing anomalies (TID mismatch, short read, ...).
Only the controller holds the transport, so the counters must travel
client -> ModbusRegisterIO.counters -> ChargingController.diagnostics ->
coordinator data / HA diagnostics, and always show the client's live value.
"""
from __future__ import annotations

from custom_components.foxess_charger.controller import ChargingController
from custom_components.foxess_charger.coordinator import (
    TRANSPORT_COUNTERS, FoxESSChargerCoordinator,
)
from custom_components.foxess_charger.transport import ModbusRegisterIO

from rebuild_simulator import CONNECTED, SimCharger


class CountingSimClient:
    """FoxESSModbusClient-shaped strict API over a SimCharger, carrying the
    real client's counter attributes."""

    def __init__(self, sim: SimCharger) -> None:
        self.sim = sim
        self.txid_mismatches = self.short_reads = self.connection_errors = 0
        self.malformed_headers = self.unit_id_mismatches = 0
        self.write_echo_mismatches = 0
        self.stale_replies = 0

    def read_registers_strict(self, address, count, *, timeout=None):
        return list(self.sim.read_sync(address, count))

    def write_register_strict(self, address, value, *, timeout=None):
        self.sim.write_sync(address, value)

    def disconnect(self):
        pass


async def test_register_io_exposes_live_client_counters():
    client = CountingSimClient(SimCharger(state=CONNECTED))
    io = ModbusRegisterIO(client)
    assert set(io.counters) == set(TRANSPORT_COUNTERS)
    assert all(v == 0 for v in io.counters.values())
    client.txid_mismatches = 3
    client.short_reads = 1
    assert io.counters["txid_mismatches"] == 3
    assert io.counters["short_reads"] == 1


async def test_register_io_counters_tolerate_a_client_without_them():
    class Bare:
        pass

    assert ModbusRegisterIO(Bare()).counters == {}


async def test_controller_diagnostics_include_transport_counters():
    client = CountingSimClient(SimCharger(state=CONNECTED))
    ctl = ChargingController(ModbusRegisterIO(client))
    await ctl.async_initialize(None, configure_safety=False)
    client.write_echo_mismatches = 2
    client.connection_errors = 5
    diag = ctl.diagnostics
    assert diag["write_echo_mismatches"] == 2
    assert diag["connection_errors"] == 5
    assert diag["txid_mismatches"] == 0
    await ctl.async_close()


async def test_counters_reach_coordinator_data(hass):
    client = CountingSimClient(SimCharger(state=CONNECTED))
    ctl = ChargingController(ModbusRegisterIO(client))
    await ctl.async_initialize(None, configure_safety=False)
    coordinator = FoxESSChargerCoordinator(hass, ctl, 10)
    client.txid_mismatches = 4
    client.malformed_headers = 1
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data["diag_txid_mismatches"] == 4
    assert coordinator.data["diag_malformed_headers"] == 1
    await ctl.async_close()
