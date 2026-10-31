"""Central parameter defaults for roomstat."""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Behavioural thresholds (climate-entity logic, independent of the PI engine)
# ---------------------------------------------------------------------------

@dataclass
class BehaviourConfig:
    """Thresholds for the send-decision logic.

    These values can be overridden via config-entry options so operators can
    tune the integration's responsiveness without touching source code.
    """

    # Rate-limit bypass: immediately send if the new target is this many °C
    # below the current Tado setpoint (urgent cool-down).
    urgent_decrease_threshold_c: float = 1.0


# ---------------------------------------------------------------------------
# Integration behaviour defaults
# ---------------------------------------------------------------------------

DEFAULT_CONTROL_INTERVAL_S: int = 60   # seconds between regulation cycles
# The watchdog (RegulationConfig.cycle_watchdog_s) checks on its own timer,
# never from the regulation cycle it watches.
WATCHDOG_CHECK_INTERVAL_S: int = 60
# The shortest timer overlay for a maximum command (RegulationConfig.
# heat_timer_s).  It is renewed every half, so each halving of the timer
# doubles the renewals (API calls, valve wake-ups) for a bound the watchdog
# already gives at 10 min; below 15 min that is no longer worth it.
HEAT_TIMER_MIN_S: float = 900.0
FROST_PROTECT_C: float = 5.0           # target temperature when HVAC is OFF
DEFAULT_SENSOR_GRACE_S: int = 300      # seconds to use last-valid room_temp when sensor is unavailable

# Minimum time between any two TRV commands, across all rooms.  Every room
# runs its cycle on the same minute after a restart, and all Tado V3+ valves
# share one bridge; spacing the commands means it never gets a burst.
TRV_COMMAND_SPACING_S: float = 5.0

# More heat asked for by a change by hand (a preset, a temperature, Heat,
# resume schedule) waits this long after the last such change, so a second
# press replaces it instead of following it.  Less heat and off go at once.
# A Tado valve that gets an off seconds after a heat command can take the
# heat and not the off: 25 Oct 2026, 25 °C followed by off 5 s (master
# bedroom) and 15 s later (second bedroom), the zones showed off at once and
# the valves heated at 100 % for 15 and 13 min; an off half an hour after
# the heat is taken within 5 min.  The presses came 9 s apart (Comfort, then
# Frost protection), 19 Oct 10 s (20 → 22 → 20).
MANUAL_SETTLE_S: float = 15.0

# An off sent less than this after a heat command to the same valve is sent
# once more this long after it (climate_controllers.off_confirm_after).  The
# settle above only joins presses less than MANUAL_SETTLE_S apart: Comfort,
# then Frost protection 20 s later, sends the heat at 15 s and the off 5 s
# after it, the pattern of 25 Oct (2.10.4 review).  One extra command, only
# in that case; the owner's choice (26 Oct 2026).  Whether a second off
# reaches a valve that kept the heat over the first is not proven yet: watch
# the zones' heating % after one.
OFF_CONFIRM_S: float = 60.0

# A valve that still reports heating (the zone's heating %, at least
# LOST_OFF_MIN_PCT) in a report made LOST_OFF_AFTER_S or more after an off it
# took, with no heat command of ours since, kept the heat and dropped the
# off: the off is sent again, at most LOST_OFF_MAX_RESENDS times per heat
# command (2.15.0, climate_controllers.lost_off_due).  25 Oct 2026: two zones
# showed off at once while their valves heated at 100 % for 13-15 min; the
# confirmation off above covers only offs within a minute of a heat command.
# 8 min: the zone's heating % reaches HA up to 4.6 min after the valve
# (first reports 10-276 s after a command, 25-30 Oct 2026), and the valve
# takes 1.5 min to close: a report that late still at 20 % is a valve that
# kept heating, with 2 min to spare.  3 min sent every off twice on a zone
# whose reports came 200 s late (2.15.0 review), 7 min again at 370 s.
# The 25 Oct valves heated 13-15 min.  Needs the zone's heating power
# sensor.
LOST_OFF_AFTER_S: float = 480.0
LOST_OFF_MIN_PCT: float = 20.0
LOST_OFF_MAX_RESENDS: int = 2

# A TRV whose setpoint still differs from the last command this long after it
# was sent did not take it (lost on the way, or a Tado schedule block ended
# the override): the command is sent again.  This replaces blind periodic
# re-sends.  Longer for the fallback entity and for any cloud entity
# (const.CLOUD_TRV_PLATFORMS), whose readings come from cloud polling (every
# 5-30 min; a command's own refresh shows at once).
TRV_DRIFT_GRACE_S: float = 120.0
FALLBACK_DRIFT_GRACE_S: float = 360.0

# A TRV that keeps not showing a command it took (a state that no longer
# updates, a zone that overrides it) is asked again after the grace above,
# then after twice that, four times that, … but never less often than this.
# Each command to a cloud zone costs API calls, and each to a HomeKit valve
# battery: without a limit such a valve got one every 6 min for as long as
# it lasted (2.8.5 review: 60 in 6 h).
RESEND_BACKOFF_MAX_S: float = 1800.0

# A TRV command that takes longer than this has failed (it may still arrive;
# the drift check above then sees it).
TRV_COMMAND_TIMEOUT_S: float = 10.0

