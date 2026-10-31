"""Dosed top-ups, the HA-free parts (dosing.py): the predictor, the shape
fit (replayed on the living room's 19 real settled top-ups of 25-30 Oct
2026), the learner, the pulse coordinator and the per-room doser."""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.pure_modules import dosing, parameters, regulation

CFG = parameters.DoseConfig()
FIXTURE = Path(__file__).parent / "fixtures" / "living_room_topups_2026_10.json"
PER_H = 1.0 / 3600.0


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
# The shape fit
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
        fit = dosing.fit_episode(ts, room, u, 0.5 * PER_H, CFG)
        assert abs(fit.theta_s - 300.0) <= 60.0
        assert fit.tau_s in (360.0, 480.0, 600.0)
        assert fit.gain_c_per_open_min == pytest.approx(0.045, rel=0.1)
        assert fit.rms_c < 0.005

    def test_the_loss_and_an_earlier_pulses_tail(self):
        """Design check for the learner's fit: a pulse whose predecessor's
        heat is still arriving in the 15 min before it, a room losing
        0.5 °C/h, 20-s readings quantised to 0.01 °C.  The earlier pulse is
        in the model's rise, so neither the gain nor the loss is biased."""
        ts = [k * 20.0 for k in range(int(6000 / 20) + 1)]
        pulses = [(60.0, 330.0), (1560.0, 1830.0)]       # 4.5 open-min each, 25 min apart
        u = [sum(max(0.0, min(t + 20.0, b) - max(t, a)) for a, b in pulses) / 20.0 for t in ts]
        rise = dosing.fopdt_response(u, 20.0, 300.0, 480.0)
        room = [round(20.4 - 0.5 * PER_H * t + 0.045 * r, 2) for t, r in zip(ts, rise)]
        start = 1500.0
        upto = [i for i, t in enumerate(ts) if t <= start + 45 * 60]
        cut = upto[-1] + 1
        fit = dosing.fit_episode(ts[:cut], room[:cut], u[:cut], 0.0, CFG, fit_loss=True,
                                 window_start_ts=start - 900.0)
        assert fit.gain_c_per_open_min == pytest.approx(0.045, rel=0.10)
        assert fit.loss_c_per_s * 3600 == pytest.approx(0.5, abs=0.05)
        assert abs(fit.theta_s - 300.0) <= 60.0
        assert fit.tau_s in (360.0, 480.0, 600.0)
        # Taking the loss as the room's own slope just before the pulse (which
        # holds the earlier pulse's landing) biases the gain.
        pre = [(t, y) for t, y in zip(ts, room) if start - 900 <= t <= start]
        slope = dosing.theil_sen(pre)
        window = [i for i in range(cut) if ts[i] >= start]
        biased = dosing.fit_episode([ts[i] for i in window], [room[i] for i in window],
                                    [u[i] for i in window], max(0.0, -slope), CFG)
        assert abs(biased.gain_c_per_open_min / 0.045 - 1) > 0.10

    def test_nothing_to_fit(self):
        assert dosing.fit_episode([0.0, 60.0], [20.0, 20.0], [0.0, 0.0], 0.0, CFG) is None
        ts = [float(t) for t in range(0, 600, 60)]
        assert dosing.fit_episode(ts, [20.0] * 10, [0.0] * 10, 0.0, CFG) is None


