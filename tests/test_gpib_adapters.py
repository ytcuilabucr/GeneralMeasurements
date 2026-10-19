"""Lake Shore 335 / 625 and Giga-tronics 2420B adapters on fake transports.

Reply strings are copied from the real instruments on the lab bus (2026-10-16).
"""

import math

import pytest

from gmcore.config import load_station
from gmcore.devices import (DriverError, FakeTransport, Gigatronics2420B, LakeShore335,
                              LakeShore625)

# -- Lake Shore 335 ----------------------------------------------------------
LS335 = {"*IDN?": "LSCI,MODEL335,LSA1789/#######,1.4", "OUTMODE? 1": "1,1,0",
         "RDGST? A": "000", "KRDG? A": "+77.589", "SETP? 1": "+06.000",
         "HTR? 1": "+000.0", "RAMPST? 1": "0"}


def ls335(replies=None, **kw):
    t = FakeTransport({**LS335, **(replies or {})})
    kw.setdefault("limits", (2.0, 300.0))
    return t, LakeShore335("T", t, **kw)


async def test_ls335_configure_is_read_only():
    t, d = ls335()
    await d.configure()
    assert t.log == ["*IDN?", "OUTMODE? 1"]


@pytest.mark.parametrize(("outmode", "msg"), [("0,1,0", "Off"), ("1,2,0", "input B")])
async def test_ls335_configure_rejects_wrong_loop_setup(outmode, msg):
    _, d = ls335({"OUTMODE? 1": outmode})
    with pytest.raises(DriverError, match=msg):
        await d.configure()


async def test_ls335_read_real_reply_format():
    _, d = ls335()
    assert await d.read() == {"T": 77.589, "T.setpoint": 6.0, "T.heater": 0.0}


async def test_ls335_sensor_fault_reads_nan():
    _, d = ls335({"RDGST? A": "016"})
    assert math.isnan((await d.read())["T"])


async def test_ls335_start_ramp_converts_to_k_per_min():
    t, d = ls335(max_rate=0.1)
    await d.start_ramp(10.0, 2 / 60)
    assert t.log[-2:] == ["RAMP 1,1,2.0", "SETP 1,10.000"]


async def test_ls335_rejects_out_of_limits_and_too_fast():
    _, d = ls335(max_rate=0.1)
    with pytest.raises(DriverError):
        await d.start_ramp(400.0, 0.05)
    with pytest.raises(DriverError):
        await d.start_ramp(10.0, 0.2)


def test_ls335_max_rate_must_fit_instrument():
    with pytest.raises(ValueError):
        LakeShore335("T", FakeTransport(), max_rate=1e-4)   # 0.006 K/min < 0.1


async def test_ls335_set_waits_until_stable():
    temps = iter(["+9.0", "+9.95", "+10.02", "+10.01", "+10.0"])
    t = FakeTransport(lambda c: next(temps) if c == "KRDG? A" else LS335.get(c, "000"))
    d = LakeShore335("T", t, limits=(2, 300), max_rate=0.1, tolerance=0.1, stable_s=0.002,
                     poll_s=0.001)
    await d.set(10.0)
    assert len([c for c in t.log if c == "KRDG? A"]) >= 3


async def test_ls335_set_times_out():
    _, d = ls335(max_rate=0.1, stable_s=0, poll_s=0.001, settle_timeout_s=0.01)
    with pytest.raises(DriverError, match="not stable"):
        await d.set(10.0)                          # reading stays at 77.589 K


async def test_ls335_safe_state_hold_and_off():
    t, d = ls335()
    await d.safe_state()
    assert t.log[-2:] == ["RAMP 1,0,10.0", "SETP 1,6.000"]
    t, d = ls335(safe_action="off")
    await d.safe_state()
    assert t.log == ["RANGE 1,0"]


# -- Lake Shore 625 ----------------------------------------------------------
LS625 = {"*IDN?": "LSCI,MODEL625,LSA179P,1.3/1.1", "LIMIT?": "+49.6000,+5.0000,+1.0000",
         "SETI?": "+00.0000", "RDGI?": "-00.0001", "RDGV?": "-0.0123", "PSH?": "0"}


