"""The regulator's dosed top-ups (regulation.py, 2.15.0): the two phases (a
heat-up from heatup_enter_c, the dosing phase below it, no hold), the
dead-time release, the pulse, its end and its cap on the room's own heating
rate, the coordinator's early fire with its floor-landing guard, and the
steady state the rule produces on a room that matches its model.  HA-free
(tests/pure_modules.py)."""
from __future__ import annotations

import dataclasses
import math
import random

import pytest

from tests.pure_modules import parameters as P
from tests.pure_modules import regulation as R

H = 3600.0
NOW = 1_800_000_000.0
THETA, TAU = 300.0, 480.0
LEAD, HORIZON = THETA + TAU / 2, THETA + 2 * TAU          # 540 s, 1260 s
GAIN = 0.045


def _reg(**overrides) -> R.FeedforwardPiRegulator:
    return R.FeedforwardPiRegulator(P.RegulationConfig(**overrides))


def _settled(mode=R.MODE_STOP, **kw) -> R.RegulationState:
    """A state in the dosing phase on 20.0 (the room reached it), shut
    unless told."""
    base = dict(mode=mode, settled=True, last_room_c=None)
    base.update(kw)
    return R.RegulationState(**base)


def _dose(reg, state, room, *, sp=20.0, slope_c_per_h=0.0, now=NOW, inflight=0.0,
          tail=0.0, gain=GAIN, fire_now=None, bias=0.0, dosing=True, trv=None, dt=60.0,
          cap_gain=None, cap_inflight=0.0, cap_slope_c_per_h=None, unbooked_until=None,
          loss_slope_c_per_h=None):
    res = reg.compute(
        setpoint_c=sp, room_temp_c=room,
        tado_internal_c=room - 3.0 if trv is None else trv,
        time_delta_s=dt, state=state,
        room_slope_c_per_s=None if slope_c_per_h is None else slope_c_per_h / H,
        dosing=dosing, inflight_c=inflight, tail_c=tail, gain_c_per_open_min=gain,
        theta_s=THETA, tau_s=TAU, bias_c=bias, now_ts=now, fire_now=fire_now,
        cap_gain_c_per_open_min=cap_gain, cap_inflight_c=cap_inflight,
        cap_loss_slope_c_per_s=None if cap_slope_c_per_h is None else cap_slope_c_per_h / H,
        unbooked_until_ts=unbooked_until,
        loss_slope_c_per_s=None if loss_slope_c_per_h is None else loss_slope_c_per_h / H,
    )
    return res, res.new_state


# ---------------------------------------------------------------------------
# The option off: exactly today
# ---------------------------------------------------------------------------

class TestOff:
    def test_dose_is_a_mode(self):
        assert R.MODE_DOSE in R.MODES
        s = R.RegulationState()
        assert (s.settled, s.pulse_start_ts, s.last_pulse_start_ts, s.last_pulse_end_ts,
                s.pulse_cap_s) == (False, None, None, None, None)

    def test_the_dose_inputs_change_nothing_while_off(self):
        """A scripted random day (heat-ups, coasts, holds, stops, full
        pushes, target changes, draughts): with dosing off, every existing
        result and state field is the same as a call without the dose
        inputs at all, however they are set."""
        legacy = [f.name for f in dataclasses.fields(R.RegulationResult)
                  if f.name in {"target_for_tado_c", "feedforward_offset_c", "p_correction_c",
                                "i_correction_c", "error_c", "is_saturated", "mode",
                                "room_held", "full_push", "lookahead_c"}]
        state_fields = ("integral_c", "mode", "last_room_c", "full_push")
        for seed in range(8):
            rnd = random.Random(seed)
            a_reg, b_reg = _reg(), _reg()
            sa, sb = R.RegulationState(), R.RegulationState()
            room, trv, sp = 18.5, 15.0, 20.0
            for k in range(500):
                if rnd.random() < 0.01:
                    sp = rnd.choice([16.0, 18.0, 20.0, 21.0, 5.0])
                room += rnd.gauss(0, 0.03) + (0.01 if rnd.random() < 0.3 else -0.004)
                if rnd.random() < 0.005:
                    room -= 0.7
                trv += rnd.gauss(0, 0.2)
                slope = rnd.choice([None, rnd.gauss(0, 1.5) / H])
                kw = dict(setpoint_c=sp, room_temp_c=room, tado_internal_c=trv,
                          time_delta_s=60.0, room_slope_c_per_s=slope)
                a = a_reg.compute(state=sa, **kw)
                b = b_reg.compute(state=sb, dosing=False, now_ts=NOW + 60 * k,
                                  inflight_c=0.3, tail_c=0.1, gain_c_per_open_min=GAIN,
                                  theta_s=THETA, tau_s=TAU, bias_c=0.1, fire_now="burn", **kw)
                for f in legacy:
                    assert getattr(a, f) == getattr(b, f), (seed, k, f)
                for f in state_fields:
                    assert getattr(a.new_state, f) == getattr(b.new_state, f), (seed, k, f)
                assert b.mode != R.MODE_DOSE and not b.settled and not b.new_state.settled
                assert b.fired_by is None and b.dose_end is None
                sa, sb = a.new_state, b.new_state

    def test_dosing_needs_a_time(self):
        res, st = _dose(_reg(), _settled(), 19.85, now=None)
        assert not res.settled and not st.settled and res.mode != R.MODE_DOSE

    def test_a_pulse_running_when_dosing_stops_is_a_coast(self):
        """Window, boost, the option or a failure: the pulse counts as a
        just-ended maximum, and its start still counts for the refractory
        time once dosing resumes."""
        st = _settled(mode=R.MODE_DOSE, pulse_start_ts=NOW - 100, last_pulse_start_ts=NOW - 5000)
        res, new = _dose(_reg(), st, 19.9, slope_c_per_h=0.5, dosing=False)
        assert res.mode == R.MODE_COAST                   # below, rising: a coast
        assert new.last_pulse_start_ts == NOW - 100 and new.pulse_start_ts is None
        assert new.last_pulse_end_ts == NOW and not new.settled
        res, _ = _dose(_reg(), st, 20.05, dosing=False)
        assert res.mode == R.MODE_STOP                    # at or above: a stop
        quiet = R.RegulationState(mode=R.MODE_STOP, last_pulse_start_ts=NOW - 900)
        _, new = _dose(_reg(), quiet, 19.9, dosing=False)
        assert new.last_pulse_start_ts == NOW - 900       # carried


