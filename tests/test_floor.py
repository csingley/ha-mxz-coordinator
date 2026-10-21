"""Freeze floor / overheat ceiling for disabled rooms: behaviour through a
config entry, the plan sensor, head states and the service calls MXZ makes.

Every test names the approved decision (1-11, DESIGN.md "APPROVED DECISIONS")
or the design's test-plan item it covers. Entities and readings are invented.
Head commands are observed as service calls; nothing here talks to a real
device. The pure math is in ``test_floor_logic.py``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import restore_state as rs
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.mxz_coordinator.const import (
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_ZONES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF_AFTER_DRY,
    INHIBIT_ACTION_ECO,
    INHIBIT_ACTION_FAN_ONLY,
    INHIBIT_ACTION_OFF,
    ZONE_CLIMATE,
    ZONE_EVIDENCE_BASIS,
    ZONE_FLOOR_HIGH,
    ZONE_FLOOR_LOW,
    ZONE_HANDS_OFF,
    ZONE_MAX_AGE,
    ZONE_NAME,
    ZONE_SAFETY_PRIORITY_ENTITY,
    ZONE_SAMPLE_TIMESTAMP_ATTR,
    ZONE_SENSOR,
)
from custom_components.mxz_coordinator.sensor import _ATTRS
from tests.flow_pages import open_options_page, prefilled, submit, suggested
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    MockHead,
    MockHeadC,
    _eid,
    _recompute,
    _set_target,
    _set_temp,
    _setup_mock_heads,
)
from tests.test_hands_off import _mode_commands, _remote, _s
from tests.test_startup_continuity import _head, _shutdown, _startup, _switch

NOTIFY = "notify.floor_test"
INHIBIT = "binary_sensor.grid_hold"
SAFETY_A = "binary_sensor.primary_safety"
HOUR = timedelta(hours=1)


class Floor:
    """One set-up entry: its heads, notices and plan, read back as plain data."""

    def __init__(
        self, hass: HomeAssistant, entry: MockConfigEntry, a: str, b: str, notes, pinned
    ) -> None:
        self.hass, self.entry, self.a, self.b, self._notes = hass, entry, a, b, notes
        self._pinned = pinned

    @property
    def coord(self):
        return self.entry.runtime_data

    @property
    def plan(self):
        return self.hass.states.get(_eid(self.hass, self.entry, "_plan"))

    def zone(self, index: int) -> dict[str, Any]:
        return self.plan.attributes["zones"][index]

    def floor(self, index: int) -> tuple[str, str | None]:
        zone = self.zone(index)
        return (zone["floor_state"], zone["floor_reason"])

    @property
    def notes(self) -> list[str]:
        return [call.data["message"] for call in self._notes]

    @property
    def pinned(self) -> list[tuple[str, str]]:
        """Home Assistant notifications created: (notification_id, message)."""
        return [(call.data["notification_id"], call.data["message"]) for call in self._pinned]

    async def temp(self, sensor: str, value: float) -> None:
        await _set_temp(self.hass, sensor, value)
        await _recompute(self.hass, self.entry)

    async def compute(self) -> None:
        await _recompute(self.hass, self.entry)

    async def switch(self, suffix: str, on: bool) -> None:
        await _switch(self.hass, _eid(self.hass, self.entry, suffix), on)
        await _recompute(self.hass, self.entry)


async def _setup(
    hass: HomeAssistant,
    *,
    floors: tuple[tuple[float | None, float | None], ...] = ((None, None), (50.0, 90.0)),
    hands_off: tuple[bool, bool] = (False, False),
    enable: tuple[str, ...] = ("_primary_enable", "_coordinator_enable"),
    hysteresis: int | None = 0,
    notify: bool = True,
    pin_real: bool = False,
    metric: bool = False,
    zone_extra: tuple[dict, dict] = ({}, {}),
    temps: tuple[float, float] | None = None,
    **extra: Any,
) -> Floor:
    """Two mock heads. By default the primary is enabled at its target and the
    secondary is disabled (held off) with a 50 °F floor and a 90 °F ceiling."""
    hass.config.units = METRIC_SYSTEM if metric else US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass, cls=MockHeadC if metric else MockHead)
    target = 21 if metric else 70
    for sensor, value in zip((SENSOR_A, SENSOR_B), temps or (target, target)):
        await _set_temp(hass, sensor, value)
    calls = async_mock_service(hass, "notify", "floor_test")
    # Home Assistant notifications are recorded, unless the test runs the
    # real component to read back what a person would see.
    pinned = [] if pin_real else async_mock_service(hass, "persistent_notification", "create")
    zones = []
    for name, head, sensor, (low, high), opted, more in zip(
        ("Primary", "Guest room"), (head_a, head_b), (SENSOR_A, SENSOR_B), floors,
        hands_off, zone_extra,
    ):
        zone = {ZONE_NAME: name, ZONE_CLIMATE: head, ZONE_SENSOR: sensor, **more}
        if low is not None:
            zone[ZONE_FLOOR_LOW] = low
        if high is not None:
            zone[ZONE_FLOOR_HIGH] = high
        if opted:
            zone[ZONE_HANDS_OFF] = True
        zones.append(zone)
    data = {CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY, CONF_ZONES: zones, **extra}
    if notify:
        data[CONF_NOTIFY_SERVICE] = NOTIFY
    if hysteresis is not None:
        data[CONF_MODE_HYSTERESIS] = hysteresis
    entry = MockConfigEntry(domain=DOMAIN, title="MXZ Coordinator", data=data, version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in enable:
        await _switch(hass, _eid(hass, entry, suffix), True)
    for suffix in ("_primary_target", "_secondary_target"):
        await _set_target(hass, _eid(hass, entry, suffix), target)
    await _recompute(hass, entry)
    return Floor(hass, entry, head_a, head_b, calls, pinned)


def _fan_calls(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Every set_fan_mode service call from here on, any head."""
    calls: list[tuple[str, str]] = []

    @callback
    def _record(event) -> None:
        if event.data["domain"] == "climate" and event.data["service"] == "set_fan_mode":
            calls.append((event.data["service_data"]["entity_id"], event.data["service_data"]["fan_mode"]))

    hass.bus.async_listen("call_service", _record)
    return calls


def _all_climate_calls(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Every climate service call from here on: (entity_id, service)."""
    calls: list[tuple[str, str]] = []

    @callback
    def _record(event) -> None:
        if event.data["domain"] == "climate":
            calls.append((event.data["service_data"].get("entity_id"), event.data["service"]))

    hass.bus.async_listen("call_service", _record)
    return calls


async def _tick(hass: HomeAssistant, freezer, delta: timedelta) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


def _reversals(states: list[str], start: str) -> int:
    return sum(1 for a, b in zip([start, *states], states) if a != b)


# ---------------------------------------------------------------------------
# Decision 1: off by default; a 3.7.0 entry is unchanged
# ---------------------------------------------------------------------------
async def test_d1_no_floor_configured_a_cold_held_off_room_stays_off(hass: HomeAssistant) -> None:
    """Decision 1: with no floor keys (the default) a disabled room at 35 °F is
    held off exactly as before: no want, no notice, plan says none."""
    f = await _setup(hass, floors=((None, None), (None, None)))
    sent = _mode_commands(hass, f.b)
    await f.temp(SENSOR_B, 35)
    assert (_s(hass, f.b), sent, f.notes) == ("off", [], [])
    assert f.floor(1) == ("none", None)
    assert f.zone(1)["floor_want"] is None
    assert (f.plan.attributes["mode_by_floor"], f.plan.attributes["mode_by_safety"]) == (False, False)
    assert [(z.floor_low, z.floor_high) for z in f.coord.zones] == [(None, None)] * 2


async def test_d1_a_3_7_0_entry_loads_unchanged(hass: HomeAssistant) -> None:
    """Decision 1 / storage: an entry stored by 3.7.0 (no floor keys) loads
    with no migration and no floor; its stored data is untouched."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 40)
    data = {
        CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
        CONF_ZONES: [
            {ZONE_NAME: "Living", ZONE_CLIMATE: head_a, ZONE_SENSOR: SENSOR_A},
            {ZONE_NAME: "Office", ZONE_CLIMATE: head_b, ZONE_SENSOR: SENSOR_B,
             ZONE_HANDS_OFF: True, ZONE_SAFETY_PRIORITY_ENTITY: "binary_sensor.office_safety"},
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, title="MXZ", data=data, version=2)
    entry.add_to_hass(hass)
    stored = {k: (list(map(dict, v)) if k == CONF_ZONES else v) for k, v in entry.data.items()}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), True)
    await _recompute(hass, entry)
    assert dict(entry.data) == stored
    assert entry.version == 2
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert [z["floor_state"] for z in plan.attributes["zones"]] == ["none", "none"]
    assert _s(hass, head_b) == "off"


async def test_d1_unusable_stored_values_are_no_floor(hass: HomeAssistant) -> None:
    """Decision 1: only a finite number is a floor; a hand-edited string, bool
    or nan stores no protection and changes nothing."""
    f = await _setup(hass, floors=((None, None), ("50", True)))
    assert (f.coord.zones[1].floor_low, f.coord.zones[1].floor_high) == (None, None)
    await f.temp(SENSOR_B, 40)
    assert (_s(hass, f.b), f.floor(1)[0]) == ("off", "none")


