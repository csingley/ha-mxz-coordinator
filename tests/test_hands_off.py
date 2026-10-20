"""Hands-off disabled rooms: a per-room opt-in, off by default.

Built from Chris Singley's #27 (a disabled head's observed heat/cool votes in
the shared mode) and #28 (park a disabled head once, then leave it alone), and
from the probes run against both. Every probe is here as a real test of the
behavior that ships:

* opted OUT (the default, and every entry stored before this option existed):
  a disabled room is held off every cycle, exactly as before — an outside
  turn-on is turned back off, a rejected park is retried, a restart parks, the
  tile reads off, and the head has no say in the shared mode;
* opted IN: disabling sends off until the head is SEEN off, then the head is
  left alone until the room is enabled again — across a restart too. While a
  person runs it, the tile shows what the head is doing, and its heat/cool is
  a vote in the shared mode that never outranks an enabled room.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import restore_state as rs
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.const import (
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_RESTING_MODE_BIAS,
    CONF_ZONES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    INHIBIT_ACTION_ECO,
    INHIBIT_ACTION_OFF,
    ZONE_CLIMATE,
    ZONE_HANDS_OFF,
    ZONE_NAME,
    ZONE_SENSOR,
)
from custom_components.mxz_coordinator.logic import shared_mode_with_votes
from tests.flow_pages import default, open_options_page, prefilled, submit
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_target,
    _set_temp,
    _setup_mock_heads,
)
from tests.test_startup_continuity import _head, _shutdown, _startup, _switch

INHIBIT = "binary_sensor.grid_down"


def _s(hass: HomeAssistant, entity_id: str) -> str:
    return hass.states.get(entity_id).state


async def _remote(hass: HomeAssistant, head: str, mode: str) -> None:
    """A person sets the head on its own remote / an automation / Apple Home."""
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": head, "hvac_mode": mode}, blocking=True
    )
    await hass.async_block_till_done()


def _plan(hass: HomeAssistant, entry: MockConfigEntry):
    return hass.states.get(_eid(hass, entry, "_plan"))


async def _setup(
    hass: HomeAssistant,
    *,
    hands_off: tuple[bool, bool] = (False, False),
    enable: tuple[str, ...] = ("_primary_enable", "_secondary_enable", "_coordinator_enable"),
    hysteresis: int | None = 0,
    legacy_key: bool = True,
    **extra,
) -> tuple[MockConfigEntry, str, str]:
    """Two mock heads, both rooms at 70 / target 70.

    ``hands_off`` opts each room in. A room left opted out stores NO key at
    all (``legacy_key``), exactly like every entry saved before the option
    existed; ``legacy_key=False`` stores an explicit False instead.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    zones = []
    for name, head, sensor, opted in (
        ("Primary", head_a, SENSOR_A, hands_off[0]),
        ("Secondary", head_b, SENSOR_B, hands_off[1]),
    ):
        zone = {ZONE_NAME: name, ZONE_CLIMATE: head, ZONE_SENSOR: sensor}
        if opted or not legacy_key:
            zone[ZONE_HANDS_OFF] = opted
        zones.append(zone)
    data = {CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY, CONF_ZONES: zones, **extra}
    if hysteresis is not None:
        data[CONF_MODE_HYSTERESIS] = hysteresis
    entry = MockConfigEntry(domain=DOMAIN, title="MXZ Coordinator", data=data, version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in enable:
        await _switch(hass, _eid(hass, entry, suffix), True)
    for suffix in ("_primary_target", "_secondary_target"):
        await _set_target(hass, _eid(hass, entry, suffix), 70)
    await _recompute(hass, entry)
    return entry, head_a, head_b


def _hvac_mode_calls(hass: HomeAssistant, head: str) -> list[str]:
    """Record every set_hvac_mode sent to ``head`` from here on."""
    calls: list[str] = []

    @callback
    def _record(event) -> None:
        data = event.data["service_data"]
        if (
            event.data["domain"] == "climate"
            and event.data["service"] == "set_hvac_mode"
            and data.get("entity_id") == head
        ):
            calls.append(data["hvac_mode"])

    hass.bus.async_listen("call_service", _record)
    return calls


# ---------------------------------------------------------------------------
# Opted out (default): held off, exactly as before
# ---------------------------------------------------------------------------
async def test_existing_entries_load_opted_out(hass: HomeAssistant) -> None:
    """No stored key (every entry before this option) and False both mean off."""
    entry, _a, _b = await _setup(hass)
    assert [z.hands_off for z in entry.runtime_data.zones] == [False, False]


async def test_non_bool_option_is_opted_out(hass: HomeAssistant) -> None:
    """Only a real True opts in; a hand-edited "yes" or 1 is not consent."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(domain=DOMAIN, title="MXZ", version=2, data={
        CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
        CONF_ZONES: [
            {ZONE_NAME: "P", ZONE_CLIMATE: head_a, ZONE_SENSOR: SENSOR_A, ZONE_HANDS_OFF: "yes"},
            {ZONE_NAME: "S", ZONE_CLIMATE: head_b, ZONE_SENSOR: SENSOR_B, ZONE_HANDS_OFF: 1},
        ],
    })
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert [z.hands_off for z in entry.runtime_data.zones] == [False, False]


@pytest.mark.parametrize("legacy_key", [True, False], ids=["no_key", "explicit_false"])
async def test_opted_out_outside_turn_on_is_held_off(
    hass: HomeAssistant, legacy_key: bool
) -> None:
    """Probe pr28d: a head timer turns a disabled head on; it goes back off, every cycle."""
    entry, _a, head_b = await _setup(hass, legacy_key=legacy_key)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    states = []
    for _ in range(3):
        await _remote(hass, head_b, "cool")  # e.g. the head's own on-timer
        await _recompute(hass, entry)
        states.append(_s(hass, head_b))
    assert states == ["off", "off", "off"]


async def test_opted_out_rejected_park_is_retried(hass: HomeAssistant) -> None:
    """Probe pr28e, opted out: a rejected park is sent again next cycle."""
    entry, _a, head_b = await _setup(hass)
    await _remote(hass, head_b, "heat")
    head = _head(hass, head_b)
    real = head.async_set_hvac_mode
    calls = {"n": 0}

    async def flaky(hvac_mode):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HomeAssistantError("invented rejection")
        await real(hvac_mode)

    head.async_set_hvac_mode = flaky
    # Turning the switch off runs a cycle of its own: that park is rejected.
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    assert _s(hass, head_b) == "heat"
    assert entry.runtime_data.command_attributes(head_b)["command_status"] == "rejected"
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    assert calls["n"] == 2


async def test_opted_out_restart_parks_a_remote_held_head(hass: HomeAssistant) -> None:
    """Probe pr28a, opted out: a restart (like any cycle) parks the head again."""
    entry, _a, head_b = await _setup(hass)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    await _shutdown(hass, entry)
    await _remote(hass, head_b, "heat")  # turned on while HA was down
    await _startup(hass, entry)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_opted_out_tile_reads_off_and_head_has_no_vote(hass: HomeAssistant) -> None:
    """Probes pr28b/pr28c and #27, opted out: the disabled head is not looked at.

    It is parked within the same cycle, so the tile reads off and the plan
    shows no standoff, whatever the head was doing a moment before.
    """
    entry, head_a, head_b = await _setup(hass, enable=("_primary_enable", "_coordinator_enable"))
    await _set_temp(hass, SENSOR_A, 76)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (_s(hass, head_a), _s(hass, head_b)) == ("cool", "off")
    assert plan.attributes["standoff"] is False
    assert plan.attributes["secondary_demand"] == "off"
    tile = hass.states.get(_eid(hass, entry, "_secondary_thermostat"))
    assert tile.state == HVACMode.OFF
    assert tile.attributes["hvac_action"] == HVACAction.OFF


# ---------------------------------------------------------------------------
# Opted in: off until it lands, then hands off
# ---------------------------------------------------------------------------
async def test_parks_until_seen_off_then_hands_off(hass: HomeAssistant) -> None:
    """#28's test, kept: park once it lands, never again; a new disablement parks anew."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _remote(hass, head_b, "heat")
    calls = _hvac_mode_calls(hass, head_b)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    assert calls == ["off"]
    # A person picks the remote back up: never re-parked, however many cycles.
    await _remote(hass, head_b, "heat")
    for _ in range(3):
        await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"
    assert calls == ["off", "heat"]  # the second one is the person's
    # Re-enabling hands the head back to the plan; disabling again parks again.
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), True)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_head_already_off_is_released_without_a_command(hass: HomeAssistant) -> None:
    """Nothing to land: an already-off head is left alone from the first cycle."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _remote(hass, head_b, "off")
    calls = _hvac_mode_calls(hass, head_b)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    await _remote(hass, head_b, "cool")
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "cool"
    assert calls == ["cool"]


async def test_rejected_park_is_retried_until_it_lands(hass: HomeAssistant) -> None:
    """Probe pr28e, opted in: a rejected off is NOT landed; it is sent again."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _remote(hass, head_b, "heat")
    head = _head(hass, head_b)
    real = head.async_set_hvac_mode
    calls = {"n": 0}

    async def flaky(hvac_mode):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise HomeAssistantError("invented rejection")
        await real(hvac_mode)

    head.async_set_hvac_mode = flaky
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)  # rejected
    assert _s(hass, head_b) == "heat"
    attrs = entry.runtime_data.command_attributes(head_b)
    assert attrs["command_status"] == "rejected"
    assert attrs["command_error"] == "invented rejection"
    await _recompute(hass, entry)
    assert (_s(hass, head_b), calls["n"]) == ("heat", 2)
    await _recompute(hass, entry)
    assert (_s(hass, head_b), calls["n"]) == ("off", 3)
    # Landed: from here the head is the person's.
    head.async_set_hvac_mode = real
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"


