"""Dosed top-ups, the HA-free parts (dosing.py): the predictor, the
per-episode shape fit (replayed on the living room's 19 real hold top-ups
of 25-30 Oct 2026), the pooled fit (2.17.0: the closed-form response, the
banded smoother, synthetic nights, the real night of 1 Nov 2026), the gate
and the update, the chains of readings, the landings, the pulse
coordinator and the per-room doser in closed loop."""
from __future__ import annotations

import dataclasses
import json
import math
import random
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.pure_modules import dosing, parameters, regulation

CFG = parameters.DoseConfig()
# The simulators' config: no dead-start grid (a diagnostic only: the triple
# in use is the same with or without it, TestPooledFit), a third of the time.
FAST = dataclasses.replace(CFG, fit_dead_start_grid_s=(0.0,), fit_dead_start_prior=(1.0,))
FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "living_room_topups_2026_10.json"
NIGHT = json.loads((FIXTURES / "living_room_night_2026_11_01.json").read_text())
PER_H = 1.0 / 3600.0
T0 = 1_800_000_000.0


def _step_at(series, t, default=None):
    """A step series' value at t (the last report at or before it)."""
    v = default
    for tt, vv in series:
        if tt > t:
            break
        v = vv
    return v


def _episodes():
    return json.loads(FIXTURE.read_text())["episodes"]


# ---------------------------------------------------------------------------
# The predictor
# ---------------------------------------------------------------------------

class TestPredictor:
    def test_one_open_minute_waits_out_the_delay_then_decays(self):
        p = dosing.TopupPredictor(CFG)
        p.book(0.0, 1.0)
        for t in range(0, 301, 60):
            p.step(float(t), 300.0, 480.0)
            if t < 300:
                assert p.committed_open_min() == pytest.approx(1.0)
        t = 300
        while t < 300 + 3 * 480:
            t += 60
            p.step(float(t), 300.0, 480.0)
        # The emitter starts delivering in the step the heat arrives (as
        # fopdt_response does): 3 tau and one step after the delay.
        assert p.committed_open_min() == pytest.approx(math.exp(-(3 * 480 + 60) / 480), rel=1e-6)
        assert p.committed_open_min() < 0.05

    def test_inflight_is_the_gain_times_what_is_committed(self):
        p = dosing.TopupPredictor(CFG)
        p.book(0.0, 2.0)
        p.step(0.0, 300.0, 480.0)
        assert p.inflight_c(0.045) == pytest.approx(0.09)
        assert p.inflight_c(0.0) == 0.0
        assert p.inflight_c(math.nan) == 0.0
        p.book(math.nan, 1.0)
        p.book(10.0, math.inf)
        assert p.committed_open_min() == pytest.approx(2.0)

    def test_restore_only_when_it_joins_up_with_now(self):
        p = dosing.TopupPredictor(CFG)
        p.book(1000.0, 1.5)
        p.step(1000.0, 300.0, 480.0)
        p.step(1400.0, 300.0, 480.0)
        saved = p.to_dict()
        q = dosing.TopupPredictor(CFG)
        assert q.restore(saved, 1460.0)
        assert q.committed_open_min() == pytest.approx(p.committed_open_min(), abs=1e-4)
        assert not q.restore(saved, 1400.0 + 601.0)
        assert q.committed_open_min() == 0.0
        assert not q.restore(saved, 1300.0)       # the last step is in the future
        assert not q.restore({"last_step": "x"}, 1460.0)
        assert not q.restore(None, 1460.0)


# ---------------------------------------------------------------------------
# The rule's arithmetic (regulation's helpers, as the doser uses them)
# ---------------------------------------------------------------------------

class TestNeed:
    def test_the_design_worked_example(self):
        # 26 Oct 00:09: fires at 0.037 under, the room falling 0.42 °C/h,
        # the heat landing over 21 min.
        assert regulation.dose_need_c(0.037, 0.42 * PER_H, 21 * 60) == pytest.approx(0.184, abs=0.001)
        assert regulation.dose_need_c(0.10, 0.0, 21 * 60) == pytest.approx(0.10)

    def test_a_rising_room_adds_nothing(self):
        fall = regulation.dose_fall_c_per_s(0.5 * PER_H, 0.1)
        assert fall == 0.0
        assert regulation.dose_need_c(0.05, fall, 1260.0) == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# The per-episode shape fit (the offline tool)
# ---------------------------------------------------------------------------

def _synthetic(gain=0.045, theta=300.0, tau=480.0, fall_h=0.5, open_from=60.0,
               open_until=270.0, dt=20.0, end=2400.0, room0=19.9):
    ts = [k * dt for k in range(int(end / dt) + 1)]
    u = [max(0.0, min(t + dt, open_until) - max(t, open_from)) / dt for t in ts]
    rise = dosing.fopdt_response(u, dt, theta, tau)
    room = [room0 - fall_h * PER_H * t + gain * r for t, r in zip(ts, rise)]
    return ts, room, u


class TestFit:
    def test_recovers_a_synthetic_response(self):
        ts, room, u = _synthetic()
        fit = dosing.fit_episode(ts, room, u, 0.5 * PER_H)
        assert abs(fit.theta_s - 300.0) <= 60.0
        assert fit.tau_s in (360.0, 480.0, 600.0)
        assert fit.gain_c_per_open_min == pytest.approx(0.045, rel=0.1)
        assert fit.rms_c < 0.005

    def test_the_loss_and_an_earlier_pulses_tail(self):
        """A pulse whose predecessor's heat is still arriving in the 15 min
        before it, a room losing 0.5 °C/h, 20-s readings quantised to
        0.01 °C: the earlier pulse is in the model's rise, so neither the
        gain nor the loss is biased; the room's own slope before the pulse
        as the loss biases the gain."""
        ts = [k * 20.0 for k in range(int(6000 / 20) + 1)]
        pulses = [(60.0, 330.0), (1560.0, 1830.0)]       # 4.5 open-min each, 25 min apart
        u = [sum(max(0.0, min(t + 20.0, b) - max(t, a)) for a, b in pulses) / 20.0 for t in ts]
        rise = dosing.fopdt_response(u, 20.0, 300.0, 480.0)
        room = [round(20.4 - 0.5 * PER_H * t + 0.045 * r, 2) for t, r in zip(ts, rise)]
        start = 1500.0
        upto = [i for i, t in enumerate(ts) if t <= start + 45 * 60]
        cut = upto[-1] + 1
        fit = dosing.fit_episode(ts[:cut], room[:cut], u[:cut], 0.0, fit_loss=True,
                                 window_start_ts=start - 900.0)
        assert fit.gain_c_per_open_min == pytest.approx(0.045, rel=0.10)
        assert fit.loss_c_per_s * 3600 == pytest.approx(0.5, abs=0.05)
        assert abs(fit.theta_s - 300.0) <= 60.0
        assert fit.tau_s in (360.0, 480.0, 600.0)
        pre = [(t, y) for t, y in zip(ts, room) if start - 900 <= t <= start]
        slope = dosing.theil_sen(pre)
        window = [i for i in range(cut) if ts[i] >= start]
        biased = dosing.fit_episode([ts[i] for i in window], [room[i] for i in window],
                                    [u[i] for i in window], max(0.0, -slope))
        assert abs(biased.gain_c_per_open_min / 0.045 - 1) > 0.10

    def test_nothing_to_fit(self):
        assert dosing.fit_episode([0.0, 60.0], [20.0, 20.0], [0.0, 0.0], 0.0) is None
        ts = [float(t) for t in range(0, 600, 60)]
        assert dosing.fit_episode(ts, [20.0] * 10, [0.0] * 10, 0.0) is None


class TestFixtureReplay:
    """The 19 settled hold top-ups the design was fitted on (design §11):
    the room polled every 20 s (as the entity reads it), the valve open
    while the zone reported heating, the pre-pulse fall continued."""

    def _fit(self, e):
        end = e["peak_after_min"] * 60 + 300
        ts = [float(t) for t in range(0, int(end) + 1, 20)]
        room = [_step_at(e["room_c"], t) for t in ts]
        u = [1.0 if _step_at(e["heating_pct"], t, 0.0) > 0 else 0.0 for t in ts]
        fall0 = max(0.0, -e["pre_pulse_slope_c_per_h"]) * PER_H
        return dosing.fit_episode(ts, room, u, fall0)

    def test_every_episode_fits_and_matches_the_design(self):
        fits = [(e, self._fit(e)) for e in _episodes()]
        assert len(fits) == 19
        for e, f in fits:
            assert f.rms_c <= 0.05, e["when"]
            assert f.gain_c_per_open_min == pytest.approx(e["fit"]["gain_c_per_open_min"], rel=0.25), e["when"]
        gains = [f.gain_c_per_open_min for _, f in fits]
        thetas = [f.theta_s / 60 for _, f in fits]
        taus = [f.tau_s / 60 for _, f in fits]
        assert 0.03 <= statistics.median(gains) <= 0.06
        assert 2 <= statistics.median(thetas) <= 9
        assert 4 <= statistics.median(taus) <= 12

    def test_the_final_rule_on_each_real_release(self):
        """Design §12: fire on the visible dead time, aim at the target,
        3-min floor; each episode's own fitted model as the truth."""
        g_use = CFG.default_gain_c_per_open_min
        lat, close = CFG.latency_s / 60, CFG.close_delay_s / 60
        landings, pulses = [], []
        for e in _episodes():
            fit = e["fit"]
            theta, tau = fit["theta_min"] * 60, fit["tau_min"] * 60
            g = fit["gain_c_per_open_min"]
            fall = max(0.0, -e["pre_pulse_slope_c_per_h"]) * PER_H
            lead, horizon = theta + tau / 2, theta + 2 * tau
            e_rel = 0.10 - lead * fall
            assert regulation.dose_ready_in_s(e_rel, fall, lead, 0.10) == 0.0
            need = regulation.dose_need_c(e_rel, fall, horizon)
            pulse = min(12.0, max(3.0, need / g_use - (close - lat)))
            opened = pulse - lat + close
            landing = -e_rel + g * opened - fall * (pulse * 60 + horizon)
            landings.append(landing)
            pulses.append(pulse)
        q = statistics.quantiles(landings)
        assert -0.08 <= statistics.median(landings) <= 0.03
        assert q[0] >= -0.10 and q[2] <= 0.10
        assert sum(abs(x) <= 0.10 for x in landings) >= 14
        assert 3.0 <= statistics.median(pulses) <= 3.5

    def test_the_loss_fitted_with_the_gain(self):
        """The loss estimated jointly, from 15 min before the release: the
        gains stay close to the offline fit's, and the fitted loss is lower
        than the slope before the release, which still held the last
        landing (median 0.34 against 0.45 °C/h)."""
        fits = []
        for e in _episodes():
            end = e["peak_after_min"] * 60 + 300
            ts = [float(t) for t in range(-900, int(end) + 1, 20)]
            room = [_step_at(e["room_c"], t, e["room_c"][0][1]) for t in ts]
            u = [1.0 if t >= 0 and _step_at(e["heating_pct"], t, 0.0) > 0 else 0.0 for t in ts]
            fit = dosing.fit_episode(ts, room, u, 0.0, fit_loss=True, window_start_ts=-900.0)
            fits.append((e, fit))
        for e, f in fits:
            assert f.rms_c <= 0.05, e["when"]
            assert -0.05 <= f.loss_c_per_s * 3600 <= 3.0, e["when"]
        gains = [f.gain_c_per_open_min for _, f in fits]
        losses = [f.loss_c_per_s * 3600 for _, f in fits]
        pre = [max(0.0, -e["pre_pulse_slope_c_per_h"]) for e, _ in fits]
        assert 0.025 <= statistics.median(gains) <= 0.06
        assert 2 <= statistics.median(f.theta_s / 60 for _, f in fits) <= 9
        assert 4 <= statistics.median(f.tau_s / 60 for _, f in fits) <= 12
        assert 0.2 <= statistics.median(losses) <= 0.6
        assert statistics.median(losses) < statistics.median(pre)
        rel = [f.gain_c_per_open_min / e["fit"]["gain_c_per_open_min"] for e, f in fits]
        assert 0.7 <= statistics.median(rel) <= 1.2

    def test_pooled_the_hold_top_ups_agree_with_their_own_fits(self):
        """The 19 top-ups pooled, each its own chain (the fixture holds 15
        min before each release: the segment's lead is shortened to fit):
        about 0.034 against the per-episode 0.036 (loss fitted) and 0.041
        (offline), theta and tau where the per-episode fits put them.  These
        were holds, the valve throttled to 31-42 %, so an open-minute here
        (the zone reporting any heating) is less heat than a pulse's: on
        the low side of the pulses' 0.045-0.05."""
        cfg = dataclasses.replace(FAST, segment_pre_s=840.0)
        chains, intervals, records = [], [], []
        for i, e in enumerate(_episodes()):
            base = T0 + i * 86400.0
            store = dosing.ChainStore(cfg)
            for t in range(-900, int(e["room_c"][-1][0]) + 1, 60):
                store.note(base + t, _step_at(e["room_c"], t, e["room_c"][0][1]), open_from=None,
                           open_until=None, hold_from=None, hold_until=None)
            chains += list(store.chains)
            on = None
            for t, pct in e["heating_pct"]:
                if pct > 0 and on is None:
                    on = t
                elif pct == 0 and on is not None:
                    intervals.append((base + on, base + t))
                    on = None
            if on is not None:
                intervals.append((base + on, base + e["heating_pct"][-1][0]))
            records.append(_record(base, e["valve_open_min"], base + e["peak_after_min"] * 60))
        inp = dosing.FitInput(
            ts=T0 + 30 * 86400.0, generation=0, chains=tuple(chains), intervals=tuple(intervals),
            pulses=tuple(records), anchor_gain=2.62 / 60.0, anchor_seeded=True,
            in_use=(0.045, 300.0, 480.0), cfg=dosing.FitConfig.from_dose(cfg))
        fit = dosing.pooled_fit(inp)
        assert fit.ok and fit.n_pulses >= 15
        assert 0.030 <= fit.gain <= 0.046
        assert 2 * 60 <= fit.theta_s <= 9 * 60 and 4 * 60 <= fit.tau_s <= 12 * 60


# ---------------------------------------------------------------------------
# The pooled fit: the closed-form response and the smoother
# ---------------------------------------------------------------------------

class TestCumulative:
    def test_agrees_with_the_discrete_response_on_a_four_minute_step(self):
        dt = 10.0
        ts = [k * dt for k in range(int(5400 / dt) + 1)]
        open_from, open_until = 120.0, 360.0
        u = [max(0.0, min(t + dt, open_until) - max(t, open_from)) / dt for t in ts]
        discrete = dosing.fopdt_response(u, dt, 300.0, 480.0)
        exact = dosing.fopdt_cumulative(ts, [(open_from, open_until)], 300.0, 480.0)
        for k in range(0, len(ts), 6):                # every minute
            assert abs(exact[k] - discrete[k]) <= 0.01 * 4.0, ts[k]
        assert exact[-1] == pytest.approx(4.0, rel=1e-3)

    def test_additive_and_the_dead_start(self):
        ts = [float(t) for t in range(0, 7200, 60)]
        a = dosing.fopdt_cumulative(ts, [(0.0, 240.0)], 300.0, 480.0)
        b = dosing.fopdt_cumulative(ts, [(1800.0, 2100.0)], 300.0, 480.0)
        both = dosing.fopdt_cumulative(ts, [(0.0, 240.0), (1800.0, 2100.0)], 300.0, 480.0)
        assert all(x + y == pytest.approx(z, abs=1e-12) for x, y, z in zip(a, b, both))
        late = dosing.fopdt_cumulative(ts, [(0.0, 240.0)], 300.0, 480.0, dead_start_s=90.0)
        assert late[-1] == pytest.approx(2.5, rel=1e-3)        # 4 - 1.5 open-minutes
        assert all(x <= y + 1e-12 for x, y in zip(late, a))
        assert dosing.fopdt_cumulative(ts, [(0.0, 60.0)], 300.0, 480.0, dead_start_s=90.0) == [0.0] * len(ts)
        assert dosing.fopdt_cumulative(ts, [(0.0, 240.0)], 300.0, 0.0) == [0.0] * len(ts)

    def test_the_fits_grid_form_is_the_closed_form(self):
        """The fit sums ramps on the grid (O(n) however many openings);
        the result is the closed form's, to rounding."""
        intervals = [(T0 - 7000.0, T0 - 6700.0), (T0 + 600.0, T0 + 900.0),
                     (T0 + 1500.0, T0 + 1980.0), (T0 + 5000.0, T0 + 5200.0)]
        for theta, tau, c0 in ((60.0, 180.0, 0.0), (300.0, 480.0, 90.0), (1091.0, 1676.0, 180.0)):
            x0, step, n = 60.0, 120.0, 80
            ts = [T0 + x0 + k * step for k in range(n)]
            exact = dosing.fopdt_cumulative(ts, intervals, theta, tau, c0)
            grid = dosing._ramps_on_grid(x0, step, n, dosing._ramp_events(intervals, theta, c0, T0), tau)
            assert max(abs(a - b) for a, b in zip(exact, grid)) < 1e-9


