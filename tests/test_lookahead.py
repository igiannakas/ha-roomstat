"""Valve look-ahead (lookahead.py, regulation.lookahead_correction_c): the
correction in hold, and learning the valve's look-ahead from its answers."""
from __future__ import annotations

import math
import random

import pytest

from tests.pure_modules import autotune, lookahead, parameters, regulation

LookaheadLearner = lookahead.LookaheadLearner
LookaheadConfig = parameters.LookaheadConfig
correction = regulation.lookahead_correction_c
PER_H = 1 / 3600.0


# ---------------------------------------------------------------------------
# The correction
# ---------------------------------------------------------------------------

class TestCorrection:
    def test_falling_reading_lowers_by_lookahead_times_the_fall_beyond_the_deadband(self):
        # 13 min look-ahead, reading falling 3.3 °C/h, deadband 0.3: 13/60 x 3.0
        assert correction(780.0, -3.3 * PER_H, 0.3, 1.5) == pytest.approx(0.65)

    @pytest.mark.parametrize("slope_c_per_h", [0.0, 0.2, 1.5, -0.2, -0.3])
    def test_flat_rising_or_within_the_deadband_is_nothing(self, slope_c_per_h):
        assert correction(780.0, slope_c_per_h * PER_H, 0.3, 1.5) == 0.0

    def test_capped(self):
        assert correction(1200.0, -10.0 * PER_H, 0.3, 1.5) == 1.5

    @pytest.mark.parametrize("args", [
        (0.0, -3.0 * PER_H, 0.3, 1.5),       # no look-ahead learned
        (780.0, None, 0.3, 1.5),             # slope unknown
        (780.0, math.nan, 0.3, 1.5),
        (math.inf, -3.0 * PER_H, 0.3, 1.5),
        (-60.0, -3.0 * PER_H, 0.3, 1.5),
        (780.0, -3.0 * PER_H, 0.3, 0.0),     # no cap: off
    ])
    def test_nothing_without_usable_inputs(self, args):
        assert correction(*args) == 0.0


# ---------------------------------------------------------------------------
# The regulator
# ---------------------------------------------------------------------------

def _hold(lookahead_s: float, trv_slope_c_per_h: float | None, *, room=19.95, sp=20.0,
          state=None, dt=60.0):
    cfg = parameters.RegulationConfig()
    reg = regulation.FeedforwardPiRegulator(cfg)
    st = state or regulation.RegulationState(mode=regulation.MODE_HOLD)
    return reg.compute(
        setpoint_c=sp, room_temp_c=room, tado_internal_c=18.1, time_delta_s=dt,
        state=st, room_slope_c_per_s=-0.3 * PER_H,
        trv_slope_c_per_s=None if trv_slope_c_per_h is None else trv_slope_c_per_h * PER_H,
        valve_lookahead_s=lookahead_s,
    )


class TestRegulatorHold:
    def test_hold_command_is_lowered_on_a_falling_reading(self):
        stock = _hold(0.0, -3.3)
        la = _hold(780.0, -3.3)
        assert la.mode == stock.mode == regulation.MODE_HOLD
        assert la.lookahead_c == pytest.approx(0.65)
        # Both are rounded to 0.1 °C after the lowering.
        assert la.target_for_tado_c == pytest.approx(stock.target_for_tado_c - 0.65, abs=0.051)
        assert not la.is_saturated            # the lowering is no saturation

    def test_rising_reading_changes_nothing(self):
        assert _hold(780.0, 2.0).target_for_tado_c == _hold(0.0, 2.0).target_for_tado_c
        assert _hold(780.0, 2.0).lookahead_c == 0.0

    def test_integral_and_p_are_the_same(self):
        stock, la = _hold(0.0, -3.3), _hold(780.0, -3.3)
        assert la.p_correction_c == stock.p_correction_c
        assert la.i_correction_c == stock.i_correction_c
        assert la.new_state.integral_c == stock.new_state.integral_c

    @pytest.mark.parametrize("mode, room", [
        (regulation.MODE_HEAT, 18.5),       # heat-up: the maximum
        (regulation.MODE_STOP, 20.3),       # stop: off
    ])
    def test_only_the_hold_is_touched(self, mode, room):
        st = regulation.RegulationState(mode=mode)
        stock = _hold(0.0, -3.3, room=room, state=regulation.RegulationState(mode=mode))
        la = _hold(780.0, -3.3, room=room, state=st)
        assert la.mode == stock.mode != regulation.MODE_HOLD
        assert la.target_for_tado_c == stock.target_for_tado_c
        assert la.lookahead_c == 0.0

    def test_full_push_is_decided_on_the_uncorrected_push(self):
        # 0.7 below: (1 + 4) x 0.7 = 3.5 over the reading, a full push either way.
        stock = _hold(0.0, -3.3, room=19.3)
        la = _hold(780.0, -3.3, room=19.3)
        assert stock.full_push and la.full_push


