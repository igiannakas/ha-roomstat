"""Deliberate valve control: closed loop against Tados that behave like the
real ones, and end to end through the climate entity.

The Tado V3+ only opens fully when its setpoint is about 5 °C above its own
reading, throttles at smaller pushes and re-opens by itself just below it
(tests/plant_sim.py ``TADO_V3``, and the two rooms fitted to real data,
``LIVING_ROOM_FIT`` / ``SECOND_BEDROOM_FIT``).  The feedforward + PI command
with the small Kp keeps the push at (1 + Kp) * error, so the valve reacts
late and blurred.  Deliberate control heats up at the maximum, lets the
radiator coast at the minimum, and holds the target with fine adjustments
(a high gain) cut by a firm stop ahead of an overshoot.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

import pytest

from tests.ha_harness import _make_entity, _run, _start, e2e, reset_timers
from tests.plant_sim import (
    LIVING_ROOM_FIT,
    SECOND_BEDROOM_FIT,
    TADO_V3,
    PlantParams,
    ProxySettings,
    RoomSim,
    hold_stats,
    schedule_from,
)
from tests.pure_modules import autotune as A
from tests.pure_modules import parameters as P
from tests.pure_modules import regulation as R

DAY = 86400.0
SCHED = schedule_from([(0, 18.0), (6, 20.0), (21.5, 18.0)])

# Rooms as measured (offsets as set in the Tado app).
LIVING = dict(trv_offset_c=-9.9, tau_room_h=50, t_out_c=12, trv_beta_c=6,
              heat_rate_c_per_h=2.5, tau_rad_min=12, water_delay_min=3, mix_lag_min=3)
SLOW = dict(trv_offset_c=-5.0, tau_room_h=40, t_out_c=12, trv_beta_c=6,
            heat_rate_c_per_h=1.2, tau_rad_min=15, water_delay_min=3, mix_lag_min=4)


def _sim(plant, stepped, *, days=8, tune=True, seed=3):
    c = P.RegulationConfig()
    c.tuning = P.CorrectionTuning(kp=0.6, ki=0.003)
    c.gain_scheduling_enabled = False
    c.stepped_enabled = stepped
    tuner = (A.Autotuner(A.AutotuneConfig(), A.Tuning(0.6, 0.003, 0.0),
                         derivative_allowed=True) if tune else None)
    sim = RoomSim(PlantParams(**dict(plant, **TADO_V3)), R, c,
                  ProxySettings(min_command_interval_s=180, min_change_threshold_c=0.3),
                  y0=18.5, tuner=tuner, autotune_module=A, seed=seed)
    return sim, sim.run(days * 24, SCHED)


def _reach_min(tr, day, band=0.15):
    t0 = day * DAY + 6 * 3600
    idx = tr.window(t0, t0 + 5 * 3600)
    k = next((i for i in idx if tr.y_true[i] >= tr.sp[i] - band), None)
    return (tr.t[k] - t0) / 60 if k is not None else 999.0


def _overshoot(tr, day):
    t0 = day * DAY + 6 * 3600
    idx = tr.window(t0, t0 + 5 * 3600)
    return max(tr.y_true[i] for i in idx) - tr.sp[idx[0]]


def _summary(tr, days=8):
    last = range(days - 3, days)
    return {
        "reach": statistics.mean(_reach_min(tr, d) for d in last),
        "overshoot": statistics.mean(_overshoot(tr, d) for d in last),
        "hold_sd": statistics.mean(
            hold_stats(tr, d * DAY + 9 * 3600, d * DAY + 21 * 3600)["sd"] for d in last),
        "sends": tr.sends / days,
    }


class TestSimulatedTado:
    def test_throttles_at_small_pushes_like_the_real_one(self):
        """Calibration guard: average opening per push (setpoint - reading),
        as in the recorder data (living room, October 2026)."""
        _, pi = _sim(LIVING, stepped=False, days=3, tune=False)
        _, st = _sim(LIVING, stepped=True, days=3, tune=False)
        b = defaultdict(list)
        for tr in (pi, st):
            for i, t in enumerate(tr.t):
                if t >= DAY and tr.cmd[i] > 5:
                    push = tr.cmd[i] - tr.tado[i]
                    key = "neg" if push < -1 else "1-2" if 1 <= push < 2 else ">=5" if push >= 5 else None
                    if key:
                        b[key].append(tr.valve[i])
        mean = {k: sum(v) / len(v) for k, v in b.items()}
        assert mean["1-2"] < 0.6          # real: 34 %
        assert mean[">=5"] > 0.85         # real: 93 % (3-min polls)
        assert mean["neg"] < 0.05         # real: ~1 %


@pytest.mark.parametrize("plant, faster_by", [(LIVING, 0.3), (SLOW, 0.1)])
def test_faster_with_less_overshoot_and_fewer_commands(plant, faster_by):
    _, pi = _sim(plant, stepped=False)
    _, st = _sim(plant, stepped=True)
    a, b = _summary(pi), _summary(st)
    assert b["reach"] < a["reach"] * (1 - faster_by)
    assert b["overshoot"] < a["overshoot"] - 0.1
    assert b["overshoot"] < 0.3
    assert b["hold_sd"] < a["hold_sd"] * 0.6
    assert b["sends"] < a["sends"]


def test_auto_tune_learns_the_coast_without_rollbacks():
    """The braking time becomes the heat-up's lead: the radiator's real
    coast after a hard stop (minutes, not the PI-era 45 min)."""
    sim, _ = _sim(LIVING, stepped=True)
    td_min = sim.tuner.active_tuning().td_s / 60
    assert 5 <= td_min <= 25
    assert sim.tuner.summary()["rollbacks"] == 0


def _fitted(plant, target, stepped, *, days=8):
    """A real room (fitted to 11 Oct 2026) with your settings of that day:
    Kp 0.6, Ki 0.003, 120 s / 0.2 °C, auto-tune on."""
    c = P.RegulationConfig()
    c.tuning = P.CorrectionTuning(kp=0.6, ki=0.003)
    c.gain_scheduling_enabled = False
    c.stepped_enabled = stepped
    tuner = A.Autotuner(A.AutotuneConfig(), A.Tuning(0.6, 0.003, 0.0), derivative_allowed=True)
    sim = RoomSim(PlantParams(**plant), R, c,
                  ProxySettings(min_command_interval_s=120, min_change_threshold_c=0.2),
                  y0=18.5, tuner=tuner, autotune_module=A, seed=3)
    tr = sim.run(days * 24, schedule_from([(0, 18.0), (6, target), (21.5, 18.0)]))
    last = range(days - 3, days)
    hold = [i for d in last for i in tr.window(d * DAY + 10 * 3600, d * DAY + 21 * 3600)]
    errs = [tr.y[i] - tr.sp[i] for i in hold]                # what the sensor shows
    peaks = []
    for d in last:
        idx = tr.window(d * DAY + 6 * 3600, d * DAY + 10 * 3600)
        peaks.append(max(tr.y[i] for i in idx) - target)
    return {"rms": statistics.mean(e * e for e in errs) ** 0.5,
            "worst": max(abs(e) for e in errs),
            "overshoot": statistics.mean(peaks), "sends": tr.sends / days}


@pytest.mark.parametrize("plant, target", [(LIVING_ROOM_FIT, 20.0), (SECOND_BEDROOM_FIT, 21.5)])
def test_real_rooms_held_tightly_with_fewer_commands(plant, target):
    old, new = _fitted(plant, target, False), _fitted(plant, target, True)
    assert new["rms"] < 0.05 and new["rms"] < old["rms"] / 4
    assert new["worst"] < 0.15
    assert new["overshoot"] < 0.2 and new["overshoot"] < old["overshoot"]
    assert new["sends"] < old["sends"]


def test_works_without_auto_tune():
    _, pi = _sim(LIVING, stepped=False, tune=False)
    _, st = _sim(LIVING, stepped=True, tune=False)
    a, b = _summary(pi), _summary(st)
    assert b["reach"] < a["reach"]
    assert b["overshoot"] < a["overshoot"] - 0.3


# ---------------------------------------------------------------------------
# End to end through the climate entity
# ---------------------------------------------------------------------------

@pytest.fixture
def _reset():
    reset_timers()
    yield
    reset_timers()


class _Clock:
    def __init__(self, monkeypatch, start=1_800_000_000.0):
        self.now = start
        monkeypatch.setattr("time.time", lambda: self.now)

    def tick(self, s=60.0):
        self.now += s


def _feed(ent, room, internal, setpoint=None):
    ent.coordinator.data = {
        "room_temp": room, "room_temp_ts": None,
        "tado_internal_temp": internal, "tado_setpoint": setpoint,
        "tado_min_temp": 5.0, "tado_max_temp": 25.0, "tado_off": False,
        "fallback_internal_temp": None, "fallback_setpoint": None,
    }


def _sent(ent):
    return [d.get("temperature") for _, svc, d in ent.hass.service_calls
            if svc == "set_temperature"]


def _cycle(ent):
    _run(ent._async_regulation_cycle("timer"))


@e2e
@pytest.mark.usefixtures("_reset")
class TestEntity:
    def test_heat_up_is_one_command_and_its_end_another(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _make_entity()
        _run(_start(ent))
        _feed(ent, 18.5, 9.0, setpoint=8.0)          # comfort 20.5, far below
        _cycle(ent)
        assert _sent(ent) == [25.0]
        assert ent.extra_state_attributes["regulation_mode"] == "heat"
        room, trv = 18.5, 9.0
        for _ in range(80):                          # rising 2 °C/h: no more commands
            clock.tick(60)
            room += 2.0 / 60
            trv += 0.25
            _feed(ent, room, trv, setpoint=25.0)
            _cycle(ent)
            if ent._reg_state.mode != "heat":
                break
        assert ent._reg_state.mode == "coast"
        assert _sent(ent) == [25.0, 5.0]             # stopped straight away
        assert room < 20.5

    def test_holding_is_fine_adjustments_cut_by_a_firm_stop(self, monkeypatch):
        """Near the target the valve gets setpoints in between, not 5 or 25.
        A room warming towards an overshoot gets one firm stop before it
        reaches the target; once the rise is over, fine adjustments again."""
        clock = _Clock(monkeypatch)
        ent = _make_entity()
        _run(_start(ent))
        room, trv = 20.3, 18.0                       # comfort 20.5
        _feed(ent, room, trv)
        _cycle(ent)
        assert ent.extra_state_attributes["regulation_mode"] == "hold"
        assert _sent(ent) == [pytest.approx(20.5 + (18.0 - 20.3) + 4 * 0.2)]
        for _ in range(30):                          # warming 1.2 °C/h
            clock.tick(60)
            room += 0.02
            trv += 0.05
            _feed(ent, room, trv, setpoint=_sent(ent)[-1])
            _cycle(ent)
            if ent._reg_state.mode == "stop":
                break
        assert ent._reg_state.mode == "stop"
        assert room < 20.5                           # ahead of the target
        assert _sent(ent)[-1] == 5.0
        assert all(5.0 < t < 25.0 for t in _sent(ent)[:-1])
        stopped = len(_sent(ent))
        for i in range(60):                          # levels off, then cools
            clock.tick(60)
            room += 0.005 if i < 10 else -0.005
            trv -= 0.05
            _feed(ent, room, trv, setpoint=_sent(ent)[-1])
            _cycle(ent)
            if len(_sent(ent)) > stopped:
                break
        assert ent._reg_state.mode == "hold"
        assert 5.0 < _sent(ent)[-1] < 25.0

    def test_starting_a_heat_up_does_not_wait_for_the_rate_limit(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _make_entity()
        _run(_start(ent))
        _feed(ent, 20.7, 19.0)
        _cycle(ent)                                  # over: a firm stop
        assert ent._reg_state.mode == "stop"
        clock.tick(60)
        _feed(ent, 19.2, 12.0, setpoint=5.0)         # e.g. a window shut
        _cycle(ent)
        assert ent._last_reason == "sent(stepped)"
        assert _sent(ent)[-1] == 25.0

    def test_option_off_keeps_the_pi_command(self, monkeypatch):
        _Clock(monkeypatch)
        ent = _make_entity({"stepped_heating_enabled": False})
        _run(_start(ent))
        _feed(ent, 18.5, 9.0, setpoint=8.0)
        _cycle(ent)
        assert ent.extra_state_attributes["regulation_mode"] == "hold"
        assert _sent(ent) and _sent(ent)[0] < 25.0

    def test_reload_mid_heat_up_carries_on(self, monkeypatch):
        """Changing a preset temperature reloads the room: a heat-up 0.8 °C
        short of the target carries on rather than falling back to the PI."""
        _Clock(monkeypatch)
        ent = _make_entity()
        _feed(ent, 19.7, 15.0, setpoint=25.0)
        _run(_start(ent))
        assert ent._reg_state.mode == "heat"
        _cycle(ent)
        assert ent._reg_state.mode == "heat"
        assert _sent(ent) == []                      # the valve already holds it

    def test_heat_up_end_on_sensor_loss_is_retried_until_taken(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _make_entity()
        _run(_start(ent))
        _feed(ent, 18.5, 9.0, setpoint=8.0)
        _cycle(ent)
        assert ent._reg_state.mode == "heat"

        async def refuse(*_a, **_k):
            raise TimeoutError

        ent.hass.services.async_call = refuse
        for _ in range(8):                           # sensor gone past the grace
            clock.tick(60)
            _feed(ent, None, 12.0, setpoint=25.0)
            _cycle(ent)
        assert ent._reg_state.mode == "heat"         # stop not taken yet
        assert ent._last_reason.startswith(("send_failed", "rate_limited"))

        ent.hass.services.async_call = ent.hass._async_call
        for _ in range(4):
            clock.tick(60)
            _feed(ent, None, 12.0, setpoint=25.0)
            _cycle(ent)
        assert ent._reg_state.mode == "coast"
        assert _sent(ent)[-1] == 5.0
