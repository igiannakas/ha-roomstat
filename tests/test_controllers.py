"""Tests for climate state-machine controllers.

Imports climate_controllers directly via importlib so that no Home Assistant
bootstrap is needed.  HA's ``async_call_later`` is never invoked because every
test passes its own ``call_later`` stub via the keyword argument.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading (HA-free)
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent / "custom_components" / "roomstat"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ctrl_mod = _load("climate_controllers")

WindowAutomationController = _ctrl_mod.WindowAutomationController
PresenceAutomationController = _ctrl_mod.PresenceAutomationController
SavedState = _ctrl_mod.SavedState


# ---------------------------------------------------------------------------
# Test stub for async_call_later
# ---------------------------------------------------------------------------

class FakeCallLater:
    """Records all scheduled callbacks; use .trigger(i) to fire one."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, hass, delay, callback):
        entry = {"delay": delay, "callback": callback, "cancelled": False}
        self.calls.append(entry)

        def cancel():
            entry["cancelled"] = True

        return cancel

    def trigger(self, index: int = 0, now=None) -> None:
        """Fire the callback at *index*."""
        self.calls[index]["callback"](now)

    @property
    def scheduled_count(self) -> int:
        return len(self.calls)

    @property
    def active_count(self) -> int:
        return sum(1 for c in self.calls if not c["cancelled"])


# ---------------------------------------------------------------------------
# WindowAutomationController
# ---------------------------------------------------------------------------

