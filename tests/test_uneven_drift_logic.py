"""Uneven drift in the pure decision math (no Home Assistant required).

``room_call`` and ``engage_with_latch`` take a per-side band: ``heat_band``
(how far below target before heat) and ``cool_band`` (how far above before
cool), each defaulting to the single ``band``. These pin that the single band
still means exactly what it meant, that each side starts on its own band, and
that every run still ends at the target.
"""

from __future__ import annotations

import itertools

import pytest

from tests.test_logic import COOL, HEAT, NEUTRAL, OFF, SAT, logic

_COMMON = {"eco_cool_max": 78.0, "eco_heat_min": 50.0}


def _today(
    *,
    temp: float,
    target: float,
    enabled: bool,
    eco: bool,
    sensor_ok: bool,
    band: float,
    neutral: str,
    heat_lockout: bool,
    cool_lockout: bool,
) -> str:
    """The symmetric rule as shipped in 3.7.1, written out independently."""
    if not enabled:
        return OFF
    if not sensor_ok:
        return neutral
    if eco:
        return COOL if temp > 78.0 else HEAT if temp < 50.0 else neutral
    if temp > target + band:
        return neutral if cool_lockout and temp <= 74.0 else COOL
    if temp < target - band:
        return neutral if heat_lockout and temp >= 66.0 else HEAT
    return neutral


_TEMPS = (45.0, 60.0, 66.0, 67.0, 68.5, 69.0, 69.5, 70.0, 70.5, 71.0, 71.5, 73.0, 74.0, 80.0)
_GRID = list(
    itertools.product(
        _TEMPS,
        (0.5, 1.0, 3.0),  # band
        (True, False),  # enabled
        (False, True),  # eco
        (True, False),  # sensor_ok
        (False, True),  # heat_lockout
        (False, True),  # cool_lockout
    )
)


@pytest.mark.parametrize("neutral", [NEUTRAL, SAT])
def test_a_single_band_and_equal_sides_both_reproduce_the_symmetric_rule(
    neutral: str,
) -> None:
    """Both blank equals today: ``band`` alone, both sides equal to it, and
    ``band`` with explicit None sides all give the 3.7.1 answer, everywhere."""
    for temp, band, enabled, eco, ok, heat_lock, cool_lock in _GRID:
        kwargs = {
            "temp": temp, "target": 70.0, "enabled": enabled, "eco": eco,
            "sensor_ok": ok, "neutral": neutral, "heat_lockout": heat_lock,
            "heat_lockout_floor": 66.0, "cool_lockout": cool_lock,
            "cool_lockout_ceiling": 74.0, **_COMMON,
        }
        expected = _today(
            temp=temp, target=70.0, enabled=enabled, eco=eco, sensor_ok=ok,
            band=band, neutral=neutral, heat_lockout=heat_lock,
            cool_lockout=cool_lock,
        )
        case = (temp, band, enabled, eco, ok, heat_lock, cool_lock)
        assert logic.room_call(band=band, **kwargs) == expected, case
        assert logic.room_call(heat_band=band, cool_band=band, **kwargs) == expected, case
        assert (
            logic.room_call(band=band, heat_band=None, cool_band=None, **kwargs)
            == expected
        ), case


@pytest.mark.parametrize("prior", [None, COOL, HEAT])
def test_the_latch_with_equal_sides_is_the_latch_with_one_band(prior: str | None) -> None:
    for temp, band in itertools.product(_TEMPS, (0.5, 1.0, 3.0)):
        kwargs = {
            "prior": prior, "temp": temp, "target": 70.0, "enabled": True,
            "eco": False, "sensor_ok": True, "neutral": SAT, **_COMMON,
        }
        assert logic.engage_with_latch(
            heat_band=band, cool_band=band, **kwargs
        ) == logic.engage_with_latch(band=band, **kwargs), (prior, temp, band)


def _call(temp: float, **bands: float) -> str:
    return logic.room_call(
        temp=temp, target=70.0, enabled=True, eco=False, sensor_ok=True,
        neutral=SAT, **bands, **_COMMON,
    )


