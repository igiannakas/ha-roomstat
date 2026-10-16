"""Closed-loop room + Tado X simulator for regulation and auto-tune tests.

Not a test module (no ``test_`` prefix).  Pure Python, no Home Assistant.

What is simulated
-----------------
* **Room**: an integrating-with-losses thermal model.  Full radiator output
  heats the room at ``heat_rate_c_per_h``; losses pull it towards the outdoor
  temperature with time constant ``tau_room_h`` (tens of hours, as measured).
* **Radiator**: first-order heat lag behind the valve, after a water
  transport delay.  This stored heat is what keeps warming the room after
  the valve starts to close.
* **Room sensor**: air-mixing lag, noise, 0.01 °C rounding, a 30 s report
  period and optional glitches (single wild readings).
* **Tado TRV**: a sensor sitting next to the radiator (reads room + bias +
  a large share of the radiator heat) and its *own PI controller* that moves
  the valve every couple of minutes.  The integrator is what makes the real
  TRVs keep heating for a long time after the proxy asks them to stop.
  With ``tado_td_min`` the controller also anticipates on the rate of its
  own reading: it throttles while the reading climbs fast and re-opens when
  it falls.  That reproduces the Tado V3+ behaviour in the October 2026
  heating-% data: fully open only at a push (setpoint - reading) of about
  +5 °C or more, partly open and pulsing at +1..+3 °C, and reliably shut
  only at about -2 °C or below (see ``TADO_V3``).
* **Proxy**: the real ``FeedforwardPiRegulator`` plus the send logic of
  ``climate_regulation.py`` (rate limit, change threshold, urgent decrease,
  0.1 °C command rounding).  Optionally the real ``Autotuner``.
* **Timer overlays** (fail cold, ``ProxySettings.heat_timer_s`` > 0): a
  maximum command is a Tado timer overlay that falls back to the zone's
  Tado app schedule (``PlantParams.tado_schedule_c``: 5 °C, or None = Off)
  when it runs out; the proxy renews it once half of it has passed while
  the command is still the maximum.  ``RoomSim.kill()`` stops the proxy
  (Home Assistant, the network or the cloud gone) while the room, the
  radiator and the valve carry on.

Calibrated against Home Assistant recorder data from four UK rooms
(October 2026): room dead time 13–21 min, heat-up 1.3–3.2 °C/h, passive
cooling 0.3–0.5 °C/h, and 0.4–1.2 °C heat-up overshoot with Kp 0.6–0.8 /
Ki 0.002–0.003, caused by the TRV continuing to heat after demand < 0.
"""
from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlantParams:
    """Physical and device parameters of one simulated room."""

    t_out_c: float = 10.0
    tau_room_h: float = 25.0          # passive time constant (h)
    heat_rate_c_per_h: float = 3.0    # room rise rate at full radiator output
    tau_rad_min: float = 12.0         # radiator heat lag behind the valve
    water_delay_min: float = 4.0      # valve -> radiator transport delay
    mix_lag_min: float = 4.0          # room air mixing / sensor lag
    trv_beta_c: float = 10.0          # TRV reading rise at full radiator output
    trv_bias_c: float = 0.5           # TRV reading bias at idle (vs room)
    trv_lag_min: float = 3.0
    trv_offset_c: float = 0.0         # user offset configured in Tado
    tado_kp: float = 0.35             # valve fraction per °C of TRV error
    tado_ti_min: float = 30.0         # TRV integral time
    tado_period_s: float = 120.0      # how often the TRV moves the valve
    tado_td_min: float = 0.0          # anticipation on its own reading's rate
    valve_leak: float = 0.0           # valve never fully closes (fraction)
    valve_gamma: float = 1.0          # flow = opening ** gamma (< 1: a radiator
                                      # valve gives most of its flow early)
    sensor_conv_c: float = 0.0        # room sensor reads this much more at full
                                      # radiator output (warm air reaching it)
    sensor_noise_c: float = 0.01
    sensor_period_s: float = 30.0
    sensor_resolution_c: float = 0.01  # reported step (many Zigbee sensors: 0.1)
    glitch_prob: float = 0.0          # chance per report of a wild reading
    glitch_size_c: float = 1.5
    extra_gain: Callable[[float], float] | None = None  # °C/h vs time (sun…)
    # The zone's schedule in the Tado app, where a timer overlay falls back
    # to when it runs out: a setpoint (5 °C), or None for Off (power off,
    # valve shut).
    tado_schedule_c: float | None = 5.0


