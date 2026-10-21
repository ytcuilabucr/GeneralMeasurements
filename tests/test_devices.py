import re

import pytest

from gmcore.config import load_station
from gmcore.devices import (SR830, SR850, DriverError, FakeTransport, Keithley2000, Keithley2400,
                            Keithley2401, Keithley2410, Keithley2450, VisaTransport)


async def test_keithley_smu_ramps_and_reads():
    t = FakeTransport({":READ?": "1.0,2e-9,0,0,0", ":SENS:CURR:PROT:TRIP?": "0"})
    k = Keithley2410("gate", t, limits=(-5, 5), settle_s=0)
    await k.prepare()
    await k.set(1.0)
    levels = [c for c in t.log if c.startswith(":SOUR:VOLT:LEV")]
    assert len(levels) > 1 and levels[-1] == ":SOUR:VOLT:LEV 1"
    assert await k.read() == {"gate.v": 1.0, "gate.i": 2e-9, "gate.compliance": 0.0}


async def test_keithley_smu_read_with_output_off_fails_fast():
    t = FakeTransport()
    k = Keithley2400("g", t, limits=(-1, 1))
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
    k = Keithley2450("g", t, mode=mode, limits=(-1, 1), compliance=1e-3 if mode == "volt" else 2.0)
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
    await Keithley2450("g", t, mode="curr", limits=(-1e-3, 1e-3), compliance=2.0).configure()
    assert ':SENS:FUNC "VOLT"' in t.log and ":SOUR:CURR:VLIM 2" in t.log
    assert not any(c.startswith(":FORM:ELEM") for c in t.log)


async def test_keithley_smu_rejects_out_of_limits():
    k = Keithley2400("g", FakeTransport(), limits=(-1, 1))
    with pytest.raises(DriverError):
        await k.set(2.0)


async def test_keithley_smu_safe_state_zeroes_then_off():
    t = FakeTransport()
    k = Keithley2450("g", t, limits=(-1, 1), settle_s=0)
    await k.set(0.5)
    await k.safe_state()
    assert t.log[-2] == ":SOUR:VOLT:LEV 0" and t.log[-1] == ":OUTP OFF"


def test_keithley_smu_limits_beyond_model():
    with pytest.raises(ValueError):
        Keithley2401("g", FakeTransport(), limits=(-50, 50))


# -- model differences (datasheets / manuals; see the model modules) ---------------
def test_keithley_smu_models_have_their_own_ranges():
    t = FakeTransport()
    assert Keithley2400("a", t).source_ranges == [0.2, 2.0, 20.0, 200.0]
    assert Keithley2401("a", t).source_ranges == [0.2, 2.0, 20.0]           # no 200 V
    assert Keithley2410("a", t).source_ranges == [0.2, 2.0, 20.0, 1000.0]
    assert Keithley2450("a", t).source_ranges == [0.02, 0.2, 2.0, 20.0, 200.0]
    assert Keithley2410("a", t).sense_ranges[4] == 0.02                     # 20 mA, not 10 mA
    assert Keithley2450("a", t).sense_ranges[0] == 1e-8                     # 10 nA
    assert Keithley2400("a", t, "curr", 1.0, (-1e-3, 1e-3)).sense_ranges == [0.2, 2.0, 20.0,
                                                                             200.0]
    for cls in (Keithley2400, Keithley2401, Keithley2410, Keithley2450):   # independent
        assert cls.__mro__ == (cls, object) and cls.DEFINITION == f"keithley_{cls.MODEL}"


