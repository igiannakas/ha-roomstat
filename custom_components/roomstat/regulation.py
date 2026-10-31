"""
Regulation for Roomstat: full-power heat-ups, and a feedforward + PI hold
near the target with a firm stop ahead of an overshoot.

The Tado valve
--------------
A Tado thermostat runs its own controller on its own reading, which sits
on the hot radiator.  Recorder data (October 2026, Tado V3+ over HomeKit,
heating % from the cloud) shows the valve fully open only when its setpoint
is about 5 °C or more above its own reading; at +1..+3 °C it throttles and
pulses (on average 30-40 % in the living room), because it anticipates on
its own reading, which climbs fast next to the radiator.  It only stays shut
at about 2 °C or more below its reading; just below it, it re-opens by
itself as the radiator cools.

Feedforward
-----------
Roomstat measures the *offset* between the Tado's reading and the room
sensor and adds it to every command, so the Tado's own controller acts on
the room's error rather than on the radiator's heat:

    command = setpoint + (tado_reading - room) + kp * error + integral

    tado_demand = command - tado_reading = (1 + kp) * error + integral

so the gain on the room error is (1 + kp), not kp.

Modes
-----
Strong commands where they matter, fine ones near the target:

* ``heat``: a deficit of ``heatup_enter_c`` or more (a heat-up).  Command
  the maximum (one command, valve fully open) until the heat still in the
  radiator is predicted to carry the room to the target:
  error - Td * rise_rate <= ``heatup_exit_c``.  Td is the braking time
  (configured, or learned by auto-tune from exactly this coast).
* ``coast``: after a heat-up (or a full push), the minimum (valve shut)
  while the room is below the target and still rising, so the leftover
  heat lands it.  A
  coast that reaches the target becomes a stop; one that stalls short
  hands over to the hold.
* ``hold``: near the target, the feedforward + PI command above.  A Tado
  answers small pushes late and partly, so kp is high (4 by default).  A
  hold whose push (tado_demand) has reached ``hold_push_c`` drives the
  valve at or near full output, as a heat-up does: until it leaves the
  hold it ends on the heat-up's exit and coasts (``full_push``).
* ``stop``: the minimum, ahead of an overshoot: once the room is predicted
  to be ``stop_over_c`` over the target within the braking time Td (at
  least ``stop_lead_s``), the radiator's coast being the same however the
  heat got there.  Released (back to hold) only once the room is predicted
  to need heat: ``release_below_c`` below the target within
  ``release_lead_s``.  A falling room is released that far ahead, so the
  radiator is warm again in time; a flat one at or just above the target
  stays shut.  A hold entered while the room is still above the target
  commands just below the valve's own reading, where a Tado trickles by
  itself (17 Oct 2026: 16 % for 10 min, +0.2-0.4 °C for the next hour or
  two); the same happened after every restart, when a shut valve resumed
  as coast and fell through to hold.

* ``dose`` (dosed top-ups, dosing.py, an option per room, 2.15.0): with the
  option on there are two phases and no hold (owner, 31 Oct 2026): a
  heat-up (a shortfall of ``heatup_enter_c`` or more: full power, ended on
  its predicted coast, then the coast while the room still rises short of
  the target, the ordinary rules) and the *dosing phase* (everything else:
  the valve shut, or full-power pulses of a computed length, then the
  minimum).  A top-up from a shut valve takes 8-12 min to show in the
  room, so the hold, a derivative or the valve look-ahead, which all act
  on what has already shown, ran the valve 10-30 min for a 2-3-min
  shortfall (19 living-room top-ups, 25-30 Oct 2026: peaks +0.23 median,
  +0.94 worst).  A Smith predictor (dosing.py) tracks the rise still in
  flight from heat already sent; the regulator decides on the *virtual*
  error (error - in flight), fires when that error plus the cooling over
  the visible dead time (theta + tau / 2) reaches ``release_below_c``
  (also at or just above the target: a 25 °C pulse cannot trickle as a
  hold command there would) and a minimum pulse would not land more than
  ``stop_over_c`` over, and ends the pulse once it covers the virtual
  shortfall plus the cooling until the heat has landed (theta + 2 tau;
  a room warming by itself needs that much less), after at least
  ``DoseConfig.pulse_min_s`` (unless the target was lowered since it
  started), at its cap (:func:`dose_cap_s`, set when it starts: the same
  dose worked out on the room's own heating rate, without the aim bias and
  on its own cooling observer, plus ``cap_margin_c``: a wrong model cannot
  run past it), on the heat-up's own exit once its heat shows, or at once on
  the measured stop rule.  The next pulse waits theta + 2 tau after the last
  one started and theta + tau / 2 after it ended: heat still in flight is
  already taken off the virtual error, so waiting for its tail to fade as
  well only let the room cool past the band (0.18 under at 0.8 °C/h).  A
  pulse may also fire early on another zone's burn or with another room's
  pulse (the boiler is on/off: radiators share its burns), only when a
  minimum pulse would land at most ``DoseConfig.fire_max_overshoot_c``
  above the target.  Design: project doc
  claude/roomstat-smith-predictor-dosed-topups-2026-10-30.md.

Valve look-ahead (lookahead.py, an option per room): a Tado valve looks
ahead on how fast its own reading moves, so after a heat-up, with the
radiator cooling and its reading sliding, a hold command just over the
reading opens it hard.  With a learned look-ahead, a hold command is
lowered by it times the reading's fall (one-sided: only while it falls,
capped), so the valve answers the push as if its reading were flat.  The
push that decides a full push is the uncorrected one.

The heat-up's end and the stop use the room's rise rate only while it
rises (one-sided); only the release looks at a fall, and it only hands back
to the hold.  A reading that fell ``fast_fall_c`` since the previous cycle
is a draught or a glitch, not a heating need: that cycle keeps the previous
reading (``RegulationResult.room_held``).  In two rooms fitted to real data
this holds within 0.03-0.05 °C RMS on average (only full/shut commands:
0.06-0.12; parameters.py).

Integral
--------
The integral only moves in hold, and only near the target:

1. It freezes whenever the output is saturated (clamped at max or min) in
   the same direction as the error, and in every other mode.
2. It only accumulates when |error| < deadband.  Outside, it *decays*
   toward zero, so a heat-up never winds it up.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .parameters import DoseConfig, RegulationConfig

_LOGGER = logging.getLogger(__name__)

# Output is considered "not saturated" when clamping error is below this threshold (°C).
# 0.01 °C is 1/10 of the Tado quantisation step (0.1 °C) – effectively zero.
_SATURATION_TOLERANCE_C = 0.01

# Modes (see the module docstring).
MODE_HOLD = "hold"
MODE_DOSE = "dose"            # a dosed top-up pulse (the maximum), see dosing.py
MODE_HEAT = "heat"
MODE_COAST = "coast"
MODE_STOP = "stop"
MODES = (MODE_HOLD, MODE_HEAT, MODE_COAST, MODE_STOP, MODE_DOSE)


# ---------------------------------------------------------------------------
# State & result data classes
# ---------------------------------------------------------------------------

@dataclass
class RegulationState:
    """Mutable state carried between regulation cycles."""

    integral_c: float = 0.0
    mode: str = MODE_HOLD
    last_room_c: float | None = None   # the previous cycle's raw reading
    full_push: bool = False            # this hold's push reached hold_push_c
    # Dosed top-ups (dosing.py): the room is in the dosing phase; the
    # running pulse's start, the target it is for and its cap; the last
    # pulse's start, end and target (the refractory, which a raised target
    # skips).  ``settled`` keeps its 2.15.0 name, which the
    # restore-state data uses.
    settled: bool = False
    pulse_start_ts: float | None = None
    last_pulse_start_ts: float | None = None
    pulse_setpoint_c: float | None = None
    pulse_cap_s: float | None = None
    last_pulse_end_ts: float | None = None
    last_pulse_setpoint_c: float | None = None


@dataclass
class RegulationResult:
    """Output of a single regulation cycle."""

    target_for_tado_c: float       # absolute temperature to send to Tado
    feedforward_offset_c: float    # measured sensor offset  (diagnostic)
    p_correction_c: float          # proportional correction (diagnostic)
    i_correction_c: float          # integral correction     (diagnostic)
    error_c: float                 # room error: setpoint - room_temp
    is_saturated: bool             # True when output was clamped
    new_state: RegulationState
    mode: str = MODE_HOLD          # heat / coast / hold (PI) / stop
    room_held: bool = False        # the reading fell fast: the previous one was used
    full_push: bool = False        # a hold that ends like a heat-up (hold_push_c)
    lookahead_c: float = 0.0       # taken off the hold command for the valve's look-ahead
    # Dosed top-ups (filled while dosing; see the module docstring).
    virtual_error_c: float = 0.0   # error - the rise still in flight
    need_c: float = 0.0            # what a pulse (still) has to cover
    pulse_s: float = 0.0           # how long the running pulse has run
    pulse_cap_s: float | None = None  # the running pulse's cap (s)
    ready_in_s: float | None = None  # seconds to the dosed release (ready, falling)
    settled: bool = False          # in the dosing phase (stop or dose; the 2.15.0 name)
    fired_by: str | None = None    # "release" / "burn" / "together", on the cycle a pulse starts
    dose_end: str | None = None    # covered / max / landing / stop_rule / heat_up, as it ends


def resume_mode(trv_setpoint_c: float | None, config: RegulationConfig) -> str:
    """The mode to start in, from the setpoint the TRV holds.

    A reload (e.g. a preset temperature change) or a restart in the middle of
    a heat-up carries it on (and it ends as usual), and one while the valve
    is shut lets the coast finish: it becomes a stop if the room is at or
    above the target, a hold if it stalled short.  Any other setpoint starts
    in hold.  An unknown one (the TRV entity not loaded yet after a restart,
    an off valve) starts in stop: a hold entered 0.07 above the target
    pushed the study's valve into the grey zone right after the 2.8.0
    restart (18 Oct 2026), and a stop still releases, or heats, as soon as
    the room needs it.  The entity re-evaluates this on the first cycle that
    sees the valve.
    """
    if trv_setpoint_c is None or not math.isfinite(trv_setpoint_c):
        return MODE_STOP
    if trv_setpoint_c >= config.max_target_c - 0.05:
        return MODE_HEAT
    if trv_setpoint_c <= config.min_target_c + 0.05:
        return MODE_COAST
    return MODE_HOLD


def lookahead_correction_c(
    lookahead_s: float,
    trv_slope_c_per_s: float | None,
    deadband_c_per_h: float,
    cap_c: float,
) -> float:
    """How much a hold command is lowered for the valve's look-ahead (0 or
    more, °C; lookahead.py): the look-ahead times the fall of the valve's
    reading beyond the deadband, at most ``cap_c``.  Nothing for a flat,
    rising or unknown reading."""
    values = (lookahead_s, trv_slope_c_per_s, deadband_c_per_h, cap_c)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        return 0.0
    if lookahead_s <= 0 or cap_c <= 0:
        return 0.0
    fall = -float(trv_slope_c_per_s) - max(0.0, deadband_c_per_h) / 3600.0
    if fall <= 0:
        return 0.0
    return min(cap_c, lookahead_s * fall)



# ---------------------------------------------------------------------------
# Dosed top-ups (dosing.py): pure helpers shared with the room's doser
# ---------------------------------------------------------------------------

def _finite(*values: object) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def dose_fall_c_per_s(room_slope_c_per_s: float | None, deadband_c_per_h: float) -> float:
    """The room's fall rate (°C/s, 0 or more) for the dose: one-sided, with
    the regulator's slope deadband; 0 for a flat, rising or unknown room."""
    if not _finite(room_slope_c_per_s, deadband_c_per_h):
        return 0.0
    fall = -float(room_slope_c_per_s)
    return fall if fall * 3600.0 >= max(0.0, deadband_c_per_h) else 0.0


