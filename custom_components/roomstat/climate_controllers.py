"""Internal state-machine controllers for RoomstatClimate.

These classes hold their own state and can be tested independently of Home
Assistant.  They only import HA helpers lazily (inside methods that schedule
timers), so a plain ``import`` in tests works without an HA bootstrap.

Architecture
------------
- ``WindowAutomationController``  – window-open/close delays & state
- ``PresenceAutomationController`` – presence-away delay & state
- ``SavedState``                  – lightweight snapshot dataclass
- ``PersistedAutomationState``    – automation snapshot that survives an HA
  restart or config-entry reload (stored via RestoreEntity extra data)
- ``normalize_restored_preset`` / ``resolve_boost_restore`` /
  ``window_startup_action`` / ``presence_startup_action`` – pure startup
  decisions used by ``async_added_to_hass``
- ``resolve_summer_state`` / ``summer_enforcement_needed`` – summer-mode lock
- ``TrvReading`` / ``read_trv`` / ``choose_trv`` / ``command_limits`` – read
  the configured TRV, or a fallback entity for the same room while it is
  unavailable, and keep commands inside the range it accepts right now
- ``trv_setpoint_drifted`` / ``CommandPacer`` – re-send a command the TRV did
  not take, and keep commands from all rooms apart
- ``plausible_c`` / ``integration_step`` – guards against faulty readings and
  pauses
- ``entity_problem`` / ``MissingEntityWatch`` / ``replace_entity_setting`` –
  notice a configured entity that was deleted, renamed or disabled (or a room
  sensor that stays unavailable), and replace it (the repairs in repairs.py)
- ``ScheduleController`` / ``parse_schedule_preset`` – follow an external
  schedule helper, with manual overrides that end at the next schedule change
  or after a configurable time (whichever comes first)
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Type alias for the cancel-callback returned by async_call_later
_CancelFn = Callable[[], None]
# Signature of async_call_later (or test stub)
_CallLaterFn = Callable[[Any, float, Callable], _CancelFn]

# Preset and state names used by the pure helpers in this module.  They mirror
# Home Assistant's PRESET_* values and const.PRESET_FROST_PROTECTION, which
# cannot be imported here without pulling in Home Assistant.
PRESET_COMFORT_NAME = "comfort"
PRESET_BOOST_NAME = "boost"
PRESET_FROST_NAME = "frost_protection"
PRESET_ECO_NAME = "eco"
PRESET_AWAY_NAME = "away"
_UNAVAILABLE_STATES = ("unavailable", "unknown")


@dataclass
class SavedState:
    """Snapshot of preset and temperature for later restoration."""

    preset: str | None = None
    temp: float | None = None


# ---------------------------------------------------------------------------
# Window automation
# ---------------------------------------------------------------------------

class WindowAutomationController:
    """Manages window-sensor automation with configurable open/close delays.

    State transitions::

        idle ──(open)──► open_pending ──(delay)──► active
                ▲                                      │
                │        (close)                       │
                └───────── close_pending ◄─────────────┘
                                │ (delay expires)
                                ▼
                              idle
    """

    def __init__(self) -> None:
        self.is_active: bool = False
        self._open_timer: _CancelFn | None = None
        self._close_timer: _CancelFn | None = None
        self._saved: SavedState = SavedState()

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    @property
    def close_delay_active(self) -> bool:
        """True while the post-close restoration timer is running."""
        return self._close_timer is not None

    def get_saved(self) -> SavedState:
        """Return a copy of the currently saved preset/temperature."""
        return SavedState(preset=self._saved.preset, temp=self._saved.temp)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def handle_window_opened(
        self,
        hass: Any,
        delay_s: float,
        on_open_action: Callable,
        *,
        call_later: _CallLaterFn | None = None,
    ) -> None:
        """React to a window-opened event.

        Special case: if a close-delay timer is running (window reopened
        during the restore countdown), cancel that timer and stay in frost
        protection without restarting the open-delay countdown.
        """
        if self._close_timer is not None:
            self._close_timer()
            self._close_timer = None
            _LOGGER.debug("Window reopened during close delay – staying in frost protection")
            return

        # Already in window mode (e.g. the sensor went on → unavailable → on,
        # or only its attributes changed while "on"): do not arm a second open
        # action.  It would snapshot the current frost-protection preset as the
        # "previous" preset and the real pre-open preset would be lost.
        if self.is_active:
            _LOGGER.debug("Window open event ignored – window mode already active")
            return

        # Cancel any previously pending open timer before scheduling a new one
        if self._open_timer is not None:
            self._open_timer()
        _cl = call_later or _get_call_later()
        self._open_timer = _cl(hass, delay_s, on_open_action)
        _LOGGER.debug("Window opened – frost-protection action in %ds", delay_s)

    def handle_window_closed(
        self,
        hass: Any,
        close_delay_s: float,
        on_timer_expire: Callable,
        *,
        call_later: _CallLaterFn | None = None,
    ) -> bool:
        """React to a window-closed event.

        Returns ``True`` when the caller should trigger an *immediate* restore
        (``close_delay_s == 0`` and the window-open mode was active).
        Returns ``False`` in all other cases (timer scheduled, nothing to do).
        """
        if self._open_timer is not None:
            self._open_timer()
            self._open_timer = None
        if self._close_timer is not None:
            self._close_timer()
            self._close_timer = None

        if not self.is_active:
            # Window closed before the open-delay fired – nothing to restore.
            return False

        if close_delay_s > 0:
            _cl = call_later or _get_call_later()
            self._close_timer = _cl(hass, close_delay_s, on_timer_expire)
            _LOGGER.debug("Window closed – restoring previous preset in %ds", close_delay_s)
            return False

        # Zero delay: caller should restore immediately (synchronously)
        return True

    # ------------------------------------------------------------------
    # State mutations called by the climate entity
    # ------------------------------------------------------------------

    def activate(self, preset: str, temp: float | None) -> None:
        """Record the pre-open state and mark window automation as active."""
        self._open_timer = None
        self._saved = SavedState(preset=preset, temp=temp)
        self.is_active = True

    def restore(self) -> SavedState:
        """Clear active state and return the saved preset/temp for restoration."""
        saved = SavedState(preset=self._saved.preset, temp=self._saved.temp)
        self._close_timer = None
        self._saved = SavedState()
        self.is_active = False
        return saved

    def update_saved(self, preset: str, temp: float | None) -> None:
        """Update the saved preset/temp without changing active state.

        Used when the user changes preset while frost protection is active
        so that the new preset is restored when the window closes.
        """
        self._saved = SavedState(preset=preset, temp=temp)

    def cancel_timers(self) -> None:
        """Cancel pending timers but keep the active flag and saved state.

        Used on entity removal so the automation snapshot can still be
        persisted and re-armed after a restart or reload.
        """
        if self._open_timer:
            self._open_timer()
            self._open_timer = None
        if self._close_timer:
            self._close_timer()
            self._close_timer = None

    def cancel_all(self) -> None:
        """Cancel all timers and reset to idle state (e.g. user override)."""
        self.cancel_timers()
        self.is_active = False
        self._saved = SavedState()


# ---------------------------------------------------------------------------
# Presence automation
# ---------------------------------------------------------------------------

class PresenceAutomationController:
    """Manages presence-sensor automation with configurable away/home delays.

    State transitions::

        home ──(away)──► away_pending ──(delay)──► active ──(home)──► home_pending ──(delay)──► home
          ▲                                           │  (away cancels home timer)                 │
          └───────────────────────────────────────────┘                                            │
          ▲                                                                                       │
          └───────────────────────────────────────────────────────────────────────────────────────┘
    """

    def __init__(self) -> None:
        self.is_active: bool = False
        self._away_timer: _CancelFn | None = None
        self._home_timer: _CancelFn | None = None
        self._saved: SavedState = SavedState()

    def handle_presence_away(
        self,
        hass: Any,
        delay_s: float,
        on_away_action: Callable,
        *,
        call_later: _CallLaterFn | None = None,
    ) -> None:
        """React to a presence-away event; schedule the away action.

        If a home-delay timer is pending (presence flickered Home then back to
        Away), cancel it so that the restore never fires and away stays active.

        If already active (rooms already in AWAY mode), only cancel the home
        timer – do **not** schedule a new away timer, because that would
        overwrite the saved pre-away state with the current AWAY preset.
        """
        # Cancel any pending home-delay timer (flicker protection)
        if self._home_timer is not None:
            self._home_timer()
            self._home_timer = None
            _LOGGER.debug("Presence away during home delay – cancelled restore, staying away")

        # Already in away mode: nothing more to do.  Starting a new away
        # timer would cause _async_presence_away_action to overwrite the
        # saved preset with PRESET_AWAY, destroying the original state.
        if self.is_active:
            _LOGGER.debug("Presence away event ignored – already active")
            return

        if self._away_timer is not None:
            self._away_timer()
        _cl = call_later or _get_call_later()
        self._away_timer = _cl(hass, delay_s, on_away_action)
        _LOGGER.debug("Presence away – switching to AWAY preset in %ds", delay_s)

    def handle_presence_home(
        self,
        hass: Any = None,
        delay_s: float = 0,
        on_home_action: Callable | None = None,
        *,
        call_later: _CallLaterFn | None = None,
    ) -> bool:
        """React to a presence-home event; cancel any pending away timer.

        When *delay_s* is 0 (or the controller is not active), behaves as
        before: returns ``True`` when the caller should trigger an immediate
        restore (presence-away mode was active).

        When *delay_s* > 0 **and** the controller is active, a home-delay
        timer is scheduled instead of restoring immediately.  The method
        returns ``False`` in that case so the caller does **not** restore yet.
        """
        if self._away_timer is not None:
            self._away_timer()
            self._away_timer = None

        if not self.is_active:
            return False

        if delay_s > 0 and on_home_action is not None:
            # Cancel any previously pending home timer
            if self._home_timer is not None:
                self._home_timer()
            _cl = call_later or _get_call_later()
            self._home_timer = _cl(hass, delay_s, on_home_action)
            _LOGGER.debug("Presence home – restoring preset in %ds", delay_s)
            return False

        # No delay: immediate restore
        return True

    def get_saved(self) -> SavedState:
        """Return a copy of the currently saved preset/temperature."""
        return SavedState(preset=self._saved.preset, temp=self._saved.temp)

    def activate(self, preset: str, temp: float | None) -> None:
        """Record the pre-away state and mark presence automation as active."""
        self._away_timer = None
        self._saved = SavedState(preset=preset, temp=temp)
        self.is_active = True

    def restore(self) -> SavedState:
        """Clear active state and return the saved preset/temp for restoration."""
        if self._home_timer is not None:
            self._home_timer()
            self._home_timer = None
        saved = SavedState(preset=self._saved.preset, temp=self._saved.temp)
        self._saved = SavedState()
        self.is_active = False
        return saved

    def update_saved(self, preset: str, temp: float | None) -> None:
        """Update the saved preset/temp without changing active state.

        Used when an external preset change arrives while away mode is active
        so that the new preset is restored when presence returns.
        """
        self._saved = SavedState(preset=preset, temp=temp)

    def cancel_timer(self) -> None:
        """Cancel the pending away timer without changing the active flag."""
        if self._away_timer is not None:
            self._away_timer()
            self._away_timer = None
        if self._home_timer is not None:
            self._home_timer()
            self._home_timer = None


# ---------------------------------------------------------------------------
# Persistence across restart / reload (pure logic, no HA imports)
# ---------------------------------------------------------------------------

def _as_opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


@dataclass
class PersistedAutomationState:
    """Automation state that must survive an HA restart or entry reload.

    The climate entity exposes this through ``extra_restore_state_data`` and
    reads it back in ``async_added_to_hass`` to re-arm the controllers.
    """

    window_active: bool = False
    window_saved: SavedState = field(default_factory=SavedState)
    presence_active: bool = False
    presence_saved: SavedState = field(default_factory=SavedState)
    # Wall-clock end of a running boost (0.0 = no boost running).
    boost_end_ts: float = 0.0
    # Preset/temperature to return to when the boost ends.
    boost_saved: SavedState = field(default_factory=SavedState)
    # Summer mode was locking the thermostat (used to leave summer mode
    # correctly when the switch was turned off while HA was down).
    summer_active: bool = False
    # Schedule following: last valid schedule preset and a manual override.
    schedule_preset: str | None = None
    schedule_override_active: bool = False
    schedule_override_until: float | None = None
    schedule_override_sticky: bool = False

    VERSION = 1

    def as_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "version": self.VERSION,
            "window_active": self.window_active,
            "window_saved_preset": self.window_saved.preset,
            "window_saved_temp": self.window_saved.temp,
            "presence_active": self.presence_active,
            "presence_saved_preset": self.presence_saved.preset,
            "presence_saved_temp": self.presence_saved.temp,
            "boost_end_ts": self.boost_end_ts,
            "boost_saved_preset": self.boost_saved.preset,
            "boost_saved_temp": self.boost_saved.temp,
            "summer_active": self.summer_active,
            "schedule_preset": self.schedule_preset,
            "schedule_override_active": self.schedule_override_active,
            "schedule_override_until": self.schedule_override_until,
            "schedule_override_sticky": self.schedule_override_sticky,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PersistedAutomationState | None:
        """Parse a stored dict; return None when it is missing or unusable."""
        if not isinstance(data, Mapping) or "version" not in data:
            return None
        return cls(
            window_active=data.get("window_active") is True,
            window_saved=SavedState(
                preset=_as_opt_str(data.get("window_saved_preset")),
                temp=_as_opt_float(data.get("window_saved_temp")),
            ),
            presence_active=data.get("presence_active") is True,
            presence_saved=SavedState(
                preset=_as_opt_str(data.get("presence_saved_preset")),
                temp=_as_opt_float(data.get("presence_saved_temp")),
            ),
            boost_end_ts=_as_opt_float(data.get("boost_end_ts")) or 0.0,
            boost_saved=SavedState(
                preset=_as_opt_str(data.get("boost_saved_preset")),
                temp=_as_opt_float(data.get("boost_saved_temp")),
            ),
            summer_active=data.get("summer_active") is True,
            schedule_preset=_as_opt_str(data.get("schedule_preset")),
            schedule_override_active=data.get("schedule_override_active") is True,
            schedule_override_until=_as_opt_float(data.get("schedule_override_until")),
            schedule_override_sticky=data.get("schedule_override_sticky") is True,
        )


def normalize_restored_preset(
    preset: str,
    persisted: PersistedAutomationState | None,
    legacy_window_active: bool = False,
) -> str:
    """Return the preset to resume with after a restart or reload.

    With a persisted automation snapshot every preset is kept as-is; a
    window-driven frost protection is forced back to frost so the re-armed
    window controller and the preset agree.  BOOST is kept here and resolved
    by :func:`resolve_boost_restore`.

    Without a snapshot (first start after upgrading from a version that did
    not persist it) BOOST falls back to COMFORT (its timer is gone) and frost
    protection falls back to COMFORT only when the restored state says the
    window automation had set it (``legacy_window_active``).  A frost
    protection preset the user selected is kept.
    """
    if persisted is not None:
        if persisted.window_active:
            return PRESET_FROST_NAME
        return preset
    if preset == PRESET_BOOST_NAME:
        return PRESET_COMFORT_NAME
    if preset == PRESET_FROST_NAME and legacy_window_active:
        return PRESET_COMFORT_NAME
    return preset


@dataclass
class BoostRestore:
    """Outcome of :func:`resolve_boost_restore`."""

    resume: bool
    remaining_s: float = 0.0
    fallback: SavedState = field(default_factory=SavedState)


def resolve_boost_restore(
    persisted: PersistedAutomationState | None,
    now: float,
) -> BoostRestore:
    """Decide how a restored BOOST preset continues after restart/reload.

    ``resume=True`` → restart the boost timer for ``remaining_s`` seconds.
    ``resume=False`` → the boost already ended while HA was down; switch to
    ``fallback`` (the pre-boost preset, COMFORT if unknown).
    """
    saved = persisted.boost_saved if persisted is not None else SavedState()
    fallback = SavedState(
        preset=saved.preset or PRESET_COMFORT_NAME,
        temp=saved.temp,
    )
    if persisted is not None and persisted.boost_end_ts > now:
        return BoostRestore(
            resume=True, remaining_s=persisted.boost_end_ts - now, fallback=fallback
        )
    return BoostRestore(resume=False, fallback=fallback)


# Startup actions returned by the *_startup_action helpers
STARTUP_NONE = "none"          # nothing to do
STARTUP_KEEP = "keep"          # keep the re-armed automation active
STARTUP_RESTORE = "restore"    # restore the saved preset now
STARTUP_ARM_OPEN = "arm_open"  # window is open: start the normal open delay


def window_startup_action(
    active: bool,
    sensor_configured: bool,
    sensor_state: str | None,
) -> str:
    """Reconcile a (re-armed) window controller with the sensor at startup."""
    if active:
        if not sensor_configured:
            # Sensor removed from the config while frost was active – nothing
            # would ever end window mode, so restore now.
            return STARTUP_RESTORE
        if sensor_state == "off":
            # Window closed while HA was down.
            return STARTUP_RESTORE
        # Still open, or not reported yet (unavailable / unknown / not loaded):
        # stay in window mode; the state listener handles the next change.
        return STARTUP_KEEP
    if sensor_configured and sensor_state == "on":
        return STARTUP_ARM_OPEN
    return STARTUP_NONE


def presence_startup_action(
    active: bool,
    sensor_configured: bool,
    sensor_state: str | None,
) -> str:
    """Reconcile a re-armed presence controller with the sensor at startup.

    Only covers the re-armed (active) case; an inactive controller is handled
    by the regular startup logic in the climate entity.
    """
    if not active:
        return STARTUP_NONE
    if not sensor_configured:
        return STARTUP_RESTORE
    if sensor_state is None or sensor_state in _UNAVAILABLE_STATES or sensor_state == "off":
        return STARTUP_KEEP
    return STARTUP_RESTORE


# ---------------------------------------------------------------------------
# Summer mode (pure logic)
# ---------------------------------------------------------------------------

def resolve_summer_state(entity_state: str | None, current: bool) -> bool:
    """Return whether summer mode is active for a given switch state.

    Only a definite ``on`` / ``off`` changes the lock.  ``unavailable``,
    ``unknown`` or a missing entity keep the current value, so a flaky helper
    can never unlock (or lock) the heating by accident.
    """
    if entity_state == "on":
        return True
    if entity_state == "off":
        return False
    return current


def summer_enforcement_needed(
    trv_state: str | None,
    trv_setpoint: float | None,
    target_c: float,
    tolerance_c: float = 0.1,
) -> bool:
    """Return True when the TRV must be commanded back to the summer target.

    The TRV must be in ``heat`` mode at ``target_c``, or ``off``: Tado shows
    its 5 °C frost setpoint as off, and Home Assistant refuses a setpoint
    below 7 °C for a valve that is off, so off counts as locked.  Nothing is
    sent while the TRV is missing or unavailable (the command could not be
    delivered).
    """
    if trv_state is None or trv_state in _UNAVAILABLE_STATES:
        return False
    if trv_state == "off":
        return False
    if trv_state != "heat":
        return True
    if trv_setpoint is None:
        return True
    return abs(trv_setpoint - target_c) >= tolerance_c


# ---------------------------------------------------------------------------
# TRV source: the configured valve, or a fallback entity for the same room
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrvReading:
    """What one TRV climate entity tells us this cycle."""

    entity_id: str | None
    internal_c: float | None = None   # the TRV's own temperature reading
    setpoint_c: float | None = None   # the setpoint it currently holds
    is_fallback: bool = False
    # The setpoint range the entity accepts right now.  Home Assistant checks
    # a command against it, and it can depend on the mode: a HomeKit Tado
    # valve accepts 5-25 °C in heat mode but 7-35 °C while it is off.
    min_c: float | None = None
    max_c: float | None = None
    off: bool = False                 # the entity is in hvac mode "off"

    @property
    def usable(self) -> bool:
        return self.entity_id is not None and self.internal_c is not None


# Readings outside this range are sensor faults (a reboot reporting 0 °C, an
# error code), never a real room or radiator: they are treated as missing.
_PLAUSIBLE_C = (-30.0, 60.0)


def plausible_c(value: Any) -> float | None:
    """A finite temperature inside the plausible range, else None."""
    v = _as_opt_float(value)
    if v is None or not _PLAUSIBLE_C[0] <= v <= _PLAUSIBLE_C[1]:
        return None
    return v


def read_trv(
    state: str | None, attributes: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Everything Roomstat uses from a TRV climate entity: own reading,
    setpoint, accepted range and whether it is off.  Missing or unavailable
    gives no values (an unavailable entity may keep stale attributes)."""
    if state is None or state in _UNAVAILABLE_STATES:
        return {"internal": None, "setpoint": None, "min": None, "max": None, "off": False}
    attrs = attributes or {}
    return {
        "internal": plausible_c(attrs.get("current_temperature")),
        "setpoint": _as_opt_float(attrs.get("temperature")),
        "min": _as_opt_float(attrs.get("min_temp")),
        "max": _as_opt_float(attrs.get("max_temp")),
        "off": state == "off",
    }


