"""Build device objects from a validated StationConfig (opens VISA sessions)."""

from __future__ import annotations

from ..devices import SR830, SR850, Keithley24xx, Keithley2000, VisaTransport
from .schema import K2000Cfg, K24xxCfg, LockinCfg, StationConfig


async def build_devices(cfg: StationConfig) -> dict[str, object]:
    out: dict[str, object] = {}
    for name, d in cfg.devices.items():
        t = VisaTransport(d.resource)
        await t.open()
        if isinstance(d, K24xxCfg):
            out[name] = Keithley24xx(name, t, d.model, d.mode, d.compliance,
                                     d.limits, d.max_rate, d.nplc)
        elif isinstance(d, K2000Cfg):
            out[name] = Keithley2000(name, t, d.function, d.nplc, d.range)
        elif isinstance(d, LockinCfg):
            cls = SR830 if d.driver == "sr830" else SR850
            out[name] = cls(name, t, d.frequency, d.amplitude, d.time_constant, d.sensitivity)
    return out