class TestFixtureReplay:
    """The 19 settled top-ups the design was fitted on (design §11): the
    room polled every 20 s (as the entity reads it), the valve open while
    the zone reported heating, the pre-pulse fall continued."""

    def _fit(self, e):
        end = e["peak_after_min"] * 60 + 300
        ts = [float(t) for t in range(0, int(end) + 1, 20)]
        room = [_step_at(e["room_c"], t) for t in ts]
        u = [1.0 if _step_at(e["heating_pct"], t, 0.0) > 0 else 0.0 for t in ts]
        fall0 = max(0.0, -e["pre_pulse_slope_c_per_h"]) * PER_H
        return dosing.fit_episode(ts, room, u, fall0, CFG)

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
        """The learner's fit (the loss estimated jointly, from 15 min before
        the release): the gains stay close to the offline fit's, and the
        fitted loss is lower than the slope before the release, which still
        held the last landing (median 0.34 against 0.45 °C/h)."""
        fits = []
        for e in _episodes():
            end = e["peak_after_min"] * 60 + 300
            ts = [float(t) for t in range(-900, int(end) + 1, 20)]
            room = [_step_at(e["room_c"], t, e["room_c"][0][1]) for t in ts]
            u = [1.0 if t >= 0 and _step_at(e["heating_pct"], t, 0.0) > 0 else 0.0 for t in ts]
            fit = dosing.fit_episode(ts, room, u, 0.0, CFG, fit_loss=True, window_start_ts=-900.0)
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

    def test_the_learner_judges_the_real_episodes(self):
        """The learner, fed every minute as the entity would (from 15 min
        before the release), judges the real episodes and fits them close to
        the offline fit.  The fixture ends 10 min after each peak, the
        learner judges 5 min after the landing it sees about 7 min after the
        peak: past the end the room goes on at its last 10 min's slope (it
        is cooling, the heat has landed)."""
        accepted = []
        for e in _episodes():
            last_t = e["room_c"][-1][0]
            tail = [(t, y) for t, y in e["room_c"] if t >= last_t - 600]
            tail_slope = dosing.theil_sen(tail) or 0.0

            def room_at(t, e=e, last_t=last_t, tail_slope=tail_slope):
                if t <= last_t:
                    return _step_at(e["room_c"], t, e["room_c"][0][1])
                return e["room_c"][-1][1] + tail_slope * (t - last_t)

            lr = dosing.DoseLearner(CFG)
            start = 1_800_000_000.0
            pulse_end = start + e["valve_open_min"] * 60
            open_from, open_until = start + CFG.latency_s, pulse_end + CFG.close_delay_s
            for t in range(-900, 0, 60):
                lr.note_cycle(start + t, room_at(float(t)),
                              heating_pct=0.0, heating_ts=start - 3600, open_from=None, open_until=None)
            room0 = _step_at(e["room_c"], 0.0)
            fall0 = max(0.0, -e["pre_pulse_slope_c_per_h"]) * PER_H
            lr.start_episode(start, e["target_c"], room0, fall0, e["trv_gap_at_start_c"])
            verdicts: list = []
            t = 60.0
            while not verdicts and t <= last_t + 900:
                now = start + t
                pct = _step_at(e["heating_pct"], t, 0.0)
                pct_ts = start + max(tt for tt, _ in e["heating_pct"] if tt <= t)
                lr.note_cycle(now, room_at(t), heating_pct=pct * 100, heating_ts=pct_ts,
                              open_from=open_from, open_until=open_until if now >= pulse_end else None)
                if now >= pulse_end:
                    lr.note_pulse_end(pulse_end)
                verdicts = lr.observe(now, e["target_c"], window=False, boost=False)
                t += 60.0
            assert len(verdicts) == 1, e["when"]
            if verdicts[0].kind == dosing.ACCEPTED:
                accepted.append((e, lr._judged[-1]))
        assert len(accepted) >= 15
        rel = [j.gain_c_per_open_min / e["fit"]["gain_c_per_open_min"] for e, j in accepted]
        assert 0.7 <= statistics.median(rel) <= 1.33


# ---------------------------------------------------------------------------
# The learner
# ---------------------------------------------------------------------------

T0 = 1_800_000_000.0


def _run_episode(lr, *, gain=0.04, fall_h=0.4, pulse_s=180.0, setpoint=20.0, room0=19.95,
                 window_at=None, heating=100.0, people_after=None, end_s=3600.0,
                 start=T0, theta=300.0, tau=480.0):
    """One synthetic pulse through the learner, sampled every minute.
    Returns (verdict, peak error seen by the episode)."""
    open_from, open_until = start + 60.0, start + pulse_s + 90.0
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
        verdicts = lr.observe(now, setpoint, window=window_at is not None and t >= window_at, boost=False)
        if verdicts:
            assert len(verdicts) == 1
            return verdicts[0]
    return None