def ls625(replies=None, **kw):
    t = FakeTransport({**LS625, **(replies or {})})
    kw.setdefault("limits", (-10.0, 10.0))
    kw.setdefault("max_rate", 0.05)
    return t, LakeShore625("B", t, **kw)


async def test_ls625_configure_is_read_only():
    t, d = ls625()
    await d.configure()
    assert t.log == ["*IDN?", "LIMIT?", "SETI?"]


async def test_ls625_configure_rejects_limits_beyond_supply():
    _, d = ls625(limits=(-60.0, 60.0))
    with pytest.raises(DriverError, match="supply limit"):
        await d.configure()
    _, d = ls625(max_rate=2.0)
    with pytest.raises(DriverError, match="A/s"):
        await d.configure()


async def test_ls625_field_units_convert():
    t, d = ls625(limits=(-1.0, 1.0), max_rate=0.01, tesla_per_amp=0.1)
    await d.start_ramp(0.5, 0.01)                 # 0.5 T at 0.01 T/s
    assert t.log[-2:] == ["RATE 0.1000", "SETI 5.0000"]
    r = await d.read()
    assert r["B"] == pytest.approx(-1e-5) and r["B.i"] == pytest.approx(-1e-4)


async def test_ls625_refuses_move_with_switch_heater_off():
    t, d = ls625(persistent_switch=True)
    with pytest.raises(DriverError, match="persistent switch"):
        await d.start_ramp(1.0, 0.05)
    assert not any(c.startswith(("RATE", "SETI ")) for c in t.log)


async def test_ls625_ramp_done_and_set():
    current = iter(["+00.0000", "+00.5000", "+00.9990", "+01.0000"])
    t = FakeTransport(lambda c: next(current) if c == "RDGI?" else LS625[c])
    d = LakeShore625("B", t, limits=(-10, 10), max_rate=0.05, poll_s=0.001)
    await d.set(1.0)
    assert "SETI 1.0000" in t.log


async def test_ls625_safe_state_holds_present_current():
    t, d = ls625({"RDGI?": "+02.5000"})
    await d.safe_state()
    assert t.log[-1] == "SETI 2.5000"
    assert not any(c.startswith("PSH ") for c in t.log)


# -- Giga-tronics 2420B ------------------------------------------------------
GT = {"*IDN?": "Giga-tronics, 2420B, 818003, V4.33b", "FREQ?": "7054000000.0",
      "POW?": "-10.00", "OUTP?": "0", "SYST:ERR?": "0, No error"}


def gt(replies=None, **kw):
    t = FakeTransport({**GT, **(replies or {})})
    return t, Gigatronics2420B("rf", t, **kw)


async def test_gt_configure_rf_off_then_fixed_power():
    t, d = gt(power_dbm=-10.0)
    await d.configure()
    writes = [c for c in t.log if not c.endswith("?")]
    assert writes == ["OUTP OFF", "POW -10.00"]


async def test_gt_configure_refuses_power_above_cap():
    _, d = gt({"POW?": "20.00"})                # what the real unit is set to now
    with pytest.raises(DriverError, match="max_power_dbm"):
        await d.configure()


async def test_gt_set_and_read():
    t, d = gt(limits=(1e9, 1e10))
    await d.set(5e9)
    assert "FREQ 5000000000.0" in t.log
    assert await d.read() == {"rf.freq": 7.054e9, "rf.power": -10.0}


async def test_gt_rejected_command_raises():
    errs = iter(['-222,"Data out of range"', "0, No error"])
    t = FakeTransport(lambda c: next(errs) if c == "SYST:ERR?" else GT[c])
    d = Gigatronics2420B("rf", t, limits=(1e9, 1e10))
    with pytest.raises(DriverError, match="out of range"):
        await d.set(5e9)


def test_gt_power_limits_capped():
    with pytest.raises(ValueError):
        Gigatronics2420B("rf", FakeTransport(), axis="power", limits=(-20, 10), max_power_dbm=0)


async def test_gt_safe_state_rf_off():
    t, d = gt()
    await d.safe_state()
    assert t.log == ["OUTP OFF"]


# -- config ------------------------------------------------------------------
def test_lab_station_validates():
    cfg = load_station("tests/fixtures/lab.toml")
    assert {"temp", "magnet", "rf", "lockin"} <= set(cfg.devices)