# Tado V3+ controller as measured (October 2026 heating-% data, see the
# module docstring): combine with a room, e.g. PlantParams(**room, **TADO_V3).
TADO_V3 = dict(tado_kp=0.35, tado_td_min=12.0)

# Two real rooms, fitted to the morning of 11 Oct 2026 (overnight cooling,
# a 06:00 heat-up and the hold after it): the real valve setpoints were
# replayed through this model and room, radiator, TRV and Tado-controller
# parameters fitted to the room sensor, the TRV's reading and the cloud's
# heating %.  RMS error: room 0.14 / 0.08 °C, TRV reading 0.19 / 0.22 °C.
# Note the strongly non-linear valve (gamma) and the warm radiator air that
# reaches the room sensor (sensor_conv_c).
LIVING_ROOM_FIT = dict(
    trv_offset_c=-9.9, t_out_c=11.0, tau_room_h=39.6, heat_rate_c_per_h=2.46,
    mix_lag_min=0.9, water_delay_min=1.3, tau_rad_min=35.3, trv_bias_c=-0.66,
    trv_beta_c=19.3, trv_lag_min=17.3, tado_kp=0.60, tado_ti_min=43.2,
    tado_td_min=13.0, valve_gamma=0.34, sensor_conv_c=1.33,
)
SECOND_BEDROOM_FIT = dict(
    trv_offset_c=-5.0, t_out_c=11.0, tau_room_h=37.0, heat_rate_c_per_h=1.13,
    mix_lag_min=1.4, water_delay_min=0.7, tau_rad_min=30.7, trv_bias_c=0.13,
    trv_beta_c=5.96, trv_lag_min=5.6, tado_kp=0.53, tado_ti_min=73.1,
    tado_td_min=3.6, valve_gamma=0.63, sensor_conv_c=0.53,
)


@dataclass
class ProxySettings:
    """Send-logic settings mirrored from climate_regulation.py."""

    control_interval_s: float = 60.0
    min_command_interval_s: float = 60.0
    min_change_threshold_c: float = 0.1
    urgent_decrease_threshold_c: float = 1.0
    command_step_c: float = 0.1
    # Fail cold: maximum commands as timer overlays of this length (0 = off,
    # Manual overlays that last until the next command).
    heat_timer_s: float = 0.0


@dataclass
class SimTrace:
    """Per-control-cycle trace (lists are parallel)."""

    t: list[float] = field(default_factory=list)
    sp: list[float] = field(default_factory=list)
    y: list[float] = field(default_factory=list)          # sensor reading
    y_true: list[float] = field(default_factory=list)
    tado: list[float] = field(default_factory=list)
    cmd: list[float] = field(default_factory=list)        # setpoint held by TRV
    valve: list[float] = field(default_factory=list)
    kp: list[float] = field(default_factory=list)
    ki: list[float] = field(default_factory=list)
    td: list[float] = field(default_factory=list)
    mode: list[str] = field(default_factory=list)
    power: list[bool] = field(default_factory=list)       # TRV on (not Off)
    sends: int = 0
    timer_renewals: int = 0                               # included in sends
    timer_expiries: list[float] = field(default_factory=list)

    def window(self, t0: float, t1: float) -> list[int]:
        return [i for i, t in enumerate(self.t) if t0 <= t < t1]


