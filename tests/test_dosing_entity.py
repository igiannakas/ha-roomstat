"""Dosed top-ups through the real climate entity (tests/ha_harness.py).

A small plant stands in for the room and its valve: the room cools at a
steady rate; the valve opens a minute after a 25 °C command and shuts 1.5 min
after an off; its heat reaches the room after a delay and spreads over a
time constant (the first-order-plus-dead-time model of design §4, with the
default gain).  With the option on, a settled room gets full-power pulses at
least 3 min long, sized to land on the target, and no second pulse while the
first one's heat is on its way; the option off is today's regulation; a
reload keeps a running pulse; a window or a target change ends the settled
state; two rooms share a burn; a valve that kept heating after an off gets
the off again; a failure leaves the room regulated as without dosing.
"""
from __future__ import annotations

import json
import logging
import math
import sys
from pathlib import Path

import pytest

from tests.ha_harness import (
    _climate,
    _make_entity,
    _restart_from,
    _run,
    _start,
    drain_tasks,
    e2e,
    reset_timers,
)

pytestmark = e2e

POWER = "sensor.test_zone_heating"
ON = {"dosed_topups": True, "heating_power_entity_id": POWER}
TARGET = 20.5            # the harness's comfort target


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


class _Clock:
    def __init__(self, monkeypatch, start=1_800_000_000.0):
        self.now = start
        monkeypatch.setattr("time.time", lambda: self.now)

    def tick(self, s=60.0):
        self.now += s


def _is_heat(call) -> bool:
    dom, svc, data = call
    return dom == "climate" and svc == "set_temperature" and data.get("temperature", 0) >= 24.9


def _is_off(call) -> bool:
    dom, svc, data = call
    mode = data.get("hvac_mode")
    return (dom == "climate" and svc == "set_hvac_mode"
            and str(getattr(mode, "value", mode)).lower() == "off")


class _Plant:
    """The room and its valve (see the module docstring).  The valve opens
    only on a 25 °C command and shuts on an off; hold commands move nothing
    (the dosed rooms never send one while settled).  ``report_lag_s``: the
    zone's heating % reaches HA that much late (the cloud: 10-276 s)."""

    LATENCY_S, CLOSE_S = 60.0, 90.0

    def __init__(self, ent, clock, *, room=20.62, fall_c_per_h=0.5, gain=0.045,
                 theta_s=300.0, tau_s=480.0, report_lag_s=0.0):
        self.ent, self.clock = ent, clock
        self.room, self.fall = room, fall_c_per_h / 3600.0
        self.gain, self.theta, self.tau = gain, theta_s, tau_s
        self.report_lag_s = report_lag_s
        self.seen = len(ent.hass.service_calls)
        self.open_from: float | None = None
        self.open_until: float | None = None
        self.heat_cmd = False                 # the valve holds a 25 °C
        self.line: list[tuple[float, float]] = []   # (arrives, open-minutes)
        self.emitter = 0.0
        self.t = clock.now
        self.power_ts = clock.now
        self.power = 0.0
        self.power_log: list[tuple[float, float]] = [(clock.now, 0.0)]
        self.peaks: list[float] = []

    def _commands(self) -> None:
        calls = self.ent.hass.service_calls
        for call in calls[self.seen:]:
            if _is_heat(call) and not self.heat_cmd:
                self.heat_cmd = True
                self.open_from, self.open_until = self.t + self.LATENCY_S, None
            elif _is_off(call) and self.heat_cmd:
                self.heat_cmd = False
                self.open_until = self.t + self.CLOSE_S
        self.seen = len(calls)

    def _valve_open(self, t: float) -> bool:
        return (self.open_from is not None and t >= self.open_from
                and (self.open_until is None or t < self.open_until))

    def _opening(self, t: float) -> float:
        """How far the valve is open (0-1)."""
        return 1.0 if self._valve_open(t) else 0.0

    def advance(self, seconds: float = 60.0) -> None:
        """Run the plant to the clock's time in 10-s steps."""
        self._commands()
        end = self.t + seconds
        while self.t < end - 1e-6:
            dt = min(10.0, end - self.t)
            opening = self._opening(self.t)
            if opening > 0:
                self.line.append((self.t + self.theta, opening * dt / 60.0))
            while self.line and self.line[0][0] <= self.t:
                self.emitter += self.line.pop(0)[1]
            arrived = self.emitter * (1.0 - math.exp(-dt / self.tau))
            self.emitter -= arrived
            self.room += self.gain * arrived - self.fall * dt
            self.t += dt
            power = round(100.0 * self._opening(self.t))
            if power != self.power:
                self.power, self.power_ts = power, self.t
                self.power_log.append((self.t, power))

    def _reported_power(self) -> tuple[float, float]:
        """The heating % HA holds now and when it changed there."""
        if self.report_lag_s <= 0:
            return self.power, self.power_ts
        seen = self.t - self.report_lag_s
        ts, value = self.power_log[0]
        for t, v in self.power_log:
            if t > seen:
                break
            ts, value = t, v
        return value, ts + self.report_lag_s

    def _trv(self) -> dict:
        return {"tado_setpoint": 25.0 if self.heat_cmd else None,
                "tado_off": not self.heat_cmd}

    def feed(self) -> None:
        power, power_ts = self._reported_power()
        self.ent.coordinator.data = {
            "room_temp": round(self.room, 2), "room_temp_ts": self.t,
            "tado_internal_temp": round(self.room - 3.0, 1),
            "tado_min_temp": 5.0, "tado_max_temp": 25.0,
            "tado_cloud": True, "tado_scheduled": False,
            "tado_heating": power > 0,
            "fallback_internal_temp": None, "fallback_setpoint": None,
            "heating_power": power, "heating_power_ts": power_ts,
            "heating_power_reported_ts": self.t,
            **self._trv(),
        }


