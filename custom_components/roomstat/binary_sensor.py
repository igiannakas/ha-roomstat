"""Binary sensors for Roomstat: room sensor degraded, and room out of band."""
from __future__ import annotations

import datetime
import time

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roomstat binary sensor entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entity = RoomstatSensorDegradedBinarySensor(coordinator, entry)
    coordinator.binary_sensor_entity = entity
    band = RoomstatOutOfBandBinarySensor(coordinator, entry)
    coordinator.out_of_band_entity = band
    async_add_entities([entity, band])


class RoomstatSensorDegradedBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating external temperature sensor degradation.

    Turns on when the external room temperature sensor becomes unavailable
    and the proxy is operating on stale or missing data.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "sensor_degraded"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor degraded binary sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensor_degraded"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info so this entity appears on the same device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
        )

    @property
    def is_on(self) -> bool | None:
        """Return True when the external sensor is degraded."""
        climate = getattr(self.coordinator, "climate_entity", None)
        if climate is None:
            return None
        return climate._sensor_degraded

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return diagnostic attributes for the sensor degradation state."""
        climate = getattr(self.coordinator, "climate_entity", None)
        if climate is None:
            return {}

        attrs: dict[str, object] = {
            "grace_period_s": climate._sensor_grace_s,
        }

        if climate._last_valid_room_temp is not None:
            attrs["last_valid_reading"] = climate._last_valid_room_temp
            if climate._last_valid_room_temp_ts > 0:
                attrs["last_valid_age_s"] = int(
                    time.time() - climate._last_valid_room_temp_ts
                )

        return attrs


class RoomstatOutOfBandBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """The room is stuck too hot or too cold (``binary_sensor.<room>_out_of_band``).

    Judged by the climate entity's OutOfBandMonitor (climate_controllers.py)
    once per regulation cycle, outside the control loop's own decisions:
    hot is measured from max(setpoint, the lowest reading since the setpoint
    was last lowered), so a room cooling after a setback never counts; cold
    is counted again from every setpoint rise, so a heat-up never counts.
    Only while heating is on, with no summer lock, no window open and a
    fresh room reading.  Meant for a notification (see the README).
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "out_of_band"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the out-of-band binary sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_out_of_band"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info so this entity appears on the same device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
        )

    @property
    def is_on(self) -> bool | None:
        """Return True while the room is out of band."""
        climate = getattr(self.coordinator, "climate_entity", None)
        if climate is None:
            return None
        return climate._band_monitor.state.on

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """How far off the room is, since when, and why."""
        climate = getattr(self.coordinator, "climate_entity", None)
        if climate is None:
            return {}
        state = climate._band_monitor.state
        return {
            "deviation_c": state.deviation_c,
            "since": (
                datetime.datetime.fromtimestamp(state.since, tz=datetime.UTC).isoformat()
                if state.since is not None
                else None
            ),
            "reason": state.reason,
            "reference_c": state.reference_c,
        }