def dose_rise_c_per_s(room_slope_c_per_s: float | None, deadband_c_per_h: float) -> float:
    """The room's own rise (°C/s, 0 or more) beyond the slope deadband: a
    room warming by itself (sun, people) needs that much less dose.  The
    other side of :func:`dose_fall_c_per_s`, and only ever less heat."""
    if not _finite(room_slope_c_per_s, deadband_c_per_h):
        return 0.0
    rise = float(room_slope_c_per_s)
    return rise if rise * 3600.0 >= max(0.0, deadband_c_per_h) else 0.0


def dose_cap_s(cap_need_c: float, cap_gain_c_per_open_min: float | None,
               cfg: DoseConfig, *, running: bool = False) -> float:
    """The longest a pulse may run (s): the open time ``cap_need_c`` plus
    ``cap_margin_c`` takes at the room's own rise per open-minute at full
    power (``cap_gain_c_per_open_min``: the auto-tuner's heating rate / 60),
    within ``pulse_min_s`` and ``pulse_ceiling_s``; as a pulse from its
    command (the valve opens ``latency_s`` after it and shuts
    ``close_delay_s`` after the off), or with ``running`` the time still to
    run from now (the valve open).  Unknown room: ``pulse_max_s``.

    ``cap_need_c`` is the dose worked out on that rate (:func:`dose_cap_need_c`),
    not the model's: the cap is the safety net for a model that is wrong
    (owner, 31 Oct 2026: "if the room is short 0.8 C and the historical heat
    up rate is smaller ... adjust the max pulse to match what the room max
    need would be"), so a slow room short 0.8 may run long, a fast room short
    0.1 only a few minutes, and a pulse that reaches its cap lands about
    ``cap_margin_c`` over if the room's heating rate is right.  Set when the
    pulse starts.  The room's cooling while the pulse runs is not added: in
    cold weather that cuts a long pulse the model sizes right, which then
    lands short and takes another go (the owner's preference over landing
    hot).  House simulation (31 Oct 2026, four seeds, real and real - 4 °C):
    with that cooling counted, or with the cap judged again every cycle, the
    living room's daily peaks rose +0.22 -> +0.25 / +0.17 -> +0.20 for less
    time under -0.2 (262 -> 239 min a day in the cold)."""
    if not _finite(cap_gain_c_per_open_min) or cap_gain_c_per_open_min <= 0:
        return cfg.pulse_max_s
    need = max(0.0, float(cap_need_c)) if _finite(cap_need_c) else 0.0
    open_min = (need + max(0.0, cfg.cap_margin_c)) / float(cap_gain_c_per_open_min)
    if running:
        return min(cfg.pulse_ceiling_s, max(0.0, 60.0 * open_min - cfg.close_delay_s))
    pulse_s = 60.0 * open_min + cfg.latency_s - cfg.close_delay_s
    return min(cfg.pulse_ceiling_s, max(cfg.pulse_min_s, pulse_s))


