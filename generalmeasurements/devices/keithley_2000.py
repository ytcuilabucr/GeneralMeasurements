"""Keithley 2000 (6.5-digit DMM), Readable only."""

from __future__ import annotations

from .definitions import describe
from .protocols import FieldInfo
from .transport import DriverError, Transport

_FUNCS = {"volt": ("VOLT:DC", "V"), "curr": ("CURR:DC", "A"), "res": ("RES", "Ohm"),
          "fres": ("FRES", "Ohm")}


class Keithley2000:
    """Readable. The function is the mode (defs/keithley_2000_<function>.toml)."""

    DEFINITION = "keithley_2000"

    def __init__(self, name: str, transport: Transport, function: str = "volt",
                 nplc: float = 1.0, range_: float | None = None):
        if function not in _FUNCS:
            raise ValueError(f"function must be one of {sorted(_FUNCS)}")
        self.name, self.t, self.function = name, transport, function
        self.nplc, self.range = nplc, range_

    def describe(self) -> dict[str, FieldInfo]:
        return describe(self)

    async def configure(self) -> None:
        scpi = _FUNCS[self.function][0]
        await self.t.write("*CLS")
        # Front-panel use leaves continuous triggering on; then every :READ?
        # logs -213 "Init ignored" (seen on hardware).
        await self.t.write(":INIT:CONT OFF")
        await self.t.write(f":SENS:FUNC '{scpi}'")
        await self.t.write(f":SENS:{scpi}:NPLC {self.nplc:g}")
        if self.range is None:
            await self.t.write(f":SENS:{scpi}:RANG:AUTO ON")
        else:
            await self.t.write(f":SENS:{scpi}:RANG {self.range:g}")
        errs = await self.errors()
        if errs:
            raise DriverError(f"{self.name}: configure rejected: {'; '.join(errs)}")

    async def sync(self) -> str:
        """Adopt the instrument's present function (query only); returns it."""
        scpi = (await self.t.query(":SENS:FUNC?")).strip().strip('"')
        for key, (name, _) in _FUNCS.items():
            if name == scpi:
                self.function = key
        return scpi

    async def switch_mode(self, function: str) -> None:
        """Measure another function (autorange): configure() with it."""
        if function not in _FUNCS:
            raise DriverError(f"{self.name}: function must be one of {sorted(_FUNCS)}")
        self.function, self.range = function, None
        await self.configure()

    async def set_nplc(self, nplc: float) -> None:
        if not 0.01 <= nplc <= 10:
            raise DriverError(f"{self.name}: NPLC {nplc} outside 0.01-10")
        await self._write_checked(f":SENS:{_FUNCS[self.function][0]}:NPLC {nplc:g}")
        self.nplc = nplc

    async def set_range(self, range_: float | None) -> None:
        """A full-scale range in the function's unit, or None for autorange."""
        scpi = _FUNCS[self.function][0]
        await self._write_checked(f":SENS:{scpi}:RANG:AUTO ON" if range_ is None
                                  else f":SENS:{scpi}:RANG {range_:g}")
        self.range = range_

    async def _write_checked(self, cmd: str) -> None:
        await self.t.write(cmd)
        errs = await self.errors()
        if errs:
            raise DriverError(f"{self.name}: {cmd!r} rejected: {'; '.join(errs)}")

    async def errors(self) -> list[str]:
        """Drain the SCPI error queue; [] when clean."""
        out: list[str] = []
        for _ in range(20):
            e = await self.t.query(":SYST:ERR?")
            if e.split(",")[0].strip() in ("0", "+0"):
                break
            out.append(e)
        return out

    async def read(self) -> dict[str, float]:
        # :FETC? needs an init; :READ? triggers a fresh reading
        raw = await self.t.query(":READ?")
        try:
            return {self.name: float(raw.split(",")[0].rstrip("VDCAOHM "))}
        except ValueError:
            raise DriverError(f"{self.name}: bad reading {raw!r}") from None

    async def snapshot(self) -> dict:
        return {"idn": await self.t.query("*IDN?"), "function": self.function,
                "nplc": self.nplc, "range": self.range}

    async def safe_state(self) -> None:
        pass  # input-only instrument
