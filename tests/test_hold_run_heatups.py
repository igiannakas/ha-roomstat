"""Heat-ups the hold ran, never at full power (2.27.1).

15 Nov 2026, living room (Kp 2.0, Ki 0.00036): Frost protection by hand
17:42-18:26, then back to 20.9 with the room 0.43 under.  Full heat starts
from 0.8 under, so the hold ran the recovery (+1.4 over the valve's reading),
the stop came at 18:44 on a rise still speeding up, and the room peaked
+0.45 (people in all evening).  The tuner booked it as a heat-up: a 16-min
coast and +0.45, a "more braking" vote, and the braking time 16.4 -> 21.3
min.  The braking time is a full radiator's coast after full power; a
hold's coast and overshoot are the hold's and its stop's.

So each cycle tells the tuner whether it ran at full power (mode heat, or a
hold's full push: ``AutotuneSample.full_power``), and a heat-up none of whose
cycles did is logged and not counted.  A save from before has no record of
how its heat-ups ran: one that started under 0.8 with its peak demand in the
first 15 min under 2.5 is taken as the hold's and dropped once on load.

``fixtures/living_room_2026_11_15.json``: the evening as the tuner was fed
it (room sensor, the valve's reading, the setpoint it held), the tuner's
state before, and the live state after (diagnostics, 16 Nov 08:53).
"""
from __future__ import annotations

import copy
import datetime
import json
from pathlib import Path

import pytest

from tests.ha_harness import _Extra, _make_entity, _run, _start, e2e, reset_timers
from tests.pure_modules import autotune as A
from tests.pure_modules import parameters as P

CFG = P.AutotuneConfig()
FIXTURES = Path(__file__).parent / "fixtures"
LIVING = json.loads((FIXTURES / "living_room_2026_11_15.json").read_text())
RECORDED = json.loads((FIXTURES / "heatups_2026_11.json").read_text())
LIVING_BASE = A.Tuning(2.0, 0.00036, 660.0)
DAY = 86400.0


