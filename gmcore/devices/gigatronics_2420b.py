"""Giga-tronics 2420B microwave signal generator, as a Movable on frequency or power.

`axis = "freq"` sweeps the CW frequency (Hz) at a fixed `power_dbm`;
`axis = "power"` sweeps the level (dBm) at a fixed `freq_hz`. RF is OFF after
configure() and in safe_state(); prepare() turns it on. Every set() is
followed by SYST:ERR? so a rejected value fails loudly instead of silently.

Query replies (FREQ?, POW?, OUTP?, SYST:ERR?) were checked against a real
2420B (fw V4.33b); the write syntax is SCPI-style from the manual and not yet
verified on hardware.
"""

from __future__ import annotations

import asyncio

from .definitions import describe
from .protocols import FieldInfo
from .transport import DriverError, Transport

# 2420 frequency span from the Giga-tronics 2400 Series (2420L/M) datasheet; no
# 2420B manual was available. Power is left to the instrument (it reports a
# rejected level through SYST:ERR?) below max_power_dbm, which the station sets.
FREQ_MIN, FREQ_MAX = 10e6, 20e9


class Gigatronics2420B:
    """Movable + Configurable. The axis is the mode (defs/gigatronics_2420b_<axis>.toml)."""

    DEFINITION = "gigatronics_2420b"

    def __init__(self, name: str, transport: Transport, axis: str = "freq",
                 limits: tuple[float, float] = (1e9, 2e10), power_dbm: float | None = None,
                 freq_hz: float | None = None, max_power_dbm: float = 0.0,
                 settle_s: float = 0.05):
        if axis not in ("freq", "power"):
            raise ValueError("axis must be 'freq' or 'power'")
        if limits[0] > limits[1]:
            raise ValueError(f"bad limits {limits}")
        span = limits if axis == "freq" else ((freq_hz,) * 2 if freq_hz is not None else None)
        if span and not FREQ_MIN <= span[0] <= span[1] <= FREQ_MAX:
            raise ValueError(f"frequency {span} outside the 2420's {FREQ_MIN:g} … "
                             f"{FREQ_MAX:g} Hz")
        if axis == "power" and limits[1] > max_power_dbm:
            raise ValueError(f"power limits {limits} exceed max_power_dbm {max_power_dbm}")
        if power_dbm is not None and power_dbm > max_power_dbm:
            raise ValueError(f"power_dbm {power_dbm} exceeds max_power_dbm {max_power_dbm}")
        self.name, self.t, self.axis, self.limits = name, transport, axis, limits
        self.power_dbm, self.freq_hz, self.max_power_dbm = power_dbm, freq_hz, max_power_dbm
        self.settle_s = settle_s

    # -- contract ---------------------------------------------------------
    def describe(self) -> dict[str, FieldInfo]:
        return describe(self)

    async def configure(self) -> None:
        """RF off, then the fixed (non-swept) quantity. Power is checked against
        max_power_dbm before RF can ever turn on."""
        idn = await self.t.query("*IDN?")
        if "2420" not in idn:
            raise DriverError(f"{self.name}: expected a Giga-tronics 2420B, got {idn!r}")
        await self.output(False)
        if self.axis == "freq" and self.power_dbm is not None:
            await self._write_checked(f"POW {self.power_dbm:.2f}")
        if self.axis == "power" and self.freq_hz is not None:
            await self._write_checked(f"FREQ {self.freq_hz:.1f}")
        power = _float(self.name, await self.t.query("POW?"))
        if self.axis == "freq" and power > self.max_power_dbm:
            raise DriverError(f"{self.name}: level is {power} dBm, above max_power_dbm "
                              f"{self.max_power_dbm}; set power_dbm in the station")

    async def prepare(self) -> None:
        await self.output(True)

    async def output(self, on: bool) -> None:
        await self._write_checked(f"OUTP {'ON' if on else 'OFF'}")

    async def output_state(self) -> bool:
        return (await self.t.query("OUTP?")).strip() in ("1", "ON")

    async def set(self, value: float) -> None:
        await (self.set_freq(value) if self.axis == "freq" else self.set_power(value))
        await asyncio.sleep(self.settle_s)

    async def set_freq(self, hz: float) -> None:
        if self.axis == "freq" and not self.limits[0] <= hz <= self.limits[1]:
            raise DriverError(f"{self.name}: {hz} Hz outside limits {self.limits}")
        if not FREQ_MIN <= hz <= FREQ_MAX:
            raise DriverError(f"{self.name}: {hz:g} Hz outside the 2420's {FREQ_MIN:g} … "
                              f"{FREQ_MAX:g} Hz")
        await self._write_checked(f"FREQ {hz:.1f}")

    async def set_power(self, dbm: float) -> None:
        if dbm > self.max_power_dbm:
            raise DriverError(f"{self.name}: {dbm} dBm above max_power_dbm {self.max_power_dbm}")
        if self.axis == "power" and not self.limits[0] <= dbm <= self.limits[1]:
            raise DriverError(f"{self.name}: {dbm} dBm outside limits {self.limits}")
        await self._write_checked(f"POW {dbm:.2f}")

    async def read(self) -> dict[str, float]:
        n = self.name
        return {f"{n}.freq": _float(n, await self.t.query("FREQ?")),
                f"{n}.power": _float(n, await self.t.query("POW?"))}

    async def snapshot(self) -> dict:
        t = self.t
        return {"idn": await t.query("*IDN?"), "freq": await t.query("FREQ?"),
                "power": await t.query("POW?"), "output": await t.query("OUTP?"),
                "axis": self.axis, "limits": list(self.limits),
                "max_power_dbm": self.max_power_dbm}

    async def safe_state(self) -> None:
        await self.t.write("OUTP OFF")

    async def errors(self) -> list[str]:
        """Drain the error queue; [] when clean."""
        out: list[str] = []
        for _ in range(20):
            e = (await self.t.query("SYST:ERR?")).strip()
            if e.split(",")[0].strip() in ("0", "+0"):
                break
            out.append(e)
        return out

    async def _write_checked(self, cmd: str) -> None:
        await self.t.write(cmd)
        errs = await self.errors()
        if errs:
            raise DriverError(f"{self.name}: {cmd!r} rejected: {'; '.join(errs)}")


def _float(name: str, raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise DriverError(f"{name}: bad reply {raw!r}") from None