# ---------------------------------------------------------------------------
# The two phases: a heat-up from heatup_enter_c, the dosing phase below it
# ---------------------------------------------------------------------------

ENTER = P.RegulationConfig().heatup_enter_c


class TestPhases:
    def test_a_stop_is_the_dosing_phase(self):
        res, st = _dose(_reg(), R.RegulationState(mode=R.MODE_STOP), 20.1)
        assert res.settled and st.settled and res.mode == R.MODE_STOP

    def test_a_hold_resumed_is_the_dosing_phase_never_a_hold(self):
        res, st = _dose(_reg(), R.RegulationState(mode=R.MODE_HOLD), 19.95)
        assert res.settled and res.mode == R.MODE_STOP

    def test_far_above_the_target_too(self):
        res, _ = _dose(_reg(), R.RegulationState(mode=R.MODE_STOP), 21.5)
        assert res.settled and res.mode == R.MODE_STOP and res.fired_by is None

    def test_a_shortfall_just_under_heatup_enter_is_dosed(self):
        """0.79 below: one pulse sized for it, not a hold (owner, 31 Oct
        2026: a heat-up or a dose, nothing in between)."""
        res, st = _dose(_reg(), _settled(), 20.0 - (ENTER - 0.01))
        assert res.settled and res.mode == R.MODE_DOSE and res.fired_by == "release"
        assert res.need_c == pytest.approx(ENTER - 0.01, abs=1e-6)
        assert st.pulse_start_ts == NOW

    def test_from_heatup_enter_it_is_a_heat_up(self):
        res, st = _dose(_reg(), _settled(), 20.0 - ENTER)
        assert not res.settled and not st.settled and res.mode == R.MODE_HEAT
        assert res.target_for_tado_c == 25.0

    def test_a_heat_up_runs_to_its_coast_then_dosing_takes_the_rest(self):
        """This morning's case (31 Oct 2026, living room): the heat-up's
        coast stalled 0.2 short; the second go is a dosed pulse, not a hold."""
        reg = _reg()
        res, st = _dose(reg, R.RegulationState(mode=R.MODE_HEAT), 19.6, slope_c_per_h=2.6)
        assert res.mode == R.MODE_COAST and not res.settled      # its coast lands it
        res, st = _dose(reg, st, 19.7, slope_c_per_h=1.0)
        assert res.mode == R.MODE_COAST and not res.settled      # still rising: coasting
        res, st = _dose(reg, st, 19.8, slope_c_per_h=0.0)        # stalled 0.2 short
        assert res.settled and res.mode == R.MODE_DOSE and res.fired_by == "release"
        assert res.need_c == pytest.approx(0.2, abs=1e-6)

    def test_a_coast_that_reaches_the_target_is_the_dosing_phase(self):
        res, _ = _dose(_reg(), R.RegulationState(mode=R.MODE_COAST), 20.02, slope_c_per_h=0.5)
        assert res.settled and res.mode == R.MODE_STOP

    def test_a_pulse_that_meets_heatup_enter_becomes_the_heat_up(self):
        """A window the sensor missed, a cold blast: the shortfall reaches
        heatup_enter_c mid-pulse; the 25 °C carries on as a heat-up."""
        st = _settled(mode=R.MODE_DOSE, pulse_start_ts=NOW - 240, pulse_cap_s=900.0)
        res, new = _dose(_reg(), st, 20.0 - ENTER - 0.05, slope_c_per_h=-1.0)
        assert res.mode == R.MODE_HEAT and res.dose_end == "heat_up" and not res.settled
        assert res.target_for_tado_c == 25.0
        assert new.pulse_start_ts is None and new.last_pulse_start_ts == NOW - 240
        assert new.last_pulse_end_ts == NOW and new.pulse_cap_s is None

    def test_never_a_hold_command_with_dosing_on(self):
        """Any mode, any room from 2 below to 2 above, any slope: with
        dosing on there is no hold, only the maximum or off."""
        reg, rnd = _reg(), random.Random(3)
        for _ in range(2000):
            st = R.RegulationState(
                mode=rnd.choice(list(R.MODES)), settled=rnd.random() < 0.5,
                pulse_start_ts=rnd.choice([None, NOW - 120, NOW - 900]),
                last_pulse_start_ts=rnd.choice([None, NOW - 300, NOW - 4000]),
                last_pulse_end_ts=rnd.choice([None, NOW - 60, NOW - 2000]),
                integral_c=rnd.uniform(-0.5, 0.5),
            )
            room = 20.0 + rnd.uniform(-2.0, 2.0)
            res, new = _dose(reg, st, room, slope_c_per_h=rnd.uniform(-3.0, 3.0),
                             inflight=rnd.choice([0.0, 0.01, 0.1, 0.5]),
                             fire_now=rnd.choice([None, "burn", "together"]),
                             cap_gain=rnd.choice([None, 0.02, 0.05, 0.1]))
            assert res.mode != R.MODE_HOLD
            assert res.target_for_tado_c in (5.0, 25.0)
            assert new.integral_c == pytest.approx(st.integral_c * (
                1.0 if abs(20.0 - room) < 0.3 else P.RegulationConfig().integral_decay))
            assert res.settled == (res.mode in (R.MODE_STOP, R.MODE_DOSE))

    def test_a_target_move_stays_in_the_dosing_phase(self):
        """The target raised 0.4 (by hand or a schedule): dosed, not
        handed to a hold."""
        res, _ = _dose(_reg(), _settled(), 20.0, sp=20.4)
        assert res.settled and res.mode == R.MODE_DOSE

    def test_the_heat_in_flight_counts(self):
        """0.25 below, but 0.1 still in flight: a virtual 0.15, a pulse."""
        res, _ = _dose(_reg(), _settled(), 19.75, inflight=0.1)
        assert res.settled and res.virtual_error_c == pytest.approx(0.15, abs=1e-6)
        assert res.mode == R.MODE_DOSE
        assert res.need_c == pytest.approx(0.15, abs=1e-6)

    def test_a_room_rising_by_itself_needs_less(self):
        """0.3 below but warming 0.6 °C/h by itself (sun, people): the dose
        is what it will not reach by itself, 0.3 - 0.6 x 21 min."""
        res, _ = _dose(_reg(), _settled(), 19.7, slope_c_per_h=0.6)
        assert res.mode == R.MODE_DOSE
        assert res.need_c == pytest.approx(0.3 - 0.6 / H * HORIZON, abs=1e-6)
        # Warming fast enough to get there by itself: no pulse, even a
        # minimum one would overshoot.
        res, _ = _dose(_reg(), _settled(), 19.7, slope_c_per_h=1.5)
        assert res.mode == R.MODE_STOP and res.fired_by is None