@pytest.mark.parametrize(("cls", "kw", "error"), [
    (Keithley2400, dict(limits=(-100, 100), compliance=0.5), "limited to ±0.105 A"),
    (Keithley2410, dict(limits=(-500, 500), compliance=0.05), "limited to ±0.021 A"),
    (Keithley2410, dict(mode="curr", limits=(-0.1, 0.1), compliance=100), "limited to ±21 V"),
    (Keithley2400, dict(compliance=2.0), "compliance 2 outside"),
    (Keithley2400, dict(compliance=1e-12), "compliance 1e-12 outside"),
    (Keithley2400, dict(nplc=20), "NPLC 20 outside"),
    (Keithley2400, dict(source_range=5.0), "not a volt range"),
    (Keithley2401, dict(source_range=200.0), "not a volt range"),
])
def test_keithley_smu_model_limits_are_checked_before_any_io(cls, kw, error):
    with pytest.raises(ValueError, match=re.escape(error)):
        cls("g", FakeTransport(), **kw)


def test_keithley_smu_power_envelope_allows_the_rated_corners():
    Keithley2400("g", FakeTransport(), limits=(-200, 200), compliance=0.1)
    Keithley2410("g", FakeTransport(), limits=(-1000, 1000), compliance=0.02)
    Keithley2450("g", FakeTransport(), "curr", 20.0, limits=(-1, 1))


async def test_keithley_smu_source_range_only_with_output_off_and_must_fit():
    t = FakeTransport({":SYST:ERR?": "0,No error"})
    k = Keithley2400("g", t, limits=(-1, 1), settle_s=0)
    await k.output(True)
    with pytest.raises(DriverError, match="output off"):
        await k.set_source_range(2.0)
    await k.output(False)
    with pytest.raises(DriverError, match="limits"):
        await k.set_source_range(0.2)           # ±1 V limits don't fit 210 mV
    assert await k.set_source_range(2.0) == 2.0
    assert ":SOUR:VOLT:RANG 2" in t.log
    assert await k.set_source_range(None) is None and ":SOUR:VOLT:RANG:AUTO ON" in t.log


async def test_keithley_smu_set_refuses_levels_beyond_a_fixed_range():
    t = FakeTransport({":SYST:ERR?": "0,No error"})
    k = Keithley2450("g", t, limits=(-5, 5), settle_s=0)
    k._source_range = 2.0                        # as if fixed (2 V, ±2.1 V)
    with pytest.raises(DriverError, match="doesn't fit"):
        await k.set(3.0)
    assert not any(c.startswith(":SOUR:VOLT:LEV") for c in t.log)


async def test_keithley_smu_sense_range_and_effective_compliance():
    t = FakeTransport({":SYST:ERR?": "0,No error", ":SENS:CURR:RANG:AUTO?": "0",
                       ":SENS:CURR:RANG?": "1.000000E-05"})
    k = Keithley2400("g", t, compliance=1e-3, limits=(-1, 1))
    assert await k.set_sense_range(1e-5) == 1e-5 and ":SENS:CURR:RANG 1e-05" in t.log
    assert k.effective_compliance == pytest.approx(1.05e-5)   # range compliance
    assert await k.get_sense_range() == 1e-5
    await k.set_sense_range(None)
    assert k.effective_compliance == 1e-3
    tiny = Keithley2400("g", t, compliance=1e-6, limits=(-1, 1))
    with pytest.raises(DriverError, match=re.escape("below 0.1%")):
        await tiny.set_sense_range(1.0)         # 1 µA is under 0.1 % of the 1 A range
    t2 = FakeTransport({":SYST:ERR?": "0,No error", ":SOUR:VOLT:ILIM?": "1.05E-05"})
    k2 = Keithley2450("g", t2, compliance=1e-3, limits=(-1, 1))
    await k2.set_sense_range(1e-5)
    assert k2.compliance == pytest.approx(1.05e-5)           # the 2450 lowered its limit


async def test_keithley_smu_range_errors_from_the_unit_are_raised():
    replies = iter(['-221,"Settings conflict"', "0,No error"])
    t = FakeTransport(lambda c: next(replies) if c == ":SYST:ERR?" else "0")
    k = Keithley2410("g", t, limits=(-1, 1))
    with pytest.raises(DriverError, match="Settings conflict"):
        await k.set_sense_range(1e-3)