# The integral never covers more than this per cycle, so a pause (a sensor
# gap, HA busy or restarting) cannot add a large step in one go.
MAX_INTEGRATION_STEP_S: float = 2 * DEFAULT_CONTROL_INTERVAL_S

# A configured entity (TRV, room sensor, helpers) that was deleted, renamed or
# disabled is reported as a Home Assistant repair after this long, counted
# only once HA has started (integrations load in any order).
ENTITY_MISSING_REPORT_S: float = 300.0

# A room sensor without a usable reading for this long is reported as a
# repair too.  Nothing else happens meanwhile: the radiator keeps the last
# setpoint it was sent (the room sensor is expected to be reliable).
ROOM_SENSOR_UNAVAILABLE_REPORT_S: float = 900.0

# After a window closes the room re-warms on its own: the walls, furniture
# and radiator are still warm, only the air was changed (18 Oct 2026: the
# study +1.9 °C in 9 min after a 3.5 °C dip, the living room +2.8 °C in
# 8 min, all four rooms decelerating over 15-40 min).  Heating that joins in
# overshoots (a 3-min full-power burst on a room rising 2.5 °C/h), and for
# 15 min the slope estimate still carries the dip.  So once the configured
# close delay has run, heating waits until the room's own rise (measured
# from fresh readings since the close) has slowed below this rate - faster
# than any radiator here heats, 1.4-2.2 °C/h at full power - polling every
# WINDOW_RECOVERY_POLL_S, for at most WINDOW_RECOVERY_MAX_S after the close.
# A close delay of 0 skips the wait.
WINDOW_RECOVERY_RATE_C_PER_H: float = 1.0
WINDOW_RECOVERY_MAX_S: float = 1800.0
WINDOW_RECOVERY_POLL_S: float = 60.0


# ---------------------------------------------------------------------------
# Tuning (the three values in the options flow, and what auto-tune learns)
# ---------------------------------------------------------------------------

@dataclass
class CorrectionTuning:
    """Gains of the controller (regulation.py).

    kp: the gain near the target (hold).  A Tado answers small pushes late
        and partly, so it is high: the valve is pushed by (1 + kp) x error.
        In the two rooms fitted to real data (tests/plant_sim.py) 3-6 all
        hold within about 0.03 °C; above 4 commands rise fast for little
        gain.
    ki: integral gain near the target, for a lasting offset.  0.001 since
        2.8.0 (0.003 before): with the stop released only once the room is
        0.1 °C under the target, the integral winds up while the room waits
        there and the hold then pushes above it (17 Oct 2026: +0.2 in 9 min
        at 0.003); 0.001 keeps a slow correction for a lasting offset and
        leaves the auto-tuner's SIMC Ki (learned from the dead time) to
        raise it where a room needs more.
    td_s: the braking time: how far ahead a heat-up predicts where the heat
        still in the radiator will carry the room (lead x rise rate), and so
        how early it stops.  0 = no anticipation.  Auto-tune learns it from
        the coast after each heat-up.
    """

    kp: float = 4.0
    ki: float = 0.001
    td_s: float = 600.0


# ---------------------------------------------------------------------------
# Auto-tune (see autotune.py and CLAUDE.md)
# ---------------------------------------------------------------------------

