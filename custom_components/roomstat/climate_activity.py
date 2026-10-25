"""The room's activity log on the climate entity (ActivityMixin).

Every regulation cycle ends with a decision line (activity.describe_cycle),
and every message the room logs at info or above becomes an event line
(``self._log(_LOGGER)``, a RoomLogAdapter), whatever the log level: a
preset or schedule change, a window, presence, a command that failed or
went out again, a start, the auto-tuner's journal.  The activity sensor
(sensor.py) shows the latest line as its state, written once per line so
the recorder keeps every one, and the last lines as its ``log`` attribute.
The live decision is the climate entity's ``activity`` attribute.  The
lines ride along in the restore-state extra data (``activity``), so a
reload (every preset temperature change) keeps them.
"""
from __future__ import annotations

import datetime
import logging
import threading
import time
from typing import Any

from homeassistant.util import dt as dt_util

from .activity import (
    PATH_REGULATED,
    PATH_SENSOR_LOST,
    WINDOW_OPEN,
    WINDOW_PENDING,
    WINDOW_REWARM,
    ActivityLog,
    CycleFacts,
    RoomLogAdapter,
    clip,
    decision_key,
    describe_cycle,
    event_text,
    preset_name,
)
from .climate_controllers import is_off_command

_LOGGER = logging.getLogger(__name__)


def local_hhmm(ts: float, now: float | None = None) -> str:
    """'07:31' today, 'Sat 23:58' on an earlier day (Home Assistant's time zone)."""
    try:
        tz = dt_util.get_default_time_zone()
        when = datetime.datetime.fromtimestamp(ts, tz=tz)
        today = datetime.datetime.fromtimestamp(time.time() if now is None else now, tz=tz)
    except (OverflowError, OSError, ValueError):
        return "?"
    return when.strftime("%H:%M" if when.date() == today.date() else "%a %H:%M")