async def test_keithley_smu_configure_applies_station_ranges():
    t = FakeTransport({":SYST:ERR?": "0,No error"})
    k = Keithley2401("g", t, "curr", 2.0, (-1e-3, 1e-3), source_range=1e-3, sense_range=2.0)
    await k.configure()
    assert ":SOUR:CURR:RANG 0.001" in t.log and ":SENS:VOLT:RANG 2" in t.log
    assert (k._source_range, k._sense_range) == (1e-3, 2.0)


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
    cfg = load_station("tests/fixtures/example.toml")
    assert set(cfg.devices) == {"gate", "bias", "dmm", "lockin"}


# -- other instruments: settings checked against their manuals ------------------------
def test_k2000_ranges_follow_the_function():
    from gmcore.devices.definitions import choices, definition_of
    k = Keithley2000("dmm", FakeTransport(), "curr")
    assert k.ranges == [0.01, 0.1, 1.0, 3.0]
    rng = definition_of(k).properties["range"]
    assert choices(rng, k)[:2] == [("Auto", None), (None, 0.01)]
    with pytest.raises(ValueError, match="not a volt range"):
        Keithley2000("dmm", FakeTransport(), "volt", range_=2.0)
    with pytest.raises(ValueError, match=re.escape("NPLC 0.001 outside")):
        Keithley2000("dmm", FakeTransport(), nplc=0.001)


async def test_k2000_set_range_refuses_a_non_range():
    k = Keithley2000("dmm", FakeTransport(), "res")
    with pytest.raises(DriverError, match="not a res range"):
        await k.set_range(5e3)


@pytest.mark.parametrize(("kw", "error"), [
    (dict(amplitude=0.001), "amplitude"), (dict(amplitude=6), "amplitude"),
    (dict(frequency=2e5), "frequency"), (dict(frequency=0.0001), "frequency")])
def test_lockin_station_values_within_the_manual(kw, error):
    with pytest.raises(ValueError, match=error):
        SR830("li", FakeTransport(), **kw)


async def test_lockin_time_constant_is_read_back():
    t = FakeTransport({"OFLT?": "13"})                 # the unit kept 30 s
    li = SR850("li", t)
    with pytest.raises(DriverError, match="30 s"):
        await li.set_time_constant(100.0)             # asked for index 14
    assert li.time_constant == 30.0
    assert await SR830("li", FakeTransport({"OFLT?": "8"})).set_time_constant(0.1) == 0.1


async def test_ls335_output2_in_voltage_mode_is_on_off_only():
    from gmcore.devices import LakeShore335
    t = FakeTransport({"HTRSET? 2": "1,1,2,0.000,1"})
    ls = LakeShore335("temp", t, "B", 2, (2, 300), 0.03)
    with pytest.raises(DriverError, match="voltage mode"):
        await ls.set_heater_range(3)
    await ls.set_heater_range(1)
    assert t.log[-1] == "RANGE 2,1"
    one = LakeShore335("temp", FakeTransport(), "A", 1, (2, 300), 0.03)
    await one.set_heater_range(3)                      # output 1: always current mode
    assert "HTRSET? 2" not in one.t.log


@pytest.mark.parametrize(("kw", "error"), [
    (dict(limits=(-70, 70), max_rate=0.1), "60.1 A"),
    (dict(limits=(-1, 1), max_rate=200), "99.999"),
    (dict(limits=(-1, 1), max_rate=1e-6, tesla_per_amp=0.1), "0.0001")])
def test_ls625_limits_from_the_manual(kw, error):
    from gmcore.devices import LakeShore625
    with pytest.raises(ValueError, match=re.escape(error)):
        LakeShore625("m", FakeTransport(), **kw)


