"""The regulation trace sensors (sensor.py): command, error and mode, read
from the climate entity each cycle so a season of behaviour survives the
recorder's purge in the 5-minute statistics."""
from __future__ import annotations

import logging

import pytest

from tests.ha_harness import (
    TRV,
    _climate,
    _make_entity,
    _run,
    _sensor,
    _start,
    e2e,
    reset_timers,
)

pytestmark = e2e

HVACMode = _climate.HVACMode if _climate is not None else None


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


def _feed(ent, room, internal, setpoint):
    ent.coordinator.data = {
        "room_temp": room, "room_temp_ts": None,
        "tado_internal_temp": internal, "tado_setpoint": setpoint,
        "tado_min_temp": 5.0, "tado_max_temp": 25.0, "tado_off": False,
        "tado_cloud": True, "tado_scheduled": False, "tado_heating": False,
        "fallback_internal_temp": None, "fallback_setpoint": None,
    }


def _trace_sensors(ent):
    ent.coordinator.climate_entity = ent
    by_key = {}
    for d in _sensor.TUNING_SENSORS:
        if d.key.startswith("regulation_"):
            by_key[d.key] = _sensor.RoomstatTuningSensor(ent.coordinator, ent._config_entry, d)
    return by_key


class TestTraceSensors:

    def test_descriptions(self):
        keys = {d.key: d for d in _sensor.TUNING_SENSORS}
        cmd, err, mode = (keys["regulation_command"], keys["regulation_error"],
                          keys["regulation_mode"])
        # Measurement sensors keep long-term statistics; the mode is an enum.
        assert cmd.state_class == "measurement" and err.state_class == "measurement"
        assert cmd.native_unit_of_measurement == "°C"
        assert err.device_class is None                 # a difference, not a temperature
        assert mode.device_class == "enum"
        assert set(mode.options) == {"heat", "coast", "hold", "stop", "dose"}
        assert all(d.entity_category == "diagnostic" for d in (cmd, err, mode))
        assert all(d.key.startswith("regulation_") and not d.needs_autotune
                   for d in (cmd, err, mode))

    def test_unknown_before_the_first_cycle(self):
        ent = _make_entity()
        _run(_start(ent))
        sensors = _trace_sensors(ent)
        assert all(s.native_value is None for s in sensors.values())
        assert all(s.available for s in sensors.values())

    def test_follow_the_regulation(self):
        ent = _make_entity()
        _run(_start(ent))
        sensors = _trace_sensors(ent)
        _feed(ent, 18.0, 9.0, 8.0)                        # 2.5 °C short: heat-up
        _run(ent._async_regulation_cycle("timer"))
        assert sensors["regulation_mode"].native_value == "heat"
        assert sensors["regulation_command"].native_value == 25.0
        assert sensors["regulation_error"].native_value == pytest.approx(2.5, abs=0.05)
        _feed(ent, 21.2, 15.0, 25.0)                      # above target: the heat-up ends
        _run(ent._async_regulation_cycle("timer"))
        assert sensors["regulation_mode"].native_value in ("coast", "stop")
        assert sensors["regulation_command"].native_value == 5.0
        assert sensors["regulation_error"].native_value < 0

    def test_written_with_the_climate_state(self):
        """The cycle writes the trace sensors, like the tuning sensors."""
        ent = _make_entity()
        _run(_start(ent))
        sensors = _trace_sensors(ent)
        for s in sensors.values():
            s.hass = ent.hass
        ent.coordinator.tuning_entities = list(sensors.values())
        _feed(ent, 18.0, 9.0, 8.0)
        _run(ent._async_regulation_cycle("timer"))
        assert all(getattr(s, "state_writes", 0) >= 1 for s in sensors.values())
        assert TRV  # the harness TRV id is what the heat-up was sent to


class TestTraceLog:

    def test_one_debug_line_per_cycle(self, caplog):
        """With debug logging on, every cycle logs its whole decision on one
        line: mode, setpoint, room and slope, the valve's reading and
        state, the command relative to that reading, P, I and the send
        reason."""
        ent = _make_entity()
        _run(_start(ent))
        logger_name = type(ent).__module__.rsplit(".", 1)[0] + ".climate_regulation"
        caplog.set_level(logging.DEBUG, logger=logger_name)
        _feed(ent, 18.0, 9.0, 8.0)
        _run(ent._async_regulation_cycle("timer"))
        lines = [r.getMessage() for r in caplog.records
                 if r.name == logger_name and "| sp " in r.getMessage()]
        assert len(lines) == 1
        line = lines[0]
        assert ": heat | sp 20.5 room 18.00 no slope | trv 9.0 idle | cmd 25.0 (+16.0 vs trv)" in line
        assert "P +10.00 I +0.00 | sent(" in line

    def test_quiet_without_debug(self, caplog):
        ent = _make_entity()
        _run(_start(ent))
        logger_name = type(ent).__module__.rsplit(".", 1)[0] + ".climate_regulation"
        caplog.set_level(logging.INFO, logger=logger_name)
        _feed(ent, 18.0, 9.0, 8.0)
        _run(ent._async_regulation_cycle("timer"))
        assert not any("| sp " in r.getMessage() for r in caplog.records)