@dataclass
class AutotuneConfig:
    """Bounds, thresholds and step limits for the background auto-tuner."""

    # Absolute hard bounds on the tuned values.  Nothing the tuner does can
    # leave these, whatever the measurements say.
    kp_min: float = 0.5
    kp_max: float = 8.0
    ki_min: float = 0.00005
    ki_max: float = 0.005
    td_max_s: float = 2700.0           # 45 min

    # Bounds relative to the user's configured values (the baseline).
    kp_rel_min: float = 0.25
    kp_rel_max: float = 2.0
    ki_rel_min: float = 0.05
    ki_rel_max: float = 2.0

    # Largest change per update (multiplicative).
    kp_step_down: float = 0.85
    kp_step_up: float = 1.10
    ki_step_down: float = 0.7
    ki_step_up: float = 1.2
    # More braking (an earlier stop, a cooler landing: the safe direction)
    # moves at most 50 % (or 5 min) per update.
    td_step_frac: float = 0.5
    td_step_min_s: float = 300.0
    # Less braking (a later stop, a warmer landing) is a moving average: each
    # update moves Td this share of the way to its target, at most
    # td_less_step_frac of its value (or td_less_step_min_s), and only once
    # the target is td_deadband_s or more below it (2.13.0).  The owner (28
    # Oct 2026): a room that lands short and takes another go is better than
    # one that lands hot and needs a window opened.  Evidence, living room
    # 21-28 Oct: heat-up results scattered +-0.33 C around zero, set by people
    # and the weather (the coast: 4-12 min empty, 15-33 min with people), while
    # 5 min of braking moves a landing by only 0.1-0.25 C; full steps chased
    # that noise (11.0 -> 7.9 -> 10.3 -> 13.1 -> 8.4 -> 13.1 rollback -> 8.0).
    # Those 12 heat-ups replayed: 9.0-13.2 min, down at most 1.6 min a time
    # (up as before, after the guests' +0.48 and the people's +0.45).
    td_less_smoothing: float = 0.3
    td_less_step_frac: float = 0.15
    td_less_step_min_s: float = 60.0
    # Changes smaller than these are ignored (noise, not evidence).
    kp_deadband_rel: float = 0.05
    ki_deadband_rel: float = 0.15
    td_deadband_s: float = 120.0

    # Minimum time between parameter changes, so each change can be judged
    # before the next one.  Detuning (making things gentler) waits less.
    min_interval_up_s: float = 12 * 3600.0
    min_interval_down_s: float = 3 * 3600.0

    # SIMC: tau_c = lambda * theta.  1.5 rather than SIMC's default 1.0
    # because overshoot costs hours here (passive cooling is slow).
    simc_lambda: float = 1.5
    # Ti may not drop below this multiple of (tau_c + theta).  SIMC uses 4.
    simc_ti_factor_min: float = 2.0
    # Heat-ups with a dead time needed before the SIMC Ki is used.
    simc_min_episodes: int = 2

    # Heat-up episode detection.
    heatup_min_step_c: float = 0.5     # setpoint step that starts an episode
    heatup_min_error_c: float = 0.4    # room must be at least this far below
    heatup_max_s: float = 5 * 3600.0   # give up if the target is not reached
    heatup_settle_s: float = 45 * 60.0 # flat/falling this long after the peak
    # A TRV demand this high after the demand went <= 0 means heating
    # restarted (a stepped top-up): the episode ends there.
    heatup_reheat_demand_c: float = 1.0
    # A heat-up is measured up to its own first peak: once the TRV was told
    # to stop and the room, having risen, rises no faster than this over 10
    # min, its coast has stalled, and a rise after that (rate_min_c_per_h or
    # faster) is people, sun or a small hold push, not this heat-up's
    # radiator (24 Oct 2026, living room: the room stalled at 19.97, then
    # lunch guests took it to 20.49 with the valve shut; booked as a 33-min
    # coast, +0.48 °C and a 46-min dead time, it raised the braking time
    # 10.3 -> 13.1 min and the next morning stopped 0.35 °C short).  The
    # regulator's slope deadband.
    coast_stall_c_per_h: float = 0.1
    # Smaller steps still show overshoot and coast, but the dead time and
    # heating rate need a bigger rise to stand out from sensor noise.  0.6
    # for both: with a 0.01 °C sensor a 0.6 °C rise is 60 steps, and in mild
    # weather a night setback can leave less than 0.8 °C to climb (living
    # room, 15 Oct 2026: 0.75 °C), so with 0.8 no dead time, and so no SIMC
    # Ki, was ever learned there.
    theta_min_step_c: float = 0.6
    rate_min_step_c: float = 0.6
    # An episode cut short by the schedule is kept if the TRV demand had
    # already gone <= 0 at least this long before (coast as a lower bound),
    # or if it ran at least this long (dead time and heating rate only).
    truncated_min_after_cross_s: float = 15 * 60.0
    truncated_min_duration_s: float = 45 * 60.0
    # A longer gap between two regulation cycles ends the episode in
    # progress.  This also decides whether a heat-up survives a restart.
    max_gap_s: float = 600.0
    # No heat-up episode starts within this long after a window was open:
    # the room's rise is its own recovery, not the radiator's (18 Oct 2026,
    # living room: a heating rate of 3.7 °C/h, a "coast" of 5 min and an
    # overshoot of -1.16 °C learned from the 10 min after a window closed;
    # the study booked its whole 45-min recovery as coast).
    window_settle_s: float = 1800.0

    # Hold windows (steady setpoint, room near target, heating active).
    hold_window_s: float = 3 * 3600.0  # evaluated every window
    hold_buffer_s: float = 24 * 3600.0 # oscillation judged over up to this much
    hold_settle_s: float = 3600.0      # setpoint unchanged this long first
    hold_after_heatup_s: float = 1800.0  # and this long after a heat-up ended
    hold_start_max_error_c: float = 0.5
    hold_min_heating_fraction: float = 0.2  # passive cool-downs are not holds
    hold_offset_c: float = 0.15        # mean error that counts as an offset
    osc_amplitude_c: float = 0.15      # swing beyond +/- this around the mean
    osc_min_half_cycles: int = 3
    # Oscillation faster than this many dead times is blamed on Kp, slower
    # on Ki (an integrating loop with too much P rings at ~4 theta).
    osc_fast_period_factor: float = 6.0
    # Slower "cycles" are weather or routine (sun, occupancy), not control.
    osc_max_period_s: float = 12 * 3600.0
    # Emergency detune when the swing is this many times osc_amplitude_c.
    emergency_amplitude_factor: float = 2.0

    # Targets.
    overshoot_target_c: float = 0.2    # acceptable heat-up overshoot
    undershoot_sag_c: float = 0.2      # peak this far below target = braked too early

    # Trim factors applied on top of the model-based targets.
    kp_trim_min: float = 0.25          # = kp_rel_min
    kp_trim_max: float = 1.5
    ki_trim_min: float = 0.05          # = ki_rel_min
    ki_trim_max: float = 3.0
    td_trim_min: float = 0.5
    td_trim_max: float = 2.5

    # Evidence: number of episodes kept for medians, and how many must agree
    # before a value moves in the "more heat" direction.
    history_len: int = 5
    confirm_up: int = 2

    # Watchdog: an update is rolled back if the next episodes are worse by
    # this much (overshoot, °C) or a new oscillation appears.
    # After a rollback the tuner carries on straight away (no freeze, nothing
    # blocked); a retry needs fresh evidence and the normal rate limit.
    rollback_margin_c: float = 0.2
    pending_timeout_s: float = 7 * 86400.0

    # Plausibility of identified values; anything outside is discarded.
    theta_min_s: float = 180.0
    theta_max_s: float = 3600.0
    rate_min_c_per_h: float = 0.2
    rate_max_c_per_h: float = 10.0


