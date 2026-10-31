"""The activity log: what a room is doing and why, in plain lines (HA-free).

One line per regulation decision: at once when the decision changes (the
mode, what the valve is told, a command waiting or failing, the target),
and at most every ``DECISION_REFRESH_S`` while only its numbers drift; and
one line per event (a preset or schedule change, a window, presence, a
command that failed or went out again, an auto-tune verdict).  The
activity sensor shows the latest line as its state, so its history is the
log, and keeps the last ``MAX_LINES`` on itself for a quick read.

A decision line names what the regulator did, why, and why not something
else, from the same predictions the regulator decided on
(:func:`regulation.outlook`), then the room, the valve's own reading and
what the valve has been told::

    Holding: 0.5 below, valve pushed 1.9 over its reading (P +1.4, I +0.00;
    I rests outside ±0.3; full heat only from 0.8 below) | room 19.5 → 20.0
    Comfort | TRV 19.2 heating | Tado 21.1 (21.4 waits: rate limit)
"""
from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .parameters import RegulationConfig
from .regulation import MODE_COAST, MODE_DOSE, MODE_HEAT, MODE_HOLD, MODE_STOP, outlook

MAX_LINES = 30
# While a decision stays the same and only its numbers move (the room, the
# valve's reading, P and I), its line is written again at most this often.
# The live line is the sensor's ``doing`` attribute.
DECISION_REFRESH_S = 300.0
# Home Assistant refuses a state longer than this.
STATE_MAX_LEN = 255
# Longest event kept (cut into state-sized lines, see split_lines).
_TEXT_MAX_LEN = 1000
# Timestamps a stored line may carry (2001-2286): anything else is corrupt.
_TS_MIN, _TS_MAX = 1.0e9, 1.0e10

# How a regulation cycle ended (CycleFacts.path).
PATH_REGULATED = "regulated"
PATH_SUMMER = "summer"
PATH_OFF = "off"
PATH_NO_DATA = "no_data"
PATH_SENSOR_LOST = "sensor_lost"
PATH_WAITING = "waiting"

# What a window is doing to the room (CycleFacts.window).
WINDOW_OPEN = "open"          # the window action holds the valve off
WINDOW_REWARM = "rewarm"      # closed, in the close delay / re-warm wait
WINDOW_PENDING = "pending"    # open, the window's open delay still running
WINDOW_IGNORED = "ignored"    # open, but no window action running or due


# The presets as the UI names them (strings.json, preset_mode).
PRESET_NAMES = {
    "comfort": "Comfort",
    "eco": "Night",
    "boost": "Boost",
    "away": "Away",
    "frost_protection": "Frost protection",
    "none": "Manual",
}


def preset_name(preset: str | None) -> str:
    """'Night' for 'eco', and so on."""
    return PRESET_NAMES.get(preset or "", str(preset or ""))


def clip(text: str, limit: int = STATE_MAX_LEN) -> str:
    """Shorten text to at most ``limit`` characters, marking the cut."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def split_lines(text: str, limit: int = STATE_MAX_LEN) -> list[str]:
    """Cut text into pieces of at most ``limit`` characters at spaces, each
    piece but the last ending in ' …' and each but the first starting with
    '… ', so a long auto-tune verdict survives in the state history whole."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    rest = text
    while rest:
        head = "… " if pieces else ""
        room = limit - len(head)
        if len(rest) <= room:
            pieces.append(head + rest)
            break
        room -= 2                                   # the trailing " …"
        cut = rest.rfind(" ", 0, room + 1)
        if cut <= room // 2:
            cut = room
        pieces.append(head + rest[:cut].rstrip() + " …")
        rest = rest[cut:].lstrip()
    return pieces


@dataclass(frozen=True)
class Entry:
    """One line of the activity log."""

    ts: float
    text: str
    decision: bool
    key: str = ""      # a decision's shape without its numbers (decision_key)