def _when(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%d %b %H:%M")


def _tuner(stored=None, base=A.Tuning(2.0, 0.0004, 600.0)):
    return A.Autotuner(P.AutotuneConfig(), base, stored=stored)


def _restart(t, base=A.Tuning(2.0, 0.0004, 600.0)):
    return _tuner(json.loads(json.dumps(t.as_dict(), allow_nan=False)), base)


def _cycle(t, ts, sp, room, demand, full_power):
    """One regulation cycle: the TRV demand is its setpoint - its reading."""
    return t.observe(A.AutotuneSample(ts, sp, room, 17.0, 17.0 + demand,
                                      full_power=full_power))


def _episode(t, t0, *, below, push, flags, peak_over=0.45, settle=True, told=True):
    """A target step to 20.0 with the room *below* under it, run with the
    TRV demand *push* (2 for a hold, 8 for a heat-up) and *flags* (a
    function of the minute) until the room reaches the target, then the
    stop: the room coasts on to ``20 + peak_over`` and stays.  The other
    cycles say they are not at full power, or nothing when not *told* (a
    caller from before 2.27.1).  Returns the time after the last cycle."""
    rest = False if told else None
    ts = t0
    for _ in range(30):                               # the old target stood
        _cycle(t, ts, 18.0, 20.0 - below, -3.0, rest)
        ts += 60.0
    room, k = 20.0 - below, 0
    while room < 20.0:                                # the push
        _cycle(t, ts, 20.0, room, push, flags(k))
        room = min(20.0, room + 1.5 / 60.0 * (1.0 if k > 5 else 0.2))
        ts += 60.0
        k += 1
    for j in range(20):                               # the stop, the coast
        room = 20.0 + peak_over * min(1.0, (j + 1) / 15.0)
        _cycle(t, ts, 20.0, room, -3.0, rest)
        ts += 60.0
    if settle:
        for _ in range(50):                           # settled: the episode ends
            _cycle(t, ts, 20.0, room, -3.0, rest)
            ts += 60.0
    return ts


class TestTheRule:
    def test_the_threshold_is_the_regulators_full_push(self):
        assert CFG.full_power_demand_c == P.RegulationConfig().hold_push_c
        assert CFG.full_power_step_c == P.RegulationConfig().heatup_enter_c

    @pytest.mark.parametrize(("step", "demand0", "full"), [
        (0.43, 1.6, False),      # the living room's 15 Nov recovery
        (0.70, 2.4, False),
        (0.70, 2.5, True),       # a full push in its first 15 min
        (0.80, 1.0, True),       # from 0.8 the regulator heats
        (1.50, None, True),
        (0.60, None, None),      # closer, and no demand saved: not known
    ])
    def test_inferred_for_a_save_from_before(self, step, demand0, full):
        assert A.inferred_full_power(step, demand0, CFG) is full

    def test_a_hold_run_heat_up_is_logged_and_not_counted(self):
        t = _tuner()
        before = (t.active_tuning(), dict(t._trim), dict(t._votes), t._model())
        _episode(t, DAY, below=0.45, push=2.0, flags=lambda k: False)
        assert t._counters["heatups"] == 1 and t._heatups == []
        assert (t.active_tuning(), dict(t._trim), dict(t._votes), t._model()) == before
        assert t.last_event == ("heat-up from 0.44 below, run by the hold: overshoot +0.45 °C, "
                                "coast 17 min")
        assert t.last_verdict.startswith(
            "never at full power, so not the radiator's coast, overshoot or heating rate: "
            "nothing counted")

    def test_the_same_at_full_power_learns_as_before(self):
        """A heat-up from 1.5 under, its push cycles at full power, and the
        same cycles with nothing said (a caller from before 2.27.1): the
        same result, its coast taken as the braking time."""
        results = []
        for flags, told in ((lambda k: True, True), (lambda k: None, False)):
            t = _tuner()
            _episode(t, DAY, below=1.5, push=8.0, flags=flags, told=told)
            assert len(t._heatups) == 1
            m = t._heatups[0]
            results.append((m.overshoot_c, m.coast_s, m.theta_s, t.active_tuning()))
            assert m.full_power is (True if told else None)
        assert results[0] == results[1]
        assert results[0][3].td_s == pytest.approx(900.0)          # 10 -> 15 min

    def test_a_full_push_counts(self):
        """From 0.6 under the hold runs it, but a push that reached 2.5 over
        the valve's reading fills the radiator: a braking-time episode."""
        t = _tuner()
        _episode(t, DAY, below=0.6, push=2.6, flags=lambda k: k >= 3)
        assert len(t._heatups) == 1 and t._heatups[0].full_power is True
        assert t.active_tuning().td_s == pytest.approx(900.0)      # 10 -> 15 min

    def test_the_new_heat_that_ends_it_is_not_its_own(self):
        """A heat command after the stop ends the episode (a top-up): that
        cycle's full power is the next heat-up's, not this one's."""
        t = _tuner()
        ts = _episode(t, DAY, below=0.45, push=2.0, flags=lambda k: False, settle=False)
        tracker = t._heatup
        assert tracker is not None and tracker.ran_held and not tracker.ran_full
        _cycle(t, ts, 20.0, 20.3, 8.0, True)
        assert t._heatup is None and t._heatups == []
        assert "run by the hold" in t.last_event

    def test_a_hold_run_heat_up_does_not_count_in_a_trial(self):
        t = _tuner()
        t._trim["kp"] = 1.4
        assert t._maybe_update(DAY, "test") and t._pending is not None
        _episode(t, DAY + 3600.0, below=0.45, push=2.0, flags=lambda k: False)
        assert t._pending is not None and t._pending["overshoots"] == []

    def test_it_takes_no_place_among_the_kept_heat_ups(self):
        t = _tuner()
        for k in range(CFG.history_len):
            _episode(t, (k + 1) * DAY, below=1.5, push=8.0, flags=lambda _: True,
                     peak_over=0.1)
        kept = [m.start_ts for m in t._heatups]
        _episode(t, 10 * DAY, below=0.45, push=2.0, flags=lambda k: False)
        assert [m.start_ts for m in t._heatups] == kept

    def test_a_reload_mid_episode_keeps_how_it_ran(self):
        for flags, kept in ((lambda k: False, False), (lambda k: k < 2, True)):
            t = _tuner()
            ts = _episode(t, DAY, below=0.45, push=2.0, flags=flags, settle=False)
            t = _restart(t)
            assert t.phase == A.PHASE_HEATUP
            for _ in range(50):
                _cycle(t, ts, 20.0, 20.45, -3.0, False)
                ts += 60.0
            assert t._counters["heatups"] == 1
            assert (len(t._heatups) == 1) is kept

    def test_an_episode_saved_before_is_judged_by_its_step_and_demand(self):
        """A heat-up in progress in a save from 2.27.0 (how it ran not kept):
        judged at its end, unless a cycle since showed full power."""
        for below, push, kept in ((0.45, 2.0, False), (1.5, 8.0, True)):
            t = _tuner()
            ts = _episode(t, DAY, below=below, push=push, flags=lambda k: None,
                          settle=False, told=False)
            d = json.loads(json.dumps(t.as_dict()))
            for key in ("ran_full", "ran_held", "told_from_start"):
                del d["episode"]["heatup"][key]
            d["coast_rules"] = 3
            t = _tuner(d)
            for _ in range(50):
                _cycle(t, ts, 20.0, 20.45, -3.0, None)
                ts += 60.0
            assert (len(t._heatups) == 1) is kept, below

    def test_a_gain_change_keeps_a_full_push_from_closer(self):
        t = _tuner()
        _episode(t, DAY, below=0.6, push=2.6, flags=lambda k: k >= 3)
        t.set_baseline(A.Tuning(1.5, 0.0004, 600.0), True)
        assert len(t._heatups) == 1


class TestTheLivingRoomOn15Nov:
    def _replay(self, full_power):
        t = A.Autotuner(CFG, LIVING_BASE, stored=copy.deepcopy(LIVING["tuner_before"]))
        seq = t.journal_seq
        st = LIVING["start_ts"]
        for off, sp, room, trv, held in LIVING["cycles"]:
            t.observe(A.AutotuneSample(st + off, sp, room, trv, held, full_power=full_power))
        return t, [text for _, text, _ in t.journal_since(seq)]

    def test_the_replay_is_the_evening_as_it_was_booked(self):
        """Fed as 2.27.0 fed it: +0.45, a 16-min coast, braking 16.4 -> 21
        (21.3 live; the recorded valve readings are 5-min steps)."""
        t, journal = self._replay(None)
        assert t.active_tuning().td_s / 60 == pytest.approx(21.0, abs=0.05)
        m = t._heatups[-1]
        live = LIVING["tuner_after"]["heatups"][-1]
        assert m.overshoot_c == pytest.approx(live["overshoot_c"])
        assert m.coast_s == pytest.approx(live["coast_s"], abs=30.0)
        assert m.step_c == pytest.approx(live["step_c"], abs=0.01)
        assert m.demand0_c == pytest.approx(live["demand0_c"])
        assert t._trim["td_s"] == pytest.approx(LIVING["tuner_after"]["trim"]["td_s"])
        assert journal[-1].startswith("heat-up: overshoot +0.45 °C, coast 16 min → overshoot "
                                      "+0.45 °C: more braking (×1.15); changed braking 16.4→21.0")

    def test_told_how_it_ran_it_no_longer_moves_the_braking_time(self):
        t, journal = self._replay(False)
        assert t.active_tuning().td_s / 60 == pytest.approx(16.4, abs=0.05)
        assert t._trim["td_s"] == pytest.approx(LIVING["tuner_before"]["trim"]["td_s"])
        assert len(t._heatups) == len(LIVING["tuner_before"]["heatups"])
        assert journal == [
            "heat-up started",
            "heat-up from 0.43 below, run by the hold: overshoot +0.45 °C, coast 16 min → "
            "never at full power, so not the radiator's coast, overshoot or heating rate: "
            "nothing counted; no change: Kp 2.00, Ki 0.00036, braking 16.4 min are at their "
            "targets (Ki as configured until 2 heat-ups give a dead time (has 0); braking = "
            "coast 14.2 min × 1.15)",
        ]


class TestTheSaveOf16Nov:
    def test_the_living_room_comes_back_to_16_4_min(self):
        t = A.Autotuner(CFG, LIVING_BASE, stored=copy.deepcopy(LIVING["tuner_after"]))
        assert t.active_tuning().td_s / 60 == pytest.approx(16.4, abs=0.05)
        assert t._last_good.td_s == t.active_tuning().td_s
        assert t._trim["td_s"] == pytest.approx(LIVING["tuner_before"]["trim"]["td_s"])
        assert [m.start_ts for m in t._heatups] == [
            m["start_ts"] for m in LIVING["tuner_after"]["heatups"][:-1]]
        assert t.heatup_model() == (pytest.approx(2.32, abs=0.005), None)
        assert t.take_load_notes(_when) == [
            "the heat-up of 15 Nov 18:26 started 0.43 below and the hold ran it, never at "
            "full power (overshoot +0.45 °C, coast 16 min): not the radiator's, no longer "
            "counted; the more braking it voted for (×1.15) is taken back",
            "braking 21.3→16.4 min, what its other heat-ups give (coast 14.2 min × 1.15)",
        ]

    def test_it_is_checked_once(self):
        t = A.Autotuner(CFG, LIVING_BASE, stored=copy.deepcopy(LIVING["tuner_after"]))
        saved = json.loads(json.dumps(t.as_dict()))
        assert saved["coast_rules"] == 4
        again = A.Autotuner(CFG, LIVING_BASE, stored=saved)
        assert again.take_load_notes(_when) == []
        assert again.active_tuning().td_s == pytest.approx(t.active_tuning().td_s, abs=0.1)

    def test_only_what_it_pulled_up_comes_down(self):
        """A hold-run heat-up that did not move the model (its coast the
        others' median, no vote) is dropped, and a braking time above the
        target for another reason (a smoothed step towards less braking)
        carries on at the tuner's pace."""
        d = copy.deepcopy(LIVING["tuner_after"])
        hold_run = d["heatups"][-1]
        hold_run["overshoot_c"] = 0.1
        hold_run["coast_s"] = 854.1                    # the others' 14.2 min
        t = A.Autotuner(CFG, LIVING_BASE, stored=d)
        assert t.active_tuning().td_s == pytest.approx(1278.8)
        assert t._trim["td_s"] == pytest.approx(d["trim"]["td_s"])
        notes = t.take_load_notes(_when)
        assert len(notes) == 1 and notes[0].startswith("the heat-up of 15 Nov 18:26")

    def test_a_braking_time_on_trial_falls_back_lower_too(self):
        d = copy.deepcopy(LIVING["tuner_after"])
        d["pending"] = {"ts": d["last_change_ts"], "prev": dict(d["params"]),
                        "ref_overshoot": 0.2, "ref_osc": False, "needs": 2,
                        "overshoots": []}
        t = A.Autotuner(CFG, LIVING_BASE, stored=d)
        assert t._pending["prev"]["td_s"] / 60 == pytest.approx(16.4, abs=0.05)


class TestTheRecordedHeatUps:
    """The 29 heat-ups of 1-9 Nov 2026 (``heatups_2026_11.json``): the four
    the hold ran, as their step and peak demand tell (they have no record
    of their modes; their demand never went over 2.4 before the stop, the
    full-power ones' 8.1-13.6)."""

    HOLD_RUN = {("living", "2026-11-04T19:38"), ("study", "2026-11-07T05:30"),
                ("second", "2026-11-02T22:32"), ("second", "2026-11-03T21:30")}

    def test_four_of_29_were_the_holds(self):
        hold_run = set()
        for e in RECORDED["episodes"]:
            st = e["start_ts"]
            m = A.analyse_heatup([(st + o, y, d) for o, y, d in e["samples"]],
                                 start_ts=st, setpoint_c=e["setpoint_c"], cfg=CFG,
                                 cross_ts=None if e["cross_s"] is None else st + e["cross_s"],
                                 tuning=A.Tuning(2.5, 0.0005, 600.0),
                                 truncated=e["truncated"])
            demand = max(d for o, _, d in e["samples"] if d is not None
                         and (e["cross_s"] is None or o <= e["cross_s"]))
            full = A.inferred_full_power(m.step_c, m.demand0_c, CFG)
            assert full is (demand >= CFG.full_power_demand_c)
            if not full:
                hold_run.add((e["room"], e["start"][:16]))
        assert hold_run == self.HOLD_RUN
        assert len(RECORDED["episodes"]) == 29

    @pytest.mark.parametrize(("room", "dropped"), [
        ("living", 0), ("master", 0), ("second", 0), ("study", 1)])
    def test_the_9_nov_saves(self, room, dropped):
        """Only the study's held one of them (7 Nov 05:30: +0.25, no vote)."""
        live = RECORDED["live_states"][room]
        base = {"living": (2.5, 0.00029887, 660.0), "study": (2.5, 0.00056964, 600.0),
                "second": (2.5, 0.00051301, 600.0), "master": (2.5, 0.00084, 600.0)}[room]
        t = A.Autotuner(CFG, A.Tuning(*base), stored=copy.deepcopy(live))
        assert len(live["heatups"]) - len(t._heatups) == dropped


@e2e
class TestThroughTheEntity:
    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_timers()
        yield
        reset_timers()

    def _room(self, monkeypatch, autotune):
        clock = [1_800_000_000.0]
        monkeypatch.setattr("time.time", lambda: clock[0])
        ent = _make_entity({"autotune_enabled": True, "correction_kp": 2.0,
                            "correction_ki": 0.00036, "derivative_time_min": 11,
                            "comfort_target": 20.9, "heat_timer_s": 0}, window=False)
        _run(_start(ent, extra=_Extra({"version": 1, "autotune": autotune})))
        return ent, clock

    def _minutes(self, ent, clock, n, room):
        for _ in range(n):
            clock[0] += 60.0
            ent.coordinator.data = {
                "room_temp": room, "room_temp_ts": None,
                "tado_internal_temp": 15.4, "tado_setpoint": 5.0,
                "tado_min_temp": 5.0, "tado_max_temp": 25.0, "tado_off": False,
                "tado_cloud": True, "tado_scheduled": False, "tado_heating": True,
                "fallback_internal_temp": None, "fallback_setpoint": None,
            }
            _run(ent._async_regulation_cycle("timer"))

    @pytest.mark.parametrize(("room", "full"), [(20.47, False), (19.6, True)])
    def test_each_cycle_says_how_it_ran(self, monkeypatch, room, full):
        """Frost protection by hand for 44 min, then Comfort 20.9: from 0.43
        under the hold runs it, from 1.3 under a heat-up."""
        ent, clock = self._room(monkeypatch, {})
        self._minutes(ent, clock, 10, 20.95)
        _run(ent.async_set_preset_mode("frost_protection"))
        self._minutes(ent, clock, 44, room)
        _run(ent.async_set_preset_mode("comfort"))
        self._minutes(ent, clock, 3, room)
        tracker = ent._autotuner._heatup
        assert tracker is not None
        assert ent._last_result.mode == ("heat" if full else "hold")
        assert (tracker.ran_full, tracker.ran_held) == (full, not full)

    def test_the_16_nov_save_says_what_it_dropped(self, monkeypatch):
        ent, _ = self._room(monkeypatch, copy.deepcopy(LIVING["tuner_after"]))
        assert ent._config.tuning.td_s / 60 == pytest.approx(16.4, abs=0.05)
        texts = [e.text for e in ent._activity.entries()]
        assert any(t.startswith("Auto-tune: the heat-up of 15 Nov 18:26 started 0.43 below "
                                "and the hold ran it") for t in texts)
        assert any(t.startswith("Auto-tune: braking 21.3→16.4 min") for t in texts)
