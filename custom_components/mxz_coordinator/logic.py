"""Pure decision math for MXZ Coordinator — no Home Assistant dependency.

Mirrors the Jinja in the YAML package's ``sensor.mxz_plan`` and the setpoint/action
math in ``script.mxz_coordinate``. Kept HA-free so the validated truth table can be
unit-tested directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from .const import (
    ECO_COOL_HIGH,
    ECO_COOL_LOW,
    ECO_HEAT_HIGH,
    ECO_HEAT_LOW,
    ENGAGE_SATISFIED,
    EVIDENCE_HA_WRITE,
    EVIDENCE_SAMPLE_TIMESTAMP,
    HEALTH_AWAITING,
    HEALTH_CADENCE_UNKNOWN,
    HEALTH_HEALTHY,
    HEALTH_INVALID,
    HEALTH_STALE,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
    STALE_MISSED_REPORTS,
)


def room_call(
    *,
    temp: float,
    target: float,
    enabled: bool,
    eco: bool,
    sensor_ok: bool,
    band: float,
    eco_cool_max: float,
    eco_heat_min: float,
    neutral: str,
    heat_lockout: bool = False,
    heat_lockout_floor: float = 0.0,
    cool_lockout: bool = False,
    cool_lockout_ceiling: float = 200.0,
) -> str:
    """Per-room call for one band (demand uses S/'neutral', engage uses D/'satisfied').

    Mirrors the primary/secondary_demand and primary/secondary_engage attributes.

    ``heat_lockout`` holds off the heat call (the room idles instead of heating — e.g.
    to let passive solar warm it in summer) UNLESS the room has dropped below
    ``heat_lockout_floor``, a safety floor so a genuinely cold room still gets heat.
    ``cool_lockout`` is the mirror for the cold season: it holds off the cool call
    (let the room drift down on its own) UNLESS it rises above ``cool_lockout_ceiling``.
    The eco extremes are unaffected; the two lockouts are independent.
    """
    if not enabled:
        return MODE_OFF
    if not sensor_ok:
        return neutral
    if eco:
        if temp > eco_cool_max:
            return MODE_COOL
        if temp < eco_heat_min:
            return MODE_HEAT
        return neutral
    if temp > target + band:
        if cool_lockout and temp <= cool_lockout_ceiling:
            return neutral
        return MODE_COOL
    if temp < target - band:
        if heat_lockout and temp >= heat_lockout_floor:
            return neutral
        return MODE_HEAT
    return neutral


def engage_with_latch(
    *,
    prior: str | None,
    band: float,
    neutral: str,
    **call_kwargs,
) -> str:
    """Per-room engage with run-to-target hysteresis.

    The engage deadband exists so a satisfied room COASTS instead of flip-
    flopping — it was never meant to truncate the approach. Without a latch,
    a room cooling toward 63 would stop at 64 (target + band) and never reach
    the number the user actually set.

    * Not engaged (``prior`` is None): a room must drift PAST ``band`` to
      engage — unchanged behavior.
    * Engaged (``prior`` is cool|heat): the room runs all the way TO its
      target — satisfied means exactly the number the user set. Crossing the target — or a lockout / eco / disable /
      sensor dropout, all still evaluated via :func:`room_call` — disengages
      it back to ``neutral`` to coast until it drifts past ``band`` again.
    * A direction flip while engaged (overshoot, opened window) never
      whiplashes: anything other than continuing in ``prior``'s direction
      disengages to ``neutral`` first.

    Eco mode bypasses the latch entirely (protection extremes are already a
    wide band; run-to-extreme would defeat their purpose).
    """
    if prior in (MODE_COOL, MODE_HEAT) and not call_kwargs.get("eco"):
        # Engaged phase: band 0 — the room runs exactly TO its target, then
        # coasts. The re-engage drift (``band``) is the only tunable here.
        raw = room_call(band=0.0, neutral=neutral, **call_kwargs)
        if raw == prior or raw == MODE_OFF:
            return raw
        return neutral  # target reached / lockout / direction flip -> coast
    return room_call(band=band, neutral=neutral, **call_kwargs)


def freshness_window(
    *,
    report_interval: Any,
    max_age: Any,
    startup_grace: Any,
) -> tuple[float, float] | None:
    """Resolve one sensor's ``(maximum_age, startup_grace)``, or None.

    Same duration unit in and out (the coordinator passes minutes). ``None``
    means the sensor's cadence is UNKNOWN — no age cutoff applies to it, which
    is the honest default for a change-only or uncontracted source. There is no
    universal fallback interval to invent one from.

    * ``report_interval`` (P) alone -> ``3P``: stale at the third due instant.
    * an explicit ``max_age`` wins over that derivation, and must be at least
      P: a window narrower than one report would park a healthy room on its
      first quiet interval.
    * ``max_age`` alone is enough: a documented maximum age needs no cadence.
    * ``startup_grace`` defaults to the resolved maximum age.

    An input that is set must be a real, finite, positive number. A profile
    with one that is not — or with a maximum age shorter than its interval —
    is INVALID as a whole, and an invalid profile is no profile: ``None``,
    exactly as if nothing had been configured. Nothing in it is read as
    unset, raised, rounded or otherwise coerced into a cutoff the person did
    not write (the caller reports the rejection once).
    """
    durations: list[float | None] = []
    for value in (report_interval, max_age, startup_grace):
        if value is not None and (value := _positive(value)) is None:
            return None
        durations.append(value)
    interval, window, grace = durations
    if window is None:
        window = None if interval is None else interval * STALE_MISSED_REPORTS
    elif interval is not None and window < interval:
        return None
    if window is None:
        return None
    return (window, window if grace is None else grace)


def evidence_contract(
    *,
    basis: Any,
    sample_timestamp_attribute: Any,
    sample_sequence_attribute: Any,
) -> tuple[str, str | None, bool] | None:
    """Resolve one sensor's evidence basis, or None when nothing is trusted.

    Returns ``(basis, marker_attribute, marker_is_a_time)``. ``None`` is both
    the default and the answer to every unrecognised or unusable declaration:
    an untrusted source shows its age and is never enforced, however precise
    its configured cadence is.

    ``ha_state_write`` is a claim about the SOURCE, not about Home Assistant:
    it may be declared only where every accepted write — including the first
    one after setup or reload — is a current acquisition. ``sample_timestamp``
    needs exactly one marker attribute named; naming both is ambiguous, and an
    ambiguous marker is not trustworthy evidence.
    """
    if basis == EVIDENCE_HA_WRITE:
        return (EVIDENCE_HA_WRITE, None, False)
    if basis == EVIDENCE_SAMPLE_TIMESTAMP:
        stamp = _attribute(sample_timestamp_attribute)
        sequence = _attribute(sample_sequence_attribute)
        if stamp is not None and sequence is None:
            return (EVIDENCE_SAMPLE_TIMESTAMP, stamp, True)
        if sequence is not None and stamp is None:
            return (EVIDENCE_SAMPLE_TIMESTAMP, sequence, False)
    return None


def _attribute(name: Any) -> str | None:
    """``name`` as a usable attribute name, else None."""
    return name if isinstance(name, str) and name else None


def sample_evidence(
    *,
    marker: float | None,
    last: float | None,
    now: float,
    receipt: float | None,
    marker_is_a_time: bool,
) -> tuple[float | None, float | None]:
    """One marker observation -> ``(marker to remember, evidence time)``.

    Pure, and the whole of the marker contract. ``marker`` is the trusted
    marker carried by this write (a POSIX sample time, or a sample sequence
    number, compared exactly as the number it is — an integer sequence is
    never rounded through a float); ``last`` is the last marker accepted for
    this sensor.

    * a missing, equal or older marker is a cached or replayed write: it is
      diagnostic only, moves no deadline and recovers nothing;
    * a sample TIME later than the evaluation clock is not evidence either —
      it is a clock fault, and accepting it would grant a free window;
    * an accepted sample time IS the evidence time, so a genuinely old sample
      arrives already aged and recovers nothing;
    * a sample SEQUENCE has no wall-clock meaning, so its evidence time is the
      HA receipt time of the write that carried the advance. The first
      sequence seen is a baseline only: nothing has advanced yet.
    """
    if marker is None or (last is not None and marker <= last):
        return (last, None)
    if marker_is_a_time:
        if marker > now:
            return (last, None)
        return (marker, marker)
    if last is None or receipt is None:
        return (marker, None)
    return (marker, min(receipt, now))


def _positive(value: Any) -> float | None:
    """``value`` as a finite positive float, else None (unset or unusable)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def sensor_health(
    *,
    valid: bool,
    max_age: float | None,
    evidence_age: float | None,
    in_grace: bool,
) -> str:
    """Classify one room sensor. Pure; the whole freshness decision lives here.

    Evaluation order is validity, then basis, then age — M13's value handling
    precedes every age question, so freshness can never make a bad value good.

    ``max_age`` is None for every sensor that is not enforcement-capable: no
    trusted evidence basis, or no maximum age. Such a room is
    ``cadence_unknown`` — its age is shown and no cutoff exists for it.

    ``evidence_age`` is how long ago this incarnation witnessed a qualifying
    report, or ``None`` when it has witnessed none. A report qualifies when it
    is a state write of a valid value that this incarnation saw; it is FRESH —
    the word every recovery rule turns on — when its own deadline is still in
    the future, so a backlog flush delivering an already-aged report recovers
    nothing. A negative age is a write stamped in the future: not evidence.

    ``in_grace`` is the bounded startup grace. It makes a configured room
    provisionally eligible while it awaits its first witnessed report; it never
    claims the current value is fresh, and it never excuses an invalid one.
    """
    if not valid:
        return HEALTH_INVALID
    if max_age is None:
        return HEALTH_CADENCE_UNKNOWN
    if evidence_age is not None and 0.0 <= evidence_age < max_age:
        return HEALTH_HEALTHY
    if in_grace:
        return HEALTH_AWAITING
    return HEALTH_STALE