def _dense_inverse(a):
    """Gauss-Jordan inverse (the test's reference)."""
    n = len(a)
    m = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        piv = m[c][c]
        m[c] = [v / piv for v in m[c]]
        for r in range(n):
            if r != c and m[r][c] != 0.0:
                f = m[r][c]
                m[r] = [v - f * w for v, w in zip(m[r], m[c])]
    return [row[n:] for row in m]


def _whittaker_matrix(n, lam):
    a = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for i in range(n - 2):
        row = {i: 1.0, i + 1: -2.0, i + 2: 1.0}
        for p, vp in row.items():
            for q, vq in row.items():
                a[p][q] += lam * vp * vq
    return a


class TestBandedSmoother:
    @pytest.mark.parametrize("n", [1, 2, 3, 5, 12, 50])
    @pytest.mark.parametrize("lam", [0.0, 1.0, 1e3, 1e6])
    def test_matches_a_dense_solve_and_trace(self, n, lam):
        rng = random.Random(n * 7 + int(math.log10(lam + 1)))
        v = [rng.uniform(-1.0, 1.0) for _ in range(n)]
        inv = _dense_inverse(_whittaker_matrix(n, lam))
        dense = [sum(inv[i][j] * v[j] for j in range(n)) for i in range(n)]
        sm = dosing.BandedSmoother(n, lam)
        got = sm.solve(v)
        scale = max(1.0, max(abs(x) for x in dense))
        assert max(abs(a - b) for a, b in zip(got, dense)) <= 1e-9 * scale
        assert sm.trace_inverse() == pytest.approx(sum(inv[i][i] for i in range(n)), rel=1e-9)
        assert sm.residual(v) == pytest.approx([a - b for a, b in zip(v, dense)], abs=1e-9 * scale)

    def test_lines_pass_through_and_the_dof_falls_with_the_stiffness(self):
        sm = dosing.BandedSmoother(40, 1e4)
        line = [3.0 - 0.02 * k for k in range(40)]
        assert max(abs(r) for r in sm.residual(line)) < 1e-9
        dofs = [dosing.BandedSmoother(200, lam).dof for lam in (1.0, 1e2, 1e4, 1e6)]
        assert dofs == sorted(dofs, reverse=True) and 2.0 <= dofs[-1] < 4.0
        with pytest.raises(ValueError):
            dosing.BandedSmoother(0, 1.0)
        with pytest.raises(ValueError):
            dosing.BandedSmoother(5, -1.0)

    def test_the_stiffness_of_the_physical_constant(self):
        """W 0.05 °C/h per 30 min on 2-min bins: q = 4.3e-4 °C, lam = 1.2e3
        at sigma 0.015 (design §2), / T for the tempered likelihood."""
        fc = dosing.FitConfig.from_dose(CFG)
        assert fc.stiffness(0.015) == pytest.approx(1.2e3, rel=0.02)
        assert fc.stiffness(0.015, 0.1) == pytest.approx(1.2e4, rel=0.02)
        assert len(fc.thetas_s) == 14 and len(fc.taus_s) == 11
        assert fc.thetas_s[0] == 60.0 and fc.thetas_s[-1] <= 1120.0
        assert fc.taus_s[0] == 180.0 and fc.taus_s[-1] <= 1680.0
        # No dead start is always on the grid (the regulator's model).
        odd = dosing.FitConfig.from_dose(dataclasses.replace(CFG, fit_dead_start_grid_s=(90.0,),
                                                             fit_dead_start_prior=(1.0,)))
        assert odd.dead_starts_s == (0.0, 90.0)


# ---------------------------------------------------------------------------
# The pooled fit on synthetic nights
# ---------------------------------------------------------------------------

def _night(seed, *, hours=10.0, g=0.041, theta=4.0, tau=7.0, loss_h=0.30, loss_sd_h=0.08,
           loss_tau_h=2.0, wobble_sd=0.02, wobble_tau_min=25.0, quant=0.005, closed=True,
           two_node=None, target=18.0, extra=None):
    """A dosed night (arch/world.py in pure Python): a FOPDT room on
    open-minutes (the true gain, delay and spread in minutes), an AR(1)
    loss drift (2-h correlation), an AR(1) sensor wobble (25 min), 0.005
    steps, a reading a minute; pulses of 3-7 min fired when the reading is
    0.08 under the target and 25 min after the last (closed loop: the
    trigger correlates with the noise), or every 45-75 min (open loop).
    ``two_node`` (k_am, k_ma per minute): the air exchanges with a mass.
    ``extra(t_min)``: added to the room (people, sun).  Returns the
    reading times, the readings and the open intervals (s)."""
    rng = random.Random(seed)
    n = int(hours * 60)
    a_l = math.exp(-1.0 / (loss_tau_h * 60.0))
    a_w = math.exp(-1.0 / wobble_tau_min)
    x = rng.gauss(0.0, loss_sd_h)
    w = rng.gauss(0.0, wobble_sd) if wobble_sd > 0 else 0.0
    air = mass = target + 0.03
    line, emitter, intervals, room = [], 0.0, [], []
    open_until = None
    next_sched = 20.0 + rng.uniform(0.0, 30.0)
    for k in range(n):
        now = float(k)
        x = a_l * x + rng.gauss(0.0, loss_sd_h * math.sqrt(1 - a_l * a_l))
        if wobble_sd > 0:
            w = a_w * w + rng.gauss(0.0, wobble_sd * math.sqrt(1 - a_w * a_w))
        if open_until is None:
            if closed:
                fire = (k > 10 and room[-1] < target - 0.08
                        and (not intervals or now - intervals[-1][0] > 25.0))
            else:
                fire = now >= next_sched
            if fire:
                open_until = now + 3.0 + rng.uniform(0.0, 4.0)
                intervals.append((now, open_until))
                next_sched = now + rng.uniform(45.0, 75.0)
        u = 0.0
        if open_until is not None:
            u = max(0.0, min(1.0, open_until - now))
            if open_until <= now + 1.0:
                open_until = None
        if u > 0:
            line.append((now + theta, u))
        while line and line[0][0] <= now:
            emitter += line.pop(0)[1]
        arrived = emitter * (1.0 - math.exp(-1.0 / tau))
        emitter -= arrived
        air += g * arrived - (loss_h + x) / 60.0
        if two_node is not None:
            k_am, k_ma = two_node
            air += k_am * (mass - air)
            mass += k_ma * (air - mass)
        reading = air + w + (extra(now) if extra is not None else 0.0)
        room.append(round(reading / quant) * quant)
    return ([T0 + 60.0 * k for k in range(n)], room,
            [(T0 + 60.0 * a, T0 + 60.0 * b) for a, b in intervals])


def _record(start, open_min, landing, **kw):
    base = dict(start_ts=start, open_from=start + 60.0, open_until=start + 60.0 + 60.0 * open_min,
                open_min=open_min, landing_ts=landing, peak_error_c=0.0, model_error_c=0.0,
                gain_at_start=0.045, end_reason="covered", rode_burn=False, shortfall_start_c=0.05,
                at_floor=False, usable=True)
    base.update(kw)
    return dosing.PulseRecord(**base)


def _night_input(ts, room, intervals, *, anchor=0.041, cfg=FAST, pulses=10, seeded=True,
                 in_use=None):
    """The fit's input as the learner builds it: the readings through a
    ChainStore, the newest usable pulses (landing 15 min after the
    opening), every opening."""
    store = dosing.ChainStore(cfg)
    for t, y in zip(ts, room):
        store.note(t, y, open_from=None, open_until=None, hold_from=None, hold_until=None)
    end = ts[-1]
    recs = [_record(a - 60.0, (b - a) / 60.0, b + 900.0) for a, b in intervals
            if b + 900.0 <= end and a - 60.0 - cfg.segment_pre_s >= ts[0]][-pulses:]
    chains, _ = store.snapshot(recs, end)
    return dosing.FitInput(
        ts=end, generation=0, chains=chains, intervals=tuple(intervals), pulses=tuple(recs),
        anchor_gain=anchor, anchor_seeded=seeded,
        in_use=in_use or (anchor, 300.0, 480.0), cfg=dosing.FitConfig.from_dose(cfg))


class TestPooledFit:
    """Synthetic nights (design §11): true gain 0.041, delay 4, spread 7."""

    def _batch(self, anchor=0.041, seeds=range(16), **kw):
        out = []
        for seed in seeds:
            ts, room, ivs = _night(seed, **kw)
            out.append(dosing.pooled_fit(_night_input(ts, room, ivs, anchor=anchor)))
        return out

    def test_the_map_is_unbiased_and_the_uncertainty_calibrated(self):
        fits = self._batch()
        rel = [f.gain / 0.041 for f in fits]
        assert sum(0.85 <= r <= 1.15 for r in rel) >= 0.8 * len(rel), rel
        z = [math.log(f.gain / 0.041) / f.gain_sd_ln for f in fits]
        assert math.sqrt(sum(v * v for v in z) / len(z)) <= 1.1
        assert all(f.ok and f.n_pulses == 10 for f in fits)
        assert statistics.median(f.gain_sd_ln for f in fits) <= 0.2
        assert all(f.loss_c_per_h is not None for f in fits)

    @pytest.mark.parametrize("factor", [1.5, 1 / 1.5])
    def test_a_wrong_anchor_ends_in_the_same_band(self, factor):
        rel = [f.gain / 0.041 for f in self._batch(anchor=0.041 * factor)]
        assert sum(0.85 <= r <= 1.15 for r in rel) >= 0.75 * len(rel), rel

    def test_one_or_two_pulses_do_not_move_the_value_in_use(self):
        """One pulse never passes the gate; two leave the inflated sd over
        0.2 on most nights (the real night: 0.40, 0.29)."""
        one, two = [], []
        for seed in range(8):
            ts, room, ivs = _night(seed)
            one.append(dosing.pooled_fit(_night_input(ts, room, ivs, pulses=1)))
            two.append(dosing.pooled_fit(_night_input(ts, room, ivs, pulses=2)))
        for f in one:
            lr = dosing.DoseLearner(FAST)
            lr.seed(0.041 * 60.0)
            f = dataclasses.replace(f, generation=lr.generation)
            line = lr.apply_fit(f, f.ts)
            assert "too uncertain" in line or "needs 2 pulses" in line
            assert lr.gain_c_per_open_min == pytest.approx(0.041) and not lr.learned
        assert statistics.median(f.gain_sd_ln for f in two) > 0.2

    def test_a_slow_room_moves_tau_off_its_prior(self):
        """tau 24 min, 17-19 pulses: the prior (8 min) gives way to 14-18;
        the gain then reads low (bigger doses: the aim bias and the
        watchdog cover it)."""
        cfg = dataclasses.replace(FAST, fit_window_pulses=19)
        fits = []
        for seed in range(3):
            ts, room, ivs = _night(seed, hours=12.0, tau=24.0, theta=3.5, g=0.038)
            fits.append(dosing.pooled_fit(_night_input(ts, room, ivs, anchor=0.038, cfg=cfg,
                                                       pulses=19)))
        assert all(f.n_pulses >= 17 for f in fits)
        assert statistics.median(f.tau_s for f in fits) >= 14 * 60.0
        assert all(0.6 <= f.gain / 0.038 <= 1.15 for f in fits)

    def test_a_two_node_plant_reads_its_effective_gain(self):
        """Air exchanging with a mass (0.05 / 0.01 per minute): the sensed
        rise is the air's less what the mass takes, 0.6-0.9 of the air's
        gain, and the fit does not fail on a plant that is no FOPDT."""
        fits = self._batch(seeds=range(8), two_node=(0.05, 0.01))
        assert all(f.ok for f in fits)
        assert all(0.6 <= f.gain / 0.041 <= 0.9 for f in fits), [f.gain / 0.041 for f in fits]

    def test_the_dead_start_is_a_diagnostic_and_moves_nothing(self):
        """The triple in use is the same with or without the c0 grid: it
        comes from the posterior given no dead start, the model the
        regulator doses with."""
        ts, room, ivs = _night(3)
        fast = dosing.pooled_fit(_night_input(ts, room, ivs))
        full = dosing.pooled_fit(_night_input(ts, room, ivs, cfg=CFG))
        for name in ("gain", "theta_s", "tau_s", "gain_sd_ln", "sigma_c", "temper"):
            assert getattr(full, name) == pytest.approx(getattr(fast, name), rel=1e-9), name
        assert [c for c, _ in full.dead_start_posterior] == [0.0, 90.0, 180.0]
        assert sum(p for _, p in full.dead_start_posterior) == pytest.approx(1.0)
        assert fast.dead_start_posterior == ((0.0, 1.0),)

    def test_nothing_to_pool(self):
        fc = dosing.FitConfig.from_dose(FAST)
        base = dict(ts=T0, generation=3, intervals=(), pulses=(), anchor_gain=0.04,
                    anchor_seeded=True, in_use=(0.04, 300.0, 480.0), cfg=fc)
        f = dosing.pooled_fit(dosing.FitInput(chains=(), **base))
        assert not f.ok and f.why == "no readings to pool" and f.generation == 3
        short = dosing.Chain(T0, 120.0, (18.0,) * 5)
        assert not dosing.pooled_fit(dosing.FitInput(chains=(short,), **base)).ok
        long = dosing.Chain(T0, 120.0, (18.0,) * 60)
        f = dosing.pooled_fit(dosing.FitInput(chains=(long,), **base))
        assert not f.ok and f.why == "no pulse in the readings"


class TestRobustRejection:
    def test_a_people_bump_in_one_segment_is_dropped(self):
        """+0.3 °C over 20 min (and back) after one pulse: that segment
        goes, and the gain is the clean night's."""
        ts, room, ivs = _night(0, wobble_sd=0.0, loss_sd_h=0.04)
        clean = dosing.pooled_fit(_night_input(ts, room, ivs))
        assert clean.dropped == ()
        t_b = ivs[4][1] + 300.0

        def bump(t_min):
            x = 60.0 * t_min + T0 - t_b
            return 0.3 * x / 1200.0 if 0 <= x < 1200 else (0.3 * (2400 - x) / 1200.0 if 1200 <= x < 2400 else 0.0)

        ts, room, ivs = _night(0, wobble_sd=0.0, loss_sd_h=0.04, extra=bump)
        fit = dosing.pooled_fit(_night_input(ts, room, ivs))
        # The segments dropped are the ones the bump lies in (it spans the
        # end of one pulse's and the lead of the next).
        assert 1 <= len(fit.dropped) <= 2 and all("worse" in why for _, why in fit.dropped)
        for start, _ in fit.dropped:
            assert t_b - CFG.segment_post_s < start < t_b + 2400.0 + CFG.segment_pre_s
        assert fit.n_pulses == clean.n_pulses - len(fit.dropped)
        assert fit.gain == pytest.approx(clean.gain, rel=0.10)

    def test_a_segment_where_the_room_warmed_by_itself_is_dropped(self):
        """Sun for three hours (the room gaining 0.7 °C/h net), pulses on a
        schedule: the stiff baseline follows it, and a segment whose fitted
        baseline rises over 0.3 °C/h is heat from elsewhere."""
        def sun(t_min):
            x = t_min - 240.0
            return 0.0 if x < 0 else 1.0 * min(x, 180.0) / 60.0

        ts, room, ivs = _night(2, wobble_sd=0.0, loss_sd_h=0.02, closed=False, hours=12.0,
                               extra=sun)
        fit = dosing.pooled_fit(_night_input(ts, room, ivs))
        warmed = [t for t, why in fit.dropped if "warmed by itself" in why]
        assert warmed, fit.dropped
        assert all(T0 + 200 * 60 <= t <= T0 + 430 * 60 for t in warmed)


# ---------------------------------------------------------------------------
# The real night of 31 Oct - 1 Nov 2026
# ---------------------------------------------------------------------------

def _night_replay(cfg=FAST, *, refit=True):
    """The night through the doser as the entity drives it: a cycle every
    minute and at each command, the sensor's value held between its
    reports, the zone's heating as reported; a refit after each usable
    pulse is judged.  Returns the doser and each refit (time, fit, line,
    gain in use after)."""
    t0, target = NIGHT["t0"], NIGHT["target_c"]
    d = dosing.RoomDoser(cfg, "living")
    d.seed(NIGHT["anchor_rate_c_per_h"])
    cmds = {p["cmd_s"] for p in NIGHT["pulses"]}
    offs = {p["off_s"]: p["cmd_s"] for p in NIGHT["pulses"]}
    times = sorted({float(t) for t in range(0, int(NIGHT["end_s"]) + 1, 60)} | cmds | set(offs))
    fits = []
    for t in times:
        now = t0 + t
        room = _step_at(NIGHT["room"], t)
        pct = _step_at(NIGHT["heating"], t, 0.0)
        pct_ts = t0 + max(tt for tt, _ in NIGHT["heating"] if tt <= t)
        _inputs(d, now, setpoint_c=target, room_c=room, room_slope_c_per_s=None)
        result = _result()
        if t in cmds:
            d.note_command(now, 25.0, min_c=5.0, max_c=25.0, ok=True)
            result = _result(mode="dose", fired_by="release", need_c=0.1)
        elif t in offs:
            d.note_command(now, 5.0, min_c=5.0, max_c=25.0, ok=True)
            result = _result(dose_end="covered",
                             new_state=SimpleNamespace(last_pulse_start_ts=t0 + offs[t]))
        out = _after(d, now, result, setpoint_c=target, room_c=room, room_slope_c_per_s=None,
                     trv_internal_c=None, heating_pct=pct, heating_ts=pct_ts)
        if out.refit_due and refit:
            line = d.refit_now(now)
            fits.append((now, d.learner.last_fit, line, d.learner.gain_c_per_open_min))
    return d, fits