class TestWindowController:

    def _ctrl(self) -> WindowAutomationController:
        return WindowAutomationController()

    # --- initial state ---

    def test_initial_not_active(self):
        assert not self._ctrl().is_active

    def test_initial_no_close_delay(self):
        assert not self._ctrl().close_delay_active

    # --- open handling ---

    def test_open_schedules_timer(self):
        c = self._ctrl()
        fake = FakeCallLater()
        actions = []
        c.handle_window_opened(None, 30, lambda now: actions.append(now), call_later=fake)
        assert fake.scheduled_count == 1
        assert fake.calls[0]["delay"] == 30
        assert not c.is_active  # not yet active – open delay still pending

    def test_second_open_cancels_first_timer(self):
        c = self._ctrl()
        fake = FakeCallLater()
        c.handle_window_opened(None, 30, lambda now: None, call_later=fake)
        c.handle_window_opened(None, 30, lambda now: None, call_later=fake)
        assert fake.calls[0]["cancelled"]
        assert not fake.calls[1]["cancelled"]
        assert fake.active_count == 1

    # --- activate / restore ---

    def test_activate_sets_active_and_saves_state(self):
        c = self._ctrl()
        c.activate("comfort", 20.5)
        assert c.is_active
        saved = c.get_saved()
        assert saved.preset == "comfort"
        assert saved.temp == 20.5

    def test_restore_returns_saved_and_clears_state(self):
        c = self._ctrl()
        c.activate("eco", 19.0)
        saved = c.restore()
        assert saved.preset == "eco"
        assert saved.temp == 19.0
        assert not c.is_active
        # Saved state is cleared after restore
        assert c.get_saved().preset is None

    def test_restore_without_activate_returns_empty(self):
        c = self._ctrl()
        saved = c.restore()
        assert saved.preset is None
        assert saved.temp is None

    # --- close handling ---

    def test_close_schedules_timer_when_active(self):
        c = self._ctrl()
        c.activate("comfort", 20.0)
        fake = FakeCallLater()
        result = c.handle_window_closed(None, 120, lambda now: None, call_later=fake)
        assert result is False  # timer scheduled, not immediate
        assert fake.scheduled_count == 1
        assert fake.calls[0]["delay"] == 120
        assert c.close_delay_active

    def test_close_no_timer_when_not_active(self):
        c = self._ctrl()
        fake = FakeCallLater()
        result = c.handle_window_closed(None, 120, lambda now: None, call_later=fake)
        assert result is False
        assert fake.scheduled_count == 0
        assert not c.close_delay_active

    def test_close_zero_delay_returns_true_when_active(self):
        c = self._ctrl()
        c.activate("comfort", 20.0)
        fake = FakeCallLater()
        result = c.handle_window_closed(None, 0, lambda now: None, call_later=fake)
        assert result is True
        assert fake.scheduled_count == 0

    def test_close_zero_delay_returns_false_when_not_active(self):
        c = self._ctrl()
        result = c.handle_window_closed(None, 0, lambda now: None, call_later=FakeCallLater())
        assert result is False

    def test_close_cancels_pending_open_timer(self):
        c = self._ctrl()
        open_fake = FakeCallLater()
        c.handle_window_opened(None, 30, lambda now: None, call_later=open_fake)
        close_fake = FakeCallLater()
        c.handle_window_closed(None, 120, lambda now: None, call_later=close_fake)
        assert open_fake.calls[0]["cancelled"]

    # --- reopen during close delay ---

    def test_reopen_during_close_delay_cancels_close_timer(self):
        c = self._ctrl()
        c.activate("comfort", 20.0)
        close_fake = FakeCallLater()
        c.handle_window_closed(None, 120, lambda now: None, call_later=close_fake)
        assert c.close_delay_active

        open_fake = FakeCallLater()
        c.handle_window_opened(None, 30, lambda now: None, call_later=open_fake)

        # Close timer must be cancelled
        assert close_fake.calls[0]["cancelled"]
        assert not c.close_delay_active
        # No new open timer – stay in frost protection
        assert open_fake.scheduled_count == 0
        # Still active (still in frost protection)
        assert c.is_active

    def test_reopen_after_full_restore_reschedules_open_timer(self):
        """After a full close→restore cycle, a new open should schedule normally."""
        c = self._ctrl()
        c.activate("comfort", 20.0)
        c.restore()  # simulate close delay firing and restoring

        fake = FakeCallLater()
        c.handle_window_opened(None, 30, lambda now: None, call_later=fake)
        assert fake.scheduled_count == 1
        assert not c.is_active  # not active yet (open delay pending)

    # --- cancel_all ---

    def test_cancel_all_resets_everything(self):
        c = self._ctrl()
        open_fake = FakeCallLater()
        c.handle_window_opened(None, 30, lambda now: None, call_later=open_fake)
        c.activate("comfort", 20.0)
        close_fake = FakeCallLater()
        c.handle_window_closed(None, 120, lambda now: None, call_later=close_fake)

        c.cancel_all()

        assert not c.is_active
        assert not c.close_delay_active
        assert c.get_saved().preset is None
        assert close_fake.calls[0]["cancelled"]

    def test_cancel_all_on_idle_is_safe(self):
        c = self._ctrl()
        c.cancel_all()  # must not raise
        assert not c.is_active

    # --- update_saved ---

    def test_update_saved_changes_saved_state(self):
        c = self._ctrl()
        c.activate("comfort", 20.0)
        assert c.get_saved().preset == "comfort"

        c.update_saved("eco", 17.0)
        assert c.get_saved().preset == "eco"
        assert c.get_saved().temp == 17.0
        # Still active
        assert c.is_active

    def test_update_saved_then_restore(self):
        """After update_saved, restore should return the updated state."""
        c = self._ctrl()
        c.activate("comfort", 20.0)
        c.update_saved("away", 16.0)
        saved = c.restore()
        assert saved.preset == "away"
        assert saved.temp == 16.0
        assert not c.is_active

    def test_update_saved_while_not_active_still_works(self):
        """update_saved works even if not active (defensive)."""
        c = self._ctrl()
        c.update_saved("eco", 19.0)
        assert c.get_saved().preset == "eco"
        assert not c.is_active

    # --- close delay extension (room re-warming on its own) ---

    def test_close_records_when_the_window_closed(self):
        c = self._ctrl()
        c.activate("comfort", 20.0)
        c.handle_window_closed(None, 300, lambda now: None, call_later=FakeCallLater(), now=1000.0)
        assert c.close_delay_elapsed_s(1300.0) == 300.0
        assert c.close_delay_elapsed_s(900.0) == 0.0      # clock went back: never negative

    def test_elapsed_is_zero_without_a_close_delay(self):
        c = self._ctrl()
        assert c.close_delay_elapsed_s(5000.0) == 0.0
        c.activate("comfort", 20.0)
        c.handle_window_closed(None, 0, lambda now: None, call_later=FakeCallLater(), now=1000.0)
        assert c.close_delay_elapsed_s(2000.0) == 0.0

    def test_extend_keeps_the_delay_active_and_the_close_time(self):
        c = self._ctrl()
        c.activate("comfort", 20.0)
        fake = FakeCallLater()
        c.handle_window_closed(None, 300, lambda now: None, call_later=fake, now=1000.0)
        fake.trigger(0)                                   # the configured delay ran out
        c.extend_close_delay(None, 60, lambda now: None, call_later=fake)
        assert c.close_delay_active
        assert c.is_active
        assert fake.scheduled_count == 2 and fake.calls[1]["delay"] == 60
        assert c.close_delay_elapsed_s(1400.0) == 400.0  # counted from the close

    def test_extend_without_window_mode_does_nothing(self):
        c = self._ctrl()
        fake = FakeCallLater()
        c.extend_close_delay(None, 60, lambda now: None, call_later=fake)
        assert fake.scheduled_count == 0
        assert not c.close_delay_active

    def test_restore_and_reopen_forget_the_close_time(self):
        c = self._ctrl()
        c.activate("comfort", 20.0)
        fake = FakeCallLater()
        c.handle_window_closed(None, 300, lambda now: None, call_later=fake, now=1000.0)
        c.restore()
        assert c.close_delay_elapsed_s(2000.0) == 0.0
        c.activate("comfort", 20.0)
        c.handle_window_closed(None, 300, lambda now: None, call_later=fake, now=3000.0)
        c.handle_window_opened(None, 30, lambda now: None, call_later=fake)
        assert c.close_delay_elapsed_s(4000.0) == 0.0
        assert not c.close_delay_active


class TestWindowRecoveryWait:
    """After a window closes the room re-warms on its own; heating waits."""

    wait = staticmethod(_ctrl_mod.window_recovery_wait)
    KW = dict(sensor_ok=True, max_wait_s=1800.0, rate_c_per_h=1.0)

    def test_waits_while_the_fresh_slope_is_unknown(self):
        assert self.wait(None, 300.0, **self.KW)
        assert self.wait(float("nan"), 300.0, **self.KW)

    def test_waits_while_rising_fast(self):
        assert self.wait(2.5 / 3600, 600.0, **self.KW)
        assert self.wait(1.0 / 3600, 600.0, **self.KW)       # at the rate: still waiting

    def test_resumes_once_the_rise_has_slowed_or_turned(self):
        assert not self.wait(0.9 / 3600, 600.0, **self.KW)
        assert not self.wait(0.0, 600.0, **self.KW)
        assert not self.wait(-0.5 / 3600, 600.0, **self.KW)

    def test_gives_up_at_the_cap(self):
        assert not self.wait(5.0 / 3600, 1800.0, **self.KW)
        assert not self.wait(None, 1800.0, **self.KW)

    def test_no_sensor_no_wait(self):
        kw = dict(self.KW, sensor_ok=False)
        assert not self.wait(None, 300.0, **kw)
        assert not self.wait(5.0 / 3600, 300.0, **kw)