async def test_returned_but_not_seen_off_is_not_landed(hass: HomeAssistant) -> None:
    """A call that returns without the head ever reporting off is sent again."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _remote(hass, head_b, "heat")
    head = _head(hass, head_b)
    real = head.async_set_hvac_mode
    calls = {"n": 0}

    async def swallowed(hvac_mode):
        calls["n"] += 1
        if calls["n"] == 1:
            return  # accepted by the integration, never reached the head
        await real(hvac_mode)

    head.async_set_hvac_mode = swallowed
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    assert entry.runtime_data.command_attributes(head_b)["command_status"] == "returned"
    assert _s(hass, head_b) == "heat"
    await _recompute(hass, entry)
    assert (_s(hass, head_b), calls["n"]) == ("off", 2)


async def test_restart_keeps_the_hold(hass: HomeAssistant) -> None:
    """Probe pr28a, opted in: a person's run survives an HA restart."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    await _shutdown(hass, entry)
    await _startup(hass, entry)
    for _ in range(2):
        await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"
    # And the restored room still re-parks on a fresh disablement.
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), True)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_restart_before_the_park_landed_still_parks(hass: HomeAssistant) -> None:
    """Disabled, but the off never landed before HA went down: it parks after."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _remote(hass, head_b, "heat")
    head = _head(hass, head_b)
    real = head.async_set_hvac_mode

    async def reject(hvac_mode):
        raise HomeAssistantError("invented rejection")

    head.async_set_hvac_mode = reject
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"
    await _shutdown(hass, entry)
    head.async_set_hvac_mode = real
    await _startup(hass, entry)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_disabled_while_down_without_record_still_parks(hass: HomeAssistant) -> None:
    """A disabled room with no hands-off record (older version, lost store) parks."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    await _remote(hass, head_b, "heat")
    await _shutdown(hass, entry)
    # Strip the record, as a store from before this option (state only, no extra data) has it.
    restore = rs.async_get(hass)
    sid = _eid(hass, entry, "_secondary_enable")
    restore.last_states[sid] = rs.StoredState(
        restore.last_states[sid].state, None, restore.last_states[sid].last_seen
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_opting_out_resumes_the_hold(hass: HomeAssistant) -> None:
    """A released room that is opted out again is held off from the next cycle."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"
    zones = [dict(z) for z in entry.data[CONF_ZONES]]
    zones[1].pop(ZONE_HANDS_OFF)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ZONES: zones})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_standby_hold_still_parks_a_released_head(hass: HomeAssistant) -> None:
    """A grid-down standby parks every head, hands-off or not; release leaves it off."""
    hass.states.async_set(INHIBIT, "off")
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True),
        **{CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_OFF},
    )
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    await _remote(hass, head_b, "heat")
    hass.states.async_set(INHIBIT, "on")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    hass.states.async_set(INHIBIT, "off")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"


async def test_eco_standby_hold_also_holds_a_released_head_off(hass: HomeAssistant) -> None:
    """An eco hold is no fixed park, but it is still a hold: the head is held off."""
    hass.states.async_set(INHIBIT, "off")
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True),
        **{CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_ECO},
    )
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    await _remote(hass, head_b, "heat")
    hass.states.async_set(INHIBIT, "on")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    assert _plan(hass, entry).attributes["zones"][1]["hands_off_vote"] is None
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    hass.states.async_set(INHIBIT, "off")
    await hass.async_block_till_done()
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"


async def test_kill_switch_off_sends_nothing_and_parking_waits(hass: HomeAssistant) -> None:
    """With the coordinator off nothing is written; the park lands once it is back on."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _remote(hass, head_b, "heat")
    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), False)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"
    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), True)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_released_head_in_a_banned_mode_is_not_healed(hass: HomeAssistant) -> None:
    """dry/auto on a hands-off head is the person's choice: no heal, no notify."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    head = _head(hass, head_b)
    head._attr_hvac_mode = "dry"
    head.async_write_ha_state()
    await hass.async_block_till_done()
    assert (head_b, "band") not in entry.runtime_data._heal_timers
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "dry"


# ---------------------------------------------------------------------------
# Opted in: the room tile shows the head
# ---------------------------------------------------------------------------
async def test_tile_reflects_the_head_a_person_runs(hass: HomeAssistant) -> None:
    """Probe pr28c: hvac_action follows the real head; hvac_mode stays off.

    hvac_mode is the room's coordinator switch (off = the coordinator is not
    running this room) and its only other mode is heat_cool, so off is the
    honest one; turning the tile on still enables the room.
    """
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    tile_id = _eid(hass, entry, "_secondary_thermostat")
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert hass.states.get(tile_id).attributes["hvac_action"] == HVACAction.OFF
    for mode, action in (("heat", HVACAction.HEATING), ("cool", HVACAction.COOLING),
                         ("fan_only", HVACAction.FAN), ("off", HVACAction.OFF)):
        await _remote(hass, head_b, mode)
        tile = hass.states.get(tile_id)
        assert tile.state == HVACMode.OFF
        assert tile.attributes["hvac_action"] == action, mode
    await _remote(hass, head_b, "heat")
    await hass.services.async_call(
        "climate", "turn_on", {"entity_id": tile_id}, blocking=True
    )
    await hass.async_block_till_done()
    assert _s(hass, _eid(hass, entry, "_secondary_enable")) == "on"
    assert hass.states.get(tile_id).state == HVACMode.HEAT_COOL
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": tile_id, "hvac_mode": "off"}, blocking=True
    )
    await hass.async_block_till_done()
    assert _s(hass, _eid(hass, entry, "_secondary_enable")) == "off"


async def test_tile_prefers_the_heads_own_action(hass: HomeAssistant) -> None:
    """A head that reports hvac_action (idle at setpoint) is shown as it reports."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    head = _head(hass, head_b)
    head._attr_hvac_mode = "heat"
    head._attr_hvac_action = HVACAction.IDLE
    head.async_write_ha_state()
    await hass.async_block_till_done()
    tile = hass.states.get(_eid(hass, entry, "_secondary_thermostat"))
    assert tile.attributes["hvac_action"] == HVACAction.IDLE


