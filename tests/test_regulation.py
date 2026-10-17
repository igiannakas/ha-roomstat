"""Tests for the regulation engine (regulation.py).

These tests validate the pure regulation logic without requiring
Home Assistant to be installed.  We import the modules directly
to avoid triggering the HA-dependent __init__.py.
"""
import importlib
import os
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Import regulation.py and parameters.py WITHOUT triggering __init__.py
# (which depends on homeassistant)
# ---------------------------------------------------------------------------

_COMP_DIR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "roomstat"
)

def _load_module(name: str, path: str) -> types.ModuleType:
    """Load a single Python module by file path."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

# Load parameters first (no HA dependency), then regulation (depends on parameters)
_params = _load_module(
    "roomstat.parameters",
    os.path.join(_COMP_DIR, "parameters.py"),
)
_reg = _load_module(
    "roomstat.regulation",
    os.path.join(_COMP_DIR, "regulation.py"),
)

RegulationConfig = _params.RegulationConfig
CorrectionTuning = _params.CorrectionTuning
PresetConfig = _params.PresetConfig
FeedforwardPiRegulator = _reg.FeedforwardPiRegulator
RegulationState = _reg.RegulationState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_regulator(**overrides) -> FeedforwardPiRegulator:
    """Create a regulator with default config, optionally overridden."""
    return FeedforwardPiRegulator(RegulationConfig(**overrides))


def step(reg, state, room, *, sp=20.0, trv=None, slope_c_per_h=0.0, dt=60.0):
    res = reg.compute(
        setpoint_c=sp, room_temp_c=room,
        tado_internal_c=room if trv is None else trv,
        time_delta_s=dt, state=state,
        room_slope_c_per_s=slope_c_per_h / 3600.0,
    )
    return res, res.new_state


class TestDefaults:
    def test_stock_tuning(self):
        """Kp is the gain near the target; the braking time looks 10 min
        ahead (parameters.py)."""
        t = CorrectionTuning()
        assert (t.kp, t.ki, t.td_s) == (4.0, 0.003, 600.0)

    def test_command_range(self):
        cfg = RegulationConfig()
        assert (cfg.min_target_c, cfg.max_target_c) == (5.0, 25.0)


# -----------------------------------------------------------------------
# Hold: feedforward + PI near the target
# -----------------------------------------------------------------------

class TestHold:
    """Near the target the command is setpoint + (TRV - room) + Kp*e + I."""

    def test_offset_compensation_at_target(self):
        """At target the command equals the TRV's own reading."""
        res, _ = step(make_regulator(), RegulationState(), 21.0, sp=21.0, trv=23.0, dt=0.0)
        assert res.mode == "hold"
        assert res.feedforward_offset_c == 2.0
        assert res.p_correction_c == 0.0
        assert res.target_for_tado_c == 23.0

    def test_push_on_the_valve_is_one_plus_kp_times_the_error(self):
        res, _ = step(make_regulator(), RegulationState(), 20.8, sp=21.0, trv=22.0, dt=0.0)
        assert res.target_for_tado_c == pytest.approx(21.0 + 1.2 + 4 * 0.2)
        assert res.target_for_tado_c - 22.0 == pytest.approx((1 + 4) * 0.2)

    def test_configured_kp_is_the_gain(self):
        reg = make_regulator(tuning=CorrectionTuning(kp=2.0))
        res, _ = step(reg, RegulationState(), 19.6, trv=18.0, dt=0.0)
        assert res.p_correction_c == pytest.approx(0.8)
        assert res.target_for_tado_c == pytest.approx(20.0 - 1.6 + 0.8)

    def test_slightly_over_lowers_the_command(self):
        res, _ = step(make_regulator(), RegulationState(), 21.05, sp=21.0, trv=22.0, dt=0.0)
        assert res.mode == "hold"
        assert res.target_for_tado_c == pytest.approx(21.0 + 0.95 - 0.2, abs=0.06)
        assert res.target_for_tado_c < 22.0

    def test_negative_offset_lowers_the_command(self):
        """TRV reading below the room (cold draught at the valve)."""
        res, _ = step(make_regulator(), RegulationState(), 20.0, trv=17.0, dt=0.0)
        assert res.feedforward_offset_c == -3.0
        assert res.target_for_tado_c == 17.0

    def test_negative_offset_clamped_at_minimum(self):
        res, _ = step(make_regulator(), RegulationState(), 20.0, trv=0.0, dt=0.0)
        assert res.target_for_tado_c == 5.0
        assert res.is_saturated is True

    def test_room_slope_does_not_change_the_command(self):
        """No derivative term: the slope only drives the predictions."""
        reg = make_regulator()
        a, _ = step(reg, RegulationState(), 19.85, trv=17.0, slope_c_per_h=0.0)
        b, _ = step(reg, RegulationState(), 19.85, trv=17.0, slope_c_per_h=0.4)
        assert a.mode == b.mode == "hold"
        assert a.target_for_tado_c == b.target_for_tado_c


