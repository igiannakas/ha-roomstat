"""Preheat on the way home: what each room would aim for while nobody is home.

HA-free, like regulation.py and autotune.py.  The climate entity
(climate_preheat.py) gathers the inputs every regulation cycle and whenever
one of the three Home ETA entities changes, and logs every change of
decision.  In this version the decision is only observed ("shadow"): the
room stays at Away and no command changes.

The rule, per room, while away mode is in charge (nobody home, no change by
hand, no window, heating on):

- **Someone close** (home or within 800 m, Home ETA's "someone close to
  home"): the schedule's temperature, the one coming home would restore.
- **Someone nearby** (within their near-home radius, 3 km here): the
  *30-minute temperature*, the schedule's temperature less what this room
  typically recovers in its first 30 minutes of heating
  (:class:`RecoveryLearner`), rounded down to 0.1 °C.  Back to the target
  in about 30 minutes on a typical day, up to about 45 on a slow one (the
  owner's choice, 25 Oct 2026: aim for 30, not aggressively).
- **Someone heading home** with the expected arrival within the room's
  heat-up time plus a lead: the schedule's temperature (the "due" latch).
  It stops when the trip home ends (no expected arrival for the grace
  time), when the arrival slips more than ``slip_s`` later than when it
  started and the room is no longer due (no waste; it starts again when the
  new arrival is due), or ``cap_s`` after it first started for this trip
  home (slips, restarts and short breaks for a window or a change by hand
  don't re-arm it).
- **Otherwise** Away.
- **Away picked by hand while nobody is home** holds the room cold: no
  preheat of any kind (the HA layer keeps it for the "change while away
  lasts" limit).

A rise applies at once; a drop to a cooler level only once nothing warmer
has been wanted for ``fall_delay_s``, so a phone hovering at a boundary
can't make the target flap.  Close and nearby unknown count as "no": that
level is not used.

The heat-up time is ``dead time + deficit / (rate_factor x rate) + lead``
with the auto-tuner's learned heating rate and dead time.  Checked on 37
recorded heat-ups (18-25 Oct 2026, four rooms): with 0.8, 34 (92 %) would
have started early enough, typically 24 min early; the three late ones were
a coast stall near the target and a heat-up right after window airing, not
the formula.  1.0 started rooms only 4-8 min later and made three more late.
"""
from __future__ import annotations

import datetime
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .parameters import PreheatConfig, RecoveryConfig

# Modes (option ``preheat_mode``).  Shadow works everything out and logs
# it, and leaves the room at Away.
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODES = (MODE_OFF, MODE_SHADOW)

# Decisions (the preheat sensor's states).
STATE_OFF = "off"               # preheat switched off for this room
STATE_INACTIVE = "inactive"     # away mode is not in charge (someone home, ...)
STATE_HELD_COLD = "held_cold"   # Away picked by hand while nobody is home
STATE_WAITING = "waiting"       # nobody close or nearby, nobody due: Away
STATE_NEARBY = "nearby"         # the 30-minute temperature
STATE_CLOSE = "close"           # the schedule's temperature
STATE_DUE = "due"               # heading home, within the heat-up time
STATES = (
    STATE_OFF, STATE_INACTIVE, STATE_HELD_COLD, STATE_WAITING,
    STATE_NEARBY, STATE_CLOSE, STATE_DUE,
)

# Why away mode is not in charge (``PreheatInputs.inactive_reason``).
INACTIVE_SUMMER = "summer"
INACTIVE_OFF = "switched_off"
INACTIVE_NO_PRESENCE = "no_presence_sensor"
INACTIVE_HOME = "someone_home"
INACTIVE_CHANGE_BY_HAND = "change_by_hand"
INACTIVE_WINDOW = "window"
INACTIVE_NOTHING_WARMER = "nothing_warmer"   # the schedule's target <= Away
INACTIVE_NO_INPUTS = "no_home_eta"            # none of the three entities set
INACTIVE_REASONS = {
    INACTIVE_SUMMER: "summer mode is on",
    INACTIVE_OFF: "the room is switched off",
    INACTIVE_NO_PRESENCE: "no presence sensor is set",
    INACTIVE_HOME: "someone is home",
    INACTIVE_CHANGE_BY_HAND: "a change by hand is in charge",
    INACTIVE_WINDOW: "a window is open",
    INACTIVE_NOTHING_WARMER: "coming home would heat no more than Away",
    INACTIVE_NO_INPUTS: "no Home ETA entity is set",
}

