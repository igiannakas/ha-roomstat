"""Off lasts until someone is home; Away and Resume schedule while out are back
to automatic (2.12.0).

27 Oct 2026: Frost protection ("Off" on the cards) was picked by hand in all
four rooms at 08:48, the owner left at 12:34 and came back at 17:50.  Off by
hand was sticky: replayed through the entity, the rooms would still have been
Off two days later; only his Resume schedule at 13:11 undid it.  Picking Away
while out would not have: it kept the Off saved for the return.

The owner's rules (27 Oct 2026), under test:
- Off picked by hand while nobody is home, or the one the room is in when
  everyone leaves, lasts while nobody is home (no limit; the room stays Off,
  not Away) and ends when someone comes home: the schedule comes back
  (Comfort without a schedule).
- An Off picked while someone is home, and nobody leaves, lasts until it is
  changed; one picked once the presence sensor reads home again (the home
  delay running) too.
- Away or Resume schedule while nobody is home is back to automatic: any
  change by hand (an Off included) and any schedule override ends, the room
  is in away mode, and coming home restores the schedule's preset.  Without a
  schedule it is the preset from before the change, never an Off, Away or
  Boost (Comfort then).
- A restart keeps all of it, and a pre-2.12 snapshot of away mode entered
  from an Off picked by hand is taken as the Off until home.
"""
from __future__ import annotations

import sys
import time

from tests.ha_harness import (
    PRESENCE,
    SCHEDULE,
    SUMMER,
    TRV,
    WINDOW,
    _climate,
    _event,
    _Extra,
    _make_entity,
    _restart_from,
    _run,
    _start,
    _State,
    drain_tasks,
    e2e,
)
from tests.test_presence_override import (  # noqa: F401  (the autouse fixture)
    _arrive,
    _entity,
    _fire,
    _held,
    _leave,
    _preset,
    _reset_timers,
    _schedule,
    _timers,
    _window,
)

pytestmark = e2e

OFF = "frost_protection"


def _everyone_leaves(ent):
    """The house empties with the room in an Off picked by hand: it stays Off."""
    ent.hass.set(PRESENCE, "off")
    ent._async_presence_changed(_event("off"))
    _fire(ent, "_async_presence_away_action")


def _attrs(ent):
    return ent.extra_state_attributes


def _log(ent):
    return [e.text for e in reversed(ent._activity.entries())]


def _off_until_home(ent) -> bool:
    return _held(ent) and ent._presence_ctrl.off_until_home


# ===========================================================================
# 27 Oct 2026, replayed
# ===========================================================================

class TestTheDayReplayed:
    def test_off_before_leaving_ends_when_he_comes_home(self):
        ent = _entity("comfort")
        _preset(ent, OFF)                             # 08:48, at home
        assert _attrs(ent)["schedule_override_active"] is True
        _everyone_leaves(ent)                         # 12:34
        # Off while out, not Away (16 °C): nothing heats it.
        assert ent.preset_mode == OFF and ent.target_temperature == 5.0
        assert _off_until_home(ent) and not ent._presence_ctrl.is_active
        assert _attrs(ent)["presence_override_until"] is None
        assert _timers("_async_presence_override_expired") == []
        assert _log(ent)[-2] == (
            "Nobody home: the Off picked by hand stays until someone is home "
            "(Away or Resume schedule goes back to automatic)"
        )
        for preset in ("night", "comfort", "night"):  # the schedule moves on
            _schedule(ent, preset)
            assert ent.preset_mode == OFF
        _schedule(ent, "comfort")
        _arrive(ent)                                  # 17:50
        assert ent.preset_mode == "comfort" and ent.target_temperature == 20.5
        assert not ent._presence_ctrl.nobody_home
        assert _attrs(ent)["schedule_override_active"] is False
        assert "Someone is home, the Off picked by hand ends: back to Comfort" in _log(ent)
        # And the schedule is in charge again.
        _schedule(ent, "night")
        assert ent.preset_mode == "eco"


