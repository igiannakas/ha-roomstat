"""The hold's integral is kept while the target is at the frost level (2.27.0).

The integral is the room's slow balance: the push the hold has learned it
needs near the target.  It decays by ``integral_decay`` every cycle the room
is ``integral_deadband_c`` or more from the target, so at a frost-level
target (an error of about -16) it was gone within the hour.  14 Nov 2026,
living room (Kp 2.0, Ki 0.00036): Frost protection picked by hand 22:20-23:00
took it +0.42 -> +0.05, the room was 0.27 under at 23:00 with the valve at
59 %, and it took the night to rebuild to +0.64.  Now, while the target is at
most ``integral_hold_frost_margin_c`` (2.0) over the lowest command (Frost
protection, its stock 7.0 included, an open window's frost protection, Roomstat switched off), it is
neither decayed nor grown, for at most ``integral_hold_frost_max_s`` (3 h)
of frost-level time without a break; then it decays as before.
"""
from __future__ import annotations

import types
from dataclasses import replace

import pytest

from tests.ha_harness import (
    WINDOW,
    _active_timers,
    _climate,
    _Extra,
    _make_entity,
    _restart_from,
    _run,
    _settle,
    _start,
    e2e,
    reset_timers,
)
from tests.plant_sim import (
    LIVING_ROOM_FIT,
    TADO_V3,
    PlantParams,
    ProxySettings,
    RoomSim,
    schedule_from,
)
from tests.pure_modules import activity as act
from tests.pure_modules import autotune as A
from tests.pure_modules import climate_controllers as ctrl
from tests.pure_modules import parameters as P
from tests.pure_modules import regulation as R
from tests.tado_plant import Clock, TadoPlant, room_entity, run_room

HVACMode = _climate.HVACMode if _climate is not None else None
HOURS_3 = 3 * 3600.0
KEPT = 0.42


def _cfg(**kw):
    return P.RegulationConfig(**kw)


def step(reg, state, room, *, sp=20.0, trv=None, dt=60.0, slope_c_per_h=0.0):
    res = reg.compute(
        setpoint_c=sp, room_temp_c=room,
        tado_internal_c=room - 3.0 if trv is None else trv,
        time_delta_s=dt, state=state, room_slope_c_per_s=slope_c_per_h / 3600.0,
    )
    return res, res.new_state


def run(reg, state, room, n, **kw):
    res = None
    for _ in range(n):
        res, state = step(reg, state, room, **kw)
    return res, state


# ---------------------------------------------------------------------------
# The rule, HA-free (regulation.py)
# ---------------------------------------------------------------------------

class TestTheDefaults:
    def test_one_degree_over_the_lowest_command_for_3_hours(self):
        cfg = _cfg()
        assert cfg.integral_hold_frost_margin_c == 2.0
        assert cfg.integral_hold_frost_max_s == HOURS_3

    def test_the_stock_frost_protection_preset_is_at_the_frost_level(self):
        """The two defaults must not drift apart: a room on the stock Frost
        protection (7.0, a real setpoint, not an off) keeps it too."""
        cfg = _cfg()
        stock = P.PresetConfig().frost_protection_target_c
        assert cfg.presets.frost_protection_target_c == stock
        assert R.frost_level(stock, cfg)
        res, st = run(R.FeedforwardPiRegulator(cfg), R.RegulationState(integral_c=KEPT),
                      20.5, 30, sp=stock)
        assert res.integral_kept and st.integral_c == KEPT

    def test_the_frost_level(self):
        cfg = _cfg()
        assert R.frost_level(5.0, cfg)
        assert R.frost_level(7.0, cfg)                       # the margin's edge
        assert not R.frost_level(7.05, cfg)
        assert not R.frost_level(17.0, cfg)                  # Away, Night
        assert not R.frost_level(float("nan"), cfg)