# Why a heading-home preheat ended (``PreheatDecision.ended``).
ENDED_LOST = "arrival_lost"
ENDED_SLIPPED = "slipped"
ENDED_CAP = "cap"

# Levels from Home ETA, warmest last.
LEVEL_NONE = 0
LEVEL_NEARBY = 1
LEVEL_CLOSE = 2

_TEMP_EPS = 0.05


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _opt_float(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def floor_tenth(value: float) -> float:
    """Round down to 0.1 °C (19.42 -> 19.4), immune to binary fractions."""
    return math.floor(round(value * 10.0, 6)) / 10.0


def parse_arrival(state: str | None) -> float | None:
    """A timestamp sensor's state as epoch seconds; None when it holds none.

    Home Assistant writes timestamps as ISO 8601 with an offset.  A value
    without one is not a time anyone can act on, so it counts as none.
    """
    if not isinstance(state, str) or state in ("", "unknown", "unavailable"):
        return None
    try:
        when = datetime.datetime.fromisoformat(state)
    except ValueError:
        return None
    if when.tzinfo is None or when.utcoffset() is None:
        return None
    try:
        ts = when.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    return ts if math.isfinite(ts) else None


def parse_on_off(state: str | None) -> bool | None:
    """A binary sensor's state: True on, False off, None anything else."""
    if state == "on":
        return True
    if state == "off":
        return False
    return None


# ---------------------------------------------------------------------------
# The settings (Configure → Preheat timing)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OptionSpec:
    """One preheat setting: its option key, the PreheatConfig field it sets,
    and the option's unit (``scale`` converts it: minutes to seconds)."""

    key: str
    field: str
    scale: float
    minimum: float
    maximum: float
    step: float
    unit: str | None

    @property
    def default(self) -> float:
        """The option's default, in its own unit (PreheatConfig's / scale)."""
        return round(getattr(PreheatConfig(), self.field) / self.scale, 6)


TIMING_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec("preheat_lead_min", "lead_s", 60.0, 0, 60, 1, "min"),
    OptionSpec("preheat_rate_factor", "rate_factor", 1.0, 0.3, 1.5, 0.05, None),
    OptionSpec("preheat_slip_min", "slip_s", 60.0, 5, 120, 5, "min"),
    OptionSpec("preheat_max_min", "cap_s", 60.0, 30, 720, 30, "min"),
    OptionSpec("preheat_drop_delay_min", "fall_delay_s", 60.0, 0, 60, 1, "min"),
    OptionSpec("preheat_arrival_grace_min", "arrival_grace_s", 60.0, 0, 60, 1, "min"),
    OptionSpec("preheat_recovery_default_c", "recovery_default_c", 1.0, 0.1, 2.0, 0.1, "°C"),
    OptionSpec(
        "preheat_fallback_rate_c_per_h", "fallback_rate_c_per_h", 1.0, 0.3, 5.0, 0.1, "°C/h"
    ),
    OptionSpec("preheat_fallback_dead_min", "fallback_dead_time_s", 60.0, 0, 60, 1, "min"),
)