@pytest.fixture(scope="module")
def replay():
    """The real night replayed once for the module (FAST: the same triple)."""
    return _night_replay()


class TestRealNight:
    """Design §12.1: the living room, night of 1 Nov 2026 (the live learner
    went 0.044 -> 0.089 / spread 8 -> 25 min on six single-pulse fits)."""

    def test_one_chain_from_2330_and_every_pulse_pooled(self, replay):
        d, fits = replay
        chains = d.learner.chains.chains
        assert len(chains) == 1 and d.learner.chains.last_break is None
        assert chains[0].start_ts <= NIGHT["t0"] + NIGHT["pulses"][0]["cmd_s"] - CFG.segment_pre_s
        assert chains[0].end_ts >= NIGHT["t0"] + NIGHT["end_s"] - 2 * CFG.fit_step_s
        records = d.learner.records
        assert len(records) == 6 and all(r.usable for r in records), [r.why_not for r in records]
        assert [round(r.open_min, 1) for r in records] == [3.8, 4.1, 4.3, 4.0, 4.8, 6.8]
        assert len(fits) == 6

    def test_the_pooled_model_after_six_pulses(self, replay):
        """g 0.040-0.060, delay 3-6, spread 6-12 min, known to within the
        gate (+-20 %) at the sixth pulse.  The heat-up rate's 0.044 lies
        inside the fit's +-1 sd: the gain stays, the spread follows."""
        d, fits = replay
        fit = fits[-1][1]
        assert fit.n_pulses == 6
        assert 0.040 <= fit.gain <= 0.060
        assert 3 * 60 <= fit.theta_s <= 6 * 60 and 6 * 60 <= fit.tau_s <= 12 * 60
        assert fit.gain_sd_ln <= 0.2
        # The gate opens late in the night (1-2 pulses: no move).
        sds = [f.gain_sd_ln for _, f, _, _ in fits]
        assert sds[0] > 0.3 and sds[1] > 0.2 and all(sd > 0.2 for sd in sds[:5])
        assert [g for *_, g in fits] == [pytest.approx(2.62 / 60.0)] * 6
        line = fits[-1][2]
        assert "pooled over 6 pulses" in line and "the gain in use 0.044 within its band" in line
        assert d.learner.tau_s == pytest.approx(fit.tau_s) and "spread in use 8.0 →" in line
        assert d.learner.learned

    def test_the_live_values_are_rejected(self, replay):
        """The single-pulse values of that night (0.089 / 3 / 25 min): in
        the fit's own calibrated units -2 ln LR 8 (tempered, against the
        mode, in the regulator's model class), the gain 3 inflated sd away."""
        _, fits = replay
        fit = fits[-1][1]
        assert fit.in_use[0] == pytest.approx(2.62 / 60.0)
        d, _ = replay
        inp = d.learner.fit_input(NIGHT["t0"] + NIGHT["end_s"])
        live = NIGHT["live_in_use_after"]
        again = dosing.pooled_fit(dataclasses.replace(
            inp, in_use=(live["gain"], live["theta_s"], live["tau_s"])))
        assert again.rejected_hypothesis_dl >= 6.0
        assert math.log(live["gain"] / again.gain) / again.gain_sd_ln >= 2.5

    def test_leave_one_pulse_out(self, replay):
        d, _ = replay
        inp = d.learner.fit_input(NIGHT["t0"] + NIGHT["end_s"])
        gains = []
        for rec in inp.pulses:
            nxt = min((a for a, _ in inp.intervals if a > rec.open_from + 1.0), default=math.inf)
            seg = (rec.start_ts - CFG.segment_pre_s, min(nxt, rec.landing_ts + CFG.segment_post_s))
            fit = dosing.pooled_fit(dataclasses.replace(
                inp, chains=tuple(dosing._without(inp.chains, [seg])),
                pulses=tuple(p for p in inp.pulses if p is not rec)))
            gains.append(fit.gain)
        assert all(0.040 <= g <= 0.065 for g in gains), gains

    def test_the_dead_start_diagnostic_on_the_real_night(self):
        """With the c0 grid (production): the same triple; c0 90 s is
        favoured that night (pulse 6, 6.8 open-min, gave more per
        open-minute), which the line says and nothing acts on."""
        d, fits = _night_replay(CFG)
        fast_fit = _night_replay()[1][-1][1]
        fit = fits[-1][1]
        assert fit.gain == pytest.approx(fast_fit.gain, rel=1e-9)
        assert (fit.theta_s, fit.tau_s) == pytest.approx((fast_fit.theta_s, fast_fit.tau_s))
        p = dict(fit.dead_start_posterior)
        assert p[0.0] < 0.5 and fit.dead_start_s == 90.0
        assert "dead start 90 s favoured" in fits[-1][2]
        assert d.learner.gain_c_per_open_min == pytest.approx(2.62 / 60.0)


# ---------------------------------------------------------------------------
# The gate and the update
# ---------------------------------------------------------------------------

def _pfit(lr, gain, *, sd=0.15, theta=300.0, tau=480.0, n_pulses=6, ts=T0, ok=True,
          dropped=(), c0=((0.0, 1.0),), generation=None, why=None):
    return dosing.PooledFit(
        ts=ts, generation=lr.generation if generation is None else generation, ok=ok, why=why,
        gain=gain, theta_s=theta, tau_s=tau, gain_sd_ln=sd, n_pulses=n_pulses,
        span_s=6.4 * 3600.0, sigma_c=0.012, rho=0.8, temper=0.11, n=190, rms_c=0.011,
        dropped=dropped, dead_start_posterior=c0, anchor_gain=lr.anchor_gain_c_per_open_min)


def _seeded(rate=2.7):
    lr = dosing.DoseLearner(FAST)
    lr.seed(rate)                                          # anchor 0.045
    return lr


class TestApplyFit:
    def test_the_gate(self):
        lr = _seeded()
        line = lr.apply_fit(_pfit(lr, 0.06, sd=0.25), T0)
        assert "too uncertain (±28 %), gain in use stays 0.045" in line
        assert lr.gain_c_per_open_min == pytest.approx(0.045) and not lr.learned
        line = lr.apply_fit(_pfit(lr, 0.06, n_pulses=1, ts=T0 + 60), T0 + 60)
        assert "needs 2 pulses, gain in use stays 0.045" in line and not lr.learned
        assert lr.last_fit.gain == 0.06               # recorded whatever it says
        assert lr.summary()["pulses_pooled"] == 1 and lr.summary()["learning"].startswith("seeded")

    def test_the_dead_band(self):
        lr = _seeded()
        line = lr.apply_fit(_pfit(lr, 0.050), T0)      # ln(50/45) = 0.105 < 0.15
        assert line.endswith("the gain in use 0.045 within its band")
        assert lr.gain_c_per_open_min == pytest.approx(0.045) and lr.learned
        assert lr.summary()["learning"] == "pooled over 6 pulses"

    def test_up_at_once_down_at_most_15_percent_per_12_h(self):
        lr = _seeded()
        assert "gain in use 0.045 → 0.060" in lr.apply_fit(_pfit(lr, 0.060), T0)
        assert lr.gain_c_per_open_min == pytest.approx(0.060)
        lr.apply_fit(_pfit(lr, 0.030, ts=T0 + 3600), T0 + 3600)
        assert lr.gain_c_per_open_min == pytest.approx(0.051)          # 0.85 x 0.060
        line = lr.apply_fit(_pfit(lr, 0.030, ts=T0 + 7200), T0 + 7200)
        assert lr.gain_c_per_open_min == pytest.approx(0.051)
        assert "0.030 wanted, a step down possible from 21:00" in line   # T0 is 08:00 UTC
        lr.apply_fit(_pfit(lr, 0.030, ts=T0 + 13 * 3600), T0 + 13 * 3600)
        assert lr.gain_c_per_open_min == pytest.approx(0.85 * 0.051)
        assert lr.gain_c_per_open_min == pytest.approx(0.04335)
        lr.apply_fit(_pfit(lr, 0.037, ts=T0 + 26 * 3600), T0 + 26 * 3600)
        assert lr.gain_c_per_open_min == pytest.approx(0.037)          # the MAP, not 0.85 x
        # Up again at once, whatever the down timer says.
        lr.apply_fit(_pfit(lr, 0.050, ts=T0 + 27 * 3600), T0 + 27 * 3600)
        assert lr.gain_c_per_open_min == pytest.approx(0.050)

    def test_an_uncertain_fit_whose_range_is_below_still_steps_down(self):
        """2.17.0 review: a gain raised (one fit, the watchdog) stayed
        1.7x the fits' for days, because only an accepted fit could step it
        down.  A fit of 5 or more pulses that fails the gate but whose whole
        1-sd range lies below the gain in use takes the slow step (at most
        15 % per 12 h, never past the range's top); fewer pulses, or a range
        that reaches the gain in use, change nothing."""
        lr = _seeded()
        lr.apply_fit(_pfit(lr, 0.084), T0)                               # accepted: up
        assert lr.gain_c_per_open_min == pytest.approx(0.084)
        t = T0 + 13 * 3600
        line = lr.apply_fit(_pfit(lr, 0.0495, sd=0.32, ts=t), t)          # top 0.068
        assert "a step 0.084 → 0.071" in line
        assert lr.gain_c_per_open_min == pytest.approx(0.85 * 0.084)
        line = lr.apply_fit(_pfit(lr, 0.0495, sd=0.32, ts=t + 3600), t + 3600)
        assert "a step down possible from" in line
        assert lr.gain_c_per_open_min == pytest.approx(0.85 * 0.084)       # the 12-h limit
        t += 13 * 3600
        lr.apply_fit(_pfit(lr, 0.0495, sd=0.32, ts=t), t)
        assert lr.gain_c_per_open_min == pytest.approx(0.0495 * math.exp(0.32))   # the top
        t += 13 * 3600
        before = lr.gain_c_per_open_min
        lr.apply_fit(_pfit(lr, 0.0495, sd=0.32, ts=t), t)                 # range reaches it
        assert lr.gain_c_per_open_min == pytest.approx(before)
        few = _seeded()
        few.apply_fit(_pfit(few, 0.084), T0)
        few.apply_fit(_pfit(few, 0.03, sd=0.4, n_pulses=4, ts=T0 + 13 * 3600), T0 + 13 * 3600)
        assert few.gain_c_per_open_min == pytest.approx(0.084)

    def test_an_owned_gain_follows_the_anchors_bounds_and_says_so(self):
        """Final review n1/n2: an owned gain is kept within the bounds of a
        new anchor at once (not only at the next reload), and the sensor says
        where it came from; n4: at the lower bound, no "step" is taken."""
        lr = _seeded()                                   # anchor 0.045
        lr.gain_c_per_open_min, lr._gain_owned = 0.09, True
        assert lr.summary()["learning"] == "seeded, then moved by the room's landings"
        lr.seed(1.0)                                     # anchor 0.0167, upper bound 0.05
        assert lr.gain_c_per_open_min == pytest.approx(3.0 * 1.0 / 60.0)
        low = _seeded()
        low.gain_c_per_open_min, low._gain_owned = 0.3 * 0.045, True       # the lower bound
        line = low.apply_fit(_pfit(low, 0.004, sd=0.3), T0)
        assert "a step" not in line and "gain in use stays" in line
        assert "gain" not in low._last_more

    def test_a_step_down_on_an_uncertain_fit_survives_the_anchor(self):
        """Before any fit is accepted, a step down taken on an uncertain fit
        is the room's evidence: the next cycle's seed keeps it."""
        lr = _seeded()
        lr.gain_c_per_open_min = 0.09                    # e.g. the watchdog
        lr._gain_owned = True
        lr.apply_fit(_pfit(lr, 0.04, sd=0.3, ts=T0), T0)
        assert not lr.learned and lr.gain_c_per_open_min == pytest.approx(0.0765)
        lr.seed(2.7)
        assert lr.gain_c_per_open_min == pytest.approx(0.0765)

    def test_a_fit_after_the_clock_went_back_is_applied(self):
        """One refit at a time: results never come back out of order, so a
        fit whose time is before the last one's (the clock went back) is
        applied, not discarded (2.17.0 review)."""
        lr = _seeded()
        lr.apply_fit(_pfit(lr, 0.045, ts=T0 + 3600), T0 + 3600)
        line = lr.apply_fit(_pfit(lr, 0.07, ts=T0), T0)
        assert line is not None and lr.gain_c_per_open_min == pytest.approx(0.07)

    def test_the_bounds(self):
        lr = _seeded()
        lr.apply_fit(_pfit(lr, 0.5), T0)
        assert lr.gain_c_per_open_min == pytest.approx(3.0 * 0.045)
        lr.apply_fit(_pfit(lr, 0.6, theta=30.0, tau=4000.0, ts=T0 + 60), T0 + 60)
        assert (lr.theta_s, lr.tau_s) == (CFG.theta_bounds_s[0], CFG.tau_bounds_s[1])

    def test_delay_and_spread_follow_the_map_outside_a_minute(self):
        lr = _seeded()
        lr.apply_fit(_pfit(lr, 0.045, theta=340.0, tau=520.0), T0)
        assert (lr.theta_s, lr.tau_s) == (300.0, 480.0)                 # under a minute
        line = lr.apply_fit(_pfit(lr, 0.045, theta=420.0, tau=360.0, ts=T0 + 60), T0 + 60)
        assert (lr.theta_s, lr.tau_s) == (420.0, 360.0)
        assert "delay in use 5.0 → 7.0 min" in line and "spread in use 8.0 → 6.0 min" in line
        before = (lr.theta_s, lr.tau_s)
        lr.apply_fit(_pfit(lr, 0.045, theta=900.0, tau=900.0, sd=0.3, ts=T0 + 120), T0 + 120)
        assert (lr.theta_s, lr.tau_s) == before                        # not past the gate

    def test_the_dead_start_and_dropped_segments_are_logged_only(self):
        lr = _seeded()
        lr._records.append(_record(T0 - 7200.0, 4.0, T0 - 6000.0))
        fit = _pfit(lr, 0.045, c0=((0.0, 0.3), (90.0, 0.67), (180.0, 0.03)),
                    dropped=((T0 - 7200.0, "fits 2.8x worse"),))
        line = lr.apply_fit(fit, T0)
        assert "dead start 90 s favoured (0.67)" in line
        assert "dropped 06:00 (fits 2.8x worse)" in line
        assert lr.gain_c_per_open_min == pytest.approx(0.045)
        assert lr.records[-1].dropped_by_fit == "fits 2.8x worse"
        lr.apply_fit(_pfit(lr, 0.045, ts=T0 + 60), T0 + 60)
        assert lr.records[-1].dropped_by_fit is None

    def test_a_fit_for_another_generation_is_discarded(self):
        lr = _seeded()
        old = _pfit(lr, 0.07)
        lr.reset()
        assert lr.apply_fit(old, T0) is None
        assert lr.gain_c_per_open_min == pytest.approx(0.045) and lr.last_fit is None

    def test_a_malformed_fit_never_raises(self):
        lr = _seeded()
        for bad in (_pfit(lr, math.nan), _pfit(lr, -0.02), _pfit(lr, 0.05, sd=math.inf),
                    _pfit(lr, 0.05, ok=False, why="no heat in the readings"),
                    _pfit(lr, 0.05, dropped="x"), _pfit(lr, 0.05, c0=((0.0, None),)),
                    dataclasses.replace(_pfit(lr, 0.05), n_pulses=2.5)):
            line = lr.apply_fit(bad, T0)
            assert line.startswith("no pooled fit (") and "stays 0.045" in line
        assert lr.apply_fit("not a fit", T0) is None
        assert lr.gain_c_per_open_min == pytest.approx(0.045) and not lr.learned

    def test_noisy_posteriors_do_not_ratchet(self):
        """arch/exp3_update.py's failure as a regression: a refit an hour,
        each MAP scattered (sd 0.08 in ln) around 0.85 x the truth with an
        sd of 0.15.  2.16.0's rule applied to them (up half way at once, down
        15 % per 12 h) ends above 0.95 x; the dead band tracks the
        posterior."""
        truth = 0.045
        rng = random.Random(1)
        maps = [0.85 * truth * math.exp(rng.gauss(0.0, 0.08)) for _ in range(48)]
        lr = _seeded()
        for k, g in enumerate(maps):
            ts = T0 + 3600.0 * k
            lr.apply_fit(_pfit(lr, g, ts=ts), ts)
        assert lr.gain_c_per_open_min / truth <= 0.95
        old, last_down = truth, -math.inf
        for k, g in enumerate(maps):
            ts = 3600.0 * k
            if g > old:
                old += 0.5 * (g - old)
            elif ts - last_down >= 43200.0:
                old += 0.15 * (g - old)
                last_down = ts
        assert old / truth > 0.95