class TestKeptAtTheFrostLevel:
    @pytest.mark.parametrize("integral", [KEPT, -0.3])
    def test_neither_decayed(self, integral):
        """40 min of Frost protection, the room 16 over: as it was."""
        res, st = run(R.FeedforwardPiRegulator(_cfg()), R.RegulationState(integral_c=integral),
                      21.0, 40, sp=5.0)
        assert res.mode == "stop" and res.integral_kept
        assert st.integral_c == integral
        assert st.integral_held_s == pytest.approx(2400.0)

    def test_nor_grown(self):
        """A room down at the frost level (in the deadband): no integration
        either (it would without the rule)."""
        kept, _ = run(R.FeedforwardPiRegulator(_cfg()), R.RegulationState(integral_c=0.2),
                      5.1, 30, sp=5.0, trv=8.0)
        off, _ = run(R.FeedforwardPiRegulator(_cfg(integral_hold_frost_max_s=0.0)),
                     R.RegulationState(integral_c=0.2), 5.1, 30, sp=5.0, trv=8.0)
        assert kept.new_state.integral_c == 0.2
        assert off.new_state.integral_c != 0.2

    def test_the_margin_s_edge(self):
        reg = R.FeedforwardPiRegulator(_cfg())
        _, st = step(reg, R.RegulationState(integral_c=KEPT), 20.0, sp=7.0)
        assert st.integral_c == KEPT
        _, st = step(reg, R.RegulationState(integral_c=KEPT), 20.0, sp=7.1)
        assert st.integral_c == pytest.approx(KEPT * 0.95)

    def test_the_first_cycle_counts_no_time(self):
        """dt 0 (the first cycle after a start, after Off -> Heat)."""
        _, st = run(R.FeedforwardPiRegulator(_cfg()), R.RegulationState(integral_c=KEPT),
                    21.0, 5, sp=5.0, dt=0.0)
        assert st.integral_c == KEPT and st.integral_held_s == 0.0


class TestStillDecays:
    """Everything else in the integral step is as before."""

    def test_on_an_ordinary_setback(self):
        """Night 18.5 with the room at 20.5."""
        res, st = step(R.FeedforwardPiRegulator(_cfg()), R.RegulationState(integral_c=KEPT),
                       20.5, sp=18.5)
        assert not res.integral_kept
        assert st.integral_c == pytest.approx(KEPT * 0.95)
        assert st.integral_held_s == 0.0

    def test_in_a_heat_up(self):
        res, st = step(R.FeedforwardPiRegulator(_cfg()), R.RegulationState(integral_c=KEPT),
                       19.0, sp=21.0)
        assert res.mode == "heat"
        assert st.integral_c == pytest.approx(KEPT * 0.95)

    def test_in_a_boost(self):
        res, st = step(R.FeedforwardPiRegulator(_cfg()), R.RegulationState(integral_c=KEPT),
                       20.5, sp=25.0)
        assert res.mode == "heat"
        assert st.integral_c == pytest.approx(KEPT * 0.95)

    @pytest.mark.parametrize("over", [0.3, 0.5, 1.0])
    def test_over_a_normal_target(self, over):
        res, st = step(R.FeedforwardPiRegulator(_cfg()), R.RegulationState(integral_c=KEPT),
                       20.0 + over, sp=20.0)
        assert not res.integral_kept
        assert st.integral_c == pytest.approx(KEPT * 0.95)


