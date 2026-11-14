"""A preset temperature is applied to the running room (2.26.0).

Until 2.26.0 every preset temperature change reloaded the room: the owner's
living room restarted or reloaded 58 times in nine days (5-14 Nov 2026), 30
of them preset temperatures.  A reload costs the room its full-push flag, a
cycle and a "Started" line.  Now an entry update that changes only preset
temperatures is handed to the running thermostat; anything else reloads as
before.
"""
from __future__ import annotations

import sys
import types

import pytest

from tests.ha_harness import TRV, _run, _start, e2e, reset_timers
from tests.tado_plant import Clock, TadoPlant, room_entity, run_room
from tests.test_migration import _integration

pytestmark = e2e


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


def _ctrl():
    return sys.modules["roomstat_e2e.climate_controllers"]


DATA = {"source_entity_id": TRV}


class TestTheRule:
    """climate_controllers.preset_temperature_changes, HA-free."""

    def changes(self, old, new, old_data=DATA, new_data=DATA):
        return _ctrl().preset_temperature_changes(old_data, old, new_data, new)

    def test_a_preset_temperature_alone(self):
        assert self.changes({"comfort_target": 21.0}, {"comfort_target": 21.2}) == {
            "comfort_target": (21.0, 21.2)}

    def test_one_set_for_the_first_time(self):
        assert self.changes({}, {"eco_target": 18.5}) == {"eco_target": (None, 18.5)}

    def test_several_at_once(self):
        old = {"comfort_target": 21.0, "eco_target": 18.0, "correction_kp": 2.0}
        new = {"comfort_target": 20.5, "eco_target": 18.5, "correction_kp": 2.0}
        assert self.changes(old, new) == {
            "comfort_target": (21.0, 20.5), "eco_target": (18.0, 18.5)}

    def test_every_preset_temperature_is_one(self):
        const = sys.modules["roomstat_e2e.const"]
        assert set(_ctrl().PRESET_TEMPERATURE_OPTIONS) == {
            const.CONF_COMFORT_TARGET, const.CONF_ECO_TARGET, const.CONF_BOOST_TARGET,
            const.CONF_AWAY_TARGET, const.CONF_FROST_PROTECTION_TARGET,
        }

    def test_anything_else_reloads(self):
        assert self.changes({"correction_kp": 2.0}, {"correction_kp": 1.5}) is None

    def test_a_preset_temperature_with_something_else_reloads(self):
        old = {"comfort_target": 21.0, "hold_release_below_c": 0.1}
        new = {"comfort_target": 21.2, "hold_release_below_c": 0.15}
        assert self.changes(old, new) is None

    def test_the_boost_duration_is_no_temperature(self):
        assert self.changes({"boost_duration": 30}, {"boost_duration": 45}) is None

    def test_nothing_changed_reloads(self):
        """A title or another entry field: as before."""
        assert self.changes({"comfort_target": 21.0}, {"comfort_target": 21.0}) is None

    def test_a_new_trv_reloads(self):
        assert self.changes(
            {"comfort_target": 21.0}, {"comfort_target": 21.2},
            new_data={"source_entity_id": "climate.another"},
        ) is None


def _holding_room(monkeypatch, options=None):
    clock = Clock(monkeypatch)
    ent = room_entity(options)
    plant = TadoPlant(ent, clock, room=20.38, fall_c_per_h=0.6)
    plant.feed()
    _run(_start(ent))
    run_room(ent, clock, plant, 90)
    return clock, ent, plant


def _texts(ent) -> list[str]:
    return [e.text for e in reversed(ent._activity.entries())]


def _set(ent, **options):
    """What the number entity does: write the option, then the listener's
    call."""
    old = dict(ent._config_entry.options)
    ent._config_entry.options = {**old, **options}
    changed = {k: (old.get(k), v) for k, v in options.items() if old.get(k) != v}
    _run(ent.async_preset_temperatures_changed(changed))


