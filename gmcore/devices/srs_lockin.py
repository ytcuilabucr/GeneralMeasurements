"""Stanford Research SR830 and SR850 lock-in amplifiers (Readable).

Both share the command set used here (OUTP?/SNAP?/FREQ/SLVL/SENS/OFLT/PHAS).
From the SR830 and SR850 manuals (thinksrs.com): SENS 0 … 26 = 2 nV … 1 V
full scale, OFLT 0 … 19 = 10 µs … 30 ks (the same tables for both); SLVL
0.004 … 5.000 V rms; FREQ 0.001 … 102 000 Hz (internal reference only); time
constants above 30 s are refused when the detection frequency exceeds 200 Hz,
and short ones are raised to the minimum the filter slope and reserve allow.

The reference frequency, amplitude and phase can be set; their bounds are the
min / max of those properties in defs/srs_lockin.toml, and every setting is
read back, so a value the lock-in refused or adjusted is reported.
"""

from __future__ import annotations

import math

from .definitions import describe, prop_bounds
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
        self._check("amplitude", amplitude, ValueError)
        if frequency is not None:
            self._check("frequency", frequency, ValueError)
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
        """Nearest step; returns the value the unit took. It may refuse or adjust
        (> 30 s above 200 Hz, or below the filter's minimum), so read it back."""
        i = _nearest(TIME_CONSTANT, seconds)
        await self.t.write(f"OFLT {i}")
        got = int(await self.t.query("OFLT?"))
        self.time_constant = TIME_CONSTANT[got]
        if got != i:
            raise DriverError(f"{self.name}: asked for {TIME_CONSTANT[i]:g} s, the lock-in set "
                              f"{self.time_constant:g} s (above 200 Hz the limit is 30 s; "
                              "short ones are limited by the filter slope and reserve)")
        return self.time_constant

    def _check(self, pid: str, value: float, error: type[Exception]) -> float:
        """Within the definition's min / max for that property."""
        lo, hi = prop_bounds(self, pid)
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise error(f"{self.name}: {pid} {value:g} outside {lo:g} … {hi:g}")
        return float(value)

    async def set_frequency(self, hz: float) -> float:
        """Internal reference frequency; refused while following an external
        reference, and when harmonic * frequency would exceed 102 kHz."""
        hz = self._check("frequency", hz, DriverError)
        if (await self.t.query("FMOD?")).strip() == "0":
            raise DriverError(f"{self.name}: the reference is external; the frequency "
                              "follows the reference input")
        harm = int(float(await self.t.query("HARM?")))
        if harm * hz > 102e3:
            raise DriverError(f"{self.name}: harmonic {harm} * {hz:g} Hz is above 102 kHz")
        await self.t.write(f"FREQ {hz:.6g}")
        got = float(await self.t.query("FREQ?"))   # rounded to 5 digits or 0.1 mHz
        if abs(got - hz) > max(1e-4, 1e-4 * hz):
            raise DriverError(f"{self.name}: asked for {hz:g} Hz, the lock-in has {got:g} Hz")
        self.frequency = got
        return got

    async def set_amplitude(self, volts: float) -> float:
        """Sine output amplitude (V rms)."""
        volts = self._check("amplitude", volts, DriverError)
        await self.t.write(f"SLVL {volts:.4g}")
        got = float(await self.t.query("SLVL?"))   # 2 mV resolution
        if abs(got - volts) > 0.0021:
            raise DriverError(f"{self.name}: asked for {volts:g} V, the lock-in has {got:g} V")
        self.amplitude = got
        return got

    async def set_phase(self, degrees: float) -> float:
        """Reference phase shift; the lock-in wraps it to ±180°."""
        degrees = self._check("phase", degrees, DriverError)
        await self.t.write(f"PHAS {degrees:.2f}")
        got = float(await self.t.query("PHAS?"))
        if abs((got - degrees + 180) % 360 - 180) > 0.011:     # same angle, wrapped
            raise DriverError(f"{self.name}: asked for {degrees:g}°, the lock-in has {got:g}°")
        return got

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