@pytest.mark.parametrize(
    ("temp", "expected"),
    [
        (66.9, HEAT),  # past cooler-by 3
        (67.0, SAT),  # exactly at the edge: not past it
        (68.5, SAT),  # past the old symmetric 1, inside cooler-by
        (71.5, SAT),  # past the old symmetric 1, inside warmer-by
        (75.0, SAT),
        (75.1, COOL),  # past warmer-by 5
    ],
)
def test_each_side_starts_on_its_own_band(temp: float, expected: str) -> None:
    """Target 70, cooler by 3, warmer by 5: heat below 67, cool above 75."""
    assert _call(temp, heat_band=3.0, cool_band=5.0) == expected


@pytest.mark.parametrize(
    ("temp", "expected"),
    [(68.9, HEAT), (69.0, SAT), (74.9, SAT), (75.1, COOL)],
)
def test_one_side_set_leaves_the_other_on_the_single_band(
    temp: float, expected: str
) -> None:
    """Only warmer-by set: the heat side keeps ``band`` (1)."""
    assert _call(temp, band=1.0, cool_band=5.0) == expected


@pytest.mark.parametrize(
    ("temp", "expected"),
    [(66.9, HEAT), (67.0, SAT), (70.9, SAT), (71.1, COOL)],
)
def test_only_cooler_by_set_leaves_cool_on_the_single_band(
    temp: float, expected: str
) -> None:
    assert _call(temp, band=1.0, heat_band=3.0) == expected


def test_a_side_with_no_band_at_all_is_refused() -> None:
    """No silent zero band: every side needs a value."""
    with pytest.raises(TypeError):
        _call(70.0)
    with pytest.raises(TypeError):
        _call(70.0, heat_band=1.0)
    with pytest.raises(TypeError):
        _call(70.0, cool_band=1.0)


def test_lockouts_still_hold_each_side() -> None:
    """A lockout holds its side off past that side's own band."""
    kwargs = {
        "target": 70.0, "enabled": True, "eco": False, "sensor_ok": True,
        "neutral": NEUTRAL, "heat_band": 3.0, "cool_band": 5.0, **_COMMON,
    }
    assert logic.room_call(temp=76.0, cool_lockout=True, cool_lockout_ceiling=80.0,
                           **kwargs) == NEUTRAL
    assert logic.room_call(temp=81.0, cool_lockout=True, cool_lockout_ceiling=80.0,
                           **kwargs) == COOL
    assert logic.room_call(temp=66.0, heat_lockout=True, heat_lockout_floor=60.0,
                           **kwargs) == NEUTRAL
    assert logic.room_call(temp=59.0, heat_lockout=True, heat_lockout_floor=60.0,
                           **kwargs) == HEAT


def _engage(prior: str | None, temp: float) -> str:
    return logic.engage_with_latch(
        prior=prior, temp=temp, target=70.0, enabled=True, eco=False,
        sensor_ok=True, neutral=SAT, heat_band=3.0, cool_band=5.0, **_COMMON,
    )


@pytest.mark.parametrize(
    ("prior", "temp", "expected"),
    [
        (COOL, 74.0, COOL),  # inside warmer-by: an engaged run keeps going
        (COOL, 70.5, COOL),
        (COOL, 70.0, SAT),  # ends AT the target, not at target + 5
        (COOL, 69.0, SAT),  # overshoot: coast, never whiplash to heat
        (HEAT, 68.0, HEAT),  # inside cooler-by: still running
        (HEAT, 69.9, HEAT),
        (HEAT, 70.0, SAT),  # ends at the target, not at target - 3
        (HEAT, 71.0, SAT),
        (None, 74.0, SAT),  # not engaged: inside warmer-by it coasts
        (None, 68.0, SAT),
    ],
)
def test_runs_still_end_at_the_target(prior: str | None, temp: float, expected: str) -> None:
    """The side bands move where a run starts; the latch runs it to target.

    Were a side band to leak into the engaged phase, the cool run at 74 and
    70.5 (inside warmer-by 5) and the heat run at 68 (inside cooler-by 3)
    would stop short of the target.
    """
    assert _engage(prior, temp) == expected
