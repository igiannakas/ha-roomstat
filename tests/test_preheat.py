"""Preheat on the way home: the HA-free rule (preheat.py).

The owner's decisions (25 Oct 2026) under test:
- within 800 m (Home ETA's "someone close to home") the schedule's
  temperature, however long the stay; within the near-home radius the
  30-minute temperature (the schedule's temperature less the room's typical
  30-minute recovery, rounded down to 0.1 °C);
- someone heading home: the schedule's temperature once the expected arrival
  is within the heat-up time + 5 min; stop when the arrival slips 20 min and
  the room is no longer due, start again when the new arrival is due; stop
  when the trip home is over (no arrival for 10 min); at most 4 h per trip;
- a drop to a cooler level after 10 min, a rise at once;
- a change by hand while nobody is home, an Off included (27 Oct 2026),
  stands preheat aside without ending the trip home.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from tests.pure_modules import parameters, preheat

P = preheat
T0 = 1_761_000_000.0  # 20 Oct 2026
MIN = 60.0


def inputs(now, **kw):
    base = dict(
        now=now, enabled=True, inactive_reason=None, scheduled_c=20.0, away_c=16.0,
        room_c=16.5, arrival_ts=None, close=False, nearby=False,
        rate_c_per_h=2.5, dead_time_s=20 * MIN, recovery_c=0.58,
    )
    base.update(kw)
    return P.PreheatInputs(**base)


def needed_s(room, scheduled=20.0, rate=2.5, dead=20 * MIN, factor=0.8, lead=5 * MIN):
    return dead + max(0.0, scheduled - room) / (factor * rate) * 3600.0 + lead


# ===========================================================================
# Parsing and rounding
# ===========================================================================

class TestParsing:
    @pytest.mark.parametrize(("state", "expected"), [
        ("2026-10-27T18:42:00+00:00", 1793126520.0),
        ("2026-10-27T18:42:00Z", 1793126520.0),
        ("2026-10-27T19:42:00+01:00", 1793126520.0),
        ("2026-10-27T18:42:00", None),       # no offset: not a time to act on
        ("unknown", None), ("unavailable", None), ("", None), (None, None),
        ("soon", None), ("9999-12-31T23:59:59+14:00", 253402250399.0),
    ])
    def test_arrival(self, state, expected):
        assert P.parse_arrival(state) == expected

    @pytest.mark.parametrize(("state", "expected"), [
        ("on", True), ("off", False), ("unknown", None), ("unavailable", None),
        (None, None), ("home", None),
    ])
    def test_on_off(self, state, expected):
        assert P.parse_on_off(state) is expected

    @pytest.mark.parametrize(("value", "expected"), [
        (19.42, 19.4), (19.5, 19.5), (20.0 - 0.6, 19.4), (20.0 - 0.58, 19.4),
        (19.4999, 19.4), (19.0, 19.0), (20.0 - 0.43, 19.5), (20.0 - 0.77, 19.2),
    ])
    def test_floor_tenth(self, value, expected):
        assert P.floor_tenth(value) == expected


# ===========================================================================
# When it applies
# ===========================================================================

class TestApplies:
    def test_off(self):
        d = P.PreheatController().evaluate(inputs(T0, enabled=False))
        assert d.state == P.STATE_OFF and d.target_c is None

    @pytest.mark.parametrize("reason", [
        P.INACTIVE_HOME, P.INACTIVE_CHANGE_BY_HAND, P.INACTIVE_WINDOW,
        P.INACTIVE_SUMMER, P.INACTIVE_OFF, P.INACTIVE_NO_PRESENCE,
    ])
    def test_inactive_whatever_home_eta_says(self, reason):
        d = P.PreheatController().evaluate(
            inputs(T0, inactive_reason=reason, close=True, arrival_ts=T0)
        )
        assert d.state == P.STATE_INACTIVE
        assert d.inactive_reason == reason and d.target_c is None

    @pytest.mark.parametrize("scheduled", [16.0, 7.0, 5.0, None])
    def test_nothing_warmer_than_away_to_preheat(self, scheduled):
        """A schedule at Away, Frost protection or unknown: no preheat."""
        d = P.PreheatController().evaluate(inputs(T0, scheduled_c=scheduled, close=True))
        assert d.state == P.STATE_INACTIVE
        assert d.inactive_reason == P.INACTIVE_NOTHING_WARMER

    def test_night_preheats_to_night(self):
        d = P.PreheatController().evaluate(inputs(T0, scheduled_c=18.0, close=True))
        assert d.state == P.STATE_CLOSE and d.target_c == 18.0

    def test_an_off_by_hand_stands_it_aside_whatever_home_eta_says(self):
        c = P.PreheatController()
        c.evaluate(inputs(T0, arrival_ts=T0 + 10 * MIN))
        assert c.heating_since == T0
        d = c.evaluate(inputs(T0 + MIN, inactive_reason=P.INACTIVE_OFF_BY_HAND,
                              close=True, arrival_ts=T0 + 9 * MIN))
        assert d.state == P.STATE_INACTIVE and d.target_c is None
        assert d.inactive_reason == P.INACTIVE_OFF_BY_HAND
        assert c.heating_since is None
        # Back to automatic (Away or Resume schedule while out): a rise at once.
        d = c.evaluate(inputs(T0 + 2 * MIN, close=True))
        assert d.state == P.STATE_CLOSE

    def test_the_held_cold_state_is_gone(self):
        """2.11.0's Away held cold went in 2.12.0 (an Off does it now)."""
        assert "held_cold" not in P.STATES
        assert not hasattr(P, "STATE_HELD_COLD")
        assert "held_cold" not in P.PreheatInputs.__dataclass_fields__