class TestTheLimit:
    def test_kept_for_3_hours_then_decaying(self):
        reg = R.FeedforwardPiRegulator(_cfg())
        res, st = run(reg, R.RegulationState(integral_c=KEPT), 21.0, 180, sp=5.0)
        assert res.integral_kept and st.integral_c == KEPT
        assert st.integral_held_s == pytest.approx(HOURS_3)
        res, st = step(reg, st, 21.0, sp=5.0)
        assert not res.integral_kept
        assert st.integral_c == pytest.approx(KEPT * 0.95)
        _, st = run(reg, st, 21.0, 9, sp=5.0)
        assert st.integral_c == pytest.approx(KEPT * 0.95 ** 10)

    def test_counted_from_the_cycles_time_steps(self):
        """Two-minute steps (a pause, capped by integration_step): 90 of them."""
        reg = R.FeedforwardPiRegulator(_cfg())
        res, st = run(reg, R.RegulationState(integral_c=KEPT), 21.0, 90, sp=5.0, dt=120.0)
        assert res.integral_kept and st.integral_c == KEPT
        res, st = step(reg, st, 21.0, sp=5.0, dt=120.0)
        assert not res.integral_kept

    def test_starts_again_once_the_target_leaves_the_frost_level(self):
        reg = R.FeedforwardPiRegulator(_cfg())
        _, st = run(reg, R.RegulationState(integral_c=KEPT), 21.0, 170, sp=5.0)
        _, st = step(reg, st, 20.4, sp=20.5)                 # back to Comfort for a cycle
        assert st.integral_held_s == 0.0
        kept = st.integral_c
        res, st = run(reg, st, 21.0, 180, sp=5.0)            # another 3 hours kept
        assert res.integral_kept and st.integral_c == kept

    def test_a_limit_of_0_is_the_old_behaviour(self):
        """With the rule off the result is what it was before 2.27.0 (the
        frost level never reached), cycle by cycle, whatever the target."""
        old = R.FeedforwardPiRegulator(_cfg(integral_hold_frost_margin_c=-100.0))
        new = R.FeedforwardPiRegulator(_cfg(integral_hold_frost_max_s=0.0))
        seq = ([(21.0, 5.0)] * 40 + [(20.2, 20.5)] * 20 + [(19.0, 21.0)] * 10
               + [(20.6, 18.5)] * 10 + [(5.1, 5.0)] * 10)
        a = b = R.RegulationState(integral_c=KEPT)
        for room, sp in seq:
            ra, a = step(old, a, room, sp=sp)
            rb, b = step(new, b, room, sp=sp)
            assert not rb.integral_kept
            assert replace(rb, new_state=None) == replace(ra, new_state=None)
            assert b.integral_c == a.integral_c and b.mode == a.mode

    def test_decays_at_the_frost_level_with_the_rule_off(self):
        _, st = run(R.FeedforwardPiRegulator(_cfg(integral_hold_frost_max_s=0.0)),
                    R.RegulationState(integral_c=KEPT), 21.0, 40, sp=5.0)
        assert st.integral_c == pytest.approx(KEPT * 0.95 ** 40)
        assert round(st.integral_c, 2) == 0.05                # the 14 Nov 23:00 value


class TestSwitchedOff:
    """regulation.FeedforwardPiRegulator.switched_off: Roomstat off."""

    def test_kept_for_3_hours_then_decaying(self):
        reg = R.FeedforwardPiRegulator(_cfg())
        st = R.RegulationState(integral_c=KEPT, mode="stop")
        for _ in range(180):
            st, kept = reg.switched_off(st, 60.0)
            assert kept
        assert st.integral_c == KEPT and st.mode == "stop"
        st, kept = reg.switched_off(st, 60.0)
        assert not kept and st.integral_c == pytest.approx(KEPT * 0.95)

    def test_the_first_cycle_counts_no_time(self):
        st, kept = R.FeedforwardPiRegulator(_cfg()).switched_off(
            R.RegulationState(integral_c=KEPT), 0.0)
        assert kept and st.integral_held_s == 0.0 and st.integral_c == KEPT

    def test_a_frost_spell_carries_on_into_off(self):
        """Frost protection, then Off: one frost-level spell."""
        reg = R.FeedforwardPiRegulator(_cfg())
        _, st = run(reg, R.RegulationState(integral_c=KEPT), 21.0, 170, sp=5.0)
        for _ in range(10):
            st, kept = reg.switched_off(st, 60.0)
        assert kept
        st, kept = reg.switched_off(st, 60.0)
        assert not kept

    def test_with_the_rule_off_it_waits_as_before(self):
        state = R.RegulationState(integral_c=KEPT, integral_held_s=99999.0)
        st, kept = R.FeedforwardPiRegulator(_cfg(integral_hold_frost_max_s=0.0)).switched_off(
            state, 60.0)
        assert st is state and not kept


# ---------------------------------------------------------------------------
# The activity line (activity.py) and the record (climate_controllers.py)
# ---------------------------------------------------------------------------

