"""
Valve look-ahead: cancel the Tado valve's own look-ahead on a falling
reading (an option per room, 2.14.0; climate_lookahead.py on the entity).

The problem
-----------
A Tado valve runs its own controller on its own reading, which sits next to
the radiator, and it looks ahead on how fast that reading moves.  After a
heat-up the radiator cools and the reading slides 2-5 °C/h; a hold command
just over the reading then looks a degree short to the valve, and it opens
hard.  Living room, 23-30 Oct 2026 (36 hold commands to a shut valve): its
first response follows push + 13 min x fall (r = 0.85, the push alone 0.50),
and a fast slide (2.5 °C/h or more) got four times the valve-minutes of a
flat reading in the first 10 min.  30 Oct 07:28: a +0.4 push on a reading
falling 4 °C/h opened it 74 %.

What it does
------------
In hold, the command is lowered by the learned look-ahead times the fall of
the valve's reading beyond a deadband, by at most a cap
(regulation.lookahead_correction_c, RegulationConfig).  The valve then
answers Roomstat's push as if its reading were flat.  One-sided: a flat,
rising or unknown reading changes nothing, and only the hold is touched, so
it only ever takes heat away.

Learning
--------
The look-ahead is the valve's, so it is learned from the valve
(:class:`LookaheadLearner`): at each release (a hold command to a shut
valve), its first heating power report above 0 (Tado's heating %) within
``observe_s`` of the command, against the push it held (its setpoint minus
its reading; the cloud rounds the setpoint to 0.5 °C) and how fast its
reading fell.  A least-squares fit over the last releases,
response = a + b x push + d x fall, gives the look-ahead d / b.  It is
trusted from ``min_samples`` releases on, when they spread enough in fall
rate, the valve clearly answers the push and push and fall are not
entangled.  The look-ahead in use starts at 0 and moves towards the fit by
at most a step per release.

The trade-off, measured
-----------------------
Project doc roomstat-valve-lookahead-2026-10-30 and CLAUDE.md "Valve
look-ahead": in a simulator fitted to the living room (seven mornings; it
reproduced 30 Oct within minutes), this learner settled at 11-20 min for a
13-min valve and cut a fast-slide release's first opening 73 -> 52 %, but
the peaks after top-ups barely moved (median 0 to -0.03), the room spent
20-40 min more a day 0.2 below its target, and commands rose 14-27 %.  Most
of a top-up's overshoot (+0.21 at night, the valve's reading barely
falling) is the dead time.  The owner's choice (30 Oct 2026): build it,
learned, per room, and judge it live.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any

from .parameters import LookaheadConfig


def _finite(*values: Any) -> bool:
    return all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        for v in values
    )


@dataclass(frozen=True)
class Release:
    """One release and the valve's first answer to it."""

    ts: float
    push_c: float           # the setpoint the valve held minus its reading
    fall_c_per_h: float     # how fast its reading fell (negative: rising)
    response_pct: float     # its first heating power report above 0


@dataclass
class _Pending:
    """A release whose answer is awaited."""

    ts: float
    command_c: float
    reading_c: float
    fall_c_per_h: float
    held_c: float | None = None     # the setpoint the valve first showed


@dataclass(frozen=True)
class Fit:
    """The least-squares fit over the releases kept."""

    lookahead_s: float      # d / b, within 0 .. max_lookahead_s
    gain_pct_per_c: float   # b: the valve's answer per °C of push
    releases: int