def dose_cap_need_c(
    error_c: float, cap_inflight_c: float, cap_slope_c_per_s: float | None,
    horizon_s: float, deadband_c_per_h: float,
) -> float:
    """The dose the cap is sized on (°C): the measured shortfall less the
    heat in flight at the cap's rise per open-minute, plus the cooling (or
    less the room's own rise) until the heat has landed, from the cap's own
    observer (dosing.RoomDoser: the room less Roomstat's heat at that rise
    per open-minute).  No aim bias and nothing of the learned gain: the
    cap must not grow with the model it guards."""
    e = float(error_c) if _finite(error_c) else 0.0
    inflight = max(0.0, float(cap_inflight_c)) if _finite(cap_inflight_c) else 0.0
    fall = dose_fall_c_per_s(cap_slope_c_per_s, deadband_c_per_h)
    rise = dose_rise_c_per_s(cap_slope_c_per_s, deadband_c_per_h)
    return dose_need_c(e - inflight, fall, horizon_s) - rise * max(0.0, horizon_s)


def dose_need_c(
    virtual_error_c: float, fall_c_per_s: float, horizon_s: float,
    aim_under_c: float = 0.0, bias_c: float = 0.0,
) -> float:
    """What a pulse still has to cover (°C): the virtual shortfall (heat in
    flight already taken off) less the aim, plus the room's cooling until
    the heat has landed (``horizon_s`` = theta + 2 tau).  Non-finite inputs
    count as 0."""
    e = float(virtual_error_c) if _finite(virtual_error_c) else 0.0
    fall = max(0.0, float(fall_c_per_s)) if _finite(fall_c_per_s) else 0.0
    horizon = max(0.0, float(horizon_s)) if _finite(horizon_s) else 0.0
    aim = float(aim_under_c) if _finite(aim_under_c) else 0.0
    bias = float(bias_c) if _finite(bias_c) else 0.0
    return (e - aim + bias) + fall * horizon


def dose_ready_in_s(
    virtual_error_c: float, fall_c_per_s: float, lead_s: float, release_below_c: float,
) -> float | None:
    """Seconds until the dosed release would fire (the virtual error plus
    the cooling over the visible dead time reaching ``release_below_c``):
    0 when it already would, None for a room that is not falling (it never
    becomes due by itself)."""
    if not _finite(virtual_error_c, fall_c_per_s, lead_s, release_below_c):
        return None
    if virtual_error_c + lead_s * max(0.0, fall_c_per_s) >= release_below_c:
        return 0.0
    if fall_c_per_s <= 0:
        return None
    return (release_below_c - virtual_error_c - lead_s * fall_c_per_s) / fall_c_per_s