# ---------------------------------------------------------------------------
# Decision 2: a freeze floor and an overheat ceiling (scenarios 1 and 3)
# ---------------------------------------------------------------------------
async def test_d2_held_off_room_below_its_floor_heats_to_clear_then_goes_off(
    hass: HomeAssistant,
) -> None:
    """Decision 2, scenario 1: the held-off guest room falls to 45 °F (floor 50):
    MXZ heats it at the clamped clear line (53 -> 59 °F), runs it to 53 °F,
    then turns the head off. One notice, at the trip; none at the clear
    (decision 7)."""
    f = await _setup(hass)
    assert f.floor(1) == ("armed", None)
    await f.temp(SENSOR_B, 45)
    head = hass.states.get(f.b)
    assert (head.state, head.attributes["target_temp_low"], head.attributes["target_temp_high"]) == (
        "heat", 59, 61,
    )
    plan = f.plan
    assert (plan.state, plan.attributes["secondary_engage"], plan.attributes["secondary_demand"]) == (
        "heat", "heat", "off",
    )
    assert f.floor(1) == ("heat", None)
    assert f.zone(1)["floor_want"] == "heat"
    assert (plan.attributes["mode_by_floor"], plan.attributes["mode_by_safety"]) == (True, True)
    assert len(f.notes) == 1
    assert "below its freeze floor of 50 °F" in f.notes[0]
    assert "until it reaches 53 °F" in f.notes[0]
    await f.temp(SENSOR_B, 52.9)
    assert _s(hass, f.b) == "heat"
    await f.temp(SENSOR_B, 53)
    assert (_s(hass, f.b), f.floor(1)) == ("off", ("armed", None))
    assert f.plan.attributes["mode_by_floor"] is False
    assert len(f.notes) == 1  # no notice on clear
    await f.temp(SENSOR_B, 50)  # inside the band: does not re-trip
    assert (_s(hass, f.b), f.floor(1)[0]) == ("off", "armed")


async def test_d2_overheat_ceiling_cools_a_held_off_room_over_an_enabled_heat_demand(
    hass: HomeAssistant,
) -> None:
    """Decision 2, scenario 3: a sunny disabled room hits 92 °F (ceiling 90)
    while the enabled room calls heat: the ceiling outranks the comfort
    demand, the mode goes cool, the enabled head parks, and the guest room
    cools at its clear line (87 °F: setpoints 85/87)."""
    f = await _setup(hass)
    await f.temp(SENSOR_A, 60)
    assert (f.plan.state, _s(hass, f.a)) == ("heat", "heat")
    await f.temp(SENSOR_B, 92)
    head = hass.states.get(f.b)
    assert (f.plan.state, _s(hass, f.a), head.state) == ("cool", "fan_only", "cool")
    assert (head.attributes["target_temp_low"], head.attributes["target_temp_high"]) == (85, 87)
    assert f.floor(1) == ("cool", None)
    assert f.plan.attributes["standoff"] is True
    assert "above its overheat ceiling of 90 °F" in f.notes[-1]
    await f.temp(SENSOR_B, 87)
    assert (_s(hass, f.b), f.floor(1)[0]) == ("off", "armed")


async def test_d2_celsius_install(hass: HomeAssistant) -> None:
    """Decision 2 on a °C install: a 10 °C floor trips below 10, heats at the
    15 °C clamp (clear line 11.5), and clears at 11.5 °C."""
    f = await _setup(hass, metric=True, floors=((None, None), (10.0, 32.0)))
    await f.temp(SENSOR_B, 9.5)
    head = hass.states.get(f.b)
    assert (head.state, head.attributes["target_temp_low"]) == ("heat", 15)
    assert "below its freeze floor of 10 °C" in f.notes[0]
    await f.temp(SENSOR_B, 11)
    assert _s(hass, f.b) == "heat"
    await f.temp(SENSOR_B, 11.5)
    assert _s(hass, f.b) == "off"


# ---------------------------------------------------------------------------
# Decision 3: standby holds
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("hands_off", [False, True], ids=["held_off", "hands_off"])
async def test_d3_eco_hold_runs_the_floor_including_load_shed(
    hass: HomeAssistant, hands_off: bool
) -> None:
    """Decision 3: under an eco standby hold (the load-shed default) the floor
    still runs a held-off or hands-off head, at its clear line (a 60 °F floor
    clears at 63), not the eco heat band (59/61)."""
    hass.states.async_set(INHIBIT, "off")
    f = await _setup(
        hass, hands_off=(False, hands_off), floors=((None, None), (60.0, 90.0)),
        **{CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_ECO},
    )
    hass.states.async_set(INHIBIT, "on")
    await hass.async_block_till_done()
    await f.temp(SENSOR_B, 55)
    assert f.plan.attributes["inhibited"] is True
    head = hass.states.get(f.b)
    assert (head.state, head.attributes["target_temp_low"], head.attributes["target_temp_high"]) == (
        "heat", 63, 65,
    )
    assert f.floor(1) == ("heat", None)


@pytest.mark.parametrize(
    ("action", "parked"), [(INHIBIT_ACTION_OFF, "off"), (INHIBIT_ACTION_FAN_ONLY, "fan_only")]
)
async def test_d3_fixed_holds_stop_the_floor(hass: HomeAssistant, action: str, parked: str) -> None:
    """Decision 3: an off or fan_only hold parks every head; the floor never
    heats under it, shows held, and charges no stall time. It runs once the
    hold lifts."""
    hass.states.async_set(INHIBIT, "off")
    f = await _setup(hass, **{CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: action})
    hass.states.async_set(INHIBIT, "on")
    await hass.async_block_till_done()
    await f.temp(SENSOR_B, 45)
    assert _s(hass, f.b) == parked
    assert f.floor(1) == ("held", "A standby hold parks every head.")
    assert f.coord._floor_driving["secondary"] is False
    hass.states.async_set(INHIBIT, "off")
    await hass.async_block_till_done()
    await f.compute()
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "heat")


# ---------------------------------------------------------------------------
# Decision 4 and fail safe: no usable reading, no action
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["unavailable", "unknown", "missing", "not_a_number"])
async def test_d4_sensor_lost_mid_run_ends_it_now_as_blind(
    hass: HomeAssistant, freezer, bad: str
) -> None:
    """Decision 4: a sensor lost mid-run ends the run in that compute (head
    off, no want) and shows blind with one notice; when it reports again
    below the floor, a new run trips. (Events an hour apart: the notice rate
    limit, P5, is tested on its own below.)"""
    f = await _setup(hass)
    await f.temp(SENSOR_B, 45)
    assert _s(hass, f.b) == "heat"
    await _tick(hass, freezer, HOUR)
    if bad == "missing":
        hass.states.async_remove(SENSOR_B)
    else:
        hass.states.async_set(SENSOR_B, bad)
    await hass.async_block_till_done()
    await f.compute()
    assert _s(hass, f.b) == "off"
    state, reason = f.floor(1)
    assert state == "blind" and "Room sensor" in reason
    assert f.zone(1)["floor_want"] is None
    assert f.plan.attributes["mode_by_floor"] is False
    assert len(f.notes) == 2 and "blind" in f.notes[1]
    await f.compute()
    assert len(f.notes) == 2  # one notice per episode
    await _tick(hass, freezer, HOUR)
    await f.compute()
    assert len(f.notes) == 2  # still one, an hour on
    await f.temp(SENSOR_B, 46)
    assert (_s(hass, f.b), f.floor(1)[0], len(f.notes)) == ("heat", "heat", 3)


async def test_fail_safe_a_stale_cold_reading_never_trips(hass: HomeAssistant, freezer) -> None:
    """Fail safe: a VALID but stale 40 °F reading (its sample time is older
    than the room's maximum age) never trips the floor; the same value with a
    current sample time does."""
    f = await _setup(
        hass,
        zone_extra=({}, {ZONE_EVIDENCE_BASIS: "sample_timestamp",
                         ZONE_SAMPLE_TIMESTAMP_ATTR: "sampled_at", ZONE_MAX_AGE: 5}),
    )
    await _tick(hass, freezer, timedelta(minutes=10))  # the startup grace is over
    sent = _mode_commands(hass, f.b)
    old = (dt_util.utcnow() - HOUR).isoformat()
    hass.states.async_set(SENSOR_B, "40", {"unit_of_measurement": "°F", "sampled_at": old})
    await hass.async_block_till_done()
    await f.compute()
    assert f.zone(1)["sensor_health"] == "stale"
    assert (_s(hass, f.b), sent) == ("off", [])
    assert f.floor(1)[0] == "blind"
    now = dt_util.utcnow().isoformat()
    hass.states.async_set(SENSOR_B, "40", {"unit_of_measurement": "°F", "sampled_at": now})
    await hass.async_block_till_done()
    await f.compute()
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "heat")


async def test_fail_safe_stale_mid_run_ends_it(hass: HomeAssistant, freezer) -> None:
    """Fail safe / decision 4: a run whose sensor goes stale ends at once."""
    f = await _setup(
        hass,
        zone_extra=({}, {ZONE_EVIDENCE_BASIS: "sample_timestamp",
                         ZONE_SAMPLE_TIMESTAMP_ATTR: "sampled_at", ZONE_MAX_AGE: 5}),
    )
    hass.states.async_set(
        SENSOR_B, "45", {"unit_of_measurement": "°F", "sampled_at": dt_util.utcnow().isoformat()}
    )
    await hass.async_block_till_done()
    await f.compute()
    assert _s(hass, f.b) == "heat"
    await _tick(hass, freezer, timedelta(minutes=6))  # no new sample: stale
    await f.compute()
    assert (_s(hass, f.b), f.floor(1)[0]) == ("off", "blind")


async def test_d4_head_unavailable_is_blind_with_no_want(hass: HomeAssistant, freezer) -> None:
    """Decision 4 / design: a tripped room whose head goes unavailable casts no
    want (the enabled room's cool takes the mode back), shows blind with the
    head named, and notifies (an hour after the trip notice, past the rate
    limit); the head returning trips a new run."""
    f = await _setup(hass)
    await f.temp(SENSOR_A, 76)
    await f.temp(SENSOR_B, 45)
    assert (f.plan.state, _s(hass, f.b)) == ("heat", "heat")
    await _tick(hass, freezer, HOUR)
    head = _head(hass, f.b)
    head._attr_available = False
    head.async_write_ha_state()
    await hass.async_block_till_done()
    await f.compute()
    state, reason = f.floor(1)
    assert state == "blind" and f.b in reason
    assert (f.plan.state, _s(hass, f.a)) == ("cool", "cool")
    assert "blind" in f.notes[-1] and f.b in f.notes[-1]
    head._attr_available = True
    head.async_write_ha_state()
    await hass.async_block_till_done()
    await f.compute()
    assert f.floor(1)[0] == "heat"


