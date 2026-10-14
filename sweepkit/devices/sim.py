"""Simulated devices for offline development and tests."""

from __future__ import annotations

import asyncio

import numpy as np

from .protocols import FieldInfo


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
