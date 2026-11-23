"""Someone counted home while the home delay runs (2.27.6), through the entity.

23 Nov 2026, 16:56: the presence sensor came back on at 16:56:18 and the
rooms left away mode about 30 s later (the home delay).  Meanwhile every card
read "Nobody home · Someon…" (preheat's "Someone close", already heating to
the restore target), though someone had just been counted home.  The owner
kept the delay; only the words change.  Under test:
- the thermostat says the delay runs (``presence_arriving``,
  ``presence_home_at``, ``presence_returns_to`` / ``_c``), from the "on"
  until the restore, and writes its state when it starts and ends;
- a flicker back to not-home within the delay clears it, nothing restored;
- a change by hand while nobody was home: it stays, so nothing "returns";
  an Off picked by hand: the schedule's preset comes back;
- a reload inside the delay restores at once (as before), nothing arriving;
- summer mode and a window: what wins as ever;
- the activity log: an event line at the start, the decision line's label
  ("Away, someone home: back to Comfort at 16:56"), a new line for it;
- preheat in mode on with someone close (the owner's case);
- nothing sent changes: the same arrival with the display path switched off
  sends the same commands at the same setpoints.
"""
from __future__ import annotations

import datetime
import sys

import pytest

import tests.test_preheat_entity as pe
from tests.ha_harness import PRESENCE, SCHEDULE, SUMMER, WINDOW, _event, _run, drain_tasks, e2e
from tests.ha_harness import _settle as settle

pytestmark = e2e

MIN = 60.0
COMFORT = 20.5   # the harness's Comfort
NIGHT = 18.5


@pytest.fixture(autouse=True)
def _reset():
    pe.reset_timers()
    yield
    pe.reset_timers()


def _attrs(ent):
    return ent.extra_state_attributes


def _counted_home(ent):
    """The presence sensor reads home again: the home delay starts."""
    ent.hass.set(PRESENCE, "on")
    ent._async_presence_changed(_event("on"))


def _home_timers():
    return pe._timers("_async_presence_home_action")


def _events(ent):
    return [e.text for e in reversed(ent._activity.entries())]


def _arriving_attrs(ent):
    a = _attrs(ent)
    return {k: a[k] for k in ("presence_arriving", "presence_home_at",
                              "presence_returns_to", "presence_returns_to_c")}


_NONE = {"presence_arriving": False, "presence_home_at": None,
         "presence_returns_to": None, "presence_returns_to_c": None}