# ---------------------------------------------------------------------------
# Decision 5: a hands-off run hands back with the head off (scenario 2)
# ---------------------------------------------------------------------------
async def test_d5_hands_off_floor_takes_the_head_and_hands_back_off(hass: HomeAssistant) -> None:
    """Decision 5, scenario 2: the office is hands-off and a person turned it
    off (released). It falls below its floor: the FIRST apply queues heat and
    the room is not re-released; the plan reads floor and MXZ's heat is never
    the person's vote. At clear the head is turned off, the off lands, and the
    room is released again; a later change at the remote is left alone."""
    f = await _setup(hass, hands_off=(False, True))
    await f.compute()
    assert f.zone(1)["hands_off"] == "released"
    sent = _mode_commands(hass, f.b)
    await f.temp(SENSOR_B, 45)
    assert sent == ["heat"]
    assert "secondary" not in f.coord._hands_off_released
    assert (f.zone(1)["hands_off"], f.zone(1)["hands_off_vote"]) == ("floor", None)
    assert f.plan.attributes["mode_by_hands_off"] is False
    await f.compute()
    assert (_s(hass, f.b), f.zone(1)["hands_off_vote"]) == ("heat", None)
    await f.temp(SENSOR_B, 53)
    assert sent == ["heat", "off"]
    await f.compute()
    assert f.zone(1)["hands_off"] == "released"
    await _remote(hass, f.b, "cool")
    for _ in range(2):
        await f.compute()
    assert (_s(hass, f.b), sent) == ("cool", ["heat", "off", "cool"])


async def _person_sets(hass: HomeAssistant, head: str, mode: str, low: float, high: float) -> None:
    """A person sets the head's mode and setpoints themselves (remote, app)."""
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": head, "hvac_mode": mode, "target_temp_low": low, "target_temp_high": high},
        blocking=True,
    )
    await hass.async_block_till_done()


def _run(hass: HomeAssistant, head: str) -> tuple[str, Any, Any]:
    state = hass.states.get(head)
    return (state.state, state.attributes["target_temp_low"], state.attributes["target_temp_high"])


async def _persons_heat_then_trip(f: Floor, mode: str = "heat", low: float = 66, high: float = 68):
    """The hands-off guest room is released and its person runs the head
    themselves; then the room trips its floor (45 °F) or ceiling (92 °F)."""
    await f.compute()
    assert f.zone(1)["hands_off"] == "released"
    await _person_sets(f.hass, f.b, mode, low, high)
    await f.compute()
    assert f.zone(1)["hands_off_vote"] == mode
    sent = _mode_commands(f.hass, f.b)
    await f.temp(SENSOR_B, 45 if mode == "heat" else 92)
    assert (f.zone(1)["hands_off"], f.zone(1)["hands_off_vote"]) == ("floor", None)
    return sent


@pytest.mark.parametrize(
    ("mode", "theirs", "ours", "clear"),
    [("heat", (66, 68), (59, 61), 53), ("cool", (72, 74), (85, 87), 87)],
    ids=["floor", "ceiling"],
)
async def test_p3_a_persons_own_run_is_handed_back_in_their_mode_at_clear(
    hass: HomeAssistant, mode, theirs, ours, clear
) -> None:
    """P3: the person was already heating the hands-off guest room at 66/68
    when it fell below its floor. MXZ runs it at the clear line (59/61), and
    at clear gives it back in heat at 66/68, never off; the room is theirs
    again at once (released, their heat is a vote). The hand-back is sent
    once: a later change by the person is left alone. Mirror: a person
    cooling at 72/74 gets cool at 72/74 back after a ceiling run."""
    f = await _setup(hass, hands_off=(False, True))
    sent = await _persons_heat_then_trip(f, mode, *theirs)
    assert _run(hass, f.b) == (mode, *ours)
    await f.temp(SENSOR_B, clear)
    assert _run(hass, f.b) == (mode, *theirs)
    assert sent == [mode, mode]
    assert (f.zone(1)["hands_off"], f.zone(1)["hands_off_vote"]) == ("released", mode)
    assert f.floor(1)[0] == "armed"
    await _person_sets(hass, f.b, mode, theirs[0] - 2, theirs[1] - 2)
    for _ in range(2):
        await f.compute()
    assert _run(hass, f.b) == (mode, theirs[0] - 2, theirs[1] - 2)
    assert sent == [mode, mode, mode]  # the third is the person's own


@pytest.mark.parametrize("before", ["cool", "fan_only"])
async def test_p3_a_run_mxz_started_from_another_mode_still_hands_back_off(
    hass: HomeAssistant, before: str
) -> None:
    """P3 / decision 5: the person was running the head in another mode
    (cool, or fan only) when the floor tripped, so the heat was MXZ's own:
    at clear the head goes off, as decision 5 says (from off: test_d5)."""
    f = await _setup(hass, hands_off=(False, True))
    await f.compute()
    await _remote(hass, f.b, before)
    await f.compute()
    sent = _mode_commands(hass, f.b)
    await f.temp(SENSOR_B, 45)
    assert _s(hass, f.b) == "heat"
    await f.temp(SENSOR_B, 53)
    assert (_s(hass, f.b), sent) == ("off", ["heat", "off"])
    assert "secondary" not in f.coord._floor_prior


async def test_p3_the_persons_run_survives_a_restart_mid_run(hass: HomeAssistant) -> None:
    """P3, restore-state continuity: the remembered heat at 66/68 is kept in
    the enable switch's restore data during the run, so after a restart the
    clear still hands the head back in heat at 66/68."""
    f = await _setup(hass, hands_off=(False, True))
    await _persons_heat_then_trip(f)
    await _shutdown(hass, f.entry)
    stored = rs.async_get(hass).last_states[_eid(hass, f.entry, "_secondary_enable")]
    assert stored.extra_data.as_dict()["floor_prior"] == {"mode": "heat", "low": 66, "high": 68}
    await _startup(hass, f.entry)
    await f.compute()
    assert (f.zone(1)["hands_off"], f.zone(1)["hands_off_vote"]) == ("floor", None)
    assert _run(hass, f.b) == ("heat", 59, 61)
    await f.temp(SENSOR_B, 53)
    assert _run(hass, f.b) == ("heat", 66, 68)
    assert f.zone(1)["hands_off"] == "released"


async def test_p3_a_hand_back_that_cannot_be_sent_is_not_sent_later(hass: HomeAssistant) -> None:
    """P3, the documented edge: Coordinator is off when the room clears, so
    MXZ sends nothing and the head keeps its last command (heat 59/61). The
    room is the person's again; turning Coordinator back on does not send the
    old hand-back over whatever they have done since."""
    f = await _setup(hass, hands_off=(False, True))
    sent = await _persons_heat_then_trip(f)
    await f.switch("_coordinator_enable", False)
    await f.temp(SENSOR_B, 53)
    assert (_run(hass, f.b), sent) == (("heat", 59, 61), ["heat"])
    assert f.zone(1)["hands_off"] == "released"
    await f.switch("_coordinator_enable", True)
    for _ in range(2):
        await f.compute()
    assert (_run(hass, f.b), sent) == (("heat", 59, 61), ["heat"])


async def test_p3_a_yield_drops_the_persons_earlier_run(hass: HomeAssistant) -> None:
    """P3 / decision 8(b): the person changes the head during the run (off):
    that is the newer wish. The earlier heat is forgotten, not kept in the
    restore data, and nothing is sent when the room later clears."""
    f = await _setup(hass, hands_off=(False, True))
    await _persons_heat_then_trip(f)
    await _remote(hass, f.b, "off")
    await f.compute()
    assert f.floor(1)[0] == "yielded"
    assert "floor_prior" not in f.coord.floor_record("secondary")
    sent = _mode_commands(hass, f.b)
    await f.temp(SENSOR_B, 53)
    for _ in range(2):
        await f.compute()
    assert (_s(hass, f.b), sent, f.floor(1)[0]) == ("off", [], "armed")


async def test_p3_a_run_that_ends_blind_also_hands_back_the_persons_heat(hass: HomeAssistant) -> None:
    """P3 at a blind end: the sensor drops out mid-run. The run ends at once
    (decision 4), and the person's own heat at 66/68 is handed back rather
    than the head being turned off in a cold room."""
    f = await _setup(hass, hands_off=(False, True))
    sent = await _persons_heat_then_trip(f)
    hass.states.async_set(SENSOR_B, "unavailable")
    await hass.async_block_till_done()
    await f.compute()
    assert f.floor(1)[0] == "blind"
    assert _run(hass, f.b) == ("heat", 66, 68)
    assert "off" not in sent
    assert (f.zone(1)["hands_off"], f.zone(1)["hands_off_vote"]) == ("released", "heat")


async def test_p3_a_stalled_run_hands_back_and_a_rearm_takes_it_again(
    hass: HomeAssistant, freezer
) -> None:
    """P3 with decision 11: a run that stalls hands the person's heat back
    (66/68), not off. A new low re-arms it: MXZ takes the head over again
    from that same heat (59/61), and the next clear hands it back again."""
    f = await _setup(hass, hands_off=(False, True))
    sent = await _persons_heat_then_trip(f)
    await _tick(hass, freezer, 2 * HOUR + timedelta(seconds=5))
    await f.compute()
    assert f.floor(1)[0] == "blind"
    assert _run(hass, f.b) == ("heat", 66, 68)
    assert f.zone(1)["hands_off"] == "released"
    await f.temp(SENSOR_B, 44)
    assert (f.floor(1)[0], _run(hass, f.b)) == ("heat", ("heat", 59, 61))
    await f.temp(SENSOR_B, 53)
    assert _run(hass, f.b) == ("heat", 66, 68)
    assert "off" not in sent


