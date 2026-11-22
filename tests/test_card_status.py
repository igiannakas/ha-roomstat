"""The card's status line, run in Node (skipped when Node is not installed).

``roomState`` in ``www/roomstat-card.js`` turns the climate attributes into
what the card shows, and ``boostConfirmText`` into the Boost button's
confirmation text.  Only the pure part of the file (constants, helpers and
``roomState``) is evaluated; nothing touches the DOM.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_CARD = Path(__file__).parent.parent / "custom_components" / "roomstat" / "www" / "roomstat-card.js"
_NODE = shutil.which("node")

_RUNNER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const start = src.indexOf("const CARD_VERSION");
const end = src.indexOf("\n}\n", src.indexOf("function roomState")) + 3;
const { roomState, boostConfirmText } = new Function(
  src.slice(start, end) + "\nreturn { roomState, boostConfirmText };")();
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const now = Date.now();
const out = cases.map((c) => {
  const a = { current_temperature: 17.4, temperature: 20, hvac_action: "idle", ...c.attrs };
  for (const key of ["presence_override_until", "schedule_override_until",
                     "window_heat_earliest", "window_heat_latest", "boost_until"]) {
    if (typeof a[key] === "number") a[key] = new Date(now + a[key] * 1000).toISOString();
  }
  const hass = { states: { "climate.x": { state: c.state || "heat", attributes: a } },
                 ...(c.hass || {}) };
  const s = roomState(hass, { entity: "climate.x" }, {});
  return { status: s.status, badge: s.badge, scheduleActive: s.scheduleActive,
           confirm: boostConfirmText({ ...s, boostTarget: c.boostTarget ?? null }) };
});
process.stdout.write(JSON.stringify(out));
"""