# ---------------------------------------------------------------------------
# The release
# ---------------------------------------------------------------------------

class TestRelease:
    def test_on_the_visible_dead_time(self):
        """Falling 0.5 °C/h: the release fires once the virtual error plus
        the cooling over theta + tau/2 (540 s, 0.075 °C) reaches 0.1: at
        0.025 below, not at 0.1."""
        reg = _reg()
        res, _ = _dose(reg, _settled(), 19.98, slope_c_per_h=-0.5)   # 0.02 below
        assert res.mode == R.MODE_STOP
        assert res.ready_in_s == pytest.approx((0.1 - 0.02 - LEAD * 0.5 / H) / (0.5 / H), abs=0.2)
        res, st = _dose(reg, _settled(), 19.97, slope_c_per_h=-0.5)  # 0.03 below
        assert res.mode == R.MODE_DOSE and res.fired_by == "release"
        assert res.target_for_tado_c == 25.0
        assert st.pulse_start_ts == NOW and res.pulse_s == 0.0

    def test_a_flat_room_at_the_band_floor(self):
        reg = _reg()
        res, _ = _dose(reg, _settled(), 19.91)
        assert res.mode == R.MODE_STOP and res.ready_in_s is None   # never due by itself
        res, _ = _dose(reg, _settled(), 19.90)
        assert res.mode == R.MODE_DOSE and res.fired_by == "release"

    def test_above_the_target_in_cold_weather(self):
        """Falling 0.8 °C/h the lead asks for a pulse 0.02 above the target:
        allowed (a 25 °C pulse cannot trickle as a hold command would)."""
        res, _ = _dose(_reg(), _settled(), 20.02, slope_c_per_h=-0.8)
        assert res.mode == R.MODE_DOSE and res.virtual_error_c < 0

    def test_heat_in_flight_is_taken_off_not_waited_for(self):
        """Readiness is the refractory time alone: heat in flight lowers the
        virtual error (0.15 - 0.06 = 0.09, under the band's floor: no pulse
        yet) but adds no wait of its own."""
        reg = _reg()
        res, _ = _dose(reg, _settled(), 19.85, inflight=0.06)
        assert res.mode == R.MODE_STOP and res.fired_by is None
        assert res.virtual_error_c == pytest.approx(0.09, abs=1e-6)
        assert res.ready_in_s is None                          # flat: never due by itself
        res, _ = _dose(reg, _settled(), 19.85, inflight=0.06, slope_c_per_h=-0.5)
        assert res.mode == R.MODE_DOSE                         # 0.09 + 0.075 >= 0.1
        res, _ = _dose(reg, _settled(), 19.85, inflight=0.04)
        assert res.mode == R.MODE_DOSE and res.fired_by == "release"

    def test_not_within_the_refractory_time(self):
        reg = _reg()
        res, _ = _dose(reg, _settled(last_pulse_start_ts=NOW - 600), 19.85)
        assert res.mode == R.MODE_STOP and res.ready_in_s is None
        res, _ = _dose(reg, _settled(last_pulse_start_ts=NOW - HORIZON), 19.85)
        assert res.mode == R.MODE_DOSE

    def test_the_refractory_time_is_capped(self):
        reg = _reg()
        st = _settled(last_pulse_start_ts=NOW - 1800)
        res = reg.compute(setpoint_c=20.0, room_temp_c=19.85, tado_internal_c=17.0,
                          time_delta_s=60.0, state=st, room_slope_c_per_s=0.0,
                          dosing=True, gain_c_per_open_min=GAIN, theta_s=900.0, tau_s=900.0,
                          now_ts=NOW)
        assert res.mode == R.MODE_DOSE                         # horizon 2700 s, capped at 1800

    def test_not_ahead_of_an_overshoot(self):
        """The measured stop rule applies in the settled state: a room 0.12
        below but rising 3 °C/h (people) gets no heat."""
        res, _ = _dose(_reg(), _settled(), 19.88, slope_c_per_h=3.0)
        assert res.mode == R.MODE_STOP and res.fired_by is None

    def test_from_a_hold_or_coast_resumed_after_a_reload(self):
        for mode in (R.MODE_HOLD, R.MODE_COAST):
            res, _ = _dose(_reg(), _settled(mode=mode), 19.95)
            assert res.mode == R.MODE_STOP                     # shut, never a hold
            res, _ = _dose(_reg(), _settled(mode=mode), 19.88)
            assert res.mode == R.MODE_DOSE

    def test_after_a_long_pulse_its_heat_must_show_first(self):
        """A 20-min pulse ended 5 min ago: its start is past the refractory,
        but its last minutes' heat has not started to show (theta + tau / 2
        after its end)."""
        reg = _reg()
        st = _settled(last_pulse_start_ts=NOW - 1500, last_pulse_end_ts=NOW - 300)
        res, _ = _dose(reg, st, 19.85)
        assert res.mode == R.MODE_STOP and res.ready_in_s is None
        st = _settled(last_pulse_start_ts=NOW - 1500, last_pulse_end_ts=NOW - LEAD)
        res, _ = _dose(reg, st, 19.85)
        assert res.mode == R.MODE_DOSE

    def test_a_minimum_pulse_that_would_overshoot_waits(self):
        """A fast room (by its heating rate 0.1 °C per open-minute): a
        minimum pulse gives 0.35, so at 0.15 below it would land 0.2 over;
        it waits until the room needs it (here 0.3 below)."""
        res, _ = _dose(_reg(), _settled(), 19.85, cap_gain=0.1)
        assert res.mode == R.MODE_STOP and res.fired_by is None
        res, _ = _dose(_reg(), _settled(), 19.7, cap_gain=0.1)
        assert res.mode == R.MODE_DOSE