class ActivityLog:
    """The last lines of a room's activity, newest last."""

    def __init__(self, max_lines: int = MAX_LINES) -> None:
        self._entries: deque[Entry] = deque(maxlen=max_lines)
        self.current: str | None = None    # the latest decision, live

    def add_event(self, ts: float, text: str) -> int:
        """Something happened: one line, or several if it is longer than a
        state may be (each is written as its own state, so the recorder
        keeps all of it).  Returns the number of lines added."""
        chunks = split_lines(clip(text, _TEXT_MAX_LEN))
        for chunk in chunks:
            self._entries.append(Entry(ts, chunk, False))
        return len(chunks)

    def add_decision(self, ts: float, text: str, key: str = "",
                     refresh_s: float = DECISION_REFRESH_S) -> bool:
        """A regulation decision.  A new line when it changes (``key``), and
        while only its numbers move at most every ``refresh_s``; always
        after an event, so the latest line is again what the room is doing.
        Returns whether a line was added."""
        text = clip(text, STATE_MAX_LEN)
        self.current = text
        last = self._entries[-1] if self._entries else None
        if last is not None and last.decision:
            if last.text == text:
                return False
            if key and last.key == key and 0 <= ts - last.ts < refresh_s:
                return False
        self._entries.append(Entry(ts, text, True, key))
        return True

    @property
    def latest(self) -> Entry | None:
        return self._entries[-1] if self._entries else None

    @property
    def doing(self) -> str | None:
        """What the room is doing now: the latest decision, live."""
        if self.current is not None:
            return self.current
        for entry in reversed(self._entries):
            if entry.decision:
                return entry.text
        return None

    def entries(self) -> list[Entry]:
        """Newest first."""
        return list(reversed(self._entries))

    def as_dict(self) -> dict[str, Any]:
        return {"lines": [[round(e.ts, 1), e.text, e.decision] for e in self._entries]}

    def restore(self, data: Any) -> bool:
        """Take back the lines saved by :meth:`as_dict`; anything malformed
        is skipped.  Returns whether any line was restored."""
        if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
            return False
        lines: list[Entry] = []
        for item in data["lines"]:
            if not (isinstance(item, list) and len(item) == 3):
                continue
            ts, text, decision = item
            if (isinstance(ts, bool) or not isinstance(ts, (int, float))
                    or not math.isfinite(ts) or not _TS_MIN <= ts <= _TS_MAX
                    or not isinstance(text, str) or not isinstance(decision, bool)):
                continue
            # The stored order is the log's order, also across a clock step.
            lines.append(Entry(float(ts), clip(text, STATE_MAX_LEN), decision))
        self._entries.clear()
        self._entries.extend(lines[-(self._entries.maxlen or MAX_LINES):])
        return bool(lines)


class RoomLogAdapter(logging.LoggerAdapter):
    """Logs a room's messages with the room's name in front, and hands every
    message at info or above to the room's activity log, whatever level the
    logger is set to (Home Assistant logs only warnings by default)."""

    def __init__(self, logger: logging.Logger, room: str,
                 sink: Callable[[int, str], None]) -> None:
        super().__init__(logger, {})
        self.room = room
        self._sink = sink

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        return f"{self.room}: {msg}", kwargs

    def log(self, level: int, msg: Any, *args: Any, **kwargs: Any) -> None:
        # The record names the caller, not this method.
        kwargs.setdefault("stacklevel", 2)
        if level >= logging.INFO:
            try:
                text = str(msg) % args if args else str(msg)
            except (TypeError, ValueError):
                text = str(msg)
            try:
                self._sink(level, text)
            except Exception:  # noqa: BLE001 - the log must never break the caller
                self.logger.debug("%s: activity log failed", self.room, exc_info=True)
        super().log(level, msg, *args, **kwargs)


def event_text(level: int, text: str) -> str:
    """An event line: warnings and errors say so."""
    if level >= logging.ERROR:
        return f"Error: {text}"
    if level >= logging.WARNING:
        return f"Warning: {text}"
    return text[:1].upper() + text[1:]


