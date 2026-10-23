"""The room slope survives a restart or reload (2.9.1).

23 Oct 2026, 20:18: Home Assistant restarted in the middle of four heat-ups.
The slope history started empty, so for 8 minutes the heat-up end could not
be predicted: the study and the second bedroom, due to stop at 20:19, ran at
full power until 20:28 (study +0.35 °C).  The samples now ride along in the
restore data and are taken back when they still join up with now.
"""
from __future__ import annotations

import json
import time

import pytest

from tests.ha_harness import (
    _Extra,
    _make_entity,
    _restart_from,
    _run,
    _start,
    e2e,
    reset_timers,
)
from tests.pure_modules import autotune as _at

SlopeEstimator = _at.SlopeEstimator


@pytest.fixture(autouse=True)
def _reset_timers():
    reset_timers()
    yield
    reset_timers()


def _rising(est, start=1000.0, n=16, rate_c_per_h=1.2, first=19.0):
    """One sample a minute, the room rising steadily."""
    for i in range(n):
        est.add(start + 60.0 * i, first + rate_c_per_h * i / 60.0)
    return start + 60.0 * (n - 1)


class TestSlopeEstimatorPersistence:
    def test_round_trip_keeps_the_slope(self):
        old = SlopeEstimator()
        last = _rising(old)
        slope = old.slope_c_per_s()
        assert slope == pytest.approx(1.2 / 3600)
        data = json.loads(json.dumps(old.to_dict()))
        new = SlopeEstimator()
        assert new.restore(data, last + 90.0) is True
        assert new.slope_c_per_s() == pytest.approx(slope, rel=1e-3)
        # It carries on from there.
        new.add(last + 90.0, 19.0 + 1.2 * 16.5 / 60.0)
        assert new.slope_c_per_s() == pytest.approx(1.2 / 3600, rel=0.02)

    def test_a_pause_longer_than_the_gap_rule_starts_empty(self):
        old = SlopeEstimator()
        last = _rising(old)
        new = SlopeEstimator()
        assert new.restore(old.to_dict(), last + old.max_gap_s + 1) is False
        assert new.slope_c_per_s() is None

    def test_samples_older_than_the_window_are_dropped(self):
        old = SlopeEstimator()
        last = _rising(old, n=16)
        new = SlopeEstimator()
        assert new.restore(old.to_dict(), last + 240.0)
        assert all(last + 240.0 - t <= new.window_s for t, _ in new._buf)
        assert len(new._buf) < 16

    @pytest.mark.parametrize(
        "data",
        [
            None,
            {},
            {"samples": []},
            {"samples": "x"},
            {"samples": [[1000.0]]},
            {"samples": [[1000.0, "19.0"]]},
            {"samples": [[1000.0, float("nan")]]},
            {"samples": [[True, 19.0]]},
            {"samples": [[1060.0, 19.0], [1000.0, 19.1]]},   # out of order
        ],
    )
    def test_unusable_data_leaves_it_empty(self, data):
        est = SlopeEstimator()
        assert est.restore(data, 1100.0) is False
        assert len(est._buf) == 0

    def test_samples_from_the_future_are_refused(self):
        """The clock went back: the history cannot be placed."""
        old = SlopeEstimator()
        last = _rising(old)
        new = SlopeEstimator()
        assert new.restore(old.to_dict(), last - 120.0) is False

    def test_a_wild_first_report_after_the_restart_is_still_held_back(self):
        old = SlopeEstimator()
        last = _rising(old)
        new = SlopeEstimator()
        new.restore(old.to_dict(), last + 60.0)
        before = new.slope_c_per_s()
        new.add(last + 60.0, 25.0, value_ts=last + 60.0)   # one wild report
        assert new._buf[-1][1] < 20.0
        assert new.slope_c_per_s() == pytest.approx(before, rel=0.05)


@e2e
class TestEntityRestart:
    def test_the_slope_rides_along_in_the_restore_data(self):
        old = _make_entity(window=False)
        _run(_start(old))
        now = time.time()
        _rising(old._slope, start=now - 15 * 60, n=16)
        slope = old._slope.slope_c_per_s()
        assert slope is not None
        data = old.extra_restore_state_data.as_dict()
        assert len(data["slope"]["samples"]) == 16
        json.dumps(data)
        _run(old.async_will_remove_from_hass())

        new = _make_entity(window=False)
        _restart_from(old, new)
        assert new._slope.slope_c_per_s() == pytest.approx(slope, rel=0.02)

    def test_an_old_snapshot_without_it_starts_empty(self):
        old = _make_entity(window=False)
        _run(_start(old))
        data = old.extra_restore_state_data.as_dict()
        data.pop("slope")
        new = _make_entity(window=False)
        _run(_start(new, extra=_Extra(data)))
        assert new._slope.slope_c_per_s() is None
