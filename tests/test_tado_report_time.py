"""The valve's own report time and the refresh after a command (2.19.0).

A Tado zone's heating sensor carries the valve's own report time (``time``).
2 Nov 2026, 16 offs from 100 % in four zones: each valve reported under 20 %
2-10 s after the off on Tado's clock, and HA saw it 5-178 s later, at the
next poll.  So with the valve's time the lost-off check waits 3 min, not 8;
the look-ahead judges its answers by it; and an option asks for one refresh
of a cloud TRV half a minute after a command (its answer then shows at once).
"""
from __future__ import annotations

import datetime as dt
import logging
import sys

import pytest

from tests.ha_harness import _CALL_LATER, TRV, _run, _start, e2e, reset_timers
from tests.pure_modules import climate_controllers as cc
from tests.pure_modules import parameters as P
from tests.tado_plant import POWER, Clock, Plant, cycle, is_heat, is_off, room_entity

UTC = dt.UTC


def _iso(ts: float) -> str:
    """Tado's format: UTC with milliseconds and a Z."""
    return dt.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# The report time
# ---------------------------------------------------------------------------

class TestTadoReportTs:
    NOW = 1_793_600_000.0

    def test_tados_format(self):
        assert cc.tado_report_ts(_iso(self.NOW - 61.24), self.NOW, max_ahead_s=120) == \
            pytest.approx(self.NOW - 61.24, abs=0.001)

    def test_an_offset_or_a_datetime(self):
        when = dt.datetime.fromtimestamp(self.NOW - 30, UTC)
        assert cc.tado_report_ts(when.isoformat(), self.NOW, max_ahead_s=120) == pytest.approx(
            self.NOW - 30)
        assert cc.tado_report_ts(when, self.NOW, max_ahead_s=120) == pytest.approx(self.NOW - 30)

    @pytest.mark.parametrize("value", [
        None, "", "  ", "yesterday", 1793600000, "2026-11-02T10:00:00",   # no UTC offset
        dt.datetime(2026, 11, 2, 10, 0),                                  # naive
    ])
    def test_unusable_is_none(self, value):
        assert cc.tado_report_ts(value, self.NOW, max_ahead_s=120) is None

    def test_a_clock_out_of_step_is_not_used(self):
        assert cc.tado_report_ts(_iso(self.NOW + 119), self.NOW, max_ahead_s=120) is not None
        assert cc.tado_report_ts(_iso(self.NOW + 121), self.NOW, max_ahead_s=120) is None


# ---------------------------------------------------------------------------
# The lost-off rule
# ---------------------------------------------------------------------------

def _due(**kw):
    args = dict(off_ts=1000.0, off_taken=True, heat_since_off=False, confirm_pending=False,
                heating_pct=100.0, reported_ts=1000.0, resends=0,
                after_s=P.LOST_OFF_AFTER_S, min_pct=P.LOST_OFF_MIN_PCT,
                max_resends=P.LOST_OFF_MAX_RESENDS,
                after_tado_s=P.LOST_OFF_AFTER_TADO_S,
                reported_after_s=P.LOST_OFF_REPORTED_AFTER_S)
    args.update(kw)
    return cc.lost_off_due(**args)


class TestLostOff:
    def test_with_the_valves_time_three_minutes(self):
        # The valve's last report is from before the off: no closing report.
        assert not _due(reported_ts=1179.0, valve_report_ts=990.0)
        assert _due(reported_ts=1180.0, valve_report_ts=990.0)

    def test_a_report_the_valve_made_after_the_off_saying_it_heats(self):
        assert not _due(reported_ts=1059.0, valve_report_ts=1059.0)
        assert _due(reported_ts=1065.0, valve_report_ts=1060.0)

    def test_without_it_eight_minutes(self):
        assert not _due(reported_ts=1000.0 + 479.0)
        assert _due(reported_ts=1000.0 + 480.0)
        assert not _due(reported_ts=1300.0, valve_report_ts=None)

    def test_a_valve_that_closed_is_no_lost_off(self):
        assert not _due(reported_ts=1300.0, valve_report_ts=1008.0, heating_pct=0.0)

    def test_the_other_conditions_still_hold(self):
        for kw in ({"off_taken": False}, {"heat_since_off": True}, {"confirm_pending": True},
                   {"resends": 2}, {"heating_pct": 19.0}):
            assert not _due(reported_ts=1300.0, valve_report_ts=990.0, **kw), kw


