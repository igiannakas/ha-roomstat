"""Preset management mixin for RoomstatClimate."""

from __future__ import annotations

import logging
import math
import time

from homeassistant.components.climate import (
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    HVACMode,
)
from homeassistant.core import callback

from .climate_controllers import SavedState, window_recovery_wait
from .const import (
    CONF_COMFORT_TARGET,
    CONF_PRESENCE_AWAY_DELAY_S,
    CONF_PRESENCE_HOME_DELAY_S,
    CONF_PRESENCE_OVERRIDE_MAX_MIN,
    CONF_PRESENCE_SENSOR_ID,
    CONF_WINDOW_CLOSE_DELAY_S,
    CONF_WINDOW_DELAY_S,
    CONF_WINDOW_SENSOR_ID,
    DEFAULT_PRESENCE_OVERRIDE_MAX_MIN,
    PRESET_FROST_PROTECTION,
    PRESET_LIST,
    PRESET_SCHEDULE,
    safe_float,
)
from .parameters import (
    FROST_PROTECT_C,
    WINDOW_RECOVERY_MAX_S,
    WINDOW_RECOVERY_POLL_S,
    WINDOW_RECOVERY_RATE_C_PER_H,
)

_LOGGER = logging.getLogger(__name__)


def async_call_later_boost(hass, delay_s, callback):
    """Thin wrapper so boost timer scheduling stays in this module."""
    from homeassistant.helpers.event import async_call_later  # noqa: PLC0415
    return async_call_later(hass, delay_s, callback)