class TestLearner:
    def test_a_clean_episode_is_learned(self):
        lr = dosing.DoseLearner(CFG)
        verdict = _run_episode(lr)
        assert verdict.kind == dosing.ACCEPTED
        judged = lr._judged[-1]
        assert judged.gain_c_per_open_min == pytest.approx(0.04, rel=0.15)
        assert judged.open_min == pytest.approx(3.5)
        assert judged.at_floor
        assert "open-min" in verdict.line and "at the peak" in verdict.line
        assert lr.learned and lr.last_line == verdict.line

    def test_a_window_drops_it(self):
        lr = dosing.DoseLearner(CFG)
        verdict = _run_episode(lr, window_at=600.0)
        assert verdict.kind == dosing.DROPPED and "window" in verdict.line
        assert not lr.learned and lr.episode is None

    def test_a_pulse_that_never_opened_the_valve(self):
        lr = dosing.DoseLearner(CFG)
        verdict = _run_episode(lr, heating=0.0)
        assert verdict.kind == dosing.NOT_OPENED
        assert "did nothing" in verdict.line
        assert lr.episode is None and not lr.learned

    def test_no_heating_sensor_cannot_tell_so_it_is_judged(self):
        lr = dosing.DoseLearner(CFG)
        verdict = _run_episode(lr, heating=None)
        assert verdict.kind == dosing.ACCEPTED

    def test_people_after_the_landing_are_not_counted(self):
        clean = dosing.DoseLearner(CFG)
        _run_episode(clean)
        busy = dosing.DoseLearner(CFG)
        verdict = _run_episode(busy, people_after=2400.0)
        assert verdict.kind == dosing.ACCEPTED
        assert busy._judged[-1] == clean._judged[-1]

    def test_people_before_the_landing_read_as_a_higher_gain(self):
        """The safe way: a gain read too high only makes the next doses
        smaller (26 and 29 Oct: 0.12 with people against 0.045 empty)."""
        clean = dosing.DoseLearner(CFG)
        _run_episode(clean)
        busy = dosing.DoseLearner(CFG)
        assert _run_episode(busy, people_after=1500.0).kind == dosing.ACCEPTED
        assert busy._judged[-1].gain_c_per_open_min > 2 * clean._judged[-1].gain_c_per_open_min
        assert busy.gain_c_per_open_min > clean.gain_c_per_open_min

    def test_a_target_change_drops_it(self):
        lr = dosing.DoseLearner(CFG)
        lr.start_episode(T0, 20.0, 19.9, 0.0, None)
        [verdict] = lr.observe(T0 + 60, 21.0, window=False, boost=False)
        assert verdict.kind == dosing.DROPPED

    def test_judged_once_the_heat_has_landed_even_without_a_peak(self):
        """No rise at all: landed theta + 4 tau after the pulse ended (here
        3 + 5 + 32 = 40 min), judged 5 min later, and a pulse that moved
        nothing is no gain."""
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
        # Judged 5 min after the landing, with the readings since in the fit.
        assert list(verdicts) == [45]
        assert verdicts[45].kind == dosing.REJECTED and "gain" in verdicts[45].line

    def test_a_small_pulse_that_only_slows_the_fall_is_learned(self):
        """Against a 0.6 °C/h fall a 3-min pulse never lifts the room: its
        rise shows only against the trend."""
        lr = dosing.DoseLearner(CFG)
        verdict = _run_episode(lr, gain=0.03, fall_h=0.6)
        assert verdict.kind == dosing.ACCEPTED
        assert lr._judged[-1].gain_c_per_open_min == pytest.approx(0.03, rel=0.2)
        assert lr._judged[-1].peak_error_c < -0.05


