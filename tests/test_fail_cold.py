"""Fail cold, never hot: the HA-free parts and the simulator.

Every command used to be a Manual overlay: a maximum command left in place by
a dead Home Assistant, network or cloud kept the valve open (with the
negative valve offsets, 25 °C is a real 30-35 °C).  A maximum command to a
cloud TRV can now be a Tado timer overlay that falls back to the zone's
schedule (5 °C or Off); a TRV back on its schedule is recognised; a room
stuck out of band is reported; and a watchdog shuts a valve the regulation
cycle stopped looking after.  The Home Assistant side is in
test_fail_cold_entity.py.
"""
from __future__ import annotations

import pytest

from tests.plant_sim import (
    LIVING_ROOM_FIT,
    SECOND_BEDROOM_FIT,
    PlantParams,
    ProxySettings,
    RoomSim,
    schedule_from,
)
from tests.pure_modules import autotune as A
from tests.pure_modules import climate_controllers as C
from tests.pure_modules import parameters as P
from tests.pure_modules import regulation as R

DAY = 86400.0
SCHED = schedule_from([(0, 18.0), (6, 20.0), (21.5, 18.0)])
HOUR = 3600.0


# ---------------------------------------------------------------------------
# A TRV back on its schedule
# ---------------------------------------------------------------------------

class TestOnSchedule:

    def test_auto_is_the_schedule(self):
        """Tado smart schedule (no overlay): HA shows hvac mode auto."""
        assert C.trv_on_schedule("auto", 15.0, 5.0)

    def test_off_block_shows_auto_without_a_setpoint(self):
        """python-tado 0.18.16 sends no temperature for an Off block."""
        assert C.trv_on_schedule("auto", 15.0, None)

    def test_a_reading_without_a_setpoint_is_no_overlay_of_ours(self):
        assert C.trv_on_schedule("heat", 15.0, None)

    @pytest.mark.parametrize("state, internal, setpoint", [
        ("heat", 15.0, 25.0),        # our overlay
        ("off", 15.0, None),         # our OFF (an overlay too)
        ("unavailable", None, None),
        ("unknown", None, None),
        (None, None, None),
        ("heat", None, None),        # nothing known
    ])
    def test_not_on_schedule(self, state, internal, setpoint):
        assert not C.trv_on_schedule(state, internal, setpoint)

    def test_shut_without_setpoint(self):
        assert C.TrvReading("climate.x", 15.0, None, off=True).shut_without_setpoint
        assert C.TrvReading("climate.x", 15.0, None, scheduled=True).shut_without_setpoint
        # A 5 °C schedule block has a setpoint to compare with.
        assert not C.TrvReading("climate.x", 15.0, 5.0, scheduled=True).shut_without_setpoint
        assert not C.TrvReading("climate.x", 15.0, 25.0).shut_without_setpoint


class TestScheduledDrift:
    """The overlay is gone: a heat command is sent again, a frost-level one
    is still in effect while the schedule keeps the valve shut."""

    def test_heat_command_lost_to_an_off_schedule(self):
        assert C.trv_setpoint_drifted(25.0, None, 600, 360, 0.3, trv_scheduled=True)

    def test_heat_command_lost_to_a_5c_schedule(self):
        assert C.trv_setpoint_drifted(25.0, 5.0, 600, 360, 0.3, trv_scheduled=True)

    def test_any_heat_command_whatever_the_schedule_holds(self):
        """Even a schedule block close to the command: it is not ours, and
        moves with the schedule."""
        assert C.trv_setpoint_drifted(18.3, 18.2, 600, 360, 0.3, trv_scheduled=True)

    @pytest.mark.parametrize("schedule", [None, 5.0])
    def test_frost_command_held_by_a_shut_schedule(self, schedule):
        assert not C.trv_setpoint_drifted(5.0, schedule, 600, 360, 0.3, trv_scheduled=True)

    def test_frost_command_not_held_by_a_heating_schedule(self):
        """A schedule block at a real temperature heats: shut it again."""
        assert C.trv_setpoint_drifted(5.0, 20.0, 600, 360, 0.3, trv_scheduled=True)

    def test_not_before_the_grace(self):
        assert not C.trv_setpoint_drifted(25.0, None, 300, 360, 0.3, trv_scheduled=True)

    def test_unknown_setpoint_without_schedule_is_still_not_drift(self):
        assert not C.trv_setpoint_drifted(25.0, None, 600, 360, 0.3)