# ===========================================================================
# Close and nearby
# ===========================================================================

class TestLevels:
    def test_close_is_the_schedule_temperature(self):
        d = P.PreheatController().evaluate(inputs(T0, close=True, nearby=True))
        assert d.state == P.STATE_CLOSE and d.target_c == 20.0
        assert d.level == P.LEVEL_CLOSE

    @pytest.mark.parametrize(("recovery", "t30", "learned"), [
        (0.58, 19.4, True),     # the study: 19.4, not 19.5 (rounded down)
        (0.77, 19.2, True),     # the living room
        (0.43, 19.5, True),     # the master bedroom
        (None, 19.5, False),    # not learned yet: 0.5 °C
        (0.0, 20.0, True),
        (-0.2, 20.0, True),     # a room that fell: no lower than the target
        (6.0, 16.0, True),      # never below Away
        (math.nan, 19.5, False),
    ])
    def test_nearby_is_the_30_minute_temperature(self, recovery, t30, learned):
        d = P.PreheatController().evaluate(inputs(T0, nearby=True, recovery_c=recovery))
        assert d.state == P.STATE_NEARBY
        assert d.target_c == t30 and d.recovery_temp_c == t30
        assert d.recovery_learned is learned

    def test_nearby_is_held_however_long(self):
        c = P.PreheatController()
        for minute in range(0, 8 * 60, 5):
            d = c.evaluate(inputs(T0 + minute * MIN, nearby=True))
            assert d.state == P.STATE_NEARBY, minute

    @pytest.mark.parametrize("close,nearby", [(None, None), (None, False), (False, None)])
    def test_unknown_is_not_used(self, close, nearby):
        d = P.PreheatController().evaluate(inputs(T0, close=close, nearby=nearby))
        assert d.state == P.STATE_WAITING and d.target_c == 16.0

    def test_a_drop_waits_ten_minutes_a_rise_does_not(self):
        c = P.PreheatController()
        assert c.evaluate(inputs(T0, close=True)).state == P.STATE_CLOSE
        d = c.evaluate(inputs(T0 + MIN, close=False, nearby=True))
        assert d.state == P.STATE_CLOSE                    # waiting to drop
        assert d.level_drop_ts == T0 + MIN + 10 * MIN
        d = c.evaluate(inputs(T0 + 10 * MIN, close=False, nearby=True))
        assert d.state == P.STATE_CLOSE
        d = c.evaluate(inputs(T0 + 11 * MIN, close=False, nearby=True))
        assert d.state == P.STATE_NEARBY and d.level_drop_ts is None
        # Back within 800 m: at once.
        assert c.evaluate(inputs(T0 + 12 * MIN, close=True)).state == P.STATE_CLOSE

    def test_hovering_at_a_boundary_does_not_flap(self):
        c = P.PreheatController()
        states = []
        for minute in range(60):
            close = minute % 6 < 3        # in and out every 3 min
            states.append(c.evaluate(inputs(T0 + minute * MIN, close=close, nearby=True)).state)
        assert set(states) == {P.STATE_CLOSE}

    def test_a_drop_lands_on_the_warmest_level_of_the_last_ten_minutes(self):
        c = P.PreheatController()
        c.evaluate(inputs(T0, close=True))
        c.evaluate(inputs(T0 + MIN, nearby=True))
        c.evaluate(inputs(T0 + 5 * MIN))            # gone further meanwhile
        d = c.evaluate(inputs(T0 + 11 * MIN))
        # Nearby was wanted until T0+5: it holds until T0+15.
        assert d.state == P.STATE_NEARBY and d.level_drop_ts == T0 + 15 * MIN
        d = c.evaluate(inputs(T0 + 15 * MIN))
        assert d.state == P.STATE_WAITING and d.target_c == 16.0

    def test_a_drop_through_two_levels_does_not_flap(self):
        """Review finding: close → none → nearby within seconds read close,
        then waiting, then nearby; nearby or close, never waiting."""
        c = P.PreheatController()
        c.evaluate(inputs(T0, close=True))
        states = [c.evaluate(inputs(T0 + 10 * MIN + s, close=close, nearby=nearby)).state
                  for s, close, nearby in ((0, False, True), (6, False, False),
                                           (12, False, True), (60, False, True))]
        assert states == [P.STATE_CLOSE] * 4
        assert c.evaluate(inputs(T0 + 20 * MIN + 13, nearby=True)).state == P.STATE_NEARBY

    def test_a_member_reloading_for_seconds_changes_nothing(self):
        c = P.PreheatController()
        c.evaluate(inputs(T0, nearby=True))
        assert c.evaluate(inputs(T0 + 30, close=None, nearby=None)).state == P.STATE_NEARBY
        assert c.evaluate(inputs(T0 + 90, nearby=True)).state == P.STATE_NEARBY


# ===========================================================================
# Heading home
# ===========================================================================