# ---------------------------------------------------------------------------
# The chains of readings
# ---------------------------------------------------------------------------

def _feed(store, start, minutes, *, room=18.0, slope_h=-0.3, step=60.0, **kw):
    for k in range(int(minutes * 60 / step)):
        t = start + k * step
        store.note(t, room + slope_h * (t - start) * PER_H, open_from=None, open_until=None,
                   hold_from=kw.get("hold_from"), hold_until=kw.get("hold_until"),
                   window=kw.get("window", False), boost=kw.get("boost", False),
                   paused=kw.get("paused", False))
    return start + minutes * 60.0


def _same_chains(a, b):
    """Chains equal to the 0.001 °C a save keeps."""
    assert len(a) == len(b)
    for x, y in zip(a, b):
        assert (x.start_ts, x.step_s, len(x.rooms)) == (y.start_ts, y.step_s, len(y.rooms))
        assert x.rooms == pytest.approx(y.rooms, abs=6e-4)


class TestChainStore:
    def test_bins_are_the_mean_of_their_readings_and_a_short_gap_is_filled(self):
        s = dosing.ChainStore(CFG)
        for t, y in ((0, 18.0), (30, 18.1), (90, 18.2), (130, 18.4), (600, 18.6), (725, 18.8)):
            s.note(T0 + t, y, open_from=None, open_until=None, hold_from=None, hold_until=None)
        [ch] = s.chains
        assert ch.start_ts == T0 and ch.step_s == 120.0
        # bins 0 (0-120 s), 1 (120-240), then 240-600 empty, filled linearly
        # from bin 1's mean to the reading at 600 s.
        assert ch.rooms[:2] == pytest.approx((18.1, 18.4))
        assert ch.rooms[2:5] == pytest.approx((18.45, 18.5, 18.55))
        assert ch.rooms[5] == pytest.approx(18.6)        # closed by the reading at 725 s
        assert s.covers(T0, T0 + 725)

    @pytest.mark.parametrize("cause, settle", [
        ("window", CFG.chain_settle_after_window_s),
        ("boost", CFG.chain_settle_after_unbooked_s),
        ("paused", 0.0),
    ])
    def test_breaks_and_their_settle(self, cause, settle):
        s = dosing.ChainStore(CFG)
        t = _feed(s, T0, 60)
        t = _feed(s, t, 5, **{cause: True})
        last_flagged = t - 60.0
        assert s.last_break[1] in ("a window", "Boost", "dosing was paused")
        assert len(s.chains) == 1 and s.chains[0].end_ts <= t - 5 * 60
        t = _feed(s, t, 90)
        [first, second] = s.chains
        expected = last_flagged + max(settle, 60.0)
        assert expected <= second.start_ts < expected + 60.0

    def test_heat_booked_nowhere_breaks_it_from_its_start_and_settles_an_hour(self):
        s = dosing.ChainStore(CFG)
        t = _feed(s, T0, 60)
        t = _feed(s, t, 30, hold_from=t)                       # a hold for 30 min
        end = t
        t = _feed(s, t, 90, hold_from=end - 1800, hold_until=end + 90)
        [first, second] = s.chains
        assert first.end_ts <= end - 1800.0
        assert second.start_ts >= end + 90 + CFG.chain_settle_after_unbooked_s - 1.0
        assert "booked nowhere" in s.why_not_covered(end)
        assert s.unbooked_between(end - 600, end) and not s.unbooked_between(T0, T0 + 600)

    def test_a_gap_a_bridged_reading_an_interruption_a_clock_going_back(self):
        s = dosing.ChainStore(CFG)
        t = _feed(s, T0, 30)
        t = _feed(s, t + 700, 30)                              # 12.7 min without a reading
        assert len(s.chains) == 2 and s.last_break[1] == "no reading for 13 min"
        s.note(t, None, open_from=None, open_until=None, hold_from=None, hold_until=None)
        assert s.last_break[1] == "a bridged reading" and s.filling_since is None
        t = _feed(s, t + 60, 30)
        s.interrupt(t, "Roomstat was switched off")
        assert s.filling_since is None
        t = _feed(s, t + 60, 30)
        before = len(s.chains)
        s.note(t - 3600, 18.0, open_from=None, open_until=None, hold_from=None, hold_until=None)
        # The chain being filled ran past the new time: its bins from then
        # on are dropped (here all), and nothing starts before the old time.
        assert s.last_break[1] == "the clock went back" and len(s.chains) == before - 1
        assert s.filling_since is None
        s.note(t - 3540, 18.0, open_from=None, open_until=None, hold_from=None, hold_until=None)
        assert s.filling_since is None                         # nothing before the last time seen
        s.note(t + 60, 18.0, open_from=None, open_until=None, hold_from=None, hold_until=None)
        assert s.filling_since == t + 60

    def test_a_resumed_pulse_counts_as_heat_booked_nowhere(self):
        d = dosing.RoomDoser(FAST, "a")
        for k in range(30):
            _inputs(d, T0 + 60 * k)
            _after(d, T0 + 60 * k, _result())
        assert d.learner.chains.filling_since is not None
        d.resume_open(T0 + 1800)
        _inputs(d, T0 + 1860)
        _after(d, T0 + 1860, _result(mode="dose"))
        assert d.learner.chains.filling_since is None
        assert "resumed without its booking" in d.learner.chains.last_break[1]
        d.note_command(T0 + 2000, 5.0, min_c=5.0, max_c=25.0, ok=True)
        for k in range(3, 120):
            _inputs(d, T0 + 1800 + 60 * k)
            _after(d, T0 + 1800 + 60 * k, _result())
        marks = d.learner.chains.marks
        assert marks[-1][1] == T0 + 2000 + CFG.close_delay_s       # the mark ended with the booking
        assert d.learner.chains.filling_since >= marks[-1][1] + CFG.chain_settle_after_unbooked_s

    def test_pruning_keeps_the_window_and_trims_to_the_segments(self):
        lr = dosing.DoseLearner(FAST)
        store = lr.chains
        t = _feed(store, T0, 240)
        recs = [_record(T0 + 3600, 4.0, T0 + 4500), _record(T0 + 7200, 4.0, T0 + 8100)]
        lr._records.extend(recs)
        store.interrupt(t, "summer mode began")
        lr._prune(t + 60)
        [ch] = store.chains
        assert ch.start_ts <= T0 + 3600 - CFG.segment_pre_s < ch.start_ts + 120
        assert ch.end_ts == pytest.approx(T0 + 8100 + CFG.segment_post_s, abs=120)
        # A chain holding no kept pulse goes; the one being filled keeps only
        # the last segment_pre_s.
        lr._records.clear()
        t = _feed(store, t + 120, 120)
        lr._prune(t)
        assert store.chains[0].start_ts >= t - CFG.segment_pre_s - 3 * 120
        assert len(store.chains) == 1

    def test_the_window_is_ten_pulses_and_48_h(self):
        lr = dosing.DoseLearner(FAST)
        lr._records.extend(_record(T0 + 3600 * k, 4.0, T0 + 3600 * k + 900) for k in range(14))
        assert [r.start_ts for r in lr._window(T0 + 14 * 3600)] == [T0 + 3600 * k for k in range(4, 14)]
        assert len(lr._window(T0 + 60 * 3600)) == 2           # 48 h back
        lr._prune(T0 + 60 * 3600)
        assert len(lr.records) == 2

    def test_round_trip_and_restore_rules(self):
        s = dosing.ChainStore(CFG)
        t = _feed(s, T0, 50)
        s.interrupt(t, "summer mode began")
        t = _feed(s, t + 120, 40)
        s.note(t, 18.0, open_from=t, open_until=None, hold_from=None, hold_until=None)
        data = json.loads(json.dumps(s.to_dict()))
        back = dosing.ChainStore(CFG)
        back.restore(data, t + 60)
        _same_chains(back.chains, s.chains)
        assert back.filling_since == s.filling_since and back.intervals == s.intervals
        assert back.last_break == s.last_break
        back.note(t + 120, 18.0, open_from=None, open_until=None, hold_from=None, hold_until=None)
        assert back.filling_since == s.filling_since           # a reload is a short gap
        s.mark_unusable(t - 300, t + 600, "a pulse resumed without its booking")
        marked = dosing.ChainStore(CFG)
        marked.restore(json.loads(json.dumps(s.to_dict())), t + 60)
        assert marked.marks == s.marks
        late = dosing.ChainStore(CFG)
        late.restore(data, t + 3600)
        assert late.filling_since is None and len(late.chains) == 2
        assert late.last_break[1] == "a restart"
        future = dosing.ChainStore(CFG)
        future.restore(data, T0 + 600)                         # the save is from the future
        assert future.chains == () and future.intervals == []
        old = dosing.ChainStore(CFG)
        old.restore(data, t + CFG.fit_window_s + 7200)
        assert old.chains == ()
        junk = dosing.ChainStore(CFG)
        junk.restore({"chains": [{"start_ts": "x"}], "filling": {"rooms": "y"},
                      "intervals": [[None, 1]], "holds": "z"}, t)
        assert junk.chains == () and junk.intervals == []


# ---------------------------------------------------------------------------
# The landings
# ---------------------------------------------------------------------------

def _run_episode(lr, *, gain=0.04, fall_h=0.4, pulse_s=180.0, setpoint=20.0, room0=19.95,
                 window_at=None, heating=100.0, people_after=None, end_s=3600.0,
                 start=T0, theta=300.0, tau=480.0, end_reason="covered", pre_s=1500.0):
    """One synthetic pulse through the learner, sampled every minute (from
    ``pre_s`` before it, so its segment is in one chain), ended for
    ``end_reason`` (the regulator's dose_end).  Returns the verdict."""
    open_from, open_until = start + 60.0, start + pulse_s + 90.0
    for t in range(-int(pre_s), 0, 60):
        lr.note_cycle(start + t, room0 - fall_h * PER_H * t, heating_pct=None, heating_ts=None,
                      open_from=None, open_until=None)
    ts, room, _ = _synthetic(gain=gain, theta=theta, tau=tau, fall_h=fall_h, open_from=60.0,
                             open_until=pulse_s + 90.0, dt=60.0, end=end_s, room0=room0)
    if people_after is not None:
        room = [y + (0.8 * PER_H * (t - people_after) if t > people_after else 0.0)
                for t, y in zip(ts, room)]
    lr.start_episode(start, setpoint, room0, fall_h * PER_H, 2.0, need_start_c=0.1)
    for t, y in zip(ts[1:], room[1:]):
        now = start + t
        lr.note_cycle(now, y, heating_pct=heating, heating_ts=start + 70.0,
                      open_from=open_from, open_until=open_until if t >= pulse_s else None)
        if t >= pulse_s:
            lr.note_pulse_end(start + pulse_s)
            lr.note_end_reason(start, end_reason)
        verdicts = lr.observe(now, setpoint, window=window_at is not None and t >= window_at, boost=False)
        if verdicts:
            assert len(verdicts) == 1
            return verdicts[0]
    return None


class TestLearner:
    def test_a_clean_episode_is_judged_and_pooled(self):
        lr = dosing.DoseLearner(CFG)
        verdict = _run_episode(lr)
        assert verdict.kind == dosing.ACCEPTED
        rec = lr.records[-1]
        assert rec.usable and rec.why_not is None and rec.open_min == pytest.approx(3.5)
        assert rec.at_floor and rec.peak_error_c is not None
        assert "open-min" in verdict.line and "at the peak" in verdict.line
        assert "(model " in verdict.line and "; pooled; aim" in verdict.line
        assert lr.refit_due and lr.last_line == verdict.line and not lr.learned
        inp = lr.fit_input(T0 + 3600)
        assert not lr.refit_due and inp.pulses == (rec,) and len(inp.chains) == 1

    def test_too_few_readings_before_it_is_not_pooled(self):
        lr = dosing.DoseLearner(CFG)
        verdict = _run_episode(lr, pre_s=600.0)
        assert verdict.kind == dosing.ACCEPTED            # its landing still counts
        assert "not pooled: too few readings before it" in verdict.line
        assert not lr.records[-1].usable and not lr.refit_due

    def test_a_window_drops_it(self):
        lr = dosing.DoseLearner(CFG)
        verdict = _run_episode(lr, window_at=600.0)
        assert verdict.kind == dosing.DROPPED and "window" in verdict.line
        assert lr.episode is None and lr.records == ()

    def test_a_pulse_that_never_opened_the_valve(self):
        lr = dosing.DoseLearner(CFG)
        verdict = _run_episode(lr, heating=0.0)
        assert verdict.kind == dosing.NOT_OPENED
        assert "did nothing" in verdict.line
        assert lr.episode is None and lr.records == () and not lr.refit_due

    def test_no_heating_sensor_cannot_tell_so_it_is_judged(self):
        lr = dosing.DoseLearner(CFG)
        verdict = _run_episode(lr, heating=None)
        assert verdict.kind == dosing.ACCEPTED and lr.records[-1].usable

    def test_people_after_the_landing_are_not_counted(self):
        clean = dosing.DoseLearner(CFG)
        _run_episode(clean)
        busy = dosing.DoseLearner(CFG)
        verdict = _run_episode(busy, people_after=2400.0)
        assert verdict.kind == dosing.ACCEPTED
        assert busy.records[-1] == clean.records[-1]

    def test_a_target_change_voids_the_landing_but_the_pulse_is_pooled(self):
        """Its heat does not depend on the target: pooled; its landing says
        nothing about the aim: no bias, no watchdog."""
        lr = dosing.DoseLearner(CFG)
        for t in range(-1500, 0, 60):
            lr.note_cycle(T0 + t, 19.9, heating_pct=None, heating_ts=None, open_from=None,
                          open_until=None)
        lr.start_episode(T0, 20.0, 19.9, 0.0, None)
        verdicts = []
        for k in range(1, 70):
            now = T0 + 60 * k
            lr.note_cycle(now, 19.9 + (0.05 if k > 10 else 0.0), heating_pct=None, heating_ts=None,
                          open_from=T0 + 60, open_until=T0 + 270 if k >= 3 else None)
            if k >= 3:
                lr.note_pulse_end(T0 + 180)
            verdicts += lr.observe(now, 21.0 if k >= 2 else 20.0, window=False, boost=False)
        [verdict] = verdicts
        assert verdict.kind == dosing.POOLED
        assert "its landing not judged: the target changed" in verdict.line
        rec = lr.records[-1]
        assert rec.usable and rec.peak_error_c is None and lr.bias_c == 0.0 and lr.refit_due

    def test_judged_once_the_heat_has_landed_even_without_a_peak(self):
        """No rise at all: landed theta + 4 tau after the pulse ended (here
        3 + 5 + 32 = 40 min), judged 5 min later."""
        lr = dosing.DoseLearner(CFG)
        lr.start_episode(T0, 20.0, 19.9, 0.0, None)
        verdicts = {}
        for k in range(1, 62):
            now = T0 + 60 * k
            lr.note_cycle(now, 19.9, heating_pct=None, heating_ts=None,
                          open_from=T0 + 60, open_until=T0 + 270 if k >= 3 else None)
            if k >= 3:
                lr.note_pulse_end(T0 + 180)
            for v in lr.observe(now, 20.0, window=False, boost=False):
                verdicts[k] = v
        assert list(verdicts) == [45]
        assert verdicts[45].kind == dosing.ACCEPTED and "-0.10 at the peak" in verdicts[45].line

    def test_a_small_pulse_that_only_slows_the_fall_is_judged(self):
        """Against a 0.6 °C/h fall a 3-min pulse never lifts the room: its
        landing shows only against the trend."""
        lr = dosing.DoseLearner(CFG)
        verdict = _run_episode(lr, gain=0.03, fall_h=0.6)
        assert verdict.kind == dosing.ACCEPTED and lr.records[-1].usable
        assert lr.records[-1].peak_error_c < -0.05