class TestLearnerSteps:
    def test_towards_smaller_doses_at_once_towards_bigger_slowly(self):
        lr = dosing.DoseLearner(CFG)
        assert lr.gain_c_per_open_min == pytest.approx(0.045)
        _run_episode(lr, gain=0.09, fall_h=0.4, room0=19.7)
        assert lr.gain_c_per_open_min == pytest.approx(0.045 + 0.5 * (lr._judged[-1].gain_c_per_open_min - 0.045))
        safe = lr.gain_c_per_open_min
        lr2 = dosing.DoseLearner(CFG)
        _run_episode(lr2, gain=0.02)
        g_ep = lr2._judged[-1].gain_c_per_open_min
        assert lr2.gain_c_per_open_min == pytest.approx(0.045 + 0.15 * (g_ep - 0.045))
        once = lr2.gain_c_per_open_min
        _run_episode(lr2, gain=0.02, start=T0 + 4 * 3600)
        assert lr2.gain_c_per_open_min == pytest.approx(once)      # not again within 12 h
        _run_episode(lr2, gain=0.02, start=T0 + 13 * 3600)
        assert lr2.gain_c_per_open_min < once
        assert safe > 0.045

    def test_bounds_hold(self):
        lr = dosing.DoseLearner(CFG)
        for k in range(6):
            _run_episode(lr, gain=0.17, room0=19.6, start=T0 + k * 7200)
        assert lr.gain_c_per_open_min <= 3.0 * 0.045 + 1e-9

    def test_the_bias_learns_from_landings_not_from_the_floor(self):
        lr = dosing.DoseLearner(CFG)
        _run_episode(lr, gain=0.03, pulse_s=300.0, fall_h=0.4, room0=19.9)
        judged = lr._judged[-1]
        assert not judged.at_floor and judged.peak_error_c < 0
        assert lr.bias_c == pytest.approx(0.3 * (0.0 - judged.peak_error_c))
        floor_hot = dosing.DoseLearner(CFG)
        _run_episode(floor_hot, gain=0.04, pulse_s=180.0, fall_h=0.0, room0=20.0)
        assert floor_hot._judged[-1].peak_error_c > 0
        assert floor_hot.bias_c == 0.0

    def test_the_bias_is_clamped(self):
        lr = dosing.DoseLearner(CFG)
        for k in range(8):
            _run_episode(lr, gain=0.03, pulse_s=300.0, fall_h=0.4, room0=19.5, start=T0 + k * 7200)
        assert lr.bias_c <= CFG.bias_clamp_c + 1e-9

    def test_two_hot_landings_cut_the_doses(self):
        lr = dosing.DoseLearner(CFG)
        _run_episode(lr, gain=0.1, fall_h=0.0, room0=20.0)
        assert lr._judged[-1].peak_error_c > 0.3
        after_first = lr.gain_c_per_open_min
        verdict = _run_episode(lr, gain=0.1, fall_h=0.0, room0=20.0, start=T0 + 7200)
        assert "two hot landings" in verdict.line
        assert lr.bias_c == pytest.approx(-0.1)
        assert lr.gain_c_per_open_min >= 1.4 * after_first
        assert lr.gain_c_per_open_min <= 3.0 * 0.045 + 1e-9

    def test_seeding_from_the_auto_tuner_is_the_gain_only(self):
        """The tuner's heating rate seeds the gain; its dead time and coast
        come from cold-radiator heat-ups (15 min in the living room and the
        study against a top-up's 4) and seed nothing."""
        lr = dosing.DoseLearner(CFG)
        lr.seed(2.52)                                          # the living room
        assert lr.gain_c_per_open_min == pytest.approx(0.042)
        assert (lr.theta_s, lr.tau_s) == (CFG.default_theta_s, CFG.default_tau_s)
        assert lr.summary()["seeded_gain_c_per_open_min"] == pytest.approx(0.042)
        lr.seed(None)
        assert (lr.gain_c_per_open_min, lr.theta_s, lr.tau_s) == (0.045, 300.0, 480.0)
        for junk in (0.0, -1.0, math.nan, math.inf):
            lr.seed(junk)
            assert lr.gain_c_per_open_min == 0.045

    def test_seeding_does_not_override_what_was_learned(self):
        lr = dosing.DoseLearner(CFG)
        _run_episode(lr)
        learned = (lr.gain_c_per_open_min, lr.theta_s, lr.tau_s)
        lr.seed(3.0)
        assert (lr.gain_c_per_open_min, lr.theta_s, lr.tau_s) == learned
        lr.reset()
        assert lr.gain_c_per_open_min == pytest.approx(0.05)
        assert (lr.theta_s, lr.tau_s) == (CFG.default_theta_s, CFG.default_tau_s)

    def test_a_save_with_three_seed_values_restores_the_rate(self):
        """Saves from before the 2.15.0 review kept (rate, dead time, coast)
        and a single running episode."""
        old = {"seed_args": [2.52, 1338.0, 1200.0], "gain": 0.042, "theta_s": 900.0, "tau_s": 600.0,
               "accepted_total": 0, "judged": [], "episode": None}
        lr = dosing.DoseLearner(CFG)
        lr.restore(old, T0)
        assert lr.gain_c_per_open_min == pytest.approx(0.042)
        assert (lr.theta_s, lr.tau_s) == (CFG.default_theta_s, CFG.default_tau_s)
        assert lr.to_dict()["seed_args"] == [2.52]

    def test_round_trip(self):
        lr = dosing.DoseLearner(CFG)
        lr.seed(2.4)
        _run_episode(lr)
        lr.start_episode(T0 + 7200, 20.0, 19.9, 0.0, 1.5, need_start_c=0.12)
        lr.note_cycle(T0 + 7260, 19.9, heating_pct=50.0, heating_ts=T0 + 7230,
                      open_from=T0 + 7260, open_until=None)
        data = json.loads(json.dumps(lr.to_dict()))
        back = dosing.DoseLearner(CFG)
        back.restore(data, T0 + 7300)
        assert back.gain_c_per_open_min == pytest.approx(lr.gain_c_per_open_min)
        assert (back.theta_s, back.tau_s, back.bias_c) == (lr.theta_s, lr.tau_s, lr.bias_c)
        assert back.learned and back.summary()["episodes"] == 1
        assert back.episode is not None and back.episode.valve_opened
        assert back.to_dict()["seed_args"] == [2.4]
        stale = dosing.DoseLearner(CFG)
        stale.restore(data, T0 + 7260 + 601)
        assert stale.episode is None and stale.learned
        junk = dosing.DoseLearner(CFG)
        junk.restore({"judged": [{"ts": "x"}], "gain": "y", "episode": {"start_ts": None}}, T0)
        assert not junk.learned and junk.episode is None


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
            "virtual_error_c": 0.0, "ready_in_s": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _inputs(d, now, **kw):
    args = {"enabled": True, "setpoint_c": 20.0, "room_c": 19.95, "room_slope_c_per_s": -0.4 * PER_H,
            "state": _state(), "deadband_c_per_h": 0.1, "release_below_c": 0.10}
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
        _inputs(d, T0)
        d.note_command(T0, 25.0, min_c=5.0, max_c=25.0, ok=True)
        _inputs(d, T0 + 120)
        d.reset()
        assert d.inflight_c == 0.0 and d.predictor.committed_open_min() == 0.0
        assert d.learner.gain_c_per_open_min == pytest.approx(0.04)

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
# The 2.15.0 review's findings
# ---------------------------------------------------------------------------

