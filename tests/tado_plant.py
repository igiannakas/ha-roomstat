"""A room and its Tado valve for end-to-end tests through the real climate
entity (tests/ha_harness.py).

``Plant``: the room cools at a steady rate; the valve opens a minute after a
25 °C command and shuts 1.5 min after an off (any other setpoint moves
nothing); its heat reaches the room after a delay and spreads over a time
constant (first order plus dead time: a gain in °C per minute fully open, a
delay and a spread).  The zone's heating % goes into the coordinator data as
Tado's heating sensor reports it, optionally late (the cloud: 10-276 s).
``TadoPlant`` holds every command as a Tado does: a setpoint between the
frost level and 25 °C opens the valve part-way.

Not a test module (no ``test_`` prefix); imported by the e2e test files.
"""
from __future__ import annotations

import math

from tests.ha_harness import _make_entity, _run

# The zone's heating power sensor (Tado's heating %).
POWER = "sensor.test_zone_heating"
# The harness's comfort target.
TARGET = 20.5


class Clock:
    """The wall clock the entity reads (``time.time``), moved by the test."""

    def __init__(self, monkeypatch, start=1_800_000_000.0):
        self.now = start
        monkeypatch.setattr("time.time", lambda: self.now)

    def tick(self, s=60.0):
        self.now += s


def is_heat(call) -> bool:
    """A full-power command (25 °C)."""
    dom, svc, data = call
    return dom == "climate" and svc == "set_temperature" and data.get("temperature", 0) >= 24.9


def is_off(call) -> bool:
    """An off (``set_hvac_mode off``)."""
    dom, svc, data = call
    mode = data.get("hvac_mode")
    return (dom == "climate" and svc == "set_hvac_mode"
            and str(getattr(mode, "value", mode)).lower() == "off")


class Plant:
    """The room and its valve (see the module docstring).  The valve opens
    only on a 25 °C command and shuts on an off; any other setpoint moves
    nothing.  ``report_lag_s``: the zone's heating % reaches HA that much
    late."""

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
        self.line: list[tuple[float, float]] = []   # (arrives, minutes fully open)
        self.emitter = 0.0
        self.t = clock.now
        self.power_ts = clock.now
        self.power = 0.0
        self.power_log: list[tuple[float, float]] = [(clock.now, 0.0)]

    def _commands(self) -> None:
        calls = self.ent.hass.service_calls
        for call in calls[self.seen:]:
            if is_heat(call) and not self.heat_cmd:
                self.heat_cmd = True
                self.open_from, self.open_until = self.t + self.LATENCY_S, None
            elif is_off(call) and self.heat_cmd:
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
        """The coordinator data as __init__.py builds it from the plant."""
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


class TadoPlant(Plant):
    """A valve that holds every command, as a Tado does: 25 °C opens it
    fully, an off shuts it, each a minute after the command (an off 1.5
    min).  A lower setpoint opens it by (setpoint - its reading) / 5 (fully
    at 5 °C over its reading)."""

    def __init__(self, ent, clock, **kw):
        super().__init__(ent, clock, **kw)
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
            elif is_off((dom, svc, data)):
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
        return min(1.0, max(0.0, push / 5.0))

    def _trv(self) -> dict:
        return {"tado_setpoint": self.setpoint, "tado_off": self.setpoint is None}


def room_entity(options=None, **kw):
    """The real climate entity, without a window sensor."""
    return _make_entity(dict(options or {}), window=False, **kw)


def cycle(ent) -> None:
    """One timed regulation cycle."""
    _run(ent._async_regulation_cycle("timer"))


def run_room(ent, clock, plant, minutes, *, on_cycle=None) -> None:
    """Minute cycles with the plant run to each one (``on_cycle`` called
    after each)."""
    for _ in range(minutes):
        clock.tick(60.0)
        plant.advance(60.0)
        plant.feed()
        cycle(ent)
        if on_cycle is not None:
            on_cycle()