class TestLandings:
    """The aim bias and the watchdog: 2.16.0's rules, fed by landings,
    which are judged against the dosing zone's aim (2.17.0)."""

    @pytest.mark.parametrize("zone_above, aim", [(0.2, 0.1), (0.1, 0.0), (0.35, 0.25)])
    def test_the_bias_learns_from_landings_not_from_the_floor(self, zone_above, aim):
        cfg = dataclasses.replace(CFG, zone_above_c=zone_above)
        assert cfg.aim_over_c == pytest.approx(aim)
        lr = dosing.DoseLearner(cfg)
        _run_episode(lr, gain=0.03, pulse_s=300.0, fall_h=0.4, room0=19.9)
        rec = lr.records[-1]
        assert not rec.at_floor and rec.peak_error_c < 0
        assert lr.bias_c == pytest.approx(0.3 * (aim - rec.peak_error_c))
        floor_hot = dosing.DoseLearner(CFG)
        _run_episode(floor_hot, gain=0.04, pulse_s=180.0, fall_h=0.0, room0=20.0)
        assert floor_hot.records[-1].peak_error_c > 0
        assert floor_hot.bias_c == 0.0

    def test_the_bias_is_clamped(self):
        lr = dosing.DoseLearner(CFG)
        for k in range(8):
            _run_episode(lr, gain=0.03, pulse_s=300.0, fall_h=0.4, room0=19.8, start=T0 + k * 7200)
        assert 0.0 < lr.bias_c <= CFG.bias_clamp_c + 1e-9

    def test_short_landings_its_dose_did_not_set_leave_the_bias(self):
        """A landing short moves the aim towards bigger doses only when the
        pulse's own dose ended it and it fired near the target (house
        simulation, 31 Oct 2026: the bias wound up to +0.25)."""
        for kwargs in ({"end_reason": "max"}, {"end_reason": "stop_rule"}, {"end_reason": None},
                       {"room0": 19.5}):
            lr = dosing.DoseLearner(CFG)
            _run_episode(lr, gain=0.03, pulse_s=300.0, fall_h=0.4, **{"room0": 19.9, **kwargs})
            assert lr.records[-1].peak_error_c < 0, kwargs
            assert lr.bias_c == 0.0, kwargs
        hot = dosing.DoseLearner(CFG)
        _run_episode(hot, gain=0.06, pulse_s=840.0, fall_h=0.2, room0=19.5, end_reason="max")
        assert hot.records[-1].peak_error_c > CFG.aim_over_c
        assert hot.bias_c < 0.0

    def test_a_landing_on_the_aim_teaches_nothing(self):
        """With the zone's aim over the target (2.17.0), a landing there is
        on the mark: no bias; the same landing with the aim on the target
        (2.16.0's band) moves it down."""
        lr = dosing.DoseLearner(CFG)
        _run_episode(lr, gain=0.045, pulse_s=330.0, fall_h=0.2, room0=19.95)
        peak = lr.records[-1].peak_error_c
        assert peak == pytest.approx(CFG.aim_over_c, abs=0.02)
        assert lr.bias_c == pytest.approx(0.3 * (CFG.aim_over_c - peak)) and abs(lr.bias_c) <= 0.006
        centred = dosing.DoseLearner(dataclasses.replace(CFG, zone_above_c=0.1))
        _run_episode(centred, gain=0.045, pulse_s=330.0, fall_h=0.2, room0=19.95)
        assert centred.bias_c == pytest.approx(-0.3 * peak) and centred.bias_c < -0.02

    def test_the_watchdogs_cut_survives_the_anchor_and_a_reload(self):
        """2.17.0 review (M1): two hot landings before any pooled fit was
        accepted: the gain x 1.5 used to be undone by the next cycle's
        seed().  It stands, through a save and restore too."""
        lr = dosing.DoseLearner(CFG)
        lr.seed(2.62)
        g0 = lr.gain_c_per_open_min
        for k in range(2):
            rec = dosing.PulseRecord(
                start_ts=T0 + k * 1800, open_from=T0 + k * 1800 + 60,
                open_until=T0 + k * 1800 + 330, open_min=4.5, landing_ts=T0 + k * 1800 + 1200,
                peak_error_c=0.45, model_error_c=0.1, gain_at_start=g0, end_reason="covered",
                rode_burn=False, shortfall_start_c=0.1, at_floor=False, usable=True)
            lr._records.append(rec)
            lr._learn_landing(rec)
        assert lr._watchdog_fired and not lr.learned
        assert lr.gain_c_per_open_min == pytest.approx(1.5 * g0)
        lr.seed(2.62)
        assert lr.gain_c_per_open_min == pytest.approx(1.5 * g0)
        back = dosing.DoseLearner(CFG)
        back.restore(lr.to_dict(), T0 + 3600)
        back.seed(2.62)
        assert back.gain_c_per_open_min == pytest.approx(1.5 * g0) and not back.learned
        back.reset()
        back.seed(2.62)
        assert back.gain_c_per_open_min == pytest.approx(g0)

    def test_the_shortfall_line_follows_the_zone_below(self):
        """2.17.0 review (m3): with a wide zone below every pulse starts
        further short; the aim bias may still move up from 0.2 under the
        zone's bottom."""
        assert CFG.bias_shortfall_line_c == pytest.approx(0.3)
        wide = dataclasses.replace(CFG, zone_below_c=0.4)
        assert wide.bias_shortfall_line_c == pytest.approx(0.6)
        lr = dosing.DoseLearner(wide)
        _run_episode(lr, gain=0.03, pulse_s=300.0, fall_h=0.4, room0=19.55)
        rec = lr.records[-1]
        assert rec.shortfall_start_c == pytest.approx(0.45) and rec.peak_error_c < wide.aim_over_c
        assert lr.bias_c > 0.0
        narrow = dosing.DoseLearner(CFG)
        _run_episode(narrow, gain=0.03, pulse_s=300.0, fall_h=0.4, room0=19.55)
        assert narrow.bias_c == 0.0

    def test_the_watchdog_line_follows_the_zone(self):
        assert CFG.watchdog_line_c == pytest.approx(0.3)
        assert dataclasses.replace(CFG, zone_above_c=0.1).watchdog_line_c == pytest.approx(0.3)
        assert dataclasses.replace(CFG, zone_above_c=0.45).watchdog_line_c == pytest.approx(0.55)

    def test_the_end_reason_rides_along(self):
        lr = dosing.DoseLearner(CFG)
        lr.start_episode(T0, 20.0, 19.9, 0.0, None)
        lr.note_end_reason(T0 + 0.4, "covered")
        lr.note_end_reason(T0, "max")                 # the first one stands
        back = dosing.DoseLearner(CFG)
        back.restore(lr.to_dict(), T0 + 60)
        assert back._episodes[-1].end_reason == "covered"

    def test_two_hot_landings_cut_the_doses(self):
        lr = dosing.DoseLearner(CFG)
        _run_episode(lr, gain=0.1, fall_h=0.0, room0=20.0)
        assert lr.records[-1].peak_error_c > 0.3
        after_first = lr.gain_c_per_open_min
        verdict = _run_episode(lr, gain=0.1, fall_h=0.0, room0=20.0, start=T0 + 7200)
        assert "two hot landings" in verdict.line
        assert lr.bias_c == pytest.approx(-0.1)
        assert lr.gain_c_per_open_min == pytest.approx(1.5 * after_first)
        assert lr.gain_c_per_open_min <= 3.0 * 0.045 + 1e-9

    def test_seeding_from_the_auto_tuner_is_the_anchor(self):
        """The tuner's heating rate anchors the gain (the prior's centre, and
        the gain in use while nothing is pooled); its dead time and coast come
        from cold-radiator heat-ups and seed nothing."""
        lr = dosing.DoseLearner(CFG)
        lr.seed(2.52)                                          # the living room
        assert lr.gain_c_per_open_min == pytest.approx(0.042)
        assert (lr.theta_s, lr.tau_s) == (CFG.default_theta_s, CFG.default_tau_s)
        assert lr.summary()["anchor_gain_c_per_open_min"] == pytest.approx(0.042)
        lr.seed(None)
        assert (lr.gain_c_per_open_min, lr.theta_s, lr.tau_s) == (0.045, 300.0, 480.0)
        for junk in (0.0, -1.0, math.nan, math.inf):
            lr.seed(junk)
            assert lr.gain_c_per_open_min == 0.045

    def test_seeding_does_not_override_what_was_pooled(self):
        lr = _seeded()
        lr.apply_fit(_pfit(lr, 0.060, theta=420.0), T0)
        learned = (lr.gain_c_per_open_min, lr.theta_s, lr.tau_s)
        lr.seed(3.0)
        assert (lr.gain_c_per_open_min, lr.theta_s, lr.tau_s) == learned
        assert lr.anchor_gain_c_per_open_min == pytest.approx(0.05)
        lr.reset()
        assert lr.gain_c_per_open_min == pytest.approx(0.05)
        assert (lr.theta_s, lr.tau_s) == (CFG.default_theta_s, CFG.default_tau_s)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_round_trip_with_chains_pulses_and_the_last_fit(self):
        d, fits = _night_replay()
        lr = d.learner
        end = NIGHT["t0"] + NIGHT["end_s"]
        data = json.loads(json.dumps(lr.to_dict()))
        assert data["v"] == 2
        back = dosing.DoseLearner(FAST)
        back.restore(data, end + 60)
        assert back.gain_c_per_open_min == pytest.approx(lr.gain_c_per_open_min)
        assert (back.theta_s, back.tau_s, back.bias_c) == (lr.theta_s, lr.tau_s, lr.bias_c)
        assert back.learned and back.summary() == lr.summary()
        assert [r.to_dict() for r in back.records] == [r.to_dict() for r in lr.records]
        _same_chains(back.chains.chains, lr.chains.chains)
        assert back.last_fit.to_dict() == lr.last_fit.to_dict()
        # The same data (to the 0.001 °C saved), the same fit.
        a = dosing.pooled_fit(lr.fit_input(end + 60))
        b = dosing.pooled_fit(back.fit_input(end + 60))
        assert a.gain == pytest.approx(b.gain, rel=5e-3)
        assert not back.refit_due
        lr.refit_due = True
        due = dosing.DoseLearner(FAST)
        due.restore(json.loads(json.dumps(lr.to_dict())), end + 60)
        assert due.refit_due                      # a refit lost to a reload comes again
        junk = dosing.DoseLearner(FAST)
        junk.restore({"v": 2, "pulses": [{"start_ts": "x"}], "gain": "y", "store": 3,
                      "last_fit": {"ts": None}, "episodes": [{"start_ts": None}]}, end)
        assert not junk.learned and junk.records == () and junk.last_fit is None

    def test_a_2_16_save_is_not_taken_back_and_says_so(self):
        """Its single-pulse values were learned on noise: the gain starts
        from the anchor, the bias from 0; the open episodes, the booked
        openings and the hold marks are kept."""
        old = {
            "gain": 0.0887, "theta_s": 180.0, "tau_s": 1512.0, "bias_c": -0.02,
            "accepted_total": 6, "seed_args": [2.62], "judged": [], "last_more": {},
            "episodes": [dosing.Episode(start_ts=T0 - 120, setpoint_c=18.0, room_start_c=17.95,
                                        fall0_c_per_s=0.0, gap_start_c=None, need_start_c=0.1,
                                        gain_at_start=0.0887,
                                        samples=[(T0 - 120, 17.95), (T0 - 60, 17.95)]).to_dict()],
            "history": [[T0 - 60, 17.95]], "intervals": [[T0 - 60, None]],
            "holds": [[T0 - 7200, T0 - 6000]],
        }
        lr = dosing.DoseLearner(CFG)
        lr.restore(old, T0)
        assert lr.gain_c_per_open_min == pytest.approx(2.62 / 60.0)
        assert (lr.theta_s, lr.tau_s, lr.bias_c) == (300.0, 480.0, 0.0) and not lr.learned
        assert len(lr.episodes) == 1 and lr.chains.intervals == [(T0 - 60, None)]
        assert lr.chains.marks[0][:2] == (T0 - 7200, T0 - 6000)
        note = lr.take_migration_note()
        assert note == ("2.17 pools pulses; the single-pulse values (gain 0.089, spread 25 min) "
                        "are dropped and the gain starts from the heat-up rate (0.044)")
        assert lr.take_migration_note() is None
        fresh = dosing.DoseLearner(CFG)
        fresh.restore({"seed_args": [None], "accepted_total": 0, "gain": 0.045}, T0)
        assert "nothing was learned yet, the gain starts from the default (0.045)" in \
            fresh.take_migration_note()

    def test_a_save_with_three_seed_values_restores_the_rate(self):
        old = {"seed_args": [2.52, 1338.0, 1200.0], "gain": 0.042, "theta_s": 900.0, "tau_s": 600.0,
               "accepted_total": 0, "judged": [], "episode": None}
        lr = dosing.DoseLearner(CFG)
        lr.restore(old, T0)
        assert lr.gain_c_per_open_min == pytest.approx(0.042)
        assert (lr.theta_s, lr.tau_s) == (CFG.default_theta_s, CFG.default_tau_s)
        assert lr.to_dict()["seed_args"] == [2.52]

    def test_a_fit_from_before_a_restore_or_reset_is_discarded(self):
        lr = _seeded()
        stale = _pfit(lr, 0.07)
        lr.restore(lr.to_dict(), T0)
        assert lr.apply_fit(stale, T0) is None

    def test_the_saved_state_stays_small(self):
        """Design §5: the real night (six pulses, the open chain, the last
        fit) is a few KB a room; ten pulses spread over 40 h of one
        unbroken chain (1 100 bins: chains break at every heat-up, so a
        window rarely spans more than ~12 h) under 10 KB (the bins as mK
        steps, lossless to 0.001 °C)."""
        d, _ = _night_replay(CFG)
        doser = len(json.dumps(d.to_dict()))
        learner = len(json.dumps(d.learner.to_dict()))
        assert learner < 6_000 and doser < 8_000, (learner, doser)
        lr = dosing.DoseLearner(CFG)
        rng = random.Random(4)
        for m in range(40 * 60):
            lr.chains.note(T0 + 60.0 * m, round(18.0 + rng.gauss(0.0, 0.01), 3), open_from=None,
                           open_until=None, hold_from=None, hold_until=None)
        lr._records.extend(_record(T0 + 3600.0 * (4 * k + 1), 5.0, T0 + 3600.0 * (4 * k + 1) + 1500)
                           for k in range(10))
        lr._prune(T0 + 40 * 3600.0)
        assert sum(len(c.rooms) for c in lr.chains.chains) > 1000
        assert len(json.dumps(lr.to_dict())) < 10_000


# ---------------------------------------------------------------------------
# The coordinator
# ---------------------------------------------------------------------------