# Times are seconds from now (converted to ISO in Node, so they match its clock).
_CASES = [
    ({"preset_mode": "away", "presence_away_active": True, "schedule_preset": "comfort"},
     "Away · nobody home", None, False),
    ({"preset_mode": "comfort", "presence_override_active": True,
      "presence_override_until": 240 * 60 - 1, "schedule_preset": "comfort"},
     "Nobody home · Day · 4 h", "mdi:hand-back-right", False),
    ({"preset_mode": "none", "temperature": 21.5, "presence_override_active": True,
      "presence_override_until": 90 * 60 - 1},
     "Nobody home · 21.5° · 1 h 30 min", "mdi:hand-back-right", False),
    ({"preset_mode": "eco", "presence_override_active": True,
      "presence_override_until": 59 * 60 - 1},
     "Nobody home · Night · 59 min", "mdi:hand-back-right", False),
    ({"preset_mode": "boost", "presence_override_active": True,
      "presence_override_until": None},
     "Nobody home · Boost · until home", "mdi:hand-back-right", False),
    # An Off picked by hand while out, or on when everyone left (2.12.0).
    ({"preset_mode": "frost_protection", "presence_override_active": True,
      "presence_override_until": None, "schedule_preset": "comfort",
      "schedule_override_active": True},
     "Nobody home · Off · until home", "mdi:hand-back-right", False),
    # Just past its end: the integration is about to take it back.
    ({"preset_mode": "comfort", "presence_override_active": True,
      "presence_override_until": -30},
     "Nobody home · Day", "mdi:hand-back-right", False),
    # Nobody home wins over a schedule override running alongside.
    ({"preset_mode": "eco", "presence_override_active": True,
      "presence_override_until": 120 * 60 - 1, "schedule_preset": "comfort",
      "schedule_override_active": True, "schedule_override_until": 3600},
     "Nobody home · Night · 2 h", "mdi:hand-back-right", False),
    ({"preset_mode": "eco", "schedule_preset": "comfort", "schedule_override_active": True,
      "schedule_override_until": 45 * 60 - 1},
     "Override · Night · 45 min", "mdi:hand-back-right", False),
    ({"preset_mode": "comfort", "schedule_preset": "comfort"},
     "Schedule · Day", None, True),
    ({"preset_mode": "comfort", "presence_override_active": True, "window_open_active": True},
     "Window open", "mdi:window-open-variant", False),
    # Preheat on the way home, mode on (2.18.0): what it heats the room to.
    ({"preset_mode": "away", "presence_away_active": True, "schedule_preset": "comfort",
      "temperature": 20.0, "preheat_mode": "on", "preheat_state": "due",
      "preheat_heating_to_c": 20.0, "preheat_arrival_at": "2026-11-01T18:42:10+00:00"},
     "Nobody home · Preheating to 20 °C · ETA 18:42", None, False),
    ({"preset_mode": "away", "presence_away_active": True, "temperature": 20.5,
      "preheat_mode": "on", "preheat_state": "close", "preheat_heating_to_c": 20.5},
     "Nobody home · Someone close · 20.5 °C", None, False),
    ({"preset_mode": "away", "presence_away_active": True, "temperature": 19.3,
      "preheat_mode": "on", "preheat_state": "nearby", "preheat_heating_to_c": 19.3},
     "Nobody home · Someone nearby · 19.3 °C", None, False),
    # Due with no arrival known (it is being lost): no ETA.
    ({"preset_mode": "away", "presence_away_active": True,
      "preheat_mode": "on", "preheat_state": "due", "preheat_heating_to_c": 20.0,
      "preheat_arrival_at": None},
     "Nobody home · Preheating to 20 °C", None, False),
    # Waiting, or shadow (the attribute is only set while it heats): Away.
    ({"preset_mode": "away", "presence_away_active": True, "preheat_mode": "on",
      "preheat_state": "waiting", "preheat_heating_to_c": None},
     "Away · nobody home", None, False),
    ({"preset_mode": "away", "presence_away_active": True, "preheat_mode": "shadow",
      "preheat_state": "due", "preheat_target_c": 20.0, "preheat_heating_to_c": None},
     "Away · nobody home", None, False),
    # Shut, its close delay running (2.18.1): until heating can first come
    # back, the range; then the most it can still take while the room
    # re-warms by itself; a wait that can't be extended, just the minutes.
    ({"preset_mode": "frost_protection", "window_open_active": True,
      "window_close_delay_active": True, "window_heat_earliest": 6 * 60 - 1,
      "window_heat_latest": 28 * 60 - 1},
     "Window · 6–28 min", "mdi:window-closed-variant", False),
    ({"preset_mode": "frost_protection", "window_open_active": True,
      "window_close_delay_active": True, "window_heat_earliest": -60,
      "window_heat_latest": 18 * 60 - 1, "window_rewarm_c_per_h": 2.4},
     "Window · up to 18 min", "mdi:window-closed-variant", False),
    ({"preset_mode": "frost_protection", "window_open_active": True,
      "window_close_delay_active": True, "window_heat_earliest": 3 * 60 - 1,
      "window_heat_latest": 3 * 60 - 1},
     "Window · 3 min", "mdi:window-closed-variant", False),
    # Its times past and the wait still on (2.23.1, card 1.5.4): it ends at
    # the next regulation cycle.
    ({"preset_mode": "frost_protection", "window_open_active": True,
      "window_close_delay_active": True, "window_heat_earliest": -120,
      "window_heat_latest": -30},
     "Window · <1 min", "mdi:window-closed-variant", False),
    ({"preset_mode": "frost_protection", "window_open_active": True,
      "window_close_delay_active": True},
     "Window", "mdi:window-closed-variant", False),
    # Open, overridden by hand (2.22.0): the mode it heats on, with the
    # open window as the badge (over the hand of the schedule's override).
    ({"preset_mode": "comfort", "window_overridden": True, "schedule_preset": "eco",
      "schedule_override_active": True, "schedule_override_until": 60 * 60 - 1},
     "Override · Day · 60 min", "mdi:window-open-variant", False),
    ({"preset_mode": "comfort", "window_overridden": True, "schedule_preset": "comfort"},
     "Schedule · Day", "mdi:window-open-variant", True),
    # A running Boost shows its own time left (2.27.4, card 1.5.5).  The
    # owner's case, 20 Nov 2026: Boost 15 min, the override 30 min; the card
    # read "Override · Boost · 27 min" from the override.  The override now
    # ends with the Boost, and the card reads the Boost's end even from an
    # integration whose override still runs longer.
    ({"preset_mode": "boost", "temperature": 22.0, "schedule_preset": "comfort",
      "schedule_override_active": True, "schedule_override_until": 27 * 60 - 1,
      "boost_until": 13 * 60 - 1},
     "Override · Boost · 13 min", "mdi:hand-back-right", False),
    ({"preset_mode": "boost", "schedule_preset": "comfort",
      "schedule_override_active": True, "schedule_override_until": 90 * 60 - 1,
      "boost_until": 90 * 60 - 1},
     "Override · Boost · 1 h 30 min", "mdi:hand-back-right", False),
    # Its end just passed: the integration is ending it.
    ({"preset_mode": "boost", "schedule_preset": "comfort",
      "schedule_override_active": True, "schedule_override_until": -20,
      "boost_until": -20},
     "Override · Boost · <1 min", "mdi:hand-back-right", False),
    # No schedule: no override, the Boost's own time (none shown before).
    ({"preset_mode": "boost", "boost_until": 8 * 60 - 1},
     "Boost · 8 min", None, False),
    # Resumed after a reload: the end saved with it, the rest of the time.
    ({"preset_mode": "boost", "schedule_preset": "eco", "schedule_override_active": True,
      "schedule_override_until": 10 * 60 - 1, "boost_until": 10 * 60 - 1},
     "Override · Boost · 10 min", "mdi:hand-back-right", False),
    # Nobody home: the Boost's time, not the hold's 4 h (the Boost ends
    # first, then the hold carries on with what it ends on) ...
    ({"preset_mode": "boost", "presence_override_active": True,
      "presence_override_until": 240 * 60 - 1, "schedule_preset": "comfort",
      "schedule_override_active": True, "schedule_override_until": 15 * 60 - 1,
      "boost_until": 15 * 60 - 1},
     "Nobody home · Boost · 15 min", "mdi:hand-back-right", False),
    ({"preset_mode": "boost", "presence_override_active": True,
      "presence_override_until": None, "boost_until": 12 * 60 - 1},
     "Nobody home · Boost · 12 min", "mdi:hand-back-right", False),
    # ... unless the hold's limit comes first: back to away ends the Boost.
    ({"preset_mode": "boost", "presence_override_active": True,
      "presence_override_until": 5 * 60 - 1, "boost_until": 60 * 60 - 1},
     "Nobody home · Boost · 5 min", "mdi:hand-back-right", False),
    # The schedule moved on mid-Boost (2.27.5): the thermostat's next state
    # is the schedule's preset, its override and boost_until cleared, so the
    # line reads the schedule at once, with no time left shown.
    ({"preset_mode": "eco", "temperature": 18.0, "schedule_preset": "eco",
      "schedule_override_active": False, "schedule_override_until": None,
      "boost_until": None, "boost_returns_to": "schedule"},
     "Schedule · Night", None, True),
]