class TestHeadingHome:
    def test_heat_up_time(self):
        d = P.PreheatController().evaluate(inputs(T0, room_c=17.0, arrival_ts=T0 + 5 * 3600))
        # 20 min + 3.0 °C / (0.8 × 2.5 °C/h) + 5 min = 115 min
        assert d.needed_s == pytest.approx(needed_s(17.0)) == pytest.approx(115 * MIN)
        assert d.due_ts == pytest.approx(T0 + 5 * 3600 - 115 * MIN)
        assert d.state == P.STATE_WAITING and d.since_ts is None

    def test_23_oct_return_living_room(self):
        """23 Oct 2026: the living room at 17.35 °C, rate 2.52 °C/h, dead time
        22.3 min: 107 min (the room came within 0.3 °C 81 min after its
        target rose, so it starts 26 min early)."""
        d = P.PreheatController().evaluate(inputs(
            T0, room_c=17.35, rate_c_per_h=2.52, dead_time_s=22.3 * MIN,
            arrival_ts=T0 + 3 * 3600,
        ))
        assert d.needed_s / MIN == pytest.approx(106.2, abs=0.1)

    def test_starts_when_due_and_stops_at_home(self):
        c = P.PreheatController()
        arrival = T0 + 3600
        n = needed_s(16.5)
        d = c.evaluate(inputs(arrival - n - MIN, arrival_ts=arrival))
        assert d.state == P.STATE_WAITING
        d = c.evaluate(inputs(arrival - n, arrival_ts=arrival))
        assert d.state == P.STATE_DUE and d.target_c == 20.0
        assert d.since_ts == arrival - n and d.started_arrival_ts == arrival
        d = c.evaluate(inputs(arrival, inactive_reason=P.INACTIVE_HOME))
        assert d.state == P.STATE_INACTIVE and c.heating_since is None

    def test_an_arrival_in_the_past_counts_as_now(self):
        d = P.PreheatController().evaluate(inputs(T0, arrival_ts=T0 - 600))
        assert d.state == P.STATE_DUE and d.arrival_ts == T0

    def test_an_arrival_more_than_12_h_ahead_is_ignored(self):
        c = P.PreheatController()
        d = c.evaluate(inputs(T0, arrival_ts=T0 + 13 * 3600, room_c=5.0,
                              rate_c_per_h=0.2))
        assert d.state == P.STATE_WAITING and d.arrival_ignored and d.arrival_ts is None

    def test_a_room_ahead_of_its_schedule_is_not_stopped(self):
        """It warmed up (needs less now): only a slip of the arrival stops it."""
        c = P.PreheatController()
        arrival = T0 + 2 * 3600
        c.evaluate(inputs(T0, room_c=16.0, arrival_ts=arrival))
        assert c.heating_since == T0
        d = c.evaluate(inputs(T0 + 30 * MIN, room_c=19.8, arrival_ts=arrival))
        assert d.state == P.STATE_DUE
        assert T0 + 30 * MIN < d.due_ts       # "not due" on today's numbers

    def test_a_slip_of_25_min_stops_it_and_the_new_arrival_restarts_it(self):
        c = P.PreheatController()
        arrival = T0 + 50 * MIN
        c.evaluate(inputs(T0, room_c=19.0, arrival_ts=arrival))   # needs 55 min: due
        assert c.heating_since == T0
        # A jam: expected 25 min later, and the room has warmed meanwhile.
        d = c.evaluate(inputs(T0 + 5 * MIN, room_c=19.4, arrival_ts=arrival + 25 * MIN))
        assert d.state == P.STATE_WAITING and d.ended == P.ENDED_SLIPPED
        assert d.ended_arrival_ts == arrival and d.target_c == 16.0
        # Due again for the new arrival.
        new_due = arrival + 25 * MIN - needed_s(19.4)
        d = c.evaluate(inputs(new_due, room_c=19.4, arrival_ts=arrival + 25 * MIN))
        assert d.state == P.STATE_DUE and d.started_arrival_ts == arrival + 25 * MIN

    def test_a_slip_of_15_min_keeps_it(self):
        c = P.PreheatController()
        c.evaluate(inputs(T0, room_c=19.0, arrival_ts=T0 + 50 * MIN))
        assert c.heating_since == T0
        d = c.evaluate(inputs(T0 + 5 * MIN, room_c=19.4, arrival_ts=T0 + 65 * MIN))
        assert d.state == P.STATE_DUE and d.ended is None

    def test_a_slip_that_is_still_due_keeps_it(self):
        c = P.PreheatController()
        c.evaluate(inputs(T0, room_c=16.0, arrival_ts=T0 + 30 * MIN))
        d = c.evaluate(inputs(T0 + 5 * MIN, room_c=16.1, arrival_ts=T0 + 90 * MIN))
        assert d.state == P.STATE_DUE

    def test_the_trip_home_ending_stops_it_after_10_min(self):
        c = P.PreheatController()
        c.evaluate(inputs(T0, arrival_ts=T0 + 30 * MIN))
        d = c.evaluate(inputs(T0 + MIN, arrival_ts=None))
        assert d.state == P.STATE_DUE                      # Home ETA reloading?
        d = c.evaluate(inputs(T0 + 10 * MIN, arrival_ts=None))
        assert d.state == P.STATE_DUE
        d = c.evaluate(inputs(T0 + 11 * MIN, arrival_ts=None))
        assert d.state == P.STATE_WAITING and d.ended == P.ENDED_LOST

    def test_nothing_new_starts_while_the_arrival_is_unknown(self):
        c = P.PreheatController()
        d = c.evaluate(inputs(T0, arrival_ts=None))
        assert d.state == P.STATE_WAITING and d.due_ts is None

    def test_the_4_h_limit(self):
        c = P.PreheatController()
        c.evaluate(inputs(T0, room_c=5.0, rate_c_per_h=0.3, arrival_ts=T0 + 6 * 3600))
        assert c.heating_since == T0
        d = c.evaluate(inputs(T0 + 4 * 3600 - 1, room_c=5.0, rate_c_per_h=0.3,
                              arrival_ts=T0 + 6 * 3600))
        assert d.state == P.STATE_DUE
        d = c.evaluate(inputs(T0 + 4 * 3600, room_c=5.0, rate_c_per_h=0.3,
                              arrival_ts=T0 + 6 * 3600))
        assert d.state == P.STATE_WAITING and d.ended == P.ENDED_CAP
        # Not again on this trip home …
        d = c.evaluate(inputs(T0 + 5 * 3600, room_c=5.0, rate_c_per_h=0.3,
                              arrival_ts=T0 + 6 * 3600))
        assert d.state == P.STATE_WAITING
        # … but on the next one, once this one is over.
        for minute in range(0, 12):
            c.evaluate(inputs(T0 + 6 * 3600 + minute * MIN, arrival_ts=None))
        d = c.evaluate(inputs(T0 + 7 * 3600, arrival_ts=T0 + 7 * 3600 + 10 * MIN))
        assert d.state == P.STATE_DUE

    def test_a_break_does_not_re_arm_the_4_h_limit(self):
        """Review finding: Home ETA stuck on an arrival 10 min away, the limit
        reached, then a window opened and shut: 4 more hours of heating."""
        c = P.PreheatController()
        kw = dict(room_c=19.0)
        c.evaluate(inputs(T0, arrival_ts=T0 + 10 * MIN, **kw))
        t = T0 + 4 * 3600
        d = c.evaluate(inputs(t, arrival_ts=t + 10 * MIN, **kw))
        assert d.ended == P.ENDED_CAP and d.capped
        for reason in (P.INACTIVE_WINDOW, P.INACTIVE_CHANGE_BY_HAND, P.INACTIVE_OFF,
                       P.INACTIVE_SUMMER, P.INACTIVE_NOTHING_WARMER,
                       P.INACTIVE_OFF_BY_HAND):
            t += MIN
            c.evaluate(inputs(t, inactive_reason=reason, arrival_ts=t + 10 * MIN, **kw))
            t += MIN
            d = c.evaluate(inputs(t, arrival_ts=t + 10 * MIN, **kw))
            assert d.state == P.STATE_WAITING, reason
        # Someone home ends the trip: the next one may heat.
        c.evaluate(inputs(t + MIN, inactive_reason=P.INACTIVE_HOME))
        d = c.evaluate(inputs(t + 2 * MIN, arrival_ts=t + 12 * MIN, **kw))
        assert d.state == P.STATE_DUE

    def test_a_break_while_heating_keeps_the_trip_s_start(self):
        c = P.PreheatController()
        c.evaluate(inputs(T0, room_c=19.0, arrival_ts=T0 + 10 * MIN))
        t = T0 + 2 * 3600
        d = c.evaluate(inputs(t, inactive_reason=P.INACTIVE_WINDOW, arrival_ts=t + 10 * MIN))
        assert d.state == P.STATE_INACTIVE and c.heating_since is None
        d = c.evaluate(inputs(t + 10 * MIN, room_c=19.0, arrival_ts=t + 20 * MIN))
        assert d.state == P.STATE_DUE and d.since_ts == t + 10 * MIN
        d = c.evaluate(inputs(T0 + 4 * 3600, room_c=19.0, arrival_ts=T0 + 4 * 3600 + 5 * MIN))
        assert d.ended == P.ENDED_CAP

    def test_the_trip_ends_with_its_arrival_even_during_a_break(self):
        c = P.PreheatController()
        c.evaluate(inputs(T0, room_c=19.0, arrival_ts=T0 + 10 * MIN))
        c.evaluate(inputs(T0 + 4 * 3600, room_c=19.0, arrival_ts=T0 + 4 * 3600 + 5 * MIN))
        t = T0 + 4 * 3600 + MIN
        c.evaluate(inputs(t, inactive_reason=P.INACTIVE_WINDOW, arrival_ts=None))
        c.evaluate(inputs(t + 11 * MIN, inactive_reason=P.INACTIVE_WINDOW, arrival_ts=None))
        d = c.evaluate(inputs(t + 12 * MIN, room_c=19.0, arrival_ts=t + 20 * MIN))
        assert d.state == P.STATE_DUE and not d.capped

    @pytest.mark.parametrize("reason", [P.INACTIVE_HOME, P.INACTIVE_NO_PRESENCE, P.INACTIVE_NO_INPUTS])
    def test_reasons_that_end_the_trip(self, reason):
        c = P.PreheatController()
        c.evaluate(inputs(T0, room_c=19.0, arrival_ts=T0 + 10 * MIN))
        c.evaluate(inputs(T0 + 4 * 3600, room_c=19.0, arrival_ts=T0 + 4 * 3600 + 5 * MIN))
        c.evaluate(inputs(T0 + 4 * 3600 + MIN, inactive_reason=reason))
        d = c.evaluate(inputs(T0 + 4 * 3600 + 2 * MIN, room_c=19.0,
                              arrival_ts=T0 + 4 * 3600 + 10 * MIN))
        assert d.state == P.STATE_DUE

    def test_the_4_h_count_from_the_trip_s_first_start(self):
        """Slips and restarts can't stretch one trip home beyond 4 h."""
        c = P.PreheatController()
        arrival = T0 + 50 * MIN
        c.evaluate(inputs(T0, room_c=19.0, arrival_ts=arrival))
        assert c.heating_since == T0
        c.evaluate(inputs(T0 + 5 * MIN, room_c=19.0, arrival_ts=arrival + 3 * 3600))
        assert c.heating_since is None
        later = arrival + 3 * 3600
        d = c.evaluate(inputs(later - needed_s(19.0), room_c=19.0, arrival_ts=later))
        assert d.state == P.STATE_DUE
        d = c.evaluate(inputs(T0 + 4 * 3600, room_c=19.0, arrival_ts=later))
        assert d.ended == P.ENDED_CAP

    def test_no_room_reading_starts_nothing_and_keeps_what_runs(self):
        c = P.PreheatController()
        d = c.evaluate(inputs(T0, room_c=None, arrival_ts=T0 + 10 * MIN))
        assert d.state == P.STATE_WAITING and d.needed_s is None
        c.evaluate(inputs(T0 + MIN, arrival_ts=T0 + 10 * MIN))
        d = c.evaluate(inputs(T0 + 2 * MIN, room_c=None, arrival_ts=T0 + 10 * MIN))
        assert d.state == P.STATE_DUE

    def test_defaults_until_learned(self):
        d = P.PreheatController().evaluate(inputs(
            T0, room_c=19.0, rate_c_per_h=None, dead_time_s=None, arrival_ts=T0 + 3600
        ))
        # 20 min + 1.0 °C / (0.8 × 1.0 °C/h) + 5 min
        assert d.needed_s == pytest.approx(20 * MIN + 75 * MIN + 5 * MIN)
        assert not d.rate_learned and not d.dead_time_learned
        for bad in (0.0, -1.0, math.inf, math.nan):
            d = P.PreheatController().evaluate(inputs(T0, rate_c_per_h=bad))
            assert d.rate_c_per_h == 1.0 and not d.rate_learned

    def test_the_settings(self):
        cfg = parameters.PreheatConfig(lead_s=0.0, rate_factor=1.0)
        d = P.PreheatController(cfg).evaluate(inputs(T0, room_c=17.5, arrival_ts=T0 + 9e3))
        assert d.needed_s == pytest.approx(20 * MIN + 60 * MIN)

    def test_due_and_close_together(self):
        c = P.PreheatController()
        d = c.evaluate(inputs(T0, close=True, arrival_ts=T0 + 5 * MIN))
        assert d.state == P.STATE_DUE and d.level == P.LEVEL_CLOSE


