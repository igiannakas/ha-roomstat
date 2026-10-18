"""Fail cold, never hot, through the real climate entity (tests/ha_harness.py).

* Maximum commands to a cloud TRV are Tado timer overlays when
  ``heat_timer_s`` > 0, renewed at half life while still the maximum.
* A TRV back on its schedule (5 °C or Off) does not hold our command: a
  heat command is sent again (as a timer), a frost-level one is not.
* The out-of-band sensor, with its eligibility conditions.
* The watchdog shuts a valve the regulation cycle stopped looking after.
"""
from __future__ import annotations

import datetime
import logging

import pytest

from tests.ha_harness import (
    SUMMER,
    TRV,
    WINDOW,
    _binary_sensor,
    _climate,
    _make_entity,
    _restart_from,
    _run,
    _start,
    e2e,
    reset_timers,
)

pytestmark = e2e

HVACMode = _climate.HVACMode if _climate is not None else None
TIMER = 3600.0


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


def _feed(ent, room, internal, setpoint, *, cloud=True, scheduled=False, off=False,
          heating=None):
    ent.coordinator.data = {
        "room_temp": room, "room_temp_ts": None,
        "tado_internal_temp": internal, "tado_setpoint": setpoint,
        "tado_min_temp": 5.0, "tado_max_temp": 25.0, "tado_off": off,
        "tado_cloud": cloud, "tado_scheduled": scheduled, "tado_heating": heating,
        "fallback_internal_temp": None, "fallback_setpoint": None,
    }


def _entity(options=None, **kwargs):
    ent = _make_entity({"heat_timer_s": TIMER, **(options or {})}, **kwargs)
    _run(_start(ent))
    return ent


def _cycle(ent):
    _run(ent._async_regulation_cycle("timer"))


def _timers(ent):
    return [d for dom, svc, d in ent.hass.service_calls
            if dom == "tado" and svc == "set_climate_timer"]


def _temps(ent):
    return [d["temperature"] for dom, svc, d in ent.hass.service_calls
            if dom == "climate" and svc == "set_temperature"]


def _heat_up(ent, clock):
    """Comfort 20.5 with the room at 18.0: a heat-up starts, and the valve
    shows the 25 °C (the command's own refresh)."""
    _feed(ent, 18.0, 9.0, 8.0)
    _cycle(ent)
    assert ent._reg_state.mode == "heat"
    _feed(ent, 18.0, 9.0, 25.0)


def _stay_cold(ent, clock, minutes, *, setpoint=25.0, **feed):
    """The room stays 2.5 °C short: heat mode throughout."""
    for _ in range(minutes):
        clock.tick(60)
        _feed(ent, 18.0, 9.0, setpoint, **feed)
        _cycle(ent)


# ---------------------------------------------------------------------------
# Timer overlays
# ---------------------------------------------------------------------------