def command_limits(
    cfg_min: float, cfg_max: float, trv_min: float | None, trv_max: float | None
) -> tuple[float, float]:
    """The range a command may use: Roomstat's own, narrowed to what the TRV
    accepts right now (anything outside it is refused before it is sent)."""
    lo = max(cfg_min, trv_min) if trv_min is not None else cfg_min
    hi = min(cfg_max, trv_max) if trv_max is not None else cfg_max
    return (lo, hi) if lo <= hi else (cfg_min, cfg_max)


def choose_trv(
    primary: TrvReading, fallback: TrvReading | None, prefer_fallback: bool = False
) -> TrvReading:
    """The TRV to read and command this cycle.

    The configured valve whenever it gives a reading.  When it does not
    (e.g. its HomeKit link dropped) and the fallback does (e.g. the room's
    Tado cloud thermostat, which reaches the same valve another way), the
    fallback.  Otherwise the configured valve, so the cycle waits for it as
    before.  A fallback that is the configured valve itself is ignored.

    With ``prefer_fallback`` the roles swap: the fallback whenever it gives
    a reading (or when neither does, so the cycle waits for the preferred
    one), the configured valve only while the fallback is unavailable.  A
    reading from the fallback entity is always marked ``is_fallback``, so it
    keeps its slower grace periods whichever entity is preferred.
    """
    if fallback is None or fallback.entity_id == primary.entity_id:
        return primary
    if prefer_fallback:
        if fallback.usable or not primary.usable:
            return replace(fallback, is_fallback=True)
        return primary
    if primary.usable or not fallback.usable:
        return primary
    return replace(fallback, is_fallback=True)


