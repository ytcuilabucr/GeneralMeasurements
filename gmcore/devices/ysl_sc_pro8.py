"""YSL Photonics SC-PRO8 supercontinuum laser, on a serial port (9600 8N1).

Source: the vendor's "YSL-SC-PRO8" (SC_PRO8.py, SerialPort.py). Commands are
ASCII ending in CR; replies are CR-separated lines, some starting with CR:

    LA1 / LA0               emission on / off   -> CR OK CR (OK CR)
    Cu<nnn>                 power: nnn = % · 6, 000-600   -> CR OK CR
    fr0<nnnn>0              repetition rate, nnnn = MHz · 100   -> OK CR
    STATE?                  FAULT / READY / Warm Up / Laser On
    Er?                     Err00 = no alarm; Err01 … / ERR11 … alarms

The vendor's set_freq accepts 0.01, 0.1, 0.5, 1, 4, 5, 10, 20, 40, 80 MHz
(its docstring also lists 0.2 MHz, which its table lacks: not offered here).

**Readings.** State and alarm are queried (read() polls STATE?). Power and
repetition rate cannot be queried: they read what the driver last set, and are
unknown after connect. Emission can only be switched on once a power has been
set in this session, so the laser never starts at a current nobody chose.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .definitions import describe
from .protocols import FieldInfo
from .transport import DriverError, SerialBytesTransport

STATES = ("FAULT", "READY", "Warm Up", "Laser On")
# repetition rate (MHz) -> the vendor's four digits
RATES_MHZ = (0.01, 0.1, 0.5, 1.0, 4.0, 5.0, 10.0, 20.0, 40.0, 80.0)
POWER_STEPS = 6                       # "Cu" takes % · 6, 0-600


def _lines(reply: bytes) -> list[str]:
    return [x.strip() for x in re.split(r"[\r\n]+", reply.decode("ascii", "replace"))
            if x.strip()]


class YslScPro8:
    DEFINITION = "ysl_sc_pro8"

    def __init__(self, name: str, transport: SerialBytesTransport, max_power: float = 100.0):
        self.name, self.t = name, transport
        self.max_power = float(max_power)
        if not 0 < self.max_power <= 100:
            raise ValueError(f"max_power {max_power}: 0 < max_power ≤ 100 %")
        self.power: float | None = None                    # %, as last set
        self.rate_mhz: float | None = None                 # as last set
        self.last_state: str | None = None
        self.rates = list(RATES_MHZ)

    # -- exchange -------------------------------------------------------------------------
    async def _ask(self, cmd: str) -> list[str]:
        return _lines(await self.t.exchange(cmd.encode("ascii") + b"\r"))

    async def _ok(self, cmd: str) -> None:
        lines = await self._ask(cmd)
        if not lines or any(x != "OK" for x in lines):
            raise DriverError(f"{self.name}: {cmd!r} answered {lines or 'nothing'}"
                              f"{await self._why()}")

    async def _why(self) -> str:
        """The alarm and state, to explain a refused command ('' if they don't come)."""
        try:
            return f" (state {await self.state()}, alarm {await self.alarm()})"
        except (DriverError, TimeoutError):
            return ""

    # -- queries --------------------------------------------------------------------------
    async def state(self) -> str:
        lines = await self._ask("STATE?")
        if not lines:
            raise TimeoutError(f"{self.name}: no reply to STATE?")
        self.last_state = lines[-1]
        return self.last_state

    async def alarm(self) -> str:
        """'' for no alarm (Err00), else the code, e.g. 'Err01', 'ERR21'."""
        lines = await self._ask("Er?")
        if not lines:
            raise TimeoutError(f"{self.name}: no reply to Er?")
        code = lines[-1]
        return "" if code.upper() == "ERR00" else code

    async def emission_on(self) -> bool:
        return await self.state() == "Laser On"

    # -- settings -------------------------------------------------------------------------
    async def set_emission(self, on: bool) -> None:
        if not on:
            await self._ok("LA0")
            return
        if self.power is None:
            raise DriverError(f"{self.name}: set Power first (the laser's present power "
                              "cannot be read back)")
        await self._ok("LA1")

    async def set_power(self, power: float) -> None:
        if not 0 <= power <= self.max_power:
            raise DriverError(f"{self.name}: power {power:g} % outside 0-{self.max_power:g} %")
        value = round(power * POWER_STEPS)
        await self._ok(f"Cu{value:03d}")
        self.power = value / POWER_STEPS

    async def set_rate(self, mhz: float) -> None:
        match = [r for r in RATES_MHZ if math.isclose(r, mhz)]
        if not match:
            raise DriverError(f"{self.name}: repetition rate {mhz:g} MHz is not one of "
                              f"{', '.join(f'{r:g}' for r in RATES_MHZ)} MHz")
        await self._ok(f"fr0{round(match[0] * 100):04d}0")
        self.rate_mhz = match[0]

    # -- contract -------------------------------------------------------------------------
    def describe(self) -> dict[str, FieldInfo]:
        return describe(self)

    async def read(self) -> dict[str, float]:
        """STATE? from the laser; power and rate as last set (NaN while unknown)."""
        state = await self.state()
        nan = math.nan
        return {f"{self.name}.emission": float(state == "Laser On"),
                f"{self.name}.fault": float(state == "FAULT"),
                f"{self.name}.power": nan if self.power is None else self.power,
                f"{self.name}.rate": nan if self.rate_mhz is None else self.rate_mhz}

    async def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {"port": self.t.resource, "max_power": self.max_power,
                               "power": self.power, "rate_mhz": self.rate_mhz}
        try:
            out["state"], out["alarm"] = await self.state(), await self.alarm()
        except (DriverError, TimeoutError) as e:
            out["state"] = f"? ({e})"
        return out

    async def safe_state(self) -> None:
        """Emission off."""
        await self._ok("LA0")