CFG = P.RegulationConfig()


def _facts(**kw):
    base = dict(path=act.PATH_REGULATED, setpoint_c=5.0, target_label="Frost protection",
                room_c=20.9, trv_c=17.9, tado="off", mode="stop", prev_mode="stop",
                error_c=-15.9, slope_c_per_h=-0.3, command_c=5.0, command_off=True,
                frost_integral_c=KEPT, frost_integral_kept=True)
    base.update(kw)
    return act.CycleFacts(**base)


class TestTheActivityLine:
    def test_kept(self):
        line = act.describe_cycle(_facts(), CFG)
        assert "; integral +0.42 kept | room 20.9 → 5.0 Frost protection" in line

    def test_decays_after_3_hours(self):
        line = act.describe_cycle(_facts(frost_integral_c=0.3, frost_integral_kept=False), CFG)
        assert "; integral +0.30 decays after 3 h |" in line

    def test_switched_off(self):
        line = act.describe_cycle(_facts(path=act.PATH_OFF, mode=None), CFG)
        assert line.startswith("Switched off: valve kept off; integral +0.42 kept | room 20.9 |")

    @pytest.mark.parametrize("value", [None, 0.04, -0.049])
    def test_nothing_worth_a_word(self, value):
        assert "integral" not in act.describe_cycle(_facts(frost_integral_c=value), CFG)

    def test_a_negative_one(self):
        assert "; integral -0.20 kept" in act.describe_cycle(_facts(frost_integral_c=-0.2), CFG)

    def test_the_limit_up_is_a_new_line_at_once(self):
        kept = act.decision_key(_facts(), CFG)
        assert act.decision_key(_facts(frost_integral_c=0.41), CFG) == kept
        assert act.decision_key(_facts(frost_integral_kept=False), CFG) != kept
        assert act.decision_key(_facts(frost_integral_c=None), CFG) != kept

    def test_a_long_phrase_is_shortened_not_the_note(self):
        f = _facts(window=act.WINDOW_OVERRIDDEN, target_label="x" * 60,
                   tado="its Tado schedule")
        line = act.describe_cycle(f, CFG)
        assert len(line) <= act.STATE_MAX_LEN
        assert "; integral +0.42 kept | room" in line

    def test_other_hours(self):
        cfg = P.RegulationConfig(integral_hold_frost_max_s=5400.0)
        line = act.describe_cycle(_facts(frost_integral_kept=False), cfg)
        assert "decays after 1.5 h" in line
        cfg = P.RegulationConfig(integral_hold_frost_max_s=2700.0)
        line = act.describe_cycle(_facts(frost_integral_kept=False), cfg)
        assert "decays after 45 min" in line


class TestTheRecord:
    TRV = "climate.trv"

    def test_the_held_time_rides_along(self):
        assert ctrl.integral_record(KEPT, 10.0, self.TRV, 2400.04) == {
            "c": KEPT, "ts": 10.0, "trv": self.TRV, "held_s": 2400.0}

    @pytest.mark.parametrize("held", [0.0, -1.0, float("nan")])
    def test_none_is_left_out(self, held):
        assert ctrl.integral_record(KEPT, 10.0, self.TRV, held) == {
            "c": KEPT, "ts": 10.0, "trv": self.TRV}

    def test_taken_back(self):
        assert ctrl.restore_integral_held_s({"c": KEPT, "held_s": 2400.0}) == 2400.0

    @pytest.mark.parametrize("data", [
        None, [], {}, {"c": KEPT}, {"held_s": "60"}, {"held_s": True},
        {"held_s": -5.0}, {"held_s": float("inf")}, {"held_s": float("nan")},
    ])
    def test_a_save_from_before_or_nonsense_is_0(self, data):
        assert ctrl.restore_integral_held_s(data) == 0.0


# ---------------------------------------------------------------------------
# The simulator: no effect on days the target never reaches the frost level
# ---------------------------------------------------------------------------

