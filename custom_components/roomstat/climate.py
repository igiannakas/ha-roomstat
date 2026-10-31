"""Climate Entity for Roomstat."""
from __future__ import annotations

import asyncio
import datetime
import logging
import math
import time
from typing import Any

from homeassistant.components.climate import (
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    PRECISION_TENTHS,
    UnitOfTemperature,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .activity import preset_name
from .autotune import (
    STATUS_DISABLED,
    Autotuner,
    SlopeEstimator,
    Tuning,
)
from .climate_activity import ActivityMixin
from .climate_controllers import (
    STARTUP_ARM_OPEN,
    STARTUP_RESTORE,
    OutOfBandMonitor,
    PersistedAutomationState,
    PresenceAutomationController,
    SavedState,
    ScheduleController,
    TrvReading,
    WindowAutomationController,
    choose_trv,
    is_maximum,
    normalize_restored_preset,
    presence_startup_action,
    resolve_boost_restore,
    window_startup_action,
)
from .climate_dosing import DosingMixin
from .climate_lookahead import LookaheadMixin
from .climate_preheat import PREHEAT_LIVE_ATTRIBUTES, PreheatMixin
from .climate_presets import PresetMixin
from .climate_regulation import RegulationMixin
from .climate_schedule import ScheduleMixin
from .climate_summer import SummerMixin
from .const import (
    CONF_AUTOTUNE_DERIVATIVE,
    CONF_AUTOTUNE_ENABLED,
    CONF_AWAY_TARGET,
    CONF_BAND_C,
    CONF_BAND_MIN,
    CONF_BOOST_DURATION,
    CONF_BOOST_TARGET,
    CONF_COLD_BAND_MIN,
    CONF_CORRECTION_KI,
    CONF_CORRECTION_KP,
    CONF_DERIVATIVE_TIME_MIN,
    CONF_ECO_TARGET,
    CONF_FALLBACK_ENTITY_ID,
    CONF_FROST_PROTECTION_TARGET,
    CONF_HEAT_TIMER_S,
    CONF_INTEGRAL_DEADBAND_C,
    CONF_MIN_CHANGE_THRESHOLD_C,
    CONF_MIN_COMMAND_INTERVAL_S,
    CONF_OVERLAY_REFRESH_S,
    CONF_PREFER_FALLBACK,
    CONF_PRESENCE_AWAY_DELAY_S,
    CONF_PRESENCE_SENSOR_ID,
    CONF_SCHEDULE_ENTITY_ID,
    CONF_SENSOR_GRACE_S,
    CONF_SUMMER_MODE_ENTITY_ID,
    CONF_URGENT_DECREASE_THRESHOLD_C,
    CONF_WINDOW_DELAY_S,
    CONF_WINDOW_SENSOR_ID,
    DOMAIN,
    PRESET_FROST_PROTECTION,
    PRESET_LIST,
    PRESET_SCHEDULE,
    safe_float,
)
from .parameters import (
    DEFAULT_CONTROL_INTERVAL_S,
    DEFAULT_SENSOR_GRACE_S,
    HEAT_TIMER_MIN_S,
    WATCHDOG_CHECK_INTERVAL_S,
    AutotuneConfig,
    BehaviourConfig,
    CorrectionTuning,
    OutOfBandConfig,
    PresetConfig,
    RegulationConfig,
)
from .regulation import FeedforwardPiRegulator, RegulationState, resume_mode

_LOGGER = logging.getLogger(__name__)


class _AutomationExtraData(ExtraStoredData):
    """RestoreEntity wrapper around the HA-free PersistedAutomationState.

    The auto-tuner's learned state rides along under the ``autotune`` key,
    so learned values survive restarts and config-entry reloads without
    ever being written to the config entry (which would trigger a reload).
    The out-of-band monitor's low since the last setback rides along under
    ``out_of_band``, the last TRV command under ``command``
    (CommandRecord: a reload seconds after a command must not forget it),
    the room slope's recent samples under ``slope`` (a restart in the
    middle of a heat-up must not blind its predicted stop), and the activity
    log's lines under ``activity`` (a reload must not empty it), the
    last change by hand under ``manual_change_ts`` (a preset temperature
    change reloads the room; it still settles before it adds heat),
    preheat's state under ``preheat`` (a heading-home start, an Away held
    cold, the learned 30-minute recovery, the decision last logged), the
    valve look-ahead's learning under ``lookahead``, and dosed top-ups'
    model, heat in flight and settled state under ``dosing``.
    """

    def __init__(
        self,
        state: PersistedAutomationState,
        autotune: dict[str, Any] | None = None,
        out_of_band: dict[str, Any] | None = None,
        command: dict[str, Any] | None = None,
        slope: dict[str, Any] | None = None,
        activity: dict[str, Any] | None = None,
        manual_change_ts: float | None = None,
        preheat: dict[str, Any] | None = None,
        lookahead: dict[str, Any] | None = None,
        dosing: dict[str, Any] | None = None,
    ) -> None:
        self._state = state
        self._autotune = autotune
        self._out_of_band = out_of_band
        self._command = command
        self._slope = slope
        self._activity = activity
        self._manual_change_ts = manual_change_ts
        self._preheat = preheat
        self._lookahead = lookahead
        self._dosing = dosing

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict for the restore-state store."""
        data = self._state.as_dict()
        if self._autotune is not None:
            data["autotune"] = self._autotune
        if self._out_of_band is not None:
            data["out_of_band"] = self._out_of_band
        if self._command is not None:
            data["command"] = self._command
        if self._slope is not None:
            data["slope"] = self._slope
        if self._activity is not None:
            data["activity"] = self._activity
        if self._manual_change_ts:
            data["manual_change_ts"] = self._manual_change_ts
        if self._preheat is not None:
            data["preheat"] = self._preheat
        if self._lookahead is not None:
            data["lookahead"] = self._lookahead
        if self._dosing is not None:
            data["dosing"] = self._dosing
        return data


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Roomstat climate entity."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entity = RoomstatClimate(
        coordinator=coordinator,
        unique_id=entry.entry_id,
        config_entry=entry,
    )
    coordinator.climate_entity = entity
    async_add_entities([entity])


class RoomstatClimate(
    ActivityMixin,
    PreheatMixin,
    LookaheadMixin,
    DosingMixin,
    RegulationMixin,
    PresetMixin,
    ScheduleMixin,
    SummerMixin,
    CoordinatorEntity,
    ClimateEntity,
    RestoreEntity,
):
    """Proxy climate entity that controls a Tado TRV via feedforward + PI."""

    _attr_has_entity_name = True
    # The live activity line changes every cycle; the activity sensor's
    # history already keeps the lines.  Likewise preheat's live numbers:
    # the Preheat sensor records each decision with what it was made with.
    # The valve look-ahead's correction changes every cycle too.
    _unrecorded_attributes = frozenset(
        {
            "activity", "valve_lookahead_c", *PREHEAT_LIVE_ATTRIBUTES,
            "topup_gain", "topup_inflight_c", "topup_virtual_error_c",
            "topup_ready_in_min",
        }
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_TENTHS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.PRESET_MODE
    )
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_preset_modes = PRESET_LIST
    _attr_translation_key = "roomstat"
    # Class-level defaults so HA's CachedProperties metaclass sees 5/25
    # BEFORE super().__init__() runs (prevents fallback to HA's 7/35).
    _attr_min_temp: float = 5.0
    _attr_max_temp: float = 25.0

    def __init__(self, coordinator, unique_id: str, config_entry: ConfigEntry):
        """Initialize the proxy thermostat."""
        super().__init__(coordinator)
        self._attr_unique_id = unique_id
        self._config_entry = config_entry
        self._attr_name = None  # uses translation key

        # Build regulation + behaviour config from defaults + options
        self._config = self._build_config(config_entry)
        self._attr_min_temp = self._config.min_target_c
        self._attr_max_temp = self._config.max_target_c
        # Invalidate HA's CachedProperties cache for min/max_temp so the
        # instance-level values from _config take effect immediately.
        self.__dict__.pop("min_temp", None)
        self.__dict__.pop("max_temp", None)
        # "Schedule" (resume) is only offered when a schedule is configured.
        self._attr_preset_modes = (
            [*PRESET_LIST, PRESET_SCHEDULE]
            if config_entry.options.get(CONF_SCHEDULE_ENTITY_ID)
            else list(PRESET_LIST)
        )
        self.__dict__.pop("preset_modes", None)
        self._behaviour = self._build_behaviour(config_entry)
        self._regulator = FeedforwardPiRegulator(self._config)
        self._reg_state = RegulationState()

        # Background auto-tune.  The configured Kp/Ki/Td are the baseline the
        # tuner starts from and is bounded around; learned values live only
        # in the restore-state data (see _AutomationExtraData).
        self._autotune_enabled: bool = bool(
            config_entry.options.get(CONF_AUTOTUNE_ENABLED, False)
        )
        self._autotuner = Autotuner(
            AutotuneConfig(),
            self._configured_tuning(),
            derivative_allowed=bool(
                config_entry.options.get(CONF_AUTOTUNE_DERIVATIVE, True)
            ),
        )
        self._slope = SlopeEstimator()
        # The valve look-ahead (climate_lookahead.py; off unless the option is on).
        self._lookahead_setup()
        self._dosing_setup()
        # The activity log (climate_activity.py): set up before anything logs.
        self._activity_setup()
        # Preheat on the way home (climate_preheat.py, shadow only).
        self._preheat_setup()
        # Set if the tuner ever raises: it is then ignored until restart.
        self._autotune_failed = False
        self._apply_active_tuning()

        # UI state
        self._hvac_mode = HVACMode.HEAT
        self._target_temp: float = self._comfort_target()
        self._preset_mode: str = PRESET_COMFORT

        # Boost timer
        self._boost_cancel: CALLBACK_TYPE | None = None
        self._boost_saved_preset: str = PRESET_COMFORT
        self._boost_saved_temp: float | None = None
        self._boost_end_ts: float = 0.0

        # Timing
        self._regulation_lock = asyncio.Lock()
        self._last_regulation_ts = 0.0
        self._last_command_sent_ts = 0.0
        # The last change by hand, and the cycle that sends the heat it asked
        # for once it has settled (MANUAL_SETTLE_S).
        self._manual_change_ts = 0.0
        self._settle_cancel: CALLBACK_TYPE | None = None
        self._last_sent_setpoint: float | None = None
        # The last command tried (the frost level for an off), whether the
        # TRV took it and which entity it went to: what pending_command judges
        # (a call that timed out may still land), kept across an entry reload
        # (CommandRecord).  _last_command_sent_ts is when it was tried.
        self._last_attempt_c: float | None = None
        self._last_attempt_ok: bool = False
        self._last_attempt_entity: str | None = None
        # The TRV's state has shown the last command (it read otherwise after
        # the command, then like it): its state is current (_note_trv_shows).
        self._last_attempt_shown: bool = False
        self._last_attempt_seen_other: bool = False
        # The last heat command while it may still be on its way: until the
        # state shows it or an off is taken (a failed off does not end it).
        self._last_heat_attempt_c: float | None = None
        self._last_heat_attempt_ts: float | None = None
        self._last_heat_attempt_ok: bool = False
        # The last heat command tried, and when an off sent seconds after
        # one is to be sent once more (OFF_CONFIRM_S, off_confirm_after).
        self._last_heat_command_ts: float | None = None
        self._off_confirm_ts: float | None = None
        # Why the off is due again: "heat" (it came seconds after a heat
        # command) or "lost" (the valve kept heating after it), and how many
        # lost-off resends went out since the last heat command.
        self._off_confirm_reason: str = "heat"
        self._lost_off_resends: int = 0
        # Back-off for a TRV that keeps not showing a command it took: how
        # many times in a row it was sent again (resend_grace_s), and whether
        # the next attempt is such a resend.
        self._resend_count: int = 0
        self._resend_due: bool = False

        # Sensor resilience: last-valid values for grace-period bridging
        self._last_valid_room_temp: float | None = None
        self._last_valid_room_temp_ts: float = 0.0
        self._sensor_grace_s: int = config_entry.options.get(
            CONF_SENSOR_GRACE_S, DEFAULT_SENSOR_GRACE_S
        )
        self._sensor_degraded: bool = False

        # Overlay refresh: optional periodic resend for cloud-API integrations
        # with timer-based overlays (e.g. exabird ha-tado-x uses 30 min TIMER).
        # Value of 0 means disabled (default, correct for Matter/Thread).
        self._overlay_refresh_s: int = config_entry.options.get(
            CONF_OVERLAY_REFRESH_S, 0
        )

        # True while the fallback entity (e.g. the room's Tado cloud
        # thermostat) is the one in use: while the configured TRV is
        # unavailable, or always when the fallback is preferred.  Starts on
        # the preferred one, so the first cycle logs no switch.
        self._trv_fallback_active: bool = self._prefer_fallback()

        # Failure handling (see climate_regulation.py): the current command
        # error (logged once, not every attempt), and when the room sensor
        # went missing (its loss is logged once).
        self._last_command_error: str | None = None
        self._room_missing_since: float | None = None
        self._room_sensor_lost_logged: bool = False

        # State-machine controllers (hold their own timer + saved-state)
        self._window_ctrl = WindowAutomationController()
        self._presence_ctrl = PresenceAutomationController()
        # The window close wait (room re-warming on its own) is logged once.
        self._window_recovery_logged: bool = False

        # Schedule following (see climate_schedule.py)
        self._schedule_ctrl = ScheduleController()

        # Summer mode: thermostat locked, the TRV held off (see climate_summer.py)
        self._summer_active: bool = False
        self._summer_bypass_rate_limit: bool = False

        # Fail cold (see climate_regulation.py): when the timer overlay of a
        # maximum command was sent (None: no timer of ours known), whether
        # its renewal already skipped the rate limit, and whether the last
        # command the TRV took was the maximum (for the watchdog).
        self._heat_timer_sent_ts: float | None = None
        self._heat_timer_refresh_tried: bool = False
        self._last_sent_was_max: bool = False
        # The resume mode is decided again on the first regulation cycle that
        # sees the TRV when it had no setpoint at startup (entity not loaded).
        self._resume_pending: bool = False
        # Watchdog: when a regulation cycle last reached its decision.
        self._last_cycle_completed_ts: float = time.time()
        self._regulation_stalled: bool = False
        self._watchdog_shut_tried: bool = False

        # Out-of-band alarm (binary_sensor.<room>_out_of_band)
        self._band_config = self._build_band_config(config_entry)
        self._band_monitor = OutOfBandMonitor()
        self._band_failed: bool = False         # its error is logged once

        # Diagnostics
        self._last_result = None
        self._last_reason = "startup"

    # ------------------------------------------------------------------
    # Config builders
    # ------------------------------------------------------------------

    @staticmethod
    def _build_config(entry: ConfigEntry) -> RegulationConfig:
        """Build regulation config, applying options over defaults."""
        config = RegulationConfig()
        opts = entry.options
        if opts:
            kp = opts.get(CONF_CORRECTION_KP, config.tuning.kp)
            ki = opts.get(CONF_CORRECTION_KI, config.tuning.ki)
            td_s = float(
                opts.get(CONF_DERIVATIVE_TIME_MIN, config.tuning.td_s / 60.0) or 0
            ) * 60.0
            config.tuning = CorrectionTuning(kp=kp, ki=ki, td_s=td_s)
            config.presets = PresetConfig(
                eco_target_c=opts.get(CONF_ECO_TARGET, config.presets.eco_target_c),
                boost_target_c=opts.get(CONF_BOOST_TARGET, config.presets.boost_target_c),
                boost_duration_min=opts.get(CONF_BOOST_DURATION, config.presets.boost_duration_min),
                away_target_c=opts.get(CONF_AWAY_TARGET, config.presets.away_target_c),
                frost_protection_target_c=opts.get(CONF_FROST_PROTECTION_TARGET, config.presets.frost_protection_target_c),
            )
            # Tado V3+ accepts 5-25 C, so the device ceiling wins over a
            # higher boost preset: clamp boost into range rather than
            # raising max_target_c above what the TRV will accept.
            config.presets.boost_target_c = min(
                config.presets.boost_target_c, config.max_target_c
            )
            config.min_command_interval_s = opts.get(
                CONF_MIN_COMMAND_INTERVAL_S, config.min_command_interval_s
            )
            config.min_change_threshold_c = opts.get(
                CONF_MIN_CHANGE_THRESHOLD_C, config.min_change_threshold_c
            )
            config.integral_deadband_c = opts.get(
                CONF_INTEGRAL_DEADBAND_C, config.integral_deadband_c
            )
            # Too short a timer only costs renewals (see HEAT_TIMER_MIN_S).
            heat_timer_s = safe_float(opts.get(CONF_HEAT_TIMER_S))
            if heat_timer_s is not None and heat_timer_s > 0:
                config.heat_timer_s = max(heat_timer_s, HEAT_TIMER_MIN_S)
        return config

    @staticmethod
    def _build_band_config(entry: ConfigEntry) -> OutOfBandConfig:
        """Out-of-band thresholds, options over defaults (bad values ignored)."""
        defaults = OutOfBandConfig()
        opts = entry.options or {}

        def positive(key: str, default: float) -> float:
            value = safe_float(opts.get(key))
            return value if value is not None and value > 0 else default

        return OutOfBandConfig(
            band_c=positive(CONF_BAND_C, defaults.band_c),
            band_min=positive(CONF_BAND_MIN, defaults.band_min),
            cold_band_min=positive(CONF_COLD_BAND_MIN, defaults.cold_band_min),
        )

    def _configured_tuning(self) -> Tuning:
        """The Kp/Ki/Td from the options (the auto-tuner's baseline).

        Read from the options, not from ``self._config``, because
        ``_apply_active_tuning`` replaces ``self._config.tuning`` with the
        learned values.
        """
        t = self._build_config(self._config_entry).tuning
        return Tuning(float(t.kp), float(t.ki), float(t.td_s))

    def _apply_active_tuning(self) -> None:
        """Point the regulator at the learned (or configured) values."""
        if self._autotune_enabled and not self._autotune_failed:
            active = self._autotuner.active_tuning()
        else:
            active = self._configured_tuning()
        self._config.tuning = CorrectionTuning(
            kp=active.kp, ki=active.ki, td_s=active.td_s
        )
        self._regulator.cfg = self._config

    @property
    def autotune_enabled(self) -> bool:
        """True when the background auto-tune is switched on."""
        return self._autotune_enabled

    @property
    def last_regulation(self) -> Any:
        """The last regulation result (mode, error, command), or None before
        the first cycle.  Read by the trace sensors (sensor.py)."""
        return self._last_result

    @property
    def active_tuning(self) -> Tuning:
        """The Kp / Ki / Td the regulator is using right now."""
        t = self._config.tuning
        return Tuning(t.kp, t.ki, t.td_s)

    @property
    def configured_tuning(self) -> Tuning:
        """The Kp / Ki / Td from the options."""
        return self._autotuner.baseline

    def autotune_summary(self) -> dict[str, Any]:
        """Diagnostic snapshot of the auto-tuner (status 'disabled' when off)."""
        try:
            summary = self._autotuner.summary(now=time.time())
        except Exception:  # noqa: BLE001 - diagnostics must never break state writes
            self._log(_LOGGER).exception("auto-tune summary failed")
            self._autotune_failed = True
            self._apply_active_tuning()     # fall back to the configured values
            return {"status": STATUS_DISABLED, "error": "auto-tune failed, see log"}
        if not self._autotune_enabled or self._autotune_failed:
            summary["status"] = STATUS_DISABLED
        return summary

    async def async_reset_autotune(self) -> None:
        """Forget learned values and return to the configured ones (the
        valve look-ahead's and dosed top-ups' too)."""
        self._autotuner.reset()
        self._apply_active_tuning()
        self._log(_LOGGER).info("auto-tune reset to configured values")
        self._lookahead_reset()
        self._dosing_reset()
        self._write_state_with_binary_sensor()

    @staticmethod
    def _build_behaviour(entry: ConfigEntry) -> BehaviourConfig:
        """Build behaviour config, applying options over defaults."""
        defaults = BehaviourConfig()
        opts = entry.options
        if not opts:
            return defaults
        return BehaviourConfig(
            urgent_decrease_threshold_c=opts.get(
                CONF_URGENT_DECREASE_THRESHOLD_C, defaults.urgent_decrease_threshold_c
            ),
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for the proxy."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.title,
            manufacturer="Roomstat",
            model="Feedforward + PI Regulator",
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to HA."""
        await super().async_added_to_hass()

        # Restore previous state
        now = time.time()
        last_state = await self.async_get_last_state()
        # Automation snapshot (window / presence / boost).  None on the first
        # start after upgrading from a version that did not persist it.
        persisted: PersistedAutomationState | None = None
        # A 2.11.0 save's Away held cold (preheat), for its conversion below.
        legacy_held_cold = False
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            extra_dict = extra.as_dict()
            persisted = PersistedAutomationState.from_dict(extra_dict)
            if isinstance(extra_dict, dict):
                saved_preheat = extra_dict.get("preheat")
                legacy_held_cold = isinstance(saved_preheat, dict) and isinstance(
                    saved_preheat.get("held_cold"), dict
                )
                self._autotuner.restore(extra_dict.get("autotune"))
                self._apply_active_tuning()
                # What the tuner last did is in its restored state, not new.
                self._activity_tuner_seq = self._autotuner.journal_seq
                self._activity.restore(extra_dict.get("activity"))
                self._band_monitor.restore(extra_dict.get("out_of_band"))
                # The last command, if it was only seconds ago (an entry
                # reload): its rate limit, and that it may not show in the
                # TRV's state yet.
                self._restore_command(extra_dict.get("command"), now)
                # A change by hand a few seconds ago (a preset temperature
                # changed: it reloads the room) still settles.
                self._restore_change_by_hand(extra_dict.get("manual_change_ts"), now)
                # Preheat: a heading-home start, the 30-minute recovery and
                # the decision last logged.
                self._preheat_restore(extra_dict.get("preheat"), now)
                # The valve look-ahead learned on this room's TRV.
                self._lookahead_restore(extra_dict.get("lookahead"), now)
                # Dosed top-ups: the model learned on this room's TRV, the
                # heat in flight and the settled state.
                self._dosing_restore(extra_dict.get("dosing"), now)
                # The room slope's recent samples, if they still join up
                # with now: a heat-up keeps its predicted stop.
                if self._slope.restore(extra_dict.get("slope"), now):
                    _LOGGER.debug(
                        "%s: room slope history taken back (%s)",
                        self._config_entry.title,
                        "known" if self._slope.slope_c_per_s() is not None else "too short yet",
                    )
        # A line in the activity log, so a gap in it reads as what it was.
        self._log(_LOGGER).info(
            "started (Home Assistant restart or room reload)%s",
            "" if extra is not None else "; nothing saved to take back",
        )
        self._lookahead_check_setup()
        self._dosing_check_setup()
        self._dosing_start()

        # Carry on a heat burst (or stop) the TRV is still holding.  What
        # the TRV holds is the last command it took (or one still on its
        # way into its state): a maximum is one the watchdog shuts if the
        # cycle stops.  After a restart the TRV entity may not have loaded
        # yet: then this is decided again on the first regulation cycle that
        # sees it (_resume_pending).
        held = self._held_command(now, self._active_trv())
        self._reg_state.mode = resume_mode(held, self._config)
        self._dosing_resume(now)
        self._resume_pending = held is None
        self._last_sent_was_max = held is not None and is_maximum(
            held, self._config.max_target_c
        )
        self._last_cycle_completed_ts = now

        if last_state:
            if last_state.state in (HVACMode.HEAT, HVACMode.OFF):
                self._hvac_mode = HVACMode(last_state.state)
            temp = safe_float(last_state.attributes.get(ATTR_TEMPERATURE))
            if temp is not None:
                self._target_temp = temp
            # Restore preset (default to comfort if missing or invalid).
            # A user-selected frost protection is kept; only a window-driven
            # one without a persisted snapshot falls back to comfort.
            restored_preset = last_state.attributes.get("preset_mode")
            if restored_preset in PRESET_LIST or restored_preset == PRESET_NONE:
                self._preset_mode = normalize_restored_preset(
                    restored_preset,
                    persisted,
                    legacy_window_active=bool(
                        last_state.attributes.get("window_open_active")
                    ),
                )

        # The schedule's state first: the presence and window restores below
        # (and at startup) need its preset and its override, as at any other
        # time.  Following it comes last (_startup_schedule).
        self._startup_schedule_state(persisted)

        # Re-arm the automation controllers from the persisted snapshot so a
        # restart or reload does not lose the pre-automation preset.
        legacy_note = None
        if persisted is not None:
            if persisted.window_active:
                self._window_ctrl.activate(
                    persisted.window_saved.preset or PRESET_COMFORT,
                    persisted.window_saved.temp,
                )
            legacy_note = self._load_presence_snapshot(persisted, legacy_held_cold)

        # Resume a running boost, or fall back if it ended while HA was down.
        if self._preset_mode == PRESET_BOOST:
            boost = resolve_boost_restore(persisted, time.time())
            self._boost_saved_preset = boost.fallback.preset
            self._boost_saved_temp = boost.fallback.temp
            if boost.resume:
                self._boost_end_ts = time.time() + boost.remaining_s
                self._boost_cancel = async_call_later(
                    self.hass, boost.remaining_s, self._async_boost_expired
                )
                self._log(_LOGGER).info(
                    "Startup: boost resumed, %d min remaining",
                    math.ceil(boost.remaining_s / 60),
                )
            else:
                self._apply_saved_preset(boost.fallback)
                self._log(_LOGGER).info(
                    "Startup: boost ended while HA was down, reverting to %s",
                    boost.fallback.preset,
                )

        # If the active preset is COMFORT, the comfort_target in options is
        # authoritative (may have changed via the number entity while HA was down).
        # For PRESET_NONE (manual), the restored slider temperature wins.
        if self._preset_mode == PRESET_COMFORT:
            self._target_temp = self._comfort_target()

        # Summer mode overrides everything restored above.
        summer_on = self._startup_summer(
            persisted.summer_active if persisted is not None else False
        )
        if legacy_note and not summer_on:
            self._log(_LOGGER).info("startup: %s", legacy_note)

        # Config entry update listener (from number entities)
        self.async_on_remove(
            self._config_entry.add_update_listener(self._async_config_entry_updated)
        )

        # State change listener on the source TRV and its fallback (summer
        # mode switches a valve turned on from the dial or app off again)
        trv_entities = [
            eid
            for eid in (
                self._config_entry.data.get("source_entity_id"),
                self._config_entry.options.get(CONF_FALLBACK_ENTITY_ID),
            )
            if eid
        ]
        if trv_entities:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    trv_entities,
                    self._async_tado_state_changed,
                )
            )

        # Window / presence sensor listeners
        window_sensor = self._config_entry.options.get(CONF_WINDOW_SENSOR_ID)
        if window_sensor:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [window_sensor],
                    self._async_window_changed,
                )
            )
        presence_sensor = self._config_entry.options.get(CONF_PRESENCE_SENSOR_ID)
        if presence_sensor:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [presence_sensor],
                    self._async_presence_changed,
                )
            )
        summer_entity = self._config_entry.options.get(CONF_SUMMER_MODE_ENTITY_ID)
        if summer_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [summer_entity],
                    self._async_summer_changed,
                )
            )
        schedule_entity = self._config_entry.options.get(CONF_SCHEDULE_ENTITY_ID)
        if schedule_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [schedule_entity],
                    self._async_schedule_changed,
                )
            )
        # Preheat's Home ETA entities (none while preheat is off).
        preheat_entities = self._preheat_entity_ids()
        if preheat_entities:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    preheat_entities,
                    self._async_preheat_input_changed,
                )
            )

        # Reconcile presence first: if it restores while window mode is still
        # active, it updates the window's saved state (see
        # _restore_presence_state), which the window reconciliation then uses.
        # Summer mode ignores window and presence entirely.
        if not summer_on:
            self._startup_reconcile_presence(presence_sensor, persisted)
            self._startup_reconcile_window(window_sensor)
        # Schedule last: it routes into window/presence saved state if active.
        self._startup_schedule(summer_on)

        # Start periodic regulation
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_regulation_cycle_timer,
                datetime.timedelta(seconds=DEFAULT_CONTROL_INTERVAL_S),
            )
        )
        # The watchdog on its own timer: it must not depend on the cycle's.
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_watchdog_check,
                datetime.timedelta(seconds=WATCHDOG_CHECK_INTERVAL_S),
            )
        )

    def _startup_reconcile_presence(
        self,
        presence_sensor: str | None,
        persisted: PersistedAutomationState | None,
    ) -> None:
        """Align presence automation with the presence sensor at startup."""
        presence_state = (
            self.hass.states.get(presence_sensor) if presence_sensor else None
        )
        state = presence_state.state if presence_state else None

        if self._presence_ctrl.override_active:
            # A change by hand while nobody was home.  Someone home now (or
            # no sensor any more): it simply stays, but an Off picked by hand
            # ends and the schedule comes back.  Still nobody (or not
            # reported yet): re-arm the rest of its limit; one that ran out
            # while HA was down is handled at once, as at any other time.
            action = presence_startup_action(True, bool(presence_sensor), state)
            if action == STARTUP_RESTORE:
                if self._presence_ctrl.off_until_home:
                    self._log(_LOGGER).info(
                        "startup, presence sensor is %s",
                        state if presence_sensor else "not configured",
                    )
                    self._restore_presence_state(notify=False)
                    return
                self._log(_LOGGER).info(
                    "startup, presence sensor is %s, the change made while "
                    "nobody was home stays",
                    state if presence_sensor else "not configured",
                )
                self._presence_ctrl.end_override()
            elif self._presence_ctrl.rearm_override(
                self.hass, time.time(), self._async_presence_override_expired
            ):
                self._log(_LOGGER).info(
                    "startup, the limit of the change made while nobody was "
                    "home ran out while Home Assistant was down",
                )
            return

        if self._presence_ctrl.is_active:
            # Re-armed from the persisted snapshot.
            action = presence_startup_action(True, bool(presence_sensor), state)
            if action == STARTUP_RESTORE:
                self._log(_LOGGER).info(
                    "Startup: presence automation was active, sensor is %s – restoring",
                    state if presence_sensor else "not configured",
                )
                self._restore_presence_state(notify=False)
            return

        if not presence_sensor or presence_state is None:
            return

        if state == "off":
            if self._preset_mode == PRESET_AWAY and persisted is None:
                # Legacy (no snapshot): AWAY was restored but the controller
                # state was not persisted.  Pre-activate with COMFORT as the
                # saved state so that coming home restores a useful preset
                # instead of AWAY → AWAY (no-op).
                self._presence_ctrl.activate(PRESET_COMFORT, self._comfort_target())
                self._log(_LOGGER).info(
                    "Startup: preset AWAY restored, controller pre-activated "
                    "with COMFORT as saved state"
                )
            else:
                delay = self._config_entry.options.get(CONF_PRESENCE_AWAY_DELAY_S, 600)
                self._presence_ctrl.handle_presence_away(
                    self.hass, delay, self._async_presence_away_action
                )
                self._log(_LOGGER).info("Startup: presence sensor is away, action in %ds", delay)
        elif state not in ("unavailable", "unknown"):
            # Legacy (no snapshot): presence shows home but preset was restored
            # as AWAY – the user returned while HA was down.  With a snapshot
            # an inactive controller means AWAY was chosen by the user: keep it.
            if self._preset_mode == PRESET_AWAY and persisted is None:
                self._preset_mode = PRESET_COMFORT
                self._target_temp = self._comfort_target()
                self._log(_LOGGER).info(
                    "Startup: presence is home but preset was AWAY, "
                    "switching to COMFORT"
                )

    def _startup_reconcile_window(self, window_sensor: str | None) -> None:
        """Align window automation with the window sensor at startup."""
        window_state = self.hass.states.get(window_sensor) if window_sensor else None
        state = window_state.state if window_state else None
        action = window_startup_action(
            self._window_ctrl.is_active, bool(window_sensor), state
        )
        if action == STARTUP_RESTORE:
            self._log(_LOGGER).info(
                "Startup: window automation was active, sensor is %s – restoring",
                state if window_sensor else "not configured",
            )
            self._restore_window_state(notify=False)
        elif action == STARTUP_ARM_OPEN:
            delay = self._config_entry.options.get(CONF_WINDOW_DELAY_S, 30)
            self._window_ctrl.handle_window_opened(
                self.hass, delay, self._async_window_action
            )
            self._log(_LOGGER).info("Startup: window sensor is open, action in %ds", delay)

    def _load_presence_snapshot(
        self, persisted: PersistedAutomationState, legacy_held_cold: bool
    ) -> str | None:
        """Take back away mode or a change by hand while nobody was home.

        A snapshot from before 2.12.0 (``presence_override_off_until_home``
        None) is read with the 2.12.0 rules (owner, 27 Oct 2026), and the
        line that says so is returned (logged unless summer mode is on):

        - away mode entered from an Off picked by hand, the Off saved for the
          return: the Off until someone is home;
        - the same after an Away picked while out (2.11.0 held the room
          cold): back to automatic, the schedule saved for the return;
        - an Off picked while out, kept as a change by hand with a limit:
          the Off until someone is home.
        """
        legacy = persisted.presence_override_off_until_home is None
        if persisted.presence_active:
            if legacy and self._saved_off_by_hand(persisted):
                if legacy_held_cold:
                    preset = self._fallback_preset()
                    self._schedule_ctrl.clear_override()
                    self._presence_ctrl.activate(preset, self._get_preset_target(preset))
                    return (
                        "the Away picked while nobody was home is back to automatic; "
                        f"{preset_name(preset)} comes back with you"
                    )
                self._presence_ctrl.load_override(None, off_until_home=True)
                frost = self._get_preset_target(PRESET_FROST_PROTECTION)
                if persisted.window_active:
                    self._window_ctrl.update_saved(PRESET_FROST_PROTECTION, frost)
                else:
                    self._preset_mode = PRESET_FROST_PROTECTION
                    self._target_temp = frost
                return "the Off picked by hand before everyone left stays until someone is home"
            self._presence_ctrl.activate(
                persisted.presence_saved.preset or PRESET_COMFORT,
                persisted.presence_saved.temp,
            )
            return None
        if not persisted.presence_override_active:
            return None
        # A change by hand while nobody was home; its timer is re-armed (or
        # found run out) in _startup_reconcile_presence.
        held = (
            persisted.window_saved.preset if persisted.window_active else self._preset_mode
        )
        off = (
            held == PRESET_FROST_PROTECTION
            if legacy
            else bool(persisted.presence_override_off_until_home)
        )
        self._presence_ctrl.load_override(
            persisted.presence_override_until,
            persisted.presence_override_before,
            off_until_home=off,
        )
        if legacy and off:
            return "the Off picked while nobody was home stays until someone is home"
        return None

    def _saved_off_by_hand(self, persisted: PersistedAutomationState) -> bool:
        """Away mode with an Off picked by hand saved for the return (see
        ``_off_picked_by_hand``; the schedule's own Off is not one)."""
        if persisted.presence_saved.preset != PRESET_FROST_PROTECTION:
            return False
        if not self.schedule_configured:
            return True
        return persisted.schedule_override_active and persisted.schedule_override_sticky

    def _apply_saved_preset(self, saved: SavedState) -> None:
        """Switch to a saved preset/temperature without side effects."""
        if saved.preset is None:
            return
        self._preset_mode = saved.preset
        if saved.preset == PRESET_COMFORT:
            self._target_temp = self._comfort_target()
        elif saved.temp is not None:
            self._target_temp = saved.temp

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Persist automation and auto-tune state across restart/reload."""
        return _AutomationExtraData(
            self._automation_snapshot(),
            self._autotuner.as_dict(),
            self._band_monitor.as_dict(),
            self._command_record().as_dict(),
            self._slope.to_dict(),
            self._activity.as_dict(),
            self._manual_change_ts,
            self._preheat_as_dict(),
            self._lookahead_state(),
            self._dosing_state(),
        )

    def _automation_snapshot(self) -> PersistedAutomationState:
        """Capture window / presence / boost automation state."""
        boost_running = self._preset_mode == PRESET_BOOST and self._boost_end_ts > 0
        return PersistedAutomationState(
            window_active=self._window_ctrl.is_active,
            window_saved=self._window_ctrl.get_saved(),
            presence_active=self._presence_ctrl.is_active,
            presence_saved=self._presence_ctrl.get_saved(),
            presence_override_active=self._presence_ctrl.override_active,
            presence_override_until=self._presence_ctrl.override_until,
            presence_override_before=self._presence_ctrl.get_before_override(),
            presence_override_off_until_home=self._presence_ctrl.off_until_home,
            boost_end_ts=self._boost_end_ts if boost_running else 0.0,
            boost_saved=SavedState(
                preset=self._boost_saved_preset, temp=self._boost_saved_temp
            ),
            summer_active=self._summer_active,
            schedule_preset=self._schedule_ctrl.schedule_preset,
            schedule_override_active=self._schedule_ctrl.override_active,
            schedule_override_until=self._schedule_ctrl.override_until,
            schedule_override_sticky=self._schedule_ctrl.override_sticky,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel all timers when the entity is being removed.

        Active flags, saved presets and the boost end time are kept so the
        automation snapshot stays accurate (HA reads extra_restore_state_data
        on removal, e.g. during a config-entry reload).
        """
        self._window_ctrl.cancel_timers()
        self._presence_ctrl.cancel_timer()
        self._schedule_ctrl.cancel_timer()
        if self._boost_cancel is not None:
            self._boost_cancel()
            self._boost_cancel = None
        self._cancel_settled_cycle()
        self._dosing_stop()
        await super().async_will_remove_from_hass()

    async def _async_config_entry_updated(self, hass, entry) -> None:
        """Called when config entry options change (e.g. from number entities)."""
        self._config_entry = entry
        self._config = self._build_config(entry)
        self._attr_min_temp = self._config.min_target_c
        self._attr_max_temp = self._config.max_target_c
        self.__dict__.pop("min_temp", None)
        self.__dict__.pop("max_temp", None)
        self._behaviour = self._build_behaviour(entry)
        self._band_config = self._build_band_config(entry)
        self._autotune_enabled = bool(entry.options.get(CONF_AUTOTUNE_ENABLED, False))
        self._autotuner.set_baseline(
            self._configured_tuning(),
            bool(entry.options.get(CONF_AUTOTUNE_DERIVATIVE, True)),
        )
        self._apply_active_tuning()
        self._sensor_grace_s = entry.options.get(
            CONF_SENSOR_GRACE_S, DEFAULT_SENSOR_GRACE_S
        )
        self._overlay_refresh_s = entry.options.get(CONF_OVERLAY_REFRESH_S, 0)
        # Only sync comfort target when COMFORT preset is active; PRESET_NONE
        # (manual) keeps its independently set temperature.
        if self._preset_mode == PRESET_COMFORT:
            self._target_temp = self._comfort_target()
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Source TRV changes
    # ------------------------------------------------------------------

    @callback
    def _async_tado_state_changed(self, event) -> None:
        """React to a change on the source TRV.

        Only summer mode cares: a valve turned on from the dial or the Tado
        app is switched off again (the regulation cycle paces it like any
        off, see _async_summer_enforce).  Outside
        summer mode a dial change is never adopted as a new target; the
        proxy's next command overwrites it.
        """
        if not self._summer_active:
            return
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is not None and (
            old_state is None
            or new_state.state != old_state.state
            or new_state.attributes.get("temperature")
            != old_state.attributes.get("temperature")
        ):
            self.hass.async_create_task(
                self._async_regulation_cycle(trigger="summer_trv_changed")
            )

    # ------------------------------------------------------------------
    # Standard climate controls
    # ------------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target HVAC mode."""
        self._summer_guard("HVAC mode change")
        if hvac_mode not in self._attr_hvac_modes:
            return
        self.note_change_by_hand()
        # Switching on or off by hand clears any active window-open state so
        # the user's intention is respected.  Restore the saved pre-frost
        # preset first – otherwise the entity stays in FROST_PROTECTION with
        # no automation left to ever restore it (e.g. window closes while HVAC
        # is OFF).  Heat while already heating is no change and leaves the
        # window alone: the heating scripts send it before every preset, and
        # until 2.10.4 it ended the window's frost protection, so the preset
        # after it heated at full power with the window open (25 Oct 2026,
        # master bedroom 11:09, second bedroom 17:12).
        if self._window_ctrl.is_active and hvac_mode != self._hvac_mode:
            saved = self._window_ctrl.get_saved()
            self._window_ctrl.cancel_all()
            restore_preset = saved.preset if saved.preset is not None else PRESET_COMFORT
            # A saved BOOST has lost its timer context – fall back to COMFORT.
            # A saved frost protection was selected by the user before the
            # window opened, so it is restored as-is.
            if restore_preset == PRESET_BOOST:
                restore_preset = self._fallback_preset()
            self._preset_mode = restore_preset
            if restore_preset == PRESET_COMFORT:
                self._target_temp = self._comfort_target()
            elif restore_preset == PRESET_NONE and saved.temp is not None:
                self._target_temp = saved.temp

        previous_mode = self._hvac_mode
        self._hvac_mode = hvac_mode
        if hvac_mode != previous_mode:
            self._log(_LOGGER).info(
                "switched %s", "off" if hvac_mode == HVACMode.OFF else "on (heat)"
            )
        self.async_write_ha_state()

        # Forward HVAC mode to the source TRV entity
        if hvac_mode == HVACMode.OFF:
            # The regulation cycle switches the TRV off (at once, under the
            # regulation lock, so a cycle in progress can't overtake it with
            # a heat command), and keeps it off: off stays off even when the
            # TRV can't be reached right now.
            await self._async_regulation_cycle(trigger="hvac_mode_change")
        elif hvac_mode == HVACMode.HEAT and previous_mode == HVACMode.OFF:
            # Returning from OFF → reset timestamp so the first HEAT cycle uses
            # dt=0 and avoids an integral spike from the long OFF period.
            self._last_regulation_ts = 0
            # Turning back on resumes the schedule (OFF is not an override, so
            # any old override is dropped too).  While nobody is home that is
            # back to automatic, as Resume schedule is: a change by hand held
            # meanwhile (an Off until someone is home included) ends, rather
            # than the schedule's preset heating the empty house with the
            # change's limit, or none (2.12.0 review).
            back_to_automatic = (
                self._presence_ctrl.nobody_home and not self._presence_reads_home()
            )
            if (
                not back_to_automatic
                and self.schedule_configured
                and self._schedule_ctrl.schedule_preset
            ):
                self._schedule_ctrl.clear_override()
                self._route_preset(self._schedule_ctrl.schedule_preset)
            # Re-evaluate window sensor: if the window is still open after
            # OFF→HEAT, restart frost protection so we don't heat into the void.
            window_sensor = self._config_entry.options.get(CONF_WINDOW_SENSOR_ID)
            if window_sensor:
                ws = self.hass.states.get(window_sensor)
                if ws and ws.state == "on":
                    delay = self._config_entry.options.get(CONF_WINDOW_DELAY_S, 30)
                    self._window_ctrl.handle_window_opened(
                        self.hass, delay, self._async_window_action
                    )
                    self._log(_LOGGER).info(
                        "HVAC OFF→HEAT: window still open, frost action in %ds",
                        delay,
                    )
            if back_to_automatic:
                await self._async_back_to_automatic(
                    "Switched on", trigger="hvac_mode_change", regulate=True
                )
                return
            # The regulation brings the TRV out of off itself: one
            # set_temperature with hvac_mode heat, or nothing if the room
            # needs no heat (2.8.4).  No set_hvac_mode heat first: a Tado
            # zone would heat at the last temperature HA sent (25 °C after a
            # heat-up) until the right command lands, or for good if it does
            # not, and it costs an API call.
            await self._async_regulation_cycle(trigger="hvac_mode_change")
        else:
            await self._async_regulation_cycle(trigger="hvac_mode_change")

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature; enters manual (PRESET_NONE) mode.

        Moving the slider is treated as a temporary manual override. It does
        NOT change the stored comfort target – use the Comfort number entity
        or the options flow for that.
        """
        self._summer_guard("temperature change")
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        try:
            temp_f = float(temp)
        except (ValueError, TypeError):
            return
        if not math.isfinite(temp_f):
            self._log(_LOGGER).warning("Ignoring non-finite temperature: %s", temp)
            return
        # Clamp to safe range
        temp_f = max(self._config.min_target_c, min(self._config.max_target_c, temp_f))
        self._log(_LOGGER).info("temperature set to %.1f °C", temp_f)
        self.note_change_by_hand()

        # A manual temperature overrides the schedule (see climate_schedule.py)
        self._schedule_note_manual_temperature()

        # During the window's close delay it ends the wait.
        self._end_window_close_delay(f"temperature set to {temp_f:.1f} °C")

        # Set by hand while nobody is home: it takes effect, away steps
        # aside (until someone is home, or the configured limit).
        self._presence_takeover()

        # An open window still wins: save the temperature for its close.
        if self._window_ctrl.is_active:
            self._window_ctrl.update_saved(PRESET_NONE, temp_f)
            self._log(_LOGGER).info(
                "window open, %.1f °C saved for the close", temp_f,
            )
            self.async_write_ha_state()
            return

        self._target_temp = temp_f

        # Any direct temperature change activates manual mode and cancels
        # any running boost timer.
        if self._preset_mode != PRESET_NONE:
            if self._boost_cancel is not None:
                self._boost_cancel()
                self._boost_cancel = None
                self._boost_end_ts = 0.0
            self._preset_mode = PRESET_NONE

        self.async_write_ha_state()
        await self._async_regulation_cycle(trigger="set_temperature")

    # ------------------------------------------------------------------
    # Properties for HA UI
    # ------------------------------------------------------------------

    @property
    def current_temperature(self) -> float | None:
        """Return the room temperature from the external sensor."""
        return self.coordinator.data.get("room_temp")

    @property
    def target_temperature(self) -> float | None:
        """Return the effective setpoint so HA always shows the active target."""
        return self._effective_setpoint()

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        """Infer heating/idle from the Tado entity state."""
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF

        trv = self._active_trv()
        if trv.internal_c is not None and trv.setpoint_c is not None:
            if trv.setpoint_c > trv.internal_c + 0.05:
                return HVACAction.HEATING

        return HVACAction.IDLE

    def _prefer_fallback(self) -> bool:
        """Whether the fallback entity is used first (option, needs one set)."""
        opts = self._config_entry.options
        return bool(opts.get(CONF_PREFER_FALLBACK)) and bool(opts.get(CONF_FALLBACK_ENTITY_ID))

    def _active_trv(self) -> TrvReading:
        """The TRV to read and command now: the preferred one (the
        configured TRV, or the fallback entity when that is preferred), or the
        other while the preferred one is unavailable."""
        data = self.coordinator.data or {}

        def reading(entity_id: str | None, prefix: str) -> TrvReading:
            return TrvReading(
                entity_id,
                data.get(f"{prefix}_internal_temp"),
                data.get(f"{prefix}_setpoint"),
                min_c=data.get(f"{prefix}_min_temp"),
                max_c=data.get(f"{prefix}_max_temp"),
                off=bool(data.get(f"{prefix}_off")),
                is_cloud=bool(data.get(f"{prefix}_cloud")),
                scheduled=bool(data.get(f"{prefix}_scheduled")),
                heating=data.get(f"{prefix}_heating"),
            )

        primary = reading(self._config_entry.data.get("source_entity_id"), "tado")
        fallback_id = self._config_entry.options.get(CONF_FALLBACK_ENTITY_ID)
        fallback = reading(fallback_id, "fallback") if fallback_id else None
        return choose_trv(primary, fallback, self._prefer_fallback())

    @property
    def icon(self) -> str:
        """Return preset-specific icon so the primary entity icon reflects the active preset."""
        if self._summer_active:
            return "mdi:weather-sunny"
        if self._hvac_mode == HVACMode.OFF:
            return "mdi:power"
        return {
            PRESET_COMFORT: "mdi:sofa",
            PRESET_ECO: "mdi:weather-night",
            PRESET_BOOST: "mdi:rocket-launch",
            PRESET_AWAY: "mdi:home-export-outline",
            PRESET_FROST_PROTECTION: "mdi:snowflake",
            PRESET_NONE: "mdi:hand-back-right",
        }.get(self._preset_mode, "mdi:thermostat")

    @property
    def preset_mode(self) -> str:
        """Return the current preset mode."""
        return self._preset_mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic attributes visible in HA Developer Tools."""
        trv = self._active_trv()
        attrs: dict[str, Any] = {
            # What the room is doing now (the activity log's live line).
            "activity": self._activity.doing,
            "regulation_reason": self._last_reason,
            "tado_internal_temp_c": trv.internal_c,
            "trv_fallback_active": trv.is_fallback,
            "trv_cloud": trv.is_cloud,
            "trv_prefer_fallback": self._prefer_fallback(),
            "active_trv_entity_id": trv.entity_id,
            "last_command_error": self._last_command_error,
            "correction_kp": self._config.tuning.kp,
            "correction_ki": self._config.tuning.ki,
            "correction_td_min": round(self._config.tuning.td_s / 60.0, 1),
            "autotune_status": self.autotune_summary()["status"],
            "effective_setpoint_c": self._effective_setpoint(),
            "window_open_active": self._window_ctrl.is_active,
            "window_close_delay_active": self._window_ctrl.close_delay_active,
            "presence_away_active": self._presence_ctrl.is_active,
            # Nobody home, but a change by hand holds away off (the card
            # shows it), until someone is home or the "until" time.
            "presence_override_active": self._presence_ctrl.override_active,
            "presence_override_until": (
                datetime.datetime.fromtimestamp(
                    self._presence_ctrl.override_until, tz=datetime.UTC
                ).isoformat()
                if self._presence_ctrl.override_until is not None
                else None
            ),
            "sensor_degraded": self._sensor_degraded,
            "summer_mode_active": self._summer_active,
            "overlay_refresh_s": self._overlay_refresh_s,
            # Fail cold: timer overlays for maximum commands (0 = off), and
            # the watchdog (no regulation cycle completed for 10 min).
            "heat_timer_s": self._config.heat_timer_s,
            "regulation_stalled": self._regulation_stalled,
            # Read by the dashboard card: boost length for its confirmation
            # text, and the Tado entity whose device holds the heating % sensor.
            "boost_duration_min": self._config.presets.boost_duration_min,
            "source_entity_id": self._config_entry.data.get("source_entity_id"),
            **self._schedule_attributes(),
            # Preheat on the way home (shadow: what it would do).
            **self._preheat_attributes(),
        }
        # The valve look-ahead (only with the option on): the look-ahead in
        # use and what it took off this cycle's command.
        if self._lookahead_enabled:
            attrs["valve_lookahead_min"] = self.valve_lookahead_min
            attrs["valve_lookahead_c"] = round(self._cycle_lookahead_c, 2)
        # Dosed top-ups (only with the option on): the model's gain, the heat
        # still on its way, the room as the regulator judges it, and when the
        # next pulse is due.
        attrs.update(self._dosing_attributes())

        # Sensor resilience diagnostics
        if self._last_valid_room_temp is not None:
            attrs["room_temp_last_valid_c"] = self._last_valid_room_temp
            if self._last_valid_room_temp_ts > 0:
                age = int(time.time() - self._last_valid_room_temp_ts)
                attrs["room_temp_last_valid_age_s"] = age

        if self._last_result:
            r = self._last_result
            attrs.update({
                "feedforward_offset_c": r.feedforward_offset_c,
                "p_correction_c": r.p_correction_c,
                "i_correction_c": r.i_correction_c,
                "error_c": r.error_c,
                "target_for_tado_c": r.target_for_tado_c,
                "is_saturated": r.is_saturated,
                "regulation_mode": r.mode,
            })

        return attrs
