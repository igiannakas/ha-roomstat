"""Valve look-ahead on the climate entity (LookaheadMixin, 2.14.0).

The learner is HA-free (lookahead.py) and so is the correction
(regulation.lookahead_correction_c).  With the room's "valve look-ahead"
option on, this mixin:

* keeps the slope of the valve's own reading (the TRV entity in use; a
  switch of entity starts it afresh, a gap over 400 s clears it) and hands
  it to the regulator with the look-ahead learned so far;
* books each release, a hold command that went to a shut valve, and the
  valve's first answer to it from the zone's heating power sensor (Tado's
  heating %), which is what the learner learns from;
* keeps the look-ahead taken off the command the valve holds, so the
  auto-tuner sees the push the valve answers
  (autotune.AutotuneSample.lookahead_c);
* persists the learner, the reading's recent slope samples and the
  look-ahead taken off the command the valve holds (restore-state
  ``lookahead``, with the TRV it was learned on: another valve starts
  afresh), so a reload (every preset temperature change) neither sends an
  unlowered hold command on a still-sliding reading nor understates the
  tuner's demand, and puts each release's lesson in the activity log.

It never stops the regulation: a failure is logged once, with its
traceback, and the look-ahead is off for the rest of the session (the room
is regulated as without it).
"""
from __future__ import annotations

import logging
from typing import Any

from .autotune import SlopeEstimator
from .climate_controllers import PENDING_HEAT, is_off_command
from .const import CONF_HEATING_POWER_ENTITY_ID, CONF_SOURCE_ENTITY_ID, CONF_VALVE_LOOKAHEAD
from .lookahead import LookaheadLearner
from .parameters import LookaheadConfig
from .regulation import MODE_HOLD

_LOGGER = logging.getLogger(__name__)


