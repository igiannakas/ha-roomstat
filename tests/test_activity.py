"""The activity log (activity.py, climate_activity.py, the activity sensor).

What the owner asked for (25 Oct 2026): one sensor per room that reads like
a debug log -- what Roomstat is doing and why, why it does *not* do
something (no full heat, the integral resting, a command waiting), the
valve's own reading and what the valve has been told, plus events and the
auto-tuner's verdicts, including why it changed nothing.
"""
from __future__ import annotations

import dataclasses
import logging

import pytest

from tests.ha_harness import (
    WINDOW,
    _event,
    _Extra,
    _fire_last_timer,
    _make_entity,
    _run,
    _sensor,
    _start,
    e2e,
    reset_timers,
)
from tests.pure_modules import activity as act
from tests.pure_modules import autotune as A
from tests.pure_modules import parameters as P

CFG = P.RegulationConfig()          # Kp 4, Td 10 min, stop lead 10 min


def _facts(**kw):
    base = dict(path=act.PATH_REGULATED, setpoint_c=20.0, target_label="Comfort",
                room_c=19.5, trv_c=19.2, trv_heating=True, tado="21.1", tado_c=21.1,
                mode="hold", prev_mode="hold", error_c=0.5, slope_c_per_h=0.0,
                command_c=21.1, p_c=2.0, i_c=0.0, send="already_at_target")
    base.update(kw)
    return act.CycleFacts(**base)


def _line(**kw):
    return act.describe_cycle(_facts(**kw), CFG)


# ---------------------------------------------------------------------------
# Decision lines
# ---------------------------------------------------------------------------