class LookaheadLearner:
    """Learns the valve's look-ahead from its answers to releases (see the
    module docstring).  ``lookahead_s`` is the value in use."""

    def __init__(self, cfg: LookaheadConfig | None = None) -> None:
        self.cfg = cfg or LookaheadConfig()
        self.reset()

    def reset(self) -> None:
        """Forget everything learned: back to no look-ahead."""
        self._releases: deque[Release] = deque(maxlen=max(1, self.cfg.window))
        self._pending: _Pending | None = None
        self.lookahead_s: float = 0.0
        self.fit: Fit | None = None
        self.last_line: str | None = None

    # -- the releases ---------------------------------------------------
    @property
    def releases(self) -> tuple[Release, ...]:
        return tuple(self._releases)

    @property
    def observing(self) -> bool:
        """A release's answer is awaited."""
        return self._pending is not None

    def start(
        self, ts: float, command_c: float, reading_c: float | None,
        trv_slope_c_per_s: float | None,
    ) -> bool:
        """A hold command went to a shut valve: watch for its first answer.

        Without the valve's reading or its slope the answer could not be
        told apart from the push, so it is not watched.  Returns whether it
        is."""
        if not _finite(ts, command_c, reading_c, trv_slope_c_per_s):
            self._pending = None
            return False
        self._pending = _Pending(
            ts=float(ts), command_c=float(command_c), reading_c=float(reading_c),
            fall_c_per_h=-float(trv_slope_c_per_s) * 3600.0,
        )
        return True

    def note_setpoint(self, setpoint_c: float | None, seen_ts: float | None = None) -> None:
        """The setpoint the valve shows while its answer is awaited: the
        first one is the push it got (a cloud zone rounds to 0.5 °C).  One
        seen at or before the command (``seen_ts``) is the valve's state
        from before it, so it is not."""
        p = self._pending
        if p is None or p.held_c is not None or not _finite(setpoint_c):
            return
        if seen_ts is not None and (not _finite(seen_ts) or seen_ts <= p.ts):
            return
        p.held_c = float(setpoint_c)

    def cancel(self) -> None:
        """The valve was told something else (off, full power) before it
        answered: the release says nothing."""
        self._pending = None

    def observe(
        self, ts: float, heating_pct: float | None, report_ts: float | None
    ) -> str | None:
        """One cycle's heating power report.  Returns a line for the log
        when a release is counted or given up, else None."""
        p = self._pending
        if p is None or not _finite(ts):
            return None
        # Its answer is a report after the command and within observe_s of
        # it: cycles that skip the look-ahead (the room sensor lost, no data)
        # must not make a much later report the first answer.
        answered = (
            _finite(heating_pct, report_ts) and heating_pct > 0
            and p.ts < report_ts <= p.ts + self.cfg.observe_s
        )
        if answered:
            self._pending = None
            held = p.held_c if p.held_c is not None else p.command_c
            push = held - p.reading_c
            heard = (f"the valve answered {heating_pct:.0f} % to a push of "
                     f"{push:+.1f} on a reading {_trend(p.fall_c_per_h)}")
            if heating_pct >= self.cfg.saturated_pct:
                return self._line(f"{heard}: fully open, so not counted "
                                  "(the push no longer shows)")
            release = Release(p.ts, push, p.fall_c_per_h, float(heating_pct))
            self._releases.append(release)
            return self._line(f"{heard} → {self._learn()}")
        if ts - p.ts > self.cfg.observe_s:
            self._pending = None
            return self._line(
                f"the valve reported no heating within {self.cfg.observe_s / 60:.0f} min "
                "of a release: not counted"
            )
        return None

    # -- the fit --------------------------------------------------------
    def _learn(self) -> str:
        """Fit again and move the look-ahead in use; what happened, in words."""
        fit, why = self._fit()
        self.fit = fit
        if fit is None:
            return f"{why}; look-ahead stays {_min(self.lookahead_s)}"
        c = self.cfg
        cur, target = self.lookahead_s, fit.lookahead_s
        gap = target - cur
        fitted = (f"fit from {fit.releases} releases: it looks {_min(target)} ahead "
                  f"({fit.gain_pct_per_c:.0f} % per °C of push)")
        if abs(gap) < c.deadband_s:
            return f"{fitted}; look-ahead stays {_min(cur)}"
        step = math.copysign(min(abs(gap), c.step_s), gap)
        self.lookahead_s = min(c.max_lookahead_s, max(0.0, cur + step))
        towards = "" if abs(self.lookahead_s - target) < 1.0 else f", a step towards {_min(target)}"
        return f"{fitted}; look-ahead {_min(cur)} → {_min(self.lookahead_s)}{towards}"

    def _fit(self) -> tuple[Fit | None, str]:
        """response = a + b x push + d x fall over the releases kept: the
        look-ahead is d / b.  None (and why) when it cannot be trusted."""
        c = self.cfg
        rel = list(self._releases)
        n = len(rel)
        if n < c.min_samples:
            return None, f"{n} of {c.min_samples} releases needed for a fit"
        mx = sum(r.push_c for r in rel) / n
        mf = sum(r.fall_c_per_h for r in rel) / n
        my = sum(r.response_pct for r in rel) / n
        sxx = sum((r.push_c - mx) ** 2 for r in rel)
        sff = sum((r.fall_c_per_h - mf) ** 2 for r in rel)
        sxf = sum((r.push_c - mx) * (r.fall_c_per_h - mf) for r in rel)
        sxy = sum((r.push_c - mx) * (r.response_pct - my) for r in rel)
        sfy = sum((r.fall_c_per_h - mf) * (r.response_pct - my) for r in rel)
        spread = math.sqrt(sff / (n - 1))
        if spread < c.min_fall_spread_c_per_h:
            return None, (f"the releases' fall rates are too alike for a fit (spread "
                          f"{spread:.1f} °C/h, needs {c.min_fall_spread_c_per_h:.1f})")
        if sxx <= 0:
            return None, "the releases' pushes are all the same: no fit"
        corr = sxf / math.sqrt(sxx * sff)
        if abs(corr) > c.max_push_fall_correlation:
            return None, (f"push and fall move together (r {corr:+.2f}): no fit")
        det = sxx * sff - sxf * sxf
        b = (sff * sxy - sxf * sfy) / det
        d = (sxx * sfy - sxf * sxy) / det
        if b < c.min_push_gain_pct_per_c:
            return None, (f"the valve barely answers the push ({b:.0f} % per °C, needs "
                          f"{c.min_push_gain_pct_per_c:.0f}): no fit")
        lookahead_s = min(c.max_lookahead_s, max(0.0, d / b * 3600.0))
        return Fit(lookahead_s=lookahead_s, gain_pct_per_c=b, releases=n), ""

    def _line(self, text: str) -> str:
        self.last_line = text
        return text

    # -- diagnostics ----------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """For the sensor's attributes and the diagnostics."""
        return {
            "lookahead_min": round(self.lookahead_s / 60.0, 1),
            "fitted_min": None if self.fit is None else round(self.fit.lookahead_s / 60.0, 1),
            "push_gain_pct_per_c": (
                None if self.fit is None else round(self.fit.gain_pct_per_c, 1)
            ),
            "releases": len(self._releases),
            "releases_needed": self.cfg.min_samples,
            "last": self.last_line,
        }

    # -- persistence (a reload, which every preset temperature change
    # causes, must not forget what was learned) ---------------------------
    def to_dict(self) -> dict[str, Any]:
        """JSON-safe state for the restore-state store."""
        p = self._pending
        return {
            "lookahead_s": round(self.lookahead_s, 1),
            "releases": [
                [round(r.ts, 1), round(r.push_c, 2), round(r.fall_c_per_h, 2),
                 round(r.response_pct, 1)]
                for r in self._releases
            ],
            "pending": None if p is None else [
                round(p.ts, 1), round(p.command_c, 2), round(p.reading_c, 2),
                round(p.fall_c_per_h, 2), None if p.held_c is None else round(p.held_c, 2),
            ],
        }

    def restore(self, data: Any, now: float) -> bool:
        """Take back a state saved by :meth:`to_dict`; anything malformed is
        left out.  A release still awaiting its answer is taken back only
        within ``observe_s`` (and not from the future).  Returns whether a
        look-ahead or releases were taken back."""
        self.reset()
        if not isinstance(data, dict):
            return False
        for item in data.get("releases") or []:
            if (
                isinstance(item, (list, tuple)) and len(item) == 4
                and _finite(*item) and 0 < item[3] <= 100
            ):
                self._releases.append(Release(*(float(v) for v in item)))
        la = data.get("lookahead_s")
        if _finite(la) and 0 <= la <= self.cfg.max_lookahead_s:
            self.lookahead_s = float(la)
        pend = data.get("pending")
        if (
            isinstance(pend, (list, tuple)) and len(pend) == 5 and _finite(*pend[:4])
            and (pend[4] is None or _finite(pend[4])) and _finite(now)
            and 0 <= now - pend[0] <= self.cfg.observe_s
        ):
            self._pending = _Pending(*(float(v) for v in pend[:4]),
                                     held_c=None if pend[4] is None else float(pend[4]))
        if self._releases:
            self.fit, _ = self._fit()
        return bool(self._releases) or self.lookahead_s > 0


def _min(seconds: float) -> str:
    return f"{seconds / 60.0:.1f} min"


def _trend(fall_c_per_h: float) -> str:
    if fall_c_per_h >= 0.05:
        return f"falling {fall_c_per_h:.1f} °C/h"
    if fall_c_per_h <= -0.05:
        return f"rising {-fall_c_per_h:.1f} °C/h"
    return "flat"