@dataclass
class CycleFacts:
    """What one regulation cycle saw and decided (see :func:`describe_cycle`)."""

    path: str
    setpoint_c: float | None = None
    target_label: str = ""          # "Comfort", "Away, nobody home", ...
    room_c: float | None = None
    room_last_c: float | None = None  # the last reading, while the sensor is lost
    trv_c: float | None = None
    trv_heating: bool | None = None
    trv_off: bool = False           # the valve reports off
    trv_scheduled: bool = False     # the valve is back on its Tado schedule
    tado: str = "unknown"           # what the valve has been told: "21.1", "off", ...
    tado_c: float | None = None     # the same as a number (None when off or unknown)
    window: str | None = None       # WINDOW_* while a window matters
    mode: str | None = None
    prev_mode: str | None = None
    resumed: bool = False           # the first decision after a restart or reload
    error_c: float | None = None
    slope_c_per_h: float | None = None
    room_held: bool = False         # a fast fall: the previous reading was used
    full_push: bool = False         # a hold that ends like a heat-up (hold_push_c)
    room_bridged: bool = False      # a short sensor gap: the last reading was used
    command_c: float | None = None  # this cycle's target for the valve
    command_off: bool = False
    p_c: float = 0.0
    i_c: float = 0.0
    saturated: bool = False
    send: str = ""                  # the send decision (climate._last_reason)
    td_s: float | None = None       # the braking time this cycle decided with
    sensor_lost_min: int | None = None
    # The valve look-ahead (lookahead.py): what it took off the hold command,
    # and how fast the valve's reading was falling.
    lookahead_c: float = 0.0
    trv_fall_c_per_h: float | None = None
    # Dosed top-ups (dosing.py): the dosing phase, the pulse and the heat
    # still on its way (the Smith predictor's in-flight rise).
    dose_settled: bool = False
    dose_pulse_s: float | None = None     # how long the running pulse has run
    dose_need_c: float | None = None      # what it still has to cover
    dose_inflight_c: float = 0.0
    dose_fired_by: str | None = None      # "release" / "burn" / "together" (the cycle a pulse starts)
    dose_with: str | None = None          # the other room, when the coordinator chose the moment
    dose_end: str | None = None           # why a pulse ended (the cycle it ends)
    dose_ready_in_s: float | None = None  # until the next pulse is due, while nothing is in flight
    dose_cap_s: float | None = None       # the pulse's cap (regulation.dose_cap_s)


def _signed(value: float, digits: int) -> str:
    """'+0.4', '-0.1', and '+0.0' rather than '-0.0'."""
    value = round(value, digits)
    return f"{value + 0.0:+.{digits}f}" if value != 0 else f"{0:+.{digits}f}"


def _num(value: float | None) -> str:
    return "?" if value is None or not math.isfinite(value) else f"{value:.1f}"


def _gap(error_c: float, digits: int = 1) -> str:
    half = 0.5 * 10.0 ** -digits
    if error_c >= half:
        return f"{error_c:.{digits}f} below"
    if error_c <= -half:
        return f"{-error_c:.{digits}f} above"
    return "on target"


def _vs_target(error_c: float) -> str:
    """'0.2 above the target', 'on the target'."""
    gap = _gap(error_c)
    return "on the target" if gap == "on target" else f"{gap} the target"


def _trend(slope_c_per_h: float | None, cfg: RegulationConfig) -> str:
    if slope_c_per_h is None or not math.isfinite(slope_c_per_h):
        return "trend not known yet"
    if abs(slope_c_per_h) < cfg.slope_deadband_c_per_h:
        return "flat"
    if slope_c_per_h > 0:
        return f"rising {slope_c_per_h:.1f} °C/h"
    return f"falling {-slope_c_per_h:.1f} °C/h"


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.0f} min"


_DOSE_ENDS = {
    "covered": "the dose is on its way",
    "max": "its cap, the most the room's own heating rate allows",
    "landing": "rising onto the target, the leftover heat lands it",
    "stop_rule": "the room rose sooner than expected",
}


