"""Stanford Research SR830 and SR850 lock-in amplifiers (Readable).

Both share the core command set used here (OUTP?/SNAP?/FREQ/SLVL/SENS/OFLT/
PHAS). Index tables are the same range for both; check the manual if you use
values near the ends. Commands from memory: verify against hardware.
"""

from __future__ import annotations

import math

from .definitions import describe
from .protocols import FieldInfo
from .transport import DriverError, Transport

# index -> full-scale sensitivity (V). 0=2 nV ... 26=1 V, 1-2-5 sequence.
SENSITIVITY = [m * 10.0**e * 1e-9 for e in range(0, 9) for m in (2, 5, 10)][:27]
# index -> time constant (s). 0=10 us ... 19=30 ks.
TIME_CONSTANT = [m * 10.0**e for e in range(-5, 5) for m in (1, 3)][:20]


def _nearest(table: list[float], value: float) -> int:
    return min(range(len(table)), key=lambda i: abs(math.log(table[i] / value)))


class _SRSLockin:
    model = ""
    DEFINITION = "srs_lockin"
    SENSITIVITY = SENSITIVITY
    TIME_CONSTANT = TIME_CONSTANT

    def __init__(self, name: str, transport: Transport, frequency: float | None = None,
                 amplitude: float = 0.1, time_constant: float = 0.1,
                 sensitivity: float = 1e-3):
        self.name, self.t = name, transport
        self.frequency, self.amplitude = frequency, amplitude
        self.time_constant, self.sensitivity = time_constant, sensitivity

    def describe(self) -> dict[str, FieldInfo]:
        return describe(self)

    async def configure(self) -> None:
        await self.t.write("OUTX 1")  # reply on GPIB
        await self.t.write(f"SLVL {self.amplitude:.4g}")
        if self.frequency is not None:
            await self.t.write(f"FREQ {self.frequency:.6g}")
        await self.t.write(f"OFLT {_nearest(TIME_CONSTANT, self.time_constant)}")
        await self.t.write(f"SENS {_nearest(SENSITIVITY, self.sensitivity)}")

    async def read(self) -> dict[str, float]:
        raw = await self.t.query("SNAP? 1,2,3,4")  # X,Y,R,theta, simultaneous
        try:
            x, y, r, th = (float(s) for s in raw.split(","))
        except ValueError as e:
            raise DriverError(f"{self.name}: bad SNAP reply {raw!r}") from e
        n = self.name
        return {f"{n}.x": x, f"{n}.y": y, f"{n}.r": r, f"{n}.theta": th}

    async def set_sensitivity(self, volts: float) -> float:
        """Nearest full-scale step; returns the value actually set."""
        i = _nearest(SENSITIVITY, volts)
        await self.t.write(f"SENS {i}")
        self.sensitivity = SENSITIVITY[i]
        return self.sensitivity

    async def set_time_constant(self, seconds: float) -> float:
        i = _nearest(TIME_CONSTANT, seconds)
        await self.t.write(f"OFLT {i}")
        self.time_constant = TIME_CONSTANT[i]
        return self.time_constant

    async def get_sensitivity(self) -> float:
        """Full-scale sensitivity now set on the unit (V)."""
        return SENSITIVITY[int(await self.t.query("SENS?"))]

    async def get_time_constant(self) -> float:
        return TIME_CONSTANT[int(await self.t.query("OFLT?"))]

    async def auto_phase(self) -> None:
        await self.t.write("APHS")

    async def read_aux(self, channel: int) -> float:
        if not 1 <= channel <= 4:
            raise ValueError("aux channel 1..4")
        return float(await self.t.query(f"OAUX? {channel}"))

    async def snapshot(self) -> dict:
        t = self.t
        return {"idn": await t.query("*IDN?"),
                "frequency": float(await t.query("FREQ?")),
                "amplitude": float(await t.query("SLVL?")),
                "phase": float(await t.query("PHAS?")),
                "sens_index": int(await t.query("SENS?")),
                "tc_index": int(await t.query("OFLT?"))}

    async def safe_state(self) -> None:
        pass  # leave reference running; nothing hazardous


class SR830(_SRSLockin):
    model = "SR830"


class SR850(_SRSLockin):
    model = "SR850"
