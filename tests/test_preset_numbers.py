"""The preset temperature numbers (number.py) work in tenths, as the
thermostat does (climate.py PRECISION_TENTHS).

3 Nov 2026, the owner: the Comfort box showed 20.6 with "Enter a valid
value" -- the numbers stepped in halves, so the frontend refused a value the
thermostat itself holds.
"""
from __future__ import annotations

import types

import pytest

from tests.ha_harness import _make_entity, _number, _run, _start, e2e

pytestmark = e2e


def _comfort_number():
    ent = _make_entity()
    _run(_start(ent))
    ent.coordinator.climate_entity = ent
    ent.hass.config_entries = types.SimpleNamespace(
        async_update_entry=lambda entry, options: setattr(entry, "options", options))
    desc = next(d for d in _number._PRESET_NUMBERS if d.conf_key == "comfort_target")
    number = _number.PresetTemperatureNumber(ent.coordinator, ent._config_entry, desc)
    number.hass = ent.hass
    number.async_write_ha_state = lambda: None
    return ent, number


def test_every_preset_number_steps_in_tenths():
    assert _number.PresetTemperatureNumber._attr_native_step == pytest.approx(0.1)


@pytest.mark.parametrize(("value", "stored"), [
    (20.6, 20.6),
    (20.600000000000001, 20.6),
    (20.649, 20.6),
    (21.05, 21.1),          # rounds to the step (binary 21.05 is just above)
    (40.0, 30.0),           # clamped as before
    (2.0, 5.0),
])
def test_a_value_is_stored_in_tenths(value, stored):
    ent, number = _comfort_number()
    _run(number.async_set_native_value(value))
    assert ent._config_entry.options["comfort_target"] == pytest.approx(stored)
    assert number.native_value == pytest.approx(stored)


def test_a_non_finite_value_is_still_ignored():
    ent, number = _comfort_number()
    before = dict(ent._config_entry.options)
    _run(number.async_set_native_value(float("nan")))
    assert ent._config_entry.options == before
