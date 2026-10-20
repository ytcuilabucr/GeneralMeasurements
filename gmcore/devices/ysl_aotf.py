"""YSL Photonics AOTF RF driver, on a serial port (9600 8N1). One driver instance
controls one diffracted line: its wavelength and its power. Nothing else.

Sources: the vendor's "YSL-AOTF_Py with new driver" (Aotf_control.py,
SerialPort.py) and the C++ library it mirrors (CPP/YSLAotfLib/YslAotfLib.cpp).

Frames are binary:

    A5 5A 5A A5 | body | 5A A5 A5 5A | CRC-16 (high byte, low byte)

with the CRC over everything before it (poly 0xA001 reflected, start 0x0000,
as in the vendor's append_crc / get_crc). Bodies used here:

    01 FFFFFFFF PPPPPP      set one line: frequency word, power word
    40                      turn off (reply "OKOKOKOK", 8 bytes)

- frequency word: int(MHz / 500 · 2³²), big-endian, 20-200 MHz.
- power word: int(% · 10.23 · factor) + 4096, three bytes. The factor depends
  on how many lines are set at once; for one line it is 0.5 (power_factors).

The vendor's sweep commands (0x11, 0x13) and multi-line frames are not used.

**Wavelength.** The calibration is the vendor's cubic,
f [MHz] = a·λ³ + b·λ² + c·λ + d with λ in nm (Aotf_control.py gives a, b, c, d
for this unit). There are no configured wavelength limits: the usable range is
where the curve stays in the RF range (usable_range, worked out in __init__;
the curve must be monotonic there).

**Control only.** The controller has no query: a setting is answered at most
with an acknowledgement, and nothing can be read back. read() reports what
the driver set, a software-level reading. On connect the present state is
unknown (NaN) and nothing is written; the first wavelength set sends power 0
unless a power was set before.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from .definitions import describe
from .protocols import FieldInfo
from .transport import DriverError, SerialBytesTransport

HEAD = bytes([0xA5, 0x5A, 0x5A, 0xA5])
TAIL = bytes([0x5A, 0xA5, 0xA5, 0x5A])
OFF_REPLY = b"OK" * 4
# vendor power_factors[channel_count - 1][index]: one line set -> 0.5
ONE_LINE_FACTOR = 0.5
# the vendor's calibration for this unit (Aotf_control.py)
VENDOR_CAL = {"a": -2.09537e-7, "b": 6.67185e-4, "c": -0.76660, "d": 363.79358}


def crc16(data: bytes) -> int:
    crc = 0x0000
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def frame(body: bytes) -> bytes:
    """HEAD + body + TAIL + CRC (high, low)."""
    data = HEAD + body + TAIL
    crc = crc16(data)
    return data + bytes([crc >> 8, crc & 0xFF])


def freq_word(mhz: float) -> bytes:
    return int(mhz / 500 * 2**32).to_bytes(4, "big")


def power_word(power: float, factor: float = ONE_LINE_FACTOR) -> bytes:
    return (int(power * 10.23 * factor) + 4096).to_bytes(3, "big")


class YslAotf:
    DEFINITION = "ysl_aotf"
    F_RANGE_MHZ = (20.0, 200.0)        # vendor: "frequency range from 20 - 200Mhz"

    def __init__(self, name: str, transport: SerialBytesTransport,
                 calibration: dict[str, float] | None = None, max_power: float = 100.0,
                 settle_s: float = 0.0):
        self.name, self.t = name, transport
        cal = dict(VENDOR_CAL if calibration is None else calibration)
        if set(cal) != {"a", "b", "c", "d"}:
            raise ValueError(f"calibration {sorted(cal)}: needs a, b, c, d "
                             "(f = a·λ³ + b·λ² + c·λ + d)")
        self.cal = {k: float(v) for k, v in cal.items()}
        self.max_power, self.settle_s = float(max_power), settle_s
        if not 0 < self.max_power <= 100:
            raise ValueError(f"max_power {max_power}: 0 < max_power ≤ 100 %")
        self.range_nm = self.usable_range()
        self.wavelength: float | None = None              # nm, as last set
        self.power: float | None = None                   # %, likewise

    # -- conversions ----------------------------------------------------------------------
    def frequency_mhz(self, nm: float) -> float:
        c = self.cal
        return ((c["a"] * nm + c["b"]) * nm + c["c"]) * nm + c["d"]

    def usable_range(self) -> tuple[float, float]:
        """Where the curve stays in 20-200 MHz (one span, monotonic), 100-5000 nm."""
        grid = [100 + k * 0.5 for k in range(9801)]
        fmin, fmax = self.F_RANGE_MHZ
        inside = [x for x in grid if fmin <= self.frequency_mhz(x) <= fmax]
        if not inside:
            raise ValueError(f"calibration {self.cal}: never in {fmin:g}-{fmax:g} MHz")
        lo, hi = inside[0], inside[-1]
        f = [self.frequency_mhz(x) for x in grid if lo <= x <= hi]
        steps = [q - p for p, q in zip(f, f[1:])]
        if not (all(s < 0 for s in steps) or all(s > 0 for s in steps)):
            raise ValueError(f"calibration {self.cal}: the curve is not monotonic over "
                             f"{lo:g}-{hi:g} nm")
        if len(inside) != len(f):
            raise ValueError(f"calibration {self.cal}: leaves {fmin:g}-{fmax:g} MHz "
                             f"between {lo:g} and {hi:g} nm")
        return lo, hi

    # -- commands -------------------------------------------------------------------------
    async def _send(self, nm: float, power: float) -> None:
        await self.t.exchange(frame(b"\x01" + freq_word(self.frequency_mhz(nm))
                                    + power_word(power)))
        if self.settle_s:
            await asyncio.sleep(self.settle_s)

    async def set(self, nm: float) -> None:
        """Movable: the wavelength (nm), with the present power (0 if unknown)."""
        lo, hi = self.range_nm
        if not lo <= nm <= hi:
            raise DriverError(f"{self.name}: {nm:g} nm is outside the calibration "
                              f"({lo:.0f}-{hi:.0f} nm, 20-200 MHz)")
        power = self.power or 0.0
        await self._send(nm, power)
        self.wavelength, self.power = nm, power

    async def set_power(self, power: float) -> None:
        """Percent of the driver's full scale, at the present wavelength."""
        if not 0 <= power <= self.max_power:
            raise DriverError(f"{self.name}: power {power:g} % outside 0-{self.max_power:g} %")
        if self.wavelength is None:
            raise DriverError(f"{self.name}: set the wavelength first (the controller "
                              "cannot report it)")
        await self._send(self.wavelength, power)
        self.power = power

    # -- readings (software level) --------------------------------------------------------
    def describe(self) -> dict[str, FieldInfo]:
        return describe(self)

    async def read(self) -> dict[str, float]:
        """What the driver set: the controller cannot be queried. NaN while unknown."""
        nan, wl = math.nan, self.wavelength
        return {f"{self.name}.wavelength": nan if wl is None else wl,
                f"{self.name}.power": nan if self.power is None else self.power,
                f"{self.name}.rf_frequency": nan if wl is None else self.frequency_mhz(wl)}

    async def snapshot(self) -> dict[str, Any]:
        return {"port": self.t.resource, "calibration": dict(self.cal),
                "range_nm": list(self.range_nm), "max_power": self.max_power,
                "wavelength": self.wavelength, "power": self.power}

    async def safe_state(self) -> None:
        """The vendor's turn-off command: no RF, no light."""
        reply = await self.t.exchange(frame(b"\x40"))
        self.power = 0.0
        if reply != OFF_REPLY:
            raise DriverError(f"{self.name}: turn off answered {reply.hex(' ') or 'nothing'}"
                              f" (expected {OFF_REPLY.hex(' ')})")
