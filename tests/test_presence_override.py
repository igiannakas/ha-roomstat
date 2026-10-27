"""A change by hand while nobody is home takes effect (2.9.0).

23 Oct 2026: the owner left at 13:18, all four rooms went to Away (16 °C).
On the way home, 18:16–18:19, he ran the Comfort script 13 times, Eco 5
times, Away once and pressed modes on single rooms ~10 times.  Every press
was only saved for the return ("Presence away: preset … saved for restore,
keeping away mode"), and the rooms got heat when he walked in at 19:32.

Rules under test:
- A temperature, preset or Boost picked by hand while away mode is on takes
  effect at once.  Away mode steps aside (``override``) until someone is
  home – then the change simply stays – or at most the configured limit
  after the last change (default 240 min, 0 = until someone is home), when
  the room goes back to away and coming home carries on with the change.
- Away picked while nobody is home is away mode itself: it ends an override
  and never leaves a sticky manual Away behind.  Resume schedule while
  nobody is home ends an override too.
- An open window still wins; the schedule, a boost ending or Off → Heat are
  not changes by hand.
- The override survives a restart or reload.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

from tests.ha_harness import (
    PRESENCE,
    SCHEDULE,
    SUMMER,
    TRV,
    WINDOW,
    _active_timers,
    _climate,
    _event,
    _make_entity,
    _restart_from,
    _run,
    _select_preset,
    _start,
    drain_tasks,
    e2e,
    reset_timers,
)

_ROOT = Path(__file__).parent.parent / "custom_components" / "roomstat"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ctrl = _load("climate_controllers")


@pytest.fixture(autouse=True)
def _reset_timers():
    reset_timers()
    yield
    reset_timers()


class _FakeCallLater:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, hass, delay, callback):
        entry = {"delay": delay, "callback": callback, "cancelled": False}
        self.calls.append(entry)

        def cancel():
            entry["cancelled"] = True

        return cancel

    @property
    def active(self):
        return [c for c in self.calls if not c["cancelled"]]


def _noop(_now):
    return None


# ===========================================================================
# PresenceAutomationController (HA-free)
# ===========================================================================

class TestControllerOverride:
    def _away(self):
        c = _ctrl.PresenceAutomationController()
        c.activate("comfort", 20.0)
        return c

    def test_start_leaves_away_and_arms_the_limit(self):
        c, cl = self._away(), _FakeCallLater()
        c.start_override(None, 1000.0, 240 * 60, _noop, call_later=cl)
        assert not c.is_active
        assert c.override_active and c.nobody_home
        assert c.override_until == 1000.0 + 240 * 60
        assert [e["delay"] for e in cl.active] == [240 * 60]
        assert c.get_saved() == _ctrl.SavedState()  # the change replaces it
        assert c.override_remaining_s(1000.0 + 60) == 240 * 60 - 60

    def test_zero_limit_lasts_until_home(self):
        c, cl = self._away(), _FakeCallLater()
        c.start_override(None, 1000.0, 0, _noop, call_later=cl)
        assert c.override_active and c.override_until is None
        assert cl.calls == []
        assert c.override_remaining_s(5000.0) is None

    def test_a_further_change_restarts_the_limit(self):
        c, cl = self._away(), _FakeCallLater()
        c.start_override(None, 1000.0, 600, _noop, call_later=cl)
        c.start_override(None, 1300.0, 600, _noop, call_later=cl)
        assert c.override_until == 1900.0
        assert len(cl.active) == 1 and cl.calls[0]["cancelled"]

    def test_home_ends_it_after_the_home_delay(self):
        c, cl = self._away(), _FakeCallLater()
        c.start_override(None, 1000.0, 600, _noop, call_later=cl)
        assert c.handle_presence_home(None, 30, _noop, call_later=cl) is False
        assert c.override_active  # until the home delay has passed
        assert cl.active[-1]["delay"] == 30
        saved = c.restore()
        assert saved == _ctrl.SavedState()
        assert not c.override_active and not c.nobody_home
        assert cl.active == []

    def test_home_without_delay_asks_for_the_restore(self):
        c = self._away()
        c.start_override(None, 1000.0, 600, _noop, call_later=_FakeCallLater())
        assert c.handle_presence_home(None, 0, None) is True

    def test_flicker_back_to_away_keeps_it(self):
        c, cl = self._away(), _FakeCallLater()
        c.start_override(None, 1000.0, 600, _noop, call_later=cl)
        c.handle_presence_home(None, 30, _noop, call_later=cl)
        c.handle_presence_away(None, 60, _noop, call_later=cl)
        assert c.override_active and not c.is_active
        # Only the limit is left: no away timer, the home timer cancelled.
        assert [e["delay"] for e in cl.active] == [600]

    def test_timer_fired_and_end(self):
        c = self._away()
        c.start_override(None, 1000.0, 600, _noop, call_later=_FakeCallLater())
        c.override_timer_fired()
        assert not c.override_active and c.override_until is None
        assert c.end_override() is False

    def test_activate_ends_it(self):
        c, cl = self._away(), _FakeCallLater()
        c.start_override(None, 1000.0, 600, _noop, call_later=cl)
        c.activate("eco", 18.0)
        assert c.is_active and not c.override_active
        assert cl.active == []

    def test_cancel_timer_keeps_it_for_the_snapshot(self):
        c, cl = self._away(), _FakeCallLater()
        c.start_override(None, 1000.0, 600, _noop, call_later=cl)
        c.cancel_timer()
        assert c.override_active and c.override_until == 1600.0
        assert cl.active == []

    @pytest.mark.parametrize(
        "until,now,ran_out,delays",
        [
            (1600.0, 1300.0, False, [300.0]),
            # Ran out while HA was down: handled at once, like any expiry.
            (1600.0, 1700.0, True, [0.0]),
            (None, 9e9, False, []),
        ],
    )
    def test_rearm(self, until, now, ran_out, delays):
        c, cl = _ctrl.PresenceAutomationController(), _FakeCallLater()
        c.load_override(until, _ctrl.SavedState("eco", 18.0))
        assert c.rearm_override(None, now, _noop, call_later=cl) is ran_out
        assert c.override_active
        assert c.get_before_override() == _ctrl.SavedState("eco", 18.0)
        assert [e["delay"] for e in cl.active] == delays

    def test_extend_and_clear_limit(self):
        c, cl = self._away(), _FakeCallLater()
        c.start_override(None, 1000.0, 600, _noop, call_later=cl)
        c.extend_override(None, 1600.0, 600, _noop, call_later=cl)
        assert c.override_active and c.override_until == 2200.0
        assert [e["delay"] for e in cl.active] == [600]
        c.clear_limit()
        assert c.override_active and c.override_until is None
        assert cl.active == []

    def test_before_override_is_what_away_had_saved(self):
        c = self._away()
        c.start_override(None, 1000.0, 600, _noop, call_later=_FakeCallLater())
        assert c.get_before_override() == _ctrl.SavedState("comfort", 20.0)
        # A further change keeps it; ending the override drops it.
        c.start_override(None, 1100.0, 600, _noop, call_later=_FakeCallLater())
        assert c.get_before_override() == _ctrl.SavedState("comfort", 20.0)
        c.end_override()
        assert c.get_before_override() == _ctrl.SavedState()

    def test_persisted_round_trip(self):
        p = _ctrl.PersistedAutomationState(
            presence_override_active=True, presence_override_until=1600.0
        )
        back = _ctrl.PersistedAutomationState.from_dict(p.as_dict())
        assert back.presence_override_active is True
        assert back.presence_override_until == 1600.0
        # Snapshots from before 2.9.0 have neither key.
        old = {k: v for k, v in p.as_dict().items() if not k.startswith("presence_override")}
        back = _ctrl.PersistedAutomationState.from_dict(old)
        assert back.presence_override_active is False
        assert back.presence_override_until is None


# ===========================================================================
# The thermostat entity
# ===========================================================================

def _timers(name):
    return [c for c in _active_timers() if getattr(c["callback"], "__name__", "") == name]


def _fire(ent, name):
    (entry,) = _timers(name)
    entry["cancelled"] = True
    _run(entry["callback"](None))
    drain_tasks(ent)


def _entity(schedule_state=None, *, limit_min=None, override_min=60, window=False):
    options = {"presence_away_delay_s": 60, "presence_home_delay_s": 30,
               "schedule_override_min": override_min, "away_target": 16.0}
    if limit_min is not None:
        options["presence_override_max_min"] = limit_min
    ent = _make_entity(options, window=window, presence=True,
                       schedule=schedule_state is not None)
    ent.hass.keep_tasks = True
    if schedule_state is not None:
        ent.hass.set(SCHEDULE, schedule_state)
    ent.hass.set(TRV, "heat", {"temperature": 20.0})
    ent.hass.set(PRESENCE, "on")
    _run(_start(ent))
    drain_tasks(ent)
    return ent


def _leave(ent):
    ent.hass.set(PRESENCE, "off")
    ent._async_presence_changed(_event("off"))
    _fire(ent, "_async_presence_away_action")
    assert ent.preset_mode == "away"
    assert ent.extra_state_attributes["presence_away_active"] is True


def _arrive(ent):
    ent.hass.set(PRESENCE, "on")
    ent._async_presence_changed(_event("on"))
    _fire(ent, "_async_presence_home_action")


def _preset(ent, preset):
    _run(_select_preset(ent, preset))
    drain_tasks(ent)


def _schedule(ent, state):
    ent.hass.set(SCHEDULE, state)
    ent._async_schedule_changed(_event(state))
    drain_tasks(ent)


def _held(ent) -> bool:
    return ent.extra_state_attributes["presence_override_active"]


@e2e
class TestChangeByHandWhileAway:
    def test_23_oct_comfort_on_the_way_home_heats_at_once(self):
        ent = _entity("comfort")
        _leave(ent)
        assert ent.target_temperature == 16.0
        before = time.time()
        _preset(ent, "comfort")
        assert ent.preset_mode == "comfort"
        assert ent.target_temperature == 20.5
        assert _held(ent)
        attrs = ent.extra_state_attributes
        assert attrs["presence_away_active"] is False
        until = _climate.datetime.datetime.fromisoformat(attrs["presence_override_until"])
        assert abs(until.timestamp() - (before + 240 * 60)) < 5
        # The Comfort the schedule asks for is no schedule override …
        assert attrs["schedule_override_active"] is False
        # … and the presses that follow keep it (the script ran 13 times).
        for _ in range(12):
            _preset(ent, "comfort")
        assert ent.preset_mode == "comfort" and _held(ent)
        assert len(_timers("_async_presence_override_expired")) == 1
        # Home 75 min later: the change simply stays.
        _arrive(ent)
        assert ent.preset_mode == "comfort"
        assert not _held(ent) and not ent._presence_ctrl.nobody_home

    def test_temperature_by_hand_takes_effect(self):
        ent = _entity("night")
        _leave(ent)
        _run(ent.async_set_temperature(temperature=21.0))
        drain_tasks(ent)
        assert ent.preset_mode == "none"
        assert ent.target_temperature == 21.0
        assert _held(ent)
        assert ent.extra_state_attributes["schedule_override_active"] is True

    def test_limit_runs_out_back_to_away_and_home_carries_on(self):
        ent = _entity(None, limit_min=90)
        _leave(ent)
        _preset(ent, "eco")
        assert ent.preset_mode == "eco"
        (limit,) = _timers("_async_presence_override_expired")
        assert limit["delay"] == 90 * 60
        _fire(ent, "_async_presence_override_expired")
        assert ent.preset_mode == "away"
        assert ent._presence_ctrl.is_active and not _held(ent)
        _arrive(ent)
        assert ent.preset_mode == "eco"

    def test_limit_running_out_after_someone_came_home_keeps_the_change(self):
        ent = _entity(None, limit_min=90)
        _leave(ent)
        _preset(ent, "eco")
        # Home, but still within the home delay when the limit runs out.
        ent.hass.set(PRESENCE, "on")
        ent._async_presence_changed(_event("on"))
        _fire(ent, "_async_presence_override_expired")
        assert ent.preset_mode == "eco"
        assert not ent._presence_ctrl.nobody_home

    def test_zero_limit_lasts_until_home(self):
        ent = _entity(None, limit_min=0)
        _leave(ent)
        _preset(ent, "comfort")
        assert _held(ent)
        assert _timers("_async_presence_override_expired") == []
        assert ent.extra_state_attributes["presence_override_until"] is None

    def test_a_flicker_home_and_back_keeps_the_change(self):
        ent = _entity(None)
        _leave(ent)
        _preset(ent, "comfort")
        ent.hass.set(PRESENCE, "on")
        ent._async_presence_changed(_event("on"))
        ent.hass.set(PRESENCE, "off")
        ent._async_presence_changed(_event("off"))
        assert _timers("_async_presence_home_action") == []
        assert _timers("_async_presence_away_action") == []
        assert ent.preset_mode == "comfort" and _held(ent)

    def test_leaving_again_after_coming_home_is_normal_away(self):
        ent = _entity(None)
        _leave(ent)
        _preset(ent, "comfort")
        _arrive(ent)
        _leave(ent)
        assert ent._presence_ctrl.get_saved().preset == "comfort"

    def test_boost_while_away_with_schedule_ends_on_the_schedule(self):
        ent = _entity("night")
        _leave(ent)
        _preset(ent, "boost")
        assert ent.preset_mode == "boost" and _held(ent)
        _fire(ent, "_async_boost_expired")
        assert ent.preset_mode == "eco"
        assert _held(ent)  # still as if home until someone is, or the limit

    def test_schedule_changes_apply_while_held(self):
        ent = _entity("comfort")
        _leave(ent)
        _preset(ent, "comfort")
        _schedule(ent, "night")
        assert ent.preset_mode == "eco"
        assert _held(ent)
        # The limit then sends the room back to away; home restores the schedule.
        _fire(ent, "_async_presence_override_expired")
        assert ent.preset_mode == "away"
        _schedule(ent, "comfort")
        _arrive(ent)
        assert ent.preset_mode == "comfort"

    def test_open_window_still_wins(self):
        ent = _entity(None, window=True)
        _leave(ent)
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        _fire(ent, "_async_window_action")
        assert ent.preset_mode == "frost_protection"
        _preset(ent, "comfort")
        assert ent.preset_mode == "frost_protection"
        assert _held(ent)
        _run(ent.async_set_temperature(temperature=21.5))
        assert ent.preset_mode == "frost_protection"
        ent.hass.set(WINDOW, "off")
        ent._async_window_changed(_event("off"))
        drain_tasks(ent)
        assert ent.preset_mode == "none"
        assert ent.target_temperature == 21.5

    def test_limit_running_out_with_the_window_open(self):
        ent = _entity(None, window=True)
        _leave(ent)
        _preset(ent, "comfort")
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        _fire(ent, "_async_window_action")
        _fire(ent, "_async_presence_override_expired")
        assert ent.preset_mode == "frost_protection"
        assert ent._presence_ctrl.is_active
        ent.hass.set(WINDOW, "off")
        ent._async_window_changed(_event("off"))
        drain_tasks(ent)
        assert ent.preset_mode == "away"
        _arrive(ent)
        assert ent.preset_mode == "comfort"

    def test_off_then_heat_is_no_change_by_hand(self):
        ent = _entity("night")
        _leave(ent)
        _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
        _run(ent.async_set_hvac_mode(_climate.HVACMode.HEAT))
        drain_tasks(ent)
        assert ent.preset_mode == "away"
        assert not _held(ent)

    def test_heat_when_already_heating_is_no_change_by_hand(self):
        """The heating scripts set hvac_mode heat before the preset."""
        ent = _entity("night")
        _leave(ent)
        _run(ent.async_set_hvac_mode(_climate.HVACMode.HEAT))
        drain_tasks(ent)
        assert ent.preset_mode == "away" and not _held(ent)

    def test_summer_mode_clears_it(self):
        ent = _make_entity({"presence_away_delay_s": 60}, window=False,
                           presence=True, summer=True)
        ent.hass.keep_tasks = True
        ent.hass.set(SUMMER, "off")
        ent.hass.set(PRESENCE, "on")
        _run(_start(ent))
        drain_tasks(ent)
        _leave(ent)
        _preset(ent, "comfort")
        assert _held(ent)
        ent.hass.set(SUMMER, "on")
        ent._async_summer_changed(_event("on"))
        drain_tasks(ent)
        assert not ent._presence_ctrl.nobody_home
        assert _timers("_async_presence_override_expired") == []


def _window(ent, state):
    ent.hass.set(WINDOW, state)
    ent._async_window_changed(_event(state))
    if state == "on":
        _fire(ent, "_async_window_action")
    drain_tasks(ent)


def _window_with_close_delay(ent):
    """Open the window, then close it: the close delay (and its re-warm wait)
    is running afterwards."""
    ent._config_entry.options["window_close_delay_s"] = 300
    _window(ent, "on")
    _window(ent, "off")
    assert ent._window_ctrl.close_delay_active


@e2e
class TestReviewFindings:
    def test_boost_in_the_close_delay_while_away_ends_on_the_pre_away_preset(self):
        ent = _entity(None, window=True)
        _leave(ent)
        _window_with_close_delay(ent)
        _preset(ent, "boost")
        assert ent.preset_mode == "boost" and _held(ent)
        assert not ent._window_ctrl.is_active
        assert _timers("_async_window_close_action") == []
        _fire(ent, "_async_boost_expired")
        assert ent.preset_mode == "comfort"
        _arrive(ent)
        assert ent.preset_mode == "comfort"

    def test_boost_in_the_close_delay_when_home_ends_on_the_pre_window_preset(self):
        ent = _entity(None, window=True)
        _preset(ent, "eco")
        _window_with_close_delay(ent)
        _preset(ent, "boost")
        _fire(ent, "_async_boost_expired")
        assert ent.preset_mode == "eco"  # was Off (the window's frost protection)

    def test_boost_in_the_close_delay_then_the_window_again(self):
        ent = _entity("night", window=True)
        _leave(ent)
        _window_with_close_delay(ent)
        _preset(ent, "boost")
        _window(ent, "on")
        _window(ent, "off")
        _fire(ent, "_async_window_close_action")
        assert ent.preset_mode == "eco"
        _arrive(ent)
        assert ent.preset_mode == "eco"

    def test_temperature_in_the_close_delay_applies_now(self):
        ent = _entity(None, window=True)
        _window_with_close_delay(ent)
        _run(ent.async_set_temperature(temperature=21.5))
        drain_tasks(ent)
        assert not ent._window_ctrl.is_active
        assert _timers("_async_window_close_action") == []
        assert ent.preset_mode == "none" and ent.target_temperature == 21.5

    # An Off has no limit at all (2.12.0, tests/test_off_and_automatic_while_away.py).
    @pytest.mark.parametrize("change", [15.0, 16.0])
    def test_a_change_heating_no_more_than_away_outlasts_the_limit(self, change):
        ent = _entity(None, limit_min=90)
        _leave(ent)
        if isinstance(change, float):
            _run(ent.async_set_temperature(temperature=change))
            drain_tasks(ent)
        else:
            _preset(ent, change)
        _fire(ent, "_async_presence_override_expired")
        assert _held(ent) and ent.preset_mode != "away"
        (limit,) = _timers("_async_presence_override_expired")
        assert limit["delay"] == 90 * 60
        # A warmer change later gets the limit again.
        _preset(ent, "comfort")
        _fire(ent, "_async_presence_override_expired")
        assert ent.preset_mode == "away"

    def test_off_hvac_outlasts_the_limit(self):
        ent = _entity(None, limit_min=90)
        _leave(ent)
        _preset(ent, "comfort")
        _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
        _fire(ent, "_async_presence_override_expired")
        assert _held(ent)

    def test_limit_running_out_with_the_sensor_unavailable_goes_to_away(self):
        ent = _entity(None, limit_min=90)
        _leave(ent)
        _preset(ent, "comfort")
        ent.hass.set(PRESENCE, "unavailable")
        ent._async_presence_changed(_event("unavailable"))
        _fire(ent, "_async_presence_override_expired")
        assert ent.preset_mode == "away"
        assert ent._presence_ctrl.is_active
        # The sensor back on home restores as usual.
        _arrive(ent)
        assert ent.preset_mode == "comfort"

    def test_option_set_to_zero_meanwhile_keeps_it_until_home(self):
        ent = _entity(None, limit_min=90)
        _leave(ent)
        _preset(ent, "comfort")
        ent._config_entry.options["presence_override_max_min"] = 0
        _fire(ent, "_async_presence_override_expired")
        assert _held(ent) and ent.preset_mode == "comfort"
        assert ent.extra_state_attributes["presence_override_until"] is None
        assert _timers("_async_presence_override_expired") == []

    def test_away_in_the_home_delay_is_an_ordinary_away(self):
        ent = _entity("comfort")
        _leave(ent)
        _preset(ent, "comfort")
        ent.hass.set(PRESENCE, "on")
        ent._async_presence_changed(_event("on"))
        _preset(ent, "away")
        assert ent.preset_mode == "away"
        _fire(ent, "_async_presence_home_action")
        assert ent.preset_mode == "away"  # picked by someone who is home
        assert ent.extra_state_attributes["schedule_override_active"] is True

    def test_away_with_the_sensor_unavailable_goes_to_away(self):
        ent = _entity("comfort")
        _leave(ent)
        _preset(ent, "comfort")
        ent.hass.set(PRESENCE, "unavailable")
        _preset(ent, "away")
        assert ent.preset_mode == "away" and ent._presence_ctrl.is_active

    def test_manual_away_then_home_with_the_window_open(self):
        ent = _entity("comfort", window=True)
        _preset(ent, "away")
        _leave(ent)
        _window(ent, "on")
        _arrive(ent)
        _window(ent, "off")
        assert ent.preset_mode == "comfort"
        assert ent.extra_state_attributes["schedule_override_active"] is False
        _schedule(ent, "night")
        assert ent.preset_mode == "eco"

    def test_manual_away_then_leaving_while_the_schedule_is_off(self):
        ent = _entity("frost_protection")
        _preset(ent, "away")
        _leave(ent)
        _arrive(ent)
        assert ent.preset_mode == "frost_protection"
        _schedule(ent, "comfort")
        assert ent.preset_mode == "comfort"  # was ignored: Off for good


@e2e
class TestAwayAndResumeWhileNobodyHome:
    def test_away_cancels_the_change(self):
        ent = _entity("night")
        _leave(ent)
        _preset(ent, "comfort")
        _preset(ent, "away")
        assert ent.preset_mode == "away"
        assert ent._presence_ctrl.is_active and not _held(ent)
        assert _timers("_async_presence_override_expired") == []
        # The schedule override the change started is cancelled with it …
        assert ent.extra_state_attributes["schedule_override_active"] is False
        assert _timers("_async_schedule_override_expired") == []
        # … so coming home brings the schedule's preset, not the change.
        _arrive(ent)
        assert ent.preset_mode == "eco"

    def test_away_cancels_a_boost_too(self):
        ent = _entity("night")
        _leave(ent)
        _preset(ent, "boost")
        _preset(ent, "away")
        assert ent.preset_mode == "away"
        assert ent._boost_cancel is None and ent.boost_remaining_minutes == 0
        _arrive(ent)
        assert ent.preset_mode == "eco"

    def test_away_without_a_schedule_brings_back_the_pre_away_preset(self):
        ent = _entity(None)
        _preset(ent, "eco")
        _leave(ent)
        _preset(ent, "comfort")
        _preset(ent, "away")
        _arrive(ent)
        assert ent.preset_mode == "eco"

    def test_away_with_the_window_open_cancels_into_the_window_target(self):
        ent = _entity("night", window=True)
        _leave(ent)
        _preset(ent, "comfort")
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        _fire(ent, "_async_window_action")
        _preset(ent, "away")
        assert ent.preset_mode == "frost_protection"
        assert ent._presence_ctrl.is_active
        ent.hass.set(WINDOW, "off")
        ent._async_window_changed(_event("off"))
        drain_tasks(ent)
        assert ent.preset_mode == "away"
        _arrive(ent)
        assert ent.preset_mode == "eco"

    def test_away_while_away_leaves_no_sticky_override(self):
        """23 Oct, 18:18:59: the Away script while nobody was home left a
        sticky manual Away behind, which would have hidden the schedule after
        the return had it been the last press."""
        ent = _entity("comfort")
        _leave(ent)
        _preset(ent, "away")
        assert ent.extra_state_attributes["schedule_override_active"] is False
        _arrive(ent)
        assert ent.preset_mode == "comfort"
        _schedule(ent, "night")
        assert ent.preset_mode == "eco"

    def test_manual_away_before_leaving_does_not_hide_the_schedule_after(self):
        ent = _entity("comfort")
        _preset(ent, "away")
        assert ent.extra_state_attributes["schedule_override_active"] is True
        _leave(ent)
        _arrive(ent)
        assert ent.preset_mode == "comfort"
        assert ent.extra_state_attributes["schedule_override_active"] is False
        _schedule(ent, "night")
        assert ent.preset_mode == "eco"

    def test_resume_schedule_while_held_goes_back_to_away(self):
        ent = _entity("night")
        _leave(ent)
        _preset(ent, "comfort")
        assert ent.extra_state_attributes["schedule_override_active"] is True
        _preset(ent, "schedule")
        assert ent.preset_mode == "away"
        assert ent._presence_ctrl.is_active and not _held(ent)
        assert ent._presence_ctrl.get_saved().preset == "eco"
        assert ent.extra_state_attributes["schedule_override_active"] is False
        _arrive(ent)
        assert ent.preset_mode == "eco"

    def test_resume_schedule_while_away_stays_away(self):
        ent = _entity("night")
        _leave(ent)
        _run(ent.async_resume_schedule())
        drain_tasks(ent)
        assert ent.preset_mode == "away" and not _held(ent)


@e2e
class TestRestart:
    def _held_entity(self, limit_min=240):
        old = _entity(None, limit_min=limit_min)
        _leave(old)
        _preset(old, "comfort")
        assert _held(old)
        _run(old.async_will_remove_from_hass())
        return old

    def _new(self, presence_state):
        new = _make_entity({"presence_away_delay_s": 60, "presence_home_delay_s": 30,
                            "away_target": 16.0}, window=False, presence=True)
        new.hass.keep_tasks = True
        new.hass.set(TRV, "heat", {"temperature": 20.0})
        new.hass.set(PRESENCE, presence_state)
        return new

    def test_still_away_re_arms_the_rest_of_the_limit(self):
        old = self._held_entity()
        reset_timers()
        new = self._new("off")
        _restart_from(old, new)
        assert new.preset_mode == "comfort" and _held(new)
        (limit,) = _timers("_async_presence_override_expired")
        assert 0 < limit["delay"] <= 240 * 60
        assert _timers("_async_presence_away_action") == []

    def test_sensor_not_loaded_yet_keeps_it(self):
        old = self._held_entity()
        new = self._new("unavailable")
        _restart_from(old, new)
        assert new.preset_mode == "comfort" and _held(new)

    def test_home_meanwhile_keeps_the_change(self):
        old = self._held_entity()
        new = self._new("on")
        _restart_from(old, new)
        assert new.preset_mode == "comfort"
        assert not new._presence_ctrl.nobody_home

    def test_cancel_after_a_restart_still_knows_the_pre_away_preset(self):
        old = _entity(None)
        _preset(old, "eco")
        _leave(old)
        _preset(old, "comfort")
        _run(old.async_will_remove_from_hass())
        new = self._new("off")
        _restart_from(old, new)
        _preset(new, "away")
        _arrive(new)
        assert new.preset_mode == "eco"

    def test_limit_ran_out_while_down_goes_back_to_away_at_once(self):
        old = self._held_entity()
        old._presence_ctrl.override_until = time.time() - 60
        reset_timers()
        new = self._new("off")
        _restart_from(old, new)
        (limit,) = _timers("_async_presence_override_expired")
        assert limit["delay"] == 0.0
        assert _timers("_async_presence_away_action") == []
        _fire(new, "_async_presence_override_expired")
        assert new.preset_mode == "away"
        _arrive(new)
        assert new.preset_mode == "comfort"

    def test_a_stale_sticky_override_is_dropped_at_startup(self):
        """A sticky schedule override with the room in another preset (left by
        an older version) would hide the schedule after the restart."""
        old = _entity("comfort")
        _preset(old, "away")
        old._preset_mode = "comfort"  # restored by presence before 2.9.0
        assert old.extra_state_attributes["schedule_override_active"] is True
        _run(old.async_will_remove_from_hass())
        new = _make_entity({"schedule_override_min": 60}, window=False,
                           presence=True, schedule=True)
        new.hass.keep_tasks = True
        new.hass.set(SCHEDULE, "comfort")
        new.hass.set(TRV, "heat", {"temperature": 20.0})
        new.hass.set(PRESENCE, "on")
        _restart_from(old, new)
        assert new.extra_state_attributes["schedule_override_active"] is False
        _schedule(new, "night")
        assert new.preset_mode == "eco"
