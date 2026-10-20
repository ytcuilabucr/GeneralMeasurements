"""Axis definitions and point-list helpers. Plans are pure data."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def linear(start: float, stop: float, n: int) -> tuple[float, ...]:
    return tuple(float(v) for v in np.linspace(start, stop, n))


def log(start: float, stop: float, n: int) -> tuple[float, ...]:
    if start <= 0 or stop <= 0:
        raise ValueError("log points need positive start and stop")
    return tuple(float(v) for v in np.geomspace(start, stop, n))


@dataclass(frozen=True)
class Axis:
    device: str                 # role name from the station config
    points: tuple[float, ...]
    settle_s: float = 0.0
    snake: bool = False         # reverse direction on alternate passes
    stage: int = 0              # set after the moved axes of lower stages and their settle

    def __post_init__(self):
        if len(self.points) == 0:
            raise ValueError(f"axis {self.device!r} has no points")
        if self.settle_s < 0:
            raise ValueError("settle_s must be >= 0")
