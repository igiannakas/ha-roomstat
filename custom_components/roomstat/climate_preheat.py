"""Preheat on the way home on the climate entity (PreheatMixin).

The rule is HA-free (preheat.py).  This mixin gathers its inputs while
nobody is home: the room's own state (away mode in charge, the target coming
home would restore, the room's temperature, the auto-tuner's heating rate
and dead time, the learned 30-minute recovery) and three Home ETA entities
(the first expected arrival, someone close to home, someone nearby).

**Modes.**  The decision is worked out at the start of every regulation
cycle and whenever one of the three entities changes, shown (the
thermostat's ``preheat_*`` attributes, the room's Preheat sensor) and logged
(the activity log and the HA log, one line per change of decision).  In
**shadow** (2.11.0, a week of decisions gathered first) that is all: the
room stays at Away.  In **on** (2.18.0, owner, 1 Nov 2026) the decision's
target is the room's setpoint while away mode is in charge
(:meth:`_preheat_target_c`, read by ``_effective_setpoint``), and a change
between cycles that moves it (a Home ETA entity, the schedule while away,
back to automatic) runs a cycle at once (:meth:`_preheat_refresh`).

**Changes by hand while nobody is home** (2.12.0, owner, 27 Oct 2026):
Away or Resume schedule is back to automatic, which is where preheat works;
an Off picked by hand, or the one the room was in when everyone left, holds
the room off until someone is home (``off_by_hand``); any other change by
hand is in charge until its limit or until someone is home
(``change_by_hand``).  Until 2.11.0 an Away picked while out held the room
cold; that went, Off does it now.

The 30-minute recovery is learned whatever the mode (preheat.RecoveryLearner,
fed by every regulated cycle), so a room has its own value by the time
preheat is switched on.
"""
from __future__ import annotations

import datetime
import logging
import time
from typing import Any

from homeassistant.components.climate import (
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    HVACMode,
)
from homeassistant.core import callback

from .climate_activity import local_hhmm
from .const import (
    CONF_PREHEAT_ARRIVAL_ENTITY_ID,
    CONF_PREHEAT_CLOSE_ENTITY_ID,
    CONF_PREHEAT_MODE,
    CONF_PREHEAT_NEARBY_ENTITY_ID,
    CONF_PRESENCE_SENSOR_ID,
    PRESET_FROST_PROTECTION,
)
from .parameters import PREHEAT_SETTLE_S
from .preheat import (
    INACTIVE_CHANGE_BY_HAND,
    INACTIVE_HOME,
    INACTIVE_NO_INPUTS,
    INACTIVE_NO_PRESENCE,
    INACTIVE_OFF,
    INACTIVE_OFF_BY_HAND,
    INACTIVE_SUMMER,
    INACTIVE_WINDOW,
    MODE_OFF,
    MODE_ON,
    MODE_SHADOW,
    MODES,
    LoggedDecision,
    PreheatController,
    PreheatDecision,
    PreheatInputs,
    RecoveryLearner,
    config_from_options,
    decision_attributes,
    describe,
    parse_arrival,
    parse_on_off,
    target_in_use,
)

_LOGGER = logging.getLogger(__name__)

PREHEAT_ENTITY_KEYS = (
    CONF_PREHEAT_ARRIVAL_ENTITY_ID,
    CONF_PREHEAT_CLOSE_ENTITY_ID,
    CONF_PREHEAT_NEARBY_ENTITY_ID,
)