def config_from_options(options: Mapping[str, Any]) -> PreheatConfig:
    """PreheatConfig from the entry options; a missing, non-numeric or
    out-of-range value keeps its default (the options flow enforces the
    ranges; this guards against a hand-edited or older entry)."""
    values: dict[str, float] = {}
    for spec in TIMING_OPTIONS:
        raw = options.get(spec.key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if math.isfinite(value) and spec.minimum <= value <= spec.maximum:
            values[spec.field] = value * spec.scale
    return PreheatConfig(**values)


# ---------------------------------------------------------------------------
# The 30-minute recovery, learned from the room's own heat-ups
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecoveryEpisode:
    """One target rise: how much the room rose in its first 30 minutes."""

    ts: float          # when the target rose
    target_c: float
    deficit_c: float   # how far below the new target the room was then
    rise_c: float      # the room's rise over the first ``window_s``

    def as_dict(self) -> dict[str, float]:
        return {
            "ts": self.ts, "target_c": self.target_c,
            "deficit_c": self.deficit_c, "rise_c": self.rise_c,
        }

    @classmethod
    def from_dict(cls, data: Any) -> RecoveryEpisode | None:
        if not isinstance(data, Mapping):
            return None
        values = [data.get(k) for k in ("ts", "target_c", "deficit_c", "rise_c")]
        if not all(_finite(v) for v in values):
            return None
        return cls(*(float(v) for v in values))


@dataclass
class _Running:
    start_ts: float
    room0_c: float
    target_c: float
    deficit_c: float
    last_ts: float
    last_room_c: float

    def as_dict(self) -> dict[str, float]:
        return {
            "start_ts": self.start_ts, "room0_c": self.room0_c,
            "target_c": self.target_c, "deficit_c": self.deficit_c,
            "last_ts": self.last_ts, "last_room_c": self.last_room_c,
        }

    @classmethod
    def from_dict(cls, data: Any) -> _Running | None:
        if not isinstance(data, Mapping):
            return None
        keys = ("start_ts", "room0_c", "target_c", "deficit_c", "last_ts", "last_room_c")
        values = [data.get(k) for k in keys]
        if not all(_finite(v) for v in values):
            return None
        running = cls(*(float(v) for v in values))
        if running.last_ts < running.start_ts:
            return None
        return running


class RecoveryLearner:
    """How much the room recovers in the first 30 minutes after its target
    rises: the median of the last ``history_len`` episodes.

    An episode starts on a target rise of ``min_step_c`` or more that
    leaves the room ``min_deficit_c`` or more below the new target, not
    within ``window_settle_s`` of a window (the room then re-warms on its
    own).  One in which the room comes within ``reach_c`` of the target by
    its 30 minutes is dropped: its rise is capped by the deficit and would
    understate the room.  It is heat or hold, whichever the
    regulator chooses: from a 30-minute temperature the room is 0.4-0.8 °C
    short, either side of the 0.8 °C heat-up threshold.  It is also dropped
    when the target changes before the 30 minutes are up, a window opens, a
    cycle is not usable (no fresh room reading, Boost, the room switched
    off, summer mode) or the cycles stop for more than ``max_gap_s``.  Measured on the owner's rooms
    (18-25 Oct 2026, the 06:00 steps, a return home, a Comfort press): the
    living room 0.60-0.95 °C (median 0.77), the master bedroom 0.30-0.57
    (0.43), the second bedroom 0.50-0.56 (0.54), the study 0.43-0.65
    (0.58).
    """

    VERSION = 1

    def __init__(self, cfg: RecoveryConfig | None = None) -> None:
        self.cfg = cfg or RecoveryConfig()
        self._episodes: list[RecoveryEpisode] = []
        self._running: _Running | None = None
        self._prev_target_c: float | None = None
        self._prev_ts: float | None = None
        self._last_window_ts: float | None = None

    # -- results -------------------------------------------------------------
    @property
    def episodes(self) -> list[RecoveryEpisode]:
        """The kept episodes, oldest first."""
        return list(self._episodes)

    @property
    def running(self) -> bool:
        """An episode is being measured."""
        return self._running is not None

    @property
    def recovery_c(self) -> float | None:
        """The typical 30-minute rise; None until ``min_episodes`` are known."""
        rises = sorted(e.rise_c for e in self._episodes)
        if len(rises) < max(1, self.cfg.min_episodes):
            return None
        mid = len(rises) // 2
        if len(rises) % 2:
            return rises[mid]
        return (rises[mid - 1] + rises[mid]) / 2.0

    # -- input ---------------------------------------------------------------
    def observe(
        self,
        ts: float,
        target_c: float | None,
        room_c: float | None,
        *,
        usable: bool,
        window: bool,
    ) -> RecoveryEpisode | None:
        """One regulated cycle; returns the episode it completed, if any.

        ``usable``: heating on, a fresh room reading, no Boost.  ``window``:
        a window is open or in its close delay.
        """
        if not _finite(ts):
            return None
        cfg = self.cfg
        prev_ts = self._prev_ts
        self._prev_ts = ts
        if prev_ts is not None and (ts < prev_ts or ts - prev_ts > cfg.max_gap_s):
            # A pause (restart, a valve that dropped off) or the clock went
            # back: what was being measured is lost, and so is the step.
            self._running = None
            self._prev_target_c = None
        if window:
            self._last_window_ts = ts
            self._running = None
            self._prev_target_c = None
            return None
        if not usable or not _finite(target_c) or not _finite(room_c):
            self._running = None
            self._prev_target_c = None
            return None
        target = float(target_c)
        room = float(room_c)
        done: RecoveryEpisode | None = None
        run = self._running
        if run is not None:
            if abs(target - run.target_c) > _TEMP_EPS:
                self._running = None              # cut short: no 30 minutes
            elif ts - run.start_ts >= cfg.window_s:
                end_ts = run.start_ts + cfg.window_s
                span = ts - run.last_ts
                if span > 0 and run.last_ts < end_ts:
                    frac = (end_ts - run.last_ts) / span
                    room_end = run.last_room_c + frac * (room - run.last_room_c)
                else:
                    room_end = room
                self._running = None
                if room_end < run.target_c - cfg.reach_c:
                    done = RecoveryEpisode(
                        ts=run.start_ts, target_c=run.target_c,
                        deficit_c=run.deficit_c, rise_c=room_end - run.room0_c,
                    )
                    self._episodes.append(done)
                    del self._episodes[: max(0, len(self._episodes) - cfg.history_len)]
            elif room >= run.target_c - cfg.reach_c:
                self._running = None              # there before 30 minutes
            else:
                run.last_ts, run.last_room_c = ts, room
        prev = self._prev_target_c
        self._prev_target_c = target
        if (
            self._running is None
            and done is None
            and prev is not None
            and target - prev >= cfg.min_step_c - 1e-9
            and target - room >= cfg.min_deficit_c - 1e-9
            and (
                self._last_window_ts is None
                or ts - self._last_window_ts >= cfg.window_settle_s
            )
        ):
            self._running = _Running(ts, room, target, target - room, ts, room)
        return done

    # -- persistence -----------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "episodes": [e.as_dict() for e in self._episodes],
            "running": self._running.as_dict() if self._running is not None else None,
            "prev_target_c": self._prev_target_c,
            "prev_ts": self._prev_ts,
            "last_window_ts": self._last_window_ts,
        }

    def restore(self, data: Any, now: float) -> bool:
        """Take back a saved state; anything malformed or from the future is
        dropped (the clock went back).  The gap rule then decides whether a
        running episode still joins up with the next cycle."""
        if not isinstance(data, Mapping) or data.get("version") != self.VERSION:
            return False
        episodes = []
        raw_episodes = data.get("episodes")
        for raw in raw_episodes if isinstance(raw_episodes, list) else []:
            episode = RecoveryEpisode.from_dict(raw)
            if episode is not None and episode.ts <= now:
                episodes.append(episode)
        self._episodes = episodes[-self.cfg.history_len:]

        def past(value: Any) -> float | None:
            v = _opt_float(value)
            return v if v is not None and v <= now else None

        running = _Running.from_dict(data.get("running"))
        self._running = running if running is not None and running.last_ts <= now else None
        self._prev_target_c = _opt_float(data.get("prev_target_c"))
        self._prev_ts = past(data.get("prev_ts"))
        self._last_window_ts = past(data.get("last_window_ts"))
        if self._prev_ts is None:
            self._running = None
            self._prev_target_c = None
        return True


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PreheatInputs:
    """What one evaluation sees."""

    now: float
    enabled: bool
    # Why away mode is not in charge (an INACTIVE_* code), None when it is.
    inactive_reason: str | None
    # The target coming home would restore right now (the schedule's
    # preset), None when unknown.
    scheduled_c: float | None
    away_c: float
    room_c: float | None
    # Home ETA: the first expected arrival (epoch seconds), None when nobody
    # is heading home or it is unknown.
    arrival_ts: float | None
    close: bool | None
    nearby: bool | None
    # Away picked by hand while nobody is home, within its limit.
    held_cold: bool = False
    # The auto-tuner's learned heating rate and dead time, None if unknown.
    rate_c_per_h: float | None = None
    dead_time_s: float | None = None
    # The learned 30-minute recovery, None until learned.
    recovery_c: float | None = None