def trv_setpoint_drifted(
    last_sent_c: float | None,
    trv_setpoint_c: float | None,
    since_sent_s: float,
    grace_s: float,
    threshold_c: float,
    *,
    trv_off: bool = False,
    floor_c: float = 5.0,
) -> bool:
    """True when the TRV no longer holds the last command.

    Only checked once ``grace_s`` has passed since the command, so the TRV's
    state has had time to report it.  A difference below ``threshold_c`` is
    not worth a command.  A TRV that is off while the last command asked for
    heat (above ``floor_c``, the frost level Tado shows as off) was switched
    off from outside, e.g. in the Tado app.
    """
    if last_sent_c is None or since_sent_s < grace_s:
        return False
    if trv_off:
        return last_sent_c > floor_c + 0.05
    if trv_setpoint_c is None:
        return False
    return abs(trv_setpoint_c - last_sent_c) >= threshold_c


class CommandPacer:
    """Keeps TRV commands from all rooms at least ``spacing_s`` apart.

    ``reserve(now)`` books the next free slot and returns how long the caller
    must wait for it.  It never awaits, so callers running at the same moment
    in one event loop each get their own slot.
    """

    def __init__(self, spacing_s: float) -> None:
        self.spacing_s = spacing_s
        self._next_free = -math.inf

    def reserve(self, now: float) -> float:
        slot = max(now, self._next_free)
        self._next_free = slot + self.spacing_s
        return slot - now