def dose_floor_landing_c(need_c: float, gain_c_per_open_min: float, floor_open_min: float) -> float:
    """Where a minimum pulse would land relative to the target (°C, positive
    is above): the rise it delivers less what is needed."""
    if not _finite(need_c, gain_c_per_open_min, floor_open_min):
        return 0.0
    return gain_c_per_open_min * max(0.0, floor_open_min) - need_c



# ---------------------------------------------------------------------------
# Regulator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Outlook:
    """The predictions one cycle's mode is decided on (one-sided, see the
    module docstring).  Errors are setpoint - room: positive is below."""

    rising_c_per_s: float     # the rise beyond the slope deadband, 0 if none
    falling_c_per_s: float    # the fall beyond the slope deadband, 0 if none
    lead_s: float             # the braking time Td (0: no lead)
    stop_lead_s: float        # max(stop_lead_s, Td)
    landing_c: float          # left to go once the radiator's heat is out: error - Td x rise
    stop_landing_c: float     # error - stop_lead x rise (<= -stop_over_c: stop)
    release_c: float          # error + release_lead x fall (>= release_below_c: release)


def outlook(error_c: float, room_slope_c_per_s: float | None,
            config: RegulationConfig, td_s: float | None = None) -> Outlook:
    """The heat-up end, stop and release predictions for this error and slope.

    The regulator decides on these, and the activity log explains its
    decisions with the same numbers (``td_s``: the braking time the cycle
    used, if the tuner has changed it since)."""
    rising = falling = 0.0
    if room_slope_c_per_s is not None and math.isfinite(room_slope_c_per_s):
        deadband = config.slope_deadband_c_per_h / 3600.0
        rising = max(0.0, room_slope_c_per_s - deadband)
        falling = max(0.0, -room_slope_c_per_s - deadband)
    if td_s is None:
        td_s = config.tuning.td_s
    lead_s = td_s if math.isfinite(td_s) and td_s > 0 else 0.0
    # The stop looks at least as far ahead as a heat-up: the radiator's
    # coast (Td) is the same however the heat got there.
    stop_lead_s = max(config.stop_lead_s, lead_s)
    return Outlook(
        rising_c_per_s=rising,
        falling_c_per_s=falling,
        lead_s=lead_s,
        stop_lead_s=stop_lead_s,
        # Where the room ends up once the heat still in the radiator is out.
        landing_c=error_c - lead_s * rising,
        stop_landing_c=error_c - stop_lead_s * rising,
        release_c=error_c + config.release_lead_s * falling,
    )


@dataclass(frozen=True)
class _DoseStep:
    """One cycle's decision with dosed top-ups on: a heat-up phase mode
    (heat, coast; ``settled`` False) or the dosing phase's (stop, dose)."""

    mode: str
    settled: bool
    pulse_start_ts: float | None
    last_pulse_start_ts: float | None
    last_pulse_end_ts: float | None
    virtual_error_c: float
    need_c: float
    last_pulse_setpoint_c: float | None = None
    pulse_setpoint_c: float | None = None
    pulse_limit_s: float | None = None     # the running pulse's cap (kept in the state)
    pulse_cap_s: float | None = None       # the same, reported
    pulse_s: float = 0.0
    ready_in_s: float | None = None
    fired_by: str | None = None
    dose_end: str | None = None


