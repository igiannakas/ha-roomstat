"""A hold swing is judged over the last 6 h, not 24 h (2.27.5).

The tuner judges the hold every 3 h (``hold_window_s``) over a buffer of
the hold's samples.  A window that changes nothing is not cleared, so while
the target stands its samples run on, up to ``hold_buffer_s``: 24 h until
2.27.5.  Living room, 22 Nov 2026 (one target, 21.0, day and night), 03:20:

    hold: oscillation ±0.39 °C, period 318 min → the integral swung with it
    (0.27 °C a cycle): less Ki (×0.7); a large swing: emergency cut of Kp
    and Ki; changed Kp 2.20→1.87, Ki 0.00046→0.00032

and at 06:20 "no oscillation … changed Ki 0.00032→0.00023".  The buffer
reached back to 18:20, and its three crossings were an evening of people
(+0.54 at 21:29, the valve shut, CO2 1,055 → 1,333 ppm; the deadband decay
took the integral +0.29 → +0.03), its rebuild over 4 h (0.14 under at
01:40) and an ordinary top-up (+0.37 at 02:44): one drain and its rebuild,
read as the integral swinging.  The 06:20 step is the rest of the same cut:
the integral verdict and the emergency each trim Ki ×0.7 (×0.49 in all),
and a change moves Ki one ×0.7 step, the next one at the 3-h limit for a
gentler change, on the next window whatever it shows.

``fixtures/hold_window_living_2026_11_22.json``: that night as the tuner
held it (a sample a minute, 18:20-06:20 UTC: room error, TRV demand,
integral, mode, the zone's heating %, CO2) and the 20 Nov 03:35 window
(the stop/release cycle, no cut), rebuilt from the recorder and the
activity log.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tests.pure_modules import autotune as A
from tests.pure_modules import parameters as P

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "hold_window_living_2026_11_22.json").read_text())
NIGHT = FIXTURE["night"]
AT_0320, AT_0620 = NIGHT["verdict_offsets_s"]
T0 = 1_763_000_000.0
H = 3600.0
TUNING = A.Tuning(FIXTURE["kp"], FIXTURE["ki"], FIXTURE["td_s"])
OLD_BUFFER = dict(hold_buffer_s=24 * H)   # 2.27.4


def _samples(rows, upto=None):
    return [(T0 + r[0], r[1], r[2], r[3]) for r in rows if upto is None or r[0] <= upto]


def _tuner(**cfg) -> A.Autotuner:
    return A.Autotuner(P.AutotuneConfig(**cfg), TUNING, derivative_allowed=True)


def _at_0320():
    return _samples(NIGHT["rows"], AT_0320)


class TestTheNightOf21To22Nov:
    def test_the_stock_buffer_is_6_h(self):
        assert P.AutotuneConfig().hold_buffer_s == 6 * H
        assert not hasattr(P.AutotuneConfig(), "osc_max_period_s")

    def test_24_h_reproduces_the_live_verdict(self):
        m = A.analyse_hold(_at_0320(), cfg=P.AutotuneConfig(**OLD_BUFFER), tuning=TUNING)
        assert m.swinging and m.oscillating
        assert m.half_cycles == 3
        assert m.period_s / 60 == pytest.approx(318, abs=2)
        assert m.amplitude_c == pytest.approx(0.39, abs=0.01)
        assert m.integral_swing_c == pytest.approx(0.27, abs=0.01)
        t = _tuner(**OLD_BUFFER)
        assert t._on_hold(m, m.ts) is not None
        assert t.last_verdict.startswith(
            "the integral swung with it (0.27 °C a cycle): less Ki (×0.7); a large swing: "
            "emergency cut of Kp and Ki; changed Kp 2.20→1.87, Ki 0.00046→0.00032")

    def test_the_follow_on_step_is_the_rest_of_the_trim(self):
        """Ki is trimmed ×0.49 (the integral's ×0.7 and the emergency's), a
        change moves it one ×0.7 step, and the next step comes with the next
        window 3 h later, which showed nothing."""
        t = _tuner(**OLD_BUFFER)
        m = A.analyse_hold(_at_0320(), cfg=t.cfg, tuning=TUNING)
        t._on_hold(m, m.ts)
        assert t._trim["ki"] == pytest.approx(0.49)
        after = [r for r in NIGHT["rows"] if r[0] > AT_0320]
        m2 = A.analyse_hold(_samples(after), cfg=t.cfg, tuning=t.active_tuning())
        assert not m2.swinging
        t._on_hold(m2, m2.ts)
        assert t.last_verdict.endswith("no Ki vote; changed Ki 0.00032→0.00023")
        assert t.active_tuning().ki == pytest.approx(0.00046 * 0.49)
        assert t.active_tuning().kp == pytest.approx(1.87)

    def test_6_h_is_no_swing(self):
        """The last 6 h (from 21:20) hold two crossings: no swing, no cut."""
        m = A.analyse_hold(_at_0320(), cfg=P.AutotuneConfig(), tuning=TUNING)
        assert m.half_cycles == 2
        assert not m.swinging and not m.oscillating
        t = _tuner()
        assert t._on_hold(m, m.ts) is None
        assert t.active_tuning() == TUNING
        assert t._last_event.startswith("hold: mean error")

    @pytest.mark.parametrize(("hours", "flagged"), [(6, False), (7, False), (7.25, False),
                                                    (7.5, True), (8, True), (24, True)])
    def test_where_the_buffer_starts_to_flag_it(self, hours, flagged):
        """From 7.5 h up the buffer reaches the under-shoot before the
        evening's people: 8 h would not have fixed it."""
        m = A.analyse_hold(_at_0320(), cfg=P.AutotuneConfig(hold_buffer_s=hours * H),
                           tuning=TUNING)
        assert m.oscillating is flagged

    def test_the_evidence(self):
        rows = NIGHT["rows"]

        def at(hhmm):   # UTC (the house's local time in November)
            h, mi = (int(x) for x in hhmm.split(":"))
            return rows[((h - 18) % 24) * 60 + mi - 21]   # the rows start at 18:21
        # 21:29: people, the valve shut, the integral drained by the decay.
        peak = min(rows[160:200], key=lambda r: r[1])
        assert peak[1] == pytest.approx(-0.54, abs=0.01)
        assert peak[4] == "stop" and peak[5] == 0
        assert at("21:00")[6] < 1100 < 1250 < peak[6]
        assert at("21:00")[3] == pytest.approx(0.29, abs=0.01)
        assert at("22:00")[3] < 0.05
        # Rebuilt by 02:30 at Ki 0.00046, then a top-up overshoot at 02:44.
        assert at("02:30")[3] == pytest.approx(0.29, abs=0.01)
        assert max(r[5] or 0 for r in rows[480:500]) >= 70
        assert min(r[1] for r in rows[500:520]) == pytest.approx(-0.37, abs=0.02)

    @pytest.mark.parametrize("old", [False, True])
    def test_through_observe(self, old):
        """Cycle by cycle, as the entity feeds it, with an hour on the target
        before (a hold window starts once the target has stood an hour):
        windows at 21:20, 00:20, 03:20 and 06:20, as live."""
        cfg = OLD_BUFFER if old else {}
        t = _tuner(**cfg)
        sp = FIXTURE["setpoint_c"]
        first = NIGHT["rows"][0]
        feed = [[off - H, *first[1:]] for off in range(0, int(H), 60)] + NIGHT["rows"]
        for off, err, demand, integral, _mode, _valve, _co2 in feed:
            t.observe(A.AutotuneSample(
                T0 + off, sp, sp - err, 15.0, None if demand is None else 15.0 + demand,
                full_power=False, integral_c=integral))
        journal = [text for _, text, _ in t.journal_since(0)]
        holds = [x for x in journal if x.startswith("hold:")]
        assert len(holds) == 4
        if old:
            assert holds[2].startswith("hold: oscillation ±0.39 °C, period 318 min → ")
            assert "changed Kp 2.20→1.87, Ki 0.00046→0.00032" in holds[2]
            assert holds[3].endswith("changed Ki 0.00032→0.00023")
        else:
            assert not any(x.startswith(("hold: oscillation", "hold: swing")) for x in holds)
            assert t.active_tuning() == TUNING

    @pytest.mark.parametrize("old", [False, True])
    def test_20_nov_stays_the_stop_release_cycle(self, old):
        """"swing ±0.29 °C, period 155 min → the integral held steady (0.04
        °C a cycle …) … no cut" live; rebuilt, ±0.29, 180 min, 0.06."""
        w = FIXTURE["window_20_nov"]
        cfg = OLD_BUFFER if old else {}
        m = A.analyse_hold(_samples(w["rows"]), cfg=P.AutotuneConfig(**cfg), tuning=TUNING)
        assert m.swinging and not m.oscillating
        assert m.amplitude_c == pytest.approx(0.29, abs=0.01)
        assert m.integral_swing_c < 0.1
        t = _tuner(**cfg)
        assert t._on_hold(m, m.ts) is None
        assert t.active_tuning() == TUNING
        assert t.last_verdict.startswith("the integral held steady (0.06 °C a cycle, needs 0.20): "
                                         "the stop/release cycle, not the gains: no cut")