# ---------------------------------------------------------------------------
# Timer overlays and the watchdog's test
# ---------------------------------------------------------------------------

class TestHeatTimer:

    def test_only_the_maximum_to_a_cloud_trv(self):
        assert C.heat_timer_duration(25.0, 25.0, cloud=True, heat_timer_s=3600) == 3600
        assert C.heat_timer_duration(24.96, 25.0, cloud=True, heat_timer_s=3600) == 3600
        assert C.heat_timer_duration(24.9, 25.0, cloud=True, heat_timer_s=3600) is None
        assert C.heat_timer_duration(5.0, 25.0, cloud=True, heat_timer_s=3600) is None

    def test_not_on_homekit_or_when_off(self):
        assert C.heat_timer_duration(25.0, 25.0, cloud=False, heat_timer_s=3600) is None
        assert C.heat_timer_duration(25.0, 25.0, cloud=True, heat_timer_s=0) is None

    def test_maximum_is_what_the_trv_takes(self):
        assert C.is_maximum(22.0, 22.0)
        assert C.heat_timer_duration(22.0, 22.0, cloud=True, heat_timer_s=1800) == 1800

    def test_renewal_at_half_life(self):
        assert not C.heat_timer_refresh_due(1000.0 + 1799, 1000.0, 3600)
        assert C.heat_timer_refresh_due(1000.0 + 1800, 1000.0, 3600)

    def test_unknown_start_is_renewed_at_once(self):
        assert C.heat_timer_refresh_due(1000.0, None, 3600)

    @pytest.mark.parametrize("seconds, text", [
        (3600, "01:00:00"), (5400, "01:30:00"), (14400, "04:00:00"), (90, "00:01:30"),
        (3600.4, "01:00:00"),
    ])
    def test_time_period(self, seconds, text):
        assert C.format_time_period(seconds) == text

    def test_stalled(self):
        assert not C.cycle_stalled(1599.0, 1000.0, 600)
        assert C.cycle_stalled(1600.0, 1000.0, 600)

    def test_defaults(self):
        cfg = P.RegulationConfig()
        assert cfg.heat_timer_s == 0.0          # off until the owner enables it
        assert cfg.cycle_watchdog_s == 600.0
        band = P.OutOfBandConfig()
        assert (band.band_c, band.band_min, band.cold_band_min) == (0.5, 30.0, 120.0)


# ---------------------------------------------------------------------------
# Out-of-band monitor
# ---------------------------------------------------------------------------

BAND = dict(band_c=0.5, hot_after_s=30 * 60, cold_after_s=120 * 60)

# Living room, 11 Oct 2026, 21:00 to 06:00 local: 5-minute means of
# sensor.living_room_air_quality_temperature (recorder statistics).  The
# schedule went comfort 20 -> night 18 at 21:30.  Note the 0.32 °C bump
# off the 19.46 low around 22:00.
NIGHT_11_OCT = [
    20.49, 20.46, 20.45, 20.41, 20.29, 20.23, 20.01, 19.88, 19.79, 19.74, 19.69, 19.62,
    19.59, 19.55, 19.53, 19.54, 19.46, 19.51, 19.61, 19.67, 19.72, 19.75, 19.75, 19.75,
    19.75, 19.75, 19.78, 19.75, 19.70, 19.66, 19.64, 19.63, 19.60, 19.56, 19.52, 19.49,
    19.46, 19.43, 19.40, 19.38, 19.36, 19.34, 19.31, 19.29, 19.28, 19.26, 19.24, 19.22,
    19.22, 19.21, 19.20, 19.18, 19.17, 19.16, 19.15, 19.14, 19.14, 19.13, 19.13, 19.12,
    19.11, 19.09, 19.08, 19.06, 19.06, 19.05, 19.05, 19.04, 19.04, 19.03, 19.03, 19.02,
    19.00, 19.00, 18.99, 18.98, 18.97, 18.96, 18.94, 18.94, 18.93, 18.91, 18.90, 18.88,
    18.87, 18.86, 18.85, 18.84, 18.83, 18.83, 18.82, 18.81, 18.80, 18.79, 18.79, 18.77,
    18.75, 18.76, 18.75, 18.73, 18.73, 18.73, 18.72, 18.72, 18.70, 18.69, 18.67, 18.67,
    18.66,
]


