"""A change on trial: ended early by a short landing, and after 3 days (2.27.3).

A "more heat" change (Ki up, less braking) goes on trial: it is judged on
the next ``confirm_up`` (2) heat-ups whose overshoot is known, and rolled
back if their median overshoot rose; until then no other change is made.
Judged heat-ups are rare (14-19 Nov 2026: 2, 1, 1 and 1 in the four rooms;
the schedule, windows and changes by hand cut most short), so at the old
7-day timeout every smoothed "less braking" step cost about a week.

Second bedroom: a trial from 15 Nov 07:27 (Ki 0.000513 -> 0.000594, braking
36.1 -> 35.0 min); on 18 Nov a heat-up stopped 0.95 under and landed -0.51:
"stopped 0.51 °C short: less braking (×0.85, 2 of 2 votes); no change while
the last one is on trial (1 of 2 heat-ups judged)", the braking left at
35.0 min against a target of 15.1 (coast 17.2 min × 0.88).

Owner, 19 Nov 2026 ("Do one and two"):
1. a judged heat-up that lands ``undershoot_sag_c`` (0.2) or more short of
   the target, with the valve told to stop and not cut short, ends the trial
   early, the change kept (as a passed trial), and the vote it cast is
   applied in the same verdict, subject to the usual rate limits; not when
   the overshoots the trial judged so far would roll it back;
2. ``pending_timeout_s`` 7 -> 3 days, with an activity line when a trial
   ends that way (a save whose trial is older is accepted on its first
   cycle).
"""
from __future__ import annotations

import copy
import datetime
import json

import pytest

from tests.ha_harness import _Extra, _make_entity, _run, _start, e2e, reset_timers
from tests.pure_modules import autotune as A
from tests.pure_modules import parameters as P

DAY = 86400.0
H = 3600.0
MIN = 60.0


def _utc(text: str) -> float:
    return datetime.datetime.fromisoformat(text).replace(tzinfo=datetime.UTC).timestamp()


def _heatup(ts, overshoot, *, coast=420.0, theta=900.0, td=780.0, truncated=False,
            crossed=True, full_power=True):
    return A.HeatupMetrics(
        ts=ts, start_ts=ts - H, setpoint_c=20.0, step_c=1.8, overshoot_c=overshoot,
        theta_s=theta, rate_c_per_h=2.5, demand0_c=5.0, coast_s=coast, coast_rise_c=0.3,
        kp=3.0, ki=0.001, td_s=td, truncated=truncated, crossed=crossed,
        full_power=full_power,
    )


def _on_trial(t, ts, *, ref=0.0, needs=2):
    """Put the current values on trial since *ts*, the values before them a
    braking time 2 min longer."""
    cur = t._params
    t._pending = {"ts": ts, "prev": A._tuning_dict(A.Tuning(cur.kp, cur.ki, cur.td_s + 120.0)),
                  "ref_overshoot": ref, "ref_osc": False, "needs": needs, "overshoots": []}
    t._last_change_ts = ts


def _tuner(cfg=None):
    t = A.Autotuner(cfg or P.AutotuneConfig(), A.Tuning(3.0, 0.001, 660.0),
                    derivative_allowed=True)
    t._params = A.Tuning(3.0, 0.001, 780.0)
    return t


def _restart(t, cfg=None):
    return A.Autotuner(cfg or P.AutotuneConfig(), t.baseline,
                       stored=json.loads(json.dumps(t.as_dict(), allow_nan=False)))


