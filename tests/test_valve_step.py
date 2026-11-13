"""Commands on the valve's own setpoint step (2.25.2), through the real
climate entity.

A Tado cloud zone takes half degrees (``target_temp_step`` 0.5) and holds
17.7 as 17.5.  13 Nov 2026, living room, minimum change 0.1: every command
off the half degree read as not held and was sent again (20 resends, 121
commands in a day against 43-75 before).  Roomstat now sends a command as
the valve will hold it, and takes a setpoint within half a step of a
command as held.
"""
from __future__ import annotations

import math

import pytest

from tests.ha_harness import _run, _start, e2e, reset_timers
from tests.tado_plant import Clock, TadoPlant, is_off, room_entity, run_room

pytestmark = e2e

STEP = 0.5
RESENT = "it will be sent again"


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


class HalfDegreeZone(TadoPlant):
    """A Tado cloud zone: every setpoint held on the half degree.  It says
    so (``target_temp_step``) unless ``says_step`` is False: Roomstat before
    2.25.2 read no step, which is the same as a zone that does not say."""

    def __init__(self, ent, clock, *, says_step: bool = True, **kw):
        super().__init__(ent, clock, **kw)
        self.says_step = says_step

    def _commands(self) -> None:
        calls = self.ent.hass.service_calls
        for dom, svc, data in calls[self.seen:]:
            if dom != "climate":
                continue
            if svc == "set_temperature":
                self.setpoint = math.floor(float(data["temperature"]) / STEP + 0.5) * STEP
                self.timeline.append((self.t + self.LATENCY_S, self.setpoint))
            elif is_off((dom, svc, data)):
                self.setpoint = None
                self.timeline.append((self.t + self.CLOSE_S, None))
        self.seen = len(calls)
        self.heat_cmd = self.setpoint is not None and self.setpoint >= 24.9

    def _trv(self) -> dict:
        return {**super()._trv(), "tado_step": STEP if self.says_step else None}


def _six_hours(monkeypatch, *, says_step: bool, min_change: float = 0.1):
    """A room around its target for six hours, a hold throttling the valve
    (the regulation trace's scenario), with a 1 °C step at 3 h."""
    clock = Clock(monkeypatch)
    ent = room_entity({"min_change_threshold_c": min_change})
    zone = HalfDegreeZone(ent, clock, room=20.62, fall_c_per_h=0.6, says_step=says_step)
    zone.feed()
    _run(_start(ent))
    run_room(ent, clock, zone, 180)
    ent._target_temp += 1.0
    run_room(ent, clock, zone, 180)
    return ent


def _setpoints(ent) -> list[float]:
    return [float(d["temperature"]) for dom, svc, d in ent.hass.service_calls
            if dom == "climate" and svc == "set_temperature"]


def _texts(ent) -> list[str]:
    return [e.text for e in reversed(ent._activity.entries())]


class TestCommandsOnTheValvesStep:

    def test_the_old_way_resends_what_the_valve_rounded(self, monkeypatch):
        """The bug, reproduced: no step known, minimum change 0.1."""
        ent = _six_hours(monkeypatch, says_step=False)
        assert any(sp % STEP for sp in _setpoints(ent))
        assert sum(RESENT in t for t in _texts(ent)) >= 3

    def test_every_command_is_on_the_half_degree(self, monkeypatch):
        ent = _six_hours(monkeypatch, says_step=True)
        setpoints = _setpoints(ent)
        assert setpoints, "the hold sent nothing"
        assert all(abs(sp / STEP - round(sp / STEP)) < 1e-9 for sp in setpoints)

    def test_nothing_is_sent_again(self, monkeypatch):
        ent = _six_hours(monkeypatch, says_step=True)
        assert not [t for t in _texts(ent) if RESENT in t]

    def test_fewer_commands_than_the_old_way(self, monkeypatch):
        old = _six_hours(monkeypatch, says_step=False)
        reset_timers()
        new = _six_hours(monkeypatch, says_step=True)
        assert len(new.hass.service_calls) < len(old.hass.service_calls)

    def test_a_minimum_change_of_0_1_costs_no_more_than_0_3(self, monkeypatch):
        """On a half-degree valve the minimum change no longer matters: a
        command that rounds to what the valve holds is not sent."""
        fine = _six_hours(monkeypatch, says_step=True, min_change=0.1)
        reset_timers()
        coarse = _six_hours(monkeypatch, says_step=True, min_change=0.3)
        assert len(fine.hass.service_calls) <= len(coarse.hass.service_calls)

    def test_the_activity_line_names_what_the_valve_gets(self, monkeypatch):
        """A command that rounds to the step the valve holds is no command
        waiting: no "wanted: under the 0.1 step" note."""
        ent = _six_hours(monkeypatch, says_step=True)
        assert not [t for t in _texts(ent) if "wanted: under the" in t]
        told = [t.rsplit("| Tado ", 1)[1].split()[0] for t in _texts(ent) if "| Tado " in t]
        numbers = [float(x) for x in told if x[0].isdigit()]
        assert numbers and all(abs(x / STEP - round(x / STEP)) < 1e-9 for x in numbers)


class TestACommandFromBeforeTheChange:

    @staticmethod
    def _held_as_17_5(monkeypatch, *, says_step: bool):
        """A command of 17.7 sent before 2.25.2 (restored with the room),
        held by the zone as 17.5, past the drift grace."""
        clock = Clock(monkeypatch)
        ent = room_entity({"min_change_threshold_c": 0.1})
        zone = HalfDegreeZone(ent, clock, room=20.62, fall_c_per_h=0.6, says_step=says_step)
        zone.feed()
        _run(_start(ent))
        zone.setpoint = 17.5
        zone.timeline.append((zone.t, 17.5))
        ent._last_sent_setpoint = ent._last_attempt_c = 17.7
        ent._last_attempt_ok = True
        ent._last_command_sent_ts = clock.now - 600.0
        run_room(ent, clock, zone, 1)
        return ent

    def test_is_held_once_the_zone_says_its_step(self, monkeypatch):
        ent = self._held_as_17_5(monkeypatch, says_step=True)
        assert ent._active_trv().step_c == STEP
        assert not [t for t in _texts(ent) if RESENT in t]

    def test_was_sent_again_before(self, monkeypatch):
        ent = self._held_as_17_5(monkeypatch, says_step=False)
        assert [t for t in _texts(ent) if RESENT in t]