# ---------------------------------------------------------------------------
# PresenceAutomationController
# ---------------------------------------------------------------------------

class TestPresenceController:

    def _ctrl(self) -> PresenceAutomationController:
        return PresenceAutomationController()

    # --- initial state ---

    def test_initial_not_active(self):
        assert not self._ctrl().is_active

    # --- away handling ---

    def test_away_schedules_timer(self):
        c = self._ctrl()
        fake = FakeCallLater()
        c.handle_presence_away(None, 1800, lambda now: None, call_later=fake)
        assert fake.scheduled_count == 1
        assert fake.calls[0]["delay"] == 1800

    def test_repeated_away_replaces_timer(self):
        c = self._ctrl()
        fake = FakeCallLater()
        c.handle_presence_away(None, 1800, lambda now: None, call_later=fake)
        c.handle_presence_away(None, 900, lambda now: None, call_later=fake)
        assert fake.calls[0]["cancelled"]
        assert not fake.calls[1]["cancelled"]
        assert fake.calls[1]["delay"] == 900

    # --- activate / restore ---

    def test_activate_sets_state(self):
        c = self._ctrl()
        c.activate("eco", 19.0)
        assert c.is_active
        assert c.restore().preset == "eco"

    def test_restore_clears_state(self):
        c = self._ctrl()
        c.activate("away", 16.0)
        saved = c.restore()
        assert saved.preset == "away"
        assert saved.temp == 16.0
        assert not c.is_active

    def test_restore_without_activate_is_safe(self):
        c = self._ctrl()
        saved = c.restore()
        assert saved.preset is None

    # --- home handling ---

    def test_home_cancels_pending_timer(self):
        c = self._ctrl()
        fake = FakeCallLater()
        c.handle_presence_away(None, 1800, lambda now: None, call_later=fake)
        result = c.handle_presence_home()
        assert fake.calls[0]["cancelled"]
        assert result is False  # not active (timer was pending, never fired)

    def test_home_returns_true_when_active(self):
        c = self._ctrl()
        c.activate("comfort", 21.0)
        result = c.handle_presence_home()
        assert result is True

    def test_home_returns_false_when_not_active(self):
        c = self._ctrl()
        result = c.handle_presence_home()
        assert result is False

    def test_home_while_timer_pending_and_not_active(self):
        """Home arrives before the away delay fires → no restore needed."""
        c = self._ctrl()
        fake = FakeCallLater()
        c.handle_presence_away(None, 1800, lambda now: None, call_later=fake)
        result = c.handle_presence_home()
        assert result is False
        assert fake.calls[0]["cancelled"]

    # --- cancel_timer ---

    def test_cancel_timer_without_timer_is_safe(self):
        c = self._ctrl()
        c.cancel_timer()  # must not raise
        assert not c.is_active

    def test_cancel_timer_does_not_change_active_flag(self):
        c = self._ctrl()
        c.activate("comfort", 20.0)
        c.cancel_timer()
        assert c.is_active  # active flag is preserved

    # --- interaction: away timer fires while already active ---

    def test_second_away_event_skips_timer_while_active(self):
        """When already active, a new away event must NOT schedule a timer.

        Starting a new away timer would cause the away-action to overwrite
        the saved preset with PRESET_AWAY, losing the original state.
        """
        c = self._ctrl()
        c.activate("comfort", 20.0)  # already away-active
        fake = FakeCallLater()
        c.handle_presence_away(None, 1800, lambda now: None, call_later=fake)
        assert fake.scheduled_count == 0  # no timer scheduled
        assert c.is_active  # active flag unchanged
        # Saved state must be preserved
        saved = c.restore()
        assert saved.preset == "comfort"
        assert saved.temp == 20.0

    # --- update_saved ---

    def test_update_saved_changes_saved_state(self):
        c = self._ctrl()
        c.activate("comfort", 20.0)
        c.update_saved("eco", 17.0)
        saved = c.restore()
        assert saved.preset == "eco"
        assert saved.temp == 17.0

    def test_update_saved_preserves_active_flag(self):
        c = self._ctrl()
        c.activate("comfort", 20.0)
        c.update_saved("eco", 17.0)
        assert c.is_active

    def test_update_saved_while_not_active(self):
        """update_saved works even if not active (defensive)."""
        c = self._ctrl()
        c.update_saved("eco", 19.0)
        saved = c.restore()
        assert saved.preset == "eco"
        assert not c.is_active

    def test_update_saved_multiple_times_keeps_last(self):
        """Multiple update_saved calls keep only the last one."""
        c = self._ctrl()
        c.activate("comfort", 20.0)
        c.update_saved("eco", 17.0)
        c.update_saved("boost", 25.0)
        saved = c.restore()
        assert saved.preset == "boost"
        assert saved.temp == 25.0

    # --- home delay (debounce) ---

    def test_home_delay_schedules_timer_when_active(self):
        """With delay > 0 and active, a home timer is scheduled, return False."""
        c = self._ctrl()
        c.activate("comfort", 21.0)
        fake = FakeCallLater()
        actions = []
        result = c.handle_presence_home(
            None, 30, lambda now: actions.append(now), call_later=fake
        )
        assert result is False  # not immediate restore
        assert fake.scheduled_count == 1
        assert fake.calls[0]["delay"] == 30
        assert c.is_active  # still active until timer fires

    def test_home_delay_zero_immediate_restore(self):
        """With delay == 0, behaves like before: returns True when active."""
        c = self._ctrl()
        c.activate("comfort", 21.0)
        result = c.handle_presence_home(None, 0, None)
        assert result is True

    def test_home_delay_not_active_no_timer(self):
        """When not active, no home timer is scheduled regardless of delay."""
        c = self._ctrl()
        fake = FakeCallLater()
        result = c.handle_presence_home(
            None, 30, lambda now: None, call_later=fake
        )
        assert result is False
        assert fake.scheduled_count == 0

    def test_away_cancels_home_timer_no_new_away_timer(self):
        """Away event during home-pending cancels the home timer.

        Since is_active is True, no new away timer should be scheduled –
        this prevents the away-action from overwriting the saved state.
        """
        c = self._ctrl()
        c.activate("comfort", 21.0)
        home_fake = FakeCallLater()
        c.handle_presence_home(
            None, 30, lambda now: None, call_later=home_fake
        )
        assert home_fake.active_count == 1

        away_fake = FakeCallLater()
        c.handle_presence_away(None, 600, lambda now: None, call_later=away_fake)

        # Home timer must be cancelled
        assert home_fake.calls[0]["cancelled"]
        # No new away timer (already active)
        assert away_fake.scheduled_count == 0
        # Still active (away mode persists)
        assert c.is_active
        # Saved state preserved
        saved = c.restore()
        assert saved.preset == "comfort"
        assert saved.temp == 21.0

    def test_flicker_scenario_no_restore(self):
        """Full flicker scenario: activate → home(delay) → away.

        The home timer should be cancelled, no new away timer scheduled
        (already active), and the original saved state must be preserved.
        """
        c = self._ctrl()
        c.activate("comfort", 21.0)

        home_fake = FakeCallLater()
        home_actions = []
        c.handle_presence_home(
            None, 30, lambda now: home_actions.append(now), call_later=home_fake
        )

        away_fake = FakeCallLater()
        c.handle_presence_away(None, 600, lambda now: None, call_later=away_fake)

        # Home callback never fired
        assert len(home_actions) == 0
        assert home_fake.calls[0]["cancelled"]
        # No new away timer (already active)
        assert away_fake.scheduled_count == 0
        # Controller still active
        assert c.is_active
        # Saved state preserved
        saved = c.restore()
        assert saved.preset == "comfort"
        assert saved.temp == 21.0

    def test_home_timer_fires_callback(self):
        """When the home delay timer fires, the callback is invoked."""
        c = self._ctrl()
        c.activate("comfort", 21.0)
        fake = FakeCallLater()
        actions = []
        c.handle_presence_home(
            None, 30, lambda now: actions.append("restored"), call_later=fake
        )
        fake.trigger(0, now=None)
        assert actions == ["restored"]

    def test_repeated_home_replaces_timer(self):
        """Two consecutive home events: first timer cancelled, only second active."""
        c = self._ctrl()
        c.activate("comfort", 21.0)
        fake = FakeCallLater()
        c.handle_presence_home(
            None, 30, lambda now: None, call_later=fake
        )
        c.handle_presence_home(
            None, 30, lambda now: None, call_later=fake
        )
        assert fake.calls[0]["cancelled"]
        assert not fake.calls[1]["cancelled"]
        assert fake.active_count == 1

    def test_restore_cancels_home_timer(self):
        """Calling restore() defensively cancels any pending home timer."""
        c = self._ctrl()
        c.activate("comfort", 21.0)
        fake = FakeCallLater()
        c.handle_presence_home(
            None, 30, lambda now: None, call_later=fake
        )
        saved = c.restore()
        assert fake.calls[0]["cancelled"]
        assert saved.preset == "comfort"
        assert not c.is_active

    def test_cancel_timer_cancels_both_timers(self):
        """cancel_timer() cancels both away and home timers."""
        c = self._ctrl()
        away_fake = FakeCallLater()
        c.handle_presence_away(None, 600, lambda now: None, call_later=away_fake)
        c.activate("comfort", 21.0)
        home_fake = FakeCallLater()
        c.handle_presence_home(
            None, 30, lambda now: None, call_later=home_fake
        )
        c.cancel_timer()
        assert home_fake.calls[0]["cancelled"]

    def test_home_with_delay_cancels_pending_away_timer(self):
        """Home event cancels pending away timer (not yet fired).

        When the away timer is still pending and a home event arrives,
        the away timer must be cancelled.  Since the controller is not yet
        active, no home timer is scheduled (nothing to restore).
        """
        c = self._ctrl()
        away_fake = FakeCallLater()
        c.handle_presence_away(None, 600, lambda now: None, call_later=away_fake)

        # Home arrives before away timer fired
        result = c.handle_presence_home()
        # Away timer must be cancelled
        assert away_fake.calls[0]["cancelled"]
        # Not active → no restore needed
        assert result is False

    def test_away_while_active_preserves_saved_state(self):
        """Repeated away events while active must not overwrite saved state.

        This is the core bug fix: sensor flicker (Home → Away) while already
        in away mode must NOT schedule a new away timer, because the timer
        callback would save PRESET_AWAY as the 'previous' preset.
        """
        c = self._ctrl()
        c.activate("eco", 19.0)

        fake = FakeCallLater()
        c.handle_presence_away(None, 600, lambda now: None, call_later=fake)

        # No timer scheduled
        assert fake.scheduled_count == 0
        # Active flag and saved state unchanged
        assert c.is_active
        saved = c.restore()
        assert saved.preset == "eco"
        assert saved.temp == 19.0

    def test_multi_flicker_preserves_saved_state(self):
        """Multiple home/away flickers must preserve the original saved state.

        Scenario: active(comfort) → home → away → home → away → home → restore
        """
        c = self._ctrl()
        c.activate("comfort", 21.0)

        for _ in range(3):
            home_fake = FakeCallLater()
            c.handle_presence_home(
                None, 30, lambda now: None, call_later=home_fake
            )
            away_fake = FakeCallLater()
            c.handle_presence_away(None, 600, lambda now: None, call_later=away_fake)
            # Home timer cancelled, no new away timer
            assert home_fake.calls[0]["cancelled"]
            assert away_fake.scheduled_count == 0

        # After all flickers, saved state is still the original
        assert c.is_active
        saved = c.restore()
        assert saved.preset == "comfort"
        assert saved.temp == 21.0