def _feed(mon, start, readings, setpoint, *, step=60.0, eligible=True,
          heating=None, trv=None):
    """Feed one reading per minute (each value held for its step).

    ``heating``: what the valve reports (None: nothing known); ``trv``: the
    TRV's own reading, a value or a callable of the room reading."""
    t = start
    states = []
    for room in readings:
        trv_c = trv(room) if callable(trv) else trv
        states.append(mon.update(t, room, setpoint, eligible=eligible,
                                 valve_heating=heating, trv_c=trv_c, **BAND))
        t += step
    return t, states


class TestOutOfBandHot:

    def test_setback_night_of_11_october_never_fires(self):
        mon = C.OutOfBandMonitor()
        t = 0.0
        states = []
        for i, room in enumerate(NIGHT_11_OCT):
            setpoint = 20.0 if i < 6 else 18.0          # 21:30 = the 7th slot
            for _ in range(5):                           # one cycle a minute
                states.append(mon.update(t, room, setpoint, eligible=True, **BAND))
                t += 60.0
        assert not any(s.on for s in states)
        # The literal rule (0.5 above the setpoint) would have been on all night.
        night = NIGHT_11_OCT[6:]
        assert min(night) >= 18.0 + 0.5

    def test_warming_off_the_low_for_30_min_fires(self):
        """A stuck valve during a setback: the room stops cooling and warms
        0.5 °C off its low."""
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0] * 5, 20.0)
        t, _ = _feed(mon, t, [19.9, 19.7, 19.5, 19.4, 19.3], 18.0)   # setback, cooling
        assert mon.min_since == pytest.approx(19.3)
        t, states = _feed(mon, t, [19.5, 19.6, 19.7], 18.0)          # warming
        assert not states[-1].on and states[-1].since is None
        start = t
        t, states = _feed(mon, t, [19.8] * 30, 18.0)                 # 19.8 = low + 0.5
        assert not states[-1].on                                     # 29 min
        state = mon.update(t, 19.8, 18.0, eligible=True, **BAND)
        assert state.on and state.reason == C.BAND_HOT
        assert state.since == start
        assert state.reference_c == pytest.approx(19.3)
        assert state.deviation_c == pytest.approx(1.8)

    def test_above_the_setpoint_without_a_change_fires(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0, 20.2, 20.4], 20.0)
        start = t
        t, states = _feed(mon, t, [20.5] * 30, 20.0)
        assert not states[-1].on
        state = mon.update(t, 20.55, 20.0, eligible=True, **BAND)
        assert state.on and state.reason == C.BAND_HOT and state.since == start

    def test_once_the_low_reaches_the_setpoint_the_setpoint_is_the_reference(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0], 20.0)
        t, _ = _feed(mon, t, [19.0, 18.5, 18.0, 17.9], 18.0)
        t, states = _feed(mon, t, [18.0], 18.0)
        assert states[-1].reference_c == pytest.approx(18.0)

    def test_a_setpoint_rise_keeps_the_low(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0], 20.0)
        t, _ = _feed(mon, t, [19.5, 19.2], 18.0)
        t, _ = _feed(mon, t, [19.2], 19.0)          # rises, still under the low
        assert mon.min_since == pytest.approx(19.2)

    def test_first_reading_is_the_low(self):
        """No memory of why the room is warm: a warm room at the first start
        is not an alarm."""
        mon = C.OutOfBandMonitor()
        t, states = _feed(mon, 0.0, [21.0] * 40, 20.0)
        assert not any(s.on for s in states)
        assert states[-1].reference_c == pytest.approx(21.0)

    def test_low_survives_a_reload(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0], 20.0)
        t, _ = _feed(mon, t, [19.5, 19.3], 18.0)
        saved = mon.as_dict()
        again = C.OutOfBandMonitor()
        again.restore(saved)
        assert again.min_since == pytest.approx(19.3)
        start = t
        t, states = _feed(again, t, [19.8] * 31, 18.0)
        assert states[-1].on and states[-1].since == start

    @pytest.mark.parametrize("data", [None, "x", {}, {"min_since": "x", "setpoint": 18},
                                      {"min_since": 19.0}, {"min_since": 99, "setpoint": 18}])
    def test_unusable_saved_data_is_ignored(self, data):
        mon = C.OutOfBandMonitor()
        mon.restore(data)
        assert mon.min_since is None and mon.setpoint is None


