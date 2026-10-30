"""The valve look-ahead through the real climate entity (tests/ha_harness.py):
off by default; with it on, a release (a hold command to a shut valve) is
watched and the valve's first heating power report teaches the learner; a
learned look-ahead lowers the hold command on a falling valve reading; the
auto-tuner sees the push the valve answers; persistence, reset, a missing
heating sensor and a failure that must not stop the regulation."""
from __future__ import annotations

import logging
import sys

import pytest

from tests.ha_harness import (
    TRV,
    _climate,
    _Extra,
    _make_entity,
    _restart_from,
    _run,
    _start,
    e2e,
    reset_timers,
)

pytestmark = e2e

POWER = "sensor.test_zone_heating"
ON = {"valve_lookahead": True, "heating_power_entity_id": POWER}


@pytest.fixture(autouse=True)
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


def _feed(ent, room, internal, *, off=True, setpoint=None, power=None, power_ts=None,
          min_c=5.0):
    ent.coordinator.data = {
        "room_temp": room, "room_temp_ts": None,
        "tado_internal_temp": internal, "tado_setpoint": setpoint,
        "tado_min_temp": min_c, "tado_max_temp": 25.0, "tado_off": off,
        "tado_cloud": True, "tado_scheduled": False, "tado_heating": None,
        "fallback_internal_temp": None, "fallback_setpoint": None,
        "heating_power": power, "heating_power_ts": power_ts,
    }


def _cycle(ent):
    _run(ent._async_regulation_cycle("timer"))


def _heat_commands(ent):
    return [d["temperature"] for dom, svc, d in ent.hass.service_calls
            if dom == "climate" and svc == "set_temperature"]


def _entity(options=None, extra=None):
    ent = _make_entity({**(options or {})}, window=False)
    _run(_start(ent, extra=extra))
    return ent


def _slide(ent, clock, *, minutes=10, room=20.6, start=19.5, rate_c_per_h=3.0):
    """The valve shut and off, the room above its target (20.5): its reading
    slides as the radiator cools.  Returns the reading reached."""
    internal = start
    for _ in range(minutes):
        _feed(ent, room, round(internal, 1))
        _cycle(ent)
        clock.tick()
        internal -= rate_c_per_h / 60.0
    return round(internal, 1)


def _release(ent, clock, internal, *, room=20.35, min_c=5.0):
    """The room has fallen 0.15 below: the stop releases into hold."""
    _feed(ent, room, internal, min_c=min_c)
    _cycle(ent)
    return ent._last_result


def _learned(lookahead_s=780.0):
    """A saved learner state for this room's TRV."""
    return {"lookahead": {"lookahead_s": lookahead_s, "releases": [], "pending": None, "trv": TRV}}


def test_off_by_default():
    ent = _entity()
    assert not ent.lookahead_enabled
    assert ent.valve_lookahead_min is None
    assert "valve_lookahead_min" not in ent.extra_state_attributes
    assert ent._lookahead_inputs(0.0, ent._active_trv()) == (None, 0.0)


def test_a_release_is_watched_and_the_valves_answer_is_learned(monkeypatch, caplog):
    clock = _Clock(monkeypatch)
    ent = _entity(ON)
    internal = _slide(ent, clock)
    result = _release(ent, clock, internal)
    assert result.mode == "hold"
    assert ent._last_reason.startswith("sent(")
    assert ent._lookahead.observing, "a hold command to a shut valve is a release"
    clock.tick()
    # The cloud shows the setpoint (rounded) and, a poll later, the valve's answer.
    _feed(ent, 20.35, internal, off=False, setpoint=19.5, power=0.0, power_ts=clock.now - 30)
    _cycle(ent)
    assert ent._lookahead.observing
    clock.tick(120)
    with caplog.at_level(logging.INFO):
        _feed(ent, 20.34, internal, off=False, setpoint=19.5, power=62.0, power_ts=clock.now)
        _cycle(ent)
    assert not ent._lookahead.observing
    (release,) = ent._lookahead.releases
    assert release.response_pct == 62.0
    assert release.push_c == pytest.approx(19.5 - internal)
    assert release.fall_c_per_h == pytest.approx(3.0, abs=0.8)
    assert any("valve look-ahead: the valve answered 62 %" in r.getMessage() for r in caplog.records)
    events = [e.text for e in ent._activity.entries() if not e.decision]
    assert any(t.startswith("Valve look-ahead: the valve answered 62 %") for t in events)
    summary = ent.valve_lookahead_summary()
    assert summary["releases"] == 1 and summary["lookahead_min"] == 0.0