# ===========================================================================
# Persistence and the clock
# ===========================================================================

class TestPersistence:
    def test_round_trip_keeps_the_start_and_the_cap(self):
        c = P.PreheatController()
        c.evaluate(inputs(T0, room_c=16.0, arrival_ts=T0 + 3600, close=True))
        c.evaluate(inputs(T0 + MIN, room_c=16.0, arrival_ts=T0 + 3600))
        data = c.as_dict()
        back = P.PreheatController()
        assert back.restore(data, T0 + 2 * MIN)
        assert back.heating_since == T0
        d = back.evaluate(inputs(T0 + 3 * MIN, room_c=16.0, arrival_ts=T0 + 3600))
        assert d.state == P.STATE_DUE and d.level == P.LEVEL_CLOSE  # drop pending
        assert d.level_drop_ts == T0 + MIN + 10 * MIN
        d = back.evaluate(inputs(T0 + 4 * 3600, room_c=16.0, arrival_ts=T0 + 5 * 3600))
        assert d.ended == P.ENDED_CAP

    @pytest.mark.parametrize("data", [None, {}, {"version": 99}, "x", {"version": 1, "level": 7}])
    def test_malformed_starts_afresh(self, data):
        c = P.PreheatController()
        c.restore(data, T0)
        assert c.heating_since is None
        assert c.evaluate(inputs(T0)).state == P.STATE_WAITING

    def test_times_from_the_future_count_as_now(self):
        c = P.PreheatController()
        c.restore({"version": 1, "level": 2, "since": T0 + 3600, "since_arrival": T0 + 7200,
                   "trip_since": T0 + 3600, "drop_since": T0 + 3600,
                   "arrival_missing_since": T0 + 3600}, T0)
        assert c.heating_since == T0

    def test_the_clock_going_back_is_no_cap(self):
        c = P.PreheatController()
        c.evaluate(inputs(T0, arrival_ts=T0 + 30 * MIN))
        d = c.evaluate(inputs(T0 - 3600, arrival_ts=T0 + 30 * MIN))
        assert d.state == P.STATE_DUE and d.since_ts == T0 - 3600


