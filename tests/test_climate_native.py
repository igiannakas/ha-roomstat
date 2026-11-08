"""The thermostat serves Home Assistant's native temperature API (2.23.0).

HA 2026.11 renamed the climate entity's temperature properties: the unit,
the current temperature and the target became native_temperature_unit,
native_current_temperature and native_target_temperature (HA converts them
to the user's unit).  The old names are deprecated and stop working in
2027.11; Roomstat 2.22 logged three deprecation warnings at every start.
The harness's ClimateEntity refuses a subclass providing any old name, so a
regression fails every entity test; these check the API itself.
"""
from __future__ import annotations

import sys

import pytest

from tests.ha_harness import _climate, _make_entity, _run, _start, e2e

pytestmark = e2e

_OLD = (
    "temperature_unit", "_attr_temperature_unit",
    "current_temperature", "_attr_current_temperature",
    "target_temperature", "_attr_target_temperature",
    "target_temperature_high", "_attr_target_temperature_high",
    "target_temperature_low", "_attr_target_temperature_low",
)


def test_no_class_of_the_thermostat_provides_the_old_names():
    climate_base = sys.modules["homeassistant.components.climate"].ClimateEntity
    for klass in _climate.RoomstatClimate.__mro__:
        if klass is climate_base:
            break
        assert not set(_OLD) & set(vars(klass)), klass.__name__


def test_the_harness_refuses_the_old_names():
    climate_base = sys.modules["homeassistant.components.climate"].ClimateEntity
    with pytest.raises(TypeError, match="current_temperature"):
        type("Old", (climate_base,), {"current_temperature": property(lambda self: 20.0)})


def test_the_native_values_are_the_room_and_the_effective_setpoint():
    ent = _make_entity()
    _run(_start(ent))
    assert _climate.RoomstatClimate._attr_native_temperature_unit == "°C"
    ent.coordinator.data = {**(ent.coordinator.data or {}), "room_temp": 19.37}
    assert ent.native_current_temperature == pytest.approx(19.37)
    assert ent.native_target_temperature == pytest.approx(ent._effective_setpoint())
    assert ent.native_target_temperature is not None
