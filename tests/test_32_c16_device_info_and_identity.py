"""C16: DeviceInfo populates sw_version and serial_number, coordinator updates the
device registry when identity arrives after a failed first poll, and SnapshotReader
stops re-polling an identity register after CommandRefused."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.helpers import device_registry as dr

from custom_components.foxess_charger import DEFAULT_MODEL, build_device_info, protocol
from custom_components.foxess_charger.const import (
    DOMAIN,
    REG_ID_MODEL_CODE,
    REG_ID_SERIAL_NUMBER,
)
from custom_components.foxess_charger.transport import CommandRefused, TransportError

from fake_controller import FakeController
from ha_harness import Harness, make_entry
from test_protocol import RegisterMap

ENTRY_ID = "c16deviceentry01"


def test_build_device_info_includes_decoded_sw_version_and_serial_when_present():
    entry = SimpleNamespace(entry_id=ENTRY_ID)
    coordinator = SimpleNamespace(
        data={
            "id_model_code": "A022KP1-E-2",
            "id_serial_number": "SN-4242",
            "software_version": 0x010A,
        }
    )
    info = build_device_info(entry, coordinator)
    assert info["model"] == "A022KP1-E-2"
    assert info["serial_number"] == "SN-4242"
    assert info["sw_version"] == "1.10"

    # Missing or sentinel (0xFFFF / None) values omit optional keys rather than crashing
    # or overwriting saved registry attributes with None.
    empty_info = build_device_info(
        entry,
        SimpleNamespace(data={"software_version": 0xFFFF, "id_serial_number": None}),
    )
    assert empty_info["model"] == DEFAULT_MODEL
    assert "sw_version" not in empty_info
    assert "serial_number" not in empty_info


async def test_device_registry_populated_at_setup_and_updated_when_identity_arrives_late(
    hass, enable_custom_integrations
):
    fake = FakeController(status=1)
    fake.hw["software_version"] = 0xFFFF  # unreadable on first poll
    identity_ready = False
    orig_poll = fake.async_poll

    async def _poll():
        snap = await orig_poll()
        if not identity_ready:
            snap.pop("id_model_code", None)
            snap.pop("id_serial_number", None)
        else:
            snap["id_model_code"] = "A022KP1-E-2"
            snap["id_serial_number"] = "SN-LATE-99"
        return snap

    fake.async_poll = _poll
    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    assert await harness.async_setup()
    try:
        reg = dr.async_get(hass)
        device = reg.async_get_device(identifiers={(DOMAIN, ENTRY_ID)})
        assert device is not None
        assert device.model == DEFAULT_MODEL
        assert device.serial_number is None
        assert device.sw_version is None

        # Identity and firmware arrive on the next poll -> device registry updates live.
        identity_ready = True
        fake.hw["software_version"] = 0x0108
        coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
        await coordinator.async_refresh()

        updated = reg.async_get_device(identifiers={(DOMAIN, ENTRY_ID)})
        assert updated.model == "A022KP1-E-2"
        assert updated.serial_number == "SN-LATE-99"
        assert updated.sw_version == "1.8"

        # Subsequent polls with unchanged identity do not spam async_update_device.
        with patch.object(reg, "async_update_device", wraps=reg.async_update_device) as spy:
            await coordinator.async_refresh()
            assert spy.call_count == 0
    finally:
        await harness.async_unload()


async def test_snapshot_reader_caches_command_refused_per_identity_register():
    io = RegisterMap()
    # Serial register refuses reads permanently; model register times out once then succeeds.
    io.fail[REG_ID_SERIAL_NUMBER] = CommandRefused("illegal data address", 0x02)
    io.fail[REG_ID_MODEL_CODE] = TransportError("timeout")

    reader = protocol.SnapshotReader(io)
    snap1 = await reader.async_read()
    assert "id_model_code" not in snap1
    assert "id_serial_number" not in snap1
    assert (REG_ID_MODEL_CODE, 4) in io.reads
    assert (REG_ID_SERIAL_NUMBER, 16) in io.reads

    # Clear the transient timeout on model; serial remains refused and must NOT be re-polled.
    io.reads.clear()
    del io.fail[REG_ID_MODEL_CODE]
    snap2 = await reader.async_read()
    assert snap2["id_model_code"] == "A7300P1"
    assert "id_serial_number" not in snap2
    assert (REG_ID_MODEL_CODE, 4) in io.reads
    assert all(addr != REG_ID_SERIAL_NUMBER for addr, _ in io.reads)

    # Third poll: model is cached and serial is cached-refused -> zero identity reads.
    io.reads.clear()
    snap3 = await reader.async_read()
    assert snap3["id_model_code"] == "A7300P1"
    assert "id_serial_number" not in snap3
    assert all(
        addr not in (REG_ID_MODEL_CODE, REG_ID_SERIAL_NUMBER) for addr, _ in io.reads
    )