def season_lockouts(
    *, outdoor_high: float | None, heat_above: float, cool_below: float
) -> tuple[bool, bool]:
    """Derive (heat_lockout, cool_lockout) from the local outdoor daily-high temp.

    The decision is driven by *local weather*, not the calendar: pass the forecast
    daily high (from a weather entity) or an outdoor temperature reading.

    * high >= ``heat_above`` -> warm season -> heat_lockout on (don't actively heat).
    * high <= ``cool_below`` -> cold season -> cool_lockout on (don't actively cool).
    * in between (shoulder season) -> neither -> normal heat + cool.

    The gap between the two thresholds is the hysteresis band — the forecast has to
    swing across the whole shoulder zone to flip a lockout, so it won't chatter.
    ``None`` (no signal — weather unavailable) yields no lockout (safe: normal auto).
    """
    if outdoor_high is None:
        return (False, False)
    if outdoor_high >= heat_above:
        return (True, False)
    if outdoor_high <= cool_below:
        return (False, True)
    return (False, False)


def shared_mode(
    *,
    demands: list[str],
    current: str,
    allowed: bool,
    resting: str | None = None,
) -> str:
    """Choose the shared mode (cool|heat). Mirrors the mxz_plan state template.

    ``demands`` is one demand per zone in PRIORITY ORDER (index 0 = highest).
    ``current`` must already be normalized to cool|heat (cold start -> cool).
    ``allowed`` is the hysteresis gate. In a standoff (some zones calling cool
    while others call heat) the HIGHEST-PRIORITY calling zone wins — the N-zone
    generalization of "the primary room wins".

    ``resting`` biases the mode used when NO room is calling. ``None`` (or any
    non cool|heat value) keeps the original behavior — rest at ``current`` (the
    last called mode). Set it to cool|heat to always settle there when idle; a
    genuine opposite demand still flips the mode (the bias only changes the
    neutral fallback, gated by the same hysteresis as any other flip).
    """
    rest = resting if resting in (MODE_COOL, MODE_HEAT) else current
    any_cool = MODE_COOL in demands
    any_heat = MODE_HEAT in demands
    standoff = any_cool and any_heat

    if standoff:
        proposed = next(
            (d for d in demands if d in (MODE_COOL, MODE_HEAT)), current
        )
    elif any_cool:
        proposed = MODE_COOL
    elif any_heat:
        proposed = MODE_HEAT
    else:
        proposed = rest

    still = (current == MODE_COOL and any_cool) or (current == MODE_HEAT and any_heat)
    if proposed != current and allowed and (standoff or not still):
        return proposed
    return current