def _sim_trace(cfg, schedule, *, tune):
    tuner = None
    if tune:
        t = cfg.tuning
        tuner = A.Autotuner(A.AutotuneConfig(), A.Tuning(t.kp, t.ki, t.td_s),
                            derivative_allowed=True)
    sim = RoomSim(PlantParams(**dict(LIVING_ROOM_FIT, **TADO_V3)), R, cfg,
                  ProxySettings(min_command_interval_s=180, min_change_threshold_c=0.3),
                  y0=18.5, tuner=tuner, autotune_module=A, seed=3)
    tr = sim.run(72, schedule)
    return (tr.y, tr.cmd, tr.mode, tr.kp, tr.ki, tr.td, tr.sends), sim.state


class TestTheSimulator:
    @pytest.mark.parametrize("tune", [False, True])
    def test_an_ordinary_schedule_is_unchanged(self, tune):
        """Night 18 / Comfort 20 / Away 17 for three days: every per-cycle
        trace identical with the rule on and off."""
        sched = schedule_from([(0, 18.0), (6, 20.0), (9, 17.0), (16, 20.0), (21.5, 18.0)])
        on, _ = _sim_trace(P.RegulationConfig(), sched, tune=tune)
        off, _ = _sim_trace(P.RegulationConfig(integral_hold_frost_max_s=0.0), sched, tune=tune)
        assert on == off

    def test_a_frost_spell_is_not(self):
        """The same with 40 min of Frost protection every evening: the rule
        acts (a check that the comparison above can see a difference)."""
        sched = schedule_from([(0, 18.0), (6, 20.0), (21.0, 5.0), (21.67, 20.0), (22.5, 18.0)])
        on, _ = _sim_trace(P.RegulationConfig(), sched, tune=False)
        off, _ = _sim_trace(P.RegulationConfig(integral_hold_frost_max_s=0.0), sched, tune=False)
        assert on != off


# ---------------------------------------------------------------------------
# Through the entity
# ---------------------------------------------------------------------------

@pytest.fixture
def _timers():
    reset_timers()
    yield
    reset_timers()


def _holding(monkeypatch, *, rule=True, window=False, options=None):
    """A room holding just under Comfort (20.5) with the integral at +0.42."""
    clock = Clock(monkeypatch)
    ent = _make_entity(dict(options or {}), window=window)
    if window:
        ent.hass.set(WINDOW, "off")
    if not rule:
        ent._config.integral_hold_frost_max_s = 0.0
    plant = TadoPlant(ent, clock, room=20.38, fall_c_per_h=0.6)
    plant.feed()
    _run(_start(ent))
    run_room(ent, clock, plant, 30)
    assert ent._last_result.mode == "hold"
    ent._reg_state.integral_c = KEPT
    return clock, ent, plant


def _window_event(state):
    return types.SimpleNamespace(data={"new_state": types.SimpleNamespace(
        state=state, attributes={})})


def _frost(ent):
    _run(ent.async_set_preset_mode("frost_protection"))


def _comfort(ent, clock):
    """Back to Comfort by hand: the cycle it runs (its heat settles first)."""
    _run(ent.async_set_preset_mode("comfort"))
    return ent._last_result


def _decisions(ent):
    return [e.text for e in reversed(ent._activity.entries()) if e.decision]