@dataclass(frozen=True)
class PreheatDecision:
    """What the room would aim for, and why (see the module docstring)."""

    state: str
    # The temperature preheat would aim for; None while off or inactive.
    target_c: float | None = None
    inactive_reason: str | None = None
    scheduled_c: float | None = None
    away_c: float | None = None
    room_c: float | None = None
    level: int = LEVEL_NONE
    # A drop to a cooler level is waiting for its delay until then.
    level_drop_ts: float | None = None
    # The 30-minute temperature, and the recovery it was worked out from.
    recovery_temp_c: float | None = None
    recovery_c: float | None = None
    recovery_learned: bool = False
    # The heading-home start: the heat-up time it needs, when it is due for
    # the expected arrival, and since when it has been heating.
    needed_s: float | None = None
    due_ts: float | None = None
    arrival_ts: float | None = None
    arrival_ignored: bool = False
    since_ts: float | None = None
    started_arrival_ts: float | None = None
    rate_c_per_h: float | None = None
    rate_learned: bool = False
    dead_time_s: float | None = None
    dead_time_learned: bool = False
    # A heading-home preheat that ended at this evaluation (ENDED_*), with
    # the arrival it had started for.
    ended: str | None = None
    ended_arrival_ts: float | None = None
    # This trip home has had its longest preheat: no new start until it ends.
    capped: bool = False

    @property
    def key(self) -> tuple[str, float | None, str | None]:
        """What a change of decision is judged on: the state, the target, and
        while not in charge why not."""
        return (
            self.state,
            None if self.target_c is None else round(self.target_c, 1),
            self.inactive_reason if self.state == STATE_INACTIVE else None,
        )


