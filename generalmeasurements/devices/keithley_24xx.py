"""Keithley 2400 / 2401 / 2410 / 2450 source-measure units (SCPI).

Source V or I, always measure both V and I. Ramp rate is enforced inside set().
The 2450 is driven in its SCPI (2400-emulation) command-set mode; the few
syntax differences live in `_Dialect`. Command strings are from memory of the
manuals: verify against hardware / golden transcripts before trusting them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .definitions import describe
from .protocols import FieldInfo
from .transport import DriverError, Transport


@dataclass(frozen=True)
class _Dialect:
    model: str
    max_v: float
    max_i: float
    limit_cmd: dict[str, str]   # source mode -> compliance command prefix
    trip_cmd: dict[str, str]    # source mode -> "compliance tripped?" query
    legacy_format: bool         # 2400-family needs :FORM:ELEM to pick V,I


_TRIP_LEGACY = {"volt": ":SENS:CURR:PROT:TRIP?", "curr": ":SENS:VOLT:PROT:TRIP?"}

_DIALECTS = {
    "2400": _Dialect("2400", 210.0, 1.05,
                     {"volt": ":SENS:CURR:PROT", "curr": ":SENS:VOLT:PROT"},
                     _TRIP_LEGACY, True),
    "2401": _Dialect("2401", 20.0, 1.05,
                     {"volt": ":SENS:CURR:PROT", "curr": ":SENS:VOLT:PROT"},
                     _TRIP_LEGACY, True),
    "2410": _Dialect("2410", 1100.0, 1.05,
                     {"volt": ":SENS:CURR:PROT", "curr": ":SENS:VOLT:PROT"},
                     _TRIP_LEGACY, True),
    "2450": _Dialect("2450", 200.0, 1.05,
                     {"volt": ":SOUR:VOLT:ILIM", "curr": ":SOUR:CURR:VLIM"},
                     {"volt": ":SOUR:VOLT:ILIM:TRIP?", "curr": ":SOUR:CURR:VLIM:TRIP?"},
                     False),
}


class Keithley24xx:
    """Movable + Readable + Configurable. `mode` is the sourced quantity; the
    properties are in defs/keithley_24xx*.toml, one file per mode."""

    DEFINITION = "keithley_24xx"

    def __init__(self, name: str, transport: Transport, model: str = "2400",
                 mode: str = "volt", compliance: float = 1e-6,
                 limits: tuple[float, float] = (-1.0, 1.0),
                 max_rate: float = 0.5, nplc: float = 1.0,
                 settle_s: float = 0.05, step_dt: float = 0.05,
                 check_compliance: bool = True):
        if model not in _DIALECTS:
            raise ValueError(f"unsupported model {model!r}")
        self.d = _DIALECTS[model]
        self.name, self.t = name, transport
        self.nplc = nplc
        self.settle_s, self.step_dt = settle_s, step_dt
        self.check_compliance = check_compliance
        self._level = 0.0
        self._output = False      # belief, like _level; set by output()
        self._set_mode(mode, compliance, limits, max_rate)

    def _set_mode(self, mode: str, compliance: float, limits: tuple[float, float],
                  max_rate: float) -> None:
        """Validate and adopt a mode's settings (no I/O)."""
        if mode not in ("volt", "curr"):
            raise ValueError("mode must be 'volt' or 'curr'")
        limits = (float(limits[0]), float(limits[1]))
        hard = self.d.max_v if mode == "volt" else self.d.max_i
        if limits[0] > limits[1] or max(abs(limits[0]), abs(limits[1])) > hard:
            raise ValueError(f"limits {limits} outside {self.d.model} range ±{hard}")
        if max_rate <= 0:
            raise ValueError("max_rate must be > 0")
        self.mode, self.compliance, self.limits, self.max_rate = (
            mode, compliance, limits, max_rate)
        self._src = "VOLT" if mode == "volt" else "CURR"
        self._src_units = "V" if mode == "volt" else "A"

    # -- contract ---------------------------------------------------------
    def describe(self) -> dict[str, FieldInfo]:
        return describe(self)

    async def configure(self) -> None:
        """Put the instrument in a known state; output stays OFF."""
        t = self.t
        await t.write("*RST")
        await t.write("*CLS")
        await t.write(f":SOUR:FUNC {self._src}")
        await t.write(f":SOUR:{self._src}:MODE FIX")
        await t.write(f":SOUR:{self._src}:LEV 0")
        other = "CURR" if self.mode == "volt" else "VOLT"
        await t.write(f"{self.d.limit_cmd[self.mode]} {self.compliance:g}")
        await t.write(':SENS:FUNC "VOLT","CURR"' if self.d.legacy_format
                      else f':SENS:FUNC "{other}"')
        await t.write(f":SENS:{other}:NPLC {self.nplc:g}")
        if self.d.legacy_format:
            await t.write(":FORM:ELEM VOLT,CURR")
        errs = await self.errors()
        if errs:
            raise DriverError(f"{self.name}: configure rejected: {'; '.join(errs)}")

    async def errors(self) -> list[str]:
        """Drain the SCPI error queue; [] when clean."""
        out: list[str] = []
        for _ in range(20):
            e = await self.t.query(":SYST:ERR?")
            if e.split(",")[0].strip() in ("0", "+0"):
                break
            out.append(e)
        return out

    async def sync(self) -> str:
        """Adopt the instrument's present output state and source level (queries
        only), so set() ramps from where the unit really is. For attaching to a
        running instrument without configure(). Returns its source function;
        if that isn't this driver's mode, the level is not adopted."""
        func = (await self.t.query(":SOUR:FUNC?")).strip()
        self._output = (await self.t.query(":OUTP?")).strip() == "1"
        if func.startswith(self._src):
            self._level = float(await self.t.query(f":SOUR:{self._src}:LEV?"))
        return func

    async def active_mode(self) -> str:
        """The unit's present source function: 'volt', 'curr' or the raw reply."""
        func = (await self.t.query(":SOUR:FUNC?")).strip()
        return {"VOLT": "volt", "CURR": "curr"}.get(func[:4].upper(), func)

    async def switch_mode(self, mode: str, compliance: float, limits: tuple[float, float],
                          max_rate: float) -> None:
        """Change the source function: ramp to 0 and output OFF in the old mode,
        then configure() (*RST) in the new one. Settings are checked first."""
        old = (self.mode, self.compliance, self.limits, self.max_rate)
        self._set_mode(mode, compliance, limits, max_rate)     # raises before any I/O
        new = (self.mode, self.compliance, self.limits, self.max_rate)
        self._set_mode(*old)
        await self.safe_state()
        self._set_mode(*new)
        self._level = 0.0
        await self.configure()

    @property
    def output_on(self) -> bool:
        return self._output

    @property
    def programmed_level(self) -> float:
        """The level last sent to (or adopted from) the unit."""
        return self._level

    async def prepare(self) -> None:
        """Called by the runner before the first move: output ON at level 0."""
        await self.t.write(f":SOUR:{self._src}:LEV 0")
        self._level = 0.0
        await self.output(True)

    async def output(self, on: bool) -> None:
        await self.t.write(f":OUTP {'ON' if on else 'OFF'}")
        self._output = on

    async def set_output(self, on: bool) -> None:
        """ON: level 0, then output on (prepare). OFF: ramp to 0, then off (safe_state)."""
        await (self.prepare() if on else self.safe_state())

    async def set(self, value: float) -> None:
        lo, hi = self.limits
        if not lo <= value <= hi:
            raise DriverError(f"{self.name}: {value} outside limits {self.limits}")
        step = self.max_rate * self.step_dt
        while abs(value - self._level) > step:
            self._level += step if value > self._level else -step
            await self.t.write(f":SOUR:{self._src}:LEV {self._level:.6g}")
            await asyncio.sleep(self.step_dt)
        self._level = value
        await self.t.write(f":SOUR:{self._src}:LEV {value:.6g}")
        await asyncio.sleep(self.settle_s)

    async def read(self) -> dict[str, float]:
        # With the output off the unit logs 803 and never replies: fail now
        # instead of after a VISA timeout (seen on a 2400 and a 2401).
        if not self._output:
            raise DriverError(f"{self.name}: output is off; read() needs prepare() first")
        # 2400 family: :FORM:ELEM makes :READ? return V,I. The 2450 returns only
        # the sensed value unless asked for the source readback element too.
        cmd = ":READ?" if self.d.legacy_format else ':READ? "defbuffer1", SOUR, READ'
        raw = await self.t.query(cmd)
        parts = raw.split(",")
        if len(parts) < 2:
            raise DriverError(f"{self.name}: bad reading {raw!r}")
        try:
            a, b = float(parts[0]), float(parts[1])
        except ValueError:
            raise DriverError(f"{self.name}: bad reading {raw!r}") from None
        if self.d.legacy_format or self.mode == "volt":
            v, i = a, b      # legacy: VOLT,CURR; 2450 volt mode: source V, sensed I
        else:
            v, i = b, a      # 2450 curr mode: source I, sensed V
        out = {f"{self.name}.v": v, f"{self.name}.i": i}
        if self.check_compliance:
            trip = await self.t.query(self.d.trip_cmd[self.mode])
            out[f"{self.name}.compliance"] = 1.0 if trip.strip() not in ("0", "+0") else 0.0
        return out

    async def snapshot(self) -> dict:
        return {"idn": await self.t.query("*IDN?"), "model": self.d.model,
                "mode": self.mode, "compliance": self.compliance,
                "limits": list(self.limits), "max_rate": self.max_rate,
                "nplc": self.nplc, "level": self._level}

    async def safe_state(self) -> None:
        """Ramp to zero at max_rate, then output OFF."""
        await self.set(0.0 if self.limits[0] <= 0 <= self.limits[1] else self._level)
        await self.output(False)