def test_a_learned_lookahead_lowers_the_hold_command_on_a_falling_reading(monkeypatch, caplog):
    def released(options, extra=None):
        clock = _Clock(monkeypatch)
        ent = _entity(options, extra=extra)
        internal = _slide(ent, clock)
        return ent, _release(ent, clock, internal)

    stock, r0 = released({})
    with caplog.at_level(logging.DEBUG):
        la, r1 = released(ON, _Extra(_learned(780.0)))
    # Explained in the decision line, and in the debug line.
    assert "off for its reading falling" in la._activity.doing
    assert any(", look-ahead -" in r.getMessage() and "vs trv" in r.getMessage()
               for r in caplog.records)
    assert r0.mode == r1.mode == "hold"
    assert r0.lookahead_c == 0.0
    # 13 min x (3.0 - 0.3 °C/h deadband), from the valve's own reading slope.
    assert r1.lookahead_c == pytest.approx(13 / 60 * 2.7, abs=0.15)
    assert _heat_commands(la)[-1] == pytest.approx(_heat_commands(stock)[-1] - r1.lookahead_c,
                                                   abs=0.11)
    assert la.extra_state_attributes["valve_lookahead_c"] == r1.lookahead_c
    assert la.extra_state_attributes["valve_lookahead_min"] == 13.0
    # The tuner is told what was taken off the command the valve holds.
    assert la._held_lookahead_c == pytest.approx(r1.lookahead_c)


def test_the_tuner_sees_the_push_the_valve_answers(monkeypatch):
    clock = _Clock(monkeypatch)
    ent = _entity({**ON, "autotune_enabled": True}, extra=_Extra(_learned(780.0)))
    samples = []
    real = ent._autotuner.observe
    monkeypatch.setattr(ent._autotuner, "observe", lambda s: (samples.append(s), real(s))[1])
    internal = _slide(ent, clock)
    result = _release(ent, clock, internal)
    sample = samples[-1]
    assert sample.lookahead_c == pytest.approx(result.lookahead_c)
    assert sample.demand_c == pytest.approx(
        _heat_commands(ent)[-1] + result.lookahead_c - internal, abs=1e-6)


def test_an_off_before_the_answer_cancels_the_release(monkeypatch):
    clock = _Clock(monkeypatch)
    ent = _entity(ON)
    internal = _slide(ent, clock)
    _release(ent, clock, internal)
    assert ent._lookahead.observing
    clock.tick(200)
    # The room shoots up (the window shut, people): the stop sends the off.
    _feed(ent, 20.75, internal, off=False, setpoint=19.5)
    _cycle(ent)
    assert ent._last_result.mode == "stop"
    assert not ent._lookahead.observing


def test_without_a_heating_sensor_it_warns_and_does_not_learn(monkeypatch, caplog):
    clock = _Clock(monkeypatch)
    with caplog.at_level(logging.WARNING):
        ent = _entity({"valve_lookahead": True})
    assert any("no heating power sensor" in r.getMessage() for r in caplog.records)
    internal = _slide(ent, clock)
    _release(ent, clock, internal)
    assert not ent._lookahead.observing


def test_persists_with_its_trv_and_another_valve_starts_afresh(monkeypatch):
    ent = _entity(ON, extra=_Extra(_learned(600.0)))
    assert ent.valve_lookahead_min == 10.0
    data = ent.extra_restore_state_data.as_dict()
    assert data["lookahead"]["trv"] == TRV and data["lookahead"]["lookahead_s"] == 600.0
    again = _make_entity(dict(ON), window=False)
    _restart_from(ent, again)
    assert again.valve_lookahead_min == 10.0
    other = _make_entity(dict(ON), window=False)
    other._config_entry.data = {"source_entity_id": "climate.another_valve"}
    _run(_start(other, extra=_Extra(data)))
    assert other.valve_lookahead_min == 0.0


def test_the_autotune_reset_forgets_it_too():
    ent = _entity(ON, extra=_Extra(_learned(600.0)))
    _run(ent.async_reset_autotune())
    assert ent.valve_lookahead_min == 0.0