@e2e
@pytest.mark.usefixtures("_timers")
class TestFrostProtectionByHand:
    def test_40_min_then_back(self, monkeypatch):
        clock, ent, plant = _holding(monkeypatch)
        _frost(ent)
        run_room(ent, clock, plant, 40)
        assert ent._reg_state.integral_c == KEPT
        assert ent._reg_state.integral_held_s == pytest.approx(2400.0, abs=120.0)
        first = _comfort(ent, clock)
        assert first.mode == "hold"
        assert ent._reg_state.integral_held_s == 0.0

    def test_the_first_hold_after_it_uses_it(self, monkeypatch):
        """The same run with the rule off: the integral has decayed, and the
        first hold after the Frost spell asks for that much less."""
        results = {}
        for rule in (True, False):
            reset_timers()
            clock, ent, plant = _holding(monkeypatch, rule=rule)
            _frost(ent)
            run_room(ent, clock, plant, 40)
            before = ent._reg_state.integral_c
            first = _comfort(ent, clock)
            results[rule] = (before, first)
        (kept, on), (decayed, off) = results[True], results[False]
        assert kept == KEPT
        assert decayed == pytest.approx(KEPT * 0.95 ** 40, abs=0.01)
        assert on.mode == off.mode == "hold"
        assert on.error_c == off.error_c
        assert on.target_for_tado_c - off.target_for_tado_c == pytest.approx(
            kept - decayed, abs=0.1)

    def test_the_activity_line(self, monkeypatch):
        clock, ent, plant = _holding(monkeypatch)
        _frost(ent)
        run_room(ent, clock, plant, 10)
        line = ent.extra_state_attributes["activity"]
        assert "; integral +0.42 kept | room" in line
        assert "Frost protection" in line
        assert ent._activity.latest.text == line

    def test_more_than_3_hours_decays(self, monkeypatch):
        clock, ent, plant = _holding(monkeypatch)
        _frost(ent)
        run_room(ent, clock, plant, 175)
        assert ent._reg_state.integral_c == KEPT
        run_room(ent, clock, plant, 25)
        assert 0.0 < ent._reg_state.integral_c < KEPT * 0.95 ** 15
        decisions = _decisions(ent)
        assert any("; integral +0.42 kept" in t for t in decisions)
        assert any("decays after 3 h" in t for t in decisions)
        first = _comfort(ent, clock)
        assert first.i_correction_c < KEPT * 0.95 ** 15

    def test_a_reload_keeps_the_integral_and_its_time(self, monkeypatch):
        clock, old, plant = _holding(monkeypatch)
        _frost(old)
        run_room(old, clock, plant, 60)
        held = old._reg_state.integral_held_s
        clock.tick(20.0)
        new = room_entity()
        _restart_from(old, new)
        plant.ent = new
        assert new.preset_mode == "frost_protection"
        assert new._reg_state.integral_c == KEPT
        assert new._reg_state.integral_held_s == pytest.approx(held)
        # The first cycle after the start counts no time: 3 h are up
        # (10800 - held) / 60 cycles after it.
        left = round((HOURS_3 - held) / 60.0)
        run_room(new, clock, plant, left)
        assert new._reg_state.integral_c == KEPT
        run_room(new, clock, plant, 2)
        assert new._reg_state.integral_c < KEPT

    def test_a_save_from_before_starts_the_time_again(self, monkeypatch):
        """A 2.26.0 save has the integral but no held time."""
        clock, old, plant = _holding(monkeypatch)
        _frost(old)
        run_room(old, clock, plant, 60)
        saved = old.extra_restore_state_data.as_dict()
        assert saved["integral"]["held_s"] > 0
        del saved["integral"]["held_s"]
        new = room_entity()
        _run(_start(new, extra=_Extra(saved)))
        assert new._reg_state.integral_c == KEPT
        assert new._reg_state.integral_held_s == 0.0

    def test_holding_saves_no_held_time(self, monkeypatch):
        clock, ent, plant = _holding(monkeypatch)
        assert "held_s" not in ent.extra_restore_state_data.as_dict()["integral"]