# ---------------------------------------------------------------------------
# Opted in: the shared-mode vote, never above an enabled room
# ---------------------------------------------------------------------------
async def test_opposite_mode_head_is_a_visible_standoff(hass: HomeAssistant) -> None:
    """Probe pr28b and #27's test: the enabled primary keeps cooling, the plan says standoff."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)  # the disabled secondary's park lands here
    await _remote(hass, head_b, "heat")
    for _ in range(2):
        await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (_s(hass, head_a), _s(hass, head_b)) == ("cool", "heat")
    assert plan.state == "cool"
    assert plan.attributes["standoff"] is True
    assert plan.attributes["secondary_demand"] == "off"  # the coordinator asks nothing of it
    assert plan.attributes["zones"][1]["hands_off_vote"] == "heat"


async def test_disabled_primary_never_idles_an_enabled_room(hass: HomeAssistant) -> None:
    """Probes pr28b2 and #27: a hands-off PRIMARY in heat vs an enabled hot secondary."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(True, False), enable=("_secondary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    await _set_temp(hass, SENSOR_B, 76)
    await _recompute(hass, entry)
    assert (_s(hass, head_a), _s(hass, head_b)) == ("off", "cool")
    coord = entry.runtime_data
    coord._last_mode_change_ts = 0.0
    await _remote(hass, head_a, "heat")
    seen = []
    for _ in range(4):
        await _recompute(hass, entry)
        seen.append((_plan(hass, entry).state, _s(hass, head_a), _s(hass, head_b)))
        coord._last_mode_change_ts = 0.0  # hysteresis long elapsed every cycle
    assert seen == [("cool", "heat", "cool")] * 4


