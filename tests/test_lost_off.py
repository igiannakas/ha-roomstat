"""A lost off through the real climate entity (2.15.0, every room with a
heating power sensor).

A zone still reporting heating (20 % or more) in a report made 8 min or more
after an off the valve took, with no heat command since, gets the off again
(at most twice per heat command; 3 min with the valve's own report time,
see tests/test_tado_report_time.py).  25 Oct 2026: two valves heated at
100 % for 13-15 min after an off their zones showed at once.  A valve that
closed, and a report from before the off reaching Home Assistant late, are
no lost off.
"""
from __future__ import annotations

import logging

import pytest

from tests.ha_harness import _run, _start, e2e, reset_timers
from tests.tado_plant import POWER, Clock, Plant, cycle, is_heat, is_off, room_entity, run_room

pytestmark = e2e

STOCK = {"heating_power_entity_id": POWER}


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


def _heating_room(monkeypatch, **plant_kw):
    """A room 1.1 °C under its target: a heat-up at full power, then its
    off once the coast is predicted to land it (about 36 min in)."""
    clock = Clock(monkeypatch)
    ent = room_entity(STOCK)
    plant = Plant(ent, clock, room=19.4, **plant_kw)
    plant.feed()
    _run(_start(ent))
    return ent, clock, plant


def _offs(ent) -> int:
    return sum(1 for c in ent.hass.service_calls if is_off(c))


def _heats(ent) -> int:
    return sum(1 for c in ent.hass.service_calls if is_heat(c))


def _until_the_heat_ups_off(ent, clock, plant, limit=90) -> None:
    for _ in range(limit):
        run_room(ent, clock, plant, 1)
        if _offs(ent):
            assert _heats(ent) == 1
            return
    raise AssertionError("the heat-up never ended")


def test_a_valve_still_heating_after_an_off_gets_it_again(monkeypatch, caplog):
    ent, clock, plant = _heating_room(monkeypatch)
    _until_the_heat_ups_off(ent, clock, plant)
    offs = _offs(ent)
    off_ts = ent._last_command_sent_ts
    # The zone reports the valve still at 60 % for nine minutes after the
    # off (the coast and the stop after it ask for no heat meanwhile).
    with caplog.at_level(logging.WARNING):
        for _ in range(9):
            clock.tick(60.0)
            plant.advance(60.0)
            plant.feed()
            ent.coordinator.data["heating_power"] = 60.0
            ent.coordinator.data["heating_power_reported_ts"] = clock.now
            cycle(ent)
    assert _heats(ent) == 1
    resent = _offs(ent) - offs
    assert 1 <= resent <= 2, resent
    assert any("still reports heating" in r.getMessage() for r in caplog.records)
    assert ent._last_command_sent_ts > off_ts


def test_no_resend_when_the_valve_reports_shut(monkeypatch, caplog):
    """The plant's valve reports 0 % once it has shut: one off per heat."""
    ent, clock, plant = _heating_room(monkeypatch)
    with caplog.at_level(logging.WARNING):
        _until_the_heat_ups_off(ent, clock, plant)
        run_room(ent, clock, plant, 20)
    assert _offs(ent) == _heats(ent) == 1
    assert not [r for r in caplog.records if "still reports heating" in r.getMessage()]


def test_late_heating_reports_send_no_second_off(monkeypatch, caplog):
    """The zone's heating % reaching HA 200 s late (the cloud: 10-276 s):
    the valve's last report from before an off is no lost off (2.15.0
    review: at 3 min every off after a heat-up went twice, with a
    warning)."""
    ent, clock, plant = _heating_room(monkeypatch, report_lag_s=200.0)
    with caplog.at_level(logging.WARNING):
        run_room(ent, clock, plant, 240)
    assert _heats(ent)
    assert not [r for r in caplog.records if "still reports heating" in r.getMessage()]
    assert _offs(ent) == _heats(ent)
