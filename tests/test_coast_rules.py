"""The heat-up's coast (2.23.2): it ends where the room's rise speeds up again
after slowing, a coast longer than any radiator's is not counted, and a save
from an older version is re-checked once when taken back.

The recorded heat-ups are in ``fixtures/heatups_2026_11.json``: every heat-up
the four rooms' auto-tuners judged, 1-9 Nov 2026, as ``analyse_heatup``
received it (replayed from the recorded activity, room-error and command
data), with what 2.23.1 measured, and each room's live tuner state of 9 Nov
2026 00:50.
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
FIX = json.loads((Path(__file__).parent / "fixtures" / "heatups_2026_11.json").read_text())
EPISODES = {(e["room"], e["start"][:16]): e for e in FIX["episodes"]}
KEYS = ("overshoot_c", "coast_s", "coast_rise_c", "theta_s", "rate_c_per_h", "later_rise_c")
BASELINE = {   # configured Kp, Ki, braking time (s), 9 Nov 2026
    "living": (2.5, 0.00029887, 660.0),
    "study": (2.5, 0.00056964, 600.0),
    "second": (2.5, 0.00051301, 600.0),
    "master": (2.5, 0.00084, 600.0),
}


def _analyse(e):
    st = e["start_ts"]
    samples = [(st + off, y, d) for off, y, d in e["samples"]]
    cross = None if e["cross_s"] is None else st + e["cross_s"]
    return A.analyse_heatup(samples, start_ts=st, setpoint_c=e["setpoint_c"], cfg=CFG,
                            cross_ts=cross, tuning=A.Tuning(2.5, 0.0005, 600.0),
                            truncated=e["truncated"])


def _measured(m):
    return {k: None if getattr(m, k) is None else round(getattr(m, k), 4) for k in KEYS}


CHANGED = {("second", "2026-11-04T19:38"), ("master", "2026-11-07T15:17"),
           ("study", "2026-11-07T15:17")}


class TestRecordedHeatups:
    @pytest.mark.parametrize("key", sorted(set(EPISODES) - CHANGED))
    def test_every_other_heatup_is_measured_as_before(self, key):
        """26 of the 29 recorded heat-ups: nothing moves, the radiator's own
        speed-up right after a stop included (second bedroom, 2 Nov 22:55:
        1.9 -> 3.2 °C/h in the 9 min after it)."""
        e = EPISODES[key]
        m = _analyse(e)
        if e["measured_2_23_1"] is None:
            assert m is None
            return
        assert _measured(m) == e["measured_2_23_1"]
        assert not m.coast_not_radiator

    def test_the_second_bedroom_4_nov_ends_before_the_room_rose_by_itself(self):
        """Stopped at 22:05 rising 0.2 °C/h, 0.4 for 10 min, 0.1 at 22:20,
        then 0.7, 1.0, 2.1, 2.5 °C/h with the valve shut and its own reading
        falling: 2.23.1 booked +0.36 (and, live, a 133-min coast) and the
        jump as its heating rate."""
        e = EPISODES[("second", "2026-11-04T19:38")]
        assert e["measured_2_23_1"]["overshoot_c"] == pytest.approx(0.36)
        m = _analyse(e)
        assert m.overshoot_c == pytest.approx(-0.07, abs=0.005)
        assert m.coast_rise_c == pytest.approx(0.07, abs=0.005)
        assert m.later_rise_c == pytest.approx(0.43, abs=0.005)    # shown, not counted
        assert m.rate_c_per_h == pytest.approx(0.86, abs=0.01)    # 2.13: the room's jump
        assert not m.coast_not_radiator

    def test_the_master_bedroom_7_nov_ends_at_its_slowest(self):
        """The rise slowed to 0.1 °C/h 27 min after the stop, then the room
        rose 0.3-0.5 °C/h again while the valve's reading fell 20.1 -> 18.5."""
        e = EPISODES[("master", "2026-11-07T15:17")]
        m = _analyse(e)
        assert e["measured_2_23_1"]["coast_s"] == pytest.approx(1560.0)
        assert m.coast_s == pytest.approx(1080.0)                  # 26 -> 18 min
        assert m.overshoot_c == pytest.approx(0.0, abs=0.005)      # +0.08 before
        assert m.theta_s == pytest.approx(e["measured_2_23_1"]["theta_s"])

    def test_a_coast_longer_than_any_radiator_is_not_counted(self):
        """Study, 7 Nov: after 7.75 h off, a 70-min heat-up stopped at 16:27
        rising 0.8 °C/h, and the room crept on for 100 min (+0.71): a
        51-min coast, more than the radiator gives.  Its dead time and
        heating rate are measured up to the stop (2.24.2): 2.23.1 took the
        steepest rise 31 min after it, in the room's own creep (28.1 min,
        1.08 °C/h)."""
        e = EPISODES[("study", "2026-11-07T15:17")]
        assert e["measured_2_23_1"]["coast_s"] > CFG.td_max_s
        m = _analyse(e)
        assert m.coast_not_radiator
        assert m.coast_s is None and m.overshoot_c is None
        assert m.coast_rise_c == pytest.approx(0.71)
        assert e["measured_2_23_1"]["theta_s"] / 60 == pytest.approx(28.1, abs=0.05)
        assert m.theta_s / 60 == pytest.approx(15.5, abs=0.05)
        assert m.rate_c_per_h == pytest.approx(0.96, abs=0.005)
        t = A.Autotuner(CFG, A.Tuning(2.5, 0.00057, 600.0))
        t._on_heatup(m, m.ts)
        assert "rose +0.71 °C after the stop, longer than any radiator's coast" in t.last_event
        assert t.last_verdict.startswith("not the radiator's rise: no coast or overshoot counted")
        back = A.HeatupMetrics.from_dict(m.as_dict())
        assert back.coast_not_radiator is True


