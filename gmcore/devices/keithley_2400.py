"""Keithley 2400 SourceMeter: ±210 V / ±1.05 A, 20 W.

A complete driver on its own (it shares no code with the other Keithley
models); the properties are in defs/keithley_2400.toml plus one file per source
function (keithley_2400_volt.toml, keithley_2400_curr.toml).

Figures from the Series 2400 datasheet (1KW-2798-1) and the Series 2400 User's
Manual (2400S-900-01 Rev. K):
- V ranges 200 mV, 2 V, 20 V, 200 V; I ranges 1 µA … 1 A (decades);
- source/sink limits ±21 V @ ±1.05 A and ±210 V @ ±105 mA;
- overrange 105 % (source and measure); minimum compliance 0.1 % of range;
  NPLC 0.01 … 10; after *RST both source and measure autorange are ON.

Source V or I, always measure both V and I. set() writes the level in one step
(there is no ramp): a scan point, the panel's Set and safe_state() all jump
straight to their value. NPLC is bounded by the definition (min / max of the
`nplc` property in defs/keithley_2400.toml).

Ranges (User's Manual sections 6 and 18):
- the source range sets the resolution of the sourced quantity, and the
  measurement of that quantity uses the same range;
- the sense range applies to the other quantity (the compliance quantity).
  Below the compliance setting it clamps the output at 105 % of the range
  ("range compliance"); autorange never does;
- a level beyond 105 % of a fixed source range is refused by the unit, so
  set() refuses it first;
- a range change glitches the output (range-change overshoot 100 mV typical),
  so the source range is only changed with the output off.
"""

from __future__ import annotations

import asyncio

from .definitions import describe, prop_bounds
from .protocols import FieldInfo
from .transport import DriverError, Transport

_SCPI = {"volt": "VOLT", "curr": "CURR"}
_OTHER = {"volt": "curr", "curr": "volt"}
_LIMIT = {"volt": ":SENS:CURR:PROT", "curr": ":SENS:VOLT:PROT"}     # compliance
_TRIP = {"volt": ":SENS:CURR:PROT:TRIP?", "curr": ":SENS:VOLT:PROT:TRIP?"}


