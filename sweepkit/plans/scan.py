"""Scan: nested axes + readables, and the point iterator."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .axes import Axis


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Scan:
    axes: tuple[Axis, ...]                       # axes[0] is outermost
    readables: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    watchdogs: tuple[Callable, ...] = ()         # async (Event) -> str | None

    mode: ClassVar[str] = "stepped"

    def __post_init__(self):
        if not self.axes:
            raise PlanError("a scan needs at least one axis")
        names = [a.device for a in self.axes]
        if len(set(names)) != len(names):
            raise PlanError(f"duplicate axis devices: {names}")

    @property
    def axis_names(self) -> tuple[str, ...]:
        return tuple(a.device for a in self.axes)

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(len(a.points) for a in self.axes)

    @property
    def n_points(self) -> int:
        n = 1
        for s in self.shape:
            n *= s
        return n

    def points(self) -> Iterator[tuple[int, tuple[int, ...], dict[str, float]]]:
        """Yield (seq, grid_index, {axis: setpoint}). A snake axis reverses when the
        sum of outer grid indices is odd, so the path is continuous (boustrophedon)."""
        seq = 0

        def rec(k: int, outer: tuple[int, ...]):
            nonlocal seq
            ax = self.axes[k]
            order = range(len(ax.points))
            if ax.snake and sum(outer) % 2:
                order = reversed(order)
            for i in order:
                idx = outer + (i,)
                if k == len(self.axes) - 1:
                    sp = {a.device: a.points[j] for a, j in zip(self.axes, idx)}
                    yield seq, idx, sp
                    seq += 1
                else:
                    yield from rec(k + 1, idx)

        yield from rec(0, ())

    def estimate_time(self, read_s: float = 0.0, move_s: float = 0.0) -> float:
        """Seconds, assuming each move costs `move_s` + the axis settle time and
        each point costs `read_s`. Only axes whose setpoint changes are moved."""
        total, prev = 0.0, {}
        for _, _, sp in self.points():
            moved = [a for a in self.axes if prev.get(a.device) != sp[a.device]]
            if moved:
                total += move_s + max(a.settle_s for a in moved)
            total += read_s
            prev = sp
        return total

    def to_dict(self) -> dict:
        """JSON-able description of the plan (stored in the data file)."""
        return {"mode": "stepped", "axes": [{"device": a.device, "points": list(a.points),
                          "settle_s": a.settle_s, "snake": a.snake} for a in self.axes],
                "readables": list(self.readables), "shape": list(self.shape),
                "metadata": dict(self.metadata)}

    def validate(self, devices: Mapping[str, object]) -> None:
        """Check roles exist and every setpoint is inside device limits.
        Called before any hardware moves."""
        for a in self.axes:
            dev = devices.get(a.device)
            if dev is None:
                raise PlanError(f"axis device {a.device!r} not in station")
            if not (hasattr(dev, "set") and hasattr(dev, "limits")):
                raise PlanError(f"{a.device!r} is not Movable")
            lo, hi = dev.limits
            bad = [p for p in a.points if not lo <= p <= hi]
            if bad:
                raise PlanError(f"{a.device!r}: {len(bad)} point(s) outside limits "
                                f"[{lo}, {hi}], first {bad[0]}")
        for r in self.readables:
            dev = devices.get(r)
            if dev is None or not hasattr(dev, "read"):
                raise PlanError(f"readable {r!r} missing or not Readable")
