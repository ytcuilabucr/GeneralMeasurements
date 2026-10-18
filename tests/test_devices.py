import pytest

from generalmeasurements.config import load_station
from generalmeasurements.devices import (SR830, SR850, DriverError, FakeTransport,
                              Keithley24xx, Keithley2000, VisaTransport)


async def test_k24xx_ramps_and_reads():
    t = FakeTransport({":READ?": "1.0,2e-9,0,0,0", ":SENS:CURR:PROT:TRIP?": "0"})
    k = Keithley24xx("gate", t, "2410", limits=(-5, 5), max_rate=100, step_dt=0.001, settle_s=0)
    await k.prepare()
    await k.set(1.0)
    levels = [c for c in t.log if c.startswith(":SOUR:VOLT:LEV")]
    assert len(levels) > 1 and levels[-1] == ":SOUR:VOLT:LEV 1"
    assert await k.read() == {"gate.v": 1.0, "gate.i": 2e-9, "gate.compliance": 0.0}


async def test_k24xx_read_with_output_off_fails_fast():
    t = FakeTransport()
    k = Keithley24xx("g", t, "2400", limits=(-1, 1))
    with pytest.raises(DriverError, match="output is off"):
        await k.read()
    assert t.log == []                           # nothing sent: the unit would never reply


@pytest.mark.parametrize(("mode", "reply", "v", "i"), [
    ("volt", "1.0,2e-9", 1.0, 2e-9),     # source V, sensed I
    ("curr", "1e-6,0.5", 0.5, 1e-6),     # source I, sensed V
])
async def test_k2450_reads_source_and_sense(mode, reply, v, i):
    trip = ":SOUR:VOLT:ILIM:TRIP?" if mode == "volt" else ":SOUR:CURR:VLIM:TRIP?"
    t = FakeTransport({':READ? "defbuffer1", SOUR, READ': reply, trip: "0"})
    k = Keithley24xx("g", t, "2450", mode=mode, limits=(-1, 1))
    await k.output(True)
    assert await k.read() == {"g.v": v, "g.i": i, "g.compliance": 0.0}


class _FakeVisaInst:
    """Stands in for a pyvisa resource: the first query times out."""

    def __init__(self):
        self.cleared, self.calls = 0, 0

    def query(self, cmd):
        import pyvisa
        self.calls += 1
        if self.calls == 1:
            raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
        return "KEITHLEY INSTRUMENTS INC.,MODEL 2000\n"

    def clear(self):
        self.cleared += 1


async def test_visa_timeout_clears_device_and_raises_timeouterror():
    t = VisaTransport("GPIB0::11::INSTR")
    t._inst = inst = _FakeVisaInst()
    with pytest.raises(TimeoutError, match="timed out"):    # retryable by RetryPolicy
        await t.query(":READ?")
    assert inst.cleared == 1                                 # late reply flushed
    assert await t.query("*IDN?") == "KEITHLEY INSTRUMENTS INC.,MODEL 2000"


async def test_k2450_configure_senses_opposite_quantity():
    t = FakeTransport({":SYST:ERR?": '0,"No error"'})
    await Keithley24xx("g", t, "2450", mode="curr", limits=(-1e-3, 1e-3)).configure()
    assert ':SENS:FUNC "VOLT"' in t.log and ":SOUR:CURR:VLIM 1e-06" in t.log
    assert not any(c.startswith(":FORM:ELEM") for c in t.log)


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