# The clock in the user's language, 24-hour choice and the server's time zone.
_HASS = {"locale": {"language": "en-GB", "time_format": "24", "time_zone": "server"},
         "config": {"time_zone": "Europe/London"}}


@pytest.mark.skipif(_NODE is None, reason="Node is not installed")
def test_status_lines(tmp_path):
    runner = tmp_path / "runner.js"
    runner.write_text(_RUNNER)
    payload = json.dumps([{"attrs": attrs, "hass": _HASS} for attrs, *_ in _CASES])
    proc = subprocess.run(
        [_NODE, str(runner), str(_CARD)], input=payload, capture_output=True,
        text=True, timeout=30, check=True,
    )
    results = json.loads(proc.stdout)
    for (_attrs, status, badge, schedule_active), got in zip(_CASES, results, strict=True):
        got.pop("confirm")
        assert got == {"status": status, "badge": badge, "scheduleActive": schedule_active}


@pytest.mark.skipif(_NODE is None, reason="Node is not installed")
@pytest.mark.parametrize(("hass", "expected"), [
    # Summer time in London: the server's zone, 24 h.
    ({"locale": {"language": "en-GB", "time_format": "24", "time_zone": "server"},
      "config": {"time_zone": "Europe/London"}}, "18:42"),
    ({"locale": {"language": "en-GB", "time_format": "24", "time_zone": "server"},
      "config": {"time_zone": "Europe/Athens"}}, "20:42"),
    ({"locale": {"language": "en-US", "time_format": "12", "time_zone": "server"},
      "config": {"time_zone": "Europe/London"}}, "6:42 PM"),
    # The language's own choice, and an unknown language tag (the browser's).
    ({"locale": {"language": "en-US", "time_format": "language", "time_zone": "server"},
      "config": {"time_zone": "Europe/London"}}, "6:42 PM"),
    ({"locale": {"language": "not a tag!", "time_format": "24", "time_zone": "server"},
      "config": {"time_zone": "Europe/London"}}, "18:42"),
])
def test_the_eta_reads_as_the_user_asked(tmp_path, hass, expected):
    runner = tmp_path / "runner.js"
    runner.write_text(_RUNNER)
    attrs = {"preset_mode": "away", "presence_away_active": True, "preheat_mode": "on",
             "preheat_state": "due", "preheat_heating_to_c": 20.0,
             "preheat_arrival_at": "2026-07-01T17:42:00+00:00"}
    proc = subprocess.run(
        [_NODE, str(runner), str(_CARD)],
        input=json.dumps([{"attrs": attrs, "hass": hass}]),
        capture_output=True, text=True, timeout=30, check=True,
    )
    (got,) = json.loads(proc.stdout)
    assert got["status"] == f"Nobody home · Preheating to 20 °C · ETA {expected}"