def _coast(rates, *, before=20, step_s=60.0, y0=20.0):
    """A heat-up's samples: 1.0 °C/h for ``before`` minutes, then the
    stop, then one minute per given rate (°C/h)."""
    ts, ys, y = [], [], y0
    for k in range(before):
        ts.append(k * step_s)
        ys.append(y)
        y += 1.0 / 60.0
    cross = len(ts)
    for k, r in enumerate(rates):
        ts.append((before + k) * step_s)
        ys.append(y)
        y += r / 60.0
    return ts, ys, cross


class TestCoastEnd:
    def test_a_speed_up_right_after_the_stop_is_the_radiator(self):
        ts, ys, ci = _coast([2.0] * 10 + [1.5] * 5 + [1.0] * 5 + [0.5] * 5 + [0.2] * 5 + [0.0] * 20)
        assert A._coast_end(ts, ys, ci, CFG) == len(ts) - 1

    def test_a_rise_after_the_coast_slowed_ends_it(self):
        ts, ys, ci = _coast([0.8] * 12 + [0.3] * 12 + [2.0] * 15)
        end = A._coast_end(ts, ys, ci, CFG)
        assert ci + 12 <= end <= ci + 26
        assert ys[end] < ys[ci] + 0.3          # before the room's own rise

    def test_a_creep_without_a_speed_up_is_kept_whole(self):
        ts, ys, ci = _coast([0.8] * 10 + [0.4] * 10 + [0.2] * 30)
        assert A._coast_end(ts, ys, ci, CFG) == len(ts) - 1


def _tuner(room, state=None):
    kp, ki, td = BASELINE[room]
    stored = copy.deepcopy(FIX["live_states"][room]) if state is None else state
    return A.Autotuner(CFG, A.Tuning(kp, ki, td), stored=stored)


