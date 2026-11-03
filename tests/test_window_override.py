"""A change by hand overrides an open window (2.22.0).

3 Nov 2026, study: the window sensor read open at 09:32 and the room went to
frost protection; Day was pressed twice at 09:37, and both presses were only
saved for the close ("Window open, comfort saved for the close, keeping frost
protection").  The owner: "open window could not be overriden", and asked
for the same rules as after the sensor reports the window shut (the close
delay, 2.19.2 / 2.19.3):

* a change by hand (a preset or Boost picked, a temperature set; every
  service call, the heating scripts' too) ends the window's hold on the
  room: its frost protection, or its open delay still due.  The room is
  regulated as if the window were shut until the sensor reports it shut;
* the schedule, Resume schedule and an override running out ask for no heat
  now and do not override it;
* Resume schedule after an override goes back to the frost protection while
  the window is still open;
* the override lasts until the sensor reports the window shut (a repeated
  "on", on -> unavailable -> on, arms nothing), also across a reload; the
  next opening acts again, and so does switching Off, then Heat.
"""
from __future__ import annotations

import types

import pytest

from tests.ha_harness import (
    SCHEDULE,
    WINDOW,
    _active_timers,
    _climate,
    _Extra,
    _make_entity,
    _run,
    _settle,
    _start,
    _State,
    e2e,
    reset_timers,
)
from tests.pure_modules import activity as act
from tests.pure_modules import parameters as P

HVACMode = _climate.HVACMode if _climate is not None else None
OPEN_DELAY = 30.0
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


def _feed(ent, room, internal=14.0, *, off=True):
    ent.coordinator.data = {
        "room_temp": room, "room_temp_ts": None,
        "tado_internal_temp": internal, "tado_setpoint": None if off else 5.0,
        "tado_min_temp": 5.0, "tado_max_temp": 25.0, "tado_off": off,
        "tado_cloud": True, "tado_scheduled": False, "tado_heating": False,
        "fallback_internal_temp": None, "fallback_setpoint": None,
    }


def _cycle(ent):
    _run(ent._async_regulation_cycle("timer"))


def _event(state):
    return types.SimpleNamespace(data={"new_state": types.SimpleNamespace(
        state=state, attributes={})})


def _window(ent, state):
    ent.hass.set(WINDOW, state)
    ent._async_window_changed(_event(state))


def _timers(name):
    return [t for t in _active_timers() if getattr(t["callback"], "__name__", "") == name]


def _fire(name):
    (entry,) = _timers(name)
    entry["cancelled"] = True
    _run(entry["callback"](None))


def _heat(ent):
    return [d["temperature"] for dom, svc, d in ent.hass.service_calls
            if dom == "climate" and svc == "set_temperature" and d["temperature"] > 5.05]


def _offs(ent):
    return [d for dom, svc, d in ent.hass.service_calls
            if dom == "climate" and svc == "set_hvac_mode" and d.get("hvac_mode") == "off"]


def _texts(ent):
    return [e.text for e in reversed(ent._activity.entries())]     # oldest first


def _entity(clock, *, schedule=None, options=None):
    ent = _make_entity({"window_delay_s": OPEN_DELAY, "window_close_delay_s": CLOSE_DELAY,
                        "heat_timer_s": 0, **(options or {})},
                       schedule=schedule is not None)
    if schedule is not None:
        ent.hass.set(SCHEDULE, schedule)
    ent.hass.set(WINDOW, "off")
    _run(_start(ent))
    _feed(ent, 19.9)
    _cycle(ent)
    ent.hass.service_calls.clear()
    return ent


def _open(ent, clock, room=18.5):
    """The window opens and its open delay runs out: frost protection."""
    _window(ent, "on")
    clock.tick(OPEN_DELAY)
    _fire("_async_window_action")
    assert ent.preset_mode == "frost_protection" and ent._window_ctrl.is_active
    clock.tick()
    _feed(ent, room)
    _cycle(ent)