class _TadoPlant(_Plant):
    """A valve that holds every command, as a Tado does: 25 °C opens it
    fully, an off shuts it, each a minute after the command (an off 1.5
    min).  A lower setpoint opens it by (setpoint - its reading) / 5 (fully
    at 5 °C over its reading), or with ``hold_open`` set, as the living
    room's valve does: shut under a 0.3 °C push, that far open up to 4.5,
    fully beyond (30-40 % on average for a hold's +1..+3)."""

    def __init__(self, ent, clock, *, hold_open: float | None = None, **kw):
        super().__init__(ent, clock, **kw)
        self.hold_open = hold_open
        self.setpoint: float | None = None          # what the TRV shows
        self.timeline: list[tuple[float, float | None]] = []

    def _commands(self) -> None:
        calls = self.ent.hass.service_calls
        for dom, svc, data in calls[self.seen:]:
            if dom != "climate":
                continue
            if svc == "set_temperature":
                self.setpoint = float(data["temperature"])
                self.timeline.append((self.t + self.LATENCY_S, self.setpoint))
            elif _is_off((dom, svc, data)):
                self.setpoint = None
                self.timeline.append((self.t + self.CLOSE_S, None))
        self.seen = len(calls)
        self.heat_cmd = self.setpoint is not None and self.setpoint >= 24.9

    def _opening(self, t: float) -> float:
        held = None
        for ts, sp in self.timeline:
            if ts > t:
                break
            held = sp
        if held is None:
            return 0.0
        if held >= 24.9:
            return 1.0
        push = held - (self.room - 3.0)
        if self.hold_open is not None:
            return 0.0 if push < 0.3 else (self.hold_open if push < 4.5 else 1.0)
        return min(1.0, max(0.0, push / 5.0))

    def _trv(self) -> dict:
        return {"tado_setpoint": self.setpoint, "tado_off": self.setpoint is None}


def _cycle(ent):
    _run(ent._async_regulation_cycle("timer"))


def _pending_wake():
    """The dose-end timer the last cycle scheduled, if any."""
    for entry in reversed(sys.modules["tests.ha_harness"]._CALL_LATER):
        if (not entry["cancelled"]
                and getattr(entry["callback"], "__name__", "") == "_async_dose_wake"):
            return entry
    return None


def _run_room(ent, clock, plant, minutes, *, on_cycle=None):
    """Minute cycles with the plant, and a running pulse's dose-end timer
    fired at its own second (the plant runs to it first)."""
    end = clock.now + 60.0 * minutes
    next_minute = clock.now + 60.0
    wake_at = None
    while True:
        t_next = next_minute if wake_at is None else min(next_minute, wake_at)
        if t_next > end + 1e-6:
            break
        dt = t_next - clock.now
        clock.tick(dt)
        plant.advance(dt)
        plant.feed()
        entry = _pending_wake() if wake_at is not None and t_next == wake_at else None
        if entry is not None:
            entry["cancelled"] = True
            _run(entry["callback"](None))
        else:
            next_minute += 60.0
            _cycle(ent)
        if on_cycle is not None:
            on_cycle()
        entry = _pending_wake()
        wake_at = clock.now + float(entry["delay"]) if entry is not None else None


class _Pulses:
    """Each pulse's start and end time, from the service calls as they go
    out (an on_cycle watcher)."""

    def __init__(self, ent, clock, plant):
        self.ent, self.clock, self.plant = ent, clock, plant
        self.seen = 0
        self.starts: list[float] = []
        self.ends: list[float] = []
        self.rooms: list[tuple[float, float]] = []

    def __call__(self) -> None:
        calls = self.ent.hass.service_calls
        for call in calls[self.seen:]:
            if _is_heat(call) and len(self.starts) == len(self.ends):
                self.starts.append(self.clock.now)
            elif _is_off(call) and len(self.ends) < len(self.starts):
                self.ends.append(self.clock.now)
        self.seen = len(calls)
        self.rooms.append((self.clock.now, self.plant.room))

    @property
    def lengths(self) -> list[float]:
        return [b - a for a, b in zip(self.starts, self.ends)]


def _entity(options=None, **kw):
    ent = _make_entity(dict(options or {}), window=False, **kw)
    return ent


def _started(monkeypatch, options=ON, **kw):
    clock = _Clock(monkeypatch)
    ent = _entity(options, **kw)
    plant = _Plant(ent, clock)
    plant.feed()
    _run(_start(ent))
    return ent, clock, plant


# ---------------------------------------------------------------------------
# A settled room is dosed
# ---------------------------------------------------------------------------