# ---------------------------------------------------------------------------
# SavedState
# ---------------------------------------------------------------------------

class TestSavedState:

    def test_defaults_are_none(self):
        s = SavedState()
        assert s.preset is None
        assert s.temp is None

    def test_fields_set_correctly(self):
        s = SavedState(preset="eco", temp=19.5)
        assert s.preset == "eco"
        assert s.temp == 19.5



# ---------------------------------------------------------------------------
# TRV source: configured valve or cloud fallback
# ---------------------------------------------------------------------------

TrvReading = _ctrl_mod.TrvReading
choose_trv = _ctrl_mod.choose_trv
read_trv = _ctrl_mod.read_trv
command_limits = _ctrl_mod.command_limits
plausible_c = _ctrl_mod.plausible_c

HOMEKIT = "climate.valve_homekit"
CLOUD = "climate.room_cloud"


class TestTrvSource:

    def test_reads_reading_setpoint_and_range(self):
        assert read_trv("heat", {"current_temperature": 20.4, "temperature": "21.5",
                                 "min_temp": 5, "max_temp": 25}) == {
            "internal": 20.4, "setpoint": 21.5, "min": 5.0, "max": 25.0, "off": False,
            "heating": None,
        }

    @pytest.mark.parametrize("action, heating", [
        ("heating", True), ("idle", False), ("off", False), (None, None),
    ])
    def test_reads_what_the_valve_says_it_is_doing(self, action, heating):
        """hvac_action: the Tado cloud derives it from the zone's heating
        power, HomeKit reports the valve's own state; absent when the entity
        has none."""
        attrs = {"current_temperature": 20.4, "temperature": 21.5}
        if action is not None:
            attrs["hvac_action"] = action
        assert read_trv("heat", attrs)["heating"] is heating

    @pytest.mark.parametrize("state", [None, "unavailable", "unknown"])
    def test_unavailable_gives_nothing_even_with_stale_attributes(self, state):
        r = read_trv(state, {"current_temperature": 20.4, "temperature": 21.5,
                             "min_temp": 5, "max_temp": 25})
        assert r == {"internal": None, "setpoint": None, "min": None, "max": None,
                     "off": False, "heating": None}

    @pytest.mark.parametrize("bad", ["x", float("nan"), float("inf"), True, None])
    def test_bad_values_are_none(self, bad):
        r = read_trv("heat", {"current_temperature": bad, "temperature": bad})
        assert r["internal"] is None and r["setpoint"] is None

    @pytest.mark.parametrize("fault", [0.0 - 40, 85.0, 127.5])
    def test_implausible_reading_is_none(self, fault):
        assert read_trv("heat", {"current_temperature": fault})["internal"] is None

    def test_off_still_reads(self):
        """A HomeKit Tado valve that is off: no setpoint, and HA accepts only
        7-35 °C for it until it heats again."""
        r = read_trv("off", {"current_temperature": 19.0, "min_temp": 7,
                             "max_temp": 35})
        assert r == {"internal": 19.0, "setpoint": None, "min": 7.0, "max": 35.0,
                     "off": True, "heating": None}

    def test_primary_wins_when_usable(self):
        p = TrvReading(HOMEKIT, 20.0, 21.0)
        f = TrvReading(CLOUD, 19.5, 21.0)
        assert choose_trv(p, f) is p

    def test_fallback_while_primary_unavailable(self):
        p = TrvReading(HOMEKIT)
        chosen = choose_trv(p, TrvReading(CLOUD, 19.5, 21.0))
        assert chosen == TrvReading(CLOUD, 19.5, 21.0, is_fallback=True)

    def test_no_fallback_configured_waits_for_primary(self):
        p = TrvReading(HOMEKIT)
        assert choose_trv(p, None) is p

    def test_unusable_fallback_waits_for_primary(self):
        p = TrvReading(HOMEKIT)
        assert choose_trv(p, TrvReading(CLOUD)) is p

    def test_fallback_equal_to_primary_is_ignored(self):
        p = TrvReading(HOMEKIT)
        assert choose_trv(p, TrvReading(HOMEKIT, 19.5, 21.0)) is p

    # prefer_fallback: the roles swap (e.g. the Tado cloud first, HomeKit as
    # the backup); a fallback reading is still marked is_fallback.

    def test_prefer_fallback_wins_when_usable(self):
        p = TrvReading(HOMEKIT, 20.0, 21.0)
        chosen = choose_trv(p, TrvReading(CLOUD, 19.5, 21.0), prefer_fallback=True)
        assert chosen == TrvReading(CLOUD, 19.5, 21.0, is_fallback=True)

    def test_prefer_fallback_uses_primary_while_fallback_unavailable(self):
        p = TrvReading(HOMEKIT, 20.0, 21.0)
        assert choose_trv(p, TrvReading(CLOUD), prefer_fallback=True) is p

    def test_prefer_fallback_waits_for_it_when_neither_reads(self):
        chosen = choose_trv(TrvReading(HOMEKIT), TrvReading(CLOUD), prefer_fallback=True)
        assert chosen.entity_id == CLOUD and chosen.is_fallback

    def test_prefer_without_fallback_keeps_primary(self):
        p = TrvReading(HOMEKIT, 20.0, 21.0)
        assert choose_trv(p, None, prefer_fallback=True) is p

    def test_prefer_fallback_equal_to_primary_is_ignored(self):
        p = TrvReading(HOMEKIT)
        assert choose_trv(p, TrvReading(HOMEKIT, 19.5, 21.0), prefer_fallback=True) is p


