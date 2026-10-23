"""The card's status line, run in Node (skipped when Node is not installed).

``roomState`` in ``www/roomstat-card.js`` turns the climate attributes into
what the card shows.  Only the pure part of the file (constants, helpers and
``roomState``) is evaluated; nothing touches the DOM.
"""
from __future__ import annotations

import json
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
  for (const key of ["presence_override_until", "schedule_override_until"]) {
    if (typeof a[key] === "number") a[key] = new Date(now + a[key] * 1000).toISOString();
  }
  const s = roomState({ states: { "climate.x": { state: c.state || "heat", attributes: a } } },
                      { entity: "climate.x" }, {});
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
]


@pytest.mark.skipif(_NODE is None, reason="Node is not installed")
def test_status_lines(tmp_path):
    runner = tmp_path / "runner.js"
    runner.write_text(_RUNNER)
    payload = json.dumps([{"attrs": attrs} for attrs, *_ in _CASES])
    proc = subprocess.run(
        [_NODE, str(runner), str(_CARD)], input=payload, capture_output=True,
        text=True, timeout=30, check=True,
    )
    results = json.loads(proc.stdout)
    for (_attrs, status, badge, schedule_active), got in zip(_CASES, results, strict=True):
        assert got == {"status": status, "badge": badge, "scheduleActive": schedule_active}