# ===========================================================================
# The log lines
# ===========================================================================

def _hhmm(ts):
    return f"T+{int(round((ts - T0) / 60))}"


class TestDescribe:
    cfg = parameters.PreheatConfig()

    def _line(self, d, prev=None, **kw):
        return P.describe(d, prev, self.cfg, _hhmm, **kw)

    def test_due(self):
        d = P.PreheatController().evaluate(inputs(T0, room_c=17.0, arrival_ts=T0 + 60 * MIN))
        assert self._line(d) == (
            "heading home: it would heat to 20.0 °C now for the expected arrival "
            "at T+60: from 17.0 °C it needs 115 min (start-up 20 min, 3.0 °C at "
            "0.8 × 2.5 °C/h, lead 5 min)"
        )

    def test_the_readme_example(self):
        """The living room coming home on 23 Oct 2026 (README, preheat.py)."""
        d = P.PreheatController().evaluate(inputs(
            T0, room_c=17.35, rate_c_per_h=2.52, dead_time_s=22.3 * MIN,
            arrival_ts=T0 + 3600,
        ))
        line = P.describe(d, None, self.cfg, lambda _ts: "18:42")
        assert line == (
            "heading home: it would heat to 20.0 °C now for the expected arrival "
            "at 18:42: from 17.4 °C it needs 106 min (start-up 22 min, 2.6 °C at "
            "0.8 × 2.5 °C/h, lead 5 min)"
        )
        readme = (Path(__file__).parent.parent / "README.md").read_text()
        assert " ".join(f"Preheat (shadow): {line}".split()) in " ".join(readme.split())

    def test_due_with_defaults(self):
        d = P.PreheatController().evaluate(inputs(
            T0, room_c=19.0, rate_c_per_h=None, dead_time_s=None, arrival_ts=T0
        ))
        assert "start-up 20 min (default)" in self._line(d)
        assert "0.8 × 1.0 °C/h (default)" in self._line(d)

    def test_nearby_and_close(self):
        c = P.PreheatController()
        d = c.evaluate(inputs(T0, nearby=True))
        assert self._line(d) == (
            "someone is nearby: it would heat to 19.4 °C (20.0 less 0.58 °C, what "
            "it recovers in 30 min)"
        )
        d = P.PreheatController().evaluate(inputs(T0, nearby=True, recovery_c=None))
        assert "the default until it has learned" in self._line(d)
        d = c.evaluate(inputs(T0 + MIN, close=True))
        assert self._line(d).startswith("someone is close to home: it would heat to 20.0 °C")

    def test_slipped(self):
        c = P.PreheatController()
        prev = c.evaluate(inputs(T0, room_c=19.0, arrival_ts=T0 + 50 * MIN))
        assert prev.state == P.STATE_DUE
        d = c.evaluate(inputs(T0 + 5 * MIN, room_c=19.4, arrival_ts=T0 + 75 * MIN))
        assert self._line(d, prev) == (
            "the expected arrival slipped to T+75 (25 min later than when it "
            "started), so it would stop heating; back to Away 16.0 °C; for the "
            "expected arrival at T+75 it would start at T+32"
        )

    def test_lost_and_cap(self):
        c = P.PreheatController()
        prev = c.evaluate(inputs(T0, arrival_ts=T0 + 30 * MIN))
        c.evaluate(inputs(T0 + MIN, arrival_ts=None))
        d = c.evaluate(inputs(T0 + 11 * MIN, arrival_ts=None))
        assert self._line(d, prev).startswith(
            "the trip home is over (no expected arrival for 10 min), so it "
            "would stop heating; back to Away 16.0 °C"
        )
        c = P.PreheatController()
        prev = c.evaluate(inputs(T0, room_c=5.0, rate_c_per_h=0.3, arrival_ts=T0 + 9 * 3600))
        d = c.evaluate(inputs(T0 + 4 * 3600, room_c=5.0, rate_c_per_h=0.3,
                              arrival_ts=T0 + 9 * 3600))
        assert self._line(d, prev) == (
            "it would stop: 240 min since it first started for this trip home; "
            "back to Away 16.0 °C"
        )
        assert d.capped

    def test_waiting(self):
        c = P.PreheatController()
        d = c.evaluate(inputs(T0))
        assert self._line(d, P.PreheatDecision(P.STATE_INACTIVE)) == (
            "nobody home: the room stays at Away 16.0 °C until someone is nearby "
            "or heading home"
        )
        prev = c.evaluate(inputs(T0 + MIN, nearby=True))
        for minute in range(2, 13):
            d = c.evaluate(inputs(T0 + minute * MIN))
        assert self._line(d, prev) == "nobody is nearby any more: back to Away 16.0 °C"

    def test_off_by_hand(self):
        inactive = P.PreheatDecision(
            P.STATE_INACTIVE, inactive_reason=P.INACTIVE_OFF_BY_HAND)
        waiting = P.PreheatDecision(P.STATE_WAITING, target_c=16.0)
        assert self._line(inactive, waiting) == (
            "stands aside: an Off picked by hand holds the room until someone is home"
        )

    def test_inactive_and_off(self):
        inactive = P.PreheatDecision(P.STATE_INACTIVE, inactive_reason=P.INACTIVE_HOME)
        waiting = P.PreheatDecision(P.STATE_WAITING, target_c=16.0)
        assert self._line(inactive, waiting) == "stands aside: someone is home"
        assert self._line(inactive, None) is None
        assert self._line(inactive, P.PreheatDecision(P.STATE_INACTIVE)) is None
        assert self._line(P.PreheatDecision(P.STATE_OFF), waiting) == "switched off"
        assert self._line(P.PreheatDecision(P.STATE_OFF), None) is None

    def test_ignored_arrival(self):
        d = P.PreheatController().evaluate(inputs(T0, arrival_ts=T0 + 20 * 3600))
        assert self._line(d).endswith("an expected arrival more than 12 h ahead is ignored")

    def test_a_new_reason_to_stand_aside_is_a_new_decision(self):
        home = P.PreheatDecision(P.STATE_INACTIVE, inactive_reason=P.INACTIVE_HOME)
        off = P.PreheatDecision(P.STATE_INACTIVE, inactive_reason=P.INACTIVE_OFF)
        assert home.key != off.key
        assert self._line(off, home) is None              # but no line for it

    def test_logged_decision_round_trip(self):
        d = P.PreheatController().evaluate(inputs(T0, nearby=True))
        logged = P.LoggedDecision(d, T0, {"target_c": 19.4})
        back = P.LoggedDecision.from_dict(logged.as_dict())
        assert back.decision.key == d.key and back.ts == T0
        assert back.attributes == {"target_c": 19.4}
        assert P.LoggedDecision.from_dict({"version": 1, "state": "nope", "ts": 1}) is None
        assert P.LoggedDecision.from_dict(None) is None


