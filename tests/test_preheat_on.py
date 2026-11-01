"""Preheat on the way home in mode on (2.18.0), through the real climate entity.

The owner's decision (1 Nov 2026): out of shadow, into production.  While
away mode is in charge the decision's target is the room's setpoint; nothing
else in the rule changes (tests/test_preheat_entity.py covers the rule
through the entity in shadow, tests/test_preheat.py the rule itself).  Under
test, the go-live list of the plan (project doc
claude/roomstat-preheat-wiring-plan-2026-10-24.md, §9 step 4):
- each level and the heading-home start heat the room; coming home is the
  normal restore, with no extra command;
- a Home ETA change that moves the target runs a cycle at once, one that
  doesn't runs none; shadow and off never do;
- the schedule while away, and Away or Resume schedule while out, retarget
  at once;
- a change by hand wins (an Off until someone is home), and preheat comes
  back when its limit sends the room back to Away;
- a window takes the room to frost and back to the target on its close;
  summer mode and a schedule at Away heat nothing;
- the trip home over, the Home ETA entities gone: back to Away;
- a restart keeps a heading-home start heating;
- the log says what it does, the activity line and the thermostat say why.
"""
from __future__ import annotations

import pytest

import tests.test_preheat_entity as pe
from tests.ha_harness import SCHEDULE, SUMMER, WINDOW, _event, _run, drain_tasks, e2e
from tests.ha_harness import _settle as settle

pytestmark = e2e

ARRIVAL, CLOSE, NEARBY, MIN = pe.ARRIVAL, pe.CLOSE, pe.NEARBY, pe.MIN
AWAY = 16.0
COMFORT = 20.5   # the harness's Comfort
NIGHT = 18.5


@pytest.fixture(autouse=True)
def _reset():
    pe.reset_timers()
    yield
    pe.reset_timers()


def _on(**kwargs):
    return pe._entity(mode="on", **kwargs)


def _cycles(ent, monkeypatch):
    """Record the triggers of the regulation cycles the entity runs."""
    triggers: list[str] = []
    run = ent._async_regulation_cycle

    async def counted(trigger):
        triggers.append(trigger)
        await run(trigger)

    monkeypatch.setattr(ent, "_async_regulation_cycle", counted)
    return triggers


def _away_and_waiting(ent, clock):
    """Out of the house, nobody close or nearby any more: Away."""
    pe._leave(ent)
    pe._set(ent, CLOSE, "off")
    pe._set(ent, NEARBY, "off")
    clock.tick(11 * MIN)
    pe._feed(ent, 18.0)
    pe._cycle(ent)
    assert pe._state(ent) == ("off" if ent.preheat_mode == "off" else "waiting")
    assert ent._effective_setpoint() == AWAY


def _sent(ent):
    return [c for c in pe._commands(ent) if c[0] in ("set_temperature", "set_hvac_mode")]


# ===========================================================================
# What it heats to
# ===========================================================================

