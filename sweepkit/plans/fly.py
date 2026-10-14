"""FlyScan: a continuous ramp of a Rampable, sampled every dt until the ramp ends.

Optionally nested under stepped `outer` axes (e.g. a field sweep at each gate
voltage): one ramp per outer point, `snake=True` alternates ramp direction.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .axes import Axis
from .scan import PlanError, Scan


@dataclass(frozen=True)
class FlyScan:
    device: str                    # role name of a Rampable
    start: float
    stop: float
    rate: float                    # units/s, > 0
    dt: float                      # sample interval, s
    readables: tuple[str, ...] = ()
    outer: tuple[Axis, ...] = ()   # stepped axes, outermost first
    snake: bool = False            # alternate ramp direction on successive ramps
    settle_s: float = 0.0          # wait at the start point before each ramp
    on_sample_error: str = "fail"  # "fail" | "skip": a failed/timed-out sample read
    max_skips: int = 5             # skip policy: abort once more samples than this are lost
    timeout_s: float | None = None  # per ramp; None: 1.5 x ideal ramp time + 10 s
    metadata: Mapping[str, Any] = field(default_factory=dict)
    watchdogs: tuple[Callable, ...] = ()

    mode: ClassVar[str] = "fly"

    def __post_init__(self):
        if self.rate <= 0 or self.dt <= 0:
            raise PlanError("rate and dt must be > 0")
        if self.on_sample_error not in ("fail", "skip"):
            raise PlanError("on_sample_error must be 'fail' or 'skip'")
        if self.max_skips < 0:
            raise PlanError("max_skips must be >= 0")
        if self.start == self.stop:
            raise PlanError("start and stop are equal")
        if self.device in {a.device for a in self.outer}:
            raise PlanError(f"{self.device!r} is both the ramped and an outer axis")
        if self.outer:
            Scan(self.outer)       # duplicate/empty outer axes rejected here

    @property
    def axis_names(self) -> tuple[str, ...]:
        return (*(a.device for a in self.outer), self.device)

    @property
    def shape(self) -> tuple[int, ...]:
        """Outer grid shape (number of ramps per axis); () for a single ramp."""
        return tuple(len(a.points) for a in self.outer)

    @property
    def n_ramps(self) -> int:
        n = 1
        for s in self.shape:
            n *= s
        return n

    @property
    def duration_s(self) -> float:
        return abs(self.stop - self.start) / self.rate

    def ramps(self) -> Iterator[tuple[int, tuple[int, ...], dict[str, float], float, float]]:
        """Yield (ramp, outer_index, outer_setpoints, start, stop) per ramp."""
        if not self.outer:
            yield 0, (), {}, self.start, self.stop
            return
        for r, idx, sp in Scan(self.outer).points():
            fwd = not (self.snake and r % 2)
            a, b = (self.start, self.stop) if fwd else (self.stop, self.start)
            yield r, idx, sp, a, b

    def estimate_time(self, move_s: float = 0.0) -> float:
        """Seconds: per ramp, the ramp itself + settle + `move_s` to reach the start."""
        return self.n_ramps * (self.duration_s + self.settle_s + move_s)

    def to_dict(self) -> dict:
        return {"mode": "fly", "device": self.device, "start": self.start,
                "stop": self.stop, "rate": self.rate, "dt": self.dt,
                "readables": list(self.readables), "settle_s": self.settle_s,
                "snake": self.snake, "on_sample_error": self.on_sample_error,
                "max_skips": self.max_skips,
                "outer": [{"device": a.device, "points": list(a.points),
                           "settle_s": a.settle_s} for a in self.outer],
                "shape": list(self.shape), "metadata": dict(self.metadata)}

    def validate(self, devices: Mapping[str, object]) -> None:
        dev = devices.get(self.device)
        if dev is None:
            raise PlanError(f"axis device {self.device!r} not in station")
        for attr in ("set", "limits", "start_ramp", "ramp_done"):
            if not hasattr(dev, attr):
                raise PlanError(f"{self.device!r} is not Rampable (missing {attr})")
        lo, hi = dev.limits
        for v in (self.start, self.stop):
            if not lo <= v <= hi:
                raise PlanError(f"{self.device!r}: {v} outside limits [{lo}, {hi}]")
        if self.outer:
            Scan(self.outer).validate(devices)
        for r in self.readables:
            d = devices.get(r)
            if d is None or not hasattr(d, "read"):
                raise PlanError(f"readable {r!r} missing or not Readable")