@e2e
@pytest.mark.usefixtures("_timers")
class TestAnOpenWindow:
    def _open(self, ent, clock, plant):
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_window_event("on"))
        clock.tick(30.0)
        pending = [c for c in _active_timers()
                   if getattr(c["callback"], "__name__", "") == "_async_window_action"]
        assert pending
        pending[-1]["cancelled"] = True
        _run(pending[-1]["callback"](None))
        assert ent.preset_mode == "frost_protection" and ent._window_ctrl.is_active

    def test_its_frost_protection_keeps_it(self, monkeypatch):
        results = {}
        for rule in (True, False):
            reset_timers()
            clock, ent, plant = _holding(monkeypatch, rule=rule, window=True)
            self._open(ent, clock, plant)
            run_room(ent, clock, plant, 40)
            results[rule] = ent._reg_state.integral_c
            line = ent.extra_state_attributes["activity"]
            if rule:
                assert line.startswith("Window open: valve off until it closes; "
                                       "integral +0.42 kept |")
            ent.hass.set(WINDOW, "off")
            ent._async_window_changed(_window_event("off"))
            for c in _active_timers():
                if getattr(c["callback"], "__name__", "") == "_async_window_close_action":
                    c["cancelled"] = True
                    _run(c["callback"](None))
            assert ent.preset_mode == "comfort" and not ent._window_ctrl.is_active
            results[rule] = (results[rule], ent._reg_state.integral_c)
        assert results[True][0] == KEPT
        assert results[False][0] == pytest.approx(KEPT * 0.95 ** 40, abs=0.01)
        # Back to Comfort: the kept integral is the one the hold carries on with.
        assert results[True][1] > results[False][1] + 0.3


@e2e
@pytest.mark.usefixtures("_timers")
class TestSwitchedOffEntity:
    def _off(self, ent):
        _run(ent.async_set_hvac_mode(HVACMode.OFF))

    def _on(self, ent):
        _run(ent.async_set_hvac_mode(HVACMode.HEAT))

    def test_40_min_off_keeps_it(self, monkeypatch):
        clock, ent, plant = _holding(monkeypatch)
        self._off(ent)
        run_room(ent, clock, plant, 40)
        assert ent._reg_state.integral_c == KEPT
        line = ent.extra_state_attributes["activity"]
        assert line.startswith("Switched off: valve kept off; integral +0.42 kept |")
        self._on(ent)
        _settle(clock)
        assert ent._reg_state.integral_held_s == 0.0
        assert ent._last_result.mode == "hold"
        assert ent._last_result.i_correction_c > 0.3

    def test_more_than_3_hours_off_decays(self, monkeypatch):
        clock, ent, plant = _holding(monkeypatch)
        self._off(ent)
        run_room(ent, clock, plant, 200)
        assert 0.0 < ent._reg_state.integral_c < KEPT * 0.95 ** 15
        assert any("Switched off: valve kept off; integral" in t and "decays after 3 h" in t
                   for t in _decisions(ent))

    def test_with_the_rule_off_it_waits_as_before(self, monkeypatch):
        """Before 2.27.0 an Off kept the integral however long it lasted."""
        clock, ent, plant = _holding(monkeypatch, rule=False)
        self._off(ent)
        run_room(ent, clock, plant, 300)
        assert ent._reg_state.integral_c == KEPT
        assert "integral" not in ent.extra_state_attributes["activity"]

    def test_a_reload_while_off_keeps_its_time(self, monkeypatch):
        clock, old, plant = _holding(monkeypatch)
        self._off(old)
        run_room(old, clock, plant, 100)
        held = old._reg_state.integral_held_s
        clock.tick(20.0)
        new = room_entity()
        _restart_from(old, new)
        plant.ent = new
        assert new.hvac_mode == HVACMode.OFF
        assert new._reg_state.integral_c == KEPT
        assert new._reg_state.integral_held_s == pytest.approx(held)


@e2e
@pytest.mark.usefixtures("_timers")
class TestTheLivingRoomOn14Nov:
    """Kp 2.0, Ki 0.00036: the integral +0.42 at 22:20, Frost protection by
    hand until 23:00.  Recorded: +0.05 at 23:00 (0.42 x 0.95^40), the room
    0.27 under.  Now: +0.42."""

    OPTIONS = {"correction_kp": 2.0, "correction_ki": 0.00036}

    def test_kept_through_the_40_minutes(self, monkeypatch):
        for rule, expected in ((False, 0.05), (True, KEPT)):
            reset_timers()
            clock, ent, plant = _holding(monkeypatch, rule=rule, options=self.OPTIONS)
            assert ent._config.tuning.kp == 2.0
            _frost(ent)
            run_room(ent, clock, plant, 40)
            assert round(ent._reg_state.integral_c, 2) == expected
            first = _comfort(ent, clock)
            assert first.mode == "hold"