def _dose_phrase(f: CycleFacts, e: float, trend: str, cfg: RegulationConfig) -> str:
    """A dosed top-up (dosing.py): a pulse, its end, the landing, or the
    room in the dosing phase waiting for its next pulse."""
    inflight = max(0.0, f.dose_inflight_c)
    need = f.dose_need_c if f.dose_need_c is not None else 0.0
    if f.mode == MODE_DOSE:
        if f.dose_fired_by is not None:
            why = {
                "burn": f"riding the {f.dose_with or 'other room'}'s call for heat",
                "together": f"with the {f.dose_with or 'other room'}'s pulse",
            }.get(f.dose_fired_by, trend)
            cap = (f"; cap about {_minutes(f.dose_cap_s)}"
                   if f.dose_cap_s is not None and math.isfinite(f.dose_cap_s) else "")
            return (f"Top-up pulse: {_gap(e, 2)}, {why}; full power until "
                    f"{need:.2f} is on its way (the cooling until it lands included){cap}")
        ran = (f.dose_pulse_s or 0.0) / 60.0
        return (f"Top-up pulse: {ran:.1f} min at full power, {inflight:.2f} on its way, "
                f"{max(0.0, need):.2f} still to cover")
    if f.dose_end is not None:
        return (f"Top-up ended ({_DOSE_ENDS.get(f.dose_end, f.dose_end)}): valve off, "
                f"{inflight:.2f} on its way")
    if inflight >= cfg.dose.inflight_min_c:
        return f"Valve off, landing: {inflight:.2f} still on its way, {_gap(e, 2)}, {trend}"
    if f.dose_ready_in_s is not None:
        when = ("now" if f.dose_ready_in_s < 60.0
                else f"in about {_minutes(f.dose_ready_in_s)}")
        return f"Valve off (dosing): {_gap(e, 2)}, {trend}; next top-up {when}"
    return (f"Valve off (dosing): {_gap(e, 2)}, {trend}; next top-up once it cools "
            "and the last pulse has landed")


