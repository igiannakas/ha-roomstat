"""Where a heat-up starts from, and the study's dead time (2.24.2).

10 Nov 2026 15:01, study: 15.5 °C picked by hand and Resume schedule 3 s
later, the room 0.48 under Comfort 20.0 on a radiator still hot from
preheat.  The tuner read a step 15.5 -> 20.0 and booked a heat-up; the
fifth kept, it pushed out one with an 8.9-min dead time, leaving 8.5 min
and the 28.6 min of 7 Nov, whose steepest rise was measured 31 min after
the valve shut, in the room's own creep (a coast 2.23.2 already found was
not the radiator's).  The dead time went 8.9 -> 18.5 min, the heating rate
1.76 -> 1.42 °C/h, and Ki started down towards the SIMC value 0.00032.

So a heat-up's step is measured from the last target that stood
``heatup_step_settle_s`` (the 15-s settle of a change by hand), and after a
coast that is not the radiator's the dead time and heating rate are
measured only up to the stop; older saves drop them once.
"""
from __future__ import annotations

import copy
import datetime
import json
import math
from pathlib import Path

import pytest

from tests.ha_harness import (
    SCHEDULE,
    _Extra,
    _make_entity,
    _run,
    _start,
    e2e,
    reset_timers,
)
from tests.pure_modules import autotune as A
from tests.pure_modules import parameters as P

CFG = P.AutotuneConfig()
STUDY = A.Tuning(2.5, 0.00056964, 600.0)
FIX = Path(__file__).parent / "fixtures"
STUDY_12_NOV = json.loads((FIX / "autotune_study_2026_11_12.json").read_text())["state"]


def _tuner(stored=None):
    return A.Autotuner(CFG, STUDY, stored=stored)


def _feed(t, ts, sp, room, demand=1.0, eligible=True):
    return t.observe(A.AutotuneSample(ts, sp, room, 20.0, 20.0 + demand, eligible))


def _restart(t):
    return _tuner(json.loads(json.dumps(t.as_dict(), allow_nan=False)))


