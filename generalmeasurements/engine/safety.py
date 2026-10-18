"""Watchdog factories (async (Event) -> str | None) and the loop-lag monitor."""

from __future__ import annotations

import asyncio
import logging
import math

from .events import Event

log = logging.getLogger("generalmeasurements.safety")


def compliance_watchdog(*fields: str):
    """Trip when any `<dev>.compliance` flag field is non-zero."""
    async def wd(ev: Event) -> str | None:
        for f in fields:
            v = ev.data.get(f)
            if v is not None and v != 0:
                return f"{f} in compliance at seq {ev.seq}"
        return None
    return wd


def range_watchdog(field: str, lo: float, hi: float):
    """Trip when a reading leaves [lo, hi] (e.g. leakage current, temperature)."""
    async def wd(ev: Event) -> str | None:
        v = ev.data.get(field)
        if v is not None and not lo <= float(v) <= hi:
            return f"{field}={float(v):.6g} outside [{lo:g}, {hi:g}] at seq {ev.seq}"
        return None
    return wd


def nan_watchdog(*fields: str, max_consecutive: int = 1):
    """Trip after `max_consecutive` NaN/inf readings in a row on any listed field."""
    streak = {f: 0 for f in fields}

    async def wd(ev: Event) -> str | None:
        for f in fields:
            v = ev.data.get(f)
            bad = v is not None and not math.isfinite(float(v))
            streak[f] = streak[f] + 1 if bad else 0
            if streak[f] >= max_consecutive:
                return f"{f} non-finite for {streak[f]} reading(s) at seq {ev.seq}"
        return None
    return wd


class LoopLagMonitor:
    """Logs (and records) event-loop stalls longer than `threshold_s`."""

    def __init__(self, threshold_s: float = 0.1, interval_s: float = 0.02):
        self.threshold_s, self.interval_s = threshold_s, interval_s
        self.stalls: list[float] = []
        self._task: asyncio.Task | None = None

    async def _loop(self):
        loop = asyncio.get_running_loop()
        while True:
            t = loop.time()
            await asyncio.sleep(self.interval_s)
            lag = loop.time() - t - self.interval_s
            if lag > self.threshold_s:
                self.stalls.append(lag)
                log.warning("event loop stalled for %.0f ms", lag * 1e3)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
