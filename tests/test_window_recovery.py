"""After a window closes the room re-warms on its own (tests/ha_harness.py).

18 Oct 2026, four rooms: the air came back 60-90 % of the way within 10-20
min of the close (study +1.9 °C in 9 min), decelerating over 15-40 min.  At
the end of a 5-min close delay the controller acted on a slope that still
carried the dip (a hold at +2.2 above the valve's reading, full power for
3 min, on a room rising 2.5 °C/h by itself) and the auto-tuner learned a
heating rate of 3.7 °C/h from the recovery.  So:

* the slope history is dropped while the window is open and refilled from
  the close on;
* the close delay is a minimum: heating waits until the fresh slope shows
  the room rising below WINDOW_RECOVERY_RATE_C_PER_H, at most
  WINDOW_RECOVERY_MAX_S after the close (0: resume at once, as before);
* the auto-tuner starts no heat-up episode after a window.
"""
from __future__ import annotations

import pytest

from tests.ha_harness import (
    WINDOW,
    _active_timers,
    _climate,
    _fire_last_timer,
    _make_entity,
    _run,
    _start,
    e2e,
    reset_timers,
)

pytestmark = e2e

HVACMode = _climate.HVACMode if _climate is not None else None
CLOSE_DELAY = 300.0


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


def _feed(ent, room, internal=12.0, setpoint=5.0):
    ent.coordinator.data = {
        "room_temp": room, "room_temp_ts": None,
        "tado_internal_temp": internal, "tado_setpoint": setpoint,
        "tado_min_temp": 5.0, "tado_max_temp": 25.0, "tado_off": False,
        "tado_cloud": True, "tado_scheduled": False, "tado_heating": None,
        "fallback_internal_temp": None, "fallback_setpoint": None,
    }


def _cycle(ent):
    _run(ent._async_regulation_cycle("timer"))


def _temps(ent):
    return [d["temperature"] for dom, svc, d in ent.hass.service_calls
            if dom == "climate" and svc == "set_temperature"]


def _event(state):
    import types
    return types.SimpleNamespace(data={"new_state": _State(state)})


class _State:
    def __init__(self, state):
        self.state = state
        self.attributes = {}


def _entity(clock, options=None):
    ent = _make_entity({"window_close_delay_s": CLOSE_DELAY, "autotune_enabled": True,
                        **(options or {})})
    _run(_start(ent))
    # Comfort 20.5, the room at the target for 15 min: a known, flat slope.
    for _ in range(15):
        clock.tick()
        _feed(ent, 20.5, 12.0, 5.0)
        _cycle(ent)
    assert ent._slope.slope_c_per_s() is not None
    return ent


def _open_window(ent, clock, *, minutes=6, fall_to=18.0):
    ent.hass.set(WINDOW, "on")
    ent._async_window_changed(_event("on"))
    _fire_last_timer()                      # the 30 s open delay: frost protection
    assert ent.preset_mode == "frost_protection"
    start = 20.5
    for k in range(1, minutes + 1):
        clock.tick()
        _feed(ent, start + (fall_to - start) * k / minutes, 12.0, 5.0)
        _cycle(ent)


def _close_window(ent):
    ent.hass.set(WINDOW, "off")
    ent._async_window_changed(_event("off"))
    assert ent._window_ctrl.close_delay_active


def _recover(ent, clock, room, rate_c_per_h, minutes):
    """The room re-warms by itself at *rate_c_per_h*; the valve stays shut."""
    for _ in range(minutes):
        clock.tick()
        room += rate_c_per_h / 60.0
        _feed(ent, room, 12.0, 5.0)
        _cycle(ent)
    return room


class TestSlopeAfterAWindow:
    def test_history_is_dropped_while_the_window_is_open(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock)
        _open_window(ent, clock)
        assert ent._slope.slope_c_per_s() is None
        assert len(ent._slope._buf) == 0

    def test_refilled_from_the_close_on(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock)
        _open_window(ent, clock)
        _close_window(ent)
        room = _recover(ent, clock, 18.0, 12.0, 5)
        assert len(ent._slope._buf) == 5            # only readings since the close
        assert ent._slope.slope_c_per_s() is None   # not enough of them yet
        _recover(ent, clock, room, 12.0, 4)
        slope = ent._slope.slope_c_per_s()
        assert slope is not None and slope * 3600 == pytest.approx(12.0, rel=0.05)