class TestDecisionLines:

    def test_every_line_names_the_valves_reading_and_what_it_was_told(self):
        cases = [
            dict(),
            dict(mode="heat", prev_mode="hold", error_c=1.8, command_c=25.0, tado="25.0"),
            dict(mode="coast", prev_mode="heat", error_c=0.7, slope_c_per_h=3.3,
                 command_c=5.0, command_off=True, tado="off", tado_c=None),
            dict(mode="stop", prev_mode="stop", error_c=-0.4, command_off=True,
                 command_c=5.0, tado="off", tado_c=None),
            dict(window=act.WINDOW_OPEN, mode="stop", command_off=True, tado="off"),
            dict(path=act.PATH_SUMMER, mode=None),
            dict(path=act.PATH_OFF, mode=None, tado="off"),
            dict(path=act.PATH_SENSOR_LOST, mode=None, sensor_lost_min=16),
            dict(path=act.PATH_WAITING, mode=None, trv_c=None),
            dict(path=act.PATH_NO_DATA, mode=None),
        ]
        for kw in cases:
            line = _line(**kw)
            assert " | TRV " in line and " | Tado " in line, line
            assert len(act.clip(line)) <= act.STATE_MAX_LEN

    def test_heat_up_start_says_why(self):
        line = _line(mode="heat", prev_mode="hold", error_c=1.8, command_c=25.0,
                     tado="25.0", slope_c_per_h=None)
        assert line.startswith("Heat-up: 1.8 below (0.8 or more heats at full power)")

    def test_full_heat_says_what_stopping_now_would_leave(self):
        # 25 Oct 2026, living room, 06:33: 0.74 to go, rising 3.25 °C/h.
        line = _line(mode="heat", prev_mode="heat", error_c=0.74, slope_c_per_h=3.25,
                     command_c=25.0, tado="25.0")
        # Td 10 min: (3.25 - 0.1) / 6 = 0.53 °C of coast, 0.21 left.
        assert "rising 3.2 °C/h; stops once the leftover heat covers the rest" in line
        assert "(stopping now would leave 0.2)" in line

    def test_coast_says_why_the_heat_stopped(self):
        line = _line(mode="coast", prev_mode="heat", error_c=0.5, slope_c_per_h=3.1,
                     command_c=5.0, command_off=True, tado="off", tado_c=None)
        assert line.startswith(
            "Coasting: valve shut with 0.5 to go; leftover heat predicted to land it "
            "(3.0 °C/h × 10 min)"
        )
        assert line.endswith("| Tado off")

    def test_a_full_push_says_it_ends_like_a_heat_up(self):
        """26 Oct 06:00, second bedroom: 0.76 below, +3.1 over the valve."""
        line = _line(error_c=0.76, slope_c_per_h=0.0, full_push=True, command_c=22.3,
                     trv_c=19.2, p_c=3.04, i_c=-0.7, tado="22.3", tado_c=22.3)
        assert line.startswith(
            "Holding, full push: 0.8 below, valve pushed 3.1 over its reading (P +3.0, "
            "I -0.70; I decays outside ±0.3); stops once the leftover heat covers the "
            "rest (stopping now would leave 0.8) |"
        )
        assert "full heat only from" not in line
        ended = _line(mode="coast", prev_mode="hold", error_c=0.42, slope_c_per_h=1.3,
                      command_c=5.0, command_off=True, tado="off", tado_c=None,
                      td_s=1500.0)
        assert ended.startswith(
            "Full push ended, coasting: valve shut with 0.4 to go; leftover heat "
            "predicted to land it (1.2 °C/h × 25 min)"
        )

    def test_a_full_push_is_a_new_decision(self):
        """It changes how the hold ends, so it is a new line at once."""
        assert act.decision_key(_facts(full_push=True), CFG) != act.decision_key(_facts(), CFG)

    def test_hold_says_why_not_more(self):
        """0.5 short after a coast: why no full heat, why the integral rests."""
        line = _line(prev_mode="coast", error_c=0.48, command_c=21.1, trv_c=19.2,
                     p_c=1.44, i_c=0.0)
        assert line.startswith(
            "Holding (the coast stalled 0.5 short): 0.5 below, valve pushed 1.9 over its "
            "reading (P +1.4, I +0.00; I decays outside ±0.3; full heat only from 0.8 below)"
        )
        assert "| room 19.5 → 20.0 Comfort | TRV 19.2 heating | Tado 21.1" in line

    def test_hold_near_the_target_has_no_excuses(self):
        line = _line(error_c=0.1, command_c=19.9, trv_c=19.5, p_c=0.4, i_c=-0.0001)
        assert "(P +0.4, I +0.00)" in line            # no '-0.00'
        assert "decays" not in line and "full heat only" not in line

    def test_hold_released_from_a_stop_says_so(self):
        line = _line(prev_mode="stop", error_c=0.05, slope_c_per_h=-0.3)
        assert line.startswith("Holding (released: heading 0.1 below within 20 min)")

    def test_hold_released_by_a_target_step_says_how_far_below(self):
        """26 Oct 06:00, second bedroom: Night → Comfort, 0.76 below at once
        (it read 'heading 0.8 below within 20 min')."""
        line = _line(prev_mode="stop", error_c=0.76, slope_c_per_h=0.0)
        assert line.startswith("Holding (released): 0.76 below, valve pushed")
        assert "full heat only from 0.8 below" in line

    def test_stop_ahead_of_an_overshoot(self):
        # The study, 25 Oct 06:25: 0.25 below, rising 1.15 °C/h, braking
        # time 25 min.
        line = _line(mode="stop", prev_mode="hold", error_c=0.25, slope_c_per_h=1.15,
                     command_c=5.0, command_off=True, tado="off", tado_c=None, td_s=1500.0)
        assert line.startswith(
            "Valve off ahead of an overshoot: 0.2 below, rising 1.1 °C/h would pass 20.1 "
            "within 25 min"
        )

    def test_a_new_stop_just_above_the_target_names_the_overshoot(self):
        """Entered just above the target, still rising: the overshoot forecast
        shut it, never "so it stays off" on the cycle that shuts it."""
        line = _line(mode="stop", prev_mode="hold", error_c=-0.05, slope_c_per_h=0.6,
                     command_c=5.0, command_off=True, tado="off", tado_c=None, td_s=1500.0)
        assert line.startswith("Valve off ahead of an overshoot: 0.1 above, rising 0.6 °C/h")
        assert "stays off" not in line

    def test_a_stop_says_when_it_heats_again(self):
        line = _line(mode="stop", prev_mode="stop", error_c=-0.36, slope_c_per_h=0.8,
                     command_c=5.0, command_off=True, tado="off", tado_c=None)
        assert line.startswith(
            "Valve off: 0.4 above, rising 0.8 °C/h; heats again at 19.9, or 20 min ahead "
            "of it once falling |"
        )
        falling = _line(mode="stop", prev_mode="stop", error_c=0.02, slope_c_per_h=-0.2,
                        command_c=5.0, command_off=True, tado="off", tado_c=None)
        # 0.02 + 20 min x (0.2 - 0.1) °C/h = 0.05: not yet the 0.1 that releases.
        assert "falling 0.2 °C/h; heats again 20 min before it reaches 19.9 (forecast for " \
            "20 min ahead: 0.05 below)" in falling

    def test_a_stop_still_rising_says_the_overshoot_keeps_it_off(self):
        """26 Oct 06:42, second bedroom: 0.11 below, rising 1.3 °C/h on the
        radiator's leftover heat.  It read 'heats again once heading 0.1
        below within 20 min (predicted: 0.11 below)' -- the release rule, met
        on paper, while the overshoot forecast was what kept it shut."""
        line = _line(mode="stop", prev_mode="stop", error_c=0.11, slope_c_per_h=1.3,
                     command_c=5.0, command_off=True, tado="off", tado_c=None, td_s=1512.0)
        assert line.startswith(
            "Valve off: 0.1 below, still rising 1.3 °C/h; on that slope it would pass "
            "20.1 within 25 min, so it stays off |"
        )

    def test_a_stop_entered_above_the_target_is_no_overshoot_warning(self):
        line = _line(mode="stop", prev_mode="hold", error_c=-0.3, slope_c_per_h=0.0,
                     command_c=5.0, command_off=True, tado="off", tado_c=None)
        assert line.startswith("Valve off: 0.3 above, flat; heats again at 19.9")

    def test_a_heat_up_ended_by_a_lower_target_is_no_coast(self):
        """25 Oct 17:12, second bedroom: Frost protection picked during a
        heat-up; for one cycle it read 'Coasting: valve shut with -11.7 to
        go; leftover heat predicted to land it'."""
        line = _line(mode="coast", prev_mode="heat", error_c=-11.65, slope_c_per_h=None,
                     setpoint_c=5.0, command_c=5.0, command_off=True, tado="off",
                     tado_c=None)
        assert line.startswith("Heat-up ended: valve shut, 11.7 above the target |")

    def test_a_coast_a_few_thousandths_short_is_still_coasting(self):
        """28 Oct 06:45, study: the coast was 0.004 short, the result's error
        rounds to 0.00, and the line read 'Heat-up ended: valve shut, on the
        target' between two 'Coasting' lines.  The regulator stops a coast at
        or above the target, so a coast that carries on is short of it."""
        line = _line(mode="coast", prev_mode="coast", error_c=0.0, slope_c_per_h=0.2,
                     command_c=5.0, command_off=True, tado="off", tado_c=None)
        assert line.startswith("Coasting: valve shut, 0.0 to go, still rising 0.2 °C/h |")
        # Entered this cycle at the target (a heat-up whose target fell to
        # the room): ended, as before.
        line = _line(mode="coast", prev_mode="heat", error_c=0.0, slope_c_per_h=0.2,
                     command_c=5.0, command_off=True, tado="off", tado_c=None)
        assert line.startswith("Heat-up ended: valve shut, on the target |")

    def test_the_first_decision_after_a_start_is_no_transition(self):
        """A shut valve resumes as coast and becomes a stop: no coast happened."""
        line = _line(mode="stop", prev_mode="coast", resumed=True, error_c=-0.2,
                     command_c=5.0, command_off=True, tado="off", tado_c=None)
        assert line.startswith("Valve off: 0.2 above")
        assert _line(prev_mode="stop", resumed=True).startswith("Holding (resumed)")

    def test_the_valves_own_state_is_shown(self):
        assert "| TRV 19.2 off | Tado 21.1" in _line(trv_off=True)
        assert "| TRV 19.2 on its Tado schedule |" in _line(trv_scheduled=True)
        key = act.decision_key(_facts(), CFG)
        assert act.decision_key(_facts(trv_off=True), CFG) != key

    def test_the_braking_time_the_cycle_used(self):
        """The tuner may change Td after the decision: the line uses the old."""
        line = _line(mode="coast", prev_mode="heat", error_c=0.5, slope_c_per_h=3.1,
                     command_c=5.0, command_off=True, tado="off", tado_c=None, td_s=780.0)
        assert "(3.0 °C/h × 13 min)" in line

    def test_a_long_line_keeps_its_numbers(self):
        line = _line(target_label="Comfort, nobody home (change by hand kept)" + "x" * 120,
                     prev_mode="stop")
        assert len(line) <= act.STATE_MAX_LEN
        assert line.endswith("| TRV 19.2 heating | Tado 21.1")

    def test_sensor_lost_shows_the_last_reading(self):
        line = _line(path=act.PATH_SENSOR_LOST, mode=None, room_c=None, room_last_c=19.4,
                     sensor_lost_min=16)
        assert line.startswith("Room sensor unavailable for 16 min: not regulating")
        assert "| room unavailable (last 19.4) → 20.0 Comfort |" in line

    def test_window_lines(self):
        assert _line(window=act.WINDOW_OPEN).startswith("Window open: valve off until it closes")
        assert _line(window=act.WINDOW_REWARM).startswith(
            "Window closed: valve off while the room re-warms by itself")
        assert _line(window=act.WINDOW_PENDING).startswith(
            "Window just opened (valve off after its delay): Holding")
        assert _line(window=act.WINDOW_IGNORED).startswith(
            "Window open, not acted on until it next opens: Holding")

    @pytest.mark.parametrize(("send", "kw", "note"), [
        ("rate_limited(120s)", dict(command_c=21.4), " (21.4 waits: rate limit)"),
        ("settling(12s)", dict(command_c=25.0), " (25.0 waits: a change by hand settling)"),
        ("send_failed(normal_update)", dict(command_c=21.4), " (21.4 failed)"),
        ("already_at_target", dict(command_c=21.2), " (21.2 wanted: under the 0.3 step)"),
        ("already_at_target", dict(command_c=21.1), ""),
        ("sent(normal_update)", dict(command_c=21.1), ""),
    ])
    def test_why_the_valve_does_not_have_the_command(self, send, kw, note):
        assert _line(send=send, **kw).endswith(f"| Tado 21.1{note}")

    def test_numbers_alone_do_not_change_the_key(self):
        a = _facts(room_c=19.5, trv_c=19.2, p_c=1.4)
        b = _facts(room_c=19.6, trv_c=19.4, p_c=1.0, error_c=0.4)
        assert act.decision_key(a, CFG) == act.decision_key(b, CFG)
        c = _facts(mode="stop", command_off=True, command_c=5.0)
        d = _facts(send="rate_limited(60s)", command_c=21.5)
        assert act.decision_key(c, CFG) != act.decision_key(a, CFG)
        assert act.decision_key(d, CFG) != act.decision_key(a, CFG)


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------

