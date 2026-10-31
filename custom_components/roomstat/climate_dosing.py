"""Dosed top-ups on the climate entity (DosingMixin, 2.15.0).

The model, the learner, the pulse coordinator and the per-room glue are
HA-free (dosing.py: TopupPredictor, DoseLearner, PulseCoordinator,
RoomDoser), and so is the dose mode (regulation.py).  With the room's "dosed
top-ups" option on, this mixin:

* books every command the room tries into the room's doser
  (``_note_attempt`` calls ``_dosing_note_command``): a pulse's open time
  comes from Roomstat's own commands, never from the zone's heating %, which
  lags the valve and which a non-linear valve makes useless as a dose
  (design §11, project doc
  claude/roomstat-smith-predictor-dosed-topups-2026-10-30.md);
* hands the regulator the heat still on its way, the learned model and the
  pulse coordinator's verdict each cycle (``_dosing_inputs``), and after the
  send starts or ends episodes, re-judges a running pulse at the second it
  should end, and puts each episode's lesson in the activity log
  (``_dosing_after_compute``);
* reports this room's zone heating to the house's pulse coordinator every
  regulated cycle, dosing or not (``_dosing_note_zone``), so dosed rooms can
  ride any zone's call for heat: the boiler is an on/off type, so radiators
  should share its burns (design §12);
* persists the doser and the regulator's settled state (restore-state
  ``dosing``, with the TRV it was learned on), so a reload (every preset
  temperature change) keeps the heat in flight and a running pulse.

It never stops the regulation: a failure is logged once, with its
traceback, and dosing is off for the rest of the session (the room is then
regulated as without it).
"""
from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.helpers.event import async_call_later

from .climate_controllers import PENDING_HEAT
from .const import (
    CONF_DOSED_TOPUPS,
    CONF_HEATING_POWER_ENTITY_ID,
    CONF_SOURCE_ENTITY_ID,
    PRESET_BOOST,
)
from .dosing import PulseCoordinator, RoomDoser
from .parameters import DoseConfig
from .regulation import MODE_DOSE, MODE_HEAT, MODE_STOP

_LOGGER = logging.getLogger(__name__)

# One coordinator for the house (like climate_regulation.COMMAND_PACER):
# every room reports its zone's heating to it, dosed rooms ask it when to
# fire, and a room that starts a pulse wakes the rooms due soon.
PULSE_COORDINATOR = PulseCoordinator(DoseConfig())


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


