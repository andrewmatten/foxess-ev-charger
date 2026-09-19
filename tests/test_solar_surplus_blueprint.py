"""Functional tests for the solar-surplus blueprint.

These drive the real blueprint YAML through a real HA automation, rather
than asserting on the file's text - the bug that prompted them was invisible
to any amount of reading and only shows up once the automation is actually
triggered repeatedly the way its own `time_pattern` trigger triggers it.

The bug: the minimum-dwell gate was written as

    {{ this.attributes.last_triggered is none or
       (now() - this.attributes.last_triggered).total_seconds() >= min_adjustment_seconds }}

HA stamps `last_triggered` at the start of *every* automation run (see
Script.async_run in homeassistant/helpers/script.py), including the
once-a-minute `time_pattern` tick and every no-op pass through the
hysteresis band. So on a blueprint that triggers at least once a minute,
`now() - last_triggered` is never more than ~60s, and the default dwell of
two minutes could never elapse: after the very first run the raise/lower
branches became permanently unreachable and the automation could only ever
fail-safe-stop, never actually modulate charging current. The dwell is now
measured from the Max Charging Current entity's own `last_changed`.
"""
from __future__ import annotations

import pathlib
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time
from homeassistant.setup import async_setup_component

BLUEPRINT_REL = "automation/foxess_charger/solar_surplus_charging.yaml"
BLUEPRINT_SRC = (
    pathlib.Path(__file__).resolve().parent.parent / "blueprints" / BLUEPRINT_REL
)

GRID = "sensor.grid_power"
NUMBER = "number.charger_max_current"
SWITCH = "switch.charger_charging"

T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def install_blueprint(hass):
    """Copies the real blueprint into the test config dir."""
    dest = pathlib.Path(hass.config.path("blueprints")) / BLUEPRINT_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT_SRC, dest)
    return dest


class Harness:
    """Records service calls and keeps the mocked entity states coherent.

    `number.set_value` really does update the number entity's state here -
    which matters, because the dwell gate under test reads that entity's
    `last_changed`. A mock that only recorded the call without moving the
    state would make the throttle look like it never engages at all.
    """

    def __init__(self, hass):
        self.hass = hass
        self.number_writes: list[float] = []
        self.turn_ons = 0
        self.turn_offs = 0

    async def async_register(self):
        async def _set_value(call):
            value = float(call.data["value"])
            self.number_writes.append(value)
            self.set_number(value)

        async def _turn_on(call):
            self.turn_ons += 1
            self.set_switch("on")

        async def _turn_off(call):
            self.turn_offs += 1
            self.set_switch("off")

        self.hass.services.async_register("number", "set_value", _set_value)
        self.hass.services.async_register("switch", "turn_on", _turn_on)
        self.hass.services.async_register("switch", "turn_off", _turn_off)

    def set_number(self, amps: float, *, minimum: float = 6, maximum: float = 32):
        self.hass.states.async_set(
            NUMBER, str(amps), {"min": minimum, "max": maximum, "step": 1}
        )

    def set_switch(self, state: str):
        self.hass.states.async_set(SWITCH, state)

    def set_grid(self, watts):
        self.hass.states.async_set(GRID, str(watts), {"device_class": "power"})


async def _setup(hass, inputs=None):
    """Instantiates the blueprint as a live automation."""
    config = {
        "grid_power_sensor": GRID,
        "max_current_number": NUMBER,
        "charging_switch": SWITCH,
        "import_ceiling_watts": 200,
        "hysteresis_watts": 150,
        "current_step_amps": 1,
        "min_adjustment_interval": {"hours": 0, "minutes": 2, "seconds": 0},
    }
    config.update(inputs or {})
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "alias": "surplus",
                "use_blueprint": {
                    "path": "foxess_charger/solar_surplus_charging.yaml",
                    "input": config,
                },
            }
        },
    )
    await hass.async_block_till_done()