# ---------------------------------------------------------------------------
# The refresh planner
# ---------------------------------------------------------------------------

class TestRefreshPlanner:
    def _planner(self):
        return cc.RefreshPlanner(30.0, 60.0, 15.0)

    def test_one_command_waits_the_delay(self):
        rp = self._planner()
        assert rp.request(100.0, "climate.a") == 30.0
        assert rp.take(130.0) == (("climate.a",), None) and rp.pending == ()

    def test_a_burst_shares_one_refresh_after_its_last_command(self):
        rp = self._planner()
        rp.request(100.0, "climate.b")
        assert rp.request(115.0, "climate.a") == 30.0         # at 145
        assert rp.request(140.0, "climate.c") == 20.0         # capped at 160
        # climate.c was commanded 20 s before: old enough to have answered.
        assert rp.take(160.0) == (("climate.a", "climate.b", "climate.c"), None)

    def test_one_commanded_just_before_the_refresh_waits_for_the_next(self):
        # Review: a command 55 s into a batch was refreshed 5 s later,
        # before the valve could answer (6-13 s), and then forgotten.
        rp = self._planner()
        rp.request(100.0, "climate.b")
        rp.request(125.0, "climate.a")
        assert rp.request(155.0, "climate.c") == 5.0          # the cap: 160
        due, again = rp.take(160.0)
        assert due == ("climate.a", "climate.b") and again == pytest.approx(25.0)
        assert rp.pending == ("climate.c",)
        assert rp.take(185.0) == (("climate.c",), None)

    def test_a_clock_that_went_back_starts_afresh(self):
        rp = self._planner()
        rp.request(1000.0, "climate.a")
        assert rp.request(500.0, "climate.b") == 30.0
        assert rp.take(530.0) == (("climate.a", "climate.b"), None)

    def test_clear(self):
        rp = self._planner()
        rp.request(100.0, "climate.a")
        rp.clear()
        assert rp.pending == () and rp.take(200.0) == ((), None)


class TestHeatingReport:
    NOW = 1_793_600_000.0

    def test_a_tado_zone(self):
        out = cc.heating_report("100.0", {"time": _iso(self.NOW - 70)}, changed_ts=self.NOW - 50,
                                reported_ts=self.NOW, from_tado=True, max_ahead_s=20)
        assert out == {"heating_power": 100.0, "heating_power_ts": self.NOW - 50,
                       "heating_power_reported_ts": self.NOW,
                       "heating_power_valve_ts": pytest.approx(self.NOW - 70, abs=0.01)}

    def test_another_sensor_with_a_time_attribute_is_not_trusted_for_it(self):
        out = cc.heating_report("40", {"time": _iso(self.NOW - 70)}, changed_ts=self.NOW,
                                reported_ts=self.NOW, from_tado=False, max_ahead_s=20)
        assert out["heating_power"] == 40.0 and out["heating_power_valve_ts"] is None

    def test_a_clock_out_of_step(self):
        # Review: at 120 s a Tado clock 90 s ahead made a report from just
        # before an off read as made after it; that report time is not used.
        out = cc.heating_report("100", {"time": _iso(self.NOW + 90)}, changed_ts=self.NOW,
                                reported_ts=self.NOW, from_tado=True,
                                max_ahead_s=P.REPORT_TS_MAX_AHEAD_S)
        assert out["heating_power_valve_ts"] is None
        # ... so the lost-off check falls back to its 8 minutes.
        assert not _due(off_ts=self.NOW - 70, reported_ts=self.NOW,
                        valve_report_ts=out["heating_power_valve_ts"])

    @pytest.mark.parametrize("value", [None, "unknown", "unavailable", "-1", "101", "nan"])
    def test_an_unusable_value(self, value):
        out = cc.heating_report(value, {"time": _iso(self.NOW)}, changed_ts=self.NOW,
                                reported_ts=self.NOW, from_tado=True, max_ahead_s=20)
        assert set(out.values()) == {None}


