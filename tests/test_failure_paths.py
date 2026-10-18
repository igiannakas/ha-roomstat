"""Failure paths, run through the real climate entity.

Each test is a way the integration used to get stuck, spam the log or keep
heating when it should not:

- a HomeKit Tado valve that is off refuses setpoints below 7 °C;
- a valve switched off from the Tado app while Roomstat asks for heat;
- a command that fails (refused, timed out, unexpected error);
- a room sensor that stays away (reported, nothing sent meanwhile);
- a long pause or a clock that goes back;
- an OFF that does not reach the valve.
"""
from __future__ import annotations

import logging
import sys

import pytest

from tests.ha_harness import (
    TRV,
    _climate,
    _make_entity,
    _run,
    _start,
    e2e,
    reset_timers,
)

pytestmark = e2e

_HAError = getattr(
    sys.modules.get("homeassistant.exceptions"), "HomeAssistantError", Exception
)
_SVError = getattr(
    sys.modules.get("homeassistant.exceptions"), "ServiceValidationError", Exception
)


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


def _feed(ent, *, room=19.0, internal=18.0, setpoint=20.0, off=False,
          lo=5.0, hi=25.0, trv_available=True):
    """Coordinator data as __init__.py builds it."""
    if not trv_available:
        internal = setpoint = lo = hi = None
        off = False
    ent.coordinator.data = {
        "room_temp": room, "room_temp_ts": None,
        "tado_internal_temp": internal, "tado_setpoint": None if off else setpoint,
        "tado_min_temp": lo, "tado_max_temp": hi, "tado_off": off,
        "fallback_internal_temp": None, "fallback_setpoint": None,
        "fallback_min_temp": None, "fallback_max_temp": None, "fallback_off": False,
    }


def _feed_off(ent, **kw):
    """A HomeKit Tado valve that is off: no setpoint, accepts 7-35 °C."""
    _feed(ent, off=True, lo=7.0, hi=35.0, **kw)


def _cycle(ent):
    _run(ent._async_regulation_cycle("timer"))


def _sent(ent):
    return [d.get("temperature") for dom, svc, d in ent.hass.service_calls
            if svc == "set_temperature"]


def _modes(ent):
    return [d["hvac_mode"] for dom, svc, d in ent.hass.service_calls
            if svc == "set_hvac_mode"]


def _entity(options=None):
    ent = _make_entity(options)
    _run(_start(ent))
    return ent


def _validating_trv(ent):
    """Service calls behave like HA's climate service: a temperature
    outside the valve's current range is refused."""
    calls = ent.hass.service_calls

    async def call(domain, service, service_data=None, blocking=False, **_):
        data = dict(service_data or {})
        if service == "set_temperature":
            d = ent.coordinator.data
            lo, hi = d["tado_min_temp"], d["tado_max_temp"]
            if not lo <= data["temperature"] <= hi:
                raise _SVError(
                    f"Provided temperature {data['temperature']} is not valid. "
                    f"Accepted range is {lo:g} to {hi:g}."
                )
        calls.append((domain, service, data))

    ent.hass.services.async_call = call


def _failing(ent, exc):
    async def call(*_a, **_k):
        raise exc

    ent.hass.services.async_call = call


def _working(ent):
    ent.hass.services.async_call = ent.hass._async_call


# ---------------------------------------------------------------------------
# A valve that is off
# ---------------------------------------------------------------------------

