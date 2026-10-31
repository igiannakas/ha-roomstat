"""
Dosed top-ups: full-power pulses sized by a Smith predictor (an option per
room, 2.15.0; climate_dosing.py on the entity, regulation.py's ``dose``
mode).  Design and evidence: project doc
claude/roomstat-smith-predictor-dosed-topups-2026-10-30.md.

The problem
-----------
In the settled state (the room has reached its target, the valve is shut)
the room cools, the hold releases and the valve opens.  The room shows the
heat only 8-12 min later (cloud latency, water, radiator, sensor), so the
valve runs until then: a 0.1 °C shortfall that needs about 2.4 min of open
valve got 10-30 min.  Living room, 25-30 Oct 2026, 19 settled top-ups:
peaks median +0.23, quartiles +0.09 / +0.42, worst +0.94.  Nothing that
acts on the measured room (PI, a derivative, the valve look-ahead) can dose
less than the dead time dictates.

What it does
------------
A Smith predictor runs a model of Roomstat's own heat alongside the room
and the regulator decides on a *virtual* room temperature: the reading plus
the rise the model says is committed but not yet visible
(:class:`TopupPredictor`).  With the option on a room is either in a
heat-up (a shortfall of heatup_enter_c or more: the ordinary heat-up and
its coast) or in the dosing phase (owner, 31 Oct 2026: no hold).  There a
top-up is a pulse at the maximum (one strong command the Tado answers the
same way every time), ended once the heat committed covers the virtual
shortfall plus the room's cooling until the heat has landed
(regulation.dose_need_c), or at its cap: a few times what the room's own
heating rate says the need takes (regulation.dose_cap_s), the safety net
for a wrong model.  The next pulse waits theta + 2 tau from the last one's
start and theta + tau / 2 from its end (the refractory time: the heat
still in flight is already taken off the virtual shortfall).

The unit is the *open-minute*: a minute the valve is commanded open,
booked from Roomstat's own maximums (:class:`RoomDoser`).  The zone's
heating % lags the valve by a minute or more and the valve is strongly
non-linear (26 Oct 02:23: 28 min at a reported 7 % lifted the room
0.3 °C), so weighting by it scattered the gain 10x; open-minutes scatter
it 2x, which is the radiator's own variation.  The heating % only verifies
that a pulse opened the valve, and tells the house that the boiler burns.

The model, fitted
-----------------
First order plus dead time: a delay theta, a first-order spread tau, times
a gain in °C of room rise per open-minute.  Fitted per episode on the 19
settled top-ups of 25-30 Oct 2026 (tests/fixtures/
living_room_topups_2026_10.json), the pre-pulse fall continued as the loss:
gain median 0.041 (quartiles 0.021 / 0.062), theta 4 min (2 / 9), tau
6 min (4 / 12), every episode within 0.044 °C RMS.  The learner
(:class:`DoseLearner`) fits each live episode the same way, the room's loss
with it and every earlier pulse's heat still arriving, and moves the values
in use towards the median of the last six: the gain at once towards smaller
doses (the owner prefers a room landing short to one landing hot), slowly
towards bigger ones; the delay and the spread follow the room either way.

The boiler
----------
The boiler is an on/off type: any single-radiator call already cycles it on
its flow thermostat.  The lever is how many radiators share each burn, so
:class:`PulseCoordinator` lets a dosed room ride another zone's burn and
fire together with another room's pulse when it is due soon anyway; it
only chooses the moment, each room sizes its own pulse.
"""
from __future__ import annotations

import bisect
import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .autotune import SlopeEstimator, theil_sen
from .parameters import AutotuneConfig, DoseConfig
from .regulation import (
    MODE_DOSE,
    dose_fall_c_per_s,
    dose_ready_in_s,
)

_LOGGER = logging.getLogger(__name__)

# The predictor's state is taken back across a reload only when it still
# joins up with now (the SlopeEstimator's own rule, a little looser: a
# reload takes about a minute).
_RESTORE_MAX_AGE_S = 600.0
# An episode's readings may pause this long (a slow coordinator refresh, a
# reload) before the episode is dropped (the auto-tuner's max_gap_s).
_EPISODE_MAX_GAP_S = 600.0
# The first peak is found as the auto-tuner finds a heat-up's (autotune.
# _coast_end): a 10-min Theil-Sen window, judged once 80 % full.
_PEAK_WINDOW_S = 600.0
# A zone report this long before the pulse still counts as the valve open
# (the zone may already have been heating).
_OPENED_EARLY_S = 60.0
# Each episode is fitted from this long before its pulse (the loss shows
# there, and the tail of an earlier pulse still arriving) to this long after
# its landing, the loss estimated with the gain.
_FIT_BEFORE_S = 900.0
_FIT_AFTER_LANDING_S = 300.0
# A fitted loss outside this range (°C/h) is no plain cooling room: a room
# warming by itself, or something no model of one radiator explains.  The
# lower end allows the fit's noise around a room that holds its heat (a
# fitted -0.004 °C/h on a synthetic room losing nothing).
_LOSS_MIN_C_PER_H = -0.05
_LOSS_MAX_C_PER_H = 3.0
# Pulses remembered for the 24-h counts on the sensor.
_COUNT_WINDOW_S = 86400.0
# Episodes judged at once at most: a pulse starts while the last one's heat
# is still being measured whenever the model under-doses (the refractory,
# theta + 2 tau, is shorter than the time to judge, 25-35 min).  More than
# this open means the landings are not being found: the oldest is dropped.
_OPEN_EPISODES_MAX = 3