# Demand.priority tiers: lower sorts first / wins a standoff. These are the
# ONLY thing that encodes "a real demand beats a vote" -- it falls out of
# comparing (PRIORITY_VOTE, i) > (PRIORITY_DEMAND, j) for any i, j, not a
# hardcoded rule, which is what lets PRIORITY_SAFETY override either kind
# without a special case (see arbitrate()'s docstring).
PRIORITY_SAFETY = -1
PRIORITY_DEMAND = 0
PRIORITY_VOTE = 1
# A tripped freeze floor (a disabled room's heat at its floor) outranks every
# other want, a PRIORITY_SAFETY one included: freeze first. A tripped overheat
# ceiling sits at PRIORITY_SAFETY with its room index, so it settles against a
# safety room by room order. Nothing else ever carries this tier.
PRIORITY_FLOOR_HEAT = -2


@dataclass(frozen=True)
class Demand:
    """One zone's current want for the shared compressor's direction this tick.

    ``direction`` is :data:`MODE_COOL`, :data:`MODE_HEAT`, or ``None`` (wants nothing).
    ``priority`` is ``(tier, configured_index)``; lower sorts first.
    ``read_only`` is True for an advisory want (a vote): it is never sent to the
    voting head, though a safety vote that wins can still park another room's
    head (see :class:`Grant`). False for a coordinated room's own demand.
    """

    direction: str | None
    priority: tuple[int, int]
    read_only: bool = False