async def test_ls625_refuses_a_rate_it_would_round_to_zero():
    from gmcore.devices import LakeShore625
    m = LakeShore625("m", FakeTransport(), (-1, 1), 0.01)
    with pytest.raises(DriverError, match="below"):
        await m.start_ramp(0.5, 0.00004)
    assert not any(c.startswith(("RATE", "SETI")) for c in m.t.log)


def test_2420_frequency_span():
    from gmcore.devices import Gigatronics2420B
    with pytest.raises(ValueError, match="frequency"):
        Gigatronics2420B("rf", FakeTransport(), "freq", (1e6, 1e9))
    with pytest.raises(ValueError, match="frequency"):
        Gigatronics2420B("rf", FakeTransport(), "power", (-30, -10), freq_hz=25e9)
    Gigatronics2420B("rf", FakeTransport(), "freq", (10e6, 20e9))


def test_validate_catches_what_the_model_cant_do(tmp_path, capsys):
    from gmcore import cli
    bad = tmp_path / "bad.toml"
    bad.write_text('[station]\nname = "x"\n[devices.hv]\ndriver = "keithley_2401"\n'
                   'resource = "GPIB0::1::INSTR"\ncompliance = 1e-6\nlimits = [-50.0, 50.0]\n',
                   "utf-8")
    assert cli.main(["validate", str(bad)]) == 1
    assert "2401 range" in capsys.readouterr().err


async def test_lockin_reference_settings_are_set_and_read_back():
    state = {"FREQ": "1000", "SLVL": "0.1", "PHAS": "0", "FMOD": "1", "HARM": "1"}

    class Unit(FakeTransport):                      # a lock-in that keeps what it is sent
        async def write(self, cmd):
            await super().write(cmd)
            k, _, v = cmd.partition(" ")
            if k == "PHAS":                          # the unit wraps the phase to ±180
                v = f"{(float(v) + 180) % 360 - 180:.2f}"
            state[k] = v
    t = Unit(lambda c: state.get(c.rstrip("?"), "0"))
    li = SR830("li", t)
    assert await li.set_frequency(1234.5) == 1234.5 and "FREQ 1234.5" in t.log
    assert await li.set_amplitude(0.5) == 0.5
    assert await li.set_phase(541.0) == -179.0        # the manual's example, wrapped
    with pytest.raises(DriverError, match=re.escape("outside 0.004")):
        await li.set_amplitude(6.0)                    # the .toml's max: nothing sent
    assert not any(c.startswith("SLVL 6") for c in t.log)
    state["HARM"] = "100"
    with pytest.raises(DriverError, match="above 102 kHz"):
        await li.set_frequency(2000.0)
    state["FMOD"] = "0"                               # external reference
    with pytest.raises(DriverError, match="external"):
        await li.set_frequency(10.0)
    state["SLVL"] = "0.2"                             # the unit kept another value

    async def ignore(cmd):
        t.log.append(cmd)
    t.write = ignore
    with pytest.raises(DriverError, match=re.escape("the lock-in has 0.2 V")):
        await li.set_amplitude(1.0)


async def test_sr850_numbers_the_reference_source_its_own_way():
    """SR850 FMOD: 0 internal, 1 internal sweep, 2 external (SR830: 0 external,
    1 internal). The SR830's numbering refused every SR850 frequency set."""
    state = {"FREQ": "1000", "FMOD": "0", "HARM": "1"}

    class Unit(FakeTransport):
        async def write(self, cmd):
            await super().write(cmd)
            k, _, v = cmd.partition(" ")
            state[k] = v
    t = Unit(lambda c: state.get(c.rstrip("?"), "0"))
    li = SR850("li", t)
    assert await li.set_frequency(137.0) == 137.0 and "FREQ 137" in t.log
    for fmod, what in (("1", "internal sweep"), ("2", "external")):
        state["FMOD"] = fmod
        with pytest.raises(DriverError, match=f"source is {what};"):
            await li.set_frequency(10.0)
    assert state["FREQ"] == "137"                      # nothing sent when refused