# ---------------------------------------------------------------------------
# The tuner's demand
# ---------------------------------------------------------------------------

def test_the_tuners_demand_counts_the_lookahead_back_in():
    s = autotune.AutotuneSample(ts=0.0, setpoint_c=20.0, room_temp_c=19.9,
                                tado_internal_c=18.1, tado_setpoint_c=17.5, lookahead_c=0.8)
    assert s.demand_c == pytest.approx(0.2)
    s.lookahead_c = math.nan
    assert s.demand_c == pytest.approx(-0.6)
    assert autotune.AutotuneSample(0.0, 20.0, 19.9, 18.1, 17.5).demand_c == pytest.approx(-0.6)


@pytest.mark.parametrize("lookahead_c, ends", [(0.0, False), (0.9, True)])
def test_a_top_up_with_the_lookahead_taken_off_still_ends_the_heatup_coast(lookahead_c, ends):
    """After the coast's crossing, a top-up whose push the look-ahead lowered
    is still a top-up for the tuner (its heat must not count as coast)."""
    cfg = autotune.AutotuneConfig()
    tr = autotune._HeatupTracker(0.0, 20.0, cfg)

    def sample(ts, sp_trv, la=0.0):
        return autotune.AutotuneSample(ts=ts, setpoint_c=20.0, room_temp_c=19.0 + ts / 36000,
                                       tado_internal_c=18.0, tado_setpoint_c=sp_trv,
                                       lookahead_c=la)
    assert tr.add(sample(60.0, 25.0)) == autotune._RUNNING        # heating
    assert tr.add(sample(120.0, 5.0)) == autotune._RUNNING        # told to stop: crossing
    assert tr.cross_ts == 120.0
    # A top-up 0.2 over the reading, after 0.9 was taken off for the look-ahead.
    result = tr.add(sample(180.0, 18.2, lookahead_c))
    assert (result == autotune._DONE) is ends


# ---------------------------------------------------------------------------
# The learner
# ---------------------------------------------------------------------------

def _valve(push_c: float, fall_c_per_h: float, lookahead_min: float, rng: random.Random,
           noise_pct: float = 8.0) -> float:
    """A Tado valve's first answer as fitted on the living room (23-30 Oct
    2026): 16 + 38 x (push + look-ahead x fall), plus noise, within 1..97 %."""
    pct = 16.0 + 38.0 * (push_c + lookahead_min / 60.0 * fall_c_per_h) + rng.gauss(0, noise_pct)
    return min(97.0, max(1.0, pct))


def _release(learner, t, rng, *, lookahead_min=13.0, push=None, fall=None, noise=8.0,
             delay_s=150.0):
    """One release through start / note_setpoint / observe."""
    push = rng.uniform(-0.3, 0.8) if push is None else push
    fall = rng.uniform(0.0, 5.0) if fall is None else fall
    reading = 18.0
    assert learner.start(t, reading + push, reading, -fall * PER_H)
    learner.note_setpoint(reading + push)
    assert learner.observe(t + 60.0, 0.0, t + 30.0) is None          # still shut
    return learner.observe(t + delay_s, _valve(push, fall, lookahead_min, rng, noise),
                           t + delay_s)