# The thermostat's live preheat numbers (they change every cycle): not
# recorded, the Preheat sensor records each decision with what it was made
# with.
PREHEAT_LIVE_ATTRIBUTES = frozenset({
    "preheat_mode", "preheat_state", "preheat_inactive_reason", "preheat_target_c",
    "preheat_heating_to_c", "preheat_recovery_temp_c", "preheat_needed_min",
    "preheat_due_at", "preheat_arrival_at", "preheat_since",
})


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(ts, tz=datetime.UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if f == f and f not in (float("inf"), float("-inf")) else None


class PreheatMixin:
    """Preheat on the way home methods for RoomstatClimate."""

    # -- setup -------------------------------------------------------------
    def _preheat_setup(self) -> None:
        opts = self._config_entry.options
        mode = opts.get(CONF_PREHEAT_MODE, MODE_OFF)
        self._preheat_mode: str = mode if mode in MODES else MODE_OFF
        self._preheat_cfg = config_from_options(opts)
        self._preheat = PreheatController(self._preheat_cfg)
        self._recovery = RecoveryLearner()
        # The live decision (every evaluation), and the one last logged:
        # the Preheat sensor shows the latter, so it changes only with a
        # new decision (one recorder row each).
        self._preheat_decision: PreheatDecision | None = None
        self._preheat_logged: LoggedDecision | None = None
        # Failures are logged once each, with their traceback; the regulation
        # carries on whatever happens here.
        self._preheat_failure: str | None = None
        self._recovery_failure: str | None = None
        # Set by the regulated path of each cycle: a cycle that never fed the
        # learner drops the episode being measured (_async_regulation_cycle).
        self._recovery_fed: bool = False
        # When the target preheat gives the room last changed (mode on): more
        # heat waits PREHEAT_SETTLE_S from then (_preheat_settle_remaining_s).
        self._preheat_change_ts: float = 0.0
        # The entity was removed (a reload): a retarget's cycle queued just
        # before runs nothing.
        self._preheat_removed: bool = False

    @property
    def preheat_mode(self) -> str:
        """off / shadow / on."""
        return self._preheat_mode

    def _preheat_target_c(self) -> float | None:
        """The setpoint preheat gives the room right now, or None (Away).

        Mode on only, and only while away mode itself is in charge: nobody
        home, the room at Away (a change by hand, or a window's frost
        protection and its close delay, are another preset), heating on, no
        summer lock.  The decision is the one last evaluated (at the start
        of every cycle, and on a change of its inputs), so these are checked
        here too: a window or a change by hand between evaluations takes the
        room off it at once, and the thermostat and the activity line never
        show a target the room is not getting.
        """
        if self._preheat_mode != MODE_ON:
            return None
        if (
            self._summer_active
            or self._hvac_mode != HVACMode.HEAT
            or self._preset_mode != PRESET_AWAY
            or not self._presence_ctrl.is_active
        ):
            return None
        return target_in_use(self._preheat_decision, self._config.presets.away_target_c)

    def _preheat_prefix(self) -> str:
        return "preheat (shadow)" if self._preheat_mode == MODE_SHADOW else "preheat"

    def _preheat_entity_ids(self) -> list[str]:
        """The Home ETA entities in use (none while preheat is off)."""
        if self._preheat_mode == MODE_OFF:
            return []
        opts = self._config_entry.options
        return [opts[k] for k in PREHEAT_ENTITY_KEYS if opts.get(k)]

    # -- inputs ------------------------------------------------------------
    def _preheat_inactive_reason(self) -> str | None:
        """Why away mode is not in charge of the room right now (None: it is)."""
        if self._summer_active:
            return INACTIVE_SUMMER
        if self._hvac_mode == HVACMode.OFF:
            return INACTIVE_OFF
        if not self._config_entry.options.get(CONF_PRESENCE_SENSOR_ID):
            return INACTIVE_NO_PRESENCE
        if not self._preheat_entity_ids():
            return INACTIVE_NO_INPUTS
        ctrl = self._presence_ctrl
        if ctrl.override_active:
            return INACTIVE_OFF_BY_HAND if ctrl.off_until_home else INACTIVE_CHANGE_BY_HAND
        if not ctrl.is_active:
            return INACTIVE_HOME
        if self._window_open():
            return INACTIVE_WINDOW
        return None

    def _preheat_scheduled_c(self) -> float | None:
        """The target coming home would restore right now.

        Mirrors ``_restore_presence_state`` and ``_effective_setpoint``: the
        preset away mode saved (updated by the schedule while away), a Boost
        replaced by the schedule's preset, Comfort at its current option.
        """
        saved = self._presence_ctrl.get_saved()
        preset, temp = saved.preset, saved.temp
        if preset is None:
            return None
        if preset == PRESET_BOOST:
            preset = self._fallback_preset()
            temp = self._get_preset_target(preset)
        if preset == PRESET_COMFORT:
            return self._comfort_target()
        if preset == PRESET_NONE:
            return _opt_float(temp)
        if preset == PRESET_ECO:
            return self._config.presets.eco_target_c
        if preset == PRESET_AWAY:
            return self._config.presets.away_target_c
        if preset == PRESET_FROST_PROTECTION:
            return self._config.presets.frost_protection_target_c
        return None

    def _preheat_state_of(self, key: str) -> str | None:
        entity_id = self._config_entry.options.get(key)
        if not entity_id or self.hass is None:
            return None
        state = self.hass.states.get(entity_id)
        return state.state if state is not None else None

    def _preheat_inputs(self, now: float) -> PreheatInputs:
        reason = self._preheat_inactive_reason()
        rate = dead = None
        if self._autotune_enabled and not self._autotune_failed:
            rate, dead = self._autotuner.heatup_model()
        return PreheatInputs(
            now=now,
            enabled=self._preheat_mode != MODE_OFF,
            inactive_reason=reason,
            scheduled_c=self._preheat_scheduled_c() if reason is None else None,
            away_c=self._config.presets.away_target_c,
            room_c=(self.coordinator.data or {}).get("room_temp"),
            arrival_ts=parse_arrival(self._preheat_state_of(CONF_PREHEAT_ARRIVAL_ENTITY_ID)),
            close=parse_on_off(self._preheat_state_of(CONF_PREHEAT_CLOSE_ENTITY_ID)),
            nearby=parse_on_off(self._preheat_state_of(CONF_PREHEAT_NEARBY_ENTITY_ID)),
            rate_c_per_h=rate,
            dead_time_s=dead,
            recovery_c=self._recovery.recovery_c,
        )

    # -- the decision ------------------------------------------------------
    def _preheat_evaluate(self, now: float, *, write: bool = False) -> bool:
        """Work out what the room aims for; log a change of decision.

        Returns whether the setpoint preheat gives the room
        (:meth:`_preheat_target_c`) changed: only ever in mode on.  Never
        raises: preheat must not stop the regulation (False then: the
        target in use stays the last one worked out).  ``write``: also write
        the thermostat's state (an input changed between cycles).
        """
        try:
            before = self._preheat_target_c()
            decision = self._preheat.evaluate(self._preheat_inputs(now))
            self._preheat_decision = decision
            changed = self._preheat_target_c() != before
            if changed:
                self._preheat_change_ts = now
            logged = self._preheat_logged
            if logged is not None and logged.decision.key == decision.key:
                self._preheat_failure = None
                return changed
            line = describe(
                decision,
                logged.decision if logged is not None else None,
                self._preheat_cfg,
                lambda ts: local_hhmm(ts, now),
                live=self._preheat_mode == MODE_ON,
            )
            self._preheat_logged = LoggedDecision(
                decision, now, decision_attributes(decision, _iso)
            )
            if line:
                self._log(_LOGGER).info("%s: %s", self._preheat_prefix(), line)
            sensor = getattr(self.coordinator, "preheat_entity", None)
            if sensor is not None and sensor.hass is not None:
                sensor.async_write_ha_state()
            if write:
                self.async_write_ha_state()
            self._preheat_failure = None
            return changed
        except Exception as err:  # noqa: BLE001 - preheat must never stop the regulation
            what = f"{type(err).__name__}: {err}"
            if what != self._preheat_failure:
                self._preheat_failure = what
                _LOGGER.exception("%s: preheat failed", self._config_entry.title)
            # Fail cold: a decision that can't be worked out again must not
            # keep the last one heating with no cap, slip or grace (2.18.0
            # review: 8 h at 20.5 °C).  The room is at Away until it can.
            heated = self._preheat_decision is not None and self._preheat_target_c() is not None
            self._preheat_decision = None
            if heated:
                self._preheat_change_ts = now
            return heated

    def _preheat_settle_remaining_s(self, now: float) -> float:
        """Seconds until a change of the target preheat gives the room has
        stood PREHEAT_SETTLE_S (0: it has).  A clock that went back ends it."""
        since = now - self._preheat_change_ts
        if since < 0 or since >= PREHEAT_SETTLE_S:
            return 0.0
        return PREHEAT_SETTLE_S - since

    def _preheat_refresh(self, trigger: str, *, write: bool = True) -> None:
        """Decide again now, between cycles (an input changed).

        In mode on, a decision that moves the setpoint preheat gives the room
        runs a regulation cycle at once, so the valve hears of it now rather
        than at the next minute; one that leaves it where it was runs none
        (a new expected arrival every few minutes changes nothing for the
        valve until the room is due).  ``write``: write the thermostat's
        state on a new decision (False: the caller writes it next).
        """
        if self._preheat_evaluate(time.time(), write=write):
            self.hass.async_create_task(self._async_preheat_cycle(trigger))

    async def _async_preheat_cycle(self, trigger: str) -> None:
        """The cycle a retarget asked for, unless the room was removed since."""
        if self._preheat_removed or self.hass is None:
            return
        await self._async_regulation_cycle(trigger=trigger)

    @callback
    def _async_preheat_input_changed(self, event) -> None:
        """A Home ETA entity changed: decide again now, not at the next cycle."""
        self._preheat_refresh("preheat")

    # -- the 30-minute recovery -------------------------------------------
    def _preheat_learn(
        self, now: float, setpoint_c: float | None, room_c: float | None
    ) -> None:
        """Feed one cycle to the 30-minute recovery learner: a regulated one
        with its setpoint and reading, any other with None (no part of a
        heat-up).  Never raises."""
        self._recovery_fed = True
        try:
            usable = (
                setpoint_c is not None
                and self._hvac_mode == HVACMode.HEAT
                and not self._summer_active
                and not self._sensor_degraded
                and self._preset_mode != PRESET_BOOST
            )
            episode = self._recovery.observe(
                now, setpoint_c, room_c, usable=usable, window=self._window_open()
            )
            if episode is not None:
                typical = self._recovery.recovery_c
                count = len(self._recovery.episodes)
                self._log(_LOGGER).info(
                    "30-min recovery: %+.2f °C in the first 30 min after the target "
                    "rose to %.1f (%.1f below); %s",
                    episode.rise_c, episode.target_c, episode.deficit_c,
                    f"typical now {typical:.2f} °C ({count} heat-ups)"
                    if typical is not None
                    else f"not enough yet to use ({count} of "
                         f"{self._recovery.cfg.min_episodes} heat-ups)",
                )
            self._recovery_failure = None
        except Exception as err:  # noqa: BLE001 - learning must never stop the regulation
            what = f"{type(err).__name__}: {err}"
            if what != self._recovery_failure:
                self._recovery_failure = what
                _LOGGER.exception(
                    "%s: learning the 30-minute recovery failed", self._config_entry.title
                )

    # -- persistence ---------------------------------------------------------
    def _preheat_as_dict(self) -> dict[str, Any]:
        return {
            "controller": self._preheat.as_dict(),
            "recovery": self._recovery.as_dict(),
            "logged": self._preheat_logged.as_dict() if self._preheat_logged else None,
        }

    def _preheat_restore(self, data: Any, now: float) -> None:
        """Take back what a restart or reload saved.

        A 2.11.0 save may hold an Away held cold (``held_cold``): that hold
        is gone in 2.12.0, so it is ignored, and a decision last logged as
        held cold is not taken back (the next evaluation logs afresh).
        """
        if not isinstance(data, dict):
            return
        try:
            self._recovery.restore(data.get("recovery"), now)
            if self._preheat_mode != MODE_OFF:
                self._preheat.restore(data.get("controller"), now)
            self._preheat_logged = LoggedDecision.from_dict(data.get("logged"))
        except Exception:  # noqa: BLE001 - a bad save must never stop the room starting
            _LOGGER.exception(
                "%s: preheat's saved state could not be taken back; starting afresh",
                self._config_entry.title,
            )
            self._recovery = RecoveryLearner()
            self._preheat = PreheatController(self._preheat_cfg)
            self._preheat_logged = None

    # -- what is shown -------------------------------------------------------
    @property
    def preheat_sensor_state(self) -> str | None:
        """The decision last logged (the Preheat sensor's state)."""
        logged = self._preheat_logged
        return logged.decision.state if logged is not None else None

    def preheat_sensor_attributes(self) -> dict[str, Any]:
        """What the decision last logged was made with, and when.  Fixed until
        the next decision, so the recorder keeps one row per decision."""
        logged = self._preheat_logged
        attrs: dict[str, Any] = {"mode": self._preheat_mode}
        if logged is not None:
            attrs["decided_at"] = _iso(logged.ts)
            attrs.update(logged.attributes)
        return attrs

    @property
    def preheat_recovery_c(self) -> float | None:
        """The learned 30-minute recovery (None until learned)."""
        value = self._recovery.recovery_c
        return None if value is None else round(value, 2)

    def preheat_recovery_attributes(self) -> dict[str, Any]:
        cfg = self._recovery.cfg
        return {
            "heat_ups": [
                {
                    "at": _iso(e.ts),
                    "target_c": round(e.target_c, 1),
                    "below_c": round(e.deficit_c, 2),
                    "rise_c": round(e.rise_c, 2),
                }
                for e in reversed(self._recovery.episodes)
            ],
            "needed": cfg.min_episodes,
            "default_c": self._preheat_cfg.recovery_default_c,
        }

    def _preheat_attributes(self) -> dict[str, Any]:
        """The live decision, on the thermostat (updated every cycle)."""
        d = self._preheat_decision
        return {
            "preheat_mode": self._preheat_mode,
            "preheat_state": d.state if d is not None else None,
            "preheat_inactive_reason": d.inactive_reason if d is not None else None,
            "preheat_target_c": d.target_c if d is not None else None,
            "preheat_heating_to_c": self._preheat_target_c(),
            "preheat_recovery_temp_c": d.recovery_temp_c if d is not None else None,
            "preheat_needed_min": (
                round(d.needed_s / 60.0, 1) if d is not None and d.needed_s is not None else None
            ),
            "preheat_due_at": _iso(d.due_ts) if d is not None else None,
            "preheat_arrival_at": _iso(d.arrival_ts) if d is not None else None,
            "preheat_since": _iso(d.since_ts) if d is not None else None,
        }

    def preheat_diagnostics(self) -> dict[str, Any]:
        d = self._preheat_decision
        return {
            "mode": self._preheat_mode,
            "heating_to_c": self._preheat_target_c(),
            "config": dict(vars(self._preheat_cfg)),
            "decision": decision_attributes(d, _iso) if d is not None else None,
            "state": d.state if d is not None else None,
            **self._preheat_as_dict(),
        }
