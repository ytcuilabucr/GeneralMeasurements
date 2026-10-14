"""Build device objects from a validated StationConfig (opens VISA sessions)."""

from __future__ import annotations

from ..devices import SR830, SR850, Keithley24xx, Keithley2000, VisaTransport
from ..devices.sim import SimLockin, SimMagnet, SimSource
from .schema import (K2000Cfg, K24xxCfg, LockinCfg, SimLockinCfg, SimMagnetCfg,
                     SimSourceCfg, StationConfig)


async def build_devices(cfg: StationConfig) -> dict[str, object]:
    out: dict[str, object] = {}
    try:
        for name, d in cfg.devices.items():
            if isinstance(d, SimSourceCfg):
                out[name] = SimSource(name, d.limits, d.delay)
            elif isinstance(d, SimMagnetCfg):
                out[name] = SimMagnet(name, d.limits, d.max_rate)
            elif isinstance(d, SimLockinCfg):
                continue                      # second pass: needs its source
            else:
                t = VisaTransport(d.resource)
                await t.open()
                if isinstance(d, K24xxCfg):
                    out[name] = Keithley24xx(name, t, d.model, d.mode, d.compliance,
                                             d.limits, d.max_rate, d.nplc)
                elif isinstance(d, K2000Cfg):
                    out[name] = Keithley2000(name, t, d.function, d.nplc, d.range)
                elif isinstance(d, LockinCfg):
                    cls = SR830 if d.driver == "sr830" else SR850
                    out[name] = cls(name, t, d.frequency, d.amplitude,
                                    d.time_constant, d.sensitivity)
        for name, d in cfg.devices.items():
            if isinstance(d, SimLockinCfg):
                src = out.get(d.source)
                if not isinstance(src, SimSource):
                    raise ValueError(f"{name}: source {d.source!r} is not a sim_source")
                out[name] = SimLockin(name, src, d.gain, d.noise)
        for name, dev in out.items():
            configure = getattr(dev, "configure", None)
            if configure:
                await configure()
    except BaseException:
        await close_devices(out)
        raise
    return out


async def close_devices(devices: dict[str, object]) -> None:
    for dev in devices.values():
        t = getattr(dev, "t", None)
        if t is not None:
            try:
                await t.close()
            except Exception:  # noqa: BLE001
                pass