# ---------------------------------------------------------------------------
# Decision 6: freeze first; same kind by room order
# ---------------------------------------------------------------------------
async def test_d6_freeze_in_a_later_room_beats_a_ceiling_in_an_earlier_room(
    hass: HomeAssistant,
) -> None:
    """Decision 6 (changed): the primary (room 1) trips its ceiling, the
    secondary (room 2) its freeze floor. Heat wins regardless of order; the
    ceiling room waits OFF (never fan_only) and shows held."""
    f = await _setup(hass, floors=((50.0, 85.0), (50.0, 90.0)), enable=("_coordinator_enable",))
    await f.temp(SENSOR_A, 88)
    assert (f.plan.state, _s(hass, f.a)) == ("cool", "cool")
    await f.temp(SENSOR_B, 45)
    assert (f.plan.state, _s(hass, f.a), _s(hass, f.b)) == ("heat", "off", "heat")
    assert f.floor(0)[0] == "held" and f.floor(1)[0] == "heat"
    await f.temp(SENSOR_B, 53)  # the freeze run clears: the ceiling runs
    assert (f.plan.state, _s(hass, f.a), _s(hass, f.b)) == ("cool", "cool", "off")


async def test_d6_two_freeze_trips_both_run(hass: HomeAssistant) -> None:
    """Decision 6: two trips of the same kind agree; both rooms are heated, and
    the earlier room ranks first."""
    f = await _setup(hass, floors=((50.0, 90.0), (50.0, 90.0)), enable=("_coordinator_enable",))
    await f.temp(SENSOR_A, 44)
    await f.temp(SENSOR_B, 46)
    assert (f.plan.state, _s(hass, f.a), _s(hass, f.b)) == ("heat", "heat", "heat")
    assert [f.floor(i)[0] for i in (0, 1)] == ["heat", "heat"]
    assert len(f.notes) == 2


async def test_d6_ranking_vs_a_safety_priority_room(hass: HomeAssistant) -> None:
    """Ranking vs #31: the enabled primary is marked Safety priority and calls
    cool; the secondary's freeze floor still wins (freeze first). Its ceiling
    against the primary's safety heat goes by room order: the primary wins."""
    hass.states.async_set(SAFETY_A, "on")
    f = await _setup(hass, zone_extra=({ZONE_SAFETY_PRIORITY_ENTITY: SAFETY_A}, {}))
    await f.temp(SENSOR_A, 76)
    assert f.plan.attributes["zones"][0]["safety_priority"] is True
    await f.temp(SENSOR_B, 45)
    assert (f.plan.state, _s(hass, f.b)) == ("heat", "heat")
    await f.temp(SENSOR_B, 60)
    await f.temp(SENSOR_A, 64)  # the safety room calls heat
    await f.temp(SENSOR_B, 92)  # the ceiling trips in the later room
    assert (f.plan.state, _s(hass, f.a), _s(hass, f.b)) == ("heat", "heat", "off")
    assert f.floor(1)[0] == "held"


# ---------------------------------------------------------------------------
# Decision 7: notifications
# ---------------------------------------------------------------------------
async def test_d7_rejected_floor_command_notifies_once(hass: HomeAssistant, freezer) -> None:
    """Decision 7: a floor command the head rejects is notified once per run
    and retried every update; the trip notice came first. The rejection in
    the trip's own update is held back by the rate limit (P5) and goes out
    at the first rejection after the hour, once."""
    f = await _setup(hass)
    head = _head(hass, f.b)
    real = head.async_set_temperature

    async def reject(**kwargs):
        raise HomeAssistantError("invented rejection")

    head.async_set_temperature = reject
    await f.temp(SENSOR_B, 45)
    assert _s(hass, f.b) == "off"
    assert len(f.notes) == 1 and "freeze floor" in f.notes[0]
    await _tick(hass, freezer, HOUR + timedelta(seconds=5))
    await f.compute()
    assert len(f.notes) == 2 and "rejected" in f.notes[1] and "invented rejection" in f.notes[1]
    await f.compute()
    assert len(f.notes) == 2
    head.async_set_temperature = real
    await f.compute()
    assert _s(hass, f.b) == "heat"


async def test_a_rejected_floor_run_is_held_not_heat_while_the_head_refuses(
    hass: HomeAssistant, freezer
) -> None:
    """Plan honesty: while the head rejects every floor command it stays off,
    so the room shows held with the rejection as its reason, never heat. The
    no-progress clock still charges (the fail-safe direction). The first
    command the head accepts shows heat again."""
    f = await _setup(hass)
    head = _head(hass, f.b)
    real = head.async_set_temperature

    async def reject(**kwargs):
        raise HomeAssistantError("invented rejection")

    head.async_set_temperature = reject
    await f.temp(SENSOR_B, 45)
    held = (
        "held",
        f"{f.b} rejected MXZ's floor command (invented rejection). MXZ keeps trying at each update.",
    )
    assert (_s(hass, f.b), f.floor(1)) == ("off", held)
    assert (f.plan.state, f.plan.attributes["mode_by_floor"]) == ("heat", True)
    await _tick(hass, freezer, HOUR)
    await f.compute()  # retried, rejected again
    assert (_s(hass, f.b), f.floor(1)) == ("off", held)
    assert f.coord._floor_runs["secondary"].driven == pytest.approx(3600, abs=1)
    head.async_set_temperature = real
    await f.compute()
    assert (_s(hass, f.b), f.floor(1)) == ("heat", ("heat", None))


async def test_d7_no_notify_service_falls_back_to_a_home_assistant_notification(
    hass: HomeAssistant,
) -> None:
    """Decision 7 / P2: notices use the integration's notification service;
    with none configured the trip notice becomes a Home Assistant
    notification (never log-only), and the floor still protects the room."""
    f = await _setup(hass, notify=False)
    await f.temp(SENSOR_B, 45)
    assert (_s(hass, f.b), f.notes) == ("heat", [])
    trip_id = f"mxz_coordinator_{f.entry.entry_id}_floor_secondary_trip"
    assert [pid for pid, _ in f.pinned] == [trip_id]
    assert "below its freeze floor of 50 °F" in f.pinned[0][1]


def _pn_panel(hass: HomeAssistant) -> dict[str, str]:
    """A live mirror of Home Assistant's notification panel, id -> message,
    kept from the component's own update callback (what the frontend's
    subscription is fed)."""
    from homeassistant.components import persistent_notification as pn

    panel: dict[str, str] = {}

    @callback
    def _update(kind, notifications) -> None:
        for nid, notification in notifications.items():
            if kind == pn.UpdateType.REMOVED:
                panel.pop(nid, None)
            else:
                panel[nid] = notification["message"]

    pn.async_register_callback(hass, _update)
    return panel


async def test_p2_without_a_notify_service_every_floor_notice_kind_reaches_ha_notifications(
    hass: HomeAssistant, freezer
) -> None:
    """P2, through Home Assistant's own notification component: with no
    notify service, trip, blind, stalled and rejected notices each appear
    as a notification with one id per room and kind, and a repeat of a kind
    replaces the last (two trips leave one trip notification, showing the
    later one). Notices are an hour apart (the P5 limit)."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "persistent_notification", {})
    shown = _pn_panel(hass)
    f = await _setup(hass, notify=False, pin_real=True)
    base = f"mxz_coordinator_{f.entry.entry_id}_floor_secondary"
    await f.temp(SENSOR_B, 45)
    await hass.async_block_till_done()
    assert list(shown) == [f"{base}_trip"]
    assert "is at 45 °F" in shown[f"{base}_trip"]
    await _tick(hass, freezer, HOUR)
    hass.states.async_set(SENSOR_B, "unavailable")
    await hass.async_block_till_done()
    await f.compute()
    await _tick(hass, freezer, HOUR)
    await f.temp(SENSOR_B, 44)  # a second trip: replaces the first
    await hass.async_block_till_done()
    assert sorted(shown) == [f"{base}_blind", f"{base}_trip"]
    assert "Room sensor" in shown[f"{base}_blind"]
    assert "is at 44 °F" in shown[f"{base}_trip"]
    await _tick(hass, freezer, 2 * HOUR + timedelta(seconds=5))
    await f.compute()  # the no-progress cap
    head = _head(hass, f.b)

    async def reject(**kwargs):
        raise HomeAssistantError("invented rejection")

    head.async_set_temperature = reject
    await _tick(hass, freezer, HOUR)
    await f.temp(SENSOR_B, 43)  # a new low re-arms; the head rejects it
    await _tick(hass, freezer, HOUR)
    await f.compute()
    await hass.async_block_till_done()
    assert sorted(shown) == [f"{base}_{kind}" for kind in ("blind", "rejected", "stalled", "trip")]
    assert "Stalled" in shown[f"{base}_stalled"]
    assert "invented rejection" in shown[f"{base}_rejected"]


async def test_p2_with_a_notify_service_no_ha_notification_is_made(hass: HomeAssistant) -> None:
    """P2 negative: with a notify service set, floor notices go only there,
    exactly as before; nothing is added to Home Assistant's notifications."""
    f = await _setup(hass)
    await f.temp(SENSOR_B, 45)
    assert len(f.notes) == 1 and f.pinned == []


async def test_p2_a_notify_service_naming_no_service_falls_back(hass: HomeAssistant) -> None:
    """P2: a stored notify value that names no service (no ``domain.``, as an
    old hand-made entry can hold) sends nothing through notify, so the
    floor notice falls back to a Home Assistant notification too."""
    f = await _setup(hass, notify=False, **{CONF_NOTIFY_SERVICE: "floor_test"})
    assert f.coord.notify_service == "floor_test"
    await f.temp(SENSOR_B, 45)
    assert f.notes == [] and len(f.pinned) == 1


# ---------------------------------------------------------------------------
# P5: at most one floor notice per room per hour
# ---------------------------------------------------------------------------
async def _flap(f: Floor, values) -> None:
    for value in values:
        if isinstance(value, str):
            f.hass.states.async_set(SENSOR_B, value)
            await f.hass.async_block_till_done()
            await f.compute()
        else:
            await f.temp(SENSOR_B, value)