# Not in charge for these reasons ends the trip home too: someone is home,
# or preheat has nothing to go on.  Any other (a window, a change by hand, the
# room switched off, summer, an Away held cold) only stands preheat aside:
# the trip's start and its cap carry on, so a short break can't re-arm the
# cap.
_ENDS_THE_TRIP = frozenset({INACTIVE_HOME, INACTIVE_NO_PRESENCE, INACTIVE_NO_INPUTS})


class PreheatController:
    """Decides, per evaluation, what the room would aim for (HA-free).

    Keeps the heading-home latch (since, the arrival it started for), the
    trip home (its first start, whether it hit the cap, how long its expected
    arrival has been missing) and the levels wanted over the last
    ``fall_delay_s``.
    """

    VERSION = 1

    def __init__(self, cfg: PreheatConfig | None = None) -> None:
        self.cfg = cfg or PreheatConfig()
        self._reset()

    def _reset(self) -> None:
        """Forget everything: preheat is off, someone is home."""
        self._stand_aside()
        self._trip_since: float | None = None
        self._capped = False
        self._arrival_missing_since: float | None = None

    def _stand_aside(self) -> None:
        """Not in charge for now: no latch, no level; the trip carries on."""
        # (ts, level wanted from then on), oldest first, back to the one in
        # force ``fall_delay_s`` ago.
        self._wants: list[tuple[float, int]] = []
        self._since: float | None = None
        self._since_arrival: float | None = None

    @property
    def heating_since(self) -> float | None:
        """When the heading-home preheat in progress started (None: none)."""
        return self._since

    # -- the trip home -------------------------------------------------------
    def _track_arrival(self, arrival_ts: Any, now: float) -> tuple[float | None, bool, bool]:
        """The arrival to use (one in the past counts as now; one further
        ahead than anyone heading home would be is a fault, ignored), whether
        it was ignored, and whether the trip home is over (no arrival for
        ``arrival_grace_s``; its start and cap are forgotten then)."""
        cfg = self.cfg
        arrival = _opt_float(arrival_ts)
        ignored = False
        if arrival is not None and arrival - now > cfg.max_arrival_ahead_s:
            arrival, ignored = None, True
        if arrival is not None:
            arrival = max(arrival, now)
            self._arrival_missing_since = None
            lost = False
        else:
            if self._arrival_missing_since is None or self._arrival_missing_since > now:
                self._arrival_missing_since = now
            lost = now - self._arrival_missing_since >= cfg.arrival_grace_s
        if self._trip_since is not None and self._trip_since > now:
            self._trip_since = now                # the clock went back
        if lost:
            self._trip_since = None
            self._capped = False
        return arrival, ignored, lost

    # -- the levels ----------------------------------------------------------
    def _level_now(self, want: int, now: float) -> tuple[int, float | None]:
        """The warmest level wanted at any time in the last ``fall_delay_s``
        (a rise at once, a drop only once nothing warmer was wanted for the
        whole delay), and when it would drop if nothing changes."""
        delay = self.cfg.fall_delay_s
        wants = self._wants
        if wants and wants[-1][0] > now:
            wants.clear()                         # the clock went back
        if not wants or wants[-1][1] != want:
            wants.append((now, want))
        # A level wanted until longer than the delay ago no longer counts.
        while len(wants) >= 2 and wants[1][0] <= now - delay:
            wants.pop(0)
        level = max(w for _, w in wants)
        drop_ts = None
        if level > want:
            ended = max(wants[i + 1][0] for i in range(len(wants) - 1) if wants[i][1] >= level)
            drop_ts = ended + delay
        return level, drop_ts

    # -- the decision ----------------------------------------------------------
    def evaluate(self, inp: PreheatInputs) -> PreheatDecision:
        cfg = self.cfg
        now = inp.now
        if not inp.enabled:
            self._reset()
            return PreheatDecision(STATE_OFF)
        scheduled = _opt_float(inp.scheduled_c)
        away = float(inp.away_c)
        room = _opt_float(inp.room_c)
        reason = inp.inactive_reason
        if reason in _ENDS_THE_TRIP:
            self._reset()
            return PreheatDecision(
                STATE_INACTIVE, inactive_reason=reason, scheduled_c=scheduled, away_c=away,
            )
        arrival, ignored, lost = self._track_arrival(inp.arrival_ts, now)
        if reason is None and (scheduled is None or scheduled <= away + _TEMP_EPS):
            reason = INACTIVE_NOTHING_WARMER
        if reason is not None or scheduled is None:
            self._stand_aside()
            return PreheatDecision(
                STATE_INACTIVE, inactive_reason=reason, scheduled_c=scheduled, away_c=away,
            )
        if inp.held_cold:
            self._stand_aside()
            return PreheatDecision(
                STATE_HELD_COLD, target_c=away, scheduled_c=scheduled,
                away_c=away, room_c=room,
            )

        # The heat-up time from the room's temperature now.
        rate_learned = _finite(inp.rate_c_per_h) and inp.rate_c_per_h > 0
        rate = float(inp.rate_c_per_h) if rate_learned else cfg.fallback_rate_c_per_h
        dead_learned = _finite(inp.dead_time_s) and inp.dead_time_s >= 0
        dead = float(inp.dead_time_s) if dead_learned else cfg.fallback_dead_time_s
        needed = None
        if room is not None:
            deficit = max(0.0, scheduled - room)
            needed = dead + deficit / (cfg.rate_factor * rate) * 3600.0 + cfg.lead_s
        due_ts = arrival - needed if arrival is not None and needed is not None else None
        due_now = due_ts is not None and now >= due_ts

        ended = None
        ended_arrival = None
        if self._since is not None and self._since > now:
            self._since = now                     # the clock went back
        if self._since is not None:
            if lost:
                ended = ENDED_LOST
            elif self._trip_since is not None and now - self._trip_since >= cfg.cap_s:
                ended = ENDED_CAP
                self._capped = True
            elif (
                arrival is not None
                and self._since_arrival is not None
                and arrival > self._since_arrival + cfg.slip_s
                and not due_now
            ):
                ended = ENDED_SLIPPED
            if ended is not None:
                ended_arrival = self._since_arrival
                self._since = None
                self._since_arrival = None
        if self._since is None and due_now and not self._trip_over_cap(now):
            self._since = now
            self._since_arrival = arrival
            if self._trip_since is None:
                self._trip_since = now

        want = (
            LEVEL_CLOSE if inp.close is True
            else LEVEL_NEARBY if inp.nearby is True
            else LEVEL_NONE
        )
        level, drop_ts = self._level_now(want, now)

        rec_learned = _finite(inp.recovery_c)
        rec = float(inp.recovery_c) if rec_learned else cfg.recovery_default_c
        t30 = min(scheduled, max(away, floor_tenth(scheduled - max(0.0, rec))))

        if self._since is not None:
            state, target = STATE_DUE, scheduled
        elif level == LEVEL_CLOSE:
            state, target = STATE_CLOSE, scheduled
        elif level == LEVEL_NEARBY:
            state, target = STATE_NEARBY, t30
        else:
            state, target = STATE_WAITING, away
        return PreheatDecision(
            state,
            target_c=target,
            scheduled_c=scheduled,
            away_c=away,
            room_c=room,
            level=level,
            level_drop_ts=drop_ts,
            recovery_temp_c=t30,
            recovery_c=rec,
            recovery_learned=rec_learned,
            needed_s=needed,
            due_ts=due_ts,
            arrival_ts=arrival,
            arrival_ignored=ignored,
            since_ts=self._since,
            started_arrival_ts=self._since_arrival,
            rate_c_per_h=rate,
            rate_learned=rate_learned,
            dead_time_s=dead,
            dead_time_learned=dead_learned,
            ended=ended,
            ended_arrival_ts=ended_arrival,
            capped=self._trip_over_cap(now),
        )

    def _trip_over_cap(self, now: float) -> bool:
        return self._capped or (
            self._trip_since is not None and now - self._trip_since >= self.cfg.cap_s
        )

    # -- persistence -----------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "wants": [[ts, w] for ts, w in self._wants],
            "since": self._since,
            "since_arrival": self._since_arrival,
            "trip_since": self._trip_since,
            "capped": self._capped,
            "arrival_missing_since": self._arrival_missing_since,
        }

    def restore(self, data: Any, now: float) -> bool:
        """Take back a saved state (a restart, or a reload, which every
        preset temperature change causes).  Times from the future (the clock
        went back) count as now; anything malformed starts afresh."""
        self._reset()
        if not isinstance(data, Mapping) or data.get("version") != self.VERSION:
            return False

        def when(key: str) -> float | None:
            v = _opt_float(data.get(key))
            return None if v is None else min(v, now)

        wants: list[tuple[float, int]] = []
        raw = data.get("wants")
        for item in raw if isinstance(raw, list) else []:
            if (
                isinstance(item, (list, tuple)) and len(item) == 2
                and _finite(item[0]) and item[1] in (LEVEL_NONE, LEVEL_NEARBY, LEVEL_CLOSE)
            ):
                wants.append((min(float(item[0]), now), int(item[1])))
        wants.sort(key=lambda pair: pair[0])
        self._wants = wants
        self._since = when("since")
        self._since_arrival = _opt_float(data.get("since_arrival"))
        if self._since is None or self._since_arrival is None:
            self._since = self._since_arrival = None
        self._trip_since = when("trip_since")
        self._capped = data.get("capped") is True
        self._arrival_missing_since = when("arrival_missing_since")
        return True