async def test_vote_decides_only_when_no_enabled_room_calls(hass: HomeAssistant) -> None:
    """No enabled room calling: the plan follows the hands-off head, without
    stamping the dwell clock, so an enabled room's later call flips at once."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    coord = entry.runtime_data
    await _recompute(hass, entry)  # park lands
    coord._last_mode_change_ts = 0.0
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == "heat"
    assert coord.current_shared_mode == "heat"
    assert coord._last_mode_change_ts == 0.0  # the vote stamped nothing
    assert _s(hass, head_a) == "fan_only"  # satisfied primary idles as always
    # The enabled primary now needs cooling: the dwell does not hold it back.
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == "cool"
    assert plan.attributes["standoff"] is True
    assert _s(hass, head_a) == "cool"
    assert _s(hass, head_b) == "heat"


async def test_vote_never_breaks_an_engaged_run(hass: HomeAssistant) -> None:
    """A room mid-run toward target (engaged, its demand back inside the band)
    is calling: the hands-off vote does not take the mode from it."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)
    assert _s(hass, head_a) == "cool"
    await _set_temp(hass, SENSOR_A, 71)  # inside the demand band, still above target
    coord._last_mode_change_ts = 0.0
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.attributes["primary_demand"] != "cool"
    assert plan.attributes["primary_engage"] == "cool"
    assert plan.state == "cool"
    assert _s(hass, head_a) == "cool"


