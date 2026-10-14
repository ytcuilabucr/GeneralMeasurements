"""Station config: pydantic discriminated union on `driver`."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Dev(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource: str


class K24xxCfg(_Dev):
    driver: Literal["keithley_24xx"]
    model: Literal["2400", "2401", "2410", "2450"] = "2400"
    mode: Literal["volt", "curr"] = "volt"
    compliance: float
    limits: tuple[float, float]
    max_rate: float
    nplc: float = 1.0


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


DeviceCfg = Annotated[K24xxCfg | K2000Cfg | LockinCfg, Field(discriminator="driver")]


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