class TestTargets:
    def test_each_level_is_the_setpoint(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = _on()
        pe._leave(ent)
        assert pe._state(ent) == "close"
        assert ent._effective_setpoint() == COMFORT
        assert ent.target_temperature == COMFORT
        assert ent.extra_state_attributes["preheat_heating_to_c"] == COMFORT
        pe._set(ent, CLOSE, "off")
        clock.tick(10 * MIN)
        pe._cycle(ent)
        # The 30-minute temperature: Comfort less the default 0.5 °C.
        assert pe._state(ent) == "nearby"
        assert ent._effective_setpoint() == 20.0
        pe._set(ent, NEARBY, "off")
        clock.tick(10 * MIN)
        pe._cycle(ent)
        assert pe._state(ent) == "waiting"
        assert ent._effective_setpoint() == AWAY
        assert ent.extra_state_attributes["preheat_heating_to_c"] is None
        assert ent.preset_mode == "away"

    def test_heading_home_heats_up_and_home_sends_nothing_more(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = _on()
        _away_and_waiting(ent, clock)
        before = len(_sent(ent))
        triggers = _cycles(ent, monkeypatch)
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
        drain_tasks(ent)
        assert pe._state(ent) == "due"
        assert triggers == ["preheat"]
        assert ent._effective_setpoint() == COMFORT
        # More heat waits until preheat's new target has stood 15 s ...
        assert _sent(ent)[before:] == []
        assert ent._last_reason == "preheat_settling(15s)"
        assert "waits: preheat's new target settling" in ent.extra_state_attributes["activity"]
        settle(clock)
        drain_tasks(ent)
        # ... then 2.5 °C short: a heat-up, at full power.
        assert _sent(ent)[before:] == [("set_temperature", 25.0, "heat")]
        for _ in range(5):
            clock.tick(MIN)
            pe._feed(ent, 18.5, setpoint=25.0)
            pe._cycle(ent)
        heating = len(_sent(ent))
        pe._arrive(ent)
        clock.tick(MIN)
        pe._cycle(ent)
        assert ent.preset_mode == "comfort"
        assert ent._effective_setpoint() == COMFORT
        assert len(_sent(ent)) == heating   # the restore changes nothing for the valve

    def test_the_log_says_what_it_does(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = _on()
        _away_and_waiting(ent, clock)
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
        drain_tasks(ent)
        lines = pe._lines(ent)
        assert all(line.startswith("Preheat: ") for line in lines)
        assert not any("would" in line for line in lines)
        assert lines[0].startswith(
            "Preheat: someone is close to home: it heats to 20.5 °C"
        )
        assert lines[-1].startswith("Preheat: heading home: it heats to 20.5 °C now")

    def test_the_activity_line_names_preheat(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = _on()
        _away_and_waiting(ent, clock)
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
        drain_tasks(ent)
        line = ent.extra_state_attributes["activity"]
        assert "→ 20.5 Away, nobody home, preheat: heading home" in line

    def test_shadow_and_off_run_no_cycle_on_a_home_eta_change(self, monkeypatch):
        for mode in ("shadow", "off"):
            pe.reset_timers()
            clock = pe._Clock(monkeypatch)
            ent = pe._entity(mode=mode)
            _away_and_waiting(ent, clock)
            triggers = _cycles(ent, monkeypatch)
            pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
            drain_tasks(ent)
            assert triggers == []
            assert ent._effective_setpoint() == AWAY

    def test_a_change_that_keeps_the_target_runs_no_cycle(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = _on()
        _away_and_waiting(ent, clock)
        triggers = _cycles(ent, monkeypatch)
        # Far ahead: not due yet, whatever it moves to.
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 6 * 3600))
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 5 * 3600))
        drain_tasks(ent)
        assert triggers == []
        assert pe._state(ent) == "waiting"
        # Close again: a rise, at once.
        pe._set(ent, CLOSE, "on")
        drain_tasks(ent)
        assert triggers == ["preheat"]
        assert ent._effective_setpoint() == COMFORT


# ===========================================================================
# Inputs that change between cycles
# ===========================================================================

class TestRetarget:
    def test_the_schedule_while_away_is_sent_at_once(self, monkeypatch):
        pe._Clock(monkeypatch)
        ent = _on(schedule="night")
        pe._leave(ent)
        assert ent._effective_setpoint() == NIGHT
        triggers = _cycles(ent, monkeypatch)
        ent.hass.set(SCHEDULE, "comfort")
        ent._async_schedule_changed(_event("comfort"))
        drain_tasks(ent)
        assert ent.preset_mode == "away"
        assert ent._effective_setpoint() == COMFORT
        assert triggers == ["schedule"]

    def test_back_to_automatic_retargets_at_once(self, monkeypatch):
        pe._Clock(monkeypatch)
        ent = _on()
        pe._leave(ent)
        # Night saved for the return (as an automation would), schedule Comfort.
        ent._presence_ctrl.update_saved("eco", NIGHT)
        pe._cycle(ent)
        assert ent._effective_setpoint() == NIGHT
        triggers = _cycles(ent, monkeypatch)
        pe._preset(ent, "away")
        assert ent._presence_ctrl.get_saved().preset == "comfort"
        assert ent._effective_setpoint() == COMFORT
        assert triggers == ["preset_change"]


# ===========================================================================
# What still wins
# ===========================================================================

class TestWhatWins:
    def test_a_change_by_hand_wins_until_its_limit(self, monkeypatch):
        """Then the room is back in away mode with the change saved for the
        return (2.9.0: coming home carries on with it), which is what
        preheat heats to."""
        clock = pe._Clock(monkeypatch)
        ent = _on(options={"presence_override_max_min": 60})
        _away_and_waiting(ent, clock)
        pe._preset(ent, "eco")
        assert ent._presence_ctrl.override_active
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
        drain_tasks(ent)
        assert pe._state(ent) == "inactive"
        assert ent.extra_state_attributes["preheat_inactive_reason"] == "change_by_hand"
        assert ent._effective_setpoint() == NIGHT
        clock.tick(60 * MIN)
        pe._fire(ent, "_async_presence_override_expired")
        assert ent.preset_mode == "away"
        assert ent._presence_ctrl.is_active
        assert ent._presence_ctrl.get_saved().preset == "eco"
        assert pe._state(ent) == "due"
        assert ent._effective_setpoint() == NIGHT

    def test_an_off_by_hand_stays_off_until_home(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = _on()
        _away_and_waiting(ent, clock)
        pe._preset(ent, "frost_protection")
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
        drain_tasks(ent)
        assert ent.extra_state_attributes["preheat_inactive_reason"] == "off_by_hand"
        assert ent._effective_setpoint() == 5.0
        clock.tick(MIN)
        pe._arrive(ent)
        assert ent.preset_mode == "comfort"

    def test_a_window_is_frost_then_the_target_again(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = _on()
        _away_and_waiting(ent, clock)
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
        drain_tasks(ent)
        assert ent._effective_setpoint() == COMFORT
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        clock.tick(30)
        pe._fire(ent, "_async_window_action")
        assert ent.preset_mode == "frost_protection"
        assert ent._effective_setpoint() == 5.0
        assert ent.extra_state_attributes["preheat_heating_to_c"] is None
        clock.tick(MIN)
        ent.hass.set(WINDOW, "off")
        ent._async_window_changed(_event("off"))
        drain_tasks(ent)
        if pe._timers("_async_window_close_action"):
            pe._fire(ent, "_async_window_close_action")
        pe._cycle(ent)
        assert ent.preset_mode == "away"
        assert pe._state(ent) == "due"
        assert ent._effective_setpoint() == COMFORT

    def test_the_window_close_delay_is_not_preheat(self, monkeypatch):
        """While the window's re-warm wait runs the room is still at frost
        protection: neither the setpoint nor what is shown is preheat's."""
        clock = pe._Clock(monkeypatch)
        ent = _on(options={"window_close_delay_s": 120})
        pe._leave(ent)
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        clock.tick(30)
        pe._fire(ent, "_async_window_action")
        clock.tick(MIN)
        ent.hass.set(WINDOW, "off")
        ent._async_window_changed(_event("off"))
        drain_tasks(ent)
        assert ent._window_ctrl.close_delay_active
        pe._cycle(ent)
        assert ent.preset_mode == "frost_protection"
        assert ent._preheat_target_c() is None
        assert ent.extra_state_attributes["preheat_heating_to_c"] is None
        assert "preheat:" not in ent.extra_state_attributes["activity"]

    def test_summer_mode_heats_nothing(self, monkeypatch):
        pe._Clock(monkeypatch)
        ent = _on(options={"summer_mode_entity_id": SUMMER})
        ent.hass.set(SUMMER, "off")
        pe._leave(ent)
        assert ent._effective_setpoint() == COMFORT
        ent.hass.set(SUMMER, "on")
        ent._async_summer_changed(_event("on"))
        drain_tasks(ent)
        pe._cycle(ent)
        assert pe._state(ent) == "inactive"
        assert ent._preheat_decision.inactive_reason == "summer"
        assert ent._effective_setpoint() == 5.0
        assert ent.extra_state_attributes["preheat_heating_to_c"] is None

    def test_a_schedule_at_away_heats_nothing(self, monkeypatch):
        pe._Clock(monkeypatch)
        ent = _on(schedule="away")
        pe._leave(ent)
        pe._cycle(ent)
        assert ent._preheat_decision.inactive_reason == "nothing_warmer"
        assert ent._effective_setpoint() == AWAY

    def test_the_room_switched_off_heats_nothing(self, monkeypatch):
        pe._Clock(monkeypatch)
        ent = _on()
        pe._leave(ent)
        assert ent._effective_setpoint() == COMFORT
        _run(ent.async_set_hvac_mode("off"))
        drain_tasks(ent)
        assert ent._effective_setpoint() == 5.0
        assert ent.extra_state_attributes["preheat_heating_to_c"] is None

    def test_a_stale_decision_never_outlives_a_change_by_hand(self, monkeypatch):
        """The setpoint is read between evaluations too (the UI, the band
        check): a change by hand takes the room off preheat at once."""
        pe._Clock(monkeypatch)
        ent = _on()
        pe._leave(ent)
        assert ent._preheat_decision.state == "close"
        ent._presence_ctrl.start_override(ent.hass, 0.0, 0, lambda _now: None)
        assert ent._preheat_decision.state == "close"     # not decided again yet
        assert ent._preheat_target_c() is None

    @pytest.mark.parametrize("what", ["summer", "off", "window"])
    def test_a_stale_decision_never_outlives_summer_off_or_a_window(self, monkeypatch, what):
        """Between the change and the cycle that decides again (a window's
        frost protection is set before its cycle waits for the lock)."""
        pe._Clock(monkeypatch)
        ent = _on()
        pe._leave(ent)
        assert ent._preheat_target_c() == COMFORT
        if what == "summer":
            ent._summer_active = True
        elif what == "off":
            ent._hvac_mode = type(ent._hvac_mode)("off")
        else:
            ent._window_ctrl.activate("away", None)
            ent._preset_mode = "frost_protection"
        assert ent._preheat_decision.state == "close"     # not decided again yet
        assert ent._preheat_target_c() is None
        assert ent.extra_state_attributes["preheat_heating_to_c"] is None


# ===========================================================================
# Endings, and a restart
# ===========================================================================

class TestEndings:
    def test_the_trip_home_over_is_back_to_away(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = _on()
        _away_and_waiting(ent, clock)
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
        drain_tasks(ent)
        assert ent._effective_setpoint() == COMFORT
        pe._set(ent, ARRIVAL, "unknown")
        drain_tasks(ent)
        assert ent._effective_setpoint() == COMFORT     # the grace
        clock.tick(10 * MIN)
        pe._cycle(ent)
        assert pe._state(ent) == "waiting"
        assert ent._effective_setpoint() == AWAY

    def test_the_home_eta_entities_gone_are_back_to_away(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = _on()
        pe._leave(ent)
        assert ent._effective_setpoint() == COMFORT
        for entity_id in (CLOSE, NEARBY):
            ent.hass.states_by_id.pop(entity_id, None)
            ent._async_preheat_input_changed(_event("unavailable"))
        clock.tick(10 * MIN)
        pe._cycle(ent)
        assert pe._state(ent) == "waiting"
        assert ent._effective_setpoint() == AWAY

    def _restart(self, ent):
        new = pe._entity(mode="on", start=False)
        new.hass = ent.hass
        new.coordinator.data = ent.coordinator.data
        new.coordinator.preheat_entity.hass = ent.hass
        pe._restart_from(ent, new)
        drain_tasks(new)
        return new

    def test_a_restart_keeps_heating(self, monkeypatch):
        """The start is carried over: by now the room has warmed so far that a
        fresh decision would not be due yet for the same arrival (2.18.0
        review: the first version passed with preheat's save thrown away)."""
        clock = pe._Clock(monkeypatch)
        ent = _on()
        _away_and_waiting(ent, clock)
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 60 * MIN))
        drain_tasks(ent)
        assert pe._state(ent) == "due"
        clock.tick(MIN)
        pe._feed(ent, 20.4)          # needs 32.5 min now, 59 min to go
        new = self._restart(ent)
        assert new._effective_setpoint() == COMFORT     # no minute at Away first
        clock.tick(MIN)
        pe._cycle(new)
        assert new.preset_mode == "away"
        assert pe._state(new) == "due"
        assert new._effective_setpoint() == COMFORT
        # Proof that it was carried: a room with no save starts waiting.
        fresh = pe._entity(mode="on")
        fresh.hass.set(ARRIVAL, ent.hass.states.get(ARRIVAL).state)
        pe._leave(fresh)
        pe._set(fresh, CLOSE, "off")
        pe._set(fresh, NEARBY, "off")
        pe._feed(fresh, 20.4)
        clock.tick(11 * MIN)
        pe._cycle(fresh)
        assert pe._state(fresh) == "waiting"

    def test_listens_to_the_three_entities(self, monkeypatch):
        pe._Clock(monkeypatch)
        ent = _on()
        assert ent._preheat_entity_ids() == [ARRIVAL, CLOSE, NEARBY]
        assert ent.preheat_mode == "on"
        assert pe._sensor_of(ent).extra_state_attributes["mode"] == "on"


# ===========================================================================
# The 2.18.0 review's findings
# ===========================================================================

class TestReviewFindings:
    def test_a_burst_of_home_eta_updates_sends_no_heat_and_off(self, monkeypatch):
        """Arrival 40 -> 90 -> 40 -> 90 min, 5 s apart: until 2.18.0's fix,
        25 °C, off, 25 °C, off and the confirmation off within 75 s."""
        clock = pe._Clock(monkeypatch)
        ent = _on()
        _away_and_waiting(ent, clock)
        before = len(_sent(ent))
        for minutes in (40, 90, 40, 90):
            pe._set(ent, ARRIVAL, pe._iso(clock.now + minutes * MIN))
            drain_tasks(ent)
            clock.tick(5)
        assert _sent(ent)[before:] == []
        # It settles on 40: one heat-up, 15 s after the last change.
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
        drain_tasks(ent)
        assert _sent(ent)[before:] == []
        settle(clock)
        drain_tasks(ent)
        assert _sent(ent)[before:] == [("set_temperature", 25.0, "heat")]

    def test_a_change_while_the_heat_waits_for_the_bridge_holds_it(self, monkeypatch):
        """The arrival slips while the cycle waits for its turn at the bridge:
        the stale 25 °C must not go out with the off straight after it."""
        import sys

        clock = pe._Clock(monkeypatch)
        ent = _on()
        _away_and_waiting(ent, clock)
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
        drain_tasks(ent)
        before = len(_sent(ent))
        mod = sys.modules["roomstat_e2e.climate_regulation"]
        real = mod._wait_for_command_slot

        async def slips_meanwhile():
            # 5 h: past the 212 min the room needs from 18 °C, so not due.
            pe._set(ent, ARRIVAL, pe._iso(clock.now + 300 * MIN))
            await real()

        monkeypatch.setattr(mod, "_wait_for_command_slot", slips_meanwhile)
        settle(clock)
        assert _sent(ent)[before:] == []
        monkeypatch.setattr(mod, "_wait_for_command_slot", real)
        drain_tasks(ent)
        assert pe._state(ent) == "waiting"
        assert not any(c[1] == 25.0 for c in _sent(ent)[before:])

    def test_a_scheduled_away_while_out_is_followed(self, monkeypatch):
        """Owner, 1 Nov 2026: a schedule's Away while nobody is home is saved
        for the return like any other preset: nothing to preheat, and coming
        home restores it until the schedule moves on."""
        clock = pe._Clock(monkeypatch)
        ent = _on()
        pe._leave(ent)
        assert ent._effective_setpoint() == COMFORT
        ent.hass.set(SCHEDULE, "away")
        ent._async_schedule_changed(_event("away"))
        drain_tasks(ent)
        assert ent._presence_ctrl.get_saved().preset == "away"
        assert ent._preheat_decision.inactive_reason == "nothing_warmer"
        assert ent._effective_setpoint() == AWAY
        clock.tick(MIN)
        pe._arrive(ent)
        assert ent.preset_mode == "away"
        ent.hass.set(SCHEDULE, "comfort")
        ent._async_schedule_changed(_event("comfort"))
        drain_tasks(ent)
        assert ent.preset_mode == "comfort"
        assert ent._effective_setpoint() == COMFORT

    def test_a_failing_decision_fails_cold_at_once(self, monkeypatch, caplog):
        clock = pe._Clock(monkeypatch)
        ent = _on()
        _away_and_waiting(ent, clock)
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
        drain_tasks(ent)
        assert ent._effective_setpoint() == COMFORT
        triggers = _cycles(ent, monkeypatch)

        def boom(_inputs):
            raise RuntimeError("boom")

        monkeypatch.setattr(ent._preheat, "evaluate", boom)
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 39 * MIN))
        drain_tasks(ent)
        assert ent._effective_setpoint() == AWAY
        assert triggers == ["preheat"]
        for _ in range(3):
            clock.tick(MIN)
            pe._cycle(ent)
        assert ent._effective_setpoint() == AWAY
        assert "preheat failed" in caplog.text

    def test_arriving_mid_preheat_books_no_heat_up(self, monkeypatch):
        """The auto-tuner would learn a dead time from the rest of preheat's
        ramp (2.18.0 review); from Away without preheat it still does."""
        for mode, booked in (("on", False), ("shadow", True)):
            pe.reset_timers()
            clock = pe._Clock(monkeypatch)
            ent = pe._entity(mode=mode, options={"autotune_enabled": True})
            _away_and_waiting(ent, clock)
            pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
            drain_tasks(ent)
            for minute in range(10):
                clock.tick(MIN)
                pe._feed(ent, 18.0 + 0.05 * minute, setpoint=25.0 if mode == "on" else None)
                pe._cycle(ent)
            pe._arrive(ent)
            clock.tick(MIN)
            pe._feed(ent, 18.6, setpoint=25.0 if mode == "on" else None)
            pe._cycle(ent)
            assert (ent._autotuner._heatup is not None) is booked, mode

    def test_a_retarget_queued_before_removal_runs_no_cycle(self, monkeypatch):
        clock = pe._Clock(monkeypatch)
        ent = _on()
        _away_and_waiting(ent, clock)
        triggers = _cycles(ent, monkeypatch)
        pe._set(ent, ARRIVAL, pe._iso(clock.now + 40 * MIN))
        ent._preheat_removed = True
        drain_tasks(ent)
        assert triggers == []