class PresetMixin:
    """Preset management methods extracted from RoomstatClimate."""

    # ------------------------------------------------------------------
    # Boost timer helpers
    # ------------------------------------------------------------------

    @property
    def boost_remaining_minutes(self) -> int:
        """Return remaining boost minutes (0 when boost is not active)."""
        if self._boost_cancel is None:
            return 0
        remaining = max(0.0, self._boost_end_ts - time.time())
        return math.ceil(remaining / 60)

    # ------------------------------------------------------------------
    # Window sensor
    # ------------------------------------------------------------------

    @callback
    def _async_window_changed(self, event) -> None:
        """Handle window sensor state changes."""
        if self._summer_active:
            return
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unavailable", "unknown"):
            return

        if new_state.state == "on":  # window opened
            self._window_recovery_logged = False
            delay = self._config_entry.options.get(CONF_WINDOW_DELAY_S, 30)
            self._window_ctrl.handle_window_opened(
                self.hass, delay, self._async_window_action
            )
        else:  # window closed
            close_delay = self._config_entry.options.get(CONF_WINDOW_CLOSE_DELAY_S, 120)
            should_restore = self._window_ctrl.handle_window_closed(
                self.hass, close_delay, self._async_window_close_action
            )
            if should_restore:
                self._restore_window_state()

    async def _async_window_action(self, _now) -> None:
        """Switch to frost protection preset after window-open delay."""
        # Already in window mode: never snapshot again.  The current preset is
        # the window-driven frost protection, so saving it would lose the real
        # pre-open preset.  Summer mode ignores the window entirely.
        if self._window_ctrl.is_active or self._summer_active:
            _LOGGER.debug("Window action skipped: window mode already active")
            return

        # Revalidate: only proceed if window sensor is still "on"
        window_sensor = self._config_entry.options.get(CONF_WINDOW_SENSOR_ID)
        if window_sensor:
            current = self.hass.states.get(window_sensor)
            if current is None or current.state != "on":
                _LOGGER.info(
                    "%s: window action skipped, the sensor is now %s",
                    self._config_entry.title,
                    current.state if current else "unavailable",
                )
                self._window_ctrl.cancel_all()
                return

        # If boost is active, cancel it and use the pre-boost preset as saved state
        if self._boost_cancel is not None:
            self._boost_cancel()
            self._boost_cancel = None
            self._boost_end_ts = 0.0
            saved_preset = self._boost_saved_preset
            saved_temp = self._boost_saved_temp
        else:
            saved_preset = self._preset_mode
            saved_temp = self._target_temp
        # A frost protection preset here was chosen by the user (window mode
        # is not active – checked above), so it is a valid restore target.
        self._window_ctrl.activate(saved_preset, saved_temp)
        self._preset_mode = PRESET_FROST_PROTECTION
        _LOGGER.info("%s: window open, switching to frost protection", self._config_entry.title)
        self.async_write_ha_state()
        await self._async_regulation_cycle(trigger="window_open")

    async def _async_window_close_action(self, _now) -> None:
        """Restore the previous preset once the close delay has run and the
        room has stopped re-warming on its own.

        The configured delay is the minimum.  After it the room is usually
        still recovering the air the window took (the walls, furniture and
        radiator kept their heat): heating that joins in overshoots, so the
        wait is extended, one poll at a time, while fresh readings since the
        close show the room rising at WINDOW_RECOVERY_RATE_C_PER_H or more,
        for at most WINDOW_RECOVERY_MAX_S after the close.
        """
        now = time.time()
        waited = self._window_ctrl.close_delay_elapsed_s(now)
        slope, sensor_ok = self._room_recovery_slope()
        if window_recovery_wait(
            slope, waited, sensor_ok=sensor_ok,
            max_wait_s=WINDOW_RECOVERY_MAX_S, rate_c_per_h=WINDOW_RECOVERY_RATE_C_PER_H,
        ):
            if not self._window_recovery_logged:
                self._window_recovery_logged = True
                _LOGGER.info(
                    "%s: window closed, the room is re-warming on its own%s; "
                    "heating waits until that slows below %.1f °C/h (at most "
                    "%d min)",
                    self._config_entry.title,
                    "" if slope is None else f" at {slope * 3600:+.1f} °C/h",
                    WINDOW_RECOVERY_RATE_C_PER_H, int(WINDOW_RECOVERY_MAX_S / 60),
                )
            self._window_ctrl.extend_close_delay(
                self.hass, WINDOW_RECOVERY_POLL_S, self._async_window_close_action
            )
            return
        if self._window_recovery_logged:
            _LOGGER.info(
                "%s: room recovery over after %d min%s",
                self._config_entry.title, int(waited / 60),
                "" if slope is None else f" ({slope * 3600:+.1f} °C/h)",
            )
        self._window_recovery_logged = False
        self._restore_window_state()

    def _end_window_close_delay(self, reason: str) -> None:
        """A change by hand during the window's close delay ends the wait.

        The room first takes back what the window had saved (quietly, as the
        close would), so the change applies on top of the real state: a
        Boost then ends on the preset from before the window, not on its
        frost protection, and while nobody is home the room is in away mode
        again, for :meth:`_presence_takeover` to leave properly.
        """
        if not self._window_ctrl.close_delay_active:
            return
        _LOGGER.info("%s: window close delay ended, %s", self._config_entry.title, reason)
        self._window_ctrl.cancel_timers()  # the close timer must not fire later
        self._restore_window_state(notify=False)

    def _restore_window_state(self, notify: bool = True) -> None:
        """Restore preset after window is closed.

        ``notify=False`` is used during ``async_added_to_hass``: the entity is
        not fully added yet, so no state write or regulation task is started
        (the platform writes the state right after, the timer regulates).
        """
        saved = self._window_ctrl.restore()
        if saved.preset is not None:
            preset_to_restore = saved.preset
            # Frost protection is restored as-is: it can only have been saved
            # when the user had selected it before the window opened.
            # A saved BOOST (selected while the window was open) is not
            # restarted: boost is a short one-off, so return to the schedule
            # preset (COMFORT without a schedule).
            if preset_to_restore == PRESET_BOOST:
                preset_to_restore = self._fallback_preset()
                _LOGGER.info(
                    "%s: window restore, BOOST replaced by %s",
                    self._config_entry.title, preset_to_restore,
                )
            # Safety net: don't restore AWAY when presence sensor shows home
            if preset_to_restore == PRESET_AWAY:
                presence_sensor = self._config_entry.options.get(CONF_PRESENCE_SENSOR_ID)
                if presence_sensor:
                    ps = self.hass.states.get(presence_sensor)
                    if ps and ps.state not in ("off", "unavailable", "unknown"):
                        preset_to_restore = self._fallback_preset()
                        _LOGGER.info(
                            "%s: window restore, AWAY overridden to %s "
                            "(presence is home)",
                            self._config_entry.title, preset_to_restore,
                        )
            self._preset_mode = preset_to_restore
            if preset_to_restore == PRESET_COMFORT:
                self._target_temp = self._comfort_target()
            elif preset_to_restore == PRESET_NONE and saved.temp is not None:
                self._target_temp = saved.temp
            elif preset_to_restore != saved.preset:
                self._target_temp = self._get_preset_target(preset_to_restore)
            elif saved.temp is not None:
                self._target_temp = saved.temp
            self._drop_stale_sticky_override()
        _LOGGER.info("%s: window closed, restoring the previous preset", self._config_entry.title)
        if not notify:
            return
        self.hass.async_create_task(
            self._async_regulation_cycle(trigger="window_closed")
        )
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Presence sensor
    # ------------------------------------------------------------------

    @callback
    def _async_presence_changed(self, event) -> None:
        """Handle presence sensor state changes."""
        if self._summer_active:
            return
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unavailable", "unknown"):
            return

        if new_state.state == "off":  # nobody home
            delay = self._config_entry.options.get(CONF_PRESENCE_AWAY_DELAY_S, 600)
            self._presence_ctrl.handle_presence_away(
                self.hass, delay, self._async_presence_away_action
            )
        else:  # someone home
            home_delay = self._config_entry.options.get(CONF_PRESENCE_HOME_DELAY_S, 30)
            if self._presence_ctrl.handle_presence_home(
                self.hass, home_delay, self._async_presence_home_action,
            ):
                self._restore_presence_state()

    async def _async_presence_away_action(self, _now, *, unknown_is_away: bool = False) -> bool:
        """Switch to AWAY preset after presence-away delay.

        Also the way back to away from an override (its limit ran out, or
        Away / Resume schedule picked by hand while nobody is home).  Then
        ``unknown_is_away``: a presence sensor that reports nothing just now
        (unavailable, unknown, gone) keeps the last thing it said, nobody
        home; otherwise the room would heat on the change with no limit
        until the sensor next reads off.
        Returns True when away mode was entered (the room is regulated, or
        frost protection stays for an open window), False when skipped.
        """
        title = self._config_entry.title
        if self._summer_active:
            return False
        # Safety: if the controller is already active (e.g. a stale timer fired
        # after a sensor flicker), do not overwrite the saved preset.
        if self._presence_ctrl.is_active:
            _LOGGER.info(
                "%s: presence away action skipped, already away", title
            )
            return False

        # Revalidate: only proceed if presence sensor is still "off"
        presence_sensor = self._config_entry.options.get(CONF_PRESENCE_SENSOR_ID)
        if presence_sensor:
            current = self.hass.states.get(presence_sensor)
            state = current.state if current is not None else None
            reports_nothing = state is None or state in ("unavailable", "unknown")
            if state != "off" and not (unknown_is_away and reports_nothing):
                _LOGGER.info(
                    "%s: presence away action skipped, the sensor is now %s",
                    title, state if state is not None else "unavailable",
                )
                self._presence_ctrl.cancel_timer()
                return False

        # If window automation is active, don't override frost protection.
        # Save current state for when presence returns, but keep frost mode.
        if self._window_ctrl.is_active:
            self._presence_ctrl.activate(
                self._window_ctrl.get_saved().preset or PRESET_COMFORT,
                self._window_ctrl.get_saved().temp,
            )
            # Update window saved state to AWAY so frost->close restores AWAY
            self._window_ctrl.update_saved(PRESET_AWAY, self._config.presets.away_target_c)
            _LOGGER.info("%s: nobody home while the window is open, AWAY saved for later", title)
            self._write_state_with_binary_sensor()
            return True

        # If boost is active, cancel it and use the pre-boost preset as saved state
        if self._boost_cancel is not None:
            self._boost_cancel()
            self._boost_cancel = None
            self._boost_end_ts = 0.0
            saved_preset = self._boost_saved_preset
            saved_temp = self._boost_saved_temp
        else:
            saved_preset = self._preset_mode
            saved_temp = self._target_temp
        # Never save AWAY as restore state – fall back to COMFORT so the user
        # isn't stuck in AWAY→AWAY after restore (e.g. after HA restart where
        # the presence sensor was briefly unavailable at boot).
        if saved_preset == PRESET_AWAY:
            saved_preset = self._fallback_preset()
            saved_temp = self._get_preset_target(saved_preset)
            # An Away picked by hand becomes away mode: its sticky schedule
            # override goes with it, or it would hide the schedule after the
            # return (also when the schedule's own preset then is Off).
            if self._schedule_ctrl.override_active and self._schedule_ctrl.override_sticky:
                self._schedule_ctrl.clear_override()
                _LOGGER.info("%s: the Away picked by hand is now away mode", title)
        self._presence_ctrl.activate(saved_preset, saved_temp)
        self._preset_mode = PRESET_AWAY
        _LOGGER.info("%s: nobody home, switching to AWAY", title)
        self.async_write_ha_state()
        await self._async_regulation_cycle(trigger="presence_away")
        return True

    async def _async_presence_home_action(self, _now) -> None:
        """Restore previous preset after presence-home delay."""
        # Revalidate: only proceed if presence sensor is still "on"
        presence_sensor = self._config_entry.options.get(CONF_PRESENCE_SENSOR_ID)
        if presence_sensor:
            current = self.hass.states.get(presence_sensor)
            if current is None or current.state == "off":
                _LOGGER.info(
                    "%s: presence home action skipped, the sensor is now %s",
                    self._config_entry.title,
                    current.state if current else "unavailable",
                )
                return
        self._restore_presence_state()

    def _restore_presence_state(self, notify: bool = True) -> None:
        """Restore preset after presence returns.

        After a change by hand while nobody was home (an override) nothing is
        restored: that change is the room's state, and it simply stays.

        ``notify=False``: see :meth:`_restore_window_state`.
        """
        title = self._config_entry.title
        if self._presence_ctrl.override_active:
            self._presence_ctrl.restore()  # ends the override
            _LOGGER.info(
                "%s: someone is home, the change made while nobody was home stays",
                title,
            )
            if notify:
                self._write_state_with_binary_sensor()
            return

        saved = self._presence_ctrl.restore()
        if saved.preset is None:
            _LOGGER.info("%s: someone is home, nothing to restore", title)
            return

        # If window automation is active, update the window's saved state
        # instead of changing the current preset (frost protection stays).
        if self._window_ctrl.is_active:
            self._window_ctrl.update_saved(saved.preset, saved.temp)
            _LOGGER.info(
                "%s: someone is home while the window is open, %s saved for the close",
                title, saved.preset,
            )
            return

        preset_to_restore = saved.preset
        # A saved BOOST (selected while away) is not restarted: boost is a
        # short one-off, so return to the schedule preset (COMFORT without one).
        if preset_to_restore == PRESET_BOOST:
            preset_to_restore = self._fallback_preset()
            _LOGGER.info("%s: presence restore, BOOST replaced by %s", title, preset_to_restore)
        self._preset_mode = preset_to_restore
        # If restoring COMFORT, take the current comfort_target from options
        # (it may have been changed via number entity while away).
        if preset_to_restore == PRESET_COMFORT:
            self._target_temp = self._comfort_target()
        elif saved.temp is not None:
            self._target_temp = saved.temp
        self._drop_stale_sticky_override()
        _LOGGER.info("%s: someone is home, restoring %s", title, preset_to_restore)
        if not notify:
            return
        self.hass.async_create_task(
            self._async_regulation_cycle(trigger="presence_home")
        )
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # A change by hand while nobody is home
    # ------------------------------------------------------------------

    def _presence_override_limit_s(self) -> float:
        """How long a change by hand while nobody is home lasts (0 = until home)."""
        minutes = safe_float(
            self._config_entry.options.get(
                CONF_PRESENCE_OVERRIDE_MAX_MIN, DEFAULT_PRESENCE_OVERRIDE_MAX_MIN
            )
        )
        if minutes is None or minutes < 0:
            minutes = DEFAULT_PRESENCE_OVERRIDE_MAX_MIN
        return minutes * 60.0

    def _presence_takeover(self) -> None:
        """Let a change by hand while nobody is home take effect.

        Called before a temperature or preset (not Away) picked by the user
        is applied.  Away mode steps aside: the room is regulated as if
        someone were home until someone is, or until the configured limit
        after the change (then back to away).  Leaving away mode, the room
        first takes back the preset it had before (as coming home would), so
        a Boost started now returns to that, not to Away.  An open window
        still wins: the change is saved for its close as usual.
        """
        ctrl = self._presence_ctrl
        if not ctrl.nobody_home:
            return
        saved = ctrl.get_saved() if ctrl.is_active else None
        limit_s = self._presence_override_limit_s()
        ctrl.start_override(
            self.hass, time.time(), limit_s, self._async_presence_override_expired
        )
        if saved is not None and self._preset_mode == PRESET_AWAY:
            base = saved.preset
            if base in (None, PRESET_AWAY, PRESET_BOOST):
                base = self._fallback_preset()
                saved = SavedState(base, self._get_preset_target(base))
            self._apply_saved_preset(SavedState(base, saved.temp))
        _LOGGER.info(
            "%s: changed by hand while nobody is home, away is held off until "
            "someone is home%s",
            self._config_entry.title,
            f" or for {round(limit_s / 60)} min at most" if limit_s > 0 else "",
        )

    async def _async_presence_override_expired(self, _now) -> None:
        """Nobody came home within the limit after a change by hand.

        Back to away – unless the change heats no more than away would (Off,
        or a temperature at or below the away target): the limit only guards
        against heating an empty house, so such a change stays, and is
        looked at again after another limit.
        """
        ctrl = self._presence_ctrl
        if not ctrl.override_active:
            return
        title = self._config_entry.title
        if self._summer_active:
            ctrl.override_timer_fired()
            return
        limit_s = self._presence_override_limit_s()
        if limit_s <= 0:
            # The option became "until someone is home" meanwhile.
            ctrl.clear_limit()
            self._write_state_with_binary_sensor()
            return
        if self._held_change_target_c() <= self._config.presets.away_target_c + 0.05:
            ctrl.extend_override(
                self.hass, time.time(), limit_s, self._async_presence_override_expired
            )
            _LOGGER.info(
                "%s: nobody came home, but the change heats no more than away: it "
                "stays (looked at again in %d min)",
                title, round(limit_s / 60),
            )
            self._write_state_with_binary_sensor()
            return
        ctrl.override_timer_fired()
        _LOGGER.info("%s: nobody came home after the change by hand, back to away", title)
        await self._async_reenter_away(trigger="presence_override_expired")

    def _held_change_target_c(self) -> float:
        """The target of the change holding away off.

        With a window open, the change is what the window will restore (its
        frost protection is temporary).  A room switched off heats to frost.
        """
        if self._hvac_mode == HVACMode.OFF:
            return FROST_PROTECT_C
        if self._window_ctrl.is_active:
            saved = self._window_ctrl.get_saved()
            preset, temp = saved.preset, saved.temp
        else:
            preset, temp = self._preset_mode, self._target_temp
        if preset is None:
            return self._effective_setpoint()
        if preset == PRESET_NONE:
            return temp if temp is not None else self._target_temp
        return self._get_preset_target(preset)

    def _presence_reads_home(self) -> bool:
        """The presence sensor reads home right now (not off, and reporting)."""
        sensor = self._config_entry.options.get(CONF_PRESENCE_SENSOR_ID)
        if not sensor:
            return False
        st = self.hass.states.get(sensor)
        return st is not None and st.state not in ("off", "unavailable", "unknown")

    async def _async_presence_back_to_away(self) -> None:
        """Away picked by hand while nobody is home: cancel a change by hand.

        Not a manual (sticky) Away: away mode is what nobody-home means.  A
        change made meanwhile is undone – the schedule override it started
        too – so coming home restores what it would have without it: the
        schedule's preset, else what away mode had saved before the change.
        """
        title = self._config_entry.title
        ctrl = self._presence_ctrl
        if not ctrl.override_active:
            _LOGGER.info("%s: Away picked while nobody is home, already away", title)
            self._write_state_with_binary_sensor()
            return
        before = ctrl.get_before_override()
        ctrl.end_override()
        schedule_preset = (
            self._schedule_ctrl.schedule_preset if self.schedule_configured else None
        )
        if schedule_preset is not None:
            self._schedule_ctrl.clear_override()
            base = SavedState(schedule_preset, self._get_preset_target(schedule_preset))
        elif before.preset not in (None, PRESET_AWAY, PRESET_BOOST):
            base = before
        else:
            base = SavedState(PRESET_COMFORT, self._comfort_target())
        self._apply_quietly(base)
        _LOGGER.info(
            "%s: Away picked while nobody is home, the change is cancelled and %s "
            "comes back with you",
            title, base.preset,
        )
        await self._async_reenter_away(trigger="preset_change")

    def _apply_quietly(self, saved: SavedState) -> None:
        """Make *saved* the room's preset without regulating.

        Used right before going back to away, which saves it for the return
        (so no heat call is sent and taken back).  A running boost ends; with
        a window open it becomes the window's restore target instead.
        """
        if self._boost_cancel is not None:
            self._boost_cancel()
            self._boost_cancel = None
            self._boost_end_ts = 0.0
        if self._window_ctrl.is_active:
            self._window_ctrl.update_saved(saved.preset, saved.temp)
        else:
            self._apply_saved_preset(saved)

    async def _async_reenter_away(self, trigger: str) -> None:
        """Go back to away mode after an override, or regulate if that's skipped.

        The away action saves the room's current preset, so coming home
        carries on with it.  It is skipped when someone is home again (the
        presence sensor reads home): the room then carries on as it is.  A
        sensor that reports nothing just now keeps "nobody home".
        """
        if not await self._async_presence_away_action(None, unknown_is_away=True):
            self._write_state_with_binary_sensor()
            await self._async_regulation_cycle(trigger=trigger)

    # ------------------------------------------------------------------
    # Preset switching
    # ------------------------------------------------------------------

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode (user / service call)."""
        self._summer_guard("preset change")
        if preset_mode == PRESET_SCHEDULE:
            await self.async_resume_schedule()
            return
        if preset_mode not in PRESET_LIST:
            _LOGGER.warning("Unknown preset mode: %s", preset_mode)
            return
        # Away while nobody is home is away mode itself, not a manual
        # (sticky) Away: it ends a change by hand, and coming home restores.
        # Once the sensor reads home again (the home delay still running) it
        # is an ordinary Away picked by someone who is home.
        if (
            preset_mode == PRESET_AWAY
            and self._presence_ctrl.nobody_home
            and not self._presence_reads_home()
        ):
            await self._async_presence_back_to_away()
            return
        # A manual preset change overrides the schedule (see climate_schedule.py)
        self._schedule_note_manual_preset(preset_mode)
        await self._async_apply_preset(preset_mode, trigger="preset_change", manual=True)

    async def _async_apply_preset(
        self, preset_mode: str, trigger: str, *, manual: bool = False
    ) -> None:
        """Apply a preset (or save it for window/presence restore) and regulate.

        ``manual``: picked by the user, so it takes effect while nobody is
        home (see :meth:`_presence_takeover`).
        """
        if self._route_preset(preset_mode, manual=manual):
            self._write_state_with_binary_sensor()
            await self._async_regulation_cycle(trigger=trigger)
        else:
            self._write_state_with_binary_sensor()

    def _route_preset(self, preset_mode: str, *, manual: bool = False) -> bool:
        """Apply *preset_mode* now, or save it while an automation is active.

        Returns True when the preset was applied (the caller regulates), False
        when it was saved for later restore by window / presence automation.
        No state is written here.  A preset picked by hand (``manual``) is
        never held back by away mode, only by an open window.
        """
        title = self._config_entry.title
        # A preset during the window's close delay ends the wait.
        self._end_window_close_delay(f"preset changed to {preset_mode}")

        if self._presence_ctrl.is_active and preset_mode != PRESET_AWAY:
            if manual:
                # Picked by hand while nobody is home: it takes effect.
                self._presence_takeover()
            else:
                # The schedule or a boost ending while away: update what
                # coming home restores, and keep away mode.
                self._presence_ctrl.update_saved(
                    preset_mode, self._get_preset_target(preset_mode)
                )
                _LOGGER.info(
                    "%s: nobody home, %s saved for the return", title, preset_mode
                )
                return False
        elif manual and self._presence_ctrl.override_active:
            # A further change by hand restarts the limit.
            self._presence_takeover()

        # If window automation is active (frost protection due to open window),
        # update the saved state so the new preset is restored when the window
        # closes, but keep frost protection active.  This includes selecting
        # frost protection itself: the user wants frost after the window closes.
        if self._window_ctrl.is_active:
            self._window_ctrl.update_saved(preset_mode, self._get_preset_target(preset_mode))
            _LOGGER.info(
                "%s: window open, %s saved for the close, keeping frost protection",
                title, preset_mode,
            )
            return False

        old_preset = self._preset_mode
        self._preset_mode = preset_mode

        # Cancel any running boost timer
        if self._boost_cancel is not None:
            self._boost_cancel()
            self._boost_cancel = None
            self._boost_end_ts = 0.0

        # When switching to COMFORT, restore the stored comfort target
        if preset_mode == PRESET_COMFORT:
            self._target_temp = self._comfort_target()

        # Start boost timer if entering boost mode
        if preset_mode == PRESET_BOOST:
            # Only update the saved preset if we're not already in boost,
            # otherwise keep the original pre-boost preset to avoid a loop
            # where boost restores into boost indefinitely.
            if old_preset != PRESET_BOOST:
                self._boost_saved_preset = old_preset
                self._boost_saved_temp = self._target_temp
            duration_s = self._config.presets.boost_duration_min * 60
            self._boost_end_ts = time.time() + duration_s
            self._boost_cancel = async_call_later_boost(
                self.hass, duration_s, self._async_boost_expired
            )
            _LOGGER.info(
                "%s: boost started for %d min", title, self._config.presets.boost_duration_min
            )

        _LOGGER.debug("%s: preset changed: %s → %s", title, old_preset, preset_mode)
        return True

    async def _async_boost_expired(self, _now) -> None:
        """Called when the boost timer expires – revert to previous preset."""
        self._boost_cancel = None
        self._boost_end_ts = 0.0

        title = self._config_entry.title
        # With a schedule, boost ends back on the schedule (and ends the
        # override the boost started).
        if self.schedule_configured and self._schedule_ctrl.schedule_preset:
            self._schedule_ctrl.clear_override()
            _LOGGER.info(
                "%s: boost over, returning to the schedule (%s)",
                title, self._schedule_ctrl.schedule_preset,
            )
            await self._async_follow_schedule(trigger="boost_expired")
            return

        restore_preset = self._boost_saved_preset
        _LOGGER.info("%s: boost over, back to %s", title, restore_preset)

        # PRESET_NONE (manual mode) is not in PRESET_LIST and would be rejected
        # by async_set_preset_mode, so handle it directly.
        if restore_preset == PRESET_NONE:
            # Don't override active window/presence automation – save for later.
            if self._window_ctrl.is_active:
                self._window_ctrl.update_saved(PRESET_NONE, self._boost_saved_temp)
                _LOGGER.info("%s: boost over while the window is open, saved for the close", title)
                return
            if self._presence_ctrl.is_active:
                self._presence_ctrl.update_saved(PRESET_NONE, self._boost_saved_temp)
                _LOGGER.info("%s: boost over while nobody is home, saved for the return", title)
                return
            if self._boost_saved_temp is not None:
                self._target_temp = self._boost_saved_temp
            self._preset_mode = PRESET_NONE
            self.async_write_ha_state()
            await self._async_regulation_cycle(trigger="boost_expired")
        else:
            await self._async_apply_preset(restore_preset, trigger="boost_expired")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _comfort_target(self) -> float:
        """Return the configured comfort target, falling back to the default.

        The comfort_target option is absent from config_entry.options until the
        Comfort Target number entity is edited once. Reading it with no default
        yields None; callers previously skipped the assignment and left
        _target_temp stuck at a frost/eco value. Fall back to the PresetConfig
        default (20 °C) so comfort always resolves.
        """
        comfort = safe_float(self._config_entry.options.get(CONF_COMFORT_TARGET))
        if comfort is not None:
            return comfort
        return self._config.presets.comfort_target_c

    def _get_preset_target(self, preset_mode: str) -> float:
        """Return the target temperature for a given preset mode."""
        if preset_mode == PRESET_COMFORT:
            return self._comfort_target()
        if preset_mode == PRESET_ECO:
            return self._config.presets.eco_target_c
        if preset_mode == PRESET_BOOST:
            return self._config.presets.boost_target_c
        if preset_mode == PRESET_AWAY:
            return self._config.presets.away_target_c
        if preset_mode == PRESET_FROST_PROTECTION:
            return self._config.presets.frost_protection_target_c
        return self._target_temp

    # ------------------------------------------------------------------
    # Effective setpoint calculation
    # ------------------------------------------------------------------

    def _effective_setpoint(self) -> float:
        """Calculate the effective setpoint based on HVAC mode and preset."""
        if self._summer_active:
            return FROST_PROTECT_C
        if self._hvac_mode == HVACMode.OFF:
            return FROST_PROTECT_C

        if self._preset_mode in (PRESET_COMFORT, PRESET_NONE):
            return self._target_temp
        if self._preset_mode == PRESET_ECO:
            return self._config.presets.eco_target_c
        if self._preset_mode == PRESET_BOOST:
            return self._config.presets.boost_target_c
        if self._preset_mode == PRESET_AWAY:
            return self._config.presets.away_target_c
        return self._config.presets.frost_protection_target_c