# ---------------------------------------------------------------------------
# Schedule following
# ---------------------------------------------------------------------------

# Presets a schedule may ask for.  BOOST (a short one-off) and manual mode are
# deliberately excluded.  "night" is the user-facing name of the eco preset.
_SCHEDULE_PRESETS = {
    "comfort": "comfort",
    "eco": PRESET_ECO_NAME,
    "night": PRESET_ECO_NAME,
    "away": PRESET_AWAY_NAME,
    "frost_protection": PRESET_FROST_NAME,
    "frost": PRESET_FROST_NAME,
}


def parse_schedule_preset(state: str | None) -> str | None:
    """Map a schedule helper state to an internal preset name.

    Case and spaces are ignored ("Night", "Frost Protection").  Returns None
    for anything that is not a schedulable preset, including
    ``unavailable`` / ``unknown`` – callers then keep the last valid value.
    """
    if not isinstance(state, str):
        return None
    key = state.strip().lower().replace(" ", "_").replace("-", "_")
    return _SCHEDULE_PRESETS.get(key)


class ScheduleController:
    """Tracks the schedule preset and a manual override of it.

    Override semantics:

    - A manual change starts an override.  It ends at the next change of the
      schedule preset, or after ``duration_min`` minutes when that is > 0 –
      whichever comes first.
    - A *sticky* override (manual Away) ignores schedule changes and has no
      timer; it only ends when the user changes the preset or resumes.
    """

    def __init__(self) -> None:
        self.schedule_preset: str | None = None
        self.override_active: bool = False
        self.override_until: float | None = None
        self.override_sticky: bool = False
        self._timer: _CancelFn | None = None

    # ------------------------------------------------------------------
    def schedule_changed(self, preset: str | None) -> bool:
        """Record a schedule value.  Returns True when it must be applied now."""
        if preset is None or preset == self.schedule_preset:
            return False
        self.schedule_preset = preset
        if self.override_active and not self.override_sticky:
            _LOGGER.debug("Schedule changed to %s – override ended", preset)
            self.clear_override()
        return not self.override_active

    def start_override(
        self,
        hass: Any,
        duration_min: float,
        now: float,
        on_expire: Callable,
        *,
        sticky: bool = False,
        call_later: _CallLaterFn | None = None,
    ) -> None:
        """Start (or restart) a manual override."""
        self.cancel_timer()
        self.override_active = True
        self.override_sticky = sticky
        if sticky or duration_min <= 0:
            self.override_until = None
            return
        delay_s = duration_min * 60
        self.override_until = now + delay_s
        _cl = call_later or _get_call_later()
        self._timer = _cl(hass, delay_s, on_expire)

    def rearm(
        self,
        hass: Any,
        now: float,
        on_expire: Callable,
        *,
        call_later: _CallLaterFn | None = None,
    ) -> bool:
        """Re-arm the override timer after a restart.

        Returns True when the override already expired (it is then cleared and
        the caller should return to the schedule).
        """
        if not self.override_active or self.override_until is None:
            return False
        remaining = self.override_until - now
        if remaining <= 0:
            self.clear_override()
            return True
        self.cancel_timer()
        _cl = call_later or _get_call_later()
        self._timer = _cl(hass, remaining, on_expire)
        return False

    def timer_fired(self) -> None:
        """Mark the override as ended by its own timer."""
        self._timer = None
        self.override_active = False
        self.override_until = None
        self.override_sticky = False

    def clear_override(self) -> None:
        """End any override (resume the schedule)."""
        self.cancel_timer()
        self.override_active = False
        self.override_until = None
        self.override_sticky = False

    def cancel_timer(self) -> None:
        """Cancel the expiry timer but keep the override state."""
        if self._timer is not None:
            self._timer()
            self._timer = None

    def remaining_minutes(self, now: float) -> int:
        """Minutes left on a timed override (0 when none / untimed)."""
        if not self.override_active or self.override_until is None:
            return 0
        return max(0, math.ceil((self.override_until - now) / 60))

    def load(self, persisted: PersistedAutomationState | None) -> None:
        """Restore state (without timers) from a persisted snapshot."""
        if persisted is None:
            return
        self.schedule_preset = persisted.schedule_preset
        self.override_active = persisted.schedule_override_active
        self.override_until = (
            persisted.schedule_override_until if self.override_active else None
        )
        self.override_sticky = self.override_active and persisted.schedule_override_sticky



