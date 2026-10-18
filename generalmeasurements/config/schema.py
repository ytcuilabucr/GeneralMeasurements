"""Station config: pydantic discriminated union on `driver`."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Dev(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource: str
    window: str | None = None        # GUI window to dock in (default: by instrument type)


class K24xxModeCfg(BaseModel):
    """Settings for the other source function, so the GUI can switch to it."""
    model_config = ConfigDict(extra="forbid")
    compliance: float
    limits: tuple[float, float]
    max_rate: float


class K24xxCfg(_Dev):
    driver: Literal["keithley_24xx"]
    model: Literal["2400", "2401", "2410", "2450"] = "2400"
    mode: Literal["volt", "curr"] = "volt"
    compliance: float
    limits: tuple[float, float]
    max_rate: float
    nplc: float = 1.0
    modes: dict[Literal["volt", "curr"], K24xxModeCfg] = {}   # [devices.X.modes.curr]


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


DeviceCfg = Annotated[
    K24xxCfg | K2000Cfg | LockinCfg | LS335Cfg | LS625Cfg | GT2420BCfg | LM510Cfg
    | SimSourceCfg | SimMagnetCfg | SimLockinCfg,
    Field(discriminator="driver")]


class Station(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    shutdown_order: list[str] = []


class StationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    station: Station
    devices: dict[str, DeviceCfg]


def load_station(path: str | Path) -> StationConfig:
    cfg = StationConfig.model_validate(tomllib.loads(Path(path).read_text("utf-8")))
    missing = set(cfg.station.shutdown_order) - set(cfg.devices)
    if missing:
        raise ValueError(f"shutdown_order names unknown devices: {sorted(missing)}")
    return cfg
