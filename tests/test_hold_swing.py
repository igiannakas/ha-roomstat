"""A hold window's swing is the gains' only when the integral swung with it
(2.27.2).

The tuner judges 3-h hold windows: a swing beyond +-0.15 °C around the mean
(wider with a wider band), three half-cycles or more, used to cut Kp (x0.85)
or Ki (x0.7), split by the period against six dead times (15 min assumed
where none was learned), and a cut has no way back up.  Living room, live:

- 12 Nov 19:19, "oscillation ±0.28 °C, period 69 min → a fast swing: less
  Kp", 2.5 -> 2.12;
- 17 Nov 00:21, "±0.23 °C, period 78 min → a fast swing: less Kp", 2.0 -> 1.7;
- 17 Nov 06:21, "±0.26 °C, period 97 min → a slow swing: less Ki", 0.00036 ->
  0.00025.

In each window the room was in stop 67-76 % of the time, the valve open
20-27 % (never over 58 %), every peak came with it shut, and the integral
barely moved: the hold's own stop/release cycle (release a little under, a
part-open top-up, the stop on its forecast near the target, the room
carried over by the radiator and people with the valve shut, a slow fall to
the release line).  In a gain limit cycle the integral is the state that
cycles: it winds up under the target, opens the valve wide, and drains in
the overshoot, every cycle.  So each cycle now tells the tuner the integral
(``AutotuneSample.integral_c``), and a swing whose integral did not rise and
fall with it by ``osc_integral_swing_c`` (0.2 °C) a cycle is the
stop/release cycle: logged, no cut.

``fixtures/hold_swings_living_2026_11.json``: the three windows as the
tuner held them (a sample a minute: room error, TRV demand, integral, mode,
the zone's heating %), rebuilt from the recorder and the activity log.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ha_harness import _make_entity, _run, _start, e2e, reset_timers
from tests.plant_sim import TADO_V3, PlantParams, ProxySettings, RoomSim, schedule_from
from tests.pure_modules import autotune as A
from tests.pure_modules import parameters as P
from tests.pure_modules import regulation as R

FIXTURES = Path(__file__).parent / "fixtures"
SWINGS = json.loads((FIXTURES / "hold_swings_living_2026_11.json").read_text())["windows"]
DAY = 86400.0
T0 = 1_763_000_000.0
# The live verdicts: (amplitude, period in min, cut).
LIVE = {
    "2026-11-12 19:18": (0.28, 69, "a fast swing: less Kp (×0.85)"),
    "2026-11-17 00:19": (0.23, 78, "a fast swing: less Kp (×0.85)"),
    "2026-11-17 06:19": (0.26, 97, "a slow swing: less Ki (×0.7)"),
}


def _cfg(w) -> P.AutotuneConfig:
    return P.AutotuneConfig(hold_band_over_c=w["band_over_c"],
                            hold_band_under_c=w["band_under_c"])


def _samples(w, *, integral=True):
    rows = w["rows"]
    if integral:
        return [(T0 + r[0], r[1], r[2], r[3]) for r in rows]
    return [(T0 + r[0], r[1], r[2]) for r in rows]


def _tuning(w) -> A.Tuning:
    return A.Tuning(w["kp"], w["ki"], w["td_s"])


def _tuner(w, cfg=None) -> A.Autotuner:
    return A.Autotuner(cfg or _cfg(w), _tuning(w), derivative_allowed=True)


@pytest.fixture(params=SWINGS, ids=[w["name"] for w in SWINGS])
def window(request):
    return request.param


class TestTheRecordedWindows:
    def test_they_reproduce_the_live_verdicts_without_the_integral(self, window):
        """Judged on the swing's shape alone (as 2.27.1 did, and as now for
        a caller that does not tell the integral): the live numbers."""
        amp, period, _ = LIVE[window["name"]]
        m = A.analyse_hold(_samples(window, integral=False), cfg=_cfg(window),
                           tuning=_tuning(window))
        assert m.oscillating and m.swinging
        assert m.integral_swing_c is None
        assert m.amplitude_c == pytest.approx(amp, abs=0.01)
        assert m.period_s / 60 == pytest.approx(period, abs=2)
        assert m.heating_fraction >= P.AutotuneConfig().hold_min_heating_fraction

    def test_with_the_integral_they_are_the_stop_release_cycle(self, window):
        m = A.analyse_hold(_samples(window), cfg=_cfg(window), tuning=_tuning(window))
        assert m.swinging and not m.oscillating
        assert m.integral_swing_c < 0.05 < P.AutotuneConfig().osc_integral_swing_c

    def test_the_evidence(self, window):
        """Mostly in stop, the valve never more than part-open, and every
        peak over the target reached with the valve shut."""
        rows = window["rows"]
        assert sum(r[4] == "stop" for r in rows) / len(rows) > 0.6
        assert max(r[5] or 0 for r in rows) < 60
        # The peaks: the lowest error of each spell over the stop line.
        peaks, spell = [], []
        for r in rows:
            if r[1] < -0.1:
                spell.append(r)
            elif spell:
                peaks.append(min(spell, key=lambda x: x[1]))
                spell = []
        assert len(peaks) >= 2
        assert all(p[5] == 0 and p[4] == "stop" for p in peaks)

    def test_the_tuner_cut_for_them_before(self, window):
        """Not told the integral, the tuner still makes the live cut."""
        t = _tuner(window)
        m = A.analyse_hold(_samples(window, integral=False), cfg=_cfg(window),
                           tuning=_tuning(window))
        reason = t._on_hold(m, m.ts)
        assert reason is not None
        assert t.last_verdict.startswith(LIVE[window["name"]][2])

    def test_the_tuner_leaves_the_gains(self, window):
        t = _tuner(window)
        m = A.analyse_hold(_samples(window), cfg=_cfg(window), tuning=_tuning(window))
        assert t._on_hold(m, m.ts) is None
        assert t.active_tuning() == _tuning(window)
        amp, period, _ = LIVE[window["name"]]
        assert t._last_event.startswith(f"hold: swing ±{m.amplitude_c:.2f} °C, period ")
        assert t.last_verdict.startswith(
            f"the integral held steady ({m.integral_swing_c:.2f} °C a cycle, needs 0.20): "
            "the stop/release cycle, not the gains: no cut")
        assert t.summary()["holds_analysed"] == 1

    @pytest.mark.parametrize("told", [True, False])
    def test_the_night_of_16_17_nov_through_observe(self, told):
        """Cycle by cycle, as the entity feeds it, from the 21:19 restart:
        an hour on the target before (a hold window starts once the target
        has stood an hour; the restart's own hour is not in the fixture),
        then 21:19-06:19.  The TRV demand is its setpoint minus its reading.
        Not told the integral, the live night: Kp cut at 00:19, "no
        oscillation" at 03:19 (which keeps the window), Ki cut at 06:19."""
        night = [w for w in SWINGS if w["name"].startswith("2026-11-17")]
        t = _tuner(night[0])
        sp = night[0]["setpoint_c"]
        start = _utc(night[0]["start_utc"])
        first = night[0]["rows"][0]
        feed = [[off - 3600.0, *first[1:]] for off in range(0, 3600, 60)]
        for w in night:
            shift = _utc(w["start_utc"]) - start
            feed += [[r[0] + shift, *r[1:]] for r in w["rows"]]
        for off, err, demand, integral, _mode, _valve in feed:
            t.observe(A.AutotuneSample(
                T0 + off, sp, sp - err, 15.0, None if demand is None else 15.0 + demand,
                full_power=False, integral_c=integral if told else None))
        journal = [text for _, text, _ in t.journal_since(0)]
        swings = [x for x in journal if x.startswith(("hold: swing", "hold: oscillation"))]
        assert len(swings) == 2
        if told:
            assert all("the stop/release cycle, not the gains: no cut" in x for x in swings)
            assert t.active_tuning() == _tuning(night[0])
        else:
            assert "a fast swing: less Kp (×0.85); changed Kp 2.00→1.70" in swings[0]
            assert "a slow swing: less Ki (×0.7); changed Ki 0.00036→0.00025" in swings[1]
            assert t.active_tuning().kp == pytest.approx(1.7)
            assert t.active_tuning().ki == pytest.approx(0.000252)


def _utc(text: str) -> float:
    import datetime
    return datetime.datetime.fromisoformat(text).timestamp()


def _cycling(ts_n, *, integral, amp=0.4, period_s=7200.0):
    """A sine swing of the room error with the given integral per sample."""
    import math
    return [(k * 60.0, amp * math.sin(2 * math.pi * k * 60.0 / period_s), 0.5,
             integral(k * 60.0)) for k in range(ts_n)]


class TestTheRule:
    def test_an_integral_that_cycles_with_the_swing_is_the_gains(self):
        import math
        s = _cycling(720, integral=lambda t: 0.5 + 0.4 * math.sin(2 * math.pi * t / 7200 - 1.0))
        m = A.analyse_hold(s, cfg=P.AutotuneConfig(), tuning=A.Tuning(2.0, 0.001))
        assert m.swinging and m.oscillating
        assert m.integral_swing_c == pytest.approx(0.8, abs=0.05)   # up 0.8 and down 0.8

    @pytest.mark.parametrize("shape", ["steady", "rises", "falls"])
    def test_one_that_only_rises_or_only_falls_is_not(self, shape):
        """A rise that stays (a colder night, a sag after people left) or a
        fall that stays (the deadband decay in an overshoot people made) is
        the integral doing its job."""
        f = {"steady": lambda t: 0.3,
             "rises": lambda t: 0.1 + 0.8 * t / 43200,
             "falls": lambda t: 0.9 - 0.8 * t / 43200}[shape]
        m = A.analyse_hold(_cycling(720, integral=f), cfg=P.AutotuneConfig(),
                           tuning=A.Tuning(2.0, 0.001))
        assert m.swinging and not m.oscillating
        assert m.integral_swing_c < 0.01

    def test_the_threshold(self):
        cfg = P.AutotuneConfig()
        assert cfg.osc_integral_swing_c == 0.2
        import math
        for half, expect in ((0.09, False), (0.11, True)):
            # Rises by 2 * half and falls by as much each cycle.
            s = _cycling(720, integral=lambda t, h=half: h * math.sin(2 * math.pi * t / 7200))
            m = A.analyse_hold(s, cfg=cfg, tuning=A.Tuning(2.0, 0.001))
            assert m.integral_swing_c == pytest.approx(2 * half, abs=0.005)
            assert m.oscillating is expect

    def test_not_told_in_a_half_cycle_is_judged_on_the_shape(self):
        s = _cycling(720, integral=lambda t: 0.3 if t == 0 else None)
        m = A.analyse_hold(s, cfg=P.AutotuneConfig(), tuning=A.Tuning(2.0, 0.001))
        assert m.swinging and m.oscillating and m.integral_swing_c is None

    def test_no_swing_no_integral_measure(self):
        m = A.analyse_hold([(k * 60.0, 0.02, 0.5, 0.3 + 0.001 * k) for k in range(300)],
                           cfg=P.AutotuneConfig(), tuning=A.Tuning(2.0, 0.001))
        assert not m.swinging and not m.oscillating and m.integral_swing_c is None


def _hold(*, swing, osc, amp=0.25, period=3600.0, isw=0.01):
    return A.HoldMetrics(ts=DAY, mean_error_c=-0.05, sd_c=amp / 2, half_cycles=4,
                         period_s=period, amplitude_c=amp, heating_fraction=0.3,
                         oscillating=osc, swinging=swing,
                         integral_swing_c=isw)


class TestTheVerdicts:
    def test_an_integral_cycle_cuts_ki_whatever_the_period(self):
        """A 60-min period read as "fast" (less Kp) with no dead time
        learned; the integral says which gain it is."""
        t = A.Autotuner(P.AutotuneConfig(), A.Tuning(2.0, 0.0006, 900.0))
        t._on_hold(_hold(swing=True, osc=True, isw=0.6), DAY)
        a = t.active_tuning()
        assert a.kp == 2.0 and a.ki == pytest.approx(0.00042)
        assert t.last_verdict.startswith("the integral swung with it (0.60 °C a cycle): "
                                         "less Ki (×0.7); changed")

    def test_a_large_integral_cycle_is_still_an_emergency(self):
        t = A.Autotuner(P.AutotuneConfig(), A.Tuning(2.0, 0.0006, 900.0))
        t._on_hold(_hold(swing=True, osc=True, amp=0.4, isw=0.6), DAY)
        a = t.active_tuning()
        assert a.kp < 2.0 and a.ki < 0.0006
        assert "emergency cut of Kp and Ki" in t.last_verdict

    def test_a_large_stop_release_swing_is_no_emergency(self):
        t = A.Autotuner(P.AutotuneConfig(), A.Tuning(2.0, 0.0006, 900.0))
        assert t._on_hold(_hold(swing=True, osc=False, amp=0.4), DAY) is None
        assert t.active_tuning() == A.Tuning(2.0, 0.0006, 900.0)

    def test_a_stop_release_swing_rolls_nothing_back(self):
        """A "more heat" change on trial is undone on an oscillation after
        it; the stop/release cycle is not one."""
        t = A.Autotuner(P.AutotuneConfig(), A.Tuning(2.0, 0.0006, 900.0),
                        derivative_allowed=False)
        t._trim["kp"] = 1.4
        assert t._maybe_update(DAY, "test") and t._pending is not None
        assert t._on_hold(_hold(swing=True, osc=False), DAY + 3600) is None
        assert t._pending is not None and t.summary()["rollbacks"] == 0
        assert not (t._on_hold(_hold(swing=True, osc=True, isw=0.5), DAY + 7200) or "").startswith(
            "no ") and t.summary()["rollbacks"] == 1

    def test_no_ki_vote_on_a_swing(self):
        t = A.Autotuner(P.AutotuneConfig(), A.Tuning(2.0, 0.0006, 900.0))
        t._votes["ki_up"] = 1
        t._on_hold(A.HoldMetrics(ts=DAY, mean_error_c=0.3, sd_c=0.1, half_cycles=4,
                                 period_s=3600.0, amplitude_c=0.25, heating_fraction=0.3,
                                 oscillating=False, swinging=True, integral_swing_c=0.0), DAY)
        assert t._votes["ki_up"] == 0

    def test_the_swing_clears_the_window(self):
        """A judged swing starts the evidence afresh, as an oscillation did."""
        w = SWINGS[1]
        t = _tuner(w)
        sp = w["setpoint_c"]
        first = w["rows"][0]
        for off, err, demand, integral, _m, _v in (
                [[o - 3600.0, *first[1:]] for o in range(0, 3600, 60)] + w["rows"]):
            t.observe(A.AutotuneSample(T0 + off, sp, sp - err, 15.0, 15.0 + demand,
                                       integral_c=integral))
        assert t._hold is not None and len(t._hold.samples) <= 1


class TestPersistence:
    def test_round_trip(self):
        m = _hold(swing=True, osc=False, isw=0.03)
        back = A.HoldMetrics.from_dict(json.loads(json.dumps(m.as_dict())))
        assert back.swinging is True and back.integral_swing_c == pytest.approx(0.03)
        assert back.swung

    def test_a_save_from_before(self):
        d = _hold(swing=True, osc=True).as_dict()
        del d["swinging"], d["integral_swing_c"]
        back = A.HoldMetrics.from_dict(d)
        assert back.swinging is None and back.integral_swing_c is None
        assert back.swung is True and back.oscillating is True

    @pytest.mark.parametrize("bad", [float("nan"), -0.1, "x"])
    def test_a_bad_value_is_dropped(self, bad):
        d = _hold(swing=True, osc=False).as_dict()
        d["integral_swing_c"] = bad
        d["swinging"] = "yes"
        back = A.HoldMetrics.from_dict(d)
        assert back.integral_swing_c is None and back.swinging is None

    def test_the_tuner_loads_an_older_save(self):
        t = A.Autotuner(P.AutotuneConfig(), A.Tuning(2.0, 0.0006, 900.0))
        t._on_hold(_hold(swing=True, osc=False), DAY)
        d = json.loads(json.dumps(t.as_dict()))
        for h in d["holds"]:
            del h["swinging"], h["integral_swing_c"]
        t2 = A.Autotuner(P.AutotuneConfig(), A.Tuning(2.0, 0.0006, 900.0), stored=d)
        assert len(t2._holds) == 1 and t2._holds[0].swinging is None


# A generic living room on the measured Tado V3+ controller at 0 °C, a
# steady 20 °C: the hold tops up from the release line and stops on its
# forecast, the radiator carries the room over, and it falls back.
GENERIC_LIVING = dict(trv_offset_c=-9.9, tau_room_h=40, heat_rate_c_per_h=2.5,
                      tau_rad_min=15, water_delay_min=3, mix_lag_min=3, trv_beta_c=8,
                      t_out_c=0.0, **TADO_V3)


def _closed_loop(plant, kp, ki, days, *, integral=True):
    c = P.RegulationConfig()
    c.tuning = P.CorrectionTuning(kp=kp, ki=ki, td_s=600.0)
    tuner = A.Autotuner(P.AutotuneConfig(), A.Tuning(kp, ki, 600.0), derivative_allowed=True)
    module = A
    if not integral:
        class _NotTold:   # a feed from before 2.27.2
            def __getattr__(self, name):
                return getattr(A, name)

            @staticmethod
            def AutotuneSample(*a, integral_c=None, **kw):  # noqa: N802
                return A.AutotuneSample(*a, **kw)
        module = _NotTold()
    sim = RoomSim(PlantParams(**plant), R, c, ProxySettings(), y0=19.0,
                  tuner=tuner, autotune_module=module, seed=3)
    sim.run(days * 24, schedule_from([(0, 20.0)]))
    return tuner


class TestClosedLoop:
    def test_a_sound_room_s_stop_release_cycle_is_left_alone(self):
        """Kp 2, Ki 0.0004 in a generic living room: judged on the shape
        alone, its stop/release cycle took Ki to its floor (0.0004 ->
        0.00005) in two days, six cuts; told the integral (pinned, the room's
        balance), every swing is logged and nothing is cut."""
        old = _closed_loop(GENERIC_LIVING, 2.0, 0.0004, 3, integral=False)
        assert old.active_tuning().ki < 0.0001
        new = _closed_loop(GENERIC_LIVING, 2.0, 0.0004, 3)
        assert new.active_tuning().kp == 2.0 and new.active_tuning().ki == 0.0004
        journal = [text for _, text, _ in new.journal_since(0)]
        assert sum("the stop/release cycle, not the gains" in x for x in journal) >= 4
        assert not any(x.startswith("hold: oscillation") for x in journal)


@e2e
class TestThroughTheEntity:
    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_timers()
        yield
        reset_timers()

    def test_each_cycle_tells_the_integral(self, monkeypatch):
        clock = [1_800_000_000.0]
        monkeypatch.setattr("time.time", lambda: clock[0])
        ent = _make_entity({"autotune_enabled": True, "correction_kp": 2.0,
                            "correction_ki": 0.00036, "derivative_time_min": 11,
                            "comfort_target": 20.0, "heat_timer_s": 0}, window=False)
        _run(_start(ent))
        seen = []
        orig = ent._autotuner.observe

        def observe(s):
            seen.append(s.integral_c)
            return orig(s)
        monkeypatch.setattr(ent._autotuner, "observe", observe)
        for k in range(20):
            clock[0] += 60.0
            ent.coordinator.data = {
                "room_temp": 19.9 - 0.002 * k, "room_temp_ts": None,
                "tado_internal_temp": 17.0, "tado_setpoint": 17.5,
                "tado_min_temp": 5.0, "tado_max_temp": 25.0, "tado_off": False,
                "tado_cloud": False, "tado_scheduled": False, "tado_heating": True,
                "fallback_internal_temp": None, "fallback_setpoint": None,
            }
            _run(ent._async_regulation_cycle("timer"))
        assert len(seen) == 20
        assert seen[-1] == pytest.approx(ent._reg_state.integral_c)
        assert seen[-1] > seen[0] >= 0.0      # the hold's integral, growing under