class TestCommandLimits:

    def test_own_range_without_trv_limits(self):
        assert command_limits(5.0, 25.0, None, None) == (5.0, 25.0)

    def test_narrowed_to_what_the_trv_accepts(self):
        assert command_limits(5.0, 25.0, 7.0, 35.0) == (7.0, 25.0)
        assert command_limits(5.0, 25.0, 5.0, 22.0) == (5.0, 22.0)

    def test_nonsense_trv_range_is_ignored(self):
        assert command_limits(5.0, 25.0, 30.0, 35.0) == (5.0, 25.0)


class TestPlausible:

    @pytest.mark.parametrize("value, expected", [
        (21.3, 21.3), ("19.5", 19.5), (-5.0, -5.0),
        (0.0 - 31, None), (61.0, None), ("unknown", None), (None, None),
        (float("nan"), None),
    ])
    def test_plausible(self, value, expected):
        assert plausible_c(value) == expected


# ---------------------------------------------------------------------------
# Lost commands and command spacing
# ---------------------------------------------------------------------------

trv_setpoint_drifted = _ctrl_mod.trv_setpoint_drifted
CommandPacer = _ctrl_mod.CommandPacer


class TestTrvDrift:

    def test_holds_the_command(self):
        assert not trv_setpoint_drifted(20.0, 20.0, 600, 120, 0.3)

    def test_lost_or_overridden_command(self):
        assert trv_setpoint_drifted(20.0, 17.0, 600, 120, 0.3)

    def test_not_checked_before_the_grace(self):
        assert not trv_setpoint_drifted(20.0, 17.0, 60, 120, 0.3)

    def test_small_difference_ignored(self):
        assert not trv_setpoint_drifted(20.0, 20.2, 600, 120, 0.3)

    @pytest.mark.parametrize("sent, held", [(None, 17.0), (20.0, None)])
    def test_unknown_is_not_drift(self, sent, held):
        assert not trv_setpoint_drifted(sent, held, 600, 120, 0.3)

    def test_switched_off_while_asked_to_heat(self):
        """Turned off from the Tado app: an off valve has no setpoint."""
        assert trv_setpoint_drifted(20.0, None, 600, 120, 0.3, trv_off=True)

    def test_off_after_a_frost_command_holds_it(self):
        """Tado shows its 5 °C frost setpoint as off."""
        assert not trv_setpoint_drifted(5.0, None, 600, 120, 0.3, trv_off=True,
                                        floor_c=5.0)

    def test_off_not_checked_before_the_grace(self):
        assert not trv_setpoint_drifted(20.0, None, 60, 120, 0.3, trv_off=True)