async def test_only_heat_and_cool_vote(hass: HomeAssistant) -> None:
    """fan_only/dry/auto/heat_cool/off/unavailable carry no vote."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None, **{CONF_RESTING_MODE_BIAS: "cool"},
    )
    coord = entry.runtime_data
    await _recompute(hass, entry)
    head = _head(hass, head_b)
    for mode in ("fan_only", "dry", "auto", "heat_cool", "off", "unavailable"):
        coord._last_mode_change_ts = 0.0
        if mode == "unavailable":
            hass.states.async_set(head_b, "unavailable")
        else:
            head._attr_hvac_mode = mode
            head.async_write_ha_state()
        await hass.async_block_till_done()
        await _recompute(hass, entry)
        plan = _plan(hass, entry)
        assert plan.state == "cool", mode
        assert plan.attributes["zones"][1]["hands_off_vote"] is None, mode


async def test_no_vote_before_the_park_lands(hass: HomeAssistant) -> None:
    """Until our off lands, the head's mode is ours or stale: it does not vote."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable",), hysteresis=None,
    )
    coord = entry.runtime_data
    await _remote(hass, head_b, "heat")  # running before the coordinator is on
    head = _head(hass, head_b)

    async def reject(hvac_mode):
        raise HomeAssistantError("invented rejection")

    head.async_set_hvac_mode = reject
    coord._last_mode_change_ts = 0.0
    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), True)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert _s(hass, head_b) == "heat"
    assert plan.attributes["zones"][1]["hands_off"] == "parking"
    assert plan.attributes["zones"][1]["hands_off_vote"] is None
    assert plan.state == "cool"
    assert plan.attributes["standoff"] is False


