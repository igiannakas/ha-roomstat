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
* the auto-tuner starts no heat-up episode after a window;
* only a change by hand ends the wait; Resume schedule and the schedule are
  saved for its end (2.19.2).
"""
from __future__ import annotations

import pytest

from tests.ha_harness import (
    PRESENCE,
    SCHEDULE,
    WINDOW,
    _active_timers,
    _climate,
    _fire_last_timer,
    _make_entity,
    _run,
    _settle,
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


def _entity(clock, options=None, *, schedule=None, presence=False):
    ent = _make_entity({"window_close_delay_s": CLOSE_DELAY, "autotune_enabled": True,
                        **(options or {})}, schedule=schedule is not None,
                       presence=presence)
    if schedule is not None:
        ent.hass.set(SCHEDULE, schedule)
    if presence:
        ent.hass.set(PRESENCE, "on")
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

    def test_the_card_and_the_log_say_how_long_heating_waits(self, monkeypatch):
        """Owner, 2 Nov 2026: "Window open" while the close delay ran gave
        no idea of the wait.  The thermostat now says when the window
        closed, the earliest and latest time heating can come back, and the
        room's own re-warm rate; the activity line says the same."""
        import datetime

        def ts(attr):
            return datetime.datetime.fromisoformat(attr).timestamp()

        clock = _Clock(monkeypatch)
        ent = _entity(clock)
        attrs = ent.extra_state_attributes
        assert attrs["window_closed_at"] is None and attrs["window_heat_latest"] is None
        _open_window(ent, clock)
        assert ent.extra_state_attributes["window_heat_earliest"] is None   # still open
        _close_window(ent)
        closed = clock.now
        attrs = ent.extra_state_attributes
        assert ts(attrs["window_closed_at"]) == pytest.approx(closed)
        # The configured 5 min, but the fresh slope needs 7.5 min; at most 30.
        assert ts(attrs["window_heat_earliest"]) == pytest.approx(closed + 450.0)
        assert ts(attrs["window_heat_latest"]) == pytest.approx(closed + 1800.0)
        assert attrs["window_rewarm_c_per_h"] is None
        room = _recover(ent, clock, 18.0, 12.0, 3)
        assert "heats again in 5–27 min" in ent._activity.doing
        # Re-warming fast once the slope is known: up to the cap.
        room = _recover(ent, clock, room, 12.0, 7)
        _fire_last_timer()
        attrs = ent.extra_state_attributes
        assert attrs["window_rewarm_c_per_h"] == pytest.approx(12.0, abs=0.6)
        assert ts(attrs["window_heat_latest"]) == pytest.approx(closed + 1800.0)
        assert "at most 20 min" in ent._activity.doing
        # Slowed below 1 °C/h: due at the next check.
        room = _recover(ent, clock, room, 0.3, 15)
        attrs = ent.extra_state_attributes
        if ent._window_ctrl.close_delay_active:
            assert attrs["window_heat_latest"] == attrs["window_heat_earliest"]
        _fire_last_timer()
        assert not ent._window_ctrl.close_delay_active
        attrs = ent.extra_state_attributes
        assert attrs["window_closed_at"] is None and attrs["window_rewarm_c_per_h"] is None
        assert "window_rewarm_c_per_h" in ent._unrecorded_attributes

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


def _fire(name):
    """Run the pending timer whose callback is *name*."""
    (entry,) = [t for t in _active_timers()
                if getattr(t["callback"], "__name__", "") == name]
    entry["cancelled"] = True
    _run(entry["callback"](None))


def _in_the_wait(ent, clock):
    """Window open, then shut; 5 min later the room still re-warms fast,
    so the wait runs on.  Returns the room's reading."""
    _open_window(ent, clock)
    _close_window(ent)
    room = _recover(ent, clock, 18.0, 12.0, 5)
    _fire("_async_window_close_action")
    assert ent._window_ctrl.close_delay_active
    assert ent.preset_mode == "frost_protection"
    return room


def _wait_ends(ent, clock, room):
    """The re-warm slows below 1 °C/h: the wait's next poll ends it."""
    room = _recover(ent, clock, room, 0.5, 12)
    _fire("_async_window_close_action")
    assert not ent._window_ctrl.close_delay_active
    assert not ent._window_ctrl.is_active
    return room


