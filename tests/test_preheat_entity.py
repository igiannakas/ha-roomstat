"""Preheat on the way home through the real climate entity (tests/ha_harness.py).

Shadow only (2.11.0): the decision is worked out, shown and logged, and the
room stays at Away.  Under test:
- shadow sends exactly the commands preheat off sends;
- an outing: close, nearby, Away, heading home, home, each one log line;
- Away or Resume schedule while nobody is home is back to automatic; an Off
  picked by hand while out, or on when everyone left, stands preheat aside
  until someone is home (2.12.0; until 2.11.0 an Away held the room cold);
- the target coming home would restore follows the schedule, as the restore
  itself does;
- a restart or reload keeps the heading-home start, an Off until home, the
  30-minute recovery and the decision last logged;
- the 30-minute recovery is learned from everyday heat-ups;
- a failure in preheat never stops the regulation.
"""
from __future__ import annotations

import datetime
import logging

import pytest

from tests.ha_harness import (
    PRESENCE,
    SCHEDULE,
    TRV,
    WINDOW,
    _active_timers,
    _climate,
    _event,
    _make_entity,
    _restart_from,
    _run,
    _sensor,
    _start,
    drain_tasks,
    e2e,
    reset_timers,
)

pytestmark = e2e

ARRIVAL = "sensor.home_eta_expected_home"
CLOSE = "binary_sensor.home_eta_someone_close_to_home"
NEARBY = "binary_sensor.home_eta_someone_nearby"
MIN = 60.0


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


def _feed(ent, room, *, internal=None, setpoint=None):
    ent.coordinator.data = {
        "room_temp": room, "room_temp_ts": None,
        "tado_internal_temp": (room + 2.0) if internal is None else internal,
        "tado_setpoint": setpoint, "tado_min_temp": 5.0, "tado_max_temp": 25.0,
        "tado_off": setpoint is None, "tado_cloud": False, "tado_scheduled": False,
        "tado_heating": False,
        "fallback_internal_temp": None, "fallback_setpoint": None,
    }


def _entity(*, mode="shadow", options=None, schedule="comfort", start=True):
    opts = {
        "presence_away_delay_s": 60, "presence_home_delay_s": 30,
        "schedule_override_min": 60, "away_target": 16.0,
        "preheat_mode": mode,
        "preheat_arrival_entity_id": ARRIVAL,
        "preheat_close_entity_id": CLOSE,
        "preheat_nearby_entity_id": NEARBY,
        **(options or {}),
    }
    ent = _make_entity(opts, window=True, presence=True, schedule=True)
    ent.hass.keep_tasks = True
    for entity_id, state in (
        (SCHEDULE, schedule), (PRESENCE, "on"), (WINDOW, "off"),
        (ARRIVAL, "unknown"), (CLOSE, "on"), (NEARBY, "on"),
    ):
        ent.hass.set(entity_id, state)
    ent.hass.set(TRV, "heat", {"temperature": 20.0})
    _feed(ent, 20.4, setpoint=20.0)
    ent.coordinator.climate_entity = ent
    (desc,) = [d for d in _sensor.TUNING_SENSORS if d.key == "preheat"]
    sensor = _sensor.RoomstatTuningSensor(ent.coordinator, ent._config_entry, desc)
    sensor.hass = ent.hass
    ent.coordinator.preheat_entity = sensor
    if start:
        _run(_start(ent))
        drain_tasks(ent)
    return ent


def _sensor_of(ent):
    return ent.coordinator.preheat_entity


def _cycle(ent):
    _run(ent._async_regulation_cycle("timer"))
    drain_tasks(ent)


def _timers(name):
    return [c for c in _active_timers() if getattr(c["callback"], "__name__", "") == name]


def _fire(ent, name):
    (entry,) = _timers(name)
    entry["cancelled"] = True
    _run(entry["callback"](None))
    drain_tasks(ent)


