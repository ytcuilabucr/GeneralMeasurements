"""Channels: what a measurement can step and log on the connected instruments,
and how a MeasurementConfig becomes a Scan the runner executes.

- A **step channel** is a numeric setting the driver's definition marks
  writable with a `set` method ("Keithley 2400 - 1.level", "SR850 - 1.frequency").
  An instrument with a plain set() / limits and no such property (simulators,
  the Lake Shore 625's field) gets one for that: "<name>.<property>" or
  "<name>.value". Its limits are the definition's (`limits`, `min`, `max`).
- A **log channel** is one field of the instrument's describe(), the same
  readings its panel shows ("Keithley 2400 - 1.i").

`build()` wraps them as the runner's devices, so the engine is unchanged:

- StepTarget: one step channel as a Movable (`set`, `limits`); it reads nothing
  (the setpoints are recorded anyway).
- LogTarget: one instrument's read(), cut down to the logged fields. An
  instrument that can't be read now (a Keithley with the output off) gives NaN.
- InstrumentRef: the instrument itself, never read: its snapshot goes into the
  data file, and with "Safe state" after the run its safe_state() runs, in the
  station's shutdown order. Nothing else is done before the run: no output is
  turned on.
- RegisterTarget (register.py): a logged instrument the measurement samples
  from the server's register instead of reading it (log_sources()).

The Timer (timer.py) is always offered: step channel "Timer.timer", log
channel "Timer.clock". build() gives every run a fresh one, set after the other
channels (axis stage 1).
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..devices import definitions as defs
from ..devices.protocols import FieldInfo
from ..plans import PlanError, Scan
from ..plans.axes import Axis
from . import timer as tm
from .config import MeasurementConfig
from .register import Register, RegisterTarget

NUMERIC = ("float", "int")


@dataclass(frozen=True)
class Channel:
    id: str                       # "<instrument>.<property>" or a describe() key
    instrument: str
    label: str
    unit: str
    kind: str                     # "step" or "log"
    limits: tuple[float | None, float | None] = (None, None)


def _definition(dev: Any) -> defs.Definition | None:
    if getattr(type(dev), "DEFINITION", None) is None:
        return None
    return defs.definition_of(dev)


def _step_props(defn: defs.Definition, dev: Any) -> dict[str, defs.Prop]:
    return {pid: p for pid, p in defs.props(defn, dev).items()
            if p.kind in NUMERIC and p.access in ("write", "both")}


def step_channels(name: str, dev: Any) -> list[Channel]:
    defn = _definition(dev)
    out = []
    covered = False                                 # does a property call dev.set?
    main_pid = None                                 # a ramp-only property: dev.set sets it
    if defn is not None:
        for pid, p in _step_props(defn, dev).items():
            if p.set:
                covered = covered or p.set == "set"
                out.append(Channel(f"{name}.{pid}", name, defs.text(p.label, dev) or pid,
                                   defs.text(p.unit, dev), "step", defs.bounds(p, dev)))
            elif p.ramp is not None and main_pid is None:
                main_pid = pid
    if not covered and callable(getattr(dev, "set", None)) and hasattr(dev, "limits"):
        if main_pid is not None and defn is not None:
            p = defn.properties[main_pid]
            out.insert(0, Channel(f"{name}.{main_pid}", name,
                                  defs.text(p.label, dev) or main_pid,
                                  defs.text(p.unit, dev), "step", defs.bounds(p, dev)))
        else:
            info = _describe(dev).get(name, FieldInfo(""))
            out.insert(0, Channel(f"{name}.value", name, "value", info.units, "step",
                                  tuple(dev.limits)))
    return out


def _describe(dev: Any) -> dict[str, FieldInfo]:
    fn = getattr(dev, "describe", None)
    return fn() if fn else {}


def log_channels(name: str, dev: Any) -> list[Channel]:
    return [Channel(key, name, info.label or key, info.units, "log")
            for key, info in _describe(dev).items() if not info.shape]


TIMER_CHANNELS = (
    Channel(tm.STEP_ID, tm.NAME, "wait until", "s", "step", (0.0, None)),
    Channel(tm.CLOCK_ID, tm.NAME, "Timer clock", "s", "log"))


def catalog(devices: Mapping[str, Any]) -> dict[str, Channel]:
    """Every step and log channel of the connected instruments, by id, and the
    Timer's (unless an instrument is itself named "Timer")."""
    out: dict[str, Channel] = {}
    for name, dev in devices.items():
        for ch in step_channels(name, dev) + log_channels(name, dev):
            out.setdefault(ch.id, ch)
    if tm.NAME not in devices:
        for ch in TIMER_CHANNELS:
            out[ch.id] = ch
    return out


