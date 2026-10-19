"""Lake Shore 335 temperature controller, one control loop as a Rampable (K).

The axis is the loop's setpoint; the reading is the configured sensor input.
Rates are K/s at the interface (the instrument takes K/min). The driver never
changes the loop's control input, PID or heater limits: those stay with the
operator. configure() only checks them; prepare() turns the heater on at the
configured range. Query replies were checked against a real 335 (fw 1.4);
writes are from the manual and not yet verified on hardware.
"""

from __future__ import annotations

import asyncio
import math

from .definitions import describe
from .protocols import FieldInfo
from .transport import DriverError, Transport

_INPUTS = {"A": 1, "B": 2}
_RATE_MIN, _RATE_MAX = 0.1, 100.0     # K/min, instrument limits for the setpoint ramp


class LakeShore335:
    """Rampable + Configurable. `loop` is the heater output (1 or 2)."""

    DEFINITION = "lakeshore_335"

    def __init__(self, name: str, transport: Transport, input: str = "A", loop: int = 1,
                 limits: tuple[float, float] = (0.0, 300.0), max_rate: float = 10 / 60,
                 heater_range: int | None = None, tolerance: float = 0.1,
                 stable_s: float = 60.0, settle_timeout_s: float = 3600.0,
                 poll_s: float = 1.0, safe_action: str = "hold"):
        if input not in _INPUTS:
            raise ValueError("input must be 'A' or 'B'")
        if loop not in (1, 2):
            raise ValueError("loop must be 1 or 2")
        if not 0 <= limits[0] < limits[1]:
            raise ValueError(f"bad limits {limits}")
        if not _RATE_MIN <= max_rate * 60 <= _RATE_MAX:
            raise ValueError(f"max_rate {max_rate} K/s outside the 335 ramp range "
                             f"({_RATE_MIN}-{_RATE_MAX} K/min)")
        if heater_range is not None and heater_range not in (0, 1, 2, 3):
            raise ValueError("heater_range must be 0 (off), 1 (low), 2 (med) or 3 (high)")
        if safe_action not in ("hold", "off"):
            raise ValueError("safe_action must be 'hold' or 'off'")
        self.name, self.t = name, transport
        self.input, self.loop, self.limits, self.max_rate = input, loop, limits, max_rate
        self.heater_range, self.tolerance, self.stable_s = heater_range, tolerance, stable_s
        self.settle_timeout_s, self.poll_s, self.safe_action = settle_timeout_s, poll_s, safe_action

    # -- contract ---------------------------------------------------------
    def describe(self) -> dict[str, FieldInfo]:
        return describe(self)

    async def configure(self) -> None:
        """Check, don't change: model, and that the loop really controls our input."""
        idn = await self.t.query("*IDN?")
        if "MODEL335" not in idn.replace(" ", ""):
            raise DriverError(f"{self.name}: expected a Lake Shore 335, got {idn!r}")
        mode, inp = await self._outmode()
        if mode == 0:
            raise DriverError(f"{self.name}: loop {self.loop} output mode is Off; set it to "
                              "closed loop on the front panel")
        if inp != _INPUTS[self.input]:
            raise DriverError(f"{self.name}: loop {self.loop} is controlled by input "
                              f"{_input_name(inp)}, but the station reads input {self.input}")
        if (self.heater_range or 0) > 1 and await self.voltage_output():
            raise DriverError(f"{self.name}: output 2 is in voltage mode, where the heater "
                              "range is only 0 (off) or 1 (on)")

    async def voltage_output(self) -> bool:
        """Output 2 can be a ±10 V voltage output (HTRSET type 1); then RANGE takes
        only 0 = off and 1 = on (335 manual, RANGE). Output 1 is always current."""
        if self.loop != 2:
            return False
        raw = await self.t.query("HTRSET? 2")
        return raw.split(",")[0].strip() == "1"

    async def prepare(self) -> None:
        if self.heater_range is not None:
            await self.set_heater_range(self.heater_range)

    async def set_heater_range(self, range_: int) -> None:
        if range_ not in (0, 1, 2, 3):
            raise DriverError(f"{self.name}: heater range must be 0-3, got {range_}")
        if range_ > 1 and await self.voltage_output():
            raise DriverError(f"{self.name}: output 2 is in voltage mode: range 0 (off) or 1 "
                              "(on) only")
        await self.t.write(f"RANGE {self.loop},{range_}")

    async def set(self, value: float) -> None:
        """Ramp the setpoint at max_rate, then wait until the temperature is within
        `tolerance` for `stable_s` continuous seconds."""
        await self.start_ramp(value, self.max_rate)
        loop = asyncio.get_running_loop()
        t0, stable_since = loop.time(), None
        while True:
            now = loop.time()
            if now - t0 > self.settle_timeout_s:
                raise DriverError(f"{self.name}: not stable at {value} K after "
                                  f"{self.settle_timeout_s:.0f} s")
            if not await self.ramp_done() or abs(await self._temperature() - value) > self.tolerance:
                stable_since = None
            elif stable_since is None:
                stable_since = now
            elif now - stable_since >= self.stable_s:
                return
            await asyncio.sleep(self.poll_s)

    async def start_ramp(self, target: float, rate: float) -> None:
        lo, hi = self.limits
        if not lo <= target <= hi:
            raise DriverError(f"{self.name}: {target} K outside limits {self.limits}")
        if not 0 < rate <= self.max_rate:
            raise DriverError(f"{self.name}: rate {rate} K/s not in (0, {self.max_rate}]")
        k_per_min = max(_RATE_MIN, min(_RATE_MAX, round(rate * 60, 1)))
        await self.t.write(f"RAMP {self.loop},1,{k_per_min:.1f}")
        await self.t.write(f"SETP {self.loop},{target:.3f}")

    async def ramp_done(self) -> bool:
        """Setpoint ramp finished. The temperature itself may still lag behind."""
        return (await self.t.query(f"RAMPST? {self.loop}")).strip() == "0"

    async def read(self) -> dict[str, float]:
        n = self.name
        return {n: await self._temperature(),
                f"{n}.setpoint": _float(n, await self.t.query(f"SETP? {self.loop}")),
                f"{n}.heater": _float(n, await self.t.query(f"HTR? {self.loop}"))}

    async def read_input(self, input: str) -> float:
        """Kelvin on any input (A or B), NaN on a sensor fault. Monitoring only:
        the axis and read() stay on the configured input."""
        if input not in _INPUTS:
            raise DriverError(f"{self.name}: input must be 'A' or 'B', got {input!r}")
        return await self._temperature(input)

    async def get_heater_range(self) -> int:
        return int(_float(self.name, await self.t.query(f"RANGE? {self.loop}")))

    async def ramp_rate(self) -> tuple[bool, float]:
        """(ramping enabled, rate in K/s) from RAMP?."""
        raw = await self.t.query(f"RAMP? {self.loop}")
        try:
            on, rate = raw.split(",")
            return on.strip() == "1", float(rate) / 60
        except ValueError:
            raise DriverError(f"{self.name}: bad RAMP reply {raw!r}") from None

    async def snapshot(self) -> dict:
        t, lp = self.t, self.loop
        return {"idn": await t.query("*IDN?"), "input": self.input, "loop": lp,
                "setpoint": await t.query(f"SETP? {lp}"), "ramp": await t.query(f"RAMP? {lp}"),
                "range": await t.query(f"RANGE? {lp}"), "outmode": await t.query(f"OUTMODE? {lp}"),
                "pid": await t.query(f"PID? {lp}"), "limits": list(self.limits),
                "max_rate": self.max_rate, "safe_action": self.safe_action}

    async def safe_state(self) -> None:
        """hold: stop the setpoint ramp where it is now. off: heater range 0."""
        if self.safe_action == "off":
            await self.set_heater_range(0)
        else:
            await self.hold()

    async def hold(self) -> None:
        """Stop the setpoint ramp at the present (ramping) setpoint."""
        here = _float(self.name, await self.t.query(f"SETP? {self.loop}"))
        await self.t.write(f"RAMP {self.loop},0,{self.max_rate * 60:.1f}")
        await self.t.write(f"SETP {self.loop},{here:.3f}")

    # -- helpers ----------------------------------------------------------
    async def _temperature(self, input: str | None = None) -> float:
        """Kelvin, or NaN when the input reports a sensor fault (so nan_watchdog sees it)."""
        inp = input or self.input
        status = int(await self.t.query(f"RDGST? {inp}"))
        if status:
            return math.nan
        return _float(self.name, await self.t.query(f"KRDG? {inp}"))

    async def _outmode(self) -> tuple[int, int]:
        raw = await self.t.query(f"OUTMODE? {self.loop}")
        try:
            mode, inp, _ = (int(s) for s in raw.split(","))
        except ValueError:
            raise DriverError(f"{self.name}: bad OUTMODE reply {raw!r}") from None
        return mode, inp


def _input_name(code: int) -> str:
    return {0: "none", 1: "A", 2: "B"}.get(code, str(code))


def _float(name: str, raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise DriverError(f"{name}: bad reply {raw!r}") from None