# ---------------------------------------------------------------------------
# Words for the log
# ---------------------------------------------------------------------------

def _min(seconds: float) -> int:
    return int(round(seconds / 60.0))


def describe(
    decision: PreheatDecision,
    previous: PreheatDecision | None,
    cfg: PreheatConfig,
    hhmm: Callable[[float], str],
    *,
    held_cold_until: float | None = None,
) -> str | None:
    """The log line for a change of decision (None: nothing worth a line).

    ``previous``: the decision last logged.  Coming from off or inactive to
    inactive (another reason) is not worth a line.
    """
    d = decision
    state = d.state
    prev_state = previous.state if previous is not None else None
    if state == STATE_OFF:
        return None if prev_state in (None, STATE_OFF) else "switched off"
    if state == STATE_INACTIVE:
        if prev_state in (None, STATE_OFF, STATE_INACTIVE):
            return None
        why = INACTIVE_REASONS.get(d.inactive_reason or "", d.inactive_reason or "")
        return f"stands aside: {why}"

    parts: list[str] = []
    if d.ended == ENDED_SLIPPED and d.arrival_ts is not None:
        late = (
            _min(d.arrival_ts - d.ended_arrival_ts)
            if d.ended_arrival_ts is not None else None
        )
        parts.append(
            f"the expected arrival slipped to {hhmm(d.arrival_ts)}"
            + (f" ({late} min later than when it started)" if late is not None else "")
            + ", so it would stop heating"
        )
    elif d.ended == ENDED_LOST:
        parts.append(
            f"the trip home is over (no expected arrival for "
            f"{_min(cfg.arrival_grace_s)} min), so it would stop heating"
        )
    elif d.ended == ENDED_CAP:
        parts.append(
            f"it would stop: {_min(cfg.cap_s)} min since it first started for "
            f"this trip home"
        )

    target = d.target_c if d.target_c is not None else 0.0
    if state == STATE_HELD_COLD:
        until = (
            f", Resume schedule or {hhmm(held_cold_until)}"
            if held_cold_until is not None else " or Resume schedule"
        )
        parts.append(
            f"Away picked by hand: the room stays at Away {target:.1f} °C with "
            f"no preheat until someone is home{until}"
        )
    elif state == STATE_DUE:
        parts.append(_due_text(d, cfg, hhmm))
    elif state == STATE_CLOSE:
        parts.append(
            f"someone is close to home: it would heat to {target:.1f} °C, the "
            f"temperature coming home restores"
        )
    elif state == STATE_NEARBY:
        rec = d.recovery_c if d.recovery_c is not None else 0.0
        learned = (
            "what it recovers in 30 min" if d.recovery_learned
            else "the default until it has learned what it recovers in 30 min"
        )
        parts.append(
            f"someone is nearby: it would heat to {target:.1f} °C "
            f"({_c(d.scheduled_c)} less {rec:.2f} °C, {learned})"
        )
    else:  # STATE_WAITING
        if prev_state in (STATE_NEARBY, STATE_CLOSE) and d.ended is None:
            parts.append(f"nobody is nearby any more: back to Away {target:.1f} °C")
        elif prev_state in (STATE_NEARBY, STATE_CLOSE, STATE_DUE):
            parts.append(f"back to Away {target:.1f} °C")
        else:
            parts.append(
                f"nobody home: the room stays at Away {target:.1f} °C until "
                f"someone is nearby or heading home"
            )
        # Waiting with a due time means it is still to come, unless the trip
        # has had its longest preheat (then it won't start again).
        if d.arrival_ts is not None and d.due_ts is not None and not d.capped:
            parts.append(
                f"for the expected arrival at {hhmm(d.arrival_ts)} it would start "
                f"at {hhmm(d.due_ts)}"
            )
    if d.arrival_ignored:
        parts.append(
            f"an expected arrival more than {_min(cfg.max_arrival_ahead_s) // 60} h "
            f"ahead is ignored"
        )
    return "; ".join(parts)