trv_holds_command = _ctrl_mod.trv_holds_command
resend_grace_s = _ctrl_mod.resend_grace_s
pending_command = _ctrl_mod.pending_command
CommandRecord = _ctrl_mod.CommandRecord
PENDING_HEAT = _ctrl_mod.PENDING_HEAT
PENDING_OFF = _ctrl_mod.PENDING_OFF


class TestTrvHoldsCommand:

    def test_heat_command_held_within_the_threshold(self):
        assert trv_holds_command(20.0, 20.1, 0.3)
        assert not trv_holds_command(20.0, 20.5, 0.3)

    def test_heat_command_not_held_off_or_on_schedule(self):
        assert not trv_holds_command(20.0, 20.0, 0.3, trv_off=True)
        assert not trv_holds_command(20.0, 20.0, 0.3, trv_scheduled=True)

    def test_off_command_held_only_while_off(self):
        assert trv_holds_command(5.0, None, 0.3, trv_off=True)
        assert not trv_holds_command(5.0, 5.0, 0.3)            # heat at 5 °C
        assert not trv_holds_command(5.0, None, 0.3, trv_scheduled=True)

    def test_nothing_known_is_not_holding(self):
        assert not trv_holds_command(None, 20.0, 0.3)
        assert not trv_holds_command(20.0, None, 0.3)


class TestResendGrace:

    def test_doubles_up_to_the_cap(self):
        assert [resend_grace_s(360, n, 1800) for n in range(5)] == [
            360, 720, 1440, 1800, 1800]

    def test_never_below_the_base(self):
        assert resend_grace_s(360, 3, 100) == 360
        assert resend_grace_s(120, -1, 1800) == 120

    def test_a_huge_count_does_not_overflow(self):
        assert resend_grace_s(120, 10_000, 1800) == 1800


class TestPendingCommand:

    def _p(self, attempt, ok, since, *, shown=False, heat_since="same",
           heat_ok=None, grace=360.0, base=360.0):
        """The last attempt; for a heat attempt the heat record is the same
        attempt unless given."""
        heat = attempt is not None and attempt > 5.05
        if heat_since == "same":
            heat_since = since if heat else None
        return pending_command(
            attempt_c=attempt, attempt_ok=ok, attempt_shown=shown, since_s=since,
            heat_since_s=heat_since,
            heat_ok=ok if heat_ok is None else heat_ok,
            base_grace_s=base, grace_s=grace, floor_c=5.0,
        )

    def test_heat_pending_until_the_grace(self):
        assert self._p(25.0, True, 10) == PENDING_HEAT
        assert self._p(25.0, True, 359) == PENDING_HEAT
        assert self._p(25.0, True, 360) is None

    def test_a_failed_heat_command_is_pending_for_the_base_grace_only(self):
        """It may have landed (a timeout), but it is no reason to back off."""
        assert self._p(25.0, False, 300, grace=1440) == PENDING_HEAT
        assert self._p(25.0, False, 400, grace=1440) is None
        assert self._p(25.0, True, 400, grace=1440) == PENDING_HEAT

    def test_an_off_is_pending_only_when_the_trv_took_it(self):
        assert self._p(5.0, True, 10) == PENDING_OFF
        assert self._p(5.0, False, 10) is None     # retried at the rate limit
        assert self._p(5.0, True, 400) is None

    def test_an_off_the_state_has_shown_is_no_longer_pending(self):
        """2.8.6 review: the valve showed off, then the app turned it on; an
        explicit OFF must not wait for the rest of the grace."""
        assert self._p(5.0, True, 60, shown=True) is None

    def test_a_failed_off_does_not_end_the_heat_before_it(self):
        """2.8.6 review: 25 °C sent, the off 10 s later failed; the stale
        "off" state must still not be believed (the watchdog was disarmed)."""
        assert self._p(5.0, False, 50, heat_since=60.0, heat_ok=True) == PENDING_HEAT
        # A successful off does end it (the heat record is cleared then, and
        # the off's own window decides).
        assert self._p(5.0, True, 400, heat_since=410.0, heat_ok=True) is None

    def test_nothing_tried_or_the_clock_went_back(self):
        assert self._p(None, True, 10) is None
        assert self._p(25.0, True, -5) is None