class TestActivityLog:

    def test_a_decision_is_written_when_it_changes_or_every_5_min(self):
        log = act.ActivityLog()
        assert log.add_decision(0.0, "Holding: a", "k1")
        assert not log.add_decision(60.0, "Holding: a", "k1")       # same line
        assert not log.add_decision(120.0, "Holding: b", "k1")      # numbers only
        assert log.doing == "Holding: b"                            # live
        assert log.add_decision(180.0, "Valve off", "k2")           # a new decision
        assert not log.add_decision(240.0, "Valve off.", "k2")
        assert log.add_decision(180.0 + act.DECISION_REFRESH_S, "Valve off..", "k2")

    def test_after_an_event_the_decision_comes_again(self):
        log = act.ActivityLog()
        log.add_decision(0.0, "Holding", "k")
        log.add_event(10.0, "Schedule switching to comfort")
        assert log.add_decision(60.0, "Holding", "k")
        assert [e.text for e in log.entries()] == [
            "Holding", "Schedule switching to comfort", "Holding"]

    def test_keeps_the_last_30_lines(self):
        log = act.ActivityLog()
        for k in range(40):
            log.add_event(float(k), f"line {k}")
        lines = log.entries()
        assert len(lines) == act.MAX_LINES
        assert lines[0].text == "line 39" and lines[-1].text == "line 10"

    def test_round_trip_and_malformed_data(self):
        log = act.ActivityLog()
        log.add_decision(1_800_000_001.0, "Holding", "k")
        log.add_event(1_800_000_002.0, "Window open")
        new = act.ActivityLog()
        assert new.restore(log.as_dict())
        assert [(e.text, e.decision) for e in new.entries()] == [
            ("Window open", False), ("Holding", True)]
        for bad in (None, {}, {"lines": "x"}, {"lines": [[1.0, 2, True]]},
                    {"lines": [[float("nan"), "x", True]]}, {"lines": [[1.0, "x"]]}):
            assert not act.ActivityLog().restore(bad)

    def test_the_state_fits_home_assistant(self):
        long = "auto-tune: " + "x" * 600
        assert len(act.clip(long)) == act.STATE_MAX_LEN
        assert act.clip(long).endswith("…")

    def test_a_long_event_becomes_several_whole_lines(self):
        words = " ".join(f"word{k}" for k in range(120))         # ~830 characters
        log = act.ActivityLog()
        assert log.add_event(1.0, words) == 4
        lines = [e.text for e in reversed(log.entries())]
        assert all(len(x) <= act.STATE_MAX_LEN for x in lines)
        assert lines[0].endswith(" …") and lines[1].startswith("… ")
        joined = " ".join(x.removeprefix("… ").removesuffix(" …") for x in lines)
        assert joined == words

    def test_restore_keeps_the_stored_order_and_skips_corrupt_times(self):
        data = {"lines": [[1_800_000_100.0, "a", False], [1_800_000_000.0, "b", False],
                          [1e18, "corrupt", False], [5.0, "too old", True]]}
        log = act.ActivityLog()
        assert log.restore(data)
        assert [e.text for e in reversed(log.entries())] == ["a", "b"]