# ---------------------------------------------------------------------------
# Preset defaults
# ---------------------------------------------------------------------------

@dataclass
class PresetConfig:
    """Temperature settings for each preset mode."""

    comfort_target_c: float = 20.0   # default comfort temperature
    eco_target_c: float = 17.0       # fixed eco temperature (independent of comfort)
    boost_target_c: float = 25.0     # fixed target during boost
    boost_duration_min: int = 30     # auto-revert to comfort after this many minutes
    away_target_c: float = 17.0      # fixed target when away
    frost_protection_target_c: float = 7.0   # frost protection temperature


# ---------------------------------------------------------------------------
# Full regulation config with safety rails
# ---------------------------------------------------------------------------

@dataclass
class RegulationConfig:
    """All regulation parameters and safety limits."""

    tuning: CorrectionTuning = field(default_factory=CorrectionTuning)
    presets: PresetConfig = field(default_factory=PresetConfig)

    # Absolute temperature limits for commands sent to Tado.
    #
    # Tado V3+ radiator thermostats (reached over HomeKit or the cloud
    # integration) accept 5–25 °C only.  Commanding above 25 makes
    # ``climate.set_temperature`` raise ServiceValidationError
    # ("Provided temperature X is not valid. Accepted range is 5 to 25"),
    # so every write is rejected and the room never heats.  Because the
    # regulator clamps to these limits *before* deciding, keeping the
    # ceiling here (rather than clamping at send time) also keeps the
    # anti-windup saturation logic honest.
    min_target_c: float = 5.0
    max_target_c: float = 25.0

    # Anti-windup limits for the integral correction term
    integral_min_c: float = -2.0
    integral_max_c: float = 2.0

    # Integral deadband: only accumulate integral when |error| < this value.
    # Outside this zone the integral decays, preventing buildup during gross
    # heating/cooling transients that would cause overshoot.
    integral_deadband_c: float = 0.3

    # Decay factor applied to the integral each cycle when |error| >= deadband.
    # 0.95 means ~5% reduction per 60s cycle → drains in ~15 min.
    integral_decay: float = 0.95

    # Rate limiting: minimum seconds between commands to Tado (battery saving)
    min_command_interval_s: float = 180.0

    # Minimum difference to current Tado setpoint before sending a new command
    min_change_threshold_c: float = 0.3

    # Room slopes below this (°C/h) are treated as flat, so sensor jitter
    # does not count as rising or falling.
    slope_deadband_c_per_h: float = 0.1

    # A room reading that fell this much since the previous cycle is a
    # draught or a glitch, never a heating need: that cycle keeps the
    # previous reading, so neither a heat-up nor a hold push starts on it.
    # A fall that goes on is accepted one cycle later (the window action,
    # when there is a sensor, has usually taken over by then).  19 Oct 2026,
    # living room: the sensor fell 19.94 -> 18.92 in one minute as a window
    # opened and 25 °C went out 16 s before the window action's 5 °C.
    fast_fall_c: float = 0.5

    # The controller (regulation.py).  The Tado's own controller opens the
    # valve fully only when its setpoint is well above its own reading
    # (about +5 °C) and stays shut only well below it (about -2 °C); in
    # between it throttles, pulses and re-opens by itself as its reading
    # falls.  So heat-ups run at full power and end with a firm stop, and
    # near the target fine adjustments are cut by a firm stop ahead of an
    # overshoot.
    #
    # Stock values from two real rooms (11 Oct 2026, Tado V3+ over HomeKit;
    # tests/plant_sim.py LIVING_ROOM_FIT / SECOND_BEDROOM_FIT) and two
    # generic ones, at 5 and 11 °C outdoors, with a valve that modulates and
    # one that pulses, auto-tune on: room RMS error 0.04-0.08 °C on average
    # while holding (only 25/5: 0.06-0.12; the old PI: 0.19-0.27), heat-up
    # overshoot <= ~0.2 °C, 45-100 commands a day (25/5: 20-60; old PI:
    # 70-110).
    #
    # Heat-ups: a deficit of heatup_enter_c or more heats at full power (the
    # maximum) until the heat still in the radiator is predicted to carry
    # the room to within heatup_exit_c of the target: error - Td x rise
    # rate.  Then the minimum while the room still rises (coast).  0.1 stops
    # a little short: the slope estimate lags while the room is still
    # accelerating, and the hold picks up any shortfall.
    # 0.8 since 2.8.2 (1.0 before; owner's choice, on trial): a morning
    # deficit of 0.8-0.9 °C ran as a hold whose command sat +4 above the
    # valve's reading, which is full power anyway, but (a) it ended on the
    # stop rule, not the heat-up's Td exit and coast, and (b) a mid-range
    # command is not the strong signal a Tado answers at once: the second
    # bedroom's valve stayed idle for 21 min on a +4.3 hold (19 Oct 2026)
    # and 9 min the day before, while every 25 °C command was answered in
    # 1-3 min.  Simulated (four rooms, deficits 0.6-0.9, 8 and 12 °C
    # outside, 0.7 vs 1.0): morning peak +0.24 -> +0.14 in the generic
    # living room, +0.08 -> +0.06 fitted second bedroom, commands -5..-12 %,
    # hold RMS unchanged, 14-26 min slower to the target in the two slow
    # rooms (Td exit, then coast).
    heatup_enter_c: float = 0.8
    heatup_exit_c: float = 0.1
    # A hold that pushes hard ends like a heat-up (2.10.4).  Once the hold's
    # push (command - the valve's reading = (1 + Kp) x error + I) reaches
    # hold_push_c, the valve is at or near full output and the radiator
    # fills with heat as in a heat-up: the push then ends on the heat-up's
    # exit (error - Td x rise <= heatup_exit_c) and coasts, until the hold
    # is left (coast, stop or heat-up).  Without it such a push ended on the
    # stop rule, 0.2 °C of landing later.  26 Oct 2026, second bedroom: a
    # 0.76 deficit at 06:00 (just under heatup_enter_c) ran as a hold at
    # +3.1 over the valve's reading, which the valve answered at 100 %; the
    # stop rule shut it at 06:31, the heat-up exit would have at 06:28 (Td
    # 25 min), and the room went on rising for 24 min on the radiator's
    # heat.  2.5 (owner's choice, 26 Oct 2026; 3.0 was proposed): the Tado
    # answers +1..+3 by throttling and pulsing (30-40 % on average) once its
    # radiator is warm, +3 and more from a cold radiator at full power.
    # Simulated (four rooms, 5-12 °C outside, morning deficits 0.4-1.2, two
    # seeds, auto-tune on; tests/plant_sim.py), 2.5 against today: morning
    # peak +0.19..+0.22 -> +0.10..+0.14 in the generic living room at 0.6-0.7
    # deficits (Td 15-18 min learned), +0.24 -> +0.18 in the generic slow
    # room at a 0.4 deficit and +0.16 -> +0.13 at 5 °C; the fitted living
    # room within +-0.01; the fitted second bedroom +0.01..+0.03 (it learns a
    # Td of about 0: its coast stalls just short and the hold tops up); hold
    # RMS within +-0.01 except +0.02 in the generic living room at a 0.4
    # deficit; commands -9..+13 %; heat-ups (deficit 1.2) unchanged.  3.0
    # left the generic living room at +0.15 at 12 °C / 0.6 (2.5: +0.10) and
    # the 0.4 deficits as they are, with the fitted rooms the same within
    # 0.02.  2.0 did no better.  Any rising hold (no threshold) halved the
    # generic rooms' hold RMS but more than doubled the fitted second
    # bedroom's commands (stopping at the target, again).
    hold_push_c: float = 2.5
    # Near the target, a firm stop (the minimum) once the room is predicted
    # to be stop_over_c over the target within the braking time Td, but at
    # least stop_lead_s, so the radiator's leftover heat lands at most
    # there (the coast is the same however the heat got there).  Released
    # only once the room is predicted to *need* heat: release_below_c
    # below the target within release_lead_s (a falling room is released
    # that far ahead, so the radiator is warm again in time; a flat one
    # waits until it is there).  Until 2.7 the release came while the room
    # was still 0.02 above the target: the hold then commanded 0.2-0.5
    # below the valve's reading, where a Tado trickles on its own (17 Oct
    # 2026, three rooms: 16 % for 10 min, +0.2-0.4 °C for the next 1-2 h).
    # 0.1 below (owner's choice, 2.7.1): the room spends part of its time a
    # little under the target instead of always a little over.  Simulated
    # (fitted rooms, auto-tune on): hold RMS +0.01, overshoot and commands
    # unchanged, time more than 0.05 under 4 -> 8 %; 0.15 doubles the RMS
    # in the generic rooms.  Without the firm stop the valve keeps heating
    # just below its reading (0.05-0.08 °C RMS, +0.3 °C peaks).
    stop_over_c: float = 0.1
    stop_lead_s: float = 600.0
    release_below_c: float = 0.10
    release_lead_s: float = 1200.0

    # Valve look-ahead (lookahead.py, an option per room, 2.14.0): while the
    # valve's own reading falls faster than this, a hold command is lowered
    # by the learned look-ahead times the fall beyond it, at most by
    # lookahead_cap_c.  The deadband keeps the reading's 0.1 °C steps (a
    # cloud zone's reading is polled every ~3 min) from moving the command.
    lookahead_deadband_c_per_h: float = 0.3
    lookahead_cap_c: float = 1.5

    # Dosed top-ups (dosing.py, an option per room, 2.15.0): in the settled
    # state the hold is replaced by full-power pulses sized by a Smith
    # predictor.  See DoseConfig.
    dose: DoseConfig = field(default_factory=lambda: DoseConfig())

    # Fail cold (see CLAUDE.md "Failure handling").  Every command is an
    # overlay that lasts until the next one, so a maximum command left in
    # place by a dead Home Assistant, network or cloud keeps the valve open
    # (with the negative valve offsets, 25 °C is a real 30-35 °C).  With
    # heat_timer_s > 0 a maximum command to a cloud TRV
    # (const.CLOUD_TRV_PLATFORMS) is a Tado timer overlay of this length
    # instead: if nobody renews it, the zone falls back to its Tado app
    # schedule, which must then be 5 °C or Off.  It is renewed once half of
    # it has passed while the command is still the maximum (one extra
    # command per heat_timer_s / 2 while heating).  0 = off: the
    # integration cannot check the Tado schedule, so the owner enables it
    # per room once the schedules are at the floor.  At least
    # HEAT_TIMER_MIN_S when on.
    heat_timer_s: float = 0.0
    # No regulation cycle completed for this long (cycles stopped, or
    # returning early every time): the room is reported as stalled, and a
    # maximum command still in place is replaced by the minimum.
    cycle_watchdog_s: float = 600.0