# ===========================================================================
# Off: until someone is home
# ===========================================================================

class TestOffUntilHome:
    def test_off_picked_while_out_has_no_limit_and_ends_at_home(self):
        ent = _entity("night", limit_min=90)
        _leave(ent)
        _preset(ent, OFF)
        assert ent.preset_mode == OFF and _off_until_home(ent)
        assert _timers("_async_presence_override_expired") == []
        assert _attrs(ent)["presence_override_until"] is None
        assert _log(ent)[-3] == (
            "Off picked while nobody is home: it stays until someone is home, then "
            "the schedule comes back (Away or Resume schedule goes back to automatic)"
        )
        _arrive(ent)
        assert ent.preset_mode == "eco"
        assert _attrs(ent)["schedule_override_active"] is False

    def test_a_flicker_home_and_back_keeps_the_off(self):
        ent = _entity("comfort")
        _leave(ent)
        _preset(ent, OFF)
        ent.hass.set(PRESENCE, "on")
        ent._async_presence_changed(_event("on"))
        ent.hass.set(PRESENCE, "off")
        ent._async_presence_changed(_event("off"))
        assert _timers("_async_presence_home_action") == []
        assert ent.preset_mode == OFF and _off_until_home(ent)

    def test_another_change_after_the_off_is_an_ordinary_change(self):
        ent = _entity("night", limit_min=90)
        _leave(ent)
        _preset(ent, OFF)
        _preset(ent, "comfort")
        assert _held(ent) and not ent._presence_ctrl.off_until_home
        assert len(_timers("_async_presence_override_expired")) == 1
        _arrive(ent)
        assert ent.preset_mode == "comfort"           # 2.9.0: the change stays

    def test_off_again_after_another_change_is_until_home_again(self):
        ent = _entity("night", limit_min=90)
        _leave(ent)
        _preset(ent, "comfort")
        _preset(ent, OFF)
        assert _off_until_home(ent)
        assert _timers("_async_presence_override_expired") == []

    def test_an_off_picked_in_the_home_delay_is_an_off_picked_at_home(self):
        ent = _entity("comfort")
        _leave(ent)
        ent.hass.set(PRESENCE, "on")
        ent._async_presence_changed(_event("on"))
        _preset(ent, OFF)
        assert not ent._presence_ctrl.off_until_home
        _fire(ent, "_async_presence_home_action")
        assert ent.preset_mode == OFF
        _schedule(ent, "night")
        assert ent.preset_mode == OFF                 # sticky, as at home

    def test_an_off_at_home_with_nobody_leaving_lasts_until_changed(self):
        ent = _entity("comfort")
        _preset(ent, OFF)
        for preset in ("night", "comfort", "night", "comfort"):
            _schedule(ent, preset)
        assert ent.preset_mode == OFF

    def test_the_schedule_s_own_off_is_ordinary_away_mode(self):
        ent = _entity(OFF)
        assert ent.preset_mode == OFF
        _leave(ent)                                   # Away, as before
        assert ent._presence_ctrl.is_active and not _held(ent)
        _schedule(ent, "comfort")
        _arrive(ent)
        assert ent.preset_mode == "comfort"

    def test_without_a_schedule_it_ends_on_comfort(self):
        ent = _entity(None)
        _preset(ent, OFF)
        _everyone_leaves(ent)
        assert ent.preset_mode == OFF and _off_until_home(ent)
        _arrive(ent)
        assert ent.preset_mode == "comfort" and ent.target_temperature == 20.5

    def test_a_boost_from_the_off_is_not_an_off(self):
        ent = _entity("comfort")
        _preset(ent, OFF)
        _preset(ent, "boost")
        _leave(ent)                                   # away mode, boost ended
        assert ent._presence_ctrl.is_active and ent._boost_cancel is None

    def test_off_with_the_window_open_when_everyone_leaves(self):
        ent = _entity("comfort", window=True)
        _preset(ent, OFF)
        _window(ent, "on")
        _everyone_leaves(ent)
        assert _off_until_home(ent)
        assert ent._window_ctrl.get_saved().preset == OFF
        _window(ent, "off")
        assert ent.preset_mode == OFF                 # not Away
        _arrive(ent)
        assert ent.preset_mode == "comfort"

    def test_coming_home_with_the_window_open(self):
        ent = _entity("comfort", window=True)
        _leave(ent)
        _preset(ent, OFF)
        _window(ent, "on")
        _arrive(ent)
        assert ent.preset_mode == OFF                 # the window's frost protection
        assert ent._window_ctrl.get_saved().preset == "comfort"
        assert _attrs(ent)["schedule_override_active"] is False
        _window(ent, "off")
        assert ent.preset_mode == "comfort"

    def test_switching_off_and_on_while_out_is_back_to_automatic(self):
        """2.12.0 review: Off then Heat routed the schedule's Comfort into
        the Off until home, heating the empty house with no limit."""
        ent = _entity("comfort", limit_min=90)
        _preset(ent, OFF)
        _everyone_leaves(ent)
        _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
        drain_tasks(ent)
        _run(ent.async_set_hvac_mode(_climate.HVACMode.HEAT))
        drain_tasks(ent)
        assert ent.preset_mode == "away" and ent._presence_ctrl.is_active
        assert not ent._presence_ctrl.off_until_home and not _held(ent)
        assert ent._presence_ctrl.get_saved().preset == "comfort"
        assert (
            "Switched on while nobody is home: the change by hand is cancelled, "
            "back to automatic; Comfort comes back with you"
        ) in _log(ent)

    def test_switching_on_in_away_mode_stays_automatic(self):
        ent = _entity("night")
        _leave(ent)
        _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
        _run(ent.async_set_hvac_mode(_climate.HVACMode.HEAT))
        drain_tasks(ent)
        assert ent.preset_mode == "away" and ent._presence_ctrl.get_saved().preset == "eco"

    def test_switching_on_in_the_home_delay_resumes_the_schedule(self):
        ent = _entity("comfort")
        _leave(ent)
        _preset(ent, OFF)
        ent.hass.set(PRESENCE, "on")
        ent._async_presence_changed(_event("on"))
        _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
        _run(ent.async_set_hvac_mode(_climate.HVACMode.HEAT))
        drain_tasks(ent)
        assert ent.preset_mode == "comfort"

    def test_a_boost_from_an_off_while_out_ends_back_on_the_off_until_home(self):
        """No schedule: the Boost ends on its pre-boost Off (2.12.0 review: that
        Off then stayed after coming home)."""
        ent = _entity(None, limit_min=90)
        _leave(ent)
        _preset(ent, OFF)
        _preset(ent, "boost")
        assert not ent._presence_ctrl.off_until_home
        _fire(ent, "_async_boost_expired")
        assert ent.preset_mode == OFF and _off_until_home(ent)
        assert _timers("_async_presence_override_expired") == []
        _arrive(ent)
        assert ent.preset_mode == "comfort"

    def test_the_activity_line_reads_until_home(self):
        ent = _entity("comfort")
        _leave(ent)
        _preset(ent, OFF)
        assert ent._activity_label() == "Frost protection, until someone is home"

    def test_without_a_schedule_the_lines_name_comfort_and_away(self):
        ent = _entity(None)
        _leave(ent)
        _preset(ent, OFF)
        assert (
            "Off picked while nobody is home: it stays until someone is home, then "
            "Comfort comes back (Away goes back to automatic)"
        ) in _log(ent)

    def test_the_card_reads_off_until_home(self):
        ent = _entity("comfort")
        _leave(ent)
        _preset(ent, OFF)
        attrs = _attrs(ent)
        assert attrs["presence_override_active"] is True
        assert attrs["presence_override_until"] is None
        assert attrs["presence_away_active"] is False
        assert ent.preset_mode == OFF