# ---------------------------------------------------------------------------
# Through the entity
# ---------------------------------------------------------------------------

class _PolledPlant(Plant):
    """A Tado zone as Home Assistant sees it: polled every ``poll_s``, each
    poll bringing the cloud's state as it is then, with the valve's own time
    of its last report (``valve_report_ts``).  ``keeps_heating``: the valve
    took a 25 °C and drops the off after it, while the zone shows off (25
    Oct 2026).  ``re_reports``:
    it says so again 10 s before each poll (else only on a change)."""

    def __init__(self, ent, clock, *, poll_s=180.0, keeps_heating=False, re_reports=False,
                 **kw):
        super().__init__(ent, clock, **kw)
        self.poll_s, self.keeps_heating, self.re_reports = poll_s, keeps_heating, re_reports
        self.polled_ts = clock.now
        self.polled = (0.0, clock.now)

    def _commands(self) -> None:
        if self.keeps_heating and self.heat_cmd:
            calls = self.ent.hass.service_calls
            # The zone takes the off and shows it; the valve keeps heating.
            if any(is_off(c) for c in calls[self.seen:]):
                self.told_off = True
            self.seen = len(calls)
            return
        super()._commands()

    def _trv(self) -> dict:
        if getattr(self, "told_off", False):
            return {"tado_setpoint": None, "tado_off": True}
        return super()._trv()

    def feed(self) -> None:
        if self.t - self.polled_ts >= self.poll_s - 1e-6:
            valve_ts = self.t - 10.0 if self.re_reports else self.power_ts
            self.polled_ts, self.polled = self.t, (self.power, valve_ts)
        super().feed()
        power, valve_ts = self.polled
        self.ent.coordinator.data.update({
            "heating_power": power, "heating_power_ts": self.polled_ts,
            "heating_power_reported_ts": self.polled_ts,
            "heating_power_valve_ts": valve_ts, "tado_heating": power > 0,
        })


@pytest.fixture(autouse=True)
def _timers():
    reset_timers()
    yield
    reset_timers()


def _room(monkeypatch, *, options=None, room=19.4, **kw):
    clock = Clock(monkeypatch)
    ent = room_entity({"heating_power_entity_id": POWER, **(options or {})})
    plant = _PolledPlant(ent, clock, room=room, **kw)
    plant.feed()
    _run(_start(ent))
    return ent, clock, plant


def _minutes(ent, clock, plant, n):
    for _ in range(n):
        clock.tick(60.0)
        plant.advance(60.0)
        plant.feed()
        cycle(ent)


def _until_resend(ent, clock, plant, limit=240):
    """Minutes until the off after the heat-up is sent again: (the first
    off's time, the resend's time or None)."""
    off_ts = None
    for _ in range(limit):
        _minutes(ent, clock, plant, 1)
        offs = [c for c in ent.hass.service_calls if is_off(c)]
        if offs and off_ts is None:
            off_ts, first = clock.now, len(offs)
        if off_ts is not None and len(offs) > first:
            return off_ts, clock.now
    return off_ts, None