def timer_step(cfg: MeasurementConfig) -> float | None:
    """The Timer's step (s): the smallest gap between its values, None when the
    Timer is not stepped as a loop."""
    for sc in cfg.step:
        if sc.channel == tm.STEP_ID and not sc.single:
            v = sorted(set(sc.values()))
            gaps = [b - a for a, b in zip(v, v[1:]) if b > a]
            return min(gaps) if gaps else None
    return None


def log_sources(cfg: MeasurementConfig, channels: Mapping[str, Channel],
                intervals: Mapping[str, float | None],
                tolerance: float = 0.01) -> dict[str, str]:
    """"direct" or "register" for every logged instrument (the Timer is always
    read directly). `channels`: the catalog; `intervals`: each instrument's panel
    polling interval in s, None when its panel isn't polling (Live off).

    auto: "register" when the Timer is stepped and the instrument's panel polls
    at another interval (relative `tolerance`): the panel alone reads the
    hardware and the measurement samples its latest values. "direct" otherwise,
    and always for an instrument whose panel isn't polling."""
    dt = timer_step(cfg)
    out: dict[str, str] = {}
    for key in cfg.log:
        ch = channels.get(key)
        if ch is None or ch.instrument == tm.NAME or ch.instrument in out:
            continue
        if cfg.log_source != "auto":
            out[ch.instrument] = cfg.log_source
            continue
        iv = intervals.get(ch.instrument)
        mismatch = dt is not None and iv is not None and abs(iv - dt) > tolerance * dt
        out[ch.instrument] = "register" if mismatch else "direct"
    return out


def estimate_s(scan: Scan, read_s: float = 0.0) -> float:
    """The run's time in s: each changed channel's wait, each point's read, and
    the Timer's waits (a pass of the Timer lasts its last value at least)."""
    t, prev = 0.0, {}
    origin: float | None = None
    last: float | None = None
    for _, _, sp in scan.points():
        moved = [a for a in scan.axes if prev.get(a.device) != sp[a.device]]
        for stage in sorted({a.stage for a in moved}):
            these = [a for a in moved if a.stage == stage]
            if any(a.device == tm.STEP_ID for a in these):
                v = sp[tm.STEP_ID]
                if origin is None or v == 0 or (last is not None and v < last):
                    origin = t - v
                else:
                    t = max(t, origin + v)
                last = v
            t += max(a.settle_s for a in these)
        t += read_s
        prev = sp
    return t


def _setter(ch: Channel, dev: Any) -> Callable[[float], Any]:
    pid = ch.id[len(ch.instrument) + 1:]
    defn = _definition(dev)
    if defn is not None and pid in defn.properties and defn.properties[pid].set:
        p = defn.properties[pid]
        cast = int if p.kind == "int" else float

        async def set_prop(v: float) -> None:
            await defs.write(defn, dev, pid, cast(round(v) if p.kind == "int" else v))
        return set_prop

    async def set_main(v: float) -> None:
        r = dev.set(v)
        if inspect.isawaitable(r):
            await r
    return set_main


class StepTarget:
    def __init__(self, ch: Channel, dev: Any):
        self.name, self.channel = ch.id, ch
        lo, hi = ch.limits
        self.limits = (-math.inf if lo is None else lo, math.inf if hi is None else hi)
        self._set = _setter(ch, dev)

    async def set(self, value: float) -> None:
        await self._set(value)

    async def read(self) -> dict:
        return {}

    def describe(self) -> dict:
        return {}


class LogTarget:
    def __init__(self, name: str, dev: Any, keys: list[str]):
        self.name, self.dev, self.keys = name, dev, keys
        defn = _definition(dev)
        self._read_if = (defn.properties[defn.read_if]
                         if defn is not None and defn.read_if else None)

    def describe(self) -> dict[str, FieldInfo]:
        full = _describe(self.dev)
        return {k: full[k] for k in self.keys if k in full}

    async def read(self) -> dict[str, Any]:
        if self._read_if is not None and not await defs.get_value(self.dev, self._read_if):
            return {k: math.nan for k in self.keys}
        data = await self.dev.read()
        return {k: data.get(k, math.nan) for k in self.keys}