@pytest.mark.parametrize("notify", [True, False], ids=["notify_service", "ha_notification"])
async def test_p5_a_flapping_sensor_sends_one_notice_an_hour(
    hass: HomeAssistant, freezer, caplog, notify: bool
) -> None:
    """P5: the guest sensor flaps unusable and back below the floor every 5
    minutes for 55 minutes: trip, blind, trip, blind... One notice goes out
    (the first trip); every held-back one is still logged at WARNING, saying
    it was rate-limited. Once the hour is up the room, blind again by then,
    sends its blind notice at the next update, and the flap after that is
    held back again."""
    f = await _setup(hass, notify=notify)
    sent = (lambda: f.notes) if notify else (lambda: [m for _, m in f.pinned])
    await f.temp(SENSOR_B, 45)
    for value in ("unavailable", 45, "unknown", 44, "unavailable", 45, "unavailable", 44,
                  "unknown", 45, "unavailable"):
        await _tick(hass, freezer, timedelta(minutes=5))
        await _flap(f, (value,))
    assert len(sent()) == 1 and "is at 45 °F" in sent()[0]
    limited = [r for r in caplog.records if "rate-limited" in r.getMessage()]
    assert limited and all(r.levelname == "WARNING" for r in limited)
    assert any("blind" in r.getMessage() for r in limited)
    assert any("is at 44 °F" in r.getMessage() for r in limited)
    await _tick(hass, freezer, timedelta(minutes=6))  # 61 min after the first
    await f.compute()
    assert len(sent()) == 2 and "blind" in sent()[1]
    await _tick(hass, freezer, timedelta(minutes=5))
    await _flap(f, (45, "unavailable"))
    assert len(sent()) == 2


async def test_p5_a_held_back_blind_notice_goes_out_after_the_hour(
    hass: HomeAssistant, freezer, caplog
) -> None:
    """P5 with decision 7: the sensor dies 10 minutes after a trip notice.
    The blind notice is held back (logged once, not on every update) and
    sent at the first update after the hour while the room is still blind,
    then never again for that episode."""
    f = await _setup(hass)
    await f.temp(SENSOR_B, 45)
    await _tick(hass, freezer, timedelta(minutes=10))
    await _flap(f, ("unavailable",))
    for _ in range(3):
        await _tick(hass, freezer, timedelta(minutes=10))
        await f.compute()
    assert len(f.notes) == 1
    limited = [r for r in caplog.records if "rate-limited" in r.getMessage()]
    assert len(limited) == 1 and "blind" in limited[0].getMessage()
    await _tick(hass, freezer, timedelta(minutes=21))  # 61 min after the trip
    await f.compute()
    assert len(f.notes) == 2 and "blind" in f.notes[1]
    await _tick(hass, freezer, 2 * HOUR)
    await f.compute()
    assert len(f.notes) == 2


async def test_p5_two_rooms_are_limited_independently(hass: HomeAssistant, freezer) -> None:
    """P5: the limit is per room. Both disabled rooms trip a minute apart:
    two notices. The first room's blind notice 5 minutes later is held back;
    the second room's own limit is not used up by the first's."""
    f = await _setup(hass, floors=((50.0, 90.0), (50.0, 90.0)), enable=("_coordinator_enable",))
    await f.temp(SENSOR_A, 45)
    await _tick(hass, freezer, timedelta(minutes=1))
    await f.temp(SENSOR_B, 46)
    assert len(f.notes) == 2
    assert f.notes[0].startswith("Primary") and f.notes[1].startswith("Guest room")
    await _tick(hass, freezer, timedelta(minutes=5))
    hass.states.async_set(SENSOR_A, "unavailable")
    await hass.async_block_till_done()
    await f.compute()
    assert len(f.notes) == 2
    await _tick(hass, freezer, timedelta(minutes=55))  # 61 min after the primary's
    await f.compute()
    assert len(f.notes) == 3 and f.notes[2].startswith("Primary") and "blind" in f.notes[2]


# ---------------------------------------------------------------------------
# Decision 8: a person at the wall
# ---------------------------------------------------------------------------
async def test_d8a_held_off_room_is_reasserted_within_15_minutes(hass: HomeAssistant, freezer) -> None:
    """Decision 8(a): a person turns a held-off room's floor head off at the
    wall. Nothing refreshes on the head change alone; the 15-minute heartbeat
    re-asserts heat."""
    f = await _setup(hass)
    await _tick(hass, freezer, timedelta(minutes=1))  # the post-start recompute ran
    await f.temp(SENSOR_B, 45)
    await _remote(hass, f.b, "off")
    await hass.async_block_till_done()
    assert _s(hass, f.b) == "off"
    await _tick(hass, freezer, timedelta(minutes=13))
    assert _s(hass, f.b) == "off"  # a head change alone refreshes nothing
    await _tick(hass, freezer, timedelta(minutes=1, seconds=5))  # the heartbeat
    await hass.async_block_till_done()
    assert _s(hass, f.b) == "heat"


async def test_d8b_hands_off_room_yields_to_a_person_and_rearms_at_clear(hass: HomeAssistant) -> None:
    """Decision 8(b): during a hands-off run a person turns the head off: MXZ
    stands down at once (released, yielded, no re-assert) and the floor
    re-arms only when the room reaches its clear line."""
    f = await _setup(hass, hands_off=(False, True))
    await f.temp(SENSOR_B, 45)
    assert _s(hass, f.b) == "heat"
    sent = _mode_commands(hass, f.b)
    await _remote(hass, f.b, "off")
    for _ in range(3):
        await f.compute()
    assert (_s(hass, f.b), sent) == ("off", ["off"])  # only the person's
    state, reason = f.floor(1)
    assert state == "yielded" and "re-arms" in reason
    assert f.zone(1)["hands_off"] == "released"
    await f.temp(SENSOR_B, 44)  # still cold: still theirs
    assert _s(hass, f.b) == "off"
    await f.temp(SENSOR_B, 53)  # reached the clear line: re-armed
    assert f.floor(1)[0] == "armed"
    await f.temp(SENSOR_B, 45)
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "heat")


async def test_d8b_yield_rearms_after_four_hours(hass: HomeAssistant, freezer) -> None:
    """Decision 8(b): a yielded floor re-arms after 4 h even if the room is
    still cold, and then takes the head again."""
    f = await _setup(hass, hands_off=(False, True))
    await f.temp(SENSOR_B, 45)
    await _remote(hass, f.b, "off")
    await f.compute()
    await _tick(hass, freezer, 4 * HOUR - timedelta(minutes=1))
    await f.compute()
    assert (_s(hass, f.b), f.floor(1)[0]) == ("off", "yielded")
    await _tick(hass, freezer, timedelta(minutes=2))
    await f.compute()
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "heat")


async def test_d8b_a_persons_dry_is_yielded_to_not_healed(hass: HomeAssistant, freezer) -> None:
    """Decision 8(b): a person's dry on a hands-off floor head is a yield, not
    band drift: no heal 20 s later, no heal notice."""
    f = await _setup(hass, hands_off=(False, True))
    await f.temp(SENSOR_B, 45)
    head = _head(hass, f.b)
    head._attr_hvac_mode = "dry"
    head.async_write_ha_state()
    await hass.async_block_till_done()
    await _tick(hass, freezer, timedelta(seconds=30))
    await f.compute()
    assert (_s(hass, f.b), f.floor(1)[0]) == ("dry", "yielded")
    assert not any("drift" in note for note in f.notes)


async def test_d8b_mxz_own_off_and_unavailable_do_not_yield(hass: HomeAssistant) -> None:
    """Decision 8(b) negatives: MXZ's own off while the floor waits out the
    mode-change wait, and an unavailable report, are not a person."""
    f = await _setup(hass, hands_off=(False, True), hysteresis=None)
    await f.temp(SENSOR_A, 76)  # the enabled room holds cool; the wait runs
    await _remote(hass, f.b, "fan_only")  # the person's head, before any trip
    await f.compute()
    sent = _mode_commands(hass, f.b)
    await f.temp(SENSOR_B, 45)  # trips while cool is held: MXZ parks it off
    assert (sent, f.floor(1)[0], f.zone(1)["hands_off"]) == (["off"], "held", "floor")
    head = _head(hass, f.b)
    head._attr_available = False
    head.async_write_ha_state()
    await hass.async_block_till_done()
    head._attr_available = True
    head.async_write_ha_state()
    await hass.async_block_till_done()
    await f.compute()
    assert f.zone(1)["hands_off"] == "floor"
    f.coord._last_mode_change_ts = 0.0
    await f.compute()
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "heat")


async def test_d8b_an_unsent_mxz_off_does_not_land_after_the_persons_change(
    hass: HomeAssistant,
) -> None:
    """Decision 8(b), NIT-2: the floor waits out the mode change, so MXZ has
    an off for the head queued, still unsent (another command holds the
    head). The person switches the head to heat meanwhile: that is a yield,
    and the queued off is disowned, so it never lands over their heat."""
    f = await _setup(hass, hands_off=(False, True), hysteresis=None)
    await f.temp(SENSOR_A, 76)  # the enabled room holds cool; the wait runs
    await _remote(hass, f.b, "fan_only")  # the person's head, before any trip
    await f.compute()
    lock = f.coord._head_locks.setdefault(f.b, asyncio.Lock())
    await lock.acquire()  # stands in for a command still running on that head
    sent = _mode_commands(hass, f.b)
    # (No block_till_done while the lock is held: it would wait for the
    # queued delivery forever.)
    hass.states.async_set(SENSOR_B, 45)
    await f.coord.async_refresh()
    assert f.coord._deliveries[f.b]["act"] == "off"
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": f.b, "hvac_mode": "heat"}, blocking=True
    )
    for _ in range(5):
        await asyncio.sleep(0)
    lock.release()
    await hass.async_block_till_done()
    await f.compute()
    assert (_s(hass, f.b), sent, f.floor(1)[0]) == ("heat", ["heat"], "yielded")


# ---------------------------------------------------------------------------
# Decision 9: no enabled-room lift; decision 10: no recipe shipped
# ---------------------------------------------------------------------------
async def test_d9_an_enabled_room_is_not_lifted_by_its_floor(hass: HomeAssistant) -> None:
    """Decision 9: the floor acts only while the room is disabled. An enabled
    room below its floor calls heat as an ordinary room (no safety tier)."""
    f = await _setup(hass, floors=((50.0, 90.0), (None, None)))
    await f.temp(SENSOR_A, 45)
    assert f.floor(0) == ("enabled", None)
    assert (f.zone(0)["floor_want"], f.plan.attributes["mode_by_floor"]) == (None, False)
    assert f.plan.attributes["mode_by_safety"] is False
    assert f.notes == []