class TestOutOfBandRadiatorEvidence:
    """Hot needs the radiator to be the cause; a room warmed by the sun with
    the valve shut is "warm": reported, not an alarm.  On 16 Oct 2026 four
    rooms rose 1.5-2.3 °C in a sunny afternoon with every valve at 0 %."""

    # Living room, 16 Oct 2026, 13:05 to 15:05: 5-minute means of the room
    # sensor; the valve reported idle throughout and its reading followed
    # the room (10.1 -> 12.3 with the -9.9 offset: no radiator heat).
    SUNNY_AFTERNOON = [
        20.10, 20.10, 20.11, 20.15, 20.20, 20.29, 20.40, 20.42, 20.39, 20.42, 20.56,
        20.71, 20.82, 21.06, 21.28, 21.37, 21.40, 21.50, 21.55, 21.72, 22.00, 22.22,
        22.34, 22.41, 22.33,
    ]

    def test_sun_with_the_valve_shut_is_warm_not_hot(self):
        mon = C.OutOfBandMonitor()
        t = 0.0
        states = []
        for room in self.SUNNY_AFTERNOON:
            t, more = _feed(mon, t, [room] * 5, 20.0, heating=False, trv=lambda r: r - 10.1)
            states += more
        assert not any(s.on for s in states)
        warm = [s for s in states if s.reason == C.BAND_WARM]
        assert warm and warm[0].radiator_heated is False
        assert warm[0].since is not None

    def test_valve_heating_during_the_excursion_is_hot(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0] * 5, 20.0, heating=False, trv=10.0)
        t, _ = _feed(mon, t, [20.5] * 10, 20.0, heating=False, trv=10.0)
        t, _ = _feed(mon, t, [20.5] * 5, 20.0, heating=True, trv=10.0)     # a pulse
        t, states = _feed(mon, t, [20.5] * 16, 20.0, heating=False, trv=10.0)
        assert states[-1].on and states[-1].reason == C.BAND_HOT
        assert states[-1].radiator_heated is True

    def test_valve_that_heated_just_before_the_excursion_is_the_cause(self):
        """The radiator's heat reaches the room after the valve shut (the
        16 Oct evening: full power 19:58-20:12, the room over the band from
        20:25): heating within the look-back still counts."""
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [19.6] * 14, 20.0, heating=True, trv=10.0)   # heats
        t, _ = _feed(mon, t, [19.9, 20.1, 20.3, 20.4] * 3, 20.0, heating=False, trv=12.0)
        t, states = _feed(mon, t, [20.55] * 31, 20.0, heating=False, trv=12.3)
        assert states[-1].on and states[-1].reason == C.BAND_HOT

    def test_valve_that_heated_long_before_is_not_the_cause(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [19.6] * 5, 20.0, heating=True, trv=10.0)
        t, _ = _feed(mon, t, [19.9] * 40, 20.0, heating=False, trv=10.0)   # 40 min idle
        t, states = _feed(mon, t, [20.55] * 31, 20.0, heating=False, trv=10.0)
        assert not states[-1].on and states[-1].reason == C.BAND_WARM

    def test_trv_reading_tells_a_hot_radiator_the_valve_does_not_report(self):
        """A stuck pin: the Tado reports 0 % but the radiator is hot, so the
        valve's own reading climbs far more than the room."""
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0] * 5, 20.0, heating=False, trv=10.0)
        t, states = _feed(mon, t, [20.5] * 31, 20.0, heating=False,
                          trv=lambda r: 10.0 + 4.0)                       # +4 on the TRV
        assert states[-1].on and states[-1].reason == C.BAND_HOT
        assert states[-1].radiator_heated is True

    def test_trv_reading_following_the_room_is_the_sun(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0] * 5, 20.0, heating=False, trv=10.0)
        t, states = _feed(mon, t, [20.5] * 31, 20.0, heating=False, trv=10.5)
        assert not states[-1].on and states[-1].reason == C.BAND_WARM

    def test_trv_reading_decides_when_the_valve_says_nothing(self):
        """No hvac_action at all (another TRV): the TRV's reading is still
        read; climbing with the room, it is the sun."""
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0] * 5, 20.0, trv=10.0)
        t, states = _feed(mon, t, [20.5] * 31, 20.0, trv=10.5)
        assert not states[-1].on and states[-1].reason == C.BAND_WARM
        t, states = _feed(mon, t, [20.6] * 5, 20.0, trv=13.0)
        assert states[-1].on and states[-1].reason == C.BAND_HOT

    def test_nothing_known_keeps_the_plain_rule(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0] * 5, 20.0)
        t, states = _feed(mon, t, [20.5] * 31, 20.0)
        assert states[-1].on and states[-1].reason == C.BAND_HOT
        assert states[-1].radiator_heated is None

    def test_warm_ends_like_hot(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0] * 5, 20.0, heating=False, trv=10.0)
        t, _ = _feed(mon, t, [20.5] * 31, 20.0, heating=False, trv=10.5)
        assert mon.state.reason == C.BAND_WARM
        state = mon.update(t, 20.3, 20.0, eligible=True, valve_heating=False,
                           trv_c=10.3, **BAND)
        assert state.reason is None and state.since is None and state.radiator_heated is None


