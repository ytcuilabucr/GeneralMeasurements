"""Build device objects from a validated StationConfig (opens VISA sessions)."""

from __future__ import annotations

from ..devices import (LM510, SR830, SR850, Gigatronics2420B, Keithley2000, Keithley2400,
                       Keithley2401, Keithley2410, Keithley2450, LakeShore335, LakeShore625,
                       VisaTransport)
from ..devices.sim import SimLockin, SimMagnet, SimSource
from .schema import (DeviceCfg, GT2420BCfg, K2000Cfg, KeithleySMUCfg, LM510Cfg, LockinCfg,
                     LS335Cfg, LS625Cfg, SimLockinCfg, SimMagnetCfg, SimSourceCfg,
                     StationConfig)

DRIVER_CLASSES: dict[str, type] = {
    "keithley_2400": Keithley2400, "keithley_2401": Keithley2401,
    "keithley_2410": Keithley2410, "keithley_2450": Keithley2450,
    "keithley_2000": Keithley2000, "sr830": SR830,
    "sr850": SR850, "lakeshore_335": LakeShore335, "lakeshore_625": LakeShore625,
    "gigatronics_2420b": Gigatronics2420B, "lm510": LM510, "sim_source": SimSource,
    "sim_magnet": SimMagnet, "sim_lockin": SimLockin,
}
# Lake Shore and Cryomagnetics instruments end replies with CR LF; the rest use LF.
_TERMINATION = {"lakeshore_335": "\r\n", "lakeshore_625": "\r\n", "lm510": "\r\n"}


async def build_device(name: str, d: DeviceCfg, others: dict[str, object]) -> object:
    """One device, session open, NOT configured (so nothing is written to it).
    `others`: devices built so far (a sim_lockin needs its sim_source)."""
    if isinstance(d, SimSourceCfg):
        return SimSource(name, d.limits, d.delay)
    if isinstance(d, SimMagnetCfg):
        return SimMagnet(name, d.limits, d.max_rate)
    if isinstance(d, SimLockinCfg):
        src = others.get(d.source)
        if not isinstance(src, SimSource):
            raise ValueError(f"{name}: source {d.source!r} is not a sim_source")
        return SimLockin(name, src, d.gain, d.noise)
    term = _TERMINATION.get(d.driver, "\n")
    t = VisaTransport(d.resource, read_termination=term, write_termination=term,
                      echo=getattr(d, "echo", False))
    dev = _make(name, d, t)              # checks the settings before the bus is touched
    await t.open()
    return dev


def check_settings(cfg: StationConfig) -> None:
    """Build every hardware driver without opening its session, so settings the
    model can't do (limits, compliance, ranges) fail here, with no bus traffic."""
    for name, d in cfg.devices.items():
        if hasattr(d, "resource"):
            try:
                _make(name, d, VisaTransport(d.resource))
            except ValueError as e:
                raise ValueError(f"{name}: {e}") from None


def _make(name: str, d: DeviceCfg, t: VisaTransport) -> object:
    if isinstance(d, KeithleySMUCfg):
        return DRIVER_CLASSES[d.driver](name, t, d.mode, d.compliance, d.limits, d.nplc, source_range=d.source_range,
                                        sense_range=d.sense_range)
    if isinstance(d, K2000Cfg):
        return Keithley2000(name, t, d.function, d.nplc, d.range)
    if isinstance(d, LockinCfg):
        cls = SR830 if d.driver == "sr830" else SR850
        return cls(name, t, d.frequency, d.amplitude, d.time_constant, d.sensitivity)
    if isinstance(d, LS335Cfg):
        return LakeShore335(name, t, d.input, d.loop, d.limits, d.max_rate,
                            d.heater_range, d.tolerance, d.stable_s,
                            d.settle_timeout_s, safe_action=d.safe_action)
    if isinstance(d, LS625Cfg):
        return LakeShore625(name, t, d.limits, d.max_rate, d.tesla_per_amp,
                            d.persistent_switch, d.tolerance_a)
    if isinstance(d, GT2420BCfg):
        return Gigatronics2420B(name, t, d.axis, d.limits, d.power_dbm,
                                d.freq_hz, d.max_power_dbm, d.settle_s)
    if isinstance(d, LM510Cfg):
        return LM510(name, t, d.channel, d.units)
    raise ValueError(f"{name}: no builder for driver {d.driver!r}")


async def build_devices(cfg: StationConfig) -> dict[str, object]:
    out: dict[str, object] = {}
    try:
        # sims that depend on other devices go last
        order = sorted(cfg.devices.items(), key=lambda kv: isinstance(kv[1], SimLockinCfg))
        for name, d in order:
            out[name] = await build_device(name, d, out)
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