class TestSettledRoom:
    def test_pulses_land_on_the_target_and_none_while_heat_is_on_its_way(self, monkeypatch):
        ent, clock, plant = _started(monkeypatch)
        watch = _Pulses(ent, clock, plant)
        _run_room(ent, clock, plant, 240, on_cycle=watch)
        assert len(watch.starts) >= 4, "a settled, cooling room is topped up"
        # Every pulse at least the minimum, and well short of an old top-up.
        assert all(180.0 - 1 <= s <= 480.0 for s in watch.lengths), watch.lengths
        # No second pulse before the first one's heat has landed (theta + 2 tau).
        gaps = [b - a for a, b in zip(watch.starts, watch.starts[1:])]
        assert min(gaps) >= 1260.0 - 1, gaps
        # After the first hour the room stays in the band around the target.
        later = [r for t, r in watch.rooms if t - watch.rooms[0][0] > 3600.0]
        assert TARGET - 0.12 <= min(later) and max(later) <= TARGET + 0.05, (
            min(later), max(later))

    def test_the_first_pulse_fires_on_the_dead_time_lead(self, monkeypatch):
        ent, clock, plant = _started(monkeypatch)
        fired = []

        def watch():
            r = ent._last_result
            if r is not None and r.fired_by and not fired:
                fired.append((plant.room, r))

        _run_room(ent, clock, plant, 60, on_cycle=watch)
        room, result = fired[0]
        assert result.fired_by == "release" and result.mode == "dose"
        # 0.10 below within the visible dead time (5 + 8/2 min) of a room
        # falling 0.5 °C/h: about 0.025 below the target, not 0.10.
        assert TARGET - room == pytest.approx(0.025, abs=0.03)

    def test_each_episode_is_learned_and_logged(self, monkeypatch):
        ent, clock, plant = _started(monkeypatch)
        _run_room(ent, clock, plant, 180)
        events = [e.text for e in ent._activity.entries() if not e.decision]
        lessons = [t for t in events if t.startswith("Dosed top-up:")]
        assert lessons, events[-10:]
        summary = ent.topup_dose_summary()
        assert summary["pulses_24h"] >= 2
        sensor_value = ent.topup_gain
        assert 0.01 < sensor_value < 0.15
        # The plant has the default model: the learned gain stays near it.
        assert sensor_value == pytest.approx(0.045, rel=0.35)

    def test_the_learned_model_stays_on_the_plant_in_closed_loop(self, monkeypatch):
        """Four hours of pulses in closed loop: each episode is judged with
        the room's loss fitted alongside its gain, so the learner neither
        drifts up (the loss taken too small, its fall booked as missing
        heat) nor down; the plant is the default model (0.045 °C per
        open-minute, delay 5 min, spread 8 min, cooling 0.5 °C/h)."""
        ent, clock, plant = _started(monkeypatch)
        learner = ent._doser.learner
        trajectory = []

        def watch():
            if learner._judged and (not trajectory or trajectory[-1][0] != learner._judged[-1].ts):
                trajectory.append((learner._judged[-1].ts, learner.gain_c_per_open_min,
                                   learner.theta_s, learner.tau_s))

        _run_room(ent, clock, plant, 240, on_cycle=watch)
        assert len(trajectory) >= 4, trajectory
        for _, gain, theta, tau in trajectory:
            assert gain == pytest.approx(0.045, rel=0.20), trajectory
            assert 240.0 <= tau <= 840.0, trajectory
            assert 120.0 <= theta <= 540.0, trajectory
        lessons = [e.text for e in ent._activity.entries()
                   if not e.decision and e.text.startswith("Dosed top-up:")]
        assert lessons and all("loss" in t for t in lessons), lessons

    def test_activity_lines_name_the_pulse_and_the_landing(self, monkeypatch):
        ent, clock, plant = _started(monkeypatch)
        _run_room(ent, clock, plant, 90)
        texts = [e.text for e in ent._activity.entries() if e.decision]
        assert any(t.startswith("Top-up pulse:") for t in texts)
        assert any(t.startswith("Valve off, landing:") or t.startswith("Top-up ended")
                   for t in texts)
        assert any(t.startswith("Valve off (dosing):") for t in texts)
        attrs = ent.extra_state_attributes
        assert "topup_inflight_c" in attrs and "topup_gain" in attrs

    def test_a_pulse_ends_at_its_second_not_the_next_minute(self, monkeypatch):
        ent, clock, plant = _started(monkeypatch)
        watch = _Pulses(ent, clock, plant)
        _run_room(ent, clock, plant, 150, on_cycle=watch)
        # A pulse whose dose is covered between two minute cycles is ended by
        # its own timer: its length is off the minute grid.
        assert any(abs(s / 60.0 - round(s / 60.0)) > 0.05 for s in watch.lengths), watch.lengths


# ---------------------------------------------------------------------------
# Off, no sensor, failure
# ---------------------------------------------------------------------------

def _day(monkeypatch, options, minutes=150):
    ent, clock, plant = _started(monkeypatch, options)
    _run_room(ent, clock, plant, minutes)
    return [(dom, svc, {k: v for k, v in data.items()}) for dom, svc, data in ent.hass.service_calls]


def _jsonable(value):
    value = getattr(value, "value", value)
    return round(value, 3) if isinstance(value, float) else value


def stock_scenario(monkeypatch, options) -> list[list]:
    """Six hours of a room on a Tado-like valve (every command held, a hold
    throttling it): the stop after a landing, the release into a hold, a
    target step of 1 °C at 3 h (a heat-up, its coast and stop, the hold
    after).  Returns every service call with its time from the start.  Also
    run on 2.14.1 to record tests/fixtures/option_off_2_14_1_calls.json."""
    clock = _Clock(monkeypatch)
    start = clock.now
    ent = _entity(options)
    plant = _TadoPlant(ent, clock, room=20.62, fall_c_per_h=0.6)
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

    _run_room(ent, clock, plant, 180, on_cycle=watch)
    ent._target_temp = TARGET + 1.0
    _run_room(ent, clock, plant, 180, on_cycle=watch)
    return out


def test_option_off_is_2_14_1s_regulation(monkeypatch):
    """With the option off (or never set) the room gets exactly the
    commands 2.14.1 sent, at the same times (recorded on 2.14.1, commit
    f293136, with this scenario)."""
    with open(Path(__file__).parent / "fixtures" / "option_off_2_14_1_calls.json") as fh:
        recorded = json.load(fh)
    assert len(recorded) > 10                     # the scenario sends a day's worth
    never_set = stock_scenario(monkeypatch, {"heating_power_entity_id": POWER})
    assert never_set == recorded
    reset_timers()
    off = stock_scenario(
        monkeypatch, {"heating_power_entity_id": POWER, "dosed_topups": False}
    )
    assert off == recorded
    reset_timers()
    dosed = stock_scenario(monkeypatch, ON)
    assert dosed != recorded


def test_option_off_is_todays_regulation(monkeypatch):
    stock = _day(monkeypatch, {"heating_power_entity_id": POWER})
    reset_timers()
    off = _day(monkeypatch, {"heating_power_entity_id": POWER, "dosed_topups": False})
    assert off == stock
    reset_timers()
    dosed = _day(monkeypatch, ON)
    assert dosed != stock


