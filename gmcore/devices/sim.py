"""Simulated devices for offline development and tests."""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from dataclasses import dataclass

import numpy as np

from .protocols import FieldInfo
from .transport import DriverError


class SimSource:
    def __init__(self, name: str, limits=(-10.0, 10.0), delay: float = 0.0):
        self.name, self.limits, self.delay, self.value = name, limits, delay, 0.0
        self.safe = False

    def describe(self):
        return {self.name: FieldInfo("V", self.name)}

    async def set(self, value: float) -> None:
        await asyncio.sleep(self.delay)
        self.value = value

    async def read(self):
        return {self.name: self.value}

    async def snapshot(self):
        return {"value": self.value}

    async def safe_state(self):
        self.value, self.safe = 0.0, True


class SimLockin:
    """Reads sig = gain * source.value + noise."""

    def __init__(self, name: str, source: SimSource, gain: float = 1e-3, noise: float = 1e-6):
        self.name, self.source, self.gain, self.noise = name, source, gain, noise
        self._rng = np.random.default_rng(0)

    def describe(self):
        return {f"{self.name}.x": FieldInfo("V"), f"{self.name}.y": FieldInfo("V")}

    async def read(self):
        x = self.gain * self.source.value + self._rng.normal(0, self.noise)
        return {f"{self.name}.x": x, f"{self.name}.y": self._rng.normal(0, self.noise)}


class SimMagnet:
    """Rampable with a real, time-based ramp (value follows wall-clock time)."""

    def __init__(self, name: str, limits=(-9.0, 9.0), max_rate: float = 0.5):
        self.name, self.limits, self.max_rate = name, limits, max_rate
        self._v0 = self._target = 0.0
        self._rate = 0.0
        self._t0 = time.monotonic()
        self.safe = False

    @property
    def value(self) -> float:
        dist = min(abs(self._target - self._v0), self._rate * (time.monotonic() - self._t0))
        return self._v0 + math.copysign(dist, self._target - self._v0)

    def describe(self):
        return {self.name: FieldInfo("T", self.name)}

    async def start_ramp(self, target: float, rate: float) -> None:
        if not self.limits[0] <= target <= self.limits[1]:
            raise ValueError(f"{self.name}: {target} outside limits {self.limits}")
        if not 0 <= rate <= self.max_rate:
            raise ValueError(f"{self.name}: rate {rate} exceeds max_rate {self.max_rate}")
        self._v0, self._target, self._rate = self.value, target, rate
        self._t0 = time.monotonic()

    async def ramp_done(self) -> bool:
        return abs(self.value - self._target) < 1e-12

    async def set(self, value: float) -> None:
        await self.start_ramp(value, self.max_rate)
        while not await self.ramp_done():
            await asyncio.sleep(0.002)

    async def read(self):
        return {self.name: self.value}

    async def snapshot(self):
        return {"value": self.value, "target": self._target, "rate": self._rate}

    async def safe_state(self):
        await self.start_ramp(self.value, 0.0)   # hold field; never quench-ramp
        self.safe = True


@dataclass
class Fault:
    """One injected fault on `method` of a wrapped device.

    kind: "timeout" (raise TimeoutError), "error" (raise DriverError),
          "delay" (sleep delay_s then proceed), "garbage" (read returns NaNs).
    The first `after` calls pass; then `times` consecutive calls fault
    (None = every call from then on).
    """
    method: str
    kind: str
    after: int = 0
    times: int | None = 1
    delay_s: float = 0.0


class FaultyDevice:
    """Wrap any sim (or real) device and inject faults into its async methods."""

    def __init__(self, inner, faults: list[Fault]):
        self._inner, self._faults = inner, faults
        self._calls: dict[str, int] = {}

    def __getattr__(self, attr):
        target = getattr(self._inner, attr)
        if not inspect.iscoroutinefunction(target):
            return target
        faults = [f for f in self._faults if f.method == attr]
        if not faults:
            return target

        async def wrapped(*args, **kwargs):
            n = self._calls.get(attr, 0)
            self._calls[attr] = n + 1
            for f in faults:
                if n >= f.after and (f.times is None or n < f.after + f.times):
                    if f.kind == "timeout":
                        raise TimeoutError(f"injected timeout in {attr}")
                    if f.kind == "error":
                        raise DriverError(f"injected error in {attr}")
                    if f.kind == "delay":
                        await asyncio.sleep(f.delay_s)
                    elif f.kind == "garbage":
                        res = await target(*args, **kwargs)
                        return {k: float("nan") for k in res}
                    else:
                        raise ValueError(f"unknown fault kind {f.kind!r}")
            return await target(*args, **kwargs)
        return wrapped