@dataclass(frozen=True)
class Grant:
    """The arbitrated outcome for one tick.

    ``read_only`` is True only when THIS tick's answer differs from what it
    would be considering commanding Demands alone (an advisory want is the
    reason the answer moved, not merely that one happens to be active) AND
    the ordinary vote gate was open: no commanding Demand had a direction
    and no room was engaged. Such a flip runs no coordinated head into a
    reversal, so it is exempt from the dwell stamp. A safety vote that moves
    the answer while a coordinated room is calling or engaged mid-run parks
    that room's head, so it reads False and is stamped like any other flip.
    Once the answer settles (nothing is actively changing it any more), this
    reads False again, even while a vote is still what the settled answer
    agrees with -- there is nothing left to exempt from the dwell clock once
    nothing is proposing a change.

    ``by_safety`` is True while a PRIORITY_SAFETY (or freeze floor) Demand
    with a direction is active and that direction is the answer. It does not
    claim the safety tier changed the answer: the same answer may follow from
    order or the dwell alone.
    """

    direction: str
    read_only: bool
    by_safety: bool


def arbitrate(
    demands: list[Demand],
    *,
    engaged: bool,
    current: str,
    allowed: bool,
    resting: str | None = None,
) -> Grant:
    """:func:`shared_mode`, generalized over a priority-ordered Demand list.

    Reproduces the original ``shared_mode_with_votes``'s precedence exactly
    as the degenerate case where every Demand sits at PRIORITY_DEMAND or
    PRIORITY_VOTE and none at PRIORITY_SAFETY. What it fixes: today's "a
    real demand always fully preempts every vote, unconditionally" was a
    hardcoded wall, not a priority relationship -- so a hands-off zone's
    safety-tier vote could never preempt an enabled zone's merely-comfort
    demand, no matter how urgently it needed to. Here a PRIORITY_SAFETY
    Demand -- from EITHER a coordinated room or a hands-off vote -- outranks
    every ordinary Demand structurally, because ``(-1, i) < (0, j)`` always,
    not via a special case, and participates regardless of ``engaged`` or
    whether anything else is commanding.

    ``engaged`` mirrors the original gate's other half: a room mid-run
    toward its target (its own demand back inside the band, but still
    engaged) must not have the mode taken from it by a mere vote, even
    though its demand itself has gone quiet. It only ever suppresses an
    ORDINARY vote (PRIORITY_VOTE, read_only) -- never a safety one.

    A tripped floor is one more commanding Demand: a freeze floor at
    PRIORITY_FLOOR_HEAT (ahead of every safety want), an overheat ceiling at
    PRIORITY_SAFETY. A standoff goes to the first ranked direction, so heat
    from a floor beats cool from a ceiling whatever their rooms' order, and
    two trips of one kind settle by room order.
    """
    ranked = sorted(demands, key=lambda d: d.priority)
    commanding = [d for d in ranked if not d.read_only]
    safety_votes = [
        d for d in ranked if d.read_only and d.priority[0] <= PRIORITY_SAFETY
    ]
    ordinary_votes = [
        d for d in ranked if d.read_only and d.priority[0] > PRIORITY_SAFETY
    ]
    commanding_silent = not any(d.direction for d in commanding)
    gate_open = commanding_silent and not engaged
    active = sorted(
        commanding + safety_votes + (ordinary_votes if gate_open else []),
        key=lambda d: d.priority,
    )
    full = shared_mode(
        demands=[d.direction for d in active if d.direction],
        current=current, allowed=allowed, resting=resting,
    )
    by_safety = any(
        d.direction == full and d.priority[0] <= PRIORITY_SAFETY for d in active
    )
    if not any(d.read_only and d.direction for d in active):
        return Grant(direction=full, read_only=False, by_safety=by_safety)
    plain = shared_mode(
        demands=[d.direction for d in active if d.direction and not d.read_only],
        current=current, allowed=allowed, resting=resting,
    )
    # Exempt from the dwell stamp only when the vote gate was open: no
    # commanding room calling AND none still engaged mid-run. A safety vote
    # that takes the mode from a calling room or from an engaged run that is
    # coasting inside its band parks a running head -- a real reversal the
    # coordinator causes -- so it stamps like any other flip.
    read_only = full != plain and gate_open
    return Grant(direction=full, read_only=read_only, by_safety=by_safety)