class TestOutOfBandCold:

    def test_cold_after_two_hours(self):
        mon = C.OutOfBandMonitor()
        t, states = _feed(mon, 0.0, [19.4] * 120, 20.0)
        assert not states[-1].on
        state = mon.update(t, 19.4, 20.0, eligible=True, **BAND)
        assert state.on and state.reason == C.BAND_COLD and state.since == 0.0
        assert state.deviation_c == pytest.approx(-0.6)

    def test_timer_restarts_on_a_setpoint_rise(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [17.4] * 100, 18.0)        # cold for 100 min
        rise = t
        t, states = _feed(mon, t, [17.4] * 120, 20.0)     # night -> comfort
        assert not states[-1].on                          # 119 min after the rise
        state = mon.update(t, 17.5, 20.0, eligible=True, **BAND)
        assert state.on and state.since == rise

    def test_back_in_band_ends_it(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [19.4] * 121, 20.0)
        assert mon.state.on
        state = mon.update(t, 19.6, 20.0, eligible=True, **BAND)
        assert not state.on and state.since is None


class TestOutOfBandEligibility:

    def test_ineligible_never_fires_and_restarts_the_timers(self):
        mon = C.OutOfBandMonitor()
        t, states = _feed(mon, 0.0, [19.4] * 200, 20.0, eligible=False)
        assert not any(s.on for s in states)
        t, states = _feed(mon, t, [19.4] * 119, 20.0)
        assert not states[-1].on                           # counted from eligibility

    def test_low_is_tracked_while_ineligible(self):
        """A window open during a setback: the low keeps following the room."""
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0], 20.0)
        t, _ = _feed(mon, t, [19.0, 18.6], 18.0, eligible=False)
        assert mon.min_since == pytest.approx(18.6)

    def test_no_reading(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [19.4] * 121, 20.0)
        assert mon.state.on
        state = mon.update(t, None, 20.0, eligible=True, **BAND)
        assert not state.on


# ---------------------------------------------------------------------------
# Simulator: timer overlays and the kill test
# ---------------------------------------------------------------------------

