"""Cloud fallback for an unavailable TRV, run through the real climate entity.

The configured TRV (e.g. a Tado valve via HomeKit) can drop off for minutes
at a time.  With a fallback entity configured (e.g. the room's Tado cloud
thermostat), Roomstat reads and commands the room through it until the TRV
is back, and the auto-tuner treats that time as a gap.
"""
from __future__ import annotations

import pytest

from tests.ha_harness import (
    SUMMER,
    TRV,
    _climate,
    _event,
    _make_entity,
    _run,
    _start,
    e2e,
    reset_timers,
)

pytestmark = e2e

CLOUD = "climate.room_cloud"


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


class _Clock:
    def __init__(self, monkeypatch, start=1_800_000_000.0):
        self.now = start
        monkeypatch.setattr("time.time", lambda: self.now)

    def tick(self, s=60.0):
        self.now += s


def _feed(ent, *, room=19.0, trv=(18.0, 20.0), cloud=(18.0, 20.0)):
    """Coordinator data: room sensor, then (reading, setpoint) of each TRV
    entity; None for an entity that is unavailable."""
    trv = trv or (None, None)
    cloud = cloud or (None, None)
    ent.coordinator.data = {
        "room_temp": room, "room_temp_ts": None,
        "tado_internal_temp": trv[0], "tado_setpoint": trv[1],
        "fallback_internal_temp": cloud[0], "fallback_setpoint": cloud[1],
    }


def _cycle(ent):
    _run(ent._async_regulation_cycle("timer"))


def _sent(ent):
    """(entity, temperature) of every set_temperature call, and (entity,
    "off") of every switch-off (the regulation's minimum since 2.8.4)."""
    return [
        (d["entity_id"], d.get("temperature") if svc == "set_temperature" else "off")
        for dom, svc, d in ent.hass.service_calls
        if dom == "climate" and (
            svc == "set_temperature"
            or (svc == "set_hvac_mode" and d.get("hvac_mode") == "off"))
    ]


def _entity(options=None):
    ent = _make_entity({"fallback_entity_id": CLOUD, **(options or {})})
    _run(_start(ent))
    return ent


def test_valve_used_while_available(monkeypatch):
    _Clock(monkeypatch)
    ent = _entity()
    _feed(ent)
    _cycle(ent)
    assert _sent(ent) and all(e == TRV for e, _ in _sent(ent))
    attrs = ent.extra_state_attributes
    assert attrs["trv_fallback_active"] is False
    assert attrs["active_trv_entity_id"] == TRV


def test_fallback_while_valve_unavailable_then_back(monkeypatch):
    clock = _Clock(monkeypatch)
    ent = _entity()
    _feed(ent)
    _cycle(ent)
    ent.hass.service_calls.clear()

    # The valve drops off HomeKit: the cloud thermostat takes over.
    clock.tick(300)
    _feed(ent, trv=None, cloud=(18.4, 20.0))
    _cycle(ent)
    assert ent._last_reason != "waiting_for_sensors"
    assert ent.extra_state_attributes["trv_fallback_active"] is True
    assert ent.extra_state_attributes["active_trv_entity_id"] == CLOUD
    assert ent.extra_state_attributes["tado_internal_temp_c"] == 18.4
    assert _sent(ent) and all(e == CLOUD for e, _ in _sent(ent))

    # Back on HomeKit: commands go to the valve again.
    ent.hass.service_calls.clear()
    clock.tick(300)
    _feed(ent, trv=(18.5, 15.0))       # reports a stale setpoint: resend
    _cycle(ent)
    assert ent.extra_state_attributes["trv_fallback_active"] is False
    assert _sent(ent) and all(e == TRV for e, _ in _sent(ent))


