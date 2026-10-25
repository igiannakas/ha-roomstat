"""Regulation cycle mixin for RoomstatClimate."""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.climate import PRESET_BOOST, HVACMode
from homeassistant.exceptions import HomeAssistantError

from .activity import (
    PATH_NO_DATA,
    PATH_OFF,
    PATH_REGULATED,
    PATH_SENSOR_LOST,
    PATH_SUMMER,
    PATH_WAITING,
)
from .autotune import AutotuneSample
from .climate_controllers import (
    BAND_WARM,
    PENDING_HEAT,
    PENDING_OFF,
    CommandPacer,
    CommandRecord,
    OutOfBandMonitor,
    TrvReading,
    command_limits,
    cycle_stalled,
    format_time_period,
    heat_timer_duration,
    heat_timer_refresh_due,
    integration_step,
    is_maximum,
    is_off_command,
    pending_command,
    resend_grace_s,
    trv_holds_command,
    trv_setpoint_drifted,
)
from .const import (
    CONF_FALLBACK_ENTITY_ID,
    CONF_WINDOW_SENSOR_ID,
    TADO_DOMAIN,
    TADO_SERVICE_TIMER,
)
from .parameters import (
    FALLBACK_DRIFT_GRACE_S,
    MAX_INTEGRATION_STEP_S,
    RESEND_BACKOFF_MAX_S,
    TRV_COMMAND_SPACING_S,
    TRV_COMMAND_TIMEOUT_S,
    TRV_DRIFT_GRACE_S,
)
from .regulation import MODE_COAST, MODE_HEAT, MODE_HOLD, resume_mode

_LOGGER = logging.getLogger(__name__)

# One pacer for every Roomstat room: commands to the TRVs (which share one
# Tado bridge) go out at least TRV_COMMAND_SPACING_S apart.
COMMAND_PACER = CommandPacer(TRV_COMMAND_SPACING_S)


async def _wait_for_command_slot() -> None:
    """Wait for this command's turn (see CommandPacer)."""
    delay = COMMAND_PACER.reserve(time.monotonic())
    if delay > 0:
        await asyncio.sleep(delay)


