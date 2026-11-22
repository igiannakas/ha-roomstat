"""A Boost and the schedule override it starts end together (2.27.4).

Owner, 20 Nov 2026, living room: Boost 22 °C for 15 min, the schedule
override 30 min.  The Boost's confirmation said "Heats to 22 °C for 15
minutes, then goes back to its schedule" (true: its end clears the override
and follows the schedule), but the card's status line then read "Override ·
Boost · 27 min": a Boost picked by hand started an ordinary override of the
override's own length, which its end cleared 15 min early.  With a Boost
longer than the override, the override's end took the room back to the
schedule mid-Boost.

Rules under test:
- A Boost picked by hand starts a schedule override that ends when the Boost
  does (``schedule_override_until`` == the Boost's end), whatever
  ``schedule_override_min`` is (0 included); pressed again, both restart.
- Whichever of the two timers fires first ends both, as the Boost's end.
- A restart or reload mid-Boost resumes both with the time left; a save from
  2.27.3 or older is aligned to the Boost's end; a Boost over while Home
  Assistant was down takes its override with it.
- The thermostat tells the card when the Boost ends (``boost_until``) and
  what it ends on (``boost_returns_to``, ``boost_returns_to_c``).
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
import time
from pathlib import Path

import pytest

from tests.ha_harness import (
    PRESENCE,
    SCHEDULE,
    TRV,
    WINDOW,
    _active_timers,
    _event,
    _Extra,
    _make_entity,
    _restart_from,
    _run,
    _select_preset,
    _start,
    _State,
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
# ScheduleController (HA-free)
# ===========================================================================

class TestControllerUntil:
    def test_until_wins_over_the_length(self):
        c, cl = _ctrl.ScheduleController(), _FakeCallLater()
        c.start_override(None, 30.0, 1000.0, _noop, until=1900.0, call_later=cl)
        assert c.override_active and not c.override_sticky
        assert c.override_until == 1900.0
        assert [e["delay"] for e in cl.active] == [900.0]
        assert c.remaining_minutes(1000.0) == 15

    def test_until_with_no_length_is_timed_too(self):
        c, cl = _ctrl.ScheduleController(), _FakeCallLater()
        c.start_override(None, 0.0, 1000.0, _noop, until=4600.0, call_later=cl)
        assert c.override_until == 4600.0
        assert [e["delay"] for e in cl.active] == [3600.0]

    def test_until_past_fires_at_once(self):
        c, cl = _ctrl.ScheduleController(), _FakeCallLater()
        c.start_override(None, 30.0, 1000.0, _noop, until=990.0, call_later=cl)
        assert [e["delay"] for e in cl.active] == [0.0]

    def test_sticky_ignores_until(self):
        c, cl = _ctrl.ScheduleController(), _FakeCallLater()
        c.start_override(None, 30.0, 1000.0, _noop, sticky=True, until=1900.0, call_later=cl)
        assert c.override_until is None and cl.calls == []

    def test_without_until_as_before(self):
        c, cl = _ctrl.ScheduleController(), _FakeCallLater()
        c.start_override(None, 30.0, 1000.0, _noop, call_later=cl)
        assert c.override_until == 2800.0
        c.start_override(None, 0.0, 1000.0, _noop, call_later=cl)
        assert c.override_until is None and cl.active == []


# ===========================================================================
# Through the entity
# ===========================================================================

def _entity(schedule_state="comfort", *, override_min=30, boost_min=15, presence=False,
            **options):
    opts = {"schedule_override_min": override_min, "boost_duration": boost_min,
            "boost_target": 22.0, **options}
    if presence:
        opts.update({"presence_away_delay_s": 60, "presence_home_delay_s": 30})
    ent = _make_entity(opts, schedule=schedule_state is not None, presence=presence)
    ent.hass.keep_tasks = True
    if schedule_state is not None:
        ent.hass.set(SCHEDULE, schedule_state)
    ent.hass.set(TRV, "heat", {"temperature": 20.0})
    ent.hass.set(WINDOW, "off")
    if presence:
        ent.hass.set(PRESENCE, "on")
    _run(_start(ent))
    drain_tasks(ent)
    return ent


def _preset(ent, preset):
    _run(_select_preset(ent, preset))
    drain_tasks(ent)


def _timers(name):
    return [c for c in _active_timers() if getattr(c["callback"], "__name__", "") == name]


def _fire(ent, name):
    (entry,) = _timers(name)
    entry["cancelled"] = True
    _run(entry["callback"](None))
    drain_tasks(ent)


def _lines(ent):
    return [e.text for e in reversed(ent._activity.entries())]  # oldest first


def _ts(iso):
    return datetime.datetime.fromisoformat(iso).timestamp()


def _schedule(ent, state):
    ent.hass.set(SCHEDULE, state)
    ent._async_schedule_changed(_event(state))
    drain_tasks(ent)


@e2e
class TestOneEnd:
    def test_20_nov_boost_15_override_30(self):
        """The owner's case: the override, its sensor and the card all read
        the Boost's 15 min, not the override's 30."""
        ent = _entity("comfort", override_min=30, boost_min=15)
        before = time.time()
        _preset(ent, "boost")
        attrs = ent.extra_state_attributes
        assert ent.preset_mode == "boost"
        assert attrs["schedule_override_active"] is True
        assert attrs["schedule_override_until"] == attrs["boost_until"]
        assert abs(_ts(attrs["boost_until"]) - (before + 900)) < 5
        assert ent._schedule_ctrl.override_until == ent._boost_end_ts
        assert ent.schedule_override_remaining_minutes == 15
        assert ent.boost_remaining_minutes == 15
        assert [e["delay"] for e in _timers("_async_schedule_override_expired")] == [900]
        assert [e["delay"] for e in _timers("_async_boost_expired")] == [900]
        assert attrs["boost_returns_to"] == "schedule"
        assert attrs["boost_returns_to_c"] is None
        lines = _lines(ent)
        assert "Boost started for 15 min" in lines
        assert "Schedule override started (until the Boost ends, 15 min)" in lines
        assert not any("30" in line for line in lines if "override started" in line)

    @pytest.mark.parametrize("first", ["_async_boost_expired", "_async_schedule_override_expired"])
    def test_whichever_timer_fires_first_ends_both(self, first):
        ent = _entity("night")
        _preset(ent, "comfort")            # an override by hand before the Boost
        _preset(ent, "boost")
        _fire(ent, first)
        assert ent.preset_mode == "eco"
        attrs = ent.extra_state_attributes
        assert attrs["schedule_override_active"] is False
        assert attrs["schedule_override_until"] is None
        assert attrs["boost_until"] is None
        assert _timers("_async_boost_expired") == []
        assert _timers("_async_schedule_override_expired") == []
        assert "Boost over, returning to the schedule (eco)" in _lines(ent)
        assert "Schedule override expired" not in _lines(ent)

    def test_a_boost_longer_than_the_override_runs_its_length(self):
        """Until 2.27.3 the override's 30 min took the room back to the
        schedule in the middle of a 60-min Boost."""
        ent = _entity("night", override_min=30, boost_min=60)
        _preset(ent, "boost")
        assert [e["delay"] for e in _timers("_async_schedule_override_expired")] == [3600]
        assert ent.schedule_override_remaining_minutes == 60
        _fire(ent, "_async_schedule_override_expired")
        assert ent.preset_mode == "eco"
        assert _timers("_async_boost_expired") == []

    def test_until_next_change_boost_still_ends_with_its_time(self):
        ent = _entity("night", override_min=0)
        _preset(ent, "boost")
        attrs = ent.extra_state_attributes
        assert attrs["schedule_override_until"] == attrs["boost_until"]
        assert ent.schedule_override_remaining_minutes == 15
        # A schedule change ends an override, and the Boost with it, as before.
        _schedule(ent, "comfort")
        assert ent.preset_mode == "comfort"
        assert ent.extra_state_attributes["boost_until"] is None
        assert _timers("_async_boost_expired") == []
        assert _timers("_async_schedule_override_expired") == []

    def test_other_changes_by_hand_keep_the_override_length(self):
        ent = _entity("night", override_min=30)
        _preset(ent, "boost")
        _preset(ent, "comfort")
        assert ent.extra_state_attributes["boost_until"] is None
        assert ent.schedule_override_remaining_minutes == 30
        assert [e["delay"] for e in _timers("_async_schedule_override_expired")] == [1800]
        _run(ent.async_set_temperature(temperature=21.0))
        assert ent.schedule_override_remaining_minutes == 30

    def test_pressed_again_restarts_both(self):
        ent = _entity("comfort")
        _preset(ent, "boost")
        ent._boost_end_ts -= 6 * 60          # as if 6 min had gone by
        ent._schedule_ctrl.override_until -= 6 * 60
        assert ent.boost_remaining_minutes == 9
        _preset(ent, "boost")
        attrs = ent.extra_state_attributes
        assert ent.boost_remaining_minutes == 15
        assert attrs["schedule_override_until"] == attrs["boost_until"]
        assert len(_timers("_async_boost_expired")) == 1
        assert len(_timers("_async_schedule_override_expired")) == 1
        assert "Boost restarted for 15 min (9 min were left)" in _lines(ent)
        # Its saved state is still the one from before the first press.
        assert ent._boost_saved_preset == "comfort"

    def test_off_by_hand_then_boost_is_no_longer_sticky(self):
        ent = _entity("comfort")
        _preset(ent, "frost_protection")
        assert ent._schedule_ctrl.override_sticky
        _preset(ent, "boost")
        assert not ent._schedule_ctrl.override_sticky
        assert ent._schedule_ctrl.override_until == ent._boost_end_ts
        _fire(ent, "_async_boost_expired")
        assert ent.preset_mode == "comfort"


@e2e
class TestWithoutSchedule:
    def test_the_boost_has_its_own_end_and_goes_back_to_the_preset_before(self):
        ent = _entity(None)
        _preset(ent, "eco")
        assert ent.extra_state_attributes["boost_returns_to"] == "eco"
        _preset(ent, "boost")
        attrs = ent.extra_state_attributes
        assert attrs["boost_until"] is not None
        assert attrs["schedule_override_active"] is False
        assert attrs["boost_returns_to"] == "eco"
        assert _timers("_async_schedule_override_expired") == []
        assert not any("override" in line for line in _lines(ent))
        _fire(ent, "_async_boost_expired")
        assert ent.preset_mode == "eco"
        assert ent.extra_state_attributes["boost_until"] is None

    def test_a_temperature_set_by_hand_is_what_it_goes_back_to(self):
        ent = _entity(None)
        _run(ent.async_set_temperature(temperature=19.5))
        drain_tasks(ent)
        attrs = ent.extra_state_attributes
        assert (attrs["boost_returns_to"], attrs["boost_returns_to_c"]) == ("none", 19.5)
        _preset(ent, "boost")
        attrs = ent.extra_state_attributes
        assert (attrs["boost_returns_to"], attrs["boost_returns_to_c"]) == ("none", 19.5)
        _fire(ent, "_async_boost_expired")
        assert ent.preset_mode == "none" and ent.native_target_temperature == 19.5

    @pytest.mark.parametrize("before", ["comfort", "eco", "frost_protection", "away"])
    def test_what_the_card_is_told_is_what_the_boost_ends_on(self, before):
        ent = _entity(None)
        _preset(ent, before)
        told = ent.extra_state_attributes["boost_returns_to"]
        _preset(ent, "boost")
        _fire(ent, "_async_boost_expired")
        assert told == before == ent.preset_mode

    def test_unknown_while_away_mode_holds_the_room(self):
        ent = _entity(None, presence=True)
        ent.hass.set(PRESENCE, "off")
        ent._async_presence_changed(_event("off"))
        _fire(ent, "_async_presence_away_action")
        assert ent.preset_mode == "away"
        assert ent.extra_state_attributes["boost_returns_to"] is None
        _preset(ent, "boost")
        # Running, it is known: the preset away mode had saved.
        assert ent.extra_state_attributes["boost_returns_to"] == ent._boost_saved_preset == "comfort"


@e2e
class TestNobodyHome:
    def test_boost_while_out_shows_its_own_end_and_the_hold_goes_on(self):
        ent = _entity("comfort", presence=True)
        ent.hass.set(PRESENCE, "off")
        ent._async_presence_changed(_event("off"))
        _fire(ent, "_async_presence_away_action")
        _preset(ent, "boost")
        attrs = ent.extra_state_attributes
        assert attrs["presence_override_active"] is True
        assert attrs["schedule_override_until"] == attrs["boost_until"]
        assert _ts(attrs["presence_override_until"]) > _ts(attrs["boost_until"]) + 3600
        _fire(ent, "_async_boost_expired")
        assert ent.preset_mode == "comfort"
        assert ent.extra_state_attributes["presence_override_active"] is True
        assert ent.extra_state_attributes["schedule_override_active"] is False


@e2e
class TestAcrossARestart:
    def test_a_reload_mid_boost_resumes_both_with_the_time_left(self):
        old = _entity("comfort")
        _preset(old, "boost")
        end = old._boost_end_ts
        old._boost_end_ts = end - 5 * 60     # 10 min left
        old._schedule_ctrl.override_until = end - 5 * 60
        reset_timers()
        new = _make_entity(dict(old._config_entry.options), schedule=True)
        new.hass = old.hass
        _restart_from(old, new)
        drain_tasks(new)
        attrs = new.extra_state_attributes
        assert new.preset_mode == "boost"
        assert new.boost_remaining_minutes == 10
        assert attrs["schedule_override_until"] == attrs["boost_until"]
        assert abs(_ts(attrs["boost_until"]) - (end - 300)) < 1
        assert [round(e["delay"]) for e in _timers("_async_boost_expired")] == [600]
        assert [round(e["delay"]) for e in _timers("_async_schedule_override_expired")] == [600]
        lines = _lines(new)
        new_lines = lines[lines.index("Startup: boost resumed, 10 min remaining"):]
        # Nothing restarted it: no new Boost or override line after the resume.
        assert not any(
            line.startswith(("Boost started", "Boost restarted", "Schedule override started"))
            for line in new_lines
        )
        assert not any("now ends with the Boost" in line for line in new_lines)

    def _boot(self, persisted, *, override_min=30, boost_min=15, helper="comfort"):
        ent = _make_entity(
            {"schedule_override_min": override_min, "boost_duration": boost_min},
            schedule=True,
        )
        ent.hass.keep_tasks = True
        ent.hass.set(SCHEDULE, helper)
        ent.hass.set(WINDOW, "off")
        ent.hass.set(TRV, "heat", {"temperature": 20.0})
        last = _State("heat", {"preset_mode": "boost", "temperature": 25.0})
        _run(_start(ent, last_state=last, extra=_Extra(persisted.as_dict())))
        drain_tasks(ent)
        return ent

    def test_a_2_27_3_save_is_aligned_to_the_boost(self):
        """Saved by 2.27.3: the 15-min Boost pressed 5 min ago, its override
        ending 15 min after the Boost."""
        now = time.time()
        p = _ctrl.PersistedAutomationState(
            boost_end_ts=now + 600, boost_saved=_ctrl.SavedState("comfort", 20.5),
            schedule_preset="comfort", schedule_override_active=True,
            schedule_override_until=now + 1500,
        )
        ent = self._boot(p)
        assert ent.preset_mode == "boost"
        assert ent._schedule_ctrl.override_until == ent._boost_end_ts
        assert ent.schedule_override_remaining_minutes == 10
        assert [round(e["delay"]) for e in _timers("_async_schedule_override_expired")] == [600]
        assert "Startup: the schedule override now ends with the Boost" in _lines(ent)

    def test_a_2_27_3_save_with_a_shorter_override_lasts_the_boost(self):
        now = time.time()
        p = _ctrl.PersistedAutomationState(
            boost_end_ts=now + 2400, boost_saved=_ctrl.SavedState("comfort", 20.5),
            schedule_preset="comfort", schedule_override_active=True,
            schedule_override_until=now + 600,
        )
        ent = self._boot(p, boost_min=60)
        assert ent._schedule_ctrl.override_until == ent._boost_end_ts
        assert [round(e["delay"]) for e in _timers("_async_schedule_override_expired")] == [2400]

    def test_over_while_down_takes_its_override_with_it(self):
        now = time.time()
        p = _ctrl.PersistedAutomationState(
            boost_end_ts=now - 60, boost_saved=_ctrl.SavedState("eco", 18.0),
            schedule_preset="comfort", schedule_override_active=True,
            schedule_override_until=now + 900,   # 2.27.3: the override outlived it
        )
        ent = self._boot(p)
        # As the Boost's own end: back to the schedule, override over.
        assert ent.preset_mode == "comfort"
        assert ent.extra_state_attributes["schedule_override_active"] is False
        assert _timers("_async_schedule_override_expired") == []
        assert "Startup: boost ended while HA was down, reverting to the schedule" in _lines(ent)


def _window(ent, state):
    ent.hass.set(WINDOW, state)
    ent._async_window_changed(_event(state))
    drain_tasks(ent)


@e2e
class TestWindow:
    def test_boost_with_the_window_open_overrides_it_and_keeps_one_end(self):
        ent = _entity("night")
        _window(ent, "on")
        _fire(ent, "_async_window_action")
        assert ent.preset_mode == "frost_protection"
        # The window holds the room: what a Boost would end on is the
        # schedule's (it has one), whatever the window saved.
        assert ent.extra_state_attributes["boost_returns_to"] == "schedule"
        _preset(ent, "boost")
        attrs = ent.extra_state_attributes
        assert ent.preset_mode == "boost" and attrs["window_overridden"] is True
        assert attrs["schedule_override_until"] == attrs["boost_until"]
        _window(ent, "off")
        assert ent.preset_mode == "boost"
        _fire(ent, "_async_boost_expired")
        assert ent.preset_mode == "eco"
        assert ent.extra_state_attributes["schedule_override_active"] is False

    def test_a_window_ending_the_boost_leaves_its_override_to_the_boosts_end(self):
        """The window ends the Boost and saves the preset from before it;
        the Boost's override still ends when the Boost would have, and the
        schedule then is what the close restores."""
        ent = _entity("night")
        _preset(ent, "comfort")
        _preset(ent, "boost")
        _window(ent, "on")
        _fire(ent, "_async_window_action")
        attrs = ent.extra_state_attributes
        assert ent.preset_mode == "frost_protection"
        assert attrs["boost_until"] is None
        assert _timers("_async_boost_expired") == []
        assert ent._window_ctrl.get_saved().preset == "comfort"
        assert [e["delay"] for e in _timers("_async_schedule_override_expired")] == [900]
        _fire(ent, "_async_schedule_override_expired")
        assert ent.preset_mode == "frost_protection"
        assert ent._window_ctrl.get_saved().preset == "eco"
        assert "Schedule override expired" in _lines(ent)
        _window(ent, "off")
        assert ent.preset_mode == "eco"


@e2e
class TestTheScheduleEndsABoost:
    """The schedule moving on ends a running Boost, before its own end
    (owner, 22 Nov 2026: keep it so).  Roomstat cannot know when the
    schedule will next change (an ``input_select``'s changes are events, it
    has no "next change" to read), so the card counts down the Boost's own
    end (``boost_until``); when the schedule ends it early the thermostat
    says so at once: the preset is the schedule's, ``boost_until`` is
    cleared, and the activity log names it.  20 Nov 2026, second bedroom:
    a Boost restarted at 19:39:52 for 30 min, "Schedule switching to eco"
    at 20:00:00 with 10 min left, and nothing said it had ended the Boost."""

    def test_the_20_nov_boost(self):
        ent = _entity("comfort", override_min=30, boost_min=30)
        _preset(ent, "boost")
        ent._boost_end_ts -= 28 * 60          # 28 min in: 2 min left
        ent._schedule_ctrl.override_until = ent._boost_end_ts
        _preset(ent, "boost")                 # 19:39:52, restarted for 30 min
        assert "Boost restarted for 30 min (2 min were left)" in _lines(ent)
        ent._boost_end_ts -= 20 * 60 + 8      # 20:00:00
        ent._schedule_ctrl.override_until = ent._boost_end_ts
        _schedule(ent, "night")
        attrs = ent.extra_state_attributes
        assert ent.preset_mode == "eco"
        assert attrs["boost_until"] is None
        assert attrs["schedule_preset"] == "eco"
        assert attrs["schedule_override_active"] is False
        assert attrs["boost_returns_to"] == "schedule"
        assert _timers("_async_boost_expired") == []
        assert _timers("_async_schedule_override_expired") == []
        lines = _lines(ent)
        at = lines.index("Schedule switching to eco")
        assert lines[at + 1] == "Boost ended by the schedule (10 min were left)"
        assert lines[at + 2] == "Preset Boost → Night"

    def test_not_when_the_boost_ends_by_itself(self):
        ent = _entity("comfort")
        _preset(ent, "boost")
        _fire(ent, "_async_boost_expired")
        assert not any(line.startswith("Boost ended by the schedule") for line in _lines(ent))

    def test_not_when_a_preset_is_picked_by_hand(self):
        ent = _entity("comfort")
        _preset(ent, "boost")
        _preset(ent, "eco")
        assert ent.preset_mode == "eco"
        assert not any(line.startswith("Boost ended by the schedule") for line in _lines(ent))

    def test_not_while_the_window_holds_the_room(self):
        """The window ended the Boost first; the schedule then only changes
        what the close restores."""
        ent = _entity("comfort")
        _preset(ent, "boost")
        _window(ent, "on")
        _fire(ent, "_async_window_action")
        _schedule(ent, "night")
        assert ent.preset_mode == "frost_protection"
        assert not any(line.startswith("Boost ended by the schedule") for line in _lines(ent))