@e2e
class TestAChangeByHandOverridesTheOpenWindow:
    def test_the_study_on_3_nov(self, monkeypatch):
        """Day pressed with the window open heats now, once the press has
        settled, and the log says the window is overridden."""
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="comfort")
        _open(ent, clock, room=19.2)
        clock.tick(5 * 60)
        _feed(ent, 16.8)
        _cycle(ent)
        assert _heat(ent) == []
        _run(ent.async_set_preset_mode("comfort"))
        _run(ent.async_set_preset_mode("comfort"))           # pressed twice
        win = ent._window_ctrl
        assert ent.preset_mode == "comfort"
        assert win.overridden and not win.is_active
        assert ent.extra_state_attributes["window_overridden"] is True
        assert ent.extra_state_attributes["window_open_active"] is False
        _settle(clock)
        assert _heat(ent) == [25.0]                          # 3.2 short: a heat-up
        texts = _texts(ent)
        assert any("Window open, overridden by hand (preset changed to Comfort)" in t
                   for t in texts)
        assert texts[-1].startswith("Window open, overridden by hand: Full heat")
        assert not any("saved for the close" in t for t in texts)

    def test_a_temperature_set_by_hand(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock)
        _open(ent, clock)
        _run(ent.async_set_temperature(temperature=21.0))
        assert (ent.preset_mode, ent.target_temperature) == ("none", 21.0)
        assert ent._window_ctrl.overridden
        _settle(clock)
        assert _heat(ent) == [25.0]

    def test_boost(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock)
        _run(ent.async_set_preset_mode("eco"))
        _open(ent, clock)
        _run(ent.async_set_preset_mode("boost"))
        assert ent.preset_mode == "boost" and ent._boost_cancel is not None
        assert ent._window_ctrl.overridden

    def test_during_the_open_delay(self, monkeypatch):
        """Picked while the open delay still runs: the window's frost
        protection never comes, and the heat waits no longer for it."""
        clock = _Clock(monkeypatch)
        ent = _entity(clock)
        _run(ent.async_set_preset_mode("eco"))
        clock.tick(200)                                # past the rate limit
        _window(ent, "on")
        assert ent._window_ctrl.open_delay_active
        clock.tick(10)
        _run(ent.async_set_preset_mode("comfort"))
        assert _timers("_async_window_action") == []
        assert ent._window_ctrl.overridden and not ent._window_ctrl.open_delay_active
        _settle(clock)
        assert ent.preset_mode == "comfort" and len(_heat(ent)) == 1   # 0.6 short: a hold push

    def test_frost_by_hand_is_off_now_and_after(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock)
        _open(ent, clock)
        _run(ent.async_set_preset_mode("frost_protection"))
        assert ent._window_ctrl.overridden
        _window(ent, "off")
        assert not ent._window_ctrl.close_delay_active
        assert ent.preset_mode == "frost_protection" and _heat(ent) == []

    def test_the_schedule_and_resume_schedule_do_not_override_it(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="eco")
        _open(ent, clock)
        ent.hass.set(SCHEDULE, "comfort")
        ent._async_schedule_changed(_event("comfort"))
        _run(ent.async_resume_schedule())
        win = ent._window_ctrl
        assert win.is_active and not win.overridden
        assert ent.preset_mode == "frost_protection"
        assert win.get_saved().preset == "comfort"