def test_a_failure_switches_it_off_and_the_room_is_still_regulated(monkeypatch, caplog):
    clock = _Clock(monkeypatch)
    ent = _entity(ON, extra=_Extra(_learned(780.0)))

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ent._trv_slope, "slope_c_per_s", boom)
    with caplog.at_level(logging.ERROR):
        internal = _slide(ent, clock)
        result = _release(ent, clock, internal)
    assert result.mode == "hold" and result.lookahead_c == 0.0
    assert ent._last_reason.startswith("sent(")
    assert not ent.lookahead_active and ent.lookahead_enabled
    assert "boom" in ent.valve_lookahead_summary()["error"]
    assert sum("valve look-ahead failed" in r.getMessage() for r in caplog.records) == 1


def test_the_option_keys_are_the_ones_the_helpers_use():
    """climate_controllers spells the option keys out (it stays HA-free)."""
    const = sys.modules[_climate.__package__ + ".const"]
    controllers = sys.modules[_climate.__package__ + ".climate_controllers"]
    flat = {const.CONF_VALVE_LOOKAHEAD: True}
    assert controllers.options_form_errors(flat)
    flat[const.CONF_HEATING_POWER_ENTITY_ID] = POWER
    assert controllers.options_form_errors(flat) == {}


def test_no_release_while_a_heat_command_of_ours_may_be_on_its_way(monkeypatch):
    """Review finding (2.14.0): a cloud zone shows "off" for a minute after a
    25 °C; a hold command then is no release (the valve is heating)."""
    clock = _Clock(monkeypatch)
    ent = _entity(ON)
    internal = _slide(ent, clock)
    # A heat-up: the room 1.5 below (a fall that fast is believed on its
    # second reading), 25 °C goes out; the state still says off.
    for _ in range(2):
        _feed(ent, 19.0, internal)
        _cycle(ent)
        clock.tick(30)
    assert _heat_commands(ent)[-1] == 25.0
    trv = ent._active_trv()
    assert trv.off
    assert not ent._lookahead_valve_shut(clock.now, trv)
    # Once the drift grace (6 min on a cloud TRV) has passed, "off" is off.
    assert ent._lookahead_valve_shut(clock.now + 400, trv)


def test_a_reload_keeps_the_slope_and_the_held_lookahead(monkeypatch):
    """Review finding (2.14.0): every preset temperature change reloads the
    room; the hold on a sliding reading stays lowered, and the tuner keeps
    the look-ahead taken off the command the valve holds."""
    clock = _Clock(monkeypatch)
    ent = _entity(ON, extra=_Extra(_learned(780.0)))
    internal = _slide(ent, clock)
    result = _release(ent, clock, internal)
    assert result.lookahead_c > 0
    held = ent._held_lookahead_c
    assert held == pytest.approx(result.lookahead_c)
    again = _make_entity(dict(ON), window=False)
    _restart_from(ent, again)
    assert again._held_lookahead_c == pytest.approx(held)
    assert again._trv_slope.slope_c_per_s() is not None
    clock.tick()
    _feed(again, 20.35, round(internal - 0.05, 1), off=False, setpoint=_heat_commands(ent)[-1])
    _cycle(again)
    assert again._last_result.mode == "hold"
    assert again._last_result.lookahead_c > 0


def test_the_off_baseline_holds_no_lookahead(monkeypatch):
    clock = _Clock(monkeypatch)
    ent = _entity(ON, extra=_Extra(_learned(780.0)))
    internal = _slide(ent, clock)
    _release(ent, clock, internal)
    assert ent._held_lookahead_c > 0
    ent._note_trv_holds_off()
    assert ent._held_lookahead_c == 0.0


def test_a_command_raised_to_what_the_valve_accepts_holds_less_lookahead(monkeypatch):
    """The valve's minimum lifts a lowered hold command: the tuner is told
    only what was actually taken off."""
    def released(options, extra=None, min_c=5.0):
        clock = _Clock(monkeypatch)
        ent = _entity(options, extra=extra)
        internal = _slide(ent, clock)
        return ent, _release(ent, clock, internal, min_c=min_c)

    stock, _ = released({})
    stock_c = _heat_commands(stock)[-1]
    _, r = released(ON, _Extra(_learned(780.0)))
    assert r.lookahead_c > 0.4
    half = r.lookahead_c / 2.0
    ent, r = released(ON, _Extra(_learned(780.0)), min_c=round(stock_c - half, 1))
    assert _heat_commands(ent)[-1] == pytest.approx(round(stock_c - half, 1), abs=0.01)
    assert ent._held_lookahead_c == pytest.approx(half, abs=0.11)
