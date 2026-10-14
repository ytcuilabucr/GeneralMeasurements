"""FlyScan: one continuous ramp of a Rampable, sampled every dt until the ramp ends."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .scan import PlanError


@dataclass(frozen=True)
class FlyScan:
    device: str                    # role name of a Rampable
    start: float
    stop: float
    rate: float                    # units/s, > 0
    dt: float                      # sample interval, s
    readables: tuple[str, ...] = ()
    settle_s: float = 0.0          # wait at start before the ramp begins
    timeout_s: float | None = None  # None: 1.5 x ideal ramp time + 10 s
    metadata: Mapping[str, Any] = field(default_factory=dict)
    watchdogs: tuple[Callable, ...] = ()

    mode: ClassVar[str] = "fly"

    def __post_init__(self):
        if self.rate <= 0 or self.dt <= 0:
            raise PlanError("rate and dt must be > 0")
        if self.start == self.stop:
            raise PlanError("start and stop are equal")

    @property
    def axis_names(self) -> tuple[str, ...]:
        return (self.device,)

    @property
    def shape(self) -> tuple[int, ...]:
        return ()                  # number of samples isn't known in advance

    @property
    def duration_s(self) -> float:
        return abs(self.stop - self.start) / self.rate

    def to_dict(self) -> dict:
        return {"mode": "fly", "device": self.device, "start": self.start,
                "stop": self.stop, "rate": self.rate, "dt": self.dt,
                "readables": list(self.readables), "settle_s": self.settle_s,
                "shape": [], "metadata": dict(self.metadata)}

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
        for r in self.readables:
            d = devices.get(r)
            if d is None or not hasattr(d, "read"):
                raise PlanError(f"readable {r!r} missing or not Readable")
