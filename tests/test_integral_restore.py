"""The hold's integral across a restart or reload (2.25.3).

Until 2.25.3 the integral started from 0 after every restart and reload, and
a preset temperature change reloads the room.  13 Nov 2026, living room at
Kp 1.5: seven restarts and reloads, and after two of them the room sat
0.2-0.4 under for 20-80 min while the integral climbed back from 0.  Now it
is saved with the room and taken back when the save is at most an hour old
and from the same TRV.
"""
from __future__ import annotations

import math
import sys

import pytest

from tests.ha_harness import TRV, _Extra, _restart_from, _run, _start, e2e, reset_timers
from tests.tado_plant import Clock, TadoPlant, room_entity, run_room

pytestmark = e2e


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


def _ctrl():
    return sys.modules["roomstat_e2e.climate_controllers"]


class TestRestoreIntegral:
    """The HA-free rule (climate_controllers.restore_integral)."""

    NOW = 1_800_000_000.0

    def take(self, data, *, now=NOW, trv=TRV):
        return _ctrl().restore_integral(data, now, trv, -2.0, 2.0)

    def test_a_fresh_save_is_taken_back(self):
        assert self.take({"c": 0.28, "ts": self.NOW - 30.0, "trv": TRV}) == 0.28

    def test_up_to_an_hour_old(self):
        keep = _ctrl().INTEGRAL_KEEP_S
        assert keep == 3600.0
        assert self.take({"c": -0.1, "ts": self.NOW - keep, "trv": TRV}) == -0.1
        assert self.take({"c": -0.1, "ts": self.NOW - keep - 1.0, "trv": TRV}) is None

    def test_a_save_from_the_future_is_not(self):
        """The clock went back: how old it is is unknown."""
        assert self.take({"c": 0.3, "ts": self.NOW + 5.0, "trv": TRV}) is None

    def test_another_trv_starts_from_0(self):
        assert self.take({"c": 0.3, "ts": self.NOW, "trv": "climate.another"}) is None

    def test_kept_inside_the_integral_s_limits(self):
        assert self.take({"c": 5.0, "ts": self.NOW, "trv": TRV}) == 2.0
        assert self.take({"c": -5.0, "ts": self.NOW, "trv": TRV}) == -2.0

    @pytest.mark.parametrize("data", [
        None, [], "x", {}, {"c": "0.3", "ts": NOW, "trv": TRV},
        {"c": True, "ts": NOW, "trv": TRV}, {"c": math.nan, "ts": NOW, "trv": TRV},
        {"c": 0.3, "ts": None, "trv": TRV}, {"c": 0.3, "ts": math.inf, "trv": TRV},
    ])
    def test_nonsense_is_not_taken_back(self, data):
        assert self.take(data) is None

    def test_the_record(self):
        assert _ctrl().integral_record(0.123456, 10.0, TRV) == {
            "c": 0.1235, "ts": 10.0, "trv": TRV,
        }


def _holding_room(monkeypatch):
    """A room held just under its target long enough to build an integral."""
    clock = Clock(monkeypatch)
    ent = room_entity()
    plant = TadoPlant(ent, clock, room=20.38, fall_c_per_h=0.6)
    plant.feed()
    _run(_start(ent))
    run_room(ent, clock, plant, 90)
    assert abs(ent._reg_state.integral_c) > 0.02, "no integral built"
    return clock, ent, plant


def _texts(ent) -> list[str]:
    return [e.text for e in reversed(ent._activity.entries())]


class TestThroughTheEntity:

    def test_saved_with_the_room(self, monkeypatch):
        clock, ent, _ = _holding_room(monkeypatch)
        saved = ent.extra_restore_state_data.as_dict()["integral"]
        assert saved["c"] == pytest.approx(ent._reg_state.integral_c, abs=1e-4)
        assert saved["ts"] == pytest.approx(clock.now)
        assert saved["trv"] == TRV

    def test_a_reload_carries_it_on(self, monkeypatch):
        clock, old, plant = _holding_room(monkeypatch)
        integral = old._reg_state.integral_c
        clock.tick(20.0)
        new = room_entity()
        _restart_from(old, new)
        assert new._reg_state.integral_c == pytest.approx(integral, abs=1e-4)
        started = [t for t in _texts(new) if t.startswith("Started")][-1]
        assert f"integral {integral:+.2f} carried on" in started

    def test_the_first_hold_after_it_uses_it(self, monkeypatch):
        """The decision line after a reload no longer reads I +0.00."""
        clock, old, plant = _holding_room(monkeypatch)
        integral = old._reg_state.integral_c
        clock.tick(20.0)
        new = room_entity()
        _restart_from(old, new)
        plant.ent = new
        run_room(new, clock, plant, 1)
        assert new._last_result.i_correction_c == pytest.approx(integral, abs=0.02)
        assert f"I {integral:+.2f}" in new.extra_state_attributes["activity"]

    def test_after_more_than_an_hour_it_starts_from_0(self, monkeypatch):
        clock, old, _ = _holding_room(monkeypatch)
        saved = old.extra_restore_state_data.as_dict()
        clock.tick(3601.0)
        new = room_entity()
        _run(_start(new, extra=_Extra(saved)))
        assert new._reg_state.integral_c == 0.0
        assert not [t for t in _texts(new) if "carried on" in t]

    def test_another_trv_starts_from_0(self, monkeypatch):
        clock, old, _ = _holding_room(monkeypatch)
        saved = old.extra_restore_state_data.as_dict()
        new = room_entity()
        new._config_entry.data = {"source_entity_id": "climate.another_valve"}
        _run(_start(new, extra=_Extra(saved)))
        assert new._reg_state.integral_c == 0.0

    def test_a_save_from_before_starts_from_0(self, monkeypatch):
        """A 2.25.2 save has no integral."""
        clock, old, _ = _holding_room(monkeypatch)
        saved = old.extra_restore_state_data.as_dict()
        del saved["integral"]
        new = room_entity()
        _run(_start(new, extra=_Extra(saved)))
        assert new._reg_state.integral_c == 0.0