# ---------------------------------------------------------------------------
# The coordinator's early fire
# ---------------------------------------------------------------------------

class TestFireNow:
    def test_a_burn_is_ridden_when_a_minimum_pulse_lands_in_the_band(self):
        """On target, falling 0.5 °C/h: the need is 0.175 (the cooling until
        the heat has landed); a 3-min pulse (3.5 open-min, 0.158) lands
        0.02 under: fire."""
        res, _ = _dose(_reg(), _settled(), 20.0, slope_c_per_h=-0.5, fire_now="burn")
        assert res.mode == R.MODE_DOSE and res.fired_by == "burn"
        assert res.need_c == pytest.approx(HORIZON * 0.5 / H, abs=1e-3)

    def test_refused_when_a_minimum_pulse_would_overshoot(self):
        """Flat, 0.05 below: a 3-min pulse would land 0.11 above."""
        res, _ = _dose(_reg(), _settled(), 19.95, fire_now="together")
        assert res.mode == R.MODE_STOP and res.fired_by is None

    def test_accepted_at_the_guard(self):
        """On target and falling (no release yet): the need is the cooling
        until the heat has landed; a minimum pulse (3.5 open-min, 0.1575)
        may land up to 0.05 above it."""
        floor = (180 - 60 + 90) / 60 * GAIN
        for need, fires in ((floor - 0.05 + 0.002, True), (floor - 0.05 - 0.002, False)):
            fall_c_per_h = need / HORIZON * H
            res, _ = _dose(_reg(), _settled(), 20.0, slope_c_per_h=-fall_c_per_h,
                           fire_now="together")
            assert res.ready_in_s is not None or res.mode == R.MODE_DOSE
            assert (res.mode == R.MODE_DOSE) is fires, need
            assert res.fired_by == ("together" if fires else None)

    def test_refused_without_a_gain_while_in_flight_or_unknown(self):
        reg = _reg()
        res, _ = _dose(reg, _settled(), 20.0, slope_c_per_h=-0.5, fire_now="burn", gain=0.0)
        assert res.mode == R.MODE_STOP
        res, _ = _dose(reg, _settled(last_pulse_start_ts=NOW - 600), 20.0,
                       slope_c_per_h=-0.5, fire_now="burn")
        assert res.mode == R.MODE_STOP                         # within the refractory time
        res, _ = _dose(reg, _settled(), 20.0, slope_c_per_h=-0.5, fire_now="please")
        assert res.mode == R.MODE_STOP


# ---------------------------------------------------------------------------
# The pulse
# ---------------------------------------------------------------------------