@e2e
class TestLostOffThroughTheEntity:
    def test_a_dropped_off_is_sent_again_within_three_to_six_minutes(self, monkeypatch, caplog):
        ent, clock, plant = _room(monkeypatch, keeps_heating=True)
        with caplog.at_level(logging.WARNING):
            off_ts, resent_ts = _until_resend(ent, clock, plant)
        assert off_ts is not None, "the heat-up ended with an off"
        assert resent_ts is not None, "the dropped off was sent again"
        # The first poll 3 min or more after the off (polls every 3 min).
        assert P.LOST_OFF_AFTER_TADO_S <= resent_ts - off_ts <= P.LOST_OFF_AFTER_TADO_S + 180.0
        assert any("still reports heating" in r.getMessage() for r in caplog.records)

    def test_a_valve_that_says_it_heats_after_the_off_gets_it_again_at_once(self, monkeypatch):
        ent, clock, plant = _room(monkeypatch, keeps_heating=True, re_reports=True, poll_s=60.0)
        off_ts, resent_ts = _until_resend(ent, clock, plant)
        assert resent_ts is not None
        assert resent_ts - off_ts < P.LOST_OFF_AFTER_TADO_S

    def test_without_the_valves_time_it_waits_eight_minutes(self, monkeypatch):
        ent, clock, plant = _room(monkeypatch, keeps_heating=True)
        real_feed = plant.feed

        def feed():
            real_feed()
            ent.coordinator.data["heating_power_valve_ts"] = None
        plant.feed = feed
        off_ts, resent_ts = _until_resend(ent, clock, plant)
        assert resent_ts is not None
        assert P.LOST_OFF_AFTER_S <= resent_ts - off_ts <= P.LOST_OFF_AFTER_S + 180.0

    def test_a_valve_that_closed_gets_its_off_once(self, monkeypatch, caplog):
        ent, clock, plant = _room(monkeypatch)
        with caplog.at_level(logging.WARNING):
            _minutes(ent, clock, plant, 240)
        heats = [c for c in ent.hass.service_calls if is_heat(c)]
        offs = [c for c in ent.hass.service_calls if is_off(c)]
        assert heats and len(offs) == len(heats), (len(heats), len(offs))
        assert not [r for r in caplog.records if "still reports heating" in r.getMessage()]


@e2e
def test_the_lookahead_judges_an_answer_by_the_valves_time(monkeypatch):
    """Made 7 min after the release, seen by Home Assistant at 10 min: an
    answer (with Home Assistant's own time it would be outside the window)."""
    from tests.test_lookahead_entity import ON as LA_ON
    from tests.test_lookahead_entity import _Clock as LaClock
    from tests.test_lookahead_entity import _entity as la_entity
    from tests.test_lookahead_entity import _feed, _release, _slide

    clock = LaClock(monkeypatch)
    ent = la_entity(LA_ON)
    internal = _slide(ent, clock)
    released_at = clock.now
    _release(ent, clock, internal)
    assert ent._lookahead.observing
    for minute in range(1, 11):
        clock.now = released_at + 60.0 * minute
        _feed(ent, 20.35, internal, off=False, setpoint=19.5, power=0.0,
              power_ts=released_at - 30)
        if minute == 10:
            ent.coordinator.data.update({"heating_power": 45.0, "heating_power_ts": clock.now,
                                         "heating_power_valve_ts": released_at + 420.0})
        cycle(ent)
    (release,) = ent._lookahead.releases
    assert release.response_pct == 45.0


# ---------------------------------------------------------------------------
# The refresh after a command
# ---------------------------------------------------------------------------

def _refresh_timers():
    return [c for c in _CALL_LATER
            if not c["cancelled"] and getattr(c["callback"], "__name__", "") == "_async_fire"]


def _fire(entry):
    entry["cancelled"] = True
    _run(entry["callback"](None))


def _updates(hass):
    return [d for dom, svc, d in hass.service_calls
            if dom == "homeassistant" and svc == "update_entity"]


def _commanded(options, *, cloud=True, entity_id=TRV, ent=None):
    ent = ent or room_entity({"heating_power_entity_id": POWER, **options})
    ent._config_entry.data["source_entity_id"] = entity_id
    ent.coordinator.data = {"tado_cloud": cloud}
    _run(ent._async_call_trv(entity_id, "set_hvac_mode", {"hvac_mode": "off"}))
    return ent


ON = {"refresh_after_command": True}


