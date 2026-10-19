"""Regulation cycle mixin for RoomstatClimate."""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.climate import PRESET_BOOST, HVACMode
from homeassistant.exceptions import HomeAssistantError

from .autotune import AutotuneSample
from .climate_controllers import (
    BAND_WARM,
    CommandPacer,
    TrvReading,
    command_limits,
    cycle_stalled,
    format_time_period,
    heat_timer_duration,
    heat_timer_refresh_due,
    integration_step,
    is_maximum,
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
                _LOGGER.exception(
                    "%s: the out-of-band check failed", self._config_entry.title
                )
            return
        minutes = int((now - state.since) / 60) if state.since is not None else 0
        if state.on and (not was.on or was.reason != state.reason):
            _LOGGER.warning(
                "%s: out of band (%s): room %+.2f °C from the target for %d min",
                self._config_entry.title, state.reason, state.deviation_c, minutes,
            )
        elif was.on and not state.on:
            _LOGGER.info("%s: back within the band", self._config_entry.title)
        elif state.reason == BAND_WARM and was.reason != BAND_WARM:
            _LOGGER.info(
                "%s: room %+.2f °C from the target for %d min with the radiator "
                "cold (sun or another source); not an alarm",
                self._config_entry.title, state.deviation_c, minutes,
            )

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
            _LOGGER.exception(
                "%s: auto-tune failed and is paused until restart; using the "
                "configured Kp/Ki/braking time",
                self._config_entry.title,
            )
            self._autotune_failed = True
            self._apply_active_tuning()
            return
        if reason:
            _LOGGER.info("%s: auto-tune %s", self._config_entry.title, reason)
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
        # clock caught up again.
        if self._last_command_sent_ts > now:
            self._last_command_sent_ts = now - self._config.min_command_interval_s
        # Likewise a timer "sent in the future" would not be renewed before it
        # runs out: its start counts as unknown, so it is renewed now.
        if self._heat_timer_sent_ts is not None and self._heat_timer_sent_ts > now:
            self._heat_timer_sent_ts = None

        # Summer mode: no regulation, hold the TRV at a fixed 5 °C.  The lock
        # is this cycle's decision (the watchdog counts it as completed).
        if self._summer_active:
            self._note_cycle_completed(now)
            await self._async_summer_enforce(now)
            return

        # Off: no regulation, but make sure the TRV is off too.
        if self._hvac_mode == HVACMode.OFF:
            self._note_cycle_completed(now)
            await self._async_keep_trv_off(now)
            return

        # Guard: skip when coordinator data is stale (update method raised an exception).
        if not self.coordinator.last_update_success:
            _LOGGER.debug("Skipping regulation cycle – coordinator update failed")
            self._last_reason = "coordinator_unavailable"
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
        if self._resume_pending and trv.setpoint_c is not None:
            self._resume_pending = False
            self._reg_state.mode = resume_mode(trv.setpoint_c, self._config)
            self._last_sent_was_max = is_maximum(trv.setpoint_c, self._config.max_target_c)
            _LOGGER.debug(
                "%s: resuming as %s from the valve's setpoint %.1f",
                self._config_entry.title, self._reg_state.mode, trv.setpoint_c,
            )

        # Sensor resilience: bridge short gaps with last-valid value
        sensor_lost = False
        if room_temp is not None:
            if self._room_sensor_lost_logged:
                _LOGGER.info("%s: room sensor is back", self._config_entry.title)
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
            _LOGGER.warning(
                "%s: room sensor unavailable for %d min; the radiator keeps "
                "its last setting until it is back",
                self._config_entry.title, int(missing_for / 60),
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
            self._write_state_with_binary_sensor()
            return

        if room_temp is None or tado_internal is None:
            self._last_reason = "waiting_for_sensors"
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
            _LOGGER.debug(
                "%s: %s | sp %.1f room %.2f%s %s | trv %.1f%s | cmd %.1f (%+.1f vs trv)"
                " P %+.2f I %+.2f | %s",
                self._config_entry.title, result.mode, setpoint, room_temp,
                " (fell fast: previous reading used)" if result.room_held else "",
                "no slope" if slope is None else f"{slope * 3600:+.2f} °C/h",
                tado_internal,
                "" if trv.heating is None else (" heating" if trv.heating else " idle"),
                result.target_for_tado_c, result.target_for_tado_c - tado_internal,
                result.p_correction_c, result.i_correction_c, self._last_reason,
            )

        self._note_cycle_completed(now)
        self._write_state_with_binary_sensor()

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
        time_since_last = now - self._last_command_sent_ts
        # A TRV that no longer holds our last command (lost on the way, a
        # Tado schedule block or an expired timer overlay ended the override,
        # or it was switched off in the Tado app) is compared with what it
        # actually holds, so the command goes out again.
        if trv_setpoint_drifted(
            self._last_sent_setpoint,
            trv.setpoint_c,
            time_since_last,
            FALLBACK_DRIFT_GRACE_S if trv.slow else TRV_DRIFT_GRACE_S,
            self._config.min_change_threshold_c,
            trv_off=trv.off,
            floor_c=self._config.min_target_c,
            trv_scheduled=trv.scheduled,
        ):
            _LOGGER.info(
                "%s: %s holds %s, not the %.1f °C sent; sending again",
                self._config_entry.title, trv.entity_id, _held_text(trv),
                self._last_sent_setpoint,
            )
            self._last_sent_setpoint = None

        # A valve that is off, or on an Off block of its schedule, already
        # does what a frost-level command asks (Tado shows its 5 °C setpoint
        # as off), and Home Assistant refuses a setpoint below 7 °C for a
        # HomeKit valve that is off.
        if trv.shut_without_setpoint and target <= self._config.min_target_c + 0.05:
            self._last_reason = "already_at_target"
            return

        # Keep the command inside what the TRV accepts right now (anything
        # else is refused before it is sent).
        lo, hi = command_limits(
            self._config.min_target_c, self._config.max_target_c, trv.min_c, trv.max_c
        )
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
            else trv.setpoint_c  # None if the TRV is off/unavailable
        )
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
        # A failed command still counts for the rate limit, so an outage
        # costs one attempt per interval, not one per cycle.
        sent = await self._async_send_to_tado(
            target, trv.entity_id, timer_s=timer_s, maximum=maximum
        )
        self._last_command_sent_ts = now
        self._last_attempt_c = target
        self._last_reason = f"{'sent' if sent else 'send_failed'}({reason})"

    async def _async_keep_trv_off(self, now: float) -> None:
        """Roomstat is off: keep the TRV off as well.

        An OFF command that failed (valve unreachable) or did not stick
        (turned back on from the Tado app or by a Tado schedule block) is sent
        again, at most once per rate-limit interval.
        """
        trv = self._active_trv()
        self._note_trv_source(trv)
        grace = FALLBACK_DRIFT_GRACE_S if trv.slow else TRV_DRIFT_GRACE_S
        since = now - self._last_command_sent_ts
        reason = "hvac_off"
        if (
            self.coordinator.last_update_success
            and trv.usable
            and not trv.off
            and since >= max(grace, self._config.min_command_interval_s)
        ):
            _LOGGER.info(
                "%s: %s is not off; sending off again",
                self._config_entry.title, trv.entity_id,
            )
            sent = await self._async_send_hvac_mode_to_tado(HVACMode.OFF)
            self._last_command_sent_ts = now
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
        # Whether the newly used entity holds a timer, and since when, is not
        # known: a maximum it holds is renewed as a timer straight away.
        self._heat_timer_sent_ts = None
        self._heat_timer_refresh_tried = False
        source_id = self._config_entry.data.get("source_entity_id")
        fallback_id = self._config_entry.options.get(CONF_FALLBACK_ENTITY_ID)
        prefer_fallback = self._prefer_fallback()
        preferred, backup = (fallback_id, source_id) if prefer_fallback else (source_id, fallback_id)
        if trv.is_fallback == prefer_fallback:
            _LOGGER.info(
                "%s: %s available again, back on it",
                self._config_entry.title,
                preferred,
            )
        else:
            _LOGGER.warning(
                "%s: %s unavailable, using %s",
                self._config_entry.title,
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
                _LOGGER.info("%s: commands to %s work again", title, entity_id)
            self._last_command_error = None
            return
        text = str(err) or type(err).__name__
        if isinstance(err, TimeoutError):
            text = f"no answer within {TRV_COMMAND_TIMEOUT_S:.0f} s"
        message = f"{what} to {entity_id} failed: {text}"
        if message == self._last_command_error:
            _LOGGER.debug("%s: %s", title, message)
        elif isinstance(err, (TimeoutError, HomeAssistantError)):
            _LOGGER.warning("%s: %s", title, message)
        else:
            # Not an expected outcome of a service call: keep the traceback.
            _LOGGER.error("%s: %s", title, message, exc_info=err)
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

    async def _async_send_hvac_mode_to_tado(self, mode: HVACMode) -> bool:
        """Forward an HVAC mode change to the TRV (or its active fallback).

        Returns whether the TRV took it.  An OFF that fails is sent again by
        the regulation cycle (see _async_keep_trv_off).
        """
        source_entity = self._active_trv().entity_id
        if not source_entity:
            _LOGGER.warning("No source_entity_id configured – cannot send HVAC mode to Tado")
            return False

        _LOGGER.debug("Sending HVAC mode %s to %s", mode, source_entity)
        if mode == HVACMode.OFF:
            # Whatever happens, the TRV no longer holds a known setpoint.
            self._last_sent_setpoint = None
        sent = await self._async_call_trv(
            source_entity, "set_hvac_mode", {"hvac_mode": mode}
        )
        if sent and mode == HVACMode.OFF:
            # An OFF overlay replaces any timer: nothing to renew or shut.
            self._heat_timer_sent_ts = None
            self._last_sent_was_max = False
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
            _LOGGER.warning("No source_entity_id configured – cannot send command to Tado")
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
            _LOGGER.info("%s: regulation is running again", self._config_entry.title)

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
            _LOGGER.warning(
                "%s: no regulation cycle has completed for %d min (last reason: %s)%s",
                self._config_entry.title,
                int((now - self._last_cycle_completed_ts) / 60),
                self._last_reason,
                "; shutting the valve" if self._last_sent_was_max else "",
            )
        if self._last_sent_was_max:
            await self._async_watchdog_shut(now)
        self._write_state_with_binary_sensor()

    async def _async_watchdog_shut(self, now: float) -> None:
        """Replace a maximum command left in place by the minimum.

        Only a valve that can be reached (it gives a reading: Home Assistant
        silently skips a service call to an unavailable entity) and that
        still holds the maximum.  One that is off, back on its schedule or at
        any lower setpoint no longer holds our maximum, and a frost-level
        command could turn an off valve on (a HomeKit valve that is off
        accepts only 7-35 °C).
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
                _LOGGER.info(
                    "%s: watchdog: %s holds %s, not the maximum; nothing to shut",
                    self._config_entry.title, trv.entity_id, _held_text(trv),
                )
                return
            self._watchdog_shut_tried = True
            sent = await self._async_send_to_tado(lo, trv.entity_id)
            self._last_command_sent_ts = now
            self._last_attempt_c = lo
            self._last_reason = f"{'sent' if sent else 'send_failed'}(watchdog)"
            if sent:
                self._reg_state.mode = MODE_COAST
                _LOGGER.warning(
                    "%s: watchdog sent %.1f °C to %s", self._config_entry.title,
                    lo, trv.entity_id,
                )


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