def _c(value: float | None) -> str:
    return "?" if value is None else f"{value:.1f}"


def _due_text(d: PreheatDecision, cfg: PreheatConfig, hhmm: Callable[[float], str]) -> str:
    """'heading home: it would heat to 20.0 °C now for the expected arrival at
    18:42: from 17.4 °C it needs 106 min (start-up 22 min, 2.6 °C at 0.8 ×
    2.5 °C/h, lead 5 min)' (the living room on 23 Oct 2026)."""
    head = f"heading home: it would heat to {_c(d.target_c)} °C now"
    if d.arrival_ts is not None:
        head += f" for the expected arrival at {hhmm(d.arrival_ts)}"
    if d.needed_s is None or d.room_c is None or d.scheduled_c is None:
        return head
    deficit = max(0.0, d.scheduled_c - d.room_c)
    rate = d.rate_c_per_h if d.rate_c_per_h is not None else cfg.fallback_rate_c_per_h
    dead = d.dead_time_s if d.dead_time_s is not None else cfg.fallback_dead_time_s
    how = (
        f"start-up {_min(dead)} min{'' if d.dead_time_learned else ' (default)'}, "
        f"{deficit:.1f} °C at {cfg.rate_factor:g} × {rate:.1f} °C/h"
        f"{'' if d.rate_learned else ' (default)'}, lead {_min(cfg.lead_s)} min"
    )
    return f"{head}: from {d.room_c:.1f} °C it needs {_min(d.needed_s)} min ({how})"