def _phrase(f: CycleFacts, cfg: RegulationConfig) -> str:
    """What the regulator did and why (the regulated path)."""
    e = f.error_c if f.error_c is not None else 0.0
    slope_s = f.slope_c_per_h / 3600.0 if f.slope_c_per_h is not None else None
    o = outlook(e, slope_s, cfg, td_s=f.td_s)
    rise = o.rising_c_per_s * 3600.0
    trend = _trend(f.slope_c_per_h, cfg)
    new = f.prev_mode != f.mode and not f.resumed

    if f.mode == MODE_HEAT:
        if f.prev_mode != MODE_HEAT:      # also after a start: the decision to heat
            return (f"Heat-up: {_gap(e)} ({cfg.heatup_enter_c:.1f} or more heats at "
                    f"full power); {trend}")
        if f.slope_c_per_h is None:
            return (f"Full heat: {_gap(e)}, trend not known yet; stops once the "
                    "leftover heat covers the rest")
        return (f"Full heat: {_gap(e)}, {trend}; stops once the leftover heat covers "
                f"the rest (stopping now would leave {o.landing_c:.1f})")

    if f.mode == MODE_DOSE or (f.mode == MODE_STOP and f.dose_settled):
        return _dose_phrase(f, e, trend, cfg)

    over = (f.setpoint_c or 0.0) + cfg.stop_over_c       # the stop's line
    if f.mode == MODE_COAST:
        if e <= 0 and f.prev_mode != MODE_COAST:
            # The target fell below the room (a lower preset, a window): a
            # heat-up ends as a coast, which the next cycle turns into a stop.
            # A coast that carries on is still short of the target by the
            # regulator's own error (one at or above it is a stop), so a 0.00
            # there is a few thousandths rounded away (28 Oct 06:45, study).
            return f"Heat-up ended: valve shut, {_vs_target(e)}"
        if new and f.prev_mode in (MODE_HEAT, MODE_HOLD):
            # From a hold: a full push ending on the heat-up's exit.
            what = "Coasting" if f.prev_mode == MODE_HEAT else "Full push ended, coasting"
            return (f"{what}: valve shut with {e:.1f} to go; leftover heat predicted "
                    f"to land it ({rise:.1f} °C/h × {_minutes(o.lead_s)})")
        return f"Coasting: valve shut, {e:.1f} to go, still {trend}"

    if f.mode == MODE_STOP:
        if new and f.prev_mode == MODE_COAST:
            return f"Valve off: the coast reached the target, {trend}"
        # Still rising towards the stop's line (the radiator's leftover heat,
        # mostly): the overshoot forecast decided the stop, and keeps it shut
        # whatever the release would say.
        towards_line = o.stop_landing_c <= -cfg.stop_over_c and e > -cfg.stop_over_c
        if new and towards_line:
            return (f"Valve off ahead of an overshoot: {_gap(e)}, {trend} would pass "
                    f"{over:.1f} within {_minutes(o.stop_lead_s)}")
        if towards_line:
            return (f"Valve off: {_gap(e)}, still {trend}; on that slope it would "
                    f"pass {over:.1f} within {_minutes(o.stop_lead_s)}, so it stays off")
        heat_at = (f.setpoint_c or 0.0) - cfg.release_below_c
        if o.falling_c_per_s > 0:
            # The forecast leaves out the slope's deadband (as the release does).
            ahead = (f"{o.release_c:.2f} below" if o.release_c > 0
                     else f"{-o.release_c:.2f} above")
            return (f"Valve off: {_gap(e)}, {trend}; heats again "
                    f"{_minutes(cfg.release_lead_s)} before it reaches {heat_at:.1f} "
                    f"(forecast for {_minutes(cfg.release_lead_s)} ahead: {ahead})")
        return (f"Valve off: {_gap(e)}, {trend}; heats again at {heat_at:.1f}, or "
                f"{_minutes(cfg.release_lead_s)} ahead of it once falling")

    # Hold.
    if f.resumed:
        head = "Holding (resumed)"
    elif f.prev_mode == MODE_STOP:
        if e >= cfg.release_below_c - 0.005:
            head = "Holding (released)"     # the gap follows
        else:
            head = (f"Holding (released: heading {o.release_c:.1f} below within "
                    f"{_minutes(cfg.release_lead_s)})")
    elif f.prev_mode == MODE_COAST:
        head = (f"Holding (the coast stalled {e:.1f} short)" if e > 0
                else "Holding (the coast ended)")
    else:
        head = "Holding"
    if f.full_push:
        head += ", full push"
    if f.command_c is not None and f.command_c >= cfg.max_target_c - 0.05:
        push = f"at full power (command at the {cfg.max_target_c:.0f} limit)"
    elif f.command_c is not None and f.trv_c is not None:
        demand = f.command_c - f.trv_c
        push = (f"pushed {demand:.1f} over its reading" if demand >= 0
                else f"set {-demand:.1f} under its reading")
    else:
        push = f"asked for {_num(f.command_c)}"
    why_not = []
    if abs(e) >= cfg.integral_deadband_c:
        why_not.append(f"I decays outside ±{cfg.integral_deadband_c:.1f}")
    elif f.saturated:
        why_not.append("I frozen at the command limit")
    digits = 1
    if cfg.integral_deadband_c <= e < cfg.heatup_enter_c and not f.full_push:
        why_not.append(f"full heat only from {cfg.heatup_enter_c:.1f} below")
        # 0.76 is not "0.8 below" next to "full heat only from 0.8 below".
        if f"{e:.1f}" == f"{cfg.heatup_enter_c:.1f}":
            digits = 2
    detail = f"P {_signed(f.p_c, 1)}, I {_signed(f.i_c, 2)}"
    if f.lookahead_c >= 0.05:
        # The valve's look-ahead on its falling reading, cancelled.
        fall = (f" falling {f.trv_fall_c_per_h:.1f} °C/h"
                if f.trv_fall_c_per_h is not None else " falling")
        detail += f"; {f.lookahead_c:.1f} off for its reading{fall}"
    if why_not:
        detail += "; " + "; ".join(why_not)
    line = f"{head}: {_gap(e, digits)}, valve {push} ({detail})"
    if f.full_push:
        # It ends on the heat-up's exit (hold_push_c), and says so.
        line += "; stops once the leftover heat covers the rest"
        if f.slope_c_per_h is not None and e > 0:
            line += f" (stopping now would leave {o.landing_c:.1f})"
    return line