# ---------------------------------------------------------------------------
# Valve look-ahead (lookahead.py, climate_lookahead.py)
# ---------------------------------------------------------------------------

@dataclass
class LookaheadConfig:
    """How the valve's look-ahead is learned (lookahead.LookaheadLearner).

    A release is a hold command to a shut valve.  Its sample is the valve's
    first heating power report above 0 within ``observe_s`` of the command,
    against the push the valve held (its setpoint minus its reading) and how
    fast its reading fell.  Living room, 23-30 Oct 2026 (36 releases): the
    first response follows push + 13 min x fall (r = 0.85; first % = 16 +
    38 x (push + 15 x fall per min), R^2 0.73), and comes 1-6 min after the
    command, so 8 min is enough for the cloud's 3-minute polls.
    """

    # Releases kept for the fit, and how many before it is trusted.
    window: int = 24
    min_samples: int = 8
    observe_s: float = 480.0
    # The look-ahead in use starts at 0 and moves towards the fit at most
    # step_s per release; a gap under deadband_s is left alone.
    step_s: float = 120.0
    deadband_s: float = 60.0
    max_lookahead_s: float = 1200.0
    # The fit is trusted only when the releases spread enough in fall rate
    # (sd, °C/h) for the look-ahead to show, the valve clearly answers the
    # push (%/°C), and push and fall are not too entangled (|r|): with the
    # look-ahead in use the push is lowered on a falling reading, so the
    # two become correlated.
    min_fall_spread_c_per_h: float = 0.5
    min_push_gain_pct_per_c: float = 10.0
    max_push_fall_correlation: float = 0.9
    # A report at or above this is the valve fully open: the push no
    # longer shows in it, so it says nothing about the look-ahead.
    saturated_pct: float = 98.0
    # The valve's reading slope (Theil-Sen, autotune.SlopeEstimator): a
    # 12-min window, known once it has 4 samples.
    slope_window_s: float = 720.0
    slope_min_samples: int = 4
    slope_max_gap_s: float = 400.0


