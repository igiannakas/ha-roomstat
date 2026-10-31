"""Tests for the background PI(D) auto-tuner and the derivative brake.

Three layers:

1. The pieces: robust slope, episode analysis, the one-sided brake.
2. The tuner's safety contract: bounds, step limits, rate limits,
   confirmation of "more heat" moves, rollback, emergency detune,
   persistence.  Fuzzed where it matters.
3. Closed loop against ``plant_sim`` (room + Tado TRV with its own PI):
   the tuner must cut the measured overshoot and calm an oscillating room
   without ever leaving its bounds.
"""
from __future__ import annotations

import math
import random

import pytest

from tests.plant_sim import (
    PlantParams,
    ProxySettings,
    RoomSim,
    schedule_from,
)
from tests.pure_modules import autotune as A
from tests.pure_modules import parameters as P
from tests.pure_modules import regulation as R

CFG = P.AutotuneConfig()
DAY = 86400.0


def _tuner(kp=0.6, ki=0.002, td=0.0, deriv=True, cfg=None, stored=None):
    return A.Autotuner(cfg or P.AutotuneConfig(), A.Tuning(kp, ki, td),
                       derivative_allowed=deriv, stored=stored)


def _heatup(ts, overshoot, *, theta=780.0, rate=2.0, coast=1800.0, td=0.0,
            truncated=False, kp=0.6, ki=0.002, crossed=True):
    return A.HeatupMetrics(
        ts=ts, start_ts=ts - 3 * 3600, setpoint_c=20.0, step_c=1.5,
        overshoot_c=overshoot, theta_s=theta, rate_c_per_h=rate, demand0_c=2.0,
        coast_s=coast, coast_rise_c=0.3, reach_s=3000.0, kp=kp, ki=ki, td_s=td,
        truncated=truncated, crossed=crossed,
    )


def _force_more_heat(t, now):
    """Make the tuner raise Kp (a judged, "more heat" change)."""
    t._trim["kp"] = 1.4
    reason = t._maybe_update(now, "test")
    assert reason and t._pending is not None
    return A._tuning_from(t._pending["prev"])


def _hold(ts, *, mean=0.0, osc=False, amp=0.1, period=None, half=0, heating=0.5):
    return A.HoldMetrics(
        ts=ts, mean_error_c=mean, sd_c=amp / 2, half_cycles=half, period_s=period,
        amplitude_c=amp, heating_fraction=heating, oscillating=osc,
    )


# ---------------------------------------------------------------------------
# 1. Pieces
# ---------------------------------------------------------------------------

class TestSpikeFilter:
    def test_single_report_spike_is_dropped_even_if_reread(self):
        f = A.SpikeFilter()
        assert f.update(0, 20.0, 0) == 20.0
        # one wild report, re-read on three cycles (same report time)
        for k in range(1, 4):
            assert f.update(k * 60, 21.5, 60) == 20.0
        assert f.update(240, 20.05, 240) == 20.05

    def test_genuine_step_is_accepted_after_confirmation(self):
        f = A.SpikeFilter()
        f.update(0, 20.0, 0)
        assert f.update(60, 21.2, 60) == 20.0     # held back once
        assert f.update(120, 21.25, 120) == 21.25

    def test_fast_trend_is_followed(self):
        f = A.SpikeFilter()
        f.update(0, 20.0, 0)
        f.update(300, 21.1, 300)
        assert f.update(600, 22.2, 600) == 22.2

    def test_steady_room_after_genuine_jump_is_not_held(self):
        """HA sends no new report while the value is steady."""
        f = A.SpikeFilter()
        f.update(0, 20.0, 0)
        assert f.update(60, 18.8, 60) == 20.0          # held once
        for k in range(2, 20):                         # same report re-read
            v = f.update(k * 60, 18.8, 60)
        assert v == 18.8

    def test_aqara_style_half_degree_reports_pass(self):
        f = A.SpikeFilter()
        f.update(0, 20.0, 0)
        assert f.update(600, 20.55, 600) == 20.55

    def test_never_freezes(self):
        f = A.SpikeFilter(max_hold_s=900)
        f.update(0, 20.0, 0)
        f.update(60, 25.0, 60)
        f.update(120, 18.5, 120)                   # disagrees: still held
        assert f.update(1200, 23.0, 1200) == 23.0  # held too long: accept


class TestSlopeEstimator:
    def test_linear_ramp(self):
        est = A.SlopeEstimator()
        for k in range(15):
            est.add(k * 60.0, 19.0 + 0.02 * k)   # 1.2 °C/h
        assert est.slope_c_per_s() * 3600 == pytest.approx(1.2, rel=1e-6)

    def test_single_glitch_ignored(self):
        est = A.SlopeEstimator()
        for k in range(13):
            v = 20.0 + (1.5 if k == 12 else 0.0)  # last reading wild
            est.add(k * 60.0, v)
        assert abs(est.slope_c_per_s() * 3600) < 0.05

    def test_needs_half_a_window(self):
        est = A.SlopeEstimator()
        for k in range(4):
            est.add(k * 60.0, 20.0 + k)
        assert est.slope_c_per_s() is None

    def test_gap_resets(self):
        est = A.SlopeEstimator()
        for k in range(12):
            est.add(k * 60.0, 20.0)
        est.add(12 * 60.0 + 900.0, 25.0)          # 15 min gap
        assert est.slope_c_per_s() is None

    def test_unchanged_value_decays_slope(self):
        """A sensor that stops changing must not leave the brake engaged."""
        est = A.SlopeEstimator()
        t = 0.0
        v = 19.0
        for k in range(20):                       # 0.1 °C sensor rising at 2 °C/h
            t = k * 60.0
            v = round((19.0 + 2.0 * t / 3600) * 10) / 10
            est.add(t, v)
        assert est.slope_c_per_s() * 3600 > 1.0
        for k in range(20, 40):                   # then flat
            est.add(k * 60.0, v)
        assert abs(est.slope_c_per_s() * 3600) < 0.2

    def test_glitch_reread_over_several_cycles(self):
        est = A.SlopeEstimator()
        for k in range(15):
            wild = 6 <= k <= 9                    # one report seen on 4 cycles
            est.add(k * 60.0, 21.5 if wild else 20.0, 360.0 if wild else k * 60.0)
        assert abs(est.slope_c_per_s() * 3600) < 0.05

    def test_glitch_spanning_two_reports_is_bounded(self):
        """Two wild reports in a row get through the spike filter, but the
        Theil-Sen median keeps the brake's slope small."""
        est = A.SlopeEstimator()
        for k in range(15):
            wild = k in (6, 7)
            est.add(k * 60.0, 21.5 if wild else 20.0, k * 60.0)
        assert abs(est.slope_c_per_s() * 3600) < 0.05

    def test_rejects_non_finite(self):
        est = A.SlopeEstimator()
        for k in range(12):
            est.add(k * 60.0, float("nan") if k == 5 else 20.0)
        assert est.slope_c_per_s() == 0.0


def _clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _synthetic_heatup(theta_min=12.0, rate=2.0, sp=20.0, y0=18.5, overshoot=0.3,
                      cross_after_reach_min=-10.0, minutes=240, glitch=False):
    """Room: flat for theta, ramps at `rate`, then eases into sp + overshoot."""
    samples = []
    cross_idx = None
    t_reach = theta_min + (sp - y0) / rate * 60.0
    for k in range(minutes):
        t = k * 60.0
        m = float(k)
        if m < theta_min:
            y = y0
        elif m < t_reach:
            y = y0 + rate * (m - theta_min) / 60.0
        else:
            y = sp + overshoot * (1 - math.exp(-(m - t_reach) / 15.0))
            if m > t_reach + 60:
                y -= 0.002 * (m - t_reach - 60)
        if glitch and k == 30:
            y += 1.5
        d = 2.0 if m < t_reach + cross_after_reach_min else -0.5
        if d <= 0 and cross_idx is None:
            cross_idx = k
        samples.append((t, y, d))
    return samples, cross_idx