# ---------------------------------------------------------------------------
# Resilience helpers
# ---------------------------------------------------------------------------

def integration_step(now: float, last_ts: float, max_s: float) -> float:
    """Seconds the integral may cover this cycle.

    0 on the first cycle and when the clock went backwards.  At most
    ``max_s`` after a pause (sensor gap, HA busy or restarting), so a long
    gap cannot add a large step to the integral in one cycle.
    """
    if last_ts <= 0 or now <= last_ts:
        return 0.0
    return min(now - last_ts, max_s)


# ---------------------------------------------------------------------------
# Configured entities that disappear
# ---------------------------------------------------------------------------

def entity_problem(
    has_state: bool, registered: bool, disabled: bool, unavailable: bool = False
) -> str | None:
    """Why a configured entity can't be used, or None.

    ``"disabled"``: disabled in the entity registry, so it never reports.
    ``"missing"``: neither a state nor a registry entry, so it was deleted or
    renamed.  ``"unavailable"``: it exists but gives no usable value (only
    checked for the room sensor, see repairs.py).  An entity that is
    registered but has no state yet (its integration is still loading) is
    not a problem.
    """
    if disabled:
        return "disabled"
    if not has_state and not registered:
        return "missing"
    if has_state and unavailable:
        return "unavailable"
    return None