class TestCommandRecord:

    def _record(self, **kw):
        base = dict(entity_id="climate.trv", attempt_c=25.0, attempt_ts=1000.0,
                    attempt_ok=True, sent_c=25.0, heat_timer_ts=1000.0,
                    attempt_shown=False, heat_c=25.0, heat_ts=1000.0, heat_ok=True)
        base.update(kw)
        return CommandRecord(**base)

    def test_round_trip(self):
        rec = self._record()
        assert CommandRecord.from_dict(rec.as_dict()) == rec

    def test_a_half_heat_record_is_dropped(self):
        data = self._record().as_dict()
        data["heat_ts"] = None
        rec = CommandRecord.from_dict(data)
        assert rec.heat_c is None and rec.heat_ts is None

    @pytest.mark.parametrize("data", [
        None, {}, {"version": 99, "attempt_ts": 1.0}, {"version": 1},
        {"version": 1, "attempt_ts": "soon"},
    ])
    def test_unusable_data(self, data):
        assert CommandRecord.from_dict(data) is None

    def test_usable_only_when_recent_and_for_this_room(self):
        rec = self._record()
        trvs = ("climate.trv", None)
        assert rec.usable(1010.0, 360.0, trvs)
        assert not rec.usable(1360.0, 360.0, trvs)          # too old
        assert not rec.usable(990.0, 360.0, trvs)           # in the future
        assert not rec.usable(1010.0, 360.0, ("climate.other",))
        assert not self._record(attempt_c=None).usable(1010.0, 360.0, trvs)
        assert not self._record(entity_id=None).usable(1010.0, 360.0, (None,))