class TestRoomLogAdapter:

    def test_info_reaches_the_log_whatever_the_level(self, caplog):
        got = []
        logger = logging.getLogger("roomstat_test.activity")
        logger.setLevel(logging.WARNING)            # Home Assistant's default
        adapter = act.RoomLogAdapter(logger, "Study", lambda lvl, t: got.append((lvl, t)))
        adapter.info("window open, %s", "switching to frost protection")
        adapter.debug("not for the log")
        with caplog.at_level(logging.WARNING, logger="roomstat_test.activity"):
            adapter.warning("room sensor unavailable for %d min", 16)
        assert got == [(logging.INFO, "window open, switching to frost protection"),
                       (logging.WARNING, "room sensor unavailable for 16 min")]
        assert "Study: room sensor unavailable for 16 min" in caplog.text
        # The record names the caller, not the adapter.
        assert caplog.records[-1].funcName == "test_info_reaches_the_log_whatever_the_level"
        assert act.event_text(logging.WARNING, got[1][1]).startswith("Warning: room sensor")
        assert act.event_text(logging.INFO, got[0][1]).startswith("Window open")

    def test_a_bad_format_never_raises(self):
        got = []
        adapter = act.RoomLogAdapter(logging.getLogger("roomstat_test.bad"), "X",
                                     lambda lvl, t: got.append(t))
        adapter.info("%d min", "not a number")
        assert got == ["%d min"]