class TestAShortLandingEndsTheTrial:

    def test_kept_and_the_vote_applied_at_once(self):
        t = _tuner()
        t._votes["td_down"] = 1
        _on_trial(t, DAY)
        reason = t._on_heatup(_heatup(2 * DAY, -0.51), 2 * DAY)
        assert reason is not None                        # a change went out
        assert "the change on trial is kept: a landing 0.51 °C short is not what a " \
               "trial guards against (1 of 2 heat-ups judged)" in t.last_verdict
        # The values it judged are the last good ones, and the step the vote
        # wanted (less braking: more heat) is a new trial.
        assert t._last_good.td_s == pytest.approx(780.0)
        assert t._params.td_s < 780.0
        assert t._pending is not None and t._pending["ts"] == 2 * DAY
        assert t._pending["overshoots"] == []
        assert "(on trial: judged on the next 2 heat-ups)" in t.last_verdict

    def test_within_the_rate_limit_the_step_waits(self):
        """A trial started 6 h before: kept, but a change towards more heat
        still waits 12 h after the last one; the next verdict after that
        makes it."""
        t = _tuner()
        t._votes["td_down"] = 1
        _on_trial(t, DAY)
        t._on_heatup(_heatup(DAY + 6 * H, -0.40), DAY + 6 * H)
        assert t._pending is None and t._params.td_s == pytest.approx(780.0)
        assert "the change on trial is kept" in t.last_verdict
        assert "a change towards more heat waits 12 h after the last one" in t.last_verdict
        assert t.last_verdict_until == pytest.approx(DAY + 12 * H)
        assert t._maybe_update(DAY + 13 * H, "heat-up") is not None
        assert t._params.td_s < 780.0 and t._pending is not None

    def test_a_landing_015_short_does_not_end_it(self):
        t = _tuner()
        _on_trial(t, DAY)
        t._on_heatup(_heatup(2 * DAY, -0.15), 2 * DAY)
        assert t._pending is not None and t._pending["overshoots"] == [-0.15]
        assert "no change while the last one is on trial (1 of 2 heat-ups judged)" \
            in t.last_verdict

    def test_exactly_at_the_sag_line_does_not_end_it(self):
        t = _tuner()
        _on_trial(t, DAY)
        t._on_heatup(_heatup(2 * DAY, -0.2), 2 * DAY)
        assert t._pending is not None

    @pytest.mark.parametrize("kind", ["truncated", "never told to stop"])
    def test_a_heatup_that_did_not_judge_the_braking_does_not_end_it(self, kind):
        """Cut short, or the valve never told to stop (not enough heat): the
        braking and the gains did not decide that landing."""
        t = _tuner()
        _on_trial(t, DAY)
        m = _heatup(2 * DAY, -0.45, truncated=kind == "truncated",
                    crossed=kind != "never told to stop")
        t._on_heatup(m, 2 * DAY)
        assert t._pending is not None
        assert "no change while the last one is on trial" in t.last_verdict

    def test_a_heatup_the_hold_ran_does_not_count(self):
        t = _tuner()
        _on_trial(t, DAY)
        t._on_heatup(_heatup(2 * DAY, -0.45, full_power=False), 2 * DAY)
        assert t._pending is not None and t._pending["overshoots"] == []
        assert "never at full power" in t.last_verdict

    def test_not_when_the_overshoots_so_far_would_roll_it_back(self):
        """Three heat-ups to judge: a +1.2 overshoot then a -0.3 landing
        (median +0.45, over the reference +0.0 by more than 0.2): the trial
        stays open; a third decides it as before."""
        cfg = P.AutotuneConfig(confirm_up=3)
        t = _tuner(cfg)
        _on_trial(t, DAY, ref=0.0, needs=3)
        t._on_heatup(_heatup(2 * DAY, 1.2), 2 * DAY)
        t._on_heatup(_heatup(3 * DAY, -0.3), 3 * DAY)
        assert t._pending is not None and t._pending["overshoots"] == [1.2, -0.3]
        t._on_heatup(_heatup(4 * DAY, 0.5), 4 * DAY)
        assert t.summary()["rollbacks"] == 1

    def test_when_the_overshoots_so_far_would_not_roll_it_back(self):
        cfg = P.AutotuneConfig(confirm_up=3)
        t = _tuner(cfg)
        _on_trial(t, DAY, ref=0.0, needs=3)
        t._on_heatup(_heatup(2 * DAY, 0.3), 2 * DAY)
        t._on_heatup(_heatup(3 * DAY, -0.3), 3 * DAY)    # median 0.0
        assert "the change on trial is kept: a landing 0.30 °C short is not what a " \
               "trial guards against (2 of 3 heat-ups judged)" in t.last_verdict
        assert t.summary()["rollbacks"] == 0

    def test_the_second_judged_one_decides_as_before(self):
        """At confirm_up the trial ends the usual way, by the median."""
        t = _tuner()
        _on_trial(t, DAY, ref=0.0)
        t._on_heatup(_heatup(2 * DAY, 0.9), 2 * DAY)
        t._on_heatup(_heatup(3 * DAY, -0.3), 3 * DAY)    # median +0.3: rolled back
        assert t.summary()["rollbacks"] == 1

    def test_an_oscillation_still_rolls_back_a_trial_still_open(self):
        t = _tuner()
        _on_trial(t, DAY)
        m = A.HoldMetrics(ts=2 * DAY, mean_error_c=0.0, sd_c=0.2, half_cycles=4,
                          period_s=3600.0, amplitude_c=0.25, heating_fraction=0.4,
                          oscillating=True, swinging=True, integral_swing_c=0.5)
        t._on_hold(m, 2 * DAY)
        assert t.summary()["rollbacks"] == 1 and t._pending is None

    def test_carried_across_a_reload(self):
        t = _tuner()
        t._votes["td_down"] = 1
        _on_trial(t, DAY)
        back = _restart(t)
        assert back._pending is not None
        back._on_heatup(_heatup(2 * DAY, -0.5), 2 * DAY)
        assert "the change on trial is kept" in back.last_verdict
        again = _restart(back)
        assert again._last_good == back._last_good
        assert (again._pending is None) == (back._pending is None)