class ActivityMixin:
    """Activity log methods for RoomstatClimate."""

    def _activity_setup(self) -> None:
        self._activity = ActivityLog()
        self._room_loggers: dict[str, RoomLogAdapter] = {}
        self._activity_resumed = True
        self._activity_tuner_seq = self._autotuner.journal_seq
        self._activity_failure: str | None = None

    def _log(self, logger: logging.Logger) -> RoomLogAdapter:
        """The room's logger for *logger*: the room's name in front of each
        message, and info and above into the activity log."""
        adapter = self._room_loggers.get(logger.name)
        if adapter is None:
            adapter = RoomLogAdapter(logger, self._config_entry.title, self._activity_event)
            self._room_loggers[logger.name] = adapter
        return adapter

    # -- lines --------------------------------------------------------------
    def _activity_event(self, level: int, text: str) -> None:
        added = self._activity.add_event(time.time(), event_text(level, text))
        self._activity_write(added)

    def _activity_cycle(
        self,
        now: float,
        path: str,
        *,
        room_c: float | None = None,
        setpoint_c: float | None = None,
        prev_mode: str | None = None,
        slope_c_per_s: float | None = None,
        sensor_lost_min: int | None = None,
        td_s: float | None = None,
    ) -> None:
        """Add this cycle's decision line (unchanged decisions add none)."""
        try:
            facts = self._activity_facts(
                path, room_c=room_c, setpoint_c=setpoint_c, prev_mode=prev_mode,
                slope_c_per_s=slope_c_per_s, sensor_lost_min=sensor_lost_min, td_s=td_s,
            )
            if self._activity.add_decision(
                now, describe_cycle(facts, self._config), decision_key(facts, self._config)
            ):
                self._activity_write(1)
            self._activity_failure = None
        except Exception as err:  # noqa: BLE001 - the log must never stop the regulation
            # Once per distinct failure, with its traceback.
            what = f"{type(err).__name__}: {err}"
            if what != self._activity_failure:
                self._activity_failure = what
                _LOGGER.exception("%s: the activity line failed", self._config_entry.title)
        if path == PATH_REGULATED:
            self._activity_resumed = False

    def _activity_facts(
        self,
        path: str,
        *,
        room_c: float | None,
        setpoint_c: float | None,
        prev_mode: str | None,
        slope_c_per_s: float | None,
        sensor_lost_min: int | None,
        td_s: float | None,
    ) -> CycleFacts:
        trv = self._active_trv()
        floor = self._config.min_target_c
        if room_c is None and path != PATH_SENSOR_LOST:
            room_c = (self.coordinator.data or {}).get("room_temp")
        tado, tado_c = self._activity_tado(trv, floor)
        facts = CycleFacts(
            path=path,
            setpoint_c=setpoint_c if setpoint_c is not None else self._effective_setpoint(),
            target_label=self._activity_label(),
            room_c=room_c,
            room_last_c=self._last_valid_room_temp,
            trv_c=trv.internal_c,
            trv_heating=trv.heating,
            trv_off=bool(trv.off),
            trv_scheduled=bool(trv.scheduled),
            tado=tado,
            tado_c=tado_c,
            window=self._activity_window(),
            send=self._last_reason,
            sensor_lost_min=sensor_lost_min,
            td_s=td_s,
        )
        result = self._last_result
        if path == PATH_REGULATED and result is not None:
            facts.mode = result.mode
            facts.prev_mode = prev_mode
            # The first decision after a start: a mode taken back from the
            # valve is no transition (a shut valve resumes as coast, then stop).
            facts.resumed = self._activity_resumed
            facts.error_c = result.error_c
            facts.slope_c_per_h = (
                slope_c_per_s * 3600.0 if slope_c_per_s is not None else None
            )
            facts.room_held = result.room_held
            facts.room_bridged = self._sensor_degraded
            facts.command_c = result.target_for_tado_c
            facts.command_off = is_off_command(result.target_for_tado_c, floor)
            facts.p_c = result.p_correction_c
            facts.i_c = result.i_correction_c
            facts.saturated = result.is_saturated
        return facts

    def _activity_tado(self, trv: Any, floor: float) -> tuple[str, float | None]:
        """What the valve has been told: our last command it took, else what
        it reports."""
        sent = self._last_sent_setpoint
        if sent is not None:
            if is_off_command(sent, floor):
                return "off", None
            return f"{sent:.1f}", sent
        if trv.off:
            return "off", None
        if trv.scheduled:
            return "its Tado schedule", None
        if trv.setpoint_c is not None:
            return f"{trv.setpoint_c:.1f}", trv.setpoint_c
        return "unknown", None

    def _activity_label(self) -> str:
        """Where the target comes from: the preset, and what holds it."""
        if self._window_ctrl.is_active or self._window_ctrl.close_delay_active:
            return "window"
        label = preset_name(self._preset_mode)
        if self._presence_ctrl.is_active:
            label += ", nobody home"
        elif self._presence_ctrl.override_active:
            label += ", nobody home (change by hand kept)"
        elif self.schedule_configured and self._schedule_ctrl.override_active:
            label += ", schedule overridden"
        return label

    def _activity_window(self) -> str | None:
        if self._window_ctrl.close_delay_active:
            return WINDOW_REWARM
        if self._window_ctrl.is_active:
            return WINDOW_OPEN
        if self._window_sensor_on() and not self._summer_active:
            return WINDOW_PENDING
        return None

    def _activity_tuner(self) -> None:
        """Log what the auto-tuner did since the last look, in order."""
        try:
            t = self._autotuner
            entries = t.journal_since(self._activity_tuner_seq)
            self._activity_tuner_seq = t.journal_seq
            for _seq, text, until in entries:
                line = f"auto-tune: {text}"
                if until is not None:
                    line += f" (possible from {local_hhmm(until)})"
                self._log(_LOGGER).info("%s", line)
        except Exception:  # noqa: BLE001 - the log must never stop the regulation
            _LOGGER.debug("%s: auto-tune journal failed", self._config_entry.title,
                          exc_info=True)

    # -- the sensor -----------------------------------------------------------
    def _activity_write(self, lines: int) -> None:
        """Write the activity sensor once per new line, so the state history
        (the recorder) keeps every line, not just the last of a burst."""
        sensor = getattr(self.coordinator, "activity_entity", None)
        if lines <= 0 or sensor is None or sensor.hass is None:
            return
        hass = self.hass
        loop = getattr(hass, "loop", None)
        loop_thread = getattr(hass, "loop_thread_id", None)
        if loop is not None and loop_thread is not None and threading.get_ident() != loop_thread:
            # Not in the event loop (never expected): hand over the latest.
            loop.call_soon_threadsafe(sensor.async_write_ha_state)
            return
        entries = self._activity.entries()[:lines]
        for entry in reversed(entries):
            sensor.pending_line = entry.text
            sensor.async_write_ha_state()
        sensor.pending_line = None

    @property
    def activity_state(self) -> str | None:
        """The latest line (the sensor's state)."""
        latest = self._activity.latest
        return clip(latest.text) if latest is not None else None

    def activity_log_lines(self) -> list[str]:
        """The recent lines, newest first, with their local time."""
        now = time.time()
        return [f"{local_hhmm(e.ts, now)}  {e.text}" for e in self._activity.entries()]
