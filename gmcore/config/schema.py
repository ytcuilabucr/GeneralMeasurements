"""Station config: pydantic discriminated union on `driver`."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..devices.transport import serial_resource


class _Dev(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource: str
    window: str | None = None        # GUI window to dock in (default: by instrument type)


class KeithleySMUModeCfg(BaseModel):
    """Settings for the other source function, so the GUI can switch to it."""
    model_config = ConfigDict(extra="forbid")
    compliance: float
    limits: tuple[float, float]


class KeithleySMUCfg(_Dev):
    """Keithley 2400 / 2401 / 2410 / 2450 (one driver per model). The model's
    ranges and limits, and the NPLC bounds of its definition, are checked when
    the device is built. The level is set in one step: there is no ramp rate."""
    driver: Literal["keithley_2400", "keithley_2401", "keithley_2410", "keithley_2450"]
    mode: Literal["volt", "curr"] = "volt"
    compliance: float
    limits: tuple[float, float]
    nplc: float = 1.0
    source_range: float | None = None     # a fixed range of the sourced quantity; None = auto
    sense_range: float | None = None      # ... of the measured (compliance) quantity
    modes: dict[Literal["volt", "curr"], KeithleySMUModeCfg] = {}   # [devices.X.modes.curr]


class K2000Cfg(_Dev):
    driver: Literal["keithley_2000"]
    function: Literal["volt", "curr", "res", "fres"] = "volt"
    nplc: float = 1.0
    range: float | None = None


class LockinCfg(_Dev):
    driver: Literal["sr830", "sr850"]
    frequency: float | None = None
    amplitude: float = 0.1
    time_constant: float = 0.1
    sensitivity: float = 1e-3


class LS335Cfg(_Dev):
    driver: Literal["lakeshore_335"]
    input: Literal["A", "B"] = "A"
    loop: Literal[1, 2] = 1
    limits: tuple[float, float]
    max_rate: float                  # K/s (the 335 takes 0.1-100 K/min)
    heater_range: Literal[0, 1, 2, 3] | None = None
    tolerance: float = 0.1           # K
    stable_s: float = 60.0
    settle_timeout_s: float = 3600.0
    safe_action: Literal["hold", "off"] = "hold"


class LS625Cfg(_Dev):
    driver: Literal["lakeshore_625"]
    limits: tuple[float, float]      # A, or T when tesla_per_amp is set
    max_rate: float                  # A/s or T/s
    tesla_per_amp: float | None = None
    persistent_switch: bool = False
    tolerance_a: float = 0.002


class GT2420BCfg(_Dev):
    driver: Literal["gigatronics_2420b"]
    axis: Literal["freq", "power"] = "freq"
    limits: tuple[float, float]      # Hz, or dBm for axis = "power"
    power_dbm: float | None = None   # fixed level while sweeping frequency
    freq_hz: float | None = None     # fixed frequency while sweeping power
    max_power_dbm: float = 0.0
    settle_s: float = 0.05


class LM510Cfg(_Dev):
    driver: Literal["lm510"]
    channel: Literal[1, 2] = 1
    units: Literal["in", "cm", "%"] = "in"
    echo: bool = True                # the meter echoes commands on RS-232


class AotfCfg(_Dev):
    """Crystal Technology octal AOTF controller (Fianium), one RF channel, over
    USB through the vendor's 32-bit AotfLibrary.dll (msl-loadlib).
    resource = "AOTF::<n>": the controller's USB instance, 0 for the first.
    mode picks the calibration: the visible or the NIR crystal. There are no
    wavelength limits: each calibration's usable range comes from the file."""
    driver: Literal["aotf_octal"]
    channel: int = Field(0, ge=0, le=7)
    mode: Literal["vis", "nir"] = "vis"
    # .aotf files ([Calibration] tuning0-3 and PowerNorm), one per mode
    calibrations: dict[Literal["vis", "nir"], str]
    max_power: float = 100.0         # % of the calibration's optimum amplitude
    settle_s: float = 0.0
    dll: str = r"C:\Program Files (x86)\Fianium\config files\AotfLibrary.dll"

    @field_validator("resource")
    @classmethod
    def _aotf_resource(cls, v: str) -> str:
        if not v.upper().startswith("AOTF::") or not v[6:].isdigit():
            raise ValueError('resource must be "AOTF::<instance>", e.g. "AOTF::0"')
        return v


class _SerialDev(_Dev):
    """A serial instrument: resource "COM<n>" or "ASRL<n>::INSTR" (stored as the latter)."""

    @field_validator("resource")
    @classmethod
    def _serial_resource(cls, v: str) -> str:
        return serial_resource(v)