class TestPulse:
    def _pulsing(self, started_s_ago, **kw):
        return _settled(mode=R.MODE_DOSE, pulse_start_ts=NOW - started_s_ago, **kw)

    def test_at_least_the_minimum(self):
        res, st = _dose(_reg(), self._pulsing(120), 20.0, inflight=0.2)   # need < 0
        assert res.mode == R.MODE_DOSE and res.need_c < 0 and res.pulse_s == 120.0
        assert st.pulse_start_ts == NOW - 120

    def test_ends_once_covered(self):
        res, st = _dose(_reg(), self._pulsing(180, last_pulse_start_ts=NOW - 4000), 20.0,
                        inflight=0.2)
        assert res.mode == R.MODE_STOP and res.dose_end == "covered"
        assert res.target_for_tado_c == 5.0
        assert st.pulse_start_ts is None and st.last_pulse_start_ts == NOW - 180
        assert st.settled

    def test_goes_on_while_short(self):
        res, _ = _dose(_reg(), self._pulsing(240), 19.9, inflight=0.05)
        assert res.mode == R.MODE_DOSE and res.need_c > 0

    def test_the_tail_counts(self):
        """0.03 still needed, and 0.0675 to come from the close delay: covered."""
        res, _ = _dose(_reg(), self._pulsing(200), 19.92, inflight=0.05, tail=0.0675)
        assert res.mode == R.MODE_STOP and res.dose_end == "covered"

    def test_at_most_its_cap(self):
        """A room nothing is known about: the fixed maximum.  Otherwise the
        cap the pulse started with."""
        res, _ = _dose(_reg(), self._pulsing(720), 19.8)
        assert res.mode == R.MODE_STOP and res.dose_end == "max"
        res, _ = _dose(_reg(), self._pulsing(400, pulse_cap_s=400.0), 19.8, cap_gain=0.01)
        assert res.mode == R.MODE_STOP and res.dose_end == "max"
        res, _ = _dose(_reg(), self._pulsing(399, pulse_cap_s=400.0), 19.8, cap_gain=0.01)
        assert res.mode == R.MODE_DOSE and res.pulse_cap_s == 400.0

    def test_the_stop_rule_ends_it_at_once(self):
        res, _ = _dose(_reg(), self._pulsing(60), 20.05, slope_c_per_h=2.0)
        assert res.mode == R.MODE_STOP and res.dose_end == "stop_rule"

    def test_an_unknown_start_counts_from_now(self):
        """A pulse whose command was held back: its clock starts now, and it
        is reported as fired again (the cycle that sends it starts its
        episode)."""
        st = _settled(mode=R.MODE_DOSE, pulse_start_ts=None)
        res, new = _dose(_reg(), st, 19.9)
        assert res.mode == R.MODE_DOSE and res.pulse_s == 0.0 and new.pulse_start_ts == NOW
        assert res.fired_by == "release"
        res, _ = _dose(_reg(), new, 19.9, now=NOW + 60)
        assert res.mode == R.MODE_DOSE and res.fired_by is None

    def test_a_bigger_shortfall_mid_pulse_is_dosed_on(self):
        """0.4 below 2 min in (the room cooled faster): the pulse goes on,
        sized for what it now needs."""
        st = self._pulsing(120, last_pulse_start_ts=NOW - 5000)
        res, new = _dose(_reg(), st, 19.6)
        assert res.settled and res.mode == R.MODE_DOSE and res.dose_end is None
        assert new.pulse_start_ts == NOW - 120

    def test_a_big_dose_in_flight_stays_settled(self):
        """Cooling 2 °C/h: a pulse carries more than 0.5 °C in flight to
        cover the cooling until it lands; the virtual excess is no reason to
        leave the settled state (only the room itself 0.5 above is)."""
        st = self._pulsing(400, pulse_setpoint_c=20.0)
        res, new = _dose(_reg(), st, 20.05, inflight=0.6, slope_c_per_h=-2.0)
        assert res.virtual_error_c == pytest.approx(-0.65, abs=1e-6)
        assert res.settled and new.settled and res.dose_end != "unsettled"

    def test_a_lowered_target_ends_it_before_the_minimum(self):
        """A target cut by hand by 0.1 (short of the stop rule) 1 min into a
        pulse: covered at once, not 25 °C until the minimum."""
        st = self._pulsing(60, pulse_setpoint_c=20.0)
        res, new = _dose(_reg(), st, 19.95, sp=19.9, inflight=0.05)
        assert res.settled and res.mode == R.MODE_STOP and res.dose_end == "covered"
        assert new.pulse_start_ts is None and new.pulse_setpoint_c is None
        # The same target: the minimum holds.
        res, _ = _dose(_reg(), st, 19.95, inflight=0.05)
        assert res.mode == R.MODE_DOSE

    def test_a_pulse_remembers_its_target(self):
        res, st = _dose(_reg(), _settled(), 19.9)               # fires
        assert res.fired_by == "release" and st.pulse_setpoint_c == 20.0
        res, st = _dose(_reg(), st, 19.9, now=NOW + 60)
        assert res.mode == R.MODE_DOSE and st.pulse_setpoint_c == 20.0