def schedule_from(spec: list[tuple[float, float]]) -> Callable[[float], float]:
    """Return sp(t) for a daily schedule [(hour, setpoint), ...]."""
    spec = sorted(spec)

    def sp(t: float) -> float:
        h = (t / 3600.0) % 24.0
        cur = spec[-1][1]
        for hour, val in spec:
            if h >= hour:
                cur = val
        return cur

    return sp


class RoomSim:
    """Discrete-time room + TRV + proxy simulation (10 s physics step)."""

    DT = 10.0

    def __init__(
        self,
        plant: PlantParams,
        reg_module: Any,
        reg_config: Any,
        proxy: ProxySettings | None = None,
        *,
        seed: int = 1,
        y0: float = 18.0,
        tuner: Any = None,
        autotune_module: Any = None,
    ) -> None:
        self.p = plant
        self.proxy = proxy or ProxySettings()
        self.rng = random.Random(seed)
        self.reg_mod = reg_module
        self.cfg = reg_config
        self.regulator = reg_module.FeedforwardPiRegulator(reg_config)
        self.state = reg_module.RegulationState()
        self.tuner = tuner
        self.at = autotune_module
        self.slope = autotune_module.SlopeEstimator() if autotune_module else None

        # physical state
        self.t = 0.0
        self.y_true = y0
        self.y_mix = y0
        self.q = 0.0                      # radiator output fraction
        delay_steps = max(1, int(plant.water_delay_min * 60 / self.DT))
        self.flow_line: deque[float] = deque([0.0] * delay_steps, maxlen=delay_steps)
        self.trv_raw = y0 + plant.trv_bias_c
        self.tado_prev = round(self.trv_raw + plant.trv_offset_c, 1)
        self.valve = 0.0
        self.tado_i = 0.0
        self.trv_sp = 5.0
        self.next_tado = 0.0
        self.sensor_value = round(y0, 2)
        # Home Assistant's last_updated: changes only when the value changes.
        self.sensor_ts = 0.0
        self.next_sensor = 0.0
        self.next_control = 0.0
        self.last_send = -1e9
        self.last_sent: float | None = None
        self.last_reg = 0.0
        # Fail cold: the TRV's power (False: an Off schedule block), when its
        # timer overlay runs out, and when the proxy last sent one.
        self.trv_power = True
        self.timer_end: float | None = None
        self.timer_sent: float | None = None
        self.alive = True
        self.trace = SimTrace()

    # -- device models ---------------------------------------------------
    def _tado_reported(self) -> float:
        return round(self.trv_raw + self.p.trv_offset_c, 1)

    def _tado_step(self) -> None:
        p = self.p
        if not self.trv_power:
            # Off: the valve stays shut (frost protection only acts near
            # freezing, far below anything simulated here).
            self.tado_prev = self._tado_reported()
            self.tado_i = 0.0
            self.valve = 0.0
            return
        reading = self._tado_reported()
        dt_min = p.tado_period_s / 60.0
        rate = (reading - self.tado_prev) / dt_min          # °C/min
        self.tado_prev = reading
        err = self.trv_sp - reading
        self.tado_i += p.tado_kp / p.tado_ti_min * err * dt_min
        self.tado_i = min(1.0, max(0.0, self.tado_i))
        lead = err - p.tado_td_min * rate
        self.valve = min(1.0, max(0.0, p.tado_kp * lead + self.tado_i))

    def _physics_step(self) -> None:
        p, dt_h, dt_min = self.p, self.DT / 3600.0, self.DT / 60.0
        flow = max(self.valve, p.valve_leak)
        if p.valve_gamma != 1.0 and flow > 0:
            flow = flow ** p.valve_gamma
        self.flow_line.append(flow)
        delayed = self.flow_line[0]
        self.q += (delayed - self.q) * min(1.0, dt_min / p.tau_rad_min)
        gain = p.extra_gain(self.t) if p.extra_gain else 0.0
        dy = (p.heat_rate_c_per_h * self.q + gain
              - (self.y_true - p.t_out_c) / p.tau_room_h) * dt_h
        self.y_true += dy
        self.y_mix += (self.y_true - self.y_mix) * min(1.0, dt_min / p.mix_lag_min)
        target = self.y_true + p.trv_bias_c + p.trv_beta_c * self.q
        self.trv_raw += (target - self.trv_raw) * min(1.0, dt_min / p.trv_lag_min)

    def _sensor_step(self) -> None:
        p = self.p
        v = self.y_mix + p.sensor_conv_c * self.q + self.rng.gauss(0.0, p.sensor_noise_c)
        if p.glitch_prob and self.rng.random() < p.glitch_prob:
            v += self.rng.choice((-1.0, 1.0)) * p.glitch_size_c
        res = p.sensor_resolution_c
        new = round(round(v / res) * res, 2)
        if new != self.sensor_value:
            self.sensor_ts = self.t
        self.sensor_value = new

    # -- Tado overlays ------------------------------------------------------
    def _deliver(self, target: float) -> None:
        """The TRV takes a command: a timer overlay for the maximum when
        ``heat_timer_s`` > 0, else a Manual one."""
        self.trv_power = True
        self.trv_sp = min(self.cfg.max_target_c, max(self.cfg.min_target_c, target))
        if self.proxy.heat_timer_s > 0 and self.trv_sp >= self.cfg.max_target_c - 0.05:
            self.timer_end = self.t + self.proxy.heat_timer_s
            self.timer_sent = self.t
        else:
            self.timer_end = self.timer_sent = None
        self.last_sent = self.trv_sp
        self.last_send = self.t
        self.trace.sends += 1

    def _timer_step(self) -> None:
        """A timer overlay that runs out hands the zone back to its schedule."""
        if self.timer_end is None or self.t < self.timer_end:
            return
        self.trace.timer_expiries.append(self.t)
        self.timer_end = self.timer_sent = None
        if self.p.tado_schedule_c is None:
            self.trv_power = False
        else:
            self.trv_sp = self.p.tado_schedule_c

    def kill(self) -> None:
        """Home Assistant, the network or the cloud are gone: no more commands
        (the room, radiator and valve carry on)."""
        self.alive = False

    # -- proxy -------------------------------------------------------------
    def _control_step(self, sp: float, sample_extra: dict | None) -> None:
        room = self.sensor_value
        tado = self._tado_reported()
        dt = (self.t - self.last_reg) if self.last_reg > 0 else 0.0
        self.last_reg = self.t

        slope = None
        if self.slope is not None:
            self.slope.add(self.t, room, self.sensor_ts)
            slope = self.slope.slope_c_per_s()

        if self.tuner is not None:
            tun = self.tuner.active_tuning()
            self.cfg.tuning.kp = tun.kp
            self.cfg.tuning.ki = tun.ki
            self.cfg.tuning.td_s = tun.td_s

        res = self.regulator.compute(
            setpoint_c=sp, room_temp_c=room, tado_internal_c=tado,
            time_delta_s=dt, state=self.state, room_slope_c_per_s=slope,
        )
        self.state = res.new_state

        target = round(res.target_for_tado_c / self.proxy.command_step_c) * self.proxy.command_step_c
        base = self.last_sent
        sent = False
        if base is None:
            sent = True
        else:
            diff = abs(target - base)
            limited = (self.t - self.last_send) < self.proxy.min_command_interval_s
            if diff < self.proxy.min_change_threshold_c:
                pass
            elif limited:
                if target < base - self.proxy.urgent_decrease_threshold_c:
                    sent = True
            else:
                sent = True
        if sent:
            self._deliver(target)
        elif (
            self.timer_sent is not None
            and target >= self.cfg.max_target_c - 0.05
            and self.t - self.timer_sent >= self.proxy.heat_timer_s / 2.0
        ):
            # Still the maximum and half the timer gone: renew it.
            self._deliver(target)
            self.trace.timer_renewals += 1

        if self.tuner is not None:
            extra = dict(sample_extra or {})
            self.tuner.observe(self.at.AutotuneSample(
                ts=self.t, setpoint_c=sp, room_temp_c=room,
                tado_internal_c=tado,
                tado_setpoint_c=self.last_sent,
                eligible=extra.get("eligible", True),
                command_saturated=res.is_saturated,
                room_temp_ts=self.sensor_ts,
            ))

        tr = self.trace
        tr.t.append(self.t)
        tr.sp.append(sp)
        tr.y.append(room)
        tr.y_true.append(self.y_true)
        tr.tado.append(tado)
        tr.cmd.append(self.trv_sp)
        tr.valve.append(self.valve)
        tr.kp.append(self.cfg.tuning.kp)
        tr.ki.append(self.cfg.tuning.ki)
        tr.td.append(getattr(self.cfg.tuning, "td_s", 0.0))
        tr.mode.append(getattr(res, "mode", ""))
        tr.power.append(self.trv_power)

    def _record_dead(self, sp: float) -> None:
        """Trace one control period while the proxy is gone."""
        tr = self.trace
        tr.t.append(self.t)
        tr.sp.append(sp)
        tr.y.append(self.sensor_value)
        tr.y_true.append(self.y_true)
        tr.tado.append(self._tado_reported())
        tr.cmd.append(self.trv_sp)
        tr.valve.append(self.valve)
        tr.kp.append(self.cfg.tuning.kp)
        tr.ki.append(self.cfg.tuning.ki)
        tr.td.append(getattr(self.cfg.tuning, "td_s", 0.0))
        tr.mode.append("dead")
        tr.power.append(self.trv_power)

    def run(
        self,
        hours: float,
        setpoint: Callable[[float], float],
        *,
        eligible: Callable[[float], bool] | None = None,
    ) -> SimTrace:
        end = self.t + hours * 3600.0
        while self.t < end:
            if self.t >= self.next_sensor:
                self._sensor_step()
                self.next_sensor += self.p.sensor_period_s
            self._timer_step()
            if self.t >= self.next_control:
                if self.alive:
                    extra = {"eligible": eligible(self.t)} if eligible else None
                    self._control_step(setpoint(self.t), extra)
                else:
                    self._record_dead(setpoint(self.t))
                self.next_control += self.proxy.control_interval_s
            if self.t >= self.next_tado:
                self._tado_step()
                self.next_tado += self.p.tado_period_s
            self._physics_step()
            self.t += self.DT
        return self.trace


# ---------------------------------------------------------------------------
# Response metrics
# ---------------------------------------------------------------------------

def step_metrics(tr: SimTrace, t_step: float, hours: float = 3.0) -> dict[str, float]:
    """Overshoot / rise metrics for the setpoint step at ``t_step``."""
    idx = tr.window(t_step, t_step + hours * 3600)
    sp = tr.sp[idx[0]]
    ys = [tr.y_true[i] for i in idx]
    reach = next((k for k, v in enumerate(ys) if v >= sp - 0.1), None)
    peak = max(ys)
    return {
        "sp": sp,
        "overshoot": peak - sp,
        "reach_min": (tr.t[idx[reach]] - t_step) / 60 if reach is not None else math.inf,
        "final_err": sp - ys[-1],
    }


def hold_stats(tr: SimTrace, t0: float, t1: float) -> dict[str, float]:
    idx = tr.window(t0, t1)
    errs = [tr.sp[i] - tr.y_true[i] for i in idx]
    n = len(errs)
    mean = sum(errs) / n
    sd = math.sqrt(sum((e - mean) ** 2 for e in errs) / n)
    return {"mean": mean, "sd": sd, "max_abs": max(abs(e) for e in errs)}
