"""A change by hand settles before it adds heat (2.10.4, MANUAL_SETTLE_S).

25 Oct 2026: a Tado valve that got an off seconds after a heat command took
the heat and not the off.  Master bedroom 11:09, 25 °C then off 5 s later;
second bedroom 17:12, 25 °C then off 15 s later (Comfort, then Frost
protection, picked 9 s apart).  Both zones showed off at once while the
valves heated at 100 % for 15 and 13 min.  More heat asked for by a change
by hand now waits until no further change has come for MANUAL_SETTLE_S
(by time: an automation's heat within it waits too); less heat and off go
at once.  More heat also waits while a window's open delay runs, and is
asked again after its turn at the bridge.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from tests.ha_harness import (
    SCHEDULE,
    SUMMER,
    TRV,
    WINDOW,
    _active_timers,
    _climate,
    _event,
    _fire_last_timer,
    _make_entity,
    _number,
    _restart_from,
    _run,
    _select_preset,
    _settle,
    _start,
    drain_tasks,
    e2e,
    reset_timers,
)
from tests.test_command_bookkeeping import _commands, _times_out_but_lands
from tests.test_fail_cold_entity import _Clock, _cycle, _feed

pytestmark = e2e


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


def _room(monkeypatch):
    """A cloud TRV with 30-min timers for the maximum (the owner's rooms),
    the valve off, the room cold, the last change by hand long ago."""
    clock = _Clock(monkeypatch)
    ent = _make_entity({"heat_timer_s": 1800})
    _run(_start(ent))
    _feed(ent, 21.5, 12.0, 5.0, off=True)
    _cycle(ent)
    _run(_select_preset(ent, "frost_protection"))
    clock.tick(300)
    _feed(ent, 18.0, 9.0, 5.0, off=True)
    _cycle(ent)
    ent.hass.service_calls.clear()
    return clock, ent


def test_more_heat_by_hand_waits_until_settled(monkeypatch):
    clock, ent = _room(monkeypatch)
    _run(_select_preset(ent, "comfort"))
    assert _commands(ent) == []
    assert ent._last_reason.startswith("settling(")
    assert ent.activity_state.endswith("| Tado off (25.0 waits: a change by hand settling)")
    _settle(clock)
    assert _commands(ent) == ["timer25"]


def test_a_second_press_within_the_settle_replaces_the_first(monkeypatch):
    """The second bedroom at 17:12, without the window: Comfort, then Frost
    protection 9 s later.  No heat command goes out at all."""
    clock, ent = _room(monkeypatch)
    _run(_select_preset(ent, "comfort"))
    clock.tick(9)
    _run(_select_preset(ent, "frost_protection"))
    _settle(clock)
    assert _commands(ent) == []


def test_a_further_change_restarts_the_wait(monkeypatch):
    clock, ent = _room(monkeypatch)
    _run(_select_preset(ent, "comfort"))
    clock.tick(10)
    _run(_select_preset(ent, "boost"))
    clock.tick(6)                      # 16 s after Comfort, 6 s after Boost
    _settle()
    assert _commands(ent) == []
    _settle(clock)
    assert _commands(ent) == ["timer25"]


def test_off_and_less_heat_go_at_once(monkeypatch):
    clock, ent = _room(monkeypatch)
    _run(_select_preset(ent, "comfort"))
    _settle(clock)
    assert _commands(ent) == ["timer25"]
    clock.tick(120)
    _feed(ent, 18.2, 12.0, 25.0, heating=True)
    _run(_select_preset(ent, "frost_protection"))
    assert _commands(ent) == ["timer25", "off"]


def test_an_automation_is_not_held_back(monkeypatch):
    """The schedule (or presence, or a window closing) changes the preset
    with no change by hand in the last 15 s: the heat goes out at once."""
    clock, ent = _room(monkeypatch)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    assert _commands(ent) == ["timer25"]


def test_a_clock_that_went_back_ends_the_wait(monkeypatch):
    clock, ent = _room(monkeypatch)
    _run(_select_preset(ent, "comfort"))
    assert _commands(ent) == []
    ent._manual_change_ts = clock.now + 3600.0     # "in the future"
    _cycle(ent)
    assert _commands(ent) == ["timer25"]


def test_an_automation_within_the_settle_waits_too(monkeypatch):
    """The wait is by time: the schedule changing the preset 5 s after a
    press by hand is held back as well (the press may yet be taken back)."""
    clock, ent = _room(monkeypatch)
    _run(_select_preset(ent, "eco"))
    clock.tick(5)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    assert _commands(ent) == []
    _settle(clock)
    assert _commands(ent) == ["timer25"]


def test_a_temperature_set_by_hand_settles(monkeypatch):
    clock, ent = _room(monkeypatch)
    _run(ent.async_set_temperature(temperature=21.0))
    assert _commands(ent) == []
    _settle(clock)
    assert _commands(ent) == ["timer25"]


def test_heat_while_heating_counts_as_a_change_by_hand(monkeypatch):
    """The heating scripts send Heat before the preset: both are presses."""
    clock, ent = _room(monkeypatch)
    clock.tick(30)
    _run(ent.async_set_hvac_mode(ent.hvac_mode))
    assert ent._manual_change_ts == clock.now


def test_only_a_valid_change_is_a_change_by_hand(monkeypatch):
    """An unknown preset, or Resume schedule without a schedule, changes
    nothing, so it holds nothing back."""
    clock, ent = _room(monkeypatch)
    before = ent._manual_change_ts
    clock.tick(30)
    _run(ent.async_set_preset_mode("no_such_preset"))
    with pytest.raises(Exception, match="No schedule helper"):
        _run(ent.async_resume_schedule())
    assert ent._manual_change_ts == before


def test_resume_schedule_by_hand_settles(monkeypatch):
    clock = _Clock(monkeypatch)
    ent = _make_entity({"heat_timer_s": 1800}, schedule=True)
    ent.hass.keep_tasks = True
    ent.hass.set(SCHEDULE, "comfort")
    _run(_start(ent))
    drain_tasks(ent)
    _feed(ent, 18.0, 9.0, 5.0, off=True)
    _run(_select_preset(ent, "frost_protection"))
    clock.tick(300)
    _cycle(ent)
    ent.hass.service_calls.clear()
    _run(ent.async_resume_schedule())
    drain_tasks(ent)
    assert ent.preset_mode == "comfort"
    assert _commands(ent) == []
    _settle(clock)
    assert _commands(ent) == ["timer25"]


def test_removal_cancels_the_settled_cycle(monkeypatch):
    clock, ent = _room(monkeypatch)
    _run(_select_preset(ent, "comfort"))
    _run(ent.async_will_remove_from_hass())
    assert not [c for c in _active_timers()
                if getattr(c["callback"], "__name__", "") == "_async_settled_cycle"]


# ---------------------------------------------------------------------------
# A reload while settling (every preset temperature change reloads the room)
# ---------------------------------------------------------------------------

def _reload(clock, old, options=None):
    """HA's entry reload: the old entity goes, a new one takes its saved
    state back."""
    _run(old.async_will_remove_from_hass())
    new = _make_entity({"heat_timer_s": 1800, **(options or {})})
    _restart_from(old, new)
    _feed(new, *old._fed)
    return new


def test_a_reload_while_settling_keeps_the_wait(monkeypatch):
    clock, old = _room(monkeypatch)
    old._fed = (18.0, 9.0, 5.0)
    _run(_select_preset(old, "comfort"))
    clock.tick(5)
    new = _reload(clock, old)
    _feed(new, 18.0, 9.0, 5.0, off=True)
    _cycle(new)
    assert _commands(new) == []
    assert new._last_reason.startswith("settling(")
    _settle(clock)
    assert _commands(new) == ["timer25"]


def test_a_settle_long_over_is_not_taken_back(monkeypatch):
    clock, old = _room(monkeypatch)
    _run(_select_preset(old, "comfort"))
    _settle(clock)
    clock.tick(60)
    old._fed = (18.0, 9.0, 5.0)
    new = _reload(clock, old)
    assert new._manual_change_ts == 0.0


def test_a_preset_temperature_change_settles_across_its_reload(monkeypatch):
    """The Comfort number raised by hand: options saved, room reloaded, and
    the new room's heat still waits for the change to settle."""
    clock = _Clock(monkeypatch)
    old = _make_entity({"heat_timer_s": 1800})
    _run(_start(old))
    _feed(old, 20.4, 13.0, 5.0, off=True)
    _run(old._async_apply_preset("comfort", trigger="schedule_change"))
    clock.tick(600)
    _cycle(old)
    old.coordinator.climate_entity = old
    old.hass.config_entries = types.SimpleNamespace(
        async_update_entry=lambda entry, options: setattr(entry, "options", options))
    comfort = next(d for d in _number._PRESET_NUMBERS if d.conf_key == "comfort_target")
    number = _number.PresetTemperatureNumber(old.coordinator, old._config_entry, comfort)
    number.hass = old.hass
    _run(number.async_set_native_value(22.0))
    assert old._manual_change_ts == clock.now
    clock.tick(3)
    old._fed = (20.4, 13.0, 5.0)
    new = _reload(clock, old, dict(old._config_entry.options))
    _feed(new, 20.4, 13.0, 5.0, off=True)
    _cycle(new)
    assert new.target_temperature == 22.0
    assert _commands(new) == []
    _settle(clock)
    assert _commands(new) == ["timer25"]


# ---------------------------------------------------------------------------
# More heat than the valve may hold
# ---------------------------------------------------------------------------

def test_less_heat_than_the_valves_schedule_goes_at_once(monkeypatch):
    """Back on its Tado schedule, the valve heats to the block's setpoint:
    a lower command is less heat, and goes at once."""
    clock, ent = _room(monkeypatch)
    _feed(ent, 20.0, 11.0, 20.0, scheduled=True)
    _run(_select_preset(ent, "comfort"))
    sent = _commands(ent)
    assert len(sent) == 1 and isinstance(sent[0], float) and sent[0] < 20.0


def test_less_heat_than_a_heat_command_on_its_way_goes_at_once(monkeypatch):
    """A heat command that timed out (but landed) may be running while the
    state still shows off: a lower one is less heat, not more."""
    clock, ent = _room(monkeypatch)
    _feed(ent, 20.0, 11.0, 5.0, off=True)
    real = _times_out_but_lands(ent)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    ent.hass.services.async_call = real
    first = _commands(ent)
    assert len(first) == 1 and first[0] > 12.5
    clock.tick(200)                      # past the rate limit, within its grace
    _run(ent.async_set_temperature(temperature=20.2))
    sent = _commands(ent)
    assert len(sent) == 2 and sent[1] < first[0]


def test_an_off_valve_is_off_whatever_setpoint_it_shows(monkeypatch):
    clock, ent = _room(monkeypatch)
    _feed(ent, 20.0, 11.0, 21.0, off=True)          # off, showing a stale 21
    _run(_select_preset(ent, "comfort"))
    assert _commands(ent) == []
    _settle(clock)
    assert len(_commands(ent)) == 1


# ---------------------------------------------------------------------------
# The window's open delay: its off is due within seconds
# ---------------------------------------------------------------------------

def _open(ent):
    ent.hass.set(WINDOW, "on")
    ent._async_window_changed(_event("on"))
    assert ent._window_ctrl.open_delay_active


def test_more_heat_waits_while_the_window_open_delay_runs(monkeypatch):
    clock, ent = _room(monkeypatch)
    _open(ent)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    assert _commands(ent) == []
    assert ent._last_reason == "window_pending"
    assert ent.activity_state.startswith("Window just opened")
    assert ent.activity_state.endswith("| Tado off (25.0 waits: the window's delay)")
    _fire_last_timer()                  # the window's action: frost protection
    assert ent.preset_mode == "frost_protection"
    assert _commands(ent) == []


def test_a_window_shut_again_within_its_delay_lets_the_heat_go(monkeypatch):
    clock, ent = _room(monkeypatch)
    _open(ent)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    assert _commands(ent) == []
    ent.hass.keep_tasks = True
    clock.tick(10)
    ent.hass.set(WINDOW, "off")
    ent._async_window_changed(_event("off"))
    drain_tasks(ent)
    assert _commands(ent) == ["timer25"]


def test_less_heat_and_off_go_during_the_open_delay(monkeypatch):
    clock, ent = _room(monkeypatch)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    assert _commands(ent) == ["timer25"]
    clock.tick(300)
    _feed(ent, 18.3, 15.0, 25.0, heating=True)
    _open(ent)
    _run(ent._async_apply_preset("frost_protection", trigger="schedule_change"))
    assert _commands(ent) == ["timer25", "off"]


# ---------------------------------------------------------------------------
# More heat waits for its turn at the bridge before it is committed
# ---------------------------------------------------------------------------

def test_a_press_while_the_heat_waits_its_turn_is_seen(monkeypatch):
    """2.10.4 review: the heating scripts set four rooms at once, so their
    settled cycles queue at the bridge (5 s apart).  Frost protection picked
    while this room's 25 °C waits its turn: the 25 °C must not go out (it
    did, with the off 5 s after it, the 25 Oct pattern)."""
    clock, ent = _room(monkeypatch)
    reg = sys.modules["roomstat_e2e.climate_regulation"]
    _run(_select_preset(ent, "comfort"))
    clock.tick(16.0)
    tasks: list = []

    async def queued_behind_other_rooms():
        if tasks:
            return
        clock.tick(5.0)                       # another room's command first
        tasks.append(asyncio.ensure_future(ent.async_set_preset_mode("frost_protection")))
        for _ in range(5):
            await asyncio.sleep(0)            # the press runs up to the lock
        clock.tick(5.0)

    monkeypatch.setattr(reg, "_wait_for_command_slot", queued_behind_other_rooms)
    settled = [c for c in _active_timers()
               if getattr(c["callback"], "__name__", "") == "_async_settled_cycle"][-1]
    settled["cancelled"] = True

    async def scenario():
        await settled["callback"](None)
        await tasks[0]

    _run(scenario())
    assert ent.preset_mode == "frost_protection"
    assert _commands(ent) == []
    clock.tick(30.0)                    # and nothing once everything has settled
    _cycle(ent)
    assert _commands(ent) == []


def test_heat_that_still_holds_after_its_turn_goes_out_once(monkeypatch):
    """Nothing changed while it waited: one command, and no second wait
    for the bridge inside the send."""
    clock, ent = _room(monkeypatch)
    reg = sys.modules["roomstat_e2e.climate_regulation"]
    waits = []

    async def slot():
        waits.append(clock.now)

    monkeypatch.setattr(reg, "_wait_for_command_slot", slot)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    assert _commands(ent) == ["timer25"]
    assert len(waits) == 1


def test_a_window_open_event_again_keeps_its_delay(monkeypatch):
    """An attribute update (or on -> unavailable -> on) while the window is
    open must not restart the open delay: the window's off would never come
    while the sensor keeps reporting, and more heat waits meanwhile."""
    clock, ent = _room(monkeypatch)
    _open(ent)
    first = [c for c in _active_timers()
             if getattr(c["callback"], "__name__", "") == "_async_window_action"]
    ent._async_window_changed(_event("unavailable"))
    ent._async_window_changed(_event("on"))
    again = [c for c in _active_timers()
             if getattr(c["callback"], "__name__", "") == "_async_window_action"]
    assert again == first and len(first) == 1


def test_a_window_action_that_finds_it_shut_lets_the_heat_go(monkeypatch):
    """The sensor read unavailable, then off, when the delay ended (no close
    event seen): the action is skipped and the held-back heat goes at once."""
    clock, ent = _room(monkeypatch)
    _open(ent)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    assert _commands(ent) == []
    ent.hass.set(WINDOW, "off")
    _fire_last_timer()
    assert not ent._window_ctrl.open_delay_active
    assert _commands(ent) == ["timer25"]


# ---------------------------------------------------------------------------
# An off seconds after a heat command is sent once more a minute later
# (OFF_CONFIRM_S; presses further apart than the settle)
# ---------------------------------------------------------------------------

def test_presses_20_s_apart_get_the_off_confirmed(monkeypatch):
    """Comfort, then Frost protection 20 s later: the heat goes at 15 s and
    the off 5 s after it (the 25 Oct pattern).  A minute later the off goes
    once more, then nothing."""
    clock, ent = _room(monkeypatch)
    _run(_select_preset(ent, "comfort"))
    _settle(clock)                                  # 16 s: the 25 °C goes
    clock.tick(4.0)
    _run(_select_preset(ent, "frost_protection"))
    assert _commands(ent) == ["timer25", "off"]
    _feed(ent, 18.0, 9.0, 5.0, off=True)            # the zone shows off at once
    clock.tick(30.0)
    _cycle(ent)
    assert _commands(ent) == ["timer25", "off"]     # not yet
    clock.tick(30.0)
    _cycle(ent)
    assert _commands(ent) == ["timer25", "off", "off"]
    assert ent._last_reason == "sent(off_confirm)"
    assert "Sending the off again" in " ".join(ent.activity_log_lines())
    for _ in range(10):
        clock.tick(60.0)
        _cycle(ent)
    assert _commands(ent) == ["timer25", "off", "off"]


def test_an_off_long_after_the_heat_is_not_confirmed(monkeypatch):
    clock, ent = _room(monkeypatch)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    clock.tick(120.0)
    _feed(ent, 18.3, 15.0, 25.0, heating=True)
    _run(ent._async_apply_preset("frost_protection", trigger="schedule_change"))
    assert _commands(ent) == ["timer25", "off"]
    _feed(ent, 18.3, 15.0, 5.0, off=True)
    for _ in range(5):
        clock.tick(60.0)
        _cycle(ent)
    assert _commands(ent) == ["timer25", "off"]


def test_heat_again_before_the_confirmation_cancels_it(monkeypatch):
    clock, ent = _room(monkeypatch)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    clock.tick(5.0)
    _run(ent._async_apply_preset("frost_protection", trigger="schedule_change"))
    assert _commands(ent) == ["timer25", "off"]
    clock.tick(30.0)
    _feed(ent, 18.0, 9.0, 5.0, off=True)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    assert _commands(ent) == ["timer25", "off", "timer25"]
    assert ent._off_confirm_ts is None              # heat wanted: nothing to confirm
    clock.tick(60.0)
    _feed(ent, 18.0, 11.0, 25.0, heating=True)
    _cycle(ent)
    assert "off" not in _commands(ent)[3:]


def test_switched_off_seconds_after_heat_gets_the_off_confirmed(monkeypatch):
    """Roomstat's own off (the keep-off path) works the same."""
    clock, ent = _room(monkeypatch)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    clock.tick(8.0)
    _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
    assert _commands(ent) == ["timer25", "off"]
    _feed(ent, 18.0, 9.0, 5.0, off=True)
    clock.tick(61.0)
    _cycle(ent)
    assert _commands(ent) == ["timer25", "off", "off"]
    clock.tick(60.0)
    _cycle(ent)
    assert _commands(ent) == ["timer25", "off", "off"]


def test_summer_seconds_after_heat_gets_the_off_confirmed(monkeypatch):
    clock = _Clock(monkeypatch)
    ent = _make_entity({"heat_timer_s": 1800}, summer=True)
    ent.hass.set(SUMMER, "off")
    _run(_start(ent))
    _feed(ent, 18.0, 9.0, 5.0, off=True)
    ent.hass.set(TRV, "off")
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    assert _commands(ent) == ["timer25"]
    ent.hass.set(TRV, "heat", {"temperature": 25.0})
    clock.tick(6.0)
    ent.hass.set(SUMMER, "on")
    ent._async_summer_changed(_event("on"))
    _cycle(ent)
    assert _commands(ent) == ["timer25", "off"]
    ent.hass.set(TRV, "off")
    _feed(ent, 18.0, 9.0, 5.0, off=True)
    clock.tick(61.0)
    _cycle(ent)
    assert _commands(ent) == ["timer25", "off", "off"]
    assert ent._last_reason == "sent(off_confirm)"


def test_a_failed_confirmation_is_retried_at_the_rate_limit(monkeypatch):
    clock, ent = _room(monkeypatch)
    _run(ent._async_apply_preset("comfort", trigger="schedule_change"))
    clock.tick(5.0)
    _run(ent._async_apply_preset("frost_protection", trigger="schedule_change"))
    _feed(ent, 18.0, 9.0, 5.0, off=True)
    clock.tick(60.0)

    async def down(*_a, **_k):
        raise TimeoutError

    real = ent.hass.services.async_call
    ent.hass.services.async_call = down
    _cycle(ent)
    assert ent._last_reason == "send_failed(off_confirm)"
    ent.hass.services.async_call = real
    clock.tick(60.0)
    _cycle(ent)
    assert _commands(ent) == ["timer25", "off"]     # waits for the rate limit
    clock.tick(130.0)
    _cycle(ent)
    assert _commands(ent) == ["timer25", "off", "off"]
    assert ent._last_reason == "sent(off_confirm)"


def test_a_reload_before_the_confirmation_keeps_it(monkeypatch):
    """Heat, off 5 s later, then a preset temperature changed (a reload):
    the new room still sends the off once more a minute after the first."""
    clock, old = _room(monkeypatch)
    _run(old._async_apply_preset("comfort", trigger="schedule_change"))
    clock.tick(5.0)
    _run(old._async_apply_preset("frost_protection", trigger="schedule_change"))
    assert _commands(old) == ["timer25", "off"]
    clock.tick(10.0)
    old._fed = (18.0, 9.0, 5.0)
    new = _reload(clock, old)
    _feed(new, 18.0, 9.0, 5.0, off=True)
    clock.tick(50.0)
    _cycle(new)
    assert _commands(new) == ["off"]
    assert new._last_reason == "sent(off_confirm)"
