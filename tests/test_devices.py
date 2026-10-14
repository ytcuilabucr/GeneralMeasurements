import pytest

from sweepkit.config import load_station
from sweepkit.devices import (SR830, SR850, DriverError, FakeTransport,
                              Keithley24xx, Keithley2000)


async def test_k24xx_ramps_and_reads():
    t = FakeTransport({":READ?": "1.0,2e-9,0,0,0"})
    k = Keithley24xx("gate", t, "2410", limits=(-5, 5), max_rate=100, step_dt=0.001, settle_s=0)
    await k.set(1.0)
    levels = [c for c in t.log if c.startswith(":SOUR:VOLT:LEV")]
    assert len(levels) > 1 and levels[-1] == ":SOUR:VOLT:LEV 1"
    assert await k.read() == {"gate.v": 1.0, "gate.i": 2e-9}


async def test_k24xx_rejects_out_of_limits():
    k = Keithley24xx("g", FakeTransport(), "2400", limits=(-1, 1))
    with pytest.raises(DriverError):
        await k.set(2.0)


async def test_k24xx_safe_state_zeroes_then_off():
    t = FakeTransport()
    k = Keithley24xx("g", t, "2450", limits=(-1, 1), max_rate=100, step_dt=0.001, settle_s=0)
    await k.set(0.5)
    await k.safe_state()
    assert t.log[-2] == ":SOUR:VOLT:LEV 0" and t.log[-1] == ":OUTP OFF"


def test_k24xx_limits_beyond_model():
    with pytest.raises(ValueError):
        Keithley24xx("g", FakeTransport(), "2401", limits=(-50, 50))


async def test_k2000_read():
    k = Keithley2000("dmm", FakeTransport({":READ?": "+1.234500E-03"}))
    assert (await k.read())["dmm"] == pytest.approx(1.2345e-3)


@pytest.mark.parametrize("cls", [SR830, SR850])
async def test_lockin_snap(cls):
    t = FakeTransport({"SNAP? 1,2,3,4": "1e-3,2e-3,2.2e-3,63.4"})
    r = await cls("li", t).read()
    assert r["li.r"] == pytest.approx(2.2e-3) and r["li.theta"] == pytest.approx(63.4)


async def test_lockin_configure_indices():
    t = FakeTransport()
    await SR830("li", t, frequency=17.77, time_constant=0.3, sensitivity=1e-3).configure()
    assert "FREQ 17.77" in t.log
    assert "OFLT 9" in t.log  # 0.3 s
    assert "SENS 17" in t.log  # 1 mV


def test_example_station_validates():
    cfg = load_station("station.example.toml")
    assert set(cfg.devices) == {"gate", "bias", "dmm", "lockin"}