class TestCloseDelayWaitsForTheRecovery:
    def test_waits_then_resumes_when_the_rise_has_slowed(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock)
        _open_window(ent, clock)
        sent_before = len(_temps(ent))
        _close_window(ent)

        # The configured 5 min: the room is coming back fast (12 °C/h).
        room = _recover(ent, clock, 18.0, 12.0, 5)
        _fire_last_timer()
        assert ent.preset_mode == "frost_protection"
        assert ent._window_ctrl.close_delay_active
        assert ent.extra_state_attributes["window_close_delay_active"] is True
        assert [t["delay"] for t in _active_timers()] == [60.0]

        # Still 3 °C/h ten minutes later: keep waiting.
        room = _recover(ent, clock, room, 3.0, 5)
        _fire_last_timer()
        assert ent.preset_mode == "frost_protection"
        assert ent._window_ctrl.close_delay_active

        # The recovery slows to 0.5 °C/h: heating may resume.
        room = _recover(ent, clock, room, 0.5, 12)
        _fire_last_timer()
        assert ent.preset_mode == "comfort"
        assert not ent._window_ctrl.close_delay_active
        assert not ent._window_ctrl.is_active
        # Nothing was sent to the valve while the room recovered by itself.
        assert len(_temps(ent)) == sent_before

    def test_gives_up_after_30_min(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock)
        _open_window(ent, clock)
        _close_window(ent)
        closed_at = clock.now
        room = 18.0
        fired = 0
        while ent._window_ctrl.close_delay_active:
            room = _recover(ent, clock, room, 6.0, 5)   # never slows down
            _fire_last_timer()
            fired += 1
            assert fired < 20, "the wait must end"
        assert ent.preset_mode == "comfort"
        assert fired == 6
        assert clock.now - closed_at == pytest.approx(1800.0)

    def test_zero_delay_resumes_at_once(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, {"window_close_delay_s": 0})
        _open_window(ent, clock)
        ent.hass.set(WINDOW, "off")
        ent._async_window_changed(_event("off"))
        assert ent.preset_mode == "comfort"
        assert not ent._window_ctrl.close_delay_active

    def test_no_room_sensor_no_wait(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _make_entity({"window_close_delay_s": CLOSE_DELAY})
        _run(_start(ent))                       # the coordinator has no room reading
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        _fire_last_timer()
        _close_window(ent)
        clock.tick(CLOSE_DELAY)
        _fire_last_timer()
        assert ent.preset_mode == "comfort"
        assert not ent._window_ctrl.close_delay_active

    def test_reopening_during_the_wait_cancels_it(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock)
        _open_window(ent, clock)
        _close_window(ent)
        room = _recover(ent, clock, 18.0, 12.0, 5)
        _fire_last_timer()
        assert ent._window_ctrl.close_delay_active
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        assert not ent._window_ctrl.close_delay_active
        assert ent._window_ctrl.is_active
        assert ent.preset_mode == "frost_protection"
        # The slope history goes again while the window is open.
        clock.tick()
        _feed(ent, room - 0.3, 12.0, 5.0)
        _cycle(ent)
        assert len(ent._slope._buf) == 0


class TestTunerAfterAWindow:
    def test_no_heat_up_episode_from_the_recovery(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock)
        _open_window(ent, clock)
        _close_window(ent)
        room = _recover(ent, clock, 18.0, 12.0, 5)
        _fire_last_timer()
        room = _recover(ent, clock, room, 0.5, 12)
        _fire_last_timer()
        assert ent.preset_mode == "comfort"
        # Comfort again, the room 1.3 °C short: a heat-up starts, but the
        # tuner does not learn from it.
        clock.tick()
        _feed(ent, room, 12.0, 5.0)
        _cycle(ent)
        assert ent._reg_state.mode == "heat"
        assert ent._autotuner._heatup is None
        assert ent._autotuner.last_event == "heat-up after a window: not analysed"