def test_switch_compares_with_what_the_new_entity_holds(monkeypatch):
    """A command sent just before the dropout may never have arrived, so
    after a switch the command is compared with the fallback's setpoint."""
    clock = _Clock(monkeypatch)
    ent = _entity()
    _feed(ent)
    _cycle(ent)
    sent_before = ent._last_sent_setpoint
    clock.tick(300)
    _feed(ent, trv=None, cloud=(18.0, sent_before - 3.0))
    _cycle(ent)
    assert _sent(ent)[-1][0] == CLOUD
    assert ent._last_reason.startswith("sent(")


def test_without_fallback_waits_for_the_valve(monkeypatch):
    _Clock(monkeypatch)
    ent = _make_entity()
    _run(_start(ent))
    _feed(ent, trv=None, cloud=(18.0, 20.0))
    _cycle(ent)
    assert ent._last_reason == "waiting_for_sensors"
    assert ent.hass.service_calls == []


def test_both_unavailable_waits(monkeypatch):
    _Clock(monkeypatch)
    ent = _entity()
    _feed(ent, trv=None, cloud=None)
    _cycle(ent)
    assert ent._last_reason == "waiting_for_sensors"
    assert ent.hass.service_calls == []


def test_tuner_keeps_learning_on_the_fallback(monkeypatch):
    """Auto-tune carries on from the room sensor, but without the cloud's
    delayed TRV reading (so the TRV demand is unknown for those cycles)."""
    clock = _Clock(monkeypatch)
    ent = _entity({"autotune_enabled": True, "correction_kp": 0.6,
                   "correction_ki": 0.002})
    seen = []
    orig = ent._autotuner.observe
    ent._autotuner.observe = lambda s: (seen.append(s), orig(s))[1]
    _feed(ent)
    _cycle(ent)
    for _ in range(3):
        clock.tick(60)
        _feed(ent, room=19.1, trv=None)
        _cycle(ent)
    clock.tick(60)
    _feed(ent)
    _cycle(ent)
    assert len(seen) == 5                 # no gap
    assert [s.tado_internal_c for s in seen] == [18.0, None, None, None, 18.0]
    assert all(s.room_temp_c is not None and s.eligible for s in seen)
    assert seen[2].demand_c is None


def test_command_the_valve_did_not_take_is_sent_again(monkeypatch):
    """The valve reports a different setpoint long after the command (it was
    lost, or a Tado schedule block ended the override): send it again."""
    clock = _Clock(monkeypatch)
    ent = _make_entity({"overlay_refresh_s": 0})
    _run(_start(ent))
    _feed(ent)
    _cycle(ent)
    sent = ent._last_sent_setpoint
    assert sent is not None
    ent.hass.service_calls.clear()

    # Shortly after the command the valve may not have reported it yet.
    clock.tick(60)
    _feed(ent, trv=(18.0, 15.0))
    _cycle(ent)
    assert _sent(ent) == []

    # Well after it, a different setpoint means the command did not stick.
    clock.tick(180)
    _feed(ent, trv=(18.0, 15.0))
    _cycle(ent)
    assert _sent(ent) == [(TRV, sent)]

    # Holding the command: nothing more is sent, ever (no blind re-sends).
    ent.hass.service_calls.clear()
    for _ in range(20):
        clock.tick(60)
        _feed(ent, trv=(18.0, sent))
        _cycle(ent)
    assert _sent(ent) == []