# -----------------------------------------------------------------------
# Integral
# -----------------------------------------------------------------------

class TestIntegral:
    def test_accumulates_near_target(self):
        reg, state = make_regulator(), RegulationState()
        for _ in range(5):
            res, state = step(reg, state, 20.8, sp=21.0, trv=22.0)
        assert res.i_correction_c == pytest.approx(5 * 0.2 * 0.003 * 60, abs=0.01)

    def test_does_not_accumulate_outside_the_deadband(self):
        reg, state = make_regulator(), RegulationState()
        for _ in range(10):
            res, state = step(reg, state, 20.4, sp=21.0, trv=21.0)
        assert res.mode == "hold"
        assert abs(res.i_correction_c) < 0.01

    def test_decays_outside_the_deadband(self):
        reg = make_regulator(integral_decay=0.9)
        res, st = step(reg, RegulationState(integral_c=1.0), 20.4, sp=21.0, trv=21.0)
        assert st.integral_c == pytest.approx(0.9, abs=0.001)

    def test_unwinds_when_slightly_over(self):
        res, st = step(make_regulator(), RegulationState(integral_c=1.0), 21.05,
                       sp=21.0, trv=22.0)
        assert 0.99 < st.integral_c < 1.0

    def test_first_cycle_does_not_integrate(self):
        res, st = step(make_regulator(), RegulationState(integral_c=1.5), 20.8,
                       sp=21.0, trv=22.0, dt=0.0)
        assert st.integral_c == pytest.approx(1.5)

    def test_freezes_while_saturated_high(self):
        reg, state = make_regulator(), RegulationState()
        for _ in range(10):
            res, state = step(reg, state, 20.8, sp=21.0, trv=26.0)
        assert res.is_saturated is True
        assert res.i_correction_c == 0.0

    def test_may_decrease_while_saturated_high(self):
        res, st = step(make_regulator(), RegulationState(integral_c=1.5), 21.05,
                       sp=21.0, trv=27.0)
        assert res.mode == "hold" and res.is_saturated
        assert st.integral_c < 1.5

    def test_clamped_at_its_limits(self):
        reg = make_regulator(integral_max_c=1.0, integral_min_c=-1.0)
        state = RegulationState(integral_c=0.95)
        for _ in range(20):
            res, state = step(reg, state, 20.8, sp=21.0, trv=21.5)
        assert res.i_correction_c <= 1.0

    def test_long_gaps_stay_bounded(self):
        reg, state = make_regulator(), RegulationState()
        for _ in range(100):
            _, state = step(reg, state, 20.8, sp=21.0, trv=22.0, dt=600.0)
        assert -2.0 <= state.integral_c <= 2.0


# -----------------------------------------------------------------------
# Modes: heat-up, coast, hold, firm stop
# -----------------------------------------------------------------------