# ---------------------------------------------------------------------------
# Dosed top-ups (dosing.py, climate_dosing.py)
# ---------------------------------------------------------------------------

@dataclass
class DoseConfig:
    """Dosed top-ups: a Smith predictor sizes full-power pulses in the
    dosing phase, every shortfall under ``heatup_enter_c`` (2.15.0, two
    phases since 2.16.0; design: project doc
    claude/roomstat-smith-predictor-dosed-topups-2026-10-30.md).

    The model is first order plus dead time on *open-minutes* (minutes the
    valve is commanded open, booked from Roomstat's own commands): a delay
    ``theta``, then a first-order spread ``tau``, times a gain in °C of room
    rise per open-minute.  Fitted on the living room's 19 settled top-ups of
    25-30 Oct 2026 (tests/fixtures/living_room_topups_2026_10.json): gain
    median 0.041 (quartiles 0.021 / 0.062), theta 4 min (2 / 9), tau 6 min
    (4 / 12), every episode within 0.044 °C RMS.  The heat shows after
    about theta + tau / 2 (the release lead) and has landed after about
    theta + 2 tau (the loss horizon and the refractory time).
    """

    # The model.  The auto-tuner's heating rate / 60 replaces the default
    # gain when known (a heat-up's rise at full power, the same unit).  The
    # delay and spread start at the top-ups' own values: the tuner's dead
    # time and coast come from heat-ups on a cold radiator (living room and
    # study 15+ min against a top-up's 4).  The learner moves them from
    # there.
    default_gain_c_per_open_min: float = 0.045
    default_theta_s: float = 300.0
    default_tau_s: float = 480.0
    # Cloud latency (command -> valve moving) and the valve's tail after an
    # off (off latency + valve travel): the open interval booked for a
    # command sent at t0 and an off sent at t1 is [t0 + latency, t1 + close].
    latency_s: float = 60.0
    close_delay_s: float = 90.0
    # A pulse whose zone reports no heating % above 0 within this of the
    # send did nothing: its episode is dropped (not learned from).  Real
    # first reports came 10-276 s after the command (25-30 Oct 2026; the
    # cloud polls every ~3 min), so 6 min leaves a slow poll room.
    opened_check_s: float = 360.0

    # The controller (regulation.py).  The band is centred on the target
    # (owner, 30 Oct 2026): the dose aims at the target itself; "land short
    # rather than hot" lives in the learner's asymmetry, not in the aim.
    aim_under_c: float = 0.0
    # The minimum pulse is the owner's decision (30 Oct 2026).  Each pulse
    # is capped by the room's own expected behaviour (owner, 31 Oct 2026: a
    # safety net, so a wrong model cannot heat the room far over): when it
    # starts, the dose is worked out again on the room's rise per open-minute
    # at full power (the auto-tuner's heating rate / 60, measured on the
    # room's own heat-ups) with no aim bias and its own cooling observer, and
    # the cap is the open time that dose plus cap_margin_c takes
    # (regulation.dose_cap_s), within pulse_min_s and pulse_ceiling_s; a
    # target raised meanwhile widens it.  While the tuner has no rate the
    # fixed pulse_max_s (the learned gain is the model the cap guards).  It
    # must not scale with that model: in the house simulation (31 Oct 2026,
    # learned gains 2-3x high, the aim bias wound up to +0.25) a cap of 1.5 x
    # the model's own need let 17-24-min pulses through near the target
    # (peaks +0.22..+0.47).  0.2: the tuner's rate is a heat-up's steepest
    # rise, 5-25 % above a top-up's rise per open-minute (living room 0.0437
    # against 0.041, master 0.029 against 0.023), and a pulse that runs to
    # its cap lands at most about 0.2 over if the room's heating rate is
    # right.
    pulse_min_s: float = 180.0
    pulse_max_s: float = 720.0
    cap_margin_c: float = 0.2
    pulse_ceiling_s: float = 3600.0
    # "Nothing left in flight": the landing phrase only (readiness is the refractory).
    inflight_min_c: float = 0.02
    # No new pulse until theta + 2 tau after the last one started (at most
    # refractory_max_s) and theta + tau / 2 after the last one ended: the
    # last pulse's heat has started to show, also after a long one.
    refractory_max_s: float = 1800.0
    # A pulse fired early (riding another zone's burn, or with another
    # room) only when a minimum pulse would land at most this far above
    # the target.  A pulse released by the room itself: at most the stop
    # rule's stop_over_c (RegulationConfig) above.
    fire_max_overshoot_c: float = 0.05

    # The pulse coordinator (dosing.PulseCoordinator; the boiler is an
    # on/off type, so radiators should share its burns).  Ride another
    # zone's burn once at or below the target; fire with another room's
    # pulse when due within this fraction of the visible dead time; a pulse
    # counts as just started for together_window_s; a zone's heating report
    # counts while it is at most zone_report_max_age_s old.  5 min: the
    # cloud polls about every 3 min, so at 3 min another zone's burn often
    # read as stale; simulated (four rooms, real - 4 °C, two seeds) 300 s
    # was equal or better on every count: boiler calls 3.3 -> 3.0 and
    # 4.3 -> 3.5 a day, single-radiator calls 0.50 -> 0.44 and 0.62 -> 0.48,
    # peaks no higher.
    ride_burn_min_error_c: float = 0.0
    fire_together_lead_frac: float = 1.0
    together_window_s: float = 120.0
    zone_report_max_age_s: float = 300.0

    # The learner (dosing.DoseLearner): each episode (a pulse to the room's
    # first peak) is fitted by a grid over theta and tau with the
    # closed-form gain and the room's loss fitted with it.  An episode with
    # no landing is judged at most episode_max_s after its pulse ended (from
    # the start, a 32-min pulse was judged at -0.21 while it peaked at -0.01:
    # review 3, 31 Oct 2026).
    episode_max_s: float = 3600.0
    episodes_kept: int = 6
    min_open_min: float = 2.0
    fit_theta_s: tuple[float, float, float] = (120.0, 1200.0, 60.0)
    fit_tau_s: tuple[float, ...] = (
        240.0, 360.0, 480.0, 600.0, 720.0, 900.0, 1080.0, 1320.0, 1560.0, 1800.0,
    )
    fit_max_rms_c: float = 0.06
    # The learner keeps this much of the room's readings and the valve's
    # open intervals, so each episode's fit can include the heat of earlier
    # pulses still arriving and estimate the room's loss with it: the 15 min
    # before the longest pulse (pulse_ceiling_s), the pulse, and up to
    # episode_max_s after it, with 5 min to spare.
    fit_history_s: float = 8400.0
    # Bounds: the gain within these multiples of the default (seeded) gain;
    # an episode's gain is accepted within these multiples of the gain in
    # use.
    gain_bounds: tuple[float, float] = (0.3, 3.0)
    gain_accept: tuple[float, float] = (0.2, 4.0)
    theta_bounds_s: tuple[float, float] = (120.0, 1200.0)
    tau_bounds_s: tuple[float, float] = (180.0, 1800.0)
    # Steps towards the median of the last episodes.  The gain moves
    # asymmetrically: up (smaller doses) half the way at once, down
    # (bigger doses) 15 % of the way, at most every 12 h.  The delay and the
    # spread move half the way either way: neither direction is the safe
    # one (longer: an earlier, bigger pulse; shorter: the next pulse before
    # this one's heat has landed), so they follow the room (2.15.0 review:
    # a 15-min delay seeded from the heat-ups landed +0.15 and took days to
    # walk back at the slow rate).
    step_safe_frac: float = 0.5
    step_more_frac: float = 0.15
    step_more_interval_s: float = 43200.0
    # The aim bias (°C added to the need) moves this fraction of each
    # landing's miss, within +-bias_clamp_c.  Towards bigger doses only on a
    # pulse that ended by its own dose (not its cap or the stop rule) from
    # a shortfall of at most bias_max_shortfall_c: a big dose landing short
    # is a gain error, the gain's to correct (slowly, the owner's choice),
    # and an additive bias learned from it overdoses every small top-up
    # (house simulation, 31 Oct 2026: the bias at +0.10..+0.25 once the
    # dosing phase took 0.3-0.8 shortfalls, and peaks near the target up).
    bias_gain: float = 0.3
    bias_target_c: float = 0.0
    bias_clamp_c: float = 0.25
    bias_max_shortfall_c: float = 0.3
    # Two landings in a row above this: the gain x 1.5 and the bias -0.1
    # at once.
    watchdog_peak_c: float = 0.3
    # Stage 2 (logged only for now): the gain against the radiator's warmth.
    radiator_term_min_episodes: int = 12


