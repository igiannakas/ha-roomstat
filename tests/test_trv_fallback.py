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
    """(entity, temperature) of every set_temperature call."""
    return [
        (d["entity_id"], d.get("temperature"))
        for dom, svc, d in ent.hass.service_calls
        if dom == "climate" and svc == "set_temperature"
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
    assert _sent(ent) == [(CLOUD, 5.0)]