class TestModes:
    def test_large_deficit_heats_with_the_maximum(self):
        res, st = step(make_regulator(), RegulationState(), 18.5)
        assert res.mode == "heat" and res.target_for_tado_c == 25.0
        assert st.mode == "heat"

    def test_boost_heats_with_the_maximum(self):
        res, _ = step(make_regulator(), RegulationState(), 18.0, sp=25.0, trv=20.0)
        assert res.target_for_tado_c == 25.0 and res.is_saturated

    def test_frost_protection_stops(self):
        res, _ = step(make_regulator(), RegulationState(), 18.0, sp=5.0, trv=20.0)
        assert res.mode == "stop" and res.target_for_tado_c == 5.0
        assert res.error_c == -13.0

    def test_heat_ends_when_the_coast_will_reach_the_target(self):
        """Rising 1.5 °C/h with the default 10 min lead: the room coasts
        another 0.23 °C; heating stops when that lands 0.1 °C short."""
        reg = make_regulator()
        res, _ = step(reg, RegulationState(mode="heat"), 19.6, slope_c_per_h=1.5)
        assert res.mode == "heat"
        res, _ = step(reg, RegulationState(mode="heat"), 19.7, slope_c_per_h=1.5)
        assert res.mode == "coast" and res.target_for_tado_c == 5.0

    def test_braking_time_sets_the_lead(self):
        st = RegulationState(mode="heat")
        res, _ = step(make_regulator(), st, 19.25, slope_c_per_h=1.5)
        assert res.mode == "heat"             # 10 min lead: 0.23 °C
        reg = make_regulator(tuning=CorrectionTuning(td_s=1800.0))
        res, _ = step(reg, st, 19.25, slope_c_per_h=1.5)
        assert res.mode == "coast"            # 30 min lead: 0.7 °C of coast

    def test_braking_time_zero_stops_at_the_target(self):
        reg = make_regulator(tuning=CorrectionTuning(td_s=0.0))
        res, _ = step(reg, RegulationState(mode="heat"), 19.85, slope_c_per_h=1.5)
        assert res.mode == "heat"
        res, _ = step(reg, RegulationState(mode="heat"), 19.95, slope_c_per_h=1.5)
        assert res.mode == "coast"

    def test_coast_lasts_while_the_room_still_rises_below_target(self):
        reg = make_regulator()
        res, _ = step(reg, RegulationState(mode="coast"), 19.8, slope_c_per_h=1.0)
        assert res.mode == "coast" and res.target_for_tado_c == 5.0
        res, _ = step(reg, RegulationState(mode="coast"), 19.8, trv=17.0)
        assert res.mode == "hold"             # risen as far as it will
        assert res.target_for_tado_c == pytest.approx(20.0 + (17.0 - 19.8) + 4 * 0.2)

    def test_coast_into_the_target_becomes_a_firm_stop(self):
        """Whatever the slope: the hold would park the command at the
        valve's reading, where a Tado trickles (17 Oct 2026)."""
        reg = make_regulator()
        res, _ = step(reg, RegulationState(mode="coast"), 20.0, slope_c_per_h=1.0)
        assert res.mode == "stop"             # 0.15 °C over within 10 min
        res, _ = step(reg, RegulationState(mode="coast"), 20.0, slope_c_per_h=0.3)
        assert res.mode == "stop"             # at the target: shut, not hold
        res, _ = step(reg, RegulationState(mode="coast"), 20.05)
        assert res.mode == "stop"             # flat, 0.05 over
        res, _ = step(reg, RegulationState(mode="coast"), 19.95)
        assert res.mode == "hold"             # stalled 0.05 short: push

    def test_shut_valve_resumed_above_the_target_stays_shut(self):
        """After a restart the TRV holds the minimum, so the room resumes
        in coast; at 0.06 over with no slope yet that must not fall through
        to hold (every restart on 17 Oct 2026 did, into the trickle)."""
        reg = make_regulator()
        res = reg.compute(setpoint_c=20.0, room_temp_c=20.06, tado_internal_c=16.0,
                          time_delta_s=60.0, state=RegulationState(mode="coast"),
                          room_slope_c_per_s=None)
        assert res.mode == "stop" and res.target_for_tado_c == 5.0
        res, _ = step(reg, res.new_state, 20.06)
        assert res.mode == "stop"             # and the stop's own rule keeps it

    def test_firm_stop_ahead_of_an_overshoot(self):
        """Stopped while still 0.05 °C below the target, because at 1.2 °C/h
        it would be 0.13 °C over within 10 min."""
        reg = make_regulator()
        res, _ = step(reg, RegulationState(), 19.95, slope_c_per_h=1.2)
        assert res.mode == "stop" and res.target_for_tado_c == 5.0
        res, _ = step(reg, RegulationState(), 19.95, slope_c_per_h=0.0)
        assert res.mode == "hold"

    def test_firm_stop_looks_as_far_ahead_as_the_braking_time(self):
        """11 Oct, living room: a 0.6 °C step went through hold, and the
        radiator coasted 19 min.  0.2 °C short and rising 1.0 °C/h: 10 min
        ahead is 0.05 short (hold); 30 min ahead is 0.25 over (stop)."""
        res, _ = step(make_regulator(), RegulationState(), 19.8, slope_c_per_h=1.0)
        assert res.mode == "hold"
        reg = make_regulator(tuning=CorrectionTuning(td_s=1800.0))
        res, _ = step(reg, RegulationState(), 19.8, slope_c_per_h=1.0)
        assert res.mode == "stop"

    def test_firm_stop_looks_at_least_10_min_ahead(self):
        """A short (or no) braking time does not shorten the stop's view."""
        for td in (0.0, 120.0):
            reg = make_regulator(tuning=CorrectionTuning(td_s=td))
            res, _ = step(reg, RegulationState(), 19.95, slope_c_per_h=1.2)
            assert res.mode == "stop"

    def test_firm_stop_lasts_until_the_room_needs_heat(self):
        """Released 0.05 °C below the target, or when a falling room will
        be there within 20 min; never while it is still above."""
        reg = make_regulator()
        stop = RegulationState(mode="stop")
        res, _ = step(reg, stop, 20.08)
        assert res.mode == "stop"             # flat, 0.08 °C over
        res, _ = step(reg, stop, 20.08, slope_c_per_h=-0.4)
        assert res.mode == "stop"             # 20 min ahead: 0.02 over, still shut
        res, _ = step(reg, stop, 20.05, slope_c_per_h=-0.6)
        assert res.mode == "hold"             # 20 min ahead: 0.12 under: warm up
        res, _ = step(reg, stop, 20.01)
        assert res.mode == "stop"             # at the target, flat: shut
        res, _ = step(reg, stop, 19.96)
        assert res.mode == "stop"             # 0.04 under, flat: not yet
        res, _ = step(reg, stop, 19.95)
        assert res.mode == "hold"             # 0.05 under: release

    def test_release_is_hysteresis_not_a_band(self):
        """A room that drifts between 0.05 under and 0.1 over does not
        toggle: the stop needs a predicted overshoot, the release a need."""
        reg = make_regulator()
        res, st = step(reg, RegulationState(mode="stop"), 19.95)
        assert res.mode == "hold"
        res, st = step(reg, st, 19.98)
        assert res.mode == "hold"             # not stopped at the target
        res, st = step(reg, st, 20.02)
        assert res.mode == "hold"             # 0.02 over, flat: the hold holds
        res, st = step(reg, st, 20.10)
        assert res.mode == "stop"             # 0.1 over: the firm stop
        res, st = step(reg, st, 20.02)
        assert res.mode == "stop"             # back to 0.02 over: still shut

    def test_rising_room_is_not_heated(self):
        """Rising fast enough to get there anyway (e.g. sun): no heat-up."""
        res, _ = step(make_regulator(), RegulationState(), 18.9, slope_c_per_h=9.0)
        assert res.mode != "heat"

    @pytest.mark.parametrize("mode, room, trv", [
        ("heat", 18.0, 18.0), ("coast", 19.9, 22.0), ("stop", 20.2, 22.0),
    ])
    def test_integral_only_moves_in_hold(self, mode, room, trv):
        reg = make_regulator(integral_deadband_c=5.0)
        res, st = step(reg, RegulationState(integral_c=0.4, mode=mode), room, trv=trv,
                       dt=600.0, slope_c_per_h=1.0 if mode == "coast" else 0.0)
        assert res.mode == mode
        assert st.integral_c == pytest.approx(0.4)
        res, st = step(reg, RegulationState(integral_c=0.4), 19.9, dt=600.0)
        assert res.mode == "hold" and st.integral_c > 0.4

    def test_unknown_stored_mode_starts_in_hold(self):
        res, _ = step(make_regulator(), RegulationState(mode="bogus"), 18.5)
        assert res.mode == "heat"
        res, _ = step(make_regulator(), RegulationState(mode="bogus"), 19.9)
        assert res.mode == "hold"

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_bad_slope_is_ignored(self, bad):
        res = make_regulator().compute(
            setpoint_c=20.0, room_temp_c=19.0, tado_internal_c=19.0,
            time_delta_s=60.0, state=RegulationState(mode="heat"), room_slope_c_per_s=bad)
        assert res.mode == "heat"

    def test_no_slope_means_no_prediction(self):
        reg = make_regulator()
        res = reg.compute(setpoint_c=20.0, room_temp_c=19.85, tado_internal_c=19.0,
                          time_delta_s=60.0, state=RegulationState(mode="heat"),
                          room_slope_c_per_s=None)
        assert res.mode == "heat"