# ===========================================================================
# The 30-minute recovery
# ===========================================================================

def feed(learner, start, series, target_before=18.0, target=20.0, every=300.0, **kw):
    """Cycles ``every`` s apart: one at the old target, then the series at
    the new one.  Returns the episodes completed."""
    out = []
    learner.observe(start - every, target_before, series[0], usable=True, window=False)
    for i, room in enumerate(series):
        e = learner.observe(start + i * every, target, room, usable=True, window=False, **kw)
        if e is not None:
            out.append(e)
    return out


# The owner's rooms, 5-min means of the room sensor from the step on
# (18-25 Oct 2026 statistics).
LIVING_21_OCT = [17.943, 17.989, 18.15, 18.365, 18.577, 18.771, 18.958, 19.113]
SECOND_22_OCT = [18.933, 18.978, 19.043, 19.155, 19.3, 19.435, 19.549, 19.634]
STUDY_24_OCT = [19.107, 19.134, 19.22, 19.322, 19.5, 19.68, 19.849, 20.075]
MASTER_23_OCT = [18.211, 18.214, 18.27, 18.367, 18.496, 18.605, 18.684]


class TestRecovery:
    def test_an_episode_that_reaches_the_target_is_no_measure(self):
        """Review finding: a 0.6 °C step reached in 20 min would book a rise
        of about 0.6 whatever the room can do."""
        r = P.RecoveryLearner()
        assert feed(r, T0, [19.4, 19.5, 19.65, 19.8, 19.95, 20.0, 20.0],
                    target_before=19.4) == []
        assert not r.running
        # Within 0.1 °C only at the 30 minutes: dropped too.
        assert feed(r, T0 + 86400, [19.3, 19.4, 19.5, 19.6, 19.7, 19.8, 19.92],
                    target_before=19.0) == []
        # There at 20 min, back down at 30 (a coast, a hold): dropped too.
        assert feed(r, T0 + 3 * 86400, [19.3, 19.5, 19.7, 19.8, 19.95, 19.85, 19.75],
                    target_before=19.0) == []
        # Short by more: kept.
        (e,) = feed(r, T0 + 2 * 86400, [19.3, 19.4, 19.5, 19.6, 19.7, 19.8, 19.85],
                    target_before=19.0)
        assert e.rise_c == pytest.approx(0.55)

    def test_replay_of_four_mornings(self):
        r = P.RecoveryLearner()
        assert r.recovery_c is None
        (living,) = feed(r, T0, LIVING_21_OCT)
        assert living.rise_c == pytest.approx(18.958 - 17.943)
        assert living.deficit_c == pytest.approx(20.0 - 17.943)
        assert r.recovery_c is None          # one heat-up is not enough
        feed(r, T0 + 86400, SECOND_22_OCT)
        assert r.recovery_c == pytest.approx(((18.958 - 17.943) + (19.549 - 18.933)) / 2)
        feed(r, T0 + 2 * 86400, STUDY_24_OCT)
        feed(r, T0 + 3 * 86400, MASTER_23_OCT, target_before=16.0)
        rises = sorted(e.rise_c for e in r.episodes)
        assert r.recovery_c == pytest.approx((rises[1] + rises[2]) / 2)

    def test_interpolates_to_30_min(self):
        r = P.RecoveryLearner()
        r.observe(T0 - 60, 18.0, 18.0, usable=True, window=False)
        r.observe(T0, 20.0, 18.0, usable=True, window=False)
        r.observe(T0 + 600, 20.0, 18.1, usable=True, window=False)
        r.observe(T0 + 1200, 20.0, 18.3, usable=True, window=False)
        r.observe(T0 + 1700, 20.0, 18.5, usable=True, window=False)
        e = r.observe(T0 + 1900, 20.0, 18.7, usable=True, window=False)
        assert e.rise_c == pytest.approx(0.6)

    @pytest.mark.parametrize(("before", "room"), [
        (19.6, 19.0),   # a step of 0.4 °C
        (18.0, 19.6),   # 0.4 °C short of the new target
    ])
    def test_small_steps_and_small_deficits_are_not_episodes(self, before, room):
        r = P.RecoveryLearner()
        assert feed(r, T0, [room] * 8, target_before=before) == []
        assert not r.running

    def test_a_target_change_cuts_it(self):
        r = P.RecoveryLearner()
        r.observe(T0 - 60, 18.0, 18.0, usable=True, window=False)
        r.observe(T0, 20.0, 18.0, usable=True, window=False)
        assert r.running
        r.observe(T0 + 600, 18.0, 18.3, usable=True, window=False)
        assert not r.running
        assert feed(r, T0 + 3600, [18.0] * 8, target_before=18.0, target=18.0) == []

    def test_a_window_drops_it_and_holds_the_next_off_30_min(self):
        r = P.RecoveryLearner()
        r.observe(T0 - 60, 18.0, 18.0, usable=True, window=False)
        r.observe(T0, 20.0, 18.0, usable=True, window=False)
        r.observe(T0 + 300, 20.0, 17.0, usable=True, window=True)
        assert not r.running
        # 20 min after the window: no start.
        assert feed(r, T0 + 1500, [18.0] * 8) == []
        # 40 min after: starts.
        assert len(feed(r, T0 + 7200, [18.0, 18.2, 18.4, 18.6, 18.7, 18.8, 18.9])) == 1

    def test_an_unusable_cycle_drops_it(self):
        r = P.RecoveryLearner()
        r.observe(T0 - 60, 18.0, 18.0, usable=True, window=False)
        r.observe(T0, 20.0, 18.0, usable=True, window=False)
        r.observe(T0 + 60, 20.0, None, usable=False, window=False)
        assert not r.running
        # No step seen across the gap in usable cycles.
        r.observe(T0 + 120, 20.0, 18.0, usable=True, window=False)
        assert not r.running

    def test_a_gap_drops_it(self):
        r = P.RecoveryLearner()
        r.observe(T0 - 60, 18.0, 18.0, usable=True, window=False)
        r.observe(T0, 20.0, 18.0, usable=True, window=False)
        r.observe(T0 + 601, 20.0, 18.3, usable=True, window=False)
        assert not r.running
        assert r.episodes == []

    def test_keeps_the_last_ten(self):
        r = P.RecoveryLearner()
        for day in range(12):
            feed(r, T0 + day * 86400, [18.0 + 0.1 * k * (1 + day / 10) for k in range(8)])
        assert len(r.episodes) == 10
        assert r.episodes[0].ts == pytest.approx(T0 + 2 * 86400)

    def test_round_trip_and_a_reload_mid_episode(self):
        r = P.RecoveryLearner()
        feed(r, T0, LIVING_21_OCT)
        r.observe(T0 + 86400 - 60, 18.0, 18.9, usable=True, window=False)
        r.observe(T0 + 86400, 20.0, 18.9, usable=True, window=False)
        r.observe(T0 + 86400 + 300, 20.0, 19.0, usable=True, window=False)
        back = P.RecoveryLearner()
        assert back.restore(r.as_dict(), T0 + 86400 + 400)
        assert back.running and len(back.episodes) == 1
        for i in range(2, 7):
            e = back.observe(T0 + 86400 + i * 300, 20.0, 19.0 + 0.1 * i, usable=True,
                             window=False)
        assert e is not None and e.rise_c == pytest.approx(0.7)

    def test_restore_drops_the_future_and_junk(self):
        r = P.RecoveryLearner()
        feed(r, T0, LIVING_21_OCT)
        data = r.as_dict()
        back = P.RecoveryLearner()
        assert back.restore(data, T0 - 3600)            # the clock went back
        assert back.episodes == [] and not back.running
        assert not back.restore({"version": 2}, T0)
        assert not back.restore(None, T0)
        data["episodes"].append({"ts": "x"})
        assert back.restore(data, T0 + 86400)
        assert len(back.episodes) == 1


