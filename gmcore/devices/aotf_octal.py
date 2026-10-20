"""Crystal Technology octal-channel AOTF controller, as supplied with the Fianium
supercontinuum source (USB VID 16C7, PID 0001). One driver instance controls
one RF channel: its wavelength and its power. Nothing else.

Sources:
- AOTF Controller Command Reference, rev. 1.3 (Crystal Technology, 2010);
  AotfLibrary.h and "AOTF Controller Communication" for the USB library.
- The lab's LabVIEW program, "AOTF Labview/" (2010): format cmd.vi,
  format cal cmd.vi, turn all off.vi, find all AOTFs.vi, get all parameters.vi.
- The calibration files, "<serial> visible.aotf" / "NIR1" / "NIR2".

Commands, in the LabVIEW program's own formats:

    dds f <ch> <MHz>            RF frequency, "%f" MHz (format cmd.vi)
    dds a <ch> <asf>            amplitude scale factor 0 … 16383, 0 = no RF
                                (format cmd.vi, turn all off.vi)
    dau en                      the controller, not the external input, drives
    mod dac <ch> 16383          the modulation, at full scale (find all AOTFs.vi
                                sends both, the latter for all channels "*")
    dds f <ch>, dds a <ch>      without a value: display (queries)
    boardid serial              (snapshot)

**Wavelength.** The calibration file's cubic gives the frequency,
f [MHz] = t0 + t1·λ + t2·λ² + t3·λ³ with λ in nm ([Calibration] tuning0-3).
The driver computes it and sends MHz, as the LabVIEW program's "legacy" path
does. It does not send "dds f <ch> #<nm>", which would use whatever
coefficients happen to be stored in the controller. There are no configured
wavelength limits: a calibration is usable inside its PowerNorm bands where the
frequency is inside the DDS range (usable_range, worked out in __init__; the
cubic must be monotonic there).

**Power.** Power is a percentage of the calibration's PowerNorm for the
wavelength's band ("Power Opt:370-600=6240,600-670=6400,…"): the amplitude
that gives the optimum diffracted power there. 100 % sends exactly that
amplitude, never more. `max_power` can cap it lower.

**Control only.** The controller generates RF; it has no optical detector in
this setup. An optional optical-power-sensor input exists, but the lab program
never reads it. read() therefore reports what the driver set, a software-level
reading, with no bus traffic. sync() (on connect, queries only) reads the
controller's present frequency and amplitude back, so the readings start from
the real state, not from a guess.

**Calibration mode.** The same controller drives the visible or the NIR
crystal; which one is a matter of the calibration file (the LabVIEW program's
"tuning curve" selector: visible / NIR1 / NIR2). The driver's mode, "vis" or
"nir", picks the calibration from those it was given. switch_mode() swaps
the calibration in software only, with no interlock: nothing is written to the
controller, and the panel's re-read shows the present RF as a wavelength (and
power) of the new curve.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .definitions import describe
from .protocols import FieldInfo
from .transport import DriverError, Transport

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


@dataclass(frozen=True)
class Calibration:
    tuning: tuple[float, float, float, float]          # MHz = Σ t_i λ^i, λ in nm
    bands: tuple[tuple[float, float, int], ...]        # (from nm, to nm, ASF at 100 %)
    path: str = ""

    def frequency(self, nm: float) -> float:
        t0, t1, t2, t3 = self.tuning
        return t0 + nm * (t1 + nm * (t2 + nm * t3))

    def norm(self, nm: float) -> int:
        for lo, hi, asf in self.bands:
            if lo <= nm <= hi:
                return asf
        raise ValueError(f"{nm:g} nm is outside the calibration's power bands")


def read_calibration(path: str | Path) -> Calibration:
    """[Calibration] tuning0-3 and PowerNorm from a .aotf file (the format the
    LabVIEW and Fianium programs save). Placeholder coefficients (all 1, as in
    the "default" files) are refused."""
    path = Path(path)
    try:
        text = path.read_text("latin-1")
    except OSError as e:
        raise ValueError(f"calibration {path}: {e}") from None
    tuning = []
    for i in range(4):
        m = re.search(rf"^\s*tuning{i}\s*=\s*({_NUM})", text, re.M | re.I)
        if not m:
            raise ValueError(f"calibration {path.name}: no tuning{i}")
        tuning.append(float(m.group(1)))
    if all(t == 1 for t in tuning):
        raise ValueError(f"calibration {path.name}: placeholder coefficients (all 1); "
                         "use the file made for this crystal")
    m = re.search(r"^\s*PowerNorm\s*=\s*\"?([^\"\r\n]*)", text, re.M | re.I)
    if not m:
        raise ValueError(f"calibration {path.name}: no PowerNorm")
    bands = tuple((float(a), float(b), int(c)) for a, b, c in
                  re.findall(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*=\s*(\d+)", m.group(1)))
    if not bands:
        raise ValueError(f"calibration {path.name}: PowerNorm has no bands")
    return Calibration((tuning[0], tuning[1], tuning[2], tuning[3]), bands, str(path))


class AotfOctal:
    DEFINITION = "aotf_octal"
    MODES = ("vis", "nir")            # which calibration: the visible or the NIR crystal
    CHANNELS = 8                      # octal controller: channels 0-7
    ASF_MAX = 16383                   # amplitude scale factor (Command Reference, Amplitude)
    F_RANGE_MHZ = (1.0, 200.0)        # DDS output (Command Reference, Ftw: 0-200 MHz)

    def __init__(self, name: str, transport: Transport,
                 calibrations: dict[str, Calibration], mode: str = "vis", channel: int = 0,
                 max_power: float = 100.0, settle_s: float = 0.0):
        self.name, self.t = name, transport
        self.channel, self.max_power, self.settle_s = int(channel), float(max_power), settle_s
        if not 0 <= self.channel < self.CHANNELS:
            raise ValueError(f"channel {channel}: the octal controller has 0-7")
        if not 0 < self.max_power <= 100:
            raise ValueError(f"max_power {max_power}: 0 < max_power ≤ 100 %")
        if bad := set(calibrations) - set(self.MODES):
            raise ValueError(f"calibrations {sorted(bad)}: modes are {', '.join(self.MODES)}")
        # each calibration's usable wavelength range, worked out once (raises if unusable)
        self.calibrations = dict(calibrations)
        self.ranges = {m: self.usable_range(c) for m, c in self.calibrations.items()}
        self._set_mode(mode)
        self.wavelength: float | None = None              # nm, as last set or read back
        self.power: float | None = None                   # %, likewise
        self.asf: int | None = None
        self._enabled = False                             # "dau en" / "mod dac" sent

    def _set_mode(self, mode: str) -> None:
        if mode not in self.MODES:
            raise ValueError(f"mode {mode!r}: one of {', '.join(self.MODES)}")
        if mode not in self.calibrations:
            raise ValueError(f"mode {mode!r}: no calibration file given for it")
        self.mode, self.cal, self.range_nm = mode, self.calibrations[mode], self.ranges[mode]
        self.cal_name = Path(self.cal.path).name or "inline"

    @classmethod
    def usable_range(cls, cal: Calibration) -> tuple[float, float]:
        """Where the calibration can be used, from the file alone: inside its
        PowerNorm bands (no gaps), with the RF frequency inside the DDS range.
        The cubic must be monotonic there, so a frequency means one wavelength."""
        name = Path(cal.path).name or "calibration"
        if not all(0 < asf <= cls.ASF_MAX for _, _, asf in cal.bands):
            raise ValueError(f"{name}: PowerNorm amplitudes must be 1-{cls.ASF_MAX}")
        lo, hi = min(b[0] for b in cal.bands), max(b[1] for b in cal.bands)
        grid = [lo + (hi - lo) * k / 2000 for k in range(2001)]
        for x in grid:
            cal.norm(x)                                  # raises at a gap in the bands
        fmin, fmax = cls.F_RANGE_MHZ
        inside = [x for x in grid if fmin <= cal.frequency(x) <= fmax]
        if not inside:
            raise ValueError(f"{name}: the tuning curve never falls in {fmin:g}-{fmax:g} MHz")
        lo, hi = inside[0], inside[-1]
        f = [cal.frequency(x) for x in grid if lo <= x <= hi]
        steps = [b - a for a, b in zip(f, f[1:])]
        if not (all(s < 0 for s in steps) or all(s > 0 for s in steps)):
            raise ValueError(f"{name}: the tuning curve is not monotonic over "
                             f"{lo:g}-{hi:g} nm")
        return lo, hi

    async def switch_mode(self, mode: str) -> None:
        """Use the other crystal's calibration. Software only: nothing is written;
        sync() then reads the present frequency as a wavelength of the new curve."""
        self._set_mode(mode)
        self.wavelength = None

    # -- conversions ----------------------------------------------------------------------
    def frequency_mhz(self, nm: float) -> float:
        return self.cal.frequency(nm)

    def wavelength_of(self, mhz: float) -> float | None:
        """The wavelength the frequency tunes to on the present curve (None if it
        is outside the calibration's usable range)."""
        lo, hi = self.range_nm
        flo, fhi = self.cal.frequency(lo), self.cal.frequency(hi)
        if not min(flo, fhi) - 1e-6 <= mhz <= max(flo, fhi) + 1e-6:
            return None
        for _ in range(80):                               # bisection: monotonic, checked
            mid = (lo + hi) / 2
            if (self.cal.frequency(mid) - mhz) * (flo - mhz) > 0:
                lo, flo = mid, self.cal.frequency(mid)
            else:
                hi = mid
        return (lo + hi) / 2

    def asf_for(self, nm: float, power: float) -> int:
        return min(round(self.cal.norm(nm) * power / 100), self.cal.norm(nm))

    # -- connect (queries only) -----------------------------------------------------------
    async def sync(self) -> None:
        """Read the channel's present frequency and amplitude back."""
        f_reply = await self.t.query(f"dds f {self.channel}")
        m = re.search(rf"frequency\s+({_NUM})\s*Hz", f_reply, re.I)
        mhz = float(m.group(1)) / 1e6 if m else None
        self.wavelength = self.wavelength_of(mhz) if mhz is not None else None
        a_reply = await self.t.query(f"dds a {self.channel}")
        ints = re.findall(r"\b\d+\b", a_reply.split("mplitude")[-1])
        self.asf = int(ints[-1]) if ints and 0 <= int(ints[-1]) <= self.ASF_MAX else None
        if self.asf is not None and self.wavelength is not None:
            self.power = 100 * self.asf / self.cal.norm(self.wavelength)
        else:
            self.power = 0.0 if self.asf == 0 else None

    # -- settings -------------------------------------------------------------------------
    async def set(self, nm: float) -> None:
        """Movable: the wavelength (nm). With power on, the amplitude follows the new
        band's PowerNorm; the lower of old and new amplitude is written first, so no
        transient goes above either setting."""
        lo, hi = self.range_nm
        if not lo <= nm <= hi:
            raise DriverError(f"{self.name}: {nm:g} nm is outside the {self.cal_name} "
                              f"calibration ({lo:.0f}-{hi:.0f} nm)")
        freq = f"dds f {self.channel} {self.frequency_mhz(nm):.6f}"
        new_asf = self.asf_for(nm, self.power) if self.power else None
        if new_asf is not None and self.asf is not None and new_asf < self.asf:
            await self.t.write(f"dds a {self.channel} {new_asf}")
            await self.t.write(freq)
        else:
            await self.t.write(freq)
            if new_asf is not None:
                await self.t.write(f"dds a {self.channel} {new_asf}")
        self.wavelength = nm
        if new_asf is not None:
            self.asf = new_asf
        if self.settle_s:
            await asyncio.sleep(self.settle_s)

    async def set_power(self, power: float) -> None:
        """Percent of the calibration's optimum power at the present wavelength."""
        if not 0 <= power <= self.max_power:
            raise DriverError(f"{self.name}: power {power:g} % outside 0-{self.max_power:g} %")
        if self.wavelength is None:
            raise DriverError(f"{self.name}: set the wavelength first (the present one is "
                              "unknown or outside the calibration)")
        asf = self.asf_for(self.wavelength, power)
        if asf and not self._enabled:
            await self.t.write("dau en")
            await self.t.write(f"mod dac {self.channel} {self.ASF_MAX}")
            self._enabled = True
        await self.t.write(f"dds a {self.channel} {asf}")
        self.power, self.asf = power, asf
        if self.settle_s:
            await asyncio.sleep(self.settle_s)

    # -- readings (software level) --------------------------------------------------------
    def describe(self) -> dict[str, FieldInfo]:
        return describe(self)

    async def read(self) -> dict[str, float]:
        """What the driver set (or read back on connect): the controller has no
        optical detector. NaN while unknown."""
        nan = math.nan
        wl = self.wavelength
        return {f"{self.name}.wavelength": nan if wl is None else wl,
                f"{self.name}.power": nan if self.power is None else self.power,
                f"{self.name}.rf_frequency": nan if wl is None else self.frequency_mhz(wl)}

    async def snapshot(self) -> dict[str, Any]:
        try:
            serial = await self.t.query("boardid serial")
        except (DriverError, TimeoutError) as e:
            serial = f"? ({e})"
        return {"channel": self.channel, "mode": self.mode, "calibration": self.cal.path,
                "tuning": list(self.cal.tuning), "power_norm": [list(b) for b in self.cal.bands],
                "range_nm": list(self.range_nm), "max_power": self.max_power,
                "wavelength": self.wavelength, "power": self.power, "asf": self.asf,
                "serial": serial}

    async def safe_state(self) -> None:
        """No light: this channel's amplitude to 0 (the frequency is kept)."""
        await self.t.write(f"dds a {self.channel} 0")
        self.power, self.asf = 0.0, 0