class LookaheadMixin:
    """The valve look-ahead's state and steps on the entity."""

    def _lookahead_setup(self) -> None:
        """Called once from __init__."""
        self._lookahead_enabled: bool = bool(
            self._config_entry.options.get(CONF_VALVE_LOOKAHEAD, False)
        )
        # The zone's heating power sensor it learns from (the options flow
        # asks for one; a repair can remove it): without it the look-ahead
        # learned so far stays as it is.
        self._heating_power_entity: str | None = (
            self._config_entry.options.get(CONF_HEATING_POWER_ENTITY_ID) or None
        )
        self._lookahead = LookaheadLearner(LookaheadConfig())
        cfg = self._lookahead.cfg
        self._trv_slope = SlopeEstimator(
            window_s=cfg.slope_window_s,
            max_gap_s=cfg.slope_max_gap_s,
            min_samples=cfg.slope_min_samples,
        )
        self._trv_slope_entity: str | None = None
        # The look-ahead taken off the command the valve holds (0 for an
        # off, full power, or a command sent without it).
        self._held_lookahead_c: float = 0.0
        # This cycle's correction, for the attributes and the activity line.
        self._cycle_lookahead_c: float = 0.0
        # Set once anything here raised: off for the rest of the session.
        self._lookahead_failure: str | None = None

    @property
    def lookahead_active(self) -> bool:
        """The look-ahead option is on (and has not failed this session)."""
        return self._lookahead_enabled and self._lookahead_failure is None

    @property
    def lookahead_enabled(self) -> bool:
        """The look-ahead option is on (the sensor is available)."""
        return self._lookahead_enabled

    @property
    def valve_lookahead_min(self) -> float | None:
        """The look-ahead in use (minutes), None while the option is off."""
        if not self._lookahead_enabled:
            return None
        return round(self._lookahead.lookahead_s / 60.0, 1)

    def valve_lookahead_summary(self) -> dict[str, Any]:
        """The learner's state, for the sensor's attributes and diagnostics."""
        summary = self._lookahead.summary()
        if self._lookahead_failure is not None:
            summary["error"] = self._lookahead_failure
        return summary

    def _lookahead_fail(self, err: BaseException) -> None:
        what = f"{type(err).__name__}: {err}"
        if self._lookahead_failure is None:
            self._lookahead_failure = what
            self._log(_LOGGER).exception(
                "the valve look-ahead failed and is off until restart"
            )
        self._cycle_lookahead_c = 0.0

    # -- the regulation cycle ---------------------------------------------
    def _lookahead_inputs(self, now: float, trv: Any) -> tuple[float | None, float]:
        """The valve reading's slope (°C/s) and the look-ahead (s) for this
        cycle's regulation: (None, 0) while the option is off."""
        self._cycle_lookahead_c = 0.0
        if not self.lookahead_active:
            return None, 0.0
        try:
            if trv.entity_id != self._trv_slope_entity or trv.internal_c is None:
                # Another entity's reading, or a gap: a fresh start.
                self._trv_slope.reset()
                self._trv_slope_entity = trv.entity_id
            if trv.internal_c is not None:
                self._trv_slope.add(now, trv.internal_c)
            return self._trv_slope.slope_c_per_s(), self._lookahead.lookahead_s
        except Exception as err:  # noqa: BLE001 - never stop the regulation
            self._lookahead_fail(err)
            return None, 0.0

    def _lookahead_valve_shut(self, now: float, trv: Any) -> bool:
        """Before this cycle's send: the valve is shut, so a hold command now
        is a release.  Not while a heat command of ours may still be on its
        way (a cloud zone's state shows "off" for a minute after a 25 °C),
        nor while it shows a heat setpoint (switched on from the app)."""
        if not self.lookahead_active:
            return False
        try:
            if self._pending(now, trv) == PENDING_HEAT:
                return False
            if trv.off:
                return True
            floor = self._config.min_target_c
            shows_heat = (
                not trv.scheduled and trv.setpoint_c is not None
                and trv.setpoint_c > floor + 0.05
            )
            return (
                self._last_sent_setpoint is not None
                and is_off_command(self._last_sent_setpoint, floor)
                and not shows_heat
            )
        except Exception as err:  # noqa: BLE001 - never stop the regulation
            self._lookahead_fail(err)
            return False

    def _lookahead_note_result(self, result: Any) -> None:
        """This cycle's correction (for the attributes and the log line)."""
        self._cycle_lookahead_c = float(getattr(result, "lookahead_c", 0.0) or 0.0)

    def _lookahead_after_send(
        self, now: float, trv: Any, result: Any, was_shut: bool
    ) -> None:
        """After the cycle's send: a release starts watching for the valve's
        answer; an off or full power sent before it answered ends that."""
        if not self.lookahead_active:
            return
        try:
            if not self._last_reason.startswith("sent(") or self._last_attempt_c is None:
                return
            # The command as tried (kept inside what the valve accepts).
            command = self._last_attempt_c
            if (
                is_off_command(command, self._config.min_target_c)
                or command >= self._config.max_target_c - 0.05
            ):
                self._lookahead.cancel()
            elif result.mode == MODE_HOLD and was_shut and self._heating_power_entity:
                self._lookahead.start(
                    now, command, trv.internal_c, self._trv_slope.slope_c_per_s()
                )
        except Exception as err:  # noqa: BLE001 - never stop the regulation
            self._lookahead_fail(err)

    def _lookahead_observe(self, now: float, trv: Any) -> None:
        """Watch a release for the valve's first answer (each regulated cycle)."""
        if not self.lookahead_active or not self._lookahead.observing:
            return
        try:
            floor = self._config.min_target_c
            last = self._last_attempt_c
            if last is not None and (
                is_off_command(last, floor) or last >= self._config.max_target_c - 0.05
            ):
                # Told something else since (off, full power: Roomstat
                # switched off, summer, the watchdog): it says nothing.
                self._lookahead.cancel()
                return
            if (
                not trv.off and not trv.scheduled and trv.setpoint_c is not None
                and trv.setpoint_c > floor + 0.05
            ):
                # Read before this cycle's send: the release's own cycle
                # still shows the valve as it was (seen_ts).
                self._lookahead.note_setpoint(trv.setpoint_c, seen_ts=now)
            data = self.coordinator.data or {}
            line = self._lookahead.observe(
                now, data.get("heating_power"), data.get("heating_power_ts")
            )
            if line:
                self._log(_LOGGER).info("valve look-ahead: %s", line)
        except Exception as err:  # noqa: BLE001 - never stop the regulation
            self._lookahead_fail(err)

    def _lookahead_check_setup(self) -> None:
        """At start: say so if it is on with nothing to learn from."""
        if self._lookahead_enabled and not self._heating_power_entity:
            self._log(_LOGGER).warning(
                "the valve look-ahead is on but has no heating power sensor to "
                "learn from: it stays at %.1f min (pick the zone's heating sensor "
                "under Configure → Valve look-ahead)",
                self._lookahead.lookahead_s / 60.0,
            )

    # -- reset and persistence --------------------------------------------
    def _lookahead_reset(self) -> None:
        """Forget what was learned (with the auto-tune reset)."""
        self._lookahead.reset()
        if self._lookahead_enabled:
            self._log(_LOGGER).info("valve look-ahead reset: learning afresh from 0")

    def _lookahead_state(self) -> dict[str, Any]:
        """For the restore-state store."""
        data = self._lookahead.to_dict()
        data["trv"] = self._config_entry.data.get(CONF_SOURCE_ENTITY_ID)
        data["slope"] = self._trv_slope.to_dict()
        data["slope_entity"] = self._trv_slope_entity
        data["held"] = [self._last_sent_setpoint, round(self._held_lookahead_c, 2)]
        return data

    def _lookahead_restore(self, data: Any, now: float) -> None:
        """Take back what was learned on this room's TRV."""
        if not isinstance(data, dict):
            return
        trv = self._config_entry.data.get(CONF_SOURCE_ENTITY_ID)
        if data.get("trv") != trv:
            if self._lookahead_enabled:
                self._log(_LOGGER).info(
                    "valve look-ahead learned on %s, not %s: learning afresh",
                    data.get("trv"), trv,
                )
            return
        try:
            self._lookahead.restore(data, now)
            # The reading's recent slope, if it still joins up with now (the
            # SlopeEstimator's own rules): a hold on a sliding reading stays
            # lowered across the reload.
            entity = data.get("slope_entity")
            if isinstance(entity, str) and self._trv_slope.restore(data.get("slope"), now):
                self._trv_slope_entity = entity
            # What was taken off the command the valve holds, if that is the
            # command taken back (CommandRecord, restored first).
            held = data.get("held")
            if (
                isinstance(held, (list, tuple)) and len(held) == 2
                and isinstance(held[0], (int, float)) and isinstance(held[1], (int, float))
                and self._last_sent_setpoint is not None
                and abs(held[0] - self._last_sent_setpoint) < 0.01
                and 0.0 <= held[1] <= self._config.lookahead_cap_c
            ):
                self._held_lookahead_c = float(held[1])
        except Exception as err:  # noqa: BLE001 - a bad save starts afresh
            self._lookahead.reset()
            self._trv_slope.reset()
            self._held_lookahead_c = 0.0
            self._log(_LOGGER).warning(
                "the saved valve look-ahead could not be taken back (%s): learning afresh",
                err,
            )