class TestCommandPacer:

    def test_first_command_goes_at_once(self):
        assert CommandPacer(5.0).reserve(100.0) == 0.0

    def test_commands_at_the_same_moment_are_spaced(self):
        p = CommandPacer(5.0)
        assert [p.reserve(100.0) for _ in range(4)] == [0.0, 5.0, 10.0, 15.0]

    def test_no_wait_once_the_gap_has_passed(self):
        p = CommandPacer(5.0)
        p.reserve(100.0)
        assert p.reserve(106.0) == 0.0

    def test_partial_wait(self):
        p = CommandPacer(5.0)
        p.reserve(100.0)
        assert p.reserve(102.0) == pytest.approx(3.0)

    def test_zero_spacing_never_waits(self):
        p = CommandPacer(0.0)
        assert [p.reserve(100.0) for _ in range(3)] == [0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# Pauses
# ---------------------------------------------------------------------------

integration_step = _ctrl_mod.integration_step


class TestIntegrationStep:

    def test_first_cycle(self):
        assert integration_step(1000.0, 0.0, 120.0) == 0.0

    def test_normal_cycle(self):
        assert integration_step(1060.0, 1000.0, 120.0) == 60.0

    def test_long_pause_is_capped(self):
        assert integration_step(8200.0, 1000.0, 120.0) == 120.0

    def test_clock_went_back(self):
        assert integration_step(900.0, 1000.0, 120.0) == 0.0


# ---------------------------------------------------------------------------
# Configured entities that disappear
# ---------------------------------------------------------------------------

entity_problem = _ctrl_mod.entity_problem
MissingEntityWatch = _ctrl_mod.MissingEntityWatch
replace_entity_setting = _ctrl_mod.replace_entity_setting


class TestEntityProblem:

    @pytest.mark.parametrize("has_state, registered, disabled, unavailable, expected", [
        (True, True, False, False, None),
        (False, True, False, False, None),     # registered, integration still loading
        (True, False, False, False, None),     # YAML entity without a unique id
        (False, False, False, False, "missing"),
        (False, True, True, False, "disabled"),
        (True, True, False, True, "unavailable"),
        (False, True, False, True, None),      # no state yet: still loading
        (False, False, False, True, "missing"),
    ])
    def test_problem(self, has_state, registered, disabled, unavailable, expected):
        assert entity_problem(has_state, registered, disabled, unavailable) == expected


class TestMissingEntityWatch:

    def test_reported_only_after_the_delay(self):
        w = MissingEntityWatch(300)
        assert w.update(1000, {"trv": "missing"}) == {}
        assert w.update(1299, {"trv": "missing"}) == {}
        assert w.update(1300, {"trv": "missing"}) == {"trv": "missing"}
        assert w.update(1360, {"trv": "missing"}) == {"trv": "missing"}

    def test_back_in_between_starts_again(self):
        w = MissingEntityWatch(300)
        w.update(1000, {"trv": "missing"})
        w.update(1200, {"trv": None})
        assert w.update(1400, {"trv": "missing"}) == {}
        assert w.update(1700, {"trv": "missing"}) == {"trv": "missing"}

    def test_delay_per_problem(self):
        w = MissingEntityWatch(300, {"unavailable": 900})
        assert w.update(1000, {"room": "unavailable", "trv": "missing"}) == {}
        assert w.update(1300, {"room": "unavailable", "trv": "missing"}) == {
            "trv": "missing"}
        assert w.update(1900, {"room": "unavailable", "trv": "missing"}) == {
            "room": "unavailable", "trv": "missing"}

    def test_no_longer_configured_is_forgotten(self):
        w = MissingEntityWatch(300)
        w.update(1000, {"window": "missing"})
        w.update(1100, {})
        assert w.update(1400, {"window": "missing"}) == {}


class TestReplaceEntitySetting:

    def test_trv_lives_in_the_data(self):
        data, opts = replace_entity_setting(
            {"source_entity_id": "climate.old", "name": "x"}, {"a": 1},
            "source_entity_id", "climate.new", in_data=True)
        assert data == {"source_entity_id": "climate.new", "name": "x"}
        assert opts == {"a": 1}

    def test_room_sensor_set_in_the_options(self):
        data, opts = replace_entity_setting(
            {"external_temperature_entity_id": "sensor.old"}, {},
            "external_temperature_entity_id", "sensor.new", in_data=False)
        assert data == {"external_temperature_entity_id": "sensor.old"}
        assert opts == {"external_temperature_entity_id": "sensor.new"}

    def test_optional_setting_removed(self):
        _, opts = replace_entity_setting(
            {}, {"window_sensor_id": "binary_sensor.old", "b": 2},
            "window_sensor_id", None, in_data=False)
        assert opts == {"b": 2}

    def test_trv_cannot_be_removed(self):
        with pytest.raises(ValueError):
            replace_entity_setting({}, {}, "source_entity_id", None, in_data=True)

    def test_inputs_are_not_changed(self):
        data, opts = {"source_entity_id": "climate.old"}, {"window_sensor_id": "b.x"}
        replace_entity_setting(data, opts, "window_sensor_id", None, in_data=False)
        assert opts == {"window_sensor_id": "b.x"}


migrate_options_v1_to_v2 = _ctrl_mod.migrate_options_v1_to_v2


class TestMigrateOptions:
    """Entries made before 2.2 (one controller, Kp is the gain near the
    target, the braking time a visible default)."""

    OLD = {
        "correction_kp": 0.6, "correction_ki": 0.003, "derivative_time_min": 0,
        "stepped_heating_enabled": True, "gain_scheduling_enabled": False,
        "gain_fine_multiplier": 1, "gain_startup_multiplier": 1.5,
        "gain_startup_threshold_c": 2, "gain_fine_threshold_c": 1,
        "min_command_interval_s": 120, "comfort_target": 21,
    }

    def test_obsolete_options_go_and_the_rest_stays(self):
        new = migrate_options_v1_to_v2(self.OLD, 4.0, 10.0)
        assert new == {
            "correction_kp": 4.0, "correction_ki": 0.003, "derivative_time_min": 10.0,
            "min_command_interval_s": 120, "comfort_target": 21,
        }

    def test_a_braking_time_already_set_is_kept(self):
        new = migrate_options_v1_to_v2({**self.OLD, "derivative_time_min": 20}, 4.0, 10.0)
        assert new["derivative_time_min"] == 20

    @pytest.mark.parametrize("bad", [None, "x", float("nan"), -5])
    def test_bad_braking_time_becomes_the_default(self, bad):
        new = migrate_options_v1_to_v2({"derivative_time_min": bad}, 4.0, 10.0)
        assert new["derivative_time_min"] == 10.0

    def test_unset_values_stay_unset(self):
        """No Kp or braking time stored: the defaults apply anyway."""
        assert migrate_options_v1_to_v2({"comfort_target": 21}, 4.0, 10.0) == {
            "comfort_target": 21}

    def test_input_is_not_changed(self):
        old = dict(self.OLD)
        migrate_options_v1_to_v2(old, 4.0, 10.0)
        assert old == self.OLD


# ---------------------------------------------------------------------------
# Changing the room's TRV (e.g. HomeKit valve -> Tado cloud thermostat)
# ---------------------------------------------------------------------------

change_trv = _ctrl_mod.change_trv


class TestChangeTrv:

    DATA = {"source_entity_id": HOMEKIT, "name": "Study",
            "external_temperature_entity_id": "sensor.study"}

    def test_to_the_fallback_removes_the_fallback(self):
        data, opts = change_trv(
            self.DATA,
            {"fallback_entity_id": CLOUD, "prefer_fallback": True, "correction_kp": 4},
            CLOUD,
        )
        assert data == {**self.DATA, "source_entity_id": CLOUD}
        assert opts == {"correction_kp": 4}

    def test_to_another_entity_keeps_the_fallback(self):
        opts_in = {"fallback_entity_id": CLOUD, "prefer_fallback": True}
        data, opts = change_trv(self.DATA, opts_in, "climate.other")
        assert data["source_entity_id"] == "climate.other"
        assert opts == opts_in

    def test_inputs_are_not_changed(self):
        data_in, opts_in = dict(self.DATA), {"fallback_entity_id": CLOUD}
        change_trv(data_in, opts_in, CLOUD)
        assert data_in == self.DATA and opts_in == {"fallback_entity_id": CLOUD}

    def test_slow_readings(self):
        assert not TrvReading(HOMEKIT, 20.0).slow
        assert TrvReading(CLOUD, 20.0, is_cloud=True).slow
        assert TrvReading(CLOUD, 20.0, is_fallback=True).slow
