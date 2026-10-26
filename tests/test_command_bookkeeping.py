"""The last command, through the real climate entity (tests/ha_harness.py).

2.8.6, from a review of the 2.8.4 / 2.8.5 off path (19 Oct 2026):

* A command still on its way into the TRV's state is judged on the last
  command *tried*: a heat command that timed out but landed, or that went to
  the other entity before a fallback switch, is not followed by a stale
  "off" taken as done (each of these left a zone at 25 °C for minutes).
* The last command survives an entry reload (every preset temperature change
  reloads the room), so a reload seconds after a heat command does not
  forget it either.
* A TRV that keeps not showing a command it took is asked again less and
  less often (2.8.5: every 6 min in the regulation, every 3 min in summer).
* Roomstat's own OFF goes through the regulation cycle: at once, not to a
  valve that is off already, and a heat-up right after it is not held back.
"""
from __future__ import annotations

import pytest

from tests.ha_harness import (
    SUMMER,
    TRV,
    _climate,
    _make_entity,
    _restart_from,
    _run,
    _settle,
    _start,
    e2e,
    reset_timers,
)
from tests.test_fail_cold_entity import _Clock, _cycle, _feed, _temps

pytestmark = e2e

HVACMode = _climate.HVACMode if _climate is not None else None
CLOUD = "climate.cloud"


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


def _owner(options=None):
    """The owner's rooms: cloud TRVs, 30-min timers for the maximum."""
    ent = _make_entity({"heat_timer_s": 1800, **(options or {})})
    _run(_start(ent))
    return ent


def _commands(ent):
    """Every command to the TRV in order: a setpoint, "timer<setpoint>" for a
    timer overlay, "off" for a switch-off."""
    out = []
    for dom, svc, d in ent.hass.service_calls:
        if dom == "tado" and svc == "set_climate_timer":
            out.append(f"timer{d['temperature']:g}")
        elif dom == "climate" and svc == "set_temperature":
            out.append(d["temperature"])
        elif dom == "climate" and svc == "set_hvac_mode":
            out.append(str(getattr(d["hvac_mode"], "value", d["hvac_mode"])))
    return out


def _times_out_but_lands(ent):
    """Every call reaches the TRV, then reports a timeout (no answer within
    10 s)."""
    real = ent.hass.services.async_call

    async def call(domain, service, service_data=None, blocking=False, **kwargs):
        await real(domain, service, service_data, blocking)
        raise TimeoutError

    ent.hass.services.async_call = call
    return real


# ---------------------------------------------------------------------------
# A command on its way into the TRV's state
# ---------------------------------------------------------------------------