class Keithley2400:
    """Movable + Readable + Configurable. `mode` is the sourced quantity."""

    DEFINITION = "keithley_2400"
    MODEL = "2400"
    V_RANGES = (0.2, 2.0, 20.0, 200.0)    # V, source and measure
    I_RANGES = (1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1.0)    # A, source and measure
    MAX_V, MAX_I = 210.0, 1.05          # |V|, |A| the unit can source
    HIGH_V, HIGH_V_MAX_I = 21.0, 0.105    # above ±21 V the current is limited to this
    OVERRANGE = 1.05
    MIN_COMPLIANCE = 1e-3                 # fraction of the sense range

    def __init__(self, name: str, transport: Transport,
                 mode: str = "volt", compliance: float = 1e-6,
                 limits: tuple[float, float] = (-1.0, 1.0), nplc: float = 1.0,
                 settle_s: float = 0.05, check_compliance: bool = True,
                 source_range: float | None = None, sense_range: float | None = None):
        self.name, self.t = name, transport
        self.settle_s = settle_s
        self.check_compliance = check_compliance
        self._level = 0.0
        self._output = False      # belief, like _level; set by output()
        self._set_mode(mode, compliance, limits)
        self.nplc = self._check_nplc(nplc, ValueError)
        # station settings applied by configure(); None = autorange
        self.cfg_source_range = self._check_range(mode, source_range)
        self.cfg_sense_range = self._check_range(_OTHER[mode], sense_range)
        self._source_range: float | None = None    # beliefs: what the unit is set to
        self._sense_range: float | None = None

    @property
    def model(self) -> str:
        return self.MODEL

    def _ranges(self, quantity: str) -> tuple[float, ...]:
        return self.V_RANGES if quantity == "volt" else self.I_RANGES

    def _check_nplc(self, nplc: float, error: type[Exception]) -> float:
        """NPLC within the definition's min / max (the `nplc` property)."""
        lo, hi = prop_bounds(self, "nplc")
        if (lo is not None and nplc < lo) or (hi is not None and nplc > hi):
            raise error(f"{self.name}: NPLC {nplc:g} outside {lo:g} … {hi:g}")
        return float(nplc)

    def _set_mode(self, mode: str, compliance: float, limits: tuple[float, float]) -> None:
        """Validate and adopt a mode's settings against the model (no I/O)."""
        if mode not in ("volt", "curr"):
            raise ValueError("mode must be 'volt' or 'curr'")
        limits = (float(limits[0]), float(limits[1]))
        peak = max(abs(limits[0]), abs(limits[1]))
        hard = self.MAX_V if mode == "volt" else self.MAX_I
        if limits[0] > limits[1] or peak > hard:
            raise ValueError(f"limits {limits} outside {self.MODEL} range ±{hard:g}")
        compliance = float(compliance)
        c_hard = self.MAX_I if mode == "volt" else self.MAX_V
        c_min = self.MIN_COMPLIANCE * min(self._ranges(_OTHER[mode]))
        if not c_min <= compliance <= c_hard:
            raise ValueError(f"compliance {compliance:g} outside {self.MODEL} range "
                             f"{c_min:g} … {c_hard:g} {'A' if mode == 'volt' else 'V'}")
        hi_i = self.HIGH_V_MAX_I               # power envelope
        if mode == "volt" and peak > self.HIGH_V and hi_i is not None and compliance > hi_i:
            raise ValueError(f"{self.MODEL}: above ±{self.HIGH_V:g} V the current is limited "
                             f"to ±{hi_i:g} A; lower the compliance or the limits")
        if mode == "curr" and hi_i is not None and peak > hi_i and compliance > self.HIGH_V:
            raise ValueError(f"{self.MODEL}: above ±{hi_i:g} A the voltage is limited to "
                             f"±{self.HIGH_V:g} V; lower the compliance or the limits")
        self.mode, self.compliance, self.limits = mode, compliance, limits
        self._src = _SCPI[mode]
        self._src_units = "V" if mode == "volt" else "A"

    def _check_range(self, quantity: str, value: float | None) -> float | None:
        if value is None:
            return None
        ranges = self._ranges(quantity)
        if float(value) not in ranges:
            raise ValueError(f"{self.MODEL}: {value:g} is not a {quantity} range; one of "
                             f"{', '.join(f'{r:g}' for r in ranges)} or auto")
        return float(value)

    # -- contract ---------------------------------------------------------
    def describe(self) -> dict[str, FieldInfo]:
        return describe(self)

    async def configure(self) -> None:
        """Put the instrument in a known state; output stays OFF."""
        t = self.t
        await t.write("*RST")
        await t.write("*CLS")
        await t.write(f":SOUR:FUNC {self._src}")
        await t.write(f":SOUR:{self._src}:MODE FIX")
        await t.write(f":SOUR:{self._src}:LEV 0")
        other = _SCPI[_OTHER[self.mode]]
        await t.write(f"{_LIMIT[self.mode]} {self.compliance:g}")
        await t.write(':SENS:FUNC "VOLT","CURR"')
        await t.write(f":SENS:{other}:NPLC {self.nplc:g}")
        await t.write(":FORM:ELEM VOLT,CURR")
        self._source_range = self._sense_range = None      # *RST: both autorange
        if self.cfg_source_range is not None:
            await t.write(f":SOUR:{self._src}:RANG {self.cfg_source_range:g}")
            self._source_range = self.cfg_source_range
        if self.cfg_sense_range is not None:
            await t.write(f":SENS:{other}:RANG {self.cfg_sense_range:g}")
            self._sense_range = self.cfg_sense_range
        errs = await self.errors()
        if errs:
            raise DriverError(f"{self.name}: configure rejected: {'; '.join(errs)}")

    async def errors(self) -> list[str]:
        """Drain the SCPI error queue; [] when clean."""
        out: list[str] = []
        for _ in range(20):
            e = await self.t.query(":SYST:ERR?")
            if e.split(",")[0].strip() in ("0", "+0"):
                break
            out.append(e)
        return out

    async def sync(self) -> str:
        """Adopt the instrument's present output state and source level (queries
        only), so set() ramps from where the unit really is. For attaching to a
        running instrument without configure(). Returns its source function;
        if that isn't this driver's mode, the level is not adopted."""
        func = (await self.t.query(":SOUR:FUNC?")).strip()
        self._output = (await self.t.query(":OUTP?")).strip() == "1"
        if func.startswith(self._src):
            self._level = float(await self.t.query(f":SOUR:{self._src}:LEV?"))
        return func

    async def active_mode(self) -> str:
        """The unit's present source function: 'volt', 'curr' or the raw reply."""
        func = (await self.t.query(":SOUR:FUNC?")).strip()
        return {"VOLT": "volt", "CURR": "curr"}.get(func[:4].upper(), func)

    async def switch_mode(self, mode: str, compliance: float,
                          limits: tuple[float, float]) -> None:
        """Change the source function, with the output OFF (else refused; the
        level may be anything): *RST and configure() in the new one, so the new
        function starts at level 0 with the output off. The new settings are
        checked against the model first, before any I/O."""
        if self._output:
            raise DriverError(f"{self.name}: turn the output OFF before switching the "
                              "source function")
        old = (self.mode, self.compliance, self.limits)
        self._set_mode(mode, compliance, limits)               # raises before any I/O
        new = (self.mode, self.compliance, self.limits)
        self._set_mode(*old)
        await self.output(False)
        self._set_mode(*new)
        self._level = 0.0
        self.cfg_source_range = self.cfg_sense_range = None    # the station's were per mode
        await self.configure()

    async def set_nplc(self, nplc: float) -> float:
        """Integration time of the measured quantity, within the definition's
        min / max; the unit's error queue is checked."""
        nplc = self._check_nplc(nplc, DriverError)
        await self.t.write(f":SENS:{_SCPI[_OTHER[self.mode]]}:NPLC {nplc:g}")
        await self._check_errors("NPLC")
        self.nplc = nplc
        return nplc

    # -- ranges -------------------------------------------------------------
    @property
    def source_ranges(self) -> list[float]:
        """This model's ranges for the sourced quantity (the GUI's choices)."""
        return list(self._ranges(self.mode))

    @property
    def sense_ranges(self) -> list[float]:
        """This model's ranges for the measured (compliance) quantity."""
        return list(self._ranges(_OTHER[self.mode]))

    async def _range(self, kind: str, scpi: str) -> float | None:
        """A range as the unit reports it: None while autoranging."""
        auto = (await self.t.query(f":{kind}:{scpi}:RANG:AUTO?")).strip()
        if auto in ("1", "ON"):
            return None
        return float(await self.t.query(f":{kind}:{scpi}:RANG?"))

    async def get_source_range(self) -> float | None:
        self._source_range = await self._range("SOUR", self._src)
        return self._source_range

    async def get_sense_range(self) -> float | None:
        self._sense_range = await self._range("SENS", _SCPI[_OTHER[self.mode]])
        return self._sense_range

    async def set_source_range(self, value: float | None) -> float | None:
        """Fix the source range (None: autorange). Output must be off: a range
        change glitches the output. Refused if the present level or the limits
        don't fit, since the unit refuses levels beyond 105 % of range."""
        value = self._check_range(self.mode, value)
        if self._output:
            raise DriverError(f"{self.name}: turn the output off before changing the "
                              "source range (a range change glitches the output)")
        if value is not None:
            top = self.OVERRANGE * value
            if abs(self._level) > top:
                raise DriverError(f"{self.name}: the level {self._level:g} "
                                  f"{self._src_units} doesn't fit the {value:g} range")
            if max(abs(x) for x in self.limits) > top:
                raise DriverError(f"{self.name}: the limits {self.limits} reach beyond the "
                                  f"{value:g} {self._src_units} range (±{top:g}); narrow "
                                  "them or use a larger range")
            await self.t.write(f":SOUR:{self._src}:RANG {value:g}")
        else:
            await self.t.write(f":SOUR:{self._src}:RANG:AUTO ON")
        await self._check_errors("source range")
        self._source_range = value
        return value

    async def set_sense_range(self, value: float | None) -> float | None:
        """Fix the measure range of the compliance quantity (None: autorange).
        A range below the compliance clamps the output at 105 % of the range."""
        value = self._check_range(_OTHER[self.mode], value)
        other = _SCPI[_OTHER[self.mode]]
        if value is not None:
            if self.compliance < self.MIN_COMPLIANCE * value:
                raise DriverError(f"{self.name}: the compliance {self.compliance:g} is "
                                  f"below {self.MIN_COMPLIANCE:.1%} of the {value:g} range")
            await self.t.write(f":SENS:{other}:RANG {value:g}")
        else:
            await self.t.write(f":SENS:{other}:RANG:AUTO ON")
        await self._check_errors("sense range")
        self._sense_range = value
        return value

    @property
    def effective_compliance(self) -> float:
        """The clamp in force: the compliance, or 105 % of a lower fixed sense range."""
        if self._sense_range is None:
            return self.compliance
        return min(self.compliance, self.OVERRANGE * self._sense_range)

    async def _check_errors(self, what: str) -> None:
        errs = await self.errors()
        if errs:
            raise DriverError(f"{self.name}: {what} rejected: {'; '.join(errs)}")

    # -- output and level ---------------------------------------------------
    @property
    def output_on(self) -> bool:
        return self._output

    @property
    def programmed_level(self) -> float:
        """The level last sent to (or adopted from) the unit."""
        return self._level

    @property
    def trip_query(self) -> str:
        """The "compliance tripped?" query for the present mode."""
        return _TRIP[self.mode]

    async def prepare(self) -> None:
        """Called by the runner before the first move: output ON at level 0."""
        await self.t.write(f":SOUR:{self._src}:LEV 0")
        self._level = 0.0
        await self.output(True)

    async def output(self, on: bool) -> None:
        await self.t.write(f":OUTP {'ON' if on else 'OFF'}")
        self._output = on

    async def set_output(self, on: bool) -> None:
        """ON: level 0, then output on (prepare). OFF: level 0, then off (safe_state)."""
        await (self.prepare() if on else self.safe_state())

    async def set(self, value: float) -> None:
        """The level, in one step (no ramp), then `settle_s`."""
        lo, hi = self.limits
        if not lo <= value <= hi:
            raise DriverError(f"{self.name}: {value} outside limits {self.limits}")
        r = self._source_range
        if r is not None and abs(value) > self.OVERRANGE * r:
            raise DriverError(f"{self.name}: {value:g} {self._src_units} doesn't fit the "
                              f"fixed {r:g} {self._src_units} source range "
                              f"(±{self.OVERRANGE * r:g})")
        await self.t.write(f":SOUR:{self._src}:LEV {value:.6g}")
        self._level = value
        await asyncio.sleep(self.settle_s)

    async def read(self) -> dict[str, float]:
        # With the output off the unit logs 803 and never replies: fail now
        # instead of after a VISA timeout (seen on a 2400 and a 2401).
        if not self._output:
            raise DriverError(f"{self.name}: output is off; read() needs prepare() first")
        raw = await self.t.query(":READ?")            # :FORM:ELEM VOLT,CURR -> V,I
        parts = raw.split(",")
        if len(parts) < 2:
            raise DriverError(f"{self.name}: bad reading {raw!r}")
        try:
            v, i = float(parts[0]), float(parts[1])
        except ValueError:
            raise DriverError(f"{self.name}: bad reading {raw!r}") from None
        out = {f"{self.name}.v": v, f"{self.name}.i": i}
        if self.check_compliance:
            trip = await self.t.query(self.trip_query)
            out[f"{self.name}.compliance"] = 1.0 if trip.strip() not in ("0", "+0") else 0.0
        return out

    async def snapshot(self) -> dict:
        return {"idn": await self.t.query("*IDN?"), "model": self.MODEL,
                "mode": self.mode, "compliance": self.compliance,
                "limits": list(self.limits), "nplc": self.nplc, "level": self._level,
                "source_range": self._source_range, "sense_range": self._sense_range}

    async def safe_state(self) -> None:
        """Level 0 in one step (when 0 is inside the limits), then output OFF."""
        if self.limits[0] <= 0 <= self.limits[1]:
            await self.set(0.0)
        await self.output(False)