class TestTimerOverlay:

    def test_heat_up_is_a_timer_on_a_cloud_trv(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        assert _timers(ent) == [
            {"entity_id": TRV, "temperature": 25.0, "time_period": "01:00:00"}
        ]
        assert _temps(ent) == []
        assert ent.extra_state_attributes["heat_timer_s"] == TIMER

    def test_heat_up_end_is_a_manual_5c(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        room = 18.0
        for _ in range(120):
            clock.tick(60)
            room += 2.0 / 60
            _feed(ent, room, 15.0, 25.0)
            _cycle(ent)
            if ent._reg_state.mode != "heat":
                break
        assert ent._reg_state.mode == "coast"
        assert _temps(ent) == [5.0]
        set_temperature = [d for dom, svc, d in ent.hass.service_calls
                           if svc == "set_temperature"][0]
        assert set_temperature["hvac_mode"] == HVACMode.HEAT
        assert ent._heat_timer_sent_ts is None

    def test_off_by_default(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _make_entity()
        _run(_start(ent))
        _heat_up(ent, clock)
        _stay_cold(ent, clock, 90)
        assert _timers(ent) == []
        assert _temps(ent) == [25.0]

    def test_not_on_a_homekit_trv(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, 18.0, 9.0, 8.0, cloud=False)
        _cycle(ent)
        _stay_cold(ent, clock, 90, cloud=False)
        assert _timers(ent) == []
        assert _temps(ent) == [25.0]

    def test_renewed_at_half_life_while_heating(self, monkeypatch):
        """One extra command per heat_timer_s / 2: at 30, 60, 90, 120 min."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        _stay_cold(ent, clock, 29)
        assert len(_timers(ent)) == 1
        _stay_cold(ent, clock, 1)
        assert len(_timers(ent)) == 2
        assert ent._last_reason == "sent(heat_timer_refresh)"
        _stay_cold(ent, clock, 90)
        assert len(_timers(ent)) == 5                     # 1 + 120 min / 30 min
        assert _temps(ent) == []

    def test_renewed_for_a_hold_clamped_at_the_maximum(self, monkeypatch):
        """Without the negative offsets the hold can ask for 25 too (here
        20.5 + 2.2 + 4 x 0.7 = 25.5, clamped): the same timer, renewed."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, 19.8, 22.0, 20.0)
        _cycle(ent)
        assert ent._reg_state.mode == "hold"
        assert _timers(ent) == [
            {"entity_id": TRV, "temperature": 25.0, "time_period": "01:00:00"}
        ]
        for _ in range(30):
            clock.tick(60)
            _feed(ent, 19.8, 22.0, 25.0)
            _cycle(ent)
        assert ent._reg_state.mode == "hold"
        assert len(_timers(ent)) == 2

    def test_no_timer_and_no_renewal_below_the_maximum(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, 20.3, 18.0, 20.0)
        _cycle(ent)
        assert ent._reg_state.mode == "hold"
        reasons = []
        for _ in range(90):
            clock.tick(60)
            _feed(ent, 20.3, 18.0, _temps(ent)[-1])
            _cycle(ent)
            reasons.append(ent._last_reason)
        assert _timers(ent) == []
        assert all(5.0 < t < 25.0 for t in _temps(ent))   # the hold's fine commands
        assert "sent(heat_timer_refresh)" not in reasons

    def test_renewal_skips_the_rate_limit_once(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        # A command 60 s ago, and the timer half gone: still renewed now.
        ent._heat_timer_sent_ts = clock.now - TIMER / 2
        ent._last_command_sent_ts = clock.now - 60
        clock.tick(0)
        _feed(ent, 18.0, 9.0, 25.0)
        _cycle(ent)
        assert ent._last_reason == "sent(heat_timer_refresh)"

    def test_failed_renewal_is_retried_once_per_interval(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        _stay_cold(ent, clock, 29)

        async def refuse(*_a, **_k):
            raise TimeoutError

        ent.hass.services.async_call = refuse
        _stay_cold(ent, clock, 1)
        assert ent._last_reason == "send_failed(heat_timer_refresh)"
        _stay_cold(ent, clock, 1)
        assert ent._last_reason.startswith("rate_limited")
        _stay_cold(ent, clock, 2)
        assert ent._last_reason == "send_failed(heat_timer_refresh)"
        ent.hass.services.async_call = ent.hass._async_call
        _stay_cold(ent, clock, 3)
        assert ent._last_reason == "sent(heat_timer_refresh)"

    def test_short_timer_is_raised_to_15_min(self, monkeypatch):
        """Renewed every half, each renewal skipping the rate limit once: a
        shorter timer would beat the 180 s limit."""
        clock = _Clock(monkeypatch)
        ent = _entity({"heat_timer_s": 300})
        assert ent._config.heat_timer_s == 900.0
        _heat_up(ent, clock)
        assert _timers(ent)[-1]["time_period"] == "00:15:00"

    def test_clock_going_back_renews(self, monkeypatch):
        """A timer sent "in the future" would not be renewed in time."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        clock.tick(-3600)
        _feed(ent, 18.0, 9.0, 25.0)
        _cycle(ent)
        assert ent._last_reason == "sent(heat_timer_refresh)"

    def test_reload_mid_heat_up_renews_once(self, monkeypatch):
        """After a reload (or enabling the timer, which reloads the room) the
        timer's start is unknown, so the maximum the valve holds is renewed
        straight away, once."""
        clock = _Clock(monkeypatch)
        ent = _make_entity({"heat_timer_s": TIMER})
        _feed(ent, 18.0, 9.0, 25.0)
        _run(_start(ent))
        assert ent._reg_state.mode == "heat"
        _cycle(ent)
        assert len(_timers(ent)) == 1
        assert ent._last_reason == "sent(heat_timer_refresh)"
        _stay_cold(ent, clock, 29)
        assert len(_timers(ent)) == 1


# ---------------------------------------------------------------------------
# A TRV back on its schedule
# ---------------------------------------------------------------------------

class TestBackOnSchedule:

    @pytest.mark.parametrize("schedule, reason", [
        (5.0, "sent(normal_update)"),      # compared with the 5 °C it holds
        (None, "sent(no_baseline)"),       # nothing to compare with
    ], ids=["5C", "Off"])
    def test_expired_timer_is_sent_again_as_a_timer(self, monkeypatch, schedule, reason):
        """HA was too busy to renew: the timer ran out and the zone went back
        to its schedule (auto at 5 °C, or auto without a setpoint for Off).
        The drift check sends it again (a renewal would say so in the
        reason)."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        clock.tick(TIMER + 60)
        _feed(ent, 18.0, 9.0, schedule, scheduled=True)
        _cycle(ent)
        assert ent._last_reason == reason
        assert len(_timers(ent)) == 2
        assert _timers(ent)[-1]["temperature"] == 25.0
        assert _temps(ent) == []

    @pytest.mark.parametrize("schedule", [5.0, None], ids=["5C", "Off"])
    def test_overlay_ended_in_the_app_is_sent_again(self, monkeypatch, schedule):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        _stay_cold(ent, clock, 5)
        _feed(ent, 18.0, 9.0, schedule, scheduled=True)   # ended in the app
        clock.tick(60)
        _cycle(ent)                                      # 6 min: the cloud grace
        assert len(_timers(ent)) == 2

    def test_heat_command_without_timer_is_sent_again_manual(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _make_entity()
        _run(_start(ent))
        _heat_up(ent, clock)
        clock.tick(400)
        _feed(ent, 18.0, 9.0, None, scheduled=True)
        _cycle(ent)
        assert _temps(ent) == [25.0, 25.0]

    @pytest.mark.parametrize("schedule", [5.0, None], ids=["5C", "Off"])
    def test_frost_level_command_to_a_shut_schedule_is_not_sent(self, monkeypatch, schedule):
        """Room above the target: the firm stop asks for 5 °C, which the
        schedule already does."""
        _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, 21.2, 15.0, schedule, scheduled=True)
        _cycle(ent)
        assert ent._reg_state.mode == "stop"
        assert ent._last_reason == "already_at_target"
        assert ent.hass.service_calls == []

    def test_frost_level_command_to_a_heating_schedule_is_sent(self, monkeypatch):
        """A schedule block at a real temperature heats: shut it."""
        _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, 21.2, 15.0, 20.0, scheduled=True)
        _cycle(ent)
        assert _temps(ent) == [5.0]


# ---------------------------------------------------------------------------
# Out-of-band sensor
# ---------------------------------------------------------------------------

def _band_entity(ent):
    ent.coordinator.climate_entity = ent
    return _binary_sensor.RoomstatOutOfBandBinarySensor(ent.coordinator, ent._config_entry)


def _minutes(ent, clock, minutes, room, *, internal=15.0, setpoint=None, heating=None):
    for _ in range(minutes):
        clock.tick(60)
        _feed(ent, room, internal, setpoint, heating=heating)
        _cycle(ent)


class TestOutOfBandEntity:

    def test_cold_after_two_hours(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        band = _band_entity(ent)
        _feed(ent, 19.9, 15.0, None)
        _cycle(ent)
        start = clock.now
        _minutes(ent, clock, 119, 19.9, setpoint=ent._last_sent_setpoint)
        assert band.is_on is False
        _minutes(ent, clock, 1, 19.9, setpoint=ent._last_sent_setpoint)
        assert band.is_on is True
        attrs = band.extra_state_attributes
        assert attrs["reason"] == "cold"
        assert attrs["deviation_c"] == pytest.approx(-0.6)
        assert attrs["since"] == datetime.datetime.fromtimestamp(
            start, tz=datetime.UTC).isoformat()

    def test_hot_after_30_min_above_the_band(self, monkeypatch):
        """The valve says it is heating (hvac_action): the radiator is the
        cause, so it is an alarm."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        band = _band_entity(ent)
        _feed(ent, 20.5, 15.0, None, heating=True)
        _cycle(ent)
        _minutes(ent, clock, 30, 21.0, heating=True)
        assert band.is_on is False
        _minutes(ent, clock, 1, 21.0, heating=True)
        assert band.is_on is True
        attrs = band.extra_state_attributes
        assert attrs["reason"] == "hot" and attrs["radiator_heated"] is True

    def test_warm_with_the_valve_shut_is_not_an_alarm(self, monkeypatch, caplog):
        """The 16 Oct afternoon: the room climbs with the valve at 0 % and
        its reading following the room (the sun)."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        band = _band_entity(ent)
        _feed(ent, 20.5, 10.4, None, heating=False)
        _cycle(ent)
        caplog.set_level(logging.INFO)
        _minutes(ent, clock, 31, 21.0, internal=10.9, heating=False)
        assert band.is_on is False
        attrs = band.extra_state_attributes
        assert attrs["reason"] == "warm" and attrs["radiator_heated"] is False
        assert attrs["since"] is not None
        assert not any(r.levelno == logging.WARNING and "out of band" in r.getMessage()
                       for r in caplog.records)
        assert any("radiator cold" in r.getMessage() for r in caplog.records)

    def test_hot_when_the_trv_reading_climbs_with_the_valve_silent(self, monkeypatch):
        """No hvac_action from the entity, but the TRV's own reading climbs
        far more than the room: a hot radiator."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        band = _band_entity(ent)
        _feed(ent, 20.5, 10.4, None)
        _cycle(ent)
        _minutes(ent, clock, 31, 21.0, internal=14.0)
        assert band.is_on is True
        assert band.extra_state_attributes["reason"] == "hot"

    def test_night_setback_never_fires(self, monkeypatch):
        """Comfort 20.5 -> night 18.5 with the room cooling 0.3 °C/h."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        band = _band_entity(ent)
        _feed(ent, 20.5, 15.0, None)
        _cycle(ent)
        _run(ent.async_set_preset_mode("eco"))
        room = 20.5
        for _ in range(8 * 60):
            clock.tick(60)
            room -= 0.3 / 60
            _feed(ent, room, 15.0, 5.0)
            _cycle(ent)
            assert band.is_on is False

    @pytest.mark.parametrize("condition, expected", [
        ("none", True),                  # the control: 1.5 °C short for 200 min
        ("off", False), ("summer", False), ("window_sensor", False),
        ("window_mode", False), ("sensor_lost", False),
    ])
    def test_judged_only_while_heating_normally(self, monkeypatch, condition, expected):
        clock = _Clock(monkeypatch)
        ent = _make_entity({"heat_timer_s": TIMER}, summer=True)
        ent.hass.set(SUMMER, "on" if condition == "summer" else "off")
        ent.hass.set(WINDOW, "on" if condition == "window_sensor" else "off")
        _run(_start(ent))
        band = _band_entity(ent)
        if condition == "off":
            _run(ent.async_set_hvac_mode(HVACMode.OFF))
        if condition == "window_mode":
            ent._window_ctrl.activate("comfort", None)
        room = None if condition == "sensor_lost" else 19.0
        _minutes(ent, clock, 200, room)
        assert band.is_on is expected
        if not expected:
            assert ent._band_monitor.state.since is None

    def test_low_is_kept_across_a_restart(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, 20.5, 15.0, None)
        _cycle(ent)
        _run(ent.async_set_preset_mode("eco"))
        _minutes(ent, clock, 10, 19.6)
        saved = ent.extra_restore_state_data.as_dict()["out_of_band"]
        assert saved == {"min_since": 19.6, "setpoint": 18.5}
        again = _make_entity({"heat_timer_s": TIMER})
        _restart_from(ent, again)
        assert again._band_monitor.min_since == 19.6


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

def _watchdog(ent):
    _run(ent._async_watchdog_check())


class TestWatchdog:

    def test_runs_on_its_own_timer(self, monkeypatch):
        registered = []

        def track(_hass, callback, interval):
            registered.append((callback.__name__, interval.total_seconds()))
            return lambda: None

        monkeypatch.setattr(_climate, "async_track_time_interval", track)
        ent = _make_entity()
        _run(_start(ent))
        assert ("_async_regulation_cycle_timer", 60.0) in registered
        assert ("_async_watchdog_check", 60.0) in registered

    def test_shuts_a_valve_left_at_the_maximum_once(self, monkeypatch, caplog):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        clock.tick(599)
        _watchdog(ent)
        assert ent.extra_state_attributes["regulation_stalled"] is False
        assert _temps(ent) == []
        caplog.set_level(logging.WARNING)
        clock.tick(1)
        _watchdog(ent)
        assert ent.extra_state_attributes["regulation_stalled"] is True
        assert _temps(ent) == [5.0]
        assert ent._last_reason == "sent(watchdog)"
        for _ in range(20):
            clock.tick(60)
            _watchdog(ent)
        assert _temps(ent) == [5.0]
        stalls = [r for r in caplog.records if "no regulation cycle" in r.getMessage()]
        assert len(stalls) == 1

    def test_nothing_to_shut_below_the_maximum(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, 20.3, 18.0, 20.0)
        _cycle(ent)
        sent = list(ent.hass.service_calls)
        clock.tick(900)
        _watchdog(ent)
        assert ent.extra_state_attributes["regulation_stalled"] is True
        assert ent.hass.service_calls == sent

    def test_cycles_that_end_early_do_not_count(self, monkeypatch):
        """The coordinator update keeps failing: every cycle stops early."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        ent.coordinator.last_update_success = False
        _feed(ent, 18.0, 9.0, 25.0)
        for _ in range(10):
            clock.tick(60)
            _cycle(ent)
            assert ent._last_reason in ("coordinator_unavailable", "sent(watchdog)")
            _watchdog(ent)
        assert ent._last_reason == "sent(watchdog)"
        assert _temps(ent) == [5.0]

    def test_an_unreachable_valve_is_not_reported_as_shut(self, monkeypatch):
        """Home Assistant skips a service call to an unavailable entity
        without an error: wait until the valve gives a reading again."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        _feed(ent, 18.0, None, None)                   # valve unavailable
        for _ in range(12):
            clock.tick(60)
            _cycle(ent)
            _watchdog(ent)
        assert ent.extra_state_attributes["regulation_stalled"] is True
        assert _temps(ent) == []
        assert ent._last_sent_was_max is True
        ent.coordinator.last_update_success = False    # back, cycles still failing
        _feed(ent, 18.0, 9.0, 25.0)
        clock.tick(60)
        _watchdog(ent)
        assert _temps(ent) == [5.0]

    @pytest.mark.parametrize("held", ["off", "off_schedule", "lower"])
    def test_a_valve_no_longer_at_the_maximum_is_left_alone(self, monkeypatch, held):
        """Shut from outside (a HomeKit valve that is off would be turned on by
        a 7 °C command), back on an Off schedule, or at a lower setpoint:
        there is no maximum to replace."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        ent.coordinator.last_update_success = False
        if held == "off":
            _feed(ent, 18.0, 9.0, None, off=True)
            ent.coordinator.data["tado_min_temp"] = 7.0
            ent.coordinator.data["tado_max_temp"] = 35.0
        elif held == "off_schedule":
            _feed(ent, 18.0, 9.0, None, scheduled=True)
        else:
            _feed(ent, 18.0, 9.0, 18.0)
        for _ in range(15):
            clock.tick(60)
            _cycle(ent)
            _watchdog(ent)
        assert ent.extra_state_attributes["regulation_stalled"] is True
        assert ent.hass.service_calls[1:] == []            # only the heat-up's timer
        assert ent._last_sent_was_max is False

    def test_failed_shut_is_retried_once_per_interval(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)

        async def refuse(*_a, **_k):
            raise TimeoutError

        ent.hass.services.async_call = refuse
        clock.tick(600)
        _watchdog(ent)
        assert ent._last_reason == "send_failed(watchdog)"
        attempts = ent._last_command_sent_ts
        clock.tick(120)
        _watchdog(ent)
        assert ent._last_command_sent_ts == attempts
        ent.hass.services.async_call = ent.hass._async_call
        clock.tick(60)
        _watchdog(ent)
        assert ent._last_reason == "sent(watchdog)"

    def test_a_completed_cycle_ends_the_stall(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _heat_up(ent, clock)
        clock.tick(600)
        _watchdog(ent)
        assert ent.extra_state_attributes["regulation_stalled"] is True
        clock.tick(60)
        _feed(ent, 18.0, 9.0, 5.0)
        _cycle(ent)
        assert ent.extra_state_attributes["regulation_stalled"] is False

    def test_summer_and_off_cycles_count(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, 20.0, 15.0, 20.0)
        _run(ent.async_set_hvac_mode(HVACMode.OFF))
        for _ in range(15):
            clock.tick(60)
            _feed(ent, 20.0, 15.0, None, off=True)
            _cycle(ent)
            _watchdog(ent)
        assert ent.extra_state_attributes["regulation_stalled"] is False


# ---------------------------------------------------------------------------
# Room sensor lost
# ---------------------------------------------------------------------------

class TestRoomSensorLost:
    """Without its sensor the room is not regulated (a deliberate pause the
    sensor-lost warning and the repair report); a maximum is never left in
    place meanwhile, and the watchdog does not report the pause again."""

    def _lose_sensor(self, ent, clock, minutes, *, internal=22.0, setpoint=25.0):
        for _ in range(minutes):
            clock.tick(60)
            _feed(ent, None, internal, setpoint)
            _cycle(ent)
            _watchdog(ent)

    def test_a_hold_clamped_at_the_maximum_is_ended_at_the_grace(self, monkeypatch):
        """No offsets: the hold asks for 25 (see the timer tests).  Once the
        5-min grace is over it is shut at once, not 10 min later by the
        watchdog."""
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, 19.8, 22.0, 20.0)
        _cycle(ent)
        assert ent._reg_state.mode == "hold" and ent._last_sent_was_max
        self._lose_sensor(ent, clock, 6)                     # bridged (300 s)
        assert _temps(ent) == []
        self._lose_sensor(ent, clock, 1)                     # past the grace
        assert _temps(ent) == [5.0]
        assert ent._last_reason.startswith("sent(")
        assert ent._reg_state.mode == "coast"
        self._lose_sensor(ent, clock, 20, setpoint=5.0)
        assert _temps(ent) == [5.0]
        assert ent.extra_state_attributes["regulation_stalled"] is False

    def test_an_ordinary_outage_is_not_a_stall(self, monkeypatch, caplog):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, 20.3, 18.0, 20.0)
        _cycle(ent)
        held = _temps(ent)[-1]
        assert held < 25.0
        caplog.set_level(logging.WARNING)
        self._lose_sensor(ent, clock, 30, internal=18.0, setpoint=held)
        assert ent._last_reason == "waiting_for_sensors"
        assert _temps(ent) == [held]                         # the valve keeps it
        assert ent.extra_state_attributes["regulation_stalled"] is False
        assert not [r for r in caplog.records if "no regulation cycle" in r.getMessage()]

    def test_without_a_trv_reading_it_is_a_stall(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _entity()
        _feed(ent, 20.3, 18.0, 20.0)
        _cycle(ent)
        for _ in range(12):
            clock.tick(60)
            _feed(ent, None, None, None)
            _cycle(ent)
            _watchdog(ent)
        assert ent._last_reason == "waiting_for_sensors"
        assert ent.extra_state_attributes["regulation_stalled"] is True


# ---------------------------------------------------------------------------
# Resuming after a restart when the TRV entity loads late
# ---------------------------------------------------------------------------

class TestResumeWithALateTrv:
    """18 Oct 2026, right after the 2.8.0 restart: the Tado entity had no
    state when the room was added, the resume defaulted to hold, and the
    first cycle (room 0.07 above the target) pushed the study's valve into
    its grey zone.  An unknown valve now resumes as stop, and the mode is
    decided again from the valve on the first cycle that sees it."""

    def _late(self, monkeypatch):
        _Clock(monkeypatch)
        ent = _make_entity({"heat_timer_s": 0})
        _run(_start(ent))                       # the coordinator has no TRV data yet
        assert ent._reg_state.mode == "stop"
        assert ent._resume_pending
        return ent

    def test_shut_valve_above_target_stays_shut(self, monkeypatch):
        ent = self._late(monkeypatch)
        _feed(ent, 20.57, 18.6, 5.0)            # comfort 20.5, room 0.07 over
        _cycle(ent)
        assert ent._reg_state.mode == "stop"
        assert _temps(ent) == []
        assert not ent._resume_pending

    def test_shut_valve_below_target_resumes_the_coast(self, monkeypatch):
        ent = self._late(monkeypatch)
        _feed(ent, 20.3, 18.6, 5.0)
        _cycle(ent)
        assert ent._reg_state.mode in ("coast", "hold")

    def test_open_valve_carries_on_the_heat_up(self, monkeypatch):
        ent = self._late(monkeypatch)
        _feed(ent, 18.5, 12.0, 25.0)
        _cycle(ent)
        assert ent._reg_state.mode == "heat"
        assert ent._last_sent_was_max
        assert _temps(ent) == []                # already at target

    def test_middle_setpoint_resumes_in_hold(self, monkeypatch):
        ent = self._late(monkeypatch)
        _feed(ent, 20.45, 18.0, 18.3)
        _cycle(ent)
        assert ent._reg_state.mode == "hold"

    def test_decided_once(self, monkeypatch):
        ent = self._late(monkeypatch)
        _feed(ent, 20.57, 18.6, 5.0)
        _cycle(ent)
        _feed(ent, 20.57, 18.6, 25.0)           # a stray reading later is not a resume
        _cycle(ent)
        assert ent._reg_state.mode == "stop"
        assert not ent._last_sent_was_max

    def test_known_valve_at_start_needs_no_second_look(self, monkeypatch):
        _Clock(monkeypatch)
        ent = _make_entity({"heat_timer_s": 0})
        _feed(ent, 18.5, 12.0, 25.0)
        _run(_start(ent))
        assert ent._reg_state.mode == "heat"
        assert not ent._resume_pending