class TestTheTimeout:

    def test_three_days(self):
        assert P.AutotuneConfig().pending_timeout_s == 3 * DAY

    def _fed(self, t, ts):
        t.observe(A.AutotuneSample(ts, 20.0, 20.0, 18.0, 18.0))

    def test_kept_after_three_days_with_a_line(self):
        t = _tuner()
        _on_trial(t, DAY)
        seq = t.journal_seq
        self._fed(t, DAY + 3 * DAY - MIN)
        assert t._pending is not None
        self._fed(t, DAY + 3 * DAY + MIN)
        assert t._pending is None and t._last_good == t._params
        texts = [text for _n, text, _u in t.journal_since(seq)]
        assert texts == ["trial over after 3.0 days with 0 of 2 heat-ups judged: "
                         "braking 15.0→13.0 min kept"]

    def test_a_stale_trial_in_a_save_ends_on_its_first_cycle(self):
        """The study's trial (less braking, 17.8 -> 15.2 min from 14 Nov
        06:34, 1 of 2 judged) and a 2.27.2 save taken back on 19 Nov."""
        start = _utc("2026-11-14T06:34:00")
        t = A.Autotuner(P.AutotuneConfig(), A.Tuning(1.5, 0.00057, 600.0))
        t._params = A.Tuning(1.5, 0.00057, 15.2 * MIN)
        t._pending = {"ts": start, "prev": A._tuning_dict(A.Tuning(1.5, 0.00057, 17.8 * MIN)),
                      "ref_overshoot": 0.1, "ref_osc": False, "needs": 2,
                      "overshoots": [0.05]}
        t._last_change_ts = start
        back = _restart(t)
        assert back._pending is not None
        seq = back.journal_seq
        self._fed(back, _utc("2026-11-19T12:00:00"))
        assert back._pending is None
        assert back._last_good.td_s == pytest.approx(15.2 * MIN)
        texts = [text for _n, text, _u in back.journal_since(seq)]
        assert texts == ["trial over after 5.2 days with 1 of 2 heat-ups judged: "
                         "braking 17.8→15.2 min kept"]


class TestANewConfiguredGain:

    def test_says_when_it_ends_a_gain_change_on_trial(self):
        """19 Nov 2026, second bedroom: a new configured Kp or Ki ended its
        Ki raise on trial without a word."""
        t = A.Autotuner(P.AutotuneConfig(), A.Tuning(1.5, 0.000513, 600.0))
        t._params = A.Tuning(1.5, 0.000594, 35.0 * MIN)
        t._pending = {"ts": DAY, "prev": A._tuning_dict(A.Tuning(1.5, 0.000513, 36.1 * MIN)),
                      "ref_overshoot": 0.0, "ref_osc": False, "needs": 2, "overshoots": []}
        t.set_baseline(A.Tuning(1.5, 0.0006, 600.0), True)
        assert t._pending is None
        assert t.last_event.endswith("; the gain change on trial ended with them")

    def test_a_braking_change_on_trial_stays_without_that_note(self):
        t = A.Autotuner(P.AutotuneConfig(), A.Tuning(1.5, 0.000513, 600.0))
        t._params = A.Tuning(1.5, 0.000513, 35.0 * MIN)
        t._pending = {"ts": DAY, "prev": A._tuning_dict(A.Tuning(1.5, 0.000513, 36.1 * MIN)),
                      "ref_overshoot": 0.0, "ref_osc": False, "needs": 2, "overshoots": []}
        t.set_baseline(A.Tuning(1.5, 0.0006, 600.0), True)
        assert t._pending is not None
        assert "on trial" not in t.last_event