@e2e
class TestRefreshAfterCommand:
    def test_one_refresh_half_a_minute_after_a_command(self, monkeypatch):
        clock = Clock(monkeypatch)
        ent = _commanded(ON)
        timers = _refresh_timers()
        assert len(timers) == 1 and timers[0]["delay"] == pytest.approx(P.COMMAND_REFRESH_DELAY_S)
        clock.tick(P.COMMAND_REFRESH_DELAY_S)
        _fire(timers[0])
        assert _updates(ent.hass) == [{"entity_id": [TRV]}]
        assert not _refresh_timers()

    @pytest.mark.parametrize("options, cloud", [
        ({}, True),                                    # the option is off by default
        ({"refresh_after_command": False}, True),
        (ON, False),                                   # a HomeKit valve reports itself
    ])
    def test_no_refresh(self, options, cloud):
        _commanded(options, cloud=cloud)
        assert not _refresh_timers()

    def test_two_rooms_share_one_refresh(self, monkeypatch):
        clock = Clock(monkeypatch)
        a = _commanded(ON, entity_id="climate.a")
        clock.tick(20.0)
        b = room_entity(ON)
        b.hass = a.hass                                  # one Home Assistant
        _commanded(ON, entity_id="climate.b", ent=b)
        timers = _refresh_timers()
        assert len(timers) == 1 and timers[0]["delay"] == pytest.approx(30.0)
        clock.tick(30.0)
        _fire(timers[0])
        assert _updates(a.hass) == [{"entity_id": ["climate.a", "climate.b"]}]

    def test_one_commanded_just_before_the_cap_gets_its_own(self, monkeypatch):
        clock = Clock(monkeypatch)
        a = _commanded(ON, entity_id="climate.a")
        b = room_entity(ON)
        b.hass = a.hass
        clock.tick(25.0)                                 # a again at 25 s
        _commanded(ON, entity_id="climate.a", ent=a)
        clock.tick(30.0)                                 # 55 s: b, 5 s before the 60-s cap
        _commanded(ON, entity_id="climate.b", ent=b)
        clock.now += _refresh_timers()[0]["delay"]
        _fire(_refresh_timers()[0])
        assert _updates(a.hass) == [{"entity_id": ["climate.a"]}]
        (again,) = _refresh_timers()
        clock.tick(again["delay"])
        _fire(again)
        assert _updates(a.hass)[-1] == {"entity_id": ["climate.b"]}

    def test_a_timed_out_command_is_refreshed_another_failure_not(self, monkeypatch):
        for err, refreshed in ((TimeoutError(), True), (RuntimeError("refused"), False)):
            reset_timers()
            ent = room_entity(ON)
            ent.coordinator.data = {"tado_cloud": True}

            async def failing(domain, service, service_data=None, blocking=False, *, _e=err, **_):
                raise _e

            ent.hass.services.async_call = failing
            assert not _run(ent._async_call_trv(TRV, "set_hvac_mode", {"hvac_mode": "off"}))
            assert bool(_refresh_timers()) is refreshed, err

    def test_the_refresh_does_not_block_and_a_failure_is_logged_once(self, monkeypatch, caplog):
        clock = Clock(monkeypatch)
        ent = _commanded(ON)
        seen = []

        async def refused(domain, service, service_data=None, blocking=False, **_):
            seen.append(blocking)
            raise RuntimeError("no such entity")

        ent.hass.services.async_call = refused
        with caplog.at_level(logging.WARNING):
            for _ in range(2):
                clock.tick(30.0)
                _fire(_refresh_timers()[0])
                ent.hass.services.async_call = refused
                COMMAND_REFRESHER().request(ent.hass, TRV, clock.now)
        assert seen == [False, False]
        assert len([r for r in caplog.records if "after a command failed" in r.getMessage()]) == 1

    def test_nothing_while_home_assistant_stops(self, monkeypatch):
        clock = Clock(monkeypatch)
        ent = _commanded(ON)
        ent.hass.is_stopping = True
        clock.tick(30.0)
        _fire(_refresh_timers()[0])
        assert not _updates(ent.hass)

    def test_cancel_forgets_it(self):
        _commanded(ON)
        (timer,) = _refresh_timers()
        COMMAND_REFRESHER().cancel()
        assert timer["cancelled"] and COMMAND_REFRESHER().planner.pending == ()

    def test_a_regulation_command_asks_for_it(self, monkeypatch):
        ent, clock, plant = _room(monkeypatch, options=ON)
        _minutes(ent, clock, plant, 3)
        assert [c for c in ent.hass.service_calls if is_heat(c)], "a heat-up started"
        assert _refresh_timers()


def COMMAND_REFRESHER():  # noqa: N802 - the module-level singleton, looked up late
    return sys.modules["roomstat_e2e.climate_regulation"].COMMAND_REFRESHER


def test_the_harness_resets_the_refresher():
    assert COMMAND_REFRESHER().planner.pending == ()