def _when(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%d %b %H:%M")


class TestTheStepIsFromATargetThatStood:
    def test_the_settle_is_the_change_by_hand_s(self):
        assert CFG.heatup_step_settle_s == P.MANUAL_SETTLE_S == 15.0

    def test_the_10_nov_tap(self):
        """Comfort 20.0 for 7 min, 15.5 at 15:01:14, Comfort again at
        15:01:17 with the room 0.48 under: no heat-up."""
        t = _tuner()
        for k in range(8):
            _feed(t, k * 60.0, 20.0, 19.52)
        _feed(t, 434.0, 15.5, 19.52, demand=-5.0)
        _feed(t, 437.0, 20.0, 19.52)
        assert t.phase != A.PHASE_HEATUP
        assert t.last_event != "heat-up started"
        for k in range(8, 40):
            _feed(t, k * 60.0, 20.0, 19.52 + 0.01 * (k - 7))
        assert t._counters["heatups"] == 0

    def test_a_step_counts_from_the_target_before_a_press_taken_back(self):
        """Night 18.5, Frost protection for 9 s, then Comfort 20.0: a step of
        1.5 from Night, as Night -> Comfort is."""
        t = _tuner()
        for k in range(5):
            _feed(t, k * 60.0, 18.5, 19.0)
        _feed(t, 300.0, 5.0, 19.0, demand=-14.0)
        _feed(t, 309.0, 20.0, 19.0)
        assert t.phase == A.PHASE_HEATUP
        assert t._heatup.start_ts == 309.0

    def test_a_target_that_stood_the_settle_counts(self):
        """Frost protection for 20 s, then Comfort with the room 0.5 under:
        a step from Frost, as before."""
        t = _tuner()
        for k in range(5):
            _feed(t, k * 60.0, 20.0, 19.5)
        _feed(t, 300.0, 5.0, 19.5, demand=-14.0)
        _feed(t, 320.0, 20.0, 19.5)
        assert t.phase == A.PHASE_HEATUP

    def test_a_morning_step(self):
        t = _tuner()
        for k in range(30):
            _feed(t, k * 60.0, 18.5, 18.9)
        _feed(t, 1800.0, 20.0, 18.9)
        assert t.phase == A.PHASE_HEATUP and t.last_event == "heat-up started"

    def test_only_at_the_change(self):
        """A room that drifts 0.4 under a few seconds after a small step is
        no heat-up, as before (the step is judged where the target moved)."""
        t = _tuner()
        for k in range(30):
            _feed(t, k * 60.0, 19.4, 19.7)
        _feed(t, 1800.0, 20.0, 19.65)               # 0.35 under: too close
        _feed(t, 1805.0, 20.0, 19.59)               # 0.41 under, 5 s later
        assert t.phase != A.PHASE_HEATUP

    def test_off_that_stood_is_no_target_and_resuming_is_a_heat_up(self):
        t = _tuner()
        _feed(t, 0.0, 20.0, 20.0)
        for k in range(1, 6):
            _feed(t, k * 60.0, math.nan, 19.0 - 0.2 * k)
        _feed(t, 360.0, 20.0, 18.0)
        assert t.phase == A.PHASE_HEATUP

    def test_off_taken_back_within_seconds_is_no_restart(self):
        t = _tuner()
        for k in range(8):
            _feed(t, k * 60.0, 20.0, 19.5)
        _feed(t, 480.0, math.nan, 19.5)
        _feed(t, 484.0, 20.0, 19.5)
        assert t.phase != A.PHASE_HEATUP

    def test_preheat_s_target_on_arrival_is_still_no_heat_up(self):
        """Away mode in charge (not eligible) at preheat's 20.0, then home
        at Comfort 20.0, with a tap of 15.5 between: nothing starts."""
        t = _tuner()
        for k in range(10):
            _feed(t, k * 60.0, 20.0, 19.4, eligible=False)
        _feed(t, 600.0, 15.5, 19.5, demand=-5.0)
        _feed(t, 603.0, 20.0, 19.5)
        assert t.phase != A.PHASE_HEATUP

    def test_it_survives_a_reload_within_the_tap(self):
        """A preset temperature change reloads the room: the target that
        stood, and the one being held and since when, ride along."""
        t = _tuner()
        for k in range(8):
            _feed(t, k * 60.0, 20.0, 19.52)
        _feed(t, 434.0, 15.5, 19.52, demand=-5.0)
        t = _restart(t)
        _feed(t, 437.0, 20.0, 19.52)
        assert t.phase != A.PHASE_HEATUP

    def test_a_save_from_before_takes_its_last_target_as_one_that_stood(self):
        t = _tuner()
        for k in range(5):
            _feed(t, k * 60.0, 18.5, 18.9)
        d = json.loads(json.dumps(t.as_dict()))
        for key in ("sp_held", "sp_held_since", "sp_settled"):
            del d["episode"][key]
        t = _tuner(d)
        _feed(t, 300.0, 20.0, 18.9)
        assert t.phase == A.PHASE_HEATUP

    def test_the_clock_going_back_is_no_target_that_stood(self):
        t = _tuner()
        for k in range(5):
            _feed(t, 1000.0 + k * 60.0, 20.0, 19.5)
        _feed(t, 1250.0, 15.5, 19.5, demand=-5.0)
        _feed(t, 100.0, 20.0, 19.5)                  # an hour back
        _feed(t, 103.0, 20.0, 19.5)
        assert t.phase != A.PHASE_HEATUP


class TestTheStudysDeadTime:
    def test_the_12_nov_save_drops_the_7_nov_dead_time_and_rate(self):
        """Its stored 7 Nov heat-up was not the radiator's after the stop;
        where its dead time and rate were measured is not saved, so they
        go.  One dead time is left (8.5 min): not enough for SIMC."""
        before = _tuner(copy.deepcopy({**STUDY_12_NOV, "coast_rules": 3}))
        assert before.heatup_model() == (pytest.approx(1.4186, abs=1e-3),
                                         pytest.approx(1111.2, abs=0.5))
        t = _tuner(copy.deepcopy(STUDY_12_NOV))
        m = t._heatups[3]
        assert m.coast_not_radiator and m.theta_s is None and m.rate_c_per_h is None
        assert t.heatup_model() == (pytest.approx(1.757, abs=1e-3), None)
        assert t._simc_ki() is None
        # The values in use are left to the tuner's usual pace and rules.
        assert t.active_tuning() == before.active_tuning()
        assert t._pending is None
        assert t.take_load_notes(_when) == [
            "the heat-up of 07 Nov 15:17: its dead time (28.6 min) and heating rate "
            "(1.08 °C/h) may come from the room's own rise after the valve shut: no "
            "longer counted",
            "dead time 18.5 min→not known until 2 heat-ups give one; "
            "heating rate 1.42→1.76 °C/h",
        ]
        assert t.take_load_notes(_when) == []
        assert t.as_dict()["coast_rules"] == 3
        assert _tuner(t.as_dict()).take_load_notes(_when) == []

    def test_ki_heads_back_to_the_configured_value(self):
        """With no dead time Ki's target is the configured value × its trim
        (0.00059), above the 0.00048 in use: the next update raises it, on
        trial, 12 h after the last change, no longer towards 0.00032."""
        t = _tuner(copy.deepcopy(STUDY_12_NOV))
        tgt = t._targets()
        assert tgt.ki == pytest.approx(0.00056964 * 1.0382452, rel=1e-4)
        assert tgt.ki > t.active_tuning().ki

    def test_two_more_heat_ups_give_it_a_dead_time_again(self):
        """The next one pushes 6 Nov's (8.5 min) out of the five kept, so a
        dead time needs two: until then preheat uses its own start-up delay
        (20 min by default) and Ki its configured value."""
        t = _tuner(copy.deepcopy(STUDY_12_NOV))
        for k, theta in enumerate((540.0, 600.0)):
            m = A.HeatupMetrics.from_dict(copy.deepcopy(STUDY_12_NOV["heatups"][0]))
            m.start_ts = 1794552000.0 + k * 86400.0
            m.ts = m.start_ts + 6000.0
            m.theta_s = theta
            t._on_heatup(m, m.ts)
            if k == 0:
                assert t.heatup_model()[1] is None
        assert t.heatup_model()[1] == pytest.approx(570.0)


@e2e
class TestThroughTheEntity:
    """The 10 Nov 15:01 tap on the stubbed Home Assistant."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_timers()
        yield
        reset_timers()

    def _room(self, monkeypatch):
        clock = [1_800_000_000.0]
        monkeypatch.setattr("time.time", lambda: clock[0])
        ent = _make_entity({"autotune_enabled": True, "correction_kp": 2.5,
                            "correction_ki": 0.00056964, "derivative_time_min": 10,
                            "comfort_target": 20.0, "heat_timer_s": 0},
                           schedule=True)
        ent.hass.set(SCHEDULE, "comfort")
        _run(_start(ent, extra=_Extra({"version": 1, "autotune": {}})))
        return ent, clock

    def _feed(self, ent, room):
        ent.coordinator.data = {
            "room_temp": room, "room_temp_ts": None,
            "tado_internal_temp": 17.0, "tado_setpoint": 18.0,
            "tado_min_temp": 5.0, "tado_max_temp": 25.0, "tado_off": False,
            "tado_cloud": True, "tado_scheduled": False, "tado_heating": True,
            "fallback_internal_temp": None, "fallback_setpoint": None,
        }

    def _comfort_for(self, ent, clock, minutes, room=19.52):
        for _ in range(minutes):
            clock[0] += 60.0
            self._feed(ent, room)
            _run(ent._async_regulation_cycle("timer"))

    def test_a_temperature_then_resume_schedule_is_no_heat_up(self, monkeypatch):
        ent, clock = self._room(monkeypatch)
        self._comfort_for(ent, clock, 7)
        assert ent.native_target_temperature == 20.0
        _run(ent.async_set_temperature(temperature=15.5))
        clock[0] += 3.0
        _run(ent.async_resume_schedule())
        assert ent.native_target_temperature == 20.0
        assert ent._autotuner.phase != A.PHASE_HEATUP
        self._comfort_for(ent, clock, 30)
        assert ent._autotuner._counters["heatups"] == 0
        assert "Auto-tune: heat-up started" not in [e.text for e in ent._activity.entries()]

    def test_one_that_stood_the_settle_still_counts(self, monkeypatch):
        ent, clock = self._room(monkeypatch)
        self._comfort_for(ent, clock, 7)
        _run(ent.async_set_temperature(temperature=15.5))
        clock[0] += 20.0
        _run(ent.async_resume_schedule())
        assert ent._autotuner.phase == A.PHASE_HEATUP

    def test_the_12_nov_save_says_what_it_dropped(self):
        """Taken back by 2.24.2: the room's activity log says so, once, and
        preheat's start-up falls back to its own delay."""
        ent = _make_entity({"autotune_enabled": True, "correction_kp": 2.5,
                            "correction_ki": 0.00056964, "derivative_time_min": 10})
        _run(_start(ent, extra=_Extra({"version": 1,
                                       "autotune": copy.deepcopy(STUDY_12_NOV)})))
        texts = [e.text for e in ent._activity.entries()]
        assert any(t.startswith("Auto-tune: the heat-up of 7 Nov ")
                   and "dead time (28.6 min) and heating rate (1.08 °C/h)" in t
                   for t in texts)
        assert ("Auto-tune: dead time 18.5 min→not known until 2 heat-ups give one; "
                "heating rate 1.42→1.76 °C/h") in texts
        assert ent._autotuner.heatup_model()[1] is None
        assert ent._config.tuning.ki == pytest.approx(0.0004785)