class MissingEntityWatch:
    """Report a configured entity only once it has been a problem for
    ``delay_s`` (after a restart, integrations load in any order), or for the
    delay given per problem in ``delays``."""

    def __init__(self, delay_s: float, delays: Mapping[str, float] | None = None) -> None:
        self.delay_s = delay_s
        self.delays = dict(delays or {})
        self._since: dict[str, float] = {}

    def update(self, now: float, problems: Mapping[str, str | None]) -> dict[str, str]:
        """Settings to report now (setting key -> problem).

        ``problems`` holds every configured entity's setting key and its
        problem (None when fine).  A setting that is fine again, or no longer
        configured, starts from zero next time.
        """
        report: dict[str, str] = {}
        for key, problem in problems.items():
            if problem is None:
                self._since.pop(key, None)
                continue
            since = self._since.setdefault(key, now)
            if now - since >= self.delays.get(problem, self.delay_s):
                report[key] = problem
        for key in [k for k in self._since if k not in problems]:
            del self._since[key]
        return report


def replace_entity_setting(
    data: Mapping[str, Any],
    options: Mapping[str, Any],
    key: str,
    new_entity_id: str | None,
    *,
    in_data: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """New (data, options) of a config entry with setting *key* pointing at
    *new_entity_id*.

    The TRV lives in the entry data (``in_data``); everything else in the
    options, which win over the data (the room sensor starts in the data).
    ``None`` removes an optional setting.
    """
    new_data, new_options = dict(data), dict(options)
    if in_data:
        if not new_entity_id:
            raise ValueError(f"{key} is required")
        new_data[key] = new_entity_id
    elif new_entity_id:
        new_options[key] = new_entity_id
    else:
        new_options.pop(key, None)
    return new_data, new_options

# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _get_call_later() -> _CallLaterFn:
    """Return HA's ``async_call_later`` (imported lazily to stay HA-free at module level)."""
    from homeassistant.helpers.event import async_call_later  # noqa: PLC0415
    return async_call_later


# ---------------------------------------------------------------------------
# Config entry migration (__init__.py async_migrate_entry)
# ---------------------------------------------------------------------------

# Options of entries made before 2.2 (config entry minor version 1), when the
# controller had a switch (deliberate valve control on or off), gain
# scheduling and a plain PI path with a small Kp.  Keys as in const.py.
OBSOLETE_OPTIONS = (
    "stepped_heating_enabled",
    "gain_scheduling_enabled",
    "gain_fine_multiplier",
    "gain_startup_multiplier",
    "gain_startup_threshold_c",
    "gain_fine_threshold_c",
)


def migrate_options_v1_to_v2(
    options: Mapping[str, Any], kp: float, braking_time_min: float,
) -> dict[str, Any]:
    """Options of a minor version 1 entry, in the current meaning.

    The obsolete keys go.  Kp was the plain PI path's gain (around 0.6-0.8),
    a different scale from the gain near the target now (``kp``, 4), so a
    stored Kp is replaced.  A braking time of 0 meant "10 minutes ahead"
    with deliberate valve control, which is now the visible default
    (``braking_time_min``); any other value already was the heat-up lead
    and is kept.  The changed values restart auto-tune's learning (its
    baseline changes), so what it learned with the old controller goes too.
    """
    new = {k: v for k, v in options.items() if k not in OBSOLETE_OPTIONS}
    if "correction_kp" in new:
        new["correction_kp"] = kp
    if "derivative_time_min" in new:
        try:
            td_min = float(new["derivative_time_min"])
        except (TypeError, ValueError):
            td_min = 0.0
        if not math.isfinite(td_min) or td_min <= 0:
            new["derivative_time_min"] = braking_time_min
    return new