def test_d10_no_floor_automation_recipe_is_shipped() -> None:
    """Decision 10: no Option C automation recipe ships with the integration."""
    for path in [*Path("examples").rglob("*"), *Path("packages").rglob("*")]:
        if path.is_file():
            text = path.read_text().lower()
            assert "freeze floor" not in text and "floor_low" not in text, path


# ---------------------------------------------------------------------------
# Decision 11: the no-progress cap
# ---------------------------------------------------------------------------
async def test_d11_stuck_reading_releases_the_house_after_two_driven_hours(
    hass: HomeAssistant, freezer
) -> None:
    """Decision 11, July case: a valid but frozen 45 °F reading (cadence
    unknown, never stale) holds heat over the enabled room's cool for two
    driven hours, not a tick less; then the run is blind (stalled), the head
    goes off, the enabled room's cool wins, and there is one blind notice."""
    f = await _setup(hass)
    await f.temp(SENSOR_A, 76)
    await f.temp(SENSOR_B, 45)
    assert (f.plan.state, _s(hass, f.b)) == ("heat", "heat")
    assert f.zone(1)["sensor_health"] == "cadence_unknown"
    for _ in range(4):
        await _tick(hass, freezer, timedelta(minutes=29, seconds=59))
        await f.compute()
    assert (f.plan.state, f.floor(1)[0]) == ("heat", "heat")
    await _tick(hass, freezer, timedelta(seconds=5))
    await f.compute()
    state, reason = f.floor(1)
    assert state == "blind" and reason.startswith("Stalled")
    assert (f.plan.state, _s(hass, f.a), _s(hass, f.b)) == ("cool", "cool", "off")
    blind = [n for n in f.notes if "blind" in n]
    for _ in range(3):
        await _tick(hass, freezer, HOUR)
        await f.compute()
    assert [n for n in f.notes if "blind" in n] == blind and len(blind) == 1
    assert _s(hass, f.b) == "off"


async def test_d11_dead_sensor_flicker_stays_blind_and_a_new_low_rearms(
    hass: HomeAssistant, freezer
) -> None:
    """Decision 11: after a stall, a reading flickering between adjacent
    values (45/46) never re-arms; one degree below the run's lowest does."""
    f = await _setup(hass)
    await f.temp(SENSOR_B, 45)
    for value in (46, 45, 46, 45, 46):
        await _tick(hass, freezer, timedelta(minutes=30))
        await f.temp(SENSOR_B, value)
    assert f.floor(1)[0] == "blind"
    for value in (45, 46, 45, 46):
        await f.temp(SENSOR_B, value)
        assert f.floor(1)[0] == "blind"
    await f.temp(SENSOR_B, 44)
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "heat")


@pytest.mark.parametrize(
    ("metric", "trip", "slide", "rearm", "mode", "notice"),
    [
        (False, 45.0, (44.9, 44.5, 44.1), 44.0, "heat", "below its freeze floor"),
        (False, 92.0, (92.1, 92.5, 92.9), 93.0, "cool", "above its overheat ceiling"),
        (True, 9.0, (8.9, 8.6), 8.5, "heat", "below its freeze floor"),
        (True, 33.0, (33.1, 33.4), 33.5, "cool", "above its overheat ceiling"),
    ],
    ids=["floor_f", "ceiling_f", "floor_c", "ceiling_c"],
)
async def test_d11_a_room_that_keeps_going_the_wrong_way_after_a_stall_is_protected_again(
    hass: HomeAssistant, freezer, metric, trip, slide, rearm, mode, notice
) -> None:
    """Decision 11 (ii): a run stalls (2 driven hours, no progress); the room
    then really keeps cooling (warming, for a ceiling) in sub-step readings.
    It stays blind and off until one full step (1 °F / 0.5 °C) past the
    stall's worst reading, then MXZ runs the head again with a new trip
    notice."""
    floors = ((None, None), (10.0, 32.0) if metric else (50.0, 90.0))
    f = await _setup(hass, metric=metric, floors=floors)
    await f.temp(SENSOR_B, trip)
    assert _s(hass, f.b) == mode
    await _tick(hass, freezer, 2 * HOUR + timedelta(seconds=5))
    await f.compute()
    assert (f.floor(1)[0], _s(hass, f.b)) == ("blind", "off")
    for value in slide:
        await f.temp(SENSOR_B, value)
        assert (f.floor(1)[0], _s(hass, f.b)) == ("blind", "off"), value
    await _tick(hass, freezer, HOUR)  # past the stall notice's rate limit (P5)
    await f.compute()
    assert (f.floor(1)[0], _s(hass, f.b)) == ("blind", "off")
    await f.temp(SENSOR_B, rearm)
    assert (f.floor(1), _s(hass, f.b)) == ((mode, None), mode)
    assert len([n for n in f.notes if notice in n]) == 2


@pytest.mark.parametrize(
    ("metric", "trip", "gains", "back", "rearm", "mode"),
    [
        (False, 40.0, (41.0, 42.0, 43.0, 44.0), 43.5, 43.0, "heat"),
        (False, 95.0, (94.0, 93.0, 92.0, 91.0), 91.5, 92.0, "cool"),
        (True, 5.0, (5.5, 6.0, 6.5, 7.0), 6.8, 6.5, "heat"),
        (True, 36.0, (35.5, 35.0, 34.5, 34.0), 34.2, 34.5, "cool"),
    ],
    ids=["floor_f", "ceiling_f", "floor_c", "ceiling_c"],
)
async def test_d11_a_run_that_gained_then_stalled_rearms_one_step_past_the_stall(
    hass: HomeAssistant, freezer, metric, trip, gains, back, rearm, mode
) -> None:
    """Decision 11 (ii), "a new low one step below the lowest reading since
    the stall": the guest room trips at 40 °F, MXZ heats it to 44 °F, then it
    stops gaining and stalls (blind, head off). It slides back: half a step
    stays blind; 43 °F runs the head again, far above the 40 °F trip reading.
    Mirrored for a ceiling and in °C."""
    floors = ((None, None), (10.0, 32.0) if metric else (50.0, 90.0))
    f = await _setup(hass, metric=metric, floors=floors)
    await f.temp(SENSOR_B, trip)
    for value in gains:
        await _tick(hass, freezer, timedelta(minutes=30))
        await f.temp(SENSOR_B, value)
        assert (f.floor(1), _s(hass, f.b)) == ((mode, None), mode), value
    await _tick(hass, freezer, 2 * HOUR + timedelta(seconds=5))
    await f.compute()
    assert (f.floor(1)[0], _s(hass, f.b)) == ("blind", "off")
    await f.temp(SENSOR_B, back)
    assert (f.floor(1)[0], _s(hass, f.b)) == ("blind", "off")
    await f.temp(SENSOR_B, rearm)
    assert (f.floor(1), _s(hass, f.b)) == ((mode, None), mode)


async def test_d11_a_standoff_loser_is_not_stalled_by_waiting(hass: HomeAssistant, freezer) -> None:
    """Decision 11: the clock runs only while MXZ drives the head. A ceiling
    that loses to a freeze floor for 3 h is still tripped (held), and runs
    when the floor clears."""
    f = await _setup(hass, floors=((50.0, 85.0), (50.0, 90.0)), enable=("_coordinator_enable",))
    await f.temp(SENSOR_B, 45)
    await f.temp(SENSOR_A, 88)
    for value in (46, 47, 48, 49, 50, 51):  # the freeze run gains 1 °F every 30 min
        await _tick(hass, freezer, timedelta(minutes=30))
        await f.temp(SENSOR_B, value)
    assert (f.floor(0)[0], f.floor(1)[0]) == ("held", "heat")
    assert f.coord._floor_runs["primary"].driven == 0
    await f.temp(SENSOR_B, 53)
    assert (_s(hass, f.a), f.floor(0)[0]) == ("cool", "cool")


async def test_d11_coordinator_off_mid_run_charges_nothing(hass: HomeAssistant, freezer) -> None:
    """Kill switch / decision 11: Coordinator off mid-run writes nothing to any
    head, the floor shows held, and 3 h off is not charged to the cap."""
    f = await _setup(hass)
    await f.temp(SENSOR_B, 45)
    await _tick(hass, freezer, HOUR)
    await f.compute()
    assert f.coord._floor_runs["secondary"].driven == pytest.approx(3600, abs=1)
    calls = _all_climate_calls(hass)
    await f.switch("_coordinator_enable", False)
    await _tick(hass, freezer, 3 * HOUR)
    await f.temp(SENSOR_B, 44.5)
    assert calls == []
    assert f.floor(1) == ("held", "Coordinator is off: MXZ sends nothing.")
    await f.switch("_coordinator_enable", True)
    assert f.coord._floor_runs["secondary"].driven == pytest.approx(3600, abs=1)
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "heat")


async def test_kill_switch_off_floor_never_starts(hass: HomeAssistant) -> None:
    """Kill switch: with Coordinator off a trip sends nothing to any head."""
    f = await _setup(hass)
    await f.switch("_coordinator_enable", False)
    calls = _all_climate_calls(hass)
    await f.temp(SENSOR_B, 40)
    assert (calls, _s(hass, f.b), f.floor(1)[0]) == ([], "off", "held")


# ---------------------------------------------------------------------------
# Fan: the floor never writes fan_mode; a manual pick is kept
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("picked", [None, "low"], ids=["auto", "manual_low"])
async def test_floor_never_writes_the_fan_and_keeps_a_manual_pick(
    hass: HomeAssistant, picked: str | None
) -> None:
    """No fan writes: a whole floor run (trip, run, clear) makes no
    set_fan_mode call to the floor head even with fan boost on and the room
    far below target, and a manual fan pick on it survives."""
    f = await _setup(hass)
    if picked:
        head = _head(hass, f.b)
        head._attr_fan_mode = picked
        head.async_write_ha_state()
        await hass.async_block_till_done()
    fans = _fan_calls(hass)
    await f.temp(SENSOR_B, 40)
    await f.compute()
    assert _s(hass, f.b) == "heat"
    await f.temp(SENSOR_B, 53)
    assert _s(hass, f.b) == "off"
    assert [c for c in fans if c[0] == f.b] == []
    assert hass.states.get(f.b).attributes["fan_mode"] == (picked or "auto")


