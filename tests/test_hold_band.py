"""The hold's band as options per room (2.20.0).

The owner, 3 Nov 2026: "The -0.1 / +0.1 target is currently hard coded. Can
you make this a parameter?"  Near the target the valve is shut once the room
is predicted to be ``stop_over_c`` over the target, and heats again once it
is predicted to be ``release_below_c`` under it.  Both are options now
(``hold_stop_over_c``, ``hold_release_below_c``, 0.1-0.5 in tenths since
2.21.0, out of range keeps the stock 0.1).

The auto-tuner judges the hold's swings: a swing beyond +-0.15 °C around the
mean is an oscillation (Kp or Ki cut) and +-0.3 an emergency (both cut).  The
stock band swings inside that, a wider one by design: so the thresholds
widen with the band, and a lasting offset is measured from the band's
middle.  Simulated (tests/plant_sim.py, generic living room, 8 °C, eight
days): a +-0.2 band with the stock thresholds took Ki 0.0003 -> 0.00005 (a
rollback, then cuts); with them following the band it stayed at 0.00035.
"""
from __future__ import annotations

import pytest

from tests.ha_harness import _make_entity, _run, _start, e2e, reset_timers
from tests.pure_modules import activity as act
from tests.pure_modules import autotune as A
from tests.pure_modules import parameters as P
from tests.pure_modules import regulation as R


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


def _reg(**band) -> R.FeedforwardPiRegulator:
    return R.FeedforwardPiRegulator(P.RegulationConfig(**band))


def _mode(reg, room, *, mode="hold", slope_c_per_h=0.0, sp=20.0):
    return reg.compute(
        setpoint_c=sp, room_temp_c=room, tado_internal_c=room, time_delta_s=60.0,
        state=R.RegulationState(mode=mode), room_slope_c_per_s=slope_c_per_h / 3600.0,
    ).mode


# ---------------------------------------------------------------------------
# The option
# ---------------------------------------------------------------------------

class TestTheOption:
    def test_the_stock_band(self):
        cfg = P.RegulationConfig()
        assert (cfg.stop_over_c, cfg.release_below_c) == (0.1, 0.1)
        at = P.AutotuneConfig()
        assert (at.hold_band_over_c, at.hold_band_under_c) == (0.1, 0.1)

    @pytest.mark.parametrize(("value", "expected"), [
        (0.2, 0.2), ("0.3", 0.3), (0.1, 0.1), (0.5, 0.5),
        (0.05, 0.1), (0.04, 0.1), (0.0, 0.1), (-0.2, 0.1), (0.55, 0.1),
        (None, 0.1), ("x", 0.1), (float("nan"), 0.1), (float("inf"), 0.1),
    ])
    def test_out_of_range_keeps_the_default(self, value, expected):
        assert P.hold_band_option(value, 0.1) == pytest.approx(expected)

    @pytest.mark.parametrize(("value", "expected"), [
        (0.05, 0.1), (0.15, 0.2), (0.25, 0.3), (0.35, 0.4), (0.45, 0.5), (0.12, 0.1),
        (0.30000000000000004, 0.3), (0.19999999999999998, 0.2), ("0.25", 0.3),
    ])
    def test_in_tenths(self, value, expected):
        """The owner, 3 Nov 2026: "these numbers must be at 0.1C
        granularity".  The form steps by 0.1; a value saved by 2.20.0 in
        0.05 steps is read to the nearest tenth, halves up."""
        assert P.hold_band_option(value, 0.1) == expected

    def test_the_form_steps_in_tenths(self):
        assert (P.HOLD_BAND_MIN_C, P.HOLD_BAND_MAX_C, P.HOLD_BAND_STEP_C) == (0.1, 0.5, 0.1)

    def test_the_release_look_ahead_is_10_min(self):
        assert P.RegulationConfig().release_lead_s == 600.0

    @pytest.mark.parametrize(("value", "seconds"), [
        (10, 600.0), ("20", 1200.0), (0, 0.0), (30, 1800.0), (7.5, 450.0),
        (31, 600.0), (-1, 600.0), (None, 600.0), ("x", 600.0), (float("nan"), 600.0),
    ])
    def test_the_release_look_ahead_option(self, value, seconds):
        assert P.release_lead_option(value, 600.0) == pytest.approx(seconds)