# ===========================================================================
# Every setting and state has its words
# ===========================================================================

_ROOT = Path(__file__).parent.parent / "custom_components" / "roomstat"


@pytest.mark.parametrize("lang", ["strings.json", "translations/en.json", "translations/de.json"])
def test_every_setting_and_state_is_explained(lang):
    import json

    d = json.loads((_ROOT / lang).read_text())
    sections = d["options"]["step"]["init"]["sections"]
    main = sections["preheat"]
    keys = {"preheat_mode", "preheat_arrival_entity_id", "preheat_close_entity_id",
            "preheat_nearby_entity_id"}
    assert set(main["data"]) == keys and set(main["data_description"]) == keys
    timing = sections["preheat_timing"]
    specs = {spec.key for spec in P.TIMING_OPTIONS}
    assert set(timing["data"]) == specs and set(timing["data_description"]) == specs
    assert all(len(text) > 40 for text in timing["data_description"].values())
    assert set(d["selector"]["preheat_mode"]["options"]) == set(P.MODES)
    assert set(d["entity"]["sensor"]["preheat"]["state"]) == set(P.STATES)
    assert d["entity"]["sensor"]["preheat_recovery"]["name"]


def test_strings_and_en_are_identical():
    assert (_ROOT / "strings.json").read_text() == (_ROOT / "translations/en.json").read_text()