# ===========================================================================
# Away and Resume schedule while out: back to automatic
# ===========================================================================

class TestBackToAutomatic:
    def _off_before_leaving(self, schedule="comfort"):
        ent = _entity(schedule)
        _preset(ent, OFF)
        _everyone_leaves(ent)
        assert _off_until_home(ent)
        return ent

    def test_the_away_script_after_an_off_before_leaving(self):
        ent = self._off_before_leaving()
        # "Heating - Away": Heat (no change while heating), then Away.
        _run(ent.async_set_hvac_mode(_climate.HVACMode.HEAT))
        drain_tasks(ent)
        _preset(ent, "away")
        assert ent.preset_mode == "away" and ent.target_temperature == 16.0
        assert ent._presence_ctrl.is_active and not _held(ent)
        assert ent._presence_ctrl.get_saved().preset == "comfort"
        assert _attrs(ent)["schedule_override_active"] is False
        assert (
            "Away picked while nobody is home: the change by hand is cancelled, back "
            "to automatic; Comfort comes back with you"
        ) in _log(ent)
        _schedule(ent, "night")                       # the return follows the schedule
        _arrive(ent)
        assert ent.preset_mode == "eco"

    def test_resume_schedule_after_an_off_before_leaving(self):
        ent = self._off_before_leaving("night")
        _preset(ent, "schedule")                      # the cards' Schedule / Resume
        assert ent.preset_mode == "away" and ent._presence_ctrl.is_active
        assert ent._presence_ctrl.get_saved().preset == "eco"
        _arrive(ent)
        assert ent.preset_mode == "eco"

    def test_away_after_an_off_while_out(self):
        ent = _entity("comfort")
        _leave(ent)
        _preset(ent, OFF)
        _preset(ent, "away")
        assert ent.preset_mode == "away" and ent._presence_ctrl.is_active
        _arrive(ent)
        assert ent.preset_mode == "comfort"

    def test_away_while_already_away_says_so_and_keeps_the_schedule(self):
        ent = _entity("night")
        _leave(ent)
        _preset(ent, "away")
        assert ent.preset_mode == "away" and ent._presence_ctrl.is_active
        assert ent._presence_ctrl.get_saved().preset == "eco"
        assert (
            "Away picked while nobody is home: back to automatic; Night comes back "
            "with you"
        ) in _log(ent)

    def test_resume_while_already_away(self):
        ent = _entity("night")
        _leave(ent)
        _preset(ent, "schedule")
        assert ent.preset_mode == "away" and not _held(ent)
        assert (
            "Resume schedule while nobody is home: back to automatic; Night comes "
            "back with you"
        ) in _log(ent)

    def test_without_a_schedule_away_never_brings_back_an_off(self):
        ent = _entity(None)
        _preset(ent, OFF)
        _everyone_leaves(ent)
        _preset(ent, "away")
        assert ent.preset_mode == "away" and ent._presence_ctrl.is_active
        _arrive(ent)
        assert ent.preset_mode == "comfort"

    def test_without_a_schedule_away_never_brings_back_an_off_saved_for_the_return(self):
        """Leaving during a Boost started from Off: away mode saves the Off for
        the return (the Boost would have ended on it); Away is automatic."""
        ent = _entity(None)
        _preset(ent, OFF)
        _preset(ent, "boost")
        _leave(ent)
        assert ent._presence_ctrl.get_saved().preset == OFF
        _preset(ent, "away")
        assert ent._presence_ctrl.get_saved().preset == "comfort"
        _preset(ent, "comfort")                       # and through a change by hand
        _preset(ent, "away")
        _arrive(ent)
        assert ent.preset_mode == "comfort"

    def test_without_a_schedule_away_brings_back_the_preset_from_before(self):
        ent = _entity(None)
        _preset(ent, "eco")
        _leave(ent)
        _preset(ent, OFF)
        _preset(ent, "away")
        _arrive(ent)
        assert ent.preset_mode == "eco"