class InstrumentRef:
    def __init__(self, dev: Any, safe: bool):
        self.dev = dev
        if hasattr(dev, "snapshot"):
            self.snapshot = dev.snapshot
        if safe and hasattr(dev, "safe_state"):
            self.safe_state = dev.safe_state


def log_key(instrument: str) -> str:
    return f"{instrument} (log)"


def build(cfg: MeasurementConfig, devices: Mapping[str, Any],
          register: Register | None = None,
          intervals: Mapping[str, float | None] | None = None,
          ) -> tuple[Scan, dict[str, Any]]:
    """The Scan and the runner's devices for `cfg` on the connected `devices`.

    `register` and `intervals` (each panel's polling interval, None when not
    polling) decide which logged instruments are sampled from the server's
    register (log_sources); the decision is in the scan's metadata
    ("log_sources"). Raises PlanError for an unknown or disconnected channel, a
    channel stepped twice, a bad step item, nothing to step, or an instrument
    to sample whose panel isn't polling."""
    if tm.NAME in devices and not isinstance(devices[tm.NAME], tm.Timer):
        raise PlanError(f"an instrument is named {tm.NAME!r}, the built-in timer's name: "
                        "rename it in the Instrument Server")
    devices = {**devices, tm.NAME: tm.Timer()}           # a fresh timer for every run
    cat = catalog(devices)
    for ch in TIMER_CHANNELS:
        cat[ch.id] = ch
    if not cfg.step:
        raise PlanError("the step sequence is empty: add a channel (one value is enough "
                        "for a single point)")
    for sc in cfg.step:
        ch = cat.get(sc.channel)
        if ch is None or ch.kind != "step":
            raise PlanError(f"step channel {sc.channel!r}: not a settable channel of a "
                            "connected instrument")
    for key in cfg.log:
        if key not in cat or cat[key].kind != "log":
            raise PlanError(f"log channel {key!r}: not a reading of a connected instrument")
    intervals = dict(intervals or {})
    sources = log_sources(cfg, cat, intervals)
    for inst, how in sources.items():
        if how != "register":
            continue
        if register is None:
            raise PlanError(f"{inst}: sampling the server's register needs the Instrument "
                            "Server")
        if intervals.get(inst) is None:
            raise PlanError(f"{inst}: its panel isn't polling (Live is off), so the "
                            "register has nothing to sample: turn Live on, or read it "
                            "directly")

    def axis(sc) -> Axis:
        try:
            values = sc.values()
        except ValueError as e:
            raise PlanError(f"{sc.channel}: {e}") from None
        return Axis(sc.channel, values, sc.settle_s, sc.snake,
                    stage=1 if sc.channel == tm.STEP_ID else 0)
    # set-once channels first (outermost), then the loops, outermost first
    single = [axis(sc) for sc in cfg.step if sc.single]
    loops = [axis(sc) for sc in reversed(cfg.step) if not sc.single]

    run_devices: dict[str, Any] = {}
    used: list[str] = []
    for sc in cfg.step:
        ch = cat[sc.channel]
        run_devices[sc.channel] = StepTarget(ch, devices[ch.instrument])
        used.append(ch.instrument)
    by_inst: dict[str, list[str]] = {}
    for key in cfg.log:
        by_inst.setdefault(cat[key].instrument, []).append(key)
    readables = []
    for inst, keys in by_inst.items():
        dev = devices[inst]
        if sources.get(inst) == "register":
            assert register is not None
            run_devices[log_key(inst)] = RegisterTarget(inst, register, keys,
                                                        _describe(dev))
        else:
            run_devices[log_key(inst)] = LogTarget(inst, dev, keys)
        readables.append(log_key(inst))
        used.append(inst)
    for inst in dict.fromkeys(used):
        run_devices[inst] = InstrumentRef(devices[inst], cfg.after == "safe")

    scan = Scan(tuple(single + loops), tuple(readables),
                metadata={"name": cfg.name, "comment": cfg.comment,
                          "step_order": [sc.channel for sc in cfg.step],
                          "log_sources": sources,
                          "server_intervals_s": {i: intervals.get(i) for i in sources}})
    scan.validate(run_devices)
    return scan, run_devices