class TestLearner:
    def test_starts_at_no_lookahead(self):
        la = LookaheadLearner()
        assert la.lookahead_s == 0.0 and la.fit is None and not la.observing

    def test_learns_the_valves_lookahead_half_the_gap_at_a_time(self):
        rng = random.Random(4)
        cfg = LookaheadConfig()
        la = LookaheadLearner()
        trusted = 0
        for k in range(40):
            before = la.lookahead_s
            line = _release(la, 1000.0 + k * 3600, rng)
            assert line
            if la.fit is None:
                # Nothing moves before a fit is trusted.
                assert la.lookahead_s == before and (trusted or before == 0.0)
                continue
            trusted += 1
            gap = la.fit.lookahead_s - before
            if abs(gap) < cfg.deadband_s:
                assert la.lookahead_s == before
            else:
                assert la.lookahead_s == pytest.approx(before + cfg.step_frac * gap)
        assert trusted and k + 1 - trusted >= cfg.min_samples - 1
        assert la.fit is not None and la.fit.uncertainty_s <= cfg.max_uncertainty_s
        assert la.fit.lookahead_s / 60 == pytest.approx(13.0, abs=2.5)
        assert la.lookahead_s / 60 == pytest.approx(13.0, abs=2.5)

    def test_a_fit_needs_releases_on_a_falling_reading(self):
        # 2 Nov 2026, master bedroom: seven releases, one on a falling reading.
        real = [(0.7, 0.0, 14), (1.4, 0.0, 67), (1.3, 0.0, 80), (0.1, 1.7, 25),
                (0.3, 0.0, 12), (1.4, 0.0, 42), (0.2, 0.0, 31)]
        la = LookaheadLearner()
        for k, (push, fall, pct) in enumerate(real + [(0.3, 0.0, 20)]):
            la.start(1000.0 + k * 3600, 18.0 + push, 18.0, -fall * PER_H)
            line = la.observe(1150.0 + k * 3600, pct, 1150.0 + k * 3600)
        # Until 2.18.2 the eighth release made a fit of 12.5 min (5.2-15.2
        # with other plausible eighth answers) and walked towards it.
        assert la.fit is None and la.lookahead_s == 0.0
        assert "1 of 3 releases on a reading falling 1.0 °C/h or more" in line
        assert la.summary()["falling_releases"] == 1

    def test_a_fit_too_uncertain_is_not_used(self):
        rng = random.Random(11)
        la = LookaheadLearner()
        for k in range(10):
            line = _release(la, 1000.0 + k * 3600, rng, noise=40.0)
        assert la.fit is None and la.lookahead_s == 0.0
        assert "too uncertain" in line

    def test_the_studys_releases_need_a_wider_push_for_a_fit(self):
        # 2 Nov 2026, study: five releases, four on a reading falling 3-4 °C/h
        # (16.5 ± 3.1 min).  All pushes small (-0.2..+0.9), so the valve's
        # answer per °C of push is known only to about ± 40 %, and the
        # look-ahead, a ratio over it, is not trusted yet (2.19.0 review);
        # one release with a bigger push pins it down, and the look-ahead
        # moves half the way to the fit.
        real = [(0.9, 0.0, 46), (-0.1, 3.0, 29), (-0.1, 3.0, 31), (-0.2, 4.0, 53),
                (0.3, 3.5, 59), (0.4, 0.0, 30)]
        la = LookaheadLearner()
        for k, (push, fall, pct) in enumerate(real + [(1.2, 0.0, 62)]):
            la.start(1000.0 + k * 3600, 18.0 + push, 18.0, -fall * PER_H)
            line = la.observe(1150.0 + k * 3600, pct, 1150.0 + k * 3600)
            if k == len(real) - 1:
                assert la.fit is None and "answer to the push is too uncertain" in line
        assert la.fit is not None
        assert la.fit.uncertainty_s <= LookaheadConfig().max_uncertainty_s
        assert la.fit.lookahead_s / 60 == pytest.approx(14.8, abs=1.0)
        assert la.lookahead_s == pytest.approx(0.5 * la.fit.lookahead_s)
        assert "±" in line and "fit from 7 releases" in line
        s = la.summary()
        assert s["fitted_uncertainty_min"] is not None and s["falling_releases"] == 4

    def test_a_valve_without_lookahead_learns_none(self):
        rng = random.Random(7)
        la = LookaheadLearner()
        for k in range(30):
            _release(la, 1000.0 + k * 3600, rng, lookahead_min=0.0)
        assert la.lookahead_s / 60 <= 2.0

    def test_needs_spread_in_fall_rate(self):
        rng = random.Random(1)
        la = LookaheadLearner()
        for k in range(12):
            line = _release(la, 1000.0 + k * 3600, rng, fall=1.0 + 0.01 * k)
        assert la.fit is None and la.lookahead_s == 0.0
        assert "too alike" in line

    def test_needs_the_valve_to_answer_the_push(self):
        rng = random.Random(2)
        la = LookaheadLearner()
        for k in range(12):
            push, fall = rng.uniform(-0.3, 0.8), rng.uniform(0.0, 5.0)
            la.start(1000.0 + k * 3600, 18.0 + push, 18.0, -fall * PER_H)
            line = la.observe(1150.0 + k * 3600, 40.0 + rng.gauss(0, 2), 1150.0 + k * 3600)
        assert la.fit is None and "barely answers the push" in line

    def test_push_and_fall_entangled_is_no_fit(self):
        rng = random.Random(3)
        la = LookaheadLearner()
        for k in range(12):
            fall = rng.uniform(0.0, 5.0)
            line = _release(la, 1000.0 + k * 3600, rng, fall=fall, push=0.8 - 0.2 * fall)
        assert la.fit is None and "move together" in line

    def test_fully_open_is_not_counted(self):
        la = LookaheadLearner()
        la.start(1000.0, 21.0, 18.0, -2.0 * PER_H)
        line = la.observe(1150.0, 100.0, 1150.0)
        assert "fully open" in line and la.releases == () and not la.observing

    def test_no_answer_within_the_window_is_given_up(self):
        cfg = LookaheadConfig()
        la = LookaheadLearner()
        la.start(1000.0, 18.3, 18.0, -2.0 * PER_H)
        assert la.observe(1000.0 + 400, 0.0, 1000.0 + 390) is None
        # Past the window, but a report made within it may still come in.
        assert la.observe(1000.0 + 481, 0.0, 1000.0 + 480) is None and la.observing
        line = la.observe(1000.0 + cfg.observe_s + cfg.answer_seen_late_s + 1, 0.0, 1000.0 + 480)
        assert "no heating" in line and not la.observing and la.releases == ()

    def test_an_answer_made_in_the_window_and_seen_a_poll_later_counts(self):
        # 2.19.0: the valve's own report time (Tado's ``time``): made 7 min
        # after the release, seen by Home Assistant at 10 min.
        la = LookaheadLearner()
        la.start(1000.0, 18.3, 18.0, -2.0 * PER_H)
        assert la.observe(1000.0 + 540, 0.0, 1000.0 + 300) is None
        line = la.observe(1000.0 + 600, 45.0, 1000.0 + 420)
        assert line and "answered 45 %" in line and la.releases[0].response_pct == 45.0
        # One made after the window does not count, however it is seen.
        la.start(5000.0, 18.3, 18.0, -2.0 * PER_H)
        assert la.observe(5000.0 + 600, 45.0, 5000.0 + 500) is None
        assert la.observing

    def test_a_report_from_before_the_command_is_not_its_answer(self):
        la = LookaheadLearner()
        la.start(1000.0, 18.3, 18.0, -2.0 * PER_H)
        assert la.observe(1060.0, 35.0, 990.0) is None            # reported before
        assert la.observing

    def test_unknown_slope_or_reading_is_not_watched(self):
        la = LookaheadLearner()
        assert not la.start(1000.0, 18.3, 18.0, None)
        assert not la.start(1000.0, 18.3, None, -2.0 * PER_H)
        assert not la.observing

    def test_the_held_setpoint_is_the_push(self):
        # A cloud zone rounds 18.3 to 18.5: the push the valve got is +0.5.
        la = LookaheadLearner()
        la.start(1000.0, 18.3, 18.0, -2.0 * PER_H)
        la.note_setpoint(18.5)
        la.note_setpoint(18.0)                                    # only the first counts
        la.observe(1150.0, 40.0, 1150.0)
        assert la.releases[0].push_c == pytest.approx(0.5)
        assert la.releases[0].fall_c_per_h == pytest.approx(2.0)

    def test_cancel(self):
        la = LookaheadLearner()
        la.start(1000.0, 18.3, 18.0, -2.0 * PER_H)
        la.cancel()
        assert not la.observing and la.observe(1150.0, 40.0, 1150.0) is None

    def test_window_keeps_the_last_releases(self):
        rng = random.Random(5)
        la = LookaheadLearner(LookaheadConfig(window=10))
        for k in range(25):
            _release(la, 1000.0 + k * 3600, rng)
        assert len(la.releases) == 10
        assert la.releases[0].ts == 1000.0 + 15 * 3600

    def test_never_beyond_the_bounds(self):
        # A 40-min valve on gentle slides and pushes (answers stay unclipped).
        rng = random.Random(6)
        la = LookaheadLearner()
        for k in range(60):
            _release(la, 1000.0 + k * 3600, rng, lookahead_min=40.0, noise=2.0,
                     push=rng.uniform(-0.3, 0.3), fall=rng.uniform(0.0, 2.0))
        assert la.fit is not None and la.fit.lookahead_s == LookaheadConfig().max_lookahead_s
        assert la.lookahead_s == pytest.approx(LookaheadConfig().max_lookahead_s, abs=60.0)
        assert la.lookahead_s <= LookaheadConfig().max_lookahead_s

    def test_reset(self):
        rng = random.Random(4)
        la = LookaheadLearner()
        for k in range(20):
            _release(la, 1000.0 + k * 3600, rng)
        la.start(99999.0, 18.3, 18.0, -2.0 * PER_H)
        la.reset()
        assert la.lookahead_s == 0.0 and la.releases == () and la.fit is None
        assert not la.observing

    def test_lines_fit_the_activity_log(self):
        rng = random.Random(4)
        la = LookaheadLearner()
        for k in range(30):
            line = _release(la, 1000.0 + k * 3600, rng)
            assert len(line) < 230
        assert "look-ahead" in line and "fit from" in line


