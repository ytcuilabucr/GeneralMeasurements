"""Cryomagnetics LM-510 liquid cryogen level monitor, as a Readable.

The reading is one channel's level, in the meter's own units (in, cm or %).
In Sample/Hold mode the meter measures on its own interval, or when asked
(`measure()`); each measurement heats the sensor and boils a little helium, so
read() only fetches the last value and never starts one.

The lab's meter is on a serial-to-LAN adapter (TCPIP SOCKET, single client:
keep one session open, don't reconnect in a loop). On RS-232 the meter echoes
each command line before replying; the transport's `echo` skips it.

MEAS takes the channel; MODE, INTVL, UNITS, LOW/HIGH and LNGTH act on the
channel selected on the meter (CHAN), which the driver does not change.

Commands are from the LM-510 manual and the legacy LabVIEW monitor
(LabviewLegacy/HeliumMonitor.vi: Initialize, Sensor Reading, Get/Set Mode).
Not yet verified on hardware: the meter has not answered over the network.
"""

from __future__ import annotations

import math
import re

from .definitions import describe
from .protocols import FieldInfo
from .transport import DriverError, Transport

UNITS = {"in": "in", "cm": "cm", "percent": "%", "%": "%"}
MODES = {"S": "sample/hold", "C": "continuous"}
_LEVEL = re.compile(r"^\s*([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)\s*(\S*)")


class LM510:
    """Readable + Configurable. `channel` is the sensor input (1 or 2)."""

    DEFINITION = "lm510"

    def __init__(self, name: str, transport: Transport, channel: int = 1, units: str = "in"):
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        if units not in ("in", "cm", "%"):
            raise ValueError("units must be 'in', 'cm' or '%'")
        self.name, self.t, self.channel, self.units = name, transport, channel, units

    # -- contract ---------------------------------------------------------
    def describe(self) -> dict[str, FieldInfo]:
        return describe(self)

    async def configure(self) -> None:
        """Check, don't change: model, and that the meter reports in our units."""
        idn = await self.t.query("*IDN?")
        if "LM-510" not in idn.upper().replace("LM510", "LM-510"):
            raise DriverError(f"{self.name}: expected a Cryomagnetics LM-510, got {idn!r}")
        units = await self.meter_units()
        if units != self.units:
            raise DriverError(f"{self.name}: the meter reports in {units!r}, the station "
                              f"expects {self.units!r}; change UNITS on the meter or station")

    async def read(self) -> dict[str, float]:
        """The channel's last measured level (does not start a measurement)."""
        return {self.name: await self.level()}

    async def safe_state(self) -> None:
        """Nothing to make safe: a level meter only measures."""

    # -- meter ------------------------------------------------------------
    async def level(self) -> float:
        raw = await self.t.query(f"MEAS? {self.channel}")
        m = _LEVEL.match(raw)
        if not m:
            raise DriverError(f"{self.name}: bad MEAS reply {raw!r}")
        unit = UNITS.get(m.group(2).lower(), m.group(2))
        if unit and unit != self.units:
            raise DriverError(f"{self.name}: level reported in {unit!r}, expected "
                              f"{self.units!r}")
        return float(m.group(1))

    async def measure(self) -> None:
        """Start one measurement now (Sample/Hold mode). Heats the sensor."""
        await self.t.write(f"MEAS {self.channel}")

    async def mode(self) -> str:
        """'sample/hold' or 'continuous' (or the raw reply)."""
        raw = (await self.t.query("MODE?")).strip()
        return MODES.get(raw[:1].upper(), raw)

    async def set_mode(self, mode: str) -> None:
        code = {v: k for k, v in MODES.items()}.get(mode)
        if code is None:
            raise DriverError(f"{self.name}: mode must be one of {list(MODES.values())}")
        await self.t.write(f"MODE {code}")

    async def interval_s(self) -> float:
        """Sample/Hold measurement interval, from 'hh:mm:ss'."""
        raw = (await self.t.query("INTVL?")).strip()
        try:
            h, m, s = (int(x) for x in raw.split(":"))
        except ValueError:
            raise DriverError(f"{self.name}: bad INTVL reply {raw!r}") from None
        return 3600 * h + 60 * m + s

    async def set_interval(self, seconds: float) -> None:
        s = round(seconds)
        if not 0 < s < 100 * 3600:
            raise DriverError(f"{self.name}: interval {seconds} s out of range")
        await self.t.write(f"INTVL {s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}")

    async def meter_units(self) -> str:
        raw = (await self.t.query("UNITS?")).strip().lower()
        return UNITS.get(raw, raw)

    async def alarms(self) -> tuple[float, float]:
        """(low, high) alarm/fill thresholds in the meter's units; NaN if unreadable."""
        out = []
        for q in ("LOW?", "HIGH?"):
            m = _LEVEL.match(await self.t.query(q))
            out.append(float(m.group(1)) if m else math.nan)
        return out[0], out[1]

    async def snapshot(self) -> dict:
        low, high = await self.alarms()
        return {"idn": await self.t.query("*IDN?"), "channel": self.channel,
                "units": self.units, "mode": await self.mode(),
                "interval_s": await self.interval_s(),
                "length": await self.t.query("LNGTH?"),
                "low": low, "high": high}
