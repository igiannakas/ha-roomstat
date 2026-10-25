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
    """The overlay is gone: the valve is back on its schedule ("auto"), which
    holds no command of ours.  A heat command is sent again, and so is an off
    (2.8.4: the owner's valves are only ever heating or off; until then a
    shut schedule block counted as holding the frost-level command)."""

    def test_heat_command_lost_to_an_off_schedule(self):
        assert C.trv_setpoint_drifted(25.0, None, 600, 360, 0.3, trv_scheduled=True)

    def test_heat_command_lost_to_a_5c_schedule(self):
        assert C.trv_setpoint_drifted(25.0, 5.0, 600, 360, 0.3, trv_scheduled=True)

    def test_any_heat_command_whatever_the_schedule_holds(self):
        """Even a schedule block close to the command: it is not ours, and
        moves with the schedule."""
        assert C.trv_setpoint_drifted(18.3, 18.2, 600, 360, 0.3, trv_scheduled=True)

    @pytest.mark.parametrize("schedule", [None, 5.0, 20.0])
    def test_off_command_not_held_by_any_schedule(self, schedule):
        assert C.trv_setpoint_drifted(5.0, schedule, 600, 360, 0.3, trv_scheduled=True)

    def test_off_command_held_only_by_an_off_valve(self):
        assert not C.trv_setpoint_drifted(5.0, 5.0, 600, 360, 0.3, trv_off=True)
        assert not C.trv_setpoint_drifted(5.0, None, 600, 360, 0.3, trv_off=True)

    @pytest.mark.parametrize("held", [5.0, 18.0])
    def test_off_command_not_held_by_a_valve_in_heat(self, held):
        """A Manual 5 °C is heat at 5, not off; any heat setpoint is drift."""
        assert C.trv_setpoint_drifted(5.0, held, 600, 360, 0.3)

    def test_off_command_with_nothing_known_is_not_drift(self):
        assert not C.trv_setpoint_drifted(5.0, None, 600, 360, 0.3)

    def test_heat_command_lost_to_an_off_valve(self):
        assert C.trv_setpoint_drifted(18.0, 5.0, 600, 360, 0.3, trv_off=True)

    def test_is_off_command(self):
        assert C.is_off_command(5.0, 5.0) and C.is_off_command(5.05, 5.0)
        assert C.is_off_command(4.0, 5.0)
        assert not C.is_off_command(5.1, 5.0)

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
        assert states[-1].heated_before_s == pytest.approx(14 * 60.0)
        assert states[-1].heated_during is False

    def test_heating_less_than_the_band_can_explain_is_no_cause(self):
        """Band / the fastest radiator here (4 °C/h): 7.5 min for 0.5 °C."""
        assert C.OutOfBandMonitor.heated_min_s(0.5) == pytest.approx(450.0)
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [19.6] * 7, 20.0, heating=True, trv=10.0)    # 7 min
        t, _ = _feed(mon, t, [20.0] * 10, 20.0, heating=False, trv=10.0)
        t, states = _feed(mon, t, [20.55] * 31, 20.0, heating=False, trv=10.0)
        assert not states[-1].on and states[-1].reason == C.BAND_WARM
        assert states[-1].heated_before_s == pytest.approx(7 * 60.0)
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [19.6] * 8, 20.0, heating=True, trv=10.0)    # 8 min
        t, _ = _feed(mon, t, [20.0] * 10, 20.0, heating=False, trv=10.0)
        t, states = _feed(mon, t, [20.55] * 31, 20.0, heating=False, trv=10.0)
        assert states[-1].on and states[-1].reason == C.BAND_HOT

    def test_only_the_look_back_counts(self):
        """Heating that ended more than 30 min before the room went too warm
        counts for nothing, however long it was."""
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [19.0] * 40, 20.0, heating=True, trv=10.0)
        t, _ = _feed(mon, t, [20.0] * 25, 20.0, heating=False, trv=10.0)
        t, states = _feed(mon, t, [20.55] * 31, 20.0, heating=False, trv=10.0)
        assert states[-1].heated_before_s == pytest.approx(5 * 60.0)
        assert not states[-1].on

    def test_a_pause_counts_for_two_minutes_at_most(self):
        """A heating report stands until the next one, but a gap (a restart)
        is no evidence of more heating."""
        mon = C.OutOfBandMonitor()
        mon.update(0.0, 19.6, 20.0, eligible=True, valve_heating=True, trv_c=10.0, **BAND)
        t, _ = _feed(mon, 900.0, [20.0] * 4, 20.0, heating=False, trv=10.0)
        t, states = _feed(mon, t, [20.55] * 31, 20.0, heating=False, trv=10.0)
        assert states[-1].heated_before_s == pytest.approx(C.HEATING_SAMPLE_MAX_S)

    def test_heating_unknown_leaves_the_trv_reading_to_decide(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0] * 5, 20.0, trv=10.0)
        t, states = _feed(mon, t, [20.55] * 31, 20.0, trv=10.6)
        assert states[-1].heated_before_s is None and states[-1].heated_during is None
        assert states[-1].radiator_rise_c == pytest.approx(0.05)
        assert not states[-1].on and states[-1].radiator_heated is False

    def test_a_cooled_radiator_turns_hot_into_warm(self):
        """Hot on the TRV's reading alone: once the radiator has cooled while
        the room stays warm, the room is warm for another reason."""
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0] * 5, 20.0, heating=False, trv=10.0)
        t, states = _feed(mon, t, [20.6] * 31, 20.0, heating=False, trv=13.0)
        assert states[-1].on and states[-1].radiator_rise_c == pytest.approx(2.4)
        t, states = _feed(mon, t, [20.6] * 5, 20.0, heating=False, trv=11.0)
        assert not states[-1].on and states[-1].reason == C.BAND_WARM

    def test_describe(self):
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0] * 5, 20.0, heating=False, trv=10.0)
        t, _ = _feed(mon, t, [19.6] * 3, 5.0, heating=False, trv=10.0)       # lowered
        t, states = _feed(mon, t, [20.6] * 31, 5.0, heating=False, trv=10.0)
        text = C.OutOfBandMonitor.describe(states[-1], 5.0, 0.5)
        assert text == (
            "room 20.60 °C, 1.00 over its low of 19.60 (target 5.0); the valve "
            "heated 0 of the 30 min before (7.5 needed), its reading is 0.0 °C "
            "over its cold level (2.0 needed)"
        )
        # Hot names only what made it hot.
        hot = _replay(TestOutOfBandReplays.LIVING_22_OCT)[125]
        assert C.OutOfBandMonitor.describe(hot, 20.0, 0.5) == (
            "room 20.61 °C, 0.61 over the target 20.0; the valve heated 12 of the "
            "30 min before"
        )
        mon = C.OutOfBandMonitor()
        t, _ = _feed(mon, 0.0, [20.0] * 5, 20.0)
        t, states = _feed(mon, t, [20.6] * 31, 20.0)
        assert C.OutOfBandMonitor.describe(states[-1], 20.0, 0.5) == (
            "room 20.60 °C, 0.60 over the target 20.0; nothing known about the valve"
        )
        mon = C.OutOfBandMonitor()
        t, states = _feed(mon, 0.0, [19.4] * 121, 20.0)
        assert C.OutOfBandMonitor.describe(states[-1], 20.0, 0.5) == (
            "room 19.40 °C, 0.60 under the target 20.0"
        )

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