def decision_attributes(
    d: PreheatDecision, iso: Callable[[float | None], str | None]
) -> dict[str, Any]:
    """The decision as plain attributes (the preheat sensor, diagnostics)."""
    return {
        "target_c": d.target_c,
        "inactive_reason": d.inactive_reason,
        "scheduled_c": d.scheduled_c,
        "away_c": d.away_c,
        "room_c": None if d.room_c is None else round(d.room_c, 2),
        "level": ("none", "nearby", "close")[d.level],
        "recovery_temp_c": d.recovery_temp_c,
        "recovery_c": None if d.recovery_c is None else round(d.recovery_c, 2),
        "recovery_learned": d.recovery_learned,
        "needed_min": None if d.needed_s is None else round(d.needed_s / 60.0, 1),
        "due_at": iso(d.due_ts),
        "arrival_at": iso(d.arrival_ts),
        "since": iso(d.since_ts),
        "started_for_arrival_at": iso(d.started_arrival_ts),
        "rate_c_per_h": None if d.rate_c_per_h is None else round(d.rate_c_per_h, 2),
        "rate_learned": d.rate_learned,
        "dead_time_min": None if d.dead_time_s is None else round(d.dead_time_s / 60.0, 1),
        "dead_time_learned": d.dead_time_learned,
        "ended": d.ended,
    }


@dataclass
class LoggedDecision:
    """The decision last logged, with when it was made: the preheat sensor's
    state and attributes (they change only with a new decision, so the
    recorder keeps one row per decision), kept across a reload."""

    decision: PreheatDecision
    ts: float
    attributes: dict[str, Any] = field(default_factory=dict)

    VERSION = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "state": self.decision.state,
            "target_c": self.decision.target_c,
            "ts": self.ts,
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: Any) -> LoggedDecision | None:
        if not isinstance(data, Mapping) or data.get("version") != cls.VERSION:
            return None
        state = data.get("state")
        ts = _opt_float(data.get("ts"))
        if state not in STATES or ts is None:
            return None
        attrs = data.get("attributes")
        return cls(
            PreheatDecision(state, target_c=_opt_float(data.get("target_c"))),
            ts,
            dict(attrs) if isinstance(attrs, Mapping) else {},
        )