def _sim(plant, *, timer_s, schedule_c=5.0, tune=False):
    c = P.RegulationConfig()
    tuner = None
    if tune:
        t = c.tuning
        tuner = A.Autotuner(A.AutotuneConfig(), A.Tuning(t.kp, t.ki, t.td_s),
                            derivative_allowed=True)
    return RoomSim(
        PlantParams(**dict(plant, tado_schedule_c=schedule_c)), R, c,
        ProxySettings(min_command_interval_s=180, min_change_threshold_c=0.3,
                      heat_timer_s=timer_s),
        y0=18.5, autotune_module=A, tuner=tuner, seed=3,
    )


def _kill_mid_heat_up(plant, kill_h, *, timer_s, schedule_c, after_h=6.0):
    """Run to *kill_h* (a heat-up on day 1), stop the proxy, carry on."""
    sim = _sim(plant, timer_s=timer_s, schedule_c=schedule_c)
    sim.run(kill_h, SCHED)
    assert sim.trace.mode[-1] == "heat"
    last_cmd = sim.last_send
    sim.kill()
    tr = sim.run(after_h, SCHED)
    after = [k for k, t in enumerate(tr.t) if t >= kill_h * HOUR]
    shut = next(
        (tr.t[k] for k in after if all(tr.valve[j] == 0.0 for j in after if j >= k)), None
    )
    return sim, last_cmd, shut, max(tr.y_true[k] for k in after)


# (room, kill time: on day 1, while it still heats up at full power)
KILLS = [(LIVING_ROOM_FIT, 24 + 6 + 20 / 60), (SECOND_BEDROOM_FIT, 24 + 6.75)]


@pytest.mark.parametrize("plant, kill_h", KILLS)
@pytest.mark.parametrize("schedule_c", [5.0, None])
def test_killed_mid_heat_up_the_valve_shuts_within_the_timer(plant, kill_h, schedule_c):
    """The proxy dies with the valve fully open.  The timer runs out
    heat_timer_s after the last command (a renewal or the heat-up's start)
    and the zone falls back to its schedule (5 °C or Off); the valve shuts
    at its next control step (it moves every 2 min in the model) and stays
    shut.  Measured: 3,660 s after the last command in both rooms, with the
    room peaking at 20.1 / 19.3 °C (target 20) instead of 26.2 / 23.4 °C."""
    timer_s = 3600.0
    sim, last_cmd, shut, peak = _kill_mid_heat_up(
        plant, kill_h, timer_s=timer_s, schedule_c=schedule_c)
    assert sim.trace.timer_expiries == [pytest.approx(last_cmd + timer_s)]
    assert shut is not None
    assert shut - last_cmd <= timer_s + sim.p.tado_period_s
    assert peak < 20.0 + 0.2
    assert sim.trv_power is (schedule_c is not None)


@pytest.mark.parametrize("plant, kill_h", KILLS)
def test_killed_mid_heat_up_without_the_timer_the_valve_stays_open(plant, kill_h):
    """Why the timer exists: a Manual 25 °C overlay outlives the proxy."""
    sim, _, shut, peak = _kill_mid_heat_up(plant, kill_h, timer_s=0.0, schedule_c=5.0)
    assert shut is None
    assert sim.valve > 0.0
    assert peak > 20.0 + 3.0


@pytest.mark.parametrize("plant", [LIVING_ROOM_FIT, SECOND_BEDROOM_FIT])
def test_the_timer_changes_nothing_while_the_proxy_runs(plant):
    """Renewed at half life, the timer never runs out: the room follows the
    same path to the last decimal, every other command is the same, and the
    API cost is at most one renewal per heat_timer_s / 2 at the maximum."""
    days = 4
    base = _sim(plant, timer_s=0.0, tune=True)
    tr0 = base.run(days * 24, SCHED)
    timed = _sim(plant, timer_s=3600.0, tune=True)
    tr1 = timed.run(days * 24, SCHED)
    assert tr1.y_true == tr0.y_true
    assert tr1.valve == tr0.valve
    assert tr1.timer_expiries == []
    assert tr1.sends - tr1.timer_renewals == tr0.sends
    at_max_s = 60.0 * sum(1 for c in tr1.cmd if c >= 25.0 - 0.05)
    assert 0 < tr1.timer_renewals <= at_max_s / (3600.0 / 2)