class TestSecondBedroom15To18Nov:
    """The second bedroom's 15-18 Nov 2026 as the tuner saw it (synthetic
    state with the live numbers: Kp 1.5, Ki 0.000594 on trial from 0.000513,
    braking 35.0 from 36.1 since 15 Nov 07:27:26, 0 of 2 judged, one vote for
    less braking; five kept heat-ups whose coast median becomes 17.2 min once
    18 Nov's 13-min coast is in; dead time 7.8 min), then 18 Nov 08:08:36:
    a heat-up that landed -0.51."""

    TRIAL = _utc("2026-11-15T07:27:26")
    LANDED = _utc("2026-11-18T08:08:36")

    def _state(self, cfg):
        t = A.Autotuner(cfg, A.Tuning(1.5, 0.000513, 600.0))
        for k, coast in enumerate([10.0, 8.0, 17.2, 25.0, 30.0]):
            t._heatups.append(_heatup(self.TRIAL - (5 - k) * DAY, 0.0, coast=coast * MIN,
                                      theta=7.8 * MIN, td=36.1 * MIN))
        t._params = A.Tuning(1.5, 0.000594, 35.0 * MIN)
        t._last_good = A.Tuning(1.5, 0.000513, 36.1 * MIN)
        span = 2.5 * 7.8 * MIN
        t._trim["ki"] = 0.000594 / (2.5 / (4.0 * span))     # Ki at its target
        t._trim["td_s"] = 0.88 / 0.85
        t._votes["td_down"] = 1
        t._pending = {"ts": self.TRIAL, "prev": A._tuning_dict(t._last_good),
                      "ref_overshoot": 0.0, "ref_osc": False, "needs": 2,
                      "overshoots": []}
        t._last_change_ts = self.TRIAL
        return t

    def _land(self, t):
        t._on_heatup(_heatup(self.LANDED, -0.51, coast=13 * MIN, theta=10 * MIN,
                             td=35.0 * MIN), self.LANDED)

    def test_the_landing_ends_the_trial_and_the_braking_steps_down(self):
        """2.27.2 said "no change while the last one is on trial (1 of 2
        heat-ups judged)" and kept 35.0 min (until a second judged heat-up,
        or 22 Nov 07:27, then the next episode).  Now the trial is kept, and
        the step the vote wants follows (3 days after the last change, well
        past the 12-h limit): 35.0 -> 29.8 min, itself on trial."""
        t = self._state(P.AutotuneConfig())
        self._land(t)
        assert t.last_verdict.startswith(
            "stopped 0.51 °C short: less braking (×0.85, 2 of 2 votes); the change on "
            "trial is kept: a landing 0.51 °C short is not what a trial guards against "
            "(1 of 2 heat-ups judged); changed braking 35.0→29.8 min, a step towards 15.1 "
            "(on trial: judged on the next 2 heat-ups)")
        assert t._targets().td_s / 60 == pytest.approx(15.1, abs=0.05)
        assert t._last_good == A.Tuning(1.5, 0.000594, 35.0 * MIN)
        assert t.active_tuning().td_s / 60 == pytest.approx(29.75, abs=0.01)
        assert t._pending["ts"] == self.LANDED

    def test_the_next_step_after_three_days_without_a_judged_heat_up(self):
        t = self._state(P.AutotuneConfig())
        self._land(t)
        t.observe(A.AutotuneSample(self.LANDED + 3 * DAY + MIN, 20.0, 20.0, 18.0, 18.0))
        assert t._pending is None
        assert t.last_event == ("trial over after 3.0 days with 0 of 2 heat-ups judged: "
                                "braking 35.0→29.8 min kept")
        assert t._maybe_update(self.LANDED + 3 * DAY + 2 * MIN, "heat-up") is not None
        # 30 % of the 14.6-min gap to 15.1 (under the 15 % step cap).
        assert t.active_tuning().td_s / 60 == pytest.approx(25.37, abs=0.01)