def _when(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%d %b %H:%M")


class TestOlderSaves:
    def test_the_second_bedroom_comes_back_to_its_other_heatups(self):
        """Live state of 9 Nov: braking 39.7 min from a model coast of 82 min
        (4 Nov's 133-min coast and 7 Nov's 31), its trim 1.15 from 4 Nov's
        +0.36.  Without that heat-up: coast 31.4 min, trim 1.0."""
        t = _tuner("second")
        assert t.active_tuning().td_s == pytest.approx(1884.0, abs=6.0)        # 31.4 min
        assert t._trim["td_s"] == pytest.approx(1.0)
        first = t._heatups[0]
        assert first.coast_s is None and first.overshoot_c is None and first.coast_not_radiator
        assert t._pending is not None                       # the Ki change stays on trial
        notes = t.take_load_notes(_when)
        assert notes == [
            "the heat-up of 04 Nov 19:37 rose +0.50 °C after the valve shut, a 133-min "
            "coast, longer than any radiator's: its coast and overshoot are no longer counted",
            "braking 39.7→31.4 min, what its other heat-ups give (coast 31.4 min × 1.00)",
            # 2.24.2: its 2.43 °C/h was the room's jump with the valve shut.
            "the heat-up of 04 Nov 19:37: its heating rate (2.43 °C/h) may come from the "
            "room's own rise after the valve shut: no longer counted",
            "heating rate 1.20→1.18 °C/h",
        ]
        assert t.take_load_notes(_when) == []               # once
        # What a rollback of that trial would restore was set before the
        # 4 Nov coast counted (26.4 min): left as it is.
        assert t._pending["prev"]["td_s"] == pytest.approx(1586.3)
        assert t._last_good.td_s == pytest.approx(1586.3)

    def test_a_rollback_cannot_bring_the_dropped_coast_back(self):
        """The same save with the trial's fallback and the last good values
        at 39.7 min too: they come down with it."""
        state = copy.deepcopy(FIX["live_states"]["second"])
        state["pending"]["prev"]["td_s"] = 2379.4
        state["last_good"]["td_s"] = 2379.4
        t = _tuner("second", state=state)
        assert t._pending["prev"]["td_s"] == pytest.approx(1884.0, abs=6.0)
        assert t._pending["prev"]["ki"] == pytest.approx(0.00035911)
        assert t._last_good.td_s == pytest.approx(1884.0, abs=6.0)
        t._rollback(t._pending["ts"] + 3600.0, "test")
        assert t.active_tuning().td_s == pytest.approx(1884.0, abs=6.0)

    def test_the_study_drops_its_7_nov_heatup_and_keeps_its_braking(self):
        """Its other heat-ups give 19.6 × 0.83 = 16.2 min, within the 2-min
        dead-band of the 17.8 it has: the braking time stays."""
        t = _tuner("study")
        assert t._heatups[-1].coast_not_radiator
        assert t.active_tuning().td_s == pytest.approx(1070.4)
        notes = t.take_load_notes(_when)
        assert len(notes) == 3 and notes[0].startswith("the heat-up of 07 Nov 15:17 rose +0.80 °C")
        # 2.24.2: and its dead time and rate, from the room's own creep.
        assert notes[1:] == [
            "the heat-up of 07 Nov 15:17: its dead time (28.6 min) and heating rate "
            "(1.08 °C/h) may come from the room's own rise after the valve shut: no "
            "longer counted",
            "dead time 8.9→8.7 min; heating rate 1.76→1.88 °C/h",
        ]

    @pytest.mark.parametrize("room", ["living", "master"])
    def test_rooms_with_no_such_heatup_are_untouched(self, room):
        live = FIX["live_states"][room]
        t = _tuner(room)
        assert t.active_tuning().td_s == pytest.approx(live["params"]["td_s"])
        assert [m.as_dict() for m in t._heatups] == [
            A.HeatupMetrics.from_dict(x).as_dict() for x in live["heatups"]]
        assert t.take_load_notes(_when) == []

    def test_a_new_save_is_not_checked_again(self):
        t = _tuner("second")
        saved = t.as_dict()
        assert saved["coast_rules"] == 3
        again = _tuner("second", state=copy.deepcopy(saved))
        assert again.take_load_notes(_when) == []
        # Saved to a tenth of a second, as every save is.
        assert again.active_tuning().td_s == pytest.approx(t.active_tuning().td_s, abs=1.0)
        assert again._heatups[0].coast_not_radiator



@e2e
def test_the_correction_is_logged_in_the_rooms_activity_log():
    """The second bedroom's save of 9 Nov taken back by 2.23.2: the room's
    activity log says what changed and why, after the start line."""
    reset_timers()
    ent = _make_entity({"autotune_enabled": True, "correction_kp": 2.5,
                        "correction_ki": 0.00051301, "derivative_time_min": 10})
    _run(_start(ent, extra=_Extra({"version": 1,
                                   "autotune": copy.deepcopy(FIX["live_states"]["second"])})))
    assert ent._config.tuning.td_s == pytest.approx(1884.0, abs=6.0)
    texts = [e.text for e in ent._activity.entries()]
    assert any(t.startswith("Auto-tune: the heat-up of 4 Nov ") and "133-min coast" in t
               for t in texts)
    assert any(t.startswith("Auto-tune: braking 39.7→31.4 min") for t in texts)
    reset_timers()