class TestPersistence:
    def test_round_trip(self):
        rng = random.Random(4)
        la = LookaheadLearner()
        for k in range(20):
            _release(la, 1000.0 + k * 3600, rng)
        la.start(200000.0, 18.3, 18.0, -2.0 * PER_H)
        la.note_setpoint(18.5)
        back = LookaheadLearner()
        assert back.restore(la.to_dict(), now=200100.0)
        assert back.lookahead_s == pytest.approx(la.lookahead_s, abs=0.1)
        assert len(back.releases) == len(la.releases)
        assert back.fit is not None
        assert back.fit.lookahead_s == pytest.approx(la.fit.lookahead_s, rel=0.02)
        assert back.observing
        back.observe(200200.0, 40.0, 200200.0)
        assert back.releases[-1].push_c == pytest.approx(0.5)

    def test_a_stale_or_future_pending_release_is_dropped(self):
        la = LookaheadLearner()
        la.start(1000.0, 18.3, 18.0, -2.0 * PER_H)
        data = la.to_dict()
        cfg = LookaheadConfig()
        # Its answer may still come in (made in the window, seen a poll later).
        kept = LookaheadLearner()
        kept.restore(data, now=1000.0 + cfg.observe_s + cfg.answer_seen_late_s)
        assert kept.observing
        late = LookaheadLearner()
        late.restore(data, now=1000.0 + cfg.observe_s + cfg.answer_seen_late_s + 1)
        assert not late.observing
        early = LookaheadLearner()
        early.restore(data, now=900.0)
        assert not early.observing

    @pytest.mark.parametrize("data", [
        None, [], "x", {"lookahead_s": "13"}, {"lookahead_s": -5},
        {"lookahead_s": 99999}, {"releases": [[1, 2, 3]]}, {"releases": [[1, 2, 3, 0]]},
        {"releases": [[1, 2, "x", 4]]}, {"pending": [1, 2, 3]},
        {"lookahead_s": math.nan, "releases": "nope", "pending": {"a": 1}},
    ])
    def test_garbage_is_left_out(self, data):
        la = LookaheadLearner()
        assert not la.restore(data, now=1000.0)
        assert la.lookahead_s == 0.0 and la.releases == () and not la.observing