class TestOnlyAChangeByHandEndsTheWait:
    """3 Nov 2026, living room: the window shut at 07:50, and at 08:09 the
    room was still re-warming on its own (20.5, rising 1.8 °C/h, 0.3 under
    Comfort 20.8).  Resume schedule then ended the wait like a change by
    hand, and the hold sent 18.9, 1.2 over the valve's reading: 93 % until
    Frost protection was picked 24 s later.  Resume schedule asks for no heat
    now; it is saved for the wait's end, as while the window is open."""

    def test_resume_schedule_keeps_the_wait(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="comfort")
        room = _in_the_wait(ent, clock)
        sent_before = len(_temps(ent))
        _run(ent.async_resume_schedule())
        assert ent._window_ctrl.close_delay_active
        assert ent.preset_mode == "frost_protection"
        assert ent._window_ctrl.get_saved().preset == "comfort"
        assert any("re-warming on its own: Comfort saved for when heating resumes"
                   in e.text for e in ent._activity.entries())
        # Still re-warming: no heat sent, and the wait carries on.
        room = _recover(ent, clock, room, 3.0, 5)
        _fire("_async_window_close_action")
        assert ent._window_ctrl.close_delay_active
        assert len(_temps(ent)) == sent_before
        _wait_ends(ent, clock, room)
        assert ent.preset_mode == "comfort"
        assert len(_temps(ent)) == sent_before

    def test_resume_schedule_ends_an_override_for_the_wait_s_end(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="comfort")
        _run(ent.async_set_preset_mode("eco"))
        assert ent.extra_state_attributes["schedule_override_active"] is True
        room = _in_the_wait(ent, clock)
        assert ent._window_ctrl.get_saved().preset == "eco"
        _run(ent.async_resume_schedule())
        assert ent.extra_state_attributes["schedule_override_active"] is False
        assert ent._window_ctrl.close_delay_active
        assert ent._window_ctrl.get_saved().preset == "comfort"
        _wait_ends(ent, clock, room)
        assert ent.preset_mode == "comfort"

    def test_the_schedule_moving_on_keeps_the_wait(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="comfort")
        room = _in_the_wait(ent, clock)
        ent.hass.keep_tasks = True
        ent.hass.set(SCHEDULE, "night")
        ent._async_schedule_changed(_event("night"))
        while ent.hass.tasks:
            _run(ent.hass.tasks.pop(0))
        ent.hass.keep_tasks = False
        assert ent._window_ctrl.close_delay_active
        assert ent.preset_mode == "frost_protection"
        assert ent._window_ctrl.get_saved().preset == "eco"
        _wait_ends(ent, clock, room)
        assert ent.preset_mode == "eco"

    def test_a_preset_picked_by_hand_still_ends_it(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="comfort")
        _in_the_wait(ent, clock)
        _run(ent.async_set_preset_mode("comfort"))
        assert not ent._window_ctrl.close_delay_active
        assert not ent._window_ctrl.is_active
        assert ent.preset_mode == "comfort"
        assert [t for t in _active_timers()
                if getattr(t["callback"], "__name__", "") == "_async_window_close_action"] == []


def _offs(ent):
    return [d for dom, svc, d in ent.hass.service_calls
            if dom == "climate" and svc == "set_hvac_mode" and d.get("hvac_mode") == "off"]


def _close_timers():
    return [t for t in _active_timers()
            if getattr(t["callback"], "__name__", "") == "_async_window_close_action"]