class TestCap:
    """A pulse's cap (owner, 31 Oct 2026: the safety net): the dose worked
    out on the room's own heating rate, with no aim bias and on its own
    cooling observer, plus cap_margin_c, so it cannot grow with the model
    it guards."""

    CFG = P.DoseConfig()
    RATE = 2.62 / 60.0          # the living room's tuner rate, per open-minute

    @staticmethod
    def _horizon():
        return THETA + 2.0 * TAU

    def test_on_the_rooms_own_heating_rate(self):
        """0.5 to cover at the living room's 2.62 °C/h: (0.5 + 0.2) / 0.0437
        = 16.0 open-min, a 15.5-min pulse."""
        cap = R.dose_cap_s(0.5, self.RATE, self.CFG)
        open_min = (0.5 + self.CFG.cap_margin_c) / self.RATE
        assert cap == pytest.approx(60.0 * open_min + 60.0 - 90.0)

    def test_a_slow_room_short_a_lot_runs_long_a_fast_room_short_a_little_short(self):
        slow = R.dose_cap_s(0.8, 1.0 / 60.0, self.CFG)        # 1 °C/h: 60 open-min
        fast = R.dose_cap_s(0.0, 6.0 / 60.0, self.CFG)        # 6 °C/h: 2 open-min
        assert slow == pytest.approx(60.0 * 60.0 - 30.0)
        assert R.dose_cap_s(1.5, 1.0 / 60.0, self.CFG) == self.CFG.pulse_ceiling_s
        assert fast == self.CFG.pulse_min_s                    # never under the minimum
        living_01 = R.dose_cap_s(0.1, self.RATE, self.CFG)
        living_08 = R.dose_cap_s(0.8, self.RATE, self.CFG)
        assert 5.5 * 60 < living_01 < 7 * 60 and 22 * 60 < living_08 < 23 * 60

    def test_an_unknown_room_gets_the_fixed_maximum(self):
        for gain in (None, 0.0, -1.0, float("nan")):
            assert R.dose_cap_s(0.5, gain, self.CFG) == self.CFG.pulse_max_s

    def test_its_need_is_the_rooms_not_the_models(self):
        """The cap's need: the measured shortfall less the heat in flight at
        the room's rate, plus the cooling from the cap's own observer over
        the landing horizon (a room rising by itself: less), no bias."""
        h = self._horizon()
        assert R.dose_cap_need_c(0.3, 0.1, -0.6 / H, h, 0.1) == pytest.approx(0.2 + 0.6 * h / H)
        assert R.dose_cap_need_c(0.3, 0.0, 0.6 / H, h, 0.1) == pytest.approx(0.3 - 0.6 * h / H)
        assert R.dose_cap_need_c(0.3, 0.0, 0.05 / H, h, 0.1) == pytest.approx(0.3)   # deadband
        assert R.dose_cap_need_c(0.3, float("nan"), None, h, 0.1) == pytest.approx(0.3)

    def test_set_when_the_pulse_starts_and_kept(self):
        """With the room's heating rate known the cap is worked out when the
        pulse fires (the cooling while it runs counted at the net rise) and
        kept; without the rate, the fixed maximum (the model's own gain is
        no guard against itself); a held-back pulse takes it afresh."""
        res, st = _dose(_reg(), _settled(), 19.6, cap_gain=self.RATE, cap_slope_c_per_h=-0.5)
        assert res.fired_by == "release"
        need = R.dose_cap_need_c(0.4, 0.0, -0.5 / H, self._horizon(), 0.1)
        cap = R.dose_cap_s(need, self.RATE, self.CFG)
        assert st.pulse_cap_s == pytest.approx(cap) and res.pulse_cap_s == pytest.approx(cap, abs=0.1)
        res2, st2 = _dose(_reg(), st, 19.62, now=NOW + 60, cap_gain=0.5, cap_slope_c_per_h=-3.0)
        assert res2.mode == R.MODE_DOSE and st2.pulse_cap_s == st.pulse_cap_s
        res3, st3 = _dose(_reg(), _settled(), 19.6, gain=0.01)
        assert res3.fired_by == "release"
        assert st3.pulse_cap_s == res3.pulse_cap_s == self.CFG.pulse_max_s
        held = _settled(mode=R.MODE_DOSE, pulse_start_ts=None, pulse_cap_s=200.0)
        res4, st4 = _dose(_reg(), held, 19.6, cap_gain=self.RATE)
        assert res4.fired_by == "release"
        assert st4.pulse_cap_s == pytest.approx(R.dose_cap_s(0.4, self.RATE, self.CFG))

    def test_still_to_run_once_open(self):
        """What is left from now with the valve open (a target raised mid-
        pulse): no latency to come, the close delay still adds its heat."""
        assert R.dose_cap_s(0.5, self.RATE, self.CFG, running=True) == pytest.approx(
            R.dose_cap_s(0.5, self.RATE, self.CFG) - 60.0)
        assert R.dose_cap_s(-1.0, self.RATE, self.CFG, running=True) == pytest.approx(
            60.0 * 0.2 / self.RATE - 90.0)

    def test_without_a_rate_it_ends_at_the_fixed_maximum(self):
        reg = _reg()
        res, st = _dose(reg, _settled(), 19.6, gain=0.002)        # a model far too weak
        t = NOW
        while res.dose_end is None and t < NOW + 3600:
            t += 60.0
            res, st = _dose(reg, st, 19.6, gain=0.002, now=t)
        assert res.dose_end == "max" and t - NOW == pytest.approx(self.CFG.pulse_max_s, abs=60)

    def _run_pulse(self, *, model_gain, room_gain, fall_c_per_h=0.0, room0=19.6, sp=20.0,
                   sp_at=None, minutes=90, delay_s=THETA):
        """One pulse on a room cooling ``fall_c_per_h``, its heat showing
        ``delay_s`` after it was sent (a pure delay), the model and the cap
        told their own heat in flight, the room's slope and its cooling.
        Returns (end, minutes run)."""
        reg = _reg()
        res, st = _dose(reg, _settled(), room0, gain=model_gain, cap_gain=room_gain,
                        cap_slope_c_per_h=-fall_c_per_h, loss_slope_c_per_h=-fall_c_per_h,
                        slope_c_per_h=-fall_c_per_h, sp=sp)
        assert res.fired_by == "release"

        def opened(t):
            return max(0.0, (t - NOW - 60.0) / 60.0)

        t = NOW
        while res.dose_end is None and t < NOW + 60 * minutes:
            t += 60.0
            sent, arrived = opened(t), opened(t - delay_s)
            room = room0 - fall_c_per_h * (t - NOW) / H + room_gain * arrived
            slope = -fall_c_per_h + (60.0 * room_gain if arrived > 0 else 0.0)
            target = sp_at[1] if sp_at is not None and t - NOW >= sp_at[0] else sp
            res, st = _dose(reg, st, room, sp=target, gain=model_gain, cap_gain=room_gain,
                            now=t, inflight=model_gain * (sent - arrived),
                            cap_inflight=room_gain * (sent - arrived), slope_c_per_h=slope,
                            loss_slope_c_per_h=-fall_c_per_h, cap_slope_c_per_h=-fall_c_per_h)
        return res.dose_end, (t - NOW) / 60.0

    def test_the_cap_stops_a_model_that_under_counts(self):
        """A model whose gain is half the room's, its heat showing after 12
        min: its own dose would run the valve on; the cap on the room's own
        rate ends it once the heat sent covers what the room needs plus the
        margin (0.4 + 0.2 at 0.044 per open-minute, the close delay's 1.5
        open-minutes included)."""
        end, ran = self._run_pulse(model_gain=0.022, room_gain=0.044, delay_s=720.0)
        assert end == "max"
        assert ran == pytest.approx(1.0 + (0.6 / 0.044 - 1.5), abs=1.01)

    def test_a_correct_model_ends_on_its_own_dose_before_its_cap(self):
        """The model right (the room's own gain), cooling 0.5 °C/h: the pulse
        ends on its dose, not at its cap.  (Cooling much faster, the cap
        without the cooling while the pulse runs cuts it short: deliberate,
        see dose_cap_s.)"""
        reg = _reg()
        res, _ = _dose(reg, _settled(), 19.7, cap_gain=0.044, cap_slope_c_per_h=-0.5,
                       slope_c_per_h=-0.5, loss_slope_c_per_h=-0.5, gain=0.044)
        cap = res.pulse_cap_s / 60.0
        end, ran = self._run_pulse(model_gain=0.044, room_gain=0.044, fall_c_per_h=0.5,
                                   room0=19.7, delay_s=720.0)
        assert end in ("covered", "landing") and ran < cap

    def test_a_target_raised_mid_pulse_widens_it(self):
        """Review 3 (M3): a target raised 0.3 at 4 min into a pulse: the cap
        follows (judged on the error now), the pulse covers the new need."""
        end, ran = self._run_pulse(model_gain=0.044, room_gain=0.044, sp_at=(240.0, 20.3),
                                   delay_s=720.0)
        assert end == "covered"
        end0, ran0 = self._run_pulse(model_gain=0.044, room_gain=0.044, delay_s=720.0)
        assert end0 == "covered" and ran > ran0 + 4.0

    def test_a_heat_up_exit_ends_a_long_pulse_once_its_heat_shows(self):
        """Review 3 (m1): a model too weak keeps a pulse going; once its heat
        shows and the room rises onto the target, the heat-up's own exit
        (error - Td x rise <= 0.1) ends it, as a full push ends."""
        reg = _reg()
        st = _settled(mode=R.MODE_DOSE, pulse_start_ts=NOW - 900, pulse_setpoint_c=20.0,
                      pulse_cap_s=3600.0)
        # The room rises 1.5 °C/h on the pulse's heat; the room as if unheated
        # (the observer) still cools 0.5: 0.25 - 10 min x 1.5 <= 0.1.
        res, _ = _dose(reg, st, 19.75, slope_c_per_h=1.5, loss_slope_c_per_h=-0.5, gain=0.01,
                       cap_gain=0.044, cap_slope_c_per_h=-0.5)
        assert res.dose_end == "landing"
        st_early = dataclasses.replace(st, pulse_start_ts=NOW - 240)    # within its dead time
        res, _ = _dose(reg, st_early, 19.75, slope_c_per_h=1.5, loss_slope_c_per_h=-0.5,
                       gain=0.01, cap_gain=0.044, cap_slope_c_per_h=-0.5)
        assert res.dose_end is None and res.mode == R.MODE_DOSE