# ---------------------------------------------------------------------------
# The regulator
# ---------------------------------------------------------------------------

class TestTheRegulator:
    def test_a_higher_stop_line_holds_on_longer(self):
        stock, wide = _reg(), _reg(stop_over_c=0.3)
        assert _mode(stock, 20.10) == "stop"
        assert _mode(wide, 20.10) == "hold"
        assert _mode(wide, 20.29) == "hold"
        assert _mode(wide, 20.30) == "stop"

    def test_the_stop_still_looks_ahead(self):
        """0.05 under and rising 1.2 °C/h: 10 min ahead it is 0.13 over.
        The stock line (0.1) stops it now, a 0.3 line does not."""
        assert _mode(_reg(), 19.95, slope_c_per_h=1.2) == "stop"
        assert _mode(_reg(stop_over_c=0.3), 19.95, slope_c_per_h=1.2) == "hold"
        assert _mode(_reg(stop_over_c=0.3), 19.95, slope_c_per_h=3.0) == "stop"

    def test_a_lower_release_line_keeps_the_valve_off_longer(self):
        stock, wide = _reg(), _reg(release_below_c=0.25)
        assert _mode(stock, 19.90, mode="stop") == "hold"
        assert _mode(wide, 19.90, mode="stop") == "stop"
        assert _mode(wide, 19.76, mode="stop") == "stop"
        assert _mode(wide, 19.75, mode="stop") == "hold"

    def test_a_falling_room_is_still_released_by_its_look_ahead(self):
        """19.90, falling: released once 10 min ahead (the release
        look-ahead) reaches 0.25 under, 0.9 °C/h beyond the slope deadband."""
        wide = _reg(release_below_c=0.25)
        assert _mode(wide, 19.90, mode="stop", slope_c_per_h=-0.9) == "stop"    # 0.23
        assert _mode(wide, 19.90, mode="stop", slope_c_per_h=-1.1) == "hold"    # 0.27

    def test_the_look_ahead_sets_how_early_a_falling_room_is_released(self):
        """19.95 (0.05 under), falling 0.8 °C/h (0.7 beyond the deadband):
        20 min ahead 0.28 under, 10 min 0.17, 5 min 0.11, none 0.05."""
        for lead_s, mode in ((1200.0, "hold"), (600.0, "hold"), (300.0, "hold"), (0.0, "stop")):
            reg = _reg(release_lead_s=lead_s)
            assert _mode(reg, 19.95, mode="stop", slope_c_per_h=-0.8) == mode, lead_s
        # Falling 0.35 °C/h (0.25 beyond the deadband): 20 min ahead 0.13
        # under (released), 10 min ahead 0.09 (not yet).
        assert _mode(_reg(release_lead_s=1200.0), 19.95, mode="stop", slope_c_per_h=-0.35) == "hold"
        assert _mode(_reg(release_lead_s=600.0), 19.95, mode="stop", slope_c_per_h=-0.35) == "stop"
        # With no look-ahead only the line releases.
        assert _mode(_reg(release_lead_s=0.0), 19.90, mode="stop", slope_c_per_h=-3.0) == "hold"
        assert _mode(_reg(release_lead_s=0.0), 19.91, mode="stop", slope_c_per_h=-3.0) == "stop"

    def test_a_heat_up_is_unchanged(self):
        """The band is the hold's: 0.8 under still heats at full power, and
        the heat-up still ends on its own exit (0.1)."""
        for band in ({}, {"stop_over_c": 0.4, "release_below_c": 0.4}):
            assert _mode(_reg(**band), 19.2) == "heat"
            assert _mode(_reg(**band), 19.75, mode="heat", slope_c_per_h=3.0) == "coast"


# ---------------------------------------------------------------------------
# The activity log
# ---------------------------------------------------------------------------

def _line(cfg, **kw):
    base = dict(path=act.PATH_REGULATED, setpoint_c=20.0, target_label="Comfort",
                room_c=19.5, trv_c=19.2, trv_heating=True, tado="21.1", tado_c=21.1,
                mode="hold", prev_mode="hold", error_c=0.5, slope_c_per_h=0.0,
                command_c=21.1, p_c=2.0, i_c=0.0, send="already_at_target")
    base.update(kw)
    return act.describe_cycle(act.CycleFacts(**base), cfg)