class TestResumeScheduleGoesBackToTheWait:
    """3 Nov 2026, living room, 08:24: the window shut at about 08:17, and
    Comfort picked by hand ended the wait; Night, then Resume schedule gave
    Comfort with no wait (a 0.6 °C push due once the press had settled,
    Frost picked a second later).  The owner: "If I manually override and
    then press resume schedule it should resume with the open window".  So
    a change by hand that ends the wait keeps when the window closed, and
    Resume schedule goes back to the wait while it would still run."""

    def test_after_a_change_by_hand_it_waits_again(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="comfort")
        room = _in_the_wait(ent, clock)
        closed = clock.now - 300.0
        # Comfort by hand ends the wait; once settled its heat-up goes out.
        _run(ent.async_set_preset_mode("comfort"))
        assert not ent._window_ctrl.close_delay_active
        _settle(clock)
        assert _temps(ent)[-1] == 25.0
        offs = len(_offs(ent))
        _run(ent.async_set_preset_mode("eco"))
        clock.tick(20.0)
        _run(ent.async_resume_schedule())
        win = ent._window_ctrl
        assert win.is_active and win.close_delay_active
        assert win.close_since == pytest.approx(closed)      # still from the close
        assert ent.preset_mode == "frost_protection"
        assert win.get_saved().preset == "comfort"
        assert ent.extra_state_attributes["schedule_override_active"] is False
        assert [t["delay"] for t in _close_timers()] == [60.0]
        assert len(_offs(ent)) > offs                        # the heat taken back at once
        assert any("heating waits again" in e.text for e in ent._activity.entries())
        # The wait then runs as after any close.
        heat = len(_temps(ent))
        room = _recover(ent, clock, room, 3.0, 5)
        _fire("_async_window_close_action")
        assert win.close_delay_active and len(_temps(ent)) == heat
        _wait_ends(ent, clock, room)
        assert ent.preset_mode == "comfort"

    def test_within_the_close_delay_the_rest_of_it(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="comfort")
        _open_window(ent, clock)
        _close_window(ent)
        _recover(ent, clock, 18.0, 12.0, 2)
        _run(ent.async_set_temperature(temperature=21.0))
        assert not ent._window_ctrl.close_delay_active
        clock.tick(60.0)                                     # 3 min after the close
        _run(ent.async_resume_schedule())
        assert ent._window_ctrl.close_delay_active
        assert [t["delay"] for t in _close_timers()] == [pytest.approx(120.0)]
        assert ent.preset_mode == "frost_protection"
        assert ent._window_ctrl.get_saved().preset == "comfort"

    def test_a_boost_is_ended_too(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="comfort")
        _in_the_wait(ent, clock)
        _run(ent.async_set_preset_mode("boost"))
        assert ent.preset_mode == "boost"
        _run(ent.async_resume_schedule())
        assert ent._boost_cancel is None
        assert [t for t in _active_timers()
                if getattr(t["callback"], "__name__", "") == "_async_boost_expired"] == []
        assert ent.preset_mode == "frost_protection"
        assert ent._window_ctrl.get_saved().preset == "comfort"

    def test_not_once_the_re_warm_has_slowed(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="comfort")
        room = _in_the_wait(ent, clock)
        _run(ent.async_set_preset_mode("eco"))
        _recover(ent, clock, room, 0.3, 12)
        _run(ent.async_resume_schedule())
        assert not ent._window_ctrl.is_active
        assert ent.preset_mode == "comfort"
        assert ent._window_ctrl.interrupted_close is None

    def test_not_30_min_after_the_close(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="comfort")
        room = _in_the_wait(ent, clock)
        _run(ent.async_set_preset_mode("eco"))
        _recover(ent, clock, room, 6.0, 25)                  # still rising fast
        _run(ent.async_resume_schedule())
        assert not ent._window_ctrl.is_active
        assert ent.preset_mode == "comfort"

    def test_not_after_off_then_heat(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="comfort")
        _in_the_wait(ent, clock)
        _run(ent.async_set_preset_mode("eco"))
        _run(ent.async_set_hvac_mode(HVACMode.OFF))
        _run(ent.async_set_hvac_mode(HVACMode.HEAT))
        _run(ent.async_resume_schedule())
        assert not ent._window_ctrl.is_active
        assert ent.preset_mode == "comfort"

    def test_not_after_the_wait_ended_by_itself(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="comfort")
        room = _in_the_wait(ent, clock)
        _wait_ends(ent, clock, room)
        _run(ent.async_set_preset_mode("eco"))
        _run(ent.async_resume_schedule())
        assert not ent._window_ctrl.is_active
        assert ent.preset_mode == "comfort"

    def test_while_nobody_is_home_back_to_away_after_the_wait(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, {"presence_away_delay_s": 60}, schedule="comfort",
                      presence=True)
        ent.hass.set(PRESENCE, "off")
        ent._async_presence_changed(_event("off"))
        _fire("_async_presence_away_action")
        assert ent.preset_mode == "away"
        room = _in_the_wait(ent, clock)
        _run(ent.async_set_preset_mode("comfort"))          # by hand while out
        assert ent.preset_mode == "comfort"
        assert ent.extra_state_attributes["presence_override_active"] is True
        _run(ent.async_resume_schedule())
        assert ent.extra_state_attributes["presence_override_active"] is False
        assert ent._window_ctrl.close_delay_active
        assert ent.preset_mode == "frost_protection"
        assert ent._window_ctrl.get_saved().preset == "away"
        _wait_ends(ent, clock, room)
        assert ent.preset_mode == "away"


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
