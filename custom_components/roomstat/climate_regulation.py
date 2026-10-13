"""Regulation cycle mixin for RoomstatClimate."""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.climate import PRESET_BOOST, HVACMode
from homeassistant.exceptions import HomeAssistantError

from .autotune import AutotuneSample
from .climate_controllers import (
    CommandPacer,
    TrvReading,
    command_limits,
    integration_step,
    trv_setpoint_drifted,
)
from .const import CONF_FALLBACK_ENTITY_ID
from .parameters import (
    FALLBACK_DRIFT_GRACE_S,
    MAX_INTEGRATION_STEP_S,
    TRV_COMMAND_SPACING_S,
    TRV_COMMAND_TIMEOUT_S,
    TRV_DRIFT_GRACE_S,
)
from .regulation import MODE_COAST, MODE_HEAT, MODE_HOLD

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
        self.async_write_ha_state()
        bs = getattr(self.coordinator, "binary_sensor_entity", None)
        if bs is not None:
            bs.async_write_ha_state()
        for attr in ("sensor_entity", "schedule_sensor_entity"):
            se = getattr(self.coordinator, attr, None)
            if se is not None and se.hass is not None:
                se.async_write_ha_state()
        for ent in getattr(self.coordinator, "tuning_entities", ()):
            if ent.hass is not None:
                ent.async_write_ha_state()

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

        # Summer mode: no regulation, hold the TRV at a fixed 5 °C.
        if self._summer_active:
            await self._async_summer_enforce(now)
            return

        # Off: no regulation, but make sure the TRV is off too.
        if self._hvac_mode == HVACMode.OFF:
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

        if sensor_lost and tado_internal is not None and self._reg_state.mode == MODE_HEAT:
            # A heat-up's maximum command is never left in place without a
            # room reading: it is ended (coast) before the room is left alone.
            # Until the TRV has taken the stop, it is tried again (once per
            # rate-limit interval).
            await self._async_decide_and_send(
                now, trv, self._config.min_target_c, strong=True
            )
            if self._last_reason.startswith("sent(") or self._last_reason == "already_at_target":
                self._reg_state.mode = MODE_COAST
            self._write_state_with_binary_sensor()
            return

        if room_temp is None or tado_internal is None:
            self._last_reason = "waiting_for_sensors"
            self._write_state_with_binary_sensor()
            return

        # 2. Time delta, capped so a pause cannot add a large integral step.
        dt = integration_step(now, self._last_regulation_ts, MAX_INTEGRATION_STEP_S)
        self._last_regulation_ts = now

        # 3. Effective setpoint (considers HVAC mode + preset)
        setpoint = self._effective_setpoint()

        # 3b. Robust room slope for the heat-up and stop predictions (only on fresh
        # readings; a bridged/stale value must not look like a flat room).
        slope = None
        if not self._sensor_degraded:
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
        # TRV demand is unknown for those cycles (no demand crossing).
        self._feed_autotune(
            now, setpoint, room_temp,
            self._source_trv_internal() if trv.is_fallback else tado_internal,
            result,
        )

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
        # Tado schedule block ended the override, or it was switched off in
        # the Tado app) is compared with what it actually holds, so the
        # command goes out again.
        if trv_setpoint_drifted(
            self._last_sent_setpoint,
            trv.setpoint_c,
            time_since_last,
            FALLBACK_DRIFT_GRACE_S if trv.is_fallback else TRV_DRIFT_GRACE_S,
            self._config.min_change_threshold_c,
            trv_off=trv.off,
            floor_c=self._config.min_target_c,
        ):
            _LOGGER.info(
                "%s: %s holds %s, not the %.1f °C sent; sending again",
                self._config_entry.title, trv.entity_id,
                "off" if trv.off else f"{trv.setpoint_c:.1f} °C",
                self._last_sent_setpoint,
            )
            self._last_sent_setpoint = None

        # A valve that is off already does what a frost-level command asks
        # (Tado shows its 5 °C setpoint as off), and Home Assistant refuses a
        # setpoint below 7 °C for a HomeKit valve that is off.
        if trv.off and target <= self._config.min_target_c + 0.05:
            self._last_reason = "already_at_target"
            return

        # Keep the command inside what the TRV accepts right now (anything
        # else is refused before it is sent).
        lo, hi = command_limits(
            self._config.min_target_c, self._config.max_target_c, trv.min_c, trv.max_c
        )
        target = max(lo, min(hi, target))

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

            if diff < self._config.min_change_threshold_c and not overlay_refresh_due:
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
        sent = await self._async_send_to_tado(target, trv.entity_id)
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
        grace = FALLBACK_DRIFT_GRACE_S if trv.is_fallback else TRV_DRIFT_GRACE_S
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

    async def _async_call_trv(self, entity_id: str, service: str, data: dict) -> bool:
        """Call a climate service on the TRV.  Never raises (except when the
        task is cancelled): a failed command must not stop the cycle."""
        await _wait_for_command_slot()
        try:
            async with asyncio.timeout(TRV_COMMAND_TIMEOUT_S):
                await self.hass.services.async_call(
                    domain="climate",
                    service=service,
                    service_data={"entity_id": entity_id, **data},
                    blocking=True,
                )
        except Exception as err:  # noqa: BLE001 - any failure is reported, never raised
            self._note_command_result(entity_id, service, err)
            return False
        self._note_command_result(entity_id, service, None)
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
        return await self._async_call_trv(
            source_entity, "set_hvac_mode", {"hvac_mode": mode}
        )

    async def _async_send_to_tado(self, target_c: float, entity_id: str | None = None) -> bool:
        """Send a temperature command to the TRV (default: the configured one).

        Returns whether the TRV took it.  A failed command is retried by the
        next cycle once the rate limit allows (no baseline, or the drift check).
        """
        source_entity = entity_id or self._config_entry.data.get("source_entity_id")
        if not source_entity:
            _LOGGER.warning("No source_entity_id configured – cannot send command to Tado")
            return False

        _LOGGER.debug("Sending %.1f°C to %s", target_c, source_entity)
        sent = await self._async_call_trv(
            source_entity,
            "set_temperature",
            {"temperature": target_c, "hvac_mode": HVACMode.HEAT},
        )
        if sent:
            self._last_sent_setpoint = target_c
        return sent
