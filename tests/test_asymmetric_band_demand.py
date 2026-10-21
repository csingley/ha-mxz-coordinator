"""Regression: Zone.explicit_band gates demand, engage, and the inversion
guard identically -- a fully explicit band never widens against drift, and
a half-written pair is inert everywhere.

Requires pytest-homeassistant-custom-component (Python 3.12+).
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.const import (
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)
from tests.test_drive import _eid, _recompute, _set_temp, _setup_mock_heads


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, "sensor.room_a_temp", 70)
    await _set_temp(hass, "sensor.room_b_temp", 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: "sensor.room_a_temp",
            CONF_SECONDARY_SENSOR: "sensor.room_b_temp",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)},
            blocking=True,
        )
    await hass.async_block_till_done()
    return entry


async def _set_number(
    hass: HomeAssistant, entry: MockConfigEntry, suffix: str, value: float
) -> None:
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, entry, suffix), "value": value},
        blocking=True,
    )
    await hass.async_block_till_done()


def _primary_demand(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    return hass.states.get(_eid(hass, entry, "_plan")).attributes["primary_demand"]


async def test_fully_explicit_band_votes_verbatim_regardless_of_drift(
    hass: HomeAssistant,
) -> None:
    """Band 66..72, drift 5 (wider than the high edge). 73 is past the
    explicit edge but inside target+drift -- must call COOL, not neutral."""
    entry = await _setup(hass)
    await _set_number(hass, entry, "_primary_drift", 5.0)
    await _set_number(hass, entry, "_primary_band_low", 66.0)
    await _set_number(hass, entry, "_primary_band_high", 72.0)

    await _set_temp(hass, "sensor.room_a_temp", 73)
    await _recompute(hass, entry)
    assert _primary_demand(hass, entry) == "cool"


async def test_partial_band_is_inert_for_demand_and_engage(hass: HomeAssistant) -> None:
    """Only one edge set -> Zone.explicit_band is None, so both demand and
    engage ignore it and fall back to target +/- drift, same as an unset
    room."""
    entry = await _setup(hass)
    await _set_number(hass, entry, "_primary_drift", 5.0)
    await _set_number(hass, entry, "_primary_band_high", 72.0)

    # 74 is past the lone explicit edge (72) but well inside target+drift
    # (75) -- both votes must read it as if band_high were never set.
    await _set_temp(hass, "sensor.room_a_temp", 74)
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["primary_demand"] == "neutral"
    assert plan.attributes["primary_engage"] == "satisfied"


async def test_symmetric_room_unaffected(hass: HomeAssistant) -> None:
    """No band set at all -> the historical target+drift behavior, untouched."""
    entry = await _setup(hass)
    await _set_number(hass, entry, "_primary_drift", 5.0)

    await _set_temp(hass, "sensor.room_a_temp", 75)
    await _recompute(hass, entry)
    assert _primary_demand(hass, entry) == "neutral"

    await _set_temp(hass, "sensor.room_a_temp", 79)
    await _recompute(hass, entry)
    assert _primary_demand(hass, entry) == "cool"


async def test_band_edge_rejects_crossing_the_other_edge(hass: HomeAssistant) -> None:
    """band_low cannot be pushed above the room's current effective high
    (explicit or still derived) -- an inverted pair would call continuously."""
    entry = await _setup(hass)
    await _set_number(hass, entry, "_primary_band_low", 66.0)
    await _set_number(hass, entry, "_primary_band_high", 72.0)

    with pytest.raises(HomeAssistantError):
        await _set_number(hass, entry, "_primary_band_low", 73.0)
    with pytest.raises(HomeAssistantError):
        await _set_number(hass, entry, "_primary_band_high", 65.0)

    # Unchanged after both rejections.
    assert entry.runtime_data.zones[0].band_low == 66.0
    assert entry.runtime_data.zones[0].band_high == 72.0


async def test_band_edge_rejects_crossing_a_still_derived_other_edge(
    hass: HomeAssistant,
) -> None:
    """Same guard when the other edge has never been set explicitly: the
    low edge still can't jump past wherever target+drift derives the high
    edge today."""
    entry = await _setup(hass)
    await _set_number(hass, entry, "_primary_drift", 2.0)  # target 70 -> derived high 72

    with pytest.raises(HomeAssistantError):
        await _set_number(hass, entry, "_primary_band_low", 73.0)
