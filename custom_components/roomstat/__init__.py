"""The Roomstat integration."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .card import async_register_card
from .climate_controllers import (
    MissingEntityWatch,
    migrate_options_v1_to_v2,
    plausible_c,
    read_trv,
    trv_on_schedule,
)
from .const import (
    CLOUD_TRV_PLATFORMS,
    CONF_EXTERNAL_TEMPERATURE_ENTITY_ID,
    CONF_FALLBACK_ENTITY_ID,
    DOMAIN,
)
from .parameters import (
    ENTITY_MISSING_REPORT_S,
    ROOM_SENSOR_UNAVAILABLE_REPORT_S,
    CorrectionTuning,
)
from .repairs import async_check_entities, async_delete_entity_issues

_LOGGER = logging.getLogger(__name__)
_COORDINATOR_LOGGER = logging.getLogger(f"{__name__}.coordinator")

# List the platforms that you want to support.
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SENSOR,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the dashboard card (custom:roomstat-card)."""
    await async_register_card(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring options written by older versions to the current meaning."""
    if entry.version > 1:
        return False                 # written by a newer Roomstat
    if entry.minor_version < 2:
        defaults = CorrectionTuning()
        options = migrate_options_v1_to_v2(
            entry.options, defaults.kp, defaults.td_s / 60.0
        )
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
        _LOGGER.info(
            "%s: settings migrated to the current controller (Kp %s, braking "
            "time %s min, gain scheduling removed)",
            entry.title, options.get("correction_kp", defaults.kp),
            options.get("derivative_time_min", defaults.td_s / 60.0),
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Roomstat from a config entry."""

    # 1. Ensure DOMAIN dict exists in hass.data
    hass.data.setdefault(DOMAIN, {})

    # A deleted, renamed or disabled entity, or a room sensor that stays
    # unavailable, becomes a repair (repairs.py).
    missing_watch = MissingEntityWatch(
        ENTITY_MISSING_REPORT_S, {"unavailable": ROOM_SENSOR_UNAVAILABLE_REPORT_S}
    )

    # 2. Define the data update method
    async def async_update_data():
        """Fetch data from entities (Source & External Sensor)."""
        source_entity_id = entry.data.get("source_entity_id")

        # Priority: Options (Dynamic) > Data (Initial Config)
        external_sensor_id = entry.options.get(
            CONF_EXTERNAL_TEMPERATURE_ENTITY_ID,
            entry.data.get(CONF_EXTERNAL_TEMPERATURE_ENTITY_ID)
        )

        data = {
            "room_temp": None,
            "room_temp_ts": None,
        }

        # Get Room Temp from External Sensor.  A value outside -30..60 °C is a
        # sensor fault (e.g. 0 after a reboot), never a real room: treated as
        # missing, so the sensor grace period and the hold below apply.
        if external_sensor_id:
            state = hass.states.get(external_sensor_id)
            if state and state.state not in ("unknown", "unavailable"):
                value = plausible_c(state.state)
                if value is not None:
                    data["room_temp"] = value
                    data["room_temp_ts"] = state.last_updated.timestamp()
                else:
                    _LOGGER.debug(
                        "Ignoring room temperature %r from %s",
                        state.state, external_sensor_id,
                    )

        # TRV's own reading, setpoint, accepted range and off state: the
        # source entity, and the optional fallback for the same room (used
        # while the source is unavailable, or first when preferred, see
        # climate_controllers.choose_trv).
        for entity_id, prefix in (
            (source_entity_id, "tado"),
            (entry.options.get(CONF_FALLBACK_ENTITY_ID), "fallback"),
        ):
            state = hass.states.get(entity_id) if entity_id else None
            trv = read_trv(
                state.state if state is not None else None,
                state.attributes if state is not None else None,
            )
            data[f"{prefix}_internal_temp"] = trv["internal"]
            data[f"{prefix}_setpoint"] = trv["setpoint"]
            data[f"{prefix}_min_temp"] = trv["min"]
            data[f"{prefix}_max_temp"] = trv["max"]
            data[f"{prefix}_off"] = trv["off"]
            # What the valve says it is doing (hvac_action), for the
            # out-of-band sensor: None when the entity does not say.
            data[f"{prefix}_heating"] = trv["heating"]
            # Back on its own schedule: our overlay ran out or was ended
            # in the Tado app (the drift check sends the command again).
            data[f"{prefix}_scheduled"] = trv_on_schedule(
                state.state if state is not None else None,
                trv["internal"],
                trv["setpoint"],
            )
            # Which integration provides it: a cloud one is polled, so its
            # values can be minutes old (slower drift grace).
            reg_entry = er.async_get(hass).async_get(entity_id) if entity_id else None
            data[f"{prefix}_cloud"] = (
                reg_entry is not None and reg_entry.platform in CLOUD_TRV_PLATFORMS
            )

        async_check_entities(
            hass, entry, missing_watch, time.time(),
            int(ROOM_SENSOR_UNAVAILABLE_REPORT_S // 60),
        )
        return data

    # 3. Create the Coordinator.  It gets its own logger: at debug it
    # reports every refresh ("Finished fetching ... data"), one a minute per
    # room, which buries the regulation trace when the package is set to
    # debug.  Leave custom_components.roomstat.coordinator at info.
    coordinator = DataUpdateCoordinator(
        hass,
        _COORDINATOR_LOGGER,
        name=f"{DOMAIN}_{entry.title}",
        update_method=async_update_data,
        update_interval=timedelta(seconds=60), # Poll every 60s as backup
    )

    # 4. Attach the config entry to the coordinator
    coordinator.config_entry = entry

    # 5. Perform initial refresh
    await coordinator.async_config_entry_first_refresh()

    # 6. Store coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # 7. Load the platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 8. Reload integration when options change (e.g. from the options flow).
    # This listener fires AFTER HA has persisted the new options, so the
    # reload always sees the up-to-date values.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    # A disabled room no longer needs its entities.  (A plain reload keeps
    # its repairs: the check needs a few minutes to report them again.)
    if entry.disabled_by is not None:
        async_delete_entity_issues(hass, entry.entry_id)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the repairs of a deleted room."""
    async_delete_entity_issues(hass, entry.entry_id)
