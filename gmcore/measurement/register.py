"""The server's register: the latest reading of every field of every connected
instrument, as the Instrument Server's panels poll them.

The panels are the only thing that reads the hardware for an instrument the
measurement *samples*: the measurement takes the register's latest values at
its own times and never talks to that instrument. So the hardware is read at
the panel's interval (its Live setting), and the measurement's interval is
free. Written from the GUI thread (each poll), read from the worker thread (the
run): one lock.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from ..devices.protocols import FieldInfo


class Register:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._now = clock
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, tuple[float, Any]]] = {}   # inst -> key -> (t, v)

    def update(self, instrument: str, t: float, data: dict[str, Any] | None) -> None:
        """One poll's data (a panel's `reading` signal: name, wall-clock time, data)."""
        if not data:
            return
        with self._lock:
            mine = self._data.setdefault(instrument, {})
            for key, value in data.items():
                mine[key] = (t, value)

    def drop(self, instrument: str) -> None:
        """The instrument disconnected: its values are no longer current."""
        with self._lock:
            self._data.pop(instrument, None)

    def sample(self, instrument: str, keys: Iterable[str]) -> tuple[dict[str, Any], float]:
        """The latest value of each key (NaN if none) and the age of the oldest
        of them, in s (NaN if none at all)."""
        with self._lock:
            mine = dict(self._data.get(instrument, {}))
        out: dict[str, Any] = {}
        oldest: float | None = None
        for k in keys:
            if k in mine:
                t, v = mine[k]
                out[k] = v
                oldest = t if oldest is None else min(oldest, t)
            else:
                out[k] = math.nan
        return out, math.nan if oldest is None else self._now() - oldest


def age_key(instrument: str) -> str:
    return f"{instrument}.sample_age"


class RegisterTarget:
    """One sampled instrument, as the runner's readable: the register's latest
    values of the logged keys, plus `<instrument>.sample_age` (s), how old the
    oldest of them is."""

    def __init__(self, instrument: str, register: Register, keys: list[str],
                 info: dict[str, FieldInfo]):
        self.name, self.register, self.keys = instrument, register, keys
        self._info = info

    def describe(self) -> dict[str, FieldInfo]:
        out = {k: self._info[k] for k in self.keys if k in self._info}
        out[age_key(self.name)] = FieldInfo("s", label=f"{self.name} sample age")
        return out

    async def read(self) -> dict[str, Any]:
        values, age = self.register.sample(self.name, self.keys)
        values[age_key(self.name)] = age
        return values