class TestAnalyseHeatup:
    def test_dead_time_and_rate(self):
        s, cross = _synthetic_heatup()
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002))
        assert m.theta_s / 60 == pytest.approx(12.0, abs=1.5)
        assert m.rate_c_per_h == pytest.approx(2.0, rel=0.05)
        assert m.overshoot_c == pytest.approx(0.3, abs=0.02)
        assert m.coast_s is not None and m.coast_s > 0

    def test_glitch_does_not_move_dead_time(self):
        s, cross = _synthetic_heatup(glitch=True)
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002))
        assert m.theta_s / 60 == pytest.approx(12.0, abs=1.5)

    def test_real_mild_morning_gives_a_dead_time(self):
        """Living room, 15 Oct 2026, 06:00 night 18 -> comfort 20 from
        19.25 °C: a 0.75 °C climb, under the old 0.8 °C bar.  Room sensor
        and TRV demand (command - TRV reading) once a minute; the stop came
        at 06:18.  The room started rising about 5 min after the step."""
        room = [19.25, 19.25, 19.25, 19.26, 19.27, 19.29, 19.31, 19.33, 19.35, 19.39,
                19.42, 19.45, 19.48, 19.5, 19.53, 19.56, 19.59, 19.61, 19.65, 19.68,
                19.72, 19.74, 19.75, 19.78, 19.8, 19.83, 19.86, 19.88, 19.9, 19.91,
                19.93, 19.94, 19.96, 19.98, 19.99, 20.0, 20.01, 20.01, 20.02, 20.03,
                20.04, 20.05, 20.05, 20.06, 20.06, 20.07, 20.07, 20.08, 20.08, 20.08,
                20.08, 20.09, 20.1, 20.1, 20.1, 20.1, 20.1, 20.1, 20.1, 20.1, 20.1,
                20.1, 20.1, 20.1, 20.1, 20.09, 20.09, 20.09, 20.08, 20.08, 20.08,
                20.08, 20.08, 20.08, 20.08, 20.08, 20.08, 20.07, 20.07, 20.07, 20.07,
                20.06, 20.06, 20.06, 20.06, 20.05, 20.05, 20.05, 20.04, 20.04]
        demand = [3.7, 3.7, 3.7, 3.7, 3.7, 3.6, 3.4, 3.5, 3.3, 2.7, 3.1, 2.9, 2.2, 2.6,
                  2.5, 2.0, 2.2, 2.0] + [-7.0] * (len(room) - 18)
        samples = [(k * 60.0, y, d) for k, (y, d) in enumerate(zip(room, demand))]
        kw = dict(start_ts=0.0, setpoint_c=20.0, cross_ts=18 * 60.0,
                  tuning=A.Tuning(4.0, 0.003, 1164.0))
        m = A.analyse_heatup(samples, cfg=CFG, **kw)
        assert m.theta_s / 60 == pytest.approx(4.7, abs=1.0)
        assert m.rate_c_per_h == pytest.approx(1.8, abs=0.2)
        assert m.overshoot_c == pytest.approx(0.1, abs=0.02)
        old = A.analyse_heatup(samples, cfg=P.AutotuneConfig(theta_min_step_c=0.8), **kw)
        assert old.theta_s is None           # what 2.4 learned from it

    def test_a_later_rise_is_not_this_heatups_coast(self):
        """Living room, 24 Oct 2026, 11:18 comfort 20 from 19.03 °C, one
        reading a minute; the stop came at 37.5 min.  The room's own coast
        stalled at 19.97 about 20 min later, then lunch guests (CO2 490 ->
        650 ppm) took it to 20.49 with the valve shut.  2.9.1 booked it as
        +0.48 °C, a 33-min coast and a 46-min dead time; the braking time went
        10.3 -> 13.1 min and the next morning stopped 0.35 °C short."""
        room = [
            19.035, 19.03, 19.025, 19.025, 19.025, 19.02, 19.025, 19.04, 19.05, 19.055, 19.065, 19.085,
            19.1, 19.12, 19.14, 19.165, 19.185, 19.21, 19.23, 19.25, 19.27, 19.285, 19.315, 19.34,
            19.365, 19.395, 19.42, 19.445, 19.465, 19.49, 19.515, 19.54, 19.57, 19.595, 19.615, 19.63,
            19.655, 19.675, 19.705, 19.725, 19.75, 19.775, 19.8, 19.82, 19.845, 19.865, 19.885, 19.895,
            19.905, 19.925, 19.935, 19.94, 19.945, 19.955, 19.96, 19.97, 19.97, 19.975, 19.975, 19.975,
            19.975, 19.975, 19.975, 19.97, 19.97, 19.97, 19.96, 19.95, 19.955, 19.96, 19.97, 19.98,
            19.99, 20, 20.005, 20.02, 20.04, 20.075, 20.12, 20.16, 20.195, 20.23, 20.255, 20.285,
            20.32, 20.35, 20.375, 20.395, 20.415, 20.43, 20.435, 20.435, 20.435, 20.46, 20.475, 20.485,
            20.475, 20.47, 20.46, 20.455, 20.455, 20.465, 20.475, 20.475, 20.455, 20.43, 20.41, 20.385,
            20.37, 20.355, 20.345, 20.335, 20.32, 20.31, 20.3, 20.29, 20.28, 20.27, 20.265, 20.255,
            20.245, 20.235, 20.22, 20.205, 20.2, 20.195, 20.205, 20.22, 20.215, 20.21, 20.2, 20.2,
            20.215, 20.24, 20.26, 20.26, 20.26, 20.25, 20.245, 20.255, 20.275, 20.275,
        ]
        cross = 37.55 * 60.0
        samples = [(k * 60.0, y, 2.0 if k * 60.0 < cross else -2.0) for k, y in enumerate(room)]
        m = A.analyse_heatup(samples, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross, tuning=A.Tuning(3.0, 0.00056, 620.0))
        assert m.overshoot_c == pytest.approx(-0.02, abs=0.02)
        assert m.coast_s / 60 == pytest.approx(11.5, abs=1.5)
        assert m.theta_s / 60 == pytest.approx(10.5, abs=1.5)
        assert m.rate_c_per_h == pytest.approx(1.5, abs=0.15)
        assert m.later_rise_c == pytest.approx(0.5, abs=0.03)

    def _with_later_rise(self, overshoot=0.1, rise_c=0.6, after_min=80.0):
        """The synthetic heat-up, plus +rise_c over 30 min starting
        after_min after the room reached the target (people coming in)."""
        s, cross = _synthetic_heatup(overshoot=overshoot)
        t_reach = (12.0 + (20.0 - 18.5) / 2.0 * 60.0) * 60.0
        t0 = t_reach + after_min * 60.0
        bumped = [(t, y + rise_c * _clamp01((t - t0) / 1800.0), d) for t, y, d in s]
        return s, bumped, cross

    def test_a_rise_after_the_coast_stalled_changes_nothing_learned(self):
        base, bumped, cross = self._with_later_rise()
        kw = dict(start_ts=0.0, setpoint_c=20.0, cfg=CFG, cross_ts=cross * 60.0,
                  tuning=A.Tuning(0.6, 0.002))
        m0 = A.analyse_heatup(base, **kw)
        m1 = A.analyse_heatup(bumped, **kw)
        assert m0.later_rise_c is None
        assert m1.overshoot_c == pytest.approx(m0.overshoot_c, abs=0.01)
        assert m1.coast_s == pytest.approx(m0.coast_s, rel=0.05)
        assert m1.theta_s == pytest.approx(m0.theta_s, abs=30.0)
        assert m1.rate_c_per_h == pytest.approx(m0.rate_c_per_h, rel=0.02)
        # +0.6 on a room that had meanwhile cooled 0.1 below its own peak.
        assert m1.later_rise_c == pytest.approx(0.5, abs=0.03)

    def test_a_coast_that_keeps_creeping_is_kept_whole(self):
        """No new rise: the slow tail still counts (as before 2.9.2)."""
        s, cross = _synthetic_heatup(overshoot=0.3)
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002))
        assert m.later_rise_c is None
        assert m.overshoot_c == pytest.approx(max(y for _, y, _ in s) - 20.0, abs=0.002)

    def test_a_stop_inside_the_dead_time_keeps_the_rise_that_follows(self):
        """Told to stop before the room had visibly moved: the heat already
        sent still arrives, and that rise is this heat-up's coast."""
        samples = []
        for k in range(150):
            m = float(k)
            y = 19.0 if m < 12 else 19.0 + 0.8 * (1 - math.exp(-(m - 12) / 20.0))
            samples.append((k * 60.0, y, 2.0 if k < 6 else -1.0))
        r = A.analyse_heatup(samples, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=6 * 60.0, tuning=A.Tuning(0.6, 0.002))
        assert r.later_rise_c is None
        assert r.overshoot_c == pytest.approx(-0.2, abs=0.01)

    def test_the_status_says_what_was_not_counted(self):
        _, bumped, cross = self._with_later_rise()
        t = _tuner(td=600.0)
        m = A.analyse_heatup(bumped, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002, 600.0))
        t._on_heatup(m, m.ts)
        assert "later rise +0.50 °C not counted" in t.last_event

    def test_learning_stored_before_2_9_2_is_kept(self):
        """Stored heat-ups have no later_rise_c: they load as they are, so
        installing the update does not restart learning."""
        old = _tuner(kp=3.0, ki=0.001, td=660.0)
        for k in range(3):
            old._on_heatup(_heatup(DAY * (k + 1), 0.05, coast=780.0, td=660.0), DAY * (k + 1))
        data = old.as_dict()
        for h in data["heatups"]:
            h.pop("later_rise_c")
        new = _tuner(kp=3.0, ki=0.001, td=660.0, stored=data)
        for name in ("kp", "ki", "td_s"):
            assert getattr(new.active_tuning(), name) == pytest.approx(
                getattr(old.active_tuning(), name), rel=1e-4)
        assert len(new._heatups) == 3
        assert all(h.later_rise_c is None for h in new._heatups)
        assert new._model()["coast_s"] == pytest.approx(old._model()["coast_s"])

    def test_small_step_gives_no_dead_time(self):
        s, cross = _synthetic_heatup(y0=19.5)
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002))
        assert m.theta_s is None           # 0.5 °C step: too small to trust
        assert m.overshoot_c is not None   # ...but overshoot still counts

    def test_truncated_low_peak_is_unknown(self):
        """A cut-off episode must not argue for less braking."""
        s, cross = _synthetic_heatup(overshoot=0.1)
        cut = [x for x in s if x[0] <= (cross + 20) * 60]
        m = A.analyse_heatup(cut, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002),
                             truncated=True)
        assert m.overshoot_c is None
        assert m.truncated                 # any coast is only a lower bound

    def test_truncated_high_peak_is_kept(self):
        s, cross = _synthetic_heatup(overshoot=0.8)
        cut = [x for x in s if x[0] <= (cross + 20) * 60]
        m = A.analyse_heatup(cut, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002),
                             truncated=True)
        assert m.overshoot_c is not None and m.overshoot_c > CFG.overshoot_target_c
        assert m.truncated                 # its coast only counts as a lower bound

    def test_two_sample_glitch_does_not_inflate_overshoot(self):
        s, cross = _synthetic_heatup(overshoot=0.15)
        k = cross + 30
        s[k] = (s[k][0], s[k][1] + 1.5, s[k][2])
        s[k + 1] = (s[k + 1][0], s[k + 1][1] + 1.5, s[k + 1][2])
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002))
        assert m.overshoot_c == pytest.approx(0.15, abs=0.05)

    def test_crossed_flag(self):
        s, cross = _synthetic_heatup()
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=None, tuning=A.Tuning(0.6, 0.002))
        assert not m.crossed and m.coast_s is None

    def test_too_short(self):
        assert A.analyse_heatup([(0, 18, 1)] * 5, start_ts=0, setpoint_c=20, cfg=CFG,
                                cross_ts=None, tuning=A.Tuning(0.6, 0.002)) is None