class YslAotfCfg(_SerialDev):
    """YSL Photonics AOTF RF driver, one line. No wavelength limits: the usable
    range is where the calibration cubic stays in 20-200 MHz."""
    driver: Literal["ysl_aotf"]
    # f [MHz] = a·λ³ + b·λ² + c·λ + d, λ in nm (the vendor's Aotf_control.py)
    calibration: dict[Literal["a", "b", "c", "d"], float] = {
        "a": -2.09537e-7, "b": 6.67185e-4, "c": -0.76660, "d": 363.79358}
    max_power: float = 100.0         # % cap
    settle_s: float = 0.0


class YslScPro8Cfg(_SerialDev):
    """YSL Photonics SC-PRO8 supercontinuum laser."""
    driver: Literal["ysl_sc_pro8"]
    max_power: float = 100.0         # % cap


class _Sim(BaseModel):
    """Simulated devices: no resource, for dry runs and offline development."""
    model_config = ConfigDict(extra="forbid")
    window: str | None = None


class SimSourceCfg(_Sim):
    driver: Literal["sim_source"]
    limits: tuple[float, float] = (-10.0, 10.0)
    delay: float = 0.0


class SimMagnetCfg(_Sim):
    driver: Literal["sim_magnet"]
    limits: tuple[float, float] = (-9.0, 9.0)
    max_rate: float = 0.5


class SimLockinCfg(_Sim):
    driver: Literal["sim_lockin"]
    source: str                      # role name of the SimSource it measures
    gain: float = 1e-3
    noise: float = 1e-6


_CFGS = (KeithleySMUCfg, K2000Cfg, LockinCfg, LS335Cfg, LS625Cfg, GT2420BCfg, LM510Cfg,
         AotfCfg, YslAotfCfg, YslScPro8Cfg, SimSourceCfg, SimMagnetCfg, SimLockinCfg)
DeviceCfg = Annotated[
    KeithleySMUCfg | K2000Cfg | LockinCfg | LS335Cfg | LS625Cfg | GT2420BCfg | LM510Cfg
    | AotfCfg | YslAotfCfg | YslScPro8Cfg | SimSourceCfg | SimMagnetCfg | SimLockinCfg,
    Field(discriminator="driver")]
# driver name -> its config class
DRIVERS: dict[str, type[BaseModel]] = {
    d: c for c in _CFGS for d in get_args(c.model_fields["driver"].annotation)}
# settings for an instrument whose driver has just been chosen: small limits, slow rates
DRIVER_DEFAULTS: dict[str, dict] = {
    **{f"keithley_{m}": {"mode": "volt", "compliance": 1e-6, "limits": (-1.0, 1.0)}
       for m in ("2400", "2401", "2410", "2450")},
    "lakeshore_335": {"limits": (2.0, 300.0), "max_rate": 1 / 60},
    "lakeshore_625": {"limits": (-1.0, 1.0), "max_rate": 0.01},
    "gigatronics_2420b": {"limits": (1e9, 2e10), "power_dbm": -20.0, "max_power_dbm": 0.0},
    "aotf_octal": {"mode": "vis",
                   "calibrations": {"vis": "Instrument config/aotf/102102 Visible.aotf",
                                    "nir": "Instrument config/aotf/102102 NIR1.aotf"},
                   "max_power": 10.0},
    "ysl_aotf": {"max_power": 10.0},
    "ysl_sc_pro8": {"max_power": 20.0},
    "sim_lockin": {"source": ""},
}


def check_name(name: str) -> str:
    """An instrument's name, as used in plans and data keys (`<name>.v`)."""
    if not name.strip() or name != name.strip():
        raise ValueError(f"name {name!r}: empty, or spaces at either end")
    if bad := set(name) & set("./\\\"'"):
        raise ValueError(f"name {name!r}: no {' '.join(sorted(bad))} (data keys and HDF5 "
                         "paths use . and /)")
    return name


class Station(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    shutdown_order: list[str] = []


class StationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    station: Station
    devices: dict[str, DeviceCfg]

    @field_validator("devices")
    @classmethod
    def _names(cls, v: dict[str, DeviceCfg]) -> dict[str, DeviceCfg]:
        for name in v:
            check_name(name)
        return v


def load_station(path: str | Path) -> StationConfig:
    cfg = StationConfig.model_validate(tomllib.loads(Path(path).read_text("utf-8")))
    missing = set(cfg.station.shutdown_order) - set(cfg.devices)
    if missing:
        raise ValueError(f"shutdown_order names unknown devices: {sorted(missing)}")
    return cfg