def floor_call(
    *,
    temp: float,
    sensor_ok: bool,
    low: float | None,
    high: float | None,
    prior: str | None,
    margin: float,
) -> str | None:
    """A disabled room's floor want: heat, cool, or None. Pure.

    Untripped (``prior`` None): heat below ``low``, cool above ``high``.
    Tripped heat holds until the room reaches ``low + margin`` (the clear
    line), tripped cool until ``high - margin``; then the floor is untripped
    and re-trips only past the floor again. The margin is the whole minimum
    run: no separate timer. Without a usable reading there is no want, from
    any prior: a floor never acts blind. ``None`` for ``low``/``high`` is no
    floor/ceiling.
    """
    if not sensor_ok:
        return None
    if prior == MODE_HEAT and low is not None and temp < low + margin:
        return MODE_HEAT
    if prior == MODE_COOL and high is not None and temp > high - margin:
        return MODE_COOL
    if low is not None and temp < low:
        return MODE_HEAT
    if high is not None and temp > high:
        return MODE_COOL
    return None


@dataclass(frozen=True)
class FloorRun:
    """One tripped floor's progress record (the no-progress cap's state).

    ``mark`` is the best reading credited this run (highest for heat, lowest
    for cool), moved only by a whole ``step``; ``driven`` is how many seconds
    MXZ has driven the head in the floor's direction since the mark last
    moved; ``worst`` is the worst reading (lowest for heat) since the mark last
    moved, frozen once the run stalls, so only a genuinely new low since the
    stall began re-arms it.
    """

    direction: str
    mark: float
    worst: float
    driven: float = 0.0
    stalled: bool = False