def test_commands_from_all_rooms_are_spaced(monkeypatch):
    """Rooms acting at the same moment reach the bridge one by one."""
    import sys
    reg = sys.modules["roomstat_e2e.climate_regulation"]
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr(reg.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(reg.time, "monotonic", lambda: 5000.0)
    monkeypatch.setattr(reg, "COMMAND_PACER", reg.CommandPacer(5.0))
    _Clock(monkeypatch)
    rooms = []
    for _ in range(3):
        ent = _make_entity()
        _run(_start(ent))
        _feed(ent)
        rooms.append(ent)
    for ent in rooms:
        _cycle(ent)
    assert all(len(_sent(ent)) == 1 for ent in rooms)
    assert waits == [5.0, 10.0]


def test_hvac_off_reaches_the_fallback(monkeypatch):
    _Clock(monkeypatch)
    ent = _entity()
    _feed(ent, trv=None)
    _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
    calls = [(d["entity_id"], d["hvac_mode"]) for dom, svc, d in ent.hass.service_calls
             if svc == "set_hvac_mode"]
    assert calls == [(CLOUD, _climate.HVACMode.OFF)]


def test_summer_lock_enforced_through_the_fallback(monkeypatch):
    _Clock(monkeypatch)
    ent = _make_entity({"fallback_entity_id": CLOUD}, summer=True)
    ent.hass.set(SUMMER, "off")
    ent.hass.set(TRV, "unavailable")
    ent.hass.set(CLOUD, "heat", {"temperature": 20.0, "current_temperature": 18.0})
    _run(_start(ent))
    _feed(ent, trv=None)
    ent.hass.set(SUMMER, "on")
    ent._async_summer_changed(_event("on"))
    _cycle(ent)
    assert _sent(ent) == [(CLOUD, "off")]


# ---------------------------------------------------------------------------
# prefer_fallback: the fallback (e.g. the Tado cloud) first, the TRV as backup
# ---------------------------------------------------------------------------

def _preferred(options=None):
    return _entity({"prefer_fallback": True, **(options or {})})


def test_prefer_fallback_commands_through_the_fallback(monkeypatch):
    _Clock(monkeypatch)
    ent = _preferred()
    _feed(ent)
    _cycle(ent)
    assert _sent(ent) and all(e == CLOUD for e, _ in _sent(ent))
    attrs = ent.extra_state_attributes
    assert attrs["trv_fallback_active"] is True
    assert attrs["trv_prefer_fallback"] is True
    assert attrs["active_trv_entity_id"] == CLOUD


def test_prefer_fallback_uses_the_valve_while_the_fallback_is_down(monkeypatch, caplog):
    clock = _Clock(monkeypatch)
    ent = _preferred()
    _feed(ent)
    _cycle(ent)
    ent.hass.service_calls.clear()

    # The cloud drops out: the valve (HomeKit) takes over, with a warning.
    clock.tick(300)
    with caplog.at_level("INFO"):
        _feed(ent, trv=(18.2, 20.0), cloud=None)
        _cycle(ent)
    assert ent.extra_state_attributes["trv_fallback_active"] is False
    assert _sent(ent) and all(e == TRV for e, _ in _sent(ent))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(f"{CLOUD} unavailable, using {TRV}" in r.getMessage() for r in warnings)

    # The cloud is back: commands go through it again (info, not a warning).
    caplog.clear()
    ent.hass.service_calls.clear()
    clock.tick(300)
    with caplog.at_level("INFO"):
        _feed(ent, cloud=(18.4, 15.0))   # stale setpoint after the switch: resend
        _cycle(ent)
    assert ent.extra_state_attributes["trv_fallback_active"] is True
    assert _sent(ent) and all(e == CLOUD for e, _ in _sent(ent))
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(f"{CLOUD} available again" in r.getMessage() for r in caplog.records)


def test_prefer_fallback_starts_on_it_without_a_switch_warning(monkeypatch, caplog):
    _Clock(monkeypatch)
    ent = _preferred()
    with caplog.at_level("INFO"):
        _feed(ent)
        _cycle(ent)
    assert not [r for r in caplog.records if "unavailable" in r.getMessage()]


def test_prefer_fallback_waits_for_it_when_both_are_down(monkeypatch):
    _Clock(monkeypatch)
    ent = _preferred()
    _feed(ent, trv=None, cloud=None)
    _cycle(ent)
    assert ent._last_reason == "waiting_for_sensors"
    assert ent.hass.service_calls == []


def test_prefer_fallback_without_a_fallback_is_ignored(monkeypatch):
    _Clock(monkeypatch)
    ent = _make_entity({"prefer_fallback": True})
    _run(_start(ent))
    _feed(ent)
    _cycle(ent)
    assert _sent(ent) and all(e == TRV for e, _ in _sent(ent))
    assert ent.extra_state_attributes["trv_prefer_fallback"] is False


def test_prefer_fallback_tuner_reads_the_valve(monkeypatch):
    """Commands go through the cloud, but auto-tune gets the valve's own
    reading from HomeKit while it is there (the same valve, pushed at once),
    and none while only the cloud reads."""
    clock = _Clock(monkeypatch)
    ent = _preferred({"autotune_enabled": True, "correction_kp": 0.6,
                      "correction_ki": 0.002})
    seen = []
    orig = ent._autotuner.observe
    ent._autotuner.observe = lambda s: (seen.append(s), orig(s))[1]
    _feed(ent, trv=(18.0, 20.0), cloud=(17.5, 20.0))
    _cycle(ent)
    clock.tick(60)
    _feed(ent, room=19.1, trv=None, cloud=(17.5, 20.0))
    _cycle(ent)
    assert [s.tado_internal_c for s in seen] == [18.0, None]


# --- A cloud thermostat as the configured TRV (no HomeKit at all) -----------


def _feed_cloud_trv(ent, *, room=19.0, trv=(18.0, 20.0)):
    """Coordinator data when the configured TRV is a cloud entity."""
    _feed(ent, room=room, trv=trv, cloud=None)
    ent.coordinator.data["tado_cloud"] = True


def test_cloud_trv_gets_the_slower_drift_grace(monkeypatch):
    """A configured TRV read through cloud polling waits the fallback's
    grace (6 min), not the local 2 min, before re-sending a command."""
    clock = _Clock(monkeypatch)
    ent = _make_entity({"overlay_refresh_s": 0})
    _run(_start(ent))
    _feed_cloud_trv(ent)
    _cycle(ent)
    sent = ent._last_sent_setpoint
    assert sent is not None
    assert ent.extra_state_attributes["trv_cloud"] is True
    assert ent.extra_state_attributes["trv_fallback_active"] is False
    ent.hass.service_calls.clear()

    # 4 min later the cloud still shows the old setpoint: not lost yet.
    clock.tick(240)
    _feed_cloud_trv(ent, trv=(18.0, 15.0))
    _cycle(ent)
    assert _sent(ent) == []

    # Past 6 min it is.
    clock.tick(180)
    _feed_cloud_trv(ent, trv=(18.0, 15.0))
    _cycle(ent)
    assert _sent(ent) == [(TRV, sent)]


def test_local_trv_keeps_the_short_grace(monkeypatch):
    clock = _Clock(monkeypatch)
    ent = _make_entity({"overlay_refresh_s": 0})
    _run(_start(ent))
    _feed(ent, cloud=None)
    _cycle(ent)
    sent = ent._last_sent_setpoint
    assert ent.extra_state_attributes["trv_cloud"] is False
    ent.hass.service_calls.clear()
    clock.tick(240)
    _feed(ent, trv=(18.0, 15.0), cloud=None)
    _cycle(ent)
    assert _sent(ent) == [(TRV, sent)]


def test_cloud_trv_reading_feeds_the_tuner(monkeypatch):
    """With no local link the cloud reading is the only one, so the tuner
    gets it (the demand crossing comes from the command step)."""
    clock = _Clock(monkeypatch)
    ent = _make_entity({"autotune_enabled": True, "correction_kp": 0.6,
                        "correction_ki": 0.002})
    _run(_start(ent))
    seen = []
    orig = ent._autotuner.observe
    ent._autotuner.observe = lambda s: (seen.append(s), orig(s))[1]
    _feed_cloud_trv(ent)
    _cycle(ent)
    clock.tick(60)
    _feed_cloud_trv(ent, room=19.1, trv=(18.4, 20.0))
    _cycle(ent)
    assert [s.tado_internal_c for s in seen] == [18.0, 18.4]