def test_without_a_heating_sensor_it_warns_and_stays_off(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        ent, clock, plant = _started(monkeypatch, {"dosed_topups": True})
    assert any("no heating power sensor" in r.getMessage() for r in caplog.records)
    assert not ent.dosing_active
    _run_room(ent, clock, plant, 60)
    assert not ent._last_result.settled
    assert ent._last_result.mode != "dose"


def test_a_failure_leaves_the_room_regulated(monkeypatch, caplog):
    ent, clock, plant = _started(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("doser broke")

    monkeypatch.setattr(ent._doser, "inputs", boom)
    with caplog.at_level(logging.ERROR):
        _run_room(ent, clock, plant, 40)
    errors = [r for r in caplog.records if "dosed top-ups failed" in r.getMessage()]
    assert len(errors) == 1
    assert not ent.dosing_active
    assert ent.topup_dose_summary()["error"].startswith("RuntimeError")
    # Regulated as without dosing: the stop released into a hold command.
    assert ent._last_result.mode in ("hold", "stop")
    assert not ent._last_result.settled


def test_the_options_form_refuses_dosing_without_a_heating_sensor():
    from tests.pure_modules import climate_controllers as cc

    assert cc.options_form_errors({"dosed_topups": True}) == {"base": "dosing_needs_heating_power"}
    assert cc.options_form_errors({"dosed_topups": True, "heating_power_entity_id": POWER}) == {}
    assert cc.options_form_errors({"dosed_topups": False}) == {}


# ---------------------------------------------------------------------------
# Reload, window, target change
# ---------------------------------------------------------------------------

def _until_pulse(ent, clock, plant, limit=90):
    for _ in range(limit):
        clock.tick(60.0)
        plant.advance(60.0)
        plant.feed()
        _cycle(ent)
        if ent._last_result is not None and ent._last_result.mode == "dose":
            return
    raise AssertionError("no pulse")


def test_a_reload_mid_pulse_keeps_the_pulse_and_its_heat(monkeypatch):
    ent, clock, plant = _started(monkeypatch)
    _until_pulse(ent, clock, plant)
    clock.tick(60.0)
    plant.advance(60.0)
    plant.feed()
    _cycle(ent)
    assert ent._last_result.mode == "dose"
    inflight = ent._doser.inflight_c
    again = _make_entity(dict(ON), window=False)
    plant.ent = again
    plant.seen = 0
    again.coordinator.data = ent.coordinator.data
    _restart_from(ent, again)
    assert again._reg_state.mode == "dose", "a pulse resumes as the pulse it is"
    assert again._reg_state.settled
    clock.tick(30.0)
    plant.advance(30.0)
    plant.feed()
    _cycle(again)
    assert again._doser.inflight_c >= inflight - 1e-6
    assert not [c for c in again.hass.service_calls if _is_heat(c)], (
        "no second 25 °C for the same pulse")


def test_a_window_ends_the_dosing_phase_and_drops_the_episode(monkeypatch):
    ent, clock, plant = _started(monkeypatch)
    _until_pulse(ent, clock, plant)
    monkeypatch.setattr(ent, "_window_open", lambda: True)
    clock.tick(60.0)
    plant.advance(60.0)
    plant.feed()
    _cycle(ent)
    assert not ent._last_result.settled
    assert ent._last_result.mode != "dose"
    events = [e.text for e in ent._activity.entries() if not e.decision]
    assert any("a window opened" in t for t in events if t.startswith("Dosed top-up:")), events[-5:]


def test_a_target_raised_under_heatup_enter_is_dosed_over_it_heats_up(monkeypatch):
    """A heat-up or a dose, nothing in between (owner, 31 Oct 2026): the
    target raised 0.5 gets a pulse sized for it, raised 1.0 a heat-up."""
    ent, clock, plant = _started(monkeypatch)
    _run_room(ent, clock, plant, 20)
    assert ent._last_result.settled
    ent._target_temp = round(plant.room, 2) + 0.5
    _run_room(ent, clock, plant, 1)
    assert ent._last_result.settled and ent._last_result.mode == "dose"
    assert ent._last_result.need_c == pytest.approx(0.5, abs=0.15)
    reset_timers()
    ent2, clock2, plant2 = _started(monkeypatch)
    _run_room(ent2, clock2, plant2, 20)
    ent2._target_temp = round(plant2.room, 2) + 1.0
    _run_room(ent2, clock2, plant2, 1)
    assert not ent2._last_result.settled and ent2._last_result.mode == "heat"


# ---------------------------------------------------------------------------
# Two rooms, one boiler
# ---------------------------------------------------------------------------

class _Entries:
    def __init__(self, *entities):
        self._by_id = {e._config_entry.entry_id: e._config_entry for e in entities}

    def async_get_entry(self, entry_id):
        return self._by_id.get(entry_id)


def _two_rooms(monkeypatch):
    clock = _Clock(monkeypatch)
    a = _entity(ON, entry_id="a", title="Living Room")
    b = _entity(ON, entry_id="b", title="Study")
    pa = _Plant(a, clock, room=20.62, fall_c_per_h=0.5)
    pb = _Plant(b, clock, room=20.66, fall_c_per_h=0.5)
    entries = _Entries(a, b)
    for ent, plant in ((a, pa), (b, pb)):
        ent.hass.config_entries = entries
        ent.hass.keep_tasks = True
        plant.feed()
        _run(_start(ent))
    return clock, (a, pa), (b, pb)


def test_a_room_due_soon_fires_with_the_other_rooms_pulse(monkeypatch):
    clock, (a, pa), (b, pb) = _two_rooms(monkeypatch)
    fired_b = []
    for _ in range(120):
        clock.tick(60.0)
        for ent, plant in ((a, pa), (b, pb)):
            plant.advance(60.0)
            plant.feed()
            _cycle(ent)
        # A pulse in one room wakes the other at once (queued tasks).
        for ent in (a, b):
            drain_tasks(ent)
        if b._last_result is not None and b._last_result.fired_by:
            fired_b.append(b._last_result.fired_by)
        if a._last_result is not None and a._last_result.fired_by == "together":
            fired_b.append("a-together")
        if fired_b:
            break
    assert fired_b, "the rooms never pulsed"
    # Whichever room fired first, the other joined it.
    texts = [e.text for ent in (a, b) for e in ent._activity.entries() if e.decision]
    assert any("with the Living Room's pulse" in t or "with the Study's pulse" in t
               for t in texts), texts[-6:]


def test_a_dosed_room_rides_another_zones_burn(monkeypatch):
    clock, (a, pa), (b, pb) = _two_rooms(monkeypatch)
    # Let both settle (cooling, both valves shut).
    for _ in range(12):
        clock.tick(60.0)
        for ent, plant in ((a, pa), (b, pb)):
            plant.advance(60.0)
            plant.feed()
            _cycle(ent)
    assert a._last_result.settled and b._last_result.settled
    # Room A's zone calls for heat (a heat-up elsewhere on the circuit), and
    # B is at its target, cooling fast enough that a minimum pulse fits.
    pb.room, pb.fall = TARGET - 0.005, 0.9 / 3600.0
    clock.tick(60.0)
    pa.advance(60.0)
    pa.feed()
    a.coordinator.data["heating_power"] = 70.0
    a.coordinator.data["tado_heating"] = True
    _cycle(a)
    pb.advance(60.0)
    pb.feed()
    _cycle(b)
    assert b._last_result.fired_by == "burn", b._last_result
    line = b._activity.doing
    assert "riding the Living Room's call for heat" in line
    # The pulse wakes the other room (a queued cycle): run it out.
    for ent in (a, b):
        drain_tasks(ent)


# ---------------------------------------------------------------------------
# A valve that kept heating after an off
# ---------------------------------------------------------------------------

def test_a_valve_still_heating_after_an_off_gets_it_again(monkeypatch, caplog):
    ent, clock, plant = _started(monkeypatch)
    _until_pulse(ent, clock, plant)
    for _ in range(10):
        clock.tick(60.0)
        plant.advance(60.0)
        plant.feed()
        _cycle(ent)
        if ent._last_result.mode != "dose":
            break
    offs = len([c for c in ent.hass.service_calls if _is_off(c)])
    assert offs >= 1
    off_ts = ent._last_command_sent_ts
    # The zone reports the valve still at 60 % eight minutes after the off.
    with caplog.at_level(logging.WARNING):
        for k in range(1, 10):
            clock.tick(60.0)
            plant.advance(60.0)
            plant.feed()
            ent.coordinator.data["heating_power"] = 60.0
            ent.coordinator.data["heating_power_reported_ts"] = clock.now
            _cycle(ent)
    resent = len([c for c in ent.hass.service_calls if _is_off(c)]) - offs
    assert 1 <= resent <= 2, resent
    assert any("still reports heating" in r.getMessage() for r in caplog.records)
    assert ent._last_command_sent_ts > off_ts


def test_no_resend_when_the_valve_reports_shut(monkeypatch):
    ent, clock, plant = _started(monkeypatch)
    _until_pulse(ent, clock, plant)
    _run_room(ent, clock, plant, 20)
    offs = [c for c in ent.hass.service_calls if _is_off(c)]
    heats = [c for c in ent.hass.service_calls if _is_heat(c)]
    # One off per pulse: the plant's valve reports 0 % after it shuts.
    assert len(offs) == len(heats)



def test_late_heating_reports_send_no_second_off(monkeypatch, caplog):
    """The zone's heating % reaching HA 200 s late (the cloud: 10-276 s):
    the valve's last report from before an off is no lost off, in a dosed
    room or in a stock room after its heat-up (2.15.0 review: at 3 min every
    off went twice, with a warning)."""
    for options, room in ((ON, 20.62), ({"heating_power_entity_id": POWER}, 19.4)):
        reset_timers()
        clock = _Clock(monkeypatch)
        ent = _entity(options)
        plant = _Plant(ent, clock, room=room, report_lag_s=200.0)
        plant.feed()
        _run(_start(ent))
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            _run_room(ent, clock, plant, 240)
        heats = [c for c in ent.hass.service_calls if _is_heat(c)]
        offs = [c for c in ent.hass.service_calls if _is_off(c)]
        assert heats, options
        assert not [r for r in caplog.records if "still reports heating" in r.getMessage()]
        assert len(offs) == len(heats), (options, len(offs), len(heats))


def test_a_late_first_report_leaves_the_heat_in_flight(monkeypatch):
    """The first heating report after a pulse comes 370 s late: the pulse
    is not learned from, but its heat stays booked, so pulses keep their
    size and the room its band (2.15.0 review: every pulse then ran to
    the 12-min cap)."""
    clock = _Clock(monkeypatch)
    ent = _entity(ON)
    plant = _Plant(ent, clock, report_lag_s=370.0)
    plant.feed()
    _run(_start(ent))
    watch = _Pulses(ent, clock, plant)
    _run_room(ent, clock, plant, 240, on_cycle=watch)
    assert len(watch.lengths) >= 4
    assert max(watch.lengths) < 600.0, watch.lengths
    later = [r for t, r in watch.rooms if t - watch.rooms[0][0] > 3600.0]
    assert TARGET - 0.12 <= min(later) and max(later) <= TARGET + 0.08, (min(later), max(later))


def _hold_commands(ent) -> list:
    """Commands that are neither the maximum nor an off: holds."""
    return [c for c in ent.hass.service_calls
            if c[0] == "climate" and c[1] == "set_temperature" and not _is_heat(c)]


def test_short_of_the_target_it_is_dosed_never_held(monkeypatch):
    """0.3 below on a Tado-like valve that throttles a hold: with dosing on
    no hold command ever goes out; the shortfall gets a pulse and the room
    lands in the band."""
    clock = _Clock(monkeypatch)
    ent = _entity(ON)
    plant = _TadoPlant(ent, clock, room=TARGET - 0.3, fall_c_per_h=0.8, hold_open=0.5)
    plant.feed()
    _run(_start(ent))
    rooms = []

    def watch():
        rooms.append((clock.now, plant.room, ent._last_result.mode))

    _run_room(ent, clock, plant, 300, on_cycle=watch)
    assert not _hold_commands(ent), _hold_commands(ent)[:3]
    assert all(m != "hold" for _, _, m in rooms)
    assert any(m == "dose" for _, _, m in rooms)
    later = [room for t, room, _ in rooms if t - rooms[0][0] > 3600.0]
    assert TARGET - 0.15 <= min(later) and max(later) <= TARGET + 0.10, (min(later), max(later))


def test_a_heat_up_then_dosing_never_a_hold(monkeypatch):
    """1.4 below (the morning's Comfort): a heat-up at full power, its coast,
    then pulses; no hold command at any point, and the room ends in the
    band."""
    clock = _Clock(monkeypatch)
    ent = _entity(ON)
    plant = _TadoPlant(ent, clock, room=TARGET - 1.4, fall_c_per_h=0.5, hold_open=0.5)
    plant.feed()
    _run(_start(ent))
    modes = []

    def watch():
        modes.append((clock.now, plant.room, ent._last_result.mode))

    _run_room(ent, clock, plant, 360, on_cycle=watch)
    seen = [m for _, _, m in modes]
    assert "heat" in seen and "dose" in seen and "hold" not in seen
    assert not _hold_commands(ent)
    later = [room for t, room, _ in modes if t - modes[0][0] > 4 * 3600.0]
    assert TARGET - 0.15 <= min(later) and max(later) <= TARGET + 0.15, (min(later), max(later))


def test_the_pulse_is_capped_on_the_rooms_heating_rate(monkeypatch):
    """With the auto-tuner's heating rate known, each pulse's cap is the
    dose worked out on that rate (the measured shortfall, the heat in
    flight at it, the cap's own observer; no aim bias) plus the margin,
    set when it starts; the pulse's line says so."""
    from tests.pure_modules import parameters as P
    from tests.pure_modules import regulation as R

    ent, clock, plant = _started(monkeypatch)
    ent._autotune_enabled = True
    monkeypatch.setattr(ent._autotuner, "heatup_model", lambda: (2.62, 840.0))
    rate = 2.62 / 60.0
    caps = []

    def watch():
        r = ent._last_result
        if r.fired_by:
            d, lr = ent._doser, ent._doser.learner
            need = R.dose_cap_need_c(r.error_c, d.predictor.inflight_c(rate),
                                     d.cap_loss_slope_c_per_s, lr.theta_s + 2 * lr.tau_s,
                                     ent._regulator.cfg.slope_deadband_c_per_h)
            caps.append((R.dose_cap_s(need, rate, P.DoseConfig()), r.pulse_cap_s,
                         ent._reg_state.pulse_cap_s))

    _run_room(ent, clock, plant, 120, on_cycle=watch)
    assert caps
    for expected, reported, kept in caps:
        assert reported == pytest.approx(expected, abs=1.0) and kept == pytest.approx(reported, abs=0.1)
    texts = [e.text for e in ent._activity.entries() if e.decision]
    assert any(t.startswith("Top-up pulse:") and "cap about" in t for t in texts), texts[-5:]


def test_a_weak_radiator_is_learned_and_the_room_held(monkeypatch):
    """A radiator less than half the model's gain, a longer delay, a
    shorter spread (gain 0.02, delay 9 min, spread 4 min, cooling 0.3 °C/h):
    the overlapping episodes are judged, the gain in use comes down to the
    plant's, and the room ends in the band (2.15.0 review: nothing was ever
    learned and the room sat 0.08-0.13 under all day)."""
    clock = _Clock(monkeypatch)
    ent = _entity(ON)
    plant = _Plant(ent, clock, gain=0.02, theta_s=540.0, tau_s=240.0, fall_c_per_h=0.3)
    plant.feed()
    _run(_start(ent))
    watch = _Pulses(ent, clock, plant)
    _run_room(ent, clock, plant, 12 * 60, on_cycle=watch)
    learner = ent._doser.learner
    assert learner._accepted_total >= 3, learner.summary()
    # The delay and spread follow the plant at once; the gain comes down
    # one 15 % step per 12 h (bigger doses slowly), the bias covers the rest.
    assert learner.theta_s >= 420.0 and learner.tau_s <= 360.0, learner.summary()
    assert learner.gain_c_per_open_min < 0.045, learner.summary()
    last = [r for t, r in watch.rooms if t - watch.rooms[0][0] > 8 * 3600.0]
    assert TARGET - 0.12 <= min(last) and max(last) <= TARGET + 0.08, (min(last), max(last))


def test_a_fast_cooling_room_is_dosed_in_the_band_never_held(monkeypatch):
    """1.5 °C/h, the most pulses covered under the old 12-min maximum: the
    pulses are sized and capped on the room, no hold goes out, and the
    room is not left 0.3 under (2.15.0 review: pulses at a fixed cap held
    a weak room at -0.37..-0.28 for 12 h)."""
    clock = _Clock(monkeypatch)
    ent = _entity(ON)
    plant = _TadoPlant(ent, clock, fall_c_per_h=1.5, hold_open=0.6)
    plant.feed()
    _run(_start(ent))
    rooms = []

    def watch():
        rooms.append((clock.now, plant.room, ent._last_result.mode))

    _run_room(ent, clock, plant, 6 * 60, on_cycle=watch)
    assert not _hold_commands(ent)
    later = [room for t, room, _ in rooms if t - rooms[0][0] > 3600.0]
    assert sum(1 for room in later if room < TARGET - 0.3) <= 10, min(later)
    assert max(later) <= TARGET + 0.15


def test_the_sensor_attributes_change_only_with_pulses_and_lessons(monkeypatch):
    """The Top-up gain sensor's attributes: the recorder writes a row for
    every change, so they change when a pulse starts or an episode is
    judged, never every cycle (2.15.0 review: 489 of 504 cycles)."""
    ent, clock, plant = _started(monkeypatch)
    watch = _Pulses(ent, clock, plant)
    changes = [0]
    last = [json.dumps(ent.topup_dose_summary(), sort_keys=True)]

    def on_cycle():
        watch()
        now = json.dumps(ent.topup_dose_summary(), sort_keys=True)
        if now != last[0]:
            changes[0] += 1
            last[0] = now

    _run_room(ent, clock, plant, 240, on_cycle=on_cycle)
    judged = ent._doser.learner._accepted_total + 2
    assert changes[0] <= len(watch.starts) + judged + 2, (changes[0], len(watch.starts), judged)
    diagnostics = ent.topup_dose_diagnostics()
    assert "inflight_c" in diagnostics and "inflight_c" not in ent.topup_dose_summary()


def test_no_cycle_after_the_entity_is_removed(monkeypatch):
    ent, clock, plant = _started(monkeypatch)
    _until_pulse(ent, clock, plant)
    wake = _pending_wake()
    calls = len(ent.hass.service_calls)
    _run(ent.async_will_remove_from_hass())
    if wake is not None:
        _run(wake["callback"](None))
    ent.hass.keep_tasks = True
    ent._dosing_wake()
    assert not ent.hass.tasks
    assert len(ent.hass.service_calls) == calls


def test_the_room_sensor_lost_mid_pulse_ends_it(monkeypatch):
    ent, clock, plant = _started(monkeypatch)
    _until_pulse(ent, clock, plant)
    heats = len([c for c in ent.hass.service_calls if _is_heat(c)])
    for _ in range(int(ent._sensor_grace_s // 60) + 3):
        clock.tick(60.0)
        plant.advance(60.0)
        plant.feed()
        ent.coordinator.data["room_temp"] = None
        _cycle(ent)
    assert [c for c in ent.hass.service_calls if _is_off(c)], "the pulse was not ended"
    assert len([c for c in ent.hass.service_calls if _is_heat(c)]) == heats
    state = ent._reg_state
    assert not state.settled and state.pulse_start_ts is None
    assert state.last_pulse_start_ts is not None
    ep = ent._doser.learner.episode
    assert ep is None or ep.pulse_end_ts is not None


def test_a_dosed_step_that_raises_leaves_the_room_regulated(monkeypatch, caplog):
    ent, clock, plant = _started(monkeypatch)

    def boom(*_a, **_k):
        raise ValueError("dose step broke")

    monkeypatch.setattr(ent._regulator, "_dose_step", boom)
    with caplog.at_level(logging.ERROR):
        _run_room(ent, clock, plant, 40)
    errors = [r for r in caplog.records if "dosed top-ups failed" in r.getMessage()]
    assert len(errors) == 1
    assert not ent.dosing_active
    assert ent._last_result.mode != "dose" and not ent._last_result.settled


def test_a_held_back_pulse_is_a_pulse_when_it_goes_out(monkeypatch):
    """A pulse due while a change by hand settles: nothing goes out, no
    episode starts and no other room is woken; it goes out once the
    change has settled, and then it is a pulse (an episode, counted)."""
    ent, clock, plant = _started(monkeypatch)
    held = []
    for _ in range(90):
        clock.tick(60.0)
        plant.advance(60.0)
        plant.feed()
        ent._manual_change_ts = clock.now - 1.0      # a press a second ago
        _cycle(ent)
        r = ent._last_result
        if r.fired_by:
            held.append(ent._last_reason)
            break
    assert held and not held[0].startswith("sent("), held
    assert not [c for c in ent.hass.service_calls if _is_heat(c)]
    assert ent._doser.learner.episode is None
    assert ent.topup_dose_summary()["pulses_24h"] == 0
    # The change settles: the pulse goes out on a later cycle.
    for _ in range(3):
        clock.tick(60.0)
        plant.advance(60.0)
        plant.feed()
        _cycle(ent)
        if [c for c in ent.hass.service_calls if _is_heat(c)]:
            break
    assert [c for c in ent.hass.service_calls if _is_heat(c)]
    assert ent._doser.learner.episode is not None
    assert ent.topup_dose_summary()["pulses_24h"] == 1


def test_a_failed_heat_call_that_lands_still_ends_by_its_dose(monkeypatch):
    """Every 25 °C call times out on the cloud but the valve takes it: the
    pulse keeps its clock and its heat is booked as possibly open, so it
    still ends by its dose or its maximum and the room stays near the band
    (2.15.0 review: the clock restarted every cycle, the valve ran 15 min
    and the room peaked +0.39)."""
    ent, clock, plant = _started(monkeypatch)
    _run_room(ent, clock, plant, 15)
    real = ent.hass._async_call

    async def flaky(domain, service, service_data=None, blocking=False, **kw):
        await real(domain, service, service_data, blocking=blocking, **kw)
        if service == "set_temperature" and (service_data or {}).get("temperature", 0) >= 24.9:
            raise TimeoutError("the cloud timed out")

    ent.hass.services.async_call = flaky
    watch = _Pulses(ent, clock, plant)
    _run_room(ent, clock, plant, 180, on_cycle=watch)
    assert len(watch.lengths) >= 3, watch.lengths
    assert max(watch.lengths) <= 720.0 + 60.0, watch.lengths
    assert max(r for _, r in watch.rooms) <= TARGET + 0.12


def test_the_watchdog_shutting_a_pulse_ends_it(monkeypatch):
    ent, clock, plant = _started(monkeypatch)
    _until_pulse(ent, clock, plant)
    start = ent._reg_state.pulse_start_ts
    # The cycles stop (the coordinator fails) and the watchdog shuts the valve.
    clock.tick(ent._config.cycle_watchdog_s + 60.0)
    plant.advance(ent._config.cycle_watchdog_s + 60.0)
    plant.feed()
    _run(ent._async_watchdog_check())
    assert [c for c in ent.hass.service_calls if _is_off(c)]
    state = ent._reg_state
    assert not state.settled and state.pulse_start_ts is None
    assert state.last_pulse_start_ts == start and state.last_pulse_end_ts == clock.now


def test_a_clock_going_back_mid_pulse_is_the_same_pulse(monkeypatch):
    ent, clock, plant = _started(monkeypatch)
    _until_pulse(ent, clock, plant)
    count = ent.topup_dose_summary()["pulses_24h"]
    clock.tick(-120.0)
    plant.feed()
    _cycle(ent)
    assert ent._last_result.mode == "dose" and ent._last_result.fired_by is None
    assert ent.topup_dose_summary()["pulses_24h"] == count


def test_a_queued_coordinated_cycle_runs_none_after_removal(monkeypatch):
    ent, clock, plant = _started(monkeypatch)
    _run_room(ent, clock, plant, 5)
    ent.hass.keep_tasks = True
    ent._dosing_wake()
    assert len(ent.hass.tasks) == 1
    cycles = []
    monkeypatch.setattr(ent, "_async_regulation_cycle",
                        lambda trigger: cycles.append(trigger) or _noop())
    _run(ent.async_will_remove_from_hass())
    drain_tasks(ent)
    assert cycles == []


async def _noop():
    return None


# ---------------------------------------------------------------------------
# Review 3 (31 Oct 2026): the paths into and out of the dosing phase
# ---------------------------------------------------------------------------

def test_switched_off_mid_pulse_the_pulse_ends(monkeypatch):
    """m5: switched off during a pulse and on again a few minutes later (well
    within any cap): the old pulse does not carry on with a new 25 °C, and
    no spurious "Top-up ended" line is written."""
    from homeassistant.components.climate import HVACMode

    ent, clock, plant = _started(monkeypatch)
    _until_pulse(ent, clock, plant)
    _run(ent.async_set_hvac_mode(HVACMode.OFF))
    assert ent._reg_state.mode != "dose" and ent._reg_state.pulse_start_ts is None
    assert ent._reg_state.last_pulse_end_ts == clock.now
    for _ in range(3):
        clock.tick(60.0)
        plant.advance(60.0)
        plant.feed()
        _cycle(ent)
    heats_before = sum(1 for c in ent.hass.service_calls if _is_heat(c))
    _run(ent.async_set_hvac_mode(HVACMode.HEAT))
    clock.tick(30.0)
    plant.advance(30.0)
    plant.feed()
    _cycle(ent)
    assert ent._last_result.dose_end is None
    assert ent._last_result.mode != "dose"
    assert sum(1 for c in ent.hass.service_calls if _is_heat(c)) == heats_before
    events = [e.text for e in ent._activity.entries() if not e.decision]
    assert any("Roomstat was switched off" in t for t in events), events[-5:]


def test_a_restart_longer_than_the_pulse_ends_it_as_a_pulse(monkeypatch):
    """m3: the room comes back after its pulse should have ended; the
    valve's 25 °C is the pulse (not a heat-up) and the first cycle ends it."""
    ent, clock, plant = _started(monkeypatch)
    _until_pulse(ent, clock, plant)
    clock.tick(60.0)
    plant.advance(60.0)
    plant.feed()
    _cycle(ent)
    assert ent._last_result.mode == "dose"
    again = _make_entity(dict(ON), window=False)
    plant.ent = again
    plant.seen = 0
    clock.tick(1500.0)                     # HA down 25 min (past the 12-min fixed limit)
    plant.advance(1500.0)
    plant.feed()
    again.coordinator.data = ent.coordinator.data
    _restart_from(ent, again)
    assert again._reg_state.mode == "dose"
    clock.tick(30.0)
    plant.advance(30.0)
    plant.feed()
    _cycle(again)
    assert again._last_result.mode == "stop"
    assert again._last_result.dose_end in ("max", "stop_rule", "covered", "landing")
    assert not [c for c in again.hass.service_calls if _is_heat(c)]


def test_a_dosed_target_step_is_no_heat_up_for_the_tuner(monkeypatch):
    """M4: a target raised 0.6 is dosed; the auto-tuner starts no heat-up
    episode on it (its "coast" would be a pulse's landing)."""
    ent, clock, plant = _started(monkeypatch)
    ent._autotune_enabled = True
    _run_room(ent, clock, plant, 30)
    assert ent._last_result.settled
    ent._target_temp = round(plant.room, 2) + 0.6
    _run_room(ent, clock, plant, 5)
    assert ent._last_result.settled
    assert ent._autotuner._heatup is None


def test_a_hold_on_the_valve_must_show_before_the_first_pulse(monkeypatch):
    """m9: dosing turned on (or the upgrade from 2.15.0) with a hold command
    on the valve: no pulse fires on top of the hold's heat; Roomstat shuts
    the valve and waits for that heat to show first."""
    clock = _Clock(monkeypatch)
    ent = _entity(ON)
    plant = _TadoPlant(ent, clock, room=TARGET - 0.25, fall_c_per_h=0.5, hold_open=0.5)
    plant.setpoint = 21.3                  # the valve holds a hold command
    plant.timeline = [(clock.now - 600.0, 21.3)]
    plant.feed()
    _run(_start(ent))
    first = []

    def watch():
        if ent._last_result is not None and ent._last_result.fired_by and not first:
            first.append(clock.now)

    start = clock.now
    _run_room(ent, clock, plant, 60, on_cycle=watch)
    assert first, "never dosed"
    lead = ent._doser.learner.visible_dead_time_s
    assert first[0] - start >= lead - 60.0