def floor_progress(
    run: FloorRun, *, temp: float, driven_for: float, step: float, cap: float
) -> FloorRun:
    """Advance one floor run by one reading. Pure.

    ``driven_for`` is the time the head was actually driven in the run's
    direction since the last reading (0 while it waited). A reading one
    ``step`` past the mark moves the mark there and restarts the clock; a
    clock that reaches ``cap`` stalls the run. A stalled run re-arms (as a
    new run) only on a reading one step past its worst one since the mark
    last moved (decision 11: "below the lowest reading since the stall");
    reaching the clear line is the caller's other way out
    (:func:`floor_call`). ``worst`` restarts at each gain and is frozen while
    stalled, so a room that keeps cooling in sub-step increments re-arms
    once it is one step below it. A sensor flickering between two adjacent
    values does neither: its low is already the window's worst.
    """
    sign = 1.0 if run.direction == MODE_HEAT else -1.0
    if run.stalled:
        if sign * (run.worst - temp) >= step - 1e-9:
            return FloorRun(direction=run.direction, mark=temp, worst=temp)
        return run
    gained = sign * (temp - run.mark) >= step - 1e-9
    worst = run.worst if sign * (temp - run.worst) >= 0 else temp
    if gained:
        return replace(run, mark=temp, worst=temp, driven=0.0)
    driven = run.driven + max(driven_for, 0.0)
    return replace(run, worst=worst, driven=driven, stalled=driven >= cap)


def setpoints(
    *,
    mode: str,
    target: float,
    eco: bool,
    clamp_min: float,
    clamp_max: float,
    band: float = 2.0,
    step: float = 1.0,
    eco_cool: tuple[float, float] = (float(ECO_COOL_LOW), float(ECO_COOL_HIGH)),
    eco_heat: tuple[float, float] = (float(ECO_HEAT_LOW), float(ECO_HEAT_HIGH)),
) -> tuple[float, float]:
    """Return (low, high) setpoint edges, clamped. Mirrors the actuator's edge math.

    Unit-agnostic: ``band``/``step``/``eco_*`` carry the temperature unit. The
    defaults reproduce the original °F behavior (2° band, whole-degree steps,
    76/78 & 59/61 eco edges); a °C caller passes a 1° band, 0.5° step, and metric
    eco edges. Edges are rounded to ``step`` and clamped to [clamp_min, clamp_max].
    """
    if eco:
        return eco_cool if mode == MODE_COOL else eco_heat
    tc = max(min(target, clamp_max), clamp_min)
    if mode == MODE_COOL:
        low, high = max(tc - band, clamp_min), tc
    else:
        low, high = tc, min(tc + band, clamp_max)
    return (_round_to(low, step), _round_to(high, step))


def _round_to(value: float, step: float) -> float:
    """Round ``value`` to the nearest multiple of ``step`` (step<=0 -> unchanged)."""
    if step <= 0:
        return value
    return round(value / step) * step


def fan_for_delta(
    *,
    delta: float,
    cur_idx: int,
    up_at: tuple[float, ...],
    down_at: tuple[float, ...],
    max_idx: int,
) -> int:
    """Pick a fan-ladder INDEX (0..) from how far the room is off-target.

    Tesla-style: a big ``delta`` steps the fan up toward ``max_idx``; as the room
    closes on target the fan eases down. ``up_at``/``down_at`` carry a 0.5 °F
    hysteresis band so the speed doesn't chatter on the boundary. The caller owns
    persisting ``cur_idx`` between calls (per head).
    """
    # step UP as far as delta allows, never above max_idx
    while cur_idx < len(up_at) and cur_idx < max_idx and delta >= up_at[cur_idx]:
        cur_idx += 1
    # step DOWN only once delta clears the lower rung's down-threshold
    while cur_idx > 0 and delta < down_at[cur_idx - 1]:
        cur_idx -= 1
    return min(cur_idx, max_idx)


def head_action(*, engage: str, mode: str, eco: bool, idle: str = MODE_FAN_ONLY) -> str:
    """Map a head's engage state to the mode to command. Mirrors p_act/s_act.

    ``idle`` is the pre-resolved parking mode (fan_only or off) for a head that
    is neither disabled nor running — satisfied rooms and standoff losers both
    land there. Precedence: disabled > eco-satisfied > running > idle.
    """
    if engage == MODE_OFF:
        return MODE_OFF
    if eco and engage == ENGAGE_SATISFIED:
        return MODE_OFF
    if engage == mode:
        return mode
    return idle
