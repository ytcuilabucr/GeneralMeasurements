"""Device contract: Readable, Movable, Rampable, Configurable. SI units at the interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class FieldInfo:
    units: str
    label: str = ""
    shape: tuple[int, ...] = ()
    dtype: str = "float64"


@runtime_checkable
class Readable(Protocol):
    name: str

    def describe(self) -> dict[str, FieldInfo]: ...

    async def read(self) -> dict[str, float | np.ndarray]: ...


@runtime_checkable
class Movable(Readable, Protocol):
    limits: tuple[float, float]

    async def set(self, value: float) -> None:
        """Return only when settled."""


@runtime_checkable
class Rampable(Movable, Protocol):
    async def start_ramp(self, target: float, rate: float) -> None: ...

    async def ramp_done(self) -> bool: ...


@runtime_checkable
class Configurable(Protocol):
    async def snapshot(self) -> dict: ...

    async def safe_state(self) -> None: ...