# ---------------------------------------------------------------------------
# Waiting, dwell and the shared mode (scenario 4)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("idle", [IDLE_ACTION_FAN_ONLY, IDLE_ACTION_OFF_AFTER_DRY])
async def test_a_waiting_floor_head_is_off_never_fan_only(hass: HomeAssistant, idle: str) -> None:
    """Design: while the mode-change wait holds the house in cool, the tripped
    room's head is commanded off, not fan_only, with idle_action fan_only and
    inside a coil-dry dwell (it was cooling until it was disabled)."""
    f = await _setup(
        hass, enable=("_primary_enable", "_secondary_enable", "_coordinator_enable"),
        hysteresis=None, **{CONF_IDLE_ACTION: idle},
    )
    await f.temp(SENSOR_A, 76)
    await f.temp(SENSOR_B, 76)
    assert (_s(hass, f.a), _s(hass, f.b)) == ("cool", "cool")
    await f.switch("_secondary_enable", False)
    sent = _mode_commands(hass, f.b)
    await f.temp(SENSOR_B, 45)
    assert (f.plan.state, f.floor(1)[0]) == ("cool", "held")
    assert "fan_only" not in sent and _s(hass, f.b) == "off"
    assert f.coord._planned_act_for(f.b) == "off"
    assert f.coord._dwell_timer is not None  # the wait's end wakes the floor


async def test_scenario4_floor_beats_a_cooling_room_after_the_wait_then_hands_back(
    hass: HomeAssistant,
) -> None:
    """Scenario 4: the living room is cooling when the guest room trips heat.
    After the wait the mode flips (stamped, not by votes), the living room
    parks; after the floor clears, cool returns only while the living room
    still demands cool."""
    f = await _setup(hass, hysteresis=None)
    await f.temp(SENSOR_A, 76)
    f.coord._last_mode_change_ts = 0.0
    await f.temp(SENSOR_B, 45)
    assert (f.plan.state, _s(hass, f.a), _s(hass, f.b)) == ("heat", "fan_only", "heat")
    assert f.plan.attributes["mode_by_hands_off"] is False
    assert f.coord._last_mode_change_ts > 0.0  # the flip starts the wait
    await f.temp(SENSOR_A, 72)  # inside its demand band, still engaged
    assert f.plan.attributes["primary_engage"] == "cool"
    await f.temp(SENSOR_B, 53)
    f.coord._last_mode_change_ts = 0.0
    await f.compute()
    assert (f.plan.state, _s(hass, f.a)) == ("heat", "fan_only")  # no demand: rests
    await f.temp(SENSOR_A, 74)  # demands cool again
    assert (f.plan.state, _s(hass, f.a)) == ("cool", "cool")


async def test_a_floor_flip_clears_a_vote_carried_mode(hass: HomeAssistant) -> None:
    """Design: a floor flip is commanding, so it clears _mode_by_votes and stamps."""
    f = await _setup(
        hass, floors=((50.0, 85.0), (None, None)), hands_off=(False, True),
        enable=("_coordinator_enable",),
    )
    await f.compute()
    await _remote(hass, f.b, "heat")  # the person's released head votes heat
    await f.compute()
    assert (f.plan.state, f.plan.attributes["mode_by_hands_off"]) == ("heat", True)
    assert f.coord._mode_by_votes is True
    f.coord._last_mode_change_ts = 0.0
    await f.temp(SENSOR_A, 88)
    assert (f.plan.state, f.plan.attributes["mode_by_hands_off"]) == ("cool", False)
    assert f.coord._mode_by_votes is False
    assert f.coord._last_mode_change_ts > 0.0


async def test_flapping_sensor_reverses_the_compressor_at_most_once_per_wait(
    hass: HomeAssistant,
) -> None:
    """Flapping: the guest sensor wanders across the trip and clear lines
    while the living room calls cool, all inside one wait: at most one
    reversal, and the living room parks at most once."""
    f = await _setup(hass, hysteresis=None)
    await f.temp(SENSOR_A, 76)
    f.coord._last_mode_change_ts = 0.0
    sent = _mode_commands(hass, f.a)
    seen = []
    for value in (49, 51, 49.5, 53, 49, 54, 48.5, 55, 49.9, 60):
        await f.temp(SENSOR_B, value)
        seen.append(f.plan.state)
    assert _reversals(seen, "cool") <= 1
    assert sent.count("fan_only") <= 1
    f.coord._last_mode_change_ts = 0.0
    await f.compute()
    assert (f.plan.state, _s(hass, f.a)) == ("cool", "cool")


async def test_d4_a_flapping_unusable_sensor_reverses_at_most_once_per_wait(
    hass: HomeAssistant,
) -> None:
    """Decision 4 / flapping: the guest sensor drops out and comes back below
    the floor again and again inside one wait while the living room calls
    cool. Each dropout ends the run and each return trips a new one, but the
    compressor reverses at most once per wait."""
    f = await _setup(hass, hysteresis=None)
    await f.temp(SENSOR_A, 76)
    f.coord._last_mode_change_ts = 0.0
    seen, floor_states = [], []
    for value in (45, "unavailable", 45, "unknown", 44, "unavailable", 45):
        if isinstance(value, str):
            hass.states.async_set(SENSOR_B, value)
            await hass.async_block_till_done()
            await f.compute()
        else:
            await f.temp(SENSOR_B, value)
        seen.append(f.plan.state)
        floor_states.append(f.floor(1)[0])
    assert _reversals(seen, "cool") <= 1
    assert floor_states == ["heat", "blind", "heat", "blind", "heat", "blind", "heat"]


async def test_disabling_below_the_floor_trips_and_enabling_takes_the_enabled_path(
    hass: HomeAssistant,
) -> None:
    """Design: enabled -> disabled while below the floor trips on the first
    compute; disabled -> enabled mid-run goes straight to the room's own
    demand, with no off in between."""
    f = await _setup(hass, enable=("_primary_enable", "_secondary_enable", "_coordinator_enable"))
    await f.temp(SENSOR_B, 45)
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "enabled")
    sent = _mode_commands(hass, f.b)
    await f.switch("_secondary_enable", False)
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "heat")
    await f.switch("_secondary_enable", True)
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "enabled")
    assert "off" not in sent


# ---------------------------------------------------------------------------
# Restart continuity
# ---------------------------------------------------------------------------
async def test_restart_mid_run_held_off_continues_with_its_mark_and_clock(
    hass: HomeAssistant, freezer
) -> None:
    """Restart: a held-off run persists its progress record; after a restart
    (with 5 h of downtime, not charged) it continues, no new trip notice,
    and stalls once the driven time adds up to 2 h."""
    f = await _setup(hass)
    await f.temp(SENSOR_B, 45)
    await _tick(hass, freezer, HOUR)
    await f.compute()
    await _shutdown(hass, f.entry)
    await _tick(hass, freezer, 5 * HOUR)
    await _startup(hass, f.entry)
    await f.compute()
    run = f.coord._floor_runs["secondary"]
    assert (run.direction, run.mark, run.stalled) == ("heat", 45.0, False)
    assert run.driven == pytest.approx(3600, abs=1)
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "heat")
    assert len([n for n in f.notes if "freeze floor" in n and "MXZ is heating" in n]) == 1
    await _tick(hass, freezer, HOUR + timedelta(seconds=5))
    await f.compute()
    assert f.floor(1)[0] == "blind"


async def test_restart_mid_run_hands_off_continues_without_a_vote(hass: HomeAssistant) -> None:
    """Restart, hands-off: the run restores as floor (not released), the head
    keeps heating, and MXZ's heat is never reported as the person's vote."""
    f = await _setup(hass, hands_off=(False, True))
    await f.temp(SENSOR_B, 45)
    await _shutdown(hass, f.entry)
    await _startup(hass, f.entry)
    await f.compute()
    assert (f.zone(1)["hands_off"], f.zone(1)["hands_off_vote"]) == ("floor", None)
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "heat")


async def test_restart_hands_off_with_the_record_lost_parks_then_releases(
    hass: HomeAssistant,
) -> None:
    """Restart, lost record: the hands-off head is still in MXZ's heat inside
    the band (51 °F), but the floor record is gone (an older store). The room
    restores parking: vote None throughout, the head is turned off, then
    released."""
    f = await _setup(hass, hands_off=(False, True))
    await f.temp(SENSOR_B, 45)
    await f.temp(SENSOR_B, 51)
    assert _s(hass, f.b) == "heat"
    await _shutdown(hass, f.entry)
    restore = rs.async_get(hass)
    sid = _eid(hass, f.entry, "_secondary_enable")
    stored = restore.last_states[sid]
    assert stored.extra_data.as_dict()["hands_off_released"] is False
    assert stored.extra_data.as_dict()["floor_run"]["direction"] == "heat"
    restore.last_states[sid] = rs.StoredState(stored.state, None, stored.last_seen)
    assert await hass.config_entries.async_setup(f.entry.entry_id)
    await hass.async_block_till_done()
    await f.compute()
    assert f.zone(1)["hands_off_vote"] is None
    assert _s(hass, f.b) == "off"
    await f.compute()
    assert (f.zone(1)["hands_off"], f.zone(1)["hands_off_vote"]) == ("released", None)