def _pulse_history(lr, pulses, *, gain, theta, tau, fall_h, room0=20.42, start=T0, end_s=7200.0,
                   setpoint=20.5, heating=100.0):
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
        for k, (a, b) in enumerate(pulses):
            if t == a:
                lr.start_episode(now, setpoint, room[float(t)], fall_h * PER_H, 1.0)
            if t == b:
                lr.note_pulse_end(now)
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
        lr = dosing.DoseLearner(CFG)
        verdicts = _pulse_history(lr, [(0, 300), (1260, 1560)], gain=0.02, theta=540.0, tau=240.0,
                                  fall_h=0.3, end_s=5400.0)
        kinds = [v.kind for _, v in verdicts]
        assert kinds == [dosing.ACCEPTED, dosing.ACCEPTED], [v.line for _, v in verdicts]
        first, second = lr._judged
        # The first is judged before the second's heat could show (its open
        # + the delay in use), on its own landing.
        cut = 1260 + CFG.latency_s + CFG.default_theta_s
        assert verdicts[0][0] <= cut + 300 < verdicts[1][0]
        for j in (first, second):
            assert j.gain_c_per_open_min == pytest.approx(0.02, rel=0.15)
            assert abs(j.theta_s - 540.0) <= 60.0 and j.tau_s in (180.0, 240.0, 360.0)
            assert j.loss_c_per_s * 3600 == pytest.approx(0.3, abs=0.05)

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
        assert lr._judged[0].gain_c_per_open_min == pytest.approx(0.02, rel=0.15)

    def test_an_under_landing_moves_the_bias_up(self):
        """(b): an episode landing short (here -0.10, a pulse sized by the
        model in use on the weaker plant) moves the aim up."""
        lr = dosing.DoseLearner(CFG)
        _pulse_history(lr, [(0, 240), (1260, 1500)], gain=0.02, theta=540.0, tau=240.0,
                       fall_h=0.3, end_s=5400.0)
        first = lr._judged[0]
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


