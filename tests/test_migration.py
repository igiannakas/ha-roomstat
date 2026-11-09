"""Upgrading a room: the config entry migration (__init__.py
async_migrate_entry) and the restore data an older version saved.

Minor version 3 (9 Nov 2026, the owner removed dosed top-ups): their three
options go, and so does the Top-up gain sensor every room had (it would
otherwise stay behind as an orphaned entity).  An entry of any older minor
version goes through every step after it.  A room's saved state from a
version with dosed top-ups loads as before, what dosing saved ignored.
"""
from __future__ import annotations

import ast
import importlib.util
import logging
import sys
import types

import pytest

from tests.ha_harness import (
    _ROOT,
    TRV,
    _Extra,
    _run,
    _start,
    _State,
    e2e,
    reset_timers,
)
from tests.tado_plant import POWER, TARGET, Clock, Plant, cycle, is_heat, is_off, room_entity

pytestmark = e2e

_NAME = "roomstat_e2e.integration"


def _integration():
    """The integration's __init__.py on the stubbed Home Assistant (its
    repairs module on the stubs tests/test_repairs.py installs)."""
    if _NAME in sys.modules:
        return sys.modules[_NAME]
    from tests.test_repairs import _load_repairs

    _load_repairs()
    # Not a package of its own: its relative imports are the harness's
    # roomstat_e2e modules.
    spec = importlib.util.spec_from_file_location(
        _NAME, _ROOT / "__init__.py", submodule_search_locations=None
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module
    spec.loader.exec_module(module)
    return module


class _Registry:
    """The entity registry: entity ID -> (domain, platform, unique ID)."""

    def __init__(self, entities):
        self.entities = dict(entities)

    def async_get_entity_id(self, domain, platform, unique_id):
        for entity_id, key in self.entities.items():
            if key == (domain, platform, unique_id):
                return entity_id
        return None

    def async_remove(self, entity_id):
        del self.entities[entity_id]


class _ConfigEntries:
    def __init__(self):
        self.updates: list[dict] = []

    def async_update_entry(self, entry, *, options=None, minor_version=None):
        self.updates.append({"options": options, "minor_version": minor_version})
        if options is not None:
            entry.options = options
        if minor_version is not None:
            entry.minor_version = minor_version
        return True


def _entry(minor_version, options, *, entry_id="e1", title="Living Room", version=1):
    return types.SimpleNamespace(entry_id=entry_id, title=title, version=version,
                                 minor_version=minor_version, options=dict(options))


def _hass(entities=()):
    return types.SimpleNamespace(entity_registry=_Registry(entities),
                                 config_entries=_ConfigEntries())


# The entities of two rooms (e1, e2) as 2.23 registered them.
_ROOMS = {
    "sensor.living_room_top_up_gain": ("sensor", "roomstat", "e1_topup_dose"),
    "sensor.living_room_activity": ("sensor", "roomstat", "e1_activity"),
    "sensor.living_room_valve_look_ahead": ("sensor", "roomstat", "e1_valve_lookahead"),
    "climate.living_room": ("climate", "roomstat", "e1"),
    "sensor.study_top_up_gain": ("sensor", "roomstat", "e2_topup_dose"),
    "sensor.other_topup_dose": ("sensor", "other", "e1_topup_dose"),
}
_V2 = {
    "dosed_topups": True, "dose_zone_above_c": 0.3, "dose_zone_below_c": 0.1,
    "valve_lookahead": True, "heating_power_entity_id": POWER,
    "correction_kp": 4.0, "comfort_target": 20.5,
}


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


# ---------------------------------------------------------------------------
# The config entry
# ---------------------------------------------------------------------------

class TestMigrateEntry:
    def test_a_2_23_entry_loses_dosing_and_its_sensor(self, caplog):
        mod = _integration()
        hass, entry = _hass(_ROOMS), _entry(2, _V2)
        with caplog.at_level(logging.INFO, logger=_NAME):
            assert _run(mod.async_migrate_entry(hass, entry)) is True
        assert entry.minor_version == 3
        assert entry.options == {
            "valve_lookahead": True, "heating_power_entity_id": POWER,
            "correction_kp": 4.0, "comfort_target": 20.5,
        }
        assert len(hass.config_entries.updates) == 1
        # Only this room's Top-up gain sensor goes.
        assert set(_ROOMS) - set(hass.entity_registry.entities) == {
            "sensor.living_room_top_up_gain"}
        (line,) = [r.getMessage() for r in caplog.records]
        assert line == (
            "Living Room: settings migrated: dosed top-ups are no longer part of "
            "Roomstat (settings dropped: dosed_topups, dose_zone_above_c, "
            "dose_zone_below_c; Top-up gain sensor: sensor.living_room_top_up_gain removed)"
        )

    def test_a_room_that_never_dosed(self, caplog):
        """The sensor existed for every room, dosed or not."""
        mod = _integration()
        hass = _hass(_ROOMS)
        entry = _entry(2, {"comfort_target": 21.0}, entry_id="e2", title="Study")
        with caplog.at_level(logging.INFO, logger=_NAME):
            assert _run(mod.async_migrate_entry(hass, entry)) is True
        assert (entry.minor_version, entry.options) == (3, {"comfort_target": 21.0})
        assert "sensor.study_top_up_gain" not in hass.entity_registry.entities
        assert len(hass.entity_registry.entities) == len(_ROOMS) - 1
        (line,) = [r.getMessage() for r in caplog.records]
        assert "settings dropped: none" in line
        assert "sensor.study_top_up_gain removed" in line

    def test_no_sensor_registered(self, caplog):
        mod = _integration()
        hass, entry = _hass(), _entry(2, {})
        with caplog.at_level(logging.INFO, logger=_NAME):
            assert _run(mod.async_migrate_entry(hass, entry)) is True
        assert entry.minor_version == 3
        (line,) = [r.getMessage() for r in caplog.records]
        assert line.endswith("(settings dropped: none; Top-up gain sensor: none registered)")

    def test_a_minor_version_1_entry_goes_through_both_steps(self, caplog):
        mod = _integration()
        hass = _hass(_ROOMS)
        entry = _entry(1, {
            "correction_kp": 0.6, "derivative_time_min": 0, "gain_scheduling_enabled": True,
            "stepped_heating_enabled": True, "comfort_target": 21.0,
        })
        with caplog.at_level(logging.INFO, logger=_NAME):
            assert _run(mod.async_migrate_entry(hass, entry)) is True
        assert entry.minor_version == 3
        assert entry.options == {
            "correction_kp": 4.0, "derivative_time_min": 10.0, "comfort_target": 21.0}
        assert [u["minor_version"] for u in hass.config_entries.updates] == [2, 3]
        lines = [r.getMessage() for r in caplog.records]
        assert len(lines) == 2
        assert "gain scheduling removed" in lines[0]
        assert "dosed top-ups are no longer part of Roomstat" in lines[1]

    def test_a_current_entry_is_left_alone(self, caplog):
        mod = _integration()
        hass, entry = _hass(_ROOMS), _entry(3, _V2)
        with caplog.at_level(logging.INFO, logger=_NAME):
            assert _run(mod.async_migrate_entry(hass, entry)) is True
        assert hass.config_entries.updates == []
        assert entry.options == _V2
        assert hass.entity_registry.entities == _ROOMS
        assert not caplog.records

    def test_a_newer_major_version_is_refused(self):
        mod = _integration()
        hass, entry = _hass(_ROOMS), _entry(1, _V2, version=2)
        assert _run(mod.async_migrate_entry(hass, entry)) is False
        assert hass.config_entries.updates == []

    def test_new_entries_are_created_at_the_last_step(self):
        """The config flow's minor version is the one the migration ends on
        (config_flow.py needs the real voluptuous: read, not run)."""
        tree = ast.parse((_ROOT / "config_flow.py").read_text(encoding="utf-8"))
        (flow,) = [n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "RoomstatConfigFlow"]
        (minor,) = [n.value.value for n in flow.body if isinstance(n, ast.Assign)
                    and [t.id for t in n.targets] == ["MINOR_VERSION"]]
        mod = _integration()
        entry = _entry(1, {})
        _run(mod.async_migrate_entry(_hass(), entry))
        assert minor == entry.minor_version == 3


# ---------------------------------------------------------------------------
# Setting a room up
# ---------------------------------------------------------------------------

class _Coordinator:
    """The update coordinator, without its polling."""

    def __init__(self, hass, logger, *, name, update_method, update_interval):
        self.name = name

    async def async_config_entry_first_refresh(self):
        return None


class _Setup:
    """A current (minor 3) entry being set up, platforms forwarded."""

    def __init__(self, entities):
        self.hass = _hass(entities)
        self.hass.data = {}
        self.hass.states = types.SimpleNamespace(get=lambda entity_id: None)
        self.forwarded: list = []

        async def forward(entry, platforms):
            self.forwarded.append(entry.entry_id)

        self.hass.config_entries.async_forward_entry_setups = forward
        self.entry = _entry(3, {})
        self.entry.data = {"source_entity_id": TRV}
        self.entry.async_on_unload = lambda remove: None
        self.entry.add_update_listener = lambda listener: (lambda: None)


class TestSetupEntry:
    def test_a_sensor_an_older_version_made_again_is_removed(self, monkeypatch, caplog):
        """Upgraded (the entry went to minor 3), downgraded to 2.23 (which
        took the 1.3 entry and registered its Top-up gain sensor again),
        upgraded again: no migration runs, so setting the room up removes
        it."""
        mod = _integration()
        monkeypatch.setattr(mod, "DataUpdateCoordinator", _Coordinator)
        setup = _Setup(_ROOMS)
        with caplog.at_level(logging.INFO, logger=_NAME):
            assert _run(mod.async_setup_entry(setup.hass, setup.entry)) is True
        assert setup.forwarded == ["e1"]
        assert set(_ROOMS) - set(setup.hass.entity_registry.entities) == {
            "sensor.living_room_top_up_gain"}
        (line,) = [r.getMessage() for r in caplog.records]
        assert line == (
            "Living Room: removed sensor.living_room_top_up_gain, the Top-up gain "
            "sensor an older Roomstat made (dosed top-ups are no longer part of Roomstat)"
        )

    def test_without_one_it_is_quiet(self, monkeypatch, caplog):
        mod = _integration()
        monkeypatch.setattr(mod, "DataUpdateCoordinator", _Coordinator)
        rooms = {k: v for k, v in _ROOMS.items() if v[2] != "e1_topup_dose"}
        setup = _Setup(rooms)
        with caplog.at_level(logging.INFO, logger=_NAME):
            assert _run(mod.async_setup_entry(setup.hass, setup.entry)) is True
        assert setup.forwarded == ["e1"]
        assert setup.hass.entity_registry.entities == rooms
        assert not caplog.records


# ---------------------------------------------------------------------------
# The restore data
# ---------------------------------------------------------------------------

def _dosing_save(now: float) -> dict:
    """What 2.23's dosed top-ups saved under ``dosing``: a pulse running."""
    return {
        "trv": TRV,
        "doser": {"version": 2, "open": True, "learner": {"gain": 0.05, "refit_due": True}},
        "settled": True,
        "pulse_start_ts": now - 120.0,
        "last_pulse_start_ts": now - 2400.0,
        "last_pulse_end_ts": now - 2100.0,
        "pulse_setpoint_c": TARGET,
        "pulse_cap_s": 600.0,
        "last_pulse_setpoint_c": TARGET,
    }


def test_a_2_23_save_loads_and_its_dosing_state_is_ignored(monkeypatch):
    """A room saved by 2.23 with a pulse running (the valve at 25 °C, the
    room on its target): it starts with what else it saved, the valve's
    25 °C resumes as a heat-up that the first cycle ends, and nothing of
    dosing is saved again."""
    clock = Clock(monkeypatch)
    old = room_entity({"heating_power_entity_id": POWER})
    plant = Plant(old, clock, room=TARGET + 0.02)
    plant.feed()
    _run(_start(old))
    for _ in range(3):
        clock.tick(60.0)
        plant.advance(60.0)
        plant.feed()
        cycle(old)
    extra = old.extra_restore_state_data.as_dict()
    assert "dosing" not in extra
    extra["dosing"] = _dosing_save(clock.now)
    lines = [e.text for e in old._activity.entries()]

    new = room_entity({"heating_power_entity_id": POWER})
    plant.ent, plant.seen = new, 0
    plant.heat_cmd = True                        # the pulse's 25 °C on the valve
    plant.feed()
    state = _State(old.hvac_mode.value, {"preset_mode": old.preset_mode,
                                         "temperature": old.native_target_temperature})
    _run(_start(new, last_state=state, extra=_Extra(extra)))
    assert [e.text for e in new._activity.entries()][-len(lines):] == lines
    assert new._reg_state.mode == "heat"
    assert "dosing" not in new.extra_restore_state_data.as_dict()

    clock.tick(60.0)
    plant.advance(60.0)
    plant.feed()
    cycle(new)
    assert new._last_result.mode in ("coast", "stop")
    calls = new.hass.service_calls
    assert len(calls) == 1 and is_off(calls[0]) and not any(is_heat(c) for c in calls)