# ---------------------------------------------------------------------------
# Preheat on the way home (preheat.py, climate_preheat.py)
# ---------------------------------------------------------------------------

@dataclass
class PreheatConfig:
    """The numbers of the preheat rule (preheat.PreheatController).

    The owner's choices (25 Oct 2026); each is an option under Configure →
    Preheat timing, explained there.

    lead_s: added to the heat-up time, so a room is at its temperature a
        little before the expected arrival, not on the dot (an arrival that
        comes a few minutes early is still warm).
    rate_factor: the heat-up time uses this share of the room's learned
        heating rate.  The auto-tuner's rate is the steepest rise of a
        heat-up; the start (valve opening, radiator warming) and the end are
        slower.  On 37 recorded heat-ups (18-25 Oct 2026) 0.8 started 34 early
        enough (typically 24 min early); 1.0 would have started rooms 4-8 min
        later and made three more late.  Lower starts earlier.
    slip_s: a heading-home preheat stops when the expected arrival moves more
        than this much later than when it started and the room is no longer
        due (the owner: no wasted heat); it starts again when the new arrival
        is due.  Home ETA's estimate wobbles by a few minutes with traffic.
    cap_s: a heading-home preheat stops this long after it first started
        for a trip home, and starts no more on that trip (a backstop: a trip
        home that never ends).  Slips, restarts and breaks (a window, a
        change by hand) don't re-arm it; the trip ends when its expected
        arrival is gone for ``arrival_grace_s`` or someone is home.
    fall_delay_s: a drop to a cooler level (close → nearby → away) waits
        this long; a rise applies at once.  A phone hovering at 800 m or at
        the near-home radius can't make the target flap.
    arrival_grace_s: an expected arrival that goes unknown (the trip home
        ended, or Home ETA reloading) ends a heading-home preheat only after
        this long; nothing new starts meanwhile.
    recovery_default_c: the 30-minute recovery until the room has learned
        its own (preheat.RecoveryLearner).
    fallback_rate_c_per_h / fallback_dead_time_s: the heat-up time's
        heating rate and dead time until the auto-tuner has learned them
        (deliberately slow: a room that starts early wastes a little, one
        that starts late arrives cold).
    max_arrival_ahead_s: an expected arrival further ahead than this is a
        fault, not a trip home, and is ignored.
    """

    lead_s: float = 300.0
    rate_factor: float = 0.8
    slip_s: float = 1200.0
    cap_s: float = 4 * 3600.0
    fall_delay_s: float = 600.0
    arrival_grace_s: float = 600.0
    recovery_default_c: float = 0.5
    fallback_rate_c_per_h: float = 1.0
    fallback_dead_time_s: float = 1200.0
    max_arrival_ahead_s: float = 12 * 3600.0


