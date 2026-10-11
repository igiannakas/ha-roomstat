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
* ``coast``: after a heat-up, the minimum (valve shut) while the room is
  below the target and still rising, so the leftover heat lands it.
* ``hold``: near the target, the feedforward + PI command above.  A Tado
  answers small pushes late and partly, so kp is high (4 by default).
* ``stop``: the minimum, ahead of an overshoot: once the room is predicted
  to be ``stop_over_c`` over the target within the braking time Td (at
  least ``stop_lead_s``), the radiator's coast being the same however the
  heat got there.  Released
  (back to hold) once it is predicted to fall back within ``release_c`` of
  the target in ``release_lead_s``, so the radiator is warm again by the
  time the room needs it.

The heat-up's end and the stop use the room's rise rate only while it
rises (one-sided); only the release looks at a fall, and it only hands back
to the hold.  In two rooms fitted to real data this holds within
0.04-0.05 °C RMS on average (only full/shut commands: 0.06-0.12;
parameters.py).

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

from .parameters import RegulationConfig

_LOGGER = logging.getLogger(__name__)

# Output is considered "not saturated" when clamping error is below this threshold (°C).
# 0.01 °C is 1/10 of the Tado quantisation step (0.1 °C) – effectively zero.
_SATURATION_TOLERANCE_C = 0.01

# Modes (see the module docstring).
MODE_HOLD = "hold"
MODE_HEAT = "heat"
MODE_COAST = "coast"
MODE_STOP = "stop"
MODES = (MODE_HOLD, MODE_HEAT, MODE_COAST, MODE_STOP)


# ---------------------------------------------------------------------------
# State & result data classes
# ---------------------------------------------------------------------------

@dataclass
class RegulationState:
    """Mutable state carried between regulation cycles."""

    integral_c: float = 0.0
    mode: str = MODE_HOLD


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


def resume_mode(trv_setpoint_c: float | None, config: RegulationConfig) -> str:
    """The mode to start in, from the setpoint the TRV holds.

    A reload (e.g. a preset temperature change) or a restart in the middle of
    a heat-up carries it on (and it ends as usual), and one while the valve
    is shut lets the coast finish.  Anything else starts in hold.
    """
    if trv_setpoint_c is None or not math.isfinite(trv_setpoint_c):
        return MODE_HOLD
    if trv_setpoint_c >= config.max_target_c - 0.05:
        return MODE_HEAT
    if trv_setpoint_c <= config.min_target_c + 0.05:
        return MODE_COAST
    return MODE_HOLD


# ---------------------------------------------------------------------------
# Regulator
# ---------------------------------------------------------------------------

class FeedforwardPiRegulator:
    """Roomstat's regulator (see the module docstring)."""

    def __init__(self, config: RegulationConfig) -> None:
        self.cfg = config

    @staticmethod
    def _next_mode(
        mode: str, error_c: float, room_slope_c_per_s: float | None,
        config: RegulationConfig,
    ) -> str:
        """The mode for this cycle (see the module docstring)."""
        rising = falling = 0.0
        if room_slope_c_per_s is not None and math.isfinite(room_slope_c_per_s):
            deadband = config.slope_deadband_c_per_h / 3600.0
            rising = max(0.0, room_slope_c_per_s - deadband)
            falling = max(0.0, -room_slope_c_per_s - deadband)
        td_s = config.tuning.td_s
        lead_s = td_s if math.isfinite(td_s) and td_s > 0 else 0.0
        # Where the room ends up once the heat still in the radiator is out.
        landing = error_c - lead_s * rising

        if mode == MODE_HEAT:
            return MODE_COAST if landing <= config.heatup_exit_c else MODE_HEAT
        if error_c >= config.heatup_enter_c and landing > config.heatup_exit_c:
            return MODE_HEAT
        if mode == MODE_COAST and error_c > 0 and rising > 0:
            return MODE_COAST
        # Near the target: fine adjustments, with a firm stop ahead of an
        # overshoot that lasts until the room is heading back to the target.
        # It looks at least as far ahead as a heat-up: the radiator's coast
        # (Td) is the same however the heat got there.
        stop_lead_s = max(config.stop_lead_s, lead_s)
        if error_c - stop_lead_s * rising <= -config.stop_over_c:
            return MODE_STOP
        if (
            mode == MODE_STOP
            and error_c + config.release_lead_s * falling < -config.release_c
        ):
            return MODE_STOP
        return MODE_HOLD

    def compute(
        self,
        setpoint_c: float,
        room_temp_c: float,
        tado_internal_c: float,
        time_delta_s: float,
        state: RegulationState,
        room_slope_c_per_s: float | None = None,
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

        # 1. Feedforward – compensate for sensor-placement offset
        feedforward_offset = tado_internal_c - room_temp_c

        # 2. Room error
        error = setpoint_c - room_temp_c

        # 3. The hold command: feedforward + P + I
        p_correction = self.cfg.tuning.kp * error
        i_correction = state.integral_c
        raw_command = setpoint_c + feedforward_offset + p_correction + i_correction

        # 4. Mode: heat (maximum), coast or stop (minimum), or hold
        prev_mode = state.mode if state.mode in MODES else MODE_HOLD
        mode = self._next_mode(prev_mode, error, room_slope_c_per_s, self.cfg)
        if mode == MODE_HEAT:
            command = self.cfg.max_target_c
        elif mode in (MODE_COAST, MODE_STOP):
            command = self.cfg.min_target_c
        else:
            command = raw_command

        # 5. Clamp to safe actuator range
        final_command = max(
            self.cfg.min_target_c,
            min(self.cfg.max_target_c, command),
        )
        is_saturated = abs(final_command - raw_command) > _SATURATION_TOLERANCE_C

        # 6. Integral (see the module docstring)
        new_integral = state.integral_c
        if time_delta_s > 0:
            saturated_high = raw_command > self.cfg.max_target_c
            saturated_low = raw_command < self.cfg.min_target_c

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

        new_state = RegulationState(integral_c=new_integral, mode=mode)

        return RegulationResult(
            target_for_tado_c=round(final_command, 1),
            feedforward_offset_c=round(feedforward_offset, 2),
            p_correction_c=round(p_correction, 2),
            i_correction_c=round(new_integral, 2),
            error_c=round(error, 2),
            is_saturated=is_saturated,
            new_state=new_state,
            mode=mode,
        )