class TestTheActivityLog:
    OFF = dict(mode="stop", prev_mode="stop", command_c=5.0, command_off=True,
               tado="off", tado_c=None)

    def test_a_stop_names_the_configured_release(self):
        """Two decimals when the line falls between tenths (0.05 steps):
        19.75 must not read as 19.8."""
        cases = [(0.25, "19.75"), (0.2, "19.8"), (0.1, "19.9")]
        for release, at in cases:
            cfg = P.RegulationConfig(release_below_c=release)
            line = _line(cfg, error_c=-0.36, slope_c_per_h=0.8, **self.OFF)
            assert f"heats again at {at}, or 10 min ahead of it once falling |" in line
            falling = _line(cfg, error_c=0.02, slope_c_per_h=-0.2, **self.OFF)
            assert f"heats again 10 min before it reaches {at} (" in falling

    @pytest.mark.parametrize(("over", "line_c"), [(0.3, "20.3"), (0.15, "20.15")])
    def test_the_overshoot_forecast_names_the_configured_stop(self, over, line_c):
        cfg = P.RegulationConfig(stop_over_c=over)
        line = _line(cfg, error_c=0.11, slope_c_per_h=3.0, td_s=1500.0, **self.OFF)
        assert f"would pass {line_c} within 25 min, so it stays off" in line


# ---------------------------------------------------------------------------
# The auto-tuner follows the band
# ---------------------------------------------------------------------------

def _square(half_c, *, hours=24, period_s=5400.0, middle_c=0.0):
    """The stop-to-release cycle of a band: the error at one edge, then the
    other, the valve heating half of the time."""
    out = []
    for k in range(int(hours * 60)):
        t = k * 60.0
        up = (t % period_s) < period_s / 2
        out.append((t, middle_c + (half_c if up else -half_c), 0.5 if up else -0.5))
    return out


class TestTheTuner:
    def test_the_stock_band_is_judged_as_before(self):
        cfg = P.AutotuneConfig()
        assert cfg.band_widening_c() == 0.0 and cfg.band_middle_c() == 0.0

    def test_a_narrower_band_does_not_tighten_the_thresholds(self):
        cfg = P.AutotuneConfig(hold_band_over_c=0.05, hold_band_under_c=0.05)
        assert cfg.band_widening_c() == 0.0

    def test_a_wider_band_s_own_swing_is_no_oscillation(self):
        swing = _square(0.2)
        stock = A.analyse_hold(swing, cfg=P.AutotuneConfig(), tuning=A.Tuning(2.5, 5e-4))
        wide = A.analyse_hold(
            swing, cfg=P.AutotuneConfig(hold_band_over_c=0.2, hold_band_under_c=0.2),
            tuning=A.Tuning(2.5, 5e-4),
        )
        assert stock.oscillating          # the stock thresholds blame the gains
        assert not wide.oscillating

    def test_a_swing_beyond_the_band_still_is(self):
        swing = _square(0.32)
        cfg = P.AutotuneConfig(hold_band_over_c=0.2, hold_band_under_c=0.2)
        assert A.analyse_hold(swing, cfg=cfg, tuning=A.Tuning(2.5, 5e-4)).oscillating

    @pytest.mark.parametrize(("band", "amp", "emergency"), [
        ((0.1, 0.1), 0.30, True),
        ((0.2, 0.2), 0.30, False),
        ((0.2, 0.2), 0.40, True),
    ])
    def test_the_emergency_line_widens_with_the_band(self, band, amp, emergency):
        cfg = P.AutotuneConfig(hold_band_over_c=band[0], hold_band_under_c=band[1])
        t = A.Autotuner(cfg, A.Tuning(2.5, 5e-4, 600.0), derivative_allowed=True)
        m = A.HoldMetrics(ts=86400.0, mean_error_c=0.0, sd_c=amp / 2, half_cycles=4,
                          period_s=3600.0, amplitude_c=amp, heating_fraction=0.5,
                          oscillating=True)
        t._on_hold(m, 86400.0)
        assert ("emergency cut" in t.last_verdict) is emergency

    def test_an_offset_is_measured_from_the_band_s_middle(self):
        """A band of 0.1 over and 0.3 under keeps the room 0.1 under on
        average by design: that is no offset."""
        m = A.HoldMetrics(ts=86400.0, mean_error_c=0.2, sd_c=0.02, half_cycles=0,
                          period_s=None, amplitude_c=0.05, heating_fraction=0.5,
                          oscillating=False)
        stock = A.Autotuner(P.AutotuneConfig(), A.Tuning(2.5, 5e-4, 600.0),
                            derivative_allowed=True)
        stock._on_hold(m, 86400.0)
        assert stock._votes["ki_up"] == 1
        low = A.Autotuner(
            P.AutotuneConfig(hold_band_over_c=0.1, hold_band_under_c=0.3),
            A.Tuning(2.5, 5e-4, 600.0), derivative_allowed=True,
        )
        low._on_hold(m, 86400.0)
        assert low._votes["ki_up"] == 0
        assert "needs 0.15 under the band's middle" in low.last_verdict

    def test_set_hold_band_drops_the_window_being_gathered(self):
        t = A.Autotuner(P.AutotuneConfig(), A.Tuning(2.5, 5e-4, 600.0),
                        derivative_allowed=True)
        t._hold = object()                 # a hold window being gathered
        t.set_hold_band(0.1, 0.1)          # unchanged: kept
        assert t._hold is not None
        t.set_hold_band(0.2, 0.25)
        assert t._hold is None
        assert (t.cfg.hold_band_over_c, t.cfg.hold_band_under_c) == (0.2, 0.25)