class RegulationMixin:
    """Regulation cycle methods extracted from RoomstatClimate."""

    def _write_state_with_binary_sensor(self) -> None:
        """Write HA state for this entity and linked sub-entities."""
        self._update_out_of_band(time.time())
        self.async_write_ha_state()
        bs = getattr(self.coordinator, "binary_sensor_entity", None)
        if bs is not None:
            bs.async_write_ha_state()
        band = getattr(self.coordinator, "out_of_band_entity", None)
        if band is not None and band.hass is not None:
            band.async_write_ha_state()
        for attr in ("sensor_entity", "schedule_sensor_entity"):
            se = getattr(self.coordinator, attr, None)
            if se is not None and se.hass is not None:
                se.async_write_ha_state()
        for ent in getattr(self.coordinator, "tuning_entities", ()):
            if ent.hass is not None:
                ent.async_write_ha_state()
        activity = getattr(self.coordinator, "activity_entity", None)
        if activity is not None and activity.hass is not None:
            activity.async_write_ha_state()

    def _window_sensor_on(self) -> bool:
        sensor = self._config_entry.options.get(CONF_WINDOW_SENSOR_ID)
        state = self.hass.states.get(sensor) if sensor and self.hass is not None else None
        return state is not None and state.state == "on"

    def _window_open(self) -> bool:
        """A window is open: the window sensor says so (also during its open
        delay), or the window automation or its close delay is running."""
        if self._window_ctrl.is_active or self._window_ctrl.close_delay_active:
            return True
        return self._window_sensor_on()

    def _window_airing(self) -> bool:
        """Air is being exchanged right now: the sensor is on, or the window
        automation is active and not yet in its close delay.  The room
        readings then show the draught, not the room, and the slope history
        is dropped so the predictions after the close start from fresh
        readings (18 Oct 2026: with the dip still in the 15-min window a hold
        pushed full power on a room rising 2.5 °C/h on its own)."""
        if self._window_ctrl.is_active and not self._window_ctrl.close_delay_active:
            return True
        return self._window_sensor_on()

    def _room_recovery_slope(self) -> tuple[float | None, bool]:
        """The fresh room slope (°C/s, None while unknown) for the window
        close wait, and whether the room sensor can show a recovery at all."""
        sensor_ok = self._last_valid_room_temp is not None and not self._sensor_degraded
        return (self._slope.slope_c_per_s() if sensor_ok else None), sensor_ok

    def _update_out_of_band(self, now: float) -> None:
        """Feed the out-of-band monitor (binary_sensor.<room>_out_of_band).

        It judges only normal heating: heating on, no summer lock, no window
        open and a fresh room reading.  The active TRV's own word (heating or
        not) and reading tell a hot radiator from the sun.  A change is
        logged once; "warm" (too warm, radiator not the cause) at info.
        """
        cfg = self._band_config
        was = self._band_monitor.state
        try:
            eligible = (
                self._hvac_mode == HVACMode.HEAT
                and not self._summer_active
                and not self._sensor_degraded
                and not self._window_open()
            )
            trv = self._active_trv()
            state = self._band_monitor.update(
                now,
                (self.coordinator.data or {}).get("room_temp"),
                self._effective_setpoint(),
                eligible=eligible,
                band_c=cfg.band_c,
                hot_after_s=cfg.band_min * 60.0,
                cold_after_s=cfg.cold_band_min * 60.0,
                valve_heating=trv.heating,
                trv_c=trv.internal_c,
            )
        except Exception:  # noqa: BLE001 - reporting must never stop the state writes
            if not self._band_failed:
                self._band_failed = True
                self._log(_LOGGER).exception(
                    "the out-of-band check failed"
                )
            return
        minutes = int((now - state.since) / 60) if state.since is not None else 0
        setpoint = self._effective_setpoint()
        if state.on and (not was.on or was.reason != state.reason):
            self._log(_LOGGER).warning(
                "out of band (%s) for %d min: %s", state.reason, minutes,
                OutOfBandMonitor.describe(state, setpoint, cfg.band_c),
            )
        elif state.reason == BAND_WARM and was.reason != BAND_WARM:
            # Also hot turning warm: the radiator has cooled, the room has not.
            self._log(_LOGGER).info(
                "too warm for %d min, not from the radiator (not an alarm): %s",
                minutes, OutOfBandMonitor.describe(state, setpoint, cfg.band_c),
            )
        elif was.on and not state.on:
            self._log(_LOGGER).info("back within the band")

    def _autotune_eligible(self) -> bool:
        """Whether this cycle is normal heating the auto-tuner may learn from.

        Window / presence automations, boost, summer mode and a stale sensor
        all produce behaviour that says nothing about the room's dynamics.
        """
        return (
            self._hvac_mode == HVACMode.HEAT
            and not self._summer_active
            and not self._window_ctrl.is_active
            and not self._window_ctrl.close_delay_active
            and not self._presence_ctrl.is_active
            and self._preset_mode != PRESET_BOOST
            and not self._sensor_degraded
        )

    def _feed_autotune(self, now, setpoint, room_temp, tado_internal, result) -> None:
        """Hand one cycle to the auto-tuner; apply any change it makes.

        The tuner must never be able to stop heating control: any exception
        switches it off for this session and the configured values are used.
        """
        if not self._autotune_enabled or self._autotune_failed:
            return
        tado_sp = (
            self._last_sent_setpoint
            if self._last_sent_setpoint is not None
            else self.coordinator.data.get("tado_setpoint")
        )
        try:
            reason = self._autotuner.observe(
                AutotuneSample(
                    ts=now,
                    setpoint_c=setpoint,
                    room_temp_c=None if self._sensor_degraded else room_temp,
                    tado_internal_c=tado_internal,
                    tado_setpoint_c=tado_sp,
                    eligible=self._autotune_eligible(),
                    command_saturated=result.is_saturated,
                    room_temp_ts=self.coordinator.data.get("room_temp_ts"),
                    window=self._window_open(),
                )
            )
        except Exception:  # noqa: BLE001 - isolate the tuner from the control loop
            self._log(_LOGGER).exception(
                "auto-tune failed and is paused until restart; using the "
                "configured Kp/Ki/braking time",
            )
            self._autotune_failed = True
            self._apply_active_tuning()
            return
        # What it learned, changed, or decided not to change (once each).
        self._activity_tuner()
        if reason:
            self._apply_active_tuning()

    async def _async_regulation_cycle_timer(self, _now) -> None:
        """Periodic timer callback."""
        await self._async_regulation_cycle(trigger="timer")

    async def _async_regulation_cycle(self, trigger: str) -> None:
        """Execute one control cycle."""
        async with self._regulation_lock:
            await self._async_regulation_cycle_locked(trigger)

    async def _async_regulation_cycle_locked(self, trigger: str) -> None:
        """Inner regulation cycle body, protected by _regulation_lock."""
        now = time.time()
        # The wall clock went back (NTP correction, restored VM snapshot): a
        # command "sent in the future" would block every command until the
        # clock caught up again.  Its age is unknown: it no longer counts for
        # the rate limit, nor as on its way into the TRV's state.
        if self._last_command_sent_ts > now:
            self._last_command_sent_ts = now - max(
                self._config.min_command_interval_s, RESEND_BACKOFF_MAX_S
            )
        if self._last_heat_attempt_ts is not None and self._last_heat_attempt_ts > now:
            self._clear_heat_attempt()
        # Likewise a timer "sent in the future" would not be renewed before it
        # runs out: its start counts as unknown, so it is renewed now.
        if self._heat_timer_sent_ts is not None and self._heat_timer_sent_ts > now:
            self._heat_timer_sent_ts = None

        # Summer mode: no regulation, hold the TRV off.  The lock
        # is this cycle's decision (the watchdog counts it as completed).
        if self._summer_active:
            self._note_cycle_completed(now)
            await self._async_summer_enforce(now)
            self._activity_cycle(now, PATH_SUMMER)
            return

        # Off: no regulation, but make sure the TRV is off too.  The cycle
        # run by switching Roomstat off sends the off at once.
        if self._hvac_mode == HVACMode.OFF:
            self._note_cycle_completed(now)
            await self._async_keep_trv_off(now, user=trigger == "hvac_mode_change")
            self._activity_cycle(now, PATH_OFF)
            return

        # Guard: skip when coordinator data is stale (update method raised an exception).
        if not self.coordinator.last_update_success:
            _LOGGER.debug("Skipping regulation cycle – coordinator update failed")
            self._last_reason = "coordinator_unavailable"
            self._activity_cycle(now, PATH_NO_DATA)
            self._write_state_with_binary_sensor()
            return

        # 1. Gather sensor data from coordinator.  The TRV is the configured
        # one, or its fallback while the configured one is unavailable.
        room_temp = self.coordinator.data.get("room_temp")
        trv = self._active_trv()
        self._note_trv_source(trv)
        tado_internal = trv.internal_c

        # The TRV had no setpoint when the entity was added (its integration
        # still loading after a restart): decide the resume mode now, from
        # what the valve holds, before anything is sent (18 Oct 2026: the
        # study resumed in hold 0.07 above the target and pushed its valve
        # into the grey zone; a shut valve must resume as coast → stop).
        # (A command of ours still on its way into the TRV's state counts as
        # what it holds: see _held_command.)
        held = self._held_command(now, trv) if self._resume_pending else None
        if held is not None:
            self._resume_pending = False
            self._reg_state.mode = resume_mode(held, self._config)
            self._last_sent_was_max = is_maximum(held, self._config.max_target_c)
            _LOGGER.debug(
                "%s: resuming as %s from %.1f held by the valve",
                self._config_entry.title, self._reg_state.mode, held,
            )

        # Sensor resilience: bridge short gaps with last-valid value
        sensor_lost = False
        if room_temp is not None:
            if self._room_sensor_lost_logged:
                self._log(_LOGGER).info("room sensor is back")
            self._room_sensor_lost_logged = False
            self._room_missing_since = None
            self._last_valid_room_temp = room_temp
            self._last_valid_room_temp_ts = self.coordinator.data.get("room_temp_ts") or now
            self._sensor_degraded = False
        else:
            if self._room_missing_since is None:
                self._room_missing_since = now
            missing_for = now - self._room_missing_since
            if missing_for > self._sensor_grace_s:
                # Gone for longer than the grace period (also a sensor that
                # never reported after a restart).
                sensor_lost = True
                self._sensor_degraded = True
            elif self._last_valid_room_temp is not None:
                # A short gap: bridged with the last valid value.  Counted
                # from when the reading went missing, not from its last
                # change, so a sensor that only reports changes is bridged
                # as well.
                room_temp = self._last_valid_room_temp
                self._sensor_degraded = True
                _LOGGER.debug(
                    "Room temp unavailable – using last valid %.1f°C "
                    "(missing for %ds, grace %ds)",
                    room_temp, int(missing_for), self._sensor_grace_s,
                )
            else:
                self._sensor_degraded = False

        if sensor_lost and not self._room_sensor_lost_logged:
            # Nothing is sent until the sensor is back: the TRV keeps its
            # last setpoint.  The room sensor is meant to be reliable, so a
            # long outage is reported (a repair after
            # ROOM_SENSOR_UNAVAILABLE_REPORT_S, see repairs.py), not bridged.
            self._room_sensor_lost_logged = True
            self._log(_LOGGER).warning(
                "room sensor unavailable for %d min; the radiator keeps "
                "its last setting until it is back", int(missing_for / 60),
            )

        if sensor_lost and tado_internal is not None:
            # Without its sensor the room is not regulated: a deliberate
            # pause, reported by the warning above and the repair, so this
            # cycle has done its job (the watchdog does not report it again).
            # A maximum command is never left in place meanwhile: a heat-up,
            # or any maximum the TRV took (a hold clamped at 25), is ended
            # (coast).  Until the TRV has taken the stop, it is tried again
            # (once per rate-limit interval).
            if self._reg_state.mode == MODE_HEAT or self._last_sent_was_max:
                await self._async_decide_and_send(
                    now, trv, self._config.min_target_c, strong=True
                )
                if (
                    self._last_reason.startswith("sent(")
                    or self._last_reason == "already_at_target"
                ):
                    self._reg_state.mode = MODE_COAST
            else:
                self._last_reason = "waiting_for_sensors"
            self._note_cycle_completed(now)
            self._activity_cycle(
                now, PATH_SENSOR_LOST, sensor_lost_min=int(missing_for / 60)
            )
            self._write_state_with_binary_sensor()
            return

        if room_temp is None or tado_internal is None:
            self._last_reason = "waiting_for_sensors"
            self._activity_cycle(now, PATH_WAITING, room_c=room_temp)
            self._write_state_with_binary_sensor()
            return

        # 1b. From here on the cycle regulates and may send: whatever the TRV
        # reports later is our own command, not a resume.
        self._resume_pending = False

        # 2. Time delta, capped so a pause cannot add a large integral step.
        dt = integration_step(now, self._last_regulation_ts, MAX_INTEGRATION_STEP_S)
        self._last_regulation_ts = now

        # 3. Effective setpoint (considers HVAC mode + preset)
        setpoint = self._effective_setpoint()

        # 3b. Robust room slope for the heat-up and stop predictions (only on fresh
        # readings; a bridged/stale value must not look like a flat room).
        # While a window is open the readings are the draught: the history is
        # dropped, and refilled from the close on (the close wait reads it).
        slope = None
        if self._window_airing():
            self._slope.reset()
        elif not self._sensor_degraded:
            self._slope.add(now, room_temp, self.coordinator.data.get("room_temp_ts"))
            slope = self._slope.slope_c_per_s()

        # 4. Compute regulation
        prev_mode = self._reg_state.mode
        result = self._regulator.compute(
            setpoint_c=setpoint,
            room_temp_c=room_temp,
            tado_internal_c=tado_internal,
            time_delta_s=dt,
            state=self._reg_state,
            room_slope_c_per_s=slope,
        )
        self._reg_state = result.new_state
        self._last_result = result

        # 5-6. Send decision and command
        await self._async_decide_and_send(
            now, trv, result.target_for_tado_c, strong=result.mode != MODE_HOLD
        )

        # The braking time this cycle decided with (the tuner may change it).
        td_used_s = self._config.tuning.td_s

        # 7. Background auto-tune (no-op when disabled).  Runs after the
        # send so it sees the command the TRV now holds; any new values take
        # effect from the next cycle.  The TRV's own reading comes from the
        # configured TRV (e.g. HomeKit, pushed at once), even while the
        # fallback carries the commands: it is the same valve.  When only the
        # fallback reads, the tuner keeps learning from the room sensor but
        # without the TRV's reading: cloud polling can be minutes old, so the
        # TRV demand is unknown for those cycles (no demand crossing).  A
        # configured TRV that is itself a cloud entity (no local link at all)
        # is the only reading there is, so the tuner gets it: the demand
        # crossing comes from the command step, which shows at once.
        self._feed_autotune(
            now, setpoint, room_temp,
            self._source_trv_internal() if trv.is_fallback else tado_internal,
            result,
        )

        if _LOGGER.isEnabledFor(logging.DEBUG):
            # One line per cycle: the whole decision, so a week of a room's
            # behaviour can be read back from the log (enable debug logging
            # for custom_components.roomstat; the coordinator's own refresh
            # chatter is on custom_components.roomstat.coordinator).
            cmd = result.target_for_tado_c
            _LOGGER.debug(
                "%s: %s | sp %.1f room %.2f%s %s | trv %.1f%s%s | cmd %s"
                " P %+.2f I %+.2f | %s",
                self._config_entry.title, result.mode, setpoint, room_temp,
                " (fell fast: previous reading used)" if result.room_held else "",
                "no slope" if slope is None else f"{slope * 3600:+.2f} °C/h",
                tado_internal,
                " off" if trv.off else (" auto" if trv.scheduled else ""),
                "" if trv.heating is None else (" heating" if trv.heating else " idle"),
                "off" if is_off_command(cmd, self._config.min_target_c)
                else f"{cmd:.1f} ({cmd - tado_internal:+.1f} vs trv)",
                result.p_correction_c, result.i_correction_c, self._last_reason,
            )

        self._activity_cycle(
            now, PATH_REGULATED, room_c=room_temp, setpoint_c=setpoint,
            prev_mode=prev_mode, slope_c_per_s=slope, td_s=td_used_s,
        )
        self._note_cycle_completed(now)
        self._write_state_with_binary_sensor()

    # ------------------------------------------------------------------
    # Command bookkeeping
    # ------------------------------------------------------------------

    def _drift_grace_s(self, trv: TrvReading) -> float:
        """How long the TRV's state is given to show a command (longer for a
        cloud or fallback entity, read by polling)."""
        return FALLBACK_DRIFT_GRACE_S if trv.slow else TRV_DRIFT_GRACE_S

    def _resend_grace_s(self, trv: TrvReading) -> float:
        """The drift grace after the back-off for a TRV that keeps not showing
        a command it took (resend_grace_s)."""
        return resend_grace_s(
            self._drift_grace_s(trv), self._resend_count, RESEND_BACKOFF_MAX_S
        )

    def _pending(self, now: float, trv: TrvReading) -> str | None:
        """A command of ours may not show in the TRV's state yet:
        PENDING_HEAT, PENDING_OFF or None (see pending_command)."""
        heat_ts = self._last_heat_attempt_ts
        return pending_command(
            attempt_c=self._last_attempt_c,
            attempt_ok=self._last_attempt_ok,
            attempt_shown=self._last_attempt_shown,
            since_s=now - self._last_command_sent_ts,
            heat_since_s=None if heat_ts is None else now - heat_ts,
            heat_ok=self._last_heat_attempt_ok,
            base_grace_s=self._drift_grace_s(trv),
            grace_s=self._resend_grace_s(trv),
            floor_c=self._config.min_target_c,
        )

    def _held_command(self, now: float, trv: TrvReading) -> float | None:
        """What the TRV holds as far as we know: a command of ours still on
        its way into its state, else the setpoint it shows (None: unknown)."""
        pending = self._pending(now, trv)
        if pending == PENDING_HEAT:
            return self._last_heat_attempt_c
        if pending == PENDING_OFF:
            return self._config.min_target_c
        return trv.setpoint_c

    def _note_attempt(
        self, now: float, command_c: float, trv: TrvReading, ok: bool
    ) -> None:
        """Book a command just tried, sent or not.

        It counts for the rate limit (a failed command too: an outage costs
        one attempt per interval), and it is what pending_command judges: a
        heat command is kept as on its way until the state shows it or an
        off is taken (a failed off does not end it: the heat may be running).
        A resend because the TRV did not show a command it took
        (``_resend_due``, set by the drift checks) backs the next check off.
        The back-off ends only when the TRV is seen holding our command
        (trv_holds_command, or off for an off), or on a switch of entity: a
        hold command that creeps by 0.1 °C is no reason to think a state that
        stopped showing commands works again.
        """
        if self._resend_due:
            self._resend_count += 1
        self._resend_due = False
        self._last_command_sent_ts = now
        self._last_attempt_c = command_c
        self._last_attempt_ok = ok
        self._last_attempt_shown = False
        self._last_attempt_entity = trv.entity_id
        # What the sending cycle saw: a state that already looked like the
        # command can't tell whether it arrived (it may be a stale one).
        self._last_attempt_seen_other = not self._trv_holds(command_c, trv)
        if not is_off_command(command_c, self._config.min_target_c):
            self._last_heat_attempt_c = command_c
            self._last_heat_attempt_ts = now
            self._last_heat_attempt_ok = ok
        elif ok:
            self._clear_heat_attempt()

    def _clear_heat_attempt(self) -> None:
        """No heat command of ours is on its way any more."""
        self._last_heat_attempt_c = None
        self._last_heat_attempt_ts = None
        self._last_heat_attempt_ok = False

    def _trv_holds(self, command_c: float, trv: TrvReading) -> bool:
        """The TRV's state looks like *command_c* (see trv_holds_command)."""
        return trv_holds_command(
            command_c,
            trv.setpoint_c,
            self._config.min_change_threshold_c,
            trv_off=trv.off,
            floor_c=self._config.min_target_c,
            trv_scheduled=trv.scheduled,
        )

    def _note_trv_shows(self, trv: TrvReading) -> None:
        """Watch the TRV's state come round to our last command.

        It has *shown* the command once it reads like it after having read
        otherwise since the command was tried: a state that looked like it
        all along may be a stale one from before (a cloud zone's state lags
        by a minute or more: an "off" from before a heat command, or a 25 °C
        from before an off), so it proves nothing.  Once shown, the state is
        current: nothing of ours is on its way any more (pending_command).
        Unknown readings are skipped.
        """
        if self._last_attempt_c is None or self._last_attempt_shown or not trv.usable:
            return
        if not self._trv_holds(self._last_attempt_c, trv):
            self._last_attempt_seen_other = True
            return
        if self._last_attempt_seen_other:
            self._last_attempt_shown = True
            if not is_off_command(self._last_attempt_c, self._config.min_target_c):
                self._clear_heat_attempt()

    def _note_trv_holds_off(self) -> None:
        """The TRV is off, as an off command wants: the frost level is its
        baseline, and it holds no maximum and no timer of ours.  (Never
        while a heat command of ours may be landing: see pending_command.)"""
        self._last_sent_setpoint = self._config.min_target_c
        self._last_sent_was_max = False
        self._heat_timer_sent_ts = None
        self._resend_count = 0
        self._resend_due = False

    def _command_record(self) -> CommandRecord:
        """The last commands, for the restore-state store (CommandRecord)."""
        return CommandRecord(
            entity_id=self._last_attempt_entity,
            attempt_c=self._last_attempt_c,
            attempt_ts=self._last_command_sent_ts,
            attempt_ok=self._last_attempt_ok,
            attempt_shown=self._last_attempt_shown,
            attempt_seen_other=self._last_attempt_seen_other,
            sent_c=self._last_sent_setpoint,
            heat_timer_ts=self._heat_timer_sent_ts,
            heat_c=self._last_heat_attempt_c,
            heat_ts=self._last_heat_attempt_ts,
            heat_ok=self._last_heat_attempt_ok,
        )

    def _restore_command(self, data, now: float) -> bool:
        """Take back the last commands after an entry reload or a quick
        restart, when recent and sent to an entity this room still uses.
        Returns whether they were taken."""
        record = CommandRecord.from_dict(data)
        entities = (
            self._config_entry.data.get("source_entity_id"),
            self._config_entry.options.get(CONF_FALLBACK_ENTITY_ID),
        )
        if record is None or not record.usable(now, FALLBACK_DRIFT_GRACE_S, entities):
            return False
        self._last_attempt_entity = record.entity_id
        self._last_attempt_c = record.attempt_c
        self._last_command_sent_ts = record.attempt_ts
        self._last_attempt_ok = record.attempt_ok
        self._last_attempt_shown = record.attempt_shown
        self._last_attempt_seen_other = record.attempt_seen_other
        self._last_sent_setpoint = record.sent_c
        if record.heat_timer_ts is not None and record.heat_timer_ts <= now:
            self._heat_timer_sent_ts = record.heat_timer_ts
        if (
            record.heat_c is not None
            and record.heat_ts is not None
            and 0.0 <= now - record.heat_ts < FALLBACK_DRIFT_GRACE_S
        ):
            self._last_heat_attempt_c = record.heat_c
            self._last_heat_attempt_ts = record.heat_ts
            self._last_heat_attempt_ok = record.heat_ok
        return True

    # ------------------------------------------------------------------
    # Send decision
    # ------------------------------------------------------------------

    async def _async_decide_and_send(
        self, now: float, trv: TrvReading, target: float, *, strong: bool = False
    ) -> None:
        """Decide whether *target* goes to the TRV this cycle, and send it.

        Sets ``_last_reason``.  Rate limiting: at most one command per
        ``min_command_interval_s``, except an urgent decrease and a
        strong command (``strong``: heat, coast or stop, one command each,
        which must not wait).
        """
        # Prefer our own last-sent value (always fresh) over coordinator data
        # (which may be up to 60s stale from the polling interval).
        # NOTE: do NOT fall back to 0.0 – an unknown baseline makes the
        # urgent-decrease check (target < baseline - threshold) always False
        # for realistic temperatures, so a needed fast cooldown would be
        # suppressed.  Instead, skip rate limiting entirely when no baseline
        # is known (e.g. after a quick HVAC OFF → HEAT cycle).
        floor = self._config.min_target_c
        time_since_last = now - self._last_command_sent_ts
        # A TRV that no longer holds our last command (lost on the way, a
        # Tado schedule block or an expired timer overlay ended the override,
        # or it was switched off in the Tado app) is compared with what it
        # actually holds, so the command goes out again.  One that keeps not
        # showing it is asked less and less often (resend_grace_s).
        if trv_setpoint_drifted(
            self._last_sent_setpoint,
            trv.setpoint_c,
            time_since_last,
            self._resend_grace_s(trv),
            self._config.min_change_threshold_c,
            trv_off=trv.off,
            floor_c=floor,
            trv_scheduled=trv.scheduled,
        ):
            again = ""
            if self._resend_count:
                next_s = resend_grace_s(
                    self._drift_grace_s(trv), self._resend_count + 1, RESEND_BACKOFF_MAX_S
                )
                again = f" (again; next check in {next_s / 60:.0f} min)"
            self._log(_LOGGER).info(
                "%s holds %s, not the %s sent; sending again%s", trv.entity_id, _held_text(trv),
                _command_text(self._last_sent_setpoint, floor), again,
            )
            self._last_sent_setpoint = None
            # A valve that showed the command and then left it (Tado app, a
            # schedule block, an expired timer) is no reason to back off.
            self._resend_due = not self._last_attempt_shown
        elif trv.usable and trv_holds_command(
            self._last_sent_setpoint,
            trv.setpoint_c,
            self._config.min_change_threshold_c,
            trv_off=trv.off,
            floor_c=floor,
            trv_scheduled=trv.scheduled,
        ):
            self._resend_count = 0
            self._resend_due = False
        self._note_trv_shows(trv)

        # Our last command may not show in the TRV's state yet (a cloud
        # zone's state follows a command by up to a minute or more).  Judged
        # on the last command tried: one that timed out may have landed.
        pending = self._pending(now, trv)

        # Shut: hvac mode off, never a 5 °C setpoint (2.8.4).
        if is_off_command(target, floor):
            await self._async_decide_and_send_off(
                now, trv, strong=strong, time_since_last=time_since_last,
                pending=pending,
            )
            return

        # Keep the command inside what the TRV accepts right now (anything
        # else is refused before it is sent).
        lo, hi = command_limits(floor, self._config.max_target_c, trv.min_c, trv.max_c)
        target = max(lo, min(hi, target))

        # Fail cold: the maximum to a cloud TRV is a timer overlay, renewed
        # once half of it has passed while the command is still the maximum
        # (whatever the mode: heat, or a hold clamped there).  If Home
        # Assistant, the network or the cloud stop renewing it, the zone
        # falls back to its Tado schedule (5 °C or Off) on its own.
        maximum = is_maximum(target, hi)
        timer_s = heat_timer_duration(
            target, hi, cloud=trv.is_cloud, heat_timer_s=self._config.heat_timer_s
        )
        timer_refresh_due = timer_s is not None and heat_timer_refresh_due(
            now, self._heat_timer_sent_ts, timer_s
        )

        current_tado_setpoint = (
            self._last_sent_setpoint
            if self._last_sent_setpoint is not None
            else trv.setpoint_c  # None if the TRV is unavailable
        )
        if (trv.off or trv.scheduled) and pending != PENDING_HEAT:
            # Off, or back on its Tado schedule: the valve holds no heat
            # command of ours, whatever setpoint it shows (Tado reports 5 °C
            # while off, the schedule's block temperature while on it), so
            # the command must go out.  A valve is only ever heating or off.
            # (Not while our heat command may simply not show yet: the drift
            # check sends it again if it still does not after the grace.)
            current_tado_setpoint = floor
        is_rate_limited = time_since_last < self._config.min_command_interval_s

        should_send = False
        reason = "noop"

        if current_tado_setpoint is None:
            # No known Tado baseline – send to establish one.
            # On the very first attempt (_last_command_sent_ts == 0) we send
            # immediately.  On subsequent retries (e.g. after a failed send)
            # we honour the rate limiter so a transient TRV/backend outage
            # does not cause command spam every regulation cycle.
            if is_rate_limited and self._last_command_sent_ts > 0:
                remaining = int(self._config.min_command_interval_s - time_since_last)
                reason = f"rate_limited({remaining}s)"
            else:
                should_send = True
                reason = "no_baseline"
        else:
            diff = abs(target - current_tado_setpoint)

            # Overlay refresh: resend the same setpoint if overlay_refresh_s has
            # elapsed, keeping timer-based overlays alive (cloud-API integrations).
            overlay_refresh_due = (
                self._overlay_refresh_s > 0
                and time_since_last >= self._overlay_refresh_s
            )

            if diff < self._config.min_change_threshold_c and timer_refresh_due:
                # A strong command: it skips the rate limit once; a renewal
                # that failed waits for the interval like any other retry.
                if is_rate_limited and self._heat_timer_refresh_tried:
                    remaining = int(self._config.min_command_interval_s - time_since_last)
                    reason = f"rate_limited({remaining}s)"
                else:
                    should_send = True
                    reason = "heat_timer_refresh"
                    self._heat_timer_refresh_tried = True
            elif diff < self._config.min_change_threshold_c and not overlay_refresh_due:
                reason = "already_at_target"
            elif diff < self._config.min_change_threshold_c and overlay_refresh_due:
                should_send = True
                reason = "overlay_refresh"
            elif is_rate_limited:
                is_urgent_decrease = (
                    target
                    < current_tado_setpoint - self._behaviour.urgent_decrease_threshold_c
                )
                # Skipping the rate limit is for a new command only: a
                # command that just failed waits like any other, so an
                # outage still costs one attempt per interval.
                new_command = (
                    self._last_attempt_c is None
                    or abs(target - self._last_attempt_c) >= 0.05
                )
                if is_urgent_decrease and new_command:
                    should_send = True
                    reason = "urgent_decrease"
                elif strong and new_command:
                    should_send = True
                    reason = "strong"
                else:
                    remaining = int(self._config.min_command_interval_s - time_since_last)
                    reason = f"rate_limited({remaining}s)"
            else:
                should_send = True
                reason = "normal_update"

        if not should_send:
            self._last_reason = reason
            return
        sent = await self._async_send_to_tado(
            target, trv.entity_id, timer_s=timer_s, maximum=maximum
        )
        self._note_attempt(now, target, trv, sent)
        self._last_reason = f"{'sent' if sent else 'send_failed'}({reason})"

    async def _async_decide_and_send_off(
        self, now: float, trv: TrvReading, *, strong: bool, time_since_last: float,
        pending: str | None,
    ) -> None:
        """Shut the valve: hvac mode ``off`` (part of _async_decide_and_send).

        Done only while the TRV reports ``off``.  A valve in heat at any
        setpoint (5 °C included) or back on its Tado schedule (``auto``) is
        switched off: the owner's valves are only ever heating or off.  Rate
        limiting as for any command: a new off (from heat or a hold) skips
        it when strong (coast, stop) or an urgent decrease; a retry waits
        for the interval.  An off the TRV took is given the drift grace to
        show up before it is sent again.

        ``pending`` (pending_command): while a heat command of ours may not
        show yet, an "off" state may be the one from before it, and is not
        believed: the off is sent.  19 Oct 2026, second bedroom: the target
        went 20 → 22 → 20 within 10 s; 25 °C went out, the cloud state still
        showed off, the off was taken as done, and the valve ran at 100 %
        until switched off by hand (2.8.5; since 2.8.6 also when the heat
        command timed out but landed, or went to the other entity before a
        fallback switch, or before an entry reload).
        """
        floor = self._config.min_target_c
        if trv.off and pending != PENDING_HEAT:
            # The valve is off: the frost level is its baseline.
            self._note_trv_holds_off()
            self._last_reason = "already_at_target"
            return
        if pending == PENDING_OFF:
            # Our off went out; the TRV's state has not shown it yet.
            self._last_reason = "already_at_target"
            return
        is_rate_limited = time_since_last < self._config.min_command_interval_s
        new_command = self._last_attempt_c is None or not is_off_command(
            self._last_attempt_c, floor
        )
        if pending == PENDING_HEAT:
            held = self._last_heat_attempt_c
        elif self._last_sent_setpoint is not None:
            held = self._last_sent_setpoint
        else:
            held = trv.setpoint_c
        urgent = (
            held is not None
            and floor < held - self._behaviour.urgent_decrease_threshold_c
        )
        if not is_rate_limited or self._last_command_sent_ts <= 0:
            reason = "normal_update"
        elif new_command and urgent:
            reason = "urgent_decrease"
        elif new_command and strong:
            reason = "strong"
        else:
            remaining = int(self._config.min_command_interval_s - time_since_last)
            self._last_reason = f"rate_limited({remaining}s)"
            return
        sent = await self._async_send_off_to_tado(trv.entity_id)
        self._note_attempt(now, floor, trv, sent)
        self._last_reason = f"{'sent' if sent else 'send_failed'}(off_{reason})"

    async def _async_keep_trv_off(self, now: float, *, user: bool = False) -> None:
        """Roomstat is off: keep the TRV off as well.

        ``user``: Roomstat was just switched off.  The TRV is switched off at
        once, unless it is off already (one command fewer to a cloud zone).
        It runs under the regulation lock, so an off can never be overtaken
        by a heat command of a cycle in progress.

        Afterwards an off that failed (valve unreachable) is sent again once
        per rate-limit interval, and so is one the valve showed and then left
        (turned back on from the Tado app, a Tado schedule block).  One the
        TRV took but never showed (a state that no longer updates) waits for
        the drift grace, less and less often while it keeps not showing
        (resend_grace_s).  A heat command of ours that may still be landing
        is followed by an off at once.
        """
        trv = self._active_trv()
        self._note_trv_source(trv)
        floor = self._config.min_target_c
        since = now - self._last_command_sent_ts
        self._note_trv_shows(trv)
        pending = self._pending(now, trv)
        send = False
        if trv.off and pending != PENDING_HEAT:
            self._note_trv_holds_off()
        elif not trv.entity_id:
            pass
        elif user:
            # An explicit OFF: sent whatever is on its way (one command more
            # at worst), unless the valve is off already.
            send = True
        elif pending == PENDING_OFF or not (
            self.coordinator.last_update_success and trv.usable
        ):
            pass
        elif (
            pending == PENDING_HEAT
            or self._last_attempt_c is None
            or not is_off_command(self._last_attempt_c, floor)
        ):
            send = True
        elif since >= self._config.min_command_interval_s:
            self._log(_LOGGER).info(
                "%s holds %s, not off; sending off again", trv.entity_id, _held_text(trv),
            )
            send = True
            # Taken but never shown: back off (one shown and then left is a
            # new event: the app, a schedule block).
            self._resend_due = self._last_attempt_ok and not self._last_attempt_shown
        reason = "hvac_off"
        if send:
            sent = await self._async_send_off_to_tado(trv.entity_id)
            self._note_attempt(now, floor, trv, sent)
            reason = "sent(hvac_off)" if sent else "send_failed(hvac_off)"
        self._last_reason = reason
        self._write_state_with_binary_sensor()

    def _source_trv_internal(self) -> float | None:
        """The configured TRV's own reading this cycle (None if unavailable)."""
        return (self.coordinator.data or {}).get("tado_internal_temp")

    def _note_trv_source(self, trv) -> None:
        """Log a switch between the configured TRV and the fallback entity.

        Leaving the preferred entity is a warning, coming back to it is
        info.  The last sent setpoint is forgotten on a switch, so the next
        cycle compares its command with what the newly used entity reports (a
        command sent just before the switch may never have arrived).
        """
        if trv.is_fallback == self._trv_fallback_active:
            return
        self._trv_fallback_active = trv.is_fallback
        self._last_sent_setpoint = None
        # The back-off was for the other entity.  The last command tried is
        # kept: it went to the same valve, and may still be on its way into
        # the newly used entity's state (pending_command).
        self._resend_count = 0
        self._resend_due = False
        # Whether the newly used entity holds a timer, and since when, is not
        # known: a maximum it holds is renewed as a timer straight away.
        self._heat_timer_sent_ts = None
        self._heat_timer_refresh_tried = False
        source_id = self._config_entry.data.get("source_entity_id")
        fallback_id = self._config_entry.options.get(CONF_FALLBACK_ENTITY_ID)
        prefer_fallback = self._prefer_fallback()
        preferred, backup = (fallback_id, source_id) if prefer_fallback else (source_id, fallback_id)
        if trv.is_fallback == prefer_fallback:
            self._log(_LOGGER).info(
                "%s available again, back on it",
                preferred,
            )
        else:
            self._log(_LOGGER).warning(
                "%s unavailable, using %s",
                preferred,
                backup,
            )

    def _note_command_result(self, entity_id: str, what: str, err: BaseException | None) -> None:
        """Log command failures once per distinct error, not every attempt.

        An unreachable valve would otherwise put a traceback in the log every
        few minutes for as long as it is away.  The current error is kept for
        the ``last_command_error`` attribute.
        """
        title = self._config_entry.title
        if err is None:
            if self._last_command_error is not None:
                self._log(_LOGGER).info("commands to %s work again", entity_id)
            self._last_command_error = None
            return
        text = str(err) or type(err).__name__
        if isinstance(err, TimeoutError):
            text = f"no answer within {TRV_COMMAND_TIMEOUT_S:.0f} s"
        message = f"{what} to {entity_id} failed: {text}"
        if message == self._last_command_error:
            _LOGGER.debug("%s: %s", title, message)
        elif isinstance(err, (TimeoutError, HomeAssistantError)):
            self._log(_LOGGER).warning("%s", message)
        else:
            # Not an expected outcome of a service call: keep the traceback.
            self._log(_LOGGER).error("%s", message, exc_info=err)
        self._last_command_error = message

    async def _async_call_trv(
        self, entity_id: str, service: str, data: dict, *, domain: str = "climate"
    ) -> bool:
        """Call a service on the TRV (``climate`` by default, ``tado`` for a
        timer overlay).  Never raises (except when the task is cancelled): a
        failed command must not stop the cycle."""
        await _wait_for_command_slot()
        what = service if domain == "climate" else f"{domain}.{service}"
        try:
            async with asyncio.timeout(TRV_COMMAND_TIMEOUT_S):
                await self.hass.services.async_call(
                    domain=domain,
                    service=service,
                    service_data={"entity_id": entity_id, **data},
                    blocking=True,
                )
        except Exception as err:  # noqa: BLE001 - any failure is reported, never raised
            self._note_command_result(entity_id, what, err)
            return False
        self._note_command_result(entity_id, what, None)
        return True

    async def _async_send_off_to_tado(self, entity_id: str | None) -> bool:
        """Shut the valve with hvac mode ``off`` (the regulation's minimum).

        A Manual OFF overlay on a Tado cloud zone (it never ends by itself),
        HomeKit's inactive state on a HomeKit valve.  Coming back is one
        ``set_temperature`` with ``hvac_mode: heat``: both integrations
        switch an off valve to heat and set the temperature in the same
        call (checked live on the second bedroom, 19 Oct 2026; HA 2026.10.1
        ``tado/climate.py`` async_set_temperature, ``homekit_controller``
        thermostat).  ``climate.set_hvac_mode heat`` first would re-use the
        last temperature Home Assistant sent, 25 °C after a heat-up.
        Returns whether the TRV took it.
        """
        if not entity_id:
            self._log(_LOGGER).warning("No TRV entity – cannot switch it off")
            return False
        _LOGGER.debug("Switching %s off", entity_id)
        sent = await self._async_call_trv(entity_id, "set_hvac_mode", {"hvac_mode": HVACMode.OFF})
        if sent:
            # The frost level is the baseline of an off valve (a later heat
            # command is compared with it; the auto-tuner sees a negative
            # demand, as with the 5 °C setpoint before).
            self._last_sent_setpoint = self._config.min_target_c
            self._last_sent_was_max = False
            # An OFF overlay replaces any timer: nothing to renew or shut.
            self._heat_timer_sent_ts = None
        return sent

    async def _async_send_to_tado(
        self,
        target_c: float,
        entity_id: str | None = None,
        *,
        timer_s: float | None = None,
        maximum: bool = False,
    ) -> bool:
        """Send a temperature command to the TRV (default: the configured one).

        With ``timer_s`` it is a Tado timer overlay of that length
        (``tado.set_climate_timer``), else a Manual one
        (``climate.set_temperature``).  ``maximum``: the command is the
        highest the TRV takes (the watchdog replaces such a command if the
        cycle stops).  Returns whether the TRV took it.  A failed command is
        retried by the next cycle once the rate limit allows (no baseline,
        or the drift check).
        """
        source_entity = entity_id or self._config_entry.data.get("source_entity_id")
        if not source_entity:
            self._log(_LOGGER).warning("No source_entity_id configured – cannot send command to Tado")
            return False

        if timer_s:
            _LOGGER.debug(
                "Sending %.1f°C to %s as a %d s timer", target_c, source_entity, int(timer_s)
            )
            # temperature + time_period only: the timer service rejects
            # anything else (requested_overlay excludes time_period, and its
            # set_timer always heats).
            sent = await self._async_call_trv(
                source_entity,
                TADO_SERVICE_TIMER,
                {"temperature": target_c, "time_period": format_time_period(timer_s)},
                domain=TADO_DOMAIN,
            )
        else:
            _LOGGER.debug("Sending %.1f°C to %s", target_c, source_entity)
            sent = await self._async_call_trv(
                source_entity,
                "set_temperature",
                {"temperature": target_c, "hvac_mode": HVACMode.HEAT},
            )
        if sent:
            self._last_sent_setpoint = target_c
            self._last_sent_was_max = maximum
            if timer_s:
                # The renewal clock starts when the TRV took the timer.
                self._heat_timer_sent_ts = time.time()
                self._heat_timer_refresh_tried = False
            else:
                # A Manual overlay replaced any timer.
                self._heat_timer_sent_ts = None
        return sent

    # ------------------------------------------------------------------
    # Watchdog (fail cold when the cycle stops)
    # ------------------------------------------------------------------

    def _note_cycle_completed(self, now: float) -> None:
        """A regulation cycle reached its decision (see the watchdog)."""
        self._last_cycle_completed_ts = now
        if self._regulation_stalled:
            self._regulation_stalled = False
            self._watchdog_shut_tried = False
            self._log(_LOGGER).info("regulation is running again")

    async def _async_watchdog_check(self, _now=None) -> None:
        """Periodic watchdog, on its own timer (never the cycle's).

        No regulation cycle has reached a decision for
        ``cycle_watchdog_s``: the cycles stopped, or every one ends early
        (no TRV or room reading, the coordinator failing, an unexpected
        error).  The room is reported as stalled (warning once, attribute
        ``regulation_stalled``), and a maximum command still in place is
        replaced by the minimum: a strong command, which skips the rate limit
        once; a failed one is tried again once per rate-limit interval.
        """
        now = time.time()
        if not cycle_stalled(now, self._last_cycle_completed_ts, self._config.cycle_watchdog_s):
            return
        if not self._regulation_stalled:
            self._regulation_stalled = True
            self._log(_LOGGER).warning(
                "no regulation cycle has completed for %d min (last reason: %s)%s",
                int((now - self._last_cycle_completed_ts) / 60),
                self._last_reason,
                "; shutting the valve" if self._last_sent_was_max else "",
            )
        if self._last_sent_was_max:
            await self._async_watchdog_shut(now)
        self._write_state_with_binary_sensor()

    async def _async_watchdog_shut(self, now: float) -> None:
        """Replace a maximum command left in place by the minimum (off).

        Only a valve that can be reached (it gives a reading: Home Assistant
        silently skips a service call to an unavailable entity) and that
        still holds the maximum.  One that is off, back on its schedule or at
        any lower setpoint no longer holds our maximum (the regulation, when
        it runs again, takes it back).
        """
        if (
            self._watchdog_shut_tried
            and now - self._last_command_sent_ts < self._config.min_command_interval_s
        ):
            return
        if self._regulation_lock.locked():
            # A cycle is running right now: it decides this time.
            return
        async with self._regulation_lock:
            trv = self._active_trv()
            if not self._last_sent_was_max or not trv.usable:
                return
            lo, hi = command_limits(
                self._config.min_target_c, self._config.max_target_c, trv.min_c, trv.max_c
            )
            if (
                trv.shut_without_setpoint
                or trv.setpoint_c is None
                or not is_maximum(trv.setpoint_c, hi)
            ):
                self._last_sent_was_max = False
                self._log(_LOGGER).info(
                    "watchdog: %s holds %s, not the maximum; nothing to shut", trv.entity_id, _held_text(trv),
                )
                return
            self._watchdog_shut_tried = True
            sent = await self._async_send_off_to_tado(trv.entity_id)
            self._note_attempt(now, self._config.min_target_c, trv, sent)
            self._last_reason = f"{'sent' if sent else 'send_failed'}(watchdog)"
            if sent:
                self._reg_state.mode = MODE_COAST
                self._log(_LOGGER).warning(
                    "watchdog switched %s off",
                    trv.entity_id,
                )


def _command_text(command_c: float | None, floor_c: float) -> str:
    """A command for the log: "off" for the frost level, else its setpoint."""
    if command_c is None:
        return "command"
    if is_off_command(command_c, floor_c):
        return "off"
    return f"{command_c:.1f} °C"


def _held_text(trv: TrvReading) -> str:
    """What the TRV holds instead of our command, for the log."""
    if trv.off:
        return "off"
    if trv.scheduled:
        if trv.setpoint_c is None:
            return "its schedule (off)"
        return f"its schedule ({trv.setpoint_c:.1f} °C)"
    if trv.setpoint_c is None:
        return "no setpoint"
    return f"{trv.setpoint_c:.1f} °C"