class TestOptions:
    def test_defaults_are_the_config_defaults(self):
        assert P.config_from_options({}) == parameters.PreheatConfig()
        by_key = {spec.key: spec for spec in P.TIMING_OPTIONS}
        assert by_key["preheat_lead_min"].default == 5.0
        assert by_key["preheat_rate_factor"].default == 0.8
        assert by_key["preheat_max_min"].default == 240.0
        assert by_key["preheat_fallback_dead_min"].default == 20.0
        for spec in P.TIMING_OPTIONS:
            assert spec.minimum <= spec.default <= spec.maximum, spec.key

    def test_values_in_their_units(self):
        cfg = P.config_from_options({
            "preheat_lead_min": 7, "preheat_rate_factor": "0.75", "preheat_slip_min": 30.0,
            "preheat_max_min": 180, "preheat_drop_delay_min": 0,
            "preheat_arrival_grace_min": 15, "preheat_recovery_default_c": 0.6,
            "preheat_fallback_rate_c_per_h": 1.5, "preheat_fallback_dead_min": 25,
        })
        assert cfg == parameters.PreheatConfig(
            lead_s=420.0, rate_factor=0.75, slip_s=1800.0, cap_s=10800.0,
            fall_delay_s=0.0, arrival_grace_s=900.0, recovery_default_c=0.6,
            fallback_rate_c_per_h=1.5, fallback_dead_time_s=1500.0,
        )

    @pytest.mark.parametrize("bad", [None, True, "x", math.nan, math.inf, -1, 0.1, 99, [1]])
    def test_bad_values_keep_the_default(self, bad):
        assert P.config_from_options({"preheat_rate_factor": bad}).rate_factor == 0.8