# ===========================================================================
# Restarts
# ===========================================================================

def _new(presence_state, schedule_state="comfort"):
    new = _make_entity({"presence_away_delay_s": 60, "presence_home_delay_s": 30,
                        "schedule_override_min": 60, "away_target": 16.0},
                       window=False, presence=True, schedule=schedule_state is not None)
    new.hass.keep_tasks = True
    new.hass.set(TRV, "heat", {"temperature": 20.0})
    new.hass.set(PRESENCE, presence_state)
    if schedule_state is not None:
        new.hass.set(SCHEDULE, schedule_state)
    return new


class TestRestart:
    def _off_while_out(self):
        old = _entity("comfort")
        _preset(old, OFF)
        _everyone_leaves(old)
        _run(old.async_will_remove_from_hass())
        return old

    def test_still_out_it_stays_off_with_no_limit(self):
        old = self._off_while_out()
        new = _new("off")
        _restart_from(old, new)
        assert new.preset_mode == OFF and _off_until_home(new)
        assert _timers("_async_presence_override_expired") == []
        assert _timers("_async_presence_away_action") == []

    def test_home_meanwhile_ends_it_and_the_schedule_applies(self):
        old = self._off_while_out()
        new = _new("on", "night")
        _restart_from(old, new)
        assert new.preset_mode == "eco"
        assert not new._presence_ctrl.nobody_home
        assert _attrs(new)["schedule_override_active"] is False
        _schedule(new, "comfort")
        assert new.preset_mode == "comfort"

    def test_home_meanwhile_with_the_schedule_at_off(self):
        """The schedule's own Off then: the room follows it, the Off picked by
        hand's override gone, so the schedule's next block applies."""
        old = self._off_while_out()
        new = _new("on", OFF)
        _restart_from(old, new)
        assert new.preset_mode == OFF
        assert _attrs(new)["schedule_override_active"] is False
        _schedule(new, "comfort")
        assert new.preset_mode == "comfort"

    def _restart_2_11(self, old, new, *, held_cold=False):
        """Restart *new* from *old*'s snapshot as 2.11.0 saved it: no Off
        until home flag, and maybe preheat's Away held cold."""
        extra = old.extra_restore_state_data.as_dict()
        extra.pop("presence_override_off_until_home")
        if held_cold:
            extra["preheat"]["held_cold"] = {"since": time.time(), "until": None}
        last = _State(old.hvac_mode.value, {
            "preset_mode": old.preset_mode, "temperature": old.target_temperature,
            "window_open_active": old._window_ctrl.is_active,
        })
        _run(_start(new, last_state=last, extra=_Extra(extra)))
        drain_tasks(new)

    def _2_11_left_with_off(self, schedule="comfort"):
        """2.11.0 left an Off picked by hand as away mode, the Off saved."""
        old = _entity(schedule)
        _preset(old, OFF)
        old._presence_ctrl.activate(OFF, 5.0)
        old._preset_mode = "away"
        assert _attrs(old)["schedule_override_active"] is True
        _run(old.async_will_remove_from_hass())
        return old

    def test_a_2_11_snapshot_of_away_from_an_off_by_hand(self):
        old = self._2_11_left_with_off()
        out = _new("off")
        self._restart_2_11(old, out)
        assert out.preset_mode == OFF and _off_until_home(out)
        assert (
            "Startup: the Off picked by hand before everyone left stays until "
            "someone is home"
        ) in _log(out)
        home = _new("on", "night")
        self._restart_2_11(old, home)
        assert home.preset_mode == "eco"
        assert _attrs(home)["schedule_override_active"] is False

    def test_a_2_11_snapshot_after_an_away_picked_while_out(self):
        """2.11.0 held the room cold then; 2.12.0 reads that Away as back to
        automatic."""
        old = self._2_11_left_with_off()
        out = _new("off", "night")
        self._restart_2_11(old, out, held_cold=True)
        assert out.preset_mode == "away" and out._presence_ctrl.is_active
        assert out._presence_ctrl.get_saved().preset == "eco"
        assert _attrs(out)["schedule_override_active"] is False
        assert (
            "Startup: the Away picked while nobody was home is back to automatic; "
            "Night comes back with you"
        ) in _log(out)

    def test_a_2_11_snapshot_of_an_off_picked_while_out(self):
        old = _entity("comfort", limit_min=90)
        _leave(old)
        _preset(old, OFF)
        old._presence_ctrl.off_until_home = False     # as 2.11.0 kept it
        old._presence_ctrl.override_until = time.time() + 90 * 60
        _run(old.async_will_remove_from_hass())
        new = _new("off")
        self._restart_2_11(old, new)
        assert _off_until_home(new)
        assert _timers("_async_presence_override_expired") == []
        new.hass.set(PRESENCE, "on")
        _arrive(new)
        assert new.preset_mode == "comfort"

    def test_a_2_12_off_picked_in_the_home_delay_is_not_taken_for_one(self):
        old = _entity("comfort")
        _leave(old)
        old.hass.set(PRESENCE, "on")
        old._async_presence_changed(_event("on"))
        _preset(old, OFF)                             # someone is home
        _run(old.async_will_remove_from_hass())
        new = _new("off")
        _restart_from(old, new)
        assert not new._presence_ctrl.off_until_home

    def test_a_2_11_snapshot_with_summer_on_says_nothing(self):
        old = self._2_11_left_with_off()
        new = _make_entity({"presence_away_delay_s": 60, "away_target": 16.0},
                           window=False, presence=True, schedule=True, summer=True)
        new.hass.keep_tasks = True
        new.hass.set(TRV, "heat", {"temperature": 20.0})
        new.hass.set(PRESENCE, "off")
        new.hass.set(SCHEDULE, "comfort")
        new.hass.set(SUMMER, "on")
        self._restart_2_11(old, new)
        assert not any(line.startswith("Startup: the Off") for line in _log(new))

    def test_the_flag_is_persisted_and_old_snapshots_read_as_off(self):
        ctrl = sys.modules["roomstat_e2e.climate_controllers"]
        persisted = ctrl.PersistedAutomationState(
            presence_override_active=True, presence_override_off_until_home=True,
        )
        assert ctrl.PersistedAutomationState.from_dict(persisted.as_dict()) == persisted
        old = persisted.as_dict()
        old.pop("presence_override_off_until_home")
        # Before 2.12.0: not recorded (read with the 2.12.0 rules at startup).
        assert ctrl.PersistedAutomationState.from_dict(old).presence_override_off_until_home is None


def test_a_window_open_at_the_restore_keeps_frost_until_it_closes():
    """The startup restore with the window open: the schedule waits for the
    close, and it is the schedule's preset (2.12.0 review: the schedule's
    state was taken back after presence, so Comfort came back, with the Off's
    sticky override still hiding the schedule)."""
    old = _entity("night", window=True)
    _leave(old)
    _preset(old, OFF)
    _window(old, "on")
    _run(old.async_will_remove_from_hass())
    new = _make_entity({"presence_home_delay_s": 30, "window_delay_s": 0,
                        "schedule_override_min": 60, "away_target": 16.0},
                       window=True, presence=True, schedule=True)
    new.hass.keep_tasks = True
    new.hass.set(TRV, "heat", {"temperature": 20.0})
    new.hass.set(PRESENCE, "on")
    new.hass.set(SCHEDULE, "night")
    new.hass.set(WINDOW, "on")
    _restart_from(old, new)
    assert new.preset_mode == OFF and new._window_ctrl.is_active
    assert new._window_ctrl.get_saved().preset == "eco"
    assert _attrs(new)["schedule_override_active"] is False
    _schedule(new, "away")                          # followed while the window is open
    _window(new, "off")
    assert new.preset_mode == "away"