class TestPending:

    def test_heat_that_timed_out_but_landed_is_not_forgotten(self, monkeypatch):
        """A heat command whose call timed out but landed, then the target
        drops 10 s later while the cloud state still shows off.  2.8.5 took
        the "off" as done and waited 5 min at 25 °C for an off it had never
        sent."""
        clock = _Clock(monkeypatch)
        ent = _owner()
        _feed(ent, 20.6, 14.7, 5.0, off=True)
        _cycle(ent)
        assert ent._last_reason == "already_at_target"
        clock.tick(60)
        ent._target_temp = 22.5
        real = _times_out_but_lands(ent)
        _cycle(ent)
        ent.hass.services.async_call = real
        assert _commands(ent) == ["timer25"]
        assert ent._last_reason == "send_failed(normal_update)"
        clock.tick(10)
        ent._target_temp = 20.5
        _feed(ent, 20.6, 14.7, 5.0, off=True)          # stale
        _cycle(ent)
        assert _commands(ent) == ["timer25", "off"]
        assert ent._last_reason == "sent(off_urgent_decrease)"
        clock.tick(60)
        _feed(ent, 20.6, 14.7, 25.0, heating=True)     # the 25 shows, then...
        _cycle(ent)
        # ...the off is on its way: only the one confirmation an off 10 s
        # after a heat command gets a minute later (OFF_CONFIRM_S).
        assert _commands(ent) == ["timer25", "off", "off"]
        assert ent._last_reason == "sent(off_confirm)"
        clock.tick(60)
        _cycle(ent)
        assert _commands(ent) == ["timer25", "off", "off"]

    def test_a_failed_off_does_not_end_the_heat_before_it(self, monkeypatch):
        """2.8.6 review: 25 °C sent, the off 10 s later fails (not landed),
        the state still shows the old off.  The off must not be taken as
        done: that cleared the maximum, and a coordinator outage then left
        the 25 °C on (the watchdog saw no maximum to shut)."""
        clock = _Clock(monkeypatch)
        ent = _owner({"heat_timer_s": 0})
        _feed(ent, 20.6, 14.7, 5.0, off=True)
        _cycle(ent)
        clock.tick(60)
        ent._target_temp = 22.5
        _cycle(ent)
        assert _commands(ent) == [25.0] and ent._last_sent_was_max
        clock.tick(10)
        ent._target_temp = 20.5

        async def fail(*_a, **_k):
            raise TimeoutError

        real = ent.hass.services.async_call
        ent.hass.services.async_call = fail
        _cycle(ent)
        ent.hass.services.async_call = real
        assert ent._last_reason == "send_failed(off_urgent_decrease)"
        clock.tick(50)
        _feed(ent, 20.6, 14.7, 5.0, off=True)        # still the old state
        _cycle(ent)
        assert ent._last_reason.startswith("rate_limited(")
        assert ent._last_sent_was_max                 # the watchdog stays armed
        clock.tick(130)                               # the failed off's interval
        _feed(ent, 20.6, 14.7, 5.0, off=True)
        _cycle(ent)
        assert _commands(ent) == [25.0, "off"]

    def test_a_stale_state_that_looks_like_the_command_proves_nothing(self, monkeypatch):
        """Heat, then an off 10 s later, the state still showing the off
        from before both: that "off" does not count as our off arriving;
        the 25 °C showing up next, then off, does."""
        clock = _Clock(monkeypatch)
        ent = _owner({"heat_timer_s": 0})
        _feed(ent, 20.6, 14.7, 5.0, off=True)
        _cycle(ent)
        clock.tick(60)
        ent._target_temp = 22.5
        _cycle(ent)
        clock.tick(10)
        ent._target_temp = 20.5
        _cycle(ent)                                    # state: the old off
        assert _commands(ent) == [25.0, "off"]
        clock.tick(30)
        _feed(ent, 20.6, 14.7, 5.0, off=True)
        _cycle(ent)
        assert not ent._last_attempt_shown
        clock.tick(30)
        _feed(ent, 20.6, 14.7, 25.0, heating=True)    # the heat catches up
        _cycle(ent)                                    # (the off's confirmation)
        clock.tick(30)
        _feed(ent, 20.6, 14.7, 5.0, off=True)         # then our off
        _cycle(ent)
        assert ent._last_attempt_shown
        assert _commands(ent) == [25.0, "off", "off"]

    def test_a_failed_heat_command_does_not_hide_a_real_off_for_long(self, monkeypatch):
        """A heat command that really failed: the valve's "off" is believed
        again after the drift grace, and the command is retried per
        rate-limit interval meanwhile."""
        clock = _Clock(monkeypatch)
        ent = _owner({"heat_timer_s": 0})
        _feed(ent, 18.0, 14.7, 5.0, off=True)

        async def fail(*_a, **_k):
            raise TimeoutError

        real = ent.hass.services.async_call
        ent.hass.services.async_call = fail
        _cycle(ent)
        assert ent._last_reason == "send_failed(normal_update)"
        ent.hass.services.async_call = real
        clock.tick(60)
        _feed(ent, 18.0, 14.7, 5.0, off=True)
        _cycle(ent)
        assert _commands(ent) == []                     # rate limit
        clock.tick(130)
        _feed(ent, 18.0, 14.7, 5.0, off=True)
        _cycle(ent)
        assert _commands(ent) == [25.0]                 # retried at the interval

    def test_fallback_switch_keeps_the_heat_on_its_way(self, monkeypatch):
        """HomeKit valve with the cloud entity as fallback: 25 °C sent over
        HomeKit, HomeKit drops 10 s later while the cloud entity still shows
        off.  2.8.5 forgot the command on the switch: 6 min at 25 °C."""
        clock = _Clock(monkeypatch)
        ent = _make_entity({"heat_timer_s": 0, "fallback_entity_id": CLOUD})
        _run(_start(ent))

        def feed(room, primary, fallback):
            data = {"room_temp": room, "room_temp_ts": None}
            for prefix, (internal, setpoint, off, cloud) in (
                ("tado", primary), ("fallback", fallback)
            ):
                data.update({
                    f"{prefix}_internal_temp": internal,
                    f"{prefix}_setpoint": setpoint,
                    f"{prefix}_min_temp": 5.0, f"{prefix}_max_temp": 25.0,
                    f"{prefix}_off": off, f"{prefix}_cloud": cloud,
                    f"{prefix}_scheduled": False, f"{prefix}_heating": None,
                })
            ent.coordinator.data = data

        feed(20.6, (14.7, 5.0, True, False), (14.7, 5.0, True, True))
        _cycle(ent)
        clock.tick(60)
        ent._target_temp = 22.5
        _cycle(ent)
        assert _commands(ent) == [25.0]
        clock.tick(10)
        ent._target_temp = 20.5
        feed(20.6, (None, None, False, False), (14.7, 5.0, True, True))
        _cycle(ent)
        assert _commands(ent) == [25.0, "off"]
        assert ent.hass.service_calls[-1][2]["entity_id"] == CLOUD

    def test_adopting_an_off_valve_drops_its_maximum_and_timer(self, monkeypatch):
        """An off valve holds no maximum or timer of ours (the watchdog and
        the timer renewal must not act on them)."""
        clock = _Clock(monkeypatch)
        ent = _owner()
        _feed(ent, 18.0, 9.0, 5.0, off=True)
        _cycle(ent)
        assert ent._last_sent_was_max and ent._heat_timer_sent_ts is not None
        clock.tick(3600)                              # long after the command
        ent._target_temp = 17.0                       # no heat wanted any more
        _feed(ent, 18.0, 9.0, 5.0, off=True)          # off (from the Tado app)
        _cycle(ent)
        assert ent._last_reason == "already_at_target"
        assert not ent._last_sent_was_max and ent._heat_timer_sent_ts is None

    def test_clock_going_back_leaves_nothing_on_its_way(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _owner({"heat_timer_s": 0})
        _feed(ent, 18.0, 14.7, 5.0, off=True)
        _cycle(ent)
        assert _commands(ent) == [25.0]
        clock.tick(-3600)                             # NTP correction
        ent._target_temp = 17.0
        _feed(ent, 18.0, 14.7, 5.0, off=True)        # really off now
        _cycle(ent)
        assert _commands(ent) == [25.0]               # believed: nothing sent
        assert ent._last_reason == "already_at_target"


# ---------------------------------------------------------------------------
# Entry reload
# ---------------------------------------------------------------------------

class TestReload:

    def _heat_then_reload(self, monkeypatch, *, after_s=10.0, **reload_options):
        """Comfort 22.5 sends 25 °C (timer); the comfort number then goes
        back to 20.5, which reloads the room *after_s* later."""
        clock = _Clock(monkeypatch)
        old = _owner({"comfort_target": 22.5})
        _feed(old, 20.6, 14.7, 5.0, off=True)
        _cycle(old)
        assert _commands(old) == ["timer25"]
        clock.tick(after_s)
        new = _make_entity({"heat_timer_s": 1800, "comfort_target": 20.5,
                            **reload_options})
        new.coordinator = old.coordinator
        _feed(new, 20.6, 14.7, 5.0, off=True)        # the state still shows off
        _restart_from(old, new)
        return clock, new

    def test_a_reload_right_after_heat_keeps_the_command(self, monkeypatch):
        """2.8.5 forgot the command, took the stale "off" as done, and the
        25 °C timer that landed ran until the drift grace was over."""
        clock, new = self._heat_then_reload(monkeypatch)
        assert new._last_attempt_c == 25.0
        assert new._reg_state.mode == "heat"           # resumes from the command
        _cycle(new)
        assert _commands(new) == ["off"]
        assert new._last_reason == "sent(off_urgent_decrease)"
        clock.tick(60)
        _feed(new, 20.6, 14.7, 25.0, heating=True)    # the timer shows
        _cycle(new)
        # The off is on its way; it came seconds after the heat (before the
        # reload), so it is confirmed once a minute later (OFF_CONFIRM_S).
        assert _commands(new) == ["off", "off"]
        assert new._last_reason == "sent(off_confirm)"

    def test_a_reload_keeps_the_rate_limit(self, monkeypatch):
        """A hold push right after a reload waits for the interval like any
        other (2.8.5 sent it at once: a reload reset the rate limit)."""
        clock = _Clock(monkeypatch)
        old = _owner()
        _feed(old, 20.3, 15.0, 5.0, off=True)
        _cycle(old)
        (held,) = _commands(old)                      # a hold command
        assert old._reg_state.mode == "hold"
        clock.tick(10)
        new = _make_entity({"heat_timer_s": 1800, "comfort_target": 20.8})
        new.coordinator = old.coordinator
        _feed(new, 20.3, 15.0, held)
        _restart_from(old, new)
        assert new._reg_state.mode == "hold"
        _cycle(new)
        assert _commands(new) == []
        assert new._last_reason.startswith("rate_limited(")
        clock.tick(180)
        _feed(new, 20.3, 15.0, held)
        _cycle(new)
        assert len(_commands(new)) == 1 and _commands(new)[0] > held

    def test_an_old_command_is_not_restored(self, monkeypatch):
        """Past the longest drift grace the TRV's state says it all."""
        _, new = self._heat_then_reload(monkeypatch, after_s=400.0)
        assert new._last_attempt_c is None
        assert new._last_command_sent_ts == 0.0

    def test_a_command_to_another_trv_is_not_restored(self, monkeypatch):
        clock = _Clock(monkeypatch)
        old = _owner({"comfort_target": 22.5})
        _feed(old, 20.6, 14.7, 5.0, off=True)
        _cycle(old)
        saved = old.extra_restore_state_data.as_dict()
        assert saved["command"]["entity_id"] == TRV
        saved["command"]["entity_id"] = "climate.someone_else"
        clock.tick(10)
        new = _make_entity({"heat_timer_s": 1800})
        _feed(new, 20.6, 14.7, 5.0, off=True)

        class _Extra:
            def as_dict(self):
                return saved

        _run(_start(new, extra=_Extra()))
        assert new._last_attempt_c is None


# ---------------------------------------------------------------------------
# Back-off for a TRV that keeps not showing a command
# ---------------------------------------------------------------------------

def _count(ent, what):
    return _commands(ent).count(what)


class TestBackoff:

    def test_a_valve_that_never_shows_off(self, monkeypatch):
        """Stuck on its schedule (auto) whatever is sent: 2.8.5 sent 60 offs
        in 6 h; now the grace doubles up to 30 min."""
        clock = _Clock(monkeypatch)
        ent = _owner()
        for _ in range(6 * 60):
            _feed(ent, 21.2, 15.0, 20.0, scheduled=True)
            _cycle(ent)
            clock.tick(60)
        # At 0, 6, 18 and 42 min, then every 30 min.
        assert _count(ent, "off") == 14

    def test_the_backoff_ends_when_the_valve_shows_the_command(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _owner()
        for _ in range(60):
            _feed(ent, 21.2, 15.0, 20.0, scheduled=True)
            _cycle(ent)
            clock.tick(60)
        assert ent._resend_count >= 2
        _feed(ent, 21.2, 15.0, 5.0, off=True)
        _cycle(ent)
        assert ent._resend_count == 0
        # The off was shown: a valve then switched on in the app is a new
        # event, switched off again at once; if it still shows heat, the off
        # gets the plain 6-min grace, not the backed-off one.
        sent = _count(ent, "off")
        offs_at = []
        for minute in range(1, 21):
            clock.tick(60)
            _feed(ent, 21.2, 15.0, 21.0)
            _cycle(ent)
            if _count(ent, "off") > sent + len(offs_at):
                offs_at.append(minute)
        assert offs_at == [1, 7, 19]               # 6 min, then 12 (backing off)

    def test_a_valve_that_never_shows_heat(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _owner({"heat_timer_s": 0})
        for _ in range(6 * 60):
            _feed(ent, 20.3, 15.0, 5.0, off=True)
            _cycle(ent)
            clock.tick(60)
        assert len(_commands(ent)) <= 16

    def test_summer_lock_backs_off(self, monkeypatch):
        """2.8.5: an off every 3 min (the rate limit) to a valve that never
        showed it, 20 an hour."""
        clock = _Clock(monkeypatch)
        ent = _make_entity({"heat_timer_s": 1800}, summer=True)
        ent.hass.set(SUMMER, "on")
        ent.hass.set(TRV, "auto", {"temperature": 20.0, "current_temperature": 15.0})
        _run(_start(ent))
        for _ in range(3 * 60):
            _feed(ent, 21.2, 15.0, 20.0, scheduled=True)
            _cycle(ent)
            clock.tick(60)
        # At 0, 6, 18 and 42 min, then every 30 min: 8 in 3 h (2.8.5: 60).
        assert _count(ent, "off") == 8

    def test_summer_on_goes_out_at_once(self, monkeypatch):
        """2.8.6 review: summer mode starting right after an off the valve
        showed (then the app turned it on) waited for the off's grace."""
        clock = _Clock(monkeypatch)
        ent = _make_entity({"heat_timer_s": 1800}, summer=True)
        ent.hass.set(SUMMER, "off")
        _run(_start(ent))
        _feed(ent, 21.2, 15.0, 21.0)
        _cycle(ent)
        assert _commands(ent) == ["off"]
        clock.tick(60)
        _feed(ent, 21.2, 15.0, 5.0, off=True)
        _cycle(ent)
        clock.tick(60)
        _feed(ent, 21.2, 15.0, 22.0)                 # the app
        ent.hass.set(TRV, "heat", {"temperature": 22.0, "current_temperature": 15.0})
        ent.hass.set(SUMMER, "on")
        ent._async_summer_changed(
            __import__("tests.ha_harness", fromlist=["_event"])._event("on"))
        _cycle(ent)
        assert _commands(ent) == ["off", "off"]
        assert ent._last_reason == "sent(summer_enforce)"

    def test_roomstat_off_backs_off(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _owner()
        _feed(ent, 21.2, 15.0, 20.0, scheduled=True)
        _run(ent.async_set_hvac_mode(HVACMode.OFF))
        for _ in range(3 * 60):
            clock.tick(60)
            _feed(ent, 21.2, 15.0, 20.0, scheduled=True)
            _cycle(ent)
        assert _count(ent, "off") == 8                # 2.8.5: 33 in 3 h


# ---------------------------------------------------------------------------
# Roomstat's own OFF
# ---------------------------------------------------------------------------

class TestRoomstatOff:

    def _off_shown_then_app_heat(self, monkeypatch, **options):
        """The regulation's off is sent and shown; 2 min later the Tado app
        turns the valve on (shown from the next minute)."""
        clock = _Clock(monkeypatch)
        ent = _owner(options)
        _feed(ent, 21.2, 15.0, 21.0)
        _cycle(ent)
        assert _commands(ent) == ["off"]
        clock.tick(60)
        _feed(ent, 21.2, 15.0, 5.0, off=True)
        _cycle(ent)
        assert ent._last_attempt_shown
        clock.tick(60)
        _feed(ent, 21.2, 15.0, 22.0)                 # the app
        return clock, ent

    def test_off_after_an_app_change_goes_out_at_once(self, monkeypatch):
        """2.8.6 review: the off was still counted as on its way, and an
        explicit OFF waited for the rest of its 6-min grace."""
        _, ent = self._off_shown_then_app_heat(monkeypatch)
        _run(ent.async_set_hvac_mode(HVACMode.OFF))
        assert _commands(ent) == ["off", "off"]
        assert ent._last_reason == "sent(hvac_off)"

    def test_regulation_takes_an_app_change_back_at_the_rate_limit(self, monkeypatch):
        clock, ent = self._off_shown_then_app_heat(monkeypatch)
        _cycle(ent)                                    # 120 s after the off
        assert _commands(ent) == ["off"]
        clock.tick(60)
        _feed(ent, 21.2, 15.0, 22.0)
        _cycle(ent)                                    # 180 s: the rate limit
        assert _commands(ent) == ["off", "off"]
        assert ent._resend_count == 0                  # a new event, no back-off

    def test_a_heat_up_right_after_off_goes_out_once_settled(self, monkeypatch):
        """Heat-up at 25 (timer), Roomstat OFF, back to HEAT a minute later
        with the valve off: 2.8.5 held the heat-up back 2 min (the off was
        not booked as the last command, so the 25 counted as a retry).  It
        goes out once the change by hand has settled (MANUAL_SETTLE_S), not
        at the rate limit."""
        clock = _Clock(monkeypatch)
        ent = _owner()
        _feed(ent, 18.0, 9.0, 5.0, off=True)
        _cycle(ent)
        _feed(ent, 18.0, 9.0, 25.0)
        clock.tick(120)
        _run(ent.async_set_hvac_mode(HVACMode.OFF))
        assert _commands(ent) == ["timer25", "off"]
        clock.tick(60)
        _feed(ent, 18.0, 9.0, 5.0, off=True)
        _run(ent.async_set_hvac_mode(HVACMode.HEAT))
        assert _commands(ent) == ["timer25", "off"]
        assert ent._last_reason.startswith("settling(")
        _settle(clock)
        assert _commands(ent) == ["timer25", "off", "timer25"]
        assert ent._last_reason == "sent(strong)"

    def test_off_is_not_sent_to_a_valve_that_is_off(self, monkeypatch):
        """One API call fewer: the valve is off already."""
        _Clock(monkeypatch)
        ent = _owner()
        _feed(ent, 21.0, 15.0, 5.0, off=True)
        _cycle(ent)
        _run(ent.async_set_hvac_mode(HVACMode.OFF))
        assert _commands(ent) == []
        assert ent._last_reason == "hvac_off"

    def test_off_right_after_heat_is_sent_even_if_the_state_shows_off(self, monkeypatch):
        clock = _Clock(monkeypatch)
        ent = _owner()
        _feed(ent, 18.0, 9.0, 5.0, off=True)
        _cycle(ent)
        assert _commands(ent) == ["timer25"]
        clock.tick(10)
        _feed(ent, 18.0, 9.0, 5.0, off=True)         # stale
        _run(ent.async_set_hvac_mode(HVACMode.OFF))
        assert _commands(ent) == ["timer25", "off"]

    def test_off_waits_for_a_cycle_in_progress(self, monkeypatch):
        """The off runs under the regulation lock: it can't go out between a
        cycle's decision and its heat command."""
        _Clock(monkeypatch)
        ent = _owner()
        _feed(ent, 18.0, 9.0, 5.0, off=True)

        async def both():
            import asyncio
            cycle = asyncio.ensure_future(ent._async_regulation_cycle("timer"))
            await asyncio.sleep(0)                    # the cycle holds the lock
            await ent.async_set_hvac_mode(HVACMode.OFF)
            await cycle

        _run(both())
        assert _commands(ent) == ["timer25", "off"]