class TestResumeMode:
    @pytest.mark.parametrize("held, mode", [
        (25.0, "heat"), (24.96, "heat"), (5.0, "coast"), (18.5, "hold"),
        (None, "hold"), (float("nan"), "hold"),
    ])
    def test_from_what_the_trv_holds(self, held, mode):
        assert _reg.resume_mode(held, RegulationConfig()) == mode


# -----------------------------------------------------------------------
# Safety
# -----------------------------------------------------------------------

class TestSafetyClamping:
    def test_never_above_the_maximum(self):
        res, _ = step(make_regulator(), RegulationState(), 15.0, sp=30.0, trv=25.0)
        assert res.target_for_tado_c <= 25.0

    def test_never_below_the_minimum(self):
        res, _ = step(make_regulator(), RegulationState(), 25.0, sp=5.0, trv=5.0)
        assert res.target_for_tado_c >= 5.0


class TestNanInfGuard:
    """Regulation must reject NaN/Inf inputs without corrupting state."""

    def test_nan_setpoint_returns_safe_fallback(self):
        state = RegulationState(integral_c=0.5, mode="heat")
        res = make_regulator().compute(setpoint_c=float("nan"), room_temp_c=20.0,
                                       tado_internal_c=22.0, time_delta_s=60.0, state=state)
        assert res.target_for_tado_c == 5.0
        assert res.new_state.integral_c == 0.5
        assert res.new_state.mode == "heat"

    @pytest.mark.parametrize("room, trv", [
        (float("nan"), 22.0), (20.0, float("inf")), (float("-inf"), 22.0),
    ])
    def test_bad_reading_falls_back_to_the_setpoint(self, room, trv):
        res = make_regulator().compute(setpoint_c=21.0, room_temp_c=room,
                                       tado_internal_c=trv, time_delta_s=60.0,
                                       state=RegulationState())
        assert res.target_for_tado_c == 21.0
        assert res.error_c == 0.0

    def test_fallback_clamped_to_bounds(self):
        res = make_regulator().compute(setpoint_c=32.0, room_temp_c=float("nan"),
                                       tado_internal_c=22.0, time_delta_s=60.0,
                                       state=RegulationState())
        assert res.target_for_tado_c == 25.0


# -----------------------------------------------------------------------
# Presets
# -----------------------------------------------------------------------

class TestPresetConfig:
    def test_default_preset_values(self):
        presets = PresetConfig()
        assert presets.eco_target_c == 17.0
        assert presets.boost_target_c == 25.0
        assert presets.boost_duration_min == 30
        assert presets.away_target_c == 17.0
        assert presets.frost_protection_target_c == 7.0

    def test_custom_preset_values(self):
        config = RegulationConfig(presets=PresetConfig(eco_target_c=17.0, away_target_c=14.0))
        assert config.presets.away_target_c == 14.0
        assert config.presets.boost_target_c == 25.0

    def test_lower_setpoint_lowers_the_command(self):
        reg = make_regulator()
        comfort, _ = step(reg, RegulationState(), 20.95, sp=21.0, trv=22.0, dt=0.0)
        night, _ = step(reg, RegulationState(), 20.95, sp=19.0, trv=22.0, dt=0.0)
        assert night.target_for_tado_c < comfort.target_for_tado_c
        assert night.error_c < 0
