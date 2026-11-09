"""Six hours of regulation through the real climate entity, command by
command, against a run recorded on 2.14.1 (commit f293136).

A room on a Tado-like valve (every command held, a hold throttling it):
the stop after a landing, the release into a hold, a target step of 1 °C at
3 h (a heat-up, its coast and stop, the hold after).  Every service call
and its time must be the ones 2.14.1 sent, so any change to what the
regulation sends shows here.  2.14.1 released a stop 20 min ahead (10 by
default since 2.21.0, an option): the scenario sets 20.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ha_harness import _run, _start, e2e, reset_timers
from tests.tado_plant import POWER, TARGET, Clock, TadoPlant, room_entity, run_room

pytestmark = e2e

_RECORDED = Path(__file__).parent / "fixtures" / "regulation_trace_2_14_1_calls.json"
AS_2_14_1 = {"heating_power_entity_id": POWER, "hold_release_lead_min": 20}


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


def _jsonable(value):
    value = getattr(value, "value", value)
    return round(value, 3) if isinstance(value, float) else value


def scenario(monkeypatch, options) -> list[list]:
    """Every service call of the six hours with its time from the start
    (the scenario that recorded the fixture on 2.14.1)."""
    clock = Clock(monkeypatch)
    start = clock.now
    ent = room_entity(options)
    plant = TadoPlant(ent, clock, room=20.62, fall_c_per_h=0.6)
    plant.feed()
    _run(_start(ent))
    out: list[list] = []
    seen = [0]

    def watch():
        calls = ent.hass.service_calls
        for dom, svc, data in calls[seen[0]:]:
            out.append([round(clock.now - start, 1), dom, svc,
                        {k: _jsonable(v) for k, v in sorted(data.items())}])
        seen[0] = len(calls)

    run_room(ent, clock, plant, 180, on_cycle=watch)
    ent._target_temp = TARGET + 1.0
    run_room(ent, clock, plant, 180, on_cycle=watch)
    return out


def _recorded() -> list[list]:
    with open(_RECORDED, encoding="utf-8") as fh:
        recorded = json.load(fh)
    assert len(recorded) > 10                     # heat-up, stops, holds
    return recorded


def test_six_hours_send_the_recorded_commands(monkeypatch):
    assert scenario(monkeypatch, AS_2_14_1) == _recorded()


def test_options_an_older_version_left_change_nothing(monkeypatch):
    """An entry that still carries the options of dosed top-ups (removed on
    9 Nov 2026; the config entry migration drops them) is regulated
    exactly as one without them."""
    leftover = {**AS_2_14_1, "dosed_topups": True,
                "dose_zone_above_c": 0.3, "dose_zone_below_c": 0.2}
    assert scenario(monkeypatch, leftover) == _recorded()
