"""The Timer: a virtual instrument for time traces, after Labber's Timer driver
(Labber-software/Drivers, Timer/Timer.py and Timer.ini).

- **Timer** (step channel "Timer.timer", s): setting t waits until t seconds
  have passed since the timer was reset, then returns. The wait is to an
  absolute time, not a sleep of the step: a slow read does not push the later
  points back, so the points stay on the grid 0, dt, 2·dt, ... Setting 0 resets
  the timer. So does the first set, and a value lower than the last one (the
  next pass of an outer loop): the timer then reads that value at once.
- **Clock** (log channel "Timer.clock", s): the time since the reset, read at
  the point, i.e. when the readings were taken. Log it to record the real time
  of every point.

Put the Timer first in the step sequence (the inner loop) from 0 to the trace
length, and the parameter to change in the outer loop: every outer value is
set, then the trace runs from 0. General Measurement sets the Timer after the
other channels and their wait (`stage` 1), so a pass starts once the outer
value is in place.

A point that is already due (a read longer than the step) is taken at once;
`late` counts them, and the clock shows by how much. A pause lets the clock
run on: the points due meanwhile are taken at once after Resume.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable

from ..devices.protocols import FieldInfo

NAME = "Timer"
STEP_ID = f"{NAME}.timer"
CLOCK_ID = f"{NAME}.clock"
FINE_S = 0.02            # the last part of a wait is polled, not slept (timer resolution)
LATE_S = 0.005           # due more than this ago: counted late


class Timer:
    """A virtual instrument (no hardware): see the module docstring."""

    limits = (0.0, math.inf)

    def __init__(self, clock: Callable[[], float] = time.perf_counter):
        self._now = clock
        self.origin: float | None = None          # clock value at the reset
        self.last: float | None = None            # the last value set
        self.late = 0                             # points that were already due

    def elapsed(self) -> float:
        if self.origin is None:
            self.origin = self._now()
        return self._now() - self.origin

    async def set(self, t: float) -> None:
        if not t >= 0:
            raise ValueError(f"Timer: {t!r} s; the timer counts up from 0")
        now = self._now()
        if self.origin is None or t == 0 or (self.last is not None and t < self.last):
            self.origin = now - t                 # reset: the clock reads t now
        else:
            due = self.origin + t
            if now - due > LATE_S:
                self.late += 1
            while (left := due - self._now()) > 0:
                # sleep most of it, then yield until due: the loop's timer is
                # only good to ~15 ms on Windows
                await asyncio.sleep(left - FINE_S if left > FINE_S else 0)
        self.last = t

    async def read(self) -> dict[str, float]:
        return {CLOCK_ID: self.elapsed()}

    def describe(self) -> dict[str, FieldInfo]:
        return {CLOCK_ID: FieldInfo("s", label="Timer clock")}
