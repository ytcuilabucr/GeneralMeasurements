"""Lake Shore 625 superconducting magnet power supply, as a Rampable.

The axis is the output current in A, or the field in T when `tesla_per_amp`
is given (the driver converts itself; the instrument's own field constant is
only recorded in the snapshot). Rates are axis units per second.

Safety: the driver never writes the persistent-switch heater, the current,
voltage or rate limits. configure() refuses a station whose limits exceed the
supply's own LIMIT? settings. With `persistent_switch = true`, moves are
refused unless the switch heater is on (otherwise the supply would ramp its
leads while the magnet stays persistent). safe_state() holds the present
current; it never ramps the magnet down.

Query replies were checked against a real 625 (fw 1.3/1.1); writes are from
the manual and not yet verified on hardware.
"""

from __future__ import annotations

import asyncio

from .definitions import describe
from .protocols import FieldInfo
from .transport import DriverError, Transport


class LakeShore625:
    """Rampable + Configurable."""

    DEFINITION = "lakeshore_625"

    def __init__(self, name: str, transport: Transport, limits: tuple[float, float],
                 max_rate: float, tesla_per_amp: float | None = None,
                 persistent_switch: bool = False, tolerance_a: float = 0.002,
                 poll_s: float = 0.5):
        if limits[0] > limits[1]:
            raise ValueError(f"bad limits {limits}")
        if max_rate <= 0:
            raise ValueError("max_rate must be > 0")
        if tesla_per_amp is not None and tesla_per_amp <= 0:
            raise ValueError("tesla_per_amp must be > 0")
        self.name, self.t, self.limits, self.max_rate = name, transport, limits, max_rate
        self.k = tesla_per_amp                 # None: the axis is current
        self.persistent_switch, self.tolerance_a, self.poll_s = (
            persistent_switch, tolerance_a, poll_s)
        self._target_a = 0.0
        self._rate_a = max_rate / (tesla_per_amp or 1.0)

    @property
    def units(self) -> str:
        return "A" if self.k is None else "T"

    @property
    def quantity(self) -> str:
        return "current" if self.k is None else "field"

    def _to_amps(self, x: float) -> float:
        return x / self.k if self.k else x

    def _from_amps(self, i: float) -> float:
        return i * self.k if self.k else i

    # -- contract ---------------------------------------------------------
    def describe(self) -> dict[str, FieldInfo]:
        return describe(self)

    async def configure(self) -> None:
        """Check, don't change: model, and that our limits fit inside the supply's."""
        idn = await self.t.query("*IDN?")
        if "MODEL625" not in idn.replace(" ", ""):
            raise DriverError(f"{self.name}: expected a Lake Shore 625, got {idn!r}")
        raw = await self.t.query("LIMIT?")
        try:
            i_lim, _v_lim, rate_lim = (float(s) for s in raw.split(","))
        except ValueError:
            raise DriverError(f"{self.name}: bad LIMIT reply {raw!r}") from None
        worst = max(abs(self._to_amps(self.limits[0])), abs(self._to_amps(self.limits[1])))
        if worst > i_lim:
            raise DriverError(f"{self.name}: limits {self.limits} {self.units} need {worst:.4f} A, "
                              f"supply limit is {i_lim} A")
        if self._rate_a > rate_lim:
            raise DriverError(f"{self.name}: max_rate needs {self._rate_a:.4f} A/s, supply "
                              f"limit is {rate_lim} A/s")
        self._target_a = _float(self.name, await self.t.query("SETI?"))

    async def set(self, value: float) -> None:
        start = self._from_amps(await self._current())
        await self.start_ramp(value, self.max_rate)
        timeout = 1.5 * abs(value - start) / self.max_rate + 30.0
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        while not await self.ramp_done():
            if loop.time() - t0 > timeout:
                raise DriverError(f"{self.name}: ramp to {value} {self.units} not done "
                                  f"after {timeout:.0f} s")
            await asyncio.sleep(self.poll_s)

    async def start_ramp(self, target: float, rate: float) -> None:
        lo, hi = self.limits
        if not lo <= target <= hi:
            raise DriverError(f"{self.name}: {target} {self.units} outside limits {self.limits}")
        if not 0 < rate <= self.max_rate:
            raise DriverError(f"{self.name}: rate {rate} not in (0, {self.max_rate}]")
        if self.persistent_switch and (await self.t.query("PSH?")).strip() != "1":
            raise DriverError(f"{self.name}: persistent switch heater is off; turn it on "
                              "before moving the magnet")
        self._target_a = self._to_amps(target)
        await self.t.write(f"RATE {self._to_amps(rate):.4f}")
        await self.t.write(f"SETI {self._target_a:.4f}")

    async def ramp_done(self) -> bool:
        return abs(await self._current() - self._target_a) <= self.tolerance_a

    async def read(self) -> dict[str, float]:
        n = self.name
        i = await self._current()
        out = {n: self._from_amps(i), f"{n}.v": _float(n, await self.t.query("RDGV?"))}
        if self.k:
            out[f"{n}.i"] = i
        return out

    async def snapshot(self) -> dict:
        t = self.t
        return {"idn": await t.query("*IDN?"), "seti": await t.query("SETI?"),
                "rate": await t.query("RATE?"), "limit": await t.query("LIMIT?"),
                "psh": await t.query("PSH?"), "flds": await t.query("FLDS?"),
                "opst": await t.query("OPST?"), "erst": await t.query("ERST?"),
                "limits": list(self.limits), "max_rate": self.max_rate,
                "tesla_per_amp": self.k}

    @property
    def target(self) -> float:
        """Present target (axis units): the last SETI the driver sent or read."""
        return self._from_amps(self._target_a)

    async def switch_heater_on(self) -> bool:
        """Persistent-switch heater state (read only; the driver never sets it)."""
        return (await self.t.query("PSH?")).strip() == "1"

    async def safe_state(self) -> None:
        """Hold the present current (stops a ramp). Never ramps the magnet down."""
        await self.hold()

    async def hold(self) -> None:
        here = await self._current()
        self._target_a = here
        await self.t.write(f"SETI {here:.4f}")

    async def _current(self) -> float:
        return _float(self.name, await self.t.query("RDGI?"))


def _float(name: str, raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise DriverError(f"{name}: bad reply {raw!r}") from None