def _finite_or(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else default


class FeedforwardPiRegulator:
    """Roomstat's regulator (see the module docstring)."""

    def __init__(self, config: RegulationConfig) -> None:
        self.cfg = config

    @staticmethod
    def _next_mode(
        mode: str, error_c: float, room_slope_c_per_s: float | None,
        config: RegulationConfig, *, push_c: float = 0.0, full_push: bool = False,
    ) -> tuple[str, bool]:
        """The mode for this cycle, and whether a hold is a full push (see
        the module docstring).  ``push_c``: this cycle's hold command over
        the valve's reading; ``full_push``: the previous cycle's flag."""
        o = outlook(error_c, room_slope_c_per_s, config)
        if mode == MODE_HEAT:
            return (MODE_COAST if o.landing_c <= config.heatup_exit_c else MODE_HEAT), False
        if error_c >= config.heatup_enter_c and o.landing_c > config.heatup_exit_c:
            return MODE_HEAT, False
        if mode == MODE_COAST and error_c > 0 and o.rising_c_per_s > 0:
            return MODE_COAST, False
        # A coast that has reached the target (or a shut valve resumed after
        # a restart with the room at or above it) stays shut until the room
        # needs heat; falling through to hold would park the command just
        # below the valve's reading, where it trickles.
        if mode == MODE_COAST and error_c <= 0:
            return MODE_STOP, False
        # Near the target: fine adjustments, with a firm stop ahead of an
        # overshoot that lasts until the room needs heat again.
        if o.stop_landing_c <= -config.stop_over_c:
            return MODE_STOP, False
        if mode == MODE_STOP and o.release_c < config.release_below_c:
            return MODE_STOP, False
        # A hold that has pushed hard has filled the radiator as a heat-up
        # does: it ends on the heat-up's exit, then coasts.  Until it leaves
        # the hold (a hold entered afresh starts gentle).
        full_push = (full_push and mode == MODE_HOLD) or push_c >= config.hold_push_c
        if (
            full_push and error_c > 0 and o.rising_c_per_s > 0
            and o.landing_c <= config.heatup_exit_c
        ):
            return MODE_COAST, False
        return MODE_HOLD, full_push

    def _dose_step(
        self, state: RegulationState, prev_mode: str, error_c: float,
        room_slope_c_per_s: float | None, setpoint_c: float, now_ts: float, *,
        inflight_c: float, tail_c: float, gain_c_per_open_min: float,
        theta_s: float, tau_s: float, bias_c: float, fire_now: str | None,
        loss_slope_c_per_s: float | None = None,
        cap_gain_c_per_open_min: float | None = None,
        cap_inflight_c: float = 0.0,
        cap_loss_slope_c_per_s: float | None = None,
        unbooked_until_ts: float | None = None,
    ) -> _DoseStep:
        """The mode for this cycle with dosed top-ups on (see the module
        docstring and dosing.py).

        Two phases, nothing in between (owner, 31 Oct 2026): a heat-up — a
        shortfall of ``heatup_enter_c`` or more, full power until its coast
        is predicted to land it, then the coast while the room still rises
        short of the target, exactly the ordinary rules — and the dosing
        phase, everything else: the valve shut, or a full-power pulse sized
        by the model.  There is no hold."""
        cfg, dose = self.cfg, self.cfg.dose
        inflight = max(0.0, _finite_or(inflight_c, 0.0))
        tail = max(0.0, _finite_or(tail_c, 0.0))
        gain = _finite_or(gain_c_per_open_min, 0.0)
        cap_gain = (float(cap_gain_c_per_open_min)
                    if _finite(cap_gain_c_per_open_min) and cap_gain_c_per_open_min > 0
                    else None)
        bias = _finite_or(bias_c, 0.0)
        theta = _finite_or(theta_s, 0.0)
        tau = _finite_or(tau_s, 0.0)
        if theta <= 0:
            theta = dose.default_theta_s
        if tau <= 0:
            tau = dose.default_tau_s
        lead_s = theta + tau / 2.0         # the heat starts to show
        horizon_s = theta + 2.0 * tau      # the heat has landed
        e_star = error_c - inflight
        # The room's cooling for the dose: the slope of the room as if
        # unheated (dosing.RoomDoser's disturbance observer) when known, else
        # the room's own slope.  After each landing the room's own slope
        # still holds part of the rise, which reads as a slower cooling: the
        # next pulse came late and small (troughs -0.13..-0.19 in the entity
        # test before 2.15.0 shipped).  A room warming by itself (sun,
        # people) needs that much less: its rise only ever takes heat off.
        loss_slope = (loss_slope_c_per_s if _finite(loss_slope_c_per_s)
                      else room_slope_c_per_s)
        fall = dose_fall_c_per_s(loss_slope, cfg.slope_deadband_c_per_h)
        rise = dose_rise_c_per_s(loss_slope, cfg.slope_deadband_c_per_h)

        def need_for(virtual_error: float) -> float:
            return (dose_need_c(virtual_error, fall, horizon_s, dose.aim_under_c, bias)
                    - rise * horizon_s)

        need = need_for(e_star)
        # The cap's own dose: the room's heating rate, no aim bias, its own
        # observer (dose_cap_need_c).
        cap_slope = (cap_loss_slope_c_per_s if _finite(cap_loss_slope_c_per_s)
                     else room_slope_c_per_s)

        def cap_need(error: float) -> float:
            return dose_cap_need_c(error, cap_inflight_c, cap_slope, horizon_s,
                                   cfg.slope_deadband_c_per_h)

        # The longest a pulse may run whatever the cap says: the fixed
        # maximum while the room's heating rate is unknown.
        limit_s = dose.pulse_ceiling_s if cap_gain is not None else dose.pulse_max_s

        def cap_at_start() -> float:
            if cap_gain is None:
                return limit_s
            return min(limit_s, dose_cap_s(cap_need(error_c), cap_gain, dose))
        # Times from the future (the clock went back) count as now.
        last_pulse = (min(state.last_pulse_start_ts, now_ts)
                      if _finite(state.last_pulse_start_ts) else None)
        last_end = (min(state.last_pulse_end_ts, now_ts)
                    if _finite(state.last_pulse_end_ts) else None)
        last_sp = state.last_pulse_setpoint_c if _finite(state.last_pulse_setpoint_c) else None
        running = prev_mode == MODE_DOSE
        o = outlook(error_c, room_slope_c_per_s, cfg)

        # The heat-up phase: the ordinary rules.  A pulse running when the
        # shortfall reaches heatup_enter_c becomes the heat-up (its 25 °C
        # carries on, ended on the heat-up's predicted coast).
        heat_up_mode = None
        if prev_mode == MODE_HEAT:
            heat_up_mode = MODE_COAST if o.landing_c <= cfg.heatup_exit_c else MODE_HEAT
        elif error_c >= cfg.heatup_enter_c and o.landing_c > cfg.heatup_exit_c:
            heat_up_mode = MODE_HEAT
        elif prev_mode == MODE_COAST and error_c > 0 and o.rising_c_per_s > 0:
            heat_up_mode = MODE_COAST
        if heat_up_mode is not None:
            dose_end = None
            if running:
                dose_end = "heat_up"
                start = state.pulse_start_ts
                last_pulse = start if _finite(start) and start <= now_ts else now_ts
                last_end = now_ts
                last_sp = (state.pulse_setpoint_c if _finite(state.pulse_setpoint_c)
                           else setpoint_c)
            return _DoseStep(
                mode=heat_up_mode, settled=False, pulse_start_ts=None,
                last_pulse_start_ts=last_pulse, last_pulse_end_ts=last_end,
                last_pulse_setpoint_c=last_sp,
                virtual_error_c=e_star, need_c=need, dose_end=dose_end,
            )

        # The dosing phase.  The measured stop rule applies here too: a room
        # predicted to overshoot gets no heat, and a running pulse ends.
        stop_due = o.stop_landing_c <= -cfg.stop_over_c
        floor_open_min = (dose.pulse_min_s - dose.latency_s + dose.close_delay_s) / 60.0
        # A minimum pulse's landing on the stronger of the two estimates of
        # the room's rise per open-minute: a pulse that would overshoot even
        # at its shortest waits until the room needs it.
        floor_gain = max(gain, cap_gain or 0.0)

        if running:
            start = state.pulse_start_ts
            # Unknown: the pulse's command was held back (a change by hand
            # settling, a window's delay), so the pulse starts when it goes
            # out: counted from now and reported as fired again, so the
            # cycle that sends it starts its episode.  A start in the future
            # (the clock went back) counts from now too, but it is the same
            # pulse.
            restarted = start is None or not math.isfinite(start)
            if restarted or start > now_ts:
                start = now_ts
            pulse_sp = (state.pulse_setpoint_c
                        if _finite(state.pulse_setpoint_c) and not restarted else setpoint_c)
            pulse_s = now_ts - start
            cap = (min(state.pulse_cap_s, limit_s)
                   if _finite(state.pulse_cap_s) and state.pulse_cap_s > 0 and not restarted
                   else cap_at_start())
            if cap_gain is not None and setpoint_c > pulse_sp + 0.05:
                # A target raised mid-pulse: the cap is worked out again for
                # what is left to cover (review 3: the old one ended the
                # pulse 0.61 short), and the pulse is for the new target.
                cap = max(cap, min(limit_s, pulse_s + dose_cap_s(
                    cap_need(error_c - cap_gain * dose.close_delay_s / 60.0), cap_gain, dose,
                    running=True)))
                pulse_sp = setpoint_c
            need_run = need_for(e_star - tail)
            # A target lowered since the pulse started (by hand): the
            # minimum length no longer applies.
            lowered = setpoint_c < pulse_sp - 0.05
            may_end = pulse_s >= dose.pulse_min_s or lowered
            end = None
            if stop_due:
                end = "stop_rule"
            elif need_run <= 0 and may_end:
                end = "covered"
            elif pulse_s >= cap:
                end = "max"
            elif (may_end and pulse_s >= lead_s and error_c > 0
                  and o.rising_c_per_s > 0 and o.landing_c <= cfg.heatup_exit_c):
                # The heat-up's own exit, measured: once the pulse's heat
                # shows and the room rises onto its target, the radiator's
                # leftover heat lands it (as a full push ends, 2.10.4).
                end = "landing"
            if end is None:
                return _DoseStep(
                    mode=MODE_DOSE, settled=True, pulse_start_ts=start,
                    last_pulse_start_ts=last_pulse, last_pulse_end_ts=last_end,
                    last_pulse_setpoint_c=last_sp,
                    virtual_error_c=e_star, need_c=need_run, pulse_s=pulse_s,
                    pulse_setpoint_c=pulse_sp, pulse_limit_s=cap, pulse_cap_s=cap,
                    fired_by="release" if restarted else None,
                )
            return _DoseStep(
                mode=MODE_STOP, settled=True, pulse_start_ts=None,
                last_pulse_start_ts=start, last_pulse_end_ts=now_ts,
                last_pulse_setpoint_c=pulse_sp,
                virtual_error_c=e_star, need_c=need_run, pulse_s=pulse_s, dose_end=end,
            )

        # Shut (a stop, a coast just ended, or a hold resumed after a
        # reload): ready once the last pulse has had theta + 2 tau from its
        # start to land (the refractory) and theta + tau / 2 from its end to
        # show.  Heat still in flight needs no wait of its own: the virtual
        # error already subtracts it, and waiting for its tail to fade let
        # the room fall to 0.18 under the target at 0.8 °C/h.  A target
        # raised since the last pulse skips the wait for the same reason
        # (2.15.0 handed such a step to the hold at once).  Heat that is
        # booked nowhere (a hold's, a heat-up's) is not in the virtual
        # error: nothing fires until it has had theta + tau / 2 to show.
        refractory_s = min(horizon_s, dose.refractory_max_s)
        raised = last_sp is not None and setpoint_c > last_sp + 0.05
        unbooked = (min(unbooked_until_ts, now_ts) if _finite(unbooked_until_ts) else None)
        ready = (
            (raised or (
                (last_pulse is None or now_ts - last_pulse >= refractory_s)
                and (last_end is None or now_ts - last_end >= lead_s)))
            and (unbooked is None or now_ts - unbooked >= lead_s)
        )
        fired_by = None
        if ready and not stop_due:
            # A negative aim bias (landings above the target) shrinks the
            # dose, not the room's need of a first pulse.
            floor_landing = dose_floor_landing_c(need - min(0.0, bias), floor_gain,
                                                 floor_open_min)
            if (e_star + lead_s * (fall - rise) >= cfg.release_below_c
                    and floor_landing <= cfg.stop_over_c):
                fired_by = "release"
            elif (fire_now in ("burn", "together") and gain > 0
                  and floor_landing <= dose.fire_max_overshoot_c):
                fired_by = fire_now
        if fired_by is not None:
            cap = cap_at_start()
            return _DoseStep(
                mode=MODE_DOSE, settled=True, pulse_start_ts=now_ts,
                last_pulse_start_ts=last_pulse, last_pulse_end_ts=last_end,
                last_pulse_setpoint_c=last_sp,
                virtual_error_c=e_star, need_c=need, fired_by=fired_by,
                pulse_setpoint_c=setpoint_c, pulse_limit_s=cap, pulse_cap_s=cap,
            )
        return _DoseStep(
            mode=MODE_STOP, settled=True, pulse_start_ts=None,
            last_pulse_start_ts=last_pulse, last_pulse_end_ts=last_end,
            last_pulse_setpoint_c=last_sp,
            virtual_error_c=e_star, need_c=need,
            ready_in_s=(
                dose_ready_in_s(e_star - lead_s * rise, fall, lead_s, cfg.release_below_c)
                if ready else None
            ),
        )

    def compute(
        self,
        setpoint_c: float,
        room_temp_c: float,
        tado_internal_c: float,
        time_delta_s: float,
        state: RegulationState,
        room_slope_c_per_s: float | None = None,
        trv_slope_c_per_s: float | None = None,
        valve_lookahead_s: float = 0.0,
        *,
        dosing: bool = False,
        inflight_c: float = 0.0,
        tail_c: float = 0.0,
        gain_c_per_open_min: float = 0.0,
        theta_s: float = 0.0,
        tau_s: float = 0.0,
        bias_c: float = 0.0,
        now_ts: float | None = None,
        fire_now: str | None = None,
        loss_slope_c_per_s: float | None = None,
        cap_gain_c_per_open_min: float | None = None,
        cap_inflight_c: float = 0.0,
        cap_loss_slope_c_per_s: float | None = None,
        unbooked_until_ts: float | None = None,
    ) -> RegulationResult:
        """Run one regulation cycle and return the result.

        Parameters
        ----------
        setpoint_c:
            Desired room temperature set by the user.
        room_temp_c:
            Measured room temperature from the external sensor.
        tado_internal_c:
            Temperature reported by Tado's own sensor (on the radiator).
        time_delta_s:
            Seconds elapsed since the last cycle (0.0 on the very first run).
        state:
            Previous regulation state (integral, mode).
        room_slope_c_per_s:
            Robust estimate of the room temperature slope (°C/s), used by the
            heat-up and stop predictions.  None: no prediction (the room is
            taken as flat).
        trv_slope_c_per_s:
            The slope of the valve's own reading (°C/s), for the valve
            look-ahead.  None: unknown (no correction).
        valve_lookahead_s:
            The valve's learned look-ahead (lookahead.py); 0: off.
        dosing:
            Dosed top-ups on for this cycle (the option, and nothing that
            suspends them: window, boost); needs ``now_ts``.  Off: the
            ordinary modes, exactly as without the option (a pulse still
            running counts as a just-ended maximum, a coast).
        inflight_c:
            The rise still in flight from heat already sent (dosing.py's
            predictor, °C).
        tail_c:
            The rise the valve's close delay would still add if a running
            pulse were ended now (°C).
        gain_c_per_open_min, theta_s, tau_s, bias_c:
            The room's dose model as learned (dosing.DoseLearner).
        now_ts:
            This cycle's time (s): pulse lengths and the refractory time.
        fire_now:
            "burn" or "together": the pulse coordinator asks a ready room
            to fire early (dosing.PulseCoordinator); honoured only when a
            minimum pulse would land at most
            ``DoseConfig.fire_max_overshoot_c`` above the target.
        loss_slope_c_per_s:
            The slope of the room as if unheated (°C/s; the room less the
            rise of Roomstat's heat that has arrived, dosing.RoomDoser): the
            dose's cooling rate.  None: the room's own slope.  The heat-up
            and stop predictions always use the room's own slope.
        cap_gain_c_per_open_min:
            The room's own rise per open-minute at full power for the
            pulse's cap (the auto-tuner's heating rate / 60, else the
            learned gain; :func:`dose_cap_s`).  None: the fixed
            ``DoseConfig.pulse_max_s``.
        cap_inflight_c, cap_loss_slope_c_per_s:
            The heat in flight at that rise per open-minute (°C) and the
            room's slope less Roomstat's heat at it (°C/s; None: the room's
            own slope): the cap's own dose (:func:`dose_cap_need_c`).
        unbooked_until_ts:
            When heat booked nowhere (a hold's, a heat-up's) last went to
            the valve (now: still going); no pulse fires until it has had
            theta + tau / 2 to show.  None: none.
        """

        # 0. Guard: reject NaN/Inf inputs – they would corrupt all calculations
        for label, value in (
            ("setpoint", setpoint_c),
            ("room_temp", room_temp_c),
            ("tado_internal", tado_internal_c),
        ):
            if not math.isfinite(value):
                _LOGGER.error(
                    "Regulation aborted: %s is %s (not a finite number)",
                    label, value,
                )
                safe_target = (
                    max(self.cfg.min_target_c, min(self.cfg.max_target_c, setpoint_c))
                    if math.isfinite(setpoint_c)
                    else self.cfg.min_target_c
                )
                return RegulationResult(
                    target_for_tado_c=safe_target,
                    feedforward_offset_c=0.0,
                    p_correction_c=0.0,
                    i_correction_c=state.integral_c,
                    error_c=0.0,
                    is_saturated=False,
                    new_state=state,
                    mode=MODE_HOLD,
                )

        # 0b. A reading that fell fast_fall_c or more since the previous
        # cycle is a draught (a window opening) or a glitch, never a heating
        # need: this cycle keeps the previous reading, so neither a heat-up
        # nor a hold push starts on it.  A fall that goes on is accepted one
        # cycle later; a one-sample glitch never is.
        last_room = state.last_room_c
        room_held = (
            last_room is not None and math.isfinite(last_room)
            and room_temp_c <= last_room - self.cfg.fast_fall_c
        )
        room_used = last_room if room_held else room_temp_c

        # 1. Feedforward – compensate for sensor-placement offset
        feedforward_offset = tado_internal_c - room_used

        # 2. Room error
        error = setpoint_c - room_used

        # 3. The hold command: feedforward + P + I
        p_correction = self.cfg.tuning.kp * error
        i_correction = state.integral_c
        raw_command = setpoint_c + feedforward_offset + p_correction + i_correction

        # 4. Mode: heat (maximum), coast or stop (minimum), or hold.  The
        # hold's push over the valve's reading: (1 + kp) * error + integral.
        # With dosed top-ups on, a heat-up phase or the dosing phase (no
        # hold); otherwise (and with the option off) the ordinary modes,
        # where a pulse still running counts as a just-ended maximum: a
        # coast.
        prev_mode = state.mode if state.mode in MODES else MODE_HOLD
        dose_on = bool(dosing) and isinstance(now_ts, (int, float)) and math.isfinite(now_ts)
        dose_step = None
        if dose_on:
            dose_step = self._dose_step(
                state, prev_mode, error, room_slope_c_per_s, setpoint_c, float(now_ts),
                inflight_c=inflight_c, tail_c=tail_c,
                gain_c_per_open_min=gain_c_per_open_min, theta_s=theta_s,
                tau_s=tau_s, bias_c=bias_c, fire_now=fire_now,
                loss_slope_c_per_s=loss_slope_c_per_s,
                cap_gain_c_per_open_min=cap_gain_c_per_open_min,
                cap_inflight_c=cap_inflight_c,
                cap_loss_slope_c_per_s=cap_loss_slope_c_per_s,
                unbooked_until_ts=unbooked_until_ts,
            )
        if dose_step is not None:
            mode, full_push = dose_step.mode, False
        else:
            mode, full_push = self._next_mode(
                MODE_COAST if prev_mode == MODE_DOSE else prev_mode,
                error, room_slope_c_per_s, self.cfg,
                push_c=raw_command - tado_internal_c, full_push=state.full_push,
            )
        lookahead = 0.0
        if mode in (MODE_HEAT, MODE_DOSE):
            command = self.cfg.max_target_c
        elif mode in (MODE_COAST, MODE_STOP):
            command = self.cfg.min_target_c
        else:
            # The valve's look-ahead on its falling reading, cancelled
            # (one-sided, capped; see the module docstring).
            lookahead = lookahead_correction_c(
                valve_lookahead_s, trv_slope_c_per_s,
                self.cfg.lookahead_deadband_c_per_h, self.cfg.lookahead_cap_c,
            )
            command = raw_command - lookahead

        # 5. Clamp to safe actuator range.  Saturated: the clamp changed the
        # command (the look-ahead's lowering is not a saturation).
        final_command = max(
            self.cfg.min_target_c,
            min(self.cfg.max_target_c, command),
        )
        is_saturated = abs(final_command - (raw_command - lookahead)) > _SATURATION_TOLERANCE_C

        # 6. Integral (see the module docstring)
        new_integral = state.integral_c
        if time_delta_s > 0:
            commanded = raw_command - lookahead
            saturated_high = commanded > self.cfg.max_target_c
            saturated_low = commanded < self.cfg.min_target_c

            may_integrate = mode == MODE_HOLD
            if saturated_high and error > 0:
                may_integrate = False
            if saturated_low and error < 0:
                may_integrate = False

            near_target = abs(error) < self.cfg.integral_deadband_c

            if may_integrate and near_target:
                new_integral += error * self.cfg.tuning.ki * time_delta_s
                new_integral = max(
                    self.cfg.integral_min_c,
                    min(self.cfg.integral_max_c, new_integral),
                )
            elif not near_target:
                new_integral *= self.cfg.integral_decay

        if dose_step is not None:
            settled = dose_step.settled
            pulse_start = dose_step.pulse_start_ts
            last_pulse = dose_step.last_pulse_start_ts
            last_end = dose_step.last_pulse_end_ts
            running = pulse_start is not None
            pulse_sp = dose_step.pulse_setpoint_c if running else None
            pulse_cap = dose_step.pulse_limit_s if running else None
            last_sp = dose_step.last_pulse_setpoint_c
        else:
            # Off: not in the dosing phase; a pulse that was running still
            # counts for the refractory time once dosing resumes.
            settled, pulse_start, pulse_sp, pulse_cap = False, None, None, None
            last_pulse = (
                state.pulse_start_ts if state.pulse_start_ts is not None
                else state.last_pulse_start_ts
            )
            last_end = state.last_pulse_end_ts
            last_sp = state.last_pulse_setpoint_c
            if state.pulse_start_ts is not None and _finite(now_ts):
                last_end = float(now_ts)
                if _finite(state.pulse_setpoint_c):
                    last_sp = state.pulse_setpoint_c
        new_state = RegulationState(
            integral_c=new_integral, mode=mode, last_room_c=room_temp_c,
            full_push=full_push, settled=settled,
            pulse_start_ts=pulse_start, last_pulse_start_ts=last_pulse,
            pulse_setpoint_c=pulse_sp, pulse_cap_s=pulse_cap, last_pulse_end_ts=last_end,
            last_pulse_setpoint_c=last_sp,
        )

        dose_fields: dict = {}
        if dose_step is not None:
            dose_fields = {
                "virtual_error_c": round(dose_step.virtual_error_c, 3),
                "need_c": round(dose_step.need_c, 3),
                "pulse_s": round(dose_step.pulse_s, 1),
                "ready_in_s": (
                    round(dose_step.ready_in_s, 1) if dose_step.ready_in_s is not None else None
                ),
                "settled": dose_step.settled,
                "fired_by": dose_step.fired_by,
                "dose_end": dose_step.dose_end,
                "pulse_cap_s": (
                    round(dose_step.pulse_cap_s, 1) if dose_step.pulse_cap_s is not None else None
                ),
            }

        return RegulationResult(
            target_for_tado_c=round(final_command, 1),
            feedforward_offset_c=round(feedforward_offset, 2),
            p_correction_c=round(p_correction, 2),
            i_correction_c=round(new_integral, 2),
            error_c=round(error, 2),
            is_saturated=is_saturated,
            new_state=new_state,
            mode=mode,
            room_held=room_held,
            full_push=full_push,
            lookahead_c=round(lookahead, 2),
            **dose_fields,
        )