class DosingMixin:
    """Dosed top-ups' state and steps on the entity."""

    def _dosing_setup(self) -> None:
        """Called once from __init__."""
        opts = self._config_entry.options
        self._dosing_enabled: bool = bool(opts.get(CONF_DOSED_TOPUPS, False))
        # Each pulse is checked against the zone's heating % (did the valve
        # open?); without the sensor dosing stays off (a repair can remove it).
        self._dosing_has_sensor: bool = bool(opts.get(CONF_HEATING_POWER_ENTITY_ID))
        self._doser = RoomDoser(
            self._config.dose, self._config_entry.entry_id, PULSE_COORDINATOR
        )
        self._dose_wake_cancel = None
        # Added to HA and in the house's coordinator: a wake or a pulse timer
        # that fires after the entity was removed runs no cycle.
        self._dosing_live = False
        # The other room, when the coordinator chose this cycle's pulse start.
        self._dose_fired_with: tuple[str, str] | None = None
        # Set once anything here raised: off for the rest of the session.
        self._dosing_failure: str | None = None

    # -- properties ---------------------------------------------------------
    @property
    def dosing_enabled(self) -> bool:
        """The option is on (the sensor is available)."""
        return self._dosing_enabled

    @property
    def dosing_active(self) -> bool:
        """The option is on, the heating sensor is set and nothing failed."""
        return (
            self._dosing_enabled and self._dosing_has_sensor
            and self._dosing_failure is None
        )

    @property
    def topup_gain(self) -> float | None:
        """The learned rise per open-minute in use (°C), None while off."""
        if not self._dosing_enabled:
            return None
        return round(self._doser.learner.gain_c_per_open_min, 4)

    def topup_dose_summary(self) -> dict[str, Any]:
        """The learned model, for the sensor's attributes: only what changes
        when an episode is judged or a pulse starts (the recorder writes a
        row for every change; the live values are on the thermostat,
        unrecorded, and in the diagnostics)."""
        summary = self._doser.summary()
        if self._dosing_failure is not None:
            summary["error"] = self._dosing_failure
        return summary

    def topup_dose_diagnostics(self) -> dict[str, Any]:
        """Everything the doser knows, for the diagnostics download."""
        data = self._doser.diagnostics()
        if self._dosing_failure is not None:
            data["error"] = self._dosing_failure
        return data

    def _dosing_attributes(self) -> dict[str, Any]:
        """Live values for the thermostat (unrecorded: they drift every cycle)."""
        if not self._dosing_enabled:
            return {}
        result = self._last_result
        ready = getattr(result, "ready_in_s", None) if result is not None else None
        virtual = getattr(result, "virtual_error_c", None) if result is not None else None
        return {
            "topup_gain": self.topup_gain,
            "topup_inflight_c": round(self._doser.inflight_c, 3),
            "topup_virtual_error_c": (
                round(virtual, 3) if _finite(virtual) and getattr(result, "settled", False)
                else None
            ),
            "topup_ready_in_min": round(ready / 60.0, 1) if _finite(ready) else None,
        }

    # -- failure isolation ----------------------------------------------------
    def _dosing_fail(self, err: BaseException) -> None:
        what = f"{type(err).__name__}: {err}"
        if self._dosing_failure is None:
            self._dosing_failure = what
            self._log(_LOGGER).exception(
                "dosed top-ups failed and are off until restart; the room is "
                "regulated without them"
            )
        self._dosing_cancel_wake()

    # -- lifecycle ------------------------------------------------------------
    def _dosing_start(self) -> None:
        """When the entity is added: join the house's pulse coordinator."""
        self._dosing_live = True
        PULSE_COORDINATOR.register(self._config_entry.entry_id, self._dosing_wake)

    def _dosing_stop(self) -> None:
        """When the entity is removed: leave it, cancel the pulse timer."""
        self._dosing_live = False
        PULSE_COORDINATOR.unregister(self._config_entry.entry_id)
        self._dosing_cancel_wake()

    def _dosing_check_setup(self) -> None:
        """At start: say so if it is on without a heating sensor."""
        if self._dosing_enabled and not self._dosing_has_sensor:
            self._log(_LOGGER).warning(
                "dosed top-ups are on but no heating power sensor is set: they "
                "stay off (pick the zone's heating sensor under Configure → "
                "Valve look-ahead)"
            )

    def _dosing_wake(self) -> None:
        """The coordinator: another room started a pulse and this one is due
        within its dead time.  A cycle now decides whether to join it."""
        if self.hass is None or not self._dosing_live or not self.dosing_active:
            return
        self.hass.async_create_task(self._async_coordinated_cycle())

    async def _async_coordinated_cycle(self) -> None:
        """The cycle a coordinator wake queued: none once the entity is gone
        (removed between the wake and the task running)."""
        if self._dosing_live:
            await self._async_regulation_cycle(trigger="coordinated")

    def _dosing_cancel_wake(self) -> None:
        if self._dose_wake_cancel is not None:
            self._dose_wake_cancel()
            self._dose_wake_cancel = None

    async def _async_dose_wake(self, _now: Any) -> None:
        """A running pulse is re-judged at the second it should end (the
        minute timer would make a 3-min pulse up to 4)."""
        self._dose_wake_cancel = None
        if not self._dosing_live:
            return
        await self._async_regulation_cycle(trigger="dose_end")

    # -- the regulation cycle ---------------------------------------------------
    def _dosing_note_command(self, now: float, command_c: float, ok: bool) -> None:
        """Every command tried, whatever the path (``_note_attempt``).  A
        maximum is a pulse's only while the cycle decided a pulse (the
        regulation state is this cycle's by the send): a heat-up's is heat
        the model does not know."""
        if not self._dosing_enabled or self._dosing_failure is not None:
            return
        try:
            self._doser.note_command(
                now, command_c, min_c=self._config.min_target_c,
                max_c=self._config.max_target_c, ok=ok,
                pulse=self._reg_state.mode == MODE_DOSE,
            )
        except Exception as err:  # noqa: BLE001 - never stop the regulation
            self._dosing_fail(err)

    def _dosing_note_zone(self, now: float, trv: Any) -> None:
        """This room's zone heating, for the house's burn signal: every
        cycle that reads the TRV, dosing or not (the heating % when known,
        stamped with when it was reported, else the entity's hvac_action)."""
        try:
            data = self.coordinator.data or {}
            self._doser.note_zone(
                now, data.get("heating_power"), trv.heating,
                reported_ts=data.get("heating_power_reported_ts"),
            )
        except Exception as err:  # noqa: BLE001 - never stop the regulation
            self._dosing_fail(err)

    def _dosing_allowed(self) -> bool:
        """Dosing decides this cycle: on, and nothing else in charge.  While
        a window is open (or re-warming) or a boost runs the room is not
        settled, and the regulator drops the settled state."""
        if not self.dosing_active:
            return False
        if self._window_open():
            return False
        return self._preset_mode != PRESET_BOOST

    def _dosing_inputs(
        self, now: float, trv: Any, setpoint: float, room_temp: float,
        slope: float | None, *, room_fresh: bool,
    ) -> dict[str, Any]:
        """regulation.compute()'s dosing keyword arguments ({} while off:
        compute's defaults are today's regulation).  A bridged room reading
        (*room_fresh* False) feeds no estimate."""
        self._dose_fired_with = None
        if not self._dosing_enabled or self._dosing_failure is not None:
            return {}
        try:
            # The valve seen shut with no heat command of ours on its way: an
            # off that was never sent (the valve already showed off, a
            # restart in between) must not leave the booking open.
            pending = self._pending(now, trv)
            if trv.off and pending != PENDING_HEAT:
                self._doser.note_valve_shut(now)
            elif self._trv_holds_unbooked_heat(trv) and pending is None:
                # A heat setpoint no command of ours explains (a hold sent
                # before dosing was on, the upgrade from 2.15.0, the app):
                # its heat must show before a pulse is judged on top of it.
                self._doser.note_valve_heating_unbooked(now)
            if self._autotune_enabled:
                # The heat-up's rise at full power seeds the gain; its dead
                # time and coast are a cold radiator's, not a top-up's.
                rate, _theta = self._autotuner.heatup_model()
                self._doser.seed(rate)
            return self._doser.inputs(
                now, enabled=self._dosing_allowed(), setpoint_c=setpoint,
                room_c=room_temp, room_slope_c_per_s=slope, state=self._reg_state,
                deadband_c_per_h=self._config.slope_deadband_c_per_h,
                release_below_c=self._config.release_below_c,
                room_fresh=room_fresh,
            )
        except Exception as err:  # noqa: BLE001 - never stop the regulation
            self._dosing_fail(err)
            return {}

    def _trv_holds_unbooked_heat(self, trv: Any) -> bool:
        """The valve holds a heat setpoint between the frost level and the
        maximum: a hold's, never a pulse's or an off's."""
        if trv.off or trv.scheduled or not _finite(trv.setpoint_c):
            return False
        cfg = self._config
        top = trv.max_c if _finite(trv.max_c) else cfg.max_target_c
        return cfg.min_target_c + 0.05 < trv.setpoint_c < min(top, cfg.max_target_c) - 0.05

    def _dosing_after_compute(
        self, now: float, result: Any, setpoint: float, room_temp: float,
        slope: float | None, trv: Any, *, room_fresh: bool,
    ) -> None:
        """After the cycle's send (a pulse's open time is booked by then)."""
        if not self._dosing_enabled or self._dosing_failure is not None:
            return
        try:
            reason = self._last_reason
            sent = reason.startswith("sent(")
            failed = reason.startswith("send_failed(")
            if getattr(result, "mode", None) == MODE_DOSE and reason == "already_at_target":
                # The valve already holds the maximum (an off of ours it
                # dropped): the pulse runs, and its open time is booked as
                # if sent now.
                sent = True
                self._dosing_note_command(now, result.target_for_tado_c, True)
            if getattr(result, "fired_by", None) and not sent and not failed:
                # The pulse's command was held back (a change by hand
                # settling, a window's delay): its clock starts when it goes
                # out, on a later cycle, and only then is it a pulse (an
                # episode, a start the other rooms may join).  A failed call
                # keeps its clock (it may have landed: booked as open), so
                # the pulse still ends by its need or its maximum length
                # (2.15.0 review: restarted every cycle, it never ended).
                self._reg_state.pulse_start_ts = None
                self._reg_state.pulse_setpoint_c = None
            data = self.coordinator.data or {}
            outcome = self._doser.after_compute(
                now, result, setpoint_c=setpoint, room_c=room_temp,
                room_slope_c_per_s=slope, trv_internal_c=trv.internal_c,
                heating_pct=data.get("heating_power"),
                heating_ts=data.get("heating_power_ts"),
                window=self._window_open(), boost=self._preset_mode == PRESET_BOOST,
                sent=sent, room_fresh=room_fresh,
            )
            self._dose_fired_with = outcome.fired_with
            for line in outcome.lines:
                self._log(_LOGGER).info("dosed top-up: %s", line)
            self._dosing_cancel_wake()
            if outcome.wake_in_s is not None and self.hass is not None and self._dosing_live:
                self._dose_wake_cancel = async_call_later(
                    self.hass, outcome.wake_in_s, self._async_dose_wake
                )
        except Exception as err:  # noqa: BLE001 - never stop the regulation
            self._dosing_fail(err)

    def _dosing_interrupted(self, now: float, why: str | None = None) -> None:
        """The room sensor is lost, the watchdog shut the valve, Roomstat was
        switched off or summer mode began: the room is out of the dosing
        phase, and a running pulse has ended now (its start, end and target
        count for the next one's refractory).  With ``why`` the episodes
        being measured are dropped (the room was not left to its pulse)."""
        state = self._reg_state
        running = state.pulse_start_ts is not None or state.mode == MODE_DOSE
        state.settled = False
        if state.pulse_start_ts is not None:
            state.last_pulse_start_ts = state.pulse_start_ts
            state.pulse_start_ts = None
            state.last_pulse_end_ts = now
            if _finite(state.pulse_setpoint_c):
                state.last_pulse_setpoint_c = state.pulse_setpoint_c
        if state.mode == MODE_DOSE:
            state.mode = MODE_STOP
        state.pulse_setpoint_c = None
        state.pulse_cap_s = None
        if why and running and self._dosing_enabled and self._dosing_failure is None:
            try:
                for verdict in self._doser.learner.drop_all(why):
                    self._log(_LOGGER).info("dosed top-up: %s", verdict.line)
            except Exception as err:  # noqa: BLE001 - never stop the regulation
                self._dosing_fail(err)

    def _dosing_resume(self, now: float) -> None:
        """After the resume mode was decided from what the valve holds: a
        maximum resumes as a heat-up, but one that is a pulse of ours still
        running (saved with the room) carries on as the pulse it is, its
        open time booked from now if the booking did not join up (a long
        restart); the regulator then ends it on its first cycle if it is
        due.  Any other resume ends a pulse saved running: the valve no
        longer holds it."""
        if not self.dosing_active:
            return
        state = self._reg_state
        if state.pulse_start_ts is None:
            return
        if state.mode == MODE_HEAT and state.settled:
            state.mode = MODE_DOSE
            if not self._doser.open:
                self._doser.resume_open(now)
        elif state.mode != MODE_DOSE:
            self._dosing_interrupted(now)

    def _dosing_activity_facts(self, facts: Any, result: Any) -> None:
        """The decision line's dosing facts (activity.CycleFacts)."""
        if not self._dosing_enabled:
            return
        facts.dose_settled = bool(getattr(result, "settled", False))
        facts.dose_pulse_s = getattr(result, "pulse_s", None)
        facts.dose_need_c = getattr(result, "need_c", None)
        facts.dose_inflight_c = self._doser.inflight_c
        facts.dose_fired_by = getattr(result, "fired_by", None)
        facts.dose_end = getattr(result, "dose_end", None)
        facts.dose_ready_in_s = getattr(result, "ready_in_s", None)
        facts.dose_cap_s = getattr(result, "pulse_cap_s", None)
        if self._dose_fired_with is not None:
            facts.dose_with = self._dosing_room_name(self._dose_fired_with[1])

    def _dosing_room_name(self, entry_id: str) -> str:
        """Another room's name (its config entry's title)."""
        hass = self.hass
        entry = None
        if hass is not None and getattr(hass, "config_entries", None) is not None:
            entry = hass.config_entries.async_get_entry(entry_id)
        return entry.title if entry is not None else "other room"

    # -- reset and persistence ---------------------------------------------------
    def _dosing_reset(self) -> None:
        """Forget what was learned (with the auto-tune reset)."""
        self._doser.reset()
        if self._dosing_enabled:
            self._log(_LOGGER).info("dosed top-ups reset: learning afresh")

    def _dosing_state(self) -> dict[str, Any]:
        """For the restore-state store."""
        state = self._reg_state
        return {
            "trv": self._config_entry.data.get(CONF_SOURCE_ENTITY_ID),
            "doser": self._doser.to_dict(),
            "settled": state.settled,
            "pulse_start_ts": state.pulse_start_ts,
            "last_pulse_start_ts": state.last_pulse_start_ts,
            "last_pulse_end_ts": state.last_pulse_end_ts,
            "pulse_setpoint_c": state.pulse_setpoint_c,
            "pulse_cap_s": state.pulse_cap_s,
            "last_pulse_setpoint_c": state.last_pulse_setpoint_c,
        }

    def _dosing_restore(self, data: Any, now: float) -> None:
        """Take back what was learned on this room's TRV, the heat in flight
        and the dosing phase (before the resume mode is decided)."""
        if not isinstance(data, dict):
            return
        trv = self._config_entry.data.get(CONF_SOURCE_ENTITY_ID)
        if data.get("trv") != trv:
            if self._dosing_enabled:
                self._log(_LOGGER).info(
                    "dosed top-ups learned on %s, not %s: learning afresh",
                    data.get("trv"), trv,
                )
            return
        try:
            self._doser.restore(data.get("doser"), now)
            cfg = self._config.dose
            state = self._reg_state

            def past(ts: Any, within_s: float) -> float | None:
                return float(ts) if _finite(ts) and now - within_s <= ts <= now + 1.0 else None

            state.settled = data.get("settled") is True
            # A pulse still running when the room was saved (an entry reload),
            # with its longest length (a save from 2.15.0: the fixed maximum).
            # Within the longest any pulse may run it is taken back, and the
            # first cycle ends it if it is due (review 3: older than its cap
            # it was dropped, and the valve's 25 °C resumed as a heat-up).
            limit = data.get("pulse_cap_s")
            limit = (min(float(limit), cfg.pulse_ceiling_s)
                     if _finite(limit) and limit > 0 else cfg.pulse_max_s)
            state.pulse_start_ts = past(data.get("pulse_start_ts"), cfg.pulse_ceiling_s)
            state.pulse_cap_s = limit if state.pulse_start_ts is not None else None
            state.last_pulse_start_ts = past(
                data.get("last_pulse_start_ts"), cfg.refractory_max_s
            )
            state.last_pulse_end_ts = past(
                data.get("last_pulse_end_ts"), cfg.refractory_max_s
            )
            if state.pulse_start_ts is None and _finite(data.get("pulse_start_ts")):
                # Saved running, over since: it ended at the latest by now.
                state.last_pulse_start_ts = past(
                    data.get("pulse_start_ts"), cfg.refractory_max_s
                )
                if state.last_pulse_start_ts is not None:
                    state.last_pulse_end_ts = now
            pulse_sp = data.get("pulse_setpoint_c")
            state.pulse_setpoint_c = (
                float(pulse_sp) if _finite(pulse_sp) and state.pulse_start_ts is not None
                else None
            )
            last_sp = data.get("last_pulse_setpoint_c")
            state.last_pulse_setpoint_c = (
                float(last_sp) if _finite(last_sp) and state.last_pulse_start_ts is not None
                else None
            )
        except Exception as err:  # noqa: BLE001 - a bad save starts afresh
            self._doser.reset()
            self._dosing_interrupted(now)
            self._log(_LOGGER).warning(
                "the saved dosed top-ups could not be taken back (%s): learning afresh",
                err,
            )