class TestAnalyseHold:
    def _samples(self, fn, hours=12, noise=0.0, seed=1):
        rng = random.Random(seed)
        return [(k * 60.0, fn(k * 60.0) + rng.gauss(0, noise), 0.5)
                for k in range(int(hours * 60))]

    def test_detects_oscillation(self):
        s = self._samples(lambda t: 0.4 * math.sin(2 * math.pi * t / 7200))
        m = A.analyse_hold(s, cfg=CFG, tuning=A.Tuning(0.6, 0.002))
        assert m.oscillating
        assert m.period_s == pytest.approx(7200, rel=0.1)

    def test_noise_is_not_oscillation(self):
        s = self._samples(lambda t: 0.0, noise=0.05)
        m = A.analyse_hold(s, cfg=CFG, tuning=A.Tuning(0.6, 0.002))
        assert not m.oscillating

    def test_slow_sawtooth_with_bursts_is_oscillation(self):
        """Short heating bursts + long passive cool-downs (seen in sims)."""
        def e(t):
            ph = (t % 32400) / 32400
            return -0.7 + 1.3 * ph if ph < 0.9 else 0.6 - 13 * (ph - 0.9)
        s = [(t, e(t), (1.0 if (t % 32400) / 32400 > 0.9 else -1.0))
             for t, _, _ in self._samples(lambda t: 0.0, hours=24)]
        m = A.analyse_hold(s, cfg=CFG, tuning=A.Tuning(0.6, 0.002))
        assert m.oscillating

    def test_very_slow_cycle_is_not_oscillation(self):
        """A daily-ish swing (sun, routine) is not the controller."""
        s = self._samples(lambda t: 0.4 * math.sin(2 * math.pi * t / (14 * 3600)), hours=24)
        m = A.analyse_hold(s, cfg=CFG, tuning=A.Tuning(0.6, 0.002))
        assert not m.oscillating

    def test_offset(self):
        s = self._samples(lambda t: 0.25, noise=0.02)
        m = A.analyse_hold(s, cfg=CFG, tuning=A.Tuning(0.6, 0.002))
        assert m.mean_error_c == pytest.approx(0.25, abs=0.02)
        assert not m.oscillating


# ---------------------------------------------------------------------------
# 2. Safety contract
# ---------------------------------------------------------------------------

def _expected_limits(t: A.Autotuner, name: str) -> tuple[float, float]:
    """Documented bounds, computed independently of the tuner's own code:
    hard bounds intersected with the bounds relative to the configured value,
    widened to include the configured value itself."""
    c, b = t.cfg, t.baseline
    if name == "kp":
        lo, hi, base = max(c.kp_min, b.kp * c.kp_rel_min), min(c.kp_max, max(b.kp * c.kp_rel_max, c.kp_min)), b.kp
    elif name == "ki":
        if b.ki <= 0:
            return 0.0, 0.0
        lo, hi, base = max(c.ki_min, b.ki * c.ki_rel_min), min(c.ki_max, max(b.ki * c.ki_rel_max, c.ki_min)), b.ki
    else:
        lo, hi, base = 0.0, c.td_max_s, b.td_s
    lo = min(lo, hi)
    return min(lo, base), max(hi, base)


def _within_bounds(t: A.Autotuner) -> None:
    a = t.active_tuning()
    for name in ("kp", "ki"):
        lo, hi = _expected_limits(t, name)
        assert lo - 1e-12 <= getattr(a, name) <= hi + 1e-12, (name, getattr(a, name), lo, hi)
    if t.derivative_allowed:
        lo, hi = _expected_limits(t, "td_s")
        assert lo - 1e-9 <= a.td_s <= hi + 1e-9
    assert a.td_s <= max(t.cfg.td_max_s, t.baseline.td_s)