# ---------------------------------------------------------------------------
# The ranking itself, pure
# ---------------------------------------------------------------------------
def test_vote_ranking_pure() -> None:
    """Votes only fill silence; an enabled demand or run always decides."""
    def decide(demands, engages, votes, current="cool", allowed=True):
        return shared_mode_with_votes(
            demands=demands, engages=engages, votes=votes,
            current=current, allowed=allowed,
        )

    # Silence: the vote decides, and says so (no dwell stamp).
    assert decide(["neutral", "off"], ["satisfied", "off"], [None, "heat"]) == ("heat", True)
    # An enabled demand in either direction outranks it.
    assert decide(["cool", "off"], ["cool", "off"], [None, "heat"]) == ("cool", False)
    assert decide(["neutral", "off"], ["cool", "off"], [None, "heat"]) == ("cool", False)
    assert decide(["heat", "off"], ["heat", "off"], ["cool", None], current="cool") == ("heat", False)
    # The dwell still gates a vote; nothing changes, so nothing is claimed.
    assert decide(["neutral", "off"], ["satisfied", "off"], [None, "heat"], allowed=False) == (
        "cool", False,
    )
    # Non-heat/cool observations are no vote at all.
    for junk in ("fan_only", "dry", "auto", "heat_cool", "off", "unavailable", None):
        assert decide(["neutral", "off"], ["satisfied", "off"], [None, junk]) == ("cool", False)


# ---------------------------------------------------------------------------
# Configure > Rooms > room page
# ---------------------------------------------------------------------------
async def test_room_page_opts_one_room_in_and_out(hass: HomeAssistant) -> None:
    """Off by default; on stores True on that room only; off removes the key."""
    entry, _a, _b = await _setup(hass)
    manager = hass.config_entries.options
    before = [dict(zone) for zone in entry.data[CONF_ZONES]]
    result = await open_options_page(hass, entry, "room_2")
    assert default(result, "hands_off") is False
    result = await submit(manager, result, {**prefilled(result), "hands_off": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_ZONES] == [before[0], {**before[1], ZONE_HANDS_OFF: True}]
    for store in (entry.data, entry.options):
        assert not {"hands_off", "primary_hands_off", "secondary_hands_off"} & set(store)
    assert [z.hands_off for z in entry.runtime_data.zones] == [False, True]

    result = await open_options_page(hass, entry, "room_2")
    assert default(result, "hands_off") is True
    result = await submit(manager, result, {**prefilled(result), "hands_off": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_ZONES] == before
    assert [z.hands_off for z in entry.runtime_data.zones] == [False, False]


def test_room_page_copy_is_translated() -> None:
    """The field has a label and a description in strings.json and en.json alike."""
    root = Path("custom_components/mxz_coordinator")
    copies = []
    for path in (root / "strings.json", root / "translations/en.json"):
        room = json.loads(path.read_text())["options"]["step"]["room"]
        copies.append((room["data"]["hands_off"], room["data_description"]["hands_off"]))
    assert copies[0] == copies[1]
    assert copies[0][0] and "own remote" in copies[0][1]