def _tado_note(f: CycleFacts, cfg: RegulationConfig) -> str:
    """Why the valve does not have this cycle's command (if it does not)."""
    if f.command_c is None or f.path != PATH_REGULATED:
        return ""
    want = "off" if f.command_off else f"{f.command_c:.1f}"
    told_off = f.tado == "off"
    same = (f.command_off and told_off) or (
        not f.command_off and f.tado_c is not None and abs(f.command_c - f.tado_c) < 0.05
    )
    if f.send.startswith("send_failed"):
        return f" ({want} failed)"
    if same:
        return ""
    if f.send.startswith("rate_limited"):
        return f" ({want} waits: rate limit)"
    if f.send.startswith("settling"):
        return f" ({want} waits: a change by hand settling)"
    if f.send == "window_pending":
        return f" ({want} waits: the window's delay)"
    if f.send == "already_at_target" and not f.command_off and not told_off:
        return f" ({want} wanted: under the {cfg.min_change_threshold_c:.1f} step)"
    return ""


def decision_key(f: CycleFacts, cfg: RegulationConfig) -> str:
    """A decision's shape without the numbers that drift: a change in it is
    a new line at once (ActivityLog.add_decision)."""
    note = _tado_note(f, cfg)
    note_kind = ("failed" if "failed" in note else "waits" if "waits" in note else "")
    missing = ("room" if f.room_c is None else "valve") if f.path == PATH_WAITING else ""
    # An open window or its re-warm wait reads the same whatever the mode.
    mode = "" if f.window in (WINDOW_OPEN, WINDOW_REWARM) else (
        (f.mode or "") + ("+push" if f.full_push else "")
        + ("+landing" if f.dose_settled and f.dose_inflight_c >= cfg.dose.inflight_min_c
           else "+dosing" if f.dose_settled else ""))
    return "|".join((
        f.path, missing, mode, f.window or "", _num(f.setpoint_c), f.target_label,
        f.tado, note_kind, "held" if f.room_held else "", "gap" if f.room_bridged else "",
        "off" if f.trv_off else "auto" if f.trv_scheduled else "",
    ))


def describe_cycle(f: CycleFacts, cfg: RegulationConfig) -> str:
    """The decision line for one cycle."""
    if f.path == PATH_SUMMER:
        phrase = "Summer mode: heating locked off"
    elif f.path == PATH_OFF:
        phrase = "Switched off: valve kept off"
    elif f.path == PATH_NO_DATA:
        phrase = "Cycle skipped: no fresh data from the valve or sensors"
    elif f.path == PATH_WAITING:
        missing = "the room sensor" if f.room_c is None else "the valve's reading"
        phrase = f"Waiting for {missing}: nothing sent"
    elif f.path == PATH_SENSOR_LOST:
        phrase = (f"Room sensor unavailable for {f.sensor_lost_min or 0} min: not "
                  "regulating (full heat is never left on)")
    elif f.window == WINDOW_OPEN:
        phrase = "Window open: valve off until it closes"
    elif f.window == WINDOW_REWARM:
        phrase = "Window closed: valve off while the room re-warms by itself"
    else:
        phrase = _phrase(f, cfg)
        if f.window == WINDOW_PENDING:
            phrase = "Window just opened (valve off after its delay): " + phrase
        elif f.window == WINDOW_IGNORED:
            phrase = "Window open, not acted on until it next opens: " + phrase
    if f.path == PATH_REGULATED:
        if f.room_held:
            phrase += " (a fast fall ignored this cycle)"
        if f.room_bridged:
            phrase += " (room sensor gap: last reading used)"

    if f.path == PATH_SENSOR_LOST:
        room = "room unavailable" + (
            f" (last {_num(f.room_last_c)})" if f.room_last_c is not None else "")
    else:
        room = f"room {_num(f.room_c)}"
    if f.setpoint_c is not None and f.path not in (PATH_SUMMER, PATH_OFF):
        room += f" → {_num(f.setpoint_c)}"
        if f.target_label:
            room += f" {f.target_label}"
    trv = f"TRV {_num(f.trv_c)}" + _trv_state(f)
    tado = f"Tado {f.tado}{_tado_note(f, cfg)}"
    tail = f" | {room} | {trv} | {tado}"
    # The numbers at the end are what a line is read for: a long phrase is
    # shortened, never the tail.
    return clip(phrase, max(40, STATE_MAX_LEN - len(tail))) + tail


def _trv_state(f: CycleFacts) -> str:
    if f.trv_off:
        return " off"
    if f.trv_scheduled:
        return " on its Tado schedule"
    return " heating" if f.trv_heating else ""
