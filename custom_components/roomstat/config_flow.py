"""Config and Options flows for Roomstat."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .climate_controllers import (
    OBSOLETE_OPTIONS,
    change_trv,
    options_form_errors,
    options_form_values,
)
from .const import (
    CONF_AUTOTUNE_DERIVATIVE,
    CONF_AUTOTUNE_ENABLED,
    CONF_BAND_C,
    CONF_BAND_MIN,
    CONF_BOOST_DURATION,
    CONF_COLD_BAND_MIN,
    CONF_CORRECTION_KI,
    CONF_CORRECTION_KP,
    CONF_DERIVATIVE_TIME_MIN,
    CONF_EXTERNAL_TEMPERATURE_ENTITY_ID,
    CONF_FALLBACK_ENTITY_ID,
    CONF_HEAT_TIMER_S,
    CONF_HEATING_POWER_ENTITY_ID,
    CONF_INTEGRAL_DEADBAND_C,
    CONF_MIN_CHANGE_THRESHOLD_C,
    CONF_MIN_COMMAND_INTERVAL_S,
    CONF_NAME,
    CONF_OVERLAY_REFRESH_S,
    CONF_PREFER_FALLBACK,
    CONF_PREHEAT_ARRIVAL_ENTITY_ID,
    CONF_PREHEAT_CLOSE_ENTITY_ID,
    CONF_PREHEAT_MODE,
    CONF_PREHEAT_NEARBY_ENTITY_ID,
    CONF_PRESENCE_AWAY_DELAY_S,
    CONF_PRESENCE_HOME_DELAY_S,
    CONF_PRESENCE_OVERRIDE_MAX_MIN,
    CONF_PRESENCE_SENSOR_ID,
    CONF_SCHEDULE_ENTITY_ID,
    CONF_SCHEDULE_OVERRIDE_MIN,
    CONF_SENSOR_GRACE_S,
    CONF_SOURCE_ENTITY_ID,
    CONF_SUMMER_MODE_ENTITY_ID,
    CONF_URGENT_DECREASE_THRESHOLD_C,
    CONF_VALVE_LOOKAHEAD,
    CONF_WINDOW_CLOSE_DELAY_S,
    CONF_WINDOW_DELAY_S,
    CONF_WINDOW_SENSOR_ID,
    DEFAULT_PRESENCE_OVERRIDE_MAX_MIN,
    DOMAIN,
)
from .parameters import (
    DEFAULT_SENSOR_GRACE_S,
    BehaviourConfig,
    OutOfBandConfig,
    RegulationConfig,
)
from .preheat import MODE_OFF, MODES, TIMING_OPTIONS


class RoomstatConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """UI setup for the integration (initial setup)."""

    VERSION = 1
    # 2: one controller (no switch, no gain scheduling); Kp is the gain near
    # the target.  __init__.py async_migrate_entry converts older options.
    MINOR_VERSION = 2

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            source_entity_id = user_input[CONF_SOURCE_ENTITY_ID]
            ext_temp_entity_id = user_input[CONF_EXTERNAL_TEMPERATURE_ENTITY_ID]
            name = (user_input.get(CONF_NAME) or "").strip() or "Roomstat"

            # EntitySelector already validates entity existence on the frontend.
            # Redundant backend registry checks caused false negatives in the
            # HA Companion App where a WebView bug can delay selector init.
            await self.async_set_unique_id(source_entity_id)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=name,
                data={
                    CONF_SOURCE_ENTITY_ID: source_entity_id,
                    CONF_NAME: name,
                    CONF_EXTERNAL_TEMPERATURE_ENTITY_ID: ext_temp_entity_id,
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_SOURCE_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                ),
                vol.Required(CONF_EXTERNAL_TEMPERATURE_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
                vol.Required(CONF_NAME, default="Roomstat"): str,
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return RoomstatOptionsFlow()


# Optional entity settings: one left empty in the form is removed.
_OPTIONAL_ENTITY_KEYS = (
    CONF_WINDOW_SENSOR_ID,
    CONF_PRESENCE_SENSOR_ID,
    CONF_SUMMER_MODE_ENTITY_ID,
    CONF_SCHEDULE_ENTITY_ID,
    CONF_FALLBACK_ENTITY_ID,
    CONF_PREHEAT_ARRIVAL_ENTITY_ID,
    CONF_PREHEAT_CLOSE_ENTITY_ID,
    CONF_PREHEAT_NEARBY_ENTITY_ID,
    CONF_HEATING_POWER_ENTITY_ID,
)


class RoomstatOptionsFlow(config_entries.OptionsFlow):
    """Per-entry options (gear icon) for tuning, presets & sensor selection."""

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}
        # On an error the form is shown again with what was entered.
        entered: dict = {}
        if user_input is not None:
            # Flatten section data (sections return nested dicts) into
            # a single flat dict for storage in config_entry.options.
            flat: dict = {}
            for key, value in user_input.items():
                if isinstance(value, dict):
                    flat.update(value)
                else:
                    flat[key] = value
            # E.g. the valve look-ahead without the heating power it learns from.
            errors = options_form_errors(flat)
            if errors:
                entered = flat
        if user_input is not None and not errors:
            # Strip empty optional sensor values so they're stored as absent
            cleaned = {k: v for k, v in flat.items() if v not in (None, "")}
            # Preserve existing options not shown in this form (e.g. preset
            # temperatures set via Number entities).
            merged = dict(self.config_entry.options)
            # Remove optional sensor keys that were cleared by the user
            for key in _OPTIONAL_ENTITY_KEYS:
                if key not in cleaned:
                    merged.pop(key, None)
            merged.update(cleaned)
            for key in OBSOLETE_OPTIONS:
                merged.pop(key, None)

            # The TRV lives in the entry data (it is also the unique ID).
            # Changing it writes data and options in one update, so the room
            # reloads once; the options saved below are then unchanged.
            entry = self.config_entry
            new_source = merged.pop(CONF_SOURCE_ENTITY_ID, None)
            if new_source and new_source != entry.data.get(CONF_SOURCE_ENTITY_ID):
                for other in self.hass.config_entries.async_entries(DOMAIN):
                    if other.entry_id != entry.entry_id and (
                        other.unique_id == new_source
                        or other.data.get(CONF_SOURCE_ENTITY_ID) == new_source
                    ):
                        return self.async_abort(reason="trv_in_use")
                new_data, merged = change_trv(entry.data, merged, new_source)
                self.hass.config_entries.async_update_entry(
                    entry, data=new_data, options=merged, unique_id=new_source
                )

            # Reload is triggered by the update_listener in __init__.py
            # AFTER HA has persisted the new options, avoiding stale data.
            return self.async_create_entry(title="", data=merged)

        # Load current values (options > data > defaults).  After an error:
        # what was just entered, an optional entity left empty included.
        opts = options_form_values(self.config_entry.options, entered, _OPTIONAL_ENTITY_KEYS)
        data = self.config_entry.data
        defaults = RegulationConfig()
        beh_defaults = BehaviourConfig()
        band_defaults = OutOfBandConfig()

        current_ext_temp = opts.get(
            CONF_EXTERNAL_TEMPERATURE_ENTITY_ID,
            data.get(CONF_EXTERNAL_TEMPERATURE_ENTITY_ID),
        )

        # --- Build window sensor section schema ---
        window_schema_dict: dict = {
            vol.Optional(CONF_WINDOW_SENSOR_ID): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor")
            ),
            vol.Required(
                CONF_WINDOW_DELAY_S,
                default=opts.get(CONF_WINDOW_DELAY_S, 30),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=3600, step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Required(
                CONF_WINDOW_CLOSE_DELAY_S,
                default=opts.get(CONF_WINDOW_CLOSE_DELAY_S, 120),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=600, step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
        }
        window_section_schema = vol.Schema(window_schema_dict)

        # Inject suggested value for optional entity selector only if set
        window_sensor = opts.get(CONF_WINDOW_SENSOR_ID)
        if window_sensor:
            window_section_schema = self.add_suggested_values_to_schema(
                window_section_schema, {CONF_WINDOW_SENSOR_ID: window_sensor}
            )

        # --- Build presence sensor section schema ---
        presence_schema_dict: dict = {
            # Presence logic treats "off" as away and any other known state as
            # home, so only on/off entities fit.  person / device_tracker
            # ("home" / "not_home") would read "not_home" as home.
            vol.Optional(CONF_PRESENCE_SENSOR_ID): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["binary_sensor", "input_boolean"])
            ),
            vol.Required(
                CONF_PRESENCE_AWAY_DELAY_S,
                default=opts.get(CONF_PRESENCE_AWAY_DELAY_S, 600),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=7200, step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Required(
                CONF_PRESENCE_HOME_DELAY_S,
                default=opts.get(CONF_PRESENCE_HOME_DELAY_S, 30),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=600, step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            # A change by hand while nobody is home lasts until someone is
            # home, or this long at most (0 = no limit).
            vol.Required(
                CONF_PRESENCE_OVERRIDE_MAX_MIN,
                default=opts.get(
                    CONF_PRESENCE_OVERRIDE_MAX_MIN, DEFAULT_PRESENCE_OVERRIDE_MAX_MIN
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=1440, step=5,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
        }
        presence_section_schema = vol.Schema(presence_schema_dict)

        presence_sensor = opts.get(CONF_PRESENCE_SENSOR_ID)
        if presence_sensor:
            presence_section_schema = self.add_suggested_values_to_schema(
                presence_section_schema, {CONF_PRESENCE_SENSOR_ID: presence_sensor}
            )

        # --- Build schedule section schema ---
        schedule_section_schema = vol.Schema(
            {
                # A helper whose state is a preset name (comfort / night /
                # away / frost_protection), set by a scheduler.
                vol.Optional(CONF_SCHEDULE_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["input_select", "select"])
                ),
                vol.Required(
                    CONF_SCHEDULE_OVERRIDE_MIN,
                    default=opts.get(CONF_SCHEDULE_OVERRIDE_MIN, 0),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=1440, step=5,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
            }
        )
        schedule_entity = opts.get(CONF_SCHEDULE_ENTITY_ID)
        if schedule_entity:
            schedule_section_schema = self.add_suggested_values_to_schema(
                schedule_section_schema, {CONF_SCHEDULE_ENTITY_ID: schedule_entity}
            )

        # --- Build preheat sections ---
        # Preheat on the way home (preheat.py): the mode and the three Home
        # ETA entities it reads, then every number of its rule.
        preheat_mode = opts.get(CONF_PREHEAT_MODE, MODE_OFF)
        preheat_section_schema = vol.Schema(
            {
                vol.Required(
                    CONF_PREHEAT_MODE,
                    default=preheat_mode if preheat_mode in MODES else MODE_OFF,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(MODES),
                        translation_key=CONF_PREHEAT_MODE,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_PREHEAT_ARRIVAL_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="timestamp")
                ),
                vol.Optional(CONF_PREHEAT_CLOSE_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor")
                ),
                vol.Optional(CONF_PREHEAT_NEARBY_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor")
                ),
            }
        )
        preheat_suggested = {
            key: opts[key]
            for key in (
                CONF_PREHEAT_ARRIVAL_ENTITY_ID,
                CONF_PREHEAT_CLOSE_ENTITY_ID,
                CONF_PREHEAT_NEARBY_ENTITY_ID,
            )
            if opts.get(key)
        }
        if preheat_suggested:
            preheat_section_schema = self.add_suggested_values_to_schema(
                preheat_section_schema, preheat_suggested
            )
        preheat_timing_schema = vol.Schema(
            {
                vol.Required(
                    spec.key, default=opts.get(spec.key, spec.default)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=spec.minimum, max=spec.maximum, step=spec.step,
                        mode=selector.NumberSelectorMode.BOX,
                        **({"unit_of_measurement": spec.unit} if spec.unit else {}),
                    )
                )
                for spec in TIMING_OPTIONS
            }
        )

        # --- Build TRV communication section schema ---
        source_entity = opts.get(CONF_SOURCE_ENTITY_ID) or data.get(CONF_SOURCE_ENTITY_ID)
        trv_section_schema = vol.Schema(
            {
                # The TRV the room reads and commands, e.g. a Tado valve via
                # HomeKit, or the room's Tado cloud thermostat to stop using
                # HomeKit.  Choosing the fallback here removes the fallback.
                vol.Required(
                    CONF_SOURCE_ENTITY_ID,
                    default=source_entity,
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                ),
                vol.Required(
                    CONF_MIN_COMMAND_INTERVAL_S,
                    default=opts.get(CONF_MIN_COMMAND_INTERVAL_S, defaults.min_command_interval_s),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=60, max=600, step=30,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_MIN_CHANGE_THRESHOLD_C,
                    default=opts.get(CONF_MIN_CHANGE_THRESHOLD_C, defaults.min_change_threshold_c),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1, max=1.0, step=0.1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="°C",
                    )
                ),
                vol.Required(
                    CONF_OVERLAY_REFRESH_S,
                    default=opts.get(CONF_OVERLAY_REFRESH_S, 0),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=3600, step=60,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                # Fail cold: full-power commands to a Tado cloud thermostat
                # as timer overlays of this length (0 = off).  Only with the
                # zone's Tado app schedule at 5 °C or Off.
                vol.Required(
                    CONF_HEAT_TIMER_S,
                    default=opts.get(CONF_HEAT_TIMER_S, defaults.heat_timer_s),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=14400, step=300,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                # Another climate entity for the same room (e.g. the Tado
                # cloud thermostat), used only while the TRV is unavailable.
                vol.Optional(CONF_FALLBACK_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="climate",
                        exclude_entities=[source_entity] if source_entity else [],
                    )
                ),
                # Swap the roles: the fallback first, the TRV above only while
                # the fallback is unavailable.
                vol.Required(
                    CONF_PREFER_FALLBACK,
                    default=bool(opts.get(CONF_PREFER_FALLBACK, False)),
                ): selector.BooleanSelector(),
            }
        )
        fallback_entity = opts.get(CONF_FALLBACK_ENTITY_ID)
        if fallback_entity:
            trv_section_schema = self.add_suggested_values_to_schema(
                trv_section_schema, {CONF_FALLBACK_ENTITY_ID: fallback_entity}
            )

        # --- Build the valve look-ahead section schema ---
        lookahead_section_schema = vol.Schema(
            {
                vol.Required(
                    CONF_VALVE_LOOKAHEAD,
                    default=bool(opts.get(CONF_VALVE_LOOKAHEAD, False)),
                ): selector.BooleanSelector(),
                # The zone's heating power (Tado's heating %), e.g.
                # sensor.<zone>_heating: what the look-ahead learns from.
                vol.Optional(CONF_HEATING_POWER_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
            }
        )
        heating_power_entity = opts.get(CONF_HEATING_POWER_ENTITY_ID)
        if heating_power_entity:
            lookahead_section_schema = self.add_suggested_values_to_schema(
                lookahead_section_schema, {CONF_HEATING_POWER_ENTITY_ID: heating_power_entity}
            )

        # --- Main options schema with sections ---
        options_schema = vol.Schema(
            {
                # Top-level: external sensor + boost duration
                vol.Required(
                    CONF_EXTERNAL_TEMPERATURE_ENTITY_ID,
                    default=current_ext_temp,
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
                vol.Required(
                    CONF_BOOST_DURATION,
                    default=opts.get(CONF_BOOST_DURATION, defaults.presets.boost_duration_min),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=120, step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                # Summer mode: an on/off helper shared by all thermostats.
                # While on, the thermostat is locked at 5 °C.
                vol.Optional(CONF_SUMMER_MODE_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["input_boolean", "binary_sensor"]
                    )
                ),

                # Section: Window sensor
                vol.Required("window_sensor"): section(
                    window_section_schema,
                    {"collapsed": True},
                ),

                # Section: Presence sensor
                vol.Required("presence_sensor"): section(
                    presence_section_schema,
                    {"collapsed": True},
                ),

                # Section: Schedule
                vol.Required("schedule"): section(
                    schedule_section_schema,
                    {"collapsed": True},
                ),

                # Section: Preheat on the way home
                vol.Required("preheat"): section(
                    preheat_section_schema,
                    {"collapsed": True},
                ),

                # Section: Preheat timing (every number of its rule)
                vol.Required("preheat_timing"): section(
                    preheat_timing_schema,
                    {"collapsed": True},
                ),

                # Section: Controller (key kept from the PI-only days)
                vol.Required("pi_controller"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_CORRECTION_KP,
                                default=opts.get(CONF_CORRECTION_KP, defaults.tuning.kp),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=0.5, max=10.0, step=0.1,
                                    mode=selector.NumberSelectorMode.BOX,
                                )
                            ),
                            vol.Required(
                                CONF_CORRECTION_KI,
                                default=opts.get(CONF_CORRECTION_KI, defaults.tuning.ki),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=0.0, max=0.1, step=0.001,
                                    mode=selector.NumberSelectorMode.BOX,
                                )
                            ),
                            vol.Required(
                                CONF_INTEGRAL_DEADBAND_C,
                                default=opts.get(CONF_INTEGRAL_DEADBAND_C, defaults.integral_deadband_c),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=0.1, max=1.0, step=0.1,
                                    mode=selector.NumberSelectorMode.BOX,
                                    unit_of_measurement="°C",
                                )
                            ),
                            vol.Required(
                                CONF_DERIVATIVE_TIME_MIN,
                                default=opts.get(
                                    CONF_DERIVATIVE_TIME_MIN, defaults.tuning.td_s / 60.0
                                ),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=0, max=45, step=1,
                                    mode=selector.NumberSelectorMode.BOX,
                                    unit_of_measurement="min",
                                )
                            ),
                        }
                    ),
                    {"collapsed": True},
                ),

                # Section: Auto-tune
                vol.Required("autotune"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_AUTOTUNE_ENABLED,
                                default=opts.get(CONF_AUTOTUNE_ENABLED, False),
                            ): selector.BooleanSelector(),
                            vol.Required(
                                CONF_AUTOTUNE_DERIVATIVE,
                                default=opts.get(CONF_AUTOTUNE_DERIVATIVE, True),
                            ): selector.BooleanSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),

                # Section: TRV Communication
                vol.Required("trv_communication"): section(
                    trv_section_schema,
                    {"collapsed": True},
                ),

                # Section: Valve look-ahead
                vol.Required("valve_lookahead"): section(
                    lookahead_section_schema,
                    {"collapsed": True},
                ),

                # Section: Out-of-band alarm
                vol.Required("out_of_band"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_BAND_C,
                                default=opts.get(CONF_BAND_C, band_defaults.band_c),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=0.2, max=3.0, step=0.1,
                                    mode=selector.NumberSelectorMode.BOX,
                                    unit_of_measurement="°C",
                                )
                            ),
                            vol.Required(
                                CONF_BAND_MIN,
                                default=opts.get(CONF_BAND_MIN, band_defaults.band_min),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=5, max=240, step=5,
                                    mode=selector.NumberSelectorMode.BOX,
                                    unit_of_measurement="min",
                                )
                            ),
                            vol.Required(
                                CONF_COLD_BAND_MIN,
                                default=opts.get(
                                    CONF_COLD_BAND_MIN, band_defaults.cold_band_min
                                ),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=15, max=720, step=15,
                                    mode=selector.NumberSelectorMode.BOX,
                                    unit_of_measurement="min",
                                )
                            ),
                        }
                    ),
                    {"collapsed": True},
                ),

                # Section: Behaviour thresholds
                vol.Required("behaviour"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_SENSOR_GRACE_S,
                                default=opts.get(CONF_SENSOR_GRACE_S, DEFAULT_SENSOR_GRACE_S),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=0, max=1800, step=30,
                                    mode=selector.NumberSelectorMode.BOX,
                                    unit_of_measurement="s",
                                )
                            ),
                            vol.Required(
                                CONF_URGENT_DECREASE_THRESHOLD_C,
                                default=opts.get(CONF_URGENT_DECREASE_THRESHOLD_C, beh_defaults.urgent_decrease_threshold_c),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=0.5, max=3.0, step=0.1,
                                    mode=selector.NumberSelectorMode.BOX,
                                    unit_of_measurement="°C",
                                )
                            ),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

        summer_entity = opts.get(CONF_SUMMER_MODE_ENTITY_ID)
        if summer_entity:
            options_schema = self.add_suggested_values_to_schema(
                options_schema, {CONF_SUMMER_MODE_ENTITY_ID: summer_entity}
            )

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
            errors=errors,
        )