class TestLearnerShapeSteps:
    """M3: the delay and the spread move either way, without a rate limit."""

    def test_the_delay_and_spread_follow_the_room_either_way(self):
        lr = dosing.DoseLearner(CFG)
        _run_episode(lr, gain=0.04, theta=180.0, tau=360.0)
        j = lr._judged[-1]
        assert j.theta_s < 300.0 and j.tau_s < 480.0
        assert lr.theta_s == pytest.approx(300.0 + 0.5 * (j.theta_s - 300.0))
        assert lr.tau_s == pytest.approx(480.0 + 0.5 * (j.tau_s - 480.0))
        theta1, tau1 = lr.theta_s, lr.tau_s
        _run_episode(lr, gain=0.04, theta=180.0, tau=360.0, start=T0 + 7200)   # 2 h later
        assert lr.theta_s < theta1 and lr.tau_s < tau1
        up = dosing.DoseLearner(CFG)
        _run_episode(up, gain=0.04, theta=600.0, tau=720.0, end_s=4800.0)
        assert up.theta_s > 300.0 and up.tau_s > 480.0

    def test_the_gain_stays_asymmetric(self):
        lr = dosing.DoseLearner(CFG)
        _run_episode(lr, gain=0.02)
        once = lr.gain_c_per_open_min
        assert once == pytest.approx(0.045 + 0.15 * (lr._judged[-1].gain_c_per_open_min - 0.045))
        _run_episode(lr, gain=0.02, start=T0 + 7200)
        assert lr.gain_c_per_open_min == once
        assert set(lr.to_dict()["last_more"]) == {"gain"}


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
                     deadband_c_per_h=0.1, release_below_c=0.1)
        assert d.inflight_c == 0.0
        d.note_command(t, 5.0, min_c=5, max_c=25, ok=True)
        room, slopes = 20.5, []
        for _ in range(25):
            t += 60
            room -= 0.5 / 60
            d.inputs(t, enabled=True, setpoint_c=20.5, room_c=round(room, 2), room_slope_c_per_s=-0.5 * PER_H,
                     state=st, deadband_c_per_h=0.1, release_below_c=0.1)
            if d.loss_slope_c_per_s is not None:
                slopes.append(d.loss_slope_c_per_s * 3600)
        assert slopes and min(slopes) >= -0.6, slopes

    def test_an_off_from_any_path_ends_the_pulse_being_measured(self):
        """The watchdog, the room sensor lost, Roomstat's own off, summer:
        no dose_end from the regulator, the off ends the learner's pulse; so
        does the valve seen shut.  (A hold ends it too, and its unbooked heat
        then drops the episode: TestHoldHeat.)"""
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
    learned from."""

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
        d.note_command(T0 + 540, 21.2, min_c=5.0, max_c=25.0, ok=True)      # unsettled into a hold
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
            assert len(lr.to_dict()["holds"]) == 1

    def test_the_marks_round_trip(self):
        d = dosing.RoomDoser(CFG, "a")
        _inputs(d, T0)
        d.note_command(T0, 21.0, min_c=5.0, max_c=25.0, ok=True)
        _after(d, T0, _result(mode="hold"))
        data = json.loads(json.dumps(d.to_dict()))
        back = dosing.RoomDoser(CFG, "a")
        back.restore(data, T0 + 60)
        assert back.diagnostics()["hold_from"] == T0 and back.learner._holds == [[T0, None]]
        future = dosing.RoomDoser(CFG, "a")
        future.restore(dict(data, hold_from=T0 + 3600), T0 + 60)
        assert future.diagnostics()["hold_from"] is None

    def test_a_pulse_then_a_hold_in_closed_loop_leaves_the_model_alone(self):
        """The coordinator's entity trace (diag_cap.py 1.5 0.045 360): the
        room cools 1.5 °C/h, a pulse, the room leaves the settled state into
        a hold at 60 %.  With the hold's heat in the window the two accepted
        episodes moved the model to gain 0.094, delay 8.3 min, spread
        24.5 min; the plant is the default model."""
        loop = _Loop(_Plant(T0, fall_c_per_h=1.5, hold_frac=0.6))
        loop.run(6 * 60)
        lr = loop.d.learner
        held = [ln for _, ln in loop.lines if "a hold's heat" in ln]
        assert held and loop.pulses
        assert lr._accepted_total == 0, loop.lines
        assert (lr.gain_c_per_open_min, lr.theta_s, lr.tau_s) == (0.045, 300.0, 480.0)


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

    def test_a_bridged_reading_feeds_neither_the_observer_nor_the_learner(self):
        d = dosing.RoomDoser(CFG, "a")
        for k in range(12):
            _inputs(d, T0 + 60 * k, room_c=20.0 - 0.01 * k)
            _after(d, T0 + 60 * k, _result(), room_c=20.0 - 0.01 * k)
        loss_before = d._loss.to_dict()
        history_before = list(d.learner._history)
        for k in range(12, 16):
            _inputs(d, T0 + 60 * k, room_c=19.89, room_fresh=False)
            _after(d, T0 + 60 * k, _result(mode="dose", fired_by="release" if k == 12 else None,
                                           need_c=0.1), room_c=19.89, room_fresh=False)
        assert d._loss.to_dict() == loss_before
        assert list(d.learner._history) == history_before
        assert d.learner.episode is None          # no episode on a bridged reading
        assert d.summary()["pulses_24h"] == 1     # the pulse still counts
        # A fresh reading again: an episode takes it, the bridged ones not.
        d.learner.start_episode(T0 + 960, 20.0, 19.88, 0.0, None)
        _after(d, T0 + 1020, _result(), room_c=19.87, room_fresh=False)
        _after(d, T0 + 1080, _result(), room_c=19.86)
        assert d.learner.episode.samples == [(T0 + 960, 19.88), (T0 + 1080, 19.86)]

    def test_summary_changes_only_on_pulses_and_verdicts_diagnostics_has_the_rest(self):
        loop = _Loop(_Plant(T0, fall_c_per_h=0.4))
        loop.run(4 * 60)
        changes = sum(1 for a, b in zip(loop.summaries, loop.summaries[1:]) if a != b)
        events = len(loop.pulses) + len(loop.lines)
        assert len(loop.summaries) > 200 and len(loop.pulses) >= 5
        assert changes <= events, (changes, len(loop.pulses), len(loop.lines))
        summary = loop.d.summary()
        live = {"inflight_c", "valve_open", "open_from", "open_until", "episode_running",
                "loss_slope_c_per_h", "virtual_error_c", "recent", "radiator_gaps_c", "episodes_running"}
        assert not live & set(summary)
        diag = loop.d.diagnostics()
        assert live <= set(diag) and set(summary) <= set(diag)
        assert json.loads(json.dumps(summary)) == summary
        assert json.loads(json.dumps(diag)) == diag


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
    goes out on a 0.3-°C change at most every 180 s."""

    def __init__(self, plant, *, setpoint=20.5, mode="stop"):
        self.cfg = parameters.RegulationConfig()
        self.reg = regulation.FeedforwardPiRegulator(self.cfg)
        self.state = regulation.RegulationState(mode=mode)
        self.d = dosing.RoomDoser(self.cfg.dose, "r")
        self.plant, self.sp, self.now = plant, setpoint, plant.t
        self.t0 = plant.t
        self.slope = dosing.SlopeEstimator()
        self.last_sent = self.last_send_ts = None
        self.result = None
        self.wake = None
        self.rooms, self.pulses, self.lines, self.summaries = [], [], [], []
        self.open_episodes_max = 0

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
        kw = self.d.inputs(self.now, enabled=True, setpoint_c=self.sp, room_c=room,
                           room_slope_c_per_s=sl, state=self.state,
                           deadband_c_per_h=self.cfg.slope_deadband_c_per_h,
                           release_below_c=self.cfg.release_below_c)
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
        """The model's own plant over 4 h: the learner neither drifts up nor
        down (the HA-free twin of the entity test)."""
        loop = _Loop(_Plant(T0))
        lr = loop.d.learner
        trajectory = []
        for _ in range(24):
            loop.run(10)
            trajectory.append((lr.gain_c_per_open_min, lr.theta_s, lr.tau_s))
        assert lr._accepted_total >= 5
        for gain, theta, tau in trajectory:
            assert gain == pytest.approx(0.045, rel=0.20)
            assert 240.0 <= tau <= 840.0 and 120.0 <= theta <= 540.0
        lo, hi, _ = loop.band(1)
        assert lo >= -0.12 and hi <= 0.05

    def test_an_under_dosing_model_learns_and_the_room_comes_up(self):
        """M1, s15/s16's plant: gain 0.02 against 0.045 in use, delay 9 min
        against 5, spread 4 min against 8, cooling 0.3 °C/h.  Before the
        review's fix every episode was dropped by the next pulse: 0 accepted
        in 24 h, the room 0.08-0.13 under all day (mean -0.105)."""
        loop = _Loop(_Plant(T0, fall_c_per_h=0.3, gain=0.02, theta_s=540.0, tau_s=240.0))
        lr = loop.d.learner
        loop.run(3 * 60)
        assert lr._accepted_total >= 3, loop.lines
        loop.run(3 * 60)
        assert lr.theta_s >= 480.0 and lr.tau_s <= 300.0          # 300 -> 540, 480 -> 240
        assert lr.gain_c_per_open_min < 0.045                    # one rate-limited step down
        assert lr.bias_c > 0.0                                    # the landings were short
        assert loop.kinds().count("learned") >= 0.8 * len(loop.lines)
        _, _, mean = loop.band(3)
        assert mean > -0.05                                       # was -0.105
        assert loop.open_episodes_max <= 2

    def test_a_weak_slow_radiator(self):
        loop = _Loop(_Plant(T0, fall_c_per_h=0.4, gain=0.03, theta_s=240.0, tau_s=720.0))
        lr = loop.d.learner
        loop.run(12 * 60)
        assert lr._accepted_total >= 10
        assert lr.tau_s >= 660.0 and lr.theta_s <= 300.0
        assert lr.gain_c_per_open_min < 0.045 and lr.bias_c > 0.0
        assert loop.band(3)[0] > -0.15

    def test_a_long_delay(self):
        loop = _Loop(_Plant(T0, fall_c_per_h=0.4, theta_s=900.0, tau_s=360.0))
        lr = loop.d.learner
        loop.run(12 * 60)
        assert lr._accepted_total >= 10
        assert lr.theta_s >= 840.0 and lr.tau_s <= 420.0
        assert loop.band(3)[0] > -0.15

    def test_late_heating_reports(self):
        """M6, s12_lagged_power.py 370: the first report above 0 comes 370 s
        late.  No pulse is taken for one that did nothing, none runs to its
        maximum, and the episodes are learned."""
        loop = _Loop(_Plant(T0, report_lag_s=370.0))
        loop.run(6 * 60)
        assert "the valve never reported heating, so the pulse did nothing" not in loop.kinds()
        assert all(p[2] != "max" for p in loop.pulses if p[2])
        assert loop.d.learner._accepted_total >= 8

    def test_the_first_pulse_after_a_hold(self):
        """M2, s17_relay_hold2.py 20.2 0.8 0.5: a hold at 50 % brings the
        room to its target, stops, settles.  Booked as fully open the hold
        read as a fast cooling: the first pulse fired 0.06 above the target
        and peaked +0.15."""
        loop = _Loop(_Plant(T0, room=20.2, fall_c_per_h=0.8, hold_frac=0.5), mode="hold")
        loop.run(5 * 60)
        assert loop.pulses, "never settled"
        start, _, _, error_at_fire = loop.pulses[0]
        assert error_at_fire < 0.0
        after = [e for t, e in loop.rooms if start <= t <= start + 3600]
        assert max(after) <= 0.05

    def test_nothing_grows_without_bound(self):
        loop = _Loop(_Plant(T0, fall_c_per_h=0.3, gain=0.02, theta_s=540.0, tau_s=240.0))
        loop.run(12 * 60)
        lr = loop.d.learner
        assert loop.open_episodes_max <= 3
        assert lr._history[-1][0] - lr._history[0][0] <= CFG.fit_history_s
        assert len(lr._intervals) <= CFG.fit_history_s / CFG.pulse_min_s
        data = json.dumps(loop.d.to_dict())
        assert len(data) < 20_000