@e2e
class TestWhileOverridden:
    def _overridden(self, clock, **kw):
        ent = _entity(clock, **kw)
        _open(ent, clock)
        _run(ent.async_set_preset_mode("comfort"))
        assert ent._window_ctrl.overridden
        return ent

    @pytest.mark.parametrize("states", [["on"], ["unavailable", "on"], ["unknown", "on"]])
    def test_the_same_opening_again_arms_nothing(self, monkeypatch, states):
        clock = _Clock(monkeypatch)
        ent = self._overridden(clock)
        for state in states:
            _window(ent, state)
        assert _timers("_async_window_action") == []
        assert ent.preset_mode == "comfort" and ent._window_ctrl.overridden

    def test_the_close_restores_nothing_and_the_next_opening_acts(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = self._overridden(clock)
        _window(ent, "off")
        win = ent._window_ctrl
        assert not win.overridden and not win.close_delay_active and not win.is_active
        assert ent.preset_mode == "comfort"
        assert "Window closed; it was overridden by hand, so nothing to restore" in _texts(ent)
        _open(ent, clock)
        assert win.get_saved().preset == "comfort"

    def test_the_schedule_s_override_running_out_applies_the_schedule(self, monkeypatch):
        """As after a close delay a change by hand ended: the room follows
        the schedule again, and the window stays overridden."""
        clock = _Clock(monkeypatch)
        ent = self._overridden(clock, schedule="eco", options={"schedule_override_min": 60})
        assert ent.extra_state_attributes["schedule_override_active"] is True
        clock.tick(3600)
        _fire("_async_schedule_override_expired")
        assert ent.preset_mode == "eco" and ent._window_ctrl.overridden
        assert not ent._window_ctrl.is_active

    def test_resume_schedule_goes_back_to_the_window(self, monkeypatch):
        """The owner, 3 Nov 2026 (the close delay, 2.19.3): "If I manually
        override and then press resume schedule it should resume with the
        open window".  The heat is taken back at once, and the close then
        runs its delay as after any opening."""
        clock = _Clock(monkeypatch)
        ent = self._overridden(clock, schedule="eco")
        _settle(clock)
        assert _heat(ent) == [25.0]
        _feed(ent, 18.6, 20.0, off=False)
        offs = len(_offs(ent))
        clock.tick(60)
        _run(ent.async_resume_schedule())
        win = ent._window_ctrl
        assert win.is_active and not win.overridden
        assert ent.preset_mode == "frost_protection"
        assert win.get_saved().preset == "eco"
        assert len(_offs(ent)) > offs
        assert any("Resume schedule: the window is still open" in t for t in _texts(ent))
        _window(ent, "off")
        assert win.close_delay_active

    def test_resume_schedule_ends_a_boost_for_the_window(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(clock, schedule="eco")
        _open(ent, clock)
        _run(ent.async_set_preset_mode("boost"))
        _run(ent.async_resume_schedule())
        assert ent._boost_cancel is None and ent._window_ctrl.is_active
        assert ent._window_ctrl.get_saved().preset == "eco"

    def test_off_then_heat_acts_on_the_open_window_again(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = self._overridden(clock)
        _run(ent.async_set_hvac_mode(HVACMode.OFF))
        assert not ent._window_ctrl.overridden
        _run(ent.async_set_hvac_mode(HVACMode.HEAT))
        assert ent._window_ctrl.open_delay_active
        clock.tick(OPEN_DELAY)
        _fire("_async_window_action")
        assert ent.preset_mode == "frost_protection"

    def test_heat_while_heating_keeps_the_override(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = self._overridden(clock)
        _run(ent.async_set_hvac_mode(HVACMode.HEAT))
        assert ent._window_ctrl.overridden and ent.preset_mode == "comfort"

    def test_the_tuner_learns_nothing_from_the_draught(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = self._overridden(clock, options={"autotune_enabled": True})
        assert not ent._autotune_eligible()
        _window(ent, "off")
        assert ent._autotune_eligible()

    def test_summer_mode_ends_it(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = self._overridden(clock)
        ent._window_ctrl.cancel_all()          # what summer mode's start does
        assert not ent._window_ctrl.overridden


@e2e
class TestAcrossAReload:
    def _restart(self, old, window_state):
        new = _make_entity(dict(old._config_entry.options))
        new.hass.set(WINDOW, window_state)
        attrs = {"preset_mode": old.preset_mode, "temperature": old.target_temperature,
                 "window_open_active": old._window_ctrl.is_active}
        _run(old.async_will_remove_from_hass())
        _run(_start(new, last_state=_State(old.hvac_mode.value, attrs),
                    extra=_Extra(old.extra_restore_state_data.as_dict())))
        return new

    def test_kept_while_the_window_is_still_open(self, monkeypatch):
        """Every preset temperature change reloads the room: the override
        must not be undone by it."""
        clock = _Clock(monkeypatch)
        old = _entity(clock)
        _open(old, clock)
        _run(old.async_set_preset_mode("comfort"))
        assert old.extra_restore_state_data.as_dict()["window_overridden"] is True
        reset_timers()
        new = self._restart(old, "on")
        assert new._window_ctrl.overridden and not new._window_ctrl.is_active
        assert new.preset_mode == "comfort"
        assert _timers("_async_window_action") == []
        assert any("Startup: the window is still open, overridden by hand" in t
                   for t in _texts(new))

    def test_forgotten_when_the_window_closed_meanwhile(self, monkeypatch):
        clock = _Clock(monkeypatch)
        old = _entity(clock)
        _open(old, clock)
        _run(old.async_set_preset_mode("comfort"))
        reset_timers()
        new = self._restart(old, "off")
        assert not new._window_ctrl.overridden and new.preset_mode == "comfort"


class TestTheController:
    """WindowAutomationController.override_open and its companions."""

    def _ctrl(self):
        from tests.pure_modules import climate_controllers as C
        return C, C.WindowAutomationController()

    @staticmethod
    def _call_later():
        calls = []

        def call_later(_hass, delay, cb):
            entry = {"delay": delay, "cb": cb, "cancelled": False}
            calls.append(entry)

            def cancel():
                entry["cancelled"] = True
            return cancel
        return calls, call_later

    def test_nothing_to_override_when_idle_or_shut(self):
        C, c = self._ctrl()
        assert not c.override_open() and not c.overridden
        calls, cl = self._call_later()
        c.activate("comfort", 20.0)
        c.handle_window_closed(None, 300, lambda now: None, call_later=cl, now=1000.0)
        assert c.close_delay_active and not c.override_open()

    def test_the_open_delay_is_cancelled(self):
        C, c = self._ctrl()
        calls, cl = self._call_later()
        c.handle_window_opened(None, 30, lambda now: None, call_later=cl)
        assert c.open_in_charge and c.override_open()
        assert calls[0]["cancelled"] and c.overridden and not c.open_delay_active
        c.handle_window_opened(None, 30, lambda now: None, call_later=cl)
        assert len(calls) == 1                     # the same opening: nothing armed

    def test_the_close_ends_it(self):
        C, c = self._ctrl()
        c.activate("comfort", 20.0)
        c.restore()
        c._overridden = False
        c.activate("eco", 19.0)
        assert c.override_open()
        c.restore()
        assert c.overridden
        calls, cl = self._call_later()
        assert c.handle_window_closed(None, 300, lambda now: None, call_later=cl) is False
        assert not c.overridden and calls == []

    def test_resume_open(self):
        C, c = self._ctrl()
        assert not c.resume_open(C.SavedState("eco", 19.0))
        c.activate("comfort", 20.0)
        c.override_open()
        c.restore()
        assert c.resume_open(C.SavedState("eco", 19.0))
        assert c.is_active and not c.overridden and c.get_saved() == C.SavedState("eco", 19.0)

    def test_marked_at_startup_and_forgotten(self):
        C, c = self._ctrl()
        c.mark_overridden()
        assert c.overridden
        c.forget_override()
        assert not c.overridden
        c.mark_overridden()
        c.cancel_all()
        assert not c.overridden
        c.activate("comfort", 20.0)
        c.mark_overridden()                        # active: nothing to take back
        assert not c.overridden

    def test_the_snapshot_keeps_it(self):
        C, _c = self._ctrl()
        snap = C.PersistedAutomationState(window_overridden=True)
        assert C.PersistedAutomationState.from_dict(snap.as_dict()).window_overridden is True
        older = snap.as_dict()
        del older["window_overridden"]
        assert C.PersistedAutomationState.from_dict(older).window_overridden is False


def test_the_decision_line_says_so():
    cfg = P.RegulationConfig()
    f = act.CycleFacts(path=act.PATH_REGULATED, mode="heat", prev_mode="stop",
                       setpoint_c=20.5, room_c=17.0, error_c=3.5,
                       window=act.WINDOW_OVERRIDDEN, target_label="Comfort")
    line = act.describe_cycle(f, cfg)
    assert line.startswith("Window open, overridden by hand: ")
    key = act.decision_key(f, cfg)
    assert key != act.decision_key(act.CycleFacts(**{**f.__dict__, "window": None}), cfg)