@pytest.mark.skipif(_NODE is None, reason="Node is not installed")
@pytest.mark.parametrize(("browser", "language", "expected"), [
    # "Use system settings": the browser's 12/24-hour choice, not the
    # language's (Node takes its default locale from LC_ALL).
    ("en_US.UTF-8", "en-GB", "6:42 pm"),
    ("en_GB.UTF-8", "en-US", "18:42"),
])
def test_the_system_choice_is_the_browsers(tmp_path, browser, language, expected):
    runner = tmp_path / "runner.js"
    runner.write_text(_RUNNER)
    attrs = {"preset_mode": "away", "presence_away_active": True, "preheat_mode": "on",
             "preheat_state": "due", "preheat_heating_to_c": 20.0,
             "preheat_arrival_at": "2026-07-01T17:42:00+00:00"}
    hass = {"locale": {"language": language, "time_format": "system", "time_zone": "server"},
            "config": {"time_zone": "Europe/London"}}
    proc = subprocess.run(
        [_NODE, str(runner), str(_CARD)],
        input=json.dumps([{"attrs": attrs, "hass": hass}]),
        capture_output=True, text=True, timeout=30, check=True,
        env={**os.environ, "LC_ALL": browser, "LANG": browser},
    )
    (got,) = json.loads(proc.stdout)
    assert got["status"] == f"Nobody home · Preheating to 20 °C · ETA {expected}"


# The Boost button's confirmation text (2.27.4, card 1.5.5): what the Boost
# ends on is the integration's ``boost_returns_to``; one already running is
# restarted at full length.
_CONFIRM_CASES = [
    # The owner's case: a schedule, so back to it; and the schedule's next
    # change ends it too, at a time the integration does not know (card
    # 1.5.6; 20 Nov 2026, second bedroom: a 30-min Boost ended 10 min early).
    ({"current_temperature": 21.3, "preset_mode": "comfort", "schedule_preset": "comfort",
      "boost_duration_min": 15, "boost_returns_to": "schedule"},
     "Currently 21.3 °C. Heats to 22 °C for 15 minutes, or until the schedule next "
     "changes, then goes back to its schedule."),
    # Pressed again while one runs: it says so, and what is left.
    ({"current_temperature": 21.3, "preset_mode": "boost", "schedule_preset": "comfort",
      "schedule_override_active": True, "boost_duration_min": 15,
      "boost_until": 9 * 60 - 1, "boost_returns_to": "schedule"},
     "Currently 21.3 °C. Boost is running, 9 min left. Restarts it for 15 minutes, "
     "or until the schedule next changes, then goes back to its schedule."),
    # No schedule: the preset it was on (until 2.27.3 always "then switches
    # to Day.", while the Boost went back to the preset before it).
    ({"current_temperature": 18.0, "preset_mode": "eco", "boost_duration_min": 15,
      "boost_returns_to": "eco"},
     "Currently 18.0 °C. Heats to 22 °C for 15 minutes, then goes back to Night."),
    ({"current_temperature": 18.0, "preset_mode": "none", "temperature": 19.5,
      "boost_duration_min": 15, "boost_returns_to": "none", "boost_returns_to_c": 19.5},
     "Currently 18.0 °C. Heats to 22 °C for 15 minutes, then goes back to 19.5 °C."),
    ({"current_temperature": 18.0, "preset_mode": "boost", "boost_duration_min": 15,
      "boost_until": -10, "boost_returns_to": "frost_protection"},
     "Currently 18.0 °C. Boost is running, <1 min left. Restarts it for 15 minutes, "
     "then goes back to Off."),
    # Nobody home, no schedule: away mode first takes the room back to what
    # it saved, which the integration does not guess.
    ({"current_temperature": 16.0, "preset_mode": "away", "presence_away_active": True,
      "boost_duration_min": 15, "boost_returns_to": None},
     "Currently 16.0 °C. Heats to 22 °C for 15 minutes, then goes back to what it was on before."),
]


@pytest.mark.skipif(_NODE is None, reason="Node is not installed")
def test_boost_confirmation_text(tmp_path):
    runner = tmp_path / "runner.js"
    runner.write_text(_RUNNER)
    payload = json.dumps(
        [{"attrs": attrs, "hass": _HASS, "boostTarget": 22} for attrs, _ in _CONFIRM_CASES]
    )
    proc = subprocess.run(
        [_NODE, str(runner), str(_CARD)], input=payload, capture_output=True,
        text=True, timeout=30, check=True,
    )
    results = json.loads(proc.stdout)
    for (_attrs, text), got in zip(_CONFIRM_CASES, results, strict=True):
        assert got["confirm"] == text