class TestThroughTheEntity:

    def test_the_comfort_temperature_moves_the_target_at_once(self, monkeypatch):
        clock, ent, plant = _holding_room(monkeypatch)
        before = ent._effective_setpoint()
        integral = ent._reg_state.integral_c
        assert abs(integral) > 0.02
        cycles = []
        real = ent._async_regulation_cycle

        async def spy(trigger="timer"):
            cycles.append(trigger)
            await real(trigger)

        monkeypatch.setattr(ent, "_async_regulation_cycle", spy)
        _set(ent, comfort_target=before + 0.2)
        assert ent._effective_setpoint() == pytest.approx(before + 0.2)
        assert ent.native_target_temperature == pytest.approx(before + 0.2)
        assert cycles == ["preset_temperature"]
        # The same room carried on: no start line, the integral not from 0.
        assert len([t for t in _texts(ent) if t.startswith("Started")]) == 1
        assert ent._reg_state.integral_c == pytest.approx(integral, abs=0.05)
        assert f"Comfort temperature {before:.1f} → {before + 0.2:.1f} °C" in " | ".join(
            _texts(ent))
        assert f"→ {before + 0.2:.1f}" in ent.extra_state_attributes["activity"]

    def test_the_learning_and_the_slope_stay(self, monkeypatch):
        clock, ent, plant = _holding_room(monkeypatch)
        tuner, slope = ent._autotuner, ent._slope
        _set(ent, comfort_target=ent._effective_setpoint() + 0.2)
        assert ent._autotuner is tuner
        assert ent._slope is slope

    def test_a_preset_not_in_use_runs_no_cycle(self, monkeypatch):
        clock, ent, plant = _holding_room(monkeypatch)
        cycles = []

        async def spy(trigger="timer"):
            cycles.append(trigger)

        monkeypatch.setattr(ent, "_async_regulation_cycle", spy)
        night = ent._config.presets.eco_target_c
        _set(ent, eco_target=night - 1.0)
        assert cycles == []
        assert ent._config.presets.eco_target_c == night - 1.0
        assert f"Night temperature {night:.1f} → {night - 1.0:.1f} °C" in " | ".join(
            _texts(ent))

    def test_the_learned_gains_stay_in_use(self, monkeypatch):
        """The config is rebuilt from the options: the tuner's values, not
        the configured ones, must still be the regulator's."""
        clock, ent, plant = _holding_room(monkeypatch, {"autotune_enabled": True})
        Tuning = sys.modules["roomstat_e2e.autotune"].Tuning
        learned = Tuning(3.1, 0.0007, 480.0)
        monkeypatch.setattr(ent._autotuner, "active_tuning", lambda: learned)
        _set(ent, comfort_target=ent._effective_setpoint() + 0.2)
        assert ent._regulator.cfg is ent._config
        assert ent._config.tuning.kp == 3.1
        assert ent._config.tuning.td_s == 480.0

    def test_more_heat_from_it_still_settles(self, monkeypatch):
        """The number entity notes the change by hand; the cycle that
        follows holds more heat back for the settle time."""
        clock, ent, plant = _holding_room(monkeypatch)
        sent = len(ent.hass.service_calls)
        ent.note_change_by_hand()
        _set(ent, comfort_target=ent._effective_setpoint() + 1.5)
        assert len(ent.hass.service_calls) == sent
        assert "a change by hand settling" in ent.extra_state_attributes["activity"]

    def test_a_failure_leaves_the_room_regulating(self, monkeypatch):
        clock, ent, plant = _holding_room(monkeypatch)

        async def boom(hass, entry):
            raise RuntimeError("no")

        monkeypatch.setattr(ent, "_async_config_entry_updated", boom)
        _set(ent, comfort_target=21.4)
        assert "applying the new preset temperatures failed" in " | ".join(_texts(ent))
        run_room(ent, clock, plant, 2)
        assert ent._last_result is not None


class TestTheListener:
    """__init__._async_options_updated: in place or a reload."""

    def setup(self, climate=True):
        mod = _integration()
        self.reloads, self.applied = [], []

        async def reload(entry_id):
            self.reloads.append(entry_id)

        async def changed(changes):
            self.applied.append(changes)

        hass = types.SimpleNamespace(
            data={}, config_entries=types.SimpleNamespace(async_reload=reload))
        entry = types.SimpleNamespace(
            entry_id="e1", data=dict(DATA), options={"comfort_target": 21.0})
        coordinator = types.SimpleNamespace(
            applied_entry=(dict(entry.data), dict(entry.options)),
            climate_entity=types.SimpleNamespace(
                async_preset_temperatures_changed=changed) if climate else None,
        )
        hass.data[mod.DOMAIN] = {"e1": coordinator}
        return mod, hass, entry, coordinator

    def test_a_preset_temperature_is_applied_in_place(self):
        mod, hass, entry, coordinator = self.setup()
        entry.options = {"comfort_target": 21.2}
        _run(mod._async_options_updated(hass, entry))
        assert self.reloads == []
        assert self.applied == [{"comfort_target": (21.0, 21.2)}]
        # The next change is measured from this one.
        entry.options = {"comfort_target": 21.3}
        _run(mod._async_options_updated(hass, entry))
        assert self.applied[-1] == {"comfort_target": (21.2, 21.3)}

    def test_another_option_reloads(self):
        mod, hass, entry, coordinator = self.setup()
        entry.options = {"comfort_target": 21.0, "correction_kp": 1.5}
        _run(mod._async_options_updated(hass, entry))
        assert self.reloads == ["e1"] and self.applied == []

    def test_without_a_thermostat_it_reloads(self):
        mod, hass, entry, coordinator = self.setup(climate=False)
        entry.options = {"comfort_target": 21.2}
        _run(mod._async_options_updated(hass, entry))
        assert self.reloads == ["e1"]

    def test_a_room_not_set_up_reloads(self):
        mod, hass, entry, coordinator = self.setup()
        hass.data[mod.DOMAIN] = {}
        entry.options = {"comfort_target": 21.2}
        _run(mod._async_options_updated(hass, entry))
        assert self.reloads == ["e1"]