class TestValveOff:

    def test_frost_target_leaves_an_off_valve_alone(self, monkeypatch):
        """The master-bedroom bug: frost preset, valve off, and every two
        minutes a 5 °C command that HA refused (7-35 °C while off)."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        _run(ent.async_set_preset_mode("frost_protection"))
        _validating_trv(ent)
        ent.hass.service_calls.clear()
        for _ in range(10):
            clock.tick(60)
            _feed_off(ent, room=20.0, internal=20.5)
            _cycle(ent)
            assert ent._last_reason == "already_at_target"
        assert ent.hass.service_calls == []
        assert ent.extra_state_attributes["last_command_error"] is None

    def test_heat_wanted_is_kept_inside_what_the_off_valve_accepts(self, monkeypatch):
        """A hold command between 5 and 7 °C goes out as 7."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        _validating_trv(ent)
        # A warm room: the command lands between 5 and 7 °C.
        ent._target_temp = 6.0
        _run(ent.async_set_temperature(temperature=6.0))
        ent.hass.service_calls.clear()
        ent._last_command_sent_ts = 0.0
        # An off valve has no setpoint, so the room resumed in stop (which
        # leaves it off); this test is about the hold's command range.
        ent._reg_state.mode = "hold"
        clock.tick(300)
        _feed_off(ent, room=6.0, internal=6.0)
        _cycle(ent)
        assert _sent(ent) == [7.0]
        assert ent._last_reason.startswith("sent(")

    def test_comfort_turns_an_off_valve_back_on(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _validating_trv(ent)
        clock.tick(300)
        _feed_off(ent, room=18.0, internal=18.0)
        _cycle(ent)
        assert len(_sent(ent)) == 1 and 7.0 <= _sent(ent)[0] <= 25.0
        assert ent.hass.service_calls[-1][2]["hvac_mode"] == _climate.HVACMode.HEAT

    def test_valve_switched_off_in_the_app_is_turned_back_on(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, room=19.0)
        _cycle(ent)
        sent = ent._last_sent_setpoint
        assert sent is not None and sent > 5.0
        ent.hass.service_calls.clear()

        # Shortly after: the valve may just be slow to report.
        clock.tick(60)
        _feed_off(ent, room=19.0)
        _cycle(ent)
        assert _sent(ent) == []

        clock.tick(180)
        _feed_off(ent, room=19.0)
        _cycle(ent)
        assert len(_sent(ent)) == 1


# ---------------------------------------------------------------------------
# Commands that fail
# ---------------------------------------------------------------------------

class TestFailedCommands:

    @pytest.mark.parametrize("exc", [
        _HAError("Bridge did not answer"),
        TimeoutError(),
        ValueError("unexpected"),
    ])
    def test_failure_never_stops_the_cycle(self, monkeypatch, exc):
        _Clock(monkeypatch)
        ent = _entity({"autotune_enabled": True})
        seen = []
        orig = ent._autotuner.observe
        ent._autotuner.observe = lambda s: (seen.append(s), orig(s))[1]
        _failing(ent, exc)
        _feed(ent)
        _cycle(ent)
        assert ent._last_reason.startswith("send_failed(")
        assert ent._last_sent_setpoint is None
        assert ent.extra_state_attributes["last_command_error"]
        assert len(seen) == 1          # the tuner still got the cycle

    def test_retried_once_per_interval_and_logged_once(self, monkeypatch, caplog):
        clock = _Clock(monkeypatch)
        ent = _entity()
        attempts = []

        async def refuse(*_a, **_k):
            attempts.append(clock.now)
            raise _HAError("Bridge did not answer")

        ent.hass.services.async_call = refuse
        caplog.set_level(logging.DEBUG)
        for _ in range(10):
            _feed(ent)
            _cycle(ent)
            clock.tick(60)
        # 10 minutes: one attempt per 180 s rate-limit interval.
        assert len(attempts) == 4
        warnings = [r for r in caplog.records
                    if r.levelno >= logging.WARNING and "failed" in r.getMessage()]
        assert len(warnings) == 1
        assert not any(r.exc_info for r in warnings)

        # Back: the next attempt succeeds and the error is cleared.
        _working(ent)
        clock.tick(180)
        _feed(ent)
        _cycle(ent)
        assert ent._last_reason.startswith("sent(")
        assert ent.extra_state_attributes["last_command_error"] is None
        assert any("work again" in r.getMessage() for r in caplog.records)

    def test_unexpected_error_keeps_its_traceback(self, monkeypatch, caplog):
        _Clock(monkeypatch)
        ent = _entity()
        _failing(ent, ValueError("bug in another integration"))
        caplog.set_level(logging.WARNING)
        _feed(ent)
        _cycle(ent)
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1 and errors[0].exc_info

    def test_a_failed_command_is_sent_again(self, monkeypatch):
        """The baseline stays at what the valve last took, so the new target
        goes out again once the rate limit allows."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, room=19.0)
        _cycle(ent)
        first = ent._last_sent_setpoint
        _failing(ent, TimeoutError())
        clock.tick(300)
        _feed(ent, room=21.0, internal=20.0, setpoint=first)
        _cycle(ent)
        assert ent._last_reason.startswith("send_failed(")
        assert ent._last_sent_setpoint == first
        _working(ent)
        clock.tick(180)
        _feed(ent, room=21.0, internal=20.0, setpoint=first)
        _cycle(ent)
        assert ent._last_reason.startswith("sent(")
        assert ent._last_sent_setpoint != first


# ---------------------------------------------------------------------------
# A room sensor that stays away
# ---------------------------------------------------------------------------

class TestRoomSensorLost:
    """The room sensor is expected to be reliable: a long outage is reported
    (a repair, see test_repairs.py), and the radiator keeps its last setting.
    """

    def test_short_gap_is_bridged(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, room=19.0)
        _cycle(ent)
        ent.hass.service_calls.clear()
        for _ in range(5):
            clock.tick(60)
            _feed(ent, room=None)
            _cycle(ent)
            assert ent._sensor_degraded
            assert ent._last_reason != "waiting_for_sensors"

    def test_long_outage_sends_nothing(self, monkeypatch, caplog):
        """Holding the target, then the sensor disappears: nothing more is
        sent, the TRV keeps what it has."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        caplog.set_level(logging.INFO)
        _feed(ent, room=20.6, internal=19.0)
        _cycle(ent)
        assert ent._reg_state.mode == "stop"
        held = ent._last_sent_setpoint
        ent.hass.service_calls.clear()
        for _ in range(30):
            clock.tick(60)
            _feed(ent, room=None, internal=19.0, setpoint=held)
            _cycle(ent)
        assert ent.hass.service_calls == []
        assert ent._last_reason == "waiting_for_sensors"
        assert ent.extra_state_attributes["sensor_degraded"] is True
        lost = [r for r in caplog.records if "room sensor unavailable" in r.getMessage()]
        assert len(lost) == 1 and lost[0].levelno == logging.WARNING

    def test_heat_up_is_ended_when_the_sensor_is_lost(self, monkeypatch):
        """A heat-up's maximum command is never left in place without a room
        reading: it is ended with one minimum command (coast), then nothing
        more."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, room=17.0, internal=18.0)
        _cycle(ent)
        assert ent._reg_state.mode == "heat"
        assert _sent(ent) == [25.0]
        ent.hass.service_calls.clear()
        for _ in range(30):
            clock.tick(60)
            _feed(ent, room=None, internal=19.0, setpoint=25.0)
            _cycle(ent)
        assert _sent(ent) == [5.0]
        assert ent._reg_state.mode == "coast"
        assert ent._last_reason == "waiting_for_sensors"

    def test_dead_sensor_after_restart_sends_nothing(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        for _ in range(10):
            _feed(ent, room=None, internal=19.0, setpoint=20.0)
            _cycle(ent)
            clock.tick(60)
        assert ent.hass.service_calls == []
        assert ent._last_reason == "waiting_for_sensors"

    def test_dead_sensor_after_restart_ends_a_maximum_the_valve_holds(self, monkeypatch):
        """The valve was left at 25 by a heat-up before the restart: once the
        sensor counts as lost, that maximum is ended like any other (the
        resume is read from the valve on the first cycle that sees it)."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        for _ in range(10):
            _feed(ent, room=None, internal=19.0, setpoint=25.0)
            _cycle(ent)
            clock.tick(60)
        assert _sent(ent) == [5.0]
        assert ent._reg_state.mode == "coast"
        assert ent._last_reason == "waiting_for_sensors"

    def test_regulation_resumes_when_the_sensor_is_back(self, monkeypatch, caplog):
        clock = _Clock(monkeypatch)
        ent = _entity()
        caplog.set_level(logging.INFO)
        _feed(ent, room=19.0)
        _cycle(ent)
        for _ in range(8):
            clock.tick(60)
            _feed(ent, room=None)
            _cycle(ent)
        clock.tick(180)
        _feed(ent, room=19.2, internal=18.0, setpoint=10.0)
        ent._last_sent_setpoint = None
        _cycle(ent)
        assert ent._last_reason.startswith("sent(")
        assert ent._sensor_degraded is False
        assert any("room sensor is back" in r.getMessage() for r in caplog.records)

    def test_valve_away_too_waits(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        clock.tick(600)
        for _ in range(8):
            _feed(ent, room=None, trv_available=False)
            _cycle(ent)
            clock.tick(60)
        assert ent._last_reason == "waiting_for_sensors"
        assert ent.hass.service_calls == []


# ---------------------------------------------------------------------------
# Pauses and the clock
# ---------------------------------------------------------------------------

class TestPausesAndClock:

    def _spy_dt(self, ent):
        seen = []
        orig = ent._regulator.compute

        def compute(**kw):
            seen.append(kw["time_delta_s"])
            return orig(**kw)

        ent._regulator.compute = compute
        return seen

    def test_long_pause_is_capped(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        seen = self._spy_dt(ent)
        _feed(ent, room=20.4)
        _cycle(ent)
        clock.tick(7200)                       # HA busy or restarting
        _feed(ent, room=20.4)
        _cycle(ent)
        assert seen == [0.0, 120.0]

    def test_clock_going_back_does_not_block_commands(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        seen = self._spy_dt(ent)
        _feed(ent, room=19.0)
        _cycle(ent)
        assert ent._last_sent_setpoint is not None
        clock.tick(-3600)                      # NTP correction
        _feed(ent, room=19.0, internal=18.0, setpoint=10.0)
        ent._last_sent_setpoint = None         # e.g. valve reports 10 °C
        _cycle(ent)
        assert seen[-1] == 0.0
        assert ent._last_reason.startswith("sent(")


# ---------------------------------------------------------------------------
# Off
# ---------------------------------------------------------------------------

class TestOff:

    def test_off_that_fails_stays_off_and_is_sent_again(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, room=19.0)
        _failing(ent, _HAError("Bridge did not answer"))
        _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
        assert ent.hvac_mode == _climate.HVACMode.OFF
        assert ent._last_reason == "send_failed(hvac_off)"

        _working(ent)
        clock.tick(60)
        _feed(ent, room=19.0)
        _cycle(ent)
        assert _modes(ent) == []               # not before the interval
        clock.tick(180)
        _feed(ent, room=19.0)
        _cycle(ent)
        assert _modes(ent) == [_climate.HVACMode.OFF]
        assert ent._last_reason == "sent(hvac_off)"

    def test_valve_turned_on_while_off_is_turned_off_again(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, room=19.0)
        _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
        ent.hass.service_calls.clear()
        for _ in range(3):
            clock.tick(60)
            _feed_off(ent, room=19.0)
            _cycle(ent)
        assert ent.hass.service_calls == []
        assert ent._last_reason == "hvac_off"
        clock.tick(60)
        _feed(ent, room=19.0, setpoint=21.0)   # dial / Tado app
        _cycle(ent)
        assert _modes(ent) == [_climate.HVACMode.OFF]

    def test_heat_runs_regulation_even_if_the_mode_change_fails(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, room=19.0)
        _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
        clock.tick(600)
        calls = []

        async def call(domain, service, service_data=None, **_):
            if service == "set_hvac_mode":
                raise _HAError("Bridge did not answer")
            calls.append(dict(service_data))

        ent.hass.services.async_call = call
        _feed_off(ent, room=19.0)
        _run(ent.async_set_hvac_mode(_climate.HVACMode.HEAT))
        assert ent.hvac_mode == _climate.HVACMode.HEAT
        assert len(calls) == 1 and calls[0]["hvac_mode"] == _climate.HVACMode.HEAT


def test_trv_attributes_reach_the_coordinator_reading():
    """The entity picks up limits and off state from the coordinator."""
    ent = _entity()
    _feed_off(ent, room=19.0, internal=18.5)
    trv = ent._active_trv()
    assert (trv.entity_id, trv.off, trv.min_c, trv.max_c, trv.setpoint_c) == (
        TRV, True, 7.0, 35.0, None)