@dataclass
class RecoveryConfig:
    """How the 30-minute recovery is learned (preheat.RecoveryLearner).

    An episode is a target rise of ``min_step_c`` or more (the auto-tuner's
    heat-up step) that leaves the room ``min_deficit_c`` or more below it.
    One in which the room comes within ``reach_c`` of the target by its 30
    minutes is dropped: its rise is capped by the deficit, so it would
    understate the room (a 0.6 °C step reached in 25 min says only "0.6 or
    more").  ``min_episodes``: as for the auto-tuner's dead time, one
    heat-up is not enough.  No episode starts within
    ``window_settle_s`` of a window (the auto-tuner's rule: the room then
    re-warms on its own), and a gap of more than ``max_gap_s`` between
    cycles ends one (the auto-tuner's ``max_gap_s``).
    """

    window_s: float = 1800.0
    min_step_c: float = 0.5
    min_deficit_c: float = 0.5
    reach_c: float = 0.1
    history_len: int = 10
    min_episodes: int = 2
    window_settle_s: float = 1800.0
    max_gap_s: float = 600.0


# ---------------------------------------------------------------------------
# Out-of-band alarm (binary_sensor.<room>_out_of_band)
# ---------------------------------------------------------------------------

@dataclass
class OutOfBandConfig:
    """When a room counts as stuck outside its target band.

    Hot: the room is ``band_c`` or more above ``max(setpoint, the lowest
    reading since the setpoint was last lowered)`` for ``band_min``.  The
    second term keeps a room that is still cooling after a setback from
    counting: on 11 Oct 2026 the living room went 20 -> 18 °C at 21:30 and
    was still at 18.66 °C at 06:00, so "0.5 above the setpoint" alone would
    have been on all night.
    Cold: the room is ``band_c`` or more below the setpoint for
    ``cold_band_min``, counted again from every setpoint rise, so a normal
    heat-up never counts.
    """

    band_c: float = 0.5
    band_min: float = 30.0
    cold_band_min: float = 120.0