async def test_repeated_triggers_do_not_permanently_wedge_the_dwell_gate(
    hass, install_blueprint
):
    """THE regression test for the `last_triggered` dwell bug.

    Drives the automation every 30 simulated seconds for 5 minutes with a
    steady, well-above-ceiling grid import and a 2-minute dwell - i.e. the
    exact cadence its own once-a-minute `time_pattern` trigger produces in
    the field. Correct behaviour is an adjustment roughly every 2 minutes.
    The old `last_triggered`-based gate produced exactly one adjustment
    (the very first run, where `last_triggered` was still None) and then
    never adjusted again for as long as the automation kept running.
    """
    h = Harness(hass)
    await h.async_register()

    with freeze_time(T0) as frozen:
        h.set_number(20)
        h.set_switch("on")
        h.set_grid(5000)  # far above ceiling+hysteresis -> should lower
        await hass.async_block_till_done()

        await _setup(hass, install_blueprint and None)

        for step in range(1, 11):  # 30s .. 300s
            frozen.move_to(T0 + timedelta(seconds=30 * step))
            # Nudge the sensor so the state trigger fires, staying well
            # above the ceiling the whole time.
            h.set_grid(5000 + step)
            await hass.async_block_till_done()

    # Over 5 minutes at a 2-minute dwell: adjustments at ~t=120s and
    # ~t=240s. The pre-fix blueprint managed exactly one, ever.
    assert len(h.number_writes) >= 2, (
        f"dwell gate wedged - only {len(h.number_writes)} adjustment(s) in "
        f"5 minutes of triggers: {h.number_writes}"
    )
    # Monotonically stepping down by the configured 1A step.
    assert h.number_writes == [19.0, 18.0]
    assert h.turn_offs == 0  # 18A is still above the entity's 6A minimum


async def test_dwell_actually_throttles_back_to_back_triggers(hass, install_blueprint):
    """The other half: the gate must still *be* a throttle.

    Two triggers moments apart must produce a single adjustment - this is
    what stops the blueprint hammering the charger's embedded Modbus stack.
    """
    h = Harness(hass)
    await h.async_register()

    with freeze_time(T0) as frozen:
        h.set_number(20)
        h.set_switch("on")
        h.set_grid(5000)
        await hass.async_block_till_done()
        await _setup(hass)

        frozen.move_to(T0 + timedelta(minutes=3))  # first adjustment allowed
        h.set_grid(5001)
        await hass.async_block_till_done()
        assert h.number_writes == [19.0]

        for i in range(5):  # a burst of triggers over the next ~5 seconds
            frozen.move_to(T0 + timedelta(minutes=3, seconds=i + 1))
            h.set_grid(5002 + i)
            await hass.async_block_till_done()

    assert h.number_writes == [19.0], "dwell did not throttle the burst"


async def test_pinned_at_entity_maximum_stops_writing(hass, install_blueprint):
    """Once the setpoint is clamped at the entity's own max, the raise
    branch must stop issuing writes entirely.

    This is load-bearing for the new dwell reference: a no-op re-write
    leaves `last_changed` frozen, which holds the dwell gate permanently
    open - turning "nothing to do" into a Modbus write every single minute,
    the exact behaviour the dwell exists to prevent.
    """
    h = Harness(hass)
    await h.async_register()

    with freeze_time(T0) as frozen:
        h.set_number(32)  # already at max
        h.set_switch("on")
        h.set_grid(-3000)  # exporting: plenty of surplus, wants to raise
        await hass.async_block_till_done()
        await _setup(hass)

        for step in range(1, 11):
            frozen.move_to(T0 + timedelta(minutes=step))
            h.set_grid(-3000 - step)
            await hass.async_block_till_done()

    assert h.number_writes == []
    assert h.turn_ons == 0  # switch was already on


async def test_below_minimum_stops_charging_once_not_every_minute(
    hass, install_blueprint
):
    """Dropping below the entity's own minimum stops the session - but the
    stop must not be re-issued every minute afterwards.

    This branch never touches the setpoint, so `last_changed` stays frozen
    and the dwell gate stays open; without the explicit "only if not
    already off" guard it is a stop command per minute for as long as
    import stays high.
    """
    h = Harness(hass)
    await h.async_register()

    with freeze_time(T0) as frozen:
        h.set_number(6)  # at the entity minimum - one more step goes under
        h.set_switch("on")
        h.set_grid(5000)
        await hass.async_block_till_done()
        await _setup(hass)

        for step in range(1, 6):
            frozen.move_to(T0 + timedelta(minutes=3 * step))
            h.set_grid(5000 + step)
            await hass.async_block_till_done()

    assert h.number_writes == []
    assert h.turn_offs == 1, f"stop command re-issued {h.turn_offs} times"


