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

# A TRV whose setpoint still differs from the last command this long after it
# was sent did not take it (lost on the way, or a Tado schedule block ended
# the override): the command is sent again.  This replaces blind periodic
# re-sends.  Longer for the fallback entity and for any cloud entity
# (const.CLOUD_TRV_PLATFORMS), whose readings come from cloud polling (every
# 5-30 min; a command's own refresh shows at once).
TRV_DRIFT_GRACE_S: float = 120.0
FALLBACK_DRIFT_GRACE_S: float = 360.0

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
    ki: integral gain near the target, for a lasting offset.
    td_s: the braking time: how far ahead a heat-up predicts where the heat
        still in the radiator will carry the room (lead x rise rate), and so
        how early it stops.  0 = no anticipation.  Auto-tune learns it from
        the coast after each heat-up.
    """

    kp: float = 4.0
    ki: float = 0.003
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
    td_step_frac: float = 0.5          # Td moves at most 50 % (or 5 min) per update
    td_step_min_s: float = 300.0
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
    heatup_enter_c: float = 1.0
    heatup_exit_c: float = 0.1
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