# ---------------------------------------------------------------------------
# Through the entity
# ---------------------------------------------------------------------------

@e2e
class TestTheEntity:
    def test_the_options_reach_the_regulator_and_the_tuner(self):
        ent = _make_entity({"hold_stop_over_c": 0.3, "hold_release_below_c": 0.2})
        _run(_start(ent))
        assert (ent._config.stop_over_c, ent._config.release_below_c) == (0.3, 0.2)
        assert ent._regulator.cfg is ent._config
        at = ent._autotuner.cfg
        assert (at.hold_band_over_c, at.hold_band_under_c) == (0.3, 0.2)

    def test_a_value_saved_in_hundredths_is_read_in_tenths(self):
        ent = _make_entity({"hold_stop_over_c": 0.25, "hold_release_below_c": 0.15})
        _run(_start(ent))
        assert (ent._config.stop_over_c, ent._config.release_below_c) == (0.3, 0.2)

    def test_none_set_is_the_stock_band(self):
        ent = _make_entity()
        _run(_start(ent))
        assert (ent._config.stop_over_c, ent._config.release_below_c) == (0.1, 0.1)
        at = ent._autotuner.cfg
        assert (at.hold_band_over_c, at.hold_band_under_c) == (0.1, 0.1)

    def test_out_of_range_is_the_stock_band(self):
        ent = _make_entity({"hold_stop_over_c": 0.9, "hold_release_below_c": "x",
                            "hold_release_lead_min": 45})
        _run(_start(ent))
        assert (ent._config.stop_over_c, ent._config.release_below_c) == (0.1, 0.1)
        assert ent._config.release_lead_s == 600.0

    @pytest.mark.parametrize(("options", "seconds"), [
        ({}, 600.0), ({"hold_release_lead_min": 20}, 1200.0), ({"hold_release_lead_min": 0}, 0.0),
    ])
    def test_the_release_look_ahead_reaches_the_regulator(self, options, seconds):
        ent = _make_entity(options)
        _run(_start(ent))
        assert ent._config.release_lead_s == seconds
        assert ent._regulator.cfg.release_lead_s == seconds

    def test_changed_without_a_reload(self):
        ent = _make_entity()
        _run(_start(ent))
        entry = ent._config_entry
        entry.options = {**entry.options, "hold_stop_over_c": 0.2, "hold_release_below_c": 0.3}
        _run(ent._async_config_entry_updated(ent.hass, entry))
        assert ent._regulator.cfg.stop_over_c == 0.2
        assert ent._regulator.cfg.release_below_c == 0.3
        at = ent._autotuner.cfg
        assert (at.hold_band_over_c, at.hold_band_under_c) == (0.2, 0.3)