class TestFromAwayMode:
    def test_arriving_then_restored(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = pe._entity(mode="off")
        pe._leave(ent)
        assert _arriving_attrs(ent) == _NONE
        writes = getattr(ent, "state_writes", 0)
        _counted_home(ent)
        assert ent.state_writes == writes + 1   # the card hears of it at once
        assert _arriving_attrs(ent) == {
            "presence_arriving": True,
            "presence_home_at": datetime.datetime.fromtimestamp(
                clock.now + 30, tz=datetime.UTC).isoformat(),
            "presence_returns_to": "comfort",
            "presence_returns_to_c": COMFORT,
        }
        # Still away mode: the delay is untouched.
        assert ent.preset_mode == "away"
        assert _attrs(ent)["presence_away_active"] is True
        assert [t["delay"] for t in _home_timers()] == [30]
        assert _events(ent)[-1] == (
            "Someone is home: back to Comfort after the home delay (30 s)"
        )
        clock.tick(30)
        pe._fire(ent, "_async_presence_home_action")
        assert ent.preset_mode == "comfort"
        assert _arriving_attrs(ent) == _NONE

    def test_the_schedule_moving_on_in_the_delay_is_what_comes_back(self, monkeypatch):
        pe._Clock(monkeypatch)
        ent = pe._entity(mode="off")
        pe._leave(ent)
        _counted_home(ent)
        ent.hass.set(SCHEDULE, "eco")
        ent._async_schedule_changed(_event("eco"))
        drain_tasks(ent)
        assert _attrs(ent)["presence_returns_to"] == "eco"
        assert _attrs(ent)["presence_returns_to_c"] == NIGHT
        pe._fire(ent, "_async_presence_home_action")
        assert ent.preset_mode == "eco"

    def test_a_repeated_on_restarts_the_delay_as_before_and_logs_once(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = pe._entity(mode="off")
        pe._leave(ent)
        _counted_home(ent)
        clock.tick(10)
        _counted_home(ent)
        assert len(_home_timers()) == 1
        home_at = datetime.datetime.fromisoformat(_attrs(ent)["presence_home_at"])
        assert home_at.timestamp() == clock.now + 30
        assert sum(t.startswith("Someone is home:") for t in _events(ent)) == 1

    def test_a_flicker_back_clears_it_with_nothing_restored(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = pe._entity(mode="off")
        pe._leave(ent)
        _counted_home(ent)
        writes = ent.state_writes
        clock.tick(10)
        ent.hass.set(PRESENCE, "off")
        ent._async_presence_changed(_event("off"))
        assert ent.state_writes == writes + 1
        assert _arriving_attrs(ent) == _NONE
        assert _home_timers() == []
        assert ent.preset_mode == "away" and _attrs(ent)["presence_away_active"]
        assert _events(ent)[-1] == (
            "Nobody home again within the home delay: nothing restored, the room "
            "stays as it was"
        )

    def test_a_home_action_that_finds_the_sensor_off_clears_it(self, monkeypatch):
        pe._Clock(monkeypatch)
        ent = pe._entity(mode="off")
        pe._leave(ent)
        _counted_home(ent)
        ent.hass.set(PRESENCE, "off")       # no event reached the room
        writes = ent.state_writes
        pe._fire(ent, "_async_presence_home_action")
        assert ent.preset_mode == "away"
        assert _arriving_attrs(ent) == _NONE
        assert ent.state_writes == writes + 1

    def test_a_reload_inside_the_delay_restores_at_once_as_before(self, monkeypatch):
        pe._Clock(monkeypatch)
        ent = pe._entity(mode="off")
        pe._leave(ent)
        _counted_home(ent)
        assert _attrs(ent)["presence_arriving"] is True
        _run(ent.async_will_remove_from_hass())    # what a reload does first
        new = pe._entity(mode="off", start=False)
        new.hass = ent.hass
        new.coordinator.data = ent.coordinator.data
        new.coordinator.preheat_entity.hass = ent.hass
        pe._restart_from(ent, new)
        drain_tasks(new)
        # The old room's timers went with it; the new one restored at start.
        assert ent._presence_ctrl.home_at is None
        assert new.preset_mode == "comfort"
        assert not new._presence_ctrl.nobody_home
        assert _arriving_attrs(new) == _NONE
        assert _home_timers() == []

    def test_summer_mode_ends_it(self, monkeypatch):
        pe._Clock(monkeypatch)
        ent = pe._entity(mode="off", options={"summer_mode_entity_id": SUMMER})
        pe._leave(ent)
        _counted_home(ent)
        ent.hass.set(SUMMER, "on")
        ent._async_summer_changed(_event("on"))
        drain_tasks(ent)
        assert _arriving_attrs(ent) == _NONE
        assert _home_timers() == []

    def test_a_window_holds_the_room_nothing_returns_now(self, monkeypatch):
        pe._Clock(monkeypatch)
        ent = pe._entity(mode="off")
        pe._leave(ent)
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        pe._fire(ent, "_async_window_action")
        _counted_home(ent)
        a = _attrs(ent)
        assert a["presence_arriving"] is True and a["presence_returns_to"] is None
        assert "Someone is home: away ends (the window's close restores the room)" in (
            _events(ent)[-1])
        # The decision line is the window's, as ever.
        pe._cycle(ent)
        line = _attrs(ent)["activity"]
        assert "→ 5.0 window |" in line and "someone home" not in line


class TestAfterAChangeByHand:
    def test_the_change_stays_nothing_returns(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = pe._entity(mode="off")
        pe._leave(ent)
        pe._preset(ent, "eco")
        assert _attrs(ent)["presence_override_active"] is True
        _counted_home(ent)
        a = _attrs(ent)
        assert a["presence_arriving"] is True
        assert a["presence_returns_to"] is None and a["presence_returns_to_c"] is None
        assert _events(ent)[-1] == (
            "Someone is home: the change by hand stays after the home delay (30 s)"
        )
        clock.tick(MIN)
        pe._feed(ent, 18.4, setpoint=18.0)
        pe._cycle(ent)
        assert "Night, someone home: the change by hand stays |" in _attrs(ent)["activity"]
        writes = ent.state_writes
        pe._fire(ent, "_async_presence_home_action")
        assert ent.preset_mode == "eco"
        assert _attrs(ent)["presence_override_active"] is False
        assert _arriving_attrs(ent) == _NONE
        assert ent.state_writes > writes

    def test_an_off_by_hand_ends_and_the_schedule_comes_back(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = pe._entity(mode="off")
        pe._leave(ent)
        pe._preset(ent, "frost_protection")
        assert ent._presence_ctrl.off_until_home
        _counted_home(ent)
        a = _attrs(ent)
        assert a["presence_returns_to"] == "comfort"
        assert a["presence_returns_to_c"] == COMFORT
        clock.tick(MIN)
        pe._feed(ent, 18.4)
        pe._cycle(ent)
        assert "Frost protection, someone home: back to Comfort at " in (
            _attrs(ent)["activity"])
        pe._fire(ent, "_async_presence_home_action")
        assert ent.preset_mode == "comfort"
        assert _arriving_attrs(ent) == _NONE


class TestTheActivityLine:
    def test_a_new_decision_line_names_it(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = pe._entity(mode="off")
        pe._leave(ent)
        clock.tick(MIN)
        pe._feed(ent, 18.4)
        pe._cycle(ent)
        before = ent._activity.entries()[0].text
        assert "Away, nobody home |" in before
        _counted_home(ent)
        clock.tick(20)
        pe._cycle(ent)
        line = ent._activity.entries()[0].text
        hhmm = sys.modules["roomstat_e2e.climate_activity"].local_hhmm(clock.now + 10)
        assert f"→ 16.0 Away, someone home: back to Comfort at {hhmm} |" in line
        assert "nobody home" not in line
        pe._fire(ent, "_async_presence_home_action")
        clock.tick(MIN)
        pe._cycle(ent)
        assert "→ 20.5 Comfort |" in ent._activity.entries()[0].text


class TestPreheatOnSomeoneClose:
    """The owner's case: preheat had the room at the restore target."""

    def test_the_label_and_attributes_say_someone_is_home(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = pe._entity(mode="on")
        pe._leave(ent)                       # close stays on
        assert pe._state(ent) == "close"
        assert _attrs(ent)["preheat_heating_to_c"] == COMFORT
        _counted_home(ent)
        a = _attrs(ent)
        assert a["presence_arriving"] is True
        assert (a["presence_returns_to"], a["presence_returns_to_c"]) == ("comfort", COMFORT)
        # Preheat's decision and target are untouched ...
        assert pe._state(ent) == "close"
        assert ent._effective_setpoint() == COMFORT
        clock.tick(20)
        pe._feed(ent, 20.3, setpoint=20.0)
        pe._cycle(ent)
        line = _attrs(ent)["activity"]
        # ... but the line no longer reads "nobody home, preheat: someone close".
        assert "→ 20.5 Away, someone home: back to Comfort at " in line
        assert "nobody home" not in line
        pe._fire(ent, "_async_presence_home_action")
        assert ent.preset_mode == "comfort"
        assert _arriving_attrs(ent) == _NONE


def _held(ent):
    """What the valve holds: the last setpoint sent, None after an off."""
    for svc, temp, _mode in reversed(pe._commands(ent)):
        if svc == "set_temperature":
            return temp
        if svc == "set_hvac_mode":
            return None
    return None


def _arrival_run(monkeypatch, *, display: bool):
    """Heading home with preheat on, a heat-up, counted home, two cycles in a
    longer (90-s) home delay, the restore, and on: every command sent and
    each cycle's setpoint."""
    pe.reset_timers()
    clock = pe._Clock(monkeypatch)
    ent = pe._entity(mode="on", options={"presence_home_delay_s": 90})
    if not display:
        ctrl_cls = type(ent._presence_ctrl)
        monkeypatch.setattr(ctrl_cls, "arriving", property(lambda self: False))
        monkeypatch.setattr(ent, "_presence_arrival_changed",
                            lambda *a, **k: None)
    pe._leave(ent)
    pe._set(ent, pe.CLOSE, "off")
    pe._set(ent, pe.NEARBY, "off")
    setpoints = []
    room = 18.0
    for _ in range(11):
        clock.tick(MIN)
        pe._feed(ent, room)
        pe._cycle(ent)
    pe._set(ent, pe.ARRIVAL, pe._iso(clock.now + 40 * MIN))
    drain_tasks(ent)
    settle(clock)
    drain_tasks(ent)
    def step(seconds, rise):
        nonlocal room
        clock.tick(seconds)
        room += rise
        pe._feed(ent, round(room, 2), setpoint=_held(ent))
        pe._cycle(ent)
        setpoints.append(ent._effective_setpoint())

    for minute in range(40):
        step(MIN, 0.06)
        if minute == 30:
            pe._set(ent, pe.CLOSE, "on")
    _counted_home(ent)
    step(40, 0.02)
    step(40, 0.02)
    pe._fire(ent, "_async_presence_home_action")
    for _ in range(10):
        step(MIN, -0.02)
    pe.reset_timers()
    return pe._commands(ent), setpoints, ent.preset_mode


def test_the_display_changes_nothing_sent(monkeypatch):
    """The same arrival with the new attributes, event lines and label
    switched off (the controller never says arriving): the same commands,
    the same setpoint every cycle, the same restore."""
    shown = _arrival_run(monkeypatch, display=True)
    with monkeypatch.context() as m:
        hidden = _arrival_run(m, display=False)
    assert shown == hidden
    commands, setpoints, preset = shown
    assert any(c[0] == "set_temperature" for c in commands)
    assert preset == "comfort"
    assert COMFORT in setpoints
