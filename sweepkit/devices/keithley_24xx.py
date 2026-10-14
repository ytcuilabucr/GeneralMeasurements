"""Keithley 2400 / 2401 / 2410 / 2450 source-measure units (SCPI).

Source V or I, always measure both V and I. Ramp rate is enforced inside set().
The 2450 is driven in its SCPI (2400-emulation) command-set mode; the few
syntax differences live in `_Dialect`. Command strings are from memory of the
manuals: verify against hardware / golden transcripts before trusting them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .protocols import FieldInfo
from .transport import DriverError, Transport


@dataclass(frozen=True)
class _Dialect:
    model: str
    max_v: float
    max_i: float
    limit_cmd: dict[str, str]   # source mode -> compliance command prefix
    legacy_format: bool         # 2400-family needs :FORM:ELEM to pick V,I


_DIALECTS = {
    "2400": _Dialect("2400", 210.0, 1.05,
                     {"volt": ":SENS:CURR:PROT", "curr": ":SENS:VOLT:PROT"}, True),
    "2401": _Dialect("2401", 20.0, 1.05,
                     {"volt": ":SENS:CURR:PROT", "curr": ":SENS:VOLT:PROT"}, True),
    "2410": _Dialect("2410", 1100.0, 1.05,
                     {"volt": ":SENS:CURR:PROT", "curr": ":SENS:VOLT:PROT"}, True),
    "2450": _Dialect("2450", 200.0, 1.05,
                     {"volt": ":SOUR:VOLT:ILIM", "curr": ":SOUR:CURR:VLIM"}, False),
}


class Keithley24xx:
    """Movable + Readable + Configurable. `mode` is the sourced quantity."""

    def __init__(self, name: str, transport: Transport, model: str = "2400",
                 mode: str = "volt", compliance: float = 1e-6,
                 limits: tuple[float, float] = (-1.0, 1.0),
                 max_rate: float = 0.5, nplc: float = 1.0,
                 settle_s: float = 0.05, step_dt: float = 0.05):
        if model not in _DIALECTS:
            raise ValueError(f"unsupported model {model!r}")
        if mode not in ("volt", "curr"):
            raise ValueError("mode must be 'volt' or 'curr'")
        self.d = _DIALECTS[model]
        self.name, self.t, self.mode = name, transport, mode
        self.compliance, self.limits = compliance, limits
        self.max_rate, self.nplc = max_rate, nplc
        self.settle_s, self.step_dt = settle_s, step_dt
        self._level = 0.0
        hard = self.d.max_v if mode == "volt" else self.d.max_i
        if limits[0] > limits[1] or max(abs(limits[0]), abs(limits[1])) > hard:
            raise ValueError(f"limits {limits} outside {model} range ±{hard}")
        if max_rate <= 0:
            raise ValueError("max_rate must be > 0")
        self._src = "VOLT" if mode == "volt" else "CURR"
        self._src_units = "V" if mode == "volt" else "A"

    # -- contract ---------------------------------------------------------
    def describe(self) -> dict[str, FieldInfo]:
        return {f"{self.name}.v": FieldInfo("V", f"{self.name} voltage"),
                f"{self.name}.i": FieldInfo("A", f"{self.name} current")}

    async def configure(self) -> None:
        """Put the instrument in a known state; output stays OFF."""
        t = self.t
        await t.write("*RST")
        await t.write(f":SOUR:FUNC {self._src}")
        await t.write(f":SOUR:{self._src}:MODE FIX")
        await t.write(f":SOUR:{self._src}:LEV 0")
        other = "CURR" if self.mode == "volt" else "VOLT"
        await t.write(f"{self.d.limit_cmd[self.mode]} {self.compliance:g}")
        await t.write(f':SENS:FUNC "VOLT","CURR"' if self.d.legacy_format
                      else ':SENS:FUNC "CURR"')
        await t.write(f":SENS:{other}:NPLC {self.nplc:g}")
        if self.d.legacy_format:
            await t.write(":FORM:ELEM VOLT,CURR")

    async def output(self, on: bool) -> None:
        await self.t.write(f":OUTP {'ON' if on else 'OFF'}")

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
        raw = await self.t.query(":READ?")
        parts = raw.split(",")
        if len(parts) < 2:
            raise DriverError(f"{self.name}: bad reading {raw!r}")
        v, i = float(parts[0]), float(parts[1])
        return {f"{self.name}.v": v, f"{self.name}.i": i}

    async def snapshot(self) -> dict:
        return {"idn": await self.t.query("*IDN?"), "model": self.d.model,
                "mode": self.mode, "compliance": self.compliance,
                "limits": list(self.limits), "max_rate": self.max_rate,
                "nplc": self.nplc, "level": self._level}

    async def safe_state(self) -> None:
        """Ramp to zero at max_rate, then output OFF."""
        await self.set(0.0 if self.limits[0] <= 0 <= self.limits[1] else self._level)
        await self.output(False)