async def test_failsafe_stops_on_unavailable_sensor_bypassing_the_dwell(
    hass, install_blueprint
):
    """An unavailable grid sensor stops charging immediately - it must not
    wait out a throttle that exists only to rate-limit ordinary tuning."""
    h = Harness(hass)
    await h.async_register()

    with freeze_time(T0) as frozen:
        h.set_number(20)
        h.set_switch("on")
        h.set_grid(100)
        await hass.async_block_till_done()
        await _setup(hass)

        # No time advanced at all: the dwell is nowhere near satisfied.
        frozen.move_to(T0 + timedelta(seconds=1))
        hass.states.async_set(GRID, "unavailable")
        await hass.async_block_till_done()

    assert h.turn_offs == 1
    assert h.number_writes == []


async def test_failsafe_does_not_rewrite_the_setpoint_from_a_dead_sensor(
    hass, install_blueprint
):
    """The most dangerous misreading of an unavailable sensor.

    `states(sensor) | float(0)` on an unavailable entity yields 0 W, which
    looks exactly like "no import at all, plenty of surplus" and would send
    the raise branch straight to switch.turn_on. The fail-safe branch has to
    win the `choose` outright - this pins that it does, and keeps winning
    while the sensor stays unavailable.
    """
    h = Harness(hass)
    await h.async_register()

    with freeze_time(T0) as frozen:
        h.set_number(20)
        h.set_switch("off")  # session already stopped
        h.set_grid(100)
        await hass.async_block_till_done()
        await _setup(hass)

        for step in range(1, 6):
            frozen.move_to(T0 + timedelta(minutes=3 * step))
            hass.states.async_set(GRID, "unavailable", {"seq": step})
            await hass.async_block_till_done()

    assert h.turn_ons == 0, "dead sensor read as zero import and started charging"
    assert h.number_writes == []
    # Switch was already off, so the fail-safe has nothing to do and must
    # not spam stop commands either.
    assert h.turn_offs == 0


async def test_hysteresis_band_does_nothing(hass, install_blueprint):
    """Import sitting inside the dead band around the ceiling: no writes,
    no start, no stop."""
    h = Harness(hass)
    await h.async_register()

    with freeze_time(T0) as frozen:
        h.set_number(20)
        h.set_switch("on")
        h.set_grid(200)  # exactly the ceiling
        await hass.async_block_till_done()
        await _setup(hass)

        for step, watts in enumerate([210, 340, 60, 190, 51], start=1):
            frozen.move_to(T0 + timedelta(minutes=3 * step))
            h.set_grid(watts)
            await hass.async_block_till_done()

    assert h.number_writes == []
    assert h.turn_ons == 0
    assert h.turn_offs == 0


async def test_raise_writes_the_setpoint_before_starting_the_session(
    hass, install_blueprint
):
    """Ordering guarantee from the blueprint's own header: the ramp-up
    current limit is written *before* switch.turn_on, so a stopped session
    can never briefly start at whatever setpoint was left over from last
    time."""
    order: list[str] = []
    h = Harness(hass)

    async def _set_value(call):
        order.append("number")
        h.number_writes.append(float(call.data["value"]))
        h.set_number(float(call.data["value"]))

    async def _turn_on(call):
        order.append("switch_on")
        h.turn_ons += 1
        h.set_switch("on")

    async def _turn_off(call):
        order.append("switch_off")
        h.turn_offs += 1
        h.set_switch("off")

    hass.services.async_register("number", "set_value", _set_value)
    hass.services.async_register("switch", "turn_on", _turn_on)
    hass.services.async_register("switch", "turn_off", _turn_off)

    with freeze_time(T0) as frozen:
        h.set_number(10)
        h.set_switch("off")
        h.set_grid(-2000)
        await hass.async_block_till_done()
        await _setup(hass)

        frozen.move_to(T0 + timedelta(minutes=3))
        h.set_grid(-2001)
        await hass.async_block_till_done()

    assert order == ["number", "switch_on"]
    assert h.number_writes == [11.0]


async def test_inverted_sign_input_flips_the_direction(hass, install_blueprint):
    """With the invert option on, a positive reading means exporting - so a
    large positive number must raise current, not lower it."""
    h = Harness(hass)
    await h.async_register()

    with freeze_time(T0) as frozen:
        h.set_number(20)
        h.set_switch("on")
        h.set_grid(3000)  # "exporting 3kW" under the inverted convention
        await hass.async_block_till_done()
        await _setup(hass, {"invert_grid_power_sign": True})

        frozen.move_to(T0 + timedelta(minutes=3))
        h.set_grid(3001)
        await hass.async_block_till_done()

    assert h.number_writes == [21.0], "inverted sign lowered instead of raising"