def _sine(hours, period_h, *, amp=0.4, integral=True):
    """A swing of the room error, with an integral that cycles with it."""
    p = period_h * H
    return [(k * 60.0, amp * math.sin(2 * math.pi * k * 60.0 / p), 0.5,
             (0.5 + 0.4 * math.sin(2 * math.pi * k * 60.0 / p - 1.0)) if integral else None)
            for k in range(int(hours * 60))]


class TestTheBuffer:
    def test_only_the_last_6_h_are_judged(self):
        """However much it is given: a 9-h cycle over a day is no swing now
        (2.27.4, 24 h and a 12-h cap, cut for it)."""
        s = _sine(24, 9)
        assert not A.analyse_hold(s, cfg=P.AutotuneConfig(), tuning=TUNING).swinging
        assert A.analyse_hold(s, cfg=P.AutotuneConfig(**OLD_BUFFER), tuning=TUNING).oscillating

    @pytest.mark.parametrize(("period_h", "share"), [(1.0, 1.0), (2.5, 1.0), (3.5, 1.0),
                                                     (4.5, (0.3, 0.7)), (6.0, 0.0)])
    def test_how_slow_a_cycle_6_h_sees(self, period_h, share):
        """Three crossings take one and a half periods after the first
        excursion: a cycle up to 3.5 h has them in every 6-h window,
        wherever it starts, one of 4.5 h in about half (the windows slide
        every 3 h, so it is still seen within a window or two), one of 6 h
        in none.  plant_sim's slowest integral cycle in its oscillating
        test room is 4.4 h; the coarse-sensor one with a 20-min release
        look-ahead 5.6-6.8 h (no longer cut for, see
        test_autotune.TestClosedLoop)."""
        starts = range(0, int(period_h * 60), 5)
        seen = sum(A.analyse_hold(_sine(6.01 + k / 60, period_h)[k:], cfg=P.AutotuneConfig(),
                                  tuning=TUNING).oscillating for k in starts) / len(starts)
        if isinstance(share, tuple):
            assert share[0] <= seen <= share[1]
        else:
            assert seen == share

    def test_the_tracker_keeps_6_h(self):
        t = _tuner()
        for k in range(10 * 60):
            ts = T0 + k * 60.0
            t.observe(A.AutotuneSample(ts, 20.0, 20.0 - 0.02 * math.sin(k / 30), 15.0, 15.2,
                                       full_power=False, integral_c=0.2))
        span = t._hold.samples[-1][0] - t._hold.samples[0][0]
        assert 5.9 * H <= span <= 6 * H

    def test_hold_windows_are_not_saved(self):
        """Only the judged windows' results are kept; a restart starts a new
        window, so nothing older than the restart is judged (no migration)."""
        t = _tuner()
        for k in range(2 * 60):
            t.observe(A.AutotuneSample(T0 + k * 60.0, 20.0, 19.95, 15.0, 15.2,
                                       full_power=False, integral_c=0.2))
        assert t._hold is not None
        d = json.loads(json.dumps(t.as_dict()))
        assert "hold" not in d["episode"]
        t2 = A.Autotuner(P.AutotuneConfig(), TUNING, derivative_allowed=True, stored=d)
        assert t2._hold is None