@e2e
class TestThroughTheEntity:
    """A restart's tuner lines in the room's activity log, after "Started"."""

    BASE = A.Tuning(2.0, 0.00036, 660.0)

    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_timers()
        yield
        reset_timers()

    def _save(self, *, trial_since):
        t = A.Autotuner(P.AutotuneConfig(), self.BASE)
        for k in range(5):
            t._heatups.append(_heatup(trial_since - (5 - k) * DAY, 0.0, coast=14 * MIN,
                                      theta=8 * MIN, td=16 * MIN))
        t._params = A.Tuning(2.0, 0.00036, 15.2 * MIN)
        t._pending = {"ts": trial_since,
                      "prev": A._tuning_dict(A.Tuning(2.0, 0.00036, 17.8 * MIN)),
                      "ref_overshoot": 0.1, "ref_osc": False, "needs": 2, "overshoots": [0.05]}
        t._last_change_ts = trial_since
        return json.loads(json.dumps(t.as_dict(), allow_nan=False))

    def _room(self, monkeypatch, autotune, now, kp=2.0):
        clock = [now]
        monkeypatch.setattr("time.time", lambda: clock[0])
        ent = _make_entity({"autotune_enabled": True, "correction_kp": kp,
                            "correction_ki": 0.00036, "derivative_time_min": 11,
                            "comfort_target": 20.0, "heat_timer_s": 0}, window=False)
        _run(_start(ent, extra=_Extra({"version": 1, "autotune": copy.deepcopy(autotune)})))
        return ent, clock

    def _cycle(self, ent, clock):
        clock[0] += 60.0
        ent.coordinator.data = {
            "room_temp": 20.0, "room_temp_ts": None,
            "tado_internal_temp": 17.0, "tado_setpoint": 17.0,
            "tado_min_temp": 5.0, "tado_max_temp": 25.0, "tado_off": False,
            "tado_cloud": True, "tado_scheduled": False, "tado_heating": False,
            "fallback_internal_temp": None, "fallback_setpoint": None,
        }
        _run(ent._async_regulation_cycle("timer"))

    @staticmethod
    def _texts(ent):
        return [e.text for e in reversed(ent._activity.entries())]   # oldest first

    def test_a_stale_trial_ends_on_the_first_cycle_with_its_line(self, monkeypatch):
        now = _utc("2026-11-19T12:00:00")
        ent, clock = self._room(monkeypatch, self._save(trial_since=_utc("2026-11-14T06:34:00")),
                                now)
        assert ent._autotuner._pending is not None
        self._cycle(ent, clock)
        assert ent._autotuner._pending is None
        texts = self._texts(ent)
        assert "Auto-tune: trial over after 5.2 days with 1 of 2 heat-ups judged: " \
               "braking 17.8→15.2 min kept" in texts
        assert ent._config.tuning.td_s / 60 == pytest.approx(15.2, abs=0.05)

    def test_a_fresh_trial_carries_on(self, monkeypatch):
        now = _utc("2026-11-19T12:00:00")
        ent, clock = self._room(monkeypatch, self._save(trial_since=now - 2 * DAY), now)
        self._cycle(ent, clock)
        assert ent._autotuner._pending is not None
        assert not any("trial over" in t for t in self._texts(ent))

    def test_a_new_configured_kp_is_a_line_after_started(self, monkeypatch):
        now = _utc("2026-11-19T12:00:00")
        ent, _clock = self._room(monkeypatch, self._save(trial_since=now - 2 * DAY), now,
                                 kp=2.1)
        texts = self._texts(ent)
        start = next(i for i, t in enumerate(texts) if t.startswith("Started"))
        line = next(i for i, t in enumerate(texts) if t.startswith(
            "Auto-tune: configured Kp or Ki changed: gain learning restarted; kept 5 "
            "full-power heat-ups and the braking time 15.2 min"))
        assert line > start
        assert ent._config.tuning.kp == pytest.approx(2.1)
        # Logged once: a second load of the same tuner has nothing new.
        assert ent._autotuner.take_load_notes(str) == []

    def test_a_new_configured_braking_time_is_a_line_too(self, monkeypatch):
        now = _utc("2026-11-19T12:00:00")
        save = self._save(trial_since=now - 2 * DAY)
        save["baseline"]["td_s"] = 900.0
        ent, _clock = self._room(monkeypatch, save, now)
        assert "Auto-tune: configured values changed – learning restarted" in self._texts(ent)