# ---------------------------------------------------------------------------
# The options form (climate_controllers.options_form_errors / _values)
# ---------------------------------------------------------------------------

from tests.pure_modules import climate_controllers as cc  # noqa: E402


class TestOptionsForm:
    def test_the_lookahead_needs_its_heating_power_sensor(self):
        assert cc.options_form_errors({"valve_lookahead": True}) == {
            "base": "lookahead_needs_heating_power"}
        assert cc.options_form_errors({"valve_lookahead": True, "heating_power_entity_id": ""}) \
            == {"base": "lookahead_needs_heating_power"}
        assert cc.options_form_errors(
            {"valve_lookahead": True, "heating_power_entity_id": "sensor.zone_heating"}) == {}
        assert cc.options_form_errors({"valve_lookahead": False}) == {}
        assert cc.options_form_errors({}) == {}

    def test_shown_again_with_what_was_entered(self):
        saved = {"window_sensor_id": "binary_sensor.w", "comfort_target": 20.0,
                 "heating_power_entity_id": "sensor.old"}
        entered = {"valve_lookahead": True, "comfort_target": 21.0, "heating_power_entity_id": ""}
        keys = ("window_sensor_id", "heating_power_entity_id")
        values = cc.options_form_values(saved, entered, keys)
        # The window sensor was left empty in the form, the heating sensor cleared.
        assert values == {"comfort_target": 21.0, "valve_lookahead": True}
        assert cc.options_form_values(saved, {}, keys) == saved


class TestAnswerWindow:
    def test_a_late_report_after_skipped_cycles_is_not_the_answer(self):
        # Review finding (2.14.0): cycles that skip the look-ahead (the room
        # sensor lost) must not make a report 28 min later the first answer.
        la = LookaheadLearner()
        la.start(0.0, 18.5, 18.0, -3.0 * PER_H)
        line = la.observe(1800.0, 55.0, 1700.0)
        assert "no heating" in line and la.releases == () and not la.observing

    def test_an_answer_within_the_window_seen_late_still_counts(self):
        la = LookaheadLearner()
        la.start(0.0, 18.5, 18.0, -3.0 * PER_H)
        assert la.observe(900.0, 55.0, 200.0)
        assert la.releases[0].response_pct == 55.0

    def test_a_setpoint_from_before_the_command_is_not_the_push(self):
        la = LookaheadLearner()
        la.start(1000.0, 18.3, 18.0, -2.0 * PER_H)
        la.note_setpoint(20.0, seen_ts=1000.0)        # the release's own cycle
        la.note_setpoint(18.5, seen_ts=1060.0)
        la.observe(1150.0, 40.0, 1150.0)
        assert la.releases[0].push_c == pytest.approx(0.5)