# ---------------------------------------------------------------------------
# Auto-tune verdicts: what it changed, and why it did not
# ---------------------------------------------------------------------------

def _heatup(ts, overshoot, *, td=780.0, coast=420.0, truncated=False, crossed=True):
    return A.HeatupMetrics(
        ts=ts, start_ts=ts - 3600, setpoint_c=20.0, step_c=1.8, overshoot_c=overshoot,
        theta_s=900.0, rate_c_per_h=2.5, demand0_c=5.0, coast_s=coast, coast_rise_c=0.3,
        kp=3.0, ki=0.0003, td_s=td, truncated=truncated, crossed=crossed,
    )


DAY = 86400.0


class TestTunerVerdicts:

    def _tuner(self):
        return A.Autotuner(P.AutotuneConfig(), A.Tuning(3.0, 0.001, 660.0),
                           derivative_allowed=True)

    def test_an_undershoot_is_a_vote_then_less_braking(self):
        t = self._tuner()
        t._params = A.Tuning(3.0, 0.001, 780.0)
        t._on_heatup(_heatup(DAY, -0.35), DAY)
        assert t.verdicts == 1
        # The coast model (7 min) wants less braking: a smoothed step, 30 % of
        # the 6-min gap, which says where it is heading; more heat is on trial.
        assert t.last_verdict == (
            "stopped 0.35 °C short: vote 1 of 2 for less braking; changed braking "
            "13.0→11.2 min, a step towards 7.0 (on trial: judged on the next 2 heat-ups)")
        t._on_heatup(_heatup(2 * DAY, -0.30), 2 * DAY)
        assert t.last_verdict == (
            "stopped 0.30 °C short: less braking (×0.85, 2 of 2 votes); no change while "
            "the last one is on trial (1 of 2 heat-ups judged)")

    def test_a_wanted_change_inside_the_rate_limit_says_when(self):
        t = self._tuner()
        t._params = A.Tuning(3.0, 0.001, 780.0)
        t._last_change_ts = DAY - 3600.0             # changed an hour ago
        t._votes["td_down"] = 1
        t._on_heatup(_heatup(DAY, -0.3), DAY)
        assert "a change towards more heat waits 12 h after the last one" in t.last_verdict
        assert t.last_verdict_until == pytest.approx(DAY - 3600.0 + 12 * 3600.0)

    def test_an_overshoot_brakes_more(self):
        t = self._tuner()
        t._on_heatup(_heatup(DAY, 0.47, td=660.0, coast=660.0), DAY)
        assert t.last_verdict.startswith("overshoot +0.47 °C: more braking (×1.15)")

    def test_a_cut_short_heat_up_is_no_vote(self):
        t = self._tuner()
        t._on_heatup(_heatup(DAY, -0.4, truncated=True), DAY)
        assert t.last_verdict.startswith("stopped 0.40 °C short but cut short: no vote")

    def test_on_target_and_nothing_to_change(self):
        t = self._tuner()
        t._on_heatup(_heatup(DAY, 0.15, td=660.0, coast=660.0), DAY)
        assert t.last_verdict.startswith("landed +0.15 °C: on target, no vote; no change:")
        assert "are at their targets" in t.last_verdict

    def test_a_hold_that_barely_heated_is_not_judged(self):
        t = self._tuner()
        t._on_hold(A.HoldMetrics(ts=DAY, mean_error_c=-0.05, sd_c=0.05, half_cycles=0,
                                 period_s=None, amplitude_c=0.1, heating_fraction=0.07,
                                 oscillating=False), DAY)
        assert t.last_verdict.startswith(
            "the valve heated 7% of the 3 h (needs 20%): Kp and Ki are judged only while it heats")

    def test_a_calm_hold_says_why_kp_and_ki_stay(self):
        t = self._tuner()
        t._on_hold(A.HoldMetrics(ts=DAY, mean_error_c=-0.05, sd_c=0.05, half_cycles=0,
                                 period_s=None, amplitude_c=0.1, heating_fraction=0.3,
                                 oscillating=False), DAY)
        assert t.last_verdict.startswith(
            "no swing: Kp stays (it moves only on a swing); no lasting offset (-0.05 °C, "
            "needs 0.15 under): no Ki vote; no change:")

    def test_the_journal_keeps_every_step_in_order(self):
        """A cut-short heat-up judged and the next started in one sample:
        both are logged, each with its own label."""
        t = self._tuner()
        seq = t.journal_seq
        t.last_event = "heat-up (cut short): overshoot unknown"
        t._set_verdict(["no overshoot vote (cut short below the target)"])
        t.last_event = "heat-up started"
        texts = [text for _n, text, _u in t.journal_since(seq)]
        assert texts == ["heat-up (cut short): overshoot unknown → no overshoot vote "
                         "(cut short below the target); no change:"[:0] + texts[0],
                         "heat-up started"]
        assert texts[0].startswith("heat-up (cut short): overshoot unknown → no overshoot vote")

    def test_a_reset_is_journaled_and_verdicts_carry_on(self):
        t = self._tuner()
        t._on_heatup(_heatup(DAY, 0.15, td=660.0, coast=660.0), DAY)
        seq = t.journal_seq
        t.reset()
        t._on_heatup(_heatup(2 * DAY, 0.15, td=660.0, coast=660.0), 2 * DAY)
        texts = [text for _n, text, _u in t.journal_since(seq)]
        assert texts[0] == "reset to configured values"
        assert texts[1].startswith("heat-up: overshoot +0.15 °C")
        assert "→ landed +0.15 °C: on target, no vote" in texts[1]

    def test_a_change_on_trial_holds_the_rest(self):
        t = self._tuner()
        t._pending = {"ts": 0.0, "prev": A._tuning_dict(t._params), "ref_overshoot": None,
                      "ref_osc": False, "needs": 2, "overshoots": []}
        t._on_heatup(_heatup(DAY, 0.15, td=660.0, coast=660.0), DAY)
        assert "no change while the last one is on trial (1 of 2 heat-ups judged)" in \
            t.last_verdict