def _replay(case, *, valve_silent=False, setpoint=None):
    """Feed a recorded episode: 5-minute means held for each minute, the
    valve's heating report and the target per minute."""
    mon = C.OutOfBandMonitor()
    if "restore" in case:
        mon.restore(case["restore"])
    eligible = case.get("eligible", lambda m: True)
    target = setpoint or case["setpoint"]
    states = []
    for m in range(len(case["room"]) * 5):
        states.append(mon.update(
            m * 60.0, case["room"][m // 5], target(m), eligible=eligible(m),
            valve_heating=None if valve_silent else case["heating"](m),
            trv_c=case["trv"][m // 5], **BAND,
        ))
    return states


def _old_rule_hot(state):
    """What the rule before 2.10.3 made of the same evidence: any heating in
    the look-back, or the gap 1 °C above its low."""
    return state.reason in (C.BAND_HOT, C.BAND_WARM) and bool(
        state.heated_during
        or (state.heated_before_s or 0.0) > 0.0
        or (state.radiator_rise_c is not None and state.radiator_rise_c >= 1.0)
    )


class TestOutOfBandReplays:
    """Every out-of-band alarm of 18-25 Oct 2026 (recorder 5-minute means of
    the room sensor and of the Tado zone's own reading; the valve's heating
    from the zone's heating power).  Under the old rule each of these was
    hot; the false ones are warm now, the real one is still hot."""

    # 25 Oct, second bedroom, from 07:50 UTC.  The room dipped to 19.6 at
    # 09:40, a hold topped up 09:45-09:52 (8 min, 26-84 %), Frost protection
    # was picked at 09:52, and the room rose 1.5 °C with the valve shut and
    # its reading flat.  Hot at 10:50 under the old rule.
    SECOND_25_OCT = dict(
        room=[20.23, 20.23, 20.21, 20.20, 20.19, 20.20, 20.20, 20.21, 20.21, 20.22,
              20.22, 20.18, 20.14, 20.16, 20.15, 20.15, 20.15, 20.14, 20.13, 20.14,
              20.14, 20.12, 19.97, 19.85, 19.67, 19.63, 19.69, 19.78, 19.96, 20.04,
              20.28, 20.59, 20.79, 20.89, 20.99, 21.05, 20.94, 20.85, 20.73],
        trv=[15.59, 15.41, 15.41, 15.36, 15.18, 15.18, 15.18, 15.18, 15.04, 15.04,
             15.04, 14.97, 14.87, 14.87, 14.87, 14.75, 14.72, 14.72, 14.70, 14.61,
             14.61, 14.60, 14.54, 14.35, 14.34, 14.40, 14.53, 14.57, 14.74, 14.81,
             14.92, 15.05, 15.14, 15.14, 15.14, 15.28, 15.28, 15.15, 15.07],
        heating=lambda m: 115 <= m <= 122,
        setpoint=lambda m: 20.0 if m < 123 else 5.0,
    )
    # 22 Oct, study, from 14:00 UTC: Comfort 20, warm since the morning, the
    # valve shut all day (0 % heating).  The valve's reading fell 15.4 ->
    # 13.9 and recovered with the room flat: three hot alarms (18:26,
    # 18:33-19:03, 20:45-20:59 local) under the old rule.
    STUDY_22_OCT = dict(
        room=[20.99, 21.01, 21.04, 21.08, 21.11, 21.14, 21.16, 21.17, 21.18, 21.18,
              21.19, 21.19, 21.20, 21.22, 21.23, 21.24, 21.25, 21.25, 21.25, 21.25,
              21.25, 21.25, 21.25, 21.24, 21.23, 21.23, 21.18, 21.14, 21.10, 21.10,
              21.11, 21.11, 21.09, 21.06, 21.05, 21.08, 21.08, 21.08, 21.06, 21.04,
              21.02, 20.98, 20.98, 20.97, 20.94, 20.87, 20.79, 20.76, 20.74, 20.71,
              20.74, 20.79, 20.84, 20.89, 20.91, 20.94, 20.95, 20.95, 20.94, 20.95,
              20.98, 21.02, 21.04, 21.04, 21.06, 21.14, 21.16, 21.21, 21.26, 21.25,
              21.20, 21.16, 21.13, 21.09, 21.06, 21.04, 21.01, 21.00],
        trv=[15.25, 15.25, 15.25, 15.29, 15.29, 15.29, 15.29, 15.34, 15.35, 15.35,
             15.32, 15.31, 15.31, 15.31, 15.29, 15.28, 15.28, 15.32, 15.35, 15.35,
             15.35, 15.30, 15.30, 15.30, 15.11, 15.11, 15.00, 14.93, 14.76, 14.61,
             14.46, 14.36, 14.25, 14.08, 13.92, 13.96, 14.26, 14.44, 14.60, 14.68,
             14.68, 14.59, 14.61, 14.73, 14.73, 14.72, 14.66, 14.49, 14.41, 14.28,
             14.28, 14.28, 14.34, 14.36, 14.36, 14.39, 14.39, 14.39, 14.39, 14.40,
             14.41, 14.41, 14.41, 14.43, 14.43, 14.56, 14.65, 14.83, 14.87, 15.06,
             15.06, 15.06, 15.09, 15.14, 15.14, 15.15, 15.17, 15.17],
        heating=lambda m: False,
        setpoint=lambda m: 20.0,
        restore={"min_since": 19.9, "setpoint": 20.0},
    )
    # 25 Oct, study, from 05:30 UTC: Night 18 -> Comfort 20 at 06:00, full
    # power 06:00-06:12; the heat-up landed at 20.06 (06:35), then the room
    # went on to 20.9 with the valve shut.  Hot at 07:57 under the old rule
    # (the valve's reading still 1.6 °C up on the cooling radiator).
    STUDY_25_OCT = dict(
        room=[19.34, 19.35, 19.36, 19.39, 19.43, 19.41, 19.40, 19.40, 19.45, 19.55,
              19.64, 19.78, 19.88, 19.98, 20.05, 20.06, 20.06, 20.08, 20.18, 20.25,
              20.31, 20.38, 20.44, 20.49, 20.55, 20.59, 20.63, 20.67, 20.71, 20.74,
              20.75, 20.76, 20.80, 20.84, 20.89, 20.90, 20.86, 20.80, 20.73, 20.66,
              20.59, 20.54, 20.49, 20.44, 20.41, 20.42, 20.42, 20.41],
        trv=[13.73, 13.69, 13.69, 13.70, 13.71, 13.71, 13.75, 14.26, 15.35, 16.27,
             17.13, 17.83, 18.21, 18.38, 18.51, 18.58, 18.53, 18.34, 18.27, 18.23,
             18.08, 17.93, 17.81, 17.61, 17.47, 17.35, 17.18, 16.96, 16.92, 16.75,
             16.58, 16.54, 16.37, 16.37, 16.23, 16.19, 15.96, 15.96, 15.83, 15.75,
             15.75, 15.62, 15.54, 15.54, 15.43, 15.36, 15.36, 15.36],
        heating=lambda m: 30 <= m < 42,
        setpoint=lambda m: 18.0 if m < 30 else 20.0,
    )
    # 22 Oct, living room, from 09:00 local: a window 09:35-10:14 (Frost),
    # Comfort again with full power 10:12-10:24, the room over the band from
    # 10:35.  Hot 11:04-11:17: the radiator's doing (with the recovery).
    LIVING_22_OCT = dict(
        room=[19.97, 20.01, 20.08, 20.09, 20.09, 20.07, 20.03, 16.53, 16.49, 17.98,
              18.45, 18.71, 18.83, 18.97, 19.03, 19.16, 19.38, 19.82, 20.27, 20.53,
              20.67, 20.69, 20.72, 20.68, 20.68, 20.61, 20.56, 20.52, 20.47, 20.40,
              20.31],
        trv=[15.1, 15.2, 15.6, 15.6, 15.5, 15.3, 15.2, 14.6, 13.0, 12.8, 12.5, 12.2,
             12.0, 11.8, 11.5, 11.5, 11.4, 11.8, 12.3, 12.9, 13.4, 13.6, 13.8, 13.9,
             13.8, 13.7, 13.5, 13.3, 13.0, 12.8, 12.6],
        heating=lambda m: 72 <= m < 84,
        setpoint=lambda m: 5.0 if 35 <= m < 75 else 20.0,
        eligible=lambda m: not 35 <= m < 75,
    )

    def test_a_short_top_up_is_not_the_cause(self):
        states = _replay(self.SECOND_25_OCT)
        assert not any(s.on for s in states)
        warm = states[180]                                   # 10:50
        assert warm.reason == C.BAND_WARM and warm.radiator_heated is False
        assert warm.reference_c == pytest.approx(19.63)
        assert warm.heated_before_s == pytest.approx(3 * 60.0)   # 09:50-09:52
        assert warm.radiator_rise_c < C.RADIATOR_RISE_C / 4
        assert _old_rule_hot(warm)

    def test_the_valves_reading_swinging_with_the_valve_shut_is_not_the_radiator(self):
        states = _replay(self.STUDY_22_OCT)
        judged = [s for s in states if s.reason]
        assert judged and all(s.reason == C.BAND_WARM for s in judged)
        rise = max(s.radiator_rise_c for s in judged)
        assert 1.0 <= rise < C.RADIATOR_RISE_C
        assert any(_old_rule_hot(s) for s in judged)

    def test_a_radiator_cooling_after_its_heat_up_is_not_the_cause(self):
        states = _replay(self.STUDY_25_OCT)
        judged = [s for s in states if s.reason]
        assert judged and all(s.reason == C.BAND_WARM for s in judged)
        assert all(s.heated_before_s == 0.0 for s in judged)
        assert max(s.radiator_rise_c for s in judged) < C.RADIATOR_RISE_C
        assert _old_rule_hot(judged[0])

    def test_full_power_just_before_is_still_hot(self):
        states = _replay(self.LIVING_22_OCT)
        on = [m for m, s in enumerate(states) if s.on]
        assert on == list(range(125, 140))                   # 11:05-11:19
        assert states[125].heated_before_s == pytest.approx(12 * 60.0)
        assert states[125].radiator_rise_c < C.RADIATOR_RISE_C   # (b) alone

    def test_the_valves_reading_shows_a_real_heat_up(self):
        """The same study morning with the valve silent and the target at
        19: full power lifted its reading 4 °C over its cold level, which
        alone makes it hot, until the radiator has cooled below 2 °C."""
        states = _replay(self.STUDY_25_OCT, valve_silent=True, setpoint=lambda m: 19.0)
        on = [m for m, s in enumerate(states) if s.on]
        assert on and on[0] == 90                            # 07:00
        assert states[90].radiator_rise_c > 3.5
        assert states[on[-1] + 1].reason == C.BAND_WARM


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
    # Shut for good: closed from k to the end, and for at least 20 min (a
    # valve pulsing at its own 25 °C setpoint in a 26 °C room is not shut,
    # even when the last sample happens to be a closed one).
    shut = next(
        (tr.t[k] for k in after
         if tr.t[-1] - tr.t[k] >= 1200.0
         and all(tr.valve[j] == 0.0 for j in after if j >= k)), None
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
    # Six hours on, in a 26 °C room, the valve still pulses at its own
    # 25 °C setpoint (any sample may be a closed one).
    assert max(sim.trace.valve[-30:]) > 0.0
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