class TestReadiness:
    """When the next pulse may fire (Review 3: M3, m4, m7, m9)."""

    def test_a_raised_target_skips_the_refractory(self):
        st = _settled(last_pulse_start_ts=NOW - 300, last_pulse_end_ts=NOW - 60,
                      last_pulse_setpoint_c=20.0)
        res, _ = _dose(_reg(), st, 20.0, sp=20.7)
        assert res.fired_by == "release"
        res, _ = _dose(_reg(), st, 19.3)                 # the same 0.7 short, target unmoved
        assert res.fired_by is None and res.ready_in_s is None

    def test_the_pulse_end_records_its_target(self):
        st = _settled(mode=R.MODE_DOSE, pulse_start_ts=NOW - 600, pulse_setpoint_c=20.0,
                      pulse_cap_s=3600.0)
        res, new = _dose(_reg(), st, 20.0, inflight=0.5)
        assert res.dose_end == "covered" and new.last_pulse_setpoint_c == 20.0

    def test_times_from_the_future_count_as_now(self):
        """The clock went back 46 min: the refractory runs from now, not
        from the saved future times."""
        st = _settled(last_pulse_start_ts=NOW + 2760, last_pulse_end_ts=NOW + 2800)
        res, new = _dose(_reg(), st, 19.8, slope_c_per_h=-0.5)
        assert res.fired_by is None
        assert new.last_pulse_start_ts == NOW and new.last_pulse_end_ts == NOW
        later = NOW + min(HORIZON, P.DoseConfig().refractory_max_s)
        res, _ = _dose(_reg(), new, 19.8, slope_c_per_h=-0.5, now=later)
        assert res.fired_by == "release"

    def test_unbooked_heat_must_show_first(self):
        """A hold's or a heat-up's heat is in no virtual error: the first
        pulse waits theta + tau / 2 after it (the upgrade from 2.15.0 with a
        hold on the valve, the option turned on)."""
        res, _ = _dose(_reg(), _settled(), 19.7, unbooked_until=NOW)
        assert res.fired_by is None
        res, _ = _dose(_reg(), _settled(), 19.7, unbooked_until=NOW - LEAD + 60)
        assert res.fired_by is None
        res, _ = _dose(_reg(), _settled(), 19.7, unbooked_until=NOW - LEAD)
        assert res.fired_by == "release"

    def test_a_negative_bias_does_not_hold_back_the_first_pulse(self):
        """Review 3 (m7): with the aim at -0.25 a flat room was first topped
        up at 0.30 below; the floor guard now leaves a negative bias out."""
        res, _ = _dose(_reg(), _settled(), 19.88, bias=-0.25, cap_gain=2.52 / 60.0)
        assert res.fired_by == "release"


# ---------------------------------------------------------------------------
# The steady state: a room that matches the model
# ---------------------------------------------------------------------------

class _Room:
    """A room cooling at a fixed rate with the dose model's own radiator
    (perfect model): the regulator's predictor stand-in books open time from
    the commands (open 60 s after a pulse starts, until 90 s after its off),
    delays it theta, then spreads it over tau."""

    def __init__(self, fall_c_per_h, *, gain=GAIN, theta=THETA, tau=TAU, error0=0.0):
        self.f = fall_c_per_h / H
        self.g, self.theta, self.tau = gain, theta, tau
        self.error = error0             # setpoint 20 - room
        self.delay: list[tuple[float, float]] = []
        self.emitter = 0.0
        self.open_from: float | None = None
        self.open_until: float | None = None

    def command(self, now, mode, prev_mode):
        if mode == R.MODE_DOSE and prev_mode != R.MODE_DOSE:
            self.open_from, self.open_until = now + 60.0, None
        elif mode != R.MODE_DOSE and prev_mode == R.MODE_DOSE:
            self.open_until = now + 90.0

    def step(self, t0, t1):
        if self.open_from is not None:
            end = t1 if self.open_until is None else min(t1, self.open_until)
            overlap = max(0.0, end - max(t0, self.open_from))
            if overlap > 0:
                self.delay.append((t0, overlap / 60.0))
        keep = []
        for ts, q in self.delay:
            if t1 - ts >= self.theta:
                self.emitter += q
            else:
                keep.append((ts, q))
        self.delay = keep
        arrived = self.emitter * (1 - math.exp(-(t1 - t0) / self.tau))
        self.emitter -= arrived
        self.error += self.f * (t1 - t0) - self.g * arrived

    def inflight(self):
        return self.g * (sum(q for _, q in self.delay) + self.emitter)

    def tail(self, mode):
        return self.g * 90.0 / 60.0 if mode == R.MODE_DOSE else 0.0


