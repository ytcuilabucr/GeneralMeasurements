"""A measurement, as the General Measurement window edits it (after Labber's
Measurement Editor). Pure data, no Qt, no instruments.

- **Step sequence**: the channels to step. The first is the innermost loop
  (stepped fastest), as in Labber. A channel with a single value is no loop:
  it is set once, before the first point.
- Each step channel's values come from its **step items**, joined in order:
  a single value, or a range given as start - stop or center - span, with a
  number of points or a step size, spaced linearly or logarithmically.
- **Log channels**: the readings recorded at every point (field keys such as
  "Keithley 2400 - 1.i").

Saved as TOML (`to_toml` / `from_toml`), so a measurement can be read and
edited by hand; the run's data file stores the same text.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import tomlkit

from ..plans.axes import linear, log

RANGE_TYPES = {"single": "Single", "start_stop": "Start - stop", "center_span": "Center - span"}
STEP_TYPES = {"points": "Number of points", "step": "Step size"}
INTERPOLATIONS = {"linear": "Linear", "log": "Logarithmic"}
AFTER = {"hold": "Leave at the last values",
         "safe": "Safe state for every instrument used"}


@dataclass
class StepItem:
    """One range of a step channel."""
    range_type: Literal["single", "start_stop", "center_span"] = "start_stop"
    start: float = 0.0              # also the value of a single item
    stop: float = 1.0
    step_type: Literal["points", "step"] = "points"
    n_points: int = 11
    step: float = 0.1
    interpolation: Literal["linear", "log"] = "linear"

    # center / span are another view of start / stop
    @property
    def center(self) -> float:
        return (self.start + self.stop) / 2

    @property
    def span(self) -> float:
        return self.stop - self.start

    def set_center_span(self, center: float, span: float) -> None:
        self.start, self.stop = center - span / 2, center + span / 2

    def points(self) -> int:
        """How many values: given, or from the step size (the step is then
        adjusted so the range ends exactly at stop)."""
        if self.range_type == "single":
            return 1
        if self.step_type == "points":
            return max(1, int(self.n_points))
        if self.step <= 0 or not math.isfinite(self.step):
            raise ValueError(f"step size must be > 0 (got {self.step:g})")
        return round(abs(self.stop - self.start) / self.step) + 1

    def values(self) -> tuple[float, ...]:
        if self.range_type == "single":
            return (float(self.start),)
        n = self.points()
        if n == 1:
            return (float(self.start),)
        if self.interpolation == "log":
            return log(self.start, self.stop, n)
        return linear(self.start, self.stop, n)


@dataclass
class StepChannel:
    channel: str                                   # "Keithley 2400 - 1.level"
    items: list[StepItem] = field(default_factory=lambda: [StepItem()])
    settle_s: float = 0.0                          # wait after each set
    snake: bool = False                            # reverse on alternate passes

    def values(self) -> tuple[float, ...]:
        """Every item's values, joined; a value repeated where one item ends and
        the next begins is kept once."""
        out: list[float] = []
        for item in self.items:
            v = list(item.values())
            if out and v and v[0] == out[-1]:
                v = v[1:]
            out.extend(v)
        return tuple(out)

    @property
    def single(self) -> bool:
        return len(self.values()) == 1


@dataclass
class MeasurementConfig:
    name: str = "measurement"
    comment: str = ""
    step: list[StepChannel] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    after: Literal["hold", "safe"] = "hold"
    data_dir: str = "Data"

    def n_points(self) -> int:
        n = 1
        for sc in self.step:
            n *= len(sc.values())
        return n

    def loops(self) -> list[StepChannel]:
        """The step channels that are loops (more than one value), inner first."""
        return [sc for sc in self.step if not sc.single]

    # -- file ---------------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MeasurementConfig:
        d = dict(d)
        unknown = set(d) - {"name", "comment", "step", "log", "after", "data_dir"}
        if unknown:
            raise ValueError(f"unknown measurement keys: {', '.join(sorted(unknown))}")
        steps = []
        for s in d.pop("step", []):
            s = dict(s)
            items = [StepItem(**i) for i in s.pop("items", [])] or [StepItem()]
            steps.append(StepChannel(items=items, **s))
        cfg = cls(step=steps, **d)
        if cfg.after not in AFTER:
            raise ValueError(f"after = {cfg.after!r}: one of {', '.join(AFTER)}")
        for sc in cfg.step:
            for it in sc.items:
                for key, allowed in (("range_type", RANGE_TYPES), ("step_type", STEP_TYPES),
                                     ("interpolation", INTERPOLATIONS)):
                    if getattr(it, key) not in allowed:
                        raise ValueError(f"{sc.channel}: {key} = {getattr(it, key)!r}: "
                                         f"one of {', '.join(allowed)}")
        return cfg

    def to_toml(self) -> str:
        doc = tomlkit.document()
        doc.add(tomlkit.comment("General Measurement: a measurement (step sequence: the "
                                "first channel is the inner loop)"))
        for k in ("name", "comment", "after", "data_dir"):
            doc[k] = getattr(self, k)
        doc["log"] = list(self.log)
        steps = tomlkit.aot()
        for sc in self.step:
            t = tomlkit.table()
            t["channel"], t["settle_s"], t["snake"] = sc.channel, sc.settle_s, sc.snake
            items = tomlkit.aot()
            for it in sc.items:
                items.append(tomlkit.item(asdict(it)))
            t["items"] = items
            steps.append(t)
        doc["step"] = steps
        return tomlkit.dumps(doc)

    @classmethod
    def from_toml(cls, text: str) -> MeasurementConfig:
        return cls.from_dict(tomllib.loads(text))
