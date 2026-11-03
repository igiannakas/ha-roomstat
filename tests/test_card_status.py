"""The card's status line, run in Node (skipped when Node is not installed).

``roomState`` in ``www/roomstat-card.js`` turns the climate attributes into
what the card shows.  Only the pure part of the file (constants, helpers and
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
const roomState = new Function(src.slice(start, end) + "\nreturn roomState;")();
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const now = Date.now();
const out = cases.map((c) => {
  const a = { current_temperature: 17.4, temperature: 20, hvac_action: "idle", ...c.attrs };
  for (const key of ["presence_override_until", "schedule_override_until",
                     "window_heat_earliest", "window_heat_latest"]) {
    if (typeof a[key] === "number") a[key] = new Date(now + a[key] * 1000).toISOString();
  }
  const hass = { states: { "climate.x": { state: c.state || "heat", attributes: a } },
                 ...(c.hass || {}) };
  const s = roomState(hass, { entity: "climate.x" }, {});
  return { status: s.status, badge: s.badge, scheduleActive: s.scheduleActive };
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
    ({"preset_mode": "frost_protection", "window_open_active": True,
      "window_close_delay_active": True, "window_heat_earliest": -120,
      "window_heat_latest": -30},
     "Window", "mdi:window-closed-variant", False),
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