# ---------------------------------------------------------------------------
# The entity and the sensor
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self, monkeypatch, start=1_800_000_000.0):
        self.now = start
        monkeypatch.setattr("time.time", lambda: self.now)

    def tick(self, s=60.0):
        self.now += s


def _feed(ent, room, internal, setpoint, *, off=False, heating=False):
    ent.coordinator.data = {
        "room_temp": room, "room_temp_ts": None,
        "tado_internal_temp": internal, "tado_setpoint": None if off else setpoint,
        "tado_min_temp": 5.0, "tado_max_temp": 25.0, "tado_off": off,
        "tado_cloud": False, "tado_scheduled": False, "tado_heating": heating,
        "fallback_internal_temp": None, "fallback_setpoint": None,
    }


def _cycle(ent):
    _run(ent._async_regulation_cycle("timer"))


def _texts(ent):
    return [e.text for e in reversed(ent._activity.entries())]     # oldest first


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


@e2e
class TestEntity:

    def _start(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _make_entity()
        _run(_start(ent))
        sensor = _sensor.RoomstatActivitySensor(ent.coordinator, ent._config_entry)
        ent.coordinator.climate_entity = ent
        ent.coordinator.activity_entity = sensor
        return clock, ent, sensor

    def test_the_sensor_shows_the_latest_line_and_the_log(self, monkeypatch):
        clock, ent, sensor = self._start(monkeypatch)
        _feed(ent, 18.2, 16.0, 5.0, off=True)
        _cycle(ent)
        state = sensor.native_value
        assert state.startswith("Heat-up: 2.3 below (0.8 or more heats at full power)")
        # The heat command went out; the valve still reports off.
        assert "| room 18.2 → 20.5 Comfort | TRV 16.0 off | Tado 25.0" in state
        attrs = sensor.extra_state_attributes
        assert list(attrs) == ["log"]                           # nothing that drifts
        assert attrs["log"][0].endswith(state)                  # newest first
        assert attrs["log"][1].endswith("Started (Home Assistant restart or room reload)"
                                         "; nothing saved to take back")
        assert sensor._unrecorded_attributes == frozenset({"log"})
        # The live line is on the thermostat, not recorded.
        assert ent.extra_state_attributes["activity"] == state
        assert "activity" in ent._unrecorded_attributes

    def test_each_line_is_written_as_its_own_state(self, monkeypatch):
        """An event and the decision after it in one cycle: both reach the
        recorder (the history is the log)."""
        clock, ent, sensor = self._start(monkeypatch)
        sensor.hass = ent.hass
        written = []
        sensor.async_write_ha_state = lambda: written.append(sensor.native_value)
        _feed(ent, 18.2, 16.0, 5.0, off=True)
        _cycle(ent)
        clock.tick(120)                 # the window's off is no quick heat -> off
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        _fire_last_timer()
        assert "Window open, switching to frost protection" in written
        assert written[-1].startswith("Window open: valve off until it closes")
        before = len(written)
        clock.tick()
        _feed(ent, 18.0, 16.0, 5.0, off=True)
        _cycle(ent)                                       # the same decision: no line
        assert all(w == written[before - 1] for w in written[before:])

    def test_window_events_and_lines(self, monkeypatch):
        clock, ent, _ = self._start(monkeypatch)
        _feed(ent, 20.5, 19.0, 20.5)
        _cycle(ent)
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        _fire_last_timer()
        clock.tick()
        _feed(ent, 19.8, 18.0, 5.0, off=True)
        _cycle(ent)
        texts = _texts(ent)
        assert "Window open, switching to frost protection" in texts
        assert texts[-1].startswith("Window open: valve off until it closes")
        assert "| room 19.8 → 5.0 window |" in texts[-1]

    def test_a_failed_command_is_an_event_and_on_the_line(self, monkeypatch):
        clock, ent, _ = self._start(monkeypatch)

        async def refuse(*_a, **_k):
            raise RuntimeError("bridge timed out")

        ent.hass.services.async_call = refuse
        _feed(ent, 18.2, 16.0, 18.0)
        _cycle(ent)
        texts = _texts(ent)
        assert any(t.startswith("Error: ") or t.startswith("Warning: ") for t in texts)
        assert "(25.0 failed" in texts[-1] and "| Tado 18.0" in texts[-1]

    def test_the_log_survives_a_reload(self, monkeypatch):
        clock, ent, _ = self._start(monkeypatch)
        _feed(ent, 18.2, 16.0, 5.0, off=True)
        _cycle(ent)
        before = _texts(ent)
        data = ent.extra_restore_state_data.as_dict()
        new = _make_entity()
        _run(_start(new, extra=_Extra(data)))
        after = _texts(new)
        assert after[: len(before)] == before
        assert after[-1] == "Started (Home Assistant restart or room reload)"

    def test_a_restart_does_not_log_the_tuners_old_news(self, monkeypatch):
        clock, ent, _ = self._start(monkeypatch)
        ent._autotune_enabled = True
        ent._autotuner.last_event = "heat-up: overshoot -0.35 °C"
        _feed(ent, 20.5, 19.0, 20.5)
        _cycle(ent)
        data = ent.extra_restore_state_data.as_dict()
        new = _make_entity({"autotune_enabled": True})
        _run(_start(new, extra=_Extra(data)))
        new.coordinator.data = ent.coordinator.data
        clock.tick()
        _cycle(new)
        after_start = _texts(new)[_texts(new).index(
            "Started (Home Assistant restart or room reload)"):]
        assert not any(t.startswith("Auto-tune: ") for t in after_start)

    def test_an_auto_tune_verdict_is_logged_once(self, monkeypatch):
        clock, ent, _ = self._start(monkeypatch)
        ent._autotune_enabled = True
        t = ent._autotuner
        _feed(ent, 19.0, 18.0, 20.5)
        _cycle(ent)
        t.last_event = "heat-up: overshoot -0.35 °C, dead time 15 min, coast 7 min"
        t._set_verdict(["stopped 0.35 °C short: vote 1 of 2 for less braking"])
        for _ in range(3):
            clock.tick()
            _cycle(ent)
        lines = [x for x in _texts(ent) if x.startswith("Auto-tune: ")]
        assert len(lines) == 1
        assert lines[0].startswith(
            "Auto-tune: heat-up: overshoot -0.35 °C, dead time 15 min, coast 7 min → "
            "stopped 0.35 °C short: vote 1 of 2 for less braking")

    def test_events_are_logged_with_the_logger_at_its_default(self, monkeypatch):
        clock, ent, _ = self._start(monkeypatch)
        for name in ("roomstat_e2e", "roomstat_e2e.climate_presets"):
            monkeypatch.setattr(logging.getLogger(name), "level", logging.WARNING)
        _feed(ent, 20.5, 19.0, 20.5)
        _cycle(ent)
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        _fire_last_timer()
        assert "Window open, switching to frost protection" in _texts(ent)

    def test_facts_cover_every_cycle_path(self, monkeypatch):
        """No cycle path ends without a decision line."""
        clock, ent, _ = self._start(monkeypatch)
        _cycle(ent)                                          # no data yet
        assert _texts(ent)[-1].startswith("Waiting for the room sensor: nothing sent")
        _feed(ent, 20.0, None, None)
        clock.tick()
        _cycle(ent)
        assert _texts(ent)[-1].startswith("Waiting for the valve's reading")
        ent.coordinator.last_update_success = False
        clock.tick()
        _cycle(ent)
        assert _texts(ent)[-1].startswith("Cycle skipped: no fresh data")
        ent.coordinator.last_update_success = True
        ent._hvac_mode = type(ent._hvac_mode)("off")
        clock.tick()
        _feed(ent, 20.0, 19.0, 20.5)
        _cycle(ent)
        assert _texts(ent)[-1].startswith("Switched off: valve kept off")

    def test_an_open_window_is_named_for_what_it_does(self, monkeypatch):
        """'Valve off' is promised only while the open delay runs.  25 Oct
        2026: it read 'Window just opened (valve off soon)' for 40 s after the
        window protection had been dropped, with nothing due."""
        clock, ent, _ = self._start(monkeypatch)
        _feed(ent, 20.5, 19.0, 20.5)
        ent.hass.set(WINDOW, "on")
        clock.tick()
        _cycle(ent)
        assert _texts(ent)[-1].startswith("Window open, not acted on until it next opens:")
        ent._async_window_changed(_event("on"))       # the open delay runs
        clock.tick()
        _cycle(ent)
        assert _texts(ent)[-1].startswith("Window just opened (valve off after its delay):")

    def test_the_line_names_the_target_it_decided_on(self, monkeypatch):
        """25 Oct 17:12: 'room 16.6 → 20.0 Frost protection' -- the target of
        the decision, the preset of a change made while its command went out."""
        clock, ent, _ = self._start(monkeypatch)
        ent._preset_mode = "frost_protection"
        ent._activity_cycle(clock.now, act.PATH_WAITING, room_c=16.6, setpoint_c=20.0,
                            decided_as=("Comfort", None))
        assert "| room 16.6 → 20.0 Comfort |" in _texts(ent)[-1]


def test_cycle_facts_fields_are_documented_defaults():
    """CycleFacts stays a plain dataclass with defaults (built field by field)."""
    names = {f.name for f in dataclasses.fields(act.CycleFacts)}
    assert {"path", "trv_c", "tado", "command_c", "send"} <= names