class TestSafety:
    def test_starts_at_baseline(self):
        t = _tuner()
        assert t.active_tuning() == A.Tuning(0.6, 0.002, 0.0)
        assert t.status == A.STATUS_LEARNING

    @pytest.mark.parametrize("seed", range(30))
    def test_fuzz_never_leaves_bounds_or_step_limits(self, seed):
        """Random (including absurd) evidence for weeks: bounds always hold,
        and every change except a rollback respects the per-step limits."""
        rng = random.Random(seed)
        t = _tuner(kp=rng.choice([0.0, 0.3, 0.6, 1.5, 4.0]),
                   ki=rng.choice([0.0, 0.001, 0.004, 0.02]),
                   td=rng.choice([0.0, 600.0]), deriv=rng.random() < 0.8)
        c = t.cfg
        now = 0.0
        rollbacks = 0
        for _ in range(300):
            now += rng.uniform(600, 12 * 3600)
            t._now = now
            before = t.active_tuning()
            if rng.random() < 0.6:
                m = _heatup(now, rng.uniform(-1.5, 3.0), theta=rng.uniform(100, 5000),
                            rate=rng.uniform(0.01, 20), coast=rng.choice([None, rng.uniform(0, 20000)]),
                            td=before.td_s, truncated=rng.random() < 0.2,
                            crossed=rng.random() < 0.8)
                if rng.random() < 0.1:
                    m.overshoot_c = None
                t._on_heatup(m, now)
            else:
                t._on_hold(_hold(now, mean=rng.uniform(-1, 1), osc=rng.random() < 0.3,
                                          amp=rng.uniform(0, 1.5), period=rng.uniform(600, 40000),
                                          half=rng.randint(0, 8), heating=rng.random()), now)
            after = t.active_tuning()
            _within_bounds(t)
            if t.summary()["rollbacks"] != rollbacks:
                # A rollback restores the previous values (and an emergency
                # may then take one step from there): exempt from step limits.
                rollbacks = t.summary()["rollbacks"]
                continue
            if after.kp != before.kp:
                assert before.kp * c.kp_step_down - 1e-12 <= after.kp <= before.kp * c.kp_step_up + 1e-12
            if after.ki != before.ki:
                assert before.ki * c.ki_step_down - 1e-15 <= after.ki <= before.ki * c.ki_step_up + 1e-15
            if after.td_s > before.td_s + 1e-9:          # more braking
                step = max(c.td_step_min_s, c.td_step_frac * before.td_s)
                assert after.td_s - before.td_s <= step + 1e-6
            elif after.td_s < before.td_s - 1e-9:        # less braking: smoothed
                step = max(c.td_less_step_min_s, c.td_less_step_frac * before.td_s)
                assert before.td_s - after.td_s <= step + 1e-6

    def test_more_braking_steps_at_the_old_pace(self):
        t = _tuner(td=600.0)
        assert t._td_step(600.0, 1800.0) == pytest.approx(900.0)    # 5 min (50 %)
        assert t._td_step(1200.0, 1500.0) == pytest.approx(1500.0)  # all of a small gap
        assert t._td_step(600.0, 600.0) == pytest.approx(600.0)

    def test_less_braking_is_a_moving_average(self):
        """28 Oct 2026, living room: the coast model said 8.0 min with 13.1
        in use, and the full step jumped there; a smoothed step goes 30 %
        of the way, and later empty mornings carry on from there."""
        t = _tuner(td=786.0)
        steps = [786.0]
        while True:
            nxt = t._td_step(steps[-1], 480.0)
            if nxt == steps[-1]:
                break
            steps.append(nxt)
        assert steps[1] == pytest.approx(786.0 - 0.3 * 306.0)           # 13.1 -> 11.6
        assert all(b < a for a, b in zip(steps, steps[1:]))
        # It stops within the dead-band above the target: a little more
        # braking than the model, never less.
        assert 480.0 < steps[-1] < 480.0 + CFG.td_deadband_s
        assert len(steps) == 4

    def test_less_braking_is_capped(self):
        t = _tuner(td=1500.0)
        # 30 % of a 20-min gap would be 6 min; 15 % of 25 min is the most.
        assert t._td_step(1500.0, 300.0) == pytest.approx(1500.0 - 225.0)
        # A short braking time still moves at least towards the minimum step.
        assert t._td_step(300.0, 0.0) == pytest.approx(240.0)           # 1 min, not 45 s

    def test_less_braking_waits_for_a_gap_beyond_the_dead_band(self):
        t = _tuner(td=600.0)
        assert t._td_step(600.0, 600.0 - CFG.td_deadband_s + 1.0) == 600.0
        assert t._td_step(600.0, 600.0 - CFG.td_deadband_s) < 600.0

    def test_a_small_less_braking_step_is_still_on_trial(self):
        """Its 54-s step is under the 2-min dead-band of a more-braking step,
        yet it is applied, rate-limited at 12 h and judged like any move to
        more heat."""
        t = _tuner(td=600.0)
        t._targets = lambda: A.Tuning(0.6, 0.002, 420.0)
        t._last_change_ts = DAY
        assert t._maybe_update(DAY + 3600, "test") is None             # < 12 h
        assert "a change towards more heat waits 12 h" in t._update_note
        assert t._maybe_update(DAY + 13 * 3600, "test") is not None
        assert t.active_tuning().td_s == pytest.approx(600.0 - 0.3 * 180.0)
        assert t._pending is not None
        assert "braking 10.0→9.1 min, a step towards 7.0 (on trial" in t._update_note

    def test_more_heat_needs_confirmation(self):
        """A single sag must not raise Kp (no braking active)."""
        t = _tuner(deriv=False)
        t._on_heatup(_heatup(DAY, -0.5, coast=None), DAY)
        assert t.active_tuning().kp == 0.6
        t._on_heatup(_heatup(2 * DAY, -0.5, coast=None), 2 * DAY)
        # second consecutive sag: trim raised; Kp may now move up (bounded)
        assert t._trim["kp"] > 1.0

    def test_rate_limit_for_more_heat(self):
        t = _tuner(deriv=False)
        t._last_change_ts = DAY
        t._trim["kp"] = 1.4
        assert t._maybe_update(DAY + 3600, "test") is None        # < 12 h
        assert t._maybe_update(DAY + 13 * 3600, "test") is not None
        assert t.active_tuning().kp == pytest.approx(0.6 * 1.10)

    def test_gentler_moves_allowed_sooner(self):
        t = _tuner()
        t._last_change_ts = DAY
        t._trim["ki"] = 0.5
        assert t._maybe_update(DAY + 1800, "test") is None         # < 3 h
        assert t._maybe_update(DAY + 4 * 3600, "test") is not None

    def test_first_heatup_starts_braking_second_moves_ki(self):
        t = _tuner()
        reason = t._on_heatup(_heatup(DAY, 0.6, coast=1800), DAY)
        a = t.active_tuning()
        assert reason and a.td_s == pytest.approx(300.0)           # first 5-min step
        assert a.ki == 0.002                # one dead time is not enough for SIMC
        t._on_heatup(_heatup(2 * DAY, 0.5, coast=1800, td=a.td_s), 2 * DAY)
        assert t.active_tuning().ki < 0.002                         # towards SIMC
        assert t.active_tuning().kp == 0.6

    def test_lower_bound_coast_only_raises_td(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6, coast=2400, truncated=True), DAY)
        td1 = t.active_tuning().td_s
        assert td1 > 0
        # a later, shorter lower bound (old one aged out) must not lower Td
        t._heatups = [_heatup(2 * DAY, 0.6, coast=300, truncated=True, td=td1)]
        t._last_change_ts = 0
        t._maybe_update(3 * DAY, "test")
        assert t.active_tuning().td_s >= td1

    def test_lower_bounds_below_complete_median_are_ignored(self):
        t = _tuner()
        t._heatups = [_heatup(DAY, 0.6, coast=600, truncated=True),
                      _heatup(2 * DAY, 0.6, coast=1200)]
        assert t._model()["coast_s"] == 1200
        assert not t._model()["coast_is_lower_bound"]

    def test_lower_bounds_above_complete_median_count(self):
        t = _tuner()
        t._heatups = [_heatup(DAY, 0.6, coast=2400, truncated=True),
                      _heatup(2 * DAY, 0.6, coast=2400, truncated=True),
                      _heatup(3 * DAY, 0.6, coast=600)]
        assert t._model()["coast_s"] == 2400

    def test_cut_short_in_dead_time_gives_no_dead_time(self):
        s, _ = _synthetic_heatup(theta_min=40.0, rate=2.4, y0=18.0)
        cut = [x for x in s if x[0] <= 46 * 60]
        m = A.analyse_heatup(cut, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=None, tuning=A.Tuning(0.6, 0.002), truncated=True)
        assert m is None or m.theta_s is None

    def test_rollback_on_worse_overshoot(self):
        t = _tuner(deriv=False)
        t._on_heatup(_heatup(DAY, 0.2), DAY)
        prev = _force_more_heat(t, 2 * DAY)          # Kp up, being judged
        assert t.active_tuning().kp > prev.kp
        t._on_heatup(_heatup(3 * DAY, 0.9), 3 * DAY)  # needs two episodes
        reason = t._on_heatup(_heatup(4 * DAY, 0.9), 4 * DAY)
        assert reason.startswith("rolled back")
        assert t.active_tuning() == prev
        assert t.status == A.STATUS_TUNING              # no freeze
        assert t._limits("kp") == _expected_limits(t, "kp")   # nothing blocked

    def test_after_rollback_it_tries_again_on_fresh_evidence(self):
        t = _tuner(deriv=False)
        prev = _force_more_heat(t, DAY)
        t._rollback(DAY + 3600, "test")
        assert t.active_tuning() == prev
        assert t._votes == dict.fromkeys(t._votes, 0)   # old evidence cleared
        assert t._maybe_update(DAY + 13 * 3600, "test") is None   # no new evidence
        t._trim["kp"] = 1.4                              # fresh evidence arrives
        assert t._maybe_update(DAY + 2 * 3600, "test") is None    # normal 12 h rate limit
        assert t._maybe_update(DAY + 14 * 3600, "test") is not None
        assert t.active_tuning().kp > prev.kp            # same move, tried again
        assert t._pending is not None                    # and on trial again

    def test_gentle_moves_allowed_soon_after_rollback(self):
        t = _tuner(deriv=False)
        _force_more_heat(t, DAY)
        t._rollback(DAY + 3600, "test")
        before = t.active_tuning()
        t._trim["ki"] = 0.5
        assert t._maybe_update(DAY + 3600 + 4 * 3600, "test") is not None
        assert t.active_tuning().ki < before.ki

    def test_rollback_on_new_oscillation(self):
        t = _tuner(deriv=False)
        prev = _force_more_heat(t, DAY)
        reason = t._on_hold(_hold(DAY + 6 * 3600, osc=True, amp=0.2, period=3600, half=4),
                            DAY + 6 * 3600)
        assert reason.startswith("rolled back")
        assert t.active_tuning() == prev

    def test_gentle_change_is_never_rolled_back(self):
        """More braking / lower Ki cannot cause a sunny heat-up's overshoot."""
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.3), DAY)          # Td up, Ki down
        changed = t.active_tuning()
        assert changed.td_s > 0 and t._pending is None
        reason = t._on_heatup(_heatup(2 * DAY, 1.2, td=changed.td_s), 2 * DAY)
        assert not (reason or "").startswith("rolled back")
        assert t.active_tuning().td_s >= changed.td_s
        assert t.summary()["rollbacks"] == 0

    def test_emergency_still_works_after_rollback(self):
        t = _tuner(kp=1.0, ki=0.003, deriv=False)
        _force_more_heat(t, DAY)
        t._on_hold(_hold(DAY + 6 * 3600, osc=True, amp=0.2, period=3600, half=4), DAY + 6 * 3600)
        assert t.summary()["rollbacks"] == 1
        before = t.active_tuning()
        now = DAY + 7 * 3600
        t._now = now
        reason = t._on_hold(_hold(now, osc=True, amp=0.6, period=3600, half=5), now)
        assert reason is not None
        a = t.active_tuning()
        assert a.kp < before.kp and a.ki < before.ki

    def test_reset_and_baseline_change_after_rollback(self):
        t = _tuner(deriv=False)
        _force_more_heat(t, DAY)
        t._rollback(DAY + 3600, "test")
        t.reset()
        assert t.active_tuning() == A.Tuning(0.6, 0.002, 0.0)
        _force_more_heat(t, 3 * DAY)
        t._rollback(3 * DAY + 3600, "test")
        t.set_baseline(A.Tuning(0.4, 0.001, 0.0), False)
        assert t.active_tuning() == A.Tuning(0.4, 0.001, 0.0)

    def test_emergency_never_reduces_braking(self):
        t = _tuner(td=1800.0)
        t._trim["td_s"] = 0.5                         # target well below current Td
        t._heatups = [_heatup(DAY, 0.1, coast=1800)]
        t._now = DAY
        reason = t._on_hold(_hold(DAY, osc=True, amp=0.6, period=3600, half=5), DAY)
        assert reason is not None
        assert t.active_tuning().td_s >= 1800.0

    def test_cut_short_stall_does_not_relax_braking(self):
        t = _tuner(td=1200.0)
        for k in range(1, 5):
            t._on_heatup(_heatup(k * DAY, -0.3, td=1200.0, truncated=True), k * DAY)
        assert t._trim["td_s"] == 1.0

    def test_manual_brake_stall_does_not_raise_kp(self):
        t = _tuner(td=1200.0, deriv=False)
        for k in range(1, 6):
            t._on_heatup(_heatup(k * DAY, -0.4, td=1200.0), k * DAY)
        assert t._trim["kp"] == 1.0
        assert t.active_tuning().kp == 0.6

    def test_underpowered_room_does_not_raise_kp(self):
        """Never reached the target with demand still positive (cold day)."""
        t = _tuner(deriv=False)
        for k in range(1, 6):
            t._on_heatup(_heatup(k * DAY, -0.8, coast=None, crossed=False), k * DAY)
        assert t._trim["kp"] == 1.0

    @pytest.mark.parametrize("kp,ki", [(0.0, 0.002), (5.0, 0.002), (0.6, 0.02), (0.6, 0.0)])
    def test_out_of_range_baseline_starts_exactly_there(self, kp, ki):
        t = _tuner(kp=kp, ki=ki)
        assert t.active_tuning() == A.Tuning(kp, ki, 0.0)

    def test_old_stored_freeze_and_blocks_are_ignored(self):
        """State saved by 1.5.0 (with a freeze and a block) loads cleanly."""
        t = _tuner(deriv=False)
        d = t.as_dict()
        d["freeze_until"] = 9e12
        d["blocked"] = {"kp": [0.6, 9e12]}
        t2 = _tuner(deriv=False, stored=d)
        assert t2.status != "frozen"
        assert t2._limits("kp") == _expected_limits(t2, "kp")

    def test_emergency_detune_bypasses_rate_limit(self):
        t = _tuner(kp=1.5, ki=0.004)
        t._last_change_ts = DAY
        t._now = DAY + 60
        reason = t._on_hold(_hold(DAY + 60, osc=True, amp=0.6, period=5000, half=5), DAY + 60)
        assert reason is not None
        a = t.active_tuning()
        assert a.kp < 1.5 and a.ki < 0.004

    def test_baseline_change_restarts(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        assert t.active_tuning() != A.Tuning(0.6, 0.002, 0.0)
        t.set_baseline(A.Tuning(0.7, 0.002, 0.0), True)
        assert t.active_tuning() == A.Tuning(0.7, 0.002, 0.0)
        assert t.summary()["updates"] == 0

    def test_integral_off_is_respected(self):
        t = _tuner(ki=0.0)
        for k in range(1, 6):
            t._on_heatup(_heatup(k * DAY, 0.6), k * DAY)
            t._on_hold(_hold(k * DAY + 4 * 3600, mean=0.4), k * DAY + 4 * 3600)
        assert t.active_tuning().ki == 0.0

    def test_derivative_not_allowed(self):
        t = _tuner(deriv=False, td=0.0)
        for k in range(1, 6):
            t._on_heatup(_heatup(k * DAY, 0.8), k * DAY)
        assert t.active_tuning().td_s == 0.0

    def test_derivative_disallowed_uses_manual_td(self):
        t = _tuner(deriv=False, td=600.0)
        assert t.active_tuning().td_s == 600.0

    def test_warm_room_hold_never_raises_ki(self):
        """A too-warm room (coasting heat) is not an integral problem."""
        t = _tuner()
        for k in range(1, 8):
            t._on_hold(_hold(k * DAY, mean=-0.4), k * DAY)
        assert t._trim["ki"] == 1.0

    def test_cold_room_hold_raises_ki_after_confirmation(self):
        t = _tuner()
        t._on_hold(_hold(DAY, mean=0.3), DAY)
        assert t._trim["ki"] == 1.0
        t._on_hold(_hold(DAY + 4 * 3600, mean=0.3), DAY + 4 * 3600)
        assert t._trim["ki"] > 1.0

    def test_dosed_cycles_make_no_hold_window(self):
        """A room settled under dosed top-ups: pulses, not the PI, are in
        charge, so no hold window is collected or judged on its cycles
        (2.15.0); the same cycles undosed start one."""
        def feed(t, dosed):
            for k in range(6 * 60):
                ts = DAY + 60.0 * k
                room = 20.0 - 0.05 * math.sin(k / 9.0)
                t.observe(A.AutotuneSample(
                    ts, 20.0, room, room - 3.0, 25.0 if k % 30 < 4 else 5.0,
                    room_temp_ts=ts, dosed=dosed,
                ))
        dosed, plain = _tuner(), _tuner()
        feed(dosed, True)
        feed(plain, False)
        assert dosed._hold is None and not dosed._holds
        assert plain._hold is not None or plain._holds

    def test_observe_survives_garbage(self):
        t = _tuner()
        for ts, sp, room, tado, cmd in [
            (float("nan"), 20, 19, 21, 22), (1.0, None, 19, 21, 22),
            (2.0, 20, None, 21, 22), (3.0, 20, float("inf"), None, None),
            (4.0, float("nan"), 19, 21, 22),
        ]:
            t.observe(A.AutotuneSample(ts, sp, room, tado, cmd))
        assert t.active_tuning() == A.Tuning(0.6, 0.002, 0.0)


class TestPersistence:
    def test_round_trip(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        t._on_hold(_hold(DAY + 4 * 3600, mean=0.05), DAY + 4 * 3600)
        _force_more_heat(t, 2 * DAY)
        d = t.as_dict()
        t2 = _tuner(stored=d)
        assert t2.active_tuning() == t.active_tuning()
        assert t2.as_dict()["heatups"] == d["heatups"]
        assert t2._pending is not None

    def test_json_safe(self):
        import json
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        json.dumps(t.as_dict())

    @pytest.mark.parametrize("bad", [
        None, [], "x", {"version": 99},
        {"version": 1, "baseline": {"kp": 0.6, "ki": 0.002, "td_s": 0}, "params": {"kp": "x"}},
        {"version": 1, "baseline": {"kp": 0.6, "ki": 0.002, "td_s": 0},
         "params": {"kp": float("nan"), "ki": 0.002, "td_s": 0}},
    ])
    def test_garbage_gives_baseline(self, bad):
        t = _tuner(stored=bad)
        assert t.active_tuning() == A.Tuning(0.6, 0.002, 0.0)

    def test_zero_dead_time_in_storage_is_ignored(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        d = t.as_dict()
        d["heatups"][0]["theta_s"] = 0.0
        t2 = _tuner(stored=d)
        t2.summary()
        t2._targets()

    def test_out_of_bounds_stored_values_are_clamped(self):
        d = _tuner().as_dict()
        d["params"] = {"kp": 50.0, "ki": 1.0, "td_s": 1e6}
        t = _tuner(stored=d)
        _within_bounds(t)

    def test_baseline_mismatch_restarts(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        t2 = _tuner(kp=0.8, stored=t.as_dict())
        assert t2.active_tuning() == A.Tuning(0.8, 0.002, 0.0)

    def test_last_event_survives_restart(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        assert _tuner(stored=t.as_dict()).last_event == t.last_event

    def test_nothing_learned_still_waiting_after_restart(self):
        # Older stored state has no last_event; nothing learned yet.
        d = _tuner().as_dict()
        del d["last_event"]
        assert _tuner(stored=d).last_event == "waiting for a heat-up"
        # Without the heat-up itself in the stored state, its event is stale.
        d["last_event"] = "heat-up started"
        assert _tuner(stored=d).last_event == "waiting for a heat-up"


def _feed(t, ts, sp, room, demand=1.0, eligible=True):
    """One regulation cycle; the TRV demand is its setpoint - its reading."""
    return t.observe(A.AutotuneSample(ts, sp, room, 20.0, 20.0 + demand, eligible))


def _restart(t, **kw):
    """Persist through JSON (as Home Assistant does) into a fresh tuner."""
    import json
    return _tuner(stored=json.loads(json.dumps(t.as_dict(), allow_nan=False)), **kw)


def _heating(t, minutes=30):
    """A step from 18 to 20 °C with the room rising from 18.5 °C."""
    _feed(t, 0.0, 18.0, 18.5)
    _feed(t, 60.0, 20.0, 18.5)
    assert t.phase == A.PHASE_HEATUP
    for k in range(2, minutes):
        _feed(t, k * 60.0, 20.0, 18.5 + 0.02 * k)
    return (minutes - 1) * 60.0


class TestEpisodeAcrossRestart:
    def test_heatup_in_progress_survives(self):
        t = _tuner()
        last = _heating(t)
        t2 = _restart(t)
        assert t2.phase == A.PHASE_HEATUP
        assert t2.last_event == "heat-up continued after a restart"
        assert t2._heatup.to_dict() == t._heatup.to_dict()
        _feed(t2, last + 90.0, 20.0, 19.1)          # back after 90 s
        assert t2.phase == A.PHASE_HEATUP

    def test_restart_is_just_a_gap(self):
        """After a restart the tuner does exactly what it does when the
        cycles stop for the same time without one."""
        def episode(t):
            return (t.phase, t._heatup.start_ts if t._heatup else None,
                    len(t._heatup.samples) if t._heatup else 0)

        for gap, continued in ((300.0, True), (1200.0, False)):
            a, b = _tuner(), _tuner()
            last = _heating(a)
            _heating(b)
            b = _restart(b)
            for t in (a, b):
                _feed(t, last + gap, 20.0, 19.0)
            assert episode(a) == episode(b)
            assert a.phase == A.PHASE_HEATUP
            # 20 min down: the old heat-up is discarded and, as the room is
            # still well below target, heating "resumes" with a new one.
            assert (b._heatup.start_ts == 60.0) is continued

    def test_long_downtime_discards_a_short_heatup(self):
        t = _tuner()
        last = _heating(t)                           # 28 min, never told to stop
        t = _restart(t)
        _feed(t, last + 1200.0, 20.0, 19.9)         # room close to target now
        assert t.phase != A.PHASE_HEATUP
        assert t.last_event == "heat-up discarded (data_gap)"
        assert t._counters["heatups"] == 0

    @pytest.mark.parametrize("restart", [False, True])
    def test_gap_ends_a_long_heatup_but_keeps_it(self, restart):
        """A TRV that drops off for 20 min (or a long restart) ends the
        heat-up like a schedule change would: what came before is kept."""
        t = _tuner()
        _feed(t, 0.0, 18.0, 18.5)
        for k in range(1, 70):                       # 1.8 °C/h, target reached at 50 min
            room = min(18.5 + 0.03 * k, 20.1)
            _feed(t, k * 60.0, 20.0, room, demand=1.0 if room < 19.95 else -0.5)
        last = 69 * 60.0
        if restart:
            t = _restart(t)
        _feed(t, last + 1200.0, 20.0, 20.1, demand=-0.5)
        assert t.phase != A.PHASE_HEATUP
        assert t._counters["heatups"] == 1
        m = t._heatups[-1]
        assert m.truncated and m.crossed
        assert m.ts == pytest.approx(last, abs=1.0)  # ends before the gap
        assert m.rate_c_per_h == pytest.approx(1.8, rel=0.15)
        assert t.last_event.startswith("heat-up (cut short)")

    def test_clock_jumping_back_discards_the_heatup(self):
        t = _tuner()
        last = _heating(t)
        t = _restart(t)
        _feed(t, last - 3600.0, 20.0, 19.9)
        assert t.phase != A.PHASE_HEATUP

    def test_step_across_a_reload_starts_a_heatup(self):
        """Changing a preset temperature reloads the room; the step must
        still count (it used to be lost with the previous setpoint)."""
        t = _tuner()
        for k in range(5):
            _feed(t, k * 60.0, 20.0, 20.0)
        t2 = _restart(t)
        _feed(t2, 300.0, 21.5, 20.0)
        assert t2.phase == A.PHASE_HEATUP
        assert t2.last_event == "heat-up started"
        # State saved by the previous version has no episode: still loads.
        d = t.as_dict()
        del d["episode"]
        t3 = _tuner(stored=d)
        _feed(t3, 300.0, 21.5, 20.0)
        assert t3.phase != A.PHASE_HEATUP

    def test_changed_baseline_drops_the_heatup(self):
        t = _tuner()
        _heating(t)
        assert _restart(t, kp=0.8).phase == A.PHASE_IDLE

    def test_reset_drops_the_heatup(self):
        t = _tuner()
        _heating(t)
        t.reset()
        assert _restart(t).phase == A.PHASE_IDLE

    @pytest.mark.parametrize("episode", [
        "x", [], {"heatup": "x"}, {"heatup": {"start_ts": "a"}},
        {"heatup": {"start_ts": 0.0, "setpoint_c": 20.0, "last_cycle_ts": -5.0, "samples": []}},
        {"heatup": {"start_ts": 0.0, "setpoint_c": 20.0, "last_cycle_ts": 120.0,
                    "samples": [[60.0, 19.0, 1.0], [0.0, 19.0, 1.0]]}},       # out of order
        {"heatup": {"start_ts": 0.0, "setpoint_c": 20.0, "last_cycle_ts": 120.0,
                    "samples": [[0.0, float("nan"), 1.0]]}},
        {"heatup": {"start_ts": 0.0, "setpoint_c": 20.0, "last_cycle_ts": 120.0,
                    "samples": [[0.0, 19.0]]}},
        {"heatup": {"start_ts": 0.0, "setpoint_c": 20.0, "last_cycle_ts": 120.0,
                    "samples": [[9999.0, 19.0, 1.0]]}},                       # after the end
        {"heatup": {"start_ts": 0.0, "setpoint_c": 20.0, "last_cycle_ts": 120.0,
                    "samples": [], "cross_ts": 500.0}},
        {"heatup": {"start_ts": 0.0, "setpoint_c": 20.0, "last_cycle_ts": 1e9,
                    "samples": [[0.0, 19.0, 1.0]] * 6000}},
        {"prev_sp": "x", "prev_ts": float("inf"), "heatup": None},
    ])
    def test_garbage_episode_is_dropped_but_learning_kept(self, episode):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        d = t.as_dict()
        d["episode"] = episode
        t2 = _tuner(stored=d)
        assert t2.phase == A.PHASE_IDLE
        assert t2.active_tuning() == t.active_tuning()
        assert t2._counters["heatups"] == 1
        _feed(t2, DAY + 60.0, 20.0, 19.0)           # and it keeps working

    def test_restart_mid_heatup_learns_the_same(self):
        """Closed loop: a restart in the middle of the evening heat-up gives
        the same measurements as no restart at all."""
        def run(restart_at_h):
            c = P.RegulationConfig()
            c.tuning = P.CorrectionTuning(kp=4.0, ki=0.002, td_s=0.0)
            sim = RoomSim(PlantParams(**LIVING), R, c, ProxySettings(), y0=19.0,
                          tuner=_tuner(kp=4.0), autotune_module=A, seed=3)
            if restart_at_h is not None:
                sim.run(restart_at_h, SCHED)
                assert sim.tuner.phase == A.PHASE_HEATUP
                sim.tuner = _restart(sim.tuner, kp=4.0)
                sim.run(48 - restart_at_h, SCHED)
            else:
                sim.run(48, SCHED)
            return sim.tuner

        plain, restarted = run(None), run(18.0)
        a = [m.as_dict() for m in plain._heatups]
        b = [m.as_dict() for m in restarted._heatups]
        assert len(a) == len(b) >= 3
        for ma, mb in zip(a, b):
            assert ma.keys() == mb.keys()
            for k in ma:
                if isinstance(ma[k], float):
                    assert mb[k] == pytest.approx(ma[k], rel=0.02, abs=0.01), k
                else:
                    assert mb[k] == ma[k], k
        pa, pb = plain.active_tuning(), restarted.active_tuning()
        assert [pb.kp, pb.ki, pb.td_s] == pytest.approx([pa.kp, pa.ki, pa.td_s], rel=0.02)


def _window(t, ts, room, minutes):
    """A window open (frost 5 °C, not eligible) for *minutes* from *ts*;
    returns the time of the last window cycle."""
    for k in range(minutes):
        t.observe(A.AutotuneSample(ts + k * 60.0, 5.0, room, 20.0, 5.0,
                                   eligible=False, window=True))
    return ts + (minutes - 1) * 60.0


class TestWindowRecoveryIsNotLearned:
    """18 Oct 2026: heating that resumes after a window started a heat-up
    episode, and the room's own re-warming (3.7 °C/h, a 5-min "coast", a
    -1.16 °C "overshoot") went into the medians."""

    def test_restore_after_a_window_starts_no_episode(self):
        t = _tuner()
        for k in range(5):
            _feed(t, k * 60.0, 20.0, 20.0)
        last = _window(t, 300.0, 17.0, 8)
        # Heating resumes with the room 2 °C short and recovering fast.
        _feed(t, last + 60.0, 20.0, 18.0)
        assert t.phase != A.PHASE_HEATUP
        assert t.last_event == "heat-up after a window: not analysed"
        for k in range(2, 20):
            _feed(t, last + k * 60.0, 20.0, min(20.0, 18.0 + 0.15 * k))
        assert t.phase != A.PHASE_HEATUP
        assert t._counters["heatups"] == 0

    def test_schedule_step_soon_after_a_window_is_skipped_too(self):
        t = _tuner()
        _feed(t, 0.0, 18.0, 18.5)
        last = _window(t, 60.0, 17.5, 5)
        _feed(t, last + 60.0, 18.0, 17.8)             # restored to eco
        _feed(t, last + 600.0, 20.0, 18.0)            # the schedule steps up 10 min later
        assert t.phase != A.PHASE_HEATUP

    def test_step_after_the_settle_time_counts(self):
        t = _tuner()
        _feed(t, 0.0, 18.0, 18.5)
        last = _window(t, 60.0, 17.5, 5)
        settle = CFG.window_settle_s
        _feed(t, last + 60.0, 18.0, 17.8)
        _feed(t, last + settle + 60.0, 18.0, 18.2)
        _feed(t, last + settle + 120.0, 20.0, 18.2)   # a real step, room settled
        assert t.phase == A.PHASE_HEATUP

    def test_resume_without_a_window_still_counts(self):
        """Presence away / summer / off: the room really cooled, heating
        resuming is a heat-up (unchanged behaviour)."""
        t = _tuner()
        _feed(t, 0.0, 20.0, 20.0)
        for k in range(1, 6):
            _feed(t, k * 60.0, 16.0, 19.0 - 0.2 * k, eligible=False)
        _feed(t, 360.0, 20.0, 18.0)
        assert t.phase == A.PHASE_HEATUP

    def test_window_opening_ends_the_episode_in_progress(self):
        t = _tuner()
        last = _heating(t, minutes=10)
        _window(t, last + 60.0, 17.0, 3)
        assert t.phase != A.PHASE_HEATUP
        assert t._counters["heatups"] == 0            # too short to keep

    def test_settle_time_survives_a_restart(self):
        t = _tuner()
        _feed(t, 0.0, 20.0, 20.0)
        last = _window(t, 60.0, 17.0, 5)
        t2 = _restart(t)
        _feed(t2, last + 120.0, 20.0, 18.0)
        assert t2.phase != A.PHASE_HEATUP
        assert t2.last_event == "heat-up after a window: not analysed"
        # State saved by the previous version: no window known, so the
        # restore counts as a heat-up as before (nothing worse than today).
        d = t.as_dict()
        del d["episode"]["last_window_ts"]
        t3 = _tuner(stored=d)
        _feed(t3, last + 120.0, 20.0, 18.0)
        assert t3.phase == A.PHASE_HEATUP


# ---------------------------------------------------------------------------
# 3. Closed loop
# ---------------------------------------------------------------------------

LIVING = dict(trv_offset_c=-9.9, tau_room_h=50, t_out_c=12, trv_beta_c=6, tado_kp=0.5,
              tado_ti_min=60, heat_rate_c_per_h=2.5, tau_rad_min=12,
              water_delay_min=3, mix_lag_min=3)
SCHED = schedule_from([(0, 18.0), (6, 20.0), (8, 5.0), (17, 20.0), (22.5, 18.0)])


def _closed_loop(plant_kw, kp, ki, days, sched=SCHED, td=0.0, deriv=True, cfg=None, tune=True):
    c = P.RegulationConfig()
    c.tuning = P.CorrectionTuning(kp=kp, ki=ki, td_s=td)
    tuner = _tuner(kp=kp, ki=ki, td=td, deriv=deriv, cfg=cfg) if tune else None
    sim = RoomSim(PlantParams(**plant_kw), R, c, ProxySettings(), y0=19.0,
                  tuner=tuner, autotune_module=A, seed=3)
    tr = sim.run(days * 24, sched)
    return tuner, tr


def _evening_overshoot(tr, day):
    t0 = day * DAY
    idx = tr.window(t0 + 17 * 3600, t0 + 22.5 * 3600)
    return max(tr.y_true[i] for i in idx) - tr.sp[idx[0]]


class TestClosedLoop:
    def test_learns_to_stop_overshoot(self):
        """Starting without a braking time (heat-ups stop only at the
        target), it learns one from the coast."""
        tuner, tr = _closed_loop(LIVING, 4.0, 0.002, days=7)
        _, fixed = _closed_loop(LIVING, 4.0, 0.002, days=7, tune=False)
        before = _evening_overshoot(fixed, 6)       # same day, no tuner
        after = _evening_overshoot(tr, 6)
        assert before > 0.3
        assert after < 0.15
        assert after < before - 0.15
        assert 0 < tuner.active_tuning().td_s <= P.AutotuneConfig().td_max_s
        assert all(1.0 <= kp <= 8.0 for kp in tr.kp)
        assert tuner.summary()["rollbacks"] == 0

    def test_calms_an_oscillating_room(self):
        """A sluggish valve and too much integral: it swings; the tuner
        calms it.  On a cold day (0 °C): the hold judgement needs the valve
        on 20 % of the time (hold_min_heating_fraction; passive cool-downs
        are not holds), and since 2.7.1 the stop is released only 0.1 °C
        under the target, so this swinging room heats 6-7 % of the time at
        12 °C outside and the tuner stands back, by design (it did the same
        in the owner's rooms on a mild day).  At 0 °C it heats 21 % of the
        time: Ki 0.01 → 0.0006, Kp 4 → 2.1, swing 0.80 → 0.58 in a week."""
        plant = dict(LIVING, tado_ti_min=2000, tado_kp=1.5, water_delay_min=8, mix_lag_min=6,
                     t_out_c=0.0)
        flat = schedule_from([(0, 20.0)])
        tuner, tr = _closed_loop(plant, 4.0, 0.01, days=7, sched=flat)
        _, fixed = _closed_loop(plant, 4.0, 0.01, days=7, sched=flat, tune=False)

        def p2p(trace, d0, d1):
            idx = trace.window(d0 * DAY, d1 * DAY)
            e = [trace.sp[i] - trace.y_true[i] for i in idx]
            return max(e) - min(e)

        assert p2p(tr, 0.5, 2) > 0.7
        assert p2p(tr, 5, 7) < 0.65
        assert p2p(tr, 5, 7) < p2p(fixed, 5, 7) - 0.15
        a = tuner.active_tuning()
        assert a.kp < 4.0 and a.ki < 0.01

    @pytest.mark.parametrize("period", [30.0, 300.0])
    def test_coarse_sensor_still_calms_oscillation(self, period):
        """0.1 °C sensor whose HA timestamp only changes with the value."""
        plant = dict(LIVING, tado_ti_min=2000, tado_kp=1.5, water_delay_min=8,
                     mix_lag_min=6, sensor_resolution_c=0.1, sensor_period_s=period)
        flat = schedule_from([(0, 20.0)])
        tuner, tr = _closed_loop(plant, 4.0, 0.01, days=7, sched=flat)
        idx = tr.window(5 * DAY, 7 * DAY)
        e = [tr.sp[i] - tr.y_true[i] for i in idx]
        assert max(e) - min(e) < 0.7
        assert tuner.summary()["holds_analysed"] > 5

    def test_coarse_sensor_still_learns_braking(self):
        plant = dict(LIVING, sensor_resolution_c=0.1, sensor_period_s=300.0)
        tuner, tr = _closed_loop(plant, 4.0, 0.002, days=7)
        assert tuner.active_tuning().td_s >= 300
        assert _evening_overshoot(tr, 6) < 0.35

    def test_does_nothing_without_episodes(self):
        """A room held at a steady temperature with no swings is left alone."""
        flat = schedule_from([(0, 20.0)])
        tuner, tr = _closed_loop(dict(LIVING, tado_ti_min=200), 4.0, 0.0003, days=3,
                                 sched=flat)
        a = tuner.active_tuning()
        assert a.kp == 4.0
        assert a.td_s == 0.0


def test_production_settings_with_sunny_days_stay_sane():
    """180 s / 0.3 °C sends, random sunny afternoons: no runaway, no locked
    brake, overshoot on ordinary days ends near target."""
    rng = random.Random(8)
    sunny = [rng.random() < 0.4 for _ in range(22)]

    def sun(t):
        d, h = int(t // DAY), (t / 3600) % 24
        return 0.4 if sunny[d] and 13 <= h < 18.5 else 0.0

    c = P.RegulationConfig()
    c.tuning = P.CorrectionTuning(kp=4.0, ki=0.002, td_s=0.0)
    tuner = _tuner(kp=4.0)
    sim = RoomSim(PlantParams(**LIVING, extra_gain=sun), R, c,
                  ProxySettings(min_command_interval_s=180, min_change_threshold_c=0.3),
                  y0=19.0, tuner=tuner, autotune_module=A, seed=8)
    tr = sim.run(21 * 24, SCHED)
    _within_bounds(tuner)
    s = tuner.summary()
    assert s["rollbacks"] <= 1
    assert tuner.active_tuning().td_s >= 600            # braking learned, not locked off
    quiet = [d for d in range(14, 21) if not sunny[d]]
    assert quiet
    assert max(_evening_overshoot(tr, d) for d in quiet) < 0.3


def test_heating_again_after_the_stop_ends_the_heat_up():
    """A stepped top-up (or the PI asking for heat again) ends the coast:
    the new heat must not count as coast, or the braking time would grow
    with every top-up."""
    t = _tuner()
    _feed(t, 0.0, 18.0, 18.5)
    room = 18.5
    for k in range(1, 46):                        # heat at 2 °C/h for 45 min
        room += 2.0 / 60
        _feed(t, k * 60.0, 20.0, room, demand=5.0)
    for k in range(46, 61):                       # stopped: coasts 0.2 °C
        room += 0.2 / 15
        _feed(t, k * 60.0, 20.0, room, demand=-8.0)
    for k in range(61, 66):                       # flat, short of the target
        _feed(t, k * 60.0, 20.0, room, demand=-8.0)
    assert t.phase == A.PHASE_HEATUP
    _feed(t, 66 * 60.0, 20.0, room, demand=5.0)   # top-up starts
    assert t.phase != A.PHASE_HEATUP
    assert t._counters["heatups"] == 1
    m = t._heatups[-1]
    assert m.coast_rise_c == pytest.approx(0.2, abs=0.06)
    assert not m.truncated