def _run(fall_c_per_h, hours=6.0, dt=10.0, room_gain_x=1.0):
    """Returns the pulses (start, length s), the room's error per step, and
    how many cycles were not settled.  ``room_gain_x``: the room's radiator
    against the model's (the regulator is given the model's in flight)."""
    reg = _reg()
    room = _Room(fall_c_per_h, gain=GAIN * room_gain_x)
    st = _settled()
    pulses, errors, t = [], [], NOW
    start, unsettled = None, 0
    for _ in range(int(hours * H / dt)):
        room.step(t, t + dt)
        t += dt
        prev = st.mode
        res, st = _dose(reg, st, 20.0 - room.error, slope_c_per_h=-fall_c_per_h, now=t,
                        inflight=room.inflight() / room_gain_x,
                        tail=room.tail(prev) / room_gain_x, dt=dt)
        room.command(t, res.mode, prev)
        unsettled += not res.settled
        if res.fired_by:
            start = t
        if res.dose_end and start is not None:
            pulses.append((start, t - start))
        errors.append((t, room.error))
    return pulses, errors, unsettled


class TestSteadyState:
    """The design's steady state (design §5, §12): with a model that
    matches the room the trough sits at the band's floor (-0.09) and the
    landing within a few hundredths of the target.  The pulse is re-judged
    each cycle with the horizon from now, so the cooling during the pulse
    is covered too: 5.0 min at 0.5 °C/h in steady state (4.8 for the first
    pulse; the design's first table, 4.4, sized the pulse once at its
    start).  The next pulse waits only for the refractory time: with an
    in-flight gate as well (nothing left in flight before the next) the
    room fell to 0.18 under at 0.8 °C/h and left the settled state at
    1.0."""

    @pytest.mark.parametrize("fall, pulse_min, period_min", [
        (0.2, 3.0, 47.0), (0.3, 3.5, 36.0), (0.5, 5.0, 30.0),
    ])
    def test_pulses_trough_and_landing(self, fall, pulse_min, period_min):
        pulses, errors, unsettled = _run(fall)
        assert unsettled == 0
        later = [p for p in pulses if p[0] - NOW > 1.5 * H]
        assert len(later) >= 3
        lengths = [d / 60 for _, d in later]
        assert all(pulse_min - 0.4 <= d <= pulse_min + 0.4 for d in lengths), lengths
        starts = [s for s, _ in later]
        gaps = [(b - a) / 60 for a, b in zip(starts, starts[1:])]
        assert all(abs(g - period_min) <= 6.0 for g in gaps), gaps
        steady = [e for ts, e in errors if ts - NOW > 1.5 * H]
        assert max(steady) <= 0.1                       # the trough: the band's floor
        assert -0.03 <= min(steady) <= 0.03             # the landing: on the target
        assert all(abs(e) <= 0.1 for e in steady)

    @pytest.mark.parametrize("fall, pulse_min, period_min", [(0.8, 7.7, 27.6), (1.0, 9.9, 28.3)])
    def test_cold_weather_stays_in_the_band(self, fall, pulse_min, period_min):
        """0.8 and 1.0 °C/h: longer pulses, still every ~28 min, the room
        within -0.11..+0.03 of the target and never unsettled."""
        pulses, errors, unsettled = _run(fall)
        assert unsettled == 0
        later = [p for p in pulses if p[0] - NOW > 1.5 * H]
        assert len(later) >= 5
        assert all(abs(d / 60 - pulse_min) <= 0.4 for _, d in later)
        starts = [s for s, _ in later]
        gaps = [(b - a) / 60 for a, b in zip(starts, starts[1:])]
        assert all(abs(g - period_min) <= 3.0 for g in gaps), gaps
        steady = [-e for ts, e in errors if ts - NOW > 1.5 * H]   # room - target
        assert -0.11 <= min(steady) and max(steady) <= 0.03

    @pytest.mark.parametrize("fall", [0.5, 0.8])
    def test_a_weaker_radiator_than_the_model(self, fall):
        """The room's radiator 40 % weaker than the model (before the
        learner has followed): the pulses come more often (every ~21 min)
        and the room runs short, troughs no lower than -0.16 at 0.5 °C/h
        and -0.20 at 0.8, never unsettled and never above the target."""
        pulses, errors, unsettled = _run(fall, room_gain_x=0.6)
        assert unsettled == 0
        steady = [-e for ts, e in errors if ts - NOW > 1.5 * H]
        assert min(steady) >= (-0.16 if fall == 0.5 else -0.20)
        assert max(steady) <= 0.0

    def test_a_stronger_radiator_than_the_model_lands_hot_but_bounded(self):
        """40 % stronger at 0.5 °C/h: landings up to +0.10, troughs -0.08,
        pulses every ~44 min (the learner's step towards smaller doses is the
        fast one)."""
        _, errors, unsettled = _run(0.5, room_gain_x=1.5)
        assert unsettled == 0
        steady = [-e for ts, e in errors if ts - NOW > 1.5 * H]
        assert min(steady) >= -0.09 and max(steady) <= 0.10

    def test_every_pulse_at_least_the_minimum(self):
        pulses, _, _ = _run(0.1, hours=8)
        assert pulses and all(d >= 180.0 for _, d in pulses)