def _finite(*values: object) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               for v in values)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _median(values: list[float]) -> float | None:
    vals = sorted(v for v in values if _finite(v))
    if not vals:
        return None
    m = len(vals)
    return vals[m // 2] if m % 2 else 0.5 * (vals[m // 2 - 1] + vals[m // 2])


def _signed(c: float) -> str:
    """A temperature relative to the target, signed (0.00 has no sign)."""
    return "0.00" if abs(c) < 0.005 else f"{c:+.2f}"


def _min(s: float) -> str:
    return f"{s / 60.0:.1f} min"


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

# fopdt_response integrates in steps of at most this (s): a 60-s grid taken
# in one step delivers each minute's heat as a lump a step early, which the
# fit then absorbed into the delay and, with the loss fitted, the gain.
_RESPONSE_SUBSTEP_S = 10.0


def fopdt_response(u: list[float], dt_s: float, theta_s: float, tau_s: float) -> list[float]:
    """The model's rise per unit gain at each grid time: the open-minutes
    delivered to the room by ``t_k`` (cumulative; 0 at the first point), for
    an input series ``u`` -- the fraction of [t_k, t_k + dt_s) the valve
    was open.  A delay of ``theta_s``, then a first-order emitter with time
    constant ``tau_s``, integrated in sub-steps of at most
    ``_RESPONSE_SUBSTEP_S`` (the delay to the nearest one)."""
    if not _finite(dt_s, theta_s, tau_s) or dt_s <= 0 or tau_s <= 0:
        return [0.0] * len(u)
    m = max(1, int(math.ceil(dt_s / _RESPONSE_SUBSTEP_S - 1e-9)))
    h = dt_s / m
    d = max(0, int(round(theta_s / h)))
    a = 1.0 - math.exp(-h / tau_s)
    rates = [(q if _finite(q) else 0.0) * h / 60.0 for q in u]
    emitter = 0.0
    y = 0.0
    out = [0.0] * len(u)
    for j in range(len(u) * m - m):
        src = j - d
        if src >= 0:
            emitter += rates[src // m]
        arrived = emitter * a
        emitter -= arrived
        y += arrived
        if (j + 1) % m == 0:
            out[(j + 1) // m] = y
    return out


@dataclass(frozen=True)
class Fit:
    """One episode's fitted model.  ``loss_c_per_s``: the room's cooling
    (positive: falling), fitted with the gain, or the one given."""

    theta_s: float
    tau_s: float
    gain_c_per_open_min: float
    rms_c: float
    loss_c_per_s: float = 0.0


def _resample(ts: list[float], room: list[float], open_fraction: list[float]
              ) -> tuple[float, list[float], list[float]] | None:
    """The series on a fixed grid (the median spacing): the room linearly
    interpolated, the open fraction held per step."""
    n = len(ts)
    if n < 3 or len(room) != n or len(open_fraction) != n:
        return None
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    dt = _median(gaps)
    if dt is None or dt <= 0:
        return None
    dt = max(10.0, dt)
    steps = int((ts[-1] - ts[0]) // dt) + 1
    ys: list[float] = []
    us: list[float] = []
    j = 0
    for k in range(steps):
        t = ts[0] + k * dt
        while j + 1 < n and ts[j + 1] <= t:
            j += 1
        if j + 1 < n and ts[j + 1] > ts[j]:
            w = (t - ts[j]) / (ts[j + 1] - ts[j])
            ys.append(room[j] + w * (room[j + 1] - room[j]))
        else:
            ys.append(room[j])
        us.append(_clamp(open_fraction[j], 0.0, 1.0) if _finite(open_fraction[j]) else 0.0)
    return dt, ys, us


def _theta_grid(cfg: DoseConfig) -> list[float]:
    lo, hi, step = cfg.fit_theta_s
    thetas: list[float] = []
    theta = lo
    while theta <= hi + 1e-9 and step > 0:
        thetas.append(theta)
        theta += step
    return thetas


def fit_episode(
    ts: list[float],
    room: list[float],
    open_fraction: list[float],
    fall0_c_per_s: float,
    cfg: DoseConfig,
    *,
    fit_loss: bool = False,
    window_start_ts: float | None = None,
) -> Fit | None:
    """Fit one episode's response (design §6, §11).

    ``ts``/``room``: the room's readings; ``open_fraction[i]``: the
    fraction of [ts[i], ts[i+1]) the valve was open.  A grid over theta and
    tau; for each pair the model's rise S(t) (:func:`fopdt_response`) and a
    linear least-squares fit; the pair with the smallest RMS wins.

    With ``fit_loss`` False (the offline fit of the 25-30 Oct episodes): the
    readings run from the pulse's start, ``fall0_c_per_s`` (0 or more) is
    continued as the loss, and only the gain is fitted.  On those episodes
    every fit was within 0.044 °C, and the loss is a large part of what a
    pulse delivered (continuing half the fall gave a gain of 0.030, none
    0.021): a fixed loss biases the gain either way.

    With ``fit_loss`` True (the learner): room(t) = c - f (t - t0) + g S(t)
    is fitted for the loss f together with the gain g, over the readings
    from ``window_start_ts`` on, S built from the *whole* open-fraction
    series given -- so the heat of an earlier pulse still arriving in the
    window is in the model, not in the loss.  Taking the loss as the
    room's own slope before the pulse made the learner drift in a closed
    loop: that slope still holds the last landing, so the gain read low;
    taking it from the model's own account of the heat that arrived coupled
    it to the gain in use, and the gain read high and climbed.
    """
    sampled = _resample(ts, room, open_fraction)
    if sampled is None:
        return None
    dt, ys, us = sampled
    if not fit_loss:
        fall = max(0.0, fall0_c_per_s) if _finite(fall0_c_per_s) else 0.0
        y = [ys[k] - ys[0] + fall * k * dt for k in range(len(ys))]
        best: Fit | None = None
        for theta in _theta_grid(cfg):
            for tau in cfg.fit_tau_s:
                s = fopdt_response(us, dt, theta, tau)
                ss = sum(v * v for v in s)
                if ss <= 0:
                    continue
                g = sum(a * b for a, b in zip(s, y)) / ss
                rms = math.sqrt(sum((g * a - b) ** 2 for a, b in zip(s, y)) / len(y))
                if best is None or rms < best.rms_c:
                    best = Fit(theta_s=theta, tau_s=tau, gain_c_per_open_min=g, rms_c=rms,
                               loss_c_per_s=fall)
        return best
    t0 = ts[0]
    k0 = 0
    if window_start_ts is not None and _finite(window_start_ts):
        k0 = max(0, int(math.ceil((window_start_ts - t0) / dt - 1e-9)))
    n = len(ys) - k0
    if n < 5:
        return None
    # Centred regressors: a = -(t - mean t) for the loss, b = S - mean S for
    # the gain; the offset drops out.
    tw = [t0 + (k0 + i) * dt for i in range(n)]
    yw = ys[k0:]
    t_mean = sum(tw) / n
    y_mean = sum(yw) / n
    a = [-(t - t_mean) for t in tw]
    yc = [y - y_mean for y in yw]
    saa = sum(v * v for v in a)
    say = sum(p * q for p, q in zip(a, yc))
    best = None
    for theta in _theta_grid(cfg):
        for tau in cfg.fit_tau_s:
            s = fopdt_response(us, dt, theta, tau)[k0:]
            s_mean = sum(s) / n
            b = [v - s_mean for v in s]
            sbb = sum(v * v for v in b)
            sab = sum(p * q for p, q in zip(a, b))
            det = saa * sbb - sab * sab
            if sbb <= 1e-12 or det <= 1e-12 * saa * sbb:
                continue
            sby = sum(p * q for p, q in zip(b, yc))
            f = (say * sbb - sby * sab) / det
            g = (sby * saa - say * sab) / det
            rms = math.sqrt(sum((f * p + g * q - r) ** 2 for p, q, r in zip(a, b, yc)) / n)
            if best is None or rms < best.rms_c:
                best = Fit(theta_s=theta, tau_s=tau, gain_c_per_open_min=g, rms_c=rms,
                           loss_c_per_s=f)
    return best


class TopupPredictor:
    """Roomstat's own heat on its way to the room: a delay line, then a
    first-order emitter (the Smith predictor's model).  What is still in
    the delay line or the emitter has been sent but not yet seen by the
    room sensor: the *in-flight* heat."""

    def __init__(self, cfg: DoseConfig) -> None:
        self.cfg = cfg
        self._line: deque[tuple[float, float]] = deque()
        self._emitter = 0.0
        self._last_step: float | None = None

    def reset(self) -> None:
        self._line.clear()
        self._emitter = 0.0
        self._last_step = None

    @property
    def last_step(self) -> float | None:
        return self._last_step

    def book(self, ts: float, open_min: float) -> None:
        """Book ``open_min`` open-minutes sent at ``ts`` (kept in time order)."""
        if not _finite(ts, open_min) or open_min <= 0:
            return
        if not self._line or ts >= self._line[-1][0]:
            self._line.append((ts, open_min))
            return
        items = list(self._line)
        bisect.insort(items, (ts, open_min))
        self._line = deque(items)

    def step(self, now: float, theta_s: float, tau_s: float) -> None:
        """Move what has passed the delay into the emitter, then let the
        emitter deliver for the time since the last step."""
        if not _finite(now, theta_s, tau_s) or tau_s <= 0:
            return
        while self._line and self._line[0][0] <= now - max(0.0, theta_s):
            self._emitter += self._line.popleft()[1]
        if self._last_step is not None and now > self._last_step:
            self._emitter *= math.exp(-(now - self._last_step) / tau_s)
        if self._emitter < 1e-6:
            self._emitter = 0.0
        if self._last_step is None or now > self._last_step:
            self._last_step = now

    def committed_open_min(self) -> float:
        return sum(q for _, q in self._line) + self._emitter

    def inflight_c(self, gain: float) -> float:
        """The rise still to come (°C, 0 or more)."""
        if not _finite(gain) or gain <= 0:
            return 0.0
        return gain * self.committed_open_min()

    def to_dict(self) -> dict[str, Any]:
        return {
            "line": [[round(t, 1), round(q, 5)] for t, q in self._line],
            "emitter": round(self._emitter, 5),
            "last_step": self._last_step,
        }

    def restore(self, data: Any, now: float) -> bool:
        """Take a saved state back if it still joins up with now: the last
        step at most 600 s ago and nothing from the future."""
        self.reset()
        if not isinstance(data, dict):
            return False
        last = data.get("last_step")
        emitter = data.get("emitter", 0.0)
        line = data.get("line", [])
        if not _finite(last, emitter, now) or not isinstance(line, list):
            return False
        if last > now + 1.0 or now - last > _RESTORE_MAX_AGE_S or emitter < 0:
            return False
        entries: list[tuple[float, float]] = []
        for item in line:
            if (not isinstance(item, (list, tuple)) or len(item) != 2
                    or not _finite(item[0], item[1]) or item[0] > now + 1.0 or item[1] < 0):
                return False
            entries.append((float(item[0]), float(item[1])))
        entries.sort()
        self._line = deque(entries)
        self._emitter = float(emitter)
        self._last_step = float(last)
        return True


# ---------------------------------------------------------------------------
# The learner
# ---------------------------------------------------------------------------

@dataclass
class Episode:
    """One pulse and the room's response up to its first peak.

    ``cut_ts``: where the next opening's heat (the next pulse, a heat-up's
    maximum) can first show (its opening + the delay in use).  The fit
    models every booked opening, so a later pulse is no contamination of
    the fit; the landing, though, is looked for only until the cut.
    """

    start_ts: float
    setpoint_c: float
    room_start_c: float
    fall0_c_per_s: float          # the room's fall rate before the pulse (0 or more)
    gap_start_c: float | None     # the valve's reading - the room at the start
    need_start_c: float           # what the regulator meant the pulse to cover
    gain_at_start: float          # the gain in use when it started (for "model" in the line)
    rode_burn: bool = False       # fired early by the coordinator
    open_from: float | None = None
    open_until: float | None = None
    valve_opened: bool | None = None   # None: no heating % seen at all (cannot tell)
    heating_seen: bool = False
    samples: list[tuple[float, float]] = field(default_factory=list)
    pulse_end_ts: float | None = None
    landed_ts: float | None = None     # when its heat was seen to have landed
    cut_ts: float | None = None        # the next opening's heat may show from here
    cut_short: bool = False            # landed at the cut, not on a stall or the deadline
    end_reason: str | None = None      # the regulator's dose_end (None: not known)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_ts": self.start_ts, "setpoint_c": self.setpoint_c,
            "room_start_c": self.room_start_c, "fall0_c_per_s": self.fall0_c_per_s,
            "gap_start_c": self.gap_start_c, "need_start_c": self.need_start_c,
            "gain_at_start": self.gain_at_start, "rode_burn": self.rode_burn,
            "open_from": self.open_from, "open_until": self.open_until,
            "valve_opened": self.valve_opened, "heating_seen": self.heating_seen,
            "samples": [[round(t, 1), round(y, 3)] for t, y in self.samples],
            "pulse_end_ts": self.pulse_end_ts, "landed_ts": self.landed_ts,
            "cut_ts": self.cut_ts, "cut_short": self.cut_short,
            "end_reason": self.end_reason,
        }

    @classmethod
    def from_dict(cls, d: Any) -> Episode | None:
        if not isinstance(d, dict):
            return None
        try:
            samples = [(float(t), float(y)) for t, y in d.get("samples", [])]
            ep = cls(
                start_ts=float(d["start_ts"]), setpoint_c=float(d["setpoint_c"]),
                room_start_c=float(d["room_start_c"]), fall0_c_per_s=float(d["fall0_c_per_s"]),
                gap_start_c=(float(d["gap_start_c"]) if d.get("gap_start_c") is not None else None),
                need_start_c=float(d.get("need_start_c", 0.0)),
                gain_at_start=float(d.get("gain_at_start", 0.0)),
                rode_burn=bool(d.get("rode_burn", False)),
                open_from=(float(d["open_from"]) if d.get("open_from") is not None else None),
                open_until=(float(d["open_until"]) if d.get("open_until") is not None else None),
                valve_opened=d.get("valve_opened") if d.get("valve_opened") in (True, False) else None,
                heating_seen=bool(d.get("heating_seen", False)),
                samples=samples,
                pulse_end_ts=(float(d["pulse_end_ts"]) if d.get("pulse_end_ts") is not None else None),
                landed_ts=(float(d["landed_ts"]) if d.get("landed_ts") is not None else None),
                cut_ts=(float(d["cut_ts"]) if d.get("cut_ts") is not None else None),
                cut_short=bool(d.get("cut_short", False)),
                end_reason=(d["end_reason"] if isinstance(d.get("end_reason"), str) else None),
            )
        except (KeyError, TypeError, ValueError):
            return None
        values = [ep.start_ts, ep.setpoint_c, ep.room_start_c, ep.fall0_c_per_s]
        optional = [v for v in (ep.open_from, ep.open_until, ep.pulse_end_ts, ep.landed_ts, ep.cut_ts)
                    if v is not None]
        if (not samples or not _finite(*values) or not _finite(*optional)
                or not all(_finite(t, y) for t, y in samples)):
            return None
        return ep


@dataclass(frozen=True)
class Judged:
    """A judged episode, kept for the median."""

    ts: float
    gain_c_per_open_min: float
    theta_s: float
    tau_s: float
    peak_error_c: float           # room at the peak - target (positive: above)
    open_min: float
    at_floor: bool
    rode_burn: bool
    gap_start_c: float | None
    rms_c: float
    loss_c_per_s: float = 0.0
    shortfall_start_c: float | None = None   # target - room when it fired
    end_reason: str | None = None            # the regulator's dose_end

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts, "gain": round(self.gain_c_per_open_min, 5),
            "loss_c_per_h": round(self.loss_c_per_s * 3600.0, 3),
            "theta_s": self.theta_s, "tau_s": self.tau_s,
            "peak_error_c": round(self.peak_error_c, 3), "open_min": round(self.open_min, 2),
            "at_floor": self.at_floor, "rode_burn": self.rode_burn,
            "gap_start_c": (round(self.gap_start_c, 2) if self.gap_start_c is not None else None),
            "rms_c": round(self.rms_c, 4),
            "shortfall_start_c": (round(self.shortfall_start_c, 3)
                                  if self.shortfall_start_c is not None else None),
            "end_reason": self.end_reason,
        }

    @classmethod
    def from_dict(cls, d: Any) -> Judged | None:
        if not isinstance(d, dict):
            return None
        try:
            j = cls(
                ts=float(d["ts"]), gain_c_per_open_min=float(d["gain"]),
                theta_s=float(d["theta_s"]), tau_s=float(d["tau_s"]),
                peak_error_c=float(d["peak_error_c"]), open_min=float(d["open_min"]),
                at_floor=bool(d.get("at_floor", False)), rode_burn=bool(d.get("rode_burn", False)),
                gap_start_c=(float(d["gap_start_c"]) if d.get("gap_start_c") is not None else None),
                rms_c=float(d.get("rms_c", 0.0)),
                loss_c_per_s=float(d.get("loss_c_per_h", 0.0)) / 3600.0,
                shortfall_start_c=(float(d["shortfall_start_c"])
                                   if d.get("shortfall_start_c") is not None else None),
                end_reason=(d["end_reason"] if isinstance(d.get("end_reason"), str) else None),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not _finite(j.ts, j.gain_c_per_open_min, j.theta_s, j.tau_s, j.peak_error_c, j.open_min,
                       j.loss_c_per_s):
            return None
        return j


# Verdict kinds.
ACCEPTED = "accepted"
DROPPED = "dropped"          # disturbed before it could be judged (window, target, ...)
REJECTED = "rejected"        # judged, but no usable fit
NOT_OPENED = "not_opened"    # the valve never reported heating: the pulse did nothing


@dataclass(frozen=True)
class Verdict:
    kind: str
    line: str


class DoseLearner:
    """Learns the room's gain, delay and spread from its own pulses, and an
    aim bias from their landings (design §6).

    Each episode is fitted (:func:`fit_episode`, the room's loss with it,
    over the history so the heat of earlier and later pulses is modelled)
    and kept; the values in use move towards the median of the last
    ``episodes_kept``.  The gain asymmetrically: towards smaller doses (a
    higher gain) ``step_safe_frac`` of the way at once, towards bigger ones
    ``step_more_frac`` of the way at most every ``step_more_interval_s``
    (the auto-tuner's "more heat" rule).  A gain read too high (people in
    the room during an episode: 0.12 on 26 and 29 Oct against 0.045 empty)
    thus only ever makes the next doses smaller.  The delay and the spread
    ``step_safe_frac`` of the way either way: neither direction is the safe
    one (longer: an earlier and bigger pulse; shorter: the next pulse before
    this one's heat has landed).

    Episodes may overlap: a model that under-doses makes the room ready
    again (theta + 2 tau after the last start, 21 min with the defaults)
    before the last pulse could be judged (25-35 min), and dropping it then
    left such a room unlearned and low for good (2.15.0 review: 0 accepted
    in 24 h, the room 0.08-0.13 under).  Each open episode is judged on its
    own landing, looked for until the next opening's heat can show
    (:attr:`Episode.cut_ts`); the fit models both pulses.
    """

    def __init__(self, cfg: DoseConfig, autotune_cfg: AutotuneConfig | None = None) -> None:
        self.cfg = cfg
        self._at = autotune_cfg or AutotuneConfig()
        self._seed_rate: float | None = None
        self._seed_gain = cfg.default_gain_c_per_open_min
        self.reset()

    # -- values in use ------------------------------------------------------
    def reset(self) -> None:
        """Forget everything learned; back to the seeded gain and the
        default delay and spread."""
        self.gain_c_per_open_min = self._seed_gain
        self.theta_s = self.cfg.default_theta_s
        self.tau_s = self.cfg.default_tau_s
        self.bias_c = 0.0
        self._judged: deque[Judged] = deque(maxlen=max(1, self.cfg.episodes_kept))
        self._accepted_total = 0
        self._last_more: dict[str, float] = {}
        self._episodes: list[Episode] = []
        self._watchdog_fired = False
        self.last_line: str | None = None
        # The room's readings and the valve's open intervals (as booked) for
        # the last fit_history_s, episode or not: the fits' data.
        self._history: deque[tuple[float, float]] = deque()
        self._intervals: list[list[float | None]] = []
        # The holds' heat, not booked (its opening is unknown): an episode
        # whose fit window it reaches is not learned from.
        self._holds: list[list[float | None]] = []

    @property
    def learned(self) -> bool:
        return self._accepted_total > 0

    @property
    def episodes(self) -> tuple[Episode, ...]:
        """The episodes being measured, oldest first."""
        return tuple(self._episodes)

    @property
    def episode(self) -> Episode | None:
        """The newest episode being measured."""
        return self._episodes[-1] if self._episodes else None

    @property
    def visible_dead_time_s(self) -> float:
        """When a pulse's heat starts to show: the release lead."""
        return self.theta_s + self.tau_s / 2.0

    @property
    def landing_horizon_s(self) -> float:
        """When a pulse's heat has landed: the loss horizon and the
        refractory time."""
        return self.theta_s + 2.0 * self.tau_s

    def _gain_bounds(self) -> tuple[float, float]:
        lo, hi = self.cfg.gain_bounds
        return lo * self._seed_gain, hi * self._seed_gain

    def seed(self, rate_c_per_h: float | None) -> None:
        """Seed the gain from the auto-tuner's heating rate (a heat-up's rise
        at full power, °C/h: the same unit per minute).  Only while nothing
        is learned: afterwards the room's own pulses decide.  The delay and
        the spread are not seeded: the tuner's dead time and coast come from
        heat-ups on a cold radiator (15 min and more in the living room and
        the study against a top-up's 4), and a delay seeded from them landed
        +0.15 against +0.02 (2.15.0 review)."""
        self._seed_rate = float(rate_c_per_h) if _finite(rate_c_per_h) and rate_c_per_h > 0 else None
        self._seed_gain = (self._seed_rate / 60.0 if self._seed_rate is not None
                           else self.cfg.default_gain_c_per_open_min)
        if not self.learned:
            self.gain_c_per_open_min = self._seed_gain

    @property
    def rate_gain_c_per_open_min(self) -> float | None:
        """The room's rise per open-minute by the auto-tuner's heating rate
        (a heat-up at full power), None while the tuner has none."""
        return self._seed_rate / 60.0 if self._seed_rate is not None else None

    # -- episodes -------------------------------------------------------------
    def start_episode(
        self, ts: float, setpoint_c: float, room_c: float, fall0_c_per_s: float,
        gap_start_c: float | None, need_start_c: float = 0.0, rode_burn: bool = False,
    ) -> str | None:
        """A pulse started.  An episode still open goes on: its landing is
        looked for until this pulse's heat can show, and the fit models
        both.  Only beyond ``_OPEN_EPISODES_MAX`` open the oldest is dropped.
        Returns that drop's line, if any."""
        if not _finite(ts, setpoint_c, room_c):
            return None
        for ep in self._episodes:
            self._cut(ep, ts + self.cfg.latency_s + self.theta_s)
        line = None
        if len(self._episodes) >= _OPEN_EPISODES_MAX:
            line = self._drop(self._episodes[0], "pulses kept coming before its heat had landed").line
        self._episodes.append(Episode(
            start_ts=ts, setpoint_c=setpoint_c, room_start_c=room_c,
            fall0_c_per_s=max(0.0, fall0_c_per_s) if _finite(fall0_c_per_s) else 0.0,
            gap_start_c=gap_start_c if _finite(gap_start_c) else None,
            need_start_c=need_start_c if _finite(need_start_c) else 0.0,
            gain_at_start=self.gain_c_per_open_min,
            rode_burn=rode_burn,
            samples=[(ts, room_c)],
        ))
        return line

    @staticmethod
    def _cut(ep: Episode, ts: float) -> None:
        if _finite(ts) and (ep.cut_ts is None or ts < ep.cut_ts):
            ep.cut_ts = float(ts)

    def note_cycle(
        self, ts: float, room_c: float | None, *, heating_pct: float | None,
        heating_ts: float | None, open_from: float | None, open_until: float | None,
        hold_from: float | None = None, hold_until: float | None = None,
    ) -> None:
        """Every regulated cycle, episode or not: the reading (None: none
        fresh), the valve's open interval as booked and the hold's unbooked
        heat go to the history; each episode being measured claims its own
        opening, takes the zone's heating report, and the reading."""
        if not _finite(ts):
            return
        self._record(ts, room_c, open_from, open_until)
        self._track(self._holds, ts, hold_from, hold_until)
        oldest = min([ep.start_ts for ep in self._episodes] + [ts])
        bounds = self.cfg
        keep_from = (oldest - _FIT_BEFORE_S - bounds.theta_bounds_s[1]
                     - 2.0 * bounds.tau_bounds_s[1])
        self._holds = [iv for iv in self._holds if iv[1] is None or iv[1] >= keep_from]
        cfg = self.cfg
        for ep in self._episodes:
            if _finite(open_from):
                if ep.open_from is None:
                    if open_from >= ep.start_ts - 1.0:
                        ep.open_from = float(open_from)
                elif abs(open_from - ep.open_from) > 1.0:
                    # Another opening: its heat is modelled in the fit, and
                    # this episode's landing is looked for until it shows.
                    self._cut(ep, open_from + self.theta_s)
                if (ep.open_from is not None and abs(open_from - ep.open_from) <= 1.0
                        and _finite(open_until)):
                    ep.open_until = float(open_until)
            if heating_pct is not None and _finite(heating_pct):
                ep.heating_seen = True
                # The valve opened when a report above 0 changed after the
                # pulse began and before the zone could have shown its
                # close (a slow cloud poll reports late: 370 s seen).
                until = (ep.pulse_end_ts if ep.pulse_end_ts is not None else ts) + cfg.opened_check_s
                if (heating_pct > 0 and _finite(heating_ts)
                        and ep.start_ts - _OPENED_EARLY_S < heating_ts <= until):
                    ep.valve_opened = True
            if _finite(room_c) and ts > ep.samples[-1][0]:
                ep.samples.append((ts, float(room_c)))

    @staticmethod
    def _track(intervals: list[list[float | None]], ts: float, frm: float | None,
               until: float | None) -> None:
        """Follow an interval the doser keeps: the same start updates its
        end, a new start ends any still open, none ends them now."""
        if _finite(frm):
            end = float(until) if _finite(until) else None
            for iv in intervals:
                if abs(iv[0] - frm) < 1.0:
                    iv[1] = end
                    return
            for iv in intervals:
                if iv[1] is None:
                    iv[1] = min(ts, float(frm))
            intervals.append([float(frm), end])
        else:
            for iv in intervals:
                if iv[1] is None:
                    iv[1] = ts

    def _record(self, ts: float, room_c: float | None, open_from: float | None,
                open_until: float | None) -> None:
        """Keep the history: readings in time order, and the open intervals
        the doser books (an interval it no longer books has ended)."""
        if _finite(room_c) and (not self._history or ts > self._history[-1][0]):
            self._history.append((ts, float(room_c)))
        self._track(self._intervals, ts, open_from, open_until)
        horizon = ts - self.cfg.fit_history_s
        while self._history and self._history[0][0] < horizon:
            self._history.popleft()
        self._intervals = [iv for iv in self._intervals if iv[1] is None or iv[1] >= horizon]

    def _grid(self, t_to: float) -> tuple[list[float], list[float], list[float]] | None:
        """The history up to ``t_to`` on a fixed grid (the median spacing of
        the readings): the room linearly interpolated, the open fraction of
        each step exact from the intervals."""
        hist = [(t, y) for t, y in self._history if t <= t_to]
        if len(hist) < 5:
            return None
        gaps = [b[0] - a[0] for a, b in zip(hist, hist[1:]) if b[0] > a[0]]
        dt = _median(gaps)
        if dt is None or dt <= 0:
            return None
        dt = max(10.0, dt)
        t0 = hist[0][0]
        steps = int((hist[-1][0] - t0) // dt) + 1
        ts: list[float] = []
        room: list[float] = []
        frac: list[float] = []
        j = 0
        for k in range(steps):
            t = t0 + k * dt
            while j + 1 < len(hist) and hist[j + 1][0] <= t:
                j += 1
            if j + 1 < len(hist) and hist[j + 1][0] > hist[j][0]:
                w = (t - hist[j][0]) / (hist[j + 1][0] - hist[j][0])
                room.append(hist[j][1] + w * (hist[j + 1][1] - hist[j][1]))
            else:
                room.append(hist[j][1])
            ts.append(t)
            opened = 0.0
            for lo, hi in self._intervals:
                end = hi if hi is not None else t_to
                opened += max(0.0, min(t + dt, end) - max(t, lo))
            frac.append(_clamp(opened / dt, 0.0, 1.0))
        return ts, room, frac

    def note_pulse_end(self, ts: float) -> None:
        """The pulse ended (its off, or any command that is no maximum,
        booked): every episode whose pulse is still open."""
        if not _finite(ts):
            return
        for ep in self._episodes:
            if ep.pulse_end_ts is None:
                ep.pulse_end_ts = float(ts)

    def note_end_reason(self, start_ts: float | None, reason: str) -> None:
        """Why the pulse that started at ``start_ts`` ended (the regulator's
        dose_end): only one its own dose ended teaches the aim bias to dose
        more."""
        if not _finite(start_ts):
            return
        for ep in self._episodes:
            if abs(ep.start_ts - start_ts) < 1.0 and ep.end_reason is None:
                ep.end_reason = reason

    def observe(self, ts: float, setpoint_c: float, *, window: bool, boost: bool) -> list[Verdict]:
        """Judge each episode being measured if it can be: dropped when
        disturbed, judged once its heat has landed (:meth:`_landing`).
        Returns the verdicts, oldest episode first."""
        if not _finite(ts):
            return []
        out: list[Verdict] = []
        for ep in list(self._episodes):
            verdict = self._observe_one(ep, ts, setpoint_c, window=window, boost=boost)
            if verdict is not None:
                out.append(verdict)
        return out

    def _observe_one(self, ep: Episode, ts: float, setpoint_c: float, *,
                     window: bool, boost: bool) -> Verdict | None:
        cfg = self.cfg
        if window:
            return self._drop(ep, "a window opened")
        if boost:
            return self._drop(ep, "Boost started")
        if not _finite(setpoint_c) or abs(setpoint_c - ep.setpoint_c) > 0.05:
            return self._drop(ep, "the target changed")
        if ts - ep.samples[-1][0] > _EPISODE_MAX_GAP_S:
            return self._drop(ep, f"no reading for {(ts - ep.samples[-1][0]) / 60.0:.0f} min")
        if self._hold_in_window(ep, ts):
            return self._drop(ep, "a hold's heat (unknown) was in the window")
        # The opened check, only once the pulse has ended and the zone has
        # had opened_check_s to report: a late first report is no "did
        # nothing", and the verdict only keeps the episode from learning
        # (the heat booked for it stays booked: if the valve really never
        # opened the room lands short, the safe side).
        unconfirmed = ep.heating_seen and not ep.valve_opened
        if unconfirmed and ep.pulse_end_ts is not None and ts >= ep.pulse_end_ts + cfg.opened_check_s:
            self._remove(ep)
            return self._verdict(NOT_OPENED, "the valve never reported heating, so the pulse did nothing")
        if ep.landed_ts is None:
            landed, why, cut_short = self._landing(ep, ts)
            if why is not None:
                return self._drop(ep, why)
            ep.landed_ts, ep.cut_short = landed, cut_short
        if ep.landed_ts is not None and ts >= ep.landed_ts + _FIT_AFTER_LANDING_S and not unconfirmed:
            return self._judge(ep)
        return None

    def _hold_in_window(self, ep: Episode, now: float) -> bool:
        """A hold's heat reaches the episode's fit window: the window from
        ``_FIT_BEFORE_S`` before the pulse to now, widened before it by the
        landing horizon in use (a hold's heat still arriving).  Its opening
        is unknown, so neither the fit nor the landing can account for it
        (2.15.0 review: a pulse, then a hold at 60 % in a room cooling
        1.5 °C/h, fitted a gain of 0.11 and a spread of 30 min)."""
        lo = ep.start_ts - _FIT_BEFORE_S - self.landing_horizon_s
        return any(frm <= now and (until is None or until > lo) for frm, until in self._holds)

    def _stall(self, ts: list[float], ys: list[float], after: float) -> int | None:
        """The first reading at which the series, having risen, stalled
        after ``after`` (autotune._coast_end's rule)."""
        rising = False
        j0 = 0
        for j in range(len(ts)):
            while ts[j] - ts[j0] > _PEAK_WINDOW_S:
                j0 += 1
            if ts[j] - ts[j0] < 0.8 * _PEAK_WINDOW_S:
                continue
            sl = theil_sen(list(zip(ts[j0:j + 1], ys[j0:j + 1])))
            if sl is None:
                continue
            rate_h = sl * 3600.0
            if rising and ts[j] >= after and rate_h <= self._at.coast_stall_c_per_h:
                return j
            if rate_h >= self._at.rate_min_c_per_h:
                rising = True
        return None

    def _landing(self, ep: Episode, now: float) -> tuple[float | None, str | None, bool]:
        """When the episode's heat has landed (the reading it is judged at),
        None while not yet; why it cannot be judged, if so; and whether it
        landed at the cut.

        The room, having risen, stalled after the pulse ended
        (autotune._coast_end's rule; a rise after the stall is something
        else -- people, sun -- and is never counted), either as read or with
        the pre-pulse fall taken out: a small pulse may only slow a falling
        room, and its rise then shows only against the trend.  Looked for
        only until the next opening's heat can show (the cut): reached
        first, the episode lands there, cut short, if its own heat has had
        the visible dead time in use to show since the pulse ended.  Failing
        all, once theta + 4 tau have passed since the pulse ended (98 % of
        the model's heat has arrived), at most ``episode_max_s`` after the
        pulse ended."""
        if ep.pulse_end_ts is None:
            return None, None, False
        cut = ep.cut_ts
        samples = [p for p in ep.samples if cut is None or p[0] <= cut]
        ts = [t for t, _ in samples]
        ys = [y for _, y in samples]
        trend = [y + ep.fall0_c_per_s * (t - ep.start_ts) for t, y in samples]
        stalls = [j for j in (self._stall(ts, ys, ep.pulse_end_ts),
                              self._stall(ts, trend, ep.pulse_end_ts)) if j is not None]
        if stalls:
            return ts[min(stalls)], None, False
        if cut is not None and now >= cut:
            if cut - ep.pulse_end_ts < self.visible_dead_time_s or ts[-1] < ep.pulse_end_ts:
                return None, "the next pulse's heat came before this one's could show", False
            return ts[-1], None, True
        deadline = ep.pulse_end_ts + min(self.theta_s + 4.0 * self.tau_s,
                                         self.cfg.episode_max_s)
        return (ts[-1], None, False) if now >= deadline else (None, None, False)

    def _open_min(self, ep: Episode, now: float) -> float:
        if ep.open_from is None or not _finite(ep.open_from):
            return 0.0
        until = ep.open_until if ep.open_until is not None else now
        return max(0.0, min(until, now) - ep.open_from) / 60.0

    def _judge(self, ep: Episode) -> Verdict:
        """Fit the episode with its loss, from ``_FIT_BEFORE_S`` before the
        pulse to ``_FIT_AFTER_LANDING_S`` after its landing, the model's
        rise built from the whole history (earlier and later pulses' heat
        included).  Its landing is the room's highest reading from the
        pulse's end to the landing: the peak when the room came back up, the
        pulse-end reading when the heat only slowed its fall."""
        self._remove(ep)
        cfg = self.cfg
        landed_ts = ep.landed_ts if ep.landed_ts is not None else ep.samples[-1][0]
        open_min = self._open_min(ep, ep.samples[-1][0])
        after_end = [(t, y) for t, y in ep.samples
                     if ep.pulse_end_ts is not None and ep.pulse_end_ts <= t <= landed_ts]
        peak_ts, peak_room = max(after_end or [ep.samples[-1]], key=lambda p: p[1])
        peak_err = peak_room - ep.setpoint_c
        if ep.heating_seen and not ep.valve_opened:
            return self._verdict(NOT_OPENED, "the valve never reported heating, so the pulse did nothing")
        if open_min < cfg.min_open_min:
            return self._verdict(REJECTED, f"{open_min:.1f} open-min is too little to learn from")
        grid = self._grid(landed_ts + _FIT_AFTER_LANDING_S)
        fit = None
        if grid is not None:
            ts, room, frac = grid
            fit = fit_episode(ts, room, frac, 0.0, cfg, fit_loss=True,
                              window_start_ts=ep.start_ts - _FIT_BEFORE_S)
        if fit is None:
            return self._verdict(REJECTED, "no fit")
        if fit.rms_c > cfg.fit_max_rms_c:
            return self._verdict(REJECTED, f"the response did not fit the model (RMS {fit.rms_c:.3f})")
        loss_h = fit.loss_c_per_s * 3600.0
        if not _LOSS_MIN_C_PER_H <= loss_h <= _LOSS_MAX_C_PER_H:
            what = "a room warming by itself" if loss_h < 0 else "no plain cooling room"
            return self._verdict(REJECTED, f"a fitted loss of {loss_h:+.2f} °C/h is {what}")
        lo, hi = cfg.gain_accept
        g_use = self.gain_c_per_open_min
        if not lo * g_use <= fit.gain_c_per_open_min <= hi * g_use:
            return self._verdict(
                REJECTED,
                f"a gain of {fit.gain_c_per_open_min:.3f} is outside {lo:g}-{hi:g}x the {g_use:.3f} in use",
            )
        at_floor = (ep.pulse_end_ts is not None
                    and ep.pulse_end_ts - ep.start_ts <= cfg.pulse_min_s + 5.0)
        judged = Judged(
            ts=ep.start_ts, gain_c_per_open_min=fit.gain_c_per_open_min,
            theta_s=fit.theta_s, tau_s=fit.tau_s, peak_error_c=peak_err, open_min=open_min,
            at_floor=at_floor, rode_burn=ep.rode_burn, gap_start_c=ep.gap_start_c, rms_c=fit.rms_c,
            loss_c_per_s=fit.loss_c_per_s, shortfall_start_c=ep.setpoint_c - ep.room_start_c,
            end_reason=ep.end_reason,
        )
        self._judged.append(judged)
        self._accepted_total += 1
        before = (self.gain_c_per_open_min, self.theta_s, self.tau_s, self.bias_c)
        self._update(judged, ep.samples[-1][0])
        # What the gain in use promised, with the loss as it turned out.
        model = (ep.room_start_c - ep.setpoint_c + ep.gain_at_start * open_min
                 - fit.loss_c_per_s * (peak_ts - ep.start_ts))
        line = (
            f"{open_min:.1f} open-min → {_signed(peak_err)} "
            f"{'by the next pulse' if ep.cut_short else 'at the peak'} "
            f"(model {_signed(model)}); gain {fit.gain_c_per_open_min:.3f}, "
            f"delay {fit.theta_s / 60.0:.0f} min, spread {fit.tau_s / 60.0:.0f} min, "
            f"loss {loss_h:.2f} °C/h; gain in use {before[0]:.3f} → {self.gain_c_per_open_min:.3f}, "
            f"delay {before[1] / 60.0:.1f} → {self.theta_s / 60.0:.1f} min, "
            f"spread {before[2] / 60.0:.1f} → {self.tau_s / 60.0:.1f} min, "
            f"aim {_signed(self.bias_c)}"
        )
        if self._watchdog_fired:
            line += "; two hot landings: doses cut"
        return self._verdict(ACCEPTED, line)

    def _step_gain(self, current: float, target: float | None, now: float) -> float:
        """One asymmetric step of the gain towards ``target``: up (smaller
        doses) at once, down (bigger doses) slowly and rate-limited."""
        if target is None or not _finite(target) or abs(target - current) < 1e-9:
            return current
        if target > current:
            return current + self.cfg.step_safe_frac * (target - current)
        last = self._last_more.get("gain")
        if last is not None and now - last < self.cfg.step_more_interval_s:
            return current
        self._last_more["gain"] = now
        return current + self.cfg.step_more_frac * (target - current)

    def _step_shape(self, current: float, target: float | None) -> float:
        """One step of the delay or the spread towards ``target``, either
        way (neither is the safe direction)."""
        if target is None or not _finite(target):
            return current
        return current + self.cfg.step_safe_frac * (target - current)

    def _update(self, judged: Judged, now: float) -> None:
        cfg = self.cfg
        recent = list(self._judged)
        g_lo, g_hi = self._gain_bounds()
        self.gain_c_per_open_min = _clamp(
            self._step_gain(self.gain_c_per_open_min,
                            _median([j.gain_c_per_open_min for j in recent]), now), g_lo, g_hi)
        self.theta_s = _clamp(
            self._step_shape(self.theta_s, _median([j.theta_s for j in recent])),
            *cfg.theta_bounds_s)
        self.tau_s = _clamp(
            self._step_shape(self.tau_s, _median([j.tau_s for j in recent])),
            *cfg.tau_bounds_s)
        # The aim bias absorbs slow drift the gain misses: a landing short
        # moves it up, a hot one down.  A floor-length pulse that landed hot
        # says nothing about the aim: the floor, not the model, set that
        # landing.  Up (bigger doses) only from a pulse its own dose ended
        # (not its cap, not the stop rule: they set that landing) that
        # started near the target: a big dose landing short is a gain error,
        # which the gain corrects slowly (the owner's choice), and an
        # additive bias learned from it overdoses every small top-up.
        miss = cfg.bias_target_c - judged.peak_error_c
        if miss > 0:
            learn = (judged.end_reason == "covered"
                     and judged.shortfall_start_c is not None
                     and judged.shortfall_start_c <= cfg.bias_max_shortfall_c)
        else:
            learn = not judged.at_floor
        if learn:
            self.bias_c = _clamp(self.bias_c + cfg.bias_gain * miss,
                                 -cfg.bias_clamp_c, cfg.bias_clamp_c)
        self._watchdog_fired = False
        if len(recent) >= 2 and all(j.peak_error_c > cfg.watchdog_peak_c for j in recent[-2:]):
            self.gain_c_per_open_min = min(g_hi, self.gain_c_per_open_min * 1.5)
            self.bias_c = max(-cfg.bias_clamp_c, self.bias_c - 0.1)
            self._watchdog_fired = True

    def _remove(self, ep: Episode) -> None:
        if ep in self._episodes:
            self._episodes.remove(ep)

    def drop_all(self, why: str) -> list[Verdict]:
        """Drop every episode being measured (a heat-up took the pulse
        over: its heat is no pulse's)."""
        return [self._drop(ep, why) for ep in list(self._episodes)]

    def _drop(self, ep: Episode, why: str) -> Verdict:
        self._remove(ep)
        return self._verdict(DROPPED, why)

    def _verdict(self, kind: str, text: str) -> Verdict:
        if kind != ACCEPTED:
            text = f"not learned from: {text}"
        self.last_line = text
        return Verdict(kind=kind, line=text)

    # -- summary and persistence ----------------------------------------------
    def summary(self) -> dict[str, Any]:
        """The learned model: changes only when an episode is judged (the
        Top-up gain sensor's attributes, recorded)."""
        return {
            "gain_c_per_open_min": round(self.gain_c_per_open_min, 4),
            "delay_min": round(self.theta_s / 60.0, 1),
            "spread_min": round(self.tau_s / 60.0, 1),
            "visible_dead_time_min": round(self.visible_dead_time_s / 60.0, 1),
            "landing_horizon_min": round(self.landing_horizon_s / 60.0, 1),
            "bias_c": round(self.bias_c, 3),
            "episodes": self._accepted_total,
            "learning": "from the room's pulses" if self.learned else "seeded, nothing learned yet",
            "seeded_gain_c_per_open_min": round(self._seed_gain, 4),
            "last": self.last_line,
        }

    def diagnostics(self) -> dict[str, Any]:
        """The summary, the episodes kept and the ones being measured."""
        data = self.summary()
        data.update({
            "recent": [j.to_dict() for j in self._judged],
            "radiator_gaps_c": [j.gap_start_c for j in self._judged],
            "episodes_running": [
                {"start_ts": ep.start_ts, "pulse_end_ts": ep.pulse_end_ts,
                 "cut_ts": ep.cut_ts, "landed_ts": ep.landed_ts,
                 "valve_opened": ep.valve_opened}
                for ep in self._episodes
            ],
        })
        return data

    def to_dict(self) -> dict[str, Any]:
        return {
            "gain": self.gain_c_per_open_min, "theta_s": self.theta_s, "tau_s": self.tau_s,
            "bias_c": self.bias_c, "accepted_total": self._accepted_total,
            "seed_args": [self._seed_rate],
            "judged": [j.to_dict() for j in self._judged],
            "last_more": dict(self._last_more),
            "episodes": [ep.to_dict() for ep in self._episodes],
            "last_line": self.last_line,
            "history": [[round(t, 1), round(y, 3)] for t, y in self._history],
            "intervals": [[round(lo, 1), (round(hi, 1) if hi is not None else None)]
                          for lo, hi in self._intervals],
            "holds": [[round(lo, 1), (round(hi, 1) if hi is not None else None)]
                      for lo, hi in self._holds],
        }

    def restore(self, data: Any, now: float) -> None:
        """Take the learned values back; the episodes being measured only if
        they still join up with now.  Anything malformed is skipped, never
        raised.  Saves before 2.15.0's review kept three seed values (rate,
        dead time, coast) and one episode: only the rate is used now."""
        if not isinstance(data, dict):
            return
        seed = data.get("seed_args")
        if isinstance(seed, (list, tuple)) and len(seed) in (1, 3):
            self.seed(seed[0] if _finite(seed[0]) else None)
        judged = [j for j in (Judged.from_dict(d) for d in data.get("judged", []) or []) if j]
        accepted = data.get("accepted_total", 0)
        accepted = int(accepted) if _finite(accepted) and accepted >= 0 else len(judged)
        gain, theta, tau, bias = (data.get(k) for k in ("gain", "theta_s", "tau_s", "bias_c"))
        if accepted > 0 and _finite(gain, theta, tau):
            g_lo, g_hi = self._gain_bounds()
            self.gain_c_per_open_min = _clamp(float(gain), g_lo, g_hi)
            self.theta_s = _clamp(float(theta), *self.cfg.theta_bounds_s)
            self.tau_s = _clamp(float(tau), *self.cfg.tau_bounds_s)
        if _finite(bias):
            self.bias_c = _clamp(float(bias), -self.cfg.bias_clamp_c, self.cfg.bias_clamp_c)
        self._judged.clear()
        self._judged.extend(judged)
        self._accepted_total = max(accepted, len(judged))
        last_more = data.get("last_more")
        if isinstance(last_more, dict) and _finite(last_more.get("gain")):
            self._last_more = {"gain": min(float(last_more["gain"]), now)}
        line = data.get("last_line")
        self.last_line = line if isinstance(line, str) else None
        saved = data.get("episodes")
        if not isinstance(saved, list):
            saved = [data.get("episode")]
        episodes = [ep for ep in (Episode.from_dict(d) for d in saved) if ep is not None]
        self._episodes = [
            ep for ep in sorted(episodes, key=lambda e: e.start_ts)
            if ep.samples[-1][0] <= now + 1.0 and now - ep.samples[-1][0] <= _EPISODE_MAX_GAP_S
        ][-_OPEN_EPISODES_MAX:]
        self._restore_history(data, now)
        self._holds = self._restore_intervals(data.get("holds"), now, 1.0) or []

    def _restore_intervals(self, items: Any, now: float, ahead_s: float) -> list[list[float | None]] | None:
        """Saved intervals ([from, until | None], from at most ``ahead_s``
        in the future), or None when any is malformed."""
        out: list[list[float | None]] = []
        for item in items if isinstance(items, list) else []:
            if (not isinstance(item, (list, tuple)) or len(item) != 2 or not _finite(item[0])
                    or item[0] > now + ahead_s
                    or (item[1] is not None and not _finite(item[1]))):
                return None
            out.append([float(item[0]), float(item[1]) if item[1] is not None else None])
        return out

    def _restore_history(self, data: dict[str, Any], now: float) -> None:
        """The history, if it joins up with now (its last reading at most
        ``_EPISODE_MAX_GAP_S`` old, nothing from the future)."""
        hist = data.get("history")
        if not isinstance(hist, list):
            return
        rows: list[tuple[float, float]] = []
        for item in hist:
            if (not isinstance(item, (list, tuple)) or len(item) != 2
                    or not _finite(item[0], item[1]) or item[0] > now + 1.0):
                return
            if rows and item[0] <= rows[-1][0]:
                return
            rows.append((float(item[0]), float(item[1])))
        if not rows or now - rows[-1][0] > _EPISODE_MAX_GAP_S:
            return
        intervals = self._restore_intervals(data.get("intervals"), now, self.cfg.latency_s + 1.0)
        if intervals is None:
            return
        self._history = deque(r for r in rows if r[0] >= now - self.cfg.fit_history_s)
        self._intervals = intervals


# ---------------------------------------------------------------------------
# The house: pulses share the boiler's burns
# ---------------------------------------------------------------------------

@dataclass
class _RoomNote:
    ts: float
    dosing: bool
    ready_in_s: float | None
    lead_s: float


class PulseCoordinator:
    """Lets dosed rooms share the boiler's burns (design §12).

    The boiler is an on/off type: one radiator draws far less than its
    minimum output, so any single-radiator call cycles it on its flow
    thermostat.  Two rules, never changing a pulse's size (each room sizes
    its own): a room due soon fires together with another room's pulse, and
    a room at or below its target rides another zone's burn.  Every room
    reports its zone's heating each cycle, dosing or not, so a heat-up in
    one room is a burn the others can ride.
    """

    def __init__(self, cfg: DoseConfig) -> None:
        self.cfg = cfg
        self._zones: dict[str, tuple[float, bool]] = {}
        self._rooms: dict[str, _RoomNote] = {}
        self._pulses: dict[str, float] = {}
        self._wake: dict[str, Callable[[], Any]] = {}

    def reset(self) -> None:
        self._zones.clear()
        self._rooms.clear()
        self._pulses.clear()
        self._wake.clear()

    def register(self, room_id: str, wake: Callable[[], Any]) -> None:
        self._wake[room_id] = wake

    def unregister(self, room_id: str) -> None:
        for table in (self._zones, self._rooms, self._pulses, self._wake):
            table.pop(room_id, None)

    def note_zone(self, room_id: str, reported_ts: float, heating: bool | None) -> None:
        """A room's zone heating, as of ``reported_ts`` (when the report was
        made: a cloud poll may hand an old one on)."""
        if heating is None or not _finite(reported_ts):
            self._zones.pop(room_id, None)
            return
        self._zones[room_id] = (float(reported_ts), bool(heating))

    def burning_room(self, now: float, exclude: str) -> str | None:
        """Another room whose zone is heating on a report at most
        ``zone_report_max_age_s`` old."""
        for room_id in sorted(self._zones):
            ts, heating = self._zones[room_id]
            if room_id != exclude and heating and 0 <= now - ts <= self.cfg.zone_report_max_age_s:
                return room_id
        return None

    def note_room(self, room_id: str, now: float, *, dosing: bool,
                  ready_in_s: float | None, lead_s: float) -> None:
        self._rooms[room_id] = _RoomNote(
            ts=now, dosing=bool(dosing),
            ready_in_s=ready_in_s if _finite(ready_in_s) else None,
            lead_s=lead_s if _finite(lead_s) else 0.0,
        )

    def _due_soon(self, ready_in_s: float | None, lead_s: float) -> bool:
        return (ready_in_s is not None and _finite(ready_in_s, lead_s)
                and ready_in_s <= self.cfg.fire_together_lead_frac * lead_s)

    def note_pulse_start(self, room_id: str, now: float) -> list[str]:
        """A room started a pulse: wake every other dosing room that is due
        within its visible dead time, so it can fire in the same burn.
        Returns the rooms woken."""
        self._pulses[room_id] = now
        woken: list[str] = []
        for other in sorted(self._rooms):
            note = self._rooms[other]
            if other == room_id or not note.dosing or other not in self._wake:
                continue
            if not 0 <= now - note.ts <= self.cfg.zone_report_max_age_s:
                continue
            if not self._due_soon(note.ready_in_s, note.lead_s):
                continue
            try:
                self._wake[other]()
            except Exception:  # noqa: BLE001 - one room's failure must not stop the others
                _LOGGER.exception("waking %s for a pulse together with %s failed", other, room_id)
                continue
            woken.append(other)
        return woken

    def should_fire(
        self, room_id: str, now: float, *, virtual_error_c: float, ready: bool,
        ready_in_s: float | None, lead_s: float,
    ) -> tuple[str, str] | None:
        """("together", room) when another dosing room started a pulse within
        ``together_window_s`` and this one is due within its visible dead
        time; ("burn", room) when another zone is heating and this one is
        ready and at or below its target; None otherwise."""
        if not ready or not _finite(now):
            return None
        for other in sorted(self._pulses):
            started = self._pulses[other]
            note = self._rooms.get(other)
            if other == room_id or note is None or not note.dosing:
                continue
            if 0 <= now - started <= self.cfg.together_window_s and self._due_soon(ready_in_s, lead_s):
                return ("together", other)
        burning = self.burning_room(now, room_id)
        if (burning is not None and _finite(virtual_error_c)
                and virtual_error_c >= self.cfg.ride_burn_min_error_c):
            return ("burn", burning)
        return None


# ---------------------------------------------------------------------------
# The room: glue shared by the entity (climate_dosing.py) and the simulator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DoserOutcome:
    """What one cycle's dosing step asks of its caller."""

    lines: list[str]                      # lessons for the activity log
    wake_in_s: float | None               # re-judge a running pulse this soon (None: the minute timer will)
    woke: list[str]                       # rooms woken to fire together
    fired_with: tuple[str, str] | None    # ("burn" | "together", room) when the coordinator chose the moment


class RoomDoser:
    """Per-room dosing glue (one implementation for the entity and the
    simulator).  It books the valve's open time from Roomstat's own commands
    into the predictor, gives the regulator its inputs, feeds the learner
    and talks to the coordinator.

    Booking: a maximum (a pulse, a heat-up) opens the valve ``latency_s``
    after it is sent; any other command -- an off, a coast or stop, a hold
    -- closes it ``close_delay_s`` after, as an off does.  A hold's real
    opening is unknown (the Tado throttles it to 30-40 %), so its heat is
    not booked at all.  Booked as fully open it read as a fast cooling to
    the observer below once the hold ended (2.15.0 review: -2.7..-3.0 °C/h
    for 20 min against a true -0.5, and the first pulse after a hold fired
    above the target and peaked +0.15); unbooked, the observer sees the room
    warming after a hold, so the first pulse comes late and small and lands
    short, the owner's preferred side.  The hold is still marked (from its
    command to the next off or maximum + ``close_delay_s``): the learner
    learns from no episode whose fit window its heat reaches.

    The cooling the dose covers is read from the room *as if unheated*: the
    reading less the rise of Roomstat's heat that has arrived (the model's
    own account), through the regulator's 15-min Theil-Sen slope.  This is
    the Smith predictor's disturbance observer.  The room's own slope still
    holds part of the last landing's rise when the next pulse is due, which
    reads as a slower cooling (the entity test: -0.25 °C/h against a true
    -0.50), so the pulse came late and small and the troughs reached
    -0.13..-0.19; it also made each episode's fitted gain too low.
    """

    def __init__(self, cfg: DoseConfig, room_id: str,
                 coordinator: PulseCoordinator | None = None) -> None:
        self.cfg = cfg
        self.room_id = room_id
        self.coordinator = coordinator
        self.predictor = TopupPredictor(cfg)
        self.learner = DoseLearner(cfg)
        self._open_from: float | None = None
        self._open_until: float | None = None
        # A hold's heat, unbooked: from its command to the next off or
        # maximum + close_delay_s (None: none, or still holding).
        self._hold_from: float | None = None
        self._hold_until: float | None = None
        self._last_book: float | None = None
        self._enabled = False
        self._fire_with: tuple[str, str] | None = None
        self._fired: deque[tuple[float, str]] = deque()
        self.inflight_c = 0.0
        self.virtual_error_c: float | None = None
        # The rise of Roomstat's heat that has arrived (°C, cumulative, each
        # arrival at the gain in use then), and the slope of the room less
        # it (the regulator's room-slope estimator and window).
        self._delivered_c = 0.0
        self._loss = SlopeEstimator()
        # The cap's own observer (regulation.dose_cap_need_c): the rise of
        # Roomstat's heat that has arrived at the cap's rise per open-minute
        # (cumulative, each arrival at the gain then, so a change of the
        # tuner's rate is no step in the history: review 3), and the slope
        # of the room less it.  Nothing of the learned gain once the rate is
        # known, so the cap does not grow with a model it guards.
        self._cap_delivered_c = 0.0
        self._cap_loss = SlopeEstimator()
        # When the last heat booked nowhere (a hold's, a heat-up's) ended,
        # kept after its mark is gone: the next pulse waits for it to show.
        self._unbooked_last_ts: float | None = None

    @property
    def loss_slope_c_per_s(self) -> float | None:
        """The room's slope as if unheated (°C/s), None while unknown."""
        return self._loss.slope_c_per_s()

    @property
    def cap_loss_slope_c_per_s(self) -> float | None:
        """The room's slope less Roomstat's heat at the cap's rise per
        open-minute (°C/s), None while unknown."""
        return self._cap_loss.slope_c_per_s()

    # -- commands and booking ---------------------------------------------------
    @property
    def open(self) -> bool:
        """The valve is (as booked) open or about to open."""
        return self._open_from is not None and self._open_until is None

    @property
    def open_interval(self) -> tuple[float | None, float | None]:
        return self._open_from, self._open_until

    def seed(self, rate_c_per_h: float | None) -> None:
        """The auto-tuner's heating rate seeds the gain (DoseLearner.seed)."""
        self.learner.seed(rate_c_per_h)

    def note_command(self, ts: float, command_c: float, *, min_c: float,
                     max_c: float, ok: bool, pulse: bool = True) -> None:
        """Every command tried, whatever the path (regulation, Roomstat's own
        off, summer, the watchdog).  A maximum (``max_c`` - 0.05 or more)
        opens the booking; anything else closes it as an off does, and ends
        the pulse of an episode being measured (see the class docstring).  A
        command between the off (``min_c`` + 0.05 or less) and the maximum
        is a hold: its heat is marked, not booked, until the next off or
        maximum.  An unknown ``min_c`` makes every command short of the
        maximum a hold (nothing learned from it, the safe way).

        A failed call books as if it had gone through only when that is the
        safe way: a maximum may have landed (a call that timed out but was
        taken; climate_controllers.pending_command counts it as on its way),
        so it opens the booking, which at worst overstates the heat in
        flight (a smaller dose) until the valve is seen shut; a failed off
        or hold leaves the booking as it was (an open valve stays booked
        open).  2.15.0 review: unbooked, a failed 25 °C left the pulse with
        no heat in flight, and one that landed ran the valve 15 min."""
        if not _finite(ts, command_c):
            return
        cfg = self.cfg
        is_max = _finite(max_c) and command_c >= max_c - 0.05
        if is_max and not pulse:
            # A heat-up's maximum (``pulse`` False): a cold radiator's heat,
            # not a pulse's, unknown to this model like a hold's (the
            # living room's 34-min heat-up of 31 Oct 2026 booked 1.48 °C
            # where the room rose 1.2, and the observer would have read the
            # gap as a fast cooling).  Marked, not booked.
            if ok:
                self._unbooked_heat(ts)
            return
        if not ok and not is_max:
            return
        if ok:
            self._mark_hold(ts, is_max or (_finite(min_c) and command_c <= min_c + 0.05))
        if is_max:
            if self.open:
                return
            if self._open_until is not None and self._open_until > ts + cfg.latency_s:
                # Re-opened while it was still closing: one interval.
                self._open_until = None
                return
            self._open_from = ts + cfg.latency_s
            self._open_until = None
            return
        if self.open:
            self._open_until = max(self._open_from, ts + cfg.close_delay_s)
        self.learner.note_pulse_end(ts)

    def _unbooked_heat(self, ts: float) -> None:
        """Heat of unknown size from now (a heat-up's maximum, or a pulse
        a heat-up took over): a pulse's booking closes, the hold mark (heat
        the learner must not fit) starts or carries on."""
        if self.open:
            self._open_until = max(self._open_from, ts + self.cfg.close_delay_s)
            self.learner.note_pulse_end(ts)
        self._mark_hold(ts, False)

    def _mark_hold(self, ts: float, max_or_off: bool) -> None:
        """A command the valve took: a maximum or an off ends a hold's mark
        (its heat still arriving for the close delay), a hold starts one or
        carries it on."""
        if max_or_off:
            if self._hold_from is not None and self._hold_until is None:
                self._hold_until = ts + self.cfg.close_delay_s
        elif self._hold_from is None or (self._hold_until is not None and self._hold_until <= ts):
            self._hold_from, self._hold_until = ts, None
        elif self._hold_until is not None:
            self._hold_until = None          # held again while its heat still arrived: one hold

    def note_valve_shut(self, now: float) -> None:
        """The valve is seen shut (the TRV reports off) with no heat command
        of Roomstat's on its way: whatever the booking says, it is shut now
        (and a pulse being measured has ended).  Without this an off that
        was never sent (the valve already showed off, a restart in between)
        would leave the booking open for good and the heat in flight growing
        without end."""
        if not _finite(now):
            return
        if self.open:
            self._open_until = max(self._open_from, now)
        if self._hold_from is not None and self._hold_until is None:
            self._hold_until = max(self._hold_from, now)
        self.learner.note_pulse_end(now)

    def step(self, now: float) -> None:
        """Book the open time since the last step into the predictor, at the
        middle of the overlap, and let the predictor deliver."""
        if not _finite(now):
            return
        last = self._last_book
        before = self.predictor.committed_open_min()
        booked = 0.0
        if last is not None and now > last and self._open_from is not None:
            until = self._open_until if self._open_until is not None else now
            lo, hi = max(last, self._open_from), min(now, until)
            if hi > lo:
                booked = (hi - lo) / 60.0
                self.predictor.book(0.5 * (lo + hi), booked)
        self.predictor.step(now, self.learner.theta_s, self.learner.tau_s)
        # What arrived at the room this step (left the emitter).
        arrived = max(0.0, before + booked - self.predictor.committed_open_min())
        self._delivered_c += self.learner.gain_c_per_open_min * arrived
        cap_gain = self.cap_gain_c_per_open_min
        self._cap_delivered_c += (
            cap_gain if cap_gain is not None else self.learner.gain_c_per_open_min) * arrived
        if last is None or now > last:
            self._last_book = now
        if self._open_until is not None and self._open_until <= now:
            self._open_from = self._open_until = None
        if self._hold_until is not None and self._hold_until <= now:
            self._unbooked_last_ts = self._hold_until
            self._hold_from = self._hold_until = None
        self.inflight_c = self.predictor.inflight_c(self.learner.gain_c_per_open_min)

    def inputs(
        self, now: float, *, enabled: bool, setpoint_c: float, room_c: float,
        room_slope_c_per_s: float | None, state: Any, deadband_c_per_h: float,
        release_below_c: float, room_fresh: bool = True,
    ) -> dict[str, Any]:
        """Step the predictor and return regulation.compute()'s dosing
        keyword arguments.  ``fire_now`` is the coordinator's verdict on
        this cycle's own numbers.  A reading bridged over a sensor gap
        (``room_fresh`` False) does not feed the observer: a flat room
        against rising delivered heat would read as a faster cooling."""
        self.step(now)
        lr = self.learner
        gain = lr.gain_c_per_open_min
        self._enabled = bool(enabled)
        self._fire_with = None
        e_star = (setpoint_c - room_c - self.inflight_c) if _finite(setpoint_c, room_c) else None
        self.virtual_error_c = e_star
        # The room as if unheated: its slope is the cooling the dose covers.
        # Only while dosing decides (a window's draught or a boost is not
        # the room's loss): otherwise the history starts afresh.
        cap_gain = self.cap_gain_c_per_open_min
        if not self._enabled:
            self._loss.reset()
            self._cap_loss.reset()
        elif room_fresh and _finite(room_c):
            self._loss.add(now, room_c - self._delivered_c)
            # Taken at the gain in use while the cap has none of its own (the
            # pulse then has the fixed maximum), so it is primed once one comes.
            self._cap_loss.add(now, room_c - self._cap_delivered_c)
        loss_slope = self._loss.slope_c_per_s()
        cooling_slope = loss_slope if loss_slope is not None else room_slope_c_per_s
        fire_now = None
        if (self._enabled and self.coordinator is not None and e_star is not None
                and getattr(state, "settled", False) and getattr(state, "mode", None) != MODE_DOSE):
            fall = dose_fall_c_per_s(cooling_slope, deadband_c_per_h)
            lead = lr.visible_dead_time_s
            ready = self._ready(now, state, setpoint_c)
            ready_in = dose_ready_in_s(e_star, fall, lead, release_below_c) if ready else None
            verdict = self.coordinator.should_fire(
                self.room_id, now, virtual_error_c=e_star, ready=ready,
                ready_in_s=ready_in, lead_s=lead)
            if verdict is not None:
                fire_now, self._fire_with = verdict[0], verdict
        return {
            "dosing": self._enabled,
            "inflight_c": self.inflight_c,
            "tail_c": gain * self.cfg.close_delay_s / 60.0 if self.open else 0.0,
            "gain_c_per_open_min": gain,
            "theta_s": lr.theta_s,
            "tau_s": lr.tau_s,
            "bias_c": lr.bias_c,
            "now_ts": now,
            "fire_now": fire_now,
            "loss_slope_c_per_s": loss_slope,
            "cap_gain_c_per_open_min": cap_gain,
            "cap_inflight_c": self.predictor.inflight_c(cap_gain) if cap_gain is not None else 0.0,
            "cap_loss_slope_c_per_s": self._cap_loss.slope_c_per_s(),
            "unbooked_until_ts": self.unbooked_until_ts(now),
        }

    @property
    def cap_gain_c_per_open_min(self) -> float | None:
        """The room's rise per open-minute a pulse's cap is sized on
        (regulation.dose_cap_s): the auto-tuner's heating rate / 60, which
        the room's heat-ups measured independently of this model (owner, 31
        Oct 2026).  None while the tuner has none: the pulse then has the
        fixed maximum (the learned gain is the model the cap guards against,
        so it is no cap: review 3)."""
        return self.learner.rate_gain_c_per_open_min

    def _ready(self, now: float, state: Any, setpoint_c: float | None) -> bool:
        """The regulator's readiness (regulation._dose_step): theta + 2 tau
        from the last pulse's start (at most refractory_max_s) and theta +
        tau / 2 from its end, unless the target was raised since; heat
        booked nowhere must have had theta + tau / 2 to show.  Times from
        the future count as now (the heat in flight is already taken off the
        virtual error)."""
        lr = self.learner
        lead = lr.visible_dead_time_s
        unbooked = self.unbooked_until_ts(now)
        if unbooked is not None and now - min(unbooked, now) < lead:
            return False
        last_sp = getattr(state, "last_pulse_setpoint_c", None)
        if _finite(last_sp, setpoint_c) and setpoint_c > last_sp + 0.05:
            return True
        start = getattr(state, "last_pulse_start_ts", None)
        end = getattr(state, "last_pulse_end_ts", None)
        if _finite(start) and now - min(start, now) < min(
                lr.landing_horizon_s, self.cfg.refractory_max_s):
            return False
        return not (_finite(end) and now - min(end, now) < lead)

    def unbooked_until_ts(self, now: float) -> float | None:
        """When heat booked nowhere (a hold's, a heat-up's maximum) last went
        to the valve: now while it still goes, None when there is none."""
        if self._hold_from is None:
            return self._unbooked_last_ts
        if self._hold_until is None:
            return now
        return self._hold_until

    def resume_open(self, now: float) -> None:
        """A pulse of ours taken back after a restart whose booking did not
        join up (the predictor older than its restore age): the valve holds
        the maximum, so it is booked open from now."""
        if not _finite(now) or self.open:
            return
        self._open_from, self._open_until = float(now), None
        if self._last_book is None:
            self._last_book = float(now)

    def note_valve_heating_unbooked(self, now: float) -> None:
        """The valve is seen holding a heat setpoint no command of ours
        explains (a hold sent before dosing was on, before the upgrade from
        2.15.0): its heat is booked nowhere, so it is marked like a hold's,
        until an off or a maximum of ours, or the valve seen shut."""
        if not _finite(now) or self.open:
            return
        self._mark_hold(now, False)

    def after_compute(
        self, now: float, result: Any, *, setpoint_c: float, room_c: float | None,
        room_slope_c_per_s: float | None, trv_internal_c: float | None,
        heating_pct: float | None, heating_ts: float | None, window: bool, boost: bool,
        sent: bool, room_fresh: bool = True,
    ) -> DoserOutcome:
        """After the cycle's send (so a pulse's open interval is booked):
        episodes, the learner, the coordinator, and when to re-judge a
        running pulse.

        ``sent``: this cycle's command went out.  A pulse that fired but did
        not go out (a change by hand settling, a window's delay, a failed
        call) starts nothing here: no episode, no count, no room woken to
        join it (the regulator restarts its clock when it goes out).
        ``room_fresh`` False: the reading is bridged over a sensor gap, and
        neither the learner's history nor an episode takes it."""
        lines: list[str] = []
        woke: list[str] = []
        fired_with: tuple[str, str] | None = None
        lr = self.learner
        fired_by = getattr(result, "fired_by", None)
        if fired_by and sent:
            if room_fresh and _finite(room_c):
                # The episode's loss: the room as if unheated when known (the
                # room's own slope would make the fitted gain too low).
                loss = self._loss.slope_c_per_s()
                cooling = loss if loss is not None else room_slope_c_per_s
                fall0 = max(0.0, -cooling) if _finite(cooling) else 0.0
                gap = (trv_internal_c - room_c) if _finite(trv_internal_c) else None
                dropped = lr.start_episode(
                    now, setpoint_c, room_c, fall0, gap,
                    need_start_c=getattr(result, "need_c", 0.0), rode_burn=fired_by != "release")
                if dropped:
                    lines.append(dropped)
            if fired_by in ("burn", "together") and self._fire_with is not None:
                fired_with = self._fire_with
            self._fired.append((now, fired_by))
            if self.coordinator is not None:
                woke = self.coordinator.note_pulse_start(self.room_id, now)
        dose_end = getattr(result, "dose_end", None)
        if dose_end == "heat_up":
            # The pulse's 25 °C carries on as a heat-up: from now its heat
            # is a heat-up's (unbooked, marked), and nothing of it is learned.
            self._unbooked_heat(now)
            lines.extend(v.line for v in lr.drop_all("a heat-up took the pulse over"))
        elif dose_end:
            lr.note_pulse_end(now)
            lr.note_end_reason(
                getattr(getattr(result, "new_state", None), "last_pulse_start_ts", None), dose_end)
        lr.note_cycle(now, room_c if room_fresh else None, heating_pct=heating_pct,
                      heating_ts=heating_ts, open_from=self._open_from, open_until=self._open_until,
                      hold_from=self._hold_from, hold_until=self._hold_until)
        # The verdicts only decide what is learned: the heat booked stays
        # booked whatever they say (a "did nothing" on a late zone report
        # cleared it mid-pulse, and every pulse then ran to its maximum).
        lines.extend(v.line for v in lr.observe(now, setpoint_c, window=window, boost=boost))
        while self._fired and now - self._fired[0][0] > _COUNT_WINDOW_S:
            self._fired.popleft()
        if self.coordinator is not None:
            self.coordinator.note_room(
                self.room_id, now, dosing=self._enabled,
                ready_in_s=getattr(result, "ready_in_s", None), lead_s=lr.visible_dead_time_s)
        return DoserOutcome(lines=lines, wake_in_s=self._wake_in(now, result),
                            woke=woke, fired_with=fired_with)

    def _wake_in(self, now: float, result: Any) -> float | None:
        """Seconds until a running pulse should be judged again: its minimum
        length, or the open time still needed to cover its need (after the
        valve has opened), at the latest its cap.  None when the minute
        timer comes first."""
        if getattr(result, "mode", None) != MODE_DOSE:
            return None
        cfg = self.cfg
        pulse_s = getattr(result, "pulse_s", 0.0) or 0.0
        need = getattr(result, "need_c", 0.0) or 0.0
        gain = self.learner.gain_c_per_open_min
        to_open = max(0.0, self._open_from - now) if self._open_from is not None else 0.0
        cover = to_open + (need / gain * 60.0 if gain > 0 and need > 0 else 0.0)
        wake = max(cfg.pulse_min_s - pulse_s, cover)
        cap = getattr(result, "pulse_cap_s", None)
        if _finite(cap):
            wake = min(wake, max(0.0, cap - pulse_s))
        if wake >= 60.0:
            return None
        return max(5.0, wake)

    def note_zone(self, now: float, heating_pct: float | None, hvac_heating: bool | None,
                  reported_ts: float | None = None) -> None:
        """This room's zone, every cycle, dosing or not: the house's burn
        signal (the heating % when known, else the entity's hvac_action).
        The heating % counts as of its report (``reported_ts``, when known
        and not from the future; else now): a stale "heating" is no burn,
        however recently the room cycled."""
        if self.coordinator is None or not _finite(now):
            return
        ts = now
        if heating_pct is not None and _finite(heating_pct):
            heating: bool | None = heating_pct > 0
            if _finite(reported_ts) and reported_ts <= now:
                ts = float(reported_ts)
        else:
            heating = hvac_heating if hvac_heating in (True, False) else None
        self.coordinator.note_zone(self.room_id, ts, heating)

    # -- summary and persistence --------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """What changes only when a pulse starts or an episode is judged
        (the Top-up gain sensor's attributes, which the recorder keeps: a
        live value here wrote a row on 489 of 504 cycles, about 2 KB each).
        A pulse leaving the 24-h counts also changes it, once per pulse."""
        data = self.learner.summary()
        kinds = [k for _, k in self._fired]
        data.update({
            "pulses_24h": len(kinds),
            "rode_burn_24h": kinds.count("burn"),
            "together_24h": kinds.count("together"),
        })
        return data

    def diagnostics(self) -> dict[str, Any]:
        """Everything: the summary, the episodes kept and being measured,
        and the live values (heat in flight, the booking, the observer)."""
        data = self.learner.diagnostics()
        data.update(self.summary())
        loss = self.loss_slope_c_per_s
        cap_loss = self.cap_loss_slope_c_per_s
        data.update({
            "inflight_c": round(self.inflight_c, 3),
            "valve_open": self.open,
            "open_from": self._open_from,
            "open_until": self._open_until,
            "hold_from": self._hold_from,
            "hold_until": self._hold_until,
            "episode_running": self.learner.episode is not None,
            "loss_slope_c_per_h": round(loss * 3600.0, 3) if loss is not None else None,
            "cap_loss_slope_c_per_h": (round(cap_loss * 3600.0, 3)
                                       if cap_loss is not None else None),
            "virtual_error_c": (round(self.virtual_error_c, 3)
                                if _finite(self.virtual_error_c) else None),
        })
        return data

    def to_dict(self) -> dict[str, Any]:
        return {
            "predictor": self.predictor.to_dict(),
            "learner": self.learner.to_dict(),
            "open_from": self._open_from,
            "open_until": self._open_until,
            "hold_from": self._hold_from,
            "hold_until": self._hold_until,
            "last_book": self._last_book,
            "fired": [[round(t, 1), k] for t, k in self._fired],
            "delivered_c": round(self._delivered_c, 5),
            "loss": self._loss.to_dict(),
            "cap_delivered_c": round(self._cap_delivered_c, 5),
            "cap_loss": self._cap_loss.to_dict(),
            "unbooked_last_ts": self._unbooked_last_ts,
        }

    def restore(self, data: Any, now: float) -> None:
        """Take each part back under its own rules: the predictor (and the
        booking clock) only if it joins up with now, the learner always, the
        open interval unless it comes from the future."""
        if not isinstance(data, dict) or not _finite(now):
            return
        self.learner.restore(data.get("learner"), now)
        # The unheated room's slope, if its samples still join up with now
        # (the SlopeEstimator's own rules), with the delivered rise they
        # were taken against.
        delivered = data.get("delivered_c")
        if _finite(delivered) and self._loss.restore(data.get("loss"), now):
            self._delivered_c = float(delivered)
        else:
            self._loss.reset()
            self._delivered_c = 0.0
        # The cap's observer likewise, with the rise it was taken against
        # (none saved before 2.16.0: it starts afresh).
        cap_delivered = data.get("cap_delivered_c")
        if _finite(cap_delivered) and self._cap_loss.restore(data.get("cap_loss"), now):
            self._cap_delivered_c = float(cap_delivered)
        else:
            self._cap_loss.reset()
            self._cap_delivered_c = 0.0
        if self.predictor.restore(data.get("predictor"), now):
            last = data.get("last_book")
            self._last_book = float(last) if _finite(last) and last <= now + 1.0 else self.predictor.last_step
        else:
            self._last_book = None
        cfg = self.cfg
        open_from, open_until = data.get("open_from"), data.get("open_until")
        if _finite(open_from) and open_from <= now + cfg.latency_s + 1.0 and self._last_book is not None:
            self._open_from = float(open_from)
            if _finite(open_until) and open_until <= now + cfg.close_delay_s + 1.0:
                self._open_until = float(open_until)
            elif open_until is None:
                self._open_until = None
            else:
                self._open_from = self._open_until = None
        unbooked_last = data.get("unbooked_last_ts")
        self._unbooked_last_ts = (float(unbooked_last)
                                  if _finite(unbooked_last) and unbooked_last <= now + 1.0 else None)
        # A hold's mark, unless it comes from the future.
        hold_from, hold_until = data.get("hold_from"), data.get("hold_until")
        self._hold_from = self._hold_until = None
        if _finite(hold_from) and hold_from <= now + 1.0:
            if hold_until is None:
                self._hold_from = float(hold_from)
            elif _finite(hold_until) and hold_from <= hold_until <= now + cfg.close_delay_s + 1.0:
                self._hold_from, self._hold_until = float(hold_from), float(hold_until)
        self._fired.clear()
        for item in data.get("fired", []) or []:
            if (isinstance(item, (list, tuple)) and len(item) == 2 and _finite(item[0])
                    and item[0] <= now + 1.0 and now - item[0] <= _COUNT_WINDOW_S
                    and item[1] in ("release", "burn", "together")):
                self._fired.append((float(item[0]), item[1]))
        self.inflight_c = self.predictor.inflight_c(self.learner.gain_c_per_open_min)

    def reset(self) -> None:
        """Forget what was learned and the heat booked (Reset auto-tune)."""
        self.learner.reset()
        self.predictor.reset()
        self._hold_from = self._hold_until = None
        self._last_book = None
        self.inflight_c = 0.0
        self._loss.reset()
        self._delivered_c = 0.0
        self._cap_loss.reset()
        self._cap_delivered_c = 0.0
        self._unbooked_last_ts = None