class TestCoordinator:
    def test_ride_a_burn(self):
        c = dosing.PulseCoordinator(CFG)
        c.note_zone("a", T0, True)
        assert c.should_fire("b", T0 + 30, virtual_error_c=0.02, ready=True, ready_in_s=600.0,
                             lead_s=540.0) == ("burn", "a")
        # Above the target, not ready, its own burn, a stale report: no.
        assert c.should_fire("b", T0 + 30, virtual_error_c=-0.02, ready=True, ready_in_s=None,
                             lead_s=540.0) is None
        assert c.should_fire("b", T0 + 30, virtual_error_c=0.05, ready=False, ready_in_s=None,
                             lead_s=540.0) is None
        assert c.should_fire("a", T0 + 30, virtual_error_c=0.05, ready=True, ready_in_s=0.0,
                             lead_s=540.0) is None
        stale = T0 + CFG.zone_report_max_age_s + 20.0
        assert c.should_fire("b", stale, virtual_error_c=0.05, ready=True, ready_in_s=0.0,
                             lead_s=540.0) is None
        c.note_zone("a", T0 + 240, False)
        assert c.burning_room(T0 + 250, "b") is None

    def test_fire_together_and_wake_the_rooms_due(self):
        c = dosing.PulseCoordinator(CFG)
        woken = []
        c.register("b", lambda: woken.append("b"))
        c.register("c", lambda: woken.append("c"))
        c.register("d", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        c.note_room("a", T0, dosing=True, ready_in_s=None, lead_s=540.0)
        c.note_room("b", T0, dosing=True, ready_in_s=300.0, lead_s=540.0)    # due within its lead
        c.note_room("c", T0, dosing=True, ready_in_s=1200.0, lead_s=540.0)   # not due soon
        c.note_room("d", T0, dosing=True, ready_in_s=10.0, lead_s=540.0)     # its wake raises
        assert c.note_pulse_start("a", T0 + 5) == ["b"]
        assert woken == ["b"]
        assert c.should_fire("b", T0 + 10, virtual_error_c=-0.01, ready=True, ready_in_s=290.0,
                             lead_s=540.0) == ("together", "a")
        assert c.should_fire("c", T0 + 10, virtual_error_c=-0.05, ready=True, ready_in_s=1190.0,
                             lead_s=540.0) is None
        # The window closes after together_window_s.
        assert c.should_fire("b", T0 + 5 + 121, virtual_error_c=-0.01, ready=True, ready_in_s=290.0,
                             lead_s=540.0) is None

    def test_a_room_not_dosing_neither_wakes_nor_counts(self):
        c = dosing.PulseCoordinator(CFG)
        woken = []
        c.register("b", lambda: woken.append("b"))
        c.note_room("a", T0, dosing=False, ready_in_s=None, lead_s=540.0)
        c.note_room("b", T0, dosing=False, ready_in_s=10.0, lead_s=540.0)
        assert c.note_pulse_start("a", T0) == [] and woken == []
        assert c.should_fire("b", T0 + 5, virtual_error_c=0.0, ready=True, ready_in_s=10.0,
                             lead_s=540.0) is None
        c.unregister("b")
        c.reset()
        assert c.burning_room(T0, "x") is None


# ---------------------------------------------------------------------------
# The room's doser
# ---------------------------------------------------------------------------

def _state(**kw):
    base = {"settled": True, "mode": "stop", "last_pulse_start_ts": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _result(**kw):
    base = {"mode": "stop", "fired_by": None, "dose_end": None, "pulse_s": 0.0, "need_c": 0.0,
            "virtual_error_c": 0.0, "ready_in_s": None, "new_state": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _inputs(d, now, **kw):
    args = {"enabled": True, "setpoint_c": 20.0, "room_c": 19.95, "room_slope_c_per_s": -0.4 * PER_H,
            "state": _state(), "deadband_c_per_h": 0.1}
    args.update(kw)
    return d.inputs(now, **args)


def _after(d, now, result, **kw):
    args = {"setpoint_c": 20.0, "room_c": 19.95, "room_slope_c_per_s": -0.4 * PER_H,
            "trv_internal_c": 18.0, "heating_pct": 0.0, "heating_ts": T0 - 600, "window": False,
            "boost": False, "sent": True}
    args.update(kw)
    return d.after_compute(now, result, **args)


class TestRoomDoser:
    def test_books_the_open_time_from_the_commands(self):
        d = dosing.RoomDoser(CFG, "a")
        d.step(T0)
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
        assert d.open and d.open_interval == (T0 + 60.0, None)
        d.step(T0 + 120)
        assert d.predictor.committed_open_min() == pytest.approx(1.0)
        d.note_command(T0 + 180, 5.0, min_c=5.0, max_c=25.0, ok=True)
        assert d.open_interval == (T0 + 60.0, T0 + 270.0) and not d.open
        d.step(T0 + 240)
        d.step(T0 + 300)
        # 3-min pulse: 3.5 open-minutes; the delay has not passed yet.
        assert d.predictor.committed_open_min() == pytest.approx(3.5)
        assert d.open_interval == (None, None)
        assert d.inflight_c == pytest.approx(3.5 * 0.045)

    def test_a_failed_maximum_books_as_open_a_failed_off_changes_nothing(self):
        """A failed 25 °C may have landed (timed out but taken): booked as
        open, the safe way (more heat in flight, a smaller dose); a failed
        off leaves an open valve booked open; a failed hold marks nothing."""
        d = dosing.RoomDoser(CFG, "a")
        d.step(T0)
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=False)
        assert d.open_interval == (T0 + 60.0, None)
        d.note_command(T0 + 180, 5.0, min_c=5.0, max_c=25.0, ok=False)
        assert d.open_interval == (T0 + 60.0, None)
        d.note_command(T0 + 200, 5.0, min_c=5.0, max_c=25.0, ok=True)
        assert d.open_interval == (T0 + 60.0, T0 + 290.0)
        d.note_command(T0 + 400, 19.0, min_c=5.0, max_c=25.0, ok=False)
        assert d.diagnostics()["hold_from"] is None

    def test_a_reopening_merges(self):
        d = dosing.RoomDoser(CFG, "a")
        d.step(T0)
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
        d.note_command(T0 + 180, 5.0, min_c=5.0, max_c=25.0, ok=True)
        d.note_command(T0 + 200, 25.0, min_c=5.0, max_c=25.0, ok=True)   # still closing
        assert d.open_interval == (T0 + 60.0, None)
        d.note_command(T0 + 220, 25.0, min_c=5.0, max_c=25.0, ok=True)   # already open
        assert d.open_interval == (T0 + 60.0, None)

    def test_a_valve_seen_shut_closes_the_booking(self):
        d = dosing.RoomDoser(CFG, "a")
        d.step(T0)
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
        d.step(T0 + 600)
        d.note_valve_shut(T0 + 600)
        d.step(T0 + 660)
        assert not d.open
        committed = d.predictor.committed_open_min()
        d.step(T0 + 3600)
        assert d.predictor.committed_open_min() < committed

    def test_inputs(self):
        d = dosing.RoomDoser(CFG, "a")
        kw = _inputs(d, T0)
        assert kw["dosing"] and kw["now_ts"] == T0 and kw["fire_now"] is None
        assert kw["gain_c_per_open_min"] == 0.045 and kw["tail_c"] == 0.0
        assert (kw["theta_s"], kw["tau_s"], kw["bias_c"]) == (300.0, 480.0, 0.0)
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
        kw = _inputs(d, T0 + 60, enabled=False)
        assert not kw["dosing"] and kw["tail_c"] == pytest.approx(0.045 * 1.5)
        assert d.virtual_error_c == pytest.approx(0.05 - d.inflight_c)

    def test_fires_with_another_rooms_burn_and_says_so(self):
        c = dosing.PulseCoordinator(CFG)
        d = dosing.RoomDoser(CFG, "b", c)
        c.note_zone("a", T0, True)
        kw = _inputs(d, T0 + 10, room_c=20.0)
        assert kw["fire_now"] == "burn"
        out = _after(d, T0 + 10, _result(mode="dose", fired_by="burn", need_c=0.12), room_c=20.0)
        assert out.fired_with == ("burn", "a")
        assert d.learner.episode is not None and d.learner.episode.rode_burn
        assert d.summary()["rode_burn_24h"] == 1 and d.summary()["pulses_24h"] == 1
        # Not settled, or a pulse running: no.
        assert _inputs(d, T0 + 20, state=_state(settled=False))["fire_now"] is None
        assert _inputs(d, T0 + 20, state=_state(mode="dose"))["fire_now"] is None

    @pytest.mark.parametrize("zone_above, rides", [(0.2, True), (0.1, False)])
    def test_a_burn_is_ridden_at_or_below_the_aim(self, zone_above, rides):
        """0.05 over the target: under the zone's aim (+0.1 by default) it
        rides another zone's burn; with the aim on the target it does not."""
        cfg = dataclasses.replace(CFG, zone_above_c=zone_above)
        c = dosing.PulseCoordinator(cfg)
        d = dosing.RoomDoser(cfg, "b", c)
        c.note_zone("a", T0, True)
        kw = _inputs(d, T0 + 10, room_c=20.05, room_slope_c_per_s=0.0)
        assert kw["fire_now"] == ("burn" if rides else None)
        diag = d.diagnostics()
        assert diag["zone_c"] == [zone_above, -0.1]
        assert diag["aim_over_c"] == pytest.approx(cfg.aim_over_c)

    def test_a_pulse_wakes_the_rooms_due_and_reports_its_readiness(self):
        c = dosing.PulseCoordinator(CFG)
        a = dosing.RoomDoser(CFG, "a", c)
        b = dosing.RoomDoser(CFG, "b", c)
        woken = []
        c.register("b", lambda: woken.append("b"))
        _inputs(b, T0)
        _after(b, T0, _result(ready_in_s=200.0))
        _inputs(a, T0 + 5)
        out = _after(a, T0 + 5, _result(mode="dose", fired_by="release", need_c=0.15))
        assert out.woke == ["b"] and woken == ["b"]

    def test_when_to_judge_a_running_pulse_again(self):
        d = dosing.RoomDoser(CFG, "a")
        _inputs(d, T0)
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
        assert _after(d, T0, _result(mode="dose", fired_by="release", need_c=0.18)).wake_in_s is None
        assert _after(d, T0 + 130, _result(mode="dose", pulse_s=130.0, need_c=0.01)).wake_in_s == \
            pytest.approx(50.0)
        assert _after(d, T0 + 240, _result(mode="dose", pulse_s=240.0, need_c=0.0225)).wake_in_s == \
            pytest.approx(30.0)
        assert _after(d, T0 + 245, _result(mode="dose", pulse_s=245.0, need_c=0.0)).wake_in_s == 5.0
        assert _after(d, T0 + 300, _result(mode="stop")).wake_in_s is None

    def test_a_pulse_that_did_nothing_is_not_learned_and_keeps_its_heat(self):
        """The opened check only keeps the episode from learning: the heat
        booked stays booked (if the valve really never opened, the room
        lands short: the safe side).  It runs only once the pulse has ended
        and the zone has had opened_check_s to report."""
        d = dosing.RoomDoser(CFG, "a")
        _inputs(d, T0)
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
        _after(d, T0, _result(mode="dose", fired_by="release", need_c=0.15), heating_pct=0.0)
        lines = []
        for k in range(1, 3 + int(CFG.opened_check_s // 60) + 1):
            now = T0 + 60 * k
            if k == 3:
                d.note_command(now, 5.0, min_c=5.0, max_c=25.0, ok=True)
            _inputs(d, now)
            committed = d.predictor.committed_open_min()
            out = _after(d, now, _result(mode="stop" if k >= 3 else "dose",
                                         dose_end="covered" if k == 3 else None), heating_pct=0.0)
            lines += [(k, ln) for ln in out.lines]
            assert d.predictor.committed_open_min() == committed
        assert [k for k, ln in lines if "did nothing" in ln] == [3 + int(CFG.opened_check_s // 60)]
        assert d.inflight_c > 0.1 and d.learner.episode is None and not d.learner.learned
        # Its opening is still modelled for the pooled fit (its heat is real).
        assert d.learner.chains.intervals == [(T0 + 60.0, T0 + 180.0 + CFG.close_delay_s)]

    def test_round_trip_keeps_the_heat_in_flight(self):
        d = dosing.RoomDoser(CFG, "a")
        _inputs(d, T0)
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
        _after(d, T0, _result(mode="dose", fired_by="release", need_c=0.15))
        _inputs(d, T0 + 120)
        data = json.loads(json.dumps(d.to_dict()))
        back = dosing.RoomDoser(CFG, "a")
        back.restore(data, T0 + 150)
        assert back.open and back.inflight_c == pytest.approx(d.inflight_c)
        back.step(T0 + 180)
        d.step(T0 + 180)
        assert back.predictor.committed_open_min() == pytest.approx(d.predictor.committed_open_min())
        assert back.learner.episode is not None and back.summary()["pulses_24h"] == 1
        late = dosing.RoomDoser(CFG, "a")
        late.restore(data, T0 + 120 + 601)
        assert late.inflight_c == 0.0 and not late.open
        future = dict(data, open_from=T0 + 10_000)
        odd = dosing.RoomDoser(CFG, "a")
        odd.restore(future, T0 + 150)
        assert not odd.open

    def test_reset_forgets(self):
        d = dosing.RoomDoser(CFG, "a")
        d.seed(2.4)
        for k in range(30):
            _inputs(d, T0 + 60 * k)
            _after(d, T0 + 60 * k, _result())
        d.note_command(T0 + 1800, 25.0, min_c=5.0, max_c=25.0, ok=True)
        _inputs(d, T0 + 1920)
        generation = d.learner.generation
        d.reset()
        assert d.inflight_c == 0.0 and d.predictor.committed_open_min() == 0.0
        assert d.learner.gain_c_per_open_min == pytest.approx(0.04)
        assert d.learner.chains.chains == () and d.learner.generation == generation + 1

    def test_refit_now_runs_the_pooled_fit_and_applies_it(self):
        d, fits = _night_replay(refit=False)
        assert fits == [] and d.learner.refit_due
        line = d.refit_now()
        assert line.startswith("pooled over 6 pulses") and not d.learner.refit_due
        assert d.refit_now() is not None                   # forced: it fits again
        assert dosing.RoomDoser(CFG, "x").refit_now(T0) is None

    def test_a_refit_in_flight_is_saved_as_due(self):
        """2.17.0 review (m1): between the snapshot and the result being
        applied (a refit running, a result waiting), the save says the refit
        is due, so a reload refits; once applied, or failed, it is not."""
        d, _ = _night_replay(refit=False)
        lr, now = d.learner, NIGHT["t0"] + NIGHT["end_s"]
        inp = lr.fit_input(now)
        assert not lr.refit_due and lr.refit_in_flight and lr.to_dict()["refit_due"] is True
        back = dosing.DoseLearner(CFG)
        back.restore(lr.to_dict(), now + 60)
        assert back.refit_due
        lr.apply_fit(dosing.pooled_fit(inp), now)
        assert not lr.refit_in_flight and lr.to_dict()["refit_due"] is False
        inp = lr.fit_input(now + 60)
        lr.fit_failed(inp.generation - 1)                   # another generation's: ignored
        assert lr.refit_in_flight
        lr.fit_failed(inp.generation)
        assert not lr.refit_in_flight and lr.to_dict()["refit_due"] is False

    def test_the_zone_signal(self):
        c = dosing.PulseCoordinator(CFG)
        d = dosing.RoomDoser(CFG, "a", c)
        d.note_zone(T0, 40.0, None)
        assert c.burning_room(T0, "b") == "a"
        # As of the report: an old one is no burn however recently the room
        # cycled; one from the future counts as now.
        d.note_zone(T0, 40.0, None, reported_ts=T0 - CFG.zone_report_max_age_s - 1)
        assert c.burning_room(T0, "b") is None
        d.note_zone(T0, 40.0, None, reported_ts=T0 - 60.0)
        assert c.burning_room(T0, "b") == "a"
        assert c.burning_room(T0 + CFG.zone_report_max_age_s - 30.0, "b") is None
        d.note_zone(T0, 40.0, None, reported_ts=T0 + 3600.0)
        assert c.burning_room(T0 + CFG.zone_report_max_age_s, "b") == "a"
        d.note_zone(T0, 40.0, None, reported_ts=math.nan)
        assert c.burning_room(T0, "b") == "a"
        d.note_zone(T0, None, False)
        assert c.burning_room(T0, "b") is None
        d.note_zone(T0, None, True)
        assert c.burning_room(T0, "b") == "a"
        d.note_zone(T0, None, None)
        assert c.burning_room(T0, "b") is None


# ---------------------------------------------------------------------------
# Overlapping episodes and booking (2.15.0's review)
# ---------------------------------------------------------------------------

def _pulse_history(lr, pulses, *, gain, theta, tau, fall_h, room0=20.42, start=T0, end_s=7200.0,
                   setpoint=20.5, heating=100.0, end_reason="covered"):
    """Feed the learner a FOPDT room (minute readings, 0.01 °C) with
    ``pulses`` (start, end) in seconds from ``start``, as the doser would:
    each pulse starts an episode, its open interval booked [start + 60,
    end + 90].  ``room0`` is the room at the first pulse (the history starts
    30 min before it).  Returns every verdict with its time."""
    dt = 20.0
    grid = [k * dt for k in range(-int(1800 / dt), int(end_s / dt) + 1)]
    booked = [(a + CFG.latency_s, b + CFG.close_delay_s) for a, b in pulses]
    u = [sum(max(0.0, min(t + dt, hi) - max(t, lo)) for lo, hi in booked) / dt for t in grid]
    rise = dosing.fopdt_response(u, dt, theta, tau)
    room = {t: round(room0 - fall_h * PER_H * t + gain * r, 2) for t, r in zip(grid, rise)}
    verdicts = []
    for t in range(-1800, int(end_s) + 1, 60):
        now = start + t
        for a, b in pulses:
            if t == a:
                lr.start_episode(now, setpoint, room[float(t)], fall_h * PER_H, 1.0)
            if t == b:
                lr.note_pulse_end(now)
                lr.note_end_reason(start + a, end_reason)
        lo = hi = None
        for a, b in booked:
            if a - CFG.latency_s <= t < b:
                lo, hi = start + a, (start + b if t >= b - CFG.close_delay_s else None)
        pct_ts = max([start + lo_ for lo_, _ in booked if start + lo_ <= now] or [start - 3600])
        on = any(lo_ <= t < hi_ for lo_, hi_ in booked)
        lr.note_cycle(now, room[float(t)], heating_pct=heating if on else 0.0, heating_ts=pct_ts,
                      open_from=lo, open_until=hi)
        verdicts += [(t, v) for v in lr.observe(now, setpoint, window=False, boost=False)]
    return verdicts


class TestOverlappingEpisodes:
    """M1: a pulse that starts while the last one is still being measured
    no longer drops it."""

    def test_both_pulses_are_judged_and_the_first_lands_at_the_cut(self):
        # The s15/s16 plant: weaker and later than the model in use, so the
        # second pulse comes 21 min after the first (theta + 2 tau in use).
        lr = dosing.DoseLearner(FAST)
        verdicts = _pulse_history(lr, [(0, 300), (1260, 1560)], gain=0.02, theta=540.0, tau=240.0,
                                  fall_h=0.3, end_s=5400.0)
        kinds = [v.kind for _, v in verdicts]
        assert kinds == [dosing.ACCEPTED, dosing.ACCEPTED], [v.line for _, v in verdicts]
        # The first is judged before the second's heat could show (its open
        # + the delay in use), on its own landing.
        cut = 1260 + CFG.latency_s + CFG.default_theta_s
        assert verdicts[0][0] <= cut + 300 < verdicts[1][0]
        assert all(r.usable for r in lr.records)
        # Pooled, the two pulses' heat is the plant's (every opening modelled).
        fit = dosing.pooled_fit(lr.fit_input(T0 + 5400))
        assert fit.n_pulses == 2 and fit.gain == pytest.approx(0.02, rel=0.25)
        assert fit.theta_s > CFG.prior_theta_s

    def test_a_pulse_due_before_the_last_one_landed_cuts_it_short(self):
        """The next pulse 14 min after the first (the refractory with a short
        model) on a 9-min plant: the first one's heat has not peaked when the
        second's can show, so it lands at the cut, and is judged there."""
        lr = dosing.DoseLearner(CFG)
        verdicts = _pulse_history(lr, [(0, 300), (840, 1140)], gain=0.02, theta=540.0, tau=240.0,
                                  fall_h=0.3, end_s=5400.0)
        assert [v.kind for _, v in verdicts] == [dosing.ACCEPTED, dosing.ACCEPTED]
        cut = 840 + CFG.latency_s + CFG.default_theta_s
        assert verdicts[0][0] == pytest.approx(cut + 300, abs=60)
        assert "by the next pulse" in verdicts[0][1].line

    def test_an_under_landing_moves_the_bias_up(self):
        """(b): an episode landing short (here -0.10, a pulse sized by the
        model in use on the weaker plant) moves the aim up."""
        lr = dosing.DoseLearner(CFG)
        _pulse_history(lr, [(0, 240), (1260, 1500)], gain=0.02, theta=540.0, tau=240.0,
                       fall_h=0.3, end_s=5400.0)
        first = lr.records[0]
        assert -0.12 <= first.peak_error_c <= -0.03
        assert lr.bias_c > 0.3 * 0.03

    def test_the_next_pulse_before_this_ones_heat_could_show_drops_it(self):
        lr = dosing.DoseLearner(CFG)
        lr.start_episode(T0, 20.0, 19.9, 0.0, None)
        lr.note_cycle(T0 + 60, 19.9, heating_pct=None, heating_ts=None, open_from=T0 + 60, open_until=None)
        lr.note_pulse_end(T0 + 180)
        lr.note_cycle(T0 + 180, 19.9, heating_pct=None, heating_ts=None, open_from=T0 + 60,
                      open_until=T0 + 270)
        lr.start_episode(T0 + 240, 20.0, 19.9, 0.0, None)          # 4 min later
        assert lr.episodes[0].cut_ts == pytest.approx(T0 + 240 + CFG.latency_s + CFG.default_theta_s)
        out = []
        for k in range(5, 20):
            lr.note_cycle(T0 + 60 * k, 19.9, heating_pct=None, heating_ts=None,
                          open_from=T0 + 300, open_until=None)
            out += lr.observe(T0 + 60 * k, 20.0, window=False, boost=False)
        assert [v.kind for v in out] == [dosing.DROPPED]
        assert "before this one's could show" in out[0].line
        assert len(lr.episodes) == 1 and lr.episode.start_ts == T0 + 240

    def test_open_episodes_are_bounded(self):
        lr = dosing.DoseLearner(CFG)
        lines = [lr.start_episode(T0 + 60 * k, 20.0, 19.9, 0.0, None) for k in range(6)]
        assert len(lr.episodes) == 3
        assert lines[:3] == [None] * 3 and all("kept coming" in ln for ln in lines[3:])
        assert [ep.start_ts for ep in lr.episodes] == [T0 + 180, T0 + 240, T0 + 300]

    def test_episodes_round_trip(self):
        lr = dosing.DoseLearner(CFG)
        _pulse_history(lr, [(0, 300), (1260, 1560)], gain=0.02, theta=540.0, tau=240.0,
                       fall_h=0.3, end_s=1500.0)
        assert len(lr.episodes) == 2 and lr.episodes[0].cut_ts is not None
        back = dosing.DoseLearner(CFG)
        back.restore(json.loads(json.dumps(lr.to_dict())), T0 + 1530)
        assert [e.to_dict() for e in back.episodes] == [e.to_dict() for e in lr.episodes]

    def test_a_late_first_heating_report_still_counts_as_opened(self):
        """A slow cloud poll: the zone's first report above 0 changed 370 s
        after the valve opened (s12_lagged_power.py 370)."""
        lr = dosing.DoseLearner(CFG)
        lr.start_episode(T0, 20.0, 19.95, 0.4 * PER_H, None)
        verdicts = []
        ts, room, _ = _synthetic(gain=0.04, fall_h=0.4, open_from=60.0, open_until=270.0, dt=60.0,
                                 end=3600.0, room0=19.95)
        for t, y in zip(ts[1:], room[1:]):
            now = T0 + t
            late = t >= 60 + 370
            lr.note_cycle(now, y, heating_pct=100.0 if late and t < 270 + 370 else 0.0,
                          heating_ts=T0 + (430.0 if late else -3600.0), open_from=T0 + 60,
                          open_until=T0 + 270 if t >= 180 else None)
            if t >= 180:
                lr.note_pulse_end(T0 + 180)
            verdicts += lr.observe(now, 20.0, window=False, boost=False)
        assert [v.kind for v in verdicts] == [dosing.ACCEPTED], [v.line for v in verdicts]


class TestBooking:
    """M2 and the paths that end a pulse."""

    def test_only_a_maximum_opens_and_anything_else_closes_like_an_off(self):
        d = dosing.RoomDoser(CFG, "a")
        d.step(T0)
        d.note_command(T0, 21.3, min_c=5.0, max_c=25.0, ok=True)       # a hold: nothing booked
        assert d.open_interval == (None, None)
        d.note_command(T0 + 60, 24.96, min_c=5.0, max_c=25.0, ok=True)  # the maximum
        assert d.open_interval == (T0 + 120.0, None)
        d.note_command(T0 + 300, 20.8, min_c=5.0, max_c=25.0, ok=True)  # a hold closes it
        assert d.open_interval == (T0 + 120.0, T0 + 390.0) and not d.open
        d.note_command(T0 + 320, 25.0, min_c=5.0, max_c=25.0, ok=True)  # re-opened while closing
        assert d.open_interval == (T0 + 120.0, None)
        d.note_command(T0 + 400, 5.0, min_c=5.0, max_c=25.0, ok=True)
        assert d.open_interval == (T0 + 120.0, T0 + 490.0)
        d.note_command(T0 + 600, 25.0, min_c=5.0, max_c=math.nan, ok=True)  # no limit: no maximum
        d.step(T0 + 600)
        assert d.open_interval == (None, None)

    def test_an_hour_of_hold_does_not_read_as_a_fast_cooling(self):
        """s3_loss.py: 60 min of hold commands at the target (the valve
        really ~30 % open), then an off; the room cools 0.5 °C/h.  Booked as
        open the observer read -2.7..-3.0 °C/h for 20 min."""
        d = dosing.RoomDoser(CFG, "r")
        st = SimpleNamespace(settled=False, mode="hold", last_pulse_start_ts=None)
        t = T0
        d.note_command(t, 21.3, min_c=5, max_c=25, ok=True)
        for k in range(60):
            t += 60
            if k % 3 == 0:
                d.note_command(t, 21.3 + 0.1 * (k % 2), min_c=5, max_c=25, ok=True)
            d.inputs(t, enabled=True, setpoint_c=20.5, room_c=20.5, room_slope_c_per_s=0.0, state=st,
                     deadband_c_per_h=0.1)
        assert d.inflight_c == 0.0
        d.note_command(t, 5.0, min_c=5, max_c=25, ok=True)
        room, slopes = 20.5, []
        for _ in range(25):
            t += 60
            room -= 0.5 / 60
            d.inputs(t, enabled=True, setpoint_c=20.5, room_c=round(room, 2), room_slope_c_per_s=-0.5 * PER_H,
                     state=st, deadband_c_per_h=0.1)
            if d.loss_slope_c_per_s is not None:
                slopes.append(d.loss_slope_c_per_s * 3600)
        assert slopes and min(slopes) >= -0.6, slopes

    def test_an_off_from_any_path_ends_the_pulse_being_measured(self):
        """The watchdog, the room sensor lost, Roomstat's own off, summer:
        no dose_end from the regulator, the off ends the learner's pulse; so
        does the valve seen shut."""
        for close in ("off", "shut"):
            d = dosing.RoomDoser(CFG, "a")
            _inputs(d, T0)
            d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
            _after(d, T0, _result(mode="dose", fired_by="release", need_c=0.15))
            if close == "off":
                d.note_command(T0 + 420, 5.0, min_c=5.0, max_c=25.0, ok=True)
            else:
                d.note_valve_shut(T0 + 420)
            _inputs(d, T0 + 480)
            _after(d, T0 + 480, _result(mode="coast"))
            assert d.learner.episode.pulse_end_ts == T0 + 420, close


class TestHoldHeat:
    """A hold's heat is unbooked (M2): an episode its heat reaches is not
    learned from, and no chain holds it."""

    def test_a_heat_ups_maximum_is_marked_not_booked(self):
        d = dosing.RoomDoser(CFG, "a")
        d.step(T0)
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True, pulse=False)
        assert d.open_interval == (None, None)
        assert d.diagnostics()["hold_from"] == T0
        d.note_command(T0 + 1800, 5.0, min_c=5.0, max_c=25.0, ok=True)
        assert d.diagnostics()["hold_until"] == T0 + 1800 + CFG.close_delay_s
        d.note_command(T0 + 4000, 25.0, min_c=5.0, max_c=25.0, ok=True)
        assert d.open_interval == (T0 + 4000 + CFG.latency_s, None)

    def test_a_pulse_a_heat_up_takes_over_stops_booking(self):
        d = dosing.RoomDoser(CFG, "a")
        d.step(T0)
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
        _inputs(d, T0 + 300)
        _after(d, T0 + 300, _result(mode="heat", dose_end="heat_up"))
        assert d.open_interval[1] == T0 + 300 + CFG.close_delay_s
        assert d.diagnostics()["hold_from"] == T0 + 300

    def test_a_hold_is_marked_from_its_command_to_the_next_off_or_maximum(self):
        d = dosing.RoomDoser(CFG, "a")
        d.step(T0)
        d.note_command(T0, 21.3, min_c=5.0, max_c=25.0, ok=True)
        d.note_command(T0 + 180, 21.7, min_c=5.0, max_c=25.0, ok=True)     # still the same hold
        assert d.diagnostics()["hold_from"] == T0 and d.diagnostics()["hold_until"] is None
        d.note_command(T0 + 600, 5.0, min_c=5.0, max_c=25.0, ok=True)
        assert d.diagnostics()["hold_until"] == T0 + 600 + CFG.close_delay_s
        d.note_command(T0 + 620, 21.0, min_c=5.0, max_c=25.0, ok=True)     # held again while closing
        assert (d.diagnostics()["hold_from"], d.diagnostics()["hold_until"]) == (T0, None)
        d.note_command(T0 + 900, 25.0, min_c=5.0, max_c=25.0, ok=True)     # a maximum ends it too
        d.step(T0 + 1200)
        assert d.diagnostics()["hold_from"] is None
        d.note_command(T0 + 1300, 21.0, min_c=5.0, max_c=25.0, ok=False)   # failed: nothing
        d.note_command(T0 + 1300, 5.0, min_c=math.nan, max_c=25.0, ok=True)  # unknown floor: a hold
        assert d.diagnostics()["hold_from"] == T0 + 1300

    def test_a_hold_during_an_episode_drops_it(self):
        d = dosing.RoomDoser(CFG, "a")
        _inputs(d, T0)
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
        _after(d, T0, _result(mode="dose", fired_by="release", need_c=0.15))
        d.note_command(T0 + 300, 5.0, min_c=5.0, max_c=25.0, ok=True)
        _inputs(d, T0 + 300)
        assert _after(d, T0 + 300, _result(mode="stop", dose_end="covered")).lines == []
        d.note_command(T0 + 540, 21.2, min_c=5.0, max_c=25.0, ok=True)      # dosing paused (a window, Boost): a hold
        _inputs(d, T0 + 540)
        out = _after(d, T0 + 540, _result(mode="hold"))
        assert out.lines == ["not learned from: a hold's heat (unknown) was in the window"]
        assert d.learner.episode is None

    def test_a_holds_heat_still_arriving_before_the_window_drops_it(self):
        """The window reaches back the landing horizon in use before its
        15 min: a hold that ended 30 min before the pulse still counts, one
        that ended an hour before does not."""
        for ended_min, dropped in ((30, True), (60, False)):
            lr = dosing.DoseLearner(CFG)
            hold_end = T0 - 60 * ended_min
            lr.note_cycle(hold_end - 1800, 20.4, heating_pct=None, heating_ts=None, open_from=None,
                          open_until=None, hold_from=hold_end - 1800, hold_until=None)
            lr.note_cycle(hold_end - 90, 20.5, heating_pct=None, heating_ts=None, open_from=None,
                          open_until=None, hold_from=hold_end - 1800, hold_until=hold_end)
            lr.note_cycle(hold_end + 60, 20.5, heating_pct=None, heating_ts=None, open_from=None,
                          open_until=None)
            lr.start_episode(T0, 20.5, 20.4, 0.0, None)
            out = lr.observe(T0 + 60, 20.5, window=False, boost=False)
            assert bool(out) is dropped, ended_min
            assert len(lr.chains.marks) == 1

    def test_the_marks_round_trip(self):
        d = dosing.RoomDoser(CFG, "a")
        _inputs(d, T0)
        d.note_command(T0, 21.0, min_c=5.0, max_c=25.0, ok=True)
        _after(d, T0, _result(mode="hold"))
        data = json.loads(json.dumps(d.to_dict()))
        back = dosing.RoomDoser(CFG, "a")
        back.restore(data, T0 + 60)
        assert back.diagnostics()["hold_from"] == T0
        assert [m[:2] for m in back.learner.chains.marks] == [(T0, None)]
        future = dosing.RoomDoser(CFG, "a")
        future.restore(dict(data, hold_from=T0 + 3600), T0 + 60)
        assert future.diagnostics()["hold_from"] is None

    def test_a_hold_while_dosing_paused_leaves_the_model_alone(self):
        """The room cools 1.5 °C/h; dosing paused (a window, Boost: the
        ordinary modes) while a hold runs the valve at 60 %, then dosing
        takes over again.  An episode whose window holds that heat is
        dropped, no chain holds it, and the pooled fit learns the plant
        (the default model) from the pulses after it."""
        loop = _Loop(_Plant(T0, room=20.2, fall_c_per_h=1.5, hold_frac=0.6), mode="hold")
        loop.enabled = False
        loop.run(40)
        loop.enabled = True
        loop.run(5 * 60)
        lr = loop.d.learner
        assert loop.pulses
        usable = [r for r in lr.records if r.usable]
        # The hold ran the first 40 min: no pooled pulse's segment starts
        # within its settle.
        assert usable and all(r.start_ts - CFG.segment_pre_s
                              >= T0 + 40 * 60 + CFG.chain_settle_after_unbooked_s for r in usable)
        assert lr.gain_c_per_open_min == pytest.approx(0.045, rel=0.25)
        assert 120.0 <= lr.theta_s <= 600.0 and 240.0 <= lr.tau_s <= 900.0


class TestRoomDoserReview:
    def test_a_pulse_that_did_not_go_out_starts_nothing(self):
        c = dosing.PulseCoordinator(CFG)
        d = dosing.RoomDoser(CFG, "b", c)
        woken = []
        c.register("a", lambda: woken.append("a"))
        c.note_room("a", T0, dosing=True, ready_in_s=60.0, lead_s=540.0)
        c.note_zone("x", T0, True)
        assert _inputs(d, T0 + 10, room_c=20.0)["fire_now"] == "burn"
        out = _after(d, T0 + 10, _result(mode="dose", fired_by="burn", need_c=0.12), room_c=20.0,
                     sent=False)
        assert out.fired_with is None and out.woke == [] and woken == []
        assert d.learner.episode is None and d.summary()["pulses_24h"] == 0
        assert "b" not in c._pulses
        # When it goes out it counts.
        out = _after(d, T0 + 70, _result(mode="dose", fired_by="burn", need_c=0.12), room_c=20.0)
        assert d.learner.episode is not None and d.summary()["pulses_24h"] == 1 and woken == ["a"]

    def test_a_bridged_reading_feeds_neither_the_observer_nor_the_chains(self):
        d = dosing.RoomDoser(CFG, "a")
        for k in range(12):
            _inputs(d, T0 + 60 * k, room_c=20.0 - 0.01 * k)
            _after(d, T0 + 60 * k, _result(), room_c=20.0 - 0.01 * k)
        loss_before = d._loss.to_dict()
        chains_before = d.learner.chains.chains
        for k in range(12, 16):
            _inputs(d, T0 + 60 * k, room_c=19.89, room_fresh=False)
            _after(d, T0 + 60 * k, _result(mode="dose", fired_by="release" if k == 12 else None,
                                           need_c=0.1), room_c=19.89, room_fresh=False)
        assert d._loss.to_dict() == loss_before
        # Broken there, nothing added (the closed chain held no pulse: pruned).
        assert chains_before and d.learner.chains.chains == ()
        assert d.learner.chains.filling_since is None
        assert d.learner.chains.last_break[1] == "a bridged reading"
        assert d.learner.episode is None          # no episode on a bridged reading
        assert d.summary()["pulses_24h"] == 1     # the pulse still counts
        # A fresh reading again: an episode takes it, the bridged ones not.
        d.learner.start_episode(T0 + 960, 20.0, 19.88, 0.0, None)
        _after(d, T0 + 1020, _result(), room_c=19.87, room_fresh=False)
        _after(d, T0 + 1080, _result(), room_c=19.86)
        assert d.learner.episode.samples == [(T0 + 960, 19.88), (T0 + 1080, 19.86)]

    def test_the_caps_observer_is_on_the_rooms_rate_not_the_learned_gain(self):
        """The cap's observer subtracts Roomstat's heat at the tuner's rate,
        the dose's at the learned gain: a learned gain twice the room's
        makes the dose's cooling read far faster, the cap's not."""
        d = dosing.RoomDoser(CFG, "a")
        d.learner.seed(2.4)                         # 0.04 per open-minute
        d.learner.gain_c_per_open_min = 0.08        # learned twice too high
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
        d.note_command(T0 + 600, 5.0, min_c=5.0, max_c=25.0, ok=True)
        room, committed = 20.0, 0.0
        for k in range(0, 26):                      # the heat still arriving
            now = T0 + 60 * k
            before = d.predictor.committed_open_min()
            d.step(now)
            committed = d.predictor.committed_open_min()
            arrived = max(0.0, before - committed) if k else 0.0
            room += 0.04 * arrived - 0.5 * PER_H * 60.0
            if k == 20:
                d.learner.seed(2.5)                 # the tuner's rate moves
            kw = _inputs(d, now, room_c=room)
        assert kw["cap_gain_c_per_open_min"] == pytest.approx(2.5 / 60.0)
        assert kw["cap_loss_slope_c_per_s"] * 3600 == pytest.approx(-0.5, abs=0.1)
        assert kw["loss_slope_c_per_s"] * 3600 < -0.7
        assert kw["cap_inflight_c"] == pytest.approx(2.5 / 60.0 * d.predictor.committed_open_min())
        back = dosing.RoomDoser(CFG, "a")
        back.restore(d.to_dict(), T0 + 1560)
        assert back._cap_delivered_c == pytest.approx(d._cap_delivered_c, abs=1e-4)
        assert back.cap_loss_slope_c_per_s == pytest.approx(d.cap_loss_slope_c_per_s, abs=1e-6)
        old = d.to_dict()
        del old["cap_delivered_c"], old["cap_loss"]
        back.restore(old, T0 + 1560)
        assert back.cap_loss_slope_c_per_s is None and back._cap_delivered_c == 0.0
        d.reset()
        assert d.cap_loss_slope_c_per_s is None and d._cap_delivered_c == 0.0
        assert "cap_loss_slope_c_per_h" in back.diagnostics()

    def test_without_a_tuner_rate_there_is_no_cap_gain(self):
        d = dosing.RoomDoser(CFG, "a")
        d.learner.gain_c_per_open_min = 0.03
        d.learner._accepted_total = 5
        assert d.cap_gain_c_per_open_min is None
        assert _inputs(d, T0)["cap_gain_c_per_open_min"] is None

    def test_unbooked_heat_is_reported_until_it_has_stopped(self):
        d = dosing.RoomDoser(CFG, "a")
        assert _inputs(d, T0)["unbooked_until_ts"] is None
        d.note_valve_heating_unbooked(T0)
        assert _inputs(d, T0 + 60)["unbooked_until_ts"] == T0 + 60
        d.note_command(T0 + 120, 5.0, min_c=5.0, max_c=25.0, ok=True)
        assert _inputs(d, T0 + 180)["unbooked_until_ts"] == T0 + 120 + CFG.close_delay_s
        d.note_valve_heating_unbooked(T0 + 600)
        d.note_valve_shut(T0 + 660)
        assert _inputs(d, T0 + 700)["unbooked_until_ts"] == T0 + 660
        d2 = dosing.RoomDoser(CFG, "b")
        d2.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
        d2.note_valve_heating_unbooked(T0 + 30)
        assert _inputs(d2, T0 + 60)["unbooked_until_ts"] is None

    def test_a_pulse_resumed_without_its_booking_is_booked_from_now(self):
        d = dosing.RoomDoser(CFG, "a")
        d.resume_open(T0)
        assert d.open
        d.step(T0)
        d.step(T0 + 120)
        assert d.predictor.committed_open_min() == pytest.approx(2.0)
        back = dosing.RoomDoser(CFG, "a")
        back.restore(json.loads(json.dumps(d.to_dict())), T0 + 150)
        assert back._resumed_from == T0

    def test_summary_changes_only_on_pulses_lessons_and_fits(self):
        loop = _Loop(_Plant(T0, fall_c_per_h=0.4))
        loop.run(4 * 60)
        changes = sum(1 for a, b in zip(loop.summaries, loop.summaries[1:]) if a != b)
        events = len(loop.pulses) + len(loop.lines) + len(loop.fits)
        assert len(loop.summaries) > 200 and len(loop.pulses) >= 5 and loop.fits
        assert changes <= events, (changes, len(loop.pulses), len(loop.lines), len(loop.fits))
        summary = loop.d.summary()
        live = {"inflight_c", "valve_open", "open_from", "open_until", "episode_running",
                "loss_slope_c_per_h", "virtual_error_c", "pulses", "chains", "last_fit",
                "episodes_running"}
        assert not live & set(summary)
        diag = loop.d.diagnostics()
        assert live <= set(diag) and set(summary) <= set(diag)
        assert json.loads(json.dumps(summary)) == summary
        assert json.loads(json.dumps(diag)) == diag
        for key in ("gain_uncertainty_pct", "pulses_pooled", "pooled_span_h", "fit_rms_mc",
                    "fit_sigma_mc", "fit_effective_samples", "anchor_gain_c_per_open_min",
                    "dead_start_s", "learning", "last"):
            assert key in summary


# ---------------------------------------------------------------------------
# Closed loop: the real regulator and doser on a room unlike the model
# ---------------------------------------------------------------------------

class _Plant:
    """Room and radiator, first order plus dead time on the valve's
    opening: fully open at the maximum (after 60 s), shut at the off (after
    90 s); a hold command opens it ``hold_frac`` while it pushes 0.3-4.5
    over the valve's reading (a Tado throttling), else not at all.  The
    zone's heating % reaches HA ``report_lag_s`` late."""

    def __init__(self, t0, *, room=20.55, fall_c_per_h=0.5, gain=0.045, theta_s=300.0,
                 tau_s=480.0, hold_frac=0.0, report_lag_s=0.0):
        self.t = t0
        self.room, self.fall = room, fall_c_per_h * PER_H
        self.gain, self.theta, self.tau = gain, theta_s, tau_s
        self.hold_frac, self.lag = hold_frac, report_lag_s
        self.cmd = None
        self.pending = []
        self.line = []
        self.emitter = 0.0
        self.power = 0.0
        self.power_hist = [(t0, 0.0)]

    def trv(self):
        return self.room - 3.0

    def command(self, ts, c, off):
        self.pending.append((ts + (90.0 if off else 60.0), None if off else c))
        self.pending.sort(key=lambda p: p[0])

    def _frac(self):
        if self.cmd is None:
            return 0.0
        if self.cmd >= 24.95:
            return 1.0
        push = self.cmd - self.trv()
        return self.hold_frac if 0.3 <= push < 4.5 else (1.0 if push >= 4.5 else 0.0)

    def advance(self, seconds):
        end = self.t + seconds
        while self.t < end - 1e-6:
            dt = min(10.0, end - self.t)
            while self.pending and self.pending[0][0] <= self.t:
                self.cmd = self.pending.pop(0)[1]
            f = self._frac()
            if f > 0:
                self.line.append((self.t + self.theta, f * dt / 60.0))
            while self.line and self.line[0][0] <= self.t:
                self.emitter += self.line.pop(0)[1]
            arrived = self.emitter * (1.0 - math.exp(-dt / self.tau))
            self.emitter -= arrived
            self.room += self.gain * arrived - self.fall * dt
            self.t += dt
            if round(100.0 * f) != self.power:
                self.power = round(100.0 * f)
                self.power_hist.append((self.t, self.power))

    def report(self):
        seen = [(t + self.lag, p) for t, p in self.power_hist if t + self.lag <= self.t]
        return (seen[-1][1], seen[-1][0]) if seen else (0.0, self.power_hist[0][0])


class _Loop:
    """The entity's cycle in miniature: every minute, and at the second a
    pulse should end; the maximum and the off are strong commands, a hold
    goes out on a 0.3-°C change at most every 180 s.  A refit runs (here,
    in line) after each cycle whose outcome says one is due."""

    def __init__(self, plant, *, setpoint=20.5, mode="stop", dose=FAST):
        self.enabled = True          # dosing decides (False: the ordinary modes)
        self.cfg = parameters.RegulationConfig(dose=dose)
        self.reg = regulation.FeedforwardPiRegulator(self.cfg)
        self.state = regulation.RegulationState(mode=mode)
        self.d = dosing.RoomDoser(self.cfg.dose, "r")
        self.plant, self.sp, self.now = plant, setpoint, plant.t
        self.t0 = plant.t
        self.slope = dosing.SlopeEstimator()
        self.last_sent = self.last_send_ts = None
        self.result = None
        self.wake = None
        self.rooms, self.pulses, self.lines, self.summaries, self.fits = [], [], [], [], []
        self.open_episodes_max = 0
        self.refit_s: list[float] = []

    def _send(self, cmd):
        lo, hi = self.cfg.min_target_c + 0.05, self.cfg.max_target_c - 0.05
        last = self.last_sent
        if last is None:
            go = True
        elif cmd <= lo or cmd >= hi or last <= lo or last >= hi:
            go = abs(cmd - last) > 0.05
        else:
            go = abs(cmd - last) >= 0.3 and self.now - self.last_send_ts >= 180.0
        if go:
            self.plant.command(self.now, cmd, cmd <= lo)
            self.d.note_command(self.now, cmd, min_c=self.cfg.min_target_c,
                                max_c=self.cfg.max_target_c, ok=True)
            self.last_sent, self.last_send_ts = cmd, self.now
        return go

    def _cycle(self, dt):
        self.now += dt
        self.plant.advance(dt)
        room, trv = round(self.plant.room, 2), round(self.plant.trv(), 1)
        self.slope.add(self.now, room)
        sl = self.slope.slope_c_per_s()
        kw = self.d.inputs(self.now, enabled=self.enabled, setpoint_c=self.sp, room_c=room,
                           room_slope_c_per_s=sl, state=self.state,
                           deadband_c_per_h=self.cfg.slope_deadband_c_per_h)
        r = self.reg.compute(self.sp, room, trv, dt, self.state, sl, **kw)
        self.state = r.new_state
        sent = self._send(r.target_for_tado_c)
        if r.fired_by and not sent:
            self.state.pulse_start_ts = None
        pct, pct_ts = self.plant.report()
        out = self.d.after_compute(self.now, r, setpoint_c=self.sp, room_c=room,
                                   room_slope_c_per_s=sl, trv_internal_c=trv, heating_pct=pct,
                                   heating_ts=pct_ts, window=False, boost=False, sent=sent)
        if r.fired_by:
            self.pulses.append([self.now, None, None, self.plant.room - self.sp])
        if r.dose_end and self.pulses and self.pulses[-1][1] is None:
            self.pulses[-1][1:3] = [self.now, r.dose_end]
        self.lines += [(self.now, ln) for ln in out.lines]
        if out.refit_due:
            started = time.perf_counter()
            line = self.d.refit_now(self.now)
            self.refit_s.append(time.perf_counter() - started)
            if line is not None:
                self.fits.append((self.now, line))
        self.rooms.append((self.now, self.plant.room - self.sp))
        self.summaries.append(self.d.summary())
        self.open_episodes_max = max(self.open_episodes_max, len(self.d.learner.episodes))
        self.result, self.wake = r, out.wake_in_s

    def run(self, minutes):
        end = self.now + 60.0 * minutes
        next_minute = self.now + 60.0
        while True:
            wake_at = self.now + self.wake if self.wake is not None else None
            t_next = next_minute if wake_at is None else min(next_minute, wake_at)
            if t_next > end + 1e-6:
                return
            if wake_at is None or t_next == next_minute:
                next_minute += 60.0
            self._cycle(t_next - self.now)

    def band(self, after_h):
        late = [e for t, e in self.rooms if t - self.t0 > after_h * 3600]
        return min(late), max(late), sum(late) / len(late)

    def kinds(self):
        return [ln.split(":")[1].strip() if ln.startswith("not learned") else "learned"
                for _, ln in self.lines]


class TestClosedLoop:
    def test_the_default_plant(self):
        """The model's own plant over 4 h: the pooled learner neither
        drifts up nor down (the HA-free twin of the entity test)."""
        loop = _Loop(_Plant(T0))
        lr = loop.d.learner
        trajectory = []
        for _ in range(24):
            loop.run(10)
            trajectory.append((lr.gain_c_per_open_min, lr.theta_s, lr.tau_s))
        assert len(loop.fits) >= 4
        for gain, theta, tau in trajectory:
            assert gain == pytest.approx(0.045, rel=0.15)
            assert 240.0 <= tau <= 840.0 and 120.0 <= theta <= 540.0
        assert lr.last_fit.gain == pytest.approx(0.045, rel=0.15)
        # In the dosing zone: down to its floor, up to about the aim.
        lo, hi, _ = loop.band(1)
        assert lo >= -loop.cfg.dose.zone_below_c - 0.02
        assert hi <= loop.cfg.dose.aim_over_c + 0.05

    def test_an_under_dosing_model_learns_and_the_room_comes_up(self):
        """M1, s15/s16's plant: gain 0.02 against 0.045 in use, delay 9 min
        against 5, spread 4 min against 8, cooling 0.3 °C/h.  The pooled fit
        finds the plant; the gain in use comes down one 15 % step at once
        (the owner's limit, then 12 h), the aim bias covers the rest and the
        room comes up (2.15.0 review: the room 0.08-0.13 under all day)."""
        loop = _Loop(_Plant(T0, fall_c_per_h=0.3, gain=0.02, theta_s=540.0, tau_s=240.0))
        lr = loop.d.learner
        loop.run(6 * 60)
        assert lr.learned and lr.last_fit.gain <= 0.03, lr.summary()
        assert lr.gain_c_per_open_min == pytest.approx(0.85 * 0.045, rel=1e-6)   # one step
        assert any("a step down possible from" in ln for _, ln in loop.fits)
        assert lr.theta_s > 300.0 and lr.tau_s < 480.0
        assert lr.bias_c > 0.0                                    # the landings were short
        _, _, mean = loop.band(3)
        assert mean > -0.05                                       # was -0.105
        assert loop.open_episodes_max <= 2

    def test_a_weak_slow_radiator(self):
        loop = _Loop(_Plant(T0, fall_c_per_h=0.4, gain=0.03, theta_s=240.0, tau_s=720.0))
        lr = loop.d.learner
        loop.run(12 * 60)
        assert lr.learned and lr.last_fit.gain < 0.04
        assert lr.gain_c_per_open_min < 0.045 and lr.bias_c > 0.0
        assert lr.tau_s > 480.0
        assert loop.band(3)[0] > -0.15

    def test_a_long_delay(self):
        loop = _Loop(_Plant(T0, fall_c_per_h=0.4, theta_s=900.0, tau_s=360.0))
        lr = loop.d.learner
        loop.run(12 * 60)
        assert lr.learned
        assert lr.theta_s >= 600.0
        assert loop.band(3)[0] > -0.15

    def test_late_heating_reports(self):
        """M6, s12_lagged_power.py 370: the first report above 0 comes 370 s
        late.  No pulse is taken for one that did nothing, none runs to its
        maximum, and the pulses are pooled."""
        loop = _Loop(_Plant(T0, report_lag_s=370.0))
        loop.run(8 * 60)
        assert "the valve never reported heating, so the pulse did nothing" not in loop.kinds()
        assert all(p[2] != "max" for p in loop.pulses if p[2])
        assert sum(r.usable for r in loop.d.learner.records) >= 8

    def test_the_first_pulse_after_a_hold(self):
        """M2, s17_relay_hold2.py 20.2 0.8 0.5: a hold at 50 % brings the
        room to its target, stops, settles.  Booked as fully open the hold
        read as a fast cooling: the first pulse fired 0.06 above the target
        and peaked +0.15 (the aim on the target).  Now it fires below the
        target and lands no more than 0.05 over the zone's aim."""
        loop = _Loop(_Plant(T0, room=20.2, fall_c_per_h=0.8, hold_frac=0.5), mode="hold")
        loop.run(5 * 60)
        assert loop.pulses, "never settled"
        start, _, _, error_at_fire = loop.pulses[0]
        assert error_at_fire < 0.0
        after = [e for t, e in loop.rooms if start <= t <= start + 3600]
        assert max(after) <= loop.cfg.dose.aim_over_c + 0.05

    def test_nothing_grows_without_bound(self):
        """12 h with the dead-start grid (production): the state stays small,
        each refit well under the executor's budget."""
        loop = _Loop(_Plant(T0, fall_c_per_h=0.3, gain=0.02, theta_s=540.0, tau_s=240.0),
                     dose=CFG)
        loop.run(12 * 60)
        lr = loop.d.learner
        assert loop.open_episodes_max <= 3
        assert len(lr.records) <= CFG.fit_window_pulses + 2
        assert len(lr.chains.intervals) <= 60
        data = json.dumps(loop.d.to_dict())
        assert len(data) < 20_000
        assert loop.refit_s and max(loop.refit_s) <= 1.5, max(loop.refit_s)