def _set(ent, entity_id, state):
    """A Home ETA entity changes (its state listener runs at once)."""
    ent.hass.set(entity_id, state)
    ent._async_preheat_input_changed(_event(state))


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, tz=datetime.UTC).isoformat()


def _leave(ent):
    ent.hass.set(PRESENCE, "off")
    ent._async_presence_changed(_event("off"))
    _fire(ent, "_async_presence_away_action")
    assert ent.preset_mode == "away"


def _arrive(ent):
    ent.hass.set(PRESENCE, "on")
    ent._async_presence_changed(_event("on"))
    _fire(ent, "_async_presence_home_action")


def _preset(ent, preset):
    _run(ent.async_set_preset_mode(preset))
    drain_tasks(ent)


def _lines(ent):
    """The preheat lines of the activity log, oldest first."""
    return [
        e.text for e in reversed(ent._activity.entries())
        if e.text.startswith("Preheat")
    ]


def _state(ent):
    return ent.extra_state_attributes["preheat_state"]


def _commands(ent):
    return [(svc, d.get("temperature"), d.get("hvac_mode"))
            for dom, svc, d in ent.hass.service_calls if dom == "climate"]


def _outing(ent, clock):
    """Leave close to home, walk off past 800 m and 3 km, head home, arrive."""
    _cycle(ent)
    clock.tick(30)
    _leave(ent)
    for minute in range(5):
        clock.tick(MIN)
        _feed(ent, 20.2 - 0.05 * minute)
        _cycle(ent)
    _set(ent, CLOSE, "off")
    for minute in range(15):
        clock.tick(MIN)
        _feed(ent, 19.9 - 0.02 * minute)
        _cycle(ent)
    _set(ent, NEARBY, "off")
    for minute in range(60):
        clock.tick(MIN)
        _feed(ent, 19.6 - 0.02 * minute)
        _cycle(ent)
    _set(ent, ARRIVAL, _iso(clock.now + 30 * MIN))
    for minute in range(25):
        clock.tick(MIN)
        _feed(ent, 18.4)
        _cycle(ent)
    _set(ent, NEARBY, "on")
    _set(ent, CLOSE, "on")
    clock.tick(MIN)
    _cycle(ent)
    _arrive(ent)
    clock.tick(MIN)
    _feed(ent, 18.4)
    _cycle(ent)


# ===========================================================================
# Shadow: nothing changes for the valve
# ===========================================================================

