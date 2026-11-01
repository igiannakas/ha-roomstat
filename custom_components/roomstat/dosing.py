"""
Dosed top-ups: full-power pulses sized by a Smith predictor (an option per
room, 2.15.0; climate_dosing.py on the entity, regulation.py's ``dose``
mode).  Design and evidence: project doc
claude/roomstat-smith-predictor-dosed-topups-2026-10-30.md; the pooled
learner (2.17.0): claude/roomstat-pooled-dose-learner-2026-11-01.md.

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

The model, learned from pooled pulses (2.17.0)
----------------------------------------------
First order plus dead time: a delay theta, a first-order spread tau, times
a gain in °C of room rise per open-minute.  Fitted per episode on the 19
settled top-ups of 25-30 Oct 2026 (tests/fixtures/
living_room_topups_2026_10.json, :func:`fit_episode`, kept as the offline
tool): gain median 0.041 (quartiles 0.021 / 0.062), theta 4 min (2 / 9),
tau 6 min (4 / 12).

Until 2.16.0 each live pulse was fitted on its own.  Fits of 3-7-min pulses
are noise: the gain trades against the room's loss (r = 0.93 over the six
fits of 1 Nov 2026) and against tau along a ridge 4-min pulses cannot
resolve, so the living room's six fits that night scattered 0.049-0.114
and the gain in use went 0.044 -> 0.089, spread 8 -> 25 min, while every
pooled fit and the heat balance put it at 0.045-0.056.  Since 2.17.0
:func:`pooled_fit` fits the last ten usable pulses together, on 2-min bins
of the room's readings in unbroken chains (:class:`ChainStore`):

    room(t) = baseline(t) + gain * S(t; theta, tau)

S the closed-form FOPDT response of every booked opening
(:func:`fopdt_cumulative`), the baseline the room as if unheated: one value
per bin with a second-difference penalty (an integrated random walk, its
slope -- the loss -- wandering about 0.05 °C/h per 30 min; solved by a
banded Cholesky, :class:`BandedSmoother`).  theta and tau are held by
log-normal priors (5 and 8 min), the gain's prior is centred on the
auto-tuner's heating rate; the posterior on the grid is tempered for the
residuals' autocorrelation and its sd inflated.  The gain in use moves only
when the fit is sure (sd of ln gain <= 0.2) and its best value lies outside
its +-1 sd band: towards smaller doses at once, towards bigger ones at most
15 % per 12 h (:meth:`DoseLearner.apply_fit`).  Each pulse's episode still
finds its landing, which teaches the aim bias and arms the
two-hot-landings watchdog.

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
import datetime
import logging
import math
import operator
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
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
# A hold's heat (heat booked nowhere) this long before a pulse, plus the
# landing horizon in use, or since, voids its landing (2.15.0 review: a
# pulse, then a hold at 60 % in a room cooling 1.5 °C/h).
_HOLD_LOOKBACK_S = 900.0
# An episode is judged this long after its landing (the readings since
# confirm the peak).
_JUDGE_AFTER_LANDING_S = 300.0
# Pulses remembered for the 24-h counts on the sensor.
_COUNT_WINDOW_S = 86400.0
# Episodes judged at once at most: a pulse starts while the last one's heat
# is still being measured whenever the model under-doses (the refractory,
# theta + 2 tau, is shorter than the time to judge, 25-35 min).  More than
# this open means the landings are not being found: the oldest is dropped.
_OPEN_EPISODES_MAX = 3
# The learner's save layout (2.17.0).  An older save (2.15.0 / 2.16.0, the
# single-pulse values) is not taken back.
_LEARNER_VERSION = 2
# A chain shorter than this (20 min of 2-min bins) carries no baseline worth
# fitting.
_MIN_CHAIN_BINS = 10
# The gain grid: the anchor times e^x, x in +-1.8 (0.17-6x) in steps of 0.02.
_GAIN_GRID_HALF_LN = 1.8
_GAIN_GRID_STEP_LN = 0.02
# A booked opening is kept while its heat may still reach a kept chain:
# the longest delay and dead start plus 12 of the longest spread (e^-12).
_TAIL_KEEP_TAU = 12.0
# Marks of heat booked nowhere are kept this long after they ended (the
# chain settle and the landing look-back fit in it).
_MARK_KEEP_S = 3 * 3600.0
# Distinct pooled-fit lines kept on records: besides the usable pulses kept
# for the window, the newest landings, for the two-hot-landings watchdog.
_RECORDS_LANDINGS_KEPT = 2

_UNBOOKED_WHY = "heat booked nowhere (a hold's, a heat-up's, the app's) in its window"

try:  # Python 3.12+: a dot product in C
    _dot = math.sumprod
except AttributeError:  # pragma: no cover - Python 3.11
    def _dot(p: Iterable[float], q: Iterable[float]) -> float:
        return sum(map(operator.mul, p, q))


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


def _utc_hhmm(ts: float) -> str:
    """The default clock for the activity lines (the entity passes Home
    Assistant's time zone)."""
    try:
        return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


def _opt(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None and _finite(value) else None


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
    ``_RESPONSE_SUBSTEP_S`` (the delay to the nearest one).  The discrete
    twin of :func:`fopdt_cumulative` (and of the predictor's delay line)."""
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


def fopdt_cumulative(
    ts: Sequence[float],
    intervals: Iterable[tuple[float, float]],
    theta_s: float,
    tau_s: float,
    dead_start_s: float = 0.0,
) -> list[float]:
    """The open-minutes delivered to the room by each time in ``ts``, for
    the valve open over ``intervals`` ([from, until), seconds): the exact
    solution of the first-order-plus-dead-time model, no sub-steps.

    With a' = from + theta + c0 and w = until - from - c0 (``c0`` the dead
    start: the first c0 seconds of each opening deliver nothing), each
    interval delivers, in seconds of open valve,

        0                                        t <= a'
        (t - a') - tau (1 - e^-(t - a')/tau)       a' < t <= a' + w
        w - tau (e^-(t - a' - w)/tau - e^-(t - a')/tau)   later,

    and the sum over the intervals, divided by 60, is S(t)."""
    out = [0.0] * len(ts)
    if not _finite(theta_s, tau_s, dead_start_s) or tau_s <= 0:
        return out
    for frm, until in intervals:
        if not _finite(frm, until):
            continue
        a1 = frm + theta_s + dead_start_s
        w = until - frm - dead_start_s
        if w <= 0:
            continue
        b1 = a1 + w
        for k, t in enumerate(ts):
            if t <= a1:
                continue
            if t <= b1:
                x = t - a1
                out[k] += x - tau_s * (1.0 - math.exp(-x / tau_s))
            else:
                out[k] += w - tau_s * (math.exp(-(t - b1) / tau_s) - math.exp(-(t - a1) / tau_s))
    return [v / 60.0 for v in out]


def _ramp_events(intervals: Sequence[tuple[float, float]], theta_s: float,
                 dead_start_s: float, ref_ts: float) -> list[tuple[float, float]]:
    """The openings as ramp events, relative to ``ref_ts`` and sorted: each
    interval's FOPDT response is R(t - a') - R(t - b'), R the response to a
    unit ramp (R(x) = x - tau + tau e^-x/tau for x > 0)."""
    events: list[tuple[float, float]] = []
    for frm, until in intervals:
        w = until - frm - dead_start_s
        if w <= 0:
            continue
        a1 = frm + theta_s + dead_start_s - ref_ts
        events.append((a1, 1.0))
        events.append((a1 + w, -1.0))
    events.sort()
    return events


def _ramps_on_grid(x0: float, step_s: float, n: int, events: Sequence[tuple[float, float]],
                   tau_s: float) -> list[float]:
    """S (open-minutes) at x0 + k step_s, k < n, from ramp events (times on
    the same reference).  The sum of ramps is A (x - tau) - B + tau E, with
    A = sum s_j, B = sum s_j c_j and E = sum s_j e^-(x - c_j)/tau over the
    events before x; E decays by a constant factor per step, so the whole
    grid costs O(n + events) however many openings it holds (equal to
    :func:`fopdt_cumulative` to rounding)."""
    r = math.exp(-step_s / tau_s)
    a_sum = b_sum = e_sum = 0.0
    p = 0
    m = len(events)
    out: list[float] = []
    append = out.append
    inv60 = 1.0 / 60.0
    x = x0
    while p < m and events[p][0] < x:
        c, s = events[p]
        a_sum += s
        b_sum += s * c
        e_sum += s * math.exp(-(x - c) / tau_s)
        p += 1
    append((a_sum * (x - tau_s) - b_sum + tau_s * e_sum) * inv60)
    for k in range(1, n):
        x = x0 + k * step_s
        e_sum *= r
        while p < m and events[p][0] < x:
            c, s = events[p]
            a_sum += s
            b_sum += s * c
            e_sum += s * math.exp(-(x - c) / tau_s)
            p += 1
        append((a_sum * (x - tau_s) - b_sum + tau_s * e_sum) * inv60)
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


# fit_episode's grids (the per-episode fit of the 19 hold top-ups of 25-30
# Oct 2026, and 2.16.0's live learner).
EPISODE_THETA_GRID_S: tuple[float, ...] = tuple(float(t) for t in range(120, 1201, 60))
EPISODE_TAU_GRID_S: tuple[float, ...] = (
    240.0, 360.0, 480.0, 600.0, 720.0, 900.0, 1080.0, 1320.0, 1560.0, 1800.0,
)


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


def fit_episode(
    ts: list[float],
    room: list[float],
    open_fraction: list[float],
    fall0_c_per_s: float,
    *,
    fit_loss: bool = False,
    window_start_ts: float | None = None,
    theta_grid_s: Sequence[float] = EPISODE_THETA_GRID_S,
    tau_grid_s: Sequence[float] = EPISODE_TAU_GRID_S,
) -> Fit | None:
    """Fit one episode's response on its own (design §6, §11): the offline
    tool for replays (the 19 hold top-ups of 25-30 Oct 2026).  The live
    learner pools pulses instead (:func:`pooled_fit`): fitted on its own a
    3-7-min pulse scatters 0.7-2x.

    ``ts``/``room``: the room's readings; ``open_fraction[i]``: the
    fraction of [ts[i], ts[i+1]) the valve was open.  A grid over theta and
    tau; for each pair the model's rise S(t) (:func:`fopdt_response`) and a
    linear least-squares fit; the pair with the smallest RMS wins.

    With ``fit_loss`` False: the readings run from the pulse's start,
    ``fall0_c_per_s`` (0 or more) is continued as the loss, and only the
    gain is fitted.  On those episodes every fit was within 0.044 °C, and
    the loss is a large part of what a pulse delivered (continuing half the
    fall gave a gain of 0.030, none 0.021): a fixed loss biases the gain
    either way.

    With ``fit_loss`` True: room(t) = c - f (t - t0) + g S(t) is fitted for
    the loss f together with the gain g, over the readings from
    ``window_start_ts`` on, S built from the *whole* open-fraction series
    given -- so the heat of an earlier pulse still arriving in the window
    is in the model, not in the loss.
    """
    sampled = _resample(ts, room, open_fraction)
    if sampled is None:
        return None
    dt, ys, us = sampled
    if not fit_loss:
        fall = max(0.0, fall0_c_per_s) if _finite(fall0_c_per_s) else 0.0
        y = [ys[k] - ys[0] + fall * k * dt for k in range(len(ys))]
        best: Fit | None = None
        for theta in theta_grid_s:
            for tau in tau_grid_s:
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
    for theta in theta_grid_s:
        for tau in tau_grid_s:
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
# The pooled fit (2.17.0)
# ---------------------------------------------------------------------------

class BandedSmoother:
    """The baseline of the pooled fit for one chain of ``n`` equally spaced
    bins: A = I + lam D'D, D the second-difference operator, factorised once
    by a banded Cholesky (bandwidth 2, O(n)).

    A^-1 v is the Whittaker smooth of v: the curve b minimising
    |v - b|^2 + lam |D b|^2, i.e. the posterior mean of an integrated random
    walk (its slope wandering) under white noise, lam = noise variance /
    the walk's second-difference variance.  :meth:`residual` is what the
    smooth leaves, v - A^-1 v; constants and straight lines leave nothing
    (D annihilates them)."""

    def __init__(self, n: int, lam: float) -> None:
        if n < 1 or not _finite(lam) or lam < 0:
            raise ValueError(f"BandedSmoother({n}, {lam})")
        self.n = n
        self.lam = float(lam)
        d0 = [1.0] * n
        d1 = [0.0] * max(0, n - 1)
        d2 = [0.0] * max(0, n - 2)
        for i in range(n - 2):
            # D's row i is [1, -2, 1] at i, i + 1, i + 2.
            d0[i] += lam
            d0[i + 1] += 4.0 * lam
            d0[i + 2] += lam
            d1[i] -= 2.0 * lam
            d1[i + 1] -= 2.0 * lam
            d2[i] += lam
        l0 = [0.0] * n
        l1 = [0.0] * max(0, n - 1)
        l2 = [0.0] * max(0, n - 2)
        for i in range(n):
            s = d0[i]
            if i >= 1:
                s -= l1[i - 1] * l1[i - 1]
            if i >= 2:
                s -= l2[i - 2] * l2[i - 2]
            if s <= 0.0:
                raise ValueError("BandedSmoother: the matrix is not positive definite")
            l0[i] = math.sqrt(s)
            if i + 1 < n:
                s1 = d1[i]
                if i >= 1:
                    s1 -= l1[i - 1] * l2[i - 1]
                l1[i] = s1 / l0[i]
            if i + 2 < n:
                l2[i] = d2[i] / l0[i]
        self._l0, self._l1, self._l2 = l0, l1, l2
        inv = [1.0 / v for v in l0]
        # Padded bands for the two sweeps (0 where the band runs out).
        self._forward = list(zip([0.0] + l1, [0.0, 0.0] + l2, inv))
        self._backward = list(zip(reversed(l1 + [0.0]), reversed(l2 + [0.0, 0.0]), reversed(inv)))

    def solve(self, v: Sequence[float]) -> list[float]:
        """A^-1 v (the smooth of v)."""
        z: list[float] = []
        append = z.append
        zm2 = zm1 = 0.0
        for vi, (p1, p2, q) in zip(v, self._forward):
            zi = (vi - p1 * zm1 - p2 * zm2) * q
            append(zi)
            zm2, zm1 = zm1, zi
        x: list[float] = []
        append = x.append
        xp2 = xp1 = 0.0
        for zi, (p1, p2, q) in zip(reversed(z), self._backward):
            xi = (zi - p1 * xp1 - p2 * xp2) * q
            append(xi)
            xp2, xp1 = xp1, xi
        x.reverse()
        return x

    def residual(self, v: Sequence[float]) -> list[float]:
        """v - A^-1 v: what the smooth baseline leaves of v."""
        return [a - b for a, b in zip(v, self.solve(v))]

    def trace_inverse(self) -> float:
        """trace(A^-1), the baseline's effective degrees of freedom: exact,
        by the band recurrence of the inverse (Takahashi's equations),
        O(n)."""
        n, l0, l1, l2 = self.n, self._l0, self._l1, self._l2
        s0 = [0.0] * n                      # (A^-1)[i][i]
        s1 = [0.0] * max(0, n - 1)          # (A^-1)[i][i + 1]
        s2 = [0.0] * max(0, n - 2)          # (A^-1)[i][i + 2]
        for i in range(n - 1, -1, -1):
            a1 = l1[i] if i + 1 < n else 0.0
            a2 = l2[i] if i + 2 < n else 0.0
            if i + 2 < n:
                s2[i] = -(a1 * s1[i + 1] + a2 * s0[i + 2]) / l0[i]
            if i + 1 < n:
                t = a1 * s0[i + 1] + (a2 * s1[i + 1] if i + 2 < n else 0.0)
                s1[i] = -t / l0[i]
            t = (a1 * s1[i] if i + 1 < n else 0.0) + (a2 * s2[i] if i + 2 < n else 0.0)
            s0[i] = (1.0 / l0[i] - t) / l0[i]
        return sum(s0)

    @property
    def dof(self) -> float:
        """The baseline's effective degrees of freedom (trace(A^-1))."""
        return self.trace_inverse()


@dataclass(frozen=True)
class Chain:
    """Consecutive bins of the room's readings: bin k is the mean of the
    fresh readings in [start_ts + k step_s, start_ts + (k + 1) step_s), an
    empty bin between two readings filled linearly.  The model is taken at
    each bin's mean reading time, ``offset_s`` into it (the chain's mean:
    minute cycles from the chain's first reading put it 30 s into a 2-min
    bin, not at its centre; taken at the centre, a 300-s delay read as 330
    and the gain slid 30 % along the g-tau ridge)."""

    start_ts: float
    step_s: float
    rooms: tuple[float, ...]
    offset_s: float | None = None

    @property
    def end_ts(self) -> float:
        return self.start_ts + len(self.rooms) * self.step_s

    @property
    def reading_offset_s(self) -> float:
        """Where in its bin a bin's readings are on average (s)."""
        if self.offset_s is None or not 0.0 <= self.offset_s <= self.step_s:
            return 0.5 * self.step_s
        return self.offset_s

    def time(self, k: int) -> float:
        """Bin k's reading time (the model is taken there)."""
        return self.start_ts + k * self.step_s + self.reading_offset_s

    def sliced(self, lo_ts: float, hi_ts: float) -> Chain | None:
        """The bins that overlap [lo_ts, hi_ts), None if none."""
        n = len(self.rooms)
        k_lo = max(0, int(math.floor((lo_ts - self.start_ts) / self.step_s + 1e-9)))
        k_hi = min(n, int(math.ceil((hi_ts - self.start_ts) / self.step_s - 1e-9)))
        if k_hi <= k_lo:
            return None
        if k_lo == 0 and k_hi == n:
            return self
        return Chain(self.start_ts + k_lo * self.step_s, self.step_s, self.rooms[k_lo:k_hi],
                     self.offset_s)

    def to_dict(self) -> dict[str, Any]:
        return {"start_ts": round(self.start_ts, 1), "step_s": self.step_s,
                "offset_s": _round(self.offset_s, 1), "mk": _encode_mk(self.rooms)}

    @classmethod
    def from_dict(cls, d: Any) -> Chain | None:
        if not isinstance(d, dict):
            return None
        start, step, rooms = d.get("start_ts"), d.get("step_s"), _decode_mk(d.get("mk"))
        if not _finite(start, step) or step <= 0 or not rooms:
            return None
        offset = d.get("offset_s")
        return cls(float(start), float(step), tuple(float(y) for y in rooms),
                   float(offset) if _finite(offset) and 0.0 <= offset <= step else None)


@dataclass(frozen=True)
class PulseRecord:
    """A judged pulse: its opening (as booked), its landing, and whether it
    can be pooled (``usable``; why not, if not).  ``peak_error_c`` None: its
    landing was not judged (the target changed meanwhile), the pulse may
    still be pooled (its heat does not depend on the target)."""

    start_ts: float
    open_from: float | None
    open_until: float | None
    open_min: float
    landing_ts: float
    peak_error_c: float | None
    model_error_c: float | None       # what the gain in use promised at that peak
    gain_at_start: float
    end_reason: str | None
    rode_burn: bool
    shortfall_start_c: float | None
    at_floor: bool
    usable: bool
    why_not: str | None = None
    dropped_by_fit: str | None = None  # the last fit left its segment out (why)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_ts": round(self.start_ts, 1),
            "open_from": _round(self.open_from, 1), "open_until": _round(self.open_until, 1),
            "open_min": round(self.open_min, 2), "landing_ts": round(self.landing_ts, 1),
            "peak_error_c": _round(self.peak_error_c, 3),
            "model_error_c": _round(self.model_error_c, 3),
            "gain_at_start": round(self.gain_at_start, 5),
            "end_reason": self.end_reason, "rode_burn": self.rode_burn,
            "shortfall_start_c": _round(self.shortfall_start_c, 3),
            "at_floor": self.at_floor, "usable": self.usable, "why_not": self.why_not,
            "dropped_by_fit": self.dropped_by_fit,
        }

    @classmethod
    def from_dict(cls, d: Any) -> PulseRecord | None:
        if not isinstance(d, dict):
            return None
        try:
            rec = cls(
                start_ts=float(d["start_ts"]), open_from=_opt(d.get("open_from")),
                open_until=_opt(d.get("open_until")), open_min=float(d["open_min"]),
                landing_ts=float(d["landing_ts"]), peak_error_c=_opt(d.get("peak_error_c")),
                model_error_c=_opt(d.get("model_error_c")),
                gain_at_start=float(d.get("gain_at_start", 0.0)),
                end_reason=d["end_reason"] if isinstance(d.get("end_reason"), str) else None,
                rode_burn=d.get("rode_burn") is True,
                shortfall_start_c=_opt(d.get("shortfall_start_c")),
                at_floor=d.get("at_floor") is True, usable=d.get("usable") is True,
                why_not=d["why_not"] if isinstance(d.get("why_not"), str) else None,
                dropped_by_fit=(d["dropped_by_fit"] if isinstance(d.get("dropped_by_fit"), str)
                                else None),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not _finite(rec.start_ts, rec.open_min, rec.landing_ts, rec.gain_at_start):
            return None
        return rec


def _encode_mk(rooms: Sequence[float]) -> list[int]:
    """Readings to 0.001 °C as the first in mK and then the steps between
    them (a few characters each in the save, lossless to 0.001 °C)."""
    out: list[int] = []
    last = 0
    for y in rooms:
        mk = int(round(y * 1000.0))
        out.append(mk - last)
        last = mk
    return out


def _decode_mk(steps: Any) -> list[float] | None:
    if not isinstance(steps, list) or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in steps):
        return None
    out: list[float] = []
    mk = 0
    for v in steps:
        mk += v
        out.append(mk / 1000.0)
    return out


def _log_grid(spec: Sequence[float]) -> tuple[float, ...]:
    """(lo, hi, ratio) -> lo, lo ratio, lo ratio^2, ... up to hi."""
    lo, hi, ratio = (float(v) for v in spec)
    if not (lo > 0 and hi >= lo and ratio > 1.0):
        raise ValueError(f"a grid of {spec}")
    out = [lo]
    while out[-1] * ratio <= hi * (1.0 + 1e-9):
        out.append(out[-1] * ratio)
    return tuple(out)


@dataclass(frozen=True)
class FitConfig:
    """The numbers the pooled fit needs, plain and immutable (they cross
    the executor boundary with the data): built from the DoseConfig."""

    step_s: float
    wander_c_per_h_per_30min: float
    noise0_c: float
    noise_floor_c: float
    thetas_s: tuple[float, ...]
    taus_s: tuple[float, ...]
    dead_starts_s: tuple[float, ...]
    dead_start_prior: tuple[float, ...]
    default_gain: float
    prior_gain_sd_ln: float
    prior_gain_sd_ln_unseeded: float
    prior_theta_s: float
    prior_theta_sd_ln: float
    prior_tau_s: float
    prior_tau_sd_ln: float
    temper_floor: float
    sd_inflation: float
    segment_pre_s: float
    segment_post_s: float
    reject_rms_factor: float
    reject_rms_min_c: float
    reject_rise_c_per_h: float
    latency_s: float

    @classmethod
    def from_dose(cls, cfg: DoseConfig) -> FitConfig:
        c0s = tuple(float(c) for c in cfg.fit_dead_start_grid_s)
        prior = tuple(float(p) for p in cfg.fit_dead_start_prior)
        if len(prior) != len(c0s) or not all(p > 0 for p in prior):
            prior = tuple(1.0 for _ in c0s)
        if 0.0 not in c0s:
            # No dead start is the regulator's own model: always on the grid.
            c0s, prior = (0.0, *c0s), (max(prior, default=1.0), *prior)
        return cls(
            step_s=float(cfg.fit_step_s),
            wander_c_per_h_per_30min=float(cfg.fit_wander_c_per_h_per_30min),
            noise0_c=float(cfg.fit_noise0_c), noise_floor_c=float(cfg.fit_noise_floor_c),
            thetas_s=_log_grid(cfg.fit_theta_grid_s), taus_s=_log_grid(cfg.fit_tau_grid_s),
            dead_starts_s=c0s, dead_start_prior=prior,
            default_gain=float(cfg.default_gain_c_per_open_min),
            prior_gain_sd_ln=float(cfg.prior_gain_sd_ln),
            prior_gain_sd_ln_unseeded=float(cfg.prior_gain_sd_ln_unseeded),
            prior_theta_s=float(cfg.prior_theta_s), prior_theta_sd_ln=float(cfg.prior_theta_sd_ln),
            prior_tau_s=float(cfg.prior_tau_s), prior_tau_sd_ln=float(cfg.prior_tau_sd_ln),
            temper_floor=float(cfg.fit_temper_floor), sd_inflation=float(cfg.fit_sd_inflation),
            segment_pre_s=float(cfg.segment_pre_s), segment_post_s=float(cfg.segment_post_s),
            reject_rms_factor=float(cfg.segment_reject_rms_factor),
            reject_rms_min_c=float(cfg.segment_reject_rms_min_c),
            reject_rise_c_per_h=float(cfg.segment_reject_rise_c_per_h),
            latency_s=float(cfg.latency_s),
        )

    def stiffness(self, sigma_c: float, temper: float = 1.0) -> float:
        """lam = sigma^2 / (T q^2): q the sd of the baseline's second
        difference per bin, for a loss rate wandering W °C/h per 30 min (a
        random walk of the slope: per bin of h minutes it moves
        W / 60 sqrt(h / 30) °C/min, i.e. q = that times h); sigma^2 / T the
        residuals' noise power at the baseline's time scales (see
        _fit_once).  W 0.05, 2-min bins: q = 4.3e-4 °C."""
        h = self.step_s / 60.0
        q = self.wander_c_per_h_per_30min / 60.0 * math.sqrt(h / 30.0) * h
        return (sigma_c / q) ** 2 / max(temper, 1e-6)


@dataclass(frozen=True)
class FitInput:
    """An immutable snapshot of what the pooled fit needs (it crosses the
    executor boundary): the chains of binned readings, every booked opening
    whose heat can reach them (the unusable pulses' too: their heat is
    real; an opening still running ends at ``ts``), the usable pulses whose
    segments lie in the chains, the gain's anchor, the values in use (for
    the "was I wrong?" number), the learner's generation (a fit for an
    older one is discarded) and the fit's numbers."""

    ts: float
    generation: int
    chains: tuple[Chain, ...]
    intervals: tuple[tuple[float, float], ...]
    pulses: tuple[PulseRecord, ...]
    anchor_gain: float
    anchor_seeded: bool
    in_use: tuple[float, float, float]          # gain, theta_s, tau_s
    cfg: FitConfig


@dataclass(frozen=True)
class PooledFit:
    """The pooled fit's answer.  The triple is the posterior mode (MAP)
    given no dead start (the regulator's model); ``gain_sd_ln`` the
    marginal posterior sd of ln gain there, inflated (the uncertainty the
    gate and the dead band use); ``dead_start_s`` (the most probable c0)
    and its posterior over the whole grid are a diagnostic only.
    ``rejected_hypothesis_dl``: -2 ln of the likelihood ratio of the values
    in use before the fit against the MAP, in the fit's own tempered units
    (the owner's "was I wrong?": on 1 Nov 2026 the single-pulse 0.089 / 3 /
    25 min scored 8, its gain 2.9 inflated sd from the mode; the data
    agent's untempered 27-67 is the same evidence on a 2x overconfident
    scale).  ``ok`` False: no fit (``why``)."""

    ts: float
    generation: int
    ok: bool
    why: str | None = None
    gain: float = 0.0
    theta_s: float = 0.0
    tau_s: float = 0.0
    dead_start_s: float = 0.0
    gain_sd_ln: float = math.inf
    theta_sd_s: float = 0.0
    tau_sd_s: float = 0.0
    dead_start_posterior: tuple[tuple[float, float], ...] = ()
    sigma_c: float = 0.0
    rho: float = 0.0
    temper: float = 0.0
    lam: float = 0.0
    n: int = 0
    n_pulses: int = 0
    span_s: float = 0.0
    rms_c: float = 0.0
    dropped: tuple[tuple[float, str], ...] = ()
    pulse_starts: tuple[float, ...] = ()
    rejected_hypothesis_dl: float | None = None
    in_use: tuple[float, float, float] = (0.0, 0.0, 0.0)
    anchor_gain: float = 0.0
    loss_c_per_h: tuple[float, float] | None = None   # the baseline's cooling, lowest and highest

    @property
    def effective_samples(self) -> float:
        return self.n * self.temper

    @property
    def gain_uncertainty_pct(self) -> float | None:
        """The inflated sd of ln gain as a percentage (e^sd - 1)."""
        if not _finite(self.gain_sd_ln):
            return None
        return 100.0 * (math.exp(self.gain_sd_ln) - 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": round(self.ts, 1), "ok": self.ok, "why": self.why,
            "gain": round(self.gain, 5), "theta_s": round(self.theta_s, 1),
            "tau_s": round(self.tau_s, 1), "dead_start_s": round(self.dead_start_s, 1),
            "gain_sd_ln": round(self.gain_sd_ln, 4) if _finite(self.gain_sd_ln) else None,
            "theta_sd_s": round(self.theta_sd_s, 1), "tau_sd_s": round(self.tau_sd_s, 1),
            "dead_start_posterior": [[c, round(p, 3)] for c, p in self.dead_start_posterior],
            "sigma_c": round(self.sigma_c, 5), "rho": round(self.rho, 3),
            "temper": round(self.temper, 4), "lam": round(self.lam, 1), "n": self.n,
            "n_pulses": self.n_pulses, "span_s": round(self.span_s, 1),
            "rms_c": round(self.rms_c, 5),
            "dropped": [[round(t, 1), why] for t, why in self.dropped],
            "pulse_starts": [round(t, 1) for t in self.pulse_starts],
            "rejected_hypothesis_dl": _round(self.rejected_hypothesis_dl, 1),
            "in_use": [round(v, 5) for v in self.in_use],
            "anchor_gain": round(self.anchor_gain, 5),
            "loss_c_per_h": ([round(v, 3) for v in self.loss_c_per_h]
                             if self.loss_c_per_h is not None else None),
        }

    @classmethod
    def from_dict(cls, d: Any, generation: int = 0) -> PooledFit | None:
        """A saved fit (for the sensor and the diagnostics), None if
        malformed."""
        if not isinstance(d, dict):
            return None
        try:
            sd = d.get("gain_sd_ln")
            loss = d.get("loss_c_per_h")
            in_use = d.get("in_use") or [0.0, 0.0, 0.0]
            fit = cls(
                ts=float(d["ts"]), generation=generation, ok=d.get("ok") is True,
                why=d["why"] if isinstance(d.get("why"), str) else None,
                gain=float(d.get("gain", 0.0)), theta_s=float(d.get("theta_s", 0.0)),
                tau_s=float(d.get("tau_s", 0.0)), dead_start_s=float(d.get("dead_start_s", 0.0)),
                gain_sd_ln=float(sd) if _finite(sd) else math.inf,
                theta_sd_s=float(d.get("theta_sd_s", 0.0)), tau_sd_s=float(d.get("tau_sd_s", 0.0)),
                dead_start_posterior=tuple((float(c), float(p))
                                           for c, p in d.get("dead_start_posterior", [])),
                sigma_c=float(d.get("sigma_c", 0.0)), rho=float(d.get("rho", 0.0)),
                temper=float(d.get("temper", 0.0)), lam=float(d.get("lam", 0.0)),
                n=int(d.get("n", 0)), n_pulses=int(d.get("n_pulses", 0)),
                span_s=float(d.get("span_s", 0.0)), rms_c=float(d.get("rms_c", 0.0)),
                dropped=tuple((float(t), str(why)) for t, why in d.get("dropped", [])),
                pulse_starts=tuple(float(t) for t in d.get("pulse_starts", [])),
                rejected_hypothesis_dl=_opt(d.get("rejected_hypothesis_dl")),
                in_use=(float(in_use[0]), float(in_use[1]), float(in_use[2])),
                anchor_gain=float(d.get("anchor_gain", 0.0)),
                loss_c_per_h=((float(loss[0]), float(loss[1]))
                              if isinstance(loss, list) and len(loss) == 2 else None),
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        if not _finite(fit.ts, fit.gain, fit.theta_s, fit.tau_s, fit.sigma_c, fit.temper):
            return None
        return fit


@dataclass
class _ChainData:
    """A chain prepared for the fit: bin centres relative to the fit's
    reference time, the readings, and (per pass) its smoother and the
    readings' residual."""

    x0: float
    step_s: float
    ys: list[float]
    start_ts: float
    smoother: BandedSmoother | None = None
    yres: list[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.ys)


@dataclass
class _Pass:
    """One pass over the grid at one stiffness."""

    lam: float
    dof: float
    syy: float
    table: dict[tuple[int, int, int], tuple[float, float]]   # (theta, tau, c0) -> sxx, sxy


@dataclass
class _Result:
    """A fit on one set of chains (before any segment is dropped)."""

    chains: list[_ChainData]
    events_ref: float
    ps: _Pass
    sigma: float
    rho: float
    temper: float
    theta_s: float          # the mode, given c0 = 0, refined off the grid
    tau_s: float
    gain: float
    gain_sd_ln: float
    theta_sd: float
    tau_sd: float
    dead_start_s: float
    dead_start_posterior: tuple[tuple[float, float], ...]
    at_edge: bool


def _prepare_chains(chains: Sequence[Chain], ref_ts: float) -> list[_ChainData]:
    return [_ChainData(x0=c.time(0) - ref_ts, step_s=c.step_s,
                       ys=list(c.rooms), start_ts=c.start_ts) for c in chains]


def _grid_pass(chains: list[_ChainData], intervals: Sequence[tuple[float, float]],
               ref_ts: float, cfg: FitConfig, lam: float, c0_indexes: Sequence[int]) -> _Pass:
    """sxx = |R S|^2 and sxy = (R S).(R y) for every grid point, R the
    residual-maker of the chain's smoother, pooled over the chains (the
    Speckman partial-residual estimator of the partially linear model
    y = baseline + g S: the gain at a grid point is sxy / sxx and the
    residual sum of squares syy - 2 g sxy + g^2 sxx a quadratic in g)."""
    dof = 0.0
    syy = 0.0
    for cd in chains:
        cd.smoother = BandedSmoother(cd.n, lam)
        cd.yres = cd.smoother.residual(cd.ys)
        syy += _dot(cd.yres, cd.yres)
        dof += cd.smoother.trace_inverse()
    table: dict[tuple[int, int, int], tuple[float, float]] = {}
    for i, theta in enumerate(cfg.thetas_s):
        for c in c0_indexes:
            events = _ramp_events(intervals, theta, cfg.dead_starts_s[c], ref_ts)
            for j, tau in enumerate(cfg.taus_s):
                sxx = sxy = 0.0
                for cd in chains:
                    s = _ramps_on_grid(cd.x0, cd.step_s, cd.n, events, tau)
                    sr = cd.smoother.residual(s)
                    sxx += _dot(sr, sr)
                    sxy += _dot(sr, cd.yres)
                table[(i, j, c)] = (sxx, sxy)
    return _Pass(lam=lam, dof=dof, syy=syy, table=table)


def _ls_best(ps: _Pass, c0_index: int | None = None
             ) -> tuple[float, tuple[int, int, int], float] | None:
    """The plain least-squares optimum over the grid (or its slice at one
    dead start): (rss, key, gain)."""
    best = None
    for key, (sxx, sxy) in ps.table.items():
        if sxx <= 1e-15 or (c0_index is not None and key[2] != c0_index):
            continue
        rss = ps.syy - sxy * sxy / sxx
        if best is None or rss < best[0]:
            best = (rss, key, sxy / sxx)
    return best


def _sigma(rss: float, n: int, dof: float, cfg: FitConfig) -> float:
    return max(cfg.noise_floor_c, math.sqrt(max(rss, 0.0) / max(1.0, n - dof - 3.0)))


def _residuals_at(chains: list[_ChainData], intervals: Sequence[tuple[float, float]],
                  ref_ts: float, theta: float, tau: float, c0: float, gain: float
                  ) -> list[tuple[list[float], list[float], list[float]]]:
    """Per chain: the response S, the residual R(y - g S) and the baseline
    (y - g S minus that residual) at one triple."""
    events = _ramp_events(intervals, theta, c0, ref_ts)
    out = []
    for cd in chains:
        s = _ramps_on_grid(cd.x0, cd.step_s, cd.n, events, tau)
        sr = cd.smoother.residual(s)
        r = [a - gain * b for a, b in zip(cd.yres, sr)]
        base = [y - gain * si - ri for y, si, ri in zip(cd.ys, s, r)]
        out.append((s, r, base))
    return out


def _lag1(series: Iterable[list[float]]) -> float:
    """The lag-1 autocorrelation pooled over series (each mean-removed)."""
    num = den = 0.0
    for r in series:
        if len(r) < 3:
            continue
        m = sum(r) / len(r)
        c = [v - m for v in r]
        num += _dot(c[1:], c[:-1])
        den += _dot(c, c)
    return num / den if den > 0 else 0.0


def _point(chains: list[_ChainData], intervals: Sequence[tuple[float, float]], ref_ts: float,
           theta: float, tau: float, c0: float = 0.0) -> tuple[float, float]:
    """sxx and sxy at one point (on the chains' current smoothers)."""
    events = _ramp_events(intervals, theta, c0, ref_ts)
    sxx = sxy = 0.0
    for cd in chains:
        s = _ramps_on_grid(cd.x0, cd.step_s, cd.n, events, tau)
        sr = cd.smoother.residual(s)
        sxx += _dot(sr, sr)
        sxy += _dot(sr, cd.yres)
    return sxx, sxy


def _nelder_mead_max(f: Callable[[float, float], float], x0: tuple[float, float],
                     step: tuple[float, float], lo: tuple[float, float],
                     hi: tuple[float, float], max_evals: int = 80,
                     tol: float = 1e-3) -> tuple[float, float]:
    """The maximum of a smooth f(x, y) near x0, within a box (Nelder-Mead,
    points clamped to the box)."""
    def clamp(p: tuple[float, float]) -> tuple[float, float]:
        return (_clamp(p[0], lo[0], hi[0]), _clamp(p[1], lo[1], hi[1]))

    pts = [clamp(x0), clamp((x0[0] + step[0], x0[1])), clamp((x0[0], x0[1] + step[1]))]
    vals = [f(*p) for p in pts]
    evals = 3
    while evals < max_evals:
        order = sorted(range(3), key=lambda i: -vals[i])
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        span = max(abs(pts[i][j] - pts[0][j]) for i in (1, 2) for j in (0, 1))
        if span < tol:
            break
        cx, cy = (pts[0][0] + pts[1][0]) / 2.0, (pts[0][1] + pts[1][1]) / 2.0
        wx, wy = pts[2]
        xr = clamp((2 * cx - wx, 2 * cy - wy))
        fr = f(*xr)
        evals += 1
        if fr > vals[0]:
            xe = clamp((3 * cx - 2 * wx, 3 * cy - 2 * wy))
            fe = f(*xe)
            evals += 1
            pts[2], vals[2] = (xe, fe) if fe > fr else (xr, fr)
        elif fr > vals[1]:
            pts[2], vals[2] = xr, fr
        else:
            xc = clamp(((cx + wx) / 2.0, (cy + wy) / 2.0))
            fc = f(*xc)
            evals += 1
            if fc > vals[2]:
                pts[2], vals[2] = xc, fc
            else:
                for i in (1, 2):
                    pts[i] = clamp(((pts[0][0] + pts[i][0]) / 2.0, (pts[0][1] + pts[i][1]) / 2.0))
                    vals[i] = f(*pts[i])
                evals += 2
    best = max(range(3), key=lambda i: vals[i])
    return pts[best]


def _golden_max(f: Callable[[float], float], lo: float, hi: float, iters: int = 40) -> float:
    g = (math.sqrt(5.0) - 1.0) / 2.0
    a, b = lo, hi
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = f(c), f(d)
    for _ in range(iters):
        if fc >= fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = f(d)
    return 0.5 * (a + b)


def _fit_once(chains: list[_ChainData], intervals: Sequence[tuple[float, float]], ref_ts: float,
              anchor: float, seeded: bool, cfg: FitConfig) -> _Result | None:
    """Two passes over the grid, the posterior, its mode and marginals.

    The stiffness: pass 1 at fit_noise0_c; pass 2 at the first pass's
    residual sd and lag-1 autocorrelation, lam = sigma^2 / (T q^2), T =
    (1 - rho) / (1 + rho).  The likelihood is tempered by T (autocorrelated
    residuals), so the baseline's prior -- the loss wandering W per 30 min
    -- weighs against that tempered likelihood: at the baseline's time
    scales the residuals' noise power is sigma^2 / T per bin, not sigma^2.
    (lam = sigma^2 / q^2 would make W an effective W / sqrt(T), about 0.12
    on the night of 1 Nov 2026: leave-one-pulse-out 0.050-0.062 against
    0.049-0.053.)

    The triple and its uncertainty come from the posterior given c0 = 0,
    the model the regulator doses with (open-minutes as booked), with that
    model's own noise; the whole grid gives c0's posterior, a diagnostic.
    Taken over the whole grid the mode on 1 Nov sat at c0 = 90 s with a
    gain of 0.071-0.081 per *effective* open-minute: as a gain per booked
    open-minute it would under-dose 1.5x."""
    n = sum(cd.n for cd in chains)
    i0 = cfg.dead_starts_s.index(0.0)
    ps1 = _grid_pass(chains, intervals, ref_ts, cfg, cfg.stiffness(cfg.noise0_c), [i0])
    best1 = _ls_best(ps1)
    if best1 is None:
        return None
    sigma1 = _sigma(best1[0], n, ps1.dof, cfg)
    temper1 = _temper(chains, intervals, ref_ts, cfg, best1)
    all_c0 = list(range(len(cfg.dead_starts_s)))
    ps = _grid_pass(chains, intervals, ref_ts, cfg, cfg.stiffness(sigma1, temper1), all_c0)
    # The noise and its autocorrelation are the regulator's model's (c0 = 0):
    # the triple is then the same whatever the c0 grid (the simulators run
    # without it), and c0's evidence is judged on that scale.
    best = _ls_best(ps, i0)
    if best is None:
        return None
    sigma = _sigma(best[0], n, ps.dof, cfg)
    rho = _lag1_at(chains, intervals, ref_ts, cfg, best)
    temper = _clamp((1.0 - rho) / (1.0 + rho), cfg.temper_floor, 1.0)

    # The posterior on (theta, tau, c0, g): the tempered likelihood and the
    # log-normal priors (syy is common to every point and left out).
    lg0 = math.log(anchor)
    sg = cfg.prior_gain_sd_ln if seeded else cfg.prior_gain_sd_ln_unseeded
    steps = int(round(2 * _GAIN_GRID_HALF_LN / _GAIN_GRID_STEP_LN))
    xs = [-_GAIN_GRID_HALF_LN + k * _GAIN_GRID_STEP_LN for k in range(steps + 1)]
    gains = [anchor * math.exp(x) for x in xs]
    prior_g = [-0.5 * (x / sg) ** 2 for x in xs]
    prior_th = [-0.5 * (math.log(t / cfg.prior_theta_s) / cfg.prior_theta_sd_ln) ** 2
                for t in cfg.thetas_s]
    prior_ta = [-0.5 * (math.log(t / cfg.prior_tau_s) / cfg.prior_tau_sd_ln) ** 2
                for t in cfg.taus_s]
    w_max = max(cfg.dead_start_prior)
    prior_c0 = [math.log(w / w_max) for w in cfg.dead_start_prior]
    k = -0.5 * temper / (sigma * sigma)
    rows: dict[tuple[int, int, int], list[float]] = {}
    top = top0 = -math.inf
    top0_at: tuple[tuple[int, int, int], int] | None = None
    for key, (sxx, sxy) in ps.table.items():
        a2, a1 = k * sxx, -2.0 * k * sxy
        base = prior_th[key[0]] + prior_ta[key[1]] + prior_c0[key[2]]
        row = [(a2 * g + a1) * g + pg + base for g, pg in zip(gains, prior_g)]
        rows[key] = row
        m = max(row)
        top = max(top, m)
        if key[2] == i0 and m > top0:
            top0 = m
            top0_at = (key, row.index(m))
    if top0_at is None or not math.isfinite(top):
        return None
    exp = math.exp
    # c0's posterior, over the whole grid.
    w_c0 = [0.0] * len(cfg.dead_starts_s)
    for key, row in rows.items():
        w_c0[key[2]] += sum(exp(v - top) for v in row)
    c0_total = sum(w_c0)
    # Everything else given c0 = 0.
    w_g = [0.0] * len(gains)
    w_th = [0.0] * len(cfg.thetas_s)
    w_ta = [0.0] * len(cfg.taus_s)
    total = 0.0
    for key, row in rows.items():
        if key[2] != i0:
            continue
        ws = [exp(v - top0) for v in row]
        s = sum(ws)
        total += s
        w_th[key[0]] += s
        w_ta[key[1]] += s
        w_g = [a + b for a, b in zip(w_g, ws)]
    if total <= 0.0 or c0_total <= 0.0:
        return None
    lgs = [lg0 + x for x in xs]
    m_lg = sum(w * lg for w, lg in zip(w_g, lgs)) / total
    v_lg = sum(w * (lg - m_lg) ** 2 for w, lg in zip(w_g, lgs)) / total
    m_th = sum(w * t for w, t in zip(w_th, cfg.thetas_s)) / total
    v_th = sum(w * (t - m_th) ** 2 for w, t in zip(w_th, cfg.thetas_s)) / total
    m_ta = sum(w * t for w, t in zip(w_ta, cfg.taus_s)) / total
    v_ta = sum(w * (t - m_ta) ** 2 for w, t in zip(w_ta, cfg.taus_s)) / total

    # The mode, refined off the grid from its best point: the grid's steps
    # are 25 %, and with precise readings a step biases the gain along the
    # g-tau ridge (a 300 / 480-s room on a minute cycle read 0.060 at the
    # 286 / 686-s point against 0.045).  The gain at each (theta, tau) is
    # the posterior's best given them (its prior included); it stays within
    # the gain grid.
    key, _ = top0_at
    u_min, u_max = lgs[0], lgs[-1]

    def best_u(sxx: float, sxy: float) -> tuple[float, float]:
        def lp(u: float) -> float:
            g = math.exp(u)
            return k * (g * g * sxx - 2.0 * g * sxy) - 0.5 * ((u - lg0) / sg) ** 2

        # The maximum lies between the likelihood's (sxy / sxx) and the
        # prior's (lg0): both rise left of it and fall right of it.
        u_ls = math.log(sxy / sxx) if sxx > 0 and sxy > 0 else u_min
        lo_u = _clamp(min(u_ls, lg0) - 0.05, u_min, u_max)
        hi_u = _clamp(max(u_ls, lg0) + 0.05, u_min, u_max)
        u = _golden_max(lp, lo_u, hi_u)
        return u, lp(u)

    def objective(x: float, y: float) -> float:
        sxx, sxy = _point(chains, intervals, ref_ts, math.exp(x), math.exp(y))
        _, v = best_u(sxx, sxy)
        return (v - 0.5 * ((x - math.log(cfg.prior_theta_s)) / cfg.prior_theta_sd_ln) ** 2
                - 0.5 * ((y - math.log(cfg.prior_tau_s)) / cfg.prior_tau_sd_ln) ** 2)

    th_lo, th_hi = math.log(cfg.thetas_s[0]), math.log(cfg.thetas_s[-1])
    ta_lo, ta_hi = math.log(cfg.taus_s[0]), math.log(cfg.taus_s[-1])
    th_step = (th_hi - th_lo) / max(1, len(cfg.thetas_s) - 1)
    ta_step = (ta_hi - ta_lo) / max(1, len(cfg.taus_s) - 1)
    x, y = _nelder_mead_max(
        objective, (math.log(cfg.thetas_s[key[0]]), math.log(cfg.taus_s[key[1]])),
        (0.5 * th_step, 0.5 * ta_step), (th_lo, ta_lo), (th_hi, ta_hi))
    if objective(x, y) < objective(math.log(cfg.thetas_s[key[0]]),
                                   math.log(cfg.taus_s[key[1]])):
        x, y = math.log(cfg.thetas_s[key[0]]), math.log(cfg.taus_s[key[1]])
    u, _ = best_u(*_point(chains, intervals, ref_ts, math.exp(x), math.exp(y)))
    gain = math.exp(u)
    c0_best = max(range(len(w_c0)), key=lambda c: w_c0[c])
    infl = cfg.sd_inflation
    return _Result(
        chains=chains, events_ref=ref_ts, ps=ps, sigma=sigma, rho=rho, temper=temper,
        theta_s=math.exp(x), tau_s=math.exp(y), gain=gain,
        gain_sd_ln=math.sqrt(max(v_lg, 0.0)) * infl,
        theta_sd=math.sqrt(max(v_th, 0.0)) * infl, tau_sd=math.sqrt(max(v_ta, 0.0)) * infl,
        dead_start_s=cfg.dead_starts_s[c0_best],
        dead_start_posterior=tuple((c0, w / c0_total) for c0, w in zip(cfg.dead_starts_s, w_c0)),
        at_edge=u <= u_min + 1e-6 or u >= u_max - 1e-6,
    )


def _lag1_at(chains: list[_ChainData], intervals: Sequence[tuple[float, float]], ref_ts: float,
             cfg: FitConfig, best: tuple[float, tuple[int, int, int], float]) -> float:
    """The lag-1 autocorrelation of the least-squares residuals."""
    _, (i, j, c), g = best
    resid = _residuals_at(chains, intervals, ref_ts, cfg.thetas_s[i], cfg.taus_s[j],
                          cfg.dead_starts_s[c], g)
    return _lag1(r for _, r, _ in resid)


def _temper(chains: list[_ChainData], intervals: Sequence[tuple[float, float]], ref_ts: float,
            cfg: FitConfig, best: tuple[float, tuple[int, int, int], float]) -> float:
    rho = _lag1_at(chains, intervals, ref_ts, cfg, best)
    return _clamp((1.0 - rho) / (1.0 + rho), cfg.temper_floor, 1.0)


def _segments(pulses: Sequence[PulseRecord], intervals: Sequence[tuple[float, float]],
              cfg: FitConfig) -> list[tuple[PulseRecord, float, float]]:
    """Each pulse's segment: from segment_pre_s before it to the next pulse
    (its command: a booked opening's start less the latency) or its landing
    + segment_post_s, whichever first.  Ending at the next command keeps a
    dropped segment from taking the next pulse's own start with it."""
    out = []
    for rec in pulses:
        own = rec.open_from if rec.open_from is not None else rec.start_ts + cfg.latency_s
        nxt = min((a - cfg.latency_s for a, _ in intervals if a > own + 1.0), default=math.inf)
        hi = max(rec.start_ts, min(nxt, rec.landing_ts + cfg.segment_post_s))
        out.append((rec, rec.start_ts - cfg.segment_pre_s, hi))
    return out


def _reject(res: _Result, pulses: Sequence[PulseRecord], intervals: Sequence[tuple[float, float]],
            cfg: FitConfig) -> list[tuple[PulseRecord, float, float, str]]:
    """The one-pass robust check: each segment's residual RMS at the mode,
    and the fitted baseline's rise over it.  Returns the segments to drop."""
    resid = _residuals_at(res.chains, intervals, res.events_ref, res.theta_s, res.tau_s, 0.0,
                          res.gain)
    judged = []
    for rec, lo, hi in _segments(pulses, intervals, cfg):
        rs: list[float] = []
        pts: list[tuple[float, float]] = []
        for cd, (_, r, base) in zip(res.chains, resid):
            for kk in range(cd.n):
                t = cd.x0 + kk * cd.step_s + res.events_ref
                if lo <= t < hi:
                    rs.append(r[kk])
                    pts.append((t, base[kk]))
        if len(rs) < 5:
            continue
        rms = math.sqrt(_dot(rs, rs) / len(rs))
        tm = sum(t for t, _ in pts) / len(pts)
        bm = sum(b for _, b in pts) / len(pts)
        stt = sum((t - tm) ** 2 for t, _ in pts)
        slope_h = (sum((t - tm) * (b - bm) for t, b in pts) / stt * 3600.0) if stt > 0 else 0.0
        judged.append((rec, lo, hi, rms, slope_h))
    med = _median([rms for *_, rms, _ in judged])
    if med is None:
        return []
    limit = max(cfg.reject_rms_factor * med, cfg.reject_rms_min_c)
    out = []
    for rec, lo, hi, rms, slope_h in judged:
        if rms > limit:
            out.append((rec, lo, hi, f"fits {rms / med:.1f}x worse" if med > 0
                        else f"fits worse ({rms * 1000:.0f} mK)"))
        elif slope_h > cfg.reject_rise_c_per_h:
            out.append((rec, lo, hi, f"the room warmed by itself ({slope_h:+.2f} °C/h)"))
    return out


def _without(chains: Sequence[Chain], excluded: Sequence[tuple[float, float]]) -> list[Chain]:
    """The chains without the bins whose centre falls in an excluded range:
    each chain splits into its unbroken pieces (no baseline across a gap)."""
    out: list[Chain] = []
    for ch in chains:
        run: list[float] = []
        run_start: float | None = None
        for k, y in enumerate(ch.rooms):
            t = ch.time(k)
            if any(lo <= t < hi for lo, hi in excluded):
                if run_start is not None and len(run) >= _MIN_CHAIN_BINS:
                    out.append(Chain(run_start, ch.step_s, tuple(run), ch.offset_s))
                run, run_start = [], None
                continue
            if run_start is None:
                run_start = ch.start_ts + k * ch.step_s
            run.append(y)
        if run_start is not None and len(run) >= _MIN_CHAIN_BINS:
            out.append(Chain(run_start, ch.step_s, tuple(run), ch.offset_s))
    return out


def _well_formed(fit: PooledFit) -> bool:
    """Every number apply_fit acts on or prints is there and finite."""
    try:
        return (
            _finite(fit.gain, fit.theta_s, fit.tau_s, fit.gain_sd_ln, fit.span_s, fit.ts)
            and fit.gain > 0 and fit.theta_s > 0 and fit.tau_s > 0 and fit.gain_sd_ln >= 0
            and isinstance(fit.n_pulses, int) and not isinstance(fit.n_pulses, bool)
            and all(_finite(t) and isinstance(why, str) for t, why in fit.dropped)
            and all(_finite(c, p) for c, p in fit.dead_start_posterior)
        )
    except (TypeError, ValueError):
        return False


def _no_fit(inp: FitInput, why: str, anchor: float) -> PooledFit:
    return PooledFit(ts=inp.ts, generation=inp.generation, ok=False, why=why,
                     in_use=inp.in_use, anchor_gain=anchor)


def _rss_at(res: _Result, intervals: Sequence[tuple[float, float]], theta: float, tau: float,
            c0: float, gain: float) -> float:
    """The residual sum of squares of one triple (on the fit's smoothers)."""
    events = _ramp_events(intervals, theta, c0, res.events_ref)
    sxx = sxy = 0.0
    for cd in res.chains:
        s = _ramps_on_grid(cd.x0, cd.step_s, cd.n, events, tau)
        sr = cd.smoother.residual(s)
        sxx += _dot(sr, sr)
        sxy += _dot(sr, cd.yres)
    return res.ps.syy - 2.0 * gain * sxy + gain * gain * sxx


def pooled_fit(inp: FitInput) -> PooledFit:
    """The pooled fit of the room's pulses (pure: no I/O, no logging,
    deterministic; it runs in an executor).

    Model: per chain, y_k = beta_k + g S(t_k; theta, tau, c0) + noise, beta
    the baseline (one value per bin) under the second-difference penalty
    lam |D beta|^2, S built from every booked opening.  For each grid point
    the baseline is projected out of y and S by the smoother's residual-
    maker R (one banded solve each): g = (RS).(Ry) / |RS|^2 and the residual
    sum of squares is a quadratic in g, so the prior on g costs nothing.
    Two passes: the stiffness lam = sigma^2 / q^2 from fit_noise0_c, then
    from the first pass's residual sd (sigma = sqrt(RSS / (n - trace(A^-1)
    - 3))).  The likelihood is tempered by (1 - rho) / (1 + rho), rho the
    lag-1 autocorrelation of the least-squares residuals; the priors are
    log-normal (the gain on the anchor, theta and tau on 5 and 8 min);
    c0 has relative prior weights.  The mode is the answer (the posterior
    mean of tau sits below the mode on the g-tau ridge and dragged the gain
    5-8 % low in closed loop); the marginal sd of ln g, inflated, its
    uncertainty.  Then each usable pulse's segment is checked once (RMS at
    the mode, the baseline's rise) and the fit is run again without the
    segments that fail, their heat still modelled."""
    cfg = inp.cfg
    anchor = (float(inp.anchor_gain) if _finite(inp.anchor_gain) and inp.anchor_gain > 0
              else cfg.default_gain)
    chains = [c for c in inp.chains
              if len(c.rooms) >= _MIN_CHAIN_BINS and _finite(c.start_ts, c.step_s, *c.rooms)]
    if not chains:
        return _no_fit(inp, "no readings to pool", anchor)
    intervals = sorted((float(a), float(b)) for a, b in inp.intervals if _finite(a, b) and b > a)
    pulses = sorted((p for p in inp.pulses
                     if any(c.start_ts <= p.start_ts < c.end_ts for c in chains)),
                    key=lambda p: p.start_ts)
    if not pulses:
        return _no_fit(inp, "no pulse in the readings", anchor)
    ref = min(c.start_ts for c in chains)
    res = _fit_once(_prepare_chains(chains, ref), intervals, ref, anchor, inp.anchor_seeded, cfg)
    if res is None:
        return _no_fit(inp, "no heat in the readings", anchor)
    dropped: list[tuple[float, str]] = []
    bad = _reject(res, pulses, intervals, cfg)
    if bad:
        kept_chains = _without(chains, [(lo, hi) for _, lo, hi, _ in bad])
        bad_starts = {rec.start_ts for rec, *_ in bad}
        kept_pulses = [p for p in pulses if p.start_ts not in bad_starts
                       and any(c.start_ts <= p.start_ts < c.end_ts for c in kept_chains)]
        again = None
        if kept_chains and kept_pulses:
            ref2 = min(c.start_ts for c in kept_chains)
            again = _fit_once(_prepare_chains(kept_chains, ref2), intervals, ref2, anchor,
                              inp.anchor_seeded, cfg)
        if again is not None:
            res, chains, pulses = again, kept_chains, kept_pulses
            dropped = [(rec.start_ts, why) for rec, _, _, why in bad]
    theta, tau, c0 = res.theta_s, res.tau_s, 0.0
    rss_map = _rss_at(res, intervals, theta, tau, c0, res.gain)
    n = sum(cd.n for cd in res.chains)
    dl = None
    g_use, th_use, ta_use = inp.in_use
    if _finite(g_use, th_use, ta_use) and g_use > 0 and th_use >= 0 and ta_use > 0:
        rss_use = _rss_at(res, intervals, th_use, ta_use, 0.0, g_use)
        dl = res.temper * (rss_use - rss_map) / (res.sigma * res.sigma)
    resid = _residuals_at(res.chains, intervals, res.events_ref, theta, tau, c0, res.gain)
    cooling = [-(b2 - b1) / cd.step_s * 3600.0
               for cd, (_, _, base) in zip(res.chains, resid) for b1, b2 in zip(base, base[1:])]
    why = "at the edge of the gain grid" if res.at_edge else None
    return PooledFit(
        ts=inp.ts, generation=inp.generation, ok=not res.at_edge, why=why,
        gain=res.gain, theta_s=theta, tau_s=tau, dead_start_s=res.dead_start_s,
        gain_sd_ln=res.gain_sd_ln, theta_sd_s=res.theta_sd, tau_sd_s=res.tau_sd,
        dead_start_posterior=res.dead_start_posterior,
        sigma_c=res.sigma, rho=res.rho, temper=res.temper, lam=res.ps.lam, n=n,
        n_pulses=len(pulses), span_s=sum(len(ch.rooms) * ch.step_s for ch in chains),
        rms_c=math.sqrt(max(rss_map, 0.0) / max(1, n)), dropped=tuple(dropped),
        pulse_starts=tuple(p.start_ts for p in pulses), rejected_hypothesis_dl=dl,
        in_use=inp.in_use, anchor_gain=anchor,
        loss_c_per_h=(min(cooling), max(cooling)) if cooling else None,
    )


# ---------------------------------------------------------------------------
# The readings pooled: chains, openings, marks
# ---------------------------------------------------------------------------

@dataclass
class _OpenChain:
    """The chain being filled: the bins closed so far and the one open
    (its readings' sum, count and the sum of their times into the bin),
    and the readings' mean time into their bins so far."""

    start_ts: float
    rooms: list[float]
    bin_k: int
    bin_sum: float
    bin_n: int
    last_ts: float
    bin_tsum: float = 0.0
    off_sum: float = 0.0
    off_n: int = 0

    def offset_s(self) -> float | None:
        return self.off_sum / self.off_n if self.off_n else None

    def close_bin(self, step_s: float) -> float:
        """The open bin's mean (and its readings' time into the bin)."""
        self.off_sum += self.bin_tsum / self.bin_n
        self.off_n += 1
        return self.bin_sum / self.bin_n


def _track(intervals: list[list[Any]], ts: float, frm: float | None, until: float | None,
           extra: Any = None) -> None:
    """Follow an interval the doser keeps: the same start updates its end,
    a new start ends any still open, none ends them now."""
    if _finite(frm):
        end = float(until) if _finite(until) else None
        for iv in intervals:
            if abs(iv[0] - frm) < 1.0:
                iv[1] = end
                return
        for iv in intervals:
            if iv[1] is None:
                iv[1] = min(ts, float(frm))
        intervals.append([float(frm), end] if extra is None else [float(frm), end, extra])
    else:
        for iv in intervals:
            if iv[1] is None:
                iv[1] = ts


class ChainStore:
    """The data the pooled fit learns from (design §4):

    * **chains**: the room's fresh readings in ``fit_step_s`` bins, unbroken.
      A chain breaks on a reading gap over ``chain_max_gap_s``, a bridged
      reading, dosing paused, Roomstat off / summer / the room sensor lost
      (:meth:`interrupt`), a clock going back; on a window (no new chain
      until ``chain_settle_after_window_s`` after it), Boost, or a mark of
      heat booked nowhere (``chain_settle_after_unbooked_s`` after its end:
      the heat of a hold, a heat-up, the app, a pulse resumed without its
      booking is unknown, so no fit may see it);
    * **openings**: every booked opening (as the doser books them), for S;
    * **marks**: heat booked nowhere (the doser's hold marks and
      :meth:`mark_unusable`), which also voids a landing (the learner's
      look-back).

    What is kept is pruned by the learner (:meth:`prune`): a chain only
    while it holds a usable pulse kept for the window (trimmed to its
    segments), the chain being filled from its first usable pulse's or
    open episode's segment (or the last ``segment_pre_s``)."""

    def __init__(self, cfg: DoseConfig) -> None:
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        self._chains: list[Chain] = []
        self._open: _OpenChain | None = None
        self._intervals: list[list[Any]] = []
        # Heat booked nowhere: the doser's hold marks [from, until | None, why]
        # (followed as it keeps them) and the explicit ones (mark_unusable).
        self._holds: list[list[Any]] = []
        self._unusable: list[list[Any]] = []
        self._blocked_until: float | None = None
        self._last_break: tuple[float, str] | None = None
        self._last_ts: float | None = None

    # -- views ----------------------------------------------------------------
    @property
    def chains(self) -> tuple[Chain, ...]:
        """Every chain, the one being filled last (its closed bins)."""
        out = list(self._chains)
        o = self._open
        if o is not None and o.rooms:
            out.append(Chain(o.start_ts, self.cfg.fit_step_s, tuple(o.rooms), o.offset_s()))
        return tuple(out)

    @property
    def intervals(self) -> list[tuple[float, float | None]]:
        return [(iv[0], iv[1]) for iv in self._intervals]

    @property
    def marks(self) -> list[tuple[float, float | None, str]]:
        """Every mark of heat booked nowhere, oldest first."""
        return sorted((m[0], m[1], m[2]) for m in self._holds + self._unusable)

    @property
    def blocked_until(self) -> float | None:
        return self._blocked_until

    @property
    def last_break(self) -> tuple[float, str] | None:
        return self._last_break

    @property
    def filling_since(self) -> float | None:
        """The start of the chain being filled (None: none)."""
        return self._open.start_ts if self._open is not None else None

    def covers(self, lo_ts: float, hi_ts: float) -> bool:
        """One unbroken chain (the one being filled) holds [lo_ts, hi_ts]."""
        o = self._open
        return (o is not None and o.start_ts <= lo_ts + 1e-6
                and o.last_ts >= hi_ts - self.cfg.chain_max_gap_s)

    def why_not_covered(self, lo_ts: float) -> str:
        brk = self._last_break
        settle = max(self.cfg.chain_settle_after_unbooked_s, self.cfg.chain_settle_after_window_s)
        if brk is not None and brk[0] >= lo_ts - settle:
            return brk[1]
        return "too few readings before it"

    def unbooked_between(self, lo_ts: float, hi_ts: float) -> bool:
        """Heat booked nowhere went to the valve between lo_ts and hi_ts."""
        return any(frm <= hi_ts and (until is None or until > lo_ts)
                   for frm, until, _ in self.marks)

    # -- feeding ----------------------------------------------------------------
    def note(self, ts: float, room_c: float | None, *, open_from: float | None,
             open_until: float | None, hold_from: float | None, hold_until: float | None,
             window: bool = False, boost: bool = False, paused: bool = False) -> None:
        """One regulated cycle: the booking and the hold mark as the doser
        keeps them, what pauses the chain, and the fresh reading (None:
        bridged or none)."""
        if not _finite(ts):
            return
        cfg = self.cfg
        if self._last_ts is not None and ts < self._last_ts - 1.0:
            self._block(ts, self._last_ts, "the clock went back")
        _track(self._intervals, ts, open_from, open_until)
        _track(self._holds, ts, hold_from, hold_until, _UNBOOKED_WHY)
        blocked = False
        if window:
            blocked |= self._block(ts, ts + cfg.chain_settle_after_window_s, "a window")
        if boost:
            blocked |= self._block(ts, ts + cfg.chain_settle_after_unbooked_s, "Boost")
        if paused and not (window or boost):
            blocked |= self._block(ts, ts, "dosing was paused")
        # A mark's block is worked out afresh every cycle (never latched: a
        # mark's end moves, a resumed pulse's from the longest a pulse may
        # run to its booking's close).
        settle = cfg.chain_settle_after_unbooked_s
        for frm, until, why in self.marks:
            end = until if until is not None else ts
            if frm <= ts and end + settle >= ts:
                self._break(ts, why, at_ts=frm)
                blocked = True
        if not _finite(room_c):
            blocked |= self._block(ts, ts, "a bridged reading")
        if self._last_ts is None or ts > self._last_ts:
            self._last_ts = ts
        if blocked or (self._blocked_until is not None and ts < self._blocked_until):
            return
        self._add(ts, float(room_c))

    def interrupt(self, ts: float, why: str) -> None:
        """Roomstat off, summer, the room sensor lost, the watchdog: the
        chain ends here."""
        if _finite(ts):
            self._block(ts, ts, why)

    def mark_unusable(self, from_ts: float, until_ts: float | None, why: str) -> None:
        """Heat of unknown size from ``from_ts`` (to ``until_ts``; None: still
        going): no chain sees it, nor its settle after.  The same start
        updates the mark's end."""
        if not _finite(from_ts):
            return
        end = float(until_ts) if _finite(until_ts) else None
        for m in self._unusable:
            if abs(m[0] - from_ts) < 1.0:
                m[1] = end
                return
        self._unusable.append([float(from_ts), end, why])

    def _block(self, ts: float, until: float, why: str) -> bool:
        """End the chain being filled and start none before ``until``."""
        self._break(ts, why)
        if self._blocked_until is None or until > self._blocked_until:
            self._blocked_until = until
        return True

    def _break(self, ts: float, why: str, at_ts: float | None = None) -> None:
        """End the chain being filled (at ``at_ts``, default ``ts``)."""
        if self._open is not None:
            self._close(min(ts, at_ts) if at_ts is not None else ts)
        self._last_break = (ts, why)

    def _close(self, at_ts: float) -> None:
        """Close the chain being filled at ``at_ts``: the bins that ended by
        then are kept (a bin still open is dropped)."""
        o = self._open
        self._open = None
        if o is None:
            return
        step = self.cfg.fit_step_s
        rooms = list(o.rooms)
        if o.bin_n > 0 and o.start_ts + (o.bin_k + 1) * step <= at_ts + 1e-6:
            rooms.append(o.close_bin(step))
        keep = int(math.floor((at_ts - o.start_ts) / step + 1e-9))
        rooms = rooms[:max(0, keep)]
        if rooms:
            self._chains.append(Chain(o.start_ts, step, tuple(rooms), o.offset_s()))

    def _add(self, ts: float, room_c: float) -> None:
        cfg = self.cfg
        step = cfg.fit_step_s
        o = self._open
        if o is not None and ts - o.last_ts > cfg.chain_max_gap_s:
            gap = ts - o.last_ts
            self._close(o.last_ts)
            self._last_break = (ts, f"no reading for {gap / 60.0:.0f} min")
            o = None
        if o is None:
            self._open = _OpenChain(start_ts=ts, rooms=[], bin_k=0, bin_sum=room_c, bin_n=1,
                                    last_ts=ts)
            return
        if ts <= o.last_ts:
            return
        k = int((ts - o.start_ts) // step)
        into = ts - o.start_ts - k * step
        if k == o.bin_k:
            o.bin_sum += room_c
            o.bin_n += 1
            o.bin_tsum += into
        elif k > o.bin_k:
            mean = o.close_bin(step)
            o.rooms.append(mean)
            empty = k - o.bin_k - 1
            for q in range(1, empty + 1):
                o.rooms.append(mean + (room_c - mean) * q / (empty + 1))
            o.bin_k, o.bin_sum, o.bin_n, o.bin_tsum = k, room_c, 1, into
        o.last_ts = ts

    # -- the snapshot and pruning -------------------------------------------------
    def snapshot(self, pulses: Sequence[PulseRecord], now: float
                 ) -> tuple[tuple[Chain, ...], tuple[tuple[float, float], ...]]:
        """The chains holding the pulses' segments, each trimmed to [first
        start - segment_pre_s, last landing + segment_post_s], and every
        booked opening (one still running ends now)."""
        pre, post = self.cfg.segment_pre_s, self.cfg.segment_post_s
        out: list[Chain] = []
        for ch in self.chains:
            inside = [p for p in pulses if ch.start_ts <= p.start_ts - pre + 1e-6
                      and p.start_ts < ch.end_ts]
            if not inside:
                continue
            sl = ch.sliced(min(p.start_ts for p in inside) - pre,
                           max(p.landing_ts for p in inside) + post)
            if sl is not None and len(sl.rooms) >= _MIN_CHAIN_BINS:
                out.append(sl)
        intervals = tuple((float(a), float(b) if b is not None else max(float(a), now))
                          for a, b in self.intervals)
        return tuple(out), intervals

    def prune(self, now: float, pulses: Sequence[PulseRecord], open_starts: Sequence[float]
              ) -> None:
        """Keep what the window needs: closed chains holding a kept usable
        pulse (trimmed to their segments), the chain being filled from its
        first such pulse or open episode, or the last segment_pre_s;
        openings while their heat may reach a kept chain; marks for the
        settle and the landing look-back."""
        cfg = self.cfg
        pre, post, step = cfg.segment_pre_s, cfg.segment_post_s, cfg.fit_step_s
        kept: list[Chain] = []
        for ch in self._chains:
            inside = [p for p in pulses if ch.start_ts <= p.start_ts - pre + 1e-6
                      and p.start_ts < ch.end_ts]
            if not inside:
                continue
            sl = ch.sliced(min(p.start_ts for p in inside) - pre,
                           max(p.landing_ts for p in inside) + post)
            if sl is not None:
                kept.append(sl)
        self._chains = kept
        o = self._open
        if o is not None:
            starts = [p.start_ts for p in pulses if o.start_ts <= p.start_ts - pre + 1e-6]
            starts += [s for s in open_starts if s >= o.start_ts]
            lo = min(starts + [now]) - pre - step
            k = int(math.floor((lo - o.start_ts) / step))
            k = min(k, len(o.rooms))
            if k > 0:
                o.rooms = o.rooms[k:]
                o.start_ts += k * step
                o.bin_k -= k
        firsts = [ch.start_ts for ch in self._chains]
        if o is not None:
            firsts.append(o.start_ts)
        earliest = min(firsts) if firsts else now
        tail = (max(cfg.fit_theta_grid_s[1], cfg.theta_bounds_s[1])
                + max(cfg.fit_dead_start_grid_s, default=0.0)
                + _TAIL_KEEP_TAU * max(cfg.fit_tau_grid_s[1], cfg.tau_bounds_s[1]))
        self._intervals = [iv for iv in self._intervals
                           if iv[1] is None or iv[1] >= earliest - tail]
        self._holds = [m for m in self._holds if m[1] is None or m[1] >= now - _MARK_KEEP_S]
        self._unusable = [m for m in self._unusable
                          if m[1] is None or m[1] >= now - _MARK_KEEP_S]

    # -- persistence ----------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        o = self._open
        return {
            "chains": [ch.to_dict() for ch in self._chains],
            "filling": ({"start_ts": round(o.start_ts, 1),
                         "mk": _encode_mk(o.rooms),
                         "bin": [o.bin_k, round(o.bin_sum, 4), o.bin_n, round(o.bin_tsum, 1)],
                         "offset": [round(o.off_sum, 1), o.off_n],
                         "last_ts": round(o.last_ts, 1)} if o is not None else None),
            "intervals": [[round(a, 1), _round(b, 1)] for a, b in self.intervals],
            "holds": [[round(a, 1), _round(b, 1)] for a, b, _ in self._holds],
            "unusable": [[round(a, 1), _round(b, 1), why] for a, b, why in self._unusable],
            "blocked_until": _round(self._blocked_until, 1),
            "last_break": ([round(self._last_break[0], 1), self._last_break[1]]
                           if self._last_break is not None else None),
            "last_ts": _round(self._last_ts, 1),
        }

    def restore(self, data: Any, now: float) -> None:
        """Take a saved state back under the rules of design §5: a chain
        older than the window or running into the future is dropped, the
        chain being filled continues only while it joins up with now (a
        reload is a short gap), openings and marks from the future go.
        Anything malformed is skipped, never raised."""
        self.reset()
        if not isinstance(data, dict) or not _finite(now):
            return
        cfg = self.cfg
        step = cfg.fit_step_s
        for d in data.get("chains") or []:
            ch = Chain.from_dict(d)
            if (ch is None or abs(ch.step_s - step) > 1e-6 or ch.end_ts > now + 1.0
                    or ch.end_ts < now - cfg.fit_window_s):
                continue
            self._chains.append(ch)
        self._chains.sort(key=lambda c: c.start_ts)
        brk = data.get("last_break")
        if (isinstance(brk, list) and len(brk) == 2 and _finite(brk[0])
                and isinstance(brk[1], str)):
            self._last_break = (min(float(brk[0]), now), brk[1])
        f = data.get("filling")
        if isinstance(f, dict):
            start, last, b = f.get("start_ts"), f.get("last_ts"), f.get("bin")
            rooms = _decode_mk(f.get("mk"))
            off = f.get("offset")
            if (_finite(start, last) and rooms is not None
                    and isinstance(b, list) and len(b) == 4 and _finite(*b) and b[2] >= 1
                    and int(b[0]) == len(rooms) and start <= last <= now + 1.0
                    and isinstance(off, list) and len(off) == 2 and _finite(*off) and off[1] >= 0):
                o = _OpenChain(start_ts=float(start), rooms=rooms,
                               bin_k=int(b[0]), bin_sum=float(b[1]), bin_n=int(b[2]),
                               last_ts=float(last), bin_tsum=float(b[3]),
                               off_sum=float(off[0]), off_n=int(off[1]))
                self._open = o
                if now - o.last_ts > cfg.chain_max_gap_s:
                    # It does not join up with now: closed where it ended
                    # (kept only within the window, like any closed chain).
                    self._close(o.last_ts)
                    self._last_break = (o.last_ts, "a restart")
                    self._chains = [ch for ch in self._chains
                                    if ch.end_ts >= now - cfg.fit_window_s]
        for item in data.get("intervals") or []:
            if (isinstance(item, (list, tuple)) and len(item) == 2 and _finite(item[0])
                    and item[0] <= now + cfg.latency_s + 1.0
                    and (item[1] is None
                         or (_finite(item[1])
                             and item[0] <= item[1] <= now + cfg.close_delay_s + 1.0))):
                self._intervals.append([float(item[0]), _opt(item[1])])
        for item in data.get("holds") or []:
            if (isinstance(item, (list, tuple)) and len(item) == 2 and _finite(item[0])
                    and item[0] <= now + 1.0
                    and (item[1] is None or (_finite(item[1]) and item[1] >= item[0]))):
                self._holds.append([float(item[0]), _opt(item[1]), _UNBOOKED_WHY])
        for item in data.get("unusable") or []:
            if (isinstance(item, (list, tuple)) and len(item) == 3 and _finite(item[0])
                    and item[0] <= now + 1.0 and isinstance(item[2], str)
                    and (item[1] is None or (_finite(item[1]) and item[1] >= item[0]))):
                self._unusable.append([float(item[0]), _opt(item[1]), item[2]])
        blocked = data.get("blocked_until")
        if _finite(blocked):
            limit = now + max(cfg.chain_settle_after_unbooked_s, cfg.chain_settle_after_window_s)
            self._blocked_until = min(float(blocked), limit)
        last = data.get("last_ts")
        if _finite(last):
            self._last_ts = min(float(last), now)

    def restore_legacy(self, data: dict[str, Any], now: float) -> None:
        """From a 2.15.0 / 2.16.0 learner save: the booked openings and the
        hold marks (their meaning is unchanged; the heat is real), no
        readings."""
        self.reset()
        cfg = self.cfg
        for key, store, ahead in (("intervals", self._intervals, cfg.latency_s + 1.0),
                                  ("holds", self._holds, 1.0)):
            for item in data.get(key) or []:
                if (isinstance(item, (list, tuple)) and len(item) == 2 and _finite(item[0])
                        and item[0] <= now + ahead
                        and (item[1] is None or _finite(item[1]))):
                    row: list[Any] = [float(item[0]), _opt(item[1])]
                    if key == "holds":
                        row.append(_UNBOOKED_WHY)
                    store.append(row)


# ---------------------------------------------------------------------------
# The learner
# ---------------------------------------------------------------------------

@dataclass
class Episode:
    """One pulse and the room's response up to its first peak.

    ``cut_ts``: where the next opening's heat (the next pulse, a heat-up's
    maximum) can first show (its opening + the delay in use).  The landing
    is looked for only until the cut.  ``landing_void``: why its landing
    will not be judged (the target changed), while the pulse may still be
    pooled.
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
    landing_void: str | None = None    # its landing is not judged (why), it may be pooled

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
            "end_reason": self.end_reason, "landing_void": self.landing_void,
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
                landing_void=(d["landing_void"] if isinstance(d.get("landing_void"), str)
                              else None),
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


# Verdict kinds.
ACCEPTED = "accepted"        # a landing judged (the pulse pooled or not)
POOLED = "pooled"            # pooled, its landing not judged (the target changed)
DROPPED = "dropped"          # disturbed before it could be judged (window, Boost, ...)
NOT_OPENED = "not_opened"    # the valve never reported heating: the pulse did nothing


@dataclass(frozen=True)
class Verdict:
    kind: str
    line: str


class DoseLearner:
    """Learns the room's gain, delay and spread from its pooled pulses, and
    an aim bias from their landings (design: project doc
    claude/roomstat-pooled-dose-learner-2026-11-01.md).

    Each pulse is an episode (:class:`Episode`) up to its landing: the
    landing teaches the aim bias (2.16.0's rules) and arms the
    two-hot-landings watchdog; the judged pulse becomes a
    :class:`PulseRecord`, usable for the pooled fit when its valve opened,
    it was long enough, and its segment lies in one unbroken chain of
    readings (:class:`ChainStore`).  A usable pulse makes a refit due
    (:attr:`refit_due`): :meth:`fit_input` takes the snapshot, the caller
    runs :func:`pooled_fit` (in an executor on the entity), and
    :meth:`apply_fit` moves the values in use by the gate and the dead band.

    Episodes may overlap: a model that under-doses makes the room ready
    again (theta + 2 tau after the last start, 21 min with the defaults)
    before the last pulse could be judged (25-35 min).  Each open episode is
    judged on its own landing, looked for until the next opening's heat can
    show (:attr:`Episode.cut_ts`).
    """

    def __init__(self, cfg: DoseConfig, autotune_cfg: AutotuneConfig | None = None) -> None:
        self.cfg = cfg
        self._at = autotune_cfg or AutotuneConfig()
        self._fit_cfg = FitConfig.from_dose(cfg)
        self._seed_rate: float | None = None
        self._seed_gain = cfg.default_gain_c_per_open_min
        # Bumped by every reset or restore: a fit started on older data is
        # discarded when it comes back (apply_fit).
        self.generation = 0
        self._chains = ChainStore(cfg)
        self._clear()

    # -- values in use ------------------------------------------------------
    def _clear(self) -> None:
        self.gain_c_per_open_min = self._seed_gain
        self.theta_s = self.cfg.default_theta_s
        self.tau_s = self.cfg.default_tau_s
        self.bias_c = 0.0
        self._accepted_total = 0
        self._accepted_pulses = 0
        self._last_more: dict[str, float] = {}
        self._episodes: list[Episode] = []
        self._records: deque[PulseRecord] = deque()
        self._watchdog_fired = False
        # The gain in use was moved by the room's own evidence (the
        # watchdog, a slow step towards a fit's range) before any fit was
        # accepted: the anchor no longer overwrites it (seed).
        self._gain_owned = False
        self.last_line: str | None = None
        self.last_fit: PooledFit | None = None
        self.refit_due = False
        # A snapshot was taken (fit_input) and its result not yet applied:
        # saved as due, so a reload in between refits (a reload drops the
        # running refit and a result waiting).
        self.refit_in_flight = False
        self._chains.reset()
        self._migration: str | None = None

    def reset(self) -> None:
        """Forget everything learned and pooled: back to the anchor and the
        default delay and spread."""
        self.generation += 1
        self._clear()

    @property
    def learned(self) -> bool:
        """A pooled fit has been acted on (passed the gate)."""
        return self._accepted_total > 0

    @property
    def chains(self) -> ChainStore:
        return self._chains

    @property
    def records(self) -> tuple[PulseRecord, ...]:
        """The judged pulses kept, oldest first."""
        return tuple(self._records)

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

    @property
    def anchor_gain_c_per_open_min(self) -> float:
        """The gain prior's centre: the auto-tuner's heating rate / 60, or
        the default."""
        return self._seed_gain

    def _gain_bounds(self) -> tuple[float, float]:
        lo, hi = self.cfg.gain_bounds
        return lo * self._seed_gain, hi * self._seed_gain

    def seed(self, rate_c_per_h: float | None) -> None:
        """The anchor from the auto-tuner's heating rate (a heat-up's rise
        at full power, °C/h: the same unit per minute): the gain prior's
        centre and its bounds; the gain in use only while nothing is learned
        and nothing the room did has moved it (afterwards the room's pulses
        decide: 2.17.0 review, the watchdog's x 1.5 was undone by the next
        cycle's seed until a fit was accepted).  The delay and the spread are
        not seeded: the tuner's dead time and coast come from heat-ups on a
        cold radiator (15 min and more in the living room and the study
        against a top-up's 4), and a delay seeded from them landed +0.15
        against +0.02 (2.15.0 review)."""
        self._seed_rate = float(rate_c_per_h) if _finite(rate_c_per_h) and rate_c_per_h > 0 else None
        self._seed_gain = (self._seed_rate / 60.0 if self._seed_rate is not None
                           else self.cfg.default_gain_c_per_open_min)
        if not self.learned and not self._gain_owned:
            self.gain_c_per_open_min = self._seed_gain
        else:
            # The room's own gain stays, within the bounds of the new anchor
            # (as a restore takes it back).
            self.gain_c_per_open_min = _clamp(self.gain_c_per_open_min, *self._gain_bounds())

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
        looked for until this pulse's heat can show.  Only beyond
        ``_OPEN_EPISODES_MAX`` open the oldest is dropped.  Returns that
        drop's line, if any."""
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
        window: bool = False, boost: bool = False, paused: bool = False,
    ) -> None:
        """Every regulated cycle, episode or not: the reading (None: none
        fresh), the valve's open interval as booked, the hold's unbooked
        heat and what pauses the readings go to the chains; each episode
        being measured claims its own opening, takes the zone's heating
        report, and the reading."""
        if not _finite(ts):
            return
        self._chains.note(ts, room_c, open_from=open_from, open_until=open_until,
                          hold_from=hold_from, hold_until=hold_until,
                          window=window, boost=boost, paused=paused)
        cfg = self.cfg
        for ep in self._episodes:
            if _finite(open_from):
                if ep.open_from is None:
                    if open_from >= ep.start_ts - 1.0:
                        ep.open_from = float(open_from)
                elif abs(open_from - ep.open_from) > 1.0:
                    # Another opening: this episode's landing is looked for
                    # until it shows.
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
        self._prune(ts)

    def interrupt(self, ts: float, why: str) -> None:
        """The room left the dosing phase for something else (Roomstat off,
        summer, the room sensor lost, the watchdog): the chain of readings
        ends."""
        self._chains.interrupt(ts, why)

    def mark_unusable(self, from_ts: float, until_ts: float | None, why: str) -> None:
        """Heat of unknown size (a pulse resumed without its booking): no
        pooled fit may see it."""
        self._chains.mark_unusable(from_ts, until_ts, why)

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
        if ep.landing_void is None and (not _finite(setpoint_c)
                                        or abs(setpoint_c - ep.setpoint_c) > 0.05):
            # Its landing says nothing about the aim any more; its heat is
            # what it was, so it may still be pooled.
            ep.landing_void = "the target changed"
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
        if ep.landed_ts is not None and ts >= ep.landed_ts + _JUDGE_AFTER_LANDING_S and not unconfirmed:
            return self._judge(ep, ts)
        return None

    def _hold_in_window(self, ep: Episode, now: float) -> bool:
        """A hold's heat (heat booked nowhere) reaches the episode: from
        ``_HOLD_LOOKBACK_S`` before the pulse, widened by the landing
        horizon in use (a hold's heat still arriving), to now.  Its opening
        is unknown, so the landing cannot account for it (2.15.0 review: a
        pulse, then a hold at 60 % in a room cooling 1.5 °C/h)."""
        lo = ep.start_ts - _HOLD_LOOKBACK_S - self.landing_horizon_s
        return self._chains.unbooked_between(lo, now)

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

    def _judge(self, ep: Episode, now: float) -> Verdict:
        """Record the landing (the room's highest reading from the pulse's
        end to the landing: the peak when the room came back up, the
        pulse-end reading when the heat only slowed its fall), whether the
        pulse can be pooled, and the aim bias and watchdog it teaches.  A
        usable pulse makes a refit due."""
        self._remove(ep)
        cfg = self.cfg
        if ep.heating_seen and not ep.valve_opened:
            return self._verdict(NOT_OPENED, "the valve never reported heating, so the pulse did nothing")
        landed_ts = ep.landed_ts if ep.landed_ts is not None else ep.samples[-1][0]
        open_min = self._open_min(ep, ep.samples[-1][0])
        after_end = [(t, y) for t, y in ep.samples
                     if ep.pulse_end_ts is not None and ep.pulse_end_ts <= t <= landed_ts]
        peak_ts, peak_room = max(after_end or [ep.samples[-1]], key=lambda p: p[1])
        peak_err = peak_room - ep.setpoint_c
        lo = ep.start_ts - cfg.segment_pre_s
        if open_min < cfg.min_open_min:
            usable, why_not = False, f"{open_min:.1f} open-min is too little"
        elif not self._chains.covers(lo, now):
            usable, why_not = False, self._chains.why_not_covered(lo)
        else:
            usable, why_not = True, None
        counted = ep.landing_void is None
        at_floor = (ep.pulse_end_ts is not None
                    and ep.pulse_end_ts - ep.start_ts <= cfg.pulse_min_s + 5.0)
        # What the gain in use promised, with the cooling known at the start.
        model = (ep.room_start_c - ep.setpoint_c + ep.gain_at_start * open_min
                 - ep.fall0_c_per_s * (peak_ts - ep.start_ts))
        record = PulseRecord(
            start_ts=ep.start_ts, open_from=ep.open_from, open_until=ep.open_until,
            open_min=open_min, landing_ts=landed_ts,
            peak_error_c=peak_err if counted else None,
            model_error_c=model if counted else None, gain_at_start=ep.gain_at_start,
            end_reason=ep.end_reason, rode_burn=ep.rode_burn,
            shortfall_start_c=ep.setpoint_c - ep.room_start_c, at_floor=at_floor,
            usable=usable, why_not=why_not,
        )
        self._records.append(record)
        if usable:
            self.refit_due = True
        pooled = "pooled" if usable else f"not pooled: {why_not}"
        if not counted:
            self._prune(now)
            if not usable:
                return self._verdict(DROPPED, f"{ep.landing_void}; {pooled}")
            line = f"{open_min:.1f} open-min, pooled; its landing not judged: {ep.landing_void}"
            self.last_line = line
            return Verdict(kind=POOLED, line=line)
        self._learn_landing(record)
        self._prune(now)
        line = (
            f"{open_min:.1f} open-min → {_signed(peak_err)} "
            f"{'by the next pulse' if ep.cut_short else 'at the peak'} "
            f"(model {_signed(model)} at {ep.gain_at_start:.3f}); {pooled}; "
            f"aim {_signed(self.bias_c)}"
        )
        if self._watchdog_fired:
            line += "; two hot landings: doses cut"
        return self._verdict(ACCEPTED, line)

    def _learn_landing(self, record: PulseRecord) -> None:
        """The aim bias and the watchdog (2.16.0's rules), from a landing.

        The aim bias absorbs slow drift the gain misses: a landing short
        moves it up, a hot one down.  A floor-length pulse that landed hot
        says nothing about the aim: the floor, not the model, set that
        landing.  Up (bigger doses) only from a pulse its own dose ended
        (not its cap, not the stop rule: they set that landing) that started
        near the target: a big dose landing short is a gain error, which the
        gain corrects slowly (the owner's choice), and an additive bias
        learned from it overdoses every small top-up ("near": within
        DoseConfig.bias_shortfall_line_c, which a wide zone below widens).
        Two landings in a row above the watchdog's line
        (DoseConfig.watchdog_line_c): the gain x 1.5 and the bias -0.1 at
        once.  A landing is judged against the aim (DoseConfig.aim_over_c,
        the dosing zone's), not the target."""
        cfg = self.cfg
        if record.peak_error_c is None:
            return
        miss = cfg.aim_over_c - record.peak_error_c
        if miss > 0:
            learn = (record.end_reason == "covered"
                     and record.shortfall_start_c is not None
                     and record.shortfall_start_c <= cfg.bias_shortfall_line_c)
        else:
            learn = not record.at_floor
        if learn:
            self.bias_c = _clamp(self.bias_c + cfg.bias_gain * miss,
                                 -cfg.bias_clamp_c, cfg.bias_clamp_c)
        self._watchdog_fired = False
        landings = [r for r in self._records if r.peak_error_c is not None]
        hot = cfg.watchdog_line_c
        if len(landings) >= 2 and all(r.peak_error_c > hot for r in landings[-2:]):
            g_lo, g_hi = self._gain_bounds()
            self.gain_c_per_open_min = _clamp(self.gain_c_per_open_min * 1.5, g_lo, g_hi)
            self.bias_c = max(-cfg.bias_clamp_c, self.bias_c - 0.1)
            self._watchdog_fired = True
            self._gain_owned = True

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
        if kind not in (ACCEPTED, POOLED):
            text = f"not learned from: {text}"
        self.last_line = text
        return Verdict(kind=kind, line=text)

    # -- the window and the pooled fit --------------------------------------------
    def _window(self, now: float) -> list[PulseRecord]:
        """The usable pulses the fit pools: the newest fit_window_pulses, at
        most fit_window_s old."""
        cfg = self.cfg
        usable = [r for r in self._records if r.usable and now - r.start_ts <= cfg.fit_window_s]
        return usable[-max(1, cfg.fit_window_pulses):]

    def _prune(self, now: float) -> None:
        """Keep the window's pulses, the newest landings (the watchdog) and
        what the chains need for them."""
        window = self._window(now)
        keep = {id(r) for r in window}
        landings = [r for r in self._records if r.peak_error_c is not None]
        keep |= {id(r) for r in landings[-_RECORDS_LANDINGS_KEPT:]}
        self._records = deque(r for r in self._records
                              if id(r) in keep and now - r.start_ts <= self.cfg.fit_window_s)
        self._chains.prune(now, window, [ep.start_ts for ep in self._episodes])

    def fit_input(self, now: float) -> FitInput | None:
        """The snapshot for :func:`pooled_fit` (None: nothing to pool); the
        refit is no longer due, but in flight until its result is applied
        (:meth:`apply_fit`) or its failure noted (:meth:`fit_failed`)."""
        self.refit_due = False
        self.refit_in_flight = False
        if not _finite(now):
            return None
        window = self._window(now)
        if not window:
            return None
        chains, intervals = self._chains.snapshot(window, now)
        if not chains:
            return None
        pulses = tuple(r for r in window if any(c.start_ts <= r.start_ts < c.end_ts for c in chains))
        if not pulses:
            return None
        self.refit_in_flight = True
        return FitInput(
            ts=float(now), generation=self.generation, chains=chains, intervals=intervals,
            pulses=pulses, anchor_gain=self._seed_gain, anchor_seeded=self._seed_rate is not None,
            in_use=(self.gain_c_per_open_min, self.theta_s, self.tau_s), cfg=self._fit_cfg,
        )

    def apply_fit(self, fit: PooledFit, now: float,
                  fmt_time: Callable[[float], str] | None = None) -> str | None:
        """Act on a pooled fit (in the event loop): the gate and the update.

        * a fit for another generation (a reset, a restore since its
          snapshot) is discarded (None); only one refit runs at a time, so
          results never come back out of order (a time test discarded every
          fit after the clock went back);
        * the fit is recorded whatever it says (the sensor shows it);
        * acted on only when ``ok``, pooled from fit_accept_min_pulses or more,
          and its inflated sd of ln gain is at most fit_accept_sd_ln;
        * the gain, with L = ln(MAP / in use) and s that sd: L > s -> the MAP
          (smaller doses) at once; L < -s -> max(MAP, (1 - step_more_frac) x
          in use), at most every step_more_interval_s (the owner's limit);
          else nothing (the dead band).  Within gain_bounds x the anchor;
        * a fit too uncertain to pass the gate but pooled from
          fit_step_more_min_pulses or more, whose whole 1-sd range lies below
          the gain in use (MAP x e^s < in use), still takes the slow step
          towards that range (max(MAP x e^s, (1 - step_more_frac) x in use),
          the same interval): only accepted fits could step down, so a room
          whose fits rarely pass the gate kept a gain the watchdog or one
          fit had raised for days (2.17.0 review: 1.7x the fits' for 3 days);
        * the delay and the spread follow the MAP when they differ from the
          values in use by a minute or more (within their bounds);
        * the dead start is logged only.

        Returns the activity line (None when discarded).  Never raises on a
        malformed fit."""
        if not isinstance(fit, PooledFit) or fit.generation != self.generation:
            return None
        self.refit_in_flight = False
        cfg = self.cfg
        fmt = fmt_time or _utc_hhmm
        g_use = self.gain_c_per_open_min
        if not fit.ok or not _well_formed(fit):
            self.last_fit = fit
            line = f"no pooled fit ({fit.why or 'malformed'}); the gain in use stays {g_use:.3f}"
            self.last_line = line
            return line
        self.last_fit = fit
        # dropped_by_fit says what the latest fit did with each usable pulse.
        dropped = dict(fit.dropped)
        self._records = deque(
            replace(r, dropped_by_fit=dropped.get(r.start_ts)) if r.usable else r
            for r in self._records)
        pct = fit.gain_uncertainty_pct or 0.0
        head = (f"pooled over {fit.n_pulses} pulses / {fit.span_s / 3600.0:.1f} h → "
                f"gain {fit.gain:.3f} ±{pct:.0f} %, delay {fit.theta_s / 60.0:.0f} min, "
                f"spread {fit.tau_s / 60.0:.0f} min")
        parts = [head]
        sd = fit.gain_sd_ln
        if fit.n_pulses < cfg.fit_accept_min_pulses or sd > cfg.fit_accept_sd_ln:
            why = (f"needs {cfg.fit_accept_min_pulses} pulses"
                   if fit.n_pulses < cfg.fit_accept_min_pulses else f"too uncertain (±{pct:.0f} %)")
            upper = fit.gain * math.exp(sd)
            g_lo, g_hi = self._gain_bounds()
            step = _clamp(max(upper, (1.0 - cfg.step_more_frac) * g_use), g_lo, g_hi)
            if fit.n_pulses >= cfg.fit_step_more_min_pulses and upper < g_use and step < g_use - 1e-9:
                last = self._last_more.get("gain")
                if last is None or now - last >= cfg.step_more_interval_s:
                    self._last_more["gain"] = now
                    self.gain_c_per_open_min = step
                    self._gain_owned = True
                    parts.append(f"{why} to act on, but its whole range is below the gain in "
                                 f"use: a step {g_use:.3f} → {step:.3f}")
                else:
                    parts.append(f"{why}, its range below the gain in use {g_use:.3f}: a step "
                                 f"down possible from {fmt(last + cfg.step_more_interval_s)}")
            else:
                parts.append(f"{why}, gain in use stays {g_use:.3f}")
        else:
            self._accepted_total += 1
            self._accepted_pulses = fit.n_pulses
            g_lo, g_hi = self._gain_bounds()
            lr = math.log(fit.gain / g_use) if g_use > 0 else math.inf
            new = g_use
            if lr > sd:
                new = fit.gain
            elif lr < -sd:
                last = self._last_more.get("gain")
                if last is None or now - last >= cfg.step_more_interval_s:
                    new = max(fit.gain, (1.0 - cfg.step_more_frac) * g_use)
                    self._last_more["gain"] = now
                else:
                    parts.append(f"{fit.gain:.3f} wanted, a step down possible from "
                                 f"{fmt(last + cfg.step_more_interval_s)}")
            new = _clamp(new, g_lo, g_hi)
            if abs(new - g_use) > 1e-9:
                self.gain_c_per_open_min = new
                parts.append(f"gain in use {g_use:.3f} → {new:.3f}")
            elif len(parts) == 1:
                parts.append(f"the gain in use {g_use:.3f} within its band")
            for name, value, lo, hi, label in (
                ("theta_s", fit.theta_s, *cfg.theta_bounds_s, "delay"),
                ("tau_s", fit.tau_s, *cfg.tau_bounds_s, "spread"),
            ):
                old = getattr(self, name)
                if abs(value - old) >= 60.0:
                    moved = _clamp(value, lo, hi)
                    if abs(moved - old) > 1e-9:
                        setattr(self, name, moved)
                        parts.append(f"{label} in use {old / 60.0:.1f} → {moved / 60.0:.1f} min")
        for start, why in fit.dropped:
            parts.append(f"dropped {fmt(start)} ({why})")
        if fit.dead_start_posterior:
            p0 = dict(fit.dead_start_posterior).get(0.0)
            c_best, p_best = max(fit.dead_start_posterior, key=lambda cp: cp[1])
            if p0 is not None and p0 < 0.5 and c_best > 0:
                parts.append(f"dead start {c_best:.0f} s favoured ({p_best:.2f})")
        line = "; ".join(parts)
        self.last_line = line
        return line

    def fit_failed(self, generation: int) -> None:
        """The refit of this generation raised (the caller logs it): no
        longer in flight; the model in use stays and the next usable pulse
        refits."""
        if generation == self.generation:
            self.refit_in_flight = False

    def take_migration_note(self) -> str | None:
        """The line for a 2.15.0 / 2.16.0 save that was not taken back, once."""
        note, self._migration = self._migration, None
        return note

    # -- summary and persistence ----------------------------------------------
    def summary(self) -> dict[str, Any]:
        """The model in use and the last pooled fit: changes only when a
        pulse is judged or a fit applied (the Top-up gain sensor's
        attributes, recorded)."""
        fit = self.last_fit if self.last_fit is not None and self.last_fit.ok else None
        pct = fit.gain_uncertainty_pct if fit is not None else None
        return {
            "gain_c_per_open_min": round(self.gain_c_per_open_min, 4),
            "gain_uncertainty_pct": round(pct) if pct is not None else None,
            "delay_min": round(self.theta_s / 60.0, 1),
            "spread_min": round(self.tau_s / 60.0, 1),
            "dead_start_s": round(fit.dead_start_s) if fit is not None else None,
            "visible_dead_time_min": round(self.visible_dead_time_s / 60.0, 1),
            "landing_horizon_min": round(self.landing_horizon_s / 60.0, 1),
            "bias_c": round(self.bias_c, 3),
            "pulses_pooled": fit.n_pulses if fit is not None else 0,
            "pooled_span_h": round(fit.span_s / 3600.0, 1) if fit is not None else None,
            "fit_rms_mc": round(fit.rms_c * 1000.0, 1) if fit is not None else None,
            "fit_sigma_mc": round(fit.sigma_c * 1000.0, 1) if fit is not None else None,
            "fit_effective_samples": round(fit.effective_samples) if fit is not None else None,
            "anchor_gain_c_per_open_min": round(self._seed_gain, 4),
            "learning": (f"pooled over {self._accepted_pulses} pulses" if self.learned
                         else "seeded, then moved by the room's landings" if self._gain_owned
                         else "seeded, nothing pooled yet"),
            "last": self.last_line,
        }

    def diagnostics(self) -> dict[str, Any]:
        """The summary, the pulses kept, the chains' spans, the last fit in
        full and the episodes being measured."""
        data = self.summary()
        fit = self.last_fit
        data.update({
            "pulses": [r.to_dict() for r in self._records],
            "chains": [[round(c.start_ts, 1), round(c.end_ts, 1), len(c.rooms)]
                       for c in self._chains.chains],
            "chain_break": (list(self._chains.last_break) if self._chains.last_break else None),
            "chain_blocked_until": self._chains.blocked_until,
            "last_fit": fit.to_dict() if fit is not None else None,
            "fits_accepted": self._accepted_total,
            "episodes_running": [
                {"start_ts": ep.start_ts, "pulse_end_ts": ep.pulse_end_ts,
                 "cut_ts": ep.cut_ts, "landed_ts": ep.landed_ts,
                 "valve_opened": ep.valve_opened, "landing_void": ep.landing_void}
                for ep in self._episodes
            ],
        })
        return data

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": _LEARNER_VERSION,
            "gain": self.gain_c_per_open_min, "theta_s": self.theta_s, "tau_s": self.tau_s,
            "bias_c": self.bias_c, "accepted_total": self._accepted_total,
            "accepted_pulses": self._accepted_pulses,
            "seed_args": [self._seed_rate],
            "last_more": dict(self._last_more),
            "watchdog_fired": self._watchdog_fired,
            "last_line": self.last_line,
            "store": self._chains.to_dict(),
            "pulses": [r.to_dict() for r in self._records],
            "episodes": [ep.to_dict() for ep in self._episodes],
            "last_fit": self.last_fit.to_dict() if self.last_fit is not None else None,
            "refit_due": self.refit_due or self.refit_in_flight,
            "gain_owned": self._gain_owned,
        }

    def restore(self, data: Any, now: float) -> None:
        """Take a 2.17 save back: the values in use (within their bounds),
        the chains (design §5's rules), the pulses kept, the last fit, the
        episodes being measured if they still join up with now.  A 2.15.0 /
        2.16.0 save (no ``"v": 2``) is not taken back -- its single-pulse
        values were learned on noise -- except the anchor, the open
        episodes (their landings still count), and the booked openings and
        hold marks; one line says so (:meth:`take_migration_note`).
        Anything malformed is skipped, never raised."""
        self.generation += 1
        self._clear()
        if not isinstance(data, dict) or not _finite(now):
            return
        seed = data.get("seed_args")
        if isinstance(seed, (list, tuple)) and len(seed) in (1, 3):
            self.seed(seed[0] if _finite(seed[0]) else None)
        self._restore_episodes(data, now)
        if data.get("v") != _LEARNER_VERSION:
            self._chains.restore_legacy(data, now)
            self._migration = self._legacy_note(data)
            return
        cfg = self.cfg
        accepted = data.get("accepted_total", 0)
        self._accepted_total = int(accepted) if _finite(accepted) and accepted >= 0 else 0
        pulses = data.get("accepted_pulses", 0)
        self._accepted_pulses = int(pulses) if _finite(pulses) and pulses >= 0 else 0
        gain, theta, tau, bias = (data.get(k) for k in ("gain", "theta_s", "tau_s", "bias_c"))
        owned = data.get("gain_owned") is True
        if (self._accepted_total > 0 or owned) and _finite(gain, theta, tau) and gain > 0:
            g_lo, g_hi = self._gain_bounds()
            self.gain_c_per_open_min = _clamp(float(gain), g_lo, g_hi)
            self.theta_s = _clamp(float(theta), *cfg.theta_bounds_s)
            self.tau_s = _clamp(float(tau), *cfg.tau_bounds_s)
            self._gain_owned = owned and self._accepted_total == 0
        else:
            self._accepted_total = 0
        if _finite(bias):
            self.bias_c = _clamp(float(bias), -cfg.bias_clamp_c, cfg.bias_clamp_c)
        last_more = data.get("last_more")
        if isinstance(last_more, dict) and _finite(last_more.get("gain")):
            self._last_more = {"gain": min(float(last_more["gain"]), now)}
        self._watchdog_fired = data.get("watchdog_fired") is True
        line = data.get("last_line")
        self.last_line = line if isinstance(line, str) else None
        self._chains.restore(data.get("store"), now)
        records = [r for r in (PulseRecord.from_dict(d) for d in data.get("pulses") or []) if r]
        self._records = deque(sorted((r for r in records if r.start_ts <= now + 1.0),
                                     key=lambda r: r.start_ts))
        self.last_fit = PooledFit.from_dict(data.get("last_fit"), self.generation)
        if self.last_fit is not None and self.last_fit.ts > now + 1.0:
            self.last_fit = None
        # A pulse judged just before the save whose fit never came back (a
        # reload drops a refit running): due again.
        self.refit_due = data.get("refit_due") is True
        self._prune(now)

    def _restore_episodes(self, data: dict[str, Any], now: float) -> None:
        saved = data.get("episodes")
        if not isinstance(saved, list):
            saved = [data.get("episode")]
        episodes = [ep for ep in (Episode.from_dict(d) for d in saved) if ep is not None]
        self._episodes = [
            ep for ep in sorted(episodes, key=lambda e: e.start_ts)
            if ep.samples[-1][0] <= now + 1.0 and now - ep.samples[-1][0] <= _EPISODE_MAX_GAP_S
        ][-_OPEN_EPISODES_MAX:]

    def _legacy_note(self, data: dict[str, Any]) -> str:
        anchor = (f"the heat-up rate ({self._seed_gain:.3f})" if self._seed_rate is not None
                  else f"the default ({self._seed_gain:.3f})")
        gain, tau, accepted = data.get("gain"), data.get("tau_s"), data.get("accepted_total")
        if _finite(gain, tau, accepted) and accepted > 0:
            return (f"2.17 pools pulses; the single-pulse values (gain {gain:.3f}, spread "
                    f"{tau / 60.0:.0f} min) are dropped and the gain starts from {anchor}")
        return f"2.17 pools pulses; nothing was learned yet, the gain starts from {anchor}"


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
    a room at or below its aim rides another zone's burn.  Every room
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
        ready and at or below its aim (``virtual_error_c`` is measured from
        the aim, DoseConfig.aim_over_c); None otherwise."""
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
    refit_due: bool = False               # a usable pulse was judged: run the pooled fit


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
    command to the next off or maximum + ``close_delay_s``): no landing is
    judged, and no pooled fit sees readings, while its heat may arrive.

    The cooling the dose covers is read from the room *as if unheated*: the
    reading less the rise of Roomstat's heat that has arrived (the model's
    own account), through the regulator's 15-min Theil-Sen slope.  This is
    the Smith predictor's disturbance observer.  The room's own slope still
    holds part of the last landing's rise when the next pulse is due, which
    reads as a slower cooling (the entity test: -0.25 °C/h against a true
    -0.50), so the pulse came late and small and the troughs reached
    -0.13..-0.19.
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
        # A pulse resumed without its booking (resume_open): from when, so
        # the pooled fit's mark of it ends with the booking.
        self._resumed_from: float | None = None

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
        """The auto-tuner's heating rate anchors the gain (DoseLearner.seed)."""
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
            if self._resumed_from is not None:
                # The resumed pulse's booking has closed: its mark ends.
                self.learner.mark_unusable(self._resumed_from, self._open_until,
                                           "a pulse resumed without its booking")
                self._resumed_from = None
            self._open_from = self._open_until = None
        if self._hold_until is not None and self._hold_until <= now:
            self._unbooked_last_ts = self._hold_until
            self._hold_from = self._hold_until = None
        self.inflight_c = self.predictor.inflight_c(self.learner.gain_c_per_open_min)

    def inputs(
        self, now: float, *, enabled: bool, setpoint_c: float, room_c: float,
        room_slope_c_per_s: float | None, state: Any, deadband_c_per_h: float,
        room_fresh: bool = True,
    ) -> dict[str, Any]:
        """Step the predictor and return regulation.compute()'s dosing
        keyword arguments.  ``fire_now`` is the coordinator's verdict on
        this cycle's own numbers, judged against the dosing zone (the
        release at ``zone_below_c``, a burn ridden at or below the aim).
        A reading bridged over a sensor gap
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
            ready_in = (dose_ready_in_s(e_star, fall, lead, self.cfg.zone_below_c)
                        if ready else None)
            verdict = self.coordinator.should_fire(
                self.room_id, now, virtual_error_c=e_star + self.cfg.aim_over_c, ready=ready,
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
        the maximum, so it is booked open from now.  When it really opened
        is unknown, so no pooled fit sees its heat (marked until the
        booking closes, at the longest a pulse may run)."""
        if not _finite(now) or self.open:
            return
        self._open_from, self._open_until = float(now), None
        if self._last_book is None:
            self._last_book = float(now)
        self._resumed_from = float(now)
        self.learner.mark_unusable(
            now, now + self.cfg.pulse_ceiling_s + self.cfg.close_delay_s,
            "a pulse resumed without its booking")

    def note_valve_heating_unbooked(self, now: float) -> None:
        """The valve is seen holding a heat setpoint no command of ours
        explains (a hold sent before dosing was on, before the upgrade from
        2.15.0): its heat is booked nowhere, so it is marked like a hold's,
        until an off or a maximum of ours, or the valve seen shut."""
        if not _finite(now) or self.open:
            return
        self._mark_hold(now, False)

    def interrupt(self, now: float, why: str) -> None:
        """Roomstat off, summer, the room sensor lost, the watchdog: the
        readings pooled break here."""
        self.learner.interrupt(now, why)

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
        neither the chains nor an episode take it.  ``refit_due`` on the
        outcome: a usable pulse was judged (it stays due until the caller
        takes :meth:`DoseLearner.fit_input`)."""
        lines: list[str] = []
        woke: list[str] = []
        fired_with: tuple[str, str] | None = None
        lr = self.learner
        fired_by = getattr(result, "fired_by", None)
        if fired_by and sent:
            if room_fresh and _finite(room_c):
                # The episode's loss: the room as if unheated when known (the
                # room's own slope still holds the last landing).
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
                      hold_from=self._hold_from, hold_until=self._hold_until,
                      window=window, boost=boost, paused=not self._enabled)
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
                            woke=woke, fired_with=fired_with, refit_due=lr.refit_due)

    def refit_now(self, now: float | None = None,
                  fmt_time: Callable[[float], str] | None = None) -> str | None:
        """The pooled fit run here and now (the simulators and the HA-free
        tests; the entity runs :func:`pooled_fit` in an executor): the
        snapshot, the fit, the gate and the update.  Returns the activity
        line, None when there was nothing to pool."""
        if now is None:
            now = self.predictor.last_step if self.predictor.last_step is not None else self._last_book
        if not _finite(now):
            return None
        inp = self.learner.fit_input(now)
        if inp is None:
            return None
        try:
            fit = pooled_fit(inp)
        except Exception:
            self.learner.fit_failed(inp.generation)
            raise
        return self.learner.apply_fit(fit, now, fmt_time)

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
        """What changes only when a pulse starts, a pulse is judged or a
        pooled fit is applied (the Top-up gain sensor's attributes, which
        the recorder keeps: a live value here wrote a row on 489 of 504
        cycles, about 2 KB each).  A pulse leaving the 24-h counts also
        changes it, once per pulse."""
        data = self.learner.summary()
        kinds = [k for _, k in self._fired]
        data.update({
            "pulses_24h": len(kinds),
            "rode_burn_24h": kinds.count("burn"),
            "together_24h": kinds.count("together"),
        })
        return data

    def diagnostics(self) -> dict[str, Any]:
        """Everything: the summary, the pulses kept and being measured, the
        last fit, and the live values (heat in flight, the booking, the
        observer)."""
        data = self.learner.diagnostics()
        data.update(self.summary())
        loss = self.loss_slope_c_per_s
        cap_loss = self.cap_loss_slope_c_per_s
        data.update({
            "zone_c": [self.cfg.zone_above_c, -self.cfg.zone_below_c],
            "aim_over_c": round(self.cfg.aim_over_c, 3),
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
            "resumed_from": self._resumed_from,
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
        resumed = data.get("resumed_from")
        self._resumed_from = (float(resumed) if _finite(resumed) and resumed <= now + 1.0
                              and self._open_from is not None else None)
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
        """Forget what was learned, pooled and booked (Reset auto-tune)."""
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
        self._resumed_from = None
