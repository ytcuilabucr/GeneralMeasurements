"""Keithley 2000 (6.5-digit DMM), Readable only."""

from __future__ import annotations

from .protocols import FieldInfo
from .transport import Transport

_FUNCS = {"volt": ("VOLT:DC", "V"), "curr": ("CURR:DC", "A"), "res": ("RES", "Ohm"),
          "fres": ("FRES", "Ohm")}


class Keithley2000:
    def __init__(self, name: str, transport: Transport, function: str = "volt",
                 nplc: float = 1.0, range_: float | None = None):
        if function not in _FUNCS:
            raise ValueError(f"function must be one of {sorted(_FUNCS)}")
        self.name, self.t, self.function = name, transport, function
        self.nplc, self.range = nplc, range_

    def describe(self) -> dict[str, FieldInfo]:
        return {self.name: FieldInfo(_FUNCS[self.function][1], self.name)}

    async def configure(self) -> None:
        scpi = _FUNCS[self.function][0]
        await self.t.write(f":SENS:FUNC '{scpi}'")
        await self.t.write(f":SENS:{scpi}:NPLC {self.nplc:g}")
        if self.range is None:
            await self.t.write(f":SENS:{scpi}:RANG:AUTO ON")
        else:
            await self.t.write(f":SENS:{scpi}:RANG {self.range:g}")

    async def read(self) -> dict[str, float]:
        # :FETC? needs an init; :READ? triggers a fresh reading
        return {self.name: float((await self.t.query(":READ?")).split(",")[0].rstrip("VDCAOHM "))}

    async def snapshot(self) -> dict:
        return {"idn": await self.t.query("*IDN?"), "function": self.function,
                "nplc": self.nplc, "range": self.range}

    async def safe_state(self) -> None:
        pass  # input-only instrument