async def test_restart_after_clear_or_with_the_floor_removed_does_not_resume(
    hass: HomeAssistant,
) -> None:
    """Restart: a run whose room warmed past clear while down clears on the
    first compute (head off); a run whose floor was removed is not restored."""
    f = await _setup(hass)
    await f.temp(SENSOR_B, 45)
    await _shutdown(hass, f.entry)
    await _set_temp(hass, SENSOR_B, 60)
    await _startup(hass, f.entry)
    await f.compute()
    assert (_s(hass, f.b), f.floor(1)[0]) == ("off", "armed")

    await f.temp(SENSOR_B, 45)
    assert _s(hass, f.b) == "heat"
    await _shutdown(hass, f.entry)
    zones = [dict(z) for z in f.entry.data[CONF_ZONES]]
    zones[1].pop(ZONE_FLOOR_LOW)
    hass.config_entries.async_update_entry(f.entry, data={**f.entry.data, CONF_ZONES: zones})
    await _startup(hass, f.entry)
    await f.compute()
    assert "secondary" not in f.coord._floor_runs
    assert (_s(hass, f.b), f.floor(1)[0]) == ("off", "armed")


async def test_restart_a_stalled_run_stays_stalled(hass: HomeAssistant, freezer) -> None:
    """Restart, decision 11: a stalled run is restored stalled (still blind,
    the head stays off), not re-armed by the restart."""
    f = await _setup(hass)
    await f.temp(SENSOR_B, 45)
    await _tick(hass, freezer, 2 * HOUR)
    await f.compute()
    assert f.floor(1)[0] == "blind"
    await _shutdown(hass, f.entry)
    await _startup(hass, f.entry)
    await f.compute()
    assert (_s(hass, f.b), f.floor(1)[0]) == ("off", "blind")
    assert f.coord._floor_runs["secondary"].stalled is True


async def test_a_room_without_a_floor_persists_exactly_what_3_7_0_did(hass: HomeAssistant) -> None:
    """Storage: with no floor run the enable switch's extra data is 3.7.0's."""
    f = await _setup(hass, floors=((None, None), (None, None)), hands_off=(False, True))
    await _shutdown(hass, f.entry)
    restore = rs.async_get(hass)
    for room in ("primary", "secondary"):
        extra = restore.last_states[_eid(hass, f.entry, f"_{room}_enable")].extra_data
        assert set(extra.as_dict()) == {"hands_off_released"}


# ---------------------------------------------------------------------------
# Plan sensor and the room tile
# ---------------------------------------------------------------------------
async def test_plan_attributes_are_honest_in_each_state(hass: HomeAssistant) -> None:
    """Plan honesty: mode_by_floor is a published attribute; each room shows
    its floor_low/high and a state that says what MXZ is doing."""
    assert "mode_by_floor" in _ATTRS
    f = await _setup(hass, floors=((None, None), (50.0, 90.0)))
    zone = f.zone(1)
    assert (zone["floor_low"], zone["floor_high"], zone["floor_state"]) == (50.0, 90.0, "armed")
    assert f.floor(0) == ("none", None)
    assert f.plan.attributes["mode_by_floor"] is False
    await f.temp(SENSOR_B, 45)
    assert (f.floor(1), f.plan.attributes["mode_by_floor"]) == (("heat", None), True)


async def test_tile_follows_the_head_only_while_tripped(hass: HomeAssistant) -> None:
    """HomeKit contract: the disabled room's tile keeps hvac_mode off; its
    hvac_action follows the head while the floor is tripped, off otherwise."""
    f = await _setup(hass)
    tile = _eid(hass, f.entry, "_secondary_thermostat")
    await f.temp(SENSOR_B, 45)
    state = hass.states.get(tile)
    assert (state.state, state.attributes["hvac_action"]) == (HVACMode.OFF, HVACAction.HEATING)
    await f.temp(SENSOR_B, 53)
    await _remote(hass, f.b, "heat")  # the head runs, but no floor is tripped
    state = hass.states.get(tile)
    assert (state.state, state.attributes["hvac_action"]) == (HVACMode.OFF, HVACAction.OFF)


@pytest.mark.parametrize("room", [0, 1], ids=["primary_floor", "secondary_floor"])
async def test_floor_touches_only_its_own_configured_head(hass: HomeAssistant, room: int) -> None:
    """Scope: a whole floor run (freeze trip, ceiling trip, clear) commands
    only that room's own head. The other room is disabled with no floor and
    its head is already off, so nothing at all is owed to it: it receives no
    command and stays off. Run with the floor on either room, so a floor
    sent to the wrong room's head fails either way. No other climate entity
    (the Carrier) is ever called."""
    hass.states.async_set("climate.carrier_infinity", "heat", {"hvac_modes": ["heat", "off"]})
    calls = _all_climate_calls(hass)
    floors = [(None, None), (None, None)]
    floors[room] = (50.0, 90.0)
    f = await _setup(hass, floors=tuple(floors), enable=("_coordinator_enable",))
    own, other = (f.a, f.b) if room == 0 else (f.b, f.a)
    sensor = (SENSOR_A, SENSOR_B)[room]
    modes = _mode_commands(hass, own)
    del calls[:]
    await f.temp(sensor, 40)
    assert _s(hass, own) == "heat"
    await f.temp(sensor, 92)
    assert _s(hass, own) == "cool"
    await f.temp(sensor, 60)
    assert modes == ["heat", "cool", "off"]
    assert {entity for entity, _ in calls} == {own}
    assert _s(hass, other) == "off"


# ---------------------------------------------------------------------------
# Configure > Rooms
# ---------------------------------------------------------------------------
async def test_config_room_page_fields_start_blank_and_set_one_room(hass: HomeAssistant) -> None:
    """Decisions 1-2: both fields blank by default; set stores floats on that
    room only, in the zones list, and the reload takes them."""
    f = await _setup(hass, floors=((None, None), (None, None)))
    before = [dict(z) for z in f.entry.data[CONF_ZONES]]
    manager = hass.config_entries.options
    result = await open_options_page(hass, f.entry, "room_2")
    assert (suggested(result, "floor_low"), suggested(result, "floor_high")) == (None, None)
    result = await submit(manager, result, {**prefilled(result), "floor_low": 50, "floor_high": 88})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert f.entry.data[CONF_ZONES] == [
        before[0], {**before[1], ZONE_FLOOR_LOW: 50.0, ZONE_FLOOR_HIGH: 88.0}
    ]
    for store in (f.entry.data, f.entry.options):
        assert not {"floor", "floor_low", "floor_high"} & set(store)
    assert (f.coord.zones[1].floor_low, f.coord.zones[1].floor_high) == (50.0, 88.0)


async def test_config_unchanged_room_save_stores_no_floor_keys(hass: HomeAssistant) -> None:
    """Decision 1: opening a room page and saving it unchanged stores no floor
    key (and keeps an existing floor exactly)."""
    f = await _setup(hass, floors=((None, None), (50.0, 90.0)))
    before = [dict(z) for z in f.entry.data[CONF_ZONES]]
    manager = hass.config_entries.options
    for room in ("room_1", "room_2"):
        result = await open_options_page(hass, f.entry, room)
        result = await submit(manager, result, prefilled(result))
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert f.entry.data[CONF_ZONES] == before
    assert ZONE_FLOOR_LOW not in f.entry.data[CONF_ZONES][0]


async def test_config_clearing_a_field_removes_the_key(hass: HomeAssistant) -> None:
    """Decision 2: clearing one field removes that key and leaves the other."""
    f = await _setup(hass, floors=((None, None), (50.0, 90.0)))
    manager = hass.config_entries.options
    result = await open_options_page(hass, f.entry, "room_2")
    values = prefilled(result)
    values.pop("floor_low")
    result = await submit(manager, result, values)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    zone = f.entry.data[CONF_ZONES][1]
    assert ZONE_FLOOR_LOW not in zone and zone[ZONE_FLOOR_HIGH] == 90.0


@pytest.mark.parametrize(
    ("metric", "field", "value", "ok"),
    [
        (False, "floor_low", 35, True), (False, "floor_low", 65, True),
        (False, "floor_low", 34.9, False), (False, "floor_low", 66, False),
        (False, "floor_high", 75, True), (False, "floor_high", 100, True),
        (False, "floor_high", 74, False), (False, "floor_high", 101, False),
        (True, "floor_low", 10, True), (True, "floor_low", 1.5, False),
        (True, "floor_low", 50, False), (True, "floor_high", 30, True),
        (True, "floor_high", 90, False), (True, "floor_high", 23.5, False),
        (False, "floor_low", float("nan"), False),
    ],
)
async def test_config_ranges_per_unit_reject_without_saving(
    hass: HomeAssistant, metric: bool, field: str, value: float, ok: bool
) -> None:
    """°C installs: the accepted range follows the HA unit (35-65 / 75-100 °F,
    2-18 / 24-38 °C); a value outside it is rejected on the floor section and
    nothing is saved."""
    f = await _setup(hass, metric=metric, floors=((None, None), (None, None)))
    before = [dict(z) for z in f.entry.data[CONF_ZONES]]
    manager = hass.config_entries.options
    result = await open_options_page(hass, f.entry, "room_2")
    result = await submit(manager, result, {**prefilled(result), field: value})
    if ok:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert f.entry.data[CONF_ZONES][1][field] == float(value)
    else:
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"floor": "floor_out_of_range"}
        assert f.entry.data[CONF_ZONES] == before


async def test_config_fields_show_the_system_unit(hass: HomeAssistant) -> None:
    """°C installs: the boxes show the HA unit."""
    from tests.flow_pages import marker_of

    f = await _setup(hass, metric=True, floors=((None, None), (None, None)))
    result = await open_options_page(hass, f.entry, "room_1")
    for key in ("floor_low", "floor_high"):
        _, number = marker_of(result, key)
        assert number.config["unit_of_measurement"] == "°C"


def test_config_floor_copy_is_in_both_english_files() -> None:
    """The floor section, its fields and its error resolve in both files alike."""
    import json

    copies = []
    root = Path("custom_components/mxz_coordinator")
    for path in (root / "strings.json", root / "translations/en.json"):
        options = json.loads(path.read_text())["options"]
        floor = options["step"]["room"]["sections"]["floor"]
        assert floor["data"] == {"floor_low": "Freeze floor", "floor_high": "Overheat ceiling"}
        assert "off by default" in floor["description"]
        assert "Nothing was saved" in options["error"]["floor_out_of_range"]
        copies.append((floor, options["error"]["floor_out_of_range"]))
    assert copies[0] == copies[1]