class TestShadow:
    def test_shadow_sends_exactly_what_off_sends(self, monkeypatch):
        runs = {}
        for mode in ("off", "shadow"):
            reset_timers()
            clock = _Clock(monkeypatch)
            ent = _entity(mode=mode)
            _outing(ent, clock)
            runs[mode] = (_commands(ent), ent.target_temperature)
        assert runs["shadow"] == runs["off"]
        # And it did work things out meanwhile.
        assert len(_lines(ent)) >= 5

    def test_the_room_stays_at_away_while_it_would_preheat(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _leave(ent)
        _cycle(ent)
        assert _state(ent) == "close"
        assert ent.extra_state_attributes["preheat_target_c"] == 20.5
        assert ent.target_temperature == 16.0
        assert ent._effective_setpoint() == 16.0
        clock.tick(MIN)

    def test_off_decides_nothing(self, monkeypatch):
        _Clock(monkeypatch)
        ent = _entity(mode="off")
        assert ent._preheat_entity_ids() == []
        _leave(ent)
        _cycle(ent)
        assert _state(ent) == "off"
        assert _lines(ent) == []
        assert _sensor_of(ent).native_value == "off"
        assert _sensor_of(ent).extra_state_attributes["mode"] == "off"

    def test_listens_to_the_three_entities(self, monkeypatch):
        _Clock(monkeypatch)
        ent = _entity()
        assert ent._preheat_entity_ids() == [ARRIVAL, CLOSE, NEARBY]


# ===========================================================================
# An outing, line by line
# ===========================================================================

class TestOuting:
    def test_each_change_of_decision_is_one_line(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _outing(ent, clock)
        lines = _lines(ent)
        assert lines[0] == (
            "Preheat (shadow): someone is close to home: it would heat to 20.5 °C, "
            "the temperature coming home restores"
        )
        assert lines[1] == (
            "Preheat (shadow): someone is nearby: it would heat to 20.0 °C (20.5 "
            "less 0.50 °C, the default until it has learned what it recovers in "
            "30 min)"
        )
        assert lines[2] == "Preheat (shadow): nobody is nearby any more: back to Away 16.0 °C"
        assert lines[3].startswith(
            "Preheat (shadow): heading home: it would heat to 20.5 °C now for the "
            "expected arrival at "
        )
        assert lines[3].endswith(
            # The room at 18.42 when the arrival came: 20 + 2.08 / 0.8 h + 5.
            "from 18.4 °C it needs 181 min (start-up 20 min (default), 2.1 °C at "
            "0.8 × 1.0 °C/h (default), lead 5 min)"
        )
        assert lines[4] == "Preheat (shadow): stands aside: someone is home"
        assert len(lines) == 5
        assert _state(ent) == "inactive"

    def test_the_drop_from_close_waits_ten_minutes(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _leave(ent)
        _set(ent, CLOSE, "off")
        start = clock.now
        assert _state(ent) == "close"
        assert ent._preheat_decision.level_drop_ts == start + 10 * MIN
        clock.tick(10 * MIN - 1)
        _cycle(ent)
        assert _state(ent) == "close"
        clock.tick(1)
        _cycle(ent)
        assert _state(ent) == "nearby"

    def test_the_sensor_changes_only_with_a_decision(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        sensor = _sensor_of(ent)
        _cycle(ent)
        assert sensor.native_value == "inactive"
        _leave(ent)
        writes = sensor.state_writes
        attrs = dict(sensor.extra_state_attributes)
        assert sensor.native_value == "close"
        assert attrs["target_c"] == 20.5 and attrs["mode"] == "shadow"
        assert attrs["decided_at"] == _iso(clock.now)
        assert attrs["level"] == "close" and attrs["room_c"] == 20.4
        for _ in range(5):
            clock.tick(MIN)
            _feed(ent, 20.0)
            _cycle(ent)
        assert sensor.state_writes == writes
        assert sensor.extra_state_attributes == attrs   # fixed until the next one

    def test_the_thermostat_shows_the_live_numbers(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(options={"autotune_enabled": True})
        monkeypatch.setattr(ent._autotuner, "heatup_model", lambda: (2.5, 20 * MIN))
        _leave(ent)
        _set(ent, CLOSE, "off")
        _set(ent, NEARBY, "off")
        clock.tick(11 * MIN)
        _feed(ent, 16.5)
        arrival = clock.now + 3 * 3600
        _set(ent, ARRIVAL, _iso(arrival))
        attrs = ent.extra_state_attributes
        assert attrs["preheat_state"] == "waiting"
        # 20 min + 4.0 °C / (0.8 × 2.5 °C/h) + 5 min = 145 min
        assert attrs["preheat_needed_min"] == 145.0
        assert attrs["preheat_due_at"] == _iso(arrival - 145 * MIN)
        assert attrs["preheat_arrival_at"] == _iso(arrival)
        assert attrs["preheat_recovery_temp_c"] == 20.0
        assert _lines(ent)[-1].endswith(
            f"it would start at {_climate_hhmm(arrival - 145 * MIN, clock.now)}"
        )

    def test_a_window_open_stands_it_aside(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _leave(ent)
        ent.hass.set(WINDOW, "on")
        clock.tick(MIN)
        _cycle(ent)
        assert _state(ent) == "inactive"
        assert ent._preheat_decision.inactive_reason == "window"
        assert _lines(ent)[-1] == "Preheat (shadow): stands aside: a window is open"

    def test_a_schedule_at_away_has_nothing_to_preheat(self, monkeypatch):
        _Clock(monkeypatch)
        ent = _entity(schedule="away")
        _leave(ent)
        _cycle(ent)
        assert _state(ent) == "inactive"
        assert ent._preheat_decision.inactive_reason == "nothing_warmer"


def _climate_hhmm(ts, now):
    return __import__("roomstat_e2e.climate_activity", fromlist=["x"]).local_hhmm(ts, now)


# ===========================================================================
# What coming home would restore
# ===========================================================================

class TestScheduled:
    def test_follows_the_schedule_while_away(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(schedule="night")
        _leave(ent)
        _cycle(ent)
        assert ent.extra_state_attributes["preheat_target_c"] == 18.5
        ent.hass.set(SCHEDULE, "comfort")
        ent._async_schedule_changed(_event("comfort"))
        drain_tasks(ent)
        clock.tick(MIN)
        _cycle(ent)
        assert ent.extra_state_attributes["preheat_target_c"] == 20.5
        assert ent.target_temperature == 16.0
        _arrive(ent)
        assert ent._effective_setpoint() == 20.5

    @pytest.mark.parametrize(("preset", "temp", "expected"), [
        ("comfort", None, 20.5), ("eco", 18.5, 18.5), ("none", 21.0, 21.0),
        ("boost", 22.0, 20.5),          # a Boost comes back as the schedule's preset
        ("away", 16.0, None), ("frost_protection", 5.0, None),
    ])
    def test_is_what_the_restore_restores(self, monkeypatch, preset, temp, expected):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _leave(ent)
        ent._presence_ctrl.update_saved(preset, temp)
        _cycle(ent)
        if expected is None:
            assert _state(ent) == "inactive"
        else:
            assert ent.extra_state_attributes["preheat_target_c"] == expected
        clock.tick(MIN)
        _arrive(ent)
        if expected is not None:
            assert ent._effective_setpoint() == expected


# ===========================================================================
# Changes by hand while nobody is home (2.12.0, owner, 27 Oct 2026): Away and
# Resume schedule are back to automatic; an Off holds the room until someone
# is home
# ===========================================================================

class TestByHandWhileOut:
    def _out(self, monkeypatch, **opts):
        clock = _Clock(monkeypatch)
        ent = _entity(**opts)
        _leave(ent)
        _cycle(ent)
        assert _state(ent) == "close"
        clock.tick(MIN)
        return ent, clock

    @pytest.mark.parametrize("back", ["away", "schedule"])
    def test_away_or_resume_while_out_is_automatic(self, monkeypatch, back):
        ent, clock = self._out(monkeypatch)
        _preset(ent, "comfort")                    # a change by hand
        assert _state(ent) == "inactive"
        assert _lines(ent)[-1] == "Preheat (shadow): stands aside: a change by hand is in charge"
        clock.tick(MIN)
        _preset(ent, back)
        assert ent.preset_mode == "away" and ent._presence_ctrl.is_active
        assert not ent._presence_ctrl.override_active
        assert _state(ent) == "close"
        # Nothing holds it: an arrival due now is preheated at once.
        _set(ent, CLOSE, "off")
        _set(ent, NEARBY, "off")
        _feed(ent, 18.0)
        clock.tick(11 * MIN)
        _set(ent, ARRIVAL, _iso(clock.now + 20 * MIN))
        assert _state(ent) == "due"

    @pytest.mark.parametrize("back", ["away", "schedule"])
    def test_already_in_away_mode_it_stays_automatic(self, monkeypatch, back):
        ent, clock = self._out(monkeypatch)
        _preset(ent, back)
        assert ent.preset_mode == "away" and _state(ent) == "close"
        assert ent._presence_ctrl.get_saved().preset == "comfort"
        assert "held_cold" not in ent.extra_state_attributes.get("preheat_state", "")

    def test_an_off_while_out_stands_it_aside_until_home(self, monkeypatch):
        ent, clock = self._out(monkeypatch)
        _preset(ent, "frost_protection")
        assert ent._presence_ctrl.off_until_home
        assert _state(ent) == "inactive"
        assert ent.extra_state_attributes["preheat_inactive_reason"] == "off_by_hand"
        assert _lines(ent)[-1] == (
            "Preheat (shadow): stands aside: an Off picked by hand holds the room "
            "until someone is home"
        )
        # Home ETA can say what it likes, and no limit ends it.
        _set(ent, ARRIVAL, _iso(clock.now + 5 * MIN))
        clock.tick(24 * 3600)
        _cycle(ent)
        assert _state(ent) == "inactive" and ent.preset_mode == "frost_protection"
        # Away is back to automatic: due at once.
        _set(ent, ARRIVAL, _iso(clock.now + 5 * MIN))
        _preset(ent, "away")
        assert _state(ent) == "due"

    def test_an_off_on_when_everyone_left_stands_it_aside(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _preset(ent, "frost_protection")           # 08:48, at home
        clock.tick(MIN)
        ent.hass.set(PRESENCE, "off")
        ent._async_presence_changed(_event("off"))
        _fire(ent, "_async_presence_away_action")  # everyone left
        assert ent.preset_mode == "frost_protection"
        _cycle(ent)
        assert ent.extra_state_attributes["preheat_inactive_reason"] == "off_by_hand"
        # Resume schedule from the card: automatic.
        _preset(ent, "schedule")
        assert ent.preset_mode == "away" and _state(ent) == "close"

    def test_an_away_picked_at_home_is_not_carried_into_the_absence(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _preset(ent, "away")
        clock.tick(MIN)
        _leave(ent)
        _cycle(ent)
        assert _state(ent) == "close"

    def test_an_off_during_the_home_delay_is_an_ordinary_off(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _leave(ent)
        ent.hass.set(PRESENCE, "on")
        ent._async_presence_changed(_event("on"))
        clock.tick(10)
        _preset(ent, "frost_protection")
        assert not ent._presence_ctrl.off_until_home
        _fire(ent, "_async_presence_home_action")
        assert ent.preset_mode == "frost_protection"   # picked by someone home


# ===========================================================================
# Restart and reload
# ===========================================================================

class TestRestart:
    def _again(self, old):
        new = _entity(start=False)
        new.hass = old.hass
        new.coordinator.data = old.coordinator.data
        new.coordinator.preheat_entity.hass = old.hass
        _restart_from(old, new)
        drain_tasks(new)
        return new

    def test_a_heading_home_start_carries_on(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _leave(ent)
        _set(ent, CLOSE, "off")
        _set(ent, NEARBY, "off")
        clock.tick(11 * MIN)
        _cycle(ent)
        _feed(ent, 18.0)
        _set(ent, ARRIVAL, _iso(clock.now + 20 * MIN))
        started = clock.now
        assert _state(ent) == "due"
        lines = len(_lines(ent))
        clock.tick(MIN)
        new = self._again(ent)
        clock.tick(MIN)
        _cycle(new)
        assert _state(new) == "due"
        assert new._preheat_decision.since_ts == started
        # The same decision: no second line after the reload.
        assert len(_lines(new)) == lines
        assert _sensor_of(new).native_value == "due"

    def test_an_off_until_home_carries_on(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _leave(ent)
        _cycle(ent)
        _preset(ent, "frost_protection")
        clock.tick(MIN)
        new = self._again(ent)
        _cycle(new)
        assert new.preset_mode == "frost_protection"
        assert new._presence_ctrl.off_until_home
        assert new.extra_state_attributes["preheat_inactive_reason"] == "off_by_hand"

    def test_a_2_11_held_cold_save_is_ignored(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _leave(ent)
        _cycle(ent)
        saved = ent._preheat_as_dict()
        saved["held_cold"] = {"since": clock.now, "until": clock.now + 240 * MIN}
        saved["logged"]["state"] = "held_cold"
        ent._preheat_restore(saved, clock.now)
        assert ent._preheat_logged is None         # not a state any more
        _cycle(ent)
        assert _state(ent) == "close"

    def test_the_recovery_carries_on(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(schedule="night")
        _heat_up_at_home(ent, clock, rise_per_min=0.03)
        assert len(ent._recovery.episodes) == 1
        new = self._again(ent)
        assert len(new._recovery.episodes) == 1


# ===========================================================================
# The 30-minute recovery
# ===========================================================================

def _heat_up_at_home(ent, clock, *, rise_per_min, start=17.9):
    """Night (18.5) to Comfort (20.5) with the room at *start*, rising."""
    _feed(ent, start)
    _cycle(ent)
    ent.hass.set(SCHEDULE, "comfort")
    ent._async_schedule_changed(_event("comfort"))
    drain_tasks(ent)
    room = start
    for _ in range(31):
        clock.tick(MIN)
        room += rise_per_min
        _feed(ent, room)
        _cycle(ent)
    ent.hass.set(SCHEDULE, "night")
    ent._async_schedule_changed(_event("night"))
    drain_tasks(ent)


class TestRecovery:
    def test_learned_from_everyday_heat_ups(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(schedule="night")
        (desc,) = [d for d in _sensor.TUNING_SENSORS if d.key == "preheat_recovery"]
        sensor = _sensor.RoomstatTuningSensor(ent.coordinator, ent._config_entry, desc)
        _heat_up_at_home(ent, clock, rise_per_min=0.03)
        (episode,) = ent._recovery.episodes
        assert episode.rise_c == pytest.approx(0.9, abs=0.02)
        assert sensor.native_value is None            # one heat-up is not enough
        line = [e.text for e in ent._activity.entries() if e.text.startswith("30-min")][0]
        assert line.startswith("30-min recovery: +0.90 °C in the first 30 min after the "
                               "target rose to 20.5 (2.6 below); not enough yet to use")
        clock.tick(3600)
        _heat_up_at_home(ent, clock, rise_per_min=0.02)
        assert sensor.native_value == pytest.approx(0.75, abs=0.02)
        attrs = sensor.extra_state_attributes
        assert [h["rise_c"] for h in attrs["heat_ups"]] == pytest.approx([0.6, 0.9], abs=0.02)
        assert attrs["needed"] == 2 and attrs["default_c"] == 0.5
        # And the nearby temperature uses it: 20.5 − 0.75 → 19.7.
        _leave(ent)
        ent.hass.set(SCHEDULE, "comfort")
        ent._async_schedule_changed(_event("comfort"))
        drain_tasks(ent)
        _set(ent, CLOSE, "off")
        clock.tick(11 * MIN)
        _cycle(ent)
        assert _state(ent) == "nearby"
        assert ent.extra_state_attributes["preheat_target_c"] == 19.7

    def test_learned_whatever_the_mode(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(mode="off", schedule="night")
        _heat_up_at_home(ent, clock, rise_per_min=0.03)
        assert len(ent._recovery.episodes) == 1

    def test_a_boost_is_not_learned_from(self, monkeypatch):
        """Boost (22 °C here) is no schedule temperature."""
        clock = _Clock(monkeypatch)
        ent = _entity(schedule="night")
        _feed(ent, 17.9)
        _cycle(ent)
        _preset(ent, "boost")
        room = 17.9
        for _ in range(31):
            clock.tick(MIN)
            room += 0.03
            _feed(ent, room)
            _cycle(ent)
        assert ent._recovery.episodes == [] and not ent._recovery.running

    def test_a_window_spoils_it(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(schedule="night")
        _feed(ent, 17.9)
        _cycle(ent)
        ent.hass.set(SCHEDULE, "comfort")
        ent._async_schedule_changed(_event("comfort"))
        drain_tasks(ent)
        assert ent._recovery.running
        ent.hass.set(WINDOW, "on")
        clock.tick(MIN)
        _cycle(ent)
        assert not ent._recovery.running


# ===========================================================================
# Failures
# ===========================================================================

class TestFailures:
    def test_a_failing_decision_never_stops_the_regulation(self, monkeypatch, caplog):
        clock = _Clock(monkeypatch)
        ent = _entity(schedule="night")

        def boom(_inputs):
            raise RuntimeError("boom")

        monkeypatch.setattr(ent._preheat, "evaluate", boom)
        _feed(ent, 17.0, setpoint=20.0)
        ent.hass.set(SCHEDULE, "comfort")
        with caplog.at_level(logging.ERROR):
            ent._async_schedule_changed(_event("comfort"))
            drain_tasks(ent)
            for _ in range(3):
                clock.tick(MIN)
                _cycle(ent)
        assert any(svc == "set_temperature" for svc, *_ in _commands(ent))
        assert sum("preheat failed" in r.getMessage() for r in caplog.records) == 1

    def test_a_failing_learner_never_stops_the_regulation(self, monkeypatch, caplog):
        clock = _Clock(monkeypatch)
        ent = _entity(schedule="night")

        def boom(*_a, **_k):
            raise ValueError("bad")

        monkeypatch.setattr(ent._recovery, "observe", boom)
        _feed(ent, 17.0, setpoint=20.0)
        ent.hass.set(SCHEDULE, "comfort")
        with caplog.at_level(logging.ERROR):
            ent._async_schedule_changed(_event("comfort"))
            drain_tasks(ent)
            clock.tick(MIN)
            _cycle(ent)
        assert any(svc == "set_temperature" for svc, *_ in _commands(ent))
        assert sum("30-minute recovery failed" in r.getMessage() for r in caplog.records) == 1

    @pytest.mark.parametrize("state", ["unavailable", "nonsense", "2026-10-27T18:00:00"])
    def test_bad_home_eta_states_count_as_unknown(self, monkeypatch, state):
        _Clock(monkeypatch)
        ent = _entity()
        _leave(ent)
        _set(ent, CLOSE, state)
        _set(ent, NEARBY, state)
        _set(ent, ARRIVAL, state)
        assert ent._preheat_decision.arrival_ts is None
        assert ent._preheat_decision.level_drop_ts is not None     # not used: dropping

    def test_diagnostics(self, monkeypatch):
        _Clock(monkeypatch)
        ent = _entity()
        _leave(ent)
        _cycle(ent)
        diag = ent.preheat_diagnostics()
        assert diag["mode"] == "shadow" and diag["state"] == "close"
        assert diag["config"]["rate_factor"] == 0.8
        assert diag["decision"]["target_c"] == 20.5
        assert diag["logged"]["state"] == "close"


# ===========================================================================
# Review findings
# ===========================================================================

class TestReviewFindings:
    def test_the_room_switched_off_mid_heat_up_spoils_the_recovery(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(schedule="night")
        _feed(ent, 17.9)
        _cycle(ent)
        ent.hass.set(SCHEDULE, "comfort")
        ent._async_schedule_changed(_event("comfort"))
        drain_tasks(ent)
        assert ent._recovery.running
        clock.tick(MIN)
        _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
        drain_tasks(ent)
        assert not ent._recovery.running
        _run(ent.async_set_hvac_mode(_climate.HVACMode.HEAT))
        drain_tasks(ent)
        for _ in range(31):
            clock.tick(MIN)
            _feed(ent, 18.5)
            _cycle(ent)
        assert ent._recovery.episodes == []

    def test_a_bridged_reading_spoils_the_recovery(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity(schedule="night")
        _feed(ent, 17.9)
        _cycle(ent)
        ent.hass.set(SCHEDULE, "comfort")
        ent._async_schedule_changed(_event("comfort"))
        drain_tasks(ent)
        assert ent._recovery.running
        clock.tick(MIN)
        _feed(ent, None, internal=19.9)            # a gap, bridged by the last value
        _cycle(ent)
        assert ent._sensor_degraded
        assert not ent._recovery.running

    def test_resume_schedule_decides_on_what_it_saved(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()                              # the schedule says Comfort
        _preset(ent, "eco")                          # Night by hand, then out
        clock.tick(MIN)
        _leave(ent)
        _cycle(ent)
        assert ent.extra_state_attributes["preheat_target_c"] == 18.5
        writes = ent.state_writes
        _preset(ent, "schedule")
        assert ent.extra_state_attributes["preheat_target_c"] == 20.5
        assert ent.state_writes > writes

    def test_a_home_eta_change_writes_the_thermostat(self, monkeypatch):
        _Clock(monkeypatch)
        ent = _entity()
        _leave(ent)
        writes = ent.state_writes
        _set(ent, CLOSE, "off")                      # no new decision yet (drop delay)
        assert ent.state_writes == writes
        _set(ent, ARRIVAL, _iso(_climate.time.time()))
        assert _state(ent) == "due" and ent.state_writes == writes + 1

    def test_the_reason_to_stand_aside_is_live_and_recorded(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _cycle(ent)
        assert ent.extra_state_attributes["preheat_inactive_reason"] == "someone_home"
        assert _sensor_of(ent).extra_state_attributes["inactive_reason"] == "someone_home"
        clock.tick(MIN)
        _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
        drain_tasks(ent)
        assert ent.extra_state_attributes["preheat_inactive_reason"] == "switched_off"
        assert _sensor_of(ent).extra_state_attributes["inactive_reason"] == "switched_off"
        assert _lines(ent) == []                     # no line for it

    def test_no_home_eta_entity_is_said(self, monkeypatch):
        _Clock(monkeypatch)
        ent = _entity(options={
            "preheat_arrival_entity_id": None, "preheat_close_entity_id": None,
            "preheat_nearby_entity_id": None,
        })
        _leave(ent)
        _cycle(ent)
        assert ent._preheat_decision.inactive_reason == "no_home_eta"

    def test_the_live_numbers_are_not_recorded(self):
        assert {"activity", "preheat_needed_min", "preheat_due_at", "preheat_state"} <= (
            _climate.RoomstatClimate._unrecorded_attributes
        )

    def test_a_bad_save_starts_preheat_afresh(self, monkeypatch, caplog):
        clock = _Clock(monkeypatch)
        old = _entity()
        data = old.extra_restore_state_data.as_dict()
        data["preheat"] = {"recovery": {"version": 1, "episodes": 7},
                           "controller": {"version": 1, "wants": "x"},
                           "held_cold": {"since": "x"}, "logged": []}
        new = _entity(start=False)
        new.hass = old.hass
        new.coordinator.data = old.coordinator.data

        class _Extra:
            def as_dict(self):
                return data

        _run(_start(new, extra=_Extra()))
        clock.tick(MIN)
        _cycle(new)
        assert new._recovery.episodes == []
        assert _state(new) == "inactive"

    def test_a_restore_that_raises_never_stops_the_room(self, monkeypatch, caplog):
        _Clock(monkeypatch)
        old = _entity()
        extra = old.extra_restore_state_data
        new = _entity(start=False)
        new.hass = old.hass
        new.coordinator.data = old.coordinator.data

        def boom(*_a, **_k):
            raise TypeError("bad")

        monkeypatch.setattr(new._recovery, "restore", boom)
        with caplog.at_level(logging.ERROR):
            _run(_start(new, extra=extra))
        assert "could not be taken back" in caplog.text
        _cycle(new)
        assert _state(new) == "inactive"
